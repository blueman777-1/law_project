"""DRF 클라이언트의 네트워크 없는 부분만 테스트한다.

실제 호출은 이용 제한이 걸릴 수 있으므로 테스트에서 하지 않는다.
"""
from pathlib import Path

import pytest

from lawrag.drf_client import (
    DrfError,
    ensure_xml,
    parse_search_results,
    parse_term_articles,
)

FIXTURE = Path(__file__).parent / "fixtures" / "lawSearch_sample.xml"
RLTJO_FIXTURE = Path(__file__).parent / "fixtures" / "lstrmRltJo_sample.xml"


def test_parses_search_results():
    hits = parse_search_results(FIXTURE.read_bytes())
    assert len(hits) == 3
    names = [h.name for h in hits]
    assert "법인세법" in names


def test_search_result_carries_mst_and_type():
    hits = parse_search_results(FIXTURE.read_bytes())
    main = next(h for h in hits if h.name == "법인세법")
    assert main.mst == "280349"
    assert main.law_type == "법률"
    assert main.enforced.isdigit() and len(main.enforced) == 8


# 인증 실패·미승인 시 서버가 HTTP 200 에 HTML 안내 페이지를 실어 보낸다.
# 이걸 XML 로 알고 파싱하면 조문 0개짜리 법령이 조용히 적재된다.

def test_html_response_is_rejected():
    with pytest.raises(DrfError):
        ensure_xml(b"<!DOCTYPE html><html><body>\xec\x9d\xb8\xec\xa6\x9d</body></html>")


def test_empty_response_is_rejected():
    with pytest.raises(DrfError):
        ensure_xml(b"")


def test_xml_response_passes_through():
    raw = FIXTURE.read_bytes()
    assert ensure_xml(raw) is raw


def test_xml_with_leading_whitespace_passes():
    assert ensure_xml(b"\n  <?xml version='1.0'?><LawSearch/>")


def test_error_result_code_is_rejected():
    """resultCode 가 00 이 아니면 실패다."""
    with pytest.raises(DrfError):
        parse_search_results(
            b"<?xml version='1.0'?><LawSearch><resultCode>01</resultCode>"
            b"<resultMsg>fail</resultMsg></LawSearch>"
        )


# --- lstrmRltJo (법령용어 → 조문 연계) ---


def test_parses_term_articles():
    links = parse_term_articles(RLTJO_FIXTURE.read_bytes())
    assert [(l.law_name, l.article_no, l.branch_no) for l in links] == [
        ("법인세법", 25, 0),
        ("법인세법", 75, 5),
        ("법인세법 시행령", 42, 0),
        ("소득세법", 35, 0),
    ]


def test_term_article_marks_core_term():
    """용어구분코드 140101(핵심용어) 여부가 검색 축의 정렬 기준이 된다."""
    links = {(l.law_name, l.article_no): l.is_core
             for l in parse_term_articles(RLTJO_FIXTURE.read_bytes())}
    assert links[("법인세법", 25)] is True
    assert links[("법인세법 시행령", 42)] is False


def test_same_article_listed_twice_is_merged_as_core():
    """같은 조문이 핵심용어·선정용어로 중복 등장한다. 핵심용어 쪽이 이긴다.

    픽스처의 법인세법 제25조가 그 경우다 — 합치지 않으면 조문 하나가
    축3 리스트에 두 번 들어가 순위가 왜곡된다.
    """
    links = parse_term_articles(RLTJO_FIXTURE.read_bytes())
    art25 = [l for l in links if l.law_name == "법인세법" and l.article_no == 25]
    assert len(art25) == 1
    assert art25[0].is_core is True


# 결과가 없어도 서버는 HTTP 200 + XML 을 준다. 루트 태그가 다르므로
# ensure_xml() 은 통과시킨다 — 여기서 걸러야 빈 연계를 조용히 적재하지 않는다.

def test_no_result_page_is_rejected():
    with pytest.raises(DrfError):
        parse_term_articles(
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<Law>일치하는 법령용어가 없습니다.</Law>".encode("utf-8")
        )
