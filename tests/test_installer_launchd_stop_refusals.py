"""Refusal coverage for launchd identity revalidation at the real C1 handoff."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from astrid.installer.core import RuntimeOperationError, Version, read_json, write_json
from astrid.installer.service import LABEL, Service, command, platform, render
from tests.test_installer_lifecycle import _installer_runtime, _publish_owner


def _owned_version(backend, name="old", release=17):
    folder = backend.root / "versions" / name
    folder.mkdir(parents=True)
    version = Version(folder, Path(sys.executable), folder / "profile.json", {}, release)
    version.profile.write_text("{}", encoding="utf-8")
    write_json(folder / "version.json", version.document())
    return version


def _print_output(backend, data, version, *, state="running", pid=777, path=None,
                  program=None, loaded_version=None):
    argv = command(loaded_version or version, data, log=backend.service.log,
                   env_file=backend.service.env_file)
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
    outputs = []
    queries = []

    def launchctl(argv):
        queries.append(list(argv))
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
             path=None, program=None, loaded_version=None):
    outputs.append(_print_output(backend, data, version, state=state, pid=pid,
                                 path=path, program=program, loaded_version=loaded_version))


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

    assert boundary.stop_calls == (0 if phase == "initial" else 1)
    assert signals == ([] if phase == "initial" else [777])
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

    assert boundary.stop_calls == 1
    assert signals == [777]
    assert backend.service.selection.read_bytes() == selection_before
    assert _preserved_selection_workspace(backend, data, paths) == preserved_before
    if case == "changed_publication":
        assert read_json(paths.discovery_path) == publication_expected
        assert read_json(paths.instance_lock_path) == publication_expected
    assert not any(argv[1:2] == ["bootout"] for argv in queries)
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

    assert boundary.stop_calls == 1
    assert signals == [777]
    assert backend.service.selection.read_bytes() == selection_before
    assert _preserved_selection_workspace(backend, data, paths) == preserved_before
    assert not any(argv[1:2] == ["bootout"] for argv in queries)


def test_exact_owned_stopped_job_reaches_one_bootout_after_c1(tmp_path, monkeypatch):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, manager_stops, signals = _prepared(
        tmp_path, monkeypatch,
    )
    _observe(backend, data, version, outputs, state="running", pid=777)
    _observe(backend, data, version, outputs)
    _observe(backend, data, version, outputs)

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
    outputs.append(subprocess.CalledProcessError(
        113, ["launchctl", "print"], output="Could not find service",
    ))
    outputs.append(subprocess.CalledProcessError(
        113, ["launchctl", "print"], output="Could not find service",
    ))
    # Runtime's C1 callback sees that launchd already unloaded this selected label.
    backend.stop(version, data)

    assert boundary.stop_calls == 1
    assert signals == [777]
    assert manager_stops == ["stop"]
    assert not any(argv[1:2] == ["bootout"] for argv in queries)


@pytest.mark.parametrize("appears", [False, True])
def test_absent_plist_still_rechecks_loaded_job_at_c1_bootout_boundary(tmp_path, monkeypatch, appears):
    backend, data, paths, boundary, version, outputs, queries, _bootouts, _stops, signals = _prepared(
        tmp_path, monkeypatch)
    boundary.live = False
    paths.discovery_path.unlink()
    paths.instance_lock_path.unlink()
    backend.service.path.unlink()
    for _ in range(2 if appears else 3):
        outputs.append(subprocess.CalledProcessError(
            113, ["launchctl", "print"], output="Could not find service"))
    if appears:
        _observe(backend, data, version, outputs)
        with pytest.raises(RuntimeOperationError):
            backend.stop(version, data)
    else:
        backend.stop(version, data)

    assert len(queries) == 3
    assert all(argv[:2] == ["launchctl", "print"] for argv in queries)
    assert signals == []
