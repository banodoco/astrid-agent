"""Durable acceptance and identity binding for click-created update jobs."""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from runtime_protocol import platform

from astrid.installer import update_job
from astrid.installer.core import Installer, Version, current, read_json, write_json
from astrid.installer.service import LABEL, Service, install_selection
from astrid.installer.tools import DEFAULT_OMP_SOURCE



def test_the_job_reads_the_install_record_of_the_home_it_was_accepted_under(tmp_path, monkeypatch):
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    install_home, launcher_home = tmp_path / "install-home", tmp_path / "launcher-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: launcher_home))
    job = {"operation_id": "0" * 32, "home": str(install_home)}
    assert update_job._recorded_selection_path(job) == install_selection(install_home, sys.platform)
    assert update_job._recorded_selection_path(job) != install_selection(launcher_home, sys.platform)


@pytest.mark.parametrize("job", [{"operation_id": "0" * 32}, {"home": "relative/home"}, {"home": None}])
def test_a_job_with_no_recorded_home_never_falls_back_to_the_ambient_home(job, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "ambient"))
    with pytest.raises(ValueError, match="installation home"):
        update_job._recorded_selection_path(job)


def _fixture(tmp_path, monkeypatch, *, available=True):
    # These job tests model an available manager; dedicated tests below probe it.
    if available:
        monkeypatch.setattr(update_job, "update_job_availability", lambda: {"available": True})
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


def test_non_linux_update_job_availability_remains_supported(monkeypatch):
    run = Mock(side_effect=AssertionError("systemctl is Linux-only"))
    monkeypatch.setattr(update_job.subprocess, "run", run)

    assert update_job.update_job_availability(system="darwin") == {"available": True}
    run.assert_not_called()


@pytest.mark.parametrize("version_result", [
    CompletedProcess([], 0, "systemd 253 (253.7)\n", ""),
    CompletedProcess([], 0, "systemd ???\n", ""),
    CompletedProcess([], 1, "", "systemctl: failed to connect"),
    FileNotFoundError("systemctl missing"),
    subprocess.TimeoutExpired(["systemctl"], 5),
])
def test_linux_update_job_availability_rejects_old_unknown_and_failed_systemd(
        monkeypatch, version_result):
    run = Mock(side_effect=version_result if isinstance(version_result, Exception)
               else [version_result])
    monkeypatch.setattr(update_job.subprocess, "run", run)

    assert update_job.update_job_availability(system="linux") == {
        "available": False, "reason": "unsupported-service-manager"}
    run.assert_called_once_with(
        ["systemctl", "--user", "--version"], capture_output=True,
        text=True, timeout=5, check=False,
    )


def test_linux_systemd_254_with_unusable_user_manager_is_unsupported(monkeypatch):
    run = Mock(side_effect=[
        CompletedProcess([], 0, "systemd 254 (254.1)\n", ""),
        CompletedProcess([], 1, "", "Failed to connect to bus"),
    ])
    monkeypatch.setattr(update_job.subprocess, "run", run)

    assert update_job.update_job_availability(system="linux") == {
        "available": False, "reason": "unsupported-service-manager"}
    assert [call.args[0] for call in run.call_args_list] == [
        ["systemctl", "--user", "--version"],
        ["systemctl", "--user", "show-environment"],
    ]


def test_linux_systemd_254_with_usable_user_manager_is_supported(monkeypatch):
    run = Mock(side_effect=[
        CompletedProcess([], 0, "systemd 254 (254.1)\n", ""),
        CompletedProcess([], 0, "PATH=/usr/bin\n", ""),
    ])
    monkeypatch.setattr(update_job.subprocess, "run", run)

    assert update_job.update_job_availability(system="linux") == {"available": True}


def test_update_job_availability_action_preserves_get_reason(monkeypatch):
    availability = update_job.update_job_availability
    monkeypatch.setattr(update_job, "update_job_availability",
                        lambda: availability(system="linux"))
    monkeypatch.setattr(update_job.subprocess, "run", Mock(side_effect=[
        CompletedProcess([], 0, "systemd 254 (254.1)\n", ""),
        CompletedProcess([], 1, "", "Failed to connect to bus"),
    ]))
    monkeypatch.setattr(update_job.sys, "stdin", io.StringIO('{"action":"availability"}'))
    stdout = io.StringIO()
    monkeypatch.setattr("builtins.print", lambda value: stdout.write(value))

    update_job.main()

    assert json.loads(stdout.getvalue()) == {
        "available": False, "reason": "unsupported-service-manager",
        "actions": list(update_job.SUPPORTED_ACTIONS)}


