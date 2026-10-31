"""Executable-boundary coverage for the Release-2 journey runner."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import journey_release2 as release2
from scripts import release2_executor as executor
from scripts import check_journey_evidence, journey_matrix
from scripts import ci_windows_journey

BOUNDARY = r"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

PROTOCOL = "astrid-release2-boundary/v1"
state_path = Path(sys.argv[1])
evidence_kind = sys.argv[2]
source_only = sys.argv[3] == "true"
synthetic = sys.argv[4] == "true"
state = json.loads(state_path.read_text()) if state_path.exists() else {"actions": [], "phase": "release1"}
request = json.loads(sys.stdin.read())
action = request["action"]
inputs = request["inputs"]
scenario = request["scenario"]
payload = request["payload"]
manager = {"macos": "launchd", "linux": "systemd-user", "windows": "task-scheduler"}[scenario["os"]]
runner_os = {"macos": "macOS", "linux": "Linux", "windows": "Windows"}[scenario["os"]]
native = {"macos": "darwin", "linux": "linux", "windows": "windows"}[scenario["os"]]

if action == "inspect-release1":
    observation = {
        "release": 1,
        "candidate_checkouts": {name: {"sha": sha} for name, sha in inputs["candidate"].items()},
        "semantic_data": {"sha256": "a" * 64},
        "os_build": runner_os + "-disposable-boundary",
        "service_manager": {
            "manager": manager,
            "version": "disposable-v1",
            "foreground_fallback": False,
        },
    }
elif action == "button-unsaved-draft":
    if state["phase"] != "release1":
        raise SystemExit(3)
    observation = {
        "surface": "browser",
        "save": "failed",
        "update_requests": 0,
        "native_jobs": 0,
        "installed_release": 1,
    }
elif action == "button-update-lost-ack":
    if state["phase"] != "release1" or payload["request"] != "explicit-update":
        raise SystemExit(4)
    state["phase"] = "recovery-needed"
    state["operation_id"] = "operation-disposable-1"
    observation = {
        "states": ["active", "recovery-needed"],
        "automatic_update_requests": 0,
        "terminal_inferred": False,
        "operation_id": state["operation_id"],
    }
elif action == "button-update-explicit":
    if state["phase"] != "recovery-needed" or payload["operation_id"] != state["operation_id"]:
        raise SystemExit(5)
    state["phase"] = "release2"
    observation = {
        "states": ["recovery-needed", "active", "terminal"],
        "action": "explicit-update",
        "reconciled_under_lock": True,
        "logical_acceptances": 1,
        "new_operation_id": False,
        "result": "updated",
        "operation_id": state["operation_id"],
        "native_job": {
            "format": "astrid-update-platform/v1",
            "system": native,
            "one_shot": True,
            "owned": True,
            "acknowledged": True,
            "operation_id": state["operation_id"],
        },
        "draft_control": {
            "surface": "browser",
            "save": "pass",
            "update_requests": 1,
            "native_jobs": 1,
            "installed_release": 2,
        },
    }
elif action == "install-line-update":
    if state["phase"] != "release1":
        raise SystemExit(6)
    state["phase"] = "release2"
    observation = {
        "channel": "install-line",
        "entrypoint": "public-bootstrap",
        **payload,
    }
elif action == "pinokio-update":
    if state["phase"] != "release1":
        raise SystemExit(7)
    state["phase"] = "release2"
    observation = {
        "channel": "pinokio-update",
        "entrypoint": "pinokio-update",
        "managed_by": "pinokio",
        **payload,
        "action_sha256": "b" * 64,
    }
elif action == "inspect-release2":
    if state["phase"] != "release2":
        raise SystemExit(8)
    target = inputs["stand_in"]["target"]
    observation = {
        "installed": {
            "release": 2,
            "qualification_marker": inputs["stand_in_marker"],
            "manifest_sha256": inputs["stand_in"]["manifest_sha256"],
            "agent": target["agent"],
            "runtime": target["runtime"],
            "schema_digest": target["schema_digest"],
        },
        "semantic_data": {"sha256": "a" * 64},
        "service": "pass",
        "health": "pass",
        "task_settled": "pass",
        "one_runtime": "pass",
        "unrelated_sentinel": "pass",
    }
elif action == "cleanup":
    observation = {"cleanup": "pass"}
else:
    raise SystemExit(9)

state["actions"].append(action)
state_path.write_text(json.dumps(state))
print(json.dumps({
    "protocol": PROTOCOL,
    "sequence": request["sequence"],
    "action": action,
    "status": "pass",
    "evidence": {
        "kind": evidence_kind,
        "synthetic": synthetic,
        "source_only": source_only,
    },
    "observation": observation,
}))
"""


