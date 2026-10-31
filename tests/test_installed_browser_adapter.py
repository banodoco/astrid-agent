"""Focused process-boundary tests for the M4-5 installed/browser adapter."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import installed_browser_adapter as adapter
from scripts import journey_release2 as release2
from scripts import release2_executor

INTERFACE = r"""#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

PROTOCOL = "astrid-installed-browser-interface/v1"
surface = sys.argv[1]
behavior = sys.argv[2]
log_path = Path(sys.argv[3])
envelope = json.load(sys.stdin)
request = envelope["request"]
action = envelope["action"]
inputs = request["inputs"]
scenario = request["scenario"]
payload = request["payload"]
operation_id = "operation-observed-1"
with log_path.open("a", encoding="utf-8") as log:
    log.write(json.dumps({"surface": surface, "action": action}) + "\n")

if behavior == "fail-after-browser" and surface == "update" and action == "button-update-lost-ack":
    raise SystemExit(71)

if behavior == "slow-handoff" and action == "button-update-lost-ack":
    time.sleep(0.12)

if behavior == "invalid-json" and surface == "update":
    print("two json values {}")
    raise SystemExit(0)

if action == "inspect-release1":
    runner_os = {"macos": "macOS", "linux": "Linux", "windows": "Windows"}[scenario["os"]]
    manager = {"macos": "launchd", "linux": "systemd-user", "windows": "task-scheduler"}[scenario["os"]]
    observation = {
        "release": 1,
        "candidate_checkouts": {name: {"sha": sha} for name, sha in inputs["candidate"].items()},
        "semantic_data": {"sha256": "a" * 64},
        "os_build": runner_os + "-observed-process",
    }
    if scenario["mode"] == "direct":
        observation["service_manager"] = {
            "manager": manager,
            "version": "" if behavior == "empty-service-version" else "observed-v1",
            "foreground_fallback": False,
        }
elif action == "button-unsaved-draft":
    observation = {
        "surface": "browser",
        "save": "failed",
        "update_requests": 0,
        "native_jobs": 0,
        "installed_release": 1,
    }
elif action == "button-update-lost-ack":
    observation = {
        "states": ["active", "recovery-needed"],
        "automatic_update_requests": 0,
        "terminal_inferred": False,
        "operation_id": operation_id,
    }
elif action == "button-update-explicit":
    observed_operation = (
        "different-operation" if behavior == "operation-mismatch" and surface == "update"
        else operation_id
    )
    if surface == "browser":
        observation = {
            "states": ["recovery-needed", "active", "terminal"],
            "action": "explicit-update",
            "operation_id": observed_operation,
        }
        if scenario["os"] == "linux":
            observation["draft_control"] = {
                "surface": "browser",
                "save": "pass",
                "update_requests": 1,
                "native_jobs": 1,
                "installed_release": 2,
            }
    else:
        observation = {
            "states": ["recovery-needed", "active", "terminal"],
            "action": "explicit-update",
            "reconciled_under_lock": True,
            "logical_acceptances": 1,
            "new_operation_id": False,
            "result": "updated",
            "operation_id": observed_operation,
        }
        observation["native_job"] = {
            "format": "astrid-update-platform/v1",
            "system": {"macos": "darwin", "linux": "linux", "windows": "windows"}[scenario["os"]],
            "one_shot": True,
            "owned": True,
            "acknowledged": True,
            "operation_id": observed_operation,
        }
elif action == "install-line-update":
    observation = {
        "channel": "install-line",
        "entrypoint": "public-bootstrap",
        **payload,
    }
elif action == "pinokio-update":
    observation = {
        "channel": "pinokio-update",
        "entrypoint": "pinokio-update",
        "managed_by": "pinokio",
        **payload,
        "action_sha256": "b" * 64,
    }
elif action == "inspect-release2":
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
    raise SystemExit(90)

