"""Refusal coverage for launchd identity revalidation at the real C1 handoff."""
from __future__ import annotations

import subprocess
import sys
import plistlib
import stat
from pathlib import Path

import pytest

from astrid.installer.core import RuntimeOperationError, Version, read_json, write_json
from astrid.installer.service import LABEL, Service, bootstrap_root, command, platform, render
from tests.test_installer_lifecycle import _installer_runtime, _publish_owner


def _owned_version(backend, name="old", release=17):
    folder = backend.root / "versions" / name
    folder.mkdir(parents=True)
    python = folder / "py" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    version = Version(folder, python, folder / "profile.json", {}, release)
    version.profile.write_text("{}", encoding="utf-8")
    write_json(folder / "version.json", version.document())
    return version


def _print_output(backend, data, version, *, state="running", pid=777, path=None,
                  program=None, loaded_version=None, loaded_action=None):
    argv = list(loaded_action or command(loaded_version or version, data, log=backend.service.log,
                                         env_file=backend.service.env_file))
    plist_path = str(path if path is not None else backend.service.path)
    executable = str(program if program is not None else argv[0])
    argv[0] = executable
    lines = [f"gui/{platform.current_user()}/{LABEL} = {{", f"\tstate = {state}",
             f"\tpath = {plist_path}", f"\tprogram = {executable}",
             "\targuments = {", "\n".join(argv), "\t}"]
    if pid is not None:
        lines.append(f"\tpid = {pid}")
    lines.append("}")
    return "\n".join(lines)


def _prepared(tmp_path, monkeypatch):
    backend, data, paths, _realm, boundary, _manager = _installer_runtime(
        tmp_path, monkeypatch,
    )
    backend.service.system = "darwin"
    backend.service.path = backend.service.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    backend.service.path.parent.mkdir(parents=True, exist_ok=True)
    version = _owned_version(backend)
    backend.service.path.write_text(
        render("darwin", version, data, backend.service.log), encoding="utf-8",
    )
    (paths.realms_dir / "realm-1" / "stop-refusal-sentinel").write_bytes(b"keep workspace")
    _publish_owner(paths)
    signals = []
    original_stop_owner = boundary.stop_owner

    def track_stop_owner(**kwargs):
        signals.append(kwargs.get("pid"))
        result = original_stop_owner(**kwargs)
        boundary.live = False
        paths.discovery_path.unlink(missing_ok=True)
        paths.instance_lock_path.unlink(missing_ok=True)
        return result

    boundary.stop_owner = track_stop_owner
    selected = {"owner": LABEL, "program_root": str(backend.root), "data_root": str(data)}
    write_json(backend.service.selection, selected)
    backend.service.recorded = lambda: read_json(backend.service.selection)
    monkeypatch.setattr(platform, "process_identity", lambda pid: {
        "pid": pid, "uid": platform.current_user(), "birth_id": "birth-1",
    })
    monkeypatch.setattr(platform, "process_executable", lambda _pid: str(version.python))
    outputs = []
    queries = []

    def launchctl(argv):
        queries.append(list(argv))
        if argv[1:2] == ["bootout"]:
            bootouts.append(list(argv))
            return ""
        response = outputs.pop(0) if outputs else _print_output(
            backend, data, version, state="not running", pid=None,
        )
        if isinstance(response, BaseException):
            raise response
        return response

    backend.service.run = launchctl
    bootouts = []
    manager_stops = []
    backend.service.disable = lambda: Service.disable(backend.service)
    backend.service.stop = lambda: manager_stops.append("stop")
    return backend, data, paths, boundary, version, outputs, queries, bootouts, manager_stops, signals


def _r0_program_root_plist(backend, data, version):
    """Put the reviewed frozen R0 program-root service action in the fixture."""
    from tests.test_installer_service_env import _legacy_posix_action

    document = plistlib.loads(backend.service.path.read_bytes())
    document["ProgramArguments"] = _legacy_posix_action(version, data, backend.service.env_file)
    document["KeepAlive"] = True
    document["RunAtLoad"] = True
    value = plistlib.dumps(document)
    backend.service.path.write_bytes(value)
    return value


