"""Durable acceptance and identity binding for click-created update jobs."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest
from runtime_protocol import platform

from astrid.installer import cli, update_job
from astrid.installer.core import Installer, Version, current, read_json, write_json
from astrid.installer.service import LABEL, install_selection
from astrid.installer.tools import DEFAULT_OMP_SOURCE


def _fixture(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData/Local"))
    root, data = tmp_path / "Astrid Program", tmp_path / "Workspace"
    old_root = root / "versions" / "old-7"
    old_root.mkdir(parents=True)
    python = old_root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("python placeholder")
    profile = old_root / "profile.json"
    write_json(profile, {"profile": "astrid"})
    old = Version(old_root, python, profile, {}, release=7)
    write_json(old_root / "version.json", old.document())
    write_json(old_root / "manifest.json", {"release": 7})
    (root / "current").write_text("old-7\n")
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    selection = install_selection(home, sys.platform)
    write_json(selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data),
                           "identity": {"release": 7, "managed_by": None}})
    incoming = tmp_path / "private selection"
    platform.private_directory(incoming)
    manifest = incoming / "manifest"
    bootstrap = incoming / "bootstrap"
    manifest.write_text(json.dumps({
        "schema_version": 1, "release": 8, "agent": {"sha": "a" * 40},
        "runtime": {"sha": "b" * 40},
        "tools": {**{name: {"version": "20.19.4" if name == "node" else "1.2.3"}
                    for name in ("uv", "python", "bun", "node", "ffmpeg")},
                  "omp": {"source": DEFAULT_OMP_SOURCE}},
    }))
    bootstrap.write_text("#!/bin/sh\nexit 0\n")
    platform.owner_only(manifest)
    platform.owner_only(bootstrap)
    request = {
        "root": str(root), "data_root": str(data), "selection_path": str(selection),
        "old_instance_id": "instance-before", "old_identity": {"release": 7, "managed_by": None},
        "old_version": old.document(),
        "metadata": {"manifest_path": str(manifest), "bootstrap_path": str(bootstrap),
                     "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                     "bootstrap_sha256": hashlib.sha256(bootstrap.read_bytes()).hexdigest(), "release": 8},
        "job_name": "astrid-update-test", "operation_id": "a" * 32,
        "log_path": str(root / "logs" / "update-test.log"),
    }
    monkeypatch.setattr(update_job, "_probe_identity",
                        lambda _data: ("instance-before", {"release": 7, "managed_by": None}))
    return root, data, old, request


def test_accept_persists_additive_job_and_private_log_under_real_installer_lock(tmp_path, monkeypatch):
    root, data, old, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    marker = read_json(root / "install.json")
    assert marker["owner"] == LABEL and marker["data_root"] == str(data)
    assert marker["update_job"] == job
    assert job["state"] == "accepted"
    assert job["target_release"] == 8
    assert job["old_instance_id"] == "instance-before"
    assert job["old_version"] == old.document()
    assert platform.is_owner_only(job["log_path"])
    assert not (root / "update-transaction.json").exists()


def test_accept_refuses_changed_selection_bytes_release_and_owner_without_journal(tmp_path, monkeypatch):
    for change in ("bytes", "release", "owner"):
        case = tmp_path / change
        case.mkdir()
        root, _, _, request = _fixture(case, monkeypatch)
        if change == "bytes":
            with open(request["metadata"]["manifest_path"], "a") as stream:
                stream.write(" ")
        elif change == "release":
            request["metadata"]["release"] = 7
        else:
            marker = read_json(root / "install.json")
            marker["owner"] = "foreign"
            write_json(root / "install.json", marker)
        with pytest.raises((ValueError, OSError)):
            update_job.accept(request)
        assert "update_job" not in read_json(root / "install.json")
        assert not (root / "update-transaction.json").exists()


def test_accept_rejects_metadata_paths_that_do_not_name_the_validated_inputs(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    alternate = request["metadata"]["manifest_path"] + "-alternate"
    Path(alternate).write_bytes(Path(request["metadata"]["manifest_path"]).read_bytes())
    platform.owner_only(alternate)
    request["metadata"]["manifest_path"] = alternate
    request["metadata"]["manifest_sha256"] = hashlib.sha256(Path(alternate).read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="fixed names"):
        update_job.accept(request)
    assert "update_job" not in read_json(root / "install.json")


def test_windows_handoff_copies_same_private_bytes_to_ps1_and_preserves_cli_selection(tmp_path, monkeypatch):
    _, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    original = dict(job["metadata"])
    selected_bytes = Path(original["bootstrap_path"]).read_bytes()
    script = update_job._windows_bootstrap_script(original, job["operation_id"])
    args = ["--manifest", original["manifest_path"], "--root", job["program_root"],
            "--data-root", job["data_root"], "--update-job", job["operation_id"]]
    command = update_job._bootstrap_command(script, args, system="win32")
    assert script.suffix == ".ps1"
    assert script.read_bytes() == selected_bytes
    assert platform.is_owner_only(script)
    assert command == ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                       "-File", str(script), *args]
    assert job["metadata"] == original
    script.unlink()


def test_accept_refuses_competing_operation_and_pending_c2_transaction(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    update_job.accept(request)
    competing = dict(request, operation_id="b" * 32)
    with pytest.raises(ValueError, match="already active"):
        update_job.accept(competing)

    marker = read_json(root / "install.json")
    marker.pop("update_job")
    write_json(root / "install.json", marker)
    write_json(root / "update-transaction.json", {"step": "stopping", "operation_id": "c" * 32})
    with pytest.raises(ValueError, match="recover its pending update"):
        update_job.accept(competing)


def test_status_read_is_bounded_and_does_not_recover(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    assert update_job.read_job(root) == {"state": "none"}
    accepted = update_job.accept(request)
    write_json(root / "update-transaction.json", {"step": "stopping", "operation_id": "c" * 32})
    assert update_job.read_job(root)["operation_id"] == accepted["operation_id"]
    assert current(root).release == 7
    assert read_json(root / "update-transaction.json")["step"] == "stopping"


def test_existing_c2_journal_associates_job_without_preseeding_transaction(tmp_path, monkeypatch):
    root, data, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    update_job._write_job(root, job["operation_id"], state="active", message="Update is running.")
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": "d" * 32,
        "transaction_kind": "migration", "data_root": str(data),
        "old": job["old_version"], "candidate": {},
    }
    installer = Installer(root, data, SimpleNamespace(_update_job_operation_id=job["operation_id"]))
    transaction["update_job"] = {"operation_id": installer.backend._update_job_operation_id}
    installer._save_update(transaction, "staged")
    journal = read_json(root / "update-transaction.json")
    saved_job = read_json(root / "install.json")["update_job"]
    assert journal["step"] == "staged"
    assert journal["update_job"] == {"operation_id": job["operation_id"]}
    assert saved_job["transaction_id"] == journal["operation_id"]
    installer._save_update(transaction, "stopping")
    assert read_json(root / "update-transaction.json")["update_job"] == journal["update_job"]


def test_worker_claims_once_and_terminal_result_is_idempotent(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    calls = []

    def launch(argv, **kwargs):
        calls.append(argv)
        update_job._write_job(root, job["operation_id"], state="updated", message="Updated to release 8.")
        return CompletedProcess(argv, 0)

    monkeypatch.setattr(update_job.subprocess, "run", launch)
    first = update_job.run_job(root, job["operation_id"])
    second = update_job.run_job(root, job["operation_id"])
    assert first["state"] == second["state"] == "updated"
    assert len(calls) == 1
    assert calls[0][0:2] == ["/bin/sh", request["metadata"]["bootstrap_path"]]


def test_worker_does_not_relaunch_an_already_active_job(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    update_job._write_job(root, job["operation_id"], state="active", message="Update is running.")
    monkeypatch.setattr(update_job.subprocess, "run",
                        lambda *_args, **_kwargs: pytest.fail("active job was launched a second time"))
    assert update_job.run_job(root, job["operation_id"])["state"] == "active"


def test_prior_rolled_back_journal_cannot_claim_new_job_rollback(tmp_path, monkeypatch):
    root, data, old, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    update_job._write_job(root, job["operation_id"], state="active", message="Update is running.")
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "e" * 32,
        "step": "rolled_back", "old": old.document(),
    })
    monkeypatch.setattr(cli, "_canonical_identity",
                        lambda _discovery, _instance: {"release": 7, "managed_by": None})
    backend = SimpleNamespace(workspace=lambda *_args: {"realm_id": "realm"},
                              verify=lambda *_args, **_kwargs: None)
    cli._record_update_failure(root, data, backend, job, old, RuntimeError("download or launch failed"))
    outcome = update_job.read_job(root)
    assert outcome["state"] == "failed"
    assert outcome["message"].startswith("Failed:")


def test_worker_recovers_existing_c2_journal_before_rechecking_and_launching(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "f" * 32,
        "transaction_kind": "migration", "step": "stopping",
        "data_root": request["data_root"],
    })
    recovered = []

    class FakeBackend:
        def __init__(self, *_args, **_kwargs):
            pass

    class FakeInstaller:
        def __init__(self, *_args, **_kwargs):
            pass

        def _recover_update(self):
            recovered.append(True)
            transaction = read_json(root / "update-transaction.json")
            transaction["step"] = "complete"
            write_json(root / "update-transaction.json", transaction)

    import astrid.installer.backend as backend_module
    import astrid.installer.core as core_module
    monkeypatch.setattr(backend_module, "Backend", FakeBackend)
    monkeypatch.setattr(core_module, "Installer", FakeInstaller)
    monkeypatch.setattr(update_job.subprocess, "run", lambda argv, **_kwargs: (
        update_job._write_job(root, job["operation_id"], state="updated", message="Updated to release 8.")
        and CompletedProcess(argv, 0)))
    result = update_job.run_job(root, job["operation_id"])
    assert recovered == [True]
    assert result["state"] == "updated"


def test_success_requires_canonical_target_identity_and_bound_c2_complete(tmp_path, monkeypatch):
    root, data, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    update_job._write_job(root, job["operation_id"], state="active", message="Update is running.")
    candidate_root = root / "versions" / "candidate-8"
    candidate_root.mkdir()
    python = candidate_root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("python placeholder")
    profile = candidate_root / "profile.json"
    write_json(profile, {"profile": "astrid"})
    candidate = Version(candidate_root, python, profile, {}, release=8)
    write_json(candidate_root / "version.json", candidate.document())
    write_json(candidate_root / "manifest.json", {"release": 8})
    (root / "current").write_text("candidate-8\n")
    write_json(root / "update-transaction.json", {
        "operation_id": "9" * 32, "step": "complete",
        "update_job": {"operation_id": job["operation_id"]},
    })
    marker = read_json(root / "install.json")
    marker["update_job"]["transaction_id"] = "9" * 32
    write_json(root / "install.json", marker)
    discovery = data / "runtime" / "discovery.json"
    write_json(discovery, {"runtime_instance_id": "instance-after"})
    backend = SimpleNamespace(workspace=lambda *_args: {"realm_id": "realm"},
                               verify=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_canonical_identity",
                        lambda *_args: {"release": 7, "managed_by": None})
    with pytest.raises(ValueError, match="did not match the target"):
        cli._record_update_success(root, data, backend, job)
    assert update_job.read_job(root)["state"] == "recovery-needed"


def test_rollback_is_not_reported_when_the_old_version_is_not_selected(tmp_path, monkeypatch):
    root, data, old, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    update_job._write_job(root, job["operation_id"], state="active", message="Update is running.")
    candidate_root = root / "versions" / "candidate-8"
    candidate_root.mkdir()
    python = candidate_root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("python placeholder")
    profile = candidate_root / "profile.json"
    write_json(profile, {"profile": "astrid"})
    candidate = Version(candidate_root, python, profile, {}, release=8)
    write_json(candidate_root / "version.json", candidate.document())
    write_json(candidate_root / "manifest.json", {"release": 8})
    (root / "current").write_text("candidate-8\n")
    journal_id = "8" * 32
    write_json(root / "update-transaction.json", {
        "operation_id": journal_id, "step": "rolled_back",
        "update_job": {"operation_id": job["operation_id"]}, "old": old.document(),
    })
    marker = read_json(root / "install.json")
    marker["update_job"]["transaction_id"] = journal_id
    write_json(root / "install.json", marker)
    backend = SimpleNamespace(workspace=lambda *_args: {"realm_id": "realm"},
                               verify=lambda *_args, **_kwargs: None)
    cli._record_update_failure(root, data, backend, job, old, RuntimeError("start failed"))
    assert update_job.read_job(root)["state"] == "recovery-needed"
