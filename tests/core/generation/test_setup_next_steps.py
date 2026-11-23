"""P56 LE-1a: shared setup contract; cards add producers in the cases folder."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from astrid.core.generation.preflight import (
    GenerationReadinessError,
    generation_error,
    readiness_envelope,
)


def _case_rows():
    rows = []
    for path in sorted(Path(__file__).with_name("setup_next_steps_cases").glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"setup_case_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rows.extend((f"{path.stem}:{name}", code, run) for name, code, run in module.CASES)
    return rows


CASES = _case_rows()


def _assert_case_coverage(codes):
    from astrid.core.generation.setup_contract import SETUP_ISSUE_CODES

    assert set(codes) == SETUP_ISSUE_CODES, "every setup issue code needs a case, and every case needs a declared code"


def test_every_declared_code_has_a_case():
    _assert_case_coverage(code for _, code, _ in CASES)


def test_a_declared_code_without_a_case_fails_the_rule():
    codes = {code for _, code, _ in CASES}
    codes.remove("needs_consent")
    with pytest.raises(AssertionError, match="every setup issue code"):
        _assert_case_coverage(codes)


@pytest.mark.parametrize("case_name,code,run", CASES, ids=[name for name, _, _ in CASES])
def test_setup_case_has_a_traceable_next_step(case_name, code, run):
    from astrid.core.generation.setup_contract import validate_setup_envelope

    result = run()
    validate_setup_envelope(result)
    assert code in {issue["code"] for issue in result["issues"]}, case_name
    assert json.loads(json.dumps(result)) == result
    error = GenerationReadinessError(result, task_id="task-test")
    assert error.to_envelope() == {**result, "task_id": "task-test"}


def _issue(code="needs_consent", fix_ids=None):
    return {"code": code, "severity": "error", "message": "Contract fixture", "details": {"field": "nodes"},
            "fix_ids": ["inspect"] if fix_ids is None else fix_ids}


def _fix():
    return {"id": "inspect", "who": "agent", "scope": "read_only", "details": {},
            "argv": ["astrid", "generate", "describe", "--entry", "example", "--json"], "placeholders": {}}


def test_order_links_placeholders_and_attempt_count_survive_all_error_adapters():
    first = {"id": "choose", "who": "human", "scope": "read_only", "details": {},
             "instruction": {"step": "Choose <ENTRY> after inspecting the reported source.",
                             "then_argv": ["astrid", "generate", "describe", "--entry", "<ENTRY>"]},
             "placeholders": {"ENTRY": "The entry chosen by the person"}}
    second = _fix()
    issues = [_issue(fix_ids=["choose", "inspect"]), _issue("pin_conflict", ["inspect"])]
    envelope = readiness_envelope(issues=issues, fixes=[first, second], attempt_count=3)
    issues[0]["fix_ids"].clear()
    first["instruction"]["then_argv"].append("--dry-run")
    second["argv"].clear()
    assert envelope["issues"][0]["fix_ids"] == ["choose", "inspect"]
    for error in (GenerationReadinessError(envelope), generation_error(envelope)):
        result = error.to_envelope()
        assert result["attempt_count"] == 3
        assert [fix["id"] for fix in result["fixes"]] == ["choose", "inspect"]
        assert error.recovery_command == ""  # AstridError's absent-command spelling; never skip the human step
        result["fixes"][0]["instruction"]["then_argv"].clear()
        assert error.to_envelope()["fixes"][0]["instruction"]["then_argv"]


def test_generation_error_links_its_one_issue_to_the_explicit_fixes():
    error = generation_error(ValueError("fixture"), code="needs_consent", fixes=[_fix()], attempt_count=2)
    assert error.to_envelope()["issues"][0]["fix_ids"] == ["inspect"]
    assert error.to_envelope()["attempt_count"] == 2
    assert error.recovery_command == "astrid generate describe --entry example --json"


@pytest.mark.parametrize("who,placeholders,argv,expected", [
    ("human", {}, ["astrid", "generate", "list"], ""),
    ("agent", {"ENTRY": "Choose an entry"}, ["astrid", "generate", "describe", "--entry", "<ENTRY>"], ""),
    ("agent", {}, ["astrid", "generate", "list"], "astrid generate list"),
])
def test_only_concrete_agent_commands_become_recovery_commands(who, placeholders, argv, expected):
    fix = {**_fix(), "who": who, "placeholders": placeholders, "argv": argv}
    error = GenerationReadinessError(readiness_envelope(issues=[_issue()], fixes=[fix]))
    assert error.recovery_command == expected


@pytest.mark.parametrize("change", [
    {"who": "someone"}, {"id": ""}, {"scope": "unknown"}, {"argv": []},
    {"argv": ["astrid", "--dry-run"]}, {"argv": "astrid generate list"},
    {"instruction": {"step": "Both actions are invalid", "then_argv": ["astrid"]}},
    {"placeholders": {"UNUSED": "unused"}}, {"argv": ["astrid", "<UNDECLARED>"]},
    {"unrecognized_action": "shell"},
])
def test_malformed_fixes_are_rejected(change):
    with pytest.raises(ValueError):
        readiness_envelope(issues=[_issue()], fixes=[{**_fix(), **change}])


@pytest.mark.parametrize("instruction", [None, {}, {"step": "do something"},
    {"step": "", "then_argv": ["astrid"]}, {"step": "inspect", "then_argv": []}])
def test_instructions_require_words_and_a_followup_command(instruction):
    fix = _fix()
    del fix["argv"]
    fix["instruction"] = instruction
    with pytest.raises(ValueError):
        readiness_envelope(issues=[_issue()], fixes=[fix])


@pytest.mark.parametrize("issues,fixes", [
    ([_issue("not_a_declared_setup_code")], [_fix()]),
    ([_issue(fix_ids=[])], [_fix()]),
    ([_issue(fix_ids=["missing"])], [_fix()]),
    ([_issue()], []),
    ([_issue()], [_fix(), _fix()]),
    ([], [_fix()]),
    ([_issue(fix_ids=["second", "inspect"])], [_fix(), {**_fix(), "id": "second"}]),
])
def test_unknown_codes_dangling_links_or_ambiguous_order_are_rejected(issues, fixes):
    with pytest.raises(ValueError):
        readiness_envelope(issues=issues, fixes=fixes, attempt_count=0)


@pytest.mark.parametrize("attempt", [-1, True, 1.5, "1"])
def test_attempt_count_is_a_nonnegative_integer(attempt):
    with pytest.raises(ValueError):
        readiness_envelope(issues=[_issue()], fixes=[_fix()], attempt_count=attempt)


def test_warnings_do_not_invent_a_repair():
    warning = {**_issue("pack_version_differs", []), "severity": "warning"}
    result = readiness_envelope(issues=[warning], attempt_count=0)
    assert result["status"] == "ready_with_warnings" and result["fixes"] == []
    with pytest.raises(ValueError, match="blocking issue"):
        GenerationReadinessError(result)
    with pytest.raises(ValueError):
        readiness_envelope(issues=[{**warning, "fix_ids": ["inspect"]}], fixes=[_fix()])


def test_legacy_envelopes_remain_unchanged_until_their_mapping_card():
    issue = {"code": "missing_node_pack", "severity": "error", "message": "Install it manually", "details": {}}
    fix = {"scope": "attached_target", "details": {"action": "Install it manually"}}
    result = readiness_envelope(issues=[issue], fixes=[fix])
    assert result == {"entry": None, "target": None, "status": "blocked", "issues": [issue], "fixes": [fix]}
    assert "argv" not in json.dumps(result)
    assert "argv" not in json.dumps(GenerationReadinessError(copy.deepcopy(result)).to_envelope())