def _trusted_bootstrap_venv(backend, version):
    """Create the authentic R0 POSIX venv-to-bootstrap interpreter shape."""
    bootstrap = bootstrap_root(backend.service.home, "darwin")
    target = (bootstrap / "python" / "cpython-3.11.13-macos-aarch64-none"
              / "bin" / "python3.11")
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    for entry in (bootstrap, bootstrap / "python", target.parent.parent, target.parent):
        entry.chmod(0o700)
    target.chmod(0o700)
    version.python.parent.mkdir(parents=True, exist_ok=True)
    version.python.unlink(missing_ok=True)
    version.python.symlink_to(target)
    config = version.root / "py" / "pyvenv.cfg"
    config.write_text(f"home = {target.parent}\nversion_info = 3.11.13\n", encoding="utf-8")
    config.chmod(0o600)
    return target, config, bootstrap


def _external_launchd_identity(backend, data, version, outputs, target):
    _observe(backend, data, version, outputs, state="running", pid=777)
    return backend.service.admit_launchd_bootout(
        data, owner_pid=777, owner_birth_id="birth-1",
    )


def _preserved_selection_workspace(backend, data, paths):
    journal = backend.root / "update-transaction.json"
    current = backend.root / "current"
    return {
        "selection": backend.service.selection.read_bytes(),
        "catalog": paths.catalog_path.read_bytes(),
        "workspace_sentinel": (paths.realms_dir / "realm-1" / "stop-refusal-sentinel").read_bytes(),
        "journal": journal.read_bytes() if journal.exists() else None,
        "current": current.read_bytes() if current.exists() else None,
    }


def _observe(backend, data, version, outputs, *, state="not running", pid=None,
             path=None, program=None, loaded_version=None, loaded_action=None):
    outputs.append(_print_output(backend, data, version, state=state, pid=pid,
                                 path=path, program=program, loaded_version=loaded_version,
                                 loaded_action=loaded_action))


def _corrupt_observation(backend, data, version, *, state, pid, corruption):
    value = _print_output(backend, data, version, state=state, pid=pid)
    target = f"gui/{platform.current_user()}/{LABEL}"
    if corruption == "malformed_pid":
        value = value.replace("\tpid = 777", "\tpid = ???", 1)
        if pid is None:
            value = value.replace("\targuments = {", "\tpid = ???\n\targuments = {", 1)
    elif corruption == "duplicate_state":
        value = value.replace("\tstate = " + state,
                              "\tstate = " + state + "\n\tstate = " + state, 1)
    elif corruption == "duplicate_pid":
        count = 2 if pid is None else 1
        for _ in range(count):
            value = value.replace("\targuments = {", "\tpid = 777\n\targuments = {", 1)
    elif corruption == "truncated_root":
        assert value.endswith("\n}")
        value = value[:-2]
    elif corruption == "wrong_target":
        value = value.replace(target + " = {",
                              f"gui/{platform.current_user()}/com.example.unrelated = {{", 1)
    return value


@pytest.mark.parametrize("corruption", ["missing", "duplicate"])
def test_nested_coalition_state_cannot_supply_or_hide_top_level_state(tmp_path, monkeypatch, corruption):
    backend, data, _paths, _boundary, version, *_ = _prepared(tmp_path, monkeypatch)
    output = _print_output(backend, data, version)
    if corruption == "missing":
        output = output.replace("\tstate = running\n", "", 1)
    else:
        output = output.replace("\tstate = running\n", "\tstate = running\n\tstate = running\n", 1)
    output = output[:-1] + "\tresource coalition = {\n\t\tstate = active\n\t}\n}"
    with pytest.raises(ValueError, match="launchd service identity is malformed"):
        backend.service._launchd_stop_identity(output, data)


@pytest.mark.parametrize("case", [
    "unrelated_loaded_identity", "changed_action", "changed_path", "replacement_pid_before_exit",
])
def test_launchd_initial_identity_refusal_precedes_runtime_signal(tmp_path, monkeypatch, case):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, _manager_stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    kwargs = {}
    if case == "unrelated_loaded_identity":
        kwargs["path"] = backend.service.path.with_name("different.plist")
    elif case == "changed_action":
        kwargs["program"] = "/unrelated/astrid-runtime"
    elif case == "changed_path":
        kwargs["path"] = "/tmp/replaced-launch-agent.plist"
    elif case == "replacement_pid_before_exit":
        kwargs["pid"] = 888
    _observe(backend, data, version, outputs, **{"state": "running", "pid": 777, **kwargs})
    before = _preserved_selection_workspace(backend, data, paths)

    with pytest.raises(RuntimeOperationError):
        backend.stop(version, data)

    assert boundary.stop_calls == 0
    assert signals == []
    assert paths.discovery_path.exists()
    assert read_json(backend.service.selection) == {
        "owner": LABEL, "program_root": str(backend.root), "data_root": str(data),
    }
    assert _preserved_selection_workspace(backend, data, paths) == before
    assert not any(argv[1:2] == ["bootout"] for argv in queries)


