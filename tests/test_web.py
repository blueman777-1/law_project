"""웹 조회 계층의 순수 로직 테스트. 서버도 DB 도 띄우지 않는다."""
import pytest

from lawrag.db import SearchHit
from lawrag.web import attach_bodies, hit_payload

LAW = "주식회사 등의 외부감사에 관한 법률"


def hit(law=LAW, label="제6조", title="재무제표의 작성 책임 및 제출", body="① 회사의 대표이사는 ...",
        vec_rank=2, kw_rank=1, term_rank=None):
    header = f"{law} {label}({title})" if title else f"{law} {label}"
    return SearchHit(
        chunk_id=7, law_name=law, label=label, header=header,
        text=f"{header}\n{body}", enforced="20250401", source_type="article",
        vec_rank=vec_rank, kw_rank=kw_rank, term_rank=term_rank, rrf=0.03252,
    )


# --- 검색 결과 직렬화 ---------------------------------------------------------

def test_payload_strips_header_from_body():
    payload = hit_payload(hit(body="① 회사의 대표이사는 ..."))
    assert payload["body"] == "① 회사의 대표이사는 ..."
    assert not payload["body"].startswith(LAW)


def test_payload_extracts_article_title():
    assert hit_payload(hit())["title"] == "재무제표의 작성 책임 및 제출"


def test_payload_without_article_title():
    assert hit_payload(hit(title=""))["title"] == ""


def test_payload_ranks_use_dash_for_unfired_axis():
    """축이 발화하지 않은 것과 순위 1위는 눈으로 구분돼야 한다 (CLI 의 v#/k#/t# 와 같은 규칙)."""
    payload = hit_payload(hit(vec_rank=2, kw_rank=1, term_rank=None))
    assert payload["ranks"] == {"v": "2", "k": "1", "t": "-"}


def test_payload_carries_enforced_date_label():
    assert hit_payload(hit())["dated"] == "시행 20250401"


# --- 인용 조문을 검색 결과에 붙이기 -------------------------------------------

def test_citation_resolves_to_searched_article():
    """프롬프트가 [참고 조문]에 실린 법령명만 쓰게 하므로 모델은 전체 이름을 인용한다."""
    hits = [hit(label="제6조", body="① 회사의 대표이사는 ...")]
    [cited] = attach_bodies([{"law": LAW, "article": "제6조", "clause": "제6항"}], hits)
    assert cited["found"] is True
    assert cited["body"].startswith("① 회사의 대표이사는")
    assert cited["dated"] == "시행 20250401"


def test_citation_not_in_search_results_is_flagged():
    """검색 결과에 없는 조문을 모델이 지어낸 경우. 조용히 넘기면 환각이 그대로 보인다."""
    [cited] = attach_bodies([{"law": "법인세법", "article": "제999조"}], [hit()])
    assert cited["found"] is False
    assert cited["body"] == ""


def test_citation_of_other_law_with_same_article_no_is_not_attached():
    """조번호는 법마다 겹친다. 법령명이 안 맞으면 본문을 붙이면 안 된다."""
    hits = [hit(law="법인세법", label="제6조", title="사업연도")]
    [cited] = attach_bodies([{"law": LAW, "article": "제6조"}], hits)
    assert cited["found"] is False


def test_citation_of_enforcement_decree_does_not_attach_to_parent_law():
    """'법인세법 시행령'은 '법인세법' 청크에 붙으면 안 된다 — 시행일도 조문도 다르다."""
    hits = [hit(law="법인세법", label="제6조", title="사업연도")]
    [cited] = attach_bodies([{"law": "법인세법 시행령", "article": "제6조"}], hits)
    assert cited["found"] is False


def test_citation_keeps_clause_when_absent():
    [cited] = attach_bodies([{"law": LAW, "article": "제6조"}], [hit()])
    assert cited["clause"] == ""
