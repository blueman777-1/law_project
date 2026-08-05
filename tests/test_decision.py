"""결정문(심판례·증선위 의결) 파서 테스트.

조문 파서와 분리된 경로다 — 결정문에는 조·항 구조가 없다.
픽스처는 실제 응답을 축약한 것이며, 실측된 함정을 그대로 담고 있다
(HTML 혼입 / 태그가 아닌 꺾쇠 / 본문에 비어 있는 사건번호 / 없는 날짜 필드).
"""
from pathlib import Path

import pytest

from lawrag.parser import (
    SFC,
    TAX_TRIBUNAL,
    chunk_decision,
    decision_field,
    parse_decision,
    parse_decision_list,
    strip_html,
)

FIXTURES = Path(__file__).parent / "fixtures"
TT_LIST = (FIXTURES / "decision_tt_list.xml").read_bytes()
TT_DETAIL = (FIXTURES / "decision_tt_detail.xml").read_bytes()
SFC_DETAIL = (FIXTURES / "decision_sfc_detail.xml").read_bytes()


# --- HTML 걷어내기 ------------------------------------------------------------
# 태그명 화이트리스트로만 지운다. <[^>]*> 로 지우면 sfc 의 실제 내용이 날아간다.

def test_strips_real_html_tags():
    assert strip_html("<p>가.</p><span>나.</span>") == "가. 나."


def test_strips_table_and_anchor_markup():
    out = strip_html('<table><tbody><tr><td>구분</td></tr></tbody></table><a href="http://x">링크</a>')
    assert "<" not in out and "구분" in out and "링크" in out


def test_keeps_angle_brackets_that_are_not_tags():
    # sfc 의 소제목이다. 지우면 문서 구조가 사라진다.
    assert strip_html("< 외부감사인 >") == "< 외부감사인 >"


def test_keeps_masked_company_name():
    # 마스킹된 상호. 'oo' 는 HTML 태그가 아니다.
    assert "<OO>" in strip_html("ㅇ <OO> 회계법인")


def test_keeps_table_reference_in_angle_brackets():
    assert "<표 1>" in strip_html("명세는 <표 1>과 같다.")


def test_drops_blank_lines():
    assert strip_html("가.\n\n\n나.") == "가.\n나."


# --- 목록 파싱 ----------------------------------------------------------------

def test_list_yields_id_and_case_number():
    assert parse_decision_list(TT_LIST, TAX_TRIBUNAL)[0] == ("2038667", "조심 2024서3564")


def test_list_reads_every_item():
    assert len(parse_decision_list(TT_LIST, TAX_TRIBUNAL)) == 2


# --- 본문 파싱 ----------------------------------------------------------------

@pytest.fixture(scope="module")
def tt():
    # 사건번호는 본문에 없어서 목록 값을 넘겨야 한다.
    return parse_decision(TT_DETAIL, TAX_TRIBUNAL, "조심 2024서3564")


def test_article_no_comes_from_serial_number(tt):
    # 순번을 매기면 재적재 때 다른 결정문을 덮어쓴다.
    assert tt.no == 2038667 and tt.branch == 0


def test_label_comes_from_the_list(tt):
    assert tt.label == "조심 2024서3564"


def test_title_is_the_case_name(tt):
    assert tt.title == "법인세 과세처분의 당부"


def test_decision_date_is_used_as_enforced(tt):
    assert tt.enforced == "20241021"


def test_body_gathers_every_content_field(tt):
    for expected in ("심판청구를 기각한다.", "청구법인의 주장", "국세기본법", "처분개요"):
        assert expected in tt.body


def test_body_has_no_html_left(tt):
    assert "<p>" not in tt.body and "<td>" not in tt.body


def test_body_keeps_non_tag_angle_brackets(tt):
    assert "<표 1>" in tt.body


def test_empty_fields_are_skipped(tt):
    # 청구취지·따른결정은 비어 있다. 빈 섹션 머리글이 남으면 안 된다.
    assert "[청구취지]" not in tt.body


def test_hash_tracks_the_body(tt):
    from lawrag.parser import Article
    assert tt.sha256 == Article.hash_body(tt.body)


def test_sfc_body_is_full_text_not_metadata():
    d = parse_decision(SFC_DETAIL, SFC, "의결 제2022-39호")
    assert "지정제외점수" in d.body and "지적사항" in d.body


def test_sfc_keeps_its_section_headings():
    d = parse_decision(SFC_DETAIL, SFC, "의결 제2022-39호")
    assert "< 외부감사인 >" in d.body


