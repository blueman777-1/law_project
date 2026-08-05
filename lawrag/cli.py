"""law-rag CLI.

    init-db        스키마 생성
    ingest         법령 수집 → 파싱 → 청킹 → 임베딩 → 적재
    refresh-terms  terms.yaml 의 법령용어별 연계조문 적재
    search         하이브리드 검색 (벡터 + 키워드 + 용어연계 RRF)
    check          문장을 넣으면 저촉되는 조문과 위법 여부를 판단
    eval           Recall@5 / Recall@10 / MRR
"""
from __future__ import annotations

import requests
import typer

from . import db as dbm
from .drf_client import DrfClient, DrfError
from .embedder import embed, embed_one
from .terms import expand_query, expansion_terms, load_aliases
from .parser import chunk_article, parse_law

app = typer.Typer(add_completion=False, help="국가법령정보 기반 조문 검색·위법 점검")

# 코퍼스는 법인세 + 외부감사 2축으로 의도적으로 좁혀 놓았다.
# 임의로 넓히면 임베딩 시간과 검색 노이즈가 함께 늘어난다.
TARGET_LAWS = [
    "법인세법",
    "법인세법 시행령",
    "법인세법 시행규칙",
    "주식회사 등의 외부감사에 관한 법률",
    "주식회사 등의 외부감사에 관한 법률 시행령",
]

DISCLAIMER = "※ 이 결과는 법률 자문이 아니며, 반드시 원문과 전문가 검토로 확인하세요."


def _ranks(hit) -> str:
    """어느 축이 이 청크를 잡았는지. 한 축이 통째로 죽어도 눈에 보이게 하는 장치다."""
    return ", ".join(
        f"{prefix}#{rank}" if rank else f"{prefix}#-"
        for prefix, rank in (("v", hit.vec_rank), ("k", hit.kw_rank), ("t", hit.term_rank))
    )


def _err(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _search(conn, text: str, limit: int):
    """질의를 확장한 뒤 검색한다. 확장문을 벡터·키워드 **양쪽**에 쓴다 —
    실측상 한쪽에만 적용하면 이득이 절반으로 줄었다 (R@5 0.733 vs 0.800).

    확장이 덧붙인 법령용어는 연계조문 축에도 넘긴다."""
    expanded = expand_query(text)
    return dbm.hybrid_search(
        conn, expanded, embed_one(expanded), limit=limit,
        link_terms=expansion_terms(text),
    )


@app.command("init-db")
def init_db() -> None:
    """스키마와 인덱스를 만든다 (idempotent)."""
    with dbm.connect() as conn:
        dbm.init_db(conn)
        indexes = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename='chunk' ORDER BY indexname"
        ).fetchall()
    typer.echo("스키마 생성 완료")
    for (name,) in indexes:
        typer.echo(f"  index: {name}")


@app.command()
def ingest(
    query: str = typer.Option(None, "--query", "-q", help="이 이름의 법령만 수집 (기본: 대상 4개 전체)"),
    limit: int = typer.Option(0, "--limit", "-n", help="수집할 법령 수 상한 (0=제한 없음)"),
) -> None:
    """법령을 수집해 조문 단위로 적재한다."""
    names = [query] if query else TARGET_LAWS
    if limit:
        names = names[:limit]

    try:
        client = DrfClient()
    except DrfError as exc:
        _err(str(exc))

    with dbm.connect() as conn:
        for name in names:
            try:
                hits = client.search_law(name, display=20)
            except DrfError as exc:
                _err(f"{name}: {exc}")

            # 검색은 부분일치라 '법인세법'으로 시행령까지 딸려온다. 정확히 일치하는 것만.
            match = next((h for h in hits if h.name == name), None)
            if match is None:
                typer.secho(f"[건너뜀] '{name}' 정확 일치 없음 (후보: {[h.name for h in hits][:3]})",
                            fg=typer.colors.YELLOW)
                continue

            law, articles = parse_law(client.fetch_law(match.mst))
            law_pk = dbm.upsert_law(conn, law, match.mst)

            changed = skipped = n_chunks = 0
            for article in articles:
                article_pk, is_changed = dbm.upsert_article(conn, law_pk, article)
                if not is_changed:
                    skipped += 1
                    continue
                chunks = chunk_article(law.name, article)
                vectors = embed([c.text for c in chunks])
                dbm.replace_chunks(conn, article_pk, chunks, vectors)
                changed += 1
                n_chunks += len(chunks)

            typer.secho(
                f"[적재] {law.name} (시행 {law.enforced}) "
                f"조문 {len(articles)}개 → 신규·변경 {changed} / 해시일치 건너뜀 {skipped} / 청크 {n_chunks}",
                fg=typer.colors.GREEN,
            )

        # 코퍼스가 바뀌면 문서빈도도 바뀐다. 이걸 갱신하지 않으면 질의 필터가 틀어진다.
        n_tokens = dbm.rebuild_token_df(conn)
        typer.echo(f"토큰 문서빈도 재집계: {n_tokens:,}개")


