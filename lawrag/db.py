"""적재 + 하이브리드 검색.

벡터와 키워드를 RRF(Reciprocal Rank Fusion, k=60)로 융합한다. 가중합을 쓰지
않는 이유: 코사인 유사도와 ts_rank_cd 는 스케일이 전혀 달라서 가중치가 코퍼스에
따라 요동친다. RRF 는 점수를 버리고 순위만 쓰므로 그 문제가 없다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

from .config import DATABASE_URL
from .drf_client import TermLink
from .parser import Article, Chunk, Law, bigram_tokens

SCHEMA = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"

RRF_K = 60

# 청크의 이 비율을 넘게 나타나는 토큰은 질의에서 뺀다. 0.5~0.1 구간이 모두
# 비슷하게 좋았고(넓은 평탄역) 그 안에서 MRR 이 가장 높은 값을 골랐다.
MAX_DF_RATIO = 0.10


@dataclass(frozen=True)
class SearchHit:
    chunk_id: int
    law_name: str
    label: str
    header: str
    text: str
    enforced: str
    source_type: str
    vec_rank: int | None
    kw_rank: int | None
    term_rank: int | None
    rrf: float


def connect(url: str = DATABASE_URL) -> psycopg.Connection:
    conn = psycopg.connect(url, autocommit=True)
    register_vector(conn)
    return conn


def init_db(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA.read_text(encoding="utf-8"))


def upsert_law(conn: psycopg.Connection, law: Law, mst: str) -> int:
    row = conn.execute(
        """
        INSERT INTO law (mst, law_id, name, law_type, dept, promulgated, enforced)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (mst) DO UPDATE SET
            name = EXCLUDED.name, law_type = EXCLUDED.law_type, dept = EXCLUDED.dept,
            promulgated = EXCLUDED.promulgated, enforced = EXCLUDED.enforced,
            updated_at = now()
        RETURNING id
        """,
        (mst, law.law_id, law.name, law.law_type, law.dept, law.promulgated, law.enforced),
    ).fetchone()
    return row[0]


def upsert_article(conn: psycopg.Connection, law_pk: int, article: Article) -> tuple[int, bool]:
    """(article_pk, 본문이 바뀌었는가) 를 돌려준다.

    바뀌지 않았으면 호출자가 재임베딩을 건너뛴다 — refresh 가 싼 이유.
    """
    previous = conn.execute(
        "SELECT id, sha256 FROM article WHERE law_pk=%s AND article_no=%s AND branch_no=%s",
        (law_pk, article.no, article.branch),
    ).fetchone()
    if previous and previous[1] == article.sha256:
        return previous[0], False

    row = conn.execute(
        """
        INSERT INTO article (law_pk, article_no, branch_no, label, title, body, enforced, sha256)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (law_pk, article_no, branch_no) DO UPDATE SET
            label = EXCLUDED.label, title = EXCLUDED.title, body = EXCLUDED.body,
            enforced = EXCLUDED.enforced, sha256 = EXCLUDED.sha256
        RETURNING id
        """,
        (
            law_pk, article.no, article.branch, article.label,
            article.title, article.body, article.enforced, article.sha256,
        ),
    ).fetchone()
    return row[0], True


def replace_chunks(
    conn: psycopg.Connection,
    article_pk: int,
    chunks: list[Chunk],
    vectors,
    source_type: str = "article",
) -> None:
    conn.execute("DELETE FROM chunk WHERE article_pk = %s", (article_pk,))
    for chunk, vector in zip(chunks, vectors):
        conn.execute(
            """
            INSERT INTO chunk (article_pk, source_type, header, text, part, n_parts, tsv, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, to_tsvector('simple', %s), %s)
            """,
            (
                article_pk, source_type, chunk.header, chunk.text,
                chunk.part, chunk.n_parts, bigram_tokens(chunk.text), vector,
            ),
        )


def discriminative_tokens(
    tokens: list[str], df: dict[str, int], n_docs: int, max_ratio: float = MAX_DF_RATIO
) -> list[str]:
    """문서빈도가 높은 토큰을 버린다.

    한국어를 문자 bigram 으로 쪼개면 '법인'(코퍼스의 93%), '인세'(88%),
    '정하'(60%) 같은 조각이 나온다. 이걸 OR 질의에 그대로 넣으면 ts_rank_cd 가
    사실상 노이즈 밀도로 순위를 매긴다. 실측으로 키워드 축 Recall@5 가
    0.333 → 0.533 으로 올랐다.

    전부 걸러지면 빈 질의를 만들지 말고 원래 토큰을 쓴다.
    """
    kept = [t for t in tokens if df.get(t, 0) <= n_docs * max_ratio]
    return kept or tokens


def to_tsquery_or(text: str, df: dict[str, int] | None = None, n_docs: int = 0) -> str:
    """bigram 토큰을 OR 로 묶는다.

    AND(plainto_tsquery) 를 쓰면 bigram 하나만 어긋나도 결과가 0건이 된다.
    OR 로 뽑고 ts_rank_cd 가 많이 겹치는 순으로 정렬하게 둔다.
    df 통계가 주어지면 변별력 없는 토큰을 먼저 걸러낸다.
    """
    tokens = list(dict.fromkeys(bigram_tokens(text).split()))
    if df and n_docs:
        tokens = discriminative_tokens(tokens, df, n_docs)
    return " | ".join(tokens)


def rebuild_token_df(conn: psycopg.Connection) -> int:
    """청크 전체에서 토큰별 문서빈도를 다시 집계한다. ingest 후에 호출한다."""
    conn.execute("TRUNCATE token_df")
    conn.execute(
        "INSERT INTO token_df (token, ndoc) "
        "SELECT word, ndoc FROM ts_stat('SELECT tsv FROM chunk')"
    )
    return conn.execute("SELECT count(*) FROM token_df").fetchone()[0]


def replace_term_links(conn: psycopg.Connection, term: str, links: list[TermLink]) -> int:
    """한 법령용어의 연계조문을 통째로 갈아 끼운다.

    코퍼스 밖 법령의 연계도 그대로 넣는다 — 검색 SQL 이 article 과 조인할 때
    자연히 걸러지고, 나중에 코퍼스를 넓혀도 다시 받을 필요가 없다.
    """
    conn.execute("DELETE FROM term_article WHERE term = %s", (term,))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO term_article (term, law_name, article_no, branch_no, is_core) "
            "VALUES (%s, %s, %s, %s, %s)",
            [(term, x.law_name, x.article_no, x.branch_no, x.is_core) for x in links],
        )
    return len(links)


def count_corpus_term_links(conn: psycopg.Connection, term: str) -> int:
    """그중 실제로 적재된 조문에 걸리는 것만 센다. refresh-terms 가 보고용으로 쓴다."""
    return conn.execute(
        """
        SELECT count(DISTINCT a.id) FROM term_article t
        JOIN law l ON l.name = t.law_name
        JOIN article a ON a.law_pk = l.id
                      AND a.article_no = t.article_no AND a.branch_no = t.branch_no
        WHERE t.term = %s
        """,
        (term,),
    ).fetchone()[0]


def load_df(conn: psycopg.Connection, tokens: list[str]) -> tuple[dict[str, int], int]:
    """질의에 등장한 토큰의 df 만 가져온다 (22,000개 전체를 읽지 않는다)."""
    n_docs = conn.execute("SELECT count(*) FROM chunk").fetchone()[0]
    rows = conn.execute(
        "SELECT token, ndoc FROM token_df WHERE token = ANY(%s)", (tokens,)
    ).fetchall()
    return {token: ndoc for token, ndoc in rows}, n_docs


# 세 축의 후보를 각각 pool 개 뽑아 RRF 로 합친다.
# LEFT JOIN 이라 한 축에만 걸린 청크도 살아남고, 어느 축이 잡았는지
# vec_rank / kw_rank / term_rank 로 확인할 수 있다.
#
# tm(용어 연계) 축은 질의 확장이 법령용어를 덧붙였을 때만 후보를 낸다.
# 정렬은 핵심용어 조문 먼저, 같은 핵심용어끼리는 그 용어가 조문에 많이 나오는 순,
# 그 안에서 벡터 거리순이다. 핵심용어(140101)는 그 조문이 용어를 정의·규율한다는
# 뜻이라 단순 언급 조문보다 앞세울 근거가 있고, 등장 횟수는 핵심용어 조문이 여럿일 때
# 어느 쪽이 그 용어를 실제로 다루는지 가른다(제6조가 사업연도 핵심 조문 중 1위가 된다).
# 로그를 씌워도 결과가 같아 튜닝할 값이 없다 — 가중치가 아니라 tie-break 다.
_HYBRID_SQL = """
WITH vec AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> %(qv)s) AS rank
    FROM chunk
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> %(qv)s
    LIMIT %(pool)s
),
kw AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS rank
    FROM chunk, to_tsquery('simple', %(tsq)s) AS q
    WHERE tsv @@ q
    ORDER BY ts_rank_cd(tsv, q) DESC
    LIMIT %(pool)s
),
tm AS (
    SELECT c.id,
           ROW_NUMBER() OVER (ORDER BY bool_or(t.is_core) DESC,
                                       max((length(a.body) - length(replace(a.body, t.term, '')))
                                           / NULLIF(length(t.term), 0)) DESC,
                                       min(c.embedding <=> %(qv)s)) AS rank
    FROM chunk c
    JOIN article a ON a.id = c.article_pk
    JOIN law l ON l.id = a.law_pk
    JOIN term_article t ON t.law_name = l.name
                       AND t.article_no = a.article_no
                       AND t.branch_no = a.branch_no
    WHERE t.term = ANY(%(terms)s::text[]) AND c.embedding IS NOT NULL
    GROUP BY c.id
    ORDER BY bool_or(t.is_core) DESC,
             max((length(a.body) - length(replace(a.body, t.term, '')))
                 / NULLIF(length(t.term), 0)) DESC,
             min(c.embedding <=> %(qv)s)
    LIMIT %(pool)s
)
SELECT c.id, l.name, a.label, c.header, c.text, a.enforced, c.source_type,
       v.rank, k.rank, t.rank,
       COALESCE(1.0 / (%(rrf_k)s + v.rank), 0)
     + COALESCE(1.0 / (%(rrf_k)s + k.rank), 0)
     + COALESCE(1.0 / (%(rrf_k)s + t.rank), 0) AS rrf
