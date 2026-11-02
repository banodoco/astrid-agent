"""Focused coverage for passive installer predicate observations."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.installer import diagnostics, legacy_identity, update_job
from astrid.installer.core import recover_transaction, write_json
from astrid.installer.service import platform
from tests.test_installer_launchd_stop_refusals import _prepared


def _records_by_predicate(records):
    return {item["predicate"]: item for item in records}


def test_stop_diagnostic_reports_each_mac_owner_predicate_and_preserves_refusal(tmp_path, monkeypatch):
    backend, _data, _paths, _boundary, version, _outputs, queries, _bootouts, _stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    escaped = tmp_path / "bootstrap-python"
    escaped.write_text("#!/bin/sh\n", encoding="utf-8")
    version.python.unlink()
    version.python.symlink_to(escaped)
    monkeypatch.setattr(platform, "process_executable", lambda _pid: str(escaped))

    with diagnostics.capture() as records:
        with pytest.raises(ValueError, match="does not match the selected Runtime owner"):
            backend.service._launchd_live_pid_is_bound(
                {"program": str(version.python)}, {"state": "running", "pid": 777},
                owner_pid=777, owner_birth_id="birth-1",
            )

    observed = _records_by_predicate(records)
    assert {"launchd_state_is_running", "discovery_owner_pid_is_present",
            "launchd_pid_matches_discovery_pid", "discovery_birth_id_is_present",
            "observed_process_pid_matches_launchd_pid", "observed_process_uid_matches_current_user",
            "discovery_birth_matches_observed_birth", "observed_executable_matches_launchd_program",
            "observed_executable_within_selected_root"} <= set(observed)
    assert observed["observed_process_pid_matches_launchd_pid"]["result"] == "pass"
    assert observed["observed_process_uid_matches_current_user"]["result"] == "pass"
    assert observed["discovery_birth_matches_observed_birth"]["result"] == "pass"
    assert observed["observed_executable_matches_launchd_program"]["result"] == "pass"
    assert observed["observed_executable_within_selected_root"]["result"] == "fail"
    assert signals == []
    assert not any(argv[1:2] == ["bootout"] for argv in queries)


def test_stop_diagnostic_marks_dependent_checks_unavailable_without_owner_pid(tmp_path, monkeypatch):
    backend, _data, _paths, _boundary, version, _outputs, _queries, _bootouts, _stops, signals = _prepared(
        tmp_path, monkeypatch,
    )

    with diagnostics.capture() as records:
        with pytest.raises(ValueError, match="does not match the selected Runtime owner"):
            backend.service._launchd_live_pid_is_bound(
                {"program": str(version.python)}, {"state": "running", "pid": 777},
                owner_pid=None, owner_birth_id=None,
            )

    observed = _records_by_predicate(records)
    assert observed["discovery_owner_pid_is_present"]["result"] == "fail"
    assert observed["observed_process_pid_matches_launchd_pid"]["result"] == "unavailable"
    assert observed["observed_process_pid_matches_launchd_pid"]["depends_on"] == "launchd admission"
    assert signals == []


def test_discovery_diagnostic_keeps_missing_identity_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(update_job, "read_json", lambda _path: {
        "runtime_instance_id": "", "endpoint": "https://example.test/runtime",
    })

    with diagnostics.capture() as records:
        with pytest.raises(update_job._IdentityUnavailable, match="identity is unavailable"):
            update_job._probe_identity(tmp_path)

    observed = _records_by_predicate(records)
    assert observed["runtime_instance_id_is_present"]["result"] == "fail"
    assert observed["runtime_endpoint_is_present"]["result"] == "pass"


def test_admission_diagnostic_keeps_foreign_service_bytes_untouched(tmp_path, monkeypatch):
    from tests.test_installer_release0_service_adoption import (
        _captured_r0_macos_definition,
        _posix_service,
    )

    manager, version, data, _fixture, _settings = _posix_service(tmp_path, monkeypatch, "darwin")
    foreign = _captured_r0_macos_definition(version, data).replace(
        str(manager.env_file).encode(), str(tmp_path / "foreign" / "service-env.json").encode(), 1,
    )
    manager.path.write_bytes(foreign)

    with diagnostics.capture() as records:
        with pytest.raises(ValueError, match="unknown or changed action/owner"):
            manager.publish_launch_admission(version, data)

    observed = _records_by_predicate(records)
    assert observed["existing_posix_service_is_regular"]["result"] == "pass"
    assert observed["service_selection_owner_matches"]["result"] == "pass"
    assert observed["launchd_action_is_known_owned_action"]["result"] == "fail"
    assert manager.path.read_bytes() == foreign
    assert not manager.env_file.exists()


def test_legacy_admission_reports_complete_problem_set_without_relaxing_it():
    with diagnostics.capture() as records:
        admitted, details = legacy_identity.classify(
            {"status": "not-ok", "runtime_instance_id": "one"},
            {"runtime_instance_id": "two"},
            {"root": "/missing", "env": {}}, {}, b"{}", Path("/missing/version.json"), {},
        )

    observed = _records_by_predicate(records)["legacy_schema_predicate_set"]
    assert admitted is False
    assert observed["result"] == "fail"
    assert observed["expected"] == []
    assert observed["observed"] == details["problems"]
    assert "health/discovery runtime instance ids do not match" in observed["observed"]


def test_recovery_diagnostic_marks_stop_unavailable_for_terminal_journal(tmp_path):
    root, data = tmp_path / "program", tmp_path / "data"
    root.mkdir()
    data.mkdir()
    write_json(root / "update-transaction.json", {"step": "complete"})
    calls = []

    with diagnostics.capture() as records:
        recover_transaction(
            root, data, object(), stop=lambda *_: calls.append("stop"),
            switch_old=lambda *_: calls.append("switch"), activate_old=lambda *_: calls.append("activate"),
        )

    observed = _records_by_predicate(records)
    assert observed["nonterminal_recovery_journal_is_present"]["result"] == "fail"
    assert observed["recovery_stop_admission"]["result"] == "unavailable"
    assert calls == []


def test_opt_in_stderr_redacts_endpoint_credentials(monkeypatch, capsys):
    monkeypatch.setenv(diagnostics.ENVIRONMENT_FLAG, "1")
    diagnostics.record(
        "discovery", "runtime_endpoint", expected="https://operator:secret@example.test:9443/private?q=token",
        observed="https://operator:secret@example.test:9443/private?q=token", result="fail",
        provenance="runtime/discovery.json",
    )

    payload = json.loads(capsys.readouterr().err.removeprefix(diagnostics.PREFIX))
    assert payload["expected"] == "https://example.test:9443/private"
    assert "secret" not in json.dumps(payload)
