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

from astrid.installer import update_job
from astrid.installer.core import Installer, Version, current, read_json, write_json
from astrid.installer.service import LABEL, Service, install_selection
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
        "job_name": "astrid-update-" + "a" * 32, "operation_id": "a" * 32,
        "log_path": str(root / "logs" / "update-test.log"),
    }
    monkeypatch.setattr(update_job, "_probe_identity",
                        lambda _data: ("instance-before", {"release": 7, "managed_by": None}))
    return root, data, old, request


def _startup_object_snapshot(root):
    service = Service(root, root / "logs" / "runtime-service.log", lambda *_args: "")
    service.path.parent.mkdir(parents=True, exist_ok=True)
    service.path.write_bytes(b"existing-startup-registration\n")
    return service.path, service.path.read_bytes()


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
    monkeypatch.setattr(update_job, "_probe_identity",
                        lambda _data: ("instance-before", {"release": 7, "managed_by": None}))
    backend = SimpleNamespace(workspace=lambda *_args: {"realm_id": "realm"},
                              verify=lambda *_args, **_kwargs: None)
    update_job.record_failure(root, data, backend, job, old, RuntimeError("download or launch failed"))
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


def test_job_descriptor_is_durable_before_registration_and_lost_ack_never_relaunches(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    calls = []
    active = True

    class Adapter:
        def describe(self, job, command):
            return {"system": "linux", "name": job["job_name"] + ".service",
                    "unit": job["job_name"] + ".service", "operation_id": job["operation_id"],
                    "user": "123", "command": command, "format": "astrid-update-platform/v1"}

        def launch(self, descriptor, command, log_path):
            calls.append((descriptor, command, log_path))
            persisted = read_json(root / "install.json")["update_job"]
            assert persisted["state"] == "launching"
            assert persisted["platform"] == descriptor
            raise RuntimeError("registration succeeded but acknowledgement was lost")

        def is_active(self, descriptor):
            return active

        def cleanup(self, descriptor, **_kwargs):
            calls.append(("cleanup", descriptor))

    adapter = Adapter()
    monkeypatch.setattr(update_job, "_platform_api", lambda: adapter)
    pending = update_job.launch_job(root, job["operation_id"])
    assert pending["state"] == "launching"
    assert pending["launch_status"] == "unknown"
    assert pending["platform"]["name"] == job["job_name"] + ".service"
    assert len(calls) == 1
    assert update_job.launch_job(root, job["operation_id"]) == pending
    assert len(calls) == 1
    assert update_job.sweep_owned_jobs(root)[0]["state"] == "launching"


def test_terminal_cleanup_records_status_error_and_retains_residue_for_next_operation(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    descriptor = {"system": "linux", "name": job["job_name"] + ".service",
                  "unit": job["job_name"] + ".service", "operation_id": job["operation_id"],
                  "user": "123", "format": "astrid-update-platform/v1"}
    update_job._write_job(root, job["operation_id"], state="launching", message="launching",
                          fields={"platform": descriptor})
    update_job._write_job(root, job["operation_id"], state="failed", message="Failed: test")

    class Adapter:
        def cleanup(self, value, **_kwargs):
            assert value == descriptor

        def is_active(self, value):
            return True

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    result = update_job._cleanup_terminal(root, job["operation_id"])
    assert result["cleanup_status"] == "residue"
    assert result["residue"] == job["job_name"] + ".service"

    second = dict(request, operation_id="b" * 32, job_name="astrid-update-" + "b" * 32)
    # A terminal residue is retained additively when the next operation is accepted.
    update_job.accept(second)
    marker = read_json(root / "install.json")
    assert marker["update_job_residue"][-1]["descriptor"] == descriptor


def test_sweep_marks_inactive_registration_failure_and_cleans_only_owned_object(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    descriptor = {"system": "linux", "name": job["job_name"] + ".service",
                  "unit": job["job_name"] + ".service", "operation_id": job["operation_id"],
                  "user": "123", "format": "astrid-update-platform/v1"}
    update_job._write_job(root, job["operation_id"], state="launching", message="launching",
                          fields={"platform": descriptor, "launch_status": "unknown"})
    calls = []

    class Adapter:
        def is_active(self, value):
            assert value == descriptor
            return False

        def cleanup(self, value, **_kwargs):
            calls.append(value)

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    swept = update_job.sweep_owned_jobs(root)
    assert swept[0]["state"] == "failed"
    assert swept[0]["cleanup_status"] == "clean"
    assert swept[0]["residue"] is None
    assert calls == [descriptor]


def test_explicit_saved_autostart_off_refuses_acceptance_without_mutating_preferences(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    startup_path, startup_before = _startup_object_snapshot(root)
    selection_path = Path(request["selection_path"])
    selection = read_json(selection_path)
    selection["preferences"] = {"autostart": False, "skills": {"on": True}}
    write_json(selection_path, selection)
    before = selection_path.read_bytes()
    with pytest.raises(ValueError, match="autostart is off"):
        update_job.accept(request)
    assert selection_path.read_bytes() == before
    assert startup_path.read_bytes() == startup_before
    assert "update_job" not in read_json(root / "install.json")


def test_saved_autostart_on_is_snapshotted_and_launch_does_not_change_selection(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    startup_path, startup_before = _startup_object_snapshot(root)
    selection_path = Path(request["selection_path"])
    selection = read_json(selection_path)
    selection["preferences"] = {"autostart": True, "skills": {"on": True}}
    write_json(selection_path, selection)
    before = selection_path.read_bytes()
    job = update_job.accept(request)
    assert job["autostart_snapshot"] is True

    class Adapter:
        def describe(self, value, command):
            return {"format": "astrid-update-platform/v1", "system": "linux",
                    "kind": "systemd-user-transient", "name": value["job_name"] + ".service",
                    "unit": value["job_name"] + ".service", "operation_id": value["operation_id"],
                    "user": "123", "command": command}

        def launch(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    launched = update_job.launch_job(root, job["operation_id"])
    assert launched["launch_status"] == "acknowledged"
    assert launched["state"] == "launching"
    assert selection_path.read_bytes() == before
    assert startup_path.read_bytes() == startup_before


def test_terminal_cleanup_failure_is_durable_and_sweep_retains_unknown_residue(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    descriptor = {"format": "astrid-update-platform/v1", "system": "linux",
                  "kind": "systemd-user-transient", "name": job["job_name"] + ".service",
                  "unit": job["job_name"] + ".service", "operation_id": job["operation_id"],
                  "user": "123", "command": ["python"]}
    update_job._write_job(root, job["operation_id"], state="launching", message="launching",
                          fields={"platform": descriptor})
    update_job._write_job(root, job["operation_id"], state="failed", message="Failed: test")

    class Adapter:
        def cleanup(self, _descriptor, **_kwargs):
            raise OSError("manager unavailable")

        def is_active(self, _descriptor):
            raise OSError("query unavailable")

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    failed = update_job._cleanup_terminal(root, job["operation_id"])
    assert failed["cleanup_status"] == "unknown"
    assert "manager unavailable" in failed["cleanup_error"]
    swept = update_job.sweep_owned_jobs(root)
    assert swept and swept[0]["cleanup_status"] == "unknown"


def test_known_launch_rejection_is_failed_only_after_inactive_query(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)

    class UpdateJobLaunchError(RuntimeError):
        pass

    class Adapter:
        active = False

        def describe(self, value, command):
            return {"format": "astrid-update-platform/v1", "system": "linux",
                    "kind": "systemd-user-transient", "name": value["job_name"] + ".service",
                    "unit": value["job_name"] + ".service", "operation_id": value["operation_id"],
                    "user": "123", "command": command}

        def launch(self, *_args, **_kwargs):
            self.active = True
            raise UpdateJobLaunchError("manager returned failure after start")

        def is_active(self, _descriptor):
            return self.active

        def cleanup(self, _descriptor, **_kwargs):
            raise RuntimeError("active object retained")

    adapter = Adapter()
    monkeypatch.setattr(update_job, "_platform_api", lambda: adapter)
    result = update_job.launch_job(root, job["operation_id"])
    assert result["state"] == "launching"
    assert result["launch_status"] == "unknown"
    assert result["launch_error"] == "manager returned failure after start"


def test_worker_subprocess_failure_attempts_terminal_cleanup(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    descriptor = {"format": "astrid-update-platform/v1", "system": "linux",
                  "kind": "systemd-user-transient", "name": job["job_name"] + ".service",
                  "unit": job["job_name"] + ".service", "operation_id": job["operation_id"],
                  "user": "123", "command": ["python"]}
    update_job._write_job(root, job["operation_id"], state="launching", message="launching",
                          fields={"platform": descriptor})
    cleanup_calls = []

    class Adapter:
        def cleanup(self, value, **kwargs):
            cleanup_calls.append((value, kwargs))

        def is_active(self, _value):
            return False

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    monkeypatch.setattr(update_job.subprocess, "run",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("bootstrap unavailable")))
    result = update_job.run_job(root, job["operation_id"])
    assert result["state"] == "failed"
    assert result["cleanup_status"] == "clean"
    assert cleanup_calls == [(descriptor, {"finishing": True})]


def test_worker_validation_failure_cleans_from_its_own_job_context(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    descriptor = {"format": "astrid-update-platform/v1", "system": "linux",
                  "kind": "systemd-user-transient", "name": job["job_name"] + ".service",
                  "unit": job["job_name"] + ".service", "operation_id": job["operation_id"],
                  "user": "123", "command": ["python"]}
    update_job._write_job(root, job["operation_id"], state="launching", message="launching",
                          fields={"platform": descriptor})
    calls = []

    class Adapter:
        def cleanup(self, _value, **kwargs):
            calls.append(kwargs)

        def is_active(self, _value):
            return False

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    monkeypatch.setattr(update_job, "_probe_identity",
                        lambda *_args: ("different-instance", {"release": 7, "managed_by": None}))
    result = update_job.run_job(root, job["operation_id"])
    assert result["state"] == "failed"
    assert result["cleanup_status"] == "clean"
    assert calls == [{"finishing": True}]


@pytest.mark.parametrize("terminal_state", ["updated", "rolled_back", "failed"])
def test_worker_terminal_handoff_cleans_once_and_preserves_preferences_and_job_name(
        tmp_path, monkeypatch, terminal_state):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    selection_path = Path(request["selection_path"])
    selection = read_json(selection_path)
    selection["preferences"] = {"autostart": True, "skills": {"on": True}}
    write_json(selection_path, selection)
    before_selection = selection_path.read_bytes()
    job = update_job.accept(request)
    assert job["autostart_snapshot"] is True
    startup_path, before_startup = _startup_object_snapshot(root)
    descriptor = {"format": "astrid-update-platform/v1", "system": "linux",
                  "kind": "systemd-user-transient", "name": job["job_name"] + ".service",
                  "unit": job["job_name"] + ".service", "operation_id": job["operation_id"],
                  "user": "123", "command": ["python"]}
    update_job._write_job(root, job["operation_id"], state="launching", message="launching",
                          fields={"platform": descriptor})
    launches, cleanups = [], []

    class Adapter:
        def cleanup(self, value, *, finishing=False):
            cleanups.append((value, finishing))

        def is_active(self, _value):
            return False

    def run_bootstrap(argv, **_kwargs):
        launches.append(argv)
        update_job._write_job(root, job["operation_id"], state=terminal_state,
                              message=f"{terminal_state} result")
        return CompletedProcess(argv, 0)

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    monkeypatch.setattr(update_job.subprocess, "run", run_bootstrap)
    first = update_job.run_job(root, job["operation_id"])
    second = update_job.run_job(root, job["operation_id"])
    assert first["state"] == second["state"] == terminal_state
    assert first["job_name"] == job["job_name"]
    assert first["cleanup_status"] == "clean"
    assert len(launches) == 1
    assert cleanups == [(descriptor, True)]
    assert selection_path.read_bytes() == before_selection
    assert startup_path.read_bytes() == before_startup


def test_worker_launch_refuses_if_autostart_was_changed_to_off(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    selection_path = Path(request["selection_path"])
    selection = read_json(selection_path)
    selection["preferences"] = {"autostart": "off", "skills": {"on": True}}
    write_json(selection_path, selection)
    before = selection_path.read_bytes()

    class Adapter:
        def describe(self, *_args):
            pytest.fail("autostart-off update must be refused before job registration")

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    result = update_job.launch_job(root, job["operation_id"])
    assert result["state"] == "failed"
    assert result["launch_status"] == "refused"
    assert "autostart is off" in result["message"]
    assert selection_path.read_bytes() == before


def test_sweep_uses_existing_installer_lock_and_retains_historical_residue(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    descriptor = {"format": "astrid-update-platform/v1", "system": "linux",
                  "kind": "systemd-user-transient", "name": job["job_name"] + ".service",
                  "unit": job["job_name"] + ".service", "operation_id": job["operation_id"],
                  "user": "123", "command": ["python"]}
    marker = read_json(root / "install.json")
    marker["update_job_residue"] = [{"operation_id": job["operation_id"],
                                    "descriptor": descriptor, "cleanup_status": "unknown",
                                    "residue": descriptor["name"]}]
    write_json(root / "install.json", marker)

    class Adapter:
        def is_active(self, _value):
            return True

        def cleanup(self, *_args, **_kwargs):
            pytest.fail("active recorded object must be retained")

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    lock = update_job._locked(root)
    try:
        result = update_job.sweep_owned_jobs(root, locked=True)
    finally:
        update_job._unlock(lock)
    assert result[-1]["cleanup_status"] == "unknown"
    assert read_json(root / "install.json")["update_job_residue"][0]["descriptor"] == descriptor


def test_sweep_without_leftovers_preserves_install_record_bytes(tmp_path, monkeypatch):
    root, _, _, _ = _fixture(tmp_path, monkeypatch)
    path = root / "install.json"
    before = path.read_bytes()
    assert update_job.sweep_owned_jobs(root) == []
    assert path.read_bytes() == before
    assert "update_job_residue" not in read_json(path)


def test_sweep_preserves_already_clean_historical_residue(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    descriptor = {"format": "astrid-update-platform/v1", "system": "linux",
                  "kind": "systemd-user-transient", "name": job["job_name"] + ".service",
                  "unit": job["job_name"] + ".service", "operation_id": job["operation_id"],
                  "user": "123", "command": ["python"]}
    marker = read_json(root / "install.json")
    marker["update_job_residue"] = [{"operation_id": job["operation_id"],
                                    "descriptor": descriptor, "cleanup_status": "clean",
                                    "cleanup_error": None, "residue": None}]
    marker.pop("update_job")
    write_json(root / "install.json", marker)
    path = root / "install.json"
    before = path.read_bytes()

    class Adapter:
        def is_active(self, *_args):
            pytest.fail("already-clean historical residue must not be queried again")

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    update_job.sweep_owned_jobs(root)
    assert path.read_bytes() == before


def test_worker_command_uses_selected_old_python_and_profile_checkouts_isolated(tmp_path, monkeypatch):
    root, _, old, request = _fixture(tmp_path, monkeypatch)
    profile = read_json(old.profile)
    runtime_checkout = old.root / "runtime checkout"
    agent_checkout = old.root / "agent checkout"
    runtime_checkout.mkdir()
    agent_checkout.mkdir()
    profile.update(runtime_checkout=str(runtime_checkout), source_checkout=str(agent_checkout))
    write_json(old.profile, profile)
    job = update_job.accept(request)
    command = update_job._worker_command(old, job)
    if sys.platform == "win32":
        assert command[:5] == [str(old.python), "-B", "-X", "utf8", "-I"]
        source = Path(command[-1]).read_text(encoding="utf-8")
        assert repr(str(runtime_checkout)) in source
        assert repr(str(agent_checkout)) in source
        assert "from astrid.installer.update_job import run_job" in source
    else:
        assert command[:6] == [str(old.python), "-B", "-X", "utf8", "-I", "-c"]
        assert str(runtime_checkout) in command[-1]
        assert str(agent_checkout) in command[-1]
        assert "from astrid.installer.update_job import run_job" in command[-1]


def test_worker_script_preparation_failure_is_recorded_before_platform_registration(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    calls = []

    class Adapter:
        def describe(self, *_args):
            calls.append("describe")
            return {}

        def launch(self, *_args):
            calls.append("launch")

    def fail_preparation(*_args):
        raise OSError("private worker script could not be written")

    monkeypatch.setattr(update_job, "_worker_command", fail_preparation)
    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())

    result = update_job.launch_job(root, job["operation_id"])

    assert result["state"] == "failed"
    assert result["launch_status"] == "failed"
    assert "private worker script could not be written" in result["launch_error"]
    assert calls == []


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
    monkeypatch.setattr(update_job, "_probe_identity",
                        lambda *_args: ("instance-after", {"release": 7, "managed_by": None}))
    with pytest.raises(ValueError, match="did not match the target"):
        update_job.record_success(root, data, backend, job)
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
    update_job.record_failure(root, data, backend, job, old, RuntimeError("start failed"))
    assert update_job.read_job(root)["state"] == "recovery-needed"


@pytest.mark.parametrize("caller", ["job", "recovery"])
@pytest.mark.parametrize("payload", [
    {"runtime_instance_id": "instance", "identity": {"release": 7, "managed_by": None}},
    {"runtime_instance_id": "different", "identity": {"release": 7, "managed_by": None}},
    {"runtime_instance_id": "instance", "identity": {"release": True, "managed_by": None}},
])
def test_job_and_recovery_identity_probes_use_runtime_validator(tmp_path, monkeypatch, caller, payload):
    from astrid_runtime import identity as runtime_identity

    discovery = {"runtime_instance_id": "instance", "endpoint": "http://127.0.0.1:12345"}
    path = tmp_path / "runtime" / "discovery.json"
    write_json(path, discovery)
    before = path.read_bytes()
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary.owner_startup_identity",
                        lambda *_args, **_kwargs: payload)
    calls = []
    validate = runtime_identity.validate_live_owner_identity

    def observe(health, *, instance_id):
        calls.append((health, instance_id))
        return validate(health, instance_id=instance_id)

    monkeypatch.setattr(runtime_identity, "validate_live_owner_identity", observe)
    # Recovery's identity checks call the same _probe_identity as the job path.
    def probe():
        return update_job._probe_identity(tmp_path)[1]
    if payload["runtime_instance_id"] == "instance" and type(payload["identity"]["release"]) is int:
        assert probe() == {"release": 7, "managed_by": None}
    else:
        with pytest.raises(runtime_identity.StartupIdentityError):
            probe()
    assert calls == [(payload, "instance")]
    assert path.read_bytes() == before