@pytest.mark.parametrize("case", ["unknown_state", "malformed_state", "query_error", "running_without_pid"])
def test_launchd_initial_uncertain_observation_refuses_before_c1(tmp_path, monkeypatch, case):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, _manager_stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    if case == "unknown_state":
        _observe(backend, data, version, outputs, state="mystery", pid=None)
    elif case == "malformed_state":
        outputs.append("bad launchctl output")
    elif case == "query_error":
        outputs.append(subprocess.CalledProcessError(
            5, ["launchctl", "print"], output="launchctl print failed unexpectedly",
        ))
    else:
        _observe(backend, data, version, outputs, state="running", pid=None)
    before = _preserved_selection_workspace(backend, data, paths)

    with pytest.raises(RuntimeOperationError):
        backend.stop(version, data)

    assert boundary.stop_calls == 0
    assert signals == []
    assert paths.discovery_path.exists()
    assert _preserved_selection_workspace(backend, data, paths) == before
    assert not any(argv[1:2] == ["bootout"] for argv in queries)


@pytest.mark.parametrize("corruption", [
    "malformed_pid", "duplicate_state", "duplicate_pid", "truncated_root", "wrong_target",
])
@pytest.mark.parametrize("phase", ["initial", "post_exit"])
def test_valid_launchctl_observation_single_corruption_refuses(tmp_path, monkeypatch, corruption, phase):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, _manager_stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    if phase == "initial":
        outputs.append(_corrupt_observation(
            backend, data, version, state="running", pid=777, corruption=corruption,
        ))
    else:
        _observe(backend, data, version, outputs, state="running", pid=777)
        outputs.append(_corrupt_observation(
            backend, data, version, state="not running", pid=None, corruption=corruption,
        ))
    before = _preserved_selection_workspace(backend, data, paths)

    with pytest.raises(RuntimeOperationError):
        backend.stop(version, data)

    # Both observations precede bootout now. A malformed second observation
    # cannot be deferred to C1, where frozen R0 KeepAlive could race it.
    assert boundary.stop_calls == 0
    assert signals == []
    assert _preserved_selection_workspace(backend, data, paths) == before
    assert not any(argv[1:2] == ["bootout"] for argv in queries)


