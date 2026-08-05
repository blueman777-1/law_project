"""일상용어 → 법령용어 질의 확장.

정적 임베딩도 bigram 키워드 검색도 '접대비 → 기업업무추진비' 같은 도약을 못 한다.
법이 용어를 개정해 버리면 일상어가 코퍼스에 아예 0건이기 때문이다(실측: 접대비 0청크,
기업업무추진비 19청크). 그래서 질의 전처리 단계에서 법령용어를 덧붙인다.

**이 사전은 임베딩하지 않는다.** 질의를 만들 때 조회만 한다.
"""
from __future__ import annotations

from pathlib import Path

import yaml

TERMS_FILE = Path(__file__).resolve().parent / "terms.yaml"

_cache: dict[str, list[str]] | None = None


def load_aliases(path: Path = TERMS_FILE) -> dict[str, list[str]]:
    global _cache
    if _cache is None:
        _cache = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _cache


def expansion_terms(text: str, aliases: dict[str, list[str]] | None = None) -> list[str]:
    """질의에 걸린 일상용어에 대응하는 법령용어 목록.

    `expand_query` 와 달리 이미 질의에 들어 있는 말도 빼지 않는다 —
    이 목록은 문장에 덧붙이는 용도가 아니라 연계조문(term_article)을 찾는 열쇠라,
    빼면 그 축이 통째로 죽는다.
    """
    if aliases is None:
        aliases = load_aliases()
    found: list[str] = []
    for common, legal in aliases.items():
        if common in text:
            found.extend(term for term in legal if term not in found)
    return found


def expand_query(text: str, aliases: dict[str, list[str]] | None = None) -> str:
    """질의에 일상용어가 있으면 대응 법령용어를 덧붙인다.

    원래 표현은 지우지 않는다 — 그 말을 그대로 쓴 조문이 있을 수도 있고,
    잘못된 치환으로 질의를 망가뜨리는 것보다 낫다.
    """
    extra = [term for term in expansion_terms(text, aliases) if term not in text]
    if not extra:
        return text
    return text + " " + " ".join(extra)