def _selected_windows_install(tmp_path, monkeypatch, *, release):
    """A selected direct install whose current version has ``release``; no job record."""
    root, data = tmp_path / "program", tmp_path / "data"
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    folder = root / "versions" / f"release{release}"
    write_json(folder / "version.json", Version(
        folder, folder / "py/python.exe", folder / "profile.json", {}, release=release).document())
    (root / "current").write_text(f"release{release}\n")
    selection = tmp_path / "selection" / "astrid-install.json"
    write_json(selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data)})
    monkeypatch.setattr(update_job, "_selection_path", lambda: selection)
    return root


def test_windows_earlier_release_availability_reports_upgrade_reason(tmp_path, monkeypatch):
    _selected_windows_install(tmp_path, monkeypatch, release=0)

    assert update_job.update_job_availability(system="win32") == {
        "available": False, "reason": "windows-upgrade-unsupported"}


def test_windows_earlier_release_availability_action_reports_upgrade_reason(tmp_path, monkeypatch):
    _selected_windows_install(tmp_path, monkeypatch, release=0)
    availability = update_job.update_job_availability
    monkeypatch.setattr(update_job, "update_job_availability", lambda: availability(system="win32"))
    monkeypatch.setattr(update_job.sys, "stdin", io.StringIO('{"action":"availability"}'))
    stdout = io.StringIO()
    monkeypatch.setattr("builtins.print", lambda value: stdout.write(value))

    update_job.main()

    reply = json.loads(stdout.getvalue())
    assert reply["available"] is False
    assert reply["reason"] == "windows-upgrade-unsupported"


def test_windows_current_release_availability_remains_supported(tmp_path, monkeypatch):
    _selected_windows_install(tmp_path, monkeypatch, release=1)

    assert update_job.update_job_availability(system="win32") == {"available": True}


def _unpointed_windows_install(tmp_path, monkeypatch):
    """A selected direct install with no current pointer, the shape an R0 install can have on Windows."""
    root, data = tmp_path / "program", tmp_path / "data"
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    selection = tmp_path / "selection" / "astrid-install.json"
    write_json(selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data)})
    monkeypatch.setattr(update_job, "_selection_path", lambda: selection)
    return root


def _task_query_answers(monkeypatch, *, count):
    """Answer the probe's sign-in task query at the process boundary it really uses."""
    scripts = []

    def run(argv, **_kwargs):
        scripts.append(argv[-1])
        return CompletedProcess(argv, 0, stdout=f"ASTRID_TASK_COUNT={count}\n", stderr="")

    monkeypatch.setattr(update_job.subprocess, "run", run)
    return scripts


def test_windows_r0_without_pointer_but_with_login_task_reports_upgrade_reason(tmp_path, monkeypatch):
    _unpointed_windows_install(tmp_path, monkeypatch)
    scripts = _task_query_answers(monkeypatch, count=1)

    assert update_job.update_job_availability(system="win32") == {
        "available": False, "reason": "windows-upgrade-unsupported"}
    assert any("Get-ScheduledTask" in script for script in scripts)


def test_windows_install_without_pointer_or_login_task_is_not_refused_as_upgrade(tmp_path, monkeypatch):
    _unpointed_windows_install(tmp_path, monkeypatch)
    scripts = _task_query_answers(monkeypatch, count=0)

    assert update_job.update_job_availability(system="win32") == {"available": True}
    assert any("Get-ScheduledTask" in script for script in scripts)


def test_windows_upgrade_refusal_gate_names_the_reason_not_the_service_manager(monkeypatch):
    monkeypatch.setattr(update_job, "update_job_availability", lambda: {
        "available": False, "reason": "windows-upgrade-unsupported"})

    with pytest.raises(update_job._UnsupportedServiceManager) as raised:
        update_job._require_update_job_available()

    assert raised.value.reason == "windows-upgrade-unsupported"
    assert str(raised.value) == (
        "Upgrading from an earlier Windows version isn't supported yet. "
        "Your data was kept; reinstall to get the new version.")


@pytest.mark.parametrize("system", ["linux", "darwin", "win32"])
def test_availability_action_lists_supported_actions_on_every_os(monkeypatch, system):
    availability = update_job.update_job_availability
    monkeypatch.setattr(update_job, "update_job_availability", lambda: availability(system=system))
    monkeypatch.setattr(update_job.subprocess, "run", Mock(side_effect=[
        CompletedProcess([], 0, "systemd 255 (255.4)\n", ""),
        CompletedProcess([], 0, "", ""),
    ]))
    monkeypatch.setattr(update_job.sys, "stdin", io.StringIO('{"action":"availability"}'))
    stdout = io.StringIO()
    monkeypatch.setattr("builtins.print", lambda value: stdout.write(value))

    update_job.main()

    reply = json.loads(stdout.getvalue())
    assert reply["available"] is True
    assert {"availability", "prepare", "accept", "launch"} <= set(reply["actions"])