@pytest.mark.parametrize("case", [
    "changed_loaded_identity", "changed_action", "changed_path", "replacement_pid_after_exit",
    "changed_publication", "unknown_state", "malformed_state", "query_error",
    "final_changed_action", "final_replacement_pid", "retained_action_swap",
    "initially_unloaded_then_loaded",
])
def test_launchd_post_exit_refusal_preserves_selection_and_never_boots_out(
        tmp_path, monkeypatch, case):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, _manager_stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    if case == "initially_unloaded_then_loaded":
        outputs.append(subprocess.CalledProcessError(
            113, ["launchctl", "print"], output="Could not find service",
        ))
        _observe(backend, data, version, outputs)
    else:
        _observe(backend, data, version, outputs, state="running", pid=777)

    if case == "changed_loaded_identity":
        _observe(backend, data, version, outputs, path=backend.service.path.with_name("other.plist"))
    elif case == "changed_action":
        alternate = _owned_version(backend, "alternate", 18)
        _observe(backend, data, version, outputs, loaded_version=alternate)
    elif case == "changed_path":
        _observe(backend, data, version, outputs, path="/tmp/replacement-launch-agent.plist")
    elif case == "replacement_pid_after_exit":
        _observe(backend, data, version, outputs, state="running", pid=888)
    elif case == "unknown_state":
        _observe(backend, data, version, outputs, state="mystery")
    elif case == "malformed_state":
        outputs.append("service = {\n state =\n path = /tmp/job.plist\n")
    elif case == "query_error":
        outputs.append(subprocess.CalledProcessError(
            5, ["launchctl", "print"], output="launchctl print failed unexpectedly",
        ))
    elif case == "final_changed_action":
        _observe(backend, data, version, outputs)
        alternate = _owned_version(backend, "alternate", 18)
        _observe(backend, data, version, outputs, loaded_version=alternate)
    elif case == "final_replacement_pid":
        _observe(backend, data, version, outputs)
        _observe(backend, data, version, outputs, state="running", pid=888)
    elif case == "retained_action_swap":
        _observe(backend, data, version, outputs)
        alternate_root = backend.root / "versions" / "alternate"
        alternate_root.mkdir(parents=True)
        alternate = Version(alternate_root, Path(sys.executable), alternate_root / "profile.json", {}, 18)
        alternate.profile.write_text("{}", encoding="utf-8")
        write_json(alternate_root / "version.json", alternate.document())
        original_stop_owner = boundary.stop_owner

        def replace_owned_action(**kwargs):
            result = original_stop_owner(**kwargs)
            backend.service.path.write_text(
                render("darwin", alternate, data, backend.service.log), encoding="utf-8",
            )
            return result

        boundary.stop_owner = replace_owned_action
    elif case == "changed_publication":
        _observe(backend, data, version, outputs)
        original_stop_owner = boundary.stop_owner
        replacement = {"pid": 888, "process_birth_id": "replacement",
                      "runtime_instance_id": "replacement-instance"}

        def replace_publication(**kwargs):
            result = original_stop_owner(**kwargs)
            write_json(paths.discovery_path, replacement)
            write_json(paths.instance_lock_path, replacement)
            return result

        boundary.stop_owner = replace_publication
        publication_expected = replacement
    else:
        publication_expected = None

    selection_before = backend.service.selection.read_bytes()
    preserved_before = _preserved_selection_workspace(backend, data, paths)
    with pytest.raises(RuntimeOperationError):
        backend.stop(version, data)

    before_c1 = {
        "changed_loaded_identity", "changed_action", "changed_path", "replacement_pid_after_exit",
        "unknown_state", "malformed_state", "query_error",
    }
    assert boundary.stop_calls == (0 if case in before_c1 else 1)
    assert signals == ([] if case in before_c1 else [777])
    assert backend.service.selection.read_bytes() == selection_before
    assert _preserved_selection_workspace(backend, data, paths) == preserved_before
    if case == "changed_publication":
        assert read_json(paths.discovery_path) == publication_expected
        assert read_json(paths.instance_lock_path) == publication_expected
    if case in before_c1 or case == "initially_unloaded_then_loaded":
        assert not any(argv[1:2] == ["bootout"] for argv in queries)
    else:
        # A post-bootout refusal may not use the replacement as a second stop
        # target or issue another bootout.
        assert [argv[1] for argv in queries].count("bootout") == 1
    # The only C1 stop was the selected owner PID captured in the discovery
    # publication; launchd replacement PIDs must never be signaled.


@pytest.mark.parametrize("state,pid", [("not running", 777), ("mystery", None)])
def test_stopped_state_requires_explicit_clean_observation(tmp_path, monkeypatch, state, pid):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, _manager_stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    _observe(backend, data, version, outputs, state="running", pid=777)
    _observe(backend, data, version, outputs, state=state, pid=pid)
    selection_before = backend.service.selection.read_bytes()
    preserved_before = _preserved_selection_workspace(backend, data, paths)

    with pytest.raises(RuntimeOperationError):
        backend.stop(version, data)

    assert boundary.stop_calls == 0
    assert signals == []
    assert backend.service.selection.read_bytes() == selection_before
    assert _preserved_selection_workspace(backend, data, paths) == preserved_before
    assert not any(argv[1:2] == ["bootout"] for argv in queries)


def test_validated_r0_keepalive_boots_out_before_c1_and_then_requires_absence(tmp_path, monkeypatch):
    backend, data, _paths, boundary, version, outputs, queries, _bootouts, manager_stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    original = _r0_program_root_plist(backend, data, version)
    action = plistlib.loads(original)["ProgramArguments"]
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "step": "staged", "data_root": str(data), "old": version.document(),
    }
    write_json(backend.root / "update-transaction.json", transaction)
    backend._update_transaction = (transaction, False)
    backend.publish_launch_admission(version, data)
    snapshot = transaction["launchd_r0_plist"]
    assert backend.service.path.read_bytes() == original
    assert snapshot["sha256"]
    _observe(backend, data, version, outputs, state="running", pid=777, loaded_action=action)
    _observe(backend, data, version, outputs, state="running", pid=777, loaded_action=action)
    outputs.append(subprocess.CalledProcessError(
        113, ["launchctl", "print"], output="Could not find service",
    ))

    backend.stop(version, data)

    bootout = next(index for index, argv in enumerate(queries) if argv[1:2] == ["bootout"])
    assert bootout < len(queries) - 1
    assert boundary.stop_calls == 1
    assert signals == [777]
    assert manager_stops == ["stop"]
    assert snapshot["bootout_intent"] is True
    assert backend.service.path.read_bytes() == original


