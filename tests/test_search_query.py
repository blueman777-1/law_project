"""질의 → tsquery 변환 테스트.

한국어를 문자 bigram 으로 쪼개면 '법인' 같은 조각이 코퍼스 대부분(93%)에 나타난다.
이걸 그대로 OR 로 묶으면 ts_rank_cd 가 노이즈 밀도로 순위를 매긴다 —
실제로 키워드 축 단독 Recall@5 가 0.333 까지 떨어졌다.
그래서 문서빈도가 높은 토큰을 질의에서 걸러낸다.
"""
from lawrag.db import MAX_DF_RATIO, discriminative_tokens, to_tsquery_or


# --- 문서빈도 필터 ------------------------------------------------------------

def test_drops_tokens_above_threshold():
    df = {"법인": 930, "세율": 30}
    assert discriminative_tokens(["법인", "세율"], df, n_docs=1000, max_ratio=0.10) == ["세율"]


def test_keeps_token_exactly_at_threshold():
    df = {"기간": 100}
    assert discriminative_tokens(["기간"], df, n_docs=1000, max_ratio=0.10) == ["기간"]


def test_unknown_token_is_kept():
    """코퍼스에 없는 토큰은 df=0 이므로 변별력이 있다고 본다."""
    assert discriminative_tokens(["없는말"], {}, n_docs=1000, max_ratio=0.10) == ["없는말"]


def test_falls_back_when_everything_is_filtered():
    """전부 흔한 토큰이면 빈 질의를 만들지 말고 원래 토큰을 쓴다."""
    df = {"법인": 930, "인세": 880}
    tokens = ["법인", "인세"]
    assert discriminative_tokens(tokens, df, n_docs=1000, max_ratio=0.10) == tokens


def test_order_is_preserved():
    df = {"가": 1, "나": 2, "다": 3}
    assert discriminative_tokens(["다", "가", "나"], df, n_docs=1000, max_ratio=0.5) == ["다", "가", "나"]


def test_default_ratio_is_ten_percent():
    assert MAX_DF_RATIO == 0.10


# --- tsquery 조립 -------------------------------------------------------------

def test_builds_or_query():
    """AND 로 묶으면 bigram 하나만 어긋나도 0건이 된다."""
    assert to_tsquery_or("납세의무자") == "납세의무자 | 납세 | 세의 | 의무 | 무자"


def test_deduplicates_tokens():
    q = to_tsquery_or("법인 법인")
    assert q.split(" | ").count("법인") == 1


def test_applies_df_filter_when_given():
    df = {"법인세법": 5, "법인": 930, "인세": 880, "세법": 900}
    q = to_tsquery_or("법인세법", df=df, n_docs=1000)
    assert q == "법인세법"


def test_without_df_keeps_everything():
    """df 통계가 아직 없으면(적재 직후 등) 필터 없이 동작해야 한다."""
    assert to_tsquery_or("법인세법").count("|") == 3


def test_empty_query():
    assert to_tsquery_or("") == ""


def test_punctuation_only_query():
    assert to_tsquery_or("!!! ???") == ""