def scenario(channel, mode, os_name="linux"):
    return next(
        row
        for row in release2.RELEASE2_SCENARIOS
        if row["channel"] == channel and row["mode"] == mode and row["os"] == os_name
    )


def make_boundary(
    tmp_path: Path,
    *,
    evidence_kind="disposable-executable",
    source_only=False,
    synthetic=False,
):
    script = tmp_path / "release2-boundary"
    script.write_text(BOUNDARY)
    script.chmod(0o755)
    state = tmp_path / "boundary-state.json"
    command = (
        str(script),
        str(state),
        evidence_kind,
        str(source_only).lower(),
        str(synthetic).lower(),
    )
    return command, state


def run_scenario(
    tmp_path: Path,
    row,
    *,
    boundary_kind="disposable-executable",
    evidence_kind=executor.DISPOSABLE_EVIDENCE,
    source_only=False,
    synthetic=False,
    environment=None,
    candidate=None,
):
    command, state = make_boundary(
        tmp_path,
        evidence_kind=boundary_kind,
        source_only=source_only,
        synthetic=synthetic,
    )
    receipt_path = tmp_path / "journey-result.json"
    receipt = executor.execute_journey(
        scenario_id=row["scenario_id"],
        candidate=candidate or release2.RELEASE1_CANDIDATE,
        stand_in_marker=release2.STAND_IN_MARKER,
        channel=row["channel"],
        mode=row["mode"],
        os_name=row["os"],
        job_id=row["job_id"],
        runner=row["runner"],
        shell=row["shell"],
        stand_in_manifest=release2.STAND_IN_MANIFEST,
        stand_in_descriptor=release2.STAND_IN_DESCRIPTOR,
        fixture_sums=release2.FIXTURE_SUMS,
        boundary_command=command,
        root=(tmp_path / "install").resolve(),
        data_root=(tmp_path / "data").resolve(),
        receipt_path=receipt_path,
        evidence_kind=evidence_kind,
        environment=environment,
    )
    assert json.loads(receipt_path.read_text()) == receipt
    return receipt, json.loads(state.read_text()), receipt_path


@pytest.mark.parametrize(
    "row",
    release2.RELEASE2_SCENARIOS,
    ids=[row["scenario_id"] for row in release2.RELEASE2_SCENARIOS],
)
def test_executor_runs_every_declared_os_channel_and_mode(tmp_path, row):
    receipt, state, _ = run_scenario(tmp_path, row)
    route_actions = {
        "button": ["button-update-lost-ack", "button-update-explicit"],
        "install-line": ["install-line-update"],
        "pinokio-update": ["pinokio-update"],
    }
    expected_actions = ["inspect-release1", *route_actions[row["channel"]]]
    if row["channel"] == "button" and row["os"] == "linux":
        expected_actions.insert(1, "button-unsaved-draft")
    expected_actions.extend(("inspect-release2", "cleanup"))
    assert receipt["status"] == "exercised"
    assert [action["action"] for action in receipt["actions"]] == expected_actions
    assert state["actions"] == [action["action"] for action in receipt["actions"]]
    assert [action["sequence"] for action in receipt["actions"]] == list(
        range(1, len(expected_actions) + 1)
    )
    assert all(
        action["scenario"]["os"] == row["os"] and action["scenario"]["mode"] == row["mode"]
        for action in receipt["actions"]
    )
    assert receipt["scenario_id"] == row["scenario_id"]
    assert receipt["mode"] == row["mode"]
    assert receipt["installed"]["manifest_sha256"] == release2.STAND_IN_MANIFEST_SHA256
    assert receipt["installed"]["release"] == 2
    assert receipt["installed"]["qualification_marker"] == release2.STAND_IN_MARKER
    inspection = next(action for action in receipt["actions"] if action["action"] == "inspect-release2")
    assert inspection["observation"]["installed"] == receipt["installed"]
    assert all(value == "pass" for value in receipt["checks"].values())


