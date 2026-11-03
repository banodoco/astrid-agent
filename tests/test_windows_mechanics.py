from __future__ import annotations

import errno
import socket
from pathlib import Path
from unittest.mock import Mock

import pytest

from astrid.core.execution import host_log, network_policy
from astrid.core.foundation.paths import venv_python
from astrid.skills.harnesses import base as skill_base


def test_venv_python_uses_platform_layout():
    assert venv_python(Path("env"), windows=True) == Path("env/Scripts/python.exe")
    assert venv_python(Path("env"), windows=False) == Path("env/bin/python")


def test_runtime_cli_accepts_structured_argv_and_windows_executable_paths():
    from astrid.runtime_cli import RUNTIME_COMMAND_ENV, _runtime_command

    assert _runtime_command({RUNTIME_COMMAND_ENV: r'["C:\\Program Files\\Astrid\\astrid-runtime.exe", "up"]'}, windows=True) == (
        "C:\\Program Files\\Astrid\\astrid-runtime.exe", "up"
    )
    assert _runtime_command({RUNTIME_COMMAND_ENV: "C:\\Program Files\\Astrid\\astrid-runtime.exe"}, windows=True) == (
        "C:\\Program Files\\Astrid\\astrid-runtime.exe",
    )


def test_skill_install_falls_back_to_a_managed_copy_and_removes_it(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("skill", encoding="utf-8")
    target = tmp_path / "skills" / "astrid-demo"

    def denied_symlink(_self, _target, **_kwargs):
        raise OSError(errno.EPERM, "symlinks require elevation")

    monkeypatch.setattr(Path, "symlink_to", denied_symlink)
    assert skill_base.ensure_symlink(target, source)
    assert not target.is_symlink()
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "skill"
    assert skill_base.managed_target_matches(target, source)
    assert skill_base.target_mechanism(target) == "copy"
    assert not skill_base.ensure_symlink(target, source)
    (source / "SKILL.md").write_text("updated", encoding="utf-8")
    assert skill_base.ensure_symlink(target, source)
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "updated"
    assert skill_base.remove_symlink(target)
    assert not target.exists()


def test_skill_cleanup_still_removes_legacy_symlinks(tmp_path):
    source = tmp_path / "declared-skill"
    source.mkdir()
    target = tmp_path / "skills" / "astrid-demo"
    target.parent.mkdir()
    target.symlink_to(source)
    assert skill_base.remove_symlink(target, sources=[source])
    assert not target.is_symlink()
    target.symlink_to(tmp_path / "foreign/astrid/demo")
    from astrid.core.contracts.errors import AstridError
    with pytest.raises(AstridError, match="preserved"):
        skill_base.remove_symlink(target, sources=[source])
    assert target.is_symlink()


def test_windows_sharing_violation_retries_replace(monkeypatch, tmp_path):
    monkeypatch.setattr(host_log.sys, "platform", "win32")
    monkeypatch.setattr(host_log.time, "sleep", lambda _duration: None)
    attempts = 0

    def replace(_source, _destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = OSError("sharing violation")
            error.winerror = 32
            raise error

    monkeypatch.setattr(host_log.os, "replace", replace)
    host_log._replace(tmp_path / "old", tmp_path / "new")
    assert attempts == 3


def test_provider_network_guard_allows_declared_host_and_denies_other_hosts(monkeypatch):
    monkeypatch.setattr(network_policy, "_POLICY", {
        "allowed_protocols": ["tcp"],
        "allowed_destinations": ["provider.example:443"],
    })
    monkeypatch.setattr(network_policy, "_DNS_NAMES", {})
    monkeypatch.setattr(network_policy, "_EVENTS", [])

    assert network_policy._check("tcp", ("provider.example", 443)) == ("provider.example", 443)
    with pytest.raises(network_policy.NetworkPolicyError, match="denied tcp destination"):
        network_policy._check("tcp", ("outside.example", 443))
    assert [event["allowed"] for event in network_policy._EVENTS] == [True, False]


def test_provider_network_hook_blocks_connect_before_socket_call(monkeypatch):
    monkeypatch.setattr(network_policy, "_POLICY", {
        "allowed_protocols": ["tcp"],
        "allowed_destinations": ["provider.example:443"],
    })
    monkeypatch.setattr(network_policy, "_DNS_NAMES", {})
    monkeypatch.setattr(network_policy, "_EVENTS", [])
    connect = Mock()
    monkeypatch.setattr(socket.socket, "connect", connect)
    originals = {
        (socket, "getaddrinfo"): lambda *args, **kwargs: [],
        (socket.socket, "connect"): connect,
        (socket.socket, "connect_ex"): Mock(),
        (socket.socket, "sendto"): Mock(),
    }
    network_policy._patch_socket(originals)
    stream = type("Stream", (), {"type": socket.SOCK_STREAM})()
    socket.socket.connect(stream, ("provider.example", 443))
    with pytest.raises(network_policy.NetworkPolicyError):
        socket.socket.connect(stream, ("outside.example", 443))
    connect.assert_called_once_with(stream, ("provider.example", 443))


def test_non_macos_provider_guard_does_not_add_os_sandbox(monkeypatch, tmp_path):
    from astrid.core.execution import generic_host

    monkeypatch.setattr(generic_host.sys, "platform", "linux")
    argv = ["python", "-m", "astrid.core.execution.generic_host_worker"]
    assert generic_host._network_sandbox_argv(argv, tmp_path, "http://127.0.0.1:4321") == argv
