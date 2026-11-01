"""T19/T20 installer-only recovery plan/apply coverage."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from astrid.installer import maintenance
from astrid.installer.core import Version, read_json, write_json
from astrid.installer.service import LABEL, install_selection


def _files(root: Path) -> dict[str, bytes]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            result[str(path.relative_to(root))] = path.read_bytes()
    return result


class _Backend:
    def __init__(self) -> None:
        self.stop_error = False
        self.schema_error = False
        self.ready_error = False
        self.calls = []

    def maintenance_quiesce(self, version, data):
        self.calls.append(("quiesce", data))
        if self.stop_error:
            raise RuntimeError("native status unavailable")

    def maintenance_schema(self, version, data):
        self.calls.append(("schema", data))
        if self.schema_error:
            raise ValueError("schema unknown")
        return {"realm_id": "realm-1", "realm_root": str(data)}, {"version": 31, "target": 31}

    def maintenance_ready(self, version, data, realm):
        self.calls.append(("ready", data))
        if self.ready_error:
            raise RuntimeError("readiness failed")
        return {"ready_instance_id": "maintained-instance", "identity": version.document()["identity"]}


def _fixture(tmp_path, monkeypatch, case="M1"):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    root, data = tmp_path / "program", tmp_path / "workspace"
    version_root = root / "versions" / "retained-7"
    version_root.mkdir(parents=True)
    python = version_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_text("retained interpreter", encoding="utf-8")
    profile = version_root / "profile.json"
    write_json(profile, {"profile": "astrid"})
    version = Version(version_root, python, profile, {}, release=7)
    write_json(version_root / "version.json", version.document())
    write_json(version_root / "manifest.json", {"release": 7})
    (root / "current").write_text("retained-7\n", encoding="utf-8")
    data.mkdir()
    (data / "projects").mkdir()
    (data / "projects" / "project.json").write_text('{"name":"kept"}\n', encoding="utf-8")
    (data / "timelines").mkdir()
    (data / "timelines" / "history.json").write_text('{"history":[1,2]}\n', encoding="utf-8")
    (data / "media.bin").write_bytes(b"retained-media")
    job = {"state": "recovery-needed", "operation_id": "a" * 32, "old_instance_id": "old"}
    if case == "M1":
        job.pop("operation_id")
    elif case == "M3":
        job["old_instance_id"] = "restarted-runtime"
    elif case == "M4":
        job["platform"] = {"unknown": "native-domain"}
    elif case == "M5":
        job["runtime"] = "unavailable"
    elif case == "M6":
        job["program_root"] = str(root / "unsafe-other-root")
    marker = {"owner": LABEL, "data_root": str(data), "installation_generation": 4, "update_job": job}
    write_json(root / "install.json", marker)
    journal = b'{"unresolved":true,"state":"recovery-needed"}\n'
    if case == "M2":
        journal = b'{ interrupted journal'
    (root / "update-transaction.json").write_bytes(journal)
    selection = install_selection(home, os.sys.platform)
    write_json(selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data),
                           "identity": {"release": 7, "managed_by": None}})
    return root, data, selection, journal, _Backend()


@pytest.mark.parametrize("case", ["M1", "M2", "M3", "M4", "M5", "M6"])
def test_noncontinuable_states_have_supported_escape(tmp_path, monkeypatch, case):
    root, data, selection, journal, backend = _fixture(tmp_path, monkeypatch, case)
    marker_before = (root / "install.json").read_bytes()
    data_before = _files(data)

    plan = maintenance.plan_update_recovery(root, data)
    result = maintenance.apply_update_recovery(root, data, plan["plan_sha256"], backend=backend)

    receipt = result["receipt"]
    marker = read_json(root / "install.json")
    working = Path(receipt["working_data_path"])
    archived = Path(result["receipt_path"]).parent / "raw-evidence"
    assert marker["installation_generation"] == 5
    assert marker["update_job"] == read_json_bytes(marker_before)["update_job"]
    assert (root / "update-transaction.json").read_bytes() == journal
    assert _files(data) == data_before
    assert _files(working) == data_before
    assert (archived / "install").read_bytes() == marker_before
    assert (archived / "journal").read_bytes() == journal
    assert receipt["old_operation_disposition"] == "quarantined-unresolved"
    assert receipt["disposition"] == "restored-working-installation"
    assert receipt["ready_instance_id"] == "maintained-instance"
    assert read_json(selection)["data_root"] == str(working)
    assert plan["external_qualification"] == ["A6", "T22", "T23"]


def read_json_bytes(value: bytes) -> dict:
    import json

    return json.loads(value)


def _assert_source_unchanged(root, data, selection, marker, journal, data_files):
    assert (root / "install.json").read_bytes() == marker
    assert (root / "update-transaction.json").read_bytes() == journal
    assert _files(data) == data_files
    assert read_json(selection)["data_root"] == str(data)


def test_maintenance_faults_preserve_evidence_and_resume(tmp_path, monkeypatch):
    root, data, selection, journal, backend = _fixture(tmp_path, monkeypatch, "M2")
    marker = (root / "install.json").read_bytes()
    data_files = _files(data)
    plan = maintenance.plan_update_recovery(root, data)

    backend.stop_error = True
    with pytest.raises(maintenance.MaintenanceRefusal, match="quiescence") as stopped:
        maintenance.apply_update_recovery(root, data, plan["plan_sha256"], backend=backend)
    assert stopped.value.reason == "unknown-stop"
    _assert_source_unchanged(root, data, selection, marker, journal, data_files)

    backend.stop_error = False
    backend.schema_error = True
    with pytest.raises(maintenance.MaintenanceRefusal) as schema:
        maintenance.apply_update_recovery(root, data, plan["plan_sha256"], backend=backend)
    assert schema.value.reason == "unknown-schema"
    _assert_source_unchanged(root, data, selection, marker, journal, data_files)

    backend.schema_error = False
    backend.ready_error = True
    with pytest.raises(maintenance.MaintenanceRefusal) as readiness:
        maintenance.apply_update_recovery(root, data, plan["plan_sha256"], backend=backend)
    assert readiness.value.reason == "verification-failed"
    _assert_source_unchanged(root, data, selection, marker, journal, data_files)

    backend.ready_error = False
    resumed = maintenance.apply_update_recovery(root, data, plan["plan_sha256"], backend=backend)
    assert resumed["state"] == "restored-working-installation"
    assert read_json(root / "install.json")["installation_generation"] == 5


def test_missing_bundle_and_foreign_or_unsafe_paths_never_start_apply(tmp_path, monkeypatch):
    root, data, selection, journal, backend = _fixture(tmp_path, monkeypatch)
    marker = (root / "install.json").read_bytes()
    data_files = _files(data)
    current_marker = read_json(root / "install.json")
    current_marker["recovery_bundle_required"] = True
    write_json(root / "install.json", current_marker)
    blocked = maintenance.plan_update_recovery(root, data)
    with pytest.raises(maintenance.MaintenanceRefusal) as bundle:
        maintenance.apply_update_recovery(root, data, blocked["plan_sha256"], backend=backend)
    assert bundle.value.reason == "missing-recovery-bundle"
    assert not (root / "maintenance").exists()

    foreign = read_json(root / "install.json")
    foreign["owner"] = "foreign"
    write_json(root / "install.json", foreign)
    with pytest.raises(maintenance.MaintenanceRefusal) as ownership:
        maintenance.plan_update_recovery(root, data)
    assert ownership.value.reason == "unknown-ownership"
    assert (root / "update-transaction.json").read_bytes() == journal
    assert _files(data) == data_files

    foreign["owner"] = LABEL
    foreign.pop("recovery_bundle_required")
    write_json(root / "install.json", foreign)
    (data / "unsafe-link").symlink_to(data / "media.bin")
    with pytest.raises(maintenance.MaintenanceRefusal) as unsafe:
        maintenance.plan_update_recovery(root, data)
    assert unsafe.value.reason == "unsafe-path"
    assert (root / "install.json").read_bytes() == marker
    assert (root / "update-transaction.json").read_bytes() == journal


def test_interrupted_and_partial_copy_leave_originals_and_use_a_new_attempt(tmp_path, monkeypatch):
    root, data, selection, journal, backend = _fixture(tmp_path, monkeypatch)
    marker = (root / "install.json").read_bytes()
    data_files = _files(data)
    plan = maintenance.plan_update_recovery(root, data)
    original_copy = maintenance._copy_tree
    calls = []

    def interrupted(source, destination):
        calls.append(destination)
        destination.mkdir(parents=True)
        (destination / "partial").write_text("partial", encoding="utf-8")
        raise maintenance.MaintenanceRefusal("copy-interrupted", "simulated interrupted backup")

    monkeypatch.setattr(maintenance, "_copy_tree", interrupted)
    with pytest.raises(maintenance.MaintenanceRefusal) as interrupted_failure:
        maintenance.apply_update_recovery(root, data, plan["plan_sha256"], backend=backend)
    assert interrupted_failure.value.reason == "copy-interrupted"
    _assert_source_unchanged(root, data, selection, marker, journal, data_files)
    assert calls and (calls[0] / "partial").exists()

    monkeypatch.setattr(maintenance, "_copy_tree", original_copy)
    result = maintenance.apply_update_recovery(root, data, plan["plan_sha256"], backend=backend)
    assert result["state"] == "restored-working-installation"
    assert "attempt-2" in result["receipt_path"]


def test_partial_copy_verification_refuses_without_clearing_then_resumes(tmp_path, monkeypatch):
    root, data, selection, journal, backend = _fixture(tmp_path, monkeypatch)
    marker = (root / "install.json").read_bytes()
    data_files = _files(data)
    plan = maintenance.plan_update_recovery(root, data)
    original_copy = maintenance._copy_tree
    copies = 0

    def partial_copy(source, destination):
        nonlocal copies
        copies += 1
        original_copy(source, destination)
        if copies == 2:
            (destination / "partial-copy-marker").write_text("not a verified data copy", encoding="utf-8")

    monkeypatch.setattr(maintenance, "_copy_tree", partial_copy)
    with pytest.raises(maintenance.MaintenanceRefusal) as partial:
        maintenance.apply_update_recovery(root, data, plan["plan_sha256"], backend=backend)
    assert partial.value.reason == "copy-verification-failed"
    _assert_source_unchanged(root, data, selection, marker, journal, data_files)

    monkeypatch.setattr(maintenance, "_copy_tree", original_copy)
    result = maintenance.apply_update_recovery(root, data, plan["plan_sha256"], backend=backend)
    assert result["state"] == "restored-working-installation"
    assert "attempt-2" in result["receipt_path"]