@pytest.mark.parametrize("mismatch", ["label", "plist_path", "arguments", "executable", "uid"])
def test_r0_prebootout_admission_refuses_each_owner_mismatch(tmp_path, monkeypatch, mismatch):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, _stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    if mismatch == "label":
        document = plistlib.loads(backend.service.path.read_bytes())
        document["Label"] = "com.example.foreign"
        backend.service.path.write_bytes(plistlib.dumps(document))
    elif mismatch == "arguments":
        document = plistlib.loads(backend.service.path.read_bytes())
        document["ProgramArguments"] = [*document["ProgramArguments"], "--foreign"]
        backend.service.path.write_bytes(plistlib.dumps(document))
    elif mismatch == "plist_path":
        _observe(backend, data, version, outputs, state="running", pid=777,
                 path=backend.service.path.with_name("foreign.plist"))
    elif mismatch == "executable":
        monkeypatch.setattr(platform, "process_executable", lambda _pid: str(tmp_path / "foreign-python"))
        _observe(backend, data, version, outputs, state="running", pid=777)
    else:
        monkeypatch.setattr(platform, "process_identity", lambda pid: {
            "pid": pid, "uid": platform.current_user() + 1, "birth_id": "birth-1",
        })
        _observe(backend, data, version, outputs, state="running", pid=777)
    before = _preserved_selection_workspace(backend, data, paths)

    with pytest.raises(RuntimeOperationError):
        backend.stop(version, data)

    assert boundary.stop_calls == 0
    assert signals == []
    assert not any(argv[1:2] == ["bootout"] for argv in queries)
    assert _preserved_selection_workspace(backend, data, paths) == before


def test_r0_post_bootout_reappearance_refuses_the_replacement_pid(tmp_path, monkeypatch):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, _stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    _observe(backend, data, version, outputs, state="running", pid=777)
    _observe(backend, data, version, outputs, state="running", pid=777)
    _observe(backend, data, version, outputs, state="running", pid=888)
    before = _preserved_selection_workspace(backend, data, paths)

    with pytest.raises(RuntimeOperationError, match="appeared after the R0 bootout"):
        backend.stop(version, data)

    assert boundary.stop_calls == 1
    assert signals == [777]
    assert [argv[1] for argv in queries].count("bootout") == 1
    assert _preserved_selection_workspace(backend, data, paths) == before


def test_recovery_intent_refuses_same_definition_with_replacement_live_identity(tmp_path, monkeypatch):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, _stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    original = _r0_program_root_plist(backend, data, version)
    action = plistlib.loads(original)["ProgramArguments"]
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "step": "stopping", "data_root": str(data), "old": version.document(),
    }
    write_json(backend.root / "update-transaction.json", transaction)
    backend._update_transaction = (transaction, False)
    backend.publish_launch_admission(version, data)
    snapshot = transaction["launchd_r0_plist"]
    _observe(backend, data, version, outputs, state="running", pid=777, loaded_action=action)
    admitted = backend.service.admit_launchd_bootout(
        data, owner_pid=777, owner_birth_id="birth-1", recovery_snapshot=snapshot,
    )
    backend._mark_launchd_bootout_intent(data, snapshot, admitted)
    assert snapshot["bootout_identity"]["pid"] == 777

    replacement = read_json(paths.discovery_path)
    replacement.update(pid=888, process_birth_id="birth-2")
    write_json(paths.discovery_path, replacement)
    write_json(paths.instance_lock_path, replacement)
    monkeypatch.setattr(platform, "process_identity", lambda pid: {
        "pid": pid, "uid": platform.current_user(), "birth_id": "birth-2",
    })
    _observe(backend, data, version, outputs, state="running", pid=888, loaded_action=action)
    backend._update_transaction = (transaction, True)

    with pytest.raises(RuntimeOperationError, match="appeared after the R0 bootout"):
        backend.stop(version, data)

    assert boundary.stop_calls == 0
    assert signals == []
    assert not any(argv[1:2] == ["bootout"] for argv in queries)