@pytest.mark.parametrize("probe_results", [
    [CompletedProcess([], 0, "systemd 253 (253.7)\n", "")],
    [CompletedProcess([], 0, "systemd 255 (255.4)\n", ""),
     CompletedProcess([], 1, "", "Failed to connect to bus")],
    FileNotFoundError("systemctl missing"),
])
def test_unsupported_post_keeps_reason_and_has_no_admission_or_launch_effects(
        tmp_path, monkeypatch, probe_results):
    root, _, _, request = _fixture(tmp_path, monkeypatch, available=False)
    availability = update_job.update_job_availability
    monkeypatch.setattr(update_job, "update_job_availability",
                        lambda: availability(system="linux"))
    run = Mock(side_effect=probe_results)
    monkeypatch.setattr(update_job.subprocess, "run", run)
    payload = {**request, "action": "accept"}
    monkeypatch.setattr(update_job.sys, "stdin", io.StringIO(json.dumps(payload)))
    stdout = io.StringIO()
    monkeypatch.setattr("builtins.print", lambda value: stdout.write(value))
    launch = Mock(side_effect=AssertionError("unsupported update must not launch"))
    monkeypatch.setattr(update_job, "launch_job", launch)

    with pytest.raises(SystemExit) as raised:
        update_job.main()

    assert raised.value.code == 1
    assert json.loads(stdout.getvalue()) == {
        "state": "recovery-needed", "reason": "unsupported-service-manager",
        "message": "This update requires user systemd 254 or newer on Linux.",
    }
    assert "update_job" not in read_json(root / "install.json")
    assert not (root / "update-transaction.json").exists()
    launch.assert_not_called()


def test_supported_update_post_still_admits(monkeypatch, tmp_path):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    availability = Mock(return_value={"available": True})
    monkeypatch.setattr(update_job, "update_job_availability", availability)

    job = update_job.accept(request)

    availability.assert_called_once_with()
    assert job["state"] == "accepted"
    assert read_json(root / "install.json")["update_job"]["operation_id"] == request["operation_id"]


def test_unsupported_native_launch_is_refused_before_lock_or_launch(monkeypatch, tmp_path):
    availability = Mock(return_value={
        "available": False, "reason": "unsupported-service-manager"})
    monkeypatch.setattr(update_job, "update_job_availability", availability)
    lock = Mock(side_effect=AssertionError("unsupported launch must not acquire the lock"))
    monkeypatch.setattr(update_job, "_locked", lock)

    with pytest.raises(update_job._UnsupportedServiceManager) as raised:
        update_job.launch_job(tmp_path, "a" * 32)

    assert str(raised.value) == "This update requires user systemd 254 or newer on Linux."
    availability.assert_called_once_with()
    lock.assert_not_called()


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
    monkeypatch.setenv("SystemRoot", str(tmp_path / "winroot"))
    command = update_job._bootstrap_command(script, args, system="win32")
    assert script.suffix == ".ps1"
    assert script.read_bytes() == selected_bytes
    assert platform.is_owner_only(script)
    assert Path(command[0]).is_absolute()
    assert command[0] == str(tmp_path / "winroot" / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    assert command[1:] == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                       "-File", str(script), *args]
    assert job["metadata"] == original
    script.unlink()