@pytest.mark.parametrize(("channel", "mode"), (
    ("button", "direct"), ("install-line", "direct"),
    ("pinokio-update", "pinokio-backend"), ("pinokio-update", "pinokio-fully-local"),
))
@pytest.mark.parametrize("missing", (False, True), ids=("wrong", "missing"))
def test_executor_rejects_wrong_or_missing_visible_r2_identity(tmp_path, monkeypatch, channel, mode, missing):
    change = ('observation["installed"].pop("qualification_marker")' if missing else
              'observation["installed"]["qualification_marker"] = "wrong"')
    modified = BOUNDARY.replace('elif action == "cleanup":',
                                '    ' + change + '\nelif action == "cleanup":')
    monkeypatch.setitem(globals(), "BOUNDARY", modified)
    with pytest.raises(executor.ExecutionError, match="installed stand-in qualification_marker"):
        run_scenario(tmp_path, scenario(channel, mode))
    failed = json.loads((tmp_path / "journey-result.json").read_text())
    assert failed["status"] == "fail"
    assert failed["checks"]["marker"] == "not-run"


def test_shared_m4_5_aggregate_rejects_disposable_receipts_duplicates_and_missing_rows(tmp_path):
    assert tuple(row["scenario_id"] for row in journey_matrix.M4_5_MATRIX) == release2.RELEASE2_SCENARIO_IDS
    for row in release2.RELEASE2_SCENARIOS:
        output = tmp_path / row["scenario_id"]
        output.mkdir()
        run_scenario(output, row)
    errors = check_journey_evidence.evaluate_m4_5_matrix(tmp_path)
    assert sum("only a hosted-lifecycle pass is admissible" in error for error in errors) == 12

    receipt = tmp_path / release2.RELEASE2_SCENARIOS[0]["scenario_id"] / "journey-result.json"
    invalid_row = json.loads(receipt.read_text())
    invalid_row.update(status="pass", evidence_kind="hosted-lifecycle", os="windows",
                       final_cleanup="fail")
    invalid_row["candidate"]["app"] = "0" * 40
    receipt.write_text(json.dumps(invalid_row))
    errors = check_journey_evidence.evaluate_m4_5_matrix(tmp_path)
    assert any("os does not match" in error for error in errors)
    assert any("candidate" in error for error in errors)
    assert any("final cleanup did not pass" in error for error in errors)

    duplicate = tmp_path / "duplicate" / "journey-result.json"
    duplicate.parent.mkdir()
    duplicate.write_bytes(receipt.read_bytes())
    errors = check_journey_evidence.evaluate_m4_5_matrix(tmp_path)
    assert any("duplicate M4-5 receipt" in error for error in errors)

    duplicate.unlink()
    receipt.unlink()
    errors = check_journey_evidence.evaluate_m4_5_matrix(tmp_path)
    assert any("missing M4-5 receipt" in error for error in errors)


def test_windows_release2_boundary_args_are_individually_quoted_for_powershell_51():
    command = ci_windows_journey.powershell5_command(
        r"C:\Program Files\Astrid\boundary.exe",
        [r"C:\Agent Work\app path", "quote'and space"],
    )
    assert command[:5] == ["powershell.exe", "-NoProfile", "-NonInteractive",
                           "-ExecutionPolicy", "Bypass"]
    assert "'C:\\Program Files\\Astrid\\boundary.exe'" in command[-1]
    assert "'C:\\Agent Work\\app path'" in command[-1]
    assert "'quote''and space'" in command[-1]


def test_linux_unsaved_draft_is_a_real_zero_update_executable_control(tmp_path):
    receipt, state, _ = run_scenario(tmp_path, scenario("button", "direct"))
    negative = next(
        control
        for control in receipt["controls"]
        if control["control"] == "unsaved-draft" and control["polarity"] == "negative"
    )
    assert negative == {
        "control": "unsaved-draft",
        "polarity": "negative",
        "surface": "browser",
        "save": "failed",
        "update_requests": 0,
        "native_jobs": 0,
        "installed_release": 1,
    }
    assert state["actions"][1:3] == ["button-unsaved-draft", "button-update-lost-ack"]
    assert receipt["checks"]["draft_controls"] == "pass"


