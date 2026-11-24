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
    if result["status"] == "ready_with_warnings":
        # Advisory cases have no fix and are never projected as errors.
        assert all(issue["severity"] == "warning" and not issue.get("fix_ids")
                   for issue in result["issues"])
        assert result["fixes"] == []
        with pytest.raises(ValueError, match="blocking issue"):
            GenerationReadinessError(result, task_id="task-test")
        return
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
    {"argv": ["astrid", "<undeclared>"]},
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
    projected = {
        **fix, "id": "setup-1", "who": "human",
        "instruction": {"step": "Install it manually", "then_argv": [
            "astrid", "generate", "targets", "--placement", "<PLACEMENT>", "--json",
        ]},
        "placeholders": {"PLACEMENT": "The originally selected target or its explicit ComfyUI URL"},
    }
    assert result == {"entry": None, "target": None, "status": "blocked",
                      "issues": [{**issue, "fix_ids": ["setup-1"]}], "fixes": [projected],
                      "attempt_count": 0}
    assert all("argv" not in fix for fix in result["fixes"])
    error = GenerationReadinessError(copy.deepcopy(result))
    assert all("argv" not in fix for fix in error.to_envelope()["fixes"])
    assert error.recovery_command == ""


def test_generic_service_conflict_is_not_a_node_install_conflict():
    error = generation_error({"code": "conflict", "message": "A service write conflicted"})
    result = error.to_envelope()
    assert result["issues"][0]["code"] == "conflict"
    assert len(result["fixes"]) == 1 and result["fixes"][0]["who"] == "human"
    assert result["issues"][0]["fix_ids"] == [result["fixes"][0]["id"]]
    assert "resolve" in result["fixes"][0]["instruction"]["step"]
    assert "argv" not in result["fixes"][0]
    assert result["attempt_count"] == 0


def test_all_non_success_projections_invoke_validator(monkeypatch):
    from astrid.core.generation import setup_contract

    original = setup_contract.validate_setup_envelope
    seen = []

    def check(envelope):
        seen.append(envelope["status"])
        return original(envelope)

    monkeypatch.setattr(setup_contract, "validate_setup_envelope", check)
    readiness_envelope(issues=[{"code": "pack_version_differs", "severity": "warning",
                                "message": "Observed version differs", "details": {}}])
    readiness_envelope(issues=[_issue()], fixes=[_fix()])
    readiness_envelope(
        issues=[{"code": "missing_node_pack", "severity": "error", "message": "Missing", "details": {}}],
        fixes=[{"scope": "attached_target", "details": {"action": "Identify the providing pack."}}],
    )
    assert seen == ["ready_with_warnings", "blocked", "blocked"]


def test_bare_or_dangling_error_fails_closed_even_without_attempt_count():
    for issue in (
        {"code": "missing_model", "severity": "error", "message": "Missing", "details": {}},
        {"code": "missing_model", "severity": "error", "message": "Missing", "details": {},
         "fix_ids": ["absent"]},
    ):
        with pytest.raises(ValueError):
            readiness_envelope(issues=[issue])


def test_existing_error_is_revalidated_before_serialization():
    error = GenerationReadinessError(readiness_envelope(issues=[_issue()], fixes=[_fix()]))
    error.readiness["issues"][0]["fix_ids"].clear()
    with pytest.raises(ValueError):
        error.to_envelope()
    with pytest.raises(ValueError):
        generation_error(error)


def test_engine_and_pack_version_codes_are_distinct_and_preserved_together():
    from astrid.core.generation.preflight import compare_versions
    from astrid.core.generation.setup_contract import SETUP_ISSUE_CODES

    assert {"version_drift", "pack_version_differs", "model_download_notice", "missing_node_pack"} <= SETUP_ISSUE_CODES
    drift = compare_versions({"comfyui": "1.0"}, {"comfyui": "2.0"})
    pack = {"code": "pack_version_differs", "severity": "warning",
            "message": "Selected pack version differs", "details": {"recorded": "1", "selected": "2"}}
    notice = {"code": "model_download_notice", "severity": "warning",
              "message": "Model download may need space", "details": {}}
    result = readiness_envelope(issues=[*drift, pack, notice])
    assert result["issues"] == [*drift, pack, notice]
    assert [issue["code"] for issue in result["issues"]] == [
        "version_drift", "pack_version_differs", "model_download_notice",
    ]
    assert result["status"] == "ready_with_warnings"
    assert result["fixes"] == []


def test_model_download_notice_cannot_be_blocking():
    with pytest.raises(ValueError, match="advisory"):
        readiness_envelope(issues=[_issue("model_download_notice")], fixes=[_fix()])


def test_redacted_fix_retains_contract_and_cannot_be_auto_executed():
    from astrid.core.generation.setup_contract import validate_setup_envelope

    fix = {**_fix(), "argv": ["astrid", "generate", "describe", "--entry", "api_key=private-fixture-value"]}
    error = GenerationReadinessError(readiness_envelope(issues=[_issue()], fixes=[fix]))
    result = error.to_envelope()
    validate_setup_envelope(result)
    assert "private-fixture-value" not in json.dumps(result)
    assert result["fixes"][0]["who"] == "human"
    assert result["fixes"][0]["argv"][-1] == "api_key=<REDACTED_VALUE>"
    assert "REDACTED_VALUE" in result["fixes"][0]["placeholders"]
    assert error.recovery_command == ""


def test_unregistered_native_error_retains_original_code_as_evidence():
    from astrid.core.generation.setup_contract import validate_setup_envelope

    result = generation_error({"code": "future_native_refusal", "message": "native failure"}).to_envelope()
    validate_setup_envelope(result)
    issue = result["issues"][0]
    assert issue["code"] == "generation_failed"
    assert issue["details"]["native_code"] == "future_native_refusal"
    assert issue["fix_ids"] == [fix["id"] for fix in result["fixes"]]


def test_target_record_projection_is_shared_by_envelope_and_repair():
    from astrid.core.generation.setup_repairs import manual_setup_fix
    from astrid.core.generation.setup_contract import validate_setup_envelope
    from astrid.core.generation.targets import TargetRecord

    record = TargetRecord(endpoint="http://127.0.0.1:8188")
    expected = record.as_dict()
    fix = manual_setup_fix("inspect", "Inspect the selected target.", target=record,
                           scope="attached_target")
    result = readiness_envelope(target=record, issues=[_issue("target_unreachable", ["inspect"])],
                                fixes=[fix])
    validate_setup_envelope(result)
    assert result["target"] == expected
    assert fix["instruction"]["then_argv"] == [
        "astrid", "generate", "targets", "--placement", record.endpoint, "--json",
    ]
