"""축별 CTE 가 결정적 순위를 매기는지 — 하이브리드 SQL 의 구조 검사.

세션 4 에서 결정문을 넣었다 지운 뒤 지표가 복귀하지 않았다(R@10 0.933 → 0.967).
데이터는 완전히 같았다. 원인은 kw 축의 `ts_rank_cd` 동점이었다 — eval 30문항을
실측하니 상위 30순위 중 **743/900(82.6%)이 동점 그룹 안**이고 25문항은 동점 그룹이
pool 경계를 걸치고 있었다. 두 번째 키가 없으면 그 안의 순서를 실행 계획이 정하므로,
dead tuple 이나 통계가 바뀌면 순위가 재배치된다.

DB 없이 도는 테스트라 SQL 문자열을 검사한다. 이 프로젝트의 테스트는 전부
DB·네트워크 없이 돌고(그래서 0.3초에 끝난다) 실제 검색 검증은 `cli eval` 이 한다.
"""
import re

from lawrag.db import _HYBRID_SQL

AXES = ("vec", "kw", "tm")


def _cte_body(name: str) -> str:
    """이름 붙은 CTE 에서 LIMIT 앞까지를 잘라낸다."""
    return _HYBRID_SQL.split(f"{name} AS (", 1)[1].split("LIMIT", 1)[0]


def _norm(clause: str) -> str:
    return " ".join(clause.split())


def _window_order(name: str) -> str:
    """ROW_NUMBER() OVER (ORDER BY ...) 의 정렬식."""
    body = _cte_body(name)
    return _norm(re.search(r"ROW_NUMBER\(\) OVER \(ORDER BY(.*?)\) AS rank", body, re.S).group(1))


def _pool_order(name: str) -> str:
    """LIMIT 으로 후보 풀을 자를 때 쓰는 정렬식 (CTE 의 마지막 ORDER BY)."""
    return _norm(_cte_body(name).rsplit("ORDER BY", 1)[1])


def test_every_axis_ranks_by_a_total_order():
    """정렬 마지막 키가 기본키여야 동점 안 순서가 결정된다.

    없으면 실행 계획이 순서를 정하고, 코퍼스를 건드릴 때마다 순위가 재배치된다.
    """
    for axis in AXES:
        assert _window_order(axis).endswith(("c.id", " id")), axis


def test_window_and_pool_orderings_match():
    """ROW_NUMBER 는 WHERE 를 통과한 전체 행에 매겨지고 LIMIT 이 뒤에 자른다.

    두 정렬식이 동점 처리에서 어긋나면 풀에 살아남은 행의 rank 가 1..pool 이 아니게 된다.
    """
    for axis in AXES:
        assert _window_order(axis) == _pool_order(axis), axis