def test_accept_refuses_competing_operation_and_pending_c2_transaction(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    update_job.accept(request)
    competing = dict(request, operation_id="b" * 32)
    monkeypatch.setattr(update_job._platform_api(), "is_registered", lambda *_args: True)
    monkeypatch.setattr(update_job._platform_api(), "is_active", lambda *_args: True)
    with pytest.raises(ValueError) as refusal:
        update_job.accept(competing)
    assert refusal.value.reason == "busy"

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


def _register_worker_for_test(root, job, monkeypatch):
    """Give direct worker tests the descriptor normally persisted by launch_job."""
    descriptor = {"format": "astrid-update-platform/v1", "system": "linux",
                  "operation_id": job["operation_id"],
                  "name": job["job_name"] + ".service", "unit": job["job_name"] + ".service"}
    update_job._write_job(root, job["operation_id"], state="launching", message="Launched.",
                          fields={"platform": descriptor})
    monkeypatch.setattr(update_job, "_cleanup_terminal", lambda *_args, **_kwargs: update_job.read_job(root))


def test_worker_claims_once_and_terminal_result_is_idempotent(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    _register_worker_for_test(root, job, monkeypatch)
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
    _register_worker_for_test(root, job, monkeypatch)
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
    selection["choices"] = {"autostart": False}
    write_json(selection_path, selection)
    before = selection_path.read_bytes()
    with pytest.raises(ValueError, match="autostart is off") as refusal:
        update_job.accept(request)
    assert getattr(refusal.value, "reason", None) == "autostart-off"
    assert selection_path.read_bytes() == before
    assert startup_path.read_bytes() == startup_before
    assert "update_job" not in read_json(root / "install.json")


def test_saved_autostart_on_is_snapshotted_and_launch_does_not_change_selection(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    startup_path, startup_before = _startup_object_snapshot(root)
    selection_path = Path(request["selection_path"])
    selection = read_json(selection_path)
    selection["choices"] = {"autostart": True}
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
def test_legacy_selection_without_a_choice_record_snapshots_autostart_on(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    selection_path = Path(request["selection_path"])
    selection = read_json(selection_path)
    selection.pop("choices", None)
    write_json(selection_path, selection)
    job = update_job.accept(request)
    assert job["autostart_snapshot"] is True


def test_worker_terminal_handoff_cleans_once_and_preserves_preferences_and_job_name(
        tmp_path, monkeypatch, terminal_state):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    selection_path = Path(request["selection_path"])
    selection = read_json(selection_path)
    selection["choices"] = {"autostart": True}
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
    selection["choices"] = {"autostart": False}
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


def test_supported_actions_cover_every_action_runtime_sends():
    # Runtime's request path: availability, reconcile, prepare (no action key), accept, launch.
    assert {"availability", "reconcile", "prepare", "accept", "launch"} <= set(update_job.SUPPORTED_ACTIONS)


def _worker_source_for(monkeypatch, tmp_path, system):
    monkeypatch.setattr(update_job, "read_json", lambda path: {})
    monkeypatch.setattr(update_job, "_attempt_tuple", lambda job: (1, "a" * 32))
    monkeypatch.setattr(update_job, "sys", SimpleNamespace(platform=system))
    old = SimpleNamespace(profile=tmp_path / "profile.json", env={})
    job = {"log_path": str(tmp_path / "job.log"), "program_root": str(tmp_path), "operation_id": "0" * 32}
    return update_job._worker_command_source(old, job)


def test_the_worker_preamble_is_the_same_on_every_platform(tmp_path, monkeypatch):
    windows = _worker_source_for(monkeypatch, tmp_path, "win32")
    assert "from astrid.installer.update_job import run_job" in windows
    assert "_log=open(" in windows and "sys.stdout=sys.stderr=_log" in windows
    for system in ("linux", "darwin"):
        assert _worker_source_for(monkeypatch, tmp_path, system) == windows


def _windows_worker_file(monkeypatch, tmp_path, first_line=""):
    source = _worker_source_for(monkeypatch, tmp_path, "win32")
    stub = ("import sys,types\n"
            "m=types.ModuleType('astrid.installer.update_job')\n"
            "m.run_job=lambda root, operation, epoch, nonce: print('run-job-output') or {'ran': True}\n"
            "for name in ('astrid', 'astrid.installer'):\n"
            "    sys.modules[name]=types.ModuleType(name)\n"
            "sys.modules['astrid.installer.update_job']=m\n")
    worker = tmp_path / "worker.py"
    worker.write_text(first_line + stub + source + "\n", encoding="utf-8")
    return worker


@pytest.mark.windows_native
def test_the_worker_owns_the_job_log_and_the_wrapper_never_writes_it(tmp_path, monkeypatch):
    from astrid.installer import update_job_platform as job_platform

    worker = _windows_worker_file(monkeypatch, tmp_path)
    log = tmp_path / "job.log"
    script = job_platform._worker_script([sys.executable, "-B", str(worker)], log)
    completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    job_text = log.read_text(encoding="utf-8", errors="replace")
    assert "run-job-output" in job_text and '"ran": true' in job_text
    assert "PermissionError" not in job_text
    wrapper = log.with_name("job.wrapper.log")
    assert "PermissionError" not in (wrapper.read_text(encoding="utf-8", errors="replace") if wrapper.exists() else "")


@pytest.mark.windows_native
def test_a_startup_traceback_lands_in_the_wrapper_log_and_the_wrapper_exits_one(tmp_path, monkeypatch):
    from astrid.installer import update_job_platform as job_platform

    worker = _windows_worker_file(monkeypatch, tmp_path, first_line="raise RuntimeError('startup boom')\n")
    log = tmp_path / "job.log"
    script = job_platform._worker_script([sys.executable, "-B", str(worker)], log)
    completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, text=True, timeout=120)
    assert completed.returncode == 1
    wrapper = log.with_name("job.wrapper.log").read_text(encoding="utf-8", errors="replace")
    assert "startup boom" in wrapper
    assert not log.exists()


class _DeadWorkerAdapter:
    """A Windows Scheduled Task manager whose recorded task reports a fixed outcome."""

    def __init__(self, finished):
        self.finished = finished
        self.calls = []

    def describe(self, value, command):
        name = value["job_name"]
        return {"format": "astrid-update-platform/v1", "system": "win32",
                "kind": "scheduled-task-on-demand", "name": name, "task_name": name,
                "user": "S-1-5-21-2", "operation_id": value["operation_id"],
                "program_root": value["program_root"], "log_path": value["log_path"],
                "command": command, "home": value["home"]}

    def launch(self, *_args, **_kwargs):
        self.calls.append("launch")

    def finished_result(self, descriptor):
        self.calls.append("finished_result")
        return self.finished

    def is_active(self, descriptor):
        return self.finished is None

    def cleanup(self, descriptor, **_kwargs):
        self.calls.append("cleanup")


def _launched_job(tmp_path, monkeypatch, finished):
    """An accepted, acknowledged job: the state the live Windows run stayed in."""
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    adapter = _DeadWorkerAdapter(finished)
    adapter.request = request  # the reconcile request Runtime's POST sends for this install
    monkeypatch.setattr(update_job, "_platform_api", lambda: adapter)
    launched = update_job.launch_job(root, job["operation_id"])
    assert launched["state"] == "launching" and launched["launch_status"] == "acknowledged"
    assert launched["bootstrap_proof"]["claim_state"] == "unclaimed"
    log = Path(launched["log_path"])
    log.write_text("Traceback (most recent call last):\nValueError: worker refused before claim\n",
                   encoding="utf-8")
    return root, launched, adapter


def _finished(code, started_at=None, bound="start-time"):
    # Task Scheduler/systemd report the run's start; a run of this attempt starts after its submission.
    return {"exit_code": code, "evidence": "Scheduled Task State=Ready LastTaskResult",
            "started_at": int(time.time()) + 1 if started_at is None and bound == "start-time" else started_at,
            "bound": bound}


def test_dead_worker_that_never_claimed_is_failed_with_exit_code_and_log_tail(tmp_path, monkeypatch):
    root, launched, adapter = _launched_job(tmp_path, monkeypatch, _finished(1))
    before_current = (root / "current").read_text()
    settled = update_job.settle_dead_worker(root)
    assert settled["state"] == "failed"
    assert settled["worker_exit_code"] == 1
    assert "ValueError: worker refused before claim" in settled["worker_log_tail"]
    assert "code 1" in settled["message"] and "worker refused before claim" in settled["message"]
    assert settled["message"].startswith("Failed:")
    assert "transaction_id" not in settled
    assert settled["cleanup_status"] == "clean"
    assert "cleanup" in adapter.calls
    # Nothing was installed: the old version stays selected.
    assert (root / "current").read_text() == before_current
    assert update_job.read_job(root)["worker_exit_code"] == 1


def test_dead_worker_after_claim_is_recovery_needed_not_failed(tmp_path, monkeypatch):
    root, launched, _adapter = _launched_job(tmp_path, monkeypatch, _finished(1))
    proof = dict(launched["bootstrap_proof"], claim_state="claimed",
                 native_claim={"operation_id": launched["operation_id"]})
    attempt = dict(launched["attempt"], phase="claimed",
                   claim={"operation_id": launched["operation_id"]})
    update_job._write_job(root, launched["operation_id"], state="active",
                          message="Update is running.",
                          fields={"bootstrap_proof": proof, "attempt": attempt})
    settled = update_job.settle_dead_worker(root)
    assert settled["state"] == "recovery-needed"
    assert settled["worker_exit_code"] == 1
    assert "code 1" in settled["message"]


@pytest.mark.parametrize("finished", [None, _finished(0)], ids=["still-running", "exit-zero"])
def test_running_or_zero_exit_worker_is_left_unchanged(tmp_path, monkeypatch, finished):
    root, _launched, adapter = _launched_job(tmp_path, monkeypatch, finished)
    before = (root / "install.json").read_bytes()
    assert update_job.settle_dead_worker(root) is None
    assert (root / "install.json").read_bytes() == before
    assert "cleanup" not in adapter.calls


def test_lost_acknowledgement_is_left_for_reconcile(tmp_path, monkeypatch):
    root, launched, adapter = _launched_job(tmp_path, monkeypatch, _finished(1))
    update_job._write_job(root, launched["operation_id"], state="launching",
                          message="Update launch acknowledgement was lost; repair will inspect the owned job.",
                          fields={"launch_status": "unknown",
                                  "attempt": dict(launched["attempt"], submit="unknown", handoff=None)})
    before = (root / "install.json").read_bytes()
    assert update_job.settle_dead_worker(root) is None
    assert (root / "install.json").read_bytes() == before
    assert "finished_result" not in adapter.calls


def test_a_run_that_started_before_this_attempt_was_submitted_is_left_unchanged(tmp_path, monkeypatch):
    # A continuation keeps the task name; the previous attempt's exit must not end the new attempt.
    root, launched, _adapter = _launched_job(tmp_path, monkeypatch, None)
    submitted = launched["attempt"]["submitted_at"]
    _adapter.finished = _finished(1, started_at=submitted - 3)
    before = (root / "install.json").read_bytes()
    assert update_job.settle_dead_worker(root) is None
    assert (root / "install.json").read_bytes() == before
    # Within the clock-resolution tolerance it is this attempt's run.
    _adapter.finished = _finished(1, started_at=submitted - 2)
    assert update_job.settle_dead_worker(root)["state"] == "failed"


def test_an_attempt_with_no_submission_time_is_left_unchanged(tmp_path, monkeypatch):
    root, launched, _adapter = _launched_job(tmp_path, monkeypatch, _finished(1))
    attempt = dict(launched["attempt"])
    attempt.pop("submitted_at")
    update_job._write_job(root, launched["operation_id"], state="launching", message="Update job launched.",
                          fields={"attempt": attempt})
    before = (root / "install.json").read_bytes()
    assert update_job.settle_dead_worker(root) is None
    assert (root / "install.json").read_bytes() == before


def test_an_attempt_replaced_before_the_lock_is_left_unchanged(tmp_path, monkeypatch):
    root, launched, adapter = _launched_job(tmp_path, monkeypatch, _finished(1))
    original = adapter.finished_result

    def replaced_during_first_look(descriptor):
        # A continuation lands between the lockless look and the locked re-read.
        current = read_json(root / "install.json")["update_job"]
        epoch = current["attempt"]["epoch"] + 1
        attempt = dict(current["attempt"], epoch=epoch,
                       handoff={"epoch": epoch, "nonce": current["attempt"]["nonce"]})
        update_job._write_job(root, launched["operation_id"], state="launching",
                              message="Update job launched.", fields={"attempt": attempt})
        adapter.finished_result = original
        return original(descriptor)

    adapter.finished_result = replaced_during_first_look
    before_state = read_json(root / "install.json")["update_job"]["state"]
    assert update_job.settle_dead_worker(root) is None
    assert read_json(root / "install.json")["update_job"]["state"] == before_state == "launching"


def test_launchd_result_bound_by_its_loaded_command_is_settled(tmp_path, monkeypatch):
    root, _launched, _adapter = _launched_job(tmp_path, monkeypatch, _finished(1, bound="command"))
    assert update_job.settle_dead_worker(root)["state"] == "failed"


def test_a_result_with_an_unknown_binding_is_left_unchanged(tmp_path, monkeypatch):
    root, _launched, _adapter = _launched_job(tmp_path, monkeypatch, _finished(1, started_at=None, bound=None))
    before = (root / "install.json").read_bytes()
    assert update_job.settle_dead_worker(root) is None
    assert (root / "install.json").read_bytes() == before


def test_held_installer_lock_leaves_dead_worker_unchanged(tmp_path, monkeypatch):
    root, _launched, _adapter = _launched_job(tmp_path, monkeypatch, _finished(1))
    before = (root / "install.json").read_bytes()
    stream = update_job._locked(root)
    try:
        assert update_job.settle_dead_worker(root) is None
    finally:
        update_job._unlock(stream)
    assert (root / "install.json").read_bytes() == before


def _status_get(monkeypatch, capsys):
    """Runtime's status GET: the Agent helper's availability action, with the lock unavailable to it."""
    taken, writes = [], []
    locked, write_job = update_job._locked, update_job._write_job
    # Recorded, not raised: main() suppresses every exception on this path.
    monkeypatch.setattr(update_job, "_locked", lambda root: taken.append(root) or locked(root))
    monkeypatch.setattr(update_job, "_write_job",
                        lambda *args, **kwargs: writes.append(args) or write_job(*args, **kwargs))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"action": "availability"})))
    update_job.main()
    assert taken == [], "A status read must not take the installer lock."
    assert writes == [], "A status read must not write the job record."
    return json.loads(capsys.readouterr().out)


