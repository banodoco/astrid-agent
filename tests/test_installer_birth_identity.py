"""Focused Release-0 to Release-1 birth-ID compatibility controls."""
from __future__ import annotations

import os
import sys
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


def test_exact_birth_identity_remains_valid_without_runtime_bridge_call(monkeypatch):
    bridge = Mock(side_effect=AssertionError("exact identity must not use the legacy bridge"))
    monkeypatch.setattr(platform, "legacy_linux_birth_matches_live_owner", bridge)
    assert birth_identity.birth_ids_match("preserved-exact-id", "preserved-exact-id", PID, PID)
    assert not birth_identity.birth_ids_match("", "", PID, PID)
    assert not birth_identity.birth_ids_match(None, "preserved-exact-id", PID, PID)
    bridge.assert_not_called()


def test_legacy_linux_bridge_delegates_selected_owner_to_runtime(monkeypatch):
    bridge = Mock(return_value=True)
    monkeypatch.setattr(platform, "legacy_linux_birth_matches_live_owner", bridge)

    assert birth_identity.birth_ids_match(LEGACY, SCOPED, PID, PID)
    bridge.assert_called_once_with(LEGACY, SCOPED, pid=PID)


@pytest.mark.skipif(sys.platform != "linux", reason="the scoped Release-1 compatibility form is Linux-only")
def test_legacy_linux_bridge_reobserves_current_owner_through_pinned_runtime():
    pid = os.getpid()
    identity = platform.process_identity(pid)
    assert identity is not None
    observed = identity["birth_id"]
    legacy = observed.split(";", 1)[0]

    assert birth_identity.birth_ids_match(legacy, observed, pid, pid)


@pytest.mark.parametrize("result", [False, None])
def test_legacy_linux_bridge_preserves_runtime_boot_namespace_and_pid_reuse_refusals(monkeypatch, result):
    bridge = Mock(return_value=result)
    monkeypatch.setattr(platform, "legacy_linux_birth_matches_live_owner", bridge)

    assert not birth_identity.birth_ids_match(LEGACY, SCOPED, PID, PID)
    bridge.assert_called_once_with(LEGACY, SCOPED, pid=PID)


@pytest.mark.parametrize("failure", [AttributeError("unavailable API"), OSError("unavailable identity")])
def test_legacy_linux_bridge_fails_closed_when_runtime_api_or_identity_is_unavailable(monkeypatch, failure):
    bridge = Mock(side_effect=failure)
    monkeypatch.setattr(platform, "legacy_linux_birth_matches_live_owner", bridge)

    assert not birth_identity.birth_ids_match(LEGACY, SCOPED, PID, PID)
    bridge.assert_called_once_with(LEGACY, SCOPED, pid=PID)


@pytest.mark.parametrize(("recorded", "observed", "recorded_pid", "observed_pid"), [
    (LEGACY, SCOPED, PID, PID + 1),
    ("", SCOPED, PID, PID),
    (LEGACY, "", PID, PID),
    (LEGACY, SCOPED, True, PID),
])
def test_legacy_linux_bridge_refuses_invalid_owner_coordinates_without_runtime_call(
        monkeypatch, recorded, observed, recorded_pid, observed_pid):
    bridge = Mock(side_effect=AssertionError("invalid coordinates must not reach Runtime"))
    monkeypatch.setattr(platform, "legacy_linux_birth_matches_live_owner", bridge)

    assert not birth_identity.birth_ids_match(recorded, observed, recorded_pid, observed_pid)
    bridge.assert_not_called()


def test_backend_admits_only_runtime_proven_scoped_r0_owner_without_discovery_mutation(monkeypatch):
    discovery = {"pid": PID, "process_birth_id": LEGACY}
    monkeypatch.setattr(
        platform, "legacy_linux_birth_matches_live_owner",
        lambda _recorded, observed, **_kwargs: observed == SCOPED,
    )
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


def test_service_foreground_waits_for_runtime_proven_scoped_successor_before_absence(tmp_path, monkeypatch):
    service = _foreground_service(tmp_path, monkeypatch)
    monkeypatch.setattr(platform, "legacy_linux_birth_matches_live_owner", lambda *_args, **_kwargs: True)
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
        tmp_path, monkeypatch, observed):
    service = _foreground_service(tmp_path, monkeypatch)
    monkeypatch.setattr(platform, "legacy_linux_birth_matches_live_owner", lambda *_args, **_kwargs: False)
    before = service.selection.read_bytes()
    monkeypatch.setattr(platform, "process_identity", lambda pid: observed)
    monkeypatch.setattr(platform, "current_user", lambda: UID)
    signal = Mock()
    monkeypatch.setattr(platform, "signal_process", signal)

    with pytest.raises(ValueError, match="identity changed"):
        service.stop_foreground(expected=service.recorded()["foreground"])

    signal.assert_not_called()
    assert service.selection.read_bytes() == before


def test_service_fallback_stop_loop_keeps_waiting_for_runtime_proven_scoped_successor(tmp_path, monkeypatch):
    from astrid.installer.service import Service

    service = _foreground_service(tmp_path, monkeypatch)
    service.system = "darwin"
    monkeypatch.setattr(platform, "legacy_linux_birth_matches_live_owner", lambda *_args, **_kwargs: True)
    observations = [{"pid": PID, "uid": UID, "birth_id": SCOPED}, None]
    monkeypatch.setattr(platform, "process_identity", lambda pid: observations.pop(0))
    monkeypatch.setattr("astrid.installer.service.time.sleep", lambda _: None)

    Service.stop(service)

    assert observations == []


def test_custody_and_generated_launcher_keep_pid_uid_gates_around_birth_bridge(monkeypatch):
    from astrid.installer.custody import _same_process_identity
    from astrid.installer.launcher import _custody_identity_matches

    recorded = {"pid": PID, "uid": UID, "birth_id": LEGACY}
    observed = {"pid": PID, "uid": UID, "birth_id": SCOPED}
    monkeypatch.setattr(
        platform, "legacy_linux_birth_matches_live_owner",
        lambda _recorded, candidate, **_kwargs: candidate == SCOPED,
    )
    for matches in (_same_process_identity, _custody_identity_matches):
        assert matches(recorded, observed)
        assert not matches(recorded, {**observed, "pid": PID + 1})
        assert not matches(recorded, {**observed, "uid": UID + 1})
        assert not matches(recorded, {**observed, "birth_id": f"{LEGACY};boot:{BOOT};pidns:4026531837"})


def test_generated_launcher_embeds_the_same_fail_closed_birth_helper(tmp_path):
    from astrid.installer.launcher import publish_runtime_runner

    source = publish_runtime_runner(tmp_path).read_text(encoding="utf-8")
    assert "def birth_ids_match" in source
    assert "legacy_linux_birth_matches_live_owner" in source
    assert "birth_ids_match(expected.get(\"birth_id\"), observed.get(\"birth_id\")," in source