def test_sfc_has_no_date():
    # 목록에도 본문에도 날짜 필드가 없다.
    assert parse_decision(SFC_DETAIL, SFC, "의결 제2022-39호").enforced == ""


def test_image_only_field_leaves_nothing(tt):
    d = parse_decision(SFC_DETAIL, SFC, "의결 제2022-39호")
    assert "flDownload" not in d.body


def test_wrong_root_is_rejected():
    # 없는 ID 를 부르면 <Law> 안내문이 온다. lstrmRltJo 와 같은 함정이다.
    missing = '<?xml version="1.0"?><Law>일치하는 결정례가 없습니다.</Law>'.encode()
    with pytest.raises(ValueError, match="본문 응답이 아닙니다"):
        parse_decision(missing, TAX_TRIBUNAL, "조심 1")


def test_decision_field_reads_metadata():
    # 세목은 본문에만 있다. 적재 범위를 좁히는 데 쓴다.
    assert decision_field(TT_DETAIL, "세목") == "법인"


# --- 청킹 --------------------------------------------------------------------
# 조문 청킹과 완전히 분리돼 있다. 결정문에는 항번호 원문자가 없다.

def test_short_decision_is_one_chunk(tt):
    assert len(chunk_decision("조세심판원 결정례", tt)) == 1


def test_header_carries_source_label_and_title(tt):
    chunk = chunk_decision("조세심판원 결정례", tt)[0]
    assert chunk.header == "조세심판원 결정례 조심 2024서3564(법인세 과세처분의 당부)"


def test_every_chunk_repeats_the_header(tt):
    from lawrag.parser import Article
    long = Article(no=1, branch=0, title="긴 결정", body="\n".join(["가나다라마"] * 400),
                   enforced="20240101", sha256="x", label_text="조심 2024서1")
    chunks = chunk_decision("조세심판원 결정례", long)
    assert len(chunks) > 1
    assert all(c.text.startswith(c.header) for c in chunks)


def test_chunks_respect_max_chars(tt):
    from lawrag.parser import Article
    long = Article(no=1, branch=0, title="긴 결정", body="\n".join(["가나다라마"] * 400),
                   enforced="20240101", sha256="x", label_text="조심 2024서1")
    bodies = [c.text[len(c.header):] for c in chunk_decision("조세심판원 결정례", long, max_chars=300)]
    assert all(len(b) <= 320 for b in bodies)


def test_single_long_line_is_split():
    # 조문과 다른 규칙이다. 항 경계가 없으니 통째로 둘 수 없다.
    from lawrag.parser import Article
    one_line = Article(no=1, branch=0, title="", body="가" * 2500,
                       enforced="", sha256="x", label_text="조심 2024서1")
    assert len(chunk_decision("조세심판원 결정례", one_line, max_chars=900)) >= 3


def test_parts_are_numbered(tt):
    from lawrag.parser import Article
    long = Article(no=1, branch=0, title="긴 결정", body="\n".join(["가나다라마"] * 400),
                   enforced="20240101", sha256="x", label_text="조심 2024서1")
    chunks = chunk_decision("조세심판원 결정례", long)
    assert [c.part for c in chunks] == list(range(len(chunks)))
    assert all(c.n_parts == len(chunks) for c in chunks)


def test_empty_body_yields_no_chunks():
    from lawrag.parser import Article
    empty = Article(no=1, branch=0, title="빈 결정", body="", enforced="", sha256="x",
                    label_text="조심 2024서1")
    assert chunk_decision("조세심판원 결정례", empty) == []


# --- 날짜 표기 ----------------------------------------------------------------
# 결정문에 '시행'을 찍으면 사실과 다르다. 법률 도구에서 그냥 두면 안 된다.

def searchhit(source_type="article", enforced="20250401"):
    from lawrag.db import SearchHit
    return SearchHit(chunk_id=1, law_name="법인세법", label="제25조", header="h", text="t",
                     enforced=enforced, source_type=source_type,
                     vec_rank=1, kw_rank=1, term_rank=None, rrf=0.03)


def test_article_shows_enforcement_date():
    assert searchhit().dated == "시행 20250401"


def test_decision_shows_decision_date():
    assert searchhit("decision", "20241021").dated == "결정 20241021"


def test_missing_date_shows_nothing():
    # 증선위 의결에는 날짜 필드가 아예 없다.
    assert searchhit("decision", "").dated == ""


# --- 조문 경로에 영향이 없어야 한다 --------------------------------------------

def test_article_label_is_unchanged_without_override():
    from lawrag.parser import Article
    a = Article(no=18, branch=2, title="x", body="y", enforced="", sha256="z")
    assert a.label == "제18조의2"