print(json.dumps({
    "protocol": PROTOCOL,
    "handoff_id": envelope["handoff_id"],
    "surface": surface,
    "action": action,
    "status": "pass",
    "evidence": {
        "kind": surface + "-process",
        "pid": os.getpid(),
        "synthetic": False,
        "source_only": False,
    },
    "observation": observation,
}))
"""


def _worker(tmp_path: Path) -> Path:
    worker = tmp_path / "installed browser worker.py"
    worker.write_text(INTERFACE, encoding="utf-8")
    worker.chmod(0o755)
    return worker


def _boundary_command(
    tmp_path: Path,
    *,
    installed_behavior: str = "normal",
    browser_behavior: str = "normal",
    update_behavior: str = "normal",
    evidence_kind: str = "disposable-executable",
    timeout: float | None = None,
) -> tuple[str, ...]:
    worker = _worker(tmp_path)
    process_log = tmp_path / "interface-processes.jsonl"
    return (
        sys.executable,
        str(Path(adapter.__file__).resolve()),
        "--installed",
        sys.executable,
        "--installed-arg",
        str(worker),
        "--installed-arg",
        "installed",
        "--installed-arg",
        installed_behavior,
        "--installed-arg",
        str(process_log),
        "--browser",
        sys.executable,
        "--browser-arg",
        str(worker),
        "--browser-arg",
        "browser",
        "--browser-arg",
        browser_behavior,
        "--browser-arg",
        str(process_log),
        "--update",
        sys.executable,
        "--update-arg",
        str(worker),
        "--update-arg",
        "update",
        "--update-arg",
        update_behavior,
        "--update-arg",
        str(process_log),
        "--evidence-kind",
        evidence_kind,
        *(() if timeout is None else ("--timeout", str(timeout))),
    )


def _run_scenario(
    tmp_path: Path,
    row: dict,
    *,
    installed_behavior: str = "normal",
    browser_behavior: str = "normal",
    update_behavior: str = "normal",
    boundary_evidence_kind: str = "disposable-executable",
    journey_evidence_kind: str = release2_executor.DISPOSABLE_EVIDENCE,
    environment: dict[str, str] | None = None,
    timeout: float | None = None,
):
    receipt_path = tmp_path / "journey-result.json"
    receipt = release2_executor.execute_journey(
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
        boundary_command=_boundary_command(
            tmp_path,
            installed_behavior=installed_behavior,
            browser_behavior=browser_behavior,
            update_behavior=update_behavior,
            evidence_kind=boundary_evidence_kind,
            timeout=timeout,
        ),
        root=(tmp_path / "install").resolve(),
        data_root=(tmp_path / "data root with spaces").resolve(),
        receipt_path=receipt_path,
        evidence_kind=journey_evidence_kind,
        environment=environment,
    )
    return receipt, receipt_path


def _initial_request(tmp_path: Path, row: dict, data_root: Path) -> dict:
    return {
        "protocol": adapter.BOUNDARY_PROTOCOL,
        "sequence": 1,
        "action": "inspect-release1",
        "scenario": {
            name: row[name] for name in ("scenario_id", "channel", "mode", "os", "job_id")
        },
        "inputs": {
            "candidate": release2.RELEASE1_CANDIDATE,
            "stand_in": release2.verified_stand_in(),
            "stand_in_marker": release2.STAND_IN_MARKER,
            "root": str(tmp_path / "install"),
            "data_root": str(data_root),
        },
        "payload": {},
    }


@pytest.mark.parametrize(
    "row",
    release2.RELEASE2_SCENARIOS,
    ids=[row["scenario_id"] for row in release2.RELEASE2_SCENARIOS],
)
def test_adapter_executes_all_twelve_declared_routes_through_separate_processes(tmp_path, row):
    receipt, _ = _run_scenario(tmp_path, row)

    assert receipt["status"] == "exercised"
    assert receipt["evidence_kind"] == "disposable-executable"
    assert len(release2.RELEASE2_SCENARIOS) == 12
    for action in receipt["actions"]:
        evidence = action["evidence"]
        assert evidence["kind"] == "disposable-executable"
        assert evidence["synthetic"] is False
        assert evidence["source_only"] is False
        assert all(interface["pid"] > 0 for interface in evidence["interfaces"])
        argv = [interface["command"]["argv"] for interface in evidence["interfaces"]]
        assert all(isinstance(command, list) for command in argv)

    journal_path = tmp_path / "data root with spaces" / "installed-browser-adapter.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "terminal"
    assert journal["cleanup"] == "pass"
    assert [item["action"] for item in journal["actions"]] == [
        item["action"] for item in receipt["actions"]
    ]


def test_linux_draft_refusal_never_crosses_the_update_interface(tmp_path):
    row = next(
        row
        for row in release2.RELEASE2_SCENARIOS
        if row["channel"] == "button" and row["os"] == "linux"
    )
    receipt, _ = _run_scenario(tmp_path, row)
    draft = next(
        action for action in receipt["actions"] if action["action"] == "button-unsaved-draft"
    )

    assert [item["surface"] for item in draft["evidence"]["interfaces"]] == ["browser"]
    assert draft["observation"] == {
        "surface": "browser",
        "save": "failed",
        "update_requests": 0,
        "native_jobs": 0,
        "installed_release": 1,
    }


def test_lost_ack_operation_is_journaled_and_reused_by_explicit_update(tmp_path):
    row = next(
        row
        for row in release2.RELEASE2_SCENARIOS
        if row["channel"] == "button" and row["os"] == "macos"
    )
    receipt, _ = _run_scenario(tmp_path, row)
    lost = next(
        action for action in receipt["actions"] if action["action"] == "button-update-lost-ack"
    )
    recovered = next(
        action for action in receipt["actions"] if action["action"] == "button-update-explicit"
    )

    assert lost["observation"]["operation_id"] == "operation-observed-1"
    assert recovered["payload"]["operation_id"] == lost["observation"]["operation_id"]
    assert recovered["observation"]["new_operation_id"] is False
    assert recovered["evidence"]["state_transition"] == {
        "from": "recovery-needed",
        "to": "release2",
    }


def test_adapter_fails_closed_when_browser_and_update_report_different_operations(tmp_path):
    row = next(
        row
        for row in release2.RELEASE2_SCENARIOS
        if row["channel"] == "button" and row["os"] == "linux"
    )
    with pytest.raises(release2_executor.ExecutionError, match="ambiguous operation_id"):
        _run_scenario(tmp_path, row, update_behavior="operation-mismatch")

    journal = json.loads(
        (tmp_path / "data root with spaces" / "installed-browser-adapter.json").read_text(
            encoding="utf-8"
        )
    )
    assert journal["phase"] == "recovery-needed"
    assert journal["operation_id"] == "operation-observed-1"
    assert journal["next_sequence"] == 4
    assert not (tmp_path / "journey-result.json").read_text().endswith('"status": "pass"\n')


def test_failure_after_browser_handoff_is_journaled_and_never_blindly_replayed(tmp_path):
    row = next(
        row
        for row in release2.RELEASE2_SCENARIOS
        if row["channel"] == "button" and row["os"] == "linux"
    )
    with pytest.raises(release2_executor.ExecutionError, match="update interface exited 71"):
        _run_scenario(tmp_path, row, update_behavior="fail-after-browser")

    journal_path = tmp_path / "data root with spaces" / "installed-browser-adapter.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "release1"
    assert journal["inflight"]["action"] == "button-update-lost-ack"
    assert journal["inflight"]["current_surface"] == "update"
    assert [item["surface"] for item in journal["inflight"]["completed_surfaces"]] == ["browser"]
    process_log = tmp_path / "interface-processes.jsonl"
    before = [json.loads(line) for line in process_log.read_text().splitlines()]
    assert (
        sum(item == {"surface": "browser", "action": "button-update-lost-ack"} for item in before)
        == 1
    )

    with pytest.raises(release2_executor.ExecutionError, match="request sequence"):
        _run_scenario(tmp_path, row, update_behavior="fail-after-browser")
    after = [json.loads(line) for line in process_log.read_text().splitlines()]
    assert after == before


def test_outer_required_invalid_observation_never_consumes_the_sequence(tmp_path):
    row = next(
        row
        for row in release2.RELEASE2_SCENARIOS
        if row["channel"] == "install-line" and row["os"] == "linux"
    )
    with pytest.raises(release2_executor.ExecutionError, match="service-manager version"):
        _run_scenario(tmp_path, row, installed_behavior="empty-service-version")

    journal_path = tmp_path / "data root with spaces" / "installed-browser-adapter.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "uninspected"
    assert journal["next_sequence"] == 1
    assert journal["actions"] == []
    assert journal["inflight"]["action"] == "inspect-release1"
    assert journal["inflight"]["current_surface"] is None
    assert [item["surface"] for item in journal["inflight"]["completed_surfaces"]] == ["installed"]
    process_log = tmp_path / "interface-processes.jsonl"
    before = process_log.read_text(encoding="utf-8")

    with pytest.raises(release2_executor.ExecutionError, match="refusing to replay"):
        _run_scenario(tmp_path, row, installed_behavior="empty-service-version")
    assert process_log.read_text(encoding="utf-8") == before


def test_multisurface_action_uses_one_deadline_not_one_timeout_per_surface(tmp_path):
    row = next(
        row
        for row in release2.RELEASE2_SCENARIOS
        if row["channel"] == "button" and row["os"] == "macos"
    )
    started = time.monotonic()
    with pytest.raises(release2_executor.ExecutionError, match="update interface timed out"):
        _run_scenario(
            tmp_path,
            row,
            browser_behavior="slow-handoff",
            update_behavior="slow-handoff",
            timeout=0.18,
        )
    assert time.monotonic() - started < 2

    journal = json.loads(
        (tmp_path / "data root with spaces" / "installed-browser-adapter.json").read_text()
    )
    assert journal["phase"] == "release1"
    assert journal["inflight"]["action"] == "button-update-lost-ack"
    assert journal["inflight"]["current_surface"] == "update"
    assert [item["surface"] for item in journal["inflight"]["completed_surfaces"]] == ["browser"]


def test_adapter_fails_closed_on_an_unavailable_or_malformed_interface(tmp_path):
    row = next(
        row
        for row in release2.RELEASE2_SCENARIOS
        if row["channel"] == "install-line" and row["os"] == "linux"
    )
    with pytest.raises(release2_executor.ExecutionError, match="invalid or ambiguous JSON"):
        _run_scenario(tmp_path, row, update_behavior="invalid-json")

    separate = tmp_path / "unavailable"
    separate.mkdir()
    command = list(_boundary_command(separate))
    update_index = command.index("--update") + 1
    command[update_index] = str(tmp_path / "does-not-exist")
    with pytest.raises(
        release2_executor.ExecutionError, match="update executable is not an executable"
    ):
        release2_executor.execute_journey(
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
            root=(separate / "install").resolve(),
            data_root=(separate / "data").resolve(),
            receipt_path=separate / "result.json",
        )


def test_adapter_rejects_one_command_vector_reused_for_two_surfaces(tmp_path, capsys):
    worker = _worker(tmp_path)
    argv = [
        "--installed",
        sys.executable,
        "--installed-arg",
        str(worker),
        "--installed-arg",
        "same",
        "--browser",
        sys.executable,
        "--browser-arg",
        str(worker),
        "--browser-arg",
        "same",
        "--update",
        sys.executable,
        "--update-arg",
        str(worker),
        "--update-arg",
        "update",
        "--evidence-kind",
        "disposable-executable",
    ]

    assert adapter.main(argv) == 1
    assert "distinct argv vectors" in capsys.readouterr().err


def test_installed_evidence_cannot_be_selected_outside_hosted_ci(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    with pytest.raises(adapter.AdapterError, match="hosted CI boundary"):
        adapter._validate_evidence_mode("installed-executable", {"scenario": {"os": "linux"}}, None)


def test_hosted_label_cannot_admit_unpinned_disposable_worker(tmp_path):
    row = next(
        row
        for row in release2.RELEASE2_SCENARIOS
        if row["channel"] == "install-line" and row["os"] == "linux"
    )
    hosted_environment = {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "Linux",
        "ImageOS": "ubuntu24",
        "ImageVersion": "20261031.1",
    }
    with pytest.raises(release2_executor.ExecutionError, match="registered artifact"):
        _run_scenario(
            tmp_path,
            row,
            boundary_evidence_kind="installed-executable",
            journey_evidence_kind=release2_executor.HOSTED_EVIDENCE,
            environment=hosted_environment,
        )
    failed = json.loads((tmp_path / "journey-result.json").read_text(encoding="utf-8"))
    assert failed["status"] == "fail"
    assert failed["actions"] == []


def test_registered_identity_covers_python_worker_bytes(tmp_path):
    worker = _worker(tmp_path)
    command = adapter._resolve_command(
        sys.executable,
        [str(worker), "installed", "normal", str(tmp_path / "log.jsonl")],
        "installed",
    )
    observed = adapter._command_provenance(
        surface="installed",
        command=command,
        artifact_paths=[str(worker)],
        mutable_arg_indices=[4],
        trusted_identities=[],
        require_trust=False,
    )
    trusted = adapter._command_provenance(
        surface="installed",
        command=command,
        artifact_paths=[str(worker)],
        mutable_arg_indices=[4],
        trusted_identities=[observed["identity_sha256"]],
        require_trust=True,
    )
    assert trusted["trusted"] is True

    worker.write_text(INTERFACE + "\n# changed bytes\n", encoding="utf-8")
    with pytest.raises(adapter.AdapterError, match="not registered"):
        adapter._command_provenance(
            surface="installed",
            command=command,
            artifact_paths=[str(worker)],
            mutable_arg_indices=[4],
            trusted_identities=[observed["identity_sha256"]],
            require_trust=True,
        )


def test_registered_identity_ignores_mutable_output_file_appearance(tmp_path):
    worker = _worker(tmp_path)
    output = tmp_path / "later-output.jsonl"
    command = adapter._resolve_command(
        sys.executable,
        [str(worker), "installed", "normal", str(output)],
        "installed",
    )
    undeclared = adapter._command_provenance(
        surface="installed",
        command=command,
        artifact_paths=[str(worker)],
        mutable_arg_indices=[],
        trusted_identities=[],
        require_trust=False,
    )
    observed = adapter._command_provenance(
        surface="installed",
        command=command,
        artifact_paths=[str(worker)],
        mutable_arg_indices=[4],
        trusted_identities=[],
        require_trust=False,
    )
    assert observed["identity_sha256"] != undeclared["identity_sha256"]

    output.write_text("mutable handoff output\n", encoding="utf-8")
    trusted = adapter._command_provenance(
        surface="installed",
        command=command,
        artifact_paths=[str(worker)],
        mutable_arg_indices=[4],
        trusted_identities=[observed["identity_sha256"]],
        require_trust=True,
    )

    assert trusted["identity_sha256"] == observed["identity_sha256"]
    assert trusted["trusted"] is True


def test_env_wrapped_worker_must_be_an_explicit_artifact(tmp_path):
    env = shutil.which("env")
    if env is None:
        pytest.skip("env executable is unavailable")
    worker = _worker(tmp_path)
    command = adapter._resolve_command(env, ["python3", str(worker)], "installed")
    observed = adapter._command_provenance(
        surface="installed",
        command=command,
        artifact_paths=[],
        mutable_arg_indices=[],
        trusted_identities=[],
        require_trust=False,
    )

    with pytest.raises(adapter.AdapterError, match="neither a registered artifact"):
        adapter._command_provenance(
            surface="installed",
            command=command,
            artifact_paths=[],
            mutable_arg_indices=[],
            trusted_identities=[observed["identity_sha256"]],
            require_trust=True,
        )


def test_py_exe_wrapped_worker_must_be_an_explicit_artifact(tmp_path):
    launcher = tmp_path / "py.exe"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    worker = _worker(tmp_path)
    command = adapter._resolve_command(str(launcher), [str(worker)], "installed")
    observed = adapter._command_provenance(
        surface="installed",
        command=command,
        artifact_paths=[],
        mutable_arg_indices=[],
        trusted_identities=[],
        require_trust=False,
    )

    with pytest.raises(adapter.AdapterError, match="neither a registered artifact"):
        adapter._command_provenance(
            surface="installed",
            command=command,
            artifact_paths=[],
            mutable_arg_indices=[],
            trusted_identities=[observed["identity_sha256"]],
            require_trust=True,
        )


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf"])
def test_cli_rejects_nonfinite_timeout(timeout, capsys):
    result = adapter.main(
        [
            "--installed",
            "missing-installed",
            "--browser",
            "missing-browser",
            "--update",
            "missing-update",
            "--evidence-kind",
            "disposable-executable",
            f"--timeout={timeout}",
        ]
    )

    assert result == 1
    assert "timeout must be positive" in capsys.readouterr().err


def test_process_cleanup_budget_fits_outer_timeout_safety_margin():
    worst_case_windows_cleanup = (
        adapter.PROCESS_TREE_KILL_TIMEOUT_SECONDS
        + adapter.PROCESS_PIPE_DRAIN_TIMEOUT_SECONDS
        + adapter.PROCESS_FINAL_WAIT_TIMEOUT_SECONDS
    )

    assert worst_case_windows_cleanup < adapter.OUTER_TIMEOUT_SAFETY_SECONDS


def test_adapter_rejects_raw_data_root_symlink(tmp_path):
    real = tmp_path / "real-data"
    real.mkdir()
    linked = tmp_path / "linked-data"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(adapter.AdapterError, match="must not be a symlink"):
        adapter._safe_data_root({"inputs": {"data_root": str(linked)}})


def test_surface_ownership_rejects_browser_manufactured_native_evidence():
    request = {"scenario": {"os": "linux"}}
    browser_observation = {
        "states": ["recovery-needed", "active", "terminal"],
        "action": "explicit-update",
        "operation_id": "operation-observed-1",
        "draft_control": {},
        "native_job": {"owned": True},
    }
    with pytest.raises(adapter.AdapterError, match="ownership mismatch"):
        adapter._validate_surface_observation(
            surface="browser",
            action="button-update-explicit",
            observation=browser_observation,
            request=request,
        )

    with pytest.raises(adapter.AdapterError, match="ownership mismatch"):
        adapter._validate_surface_observation(
            surface="update",
            action="button-update-explicit",
            observation={},
            request=request,
        )


def test_adapter_refuses_to_replay_a_durably_reserved_handoff(tmp_path):
    row = next(
        row
        for row in release2.RELEASE2_SCENARIOS
        if row["channel"] == "install-line" and row["os"] == "linux"
    )
    data_root = tmp_path / "data"
    data_root.mkdir()
    request = _initial_request(tmp_path, row, data_root)
    journal = adapter._new_journal(request)
    journal["inflight"] = {
        "sequence": 1,
        "action": "inspect-release1",
        "handoff_id": adapter._handoff_id(request),
        "current_surface": "installed",
        "completed_surfaces": [],
    }
    adapter._write_json(data_root / "installed-browser-adapter.json", journal)

    with pytest.raises(adapter.AdapterError, match="refusing to replay"):
        adapter._execute(
            request=request,
            commands={},
            evidence_kind="disposable-executable",
            timeout=1,
            journal_name="installed-browser-adapter.json",
        )


def test_action_deadline_bounds_held_journal_lock(tmp_path):
    row = next(
        row
        for row in release2.RELEASE2_SCENARIOS
        if row["channel"] == "install-line" and row["os"] == "linux"
    )
    data_root = tmp_path / "data"
    data_root.mkdir()
    request = _initial_request(tmp_path, row, data_root)
    lock_path = data_root / ".installed-browser-adapter.json.lock"
    ready_path = tmp_path / "lock-ready"
    holder_source = r"""
import os
import sys
import time
from pathlib import Path

lock_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
with lock_path.open("a+b") as handle:
    if os.name == "nt":
        import msvcrt
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    ready_path.write_text("ready", encoding="utf-8")
    time.sleep(5)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_source, str(lock_path), str(ready_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ready_deadline = time.monotonic() + 3
        while not ready_path.exists() and time.monotonic() < ready_deadline:
            time.sleep(0.01)
        assert ready_path.exists()

        started = time.monotonic()
        with pytest.raises(adapter.AdapterError, match="deadline expired.*journal lock"):
            adapter._execute(
                request=request,
                commands={},
                evidence_kind="disposable-executable",
                timeout=0.05,
                journal_name="installed-browser-adapter.json",
            )
        assert time.monotonic() - started < 0.2
    finally:
        holder.terminate()
        try:
            holder.wait(timeout=2)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=2)
