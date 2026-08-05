"""일상용어 → 법령용어 질의 확장 테스트.

검색 실패 문항이 전부 용어 격차였다. '접대비'는 코퍼스에 0건이고 법령은
'기업업무추진비'(19청크)를 쓴다 — 법이 용어를 개정했기 때문이다.
정적 임베딩도 bigram 검색도 이 도약을 못 하므로 질의 단계에서 덧붙인다.
"""
from lawrag.terms import expand_query, expansion_terms, load_aliases

ALIASES = {
    "접대비": ["기업업무추진비"],
    "적자": ["결손금", "이월결손금"],
    "회계기간": ["사업연도"],
}


def test_appends_legal_term():
    out = expand_query("접대비 한도", ALIASES)
    assert "기업업무추진비" in out


def test_keeps_original_wording():
    """원래 표현을 지우면 안 된다 — 그 말이 그대로 쓰인 조문도 있을 수 있다."""
    assert "접대비" in expand_query("접대비 한도", ALIASES)


def test_appends_all_variants():
    out = expand_query("작년 적자", ALIASES)
    assert "결손금" in out and "이월결손금" in out


def test_no_match_returns_unchanged():
    assert expand_query("감사인 독립성", ALIASES) == "감사인 독립성"


def test_does_not_duplicate_existing_term():
    """이미 법령용어를 쓴 질의에 같은 말을 또 붙이지 않는다."""
    out = expand_query("기업업무추진비와 접대비", ALIASES)
    assert out.count("기업업무추진비") == 1


def test_multiple_aliases_in_one_query():
    out = expand_query("적자와 회계기간", ALIASES)
    assert "결손금" in out and "사업연도" in out


def test_empty_query():
    assert expand_query("", ALIASES) == ""


def test_empty_alias_table_is_noop():
    assert expand_query("접대비 한도", {}) == "접대비 한도"


# --- 연계조문 축에 넘길 법령용어 ---------------------------------------------

def test_expansion_terms_lists_added_legal_terms():
    assert expansion_terms("작년 적자", ALIASES) == ["결손금", "이월결손금"]


def test_expansion_terms_empty_when_no_alias_fires():
    assert expansion_terms("감사인 독립성", ALIASES) == []


def test_expansion_terms_includes_term_already_in_query():
    """질의에 이미 법령용어가 있어도 연계조문은 그 용어로 찾아야 한다.

    expand_query 는 중복 표기를 피하려고 빼지만, 여기서 빼면 축이 통째로 죽는다.
    """
    assert expansion_terms("기업업무추진비와 접대비", ALIASES) == ["기업업무추진비"]


def test_expansion_terms_deduplicates():
    """서로 다른 일상어가 같은 법령용어를 가리켜도 한 번만 나온다."""
    aliases = {"접대비": ["기업업무추진비"], "접대": ["기업업무추진비"]}
    assert expansion_terms("접대비 한도", aliases) == ["기업업무추진비"]


# --- 사전 로딩 ---------------------------------------------------------------

def test_loads_shipped_dictionary():
    aliases = load_aliases()
    assert aliases, "기본 사전이 비어 있으면 확장이 동작하지 않는다"


def test_shipped_dictionary_values_are_lists():
    for common, legal in load_aliases().items():
        assert isinstance(legal, list) and legal, f"{common} 의 값이 비어 있다"
        assert common not in legal, f"{common} 이 자기 자신을 가리킨다"
