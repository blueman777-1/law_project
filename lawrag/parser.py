"""lawService.do XML → 조문 → 청크 → 검색 토큰.

청크 단위를 조(條)로 잡는 이유: 고정 길이로 자르면 "제8조 제2항"이 두 청크로
갈려 인용이 불가능해진다. 900자를 넘으면 항(項) 경계에서만 자른다.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

# 항번호로 쓰이는 원문자. 청크는 이 문자로 시작하는 지점에서만 잘린다.
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

_WORD = re.compile(r"[0-9A-Za-z가-힣]+")
_HANGUL = re.compile(r"[가-힣]+")


@dataclass(frozen=True)
class Law:
    law_id: str
    name: str
    law_type: str
    dept: str
    promulgated: str  # YYYYMMDD
    enforced: str  # YYYYMMDD


@dataclass(frozen=True)
class Article:
    no: int
    branch: int  # 가지번호. 제18조의2 면 2, 없으면 0
    title: str
    body: str
    enforced: str
    sha256: str
    # 결정문처럼 조 구조가 없는 자료용. 비어 있으면 조문 규칙대로 '제N조' 를 만든다.
    label_text: str = ""

    @staticmethod
    def hash_body(body: str) -> str:
        """개정 감지용. 본문이 그대로면 재임베딩을 건너뛴다."""
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @property
    def label(self) -> str:
        if self.label_text:
            return self.label_text
        return f"제{self.no}조의{self.branch}" if self.branch else f"제{self.no}조"


@dataclass(frozen=True)
class Chunk:
    header: str
    text: str
    part: int
    n_parts: int


def _text(el: ET.Element | None, tag: str) -> str:
    if el is None:
        return ""
    found = el.find(tag)
    return (found.text or "").strip() if found is not None else ""


def _int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def _blocks(unit: ET.Element) -> list[str]:
    """조문을 '자를 수 있는 최소 단위'들로 쪼갠다.

    blocks[0] 은 조문내용(제목 줄 또는 본문 인라인), 그 뒤는 항 하나씩.
    항내용에는 이미 항번호(①)가 포함돼 있으므로 따로 붙이지 않는다.
    항번호가 비어 있고 호만 있는 조문(예: 제2조 정의)도 있다.
    """
    out = [_text(unit, "조문내용")]
    for para in unit.findall("항"):
        lines = [_text(para, "항내용")]
        lines += [_text(ho, "호내용") for ho in para.findall("호")]
        block = "\n".join(line for line in lines if line)
        if block:
            out.append(block)
    return [b for b in out if b]


def parse_law(xml: bytes) -> tuple[Law, list[Article]]:
    root = ET.fromstring(xml)
    basic = root.find("기본정보")
    law = Law(
        law_id=_text(basic, "법령ID"),
        name=_text(basic, "법령명_한글"),
        law_type=_text(basic, "법종구분"),
        dept=_text(basic, "소관부처"),
        promulgated=_text(basic, "공포일자"),
        enforced=_text(basic, "시행일자"),
    )

    articles: list[Article] = []
    for unit in root.findall(".//조문단위"):
        # 조문여부='전문' 은 조문이 아니라 장 제목("제1장 총칙")이다. 걸러내지 않으면
        # 청크에 알맹이 없는 헤더가 섞여 검색 품질이 떨어진다.
        if _text(unit, "조문여부") != "조문":
            continue
        body = "\n".join(_blocks(unit))
        articles.append(
            Article(
                no=_int(_text(unit, "조문번호")),
                branch=_int(_text(unit, "조문가지번호")),
                title=_text(unit, "조문제목"),
                body=body,
                enforced=_text(unit, "조문시행일자") or law.enforced,
                sha256=Article.hash_body(body),
            )
        )
    return law, articles


def chunk_article(law_name: str, article: Article, max_chars: int = 900) -> list[Chunk]:
    """조 단위 청킹. 900자를 넘으면 항 경계에서만 자른다.

    항 하나가 통째로 max_chars 를 넘어도 쪼개지 않는다 — 인용 가능성이
    청크 크기 균일함보다 중요하다.
    """
    header = f"{law_name} {article.label}({article.title})" if article.title else f"{law_name} {article.label}"
    blocks = _split_blocks(article.body)

    parts: list[str] = []
    current: list[str] = []
    for block in blocks:
        splittable = bool(current) and block.startswith(tuple(CIRCLED))
        if splittable and len("\n".join(current)) + len(block) > max_chars:
            parts.append("\n".join(current))
            current = [block]
        else:
            current.append(block)
    if current:
        parts.append("\n".join(current))

    return [
        Chunk(header=header, text=f"{header}\n{part}", part=i, n_parts=len(parts))
        for i, part in enumerate(parts)
    ]


def _split_blocks(body: str) -> list[str]:
    """본문을 항 경계로 되돌린다. 항번호로 시작하는 줄이 새 블록의 시작."""
    blocks: list[str] = []
    for line in body.split("\n"):
        if blocks and not line.startswith(tuple(CIRCLED)):
            blocks[-1] += "\n" + line
        else:
            blocks.append(line)
    return blocks


# --- 결정문 (심판례·의결서) ---------------------------------------------------
# 조문 파서와 분리돼 있다. 결정문에는 조·항 구조가 없어 위의 규칙이 하나도 안 맞는다.


@dataclass(frozen=True)
class DecisionSource:
    """결정문 자료원 하나. 두 자료원의 프로토콜은 같고 필드명만 다르다.

    목록 → `ID` → 본문 순서로 받는다 (법령의 `MST` 가 아니라 `ID` 다).
    """
    target: str
    law_name: str
    list_tag: str  # 목록 응답의 반복 단위
    detail_root: str  # 본문 응답의 루트 태그. 없는 ID 는 <Law> 로 오므로 가드가 필요하다
    id_field: str
    label_field: str  # 사건번호. **목록에만** 있다 — 본문 쪽은 늘 비어 있다
    title_field: str
    date_field: str  # 없는 자료원도 있다 (sfc)
    body_fields: tuple[str, ...]


TAX_TRIBUNAL = DecisionSource(
    target="ttSpecialDecc",
    law_name="조세심판원 결정례",
    list_tag="decc",
    detail_root="SpecialDeccService",
    id_field="특별행정심판재결례일련번호",
    label_field="청구번호",
    title_field="사건명",
    date_field="의결일자",
    body_fields=("재결요지", "주문", "청구취지", "이유", "관련법령"),
)

SFC = DecisionSource(
    target="sfc",
    law_name="증권선물위원회 의결",
    list_tag="sfc",
    detail_root="SfcService",
    id_field="결정문일련번호",
    label_field="의결번호",
    title_field="안건명",
    date_field="",  # 목록에도 본문에도 날짜가 없다
    body_fields=("조치대상자의인적사항", "조치내용", "조치이유"),
)

# 본문에 진짜 HTML 이 섞여 있다(tt: p·span·table·td·tr·a, sfc: img).
# 그런데 sfc 에는 **태그가 아닌 꺾쇠가 내용**으로 들어 있다 — '< 외부감사인 >' 같은
# 소제목과 마스킹된 상호 '<OO>'. `<[^>]*>` 로 지우면 그게 통째로 날아간다.
# 그래서 실제로 관측된 태그명만 지운다.
_HTML_TAGS = (
    "p", "span", "div", "br", "table", "tbody", "thead", "tr", "td", "th",
    "a", "img", "ul", "ol", "li", "strong", "em", "b", "i", "u", "font",
    "h1", "h2", "h3", "h4", "h5", "h6",
)
_HTML = re.compile(r"</?(?:" + "|".join(_HTML_TAGS) + r")(?:\s[^>]*)?/?>", re.I)


def strip_html(text: str) -> str:
    """본문의 HTML 태그만 걷어낸다. 태그가 아닌 꺾쇠는 내용이라 건드리지 않는다."""
    lines = (" ".join(line.split()) for line in _HTML.sub(" ", text).split("\n"))
    return "\n".join(line for line in lines if line)


def decision_field(xml: bytes, name: str) -> str:
    """본문에서 필드 하나만 꺼낸다. 세목처럼 적재 범위를 좁히는 메타데이터용이다."""
    return _text(ET.fromstring(xml), name)


def parse_decision_list(xml: bytes, source: DecisionSource) -> list[tuple[str, str]]:
    """목록에서 (일련번호, 사건번호) 를 뽑는다.

    사건번호를 여기서 챙겨야 한다 — 본문 응답의 사건번호·청구번호는 늘 비어 있다.
    """
    return [
        (ident, _text(item, source.label_field))
        for item in ET.fromstring(xml).findall(source.list_tag)
        if (ident := _text(item, source.id_field))
    ]


def parse_decision(xml: bytes, source: DecisionSource, label: str) -> Article:
    """결정문 본문 → Article.

    `article_no` 에 응답의 일련번호를 그대로 쓴다. 순번을 매기면 재적재 때 순서가
    달라지는 순간 다른 결정문을 덮어쓴다 — upsert 키가 (법령, 조번호, 가지번호)다.
    """
    root = ET.fromstring(xml)
    if root.tag != source.detail_root:
        raise ValueError(
            f"{source.target} 본문 응답이 아닙니다: <{root.tag}> "
            f"{(root.text or '').strip()[:60]}"
        )
    blocks = [
        f"[{field}]\n{value}"
        for field in source.body_fields
        if (value := strip_html(_text(root, field)))
    ]
    body = "\n\n".join(blocks)
    return Article(
        no=_int(_text(root, source.id_field)),
        branch=0,
        title=_text(root, source.title_field),
        body=body,
        enforced=_text(root, source.date_field) if source.date_field else "",
        sha256=Article.hash_body(body),
        label_text=label,
    )


def chunk_decision(law_name: str, article: Article, max_chars: int = 900) -> list[Chunk]:
    """결정문 청킹. 줄 경계에서 자르고, 한 줄이 통째로 길면 그 줄도 자른다.

    조문 청킹(`chunk_article`)과 규칙이 다르다. 조문은 인용 가능성 때문에 항이
    아무리 길어도 쪼개지 않지만, 결정문에는 인용 단위가 될 항 경계가 없다.
    """
    if not article.body.strip():
        return []

    header = (
        f"{law_name} {article.label}({article.title})"
        if article.title else f"{law_name} {article.label}"
    )
    pieces: list[str] = []
    for line in article.body.split("\n"):
        if len(line) > max_chars:
            pieces.extend(line[i : i + max_chars] for i in range(0, len(line), max_chars))
        else:
            pieces.append(line)

    parts: list[str] = []
    current: list[str] = []
    for piece in pieces:
        if current and len("\n".join(current)) + len(piece) > max_chars:
            parts.append("\n".join(current))
            current = [piece]
        else:
            current.append(piece)
    if current:
        parts.append("\n".join(current))

    return [
        Chunk(header=header, text=f"{header}\n{part}", part=i, n_parts=len(parts))
        for i, part in enumerate(parts)
    ]


def bigram_tokens(text: str) -> str:
    """한국어 키워드 검색용 토큰열 → to_tsvector('simple') 에 넣는다.

    PostgreSQL 기본 FTS 는 한국어를 공백 단위로만 끊어서 '납세의무자' 안의
    '납세'를 못 찾는다. mecab-ko 의존성을 피하려고 문자 bigram 을 쓴다.
    단어 원형도 함께 넣는 이유는 '제3조' 같은 식별자를 정확히 잡기 위해서다
    (벡터 검색이 가장 약한 지점).
    """
    out: list[str] = []
    for word in _WORD.findall(text):
        out.append(word)
        for run in _HANGUL.findall(word):
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return " ".join(out)