FROM chunk c
JOIN article a ON a.id = c.article_pk
JOIN law l ON l.id = a.law_pk
LEFT JOIN vec v ON v.id = c.id
LEFT JOIN kw  k ON k.id = c.id
LEFT JOIN tm  t ON t.id = c.id
WHERE v.id IS NOT NULL OR k.id IS NOT NULL OR t.id IS NOT NULL
ORDER BY rrf DESC, c.id
LIMIT %(limit)s
"""


def hybrid_search(
    conn: psycopg.Connection,
    query: str,
    query_vector,
    limit: int = 5,
    pool: int = 30,
    link_terms: list[str] | None = None,
) -> list[SearchHit]:
    """link_terms 는 질의 확장이 덧붙인 법령용어다. 비어 있으면 tm 축은 놀고,
    검색은 기존 2축과 완전히 같아진다."""
    tokens = list(dict.fromkeys(bigram_tokens(query).split()))
    df, n_docs = load_df(conn, tokens)
    tsq = to_tsquery_or(query, df=df, n_docs=n_docs)
    rows = conn.execute(
        _HYBRID_SQL,
        {
            "qv": query_vector,
            "tsq": tsq or "그런토큰없음",
            "terms": link_terms or [],
            "pool": pool,
            "limit": limit,
            "rrf_k": RRF_K,
        },
    ).fetchall()
    return [
        SearchHit(
            chunk_id=r[0], law_name=r[1], label=r[2], header=r[3], text=r[4],
            enforced=r[5], source_type=r[6], vec_rank=r[7], kw_rank=r[8],
            term_rank=r[9], rrf=float(r[10]),
        )
        for r in rows
    ]
