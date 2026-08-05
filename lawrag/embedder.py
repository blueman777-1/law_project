"""model2vec 정적 임베딩.

BGE-M3 대신 쓰는 이유는 순전히 환경 제약이다: 이 머신은 Python 3.14 뿐이고
torch 휠이 없다(→ sentence-transformers/fastembed 모두 불가). model2vec 은
numpy + tokenizers 만으로 돈다. 대신 문맥을 반영하지 못하는 정적 임베딩이라
검색 품질은 BGE-M3 보다 낮다 — 키워드 축과의 RRF 융합이 그만큼 중요하다.
"""
from __future__ import annotations

import numpy as np

from .config import EMBED_DIM, EMBED_MODEL

_model = None


def get_model():
    """최초 호출 시에만 로드한다(수백 MB 다운로드)."""
    global _model
    if _model is None:
        from model2vec import StaticModel

        _model = StaticModel.from_pretrained(EMBED_MODEL)
        if _model.dim != EMBED_DIM:
            raise RuntimeError(
                f"임베딩 차원 불일치: 모델 {_model.dim} vs 설정 EMBED_DIM={EMBED_DIM}. "
                f"sql/schema.sql 의 vector(...) 도 함께 맞춰야 한다."
            )
    return _model


def embed(texts: list[str]) -> np.ndarray:
    """L2 정규화된 (n, dim) 행렬. 정규화해 두면 코사인 거리가 내적과 같아진다."""
    vectors = get_model().encode(texts)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


def embed_one(text: str) -> np.ndarray:
    """numpy 배열로 돌려준다 — 파이썬 list 로 넘기면 psycopg 가 이를
    double precision[] 로 어댑트해서 pgvector 의 <=> 연산자가 걸리지 않는다."""
    return embed([text])[0]