def test_lost_ack_stops_at_recovery_needed_until_explicit_update(tmp_path):
    receipt, state, _ = run_scenario(tmp_path, scenario("button", "direct"))
    actions = [action["action"] for action in receipt["actions"]]
    assert actions == [
        "inspect-release1",
        "button-unsaved-draft",
        "button-update-lost-ack",
        "button-update-explicit",
        "inspect-release2",
        "cleanup",
    ]
    negative = next(
        control
        for control in receipt["controls"]
        if control["control"] == "lost-acknowledgement" and control["polarity"] == "negative"
    )
    positive = next(
        control
        for control in receipt["controls"]
        if control["control"] == "lost-acknowledgement" and control["polarity"] == "positive"
    )
    assert negative["states"] == ["active", "recovery-needed"]
    assert negative["automatic_update_requests"] == 0
    assert positive["states"] == ["recovery-needed", "active", "terminal"]
    assert positive["action"] == "explicit-update"
    assert positive["operation_id"] == negative["operation_id"]
    assert positive["new_operation_id"] is False
    assert state["phase"] == "release2"


def test_disposable_execution_cannot_be_admitted_as_a_hosted_pass(tmp_path):
    receipt, _, _ = run_scenario(tmp_path, scenario("install-line", "direct"))
    errors = release2.receipt_errors(receipt)
    assert "receipt status is not pass" in errors
    assert "source or synthetic evidence cannot satisfy a hosted lifecycle receipt" in errors
    assert len(errors) == 2


@pytest.mark.parametrize(
    ("source_only", "synthetic", "message"),
    (
        (True, False, "source-only"),
        (False, True, "synthetic"),
        (False, False, "hosted OS build"),
    ),
)
def test_hosted_execution_rejects_source_synthetic_or_disposable_boundary_evidence(
    tmp_path, source_only, synthetic, message
):
    row = scenario("install-line", "direct")
    command, _ = make_boundary(
        tmp_path,
        evidence_kind="installed-executable",
        source_only=source_only,
        synthetic=synthetic,
    )
    receipt_path = tmp_path / "journey-result.json"
    with pytest.raises(executor.ExecutionError, match=message):
        executor.execute_journey(
            scenario_id=row["scenario_id"],
            candidate=release2.RELEASE1_CANDIDATE,
            stand_in_marker=release2.STAND_IN_MARKER,
            channel=row["channel"],
            mode=row["mode"],
            os_name=row["os"],
            job_id=row["job_id"],
            runner=row["runner"],
            shell=row["shell"],
            stand_in_manifest=release2.STAND_IN_MANIFEST,
            stand_in_descriptor=release2.STAND_IN_DESCRIPTOR,
            fixture_sums=release2.FIXTURE_SUMS,
            boundary_command=command,
            root=(tmp_path / "install").resolve(),
            data_root=(tmp_path / "data").resolve(),
            receipt_path=receipt_path,
            evidence_kind=executor.HOSTED_EVIDENCE,
            environment={
                "CI": "true",
                "GITHUB_ACTIONS": "true",
                "RUNNER_OS": "Linux",
                "ImageOS": "ubuntu24",
                "ImageVersion": "20261030.1",
            },
        )
    failed = json.loads(receipt_path.read_text())
    assert failed["status"] == "fail"
    assert failed["evidence_kind"] == "hosted-lifecycle"
    assert failed["checks"]["release"] == "not-run"


def test_executor_rejects_changed_immutable_candidate_before_boundary(tmp_path):
    row = scenario("pinokio-update", "pinokio-backend")
    candidate = deepcopy(release2.RELEASE1_CANDIDATE)
    candidate["runtime"] = "f" * 40
    command, state = make_boundary(tmp_path)
    with pytest.raises(ValueError, match="candidate runtime"):
        executor.execute_journey(
            scenario_id=row["scenario_id"],
            candidate=candidate,
            stand_in_marker=release2.STAND_IN_MARKER,
            channel=row["channel"],
            mode=row["mode"],
            os_name=row["os"],
            job_id=row["job_id"],
            runner=row["runner"],
            shell=row["shell"],
            stand_in_manifest=release2.STAND_IN_MANIFEST,
            stand_in_descriptor=release2.STAND_IN_DESCRIPTOR,
            fixture_sums=release2.FIXTURE_SUMS,
            boundary_command=command,
            root=(tmp_path / "install").resolve(),
            data_root=(tmp_path / "data").resolve(),
            receipt_path=tmp_path / "journey-result.json",
        )
    assert not state.exists()
