"""R0 publications, native Linux bridge, and Agent consumer refusal controls."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from runtime_protocol import platform

from astrid.installer import update_job
from astrid.installer.backend import Backend
from astrid.installer.birth_identity import birth_matches_live_owner
from astrid.installer.core import current, write_json
from astrid.installer.service import LABEL, Service

CAPTURES = Path(__file__).parent / "fixtures/release0/owner-captures"
LINUX = pytest.mark.skipif(sys.platform != "linux", reason="Runtime's R0 Linux bridge")


def _capture(name):
    checksums = dict(row.split(maxsplit=1)[::-1]
                     for row in (CAPTURES / "SHA256SUMS").read_text().splitlines())
    raw = (CAPTURES / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == checksums[name]
    return json.loads(raw)


def _publications(tmp_path):
    from astrid_runtime.paths import RuntimePaths

    ready = _capture("r0-ready.json")
    paths = RuntimePaths.current(data_root=tmp_path / "data")
    for path, name in [(paths.discovery_path, "runtime/discovery.json"),
                       (paths.instance_lock_path, "runtime/instance.lock")]:
        item = ready["files"][name]
        raw = item["contents"].encode()
        assert len(raw) == item["size"]
        assert hashlib.sha256(raw).hexdigest() == item["sha256"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    discovery = json.loads(paths.discovery_path.read_bytes())
    assert discovery == _capture("r0-selection.json")["discovery"]
    return paths, discovery, ready["selected_process_sample"]


def _scoped(sample):
    return {"pid": sample["pid"], "uid": sample["uid"], "birth_id": (
        f"proc-start-ticks:{sample['start_ticks']};boot:{sample['boot_id']};pidns:{sample['pidns_inode']}")}


def _replay_proc(monkeypatch, sample):
    # Replay the recorded OS scope for historical PID 3602. This is separate
    # from the live native test below and does not claim that PID exists now.
    read_text, stat = Path.read_text, Path.stat

    def read(path, *args, **kwargs):
        if str(path) == "/proc/sys/kernel/random/boot_id":
            return sample["boot_id"] + "\n"
        return read_text(path, *args, **kwargs)

    def metadata(path, *args, **kwargs):
        if str(path) == "/proc/1/ns/pid":
            return SimpleNamespace(st_ino=sample["pidns_inode"])
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "stat", metadata)
    monkeypatch.setattr(platform, "process_identity", lambda _pid, **_kwargs: _scoped(sample))


def _manager(tmp_path, monkeypatch, discovery):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    manager = Service(tmp_path / "program", tmp_path / "service.log", Mock(),
                      home=tmp_path, system="linux")
    data = tmp_path / "data"
    write_json(data / "runtime/catalog.json", {
        "selected_realm_id": discovery["active_realm"],
        "realms": [{"realm_id": discovery["active_realm"], "data_root": discovery["realm_root"]}],
    })
    expected = {"pid": discovery["pid"], "birth_id": discovery["process_birth_id"],
                "realm_id": discovery["active_realm"], "realm_root": discovery["realm_root"]}
    write_json(manager.selection, {"owner": LABEL, "program_root": str(manager.root),
                                  "data_root": str(data), "foreground": expected})
    monkeypatch.setattr(manager, "systemd", lambda: False)
    return manager, expected


def test_captured_r0_current_load_preserves_managed_classification(tmp_path, monkeypatch):
    paths, _discovery, _sample = _publications(tmp_path)
    original = _capture("r0-selection.json")["version"]
    root = tmp_path / "program"
    old_program = original["root"].rsplit("/versions/", 1)[0]

    def rebase(value):
        if isinstance(value, dict):
            return {key: rebase(item) for key, item in value.items()}
        return value.replace(old_program, str(root)) if isinstance(value, str) else value

    record = rebase(original)
    assert "identity" not in record
    folder = Path(record["root"])
    folder.mkdir(parents=True)
    write_json(folder / "version.json", record)
    manifest = Path(__file__).parent / "fixtures/release0/served/install-manifest.json"
    (folder / "manifest.json").write_bytes(manifest.read_bytes())
    (root / "current").write_text(folder.name + "\n")
    python = Path(record["python"])
    python.parent.mkdir(parents=True)
    python.touch()  # Models CLI's presence check, never executed.
    before = {p: p.read_bytes() for p in (folder / "version.json", paths.discovery_path, paths.instance_lock_path)}
    selected = current(root)
    assert selected and selected.python.is_file() and selected.release == 0 and selected.managed_by is None
    reader = Mock(side_effect=PermissionError("unknown owner"))
    monkeypatch.setattr(platform, "process_identity", reader)
    backend = object.__new__(Backend)
    assert backend.source_checkout_ownership(paths.app_support, selected) is False
    reader.assert_not_called()
    python.unlink()
    selected = current(root)
    admitted = selected if selected.python.is_file() else None
    assert admitted is None
    with pytest.raises(RuntimeError, match="could not be observed"):
        backend.source_checkout_ownership(paths.app_support, admitted)
    reader.assert_called_once_with(3602)
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("ordinary, bridge_result, accepted", [
    (True, False, True), (False, True, False), (None, True, True),
    (None, False, False), (None, None, False),
])
def test_only_positive_runtime_results_authorize(monkeypatch, ordinary, bridge_result, accepted):
    monkeypatch.setattr(platform, "process_birth_matches", lambda *_: ordinary)
    bridge = Mock(return_value=bridge_result)
    monkeypatch.setattr(platform, "legacy_linux_birth_matches_live_owner", bridge)
    assert birth_matches_live_owner("proc-start-ticks:12436", {"pid": 3602, "birth_id": "scoped"},
                                    pid=3602) is accepted
    if ordinary is None:
        bridge.assert_called_once_with("proc-start-ticks:12436", "scoped", pid=3602)
    else:
        bridge.assert_not_called()


def test_runtime_without_bridge_refuses_legacy(monkeypatch):
    monkeypatch.delattr(platform, "legacy_linux_birth_matches_live_owner")
    assert not birth_matches_live_owner("proc-start-ticks:12436",
                                        {"pid": 3602, "birth_id": "proc-start-ticks:12436"}, pid=3602)


@LINUX
def test_actual_bridge_with_live_native_identity(tmp_path, monkeypatch):
    # stdin closure exits the owned child normally; no test signal is needed.
    with subprocess.Popen([sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read()"],
                          stdin=subprocess.PIPE) as child:
        try:
            observed = platform.process_identity(child.pid)
            assert observed and observed["uid"] == platform.current_user()
            legacy = observed["birth_id"].split(";", 1)[0]  # Exact R0 writer encoding.
            assert platform.process_birth_matches(legacy, observed["birth_id"]) is None
            assert birth_matches_live_owner(legacy, observed, pid=child.pid)
            assert not birth_matches_live_owner(legacy, observed, pid=child.pid + 1)
            assert not birth_matches_live_owner(legacy, observed, pid=True)
            assert not birth_matches_live_owner("proc-start-ticks:0", observed, pid=child.pid)
            assert not birth_matches_live_owner(legacy, {**observed, "birth_id": legacy}, pid=child.pid)
            assert not birth_matches_live_owner(legacy, {**observed, "birth_id": observed["birth_id"] + ";bad:scope"}, pid=child.pid)
            assert not birth_matches_live_owner(legacy, {**observed, "birth_id": observed["birth_id"].replace(";boot:", ";boot:bad-")}, pid=child.pid)
            assert not birth_matches_live_owner(legacy, {**observed, "birth_id": observed["birth_id"].split(";pidns:")[0] + ";pidns:0"}, pid=child.pid)
            # Rebind only the captured PID/birth to this disposable process.
            discovery = {**_capture("r0-selection.json")["discovery"],
                         "pid": child.pid, "process_birth_id": legacy}
            assert Backend._discovery_owner_state(discovery) == "live"
            manager, expected = _manager(tmp_path, monkeypatch, discovery)

            def finish(identity, signum):
                assert identity == observed and signum == platform.TERM
                child.stdin.close()
                child.wait(timeout=5)

            signal = Mock(side_effect=finish)
            monkeypatch.setattr(platform, "signal_process", signal)
            manager.stop_foreground(expected=expected)
            signal.assert_called_once_with(observed, platform.TERM)
        finally:
            child.stdin.close()
            child.wait(timeout=5)


@LINUX
def test_captured_owner_uses_real_runtime_bridge_in_discovery_and_stop(tmp_path, monkeypatch):
    _paths, discovery, sample = _publications(tmp_path)
    _replay_proc(monkeypatch, sample)
    assert Backend._discovery_owner_state(discovery) == "live"
    manager, expected = _manager(tmp_path, monkeypatch, discovery)
    monkeypatch.setattr(platform, "current_user", lambda: sample["uid"])
    signalled = False
    observations_after_signal = []

    def observe(_pid):
        if signalled:
            observations_after_signal.append(True)
            if len(observations_after_signal) > 2:
                return None
        return _scoped(sample)

    def signal(identity, signum):
        nonlocal signalled
        assert identity == _scoped(sample) and signum == platform.TERM
        signalled = True

    monkeypatch.setattr(platform, "process_identity", observe)
    signal_spy = Mock(side_effect=signal)
    monkeypatch.setattr(platform, "signal_process", signal_spy)
    monkeypatch.setattr("astrid.installer.service.time.sleep", lambda _seconds: None)
    manager.stop_foreground(expected=expected)
    signal_spy.assert_called_once_with(_scoped(sample), platform.TERM)
    assert len(observations_after_signal) == 3  # Two live observations (including bridge), then absence.


@LINUX
@pytest.mark.parametrize("failure", ["pid", "tick", "boot", "pidns", "malformed", "unscoped",
                                     "unreadable", "scope-unreadable", "scope-malformed",
                                     "stale", "uid", "fresh-uid", "fresh-pid", "fresh-birth",
                                     "fresh-unknown", "fresh-absent", "selection"])
def test_captured_owner_refuses_before_signal(tmp_path, monkeypatch, failure):
    _paths, discovery, sample = _publications(tmp_path)
    _replay_proc(monkeypatch, sample)
    manager, expected = _manager(tmp_path, monkeypatch, discovery)
    monkeypatch.setattr(platform, "current_user", lambda: sample["uid"])
    signal = Mock()
    monkeypatch.setattr(platform, "signal_process", signal)
    original = _scoped(sample)
    changed = dict(original)
    calls = []

    def observe(_pid):
        calls.append(_pid)
        if failure == "unreadable":
            raise PermissionError("cannot observe")
        if failure == "fresh-unknown" and len(calls) == 3:
            raise PermissionError("fresh identity unavailable")
        if failure == "fresh-absent" and len(calls) == 3:
            return None
        if failure in {"stale", "fresh-uid", "fresh-pid", "fresh-birth"}:
            return original if len(calls) < (2 if failure == "stale" else 3) else changed
        if failure == "selection" and len(calls) == 3:
            write_json(manager.selection, {**manager.recorded(), "foreground": {**expected, "pid": 1}})
        return changed

    if failure in {"pid", "fresh-pid"}:
        changed["pid"] += 1
    elif failure in {"tick", "stale", "fresh-birth"}:
        changed["birth_id"] = changed["birth_id"].replace("12436", "12437")
    elif failure == "boot":
        changed["birth_id"] = changed["birth_id"].replace(sample["boot_id"], "00000000-0000-0000-0000-000000000000")
    elif failure == "pidns":
        changed["birth_id"] = changed["birth_id"].replace(str(sample["pidns_inode"]), "0")
    elif failure == "malformed":
        changed["birth_id"] += ";pidns:duplicate"
    elif failure == "unscoped":
        changed["birth_id"] = discovery["process_birth_id"]
    elif failure in {"uid", "fresh-uid"}:
        changed["uid"] += 1
    monkeypatch.setattr(platform, "process_identity", observe)
    if failure in {"scope-unreadable", "scope-malformed"}:
        read_text = Path.read_text

        def read(path, *args, **kwargs):
            if str(path) == "/proc/sys/kernel/random/boot_id":
                if failure == "scope-unreadable":
                    raise PermissionError("boot scope unavailable")
                return "malformed-boot"
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises((ValueError, OSError)):
        manager.stop_foreground(expected=expected)
    signal.assert_not_called()
    if failure not in {"uid", "selection", "stale"} and not failure.startswith("fresh-"):
        with pytest.raises(RuntimeError):
            Backend._discovery_owner_state(discovery)


@LINUX
@pytest.mark.parametrize("field", ["realm_id", "realm_root"])
def test_foreground_realm_mismatch_refuses_before_signal(tmp_path, monkeypatch, field):
    _paths, discovery, _sample = _publications(tmp_path)
    manager, expected = _manager(tmp_path, monkeypatch, discovery)
    expected[field] = "/different"
    write_json(manager.selection, {**manager.recorded(), "foreground": expected})
    signal = Mock()
    monkeypatch.setattr(platform, "signal_process", signal)
    with pytest.raises(ValueError, match="selected workspace"):
        manager.stop_foreground(expected=expected)
    signal.assert_not_called()


@LINUX
@pytest.mark.parametrize("foreground", [False, True])
@pytest.mark.parametrize("failure", [None, "mismatch", "unknown"])
def test_both_waits_require_positive_absence(tmp_path, monkeypatch, foreground, failure):
    _paths, discovery, sample = _publications(tmp_path)
    _replay_proc(monkeypatch, sample)
    manager, expected = _manager(tmp_path, monkeypatch, discovery)
    monkeypatch.setattr(platform, "current_user", lambda: sample["uid"])
    signalled = not foreground
    calls = []

    def observe(_pid):
        if not signalled:
            return _scoped(sample)
        calls.append(_pid)
        if failure == "unknown":
            raise PermissionError("unknown observation")
        if failure == "mismatch":
            return {**_scoped(sample), "birth_id": "proc-start-ticks:12437"}
        return _scoped(sample) if len(calls) <= 2 else None

    def signal(*_args):
        nonlocal signalled
        signalled = True

    monkeypatch.setattr(platform, "process_identity", observe)
    monkeypatch.setattr(platform, "signal_process", Mock(side_effect=signal))
    monkeypatch.setattr("astrid.installer.service.time.sleep", lambda _seconds: None)
    operation = lambda: manager.stop_foreground(expected=expected) if foreground else manager.stop()
    if failure:
        with pytest.raises((ValueError, OSError)):
            operation()
    else:
        operation()
        assert len(calls) == 3


@LINUX
@pytest.mark.parametrize("failure", [None, "pid", "birth", "instance", "realm", "root", "endpoint",
                                     "expected-realm", "expected-root", "os-pid", "os-birth", "os-unknown",
                                     "boundary", "probe-instance", "probe-identity", "discovery-race", "lock-race"])
def test_update_job_binds_captured_publications_before_recovery(tmp_path, monkeypatch, failure):
    from astrid_runtime.runtime_boundary import LocalRuntimeBoundary

    paths, discovery, sample = _publications(tmp_path)
    _replay_proc(monkeypatch, sample)
    identity = {"release": 0, "managed_by": None}
    realm = {"realm_id": discovery["active_realm"], "realm_root": discovery["realm_root"]}
    if failure in {"pid", "birth", "instance", "realm", "root"}:
        lock = json.loads(paths.instance_lock_path.read_bytes())
        key = {"pid": "pid", "birth": "process_birth_id", "instance": "runtime_instance_id",
               "realm": "realm_id", "root": "realm_root"}[failure]
        lock[key] = 1 if key == "pid" else "/different" if key == "realm_root" else "different"
        write_json(paths.instance_lock_path, lock)
    elif failure == "endpoint":
        write_json(paths.discovery_path, {**discovery, "endpoint": "", "door_endpoint": ""})
    elif failure in {"expected-realm", "expected-root"}:
        realm["realm_id" if failure == "expected-realm" else "realm_root"] = "/different"
    elif failure in {"os-pid", "os-birth", "os-unknown"}:
        if failure == "os-unknown":
            observer = Mock(side_effect=PermissionError("cannot observe"))
        else:
            changed = {**_scoped(sample), **({"pid": sample["pid"] + 1} if failure == "os-pid"
                                           else {"birth_id": "proc-start-ticks:12437"})}
            observer = Mock(return_value=changed)
        monkeypatch.setattr(platform, "process_identity", observer)
    before = {p: p.read_bytes() for p in (paths.discovery_path, paths.instance_lock_path)}

    def validate(_self, **kwargs):
        assert kwargs == {"endpoint": discovery["endpoint"], "pid": sample["pid"],
                          "instance_id": discovery["runtime_instance_id"], "owner_lock": paths.instance_lock_path,
                          "process_birth_id": discovery["process_birth_id"],
                          "expected_realm_id": realm["realm_id"], "expected_realm_root": Path(realm["realm_root"])}
        if failure in {"discovery-race", "lock-race"}:
            path = paths.discovery_path if failure == "discovery-race" else paths.instance_lock_path
            path.write_bytes(path.read_bytes() + b" ")
        return failure != "boundary"

    validator = Mock(side_effect=validate)
    monkeypatch.setattr(LocalRuntimeBoundary, "validate_owner", lambda self, **kwargs: validator(self, **kwargs))
    probe = Mock(return_value=("different" if failure == "probe-instance" else discovery["runtime_instance_id"],
                               {} if failure == "probe-identity" else identity))
    monkeypatch.setattr(update_job, "_probe_identity", probe)
    if failure:
        with pytest.raises((ValueError, OSError)):
            update_job._prove_recovery_owner(paths.app_support, identity, realm)
    else:
        assert update_job._prove_recovery_owner(paths.app_support, identity, realm) == "live"
        validator.assert_called_once()
        probe.assert_called_once_with(paths.app_support)
    if failure not in {"discovery-race", "lock-race"}:
        assert {p: p.read_bytes() for p in before} == before


@LINUX
def test_update_job_with_real_runtime_owner_validator(tmp_path, monkeypatch):
    from astrid_runtime.runtime_boundary import LocalRuntimeBoundary

    paths, discovery, sample = _publications(tmp_path)
    _replay_proc(monkeypatch, sample)
    identity = {"release": 0, "managed_by": None}
    health = {"runtime_instance_id": discovery["runtime_instance_id"], "identity": identity}
    # Only native historical observations and HTTP transport are replayed.
    # Runtime validate_owner, Agent probe and publication checks remain real.
    monkeypatch.setattr(LocalRuntimeBoundary, "_http_health_payload", staticmethod(lambda _endpoint: health))
    before = {p: p.read_bytes() for p in (paths.discovery_path, paths.instance_lock_path)}
    realm = {"realm_id": discovery["active_realm"], "realm_root": discovery["realm_root"]}
    assert update_job._prove_recovery_owner(paths.app_support, identity, realm) == "live"
    assert {p: p.read_bytes() for p in before} == before


@LINUX
def test_update_job_stopped_proof_requires_native_absence(tmp_path, monkeypatch):
    paths, discovery, _sample = _publications(tmp_path)
    monkeypatch.setattr(platform, "process_identity", lambda _pid: None)
    probe = Mock(side_effect=AssertionError("absent process has no live endpoint"))
    monkeypatch.setattr(update_job, "_probe_identity", probe)
    realm = {"realm_id": discovery["active_realm"], "realm_root": discovery["realm_root"]}
    assert update_job._prove_recovery_owner(paths.app_support, {"release": 0, "managed_by": None}, realm) == "stopped"
    probe.assert_not_called()