def _interrupted_stopped_r0(backend, data, paths, boundary, version, outputs):
    """Persist a pre-bootout stopped R0 recovery boundary with no live owner."""
    original = _r0_program_root_plist(backend, data, version)
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "step": "stopping", "data_root": str(data), "old": version.document(),
    }
    write_json(backend.root / "update-transaction.json", transaction)
    backend._update_transaction = (transaction, False)
    backend.publish_launch_admission(version, data)
    snapshot = transaction["launchd_r0_plist"]
    assert backend.service.path.read_bytes() == original
    action = plistlib.loads(original)["ProgramArguments"]
    boundary.live = False
    paths.discovery_path.unlink()
    paths.instance_lock_path.unlink()
    _observe(backend, data, version, outputs, state="not running", pid=None,
             loaded_action=action)
    admitted = backend.service.admit_launchd_bootout(
        data, owner_pid=None, owner_birth_id=None, recovery_snapshot=snapshot,
    )
    backend._mark_launchd_bootout_intent(data, snapshot, admitted)
    assert snapshot["bootout_identity"] is None
    backend._update_transaction = (transaction, True)
    return transaction, snapshot, action


def test_recovery_intent_accepts_matching_stopped_job_without_runtime_owner(tmp_path, monkeypatch):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, manager_stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    _transaction, snapshot, action = _interrupted_stopped_r0(
        backend, data, paths, boundary, version, outputs,
    )
    _observe(backend, data, version, outputs, state="not running", pid=None,
             loaded_action=action)
    _observe(backend, data, version, outputs, state="not running", pid=None,
             loaded_action=action)
    outputs.append(subprocess.CalledProcessError(
        113, ["launchctl", "print"], output="Could not find service",
    ))

    backend.stop(version, data)

    assert snapshot["bootout_identity"] is None
    assert boundary.stop_calls == 0
    assert signals == []
    assert manager_stops == ["stop"]
    assert [argv[1] for argv in queries].count("bootout") == 1


def test_recovery_intent_refuses_stopped_job_with_changed_definition(tmp_path, monkeypatch):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, _stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    _transaction, _snapshot, action = _interrupted_stopped_r0(
        backend, data, paths, boundary, version, outputs,
    )
    _observe(backend, data, version, outputs, state="not running", pid=None,
             loaded_action=[*action, "--changed"])

    with pytest.raises(RuntimeOperationError):
        backend.stop(version, data)

    assert boundary.stop_calls == 0
    assert signals == []
    assert not any(argv[1:2] == ["bootout"] for argv in queries)


def test_recovery_intent_refuses_stopped_job_with_live_non_r0_owner(tmp_path, monkeypatch):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, _stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    _transaction, _snapshot, action = _interrupted_stopped_r0(
        backend, data, paths, boundary, version, outputs,
    )
    boundary.live = True
    _publish_owner(paths, pid=888, birth="birth-1")
    _observe(backend, data, version, outputs, state="not running", pid=None,
             loaded_action=action)

    with pytest.raises(RuntimeOperationError, match="appeared after the R0 bootout"):
        backend.stop(version, data)

    assert boundary.stop_calls == 0
    assert signals == []
    assert not any(argv[1:2] == ["bootout"] for argv in queries)


def test_exact_owned_stopped_job_reaches_one_bootout_after_c1(tmp_path, monkeypatch):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, manager_stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    _observe(backend, data, version, outputs, state="running", pid=777)
    _observe(backend, data, version, outputs, state="running", pid=777)
    outputs.append(subprocess.CalledProcessError(
        113, ["launchctl", "print"], output="Could not find service",
    ))

    backend.stop(version, data)

    assert boundary.stop_calls == 1
    assert signals == [777]
    assert [argv[1] for argv in queries].count("bootout") == 1
    assert manager_stops == ["stop"]
    with pytest.raises(RuntimeError, match="validated Runtime C1"):
        Service.disable(backend.service)
    assert [argv[1] for argv in queries].count("bootout") == 1


def test_busy_realm_refuses_without_runtime_signal_or_launchd_mutation(tmp_path, monkeypatch):
    import sqlite3

    backend, data, paths, boundary, version, outputs, queries, _bootouts, _manager_stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    _observe(backend, data, version, outputs, state="running", pid=777)
    # The fixture's selected realm is a real Runtime realm with the C1 busy tables.
    realm = paths.realms_dir / "realm-1"
    with sqlite3.connect(realm / "realm.sqlite3") as connection:
        connection.execute("INSERT INTO runs(id,capability,status,created_at,updated_at) "
                           "VALUES ('busy-run','render','running','2000-01-01T00:00:00Z','2000-01-01T00:00:00Z')")
        connection.execute("INSERT INTO tasks(id,run_id,capability,status,created_at,updated_at) "
                           "VALUES ('busy-task','busy-run','render','running','2000-01-01T00:00:00Z','2000-01-01T00:00:00Z')")
    selection_before = backend.service.selection.read_bytes()
    preserved_before = _preserved_selection_workspace(backend, data, paths)

    with pytest.raises(RuntimeOperationError):
        backend.stop(version, data)

    assert not any(argv[1:2] == ["bootout"] for argv in queries)
    assert boundary.stop_calls == 0
    assert signals == []
    assert backend.service.selection.read_bytes() == selection_before
    assert _preserved_selection_workspace(backend, data, paths) == preserved_before
    assert paths.discovery_path.exists()


