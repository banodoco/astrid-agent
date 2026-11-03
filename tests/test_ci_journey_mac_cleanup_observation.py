"""External macOS final-uninstall diagnostics never grant stop authority."""

import argparse
from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey


def test_mac_observation_redacts_private_output_and_reads_support_files(tmp_path, monkeypatch):
    data = tmp_path / "data"
    support = data / "runtime"
    support.mkdir(parents=True)
    (support / "discovery.json").write_text(json.dumps({"pid": 42, "secret": "PRIVATE"}))
    (support / "catalog.json").write_text(json.dumps({"selected_realm_id": "private-realm",
                                                       "realms": [{"realm_id": "private-realm"}]}))
    commands = []
    def launchctl(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="state = SIGTERMed\npid = 42\nPRIVATE=secret\n",
                               stderr="PRIVATE stderr")
    monkeypatch.setattr(journey.subprocess, "run", launchctl)
    monkeypatch.setattr(journey.os, "getuid", lambda: 501)
    monkeypatch.setattr(journey, "runtime_processes", lambda _: [42])
    monkeypatch.setattr(journey, "process_birth", lambda _: "Mon Nov  2 12:00:00 2026")
    monkeypatch.setattr(journey, "sentinel_observation", lambda *_: {"alive": True, "exit_code": None,
                                                                     "observed_birth": "birth",
                                                                     "birth_error": "PRIVATE"})
    result = journey.mac_cleanup_observation(data, None, "birth")
    assert commands == [["launchctl", "print", "gui/501/haus.astrid.runtime"]]
    assert result["launchctl"] == {"status": "observed", "exit_code": 0, "state": "SIGTERMed", "pid": 42}
    assert result["owners"]["processes"] == [{"pid": 42, "birth": "Mon Nov  2 12:00:00 2026"}]
    assert result["discovery"] == {"status": "observed", "pid": 42}
    assert result["catalog"] == {"status": "observed", "selected_realm_present": True}
    assert result["sentinel"] == {"status": "observed", "alive": True, "exit_code": None}
    assert result["started_monotonic_ns"] <= result["finished_monotonic_ns"]
    assert "PRIVATE" not in json.dumps(result) and "private-realm" not in json.dumps(result)


def test_cleanup_json_refuses_symlink_oversize_and_nonregular(tmp_path):
    target = tmp_path / "target"
    target.write_text('{"secret":"private"}')
    link = tmp_path / "link"
    link.symlink_to(target)
    assert journey._cleanup_json(link) == ("unavailable", None)
    target.write_bytes(b"x" * 65537)
    assert journey._cleanup_json(target) == ("oversize", None)
    assert journey._cleanup_json(tmp_path) == ("not-regular", None)
    assert journey._cleanup_json(tmp_path / "missing") == ("absent", None)


@pytest.mark.parametrize("exit_code", [0, 37])
def test_cleanup_observes_command_boundaries_even_when_uninstall_fails(tmp_path, monkeypatch, exit_code):
    run_root = tmp_path / "run"
    root, data = run_root / "program", run_root / "data"
    evidence = run_root / "evidence" / "release0-to-release1"
    args = argparse.Namespace(run_root=run_root, root=root, data_root=data,
                              leg="release0-to-release1", mode="direct", shell="sh")
    monkeypatch.setattr(journey.sys, "platform", "darwin")
    monkeypatch.setattr(journey, "assert_host_available", lambda *args: None)
    monkeypatch.setattr(journey, "snapshot_failure_evidence", lambda *args: None)
    monkeypatch.setattr(journey, "runtime_processes", lambda *_: [])
    monkeypatch.setattr(journey, "sentinel_observation", lambda *_: {"alive": True})
    @contextmanager
    def sentinel(*_):
        yield SimpleNamespace(pid=1), "birth"
    monkeypatch.setattr(journey, "sentinel_process", sentinel)
    def lifecycle(*_):
        root.mkdir()
        (root / "current").write_text("v0")
        journey.write_json(evidence / "journey-result.json", {"status": "fail", "checks": {"semantic_data": "not-run"}})
        raise ValueError("primary failure")
    monkeypatch.setattr(journey, "_execute_owned", lifecycle)
    monkeypatch.setattr(journey, "installation", lambda *_: (None, data, {}))
    events = []
    def observe(*_):
        events.append("observe")
        return {"sentinel": {"status": "observed", "alive": len(events) == 1}}
    monkeypatch.setattr(journey, "safe_mac_cleanup_observation", observe)
    def uninstall(_installed, argv, _evidence, name, **_kwargs):
        events.append("uninstall")
        assert argv == ["install", "--uninstall", "--root", str(root), "--data-root", str(data)]
        assert name == "final-uninstall.log"
        if exit_code:
            raise RuntimeError("final-uninstall.log: exit 37; see private path")
        (root / "current").unlink()
    monkeypatch.setattr(journey, "installed_cli", uninstall)
    with pytest.raises(ValueError, match="primary failure"):
        journey.execute(args)
    result = journey.read_json(evidence / "journey-result.json")
    obs = result["mac_final_uninstall_observation"]
    assert events == ["observe", "uninstall", "observe"]
    assert obs["command"]["exit_code"] == exit_code
    assert obs["before"]["sentinel"]["alive"] is True
    assert obs["after"]["sentinel"]["alive"] is False
    assert obs["sentinel_alive_unchanged"] is False
    assert obs["workspace_semantics"] == "unavailable-no-read-only-producer"
    assert result["checks"]["semantic_data"] == "not-run"
    assert "private path" not in json.dumps(obs)


def test_capture_failure_is_nonfatal_and_does_not_disclose_exception(tmp_path, monkeypatch):
    def fail(*_):
        raise RuntimeError("PRIVATE")
    monkeypatch.setattr(journey, "mac_cleanup_observation", fail)
    result = journey.safe_mac_cleanup_observation(tmp_path, None, "birth")
    assert result["status"] == "unavailable"
    assert "PRIVATE" not in json.dumps(result)


def test_unknown_birth_is_not_reported_as_dead(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=113, stdout="state = running\npid = 123\n", stderr="PRIVATE"))
    monkeypatch.setattr(journey, "runtime_processes", lambda *_: [])
    monkeypatch.setattr(journey, "sentinel_observation", lambda *_: {"alive": False, "exit_code": None,
                                                                     "observed_birth": None})
    result = journey.mac_cleanup_observation(tmp_path, None, "birth")
    assert result["launchctl"]["state"] is None
    assert result["launchctl"]["pid"] is None
    assert result["sentinel"] == {"status": "unavailable"}


def test_sanitized_observation_survives_existing_receipt_staging(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    evidence = run_root / "evidence" / "release0-to-release1"
    receipt = evidence / "journey-result.json"
    journey.write_json(receipt, {"status": "fail"})
    before = {"sentinel": {"status": "observed", "alive": True}}
    after = {"sentinel": {"status": "unavailable"}}
    journey.record_mac_cleanup_observation(receipt, before, after, {"exit_code": 1})
    monkeypatch.setattr(journey, "capture_manager_state", lambda *_: None)
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *_: None)
    journey.snapshot_failure_evidence(evidence, run_root, run_root / "data", "after-cleanup")
    staged = journey.read_json(run_root / "evidence-upload" / "after-cleanup" / "journey-result.json")
    assert staged["mac_final_uninstall_observation"]["sentinel_alive_unchanged"] is None
    assert staged["mac_final_uninstall_observation"]["command"]["exit_code"] == 1
