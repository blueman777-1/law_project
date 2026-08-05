-- law-rag 스키마. PostgreSQL + pgvector 하나로 키워드·벡터 검색을 모두 처리한다.
-- 별도 벡터DB를 쓰지 않는 이유: 목표 규모 ~1만 청크면 HNSW 로 충분하고,
-- 메타데이터 필터를 같은 SQL 안에서 동시에 걸 수 있다.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS law (
    id          SERIAL PRIMARY KEY,
    mst         TEXT NOT NULL UNIQUE,   -- 법령일련번호. 본문 조회 키
    law_id      TEXT NOT NULL,
    name        TEXT NOT NULL,
    law_type    TEXT,
    dept        TEXT,
    promulgated TEXT,                   -- YYYYMMDD
    enforced    TEXT,                   -- YYYYMMDD
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS article (
    id         SERIAL PRIMARY KEY,
    law_pk     INT  NOT NULL REFERENCES law(id) ON DELETE CASCADE,
    article_no INT  NOT NULL,
    branch_no  INT  NOT NULL DEFAULT 0, -- 제18조의2 면 2
    label      TEXT NOT NULL,           -- '제18조의2'
    title      TEXT,
    body       TEXT NOT NULL,
    enforced   TEXT,
    -- 본문 해시. 개정 시 변경된 조문만 재임베딩하기 위한 것.
    sha256     TEXT NOT NULL,
    UNIQUE (law_pk, article_no, branch_no)
);

CREATE TABLE IF NOT EXISTS chunk (
    id          SERIAL PRIMARY KEY,
    article_pk  INT  NOT NULL REFERENCES article(id) ON DELETE CASCADE,
    -- article  : 법령·행정규칙 조문
    -- decision : 심판례·의결서 (본문 있음)
    -- reference: 본문 API 가 없어 안건명·링크만 있는 해석 → 프롬프트에서 다르게 취급
    source_type TEXT NOT NULL DEFAULT 'article'
                CHECK (source_type IN ('article', 'decision', 'reference')),
    header      TEXT NOT NULL,          -- '법인세법 제18조의2(내국법인 수입배당금액의 익금불산입)'
    text        TEXT NOT NULL,          -- header + 본문. 임베딩 대상
    part        INT  NOT NULL DEFAULT 0,
    n_parts     INT  NOT NULL DEFAULT 1,
    tsv         tsvector,               -- Python bigram → to_tsvector('simple')
    embedding   vector(256),
    UNIQUE (article_pk, part)
);

-- 토큰별 문서빈도. 질의에서 변별력 없는 bigram('법인'은 청크의 93%에 등장)을
-- 걸러내는 데 쓴다. ingest 후 ts_stat 로 다시 집계한다.
CREATE TABLE IF NOT EXISTS token_df (
    token TEXT PRIMARY KEY,
    ndoc  INT NOT NULL
);

-- 법령용어 → 그 용어가 쓰인 조문 (DRF target=lstrmRltJo). 검색의 세 번째 축.
-- article 을 직접 참조하지 않고 (법령명, 조번호, 가지번호) 로 느슨하게 잇는다:
-- 재적재해도 깨지지 않고, 아직 적재하지 않은 법령의 연계도 그대로 둘 수 있다.
CREATE TABLE IF NOT EXISTS term_article (
    term       TEXT NOT NULL,           -- 법령용어. terms.yaml 이 질의에 덧붙이는 말
    law_name   TEXT NOT NULL,
    article_no INT  NOT NULL,
    branch_no  INT  NOT NULL DEFAULT 0,
    -- 용어구분코드 140101. 그 조문이 이 용어를 '정의·규율'한다는 뜻이라
    -- 단순히 언급만 한 조문보다 앞에 세운다.
    is_core    BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (term, law_name, article_no, branch_no)
);

CREATE INDEX IF NOT EXISTS term_article_lookup_idx
    ON term_article (law_name, article_no, branch_no);

-- 하이브리드 검색의 두 축. 둘 다 있어야 RRF 가 의미를 갖는다.
CREATE INDEX IF NOT EXISTS chunk_tsv_idx ON chunk USING GIN (tsv);
CREATE INDEX IF NOT EXISTS chunk_embedding_idx
    ON chunk USING hnsw (embedding vector_cosine_ops);