def test_already_unloaded_launchd_job_is_idempotent_after_c1_exit(tmp_path, monkeypatch):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, manager_stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    _observe(backend, data, version, outputs, state="running", pid=777)
    _observe(backend, data, version, outputs, state="running", pid=777)
    outputs.append(subprocess.CalledProcessError(
        113, ["launchctl", "print"], output="Could not find service",
    ))
    # The validated job is gone before Runtime's C1 callback runs.
    backend.stop(version, data)

    assert boundary.stop_calls == 1
    assert signals == [777]
    assert manager_stops == ["stop"]
    assert [argv[1] for argv in queries].count("bootout") == 1


@pytest.mark.parametrize("layout", ["default", "custom"])
def test_trusted_bootstrap_venv_interpreter_is_shared_by_live_and_retained_validation(
        tmp_path, monkeypatch, layout):
    backend, data, _paths, _boundary, version, outputs, _queries, _bootouts, _stops, _signals = _prepared(
        tmp_path, monkeypatch,
    )
    if layout == "default":
        root = bootstrap_root(backend.service.home, "darwin").parent
        root.mkdir(parents=True, exist_ok=True)
        backend.service.root = root
        version_root = root / "versions" / "old"
        version_root.mkdir(parents=True)
        program = version_root / "py" / "bin" / "python"
        program.parent.mkdir(parents=True)
        version = Version(version_root, program, version_root / "profile.json", {}, 17)
        version.profile.write_text("{}", encoding="utf-8")
        target, config, _bootstrap = _trusted_bootstrap_venv(backend, version)
        # The bootstrap is within the default program root.  This preserves the
        # ordinary in-root form while using the same rule as the custom case.
        assert backend.service._trusted_launchd_interpreter(program, target)
        assert config.is_file()
        return

    target, _config, _bootstrap = _trusted_bootstrap_venv(backend, version)
    monkeypatch.setattr(platform, "process_executable", lambda _pid: str(target))
    identity = _external_launchd_identity(backend, data, version, outputs, target)
    retained = backend.service.retain_launchd_bootout_identity(identity)

    assert identity["live_owner"]["format"] == "astrid-launchd-r0-live-owner/v2"
    assert identity["live_owner"]["program"] == str(version.python)
    assert retained == identity["live_owner"]
    assert backend.service.launchd_bootout_identity_matches(identity, retained)


@pytest.mark.parametrize("case", [
    "foreign_owner", "unsafe_target_mode", "unsafe_bootstrap_mode", "traversal",
    "symlinked_parent", "wrong_home", "wrong_target", "untrusted_bootstrap",
])
def test_trusted_bootstrap_venv_refuses_each_provenance_mismatch(tmp_path, monkeypatch, case):
    backend, _data, _paths, _boundary, version, _outputs, _queries, _bootouts, _stops, _signals = _prepared(
        tmp_path, monkeypatch,
    )
    target, config, bootstrap = _trusted_bootstrap_venv(backend, version)
    if case == "foreign_owner":
        from astrid.installer import service as module

        owned = module.owned_by_current_user
        monkeypatch.setattr(module, "owned_by_current_user",
                            lambda path, observed: path != target and owned(path, observed))
    elif case == "unsafe_target_mode":
        target.chmod(0o720)
    elif case == "unsafe_bootstrap_mode":
        bootstrap.chmod(0o720)
    elif case == "traversal":
        config.write_text(f"home = {target.parent}/../bin\n", encoding="utf-8")
    elif case == "symlinked_parent":
        parent = version.python.parent
        real_parent = parent.with_name("real-bin")
        parent.rename(real_parent)
        parent.symlink_to(real_parent, target_is_directory=True)
    elif case == "wrong_home":
        wrong = target.parent.with_name("wrong-home")
        wrong.mkdir()
        config.write_text(f"home = {wrong}\n", encoding="utf-8")
    elif case == "wrong_target":
        wrong = target.parent.parent / "other-bin" / "python3"
        wrong.parent.mkdir()
        wrong.write_text("#!/bin/sh\n", encoding="utf-8")
        wrong.chmod(0o700)
        config.write_text(f"home = {wrong.parent}\n", encoding="utf-8")
        version.python.unlink()
        version.python.symlink_to(wrong)
    else:
        fake_home = tmp_path / "attacker-home"
        fake_target = fake_home / "python" / "cpython-3.11.13-macos-aarch64-none" / "bin" / "python3.11"
        fake_target.parent.mkdir(parents=True)
        fake_target.write_text("#!/bin/sh\n", encoding="utf-8")
        fake_target.chmod(0o700)
        config.write_text(f"home = {fake_target.parent}\n", encoding="utf-8")
        version.python.unlink()
        version.python.symlink_to(fake_target)
        monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", str(fake_home / "python"))
        monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / "data"))
        assert bootstrap_root(backend.service.home, "darwin") == bootstrap

    assert not backend.service._trusted_launchd_interpreter(version.python, version.python.resolve())