def _claimed(root, launched):
    proof = dict(launched["bootstrap_proof"], claim_state="claimed",
                 native_claim={"operation_id": launched["operation_id"]})
    update_job._write_job(root, launched["operation_id"], state="active", message="Update is running.",
                          fields={"bootstrap_proof": proof})


def test_status_get_reports_a_dead_unclaimed_worker_as_failed_and_writes_nothing(tmp_path, monkeypatch, capsys):
    root, launched, adapter = _launched_job(tmp_path, monkeypatch, _finished(1))
    before = (root / "install.json").read_bytes()
    with monkeypatch.context() as get:
        reply = _status_get(get, capsys)
    assert (root / "install.json").read_bytes() == before
    assert not (root / "update-transaction.json").exists()
    assert {key: reply[key] for key in ("available", "actions")} == {
        "available": True, "actions": list(update_job.SUPPORTED_ACTIONS)}
    observed = reply["observed_job"]
    assert observed["operation_id"] == launched["operation_id"] and observed["persisted"] is False
    assert observed["state"] == "failed" and observed["worker_exit_code"] == 1
    assert "transaction_id" not in observed
    assert "ValueError: worker refused before claim" in observed["worker_log_tail"]
    assert observed["message"].endswith("ValueError: worker refused before claim")
    assert "cleanup" not in adapter.calls  # GET leaves the task registered

    # The next Update press reaches the locked reconciler, which records exactly that outcome.
    reconciled = update_job.reconcile(adapter.request)
    assert reconciled["disposition"] == "confirmed-prior"
    job = read_json(root / "install.json")["update_job"]
    for key in ("state", "message", "worker_exit_code", "worker_log_tail", "worker_wrapper_log_tail"):
        assert job[key] == observed[key], key
    assert reconciled["job"]["state"] == "failed" and "cleanup" in adapter.calls
    assert "launch" not in adapter.calls[adapter.calls.index("finished_result"):]  # not continued


