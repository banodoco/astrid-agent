"""Focused controls for the disposable full-view keep-going driver."""
import pytest

from scripts import ci_journey_keepgoing as keepgoing


def test_release0_owner_captures_service_health_and_process_identity(monkeypatch, tmp_path):
    calls = []
    health = {"pid": 431, "runtime_instance_id": "r0-instance"}
    monkeypatch.setattr(keepgoing.journey, "assert_direct_service_ownership",
                        lambda root, data: calls.append(("service", root, data)) or {"manager": "test"})
    monkeypatch.setattr(keepgoing.journey, "healthy", lambda data: calls.append(("health", data)) or health)
    monkeypatch.setattr(keepgoing.journey, "process_birth", lambda pid: calls.append(("birth", pid)) or "birth-1")

    owner = keepgoing.release0_owner(tmp_path / "root", tmp_path / "data")

    assert owner == {"service": {"manager": "test"}, "health": health, "pid": 431, "birth": "birth-1"}
    assert [call[0] for call in calls] == ["service", "health", "birth"]


def test_release0_owner_requires_a_process_birth_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(keepgoing.journey, "assert_direct_service_ownership", lambda *_: {})
    monkeypatch.setattr(keepgoing.journey, "healthy", lambda _: {"pid": 431, "runtime_instance_id": "r0"})
    monkeypatch.setattr(keepgoing.journey, "process_birth", lambda _: None)

    with pytest.raises(RuntimeError, match="Cannot establish the release-0 Runtime process identity"):
        keepgoing.release0_owner(tmp_path / "root", tmp_path / "data")


def test_update_preflight_requires_same_live_service_owner(monkeypatch, tmp_path):
    calls = []
    owner = {"health": {"pid": 431, "runtime_instance_id": "r0-instance"},
             "pid": 431, "birth": "birth-1"}
    monkeypatch.setattr(keepgoing.journey, "assert_direct_service_ownership",
                        lambda *_: calls.append("service") or {})
    monkeypatch.setattr(keepgoing.journey, "healthy",
                        lambda _: calls.append("health") or {"pid": 431, "runtime_instance_id": "r0-instance"})
    monkeypatch.setattr(keepgoing.journey, "process_birth", lambda pid: calls.append(("birth", pid)) or "birth-1")

    assert keepgoing.assert_release0_owner(tmp_path / "root", tmp_path / "data", owner) == owner["health"]
    assert calls == ["service", "health", ("birth", 431)]


@pytest.mark.parametrize("health,birth", [
    ({"pid": 431, "runtime_instance_id": "replacement-instance"}, "birth-1"),
    ({"pid": 432, "runtime_instance_id": "r0-instance"}, "birth-1"),
    ({"pid": 431, "runtime_instance_id": "r0-instance"}, "replacement-birth"),
])
def test_update_preflight_rejects_changed_runtime_owner(monkeypatch, tmp_path, health, birth):
    owner = {"health": {"pid": 431, "runtime_instance_id": "r0-instance"},
             "pid": 431, "birth": "birth-1"}
    monkeypatch.setattr(keepgoing.journey, "assert_direct_service_ownership", lambda *_: {})
    monkeypatch.setattr(keepgoing.journey, "healthy", lambda _: health)
    monkeypatch.setattr(keepgoing.journey, "process_birth", lambda _: birth)

    with pytest.raises(RuntimeError, match="owner changed before the direct release-1 update"):
        keepgoing.assert_release0_owner(tmp_path / "root", tmp_path / "data", owner)