@app.command()
def refresh() -> None:
    """개정분만 다시 적재한다. 본문 해시가 같은 조문은 재임베딩하지 않는다."""
    ingest(query=None, limit=0)


@app.command("refresh-terms")
def refresh_terms(
    force: bool = typer.Option(False, "--force", help="이미 받아 둔 용어도 다시 받는다"),
) -> None:
    """terms.yaml 의 법령용어별 연계조문을 받아 term_article 에 넣는다.

    응답이 최대 3MB 라 검색 때마다 부를 수 없다. 사전이 바뀌었을 때만 돌리면 된다.
    서버가 큰 응답에서 몇 분씩 매달리는 일이 있어, 이미 받은 용어는 건너뛰고
    이어받는다. 중간에 끊겨도 다시 돌리면 남은 것만 받는다.
    """
    terms: list[str] = []
    for legal in load_aliases().values():
        terms.extend(t for t in legal if t not in terms)

    client = DrfClient()
    with dbm.connect() as conn:
        if not force:
            done = {r[0] for r in conn.execute("SELECT DISTINCT term FROM term_article").fetchall()}
            skipped = [t for t in terms if t in done]
            if skipped:
                typer.echo(f"  이미 받아 둠, 건너뜀: {', '.join(skipped)}")
            terms = [t for t in terms if t not in done]

        for term in terms:
            try:
                links = client.fetch_term_articles(term)
            except (DrfError, requests.RequestException) as exc:
                # 용어가 없는 것, API 가 막힌 것, 응답이 커서 시간이 초과된 것이
                # 모두 여기로 온다. 한 용어 때문에 나머지를 다시 받게 하지 않는다.
                # 조용히 넘기면 축이 반쯤 죽은 채로 돌므로 눈에 보이게 남긴다.
                typer.secho(f"  [건너뜀] {term}: {exc}", fg=typer.colors.YELLOW)
                continue
            dbm.replace_term_links(conn, term, links)
            typer.echo(
                f"  {term}: 연계조문 {len(links)}개 "
                f"(코퍼스에 있는 것 {dbm.count_corpus_term_links(conn, term)}개)"
            )
    typer.secho(f"용어 {len(terms)}개 갱신 완료", fg=typer.colors.GREEN)


@app.command()
def search(
    query: str = typer.Argument(..., help="검색어"),
    limit: int = typer.Option(5, "--limit", "-n"),
) -> None:
    """하이브리드 검색. 벡터 순위와 키워드 순위를 함께 보여준다."""
    with dbm.connect() as conn:
        hits = _search(conn, query, limit)

    if not hits:
        typer.secho("검색 결과 없음", fg=typer.colors.YELLOW)
        return

    typer.echo(f"\n질의: {query}\n" + "─" * 78)
    for i, h in enumerate(hits, 1):
        typer.secho(f"[{i}] {h.law_name} {h.label}", fg=typer.colors.CYAN, bold=True)
        typer.echo(f"    RRF {h.rrf:.5f}  ({_ranks(h)})  시행 {h.enforced}")
        body = h.text[len(h.header):].strip().replace("\n", " ")
        typer.echo(f"    {body[:150]}...")