def test_status_get_reports_a_dead_claimed_worker_as_recovery_needed_and_writes_nothing(
        tmp_path, monkeypatch, capsys):
    root, launched, adapter = _launched_job(tmp_path, monkeypatch, _finished(1))
    _claimed(root, launched)
    before = (root / "install.json").read_bytes()
    with monkeypatch.context() as get:
        observed = _status_get(get, capsys)["observed_job"]
    assert (root / "install.json").read_bytes() == before
    assert observed["state"] == "recovery-needed" and observed["worker_exit_code"] == 1
    assert observed["message"].startswith("Recovery needed.")

    with pytest.raises(update_job._BusyRefusal) as refusal:
        update_job.reconcile(adapter.request)
    assert str(refusal.value) == observed["message"]
    job = read_json(root / "install.json")["update_job"]
    for key in ("state", "message", "worker_exit_code", "worker_log_tail"):
        assert job[key] == observed[key], key


def test_status_get_on_a_held_lock_still_only_observes(tmp_path, monkeypatch, capsys):
    # A live installer holds the lock; the GET still reports, and still writes nothing.
    root, _launched, _adapter = _launched_job(tmp_path, monkeypatch, _finished(1))
    before = (root / "install.json").read_bytes()
    stream = update_job._locked(root)
    try:
        with monkeypatch.context() as get:
            assert _status_get(get, capsys)["observed_job"]["state"] == "failed"
    finally:
        update_job._unlock(stream)
    assert (root / "install.json").read_bytes() == before