@pytest.mark.parametrize("case", ["pid", "birth", "owner"])
def test_external_bootstrap_venv_keeps_live_identity_refusals(tmp_path, monkeypatch, case):
    backend, data, _paths, _boundary, version, outputs, queries, _bootouts, _stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    target, _config, _bootstrap = _trusted_bootstrap_venv(backend, version)
    monkeypatch.setattr(platform, "process_executable", lambda _pid: str(target))
    if case == "pid":
        _observe(backend, data, version, outputs, state="running", pid=778)
        owner_pid, owner_birth = 777, "birth-1"
    elif case == "birth":
        monkeypatch.setattr(platform, "process_identity", lambda pid: {
            "pid": pid, "uid": platform.current_user(), "birth_id": "replacement-birth",
        })
        _observe(backend, data, version, outputs, state="running", pid=777)
        owner_pid, owner_birth = 777, "birth-1"
    else:
        monkeypatch.setattr(platform, "process_identity", lambda pid: {
            "pid": pid, "uid": platform.current_user() + 1, "birth_id": "birth-1",
        })
        _observe(backend, data, version, outputs, state="running", pid=777)
        owner_pid, owner_birth = 777, "birth-1"

    with pytest.raises(ValueError, match="selected Runtime owner"):
        backend.service.admit_launchd_bootout(data, owner_pid=owner_pid, owner_birth_id=owner_birth)

    assert signals == []
    assert not any(argv[1:2] == ["bootout"] for argv in queries)


def test_external_bootstrap_venv_retained_identity_rejects_inconsistent_program_and_changed_target(
        tmp_path, monkeypatch):
    backend, data, _paths, _boundary, version, outputs, _queries, _bootouts, _stops, _signals = _prepared(
        tmp_path, monkeypatch,
    )
    target, _config, _bootstrap = _trusted_bootstrap_venv(backend, version)
    monkeypatch.setattr(platform, "process_executable", lambda _pid: str(target))
    identity = _external_launchd_identity(backend, data, version, outputs, target)
    retained = backend.service.retain_launchd_bootout_identity(identity)
    malformed = {**retained, "program": str(version.python.with_name("foreign-python"))}

    with pytest.raises(ValueError, match="retained R0 LaunchAgent live identity"):
        backend.service._validated_launchd_bootout_identity(malformed)

    replacement = target.with_name("python3")
    replacement.write_text("#!/bin/sh\n", encoding="utf-8")
    replacement.chmod(stat.S_IMODE(target.stat().st_mode))
    version.python.unlink()
    version.python.symlink_to(replacement)
    with pytest.raises(ValueError, match="retained R0 LaunchAgent live identity"):
        backend.service.launchd_bootout_identity_matches(identity, retained)


@pytest.mark.parametrize("appears", [False, True])
def test_absent_plist_still_rechecks_loaded_job_at_c1_bootout_boundary(tmp_path, monkeypatch, appears):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, _stops, signals = _prepared(
        tmp_path, monkeypatch)
    boundary.live = False
    paths.discovery_path.unlink()
    paths.instance_lock_path.unlink()
    backend.service.path.unlink()
    for _ in range(1 if appears else 2):
        outputs.append(subprocess.CalledProcessError(
            113, ["launchctl", "print"], output="Could not find service"))
    if appears:
        _observe(backend, data, version, outputs)
        with pytest.raises(RuntimeOperationError):
            backend.stop(version, data)
    else:
        backend.stop(version, data)

    assert len(queries) == 2
    assert all(argv[:2] == ["launchctl", "print"] for argv in queries)
    assert signals == []
