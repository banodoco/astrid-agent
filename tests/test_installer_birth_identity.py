"""Focused Release-0 to Release-1 birth-ID compatibility controls."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from runtime_protocol import platform

from astrid.installer import birth_identity
from astrid.installer.backend import Backend
from astrid.installer.core import write_json


PID = 4242
UID = 501
TICKS = "123456"
BOOT = "12345678-1234-4234-8234-123456789abc"
NAMESPACE = "4026531836"
LEGACY = f"proc-start-ticks:{TICKS}"
SCOPED = f"{LEGACY};boot:{BOOT};pidns:{NAMESPACE}"


@pytest.fixture
def linux_scope(monkeypatch):
    monkeypatch.setattr(birth_identity, "_current_linux_scope", lambda pid: (BOOT, NAMESPACE))


@pytest.mark.parametrize("system", ["linux", "darwin", "win32"])
def test_exact_birth_identity_remains_valid_on_every_supported_platform(monkeypatch, system):
    monkeypatch.setattr(sys, "platform", system)
    assert birth_identity.birth_ids_match("preserved-exact-id", "preserved-exact-id", PID, PID)
    assert not birth_identity.birth_ids_match("", "", PID, PID)
    assert not birth_identity.birth_ids_match(None, "preserved-exact-id", PID, PID)


@pytest.mark.parametrize(("recorded", "observed", "recorded_pid", "observed_pid", "scope", "expected"), [
    (LEGACY, SCOPED, PID, PID, (BOOT, NAMESPACE), True),
    (LEGACY, SCOPED.replace(TICKS, "123457"), PID, PID, (BOOT, NAMESPACE), False),
    (LEGACY, SCOPED.replace(BOOT, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), PID, PID, (BOOT, NAMESPACE), False),
    (LEGACY, SCOPED.replace(NAMESPACE, "4026531837"), PID, PID, (BOOT, NAMESPACE), False),
    (LEGACY, SCOPED, PID, PID + 1, (BOOT, NAMESPACE), False),
    (LEGACY, SCOPED, PID, PID, None, False),
    ("proc-start-ticks:0", SCOPED, PID, PID, (BOOT, NAMESPACE), False),
    ("proc-start-ticks:not-decimal", SCOPED, PID, PID, (BOOT, NAMESPACE), False),
    (LEGACY, f"{LEGACY};boot:{BOOT.upper()};pidns:{NAMESPACE}", PID, PID, (BOOT, NAMESPACE), False),
    (LEGACY, f"{LEGACY};pidns:{NAMESPACE};boot:{BOOT}", PID, PID, (BOOT, NAMESPACE), False),
    (LEGACY, f"{LEGACY};boot:{BOOT}", PID, PID, (BOOT, NAMESPACE), False),
    (LEGACY, f"{LEGACY};boot:{BOOT};pidns:0", PID, PID, (BOOT, NAMESPACE), False),
    (LEGACY, f"{SCOPED};extra:foreign", PID, PID, (BOOT, NAMESPACE), False),
    (SCOPED, f"{LEGACY};boot:{BOOT};pidns:4026531837", PID, PID, (BOOT, NAMESPACE), False),
    ("darwin-birth", SCOPED, PID, PID, (BOOT, NAMESPACE), False),
    (LEGACY, "windows-creation:99", PID, PID, (BOOT, NAMESPACE), False),
    (LEGACY, None, PID, PID, (BOOT, NAMESPACE), False),
])
def test_legacy_linux_bridge_requires_every_reviewed_scope_fact(
        monkeypatch, recorded, observed, recorded_pid, observed_pid, scope, expected):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(birth_identity, "_current_linux_scope", lambda checked_pid: scope)
    assert birth_identity.birth_ids_match(recorded, observed, recorded_pid, observed_pid) is expected


@pytest.mark.parametrize("system", ["darwin", "win32"])
def test_legacy_linux_shape_never_bridges_on_other_supported_platforms(monkeypatch, system, linux_scope):
    monkeypatch.setattr(sys, "platform", system)
    assert not birth_identity.birth_ids_match(LEGACY, SCOPED, PID, PID)


@pytest.mark.skipif(sys.platform != "linux", reason="the scoped Release-1 compatibility form is Linux-only")
def test_live_linux_scope_bridge_requires_current_host_facts():
    scope = birth_identity._current_linux_scope(os.getpid())
    assert scope is not None
    fields = (Path("/proc") / str(os.getpid()) / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    legacy = f"proc-start-ticks:{fields[19]}"
    observed = f"{legacy};boot:{scope[0]};pidns:{scope[1]}"
    assert birth_identity.birth_ids_match(legacy, observed, os.getpid(), os.getpid())


def test_backend_admits_only_current_scoped_r0_owner_without_discovery_mutation(monkeypatch, linux_scope):
    discovery = {"pid": PID, "process_birth_id": LEGACY}
    monkeypatch.setattr(platform, "process_identity", lambda pid: {"pid": pid, "birth_id": SCOPED})
    assert Backend._discovery_owner_state(discovery) == "live"
    before = dict(discovery)
    for foreign in (
        {"pid": PID, "birth_id": f"{LEGACY};boot:{BOOT};pidns:4026531837"},
        {"pid": PID + 1, "birth_id": SCOPED},
    ):
        monkeypatch.setattr(platform, "process_identity", lambda pid: foreign)
        with pytest.raises(RuntimeError, match="owner identity changed"):
            Backend._discovery_owner_state(discovery)
    assert discovery == before


def _foreground_service(tmp_path, monkeypatch, *, birth=LEGACY):
    from astrid.installer.service import Service
    from tests.test_installer_busy_fence import realm_fixture

    backend, _old, _data, _paths, realm, _calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    backend.service.stop_foreground = lambda **kwargs: Service.stop_foreground(backend.service, **kwargs)
    record = backend.service.recorded()
    write_json(backend.service.selection, {**record, "foreground": {
        "pid": PID, "birth_id": birth, "realm_id": "selected", "realm_root": str(realm),
    }})
    return backend.service


def test_service_foreground_waits_for_valid_scoped_successor_before_absence(tmp_path, monkeypatch, linux_scope):
    service = _foreground_service(tmp_path, monkeypatch)
    observations = [{"pid": PID, "uid": UID, "birth_id": SCOPED},
                    {"pid": PID, "uid": UID, "birth_id": SCOPED}, None]
    monkeypatch.setattr(platform, "process_identity", lambda pid: observations.pop(0))
    monkeypatch.setattr(platform, "current_user", lambda: UID)
    signal = Mock()
    monkeypatch.setattr(platform, "signal_process", signal)

    service.stop_foreground(expected=service.recorded()["foreground"])

    signal.assert_called_once()
    assert observations == []  # The post-TERM comparison did not mistake the R1 spelling for replacement.


@pytest.mark.parametrize("observed", [
    {"pid": PID, "uid": UID, "birth_id": f"{LEGACY};boot:{BOOT};pidns:4026531837"},
    {"pid": PID + 1, "uid": UID, "birth_id": SCOPED},
])
def test_service_refuses_foreign_scoped_owner_before_signal_or_selection_mutation(
        tmp_path, monkeypatch, linux_scope, observed):
    service = _foreground_service(tmp_path, monkeypatch)
    before = service.selection.read_bytes()
    monkeypatch.setattr(platform, "process_identity", lambda pid: observed)
    monkeypatch.setattr(platform, "current_user", lambda: UID)
    signal = Mock()
    monkeypatch.setattr(platform, "signal_process", signal)

    with pytest.raises(ValueError, match="identity changed"):
        service.stop_foreground(expected=service.recorded()["foreground"])

    signal.assert_not_called()
    assert service.selection.read_bytes() == before


def test_service_fallback_stop_loop_keeps_waiting_for_valid_scoped_successor(tmp_path, monkeypatch, linux_scope):
    from astrid.installer.service import Service

    service = _foreground_service(tmp_path, monkeypatch)
    service.system = "darwin"
    observations = [{"pid": PID, "uid": UID, "birth_id": SCOPED}, None]
    monkeypatch.setattr(platform, "process_identity", lambda pid: observations.pop(0))
    monkeypatch.setattr("astrid.installer.service.time.sleep", lambda _: None)

    Service.stop(service)

    assert observations == []


def test_custody_and_generated_launcher_keep_pid_uid_gates_around_birth_bridge(linux_scope):
    from astrid.installer.custody import _same_process_identity
    from astrid.installer.launcher import _custody_identity_matches

    recorded = {"pid": PID, "uid": UID, "birth_id": LEGACY}
    observed = {"pid": PID, "uid": UID, "birth_id": SCOPED}
    for matches in (_same_process_identity, _custody_identity_matches):
        assert matches(recorded, observed)
        assert not matches(recorded, {**observed, "pid": PID + 1})
        assert not matches(recorded, {**observed, "uid": UID + 1})
        assert not matches(recorded, {**observed, "birth_id": f"{LEGACY};boot:{BOOT};pidns:4026531837"})


def test_generated_launcher_embeds_the_same_fail_closed_birth_helper(tmp_path):
    from astrid.installer.launcher import publish_runtime_runner

    source = publish_runtime_runner(tmp_path).read_text(encoding="utf-8")
    assert "def _current_linux_scope" in source
    assert "def birth_ids_match" in source
    assert "birth_ids_match(expected.get(\"birth_id\"), observed.get(\"birth_id\")," in source