def test_status_get_and_the_writer_change_nothing_without_attempt_bound_evidence(tmp_path, monkeypatch, capsys):
    root, launched, adapter = _launched_job(tmp_path, monkeypatch, None)
    submitted = launched["attempt"]["submitted_at"]
    for finished in (None, _finished(0), _finished(1, started_at=submitted - 3), _finished(1, bound=None)):
        adapter.finished = finished
        before = (root / "install.json").read_bytes()
        with monkeypatch.context() as get:
            assert "observed_job" not in _status_get(get, capsys)
        assert update_job.settle_dead_worker(root) is None
        assert (root / "install.json").read_bytes() == before


def test_reconciler_leaves_a_journal_bearing_dead_worker_to_its_transaction_recovery(tmp_path, monkeypatch):
    root, launched, adapter = _launched_job(tmp_path, monkeypatch, _finished(1))
    write_json(root / "update-transaction.json",
               {"operation_id": "b" * 32, "step": "staged", "update_job": {"operation_id": launched["operation_id"]}})
    before = (root / "install.json").read_bytes()
    assert update_job._settle_dead_worker_locked(root, adapter, defer_transactions=True) is None
    assert (root / "install.json").read_bytes() == before
    # The install-line writer records it, with the transaction, as GET reported it.
    observed = update_job.observe_dead_worker(root, adapter)
    settled = update_job._settle_dead_worker_locked(root, adapter)
    assert observed["state"] == settled["state"] == "recovery-needed"
    assert observed["transaction_id"] == settled["transaction_id"] == "b" * 32


def test_install_line_sweep_records_the_same_attempt_bound_outcome(tmp_path, monkeypatch):
    root, launched, adapter = _launched_job(tmp_path, monkeypatch, _finished(1))
    observed = update_job.observe_dead_worker(root, adapter)
    swept = update_job.sweep_owned_jobs(root)
    job = read_json(root / "install.json")["update_job"]
    assert swept[0]["state"] == job["state"] == observed["state"] == "failed"
    assert job["message"] == observed["message"] and job["worker_exit_code"] == 1
    assert job["launch_status"] == "acknowledged"


