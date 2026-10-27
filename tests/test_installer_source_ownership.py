from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from astrid.installer.backend import Backend
from astrid.installer.core import Version, write_json


def _git_checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "tracked.txt").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True)
    return path


def _backend(tmp_path: Path) -> Backend:
    return Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv")


def _profile(data: Path, runtime: Path, agent: Path) -> Path:
    path = data / "runtime" / "source-profiles" / "astrid.json"
    write_json(path, {"profile": "astrid", "runtime_checkout": str(runtime), "source_checkout": str(agent)})
    return path


def test_stopped_legacy_source_profile_refuses_clean_and_dirty_checkouts(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_INSTALL_RELEASE", "15")
    monkeypatch.setenv("ASTRID_INSTALL_MANAGED_BY", "")
    runtime = _git_checkout(tmp_path / "runtime")
    agent = _git_checkout(tmp_path / "agent")
    data = tmp_path / "data"
    _profile(data, runtime, agent)

    backend = _backend(tmp_path)
    assert backend.source_checkout_ownership(data)
    (agent / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    assert backend.source_checkout_ownership(data)


def test_live_b1_source_identity_refuses_after_process_and_instance_validation(tmp_path, monkeypatch):
    from runtime_protocol import platform
    from astrid_runtime.paths import RuntimePaths

    data = tmp_path / "data"
    paths = RuntimePaths.current(data_root=data)
    write_json(paths.discovery_path, {
        "pid": 4242, "process_birth_id": "birth", "runtime_instance_id": "instance",
        "endpoint": "http://127.0.0.1:12345",
    })
    monkeypatch.setattr(platform, "process_identity", lambda pid: {"pid": pid, "birth_id": "birth"})
    identity = {"release": 0, "managed_by": "source", "runtime_head": "a" * 40,
                "agent_head": "b" * 40, "dirty": True, "dirty_hash": "c" * 64}
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary._http_health_payload",
                        staticmethod(lambda *_args, **_kwargs: {"runtime_instance_id": "instance", "identity": identity}))
    assert _backend(tmp_path).source_checkout_ownership(data)


@pytest.mark.parametrize("reason", ["malformed", "stale"])
def test_unverifiable_discovery_fails_closed(tmp_path, reason):
    from astrid_runtime.paths import RuntimePaths

    data = tmp_path / "data"
    path = RuntimePaths.current(data_root=data).discovery_path
    path.parent.mkdir(parents=True)
    if reason == "malformed":
        path.write_text("{", encoding="utf-8")
    else:
        write_json(path, {"pid": 4242, "process_birth_id": "birth", "runtime_instance_id": "instance"})
    expected = ("Runtime discovery owner identity" if reason == "malformed"
                else "Stale Runtime discovery has no retained configuration")
    with pytest.raises(RuntimeError, match=expected):
        _backend(tmp_path).source_checkout_ownership(data)


@pytest.mark.parametrize("managed_by", [None, "pinokio"])
def test_managed_version_archive_profile_is_not_a_source_checkout(tmp_path, managed_by):
    data = tmp_path / "data"
    # A managed installation's source profile is retained for Runtime startup,
    # but its extracted release trees are not Git worktrees.
    _profile(data, tmp_path / "program" / "versions" / "v1" / "r",
             tmp_path / "program" / "versions" / "v1" / "a")
    installed = Version(tmp_path / "program" / "versions" / "v1", tmp_path / "py",
                        tmp_path / "program" / "versions" / "v1" / "profile.json", {}, 1, managed_by)
    assert not _backend(tmp_path).source_checkout_ownership(data, installed)


def test_fresh_workspace_and_misleading_environment_are_not_source_ownership(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_INSTALL_RELEASE", "0")
    monkeypatch.setenv("ASTRID_INSTALL_MANAGED_BY", "source")
    assert not _backend(tmp_path).source_checkout_ownership(tmp_path / "empty")


def test_malformed_retained_source_profile_fails_with_its_real_error(tmp_path):
    from astrid_runtime.paths import RuntimePaths

    data = tmp_path / "data"
    path = RuntimePaths.current(data_root=data).source_profiles_dir / "astrid.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"profile": "wrong"}), encoding="utf-8")
    with pytest.raises(ValueError, match="Retained Runtime source configuration cannot be verified: Source profile must be"):
        _backend(tmp_path).source_checkout_ownership(data)


def test_source_profile_with_non_git_paths_fails_closed(tmp_path):
    data = tmp_path / "data"
    _profile(data, tmp_path / "archive-runtime", tmp_path / "archive-agent")
    with pytest.raises(ValueError, match="Retained Runtime source configuration cannot be verified"):
        _backend(tmp_path).source_checkout_ownership(data)


def test_live_non_source_identity_conflicting_with_source_profile_fails_closed(tmp_path, monkeypatch):
    from runtime_protocol import platform
    from astrid_runtime.paths import RuntimePaths

    data = tmp_path / "data"
    runtime, agent = _git_checkout(tmp_path / "runtime"), _git_checkout(tmp_path / "agent")
    _profile(data, runtime, agent)
    path = RuntimePaths.current(data_root=data).discovery_path
    write_json(path, {"pid": 4242, "process_birth_id": "birth", "runtime_instance_id": "instance",
                      "endpoint": "http://127.0.0.1:12345"})
    monkeypatch.setattr(platform, "process_identity", lambda pid: {"pid": pid, "birth_id": "birth"})
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary._http_health_payload",
                        staticmethod(lambda *_args, **_kwargs: {
                            "runtime_instance_id": "instance", "identity": {"release": 2, "managed_by": None}}))
    with pytest.raises(RuntimeError, match="conflicts with retained source checkout configuration"):
        _backend(tmp_path).source_checkout_ownership(data)


def test_live_identity_must_match_discovery_instance(tmp_path, monkeypatch):
    from runtime_protocol import platform
    from astrid_runtime.paths import RuntimePaths

    data = tmp_path / "data"
    path = RuntimePaths.current(data_root=data).discovery_path
    write_json(path, {"pid": 4242, "process_birth_id": "birth", "runtime_instance_id": "instance",
                      "endpoint": "http://127.0.0.1:12345"})
    monkeypatch.setattr(platform, "process_identity", lambda pid: {"pid": pid, "birth_id": "birth"})
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary._http_health_payload",
                        staticmethod(lambda *_args, **_kwargs: {
                            "runtime_instance_id": "other", "identity": {"release": 2, "managed_by": None}}))
    with pytest.raises(RuntimeError, match="Live Runtime startup identity cannot be verified"):
        _backend(tmp_path).source_checkout_ownership(data)


def test_live_source_identity_conflicts_with_selected_managed_version(tmp_path, monkeypatch):
    from runtime_protocol import platform
    from astrid_runtime.paths import RuntimePaths

    data = tmp_path / "data"
    path = RuntimePaths.current(data_root=data).discovery_path
    write_json(path, {"pid": 4242, "process_birth_id": "birth", "runtime_instance_id": "instance",
                      "endpoint": "http://127.0.0.1:12345"})
    monkeypatch.setattr(platform, "process_identity", lambda pid: {"pid": pid, "birth_id": "birth"})
    identity = {"release": 0, "managed_by": "source", "runtime_head": "a" * 40,
                "agent_head": "b" * 40, "dirty": False, "dirty_hash": "c" * 64}
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary._http_health_payload",
                        staticmethod(lambda *_args, **_kwargs: {
                            "runtime_instance_id": "instance", "identity": identity}))
    installed = Version(tmp_path / "program/versions/v1", tmp_path / "py",
                        tmp_path / "profile", {}, 1, None)
    with pytest.raises(RuntimeError, match="conflicts with the selected managed version"):
        _backend(tmp_path).source_checkout_ownership(data, installed)
