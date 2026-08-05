"""국가법령정보 OPEN API (DRF) 클라이언트.

현재는 `target=law` 만 지원한다. 심판례·행정규칙·부처별 해석의 target 코드는
공식 가이드에서 확인되지 않아 넣지 않았다 — 추정해서 넣으면 조용히 빈 결과를
적재하게 된다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import requests

from .config import DRF_SLEEP, LAW_OC

SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
SERVICE_URL = "https://www.law.go.kr/DRF/lawService.do"

# 이 헤더가 없으면 안내 페이지로 돌려보내진다.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://www.law.go.kr/",
}


class DrfError(RuntimeError):
    pass


@dataclass(frozen=True)
class TermLink:
    """법령용어가 쓰인 조문 하나. `조번호`/`조가지번호` 로 article 과 맞춘다."""

    law_name: str
    article_no: int
    branch_no: int
    is_core: bool  # 용어구분코드 140101(핵심용어). 검색 축의 정렬 기준


@dataclass(frozen=True)
class LawHit:
    mst: str  # 법령일련번호. 본문 조회 키
    law_id: str
    name: str
    law_type: str
    enforced: str


def ensure_xml(body: bytes) -> bytes:
    """인증 실패 시 서버는 HTTP 200 에 HTML 안내 페이지를 실어 보낸다.

    이걸 걸러내지 않으면 조문 0개짜리 법령이 조용히 적재된다.
    """
    if not body or not body.lstrip().startswith(b"<?xml"):
        head = body[:120].decode("utf-8", "replace") if body else "(빈 응답)"
        raise DrfError(
            f"XML 이 아닌 응답을 받았습니다. LAW_OC 승인 상태를 확인하세요.\n  받은 내용: {head}"
        )
    return body


def _check_result_code(root: ET.Element) -> None:
    code = (root.findtext("resultCode") or "").strip()
    if code and code != "00":
        msg = (root.findtext("resultMsg") or "").strip()
        raise DrfError(f"API 오류 resultCode={code} {msg}")


def parse_search_results(body: bytes) -> list[LawHit]:
    root = ET.fromstring(ensure_xml(body))
    _check_result_code(root)
    hits = []
    for law in root.findall("law"):
        hits.append(
            LawHit(
                mst=(law.findtext("법령일련번호") or "").strip(),
                law_id=(law.findtext("법령ID") or "").strip(),
                name=(law.findtext("법령명한글") or "").strip(),
                law_type=(law.findtext("법령구분명") or "").strip(),
                enforced=(law.findtext("시행일자") or "").strip(),
            )
        )
    return hits


def parse_term_articles(body: bytes) -> list[TermLink]:
    """lstrmRltJo 응답 → 그 법령용어가 쓰인 조문 목록.

    같은 조문이 핵심용어·선정용어로 두 번 실려 오는 일이 있다. 합치지 않으면
    검색 축 리스트에 같은 조문이 중복돼 순위가 왜곡되므로, 조문 단위로 묶고
    한 번이라도 핵심용어면 핵심용어로 본다.
    """
    root = ET.fromstring(ensure_xml(body))
    if root.tag != "lstrmRltJoService":
        # 일치하는 용어가 없어도 서버는 HTTP 200 + XML 을 준다(루트 태그가 <Law>).
        # ensure_xml 은 이걸 통과시키므로 여기서 걸러야 한다.
        raise DrfError(f"연계조문 응답이 아닙니다: <{root.tag}> {(root.text or '').strip()[:60]}")

    def number(tag: str, unit: ET.Element) -> int:
        # '0025' → 25, '00' → 0. 비어 있으면 0.
        raw = (unit.findtext(tag) or "").strip()
        return int(raw) if raw.isdigit() else 0

    merged: dict[tuple[str, int, int], bool] = {}
    for unit in root.iter("연계법령"):
        key = (
            (unit.findtext("법령명") or "").strip(),
            number("조번호", unit),
            number("조가지번호", unit),
        )
        is_core = (unit.findtext("용어구분코드") or "").strip() == "140101"
        merged[key] = merged.get(key, False) or is_core
    return [
        TermLink(law_name=name, article_no=no, branch_no=branch, is_core=core)
        for (name, no, branch), core in merged.items()
    ]


class DrfClient:
    def __init__(self, oc: str = LAW_OC, sleep: float = DRF_SLEEP):
        if not oc:
            raise DrfError("LAW_OC 가 비어 있습니다. .env 를 확인하세요.")
        self.oc = oc
        self.sleep = sleep
        self._session = requests.Session()
        self._session.headers.update(HEADERS)
        self._last_call = 0.0

    def _get(self, url: str, params: dict, timeout: int = 30) -> bytes:
        # 호출 간격을 지킨다. 짧은 시간 내 과도한 호출은 이용 제한 대상이다.
        wait = self.sleep - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        response = self._session.get(
            url, params={"OC": self.oc, "type": "XML", **params}, timeout=timeout
        )
        self._last_call = time.monotonic()
        response.raise_for_status()
        return ensure_xml(response.content)

    def search_law(self, query: str, display: int = 20) -> list[LawHit]:
        body = self._get(SEARCH_URL, {"target": "law", "query": query, "display": display})
        return parse_search_results(body)

    def fetch_law(self, mst: str) -> bytes:
        """법령 본문 XML. parser.parse_law 에 그대로 넘긴다."""
        return self._get(SERVICE_URL, {"target": "law", "MST": mst})

    def search_decisions(self, target: str, page: int = 1, display: int = 100, **filters) -> bytes:
        """결정문 목록 XML. `display` 최대는 100 이다 — 200 을 줘도 100 만 온다.

        `filters` 로 넘기는 값이 틀리면 **에러 없이 조용히 무시된다.**
        (`rslYd` 의 `~` 를 퍼센트 인코딩하면 필터가 통째로 사라진다.)
        호출부가 `totalCnt` 로 좁혀졌는지 확인해야 한다.
        """
        return self._get(
            SEARCH_URL,
            {"target": target, "display": display, "page": page, **filters},
            timeout=180,
        )

    def fetch_decision(self, target: str, ident: str) -> bytes:
        """결정문 본문 XML. 조회 키가 법령의 `MST` 가 아니라 `ID` 다."""
        return self._get(SERVICE_URL, {"target": target, "ID": ident}, timeout=180)

    def fetch_term_articles(self, term: str) -> list[TermLink]:
        """법령용어가 쓰인 조문 목록. `query` 는 부분일치가 아니라 정확일치다.

        응답이 크다(`사업연도` 는 3MB·1,139건). 검색 때마다 부를 것이 아니라
        `refresh-terms` 로 미리 받아 DB 에 넣어 둔다. 기본 30초로는 실제로
        read timeout 이 났다.
        """
        return parse_term_articles(
            self._get(SERVICE_URL, {"target": "lstrmRltJo", "query": term}, timeout=180)
        )