@app.command()
def check(
    sentence: str = typer.Argument(..., help="점검할 문장"),
    top: int = typer.Option(8, "--top", help="LLM 에 넘길 조문 수"),
) -> None:
    """문장이 어느 조문에 저촉되는지 판단한다."""
    from .judge import judge  # claude CLI 를 쓰므로 필요할 때만 로드

    with dbm.connect() as conn:
        hits = _search(conn, sentence, top)

    if not hits:
        _err("검색된 조문이 없습니다. 먼저 ingest 를 실행하세요.")

    typer.echo(f"\n질의: {sentence}")
    typer.echo("─" * 78)
    typer.secho("▸ 검색된 근거 후보", bold=True)
    for i, h in enumerate(hits[:5], 1):
        typer.echo(f"  [{i}] {h.law_name} {h.label}  RRF {h.rrf:.5f} ({_ranks(h)})")

    typer.echo()
    typer.secho("▸ 판단 중 (claude CLI)...", fg=typer.colors.BRIGHT_BLACK)
    result = judge(sentence, hits)

    color = {
        "위반 소지 있음": typer.colors.RED,
        "위반으로 보기 어려움": typer.colors.GREEN,
        "정보 부족": typer.colors.YELLOW,
    }[result.verdict]
    typer.echo("─" * 78)
    typer.secho(f"▸ 판정: {result.verdict}", fg=color, bold=True)
    if result.confidence:
        typer.echo(f"  신뢰도: {result.confidence}")

    if result.cited:
        typer.secho("\n▸ 근거 조문", bold=True)
        by_label = {(h.law_name, h.label): h for h in hits}
        for c in result.cited:
            law_name, article = c.get("law", ""), c.get("article", "")
            clause = c.get("clause") or ""
            typer.secho(f"  · {law_name} {article} {clause}".rstrip(), fg=typer.colors.CYAN)
            source = next(
                (h for (ln, lb), h in by_label.items() if lb == article and law_name in ln), None
            )
            if source:
                body = source.text[len(source.header):].strip().replace("\n", " ")
                typer.echo(f"    (시행 {source.enforced}) {body[:180]}...")
            else:
                typer.secho("    ⚠ 검색 결과에 없는 조문입니다 — 인용을 신뢰하지 마세요.",
                            fg=typer.colors.YELLOW)

    typer.echo(f"\n▸ 이유\n  {result.reason}")
    typer.secho(f"\n{DISCLAIMER}", fg=typer.colors.BRIGHT_BLACK)


@app.command("eval")
def eval_cmd(
    path: str = typer.Option("eval/questions.yaml", "--file", "-f"),
    pool: int = typer.Option(10, "--pool", help="Recall 계산에 쓸 상위 N"),
) -> None:
    """Recall@5 / Recall@10 / MRR 을 출력한다."""
    import yaml

    with open(path, encoding="utf-8") as fh:
        questions = yaml.safe_load(fh)["questions"]

    ranks: list[int | None] = []
    with dbm.connect() as conn:
        for q in questions:
            hits = _search(conn, q["query"], pool)
            # 법령명은 정확히 일치시켜야 한다. 부분일치로 하면 '법인세법'이
            # '법인세법 시행령'에도 걸려서, 양쪽에 다 있는 제6조가 오탐이 된다.
            rank = next(
                (i for i, h in enumerate(hits, 1)
                 if h.label == q["expect_article"] and h.law_name == q["expect_law"]),
                None,
            )
            ranks.append(rank)
            mark = "OK " if rank and rank <= 5 else ("~  " if rank else "MISS")
            typer.echo(f"  [{mark}] rank={rank or '-':>3}  {q['query'][:44]}")

    n = len(ranks)
    r5 = sum(1 for r in ranks if r and r <= 5) / n
    r10 = sum(1 for r in ranks if r and r <= 10) / n
    mrr = sum(1 / r for r in ranks if r) / n
    typer.echo("─" * 78)
    typer.secho(f"문항 {n}개   Recall@5 {r5:.3f}   Recall@10 {r10:.3f}   MRR {mrr:.3f}", bold=True)


if __name__ == "__main__":
    app()
