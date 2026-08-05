"""파서·청킹·토큰화 테스트.

픽스처는 실제 lawService.do 응답(법인세법 MST=280349)에서 대표 조문만 추린 것이다.
경계 케이스 6종: 전문(장 제목) / 본문 인라인 / 본문이 항에만 있음 / 가지번호 /
900자 초과 / 항 아래 호 구조.
"""
from pathlib import Path

import pytest

from lawrag.parser import (
    Article,
    bigram_tokens,
    chunk_article,
    parse_law,
)

FIXTURE = Path(__file__).parent / "fixtures" / "lawService_sample.xml"


@pytest.fixture(scope="module")
def parsed():
    return parse_law(FIXTURE.read_bytes())


@pytest.fixture(scope="module")
def law(parsed):
    return parsed[0]


@pytest.fixture(scope="module")
def articles(parsed):
    return parsed[1]


@pytest.fixture(scope="module")
def by_label(articles):
    return {a.label: a for a in articles}


# --- 법령 메타데이터 ---------------------------------------------------------

def test_law_metadata(law):
    assert law.name == "법인세법"
    assert law.law_id == "001563"
    assert law.law_type == "법률"
    assert law.enforced == "20260101"
    assert law.promulgated == "20251223"


# --- 전문(장 제목) 필터링 — 이걸 놓치면 청크에 쓰레기가 섞인다 ----------------

def test_chapter_headings_are_excluded(articles):
    """픽스처의 조문단위는 6개지만 그중 1개는 조문여부='전문'(제1장 총칙)."""
    assert len(articles) == 5


def test_no_article_body_starts_with_chapter_marker(articles):
    for a in articles:
        assert not a.body.lstrip().startswith("제1장")


# --- 본문 추출 ---------------------------------------------------------------

def test_body_inline_in_article_content(by_label):
    """제1조는 조문내용에 본문이 그대로 들어있다."""
    a = by_label["제1조"]
    assert a.title == "목적"
    assert "이 법은 법인세의 과세" in a.body


def test_body_collected_from_paragraphs(by_label):
    """제3조는 조문내용이 '제3조(납세의무자)' 뿐이고 실제 본문은 항에 있다."""
    a = by_label["제3조"]
    assert a.title == "납세의무자"
    assert "법인세를 납부할 의무가 있다" in a.body
    assert len(a.body) > 200, "항 본문이 누락되면 길이가 확 줄어든다"


def test_paragraph_numbers_preserved(by_label):
    """항번호(①②)가 보존돼야 '제3조 제1항' 식 인용이 가능하다."""
    assert "①" in by_label["제3조"].body


# --- 가지번호 (제N조의M) -----------------------------------------------------

def test_branch_number_label(by_label):
    a = by_label["제18조의2"]
    assert a.no == 18
    assert a.branch == 2
    assert a.title == "내국법인 수입배당금액의 익금불산입"


def test_plain_article_has_zero_branch(by_label):
    assert by_label["제1조"].branch == 0


# --- 해시 (개정 감지용) ------------------------------------------------------

def test_sha256_is_stable_and_distinct(by_label):
    a = by_label["제1조"]
    assert a.sha256 == Article.hash_body(a.body)
    assert len({x.sha256 for x in by_label.values()}) == len(by_label)


# --- 청킹 -------------------------------------------------------------------

def test_short_article_is_single_chunk(by_label):
    chunks = chunk_article("법인세법", by_label["제1조"])
    assert len(chunks) == 1


def test_long_article_is_split(by_label):
    """제29조는 1800자 이상 → 900자 기준으로 분할돼야 한다."""
    a = by_label["제29조"]
    assert len(a.body) > 900
    chunks = chunk_article("법인세법", a, max_chars=900)
    assert len(chunks) > 1


def test_every_chunk_carries_the_header(by_label):
    """청크 단독으로 의미가 살도록 '법령명 제N조(제목)' 헤더를 앞에 붙인다."""
    a = by_label["제18조의2"]
    for c in chunk_article("법인세법", a):
        assert c.header == "법인세법 제18조의2(내국법인 수입배당금액의 익금불산입)"
        assert c.text.startswith(c.header)


def test_chunks_are_numbered(by_label):
    chunks = chunk_article("법인세법", by_label["제29조"], max_chars=900)
    assert [c.part for c in chunks] == list(range(len(chunks)))
    assert all(c.n_parts == len(chunks) for c in chunks)


def test_split_happens_on_paragraph_boundaries(by_label):
    """항 중간을 자르면 '제N조 제M항' 인용이 불가능해진다."""
    chunks = chunk_article("법인세법", by_label["제29조"], max_chars=900)
    for c in chunks[1:]:
        first_line = c.text[len(c.header):].strip().splitlines()[0]
        assert first_line.startswith(("①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩")), (
            f"항 경계가 아닌 곳에서 잘렸다: {first_line[:40]!r}"
        )


def test_chunk_text_preserves_all_body_content(by_label):
    """분할해도 본문이 유실되면 안 된다."""
    a = by_label["제29조"]
    joined = "".join(
        c.text[len(c.header):] for c in chunk_article("법인세법", a, max_chars=900)
    )
    assert joined.replace("\n", "").replace(" ", "") == a.body.replace("\n", "").replace(" ", "")


# --- 한국어 bigram 토큰화 ----------------------------------------------------

def test_bigram_of_hangul_word():
    assert bigram_tokens("납세의무자") == "납세의무자 납세 세의 의무 무자"


def test_single_char_word_kept():
    assert bigram_tokens("법") == "법"


def test_article_number_kept_whole(by_label):
    """벡터 검색은 조번호에 약하다. 키워드 축이 '제3조'를 정확히 잡아야 한다."""
    assert "제3조" in bigram_tokens("제3조(납세의무자)").split()


def test_punctuation_is_dropped():
    assert "(" not in bigram_tokens("제3조(납세의무자)")


def test_empty_input():
    assert bigram_tokens("") == ""
