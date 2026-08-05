"""판단 모듈의 순수 로직 테스트. claude CLI 는 호출하지 않는다."""
import json
import subprocess

import pytest

from lawrag import judge as judge_mod
from lawrag.db import SearchHit
from lawrag.judge import (
    VERDICTS, JudgeError, Judgment, build_prompt, extract_json, judge, parse_judgment,
)


def hit(law="주식회사 등의 외부감사에 관한 법률", label="제21조", text="감사인은 ...",
        source_type="article", vec_rank=1, kw_rank=2, term_rank=None):
    return SearchHit(
        chunk_id=1, law_name=law, label=label,
        header=f"{law} {label}(감사인의 권한 등)", text=text,
        enforced="20250401", source_type=source_type,
        vec_rank=vec_rank, kw_rank=kw_rank, term_rank=term_rank, rrf=0.032,
    )


# --- 모델 출력에서 JSON 꺼내기 ------------------------------------------------

def test_extract_bare_json():
    assert extract_json('{"verdict": "정보 부족"}') == {"verdict": "정보 부족"}


def test_extract_fenced_json():
    raw = '```json\n{"verdict": "정보 부족"}\n```'
    assert extract_json(raw) == {"verdict": "정보 부족"}


def test_extract_json_with_surrounding_prose():
    raw = '판단 결과입니다.\n{"verdict": "정보 부족", "reason": "x"}\n이상입니다.'
    assert extract_json(raw)["verdict"] == "정보 부족"


def test_extract_json_failure_raises():
    with pytest.raises(ValueError):
        extract_json("JSON 이 전혀 없는 응답")


# --- 판단 결과 검증 -----------------------------------------------------------

def test_parse_judgment_ok():
    j = parse_judgment({
        "verdict": "위반 소지 있음",
        "cited": [{"law": "외부감사법", "article": "제21조", "clause": "제2항"}],
        "reason": "감사인의 재무제표 대리작성을 금지한다.",
        "confidence": "높음",
    })
    assert isinstance(j, Judgment)
    assert j.verdict == "위반 소지 있음"
    assert j.cited[0]["article"] == "제21조"


def test_unknown_verdict_is_rejected():
    """모델이 임의의 판정 문구를 지어내면 조용히 통과시키지 않는다."""
    with pytest.raises(ValueError):
        parse_judgment({"verdict": "완전 불법", "reason": "x"})


def test_missing_verdict_is_rejected():
    with pytest.raises(ValueError):
        parse_judgment({"reason": "x"})


def test_verdicts_are_the_three_agreed_values():
    assert VERDICTS == ("위반 소지 있음", "위반으로 보기 어려움", "정보 부족")


# --- 프롬프트 조립 ------------------------------------------------------------

def test_prompt_contains_question_and_articles():
    p = build_prompt("감사인이 재무제표를 대신 작성했다", [hit(text="감사인은 재무제표를 대표하여 작성할 수 없다")])
    assert "감사인이 재무제표를 대신 작성했다" in p
    assert "감사인은 재무제표를 대표하여 작성할 수 없다" in p


def test_prompt_numbers_the_sources():
    p = build_prompt("q", [hit(), hit(label="제39조")])
    assert "[1]" in p and "[2]" in p


def test_prompt_carries_enforcement_date():
    """답변이 시행일을 인용할 수 있어야 한다."""
    assert "20250401" in build_prompt("q", [hit()])


def sources_section(prompt):
    """지시문에도 '본문 미제공'과 '[참고 조문]'이 나오므로 마지막 조각만 떼어 본다."""
    return prompt.rsplit("[참고 조문]", 1)[1]


def test_reference_type_is_flagged_as_bodyless():
    """본문 API 가 없는 해석례는 '본문 미제공'으로 다르게 취급해야 한다."""
    p = build_prompt("q", [hit(source_type="reference")])
    assert "본문 미제공" in sources_section(p)


def test_article_type_is_not_flagged():
    p = build_prompt("q", [hit(source_type="article")])
    assert "본문 미제공" not in sources_section(p)


def test_prompt_forbids_outside_knowledge():
    p = build_prompt("q", [hit()])
    assert "제공된 조문" in p


def test_empty_hits_still_builds_prompt():
    assert build_prompt("q", [])


# --- 백엔드 실패 처리 ---------------------------------------------------------
# 데모 명령이 raw 트레이스백을 뱉지 않도록, 백엔드 쪽 실패는 전부 JudgeError 다.

def fake_run(stdout="", returncode=0, raises=None):
    def run(*args, **kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="boom")
    return run


@pytest.fixture
def cli_present(monkeypatch):
    monkeypatch.setattr(judge_mod.shutil, "which", lambda _: "/usr/local/bin/claude")


def test_missing_cli_raises_judge_error(monkeypatch):
    monkeypatch.setattr(judge_mod.shutil, "which", lambda _: None)
    with pytest.raises(JudgeError, match="찾을 수 없"):
        judge("q", [hit()])


def test_unexecutable_cli_raises_judge_error(cli_present, monkeypatch):
    # 이 머신의 실제 증상: which 는 성공하는데 exec 에서 죽는다 (Errno 8).
    err = OSError(8, "Exec format error", "claude")
    monkeypatch.setattr(judge_mod.subprocess, "run", fake_run(raises=err))
    with pytest.raises(JudgeError, match="실행할 수 없"):
        judge("q", [hit()])


def test_timeout_raises_judge_error(cli_present, monkeypatch):
    err = subprocess.TimeoutExpired(cmd="claude", timeout=180)
    monkeypatch.setattr(judge_mod.subprocess, "run", fake_run(raises=err))
    with pytest.raises(JudgeError, match="응답하지 않"):
        judge("q", [hit()])


def test_nonzero_exit_raises_judge_error(cli_present, monkeypatch):
    monkeypatch.setattr(judge_mod.subprocess, "run", fake_run(returncode=1))
    with pytest.raises(JudgeError, match="exit 1"):
        judge("q", [hit()])


def test_unreadable_envelope_raises_judge_error(cli_present, monkeypatch):
    monkeypatch.setattr(judge_mod.subprocess, "run", fake_run(stdout="not json at all"))
    with pytest.raises(JudgeError):
        judge("q", [hit()])


def test_error_envelope_raises_judge_error(cli_present, monkeypatch):
    envelope = json.dumps({"is_error": True, "result": "rate limited"})
    monkeypatch.setattr(judge_mod.subprocess, "run", fake_run(stdout=envelope))
    with pytest.raises(JudgeError, match="rate limited"):
        judge("q", [hit()])


def test_unparsable_model_output_raises_judge_error(cli_present, monkeypatch):
    envelope = json.dumps({"is_error": False, "result": "JSON 이 전혀 없는 답변"})
    monkeypatch.setattr(judge_mod.subprocess, "run", fake_run(stdout=envelope))
    with pytest.raises(JudgeError):
        judge("q", [hit()])


def test_successful_judgment(cli_present, monkeypatch):
    payload = {"verdict": "정보 부족", "reason": "근거 없음", "confidence": "낮음"}
    envelope = json.dumps({"is_error": False, "result": json.dumps(payload)})
    monkeypatch.setattr(judge_mod.subprocess, "run", fake_run(stdout=envelope))
    assert judge("q", [hit()]).verdict == "정보 부족"