def test_status_availability_read_leaves_a_running_worker_alone(tmp_path, monkeypatch, capsys):
    root, _launched, _adapter = _launched_job(tmp_path, monkeypatch, None)
    before = (root / "install.json").read_bytes()
    with monkeypatch.context() as get:
        reply = _status_get(get, capsys)
    assert reply == {"available": True, "actions": list(update_job.SUPPORTED_ACTIONS)}
    assert (root / "install.json").read_bytes() == before


def test_sweep_of_an_inactive_claimed_job_is_recovery_needed(tmp_path, monkeypatch):
    root, launched, adapter = _launched_job(tmp_path, monkeypatch, None)
    proof = dict(launched["bootstrap_proof"], claim_state="claimed",
                 native_claim={"operation_id": launched["operation_id"]})
    update_job._write_job(root, launched["operation_id"], state="active", message="Update is running.",
                          fields={"bootstrap_proof": proof})
    adapter.finished = _finished(1)  # now inactive
    swept = update_job.sweep_owned_jobs(root)
    assert swept[0]["state"] == "recovery-needed"


_CAPTURED_WORKER_LOG = (Path(__file__).parent / "fixtures" / "installer"
                        / "windows-update-job-run-42498677457.log")


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8"], ids=["as-powershell-wrote-it", "decoded"])
def test_captured_windows_worker_death_is_failed_with_its_error(tmp_path, monkeypatch, encoding):
    # Run 42498677457: the worker raised on its first line, before any claim, and the task ended with 1.
    root, launched, _adapter = _launched_job(tmp_path, monkeypatch, _finished(1))
    text = _CAPTURED_WORKER_LOG.read_text(encoding="utf-8")
    Path(launched["log_path"]).write_bytes(text.encode(encoding))  # utf-16: little-endian with a mark
    settled = update_job.settle_dead_worker(root)
    assert settled["state"] == "failed"
    assert settled["worker_exit_code"] == 1
    assert settled["message"].startswith(
        "Failed: the update worker exited with code 1 before it started; Astrid is unchanged. "
        "Last log line: ogs\\\\astrid-update-39170ee6d8594fe18befe0b9fd4ddffe.log'")
    tail = settled["worker_log_tail"]
    assert "\x00" not in tail and "PermissionError: [Errno 13] Permission denied" in tail
    assert tail.splitlines()[-1] == "ogs\\\\astrid-update-39170ee6d8594fe18befe0b9fd4ddffe.log'"


@pytest.mark.parametrize("job_log", ["empty", "absent"])
def test_startup_traceback_only_in_the_wrapper_log_is_the_failed_message(tmp_path, monkeypatch, job_log):
    # One writer per file: a worker that dies before run_job leaves its traceback only in
    # astrid-update-<op>.wrapper.log. The captured text of run 42498677457 is exactly what
    # the wrapper's *>> wrote (PowerShell's NativeCommandError header, then the traceback).
    from astrid.installer.update_job_platform import wrapper_log_path

    root, launched, _adapter = _launched_job(tmp_path, monkeypatch, _finished(1))
    log = Path(launched["log_path"])
    if job_log == "empty":
        log.write_bytes(b"")
    else:
        log.unlink()
    wrapper = wrapper_log_path(log)
    assert wrapper.name == log.stem + ".wrapper.log"
    wrapper.write_bytes(_CAPTURED_WORKER_LOG.read_text(encoding="utf-8").encode("utf-16"))
    settled = update_job.settle_dead_worker(root)
    assert settled["state"] == "failed" and settled["worker_exit_code"] == 1
    assert settled["worker_log_tail"] is None
    assert "PermissionError: [Errno 13] Permission denied" in settled["worker_wrapper_log_tail"]
    assert settled["message"].startswith(
        "Failed: the update worker exited with code 1 before it started; Astrid is unchanged. "
        "Last log line: ogs\\\\astrid-update-39170ee6d8594fe18befe0b9fd4ddffe.log'")


def test_the_job_log_is_preferred_and_the_wrapper_log_is_kept(tmp_path, monkeypatch):
    from astrid.installer.update_job_platform import wrapper_log_path

    root, launched, _adapter = _launched_job(tmp_path, monkeypatch, _finished(1))
    wrapper_log_path(Path(launched["log_path"])).write_bytes(
        "python.exe : RuntimeError: wrapper side\r\n".encode("utf-16"))
    settled = update_job.settle_dead_worker(root)
    assert settled["message"].endswith("ValueError: worker refused before claim")
    assert "RuntimeError: wrapper side" in settled["worker_wrapper_log_tail"]
