"""검색된 조문을 근거로 위법 여부를 판단한다.

LLM 백엔드는 `claude` CLI 서브프로세스다. 이 머신에 API 키가 없고 CLI 는 이미
설치돼 있어서 고른 방식이다 — README 의 한계 항목에 명시해 둔다.

판단은 반드시 **검색된 조문 안에서만** 이뤄져야 한다. 모델이 아는 법 지식으로
답하면 인용과 답변이 어긋나서 RAG 를 만든 의미가 없어진다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from .db import SearchHit

VERDICTS = ("위반 소지 있음", "위반으로 보기 어려움", "정보 부족")


class JudgeError(RuntimeError):
    """LLM 백엔드를 쓸 수 없거나 응답을 해석할 수 없을 때.

    호출부가 트레이스백 대신 안내 문구를 내보낼 수 있도록 백엔드 쪽 실패를
    한 종류로 모은다. 검색은 이미 끝난 뒤라 결과 자체는 살아 있다.
    """

JUDGE_MODEL = "sonnet"

_INSTRUCTIONS = f"""당신은 한국 법령 검토 보조자입니다.

아래 [질의]가 [참고 조문] 중 어느 조문에 저촉되는지 판단하세요.

규칙:
- **제공된 조문만 근거로 삼으세요.** 당신이 따로 아는 법 지식으로 답하지 마세요.
- 제공된 조문으로 판단할 수 없으면 verdict 를 "정보 부족" 으로 하세요.
- "본문 미제공" 으로 표시된 항목은 본문이 없으므로 근거로 인용하지 마세요.
- 인용은 반드시 [참고 조문]에 실제로 있는 법령명·조번호만 쓰세요. 지어내지 마세요.
- 항이 특정되면 clause 에 "제2항" 처럼 적고, 특정 못 하면 빈 문자열로 두세요.

verdict 는 다음 셋 중 하나여야 합니다: {' / '.join(VERDICTS)}
confidence 는 높음 / 중간 / 낮음 중 하나입니다.

다른 말 없이 아래 JSON 만 출력하세요:
{{"verdict": "...", "cited": [{{"law": "...", "article": "...", "clause": "..."}}],
 "reason": "...", "confidence": "..."}}"""


@dataclass(frozen=True)
class Judgment:
    verdict: str
    reason: str
    confidence: str = ""
    cited: list[dict] = field(default_factory=list)


def build_prompt(question: str, hits: list[SearchHit]) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        # 본문 API 가 없어 안건명·링크만 있는 자료는 근거로 쓰면 안 된다.
        tag = " [본문 미제공 — 참고용]" if h.source_type == "reference" else ""
        dated = f" ({h.dated})" if h.dated else ""
        blocks.append(f"[{i}] {h.law_name} {h.label}{dated}{tag}\n{h.text}")
    sources = "\n\n".join(blocks) if blocks else "(검색된 조문 없음)"
    return f"{_INSTRUCTIONS}\n\n[질의]\n{question}\n\n[참고 조문]\n{sources}"


def extract_json(text: str) -> dict:
    """모델 출력에서 JSON 객체를 꺼낸다. 코드펜스나 앞뒤 설명이 붙어도 견딘다."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    brace = re.search(r"\{.*\}", text, re.S)
    if brace:
        candidates.append(brace.group(0))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"응답에서 JSON 을 찾지 못했습니다: {text[:200]!r}")


def parse_judgment(data: dict) -> Judgment:
    verdict = data.get("verdict", "")
    if verdict not in VERDICTS:
        raise ValueError(f"알 수 없는 판정값: {verdict!r} (허용: {VERDICTS})")
    return Judgment(
        verdict=verdict,
        reason=data.get("reason", ""),
        confidence=data.get("confidence", ""),
        cited=data.get("cited") or [],
    )


def judge(question: str, hits: list[SearchHit], model: str = JUDGE_MODEL, timeout: int = 180) -> Judgment:
    if not shutil.which("claude"):
        raise JudgeError("claude CLI 를 찾을 수 없습니다. 이 프로젝트의 LLM 백엔드입니다.")
    try:
        result = subprocess.run(
            ["claude", "-p", build_prompt(question, hits), "--output-format", "json", "--model", model],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise JudgeError(f"claude CLI 가 {timeout}초 안에 응답하지 않았습니다.") from exc
    except OSError as exc:
        # which 는 통과하는데 exec 에서 죽는 경우가 있다. 설치가 깨지면
        # PATH 에 실행 불가능한 파일만 남는다 (Errno 8 Exec format error).
        raise JudgeError(f"claude CLI 를 실행할 수 없습니다: {exc}") from exc
    if result.returncode != 0:
        raise JudgeError(f"claude CLI 실패 (exit {result.returncode}): {result.stderr[:300]}")
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise JudgeError(f"claude CLI 응답을 읽을 수 없습니다: {result.stdout[:200]!r}") from exc
    if envelope.get("is_error"):
        raise JudgeError(f"claude CLI 오류: {envelope.get('result', '')[:300]}")
    try:
        return parse_judgment(extract_json(envelope["result"]))
    except (KeyError, ValueError) as exc:
        raise JudgeError(f"판정 응답을 해석하지 못했습니다: {exc}") from exc
