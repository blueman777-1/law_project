"""브라우저에서 문장을 넣어 결과를 눈으로 확인하는 조회 계층.

검색과 판정은 CLI 와 **같은 함수**를 부른다 (`cli._search`, `judge.judge`).
여기에는 검색·판정 로직이 없다 — 있으면 CLI 와 결과가 갈라진다.

의존성을 늘리지 않으려고 표준 라이브러리 `http.server` 를 쓴다. 로컬에서
혼자 확인하는 용도이고, 프로덕션 서버가 아니다.

검색(빠름)과 판정(claude CLI, 십수 초)을 **다른 엔드포인트로 나눴다.**
한 번에 처리하면 화면이 수십 초 동안 비어 있어서 "무엇이 걸렸는지"를
눈으로 볼 수 없다. 판정 쪽은 같은 문장으로 검색을 다시 돌린다 — 검색이
데이터만으로 결정되므로(불변식 9) 두 번 돌려도 같은 결과다.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db as dbm
from .db import SearchHit

PAGE = Path(__file__).resolve().parent / "static" / "index.html"
MAX_BODY = 8_192  # 점검 문장 하나면 충분하다
DISCLAIMER = "※ 이 결과는 법률 자문이 아니며, 반드시 원문과 전문가 검토로 확인하세요."


def _body_of(hit: SearchHit) -> str:
    """청크 본문에서 헤더(법령명 제N조(제목))를 뗀다."""
    return hit.text[len(hit.header):].strip()


def hit_payload(hit: SearchHit) -> dict:
    """검색 결과 한 건을 화면용 dict 로. 축 순위는 CLI 의 v#/k#/t# 와 같은 규칙이다."""
    return {
        "law": hit.law_name,
        "label": hit.label,
        "title": hit.header[len(f"{hit.law_name} {hit.label}"):].strip("()"),
        "body": _body_of(hit),
        "dated": hit.dated,
        "rrf": hit.rrf,
        "ranks": {
            key: str(rank) if rank else "-"
            for key, rank in (("v", hit.vec_rank), ("k", hit.kw_rank), ("t", hit.term_rank))
        },
    }


def attach_bodies(cited: list[dict], hits: list[SearchHit]) -> list[dict]:
    """모델이 인용한 조문을 검색 결과에 되짚는다.

    되짚기 규칙은 CLI 와 같다(법령명 부분일치 + 조번호 정확일치). 검색 결과에
    없는 인용은 모델이 지어낸 것이므로 `found=False` 로 남긴다 — CLI 가 ⚠ 를
    찍는 자리와 같다. 조용히 빈 칸으로 두면 환각이 그대로 사실처럼 보인다.
    """
    out = []
    for c in cited:
        law, article = c.get("law", ""), c.get("article", "")
        source = next((h for h in hits if h.label == article and law in h.law_name), None)
        out.append({
            "law": law,
            "article": article,
            "clause": c.get("clause") or "",
            "found": source is not None,
            "body": _body_of(source) if source else "",
            "dated": source.dated if source else "",
        })
    return out


def search_response(sentence: str, top: int) -> dict:
    from .cli import _search  # typer 앱을 import 하므로 요청 시점에만
    from .terms import expand_query

    with dbm.connect() as conn:
        hits = _search(conn, sentence, top)
    return {
        "sentence": sentence,
        "expanded": expand_query(sentence),
        "hits": [hit_payload(h) for h in hits],
    }


def judge_response(sentence: str, top: int) -> dict:
    from .cli import _search
    from .judge import JudgeError, judge

    with dbm.connect() as conn:
        hits = _search(conn, sentence, top)
    if not hits:
        return {"error": "검색된 조문이 없습니다. 먼저 ingest 를 실행하세요."}
    try:
        result = judge(sentence, hits)
    except JudgeError as exc:
        # 검색은 이미 화면에 나가 있다. 판정 단계만 죽은 것이므로 그렇게 알린다.
        return {"error": str(exc)}
    return {
        "verdict": result.verdict,
        "confidence": result.confidence,
        "reason": result.reason,
        "cited": attach_bodies(result.cited, hits),
        "disclaimer": DISCLAIMER,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "lawrag"

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.read_bytes())
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self) -> None:
        handlers = {"/api/search": search_response, "/api/judge": judge_response}
        handler = handlers.get(self.path)
        if handler is None:
            return self._json(404, {"error": "not found"})

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "문장이 너무 깁니다."})
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            sentence = (payload.get("sentence") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            return self._json(400, {"error": "요청을 읽을 수 없습니다."})
        if not sentence:
            return self._json(400, {"error": "문장을 입력하세요."})

        try:
            self._json(200, handler(sentence, int(payload.get("top") or 8)))
        except Exception as exc:  # DB 가 꺼져 있는 경우가 대부분이다
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, "application/json; charset=utf-8",
                   json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"  {self.address_string()} {fmt % args}")


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    from .embedder import get_model

    get_model()  # 첫 질의가 모델 다운로드로 멈추지 않게 미리 올린다
    with ThreadingHTTPServer((host, port), Handler) as httpd:
        print(f"law-rag  http://{host}:{port}  (Ctrl+C 로 종료)")
        httpd.serve_forever()
