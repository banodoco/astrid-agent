"""Managed startup is opt-in and only follows the verified stopped-server result."""
from types import SimpleNamespace

import pytest

from astrid.core.generation import engine_execution, engine_local_endpoint, engine_local_server, engine_runner, preflight
from astrid.core.generation.engine_contracts import EndpointHealthError, EndpointOwnershipError


@pytest.mark.parametrize("status,step,code,starts", [
    ("failed", "start", "endpoint_unhealthy", 1),
    ("failed", "start", "endpoint_ownership", 0),
    ("failed", "environment:comfy", "integrity_error", 0),
    ("failed", "receipt-evidence", "integrity_error", 0),
    ("failed", "health", "endpoint_unhealthy", 0),
    ("installed", None, None, 0),
])
def test_start_only_after_verified_absent_server(monkeypatch, status, step, code, starts):
    seen = []
    monkeypatch.setattr(engine_execution, "create_local_backend", lambda **kw: (object(), object()))
    monkeypatch.setattr(engine_runner, "engine_status", lambda *a, **kw: SimpleNamespace(
        status=status, failed_step_id=step, error_code=code))
    monkeypatch.setattr(engine_local_server, "ensure_local_engine_server", lambda *a, **kw: seen.append(kw))
    engine_local_server.start_verified_stopped_engine("comfyui", root="/installed")
    assert len(seen) == starts


def test_managed_selection_starts_only_when_opted_in_and_revalidates(monkeypatch):
    state = {"running": False, "starts": 0, "checks": 0}
    monkeypatch.setattr(engine_local_endpoint, "recorded_engine_root", lambda engine: "/installed")
    monkeypatch.setattr(engine_execution, "create_local_backend", lambda **kw: (object(), object()))
    def status(*args, **kwargs):
        return SimpleNamespace(status="installed" if state["running"] else "failed",
            failed_step_id=None if state["running"] else "start",
            error_code=None if state["running"] else EndpointHealthError.code)
    monkeypatch.setattr(engine_runner, "engine_status", status)
    def start(*args, **kwargs):
        state["starts"] += 1
        state["running"] = True
    monkeypatch.setattr(engine_local_server, "ensure_local_engine_server", start)
    def endpoint(*args, **kwargs):
        state["checks"] += 1
        if not state["running"]:
            raise EndpointHealthError("server absent")
        return SimpleNamespace(endpoint=SimpleNamespace(url="http://127.0.0.1:8188",
            health=SimpleNamespace(object_info_sha256="a" * 64, checked_at="2026-11-01T00:00:00Z")))
    monkeypatch.setattr(engine_local_endpoint, "local_engine_endpoint", endpoint)
    with pytest.raises(preflight.GenerationReadinessError):
        preflight.select_generation_target("managed")
    assert state == {"running": False, "starts": 0, "checks": 1}
    assert preflight.select_generation_target("managed", start_managed=True).target.kind == "managed"
    assert preflight.select_generation_target("managed", start_managed=True).target.kind == "managed"
    assert state == {"running": True, "starts": 1, "checks": 3}


def test_unowned_service_is_never_started_or_adopted(monkeypatch):
    monkeypatch.setattr(engine_local_endpoint, "recorded_engine_root", lambda engine: "/installed")
    monkeypatch.setattr(engine_execution, "create_local_backend", lambda **kw: (object(), object()))
    monkeypatch.setattr(engine_runner, "engine_status", lambda *a, **kw: SimpleNamespace(
        status="failed", failed_step_id="start", error_code=EndpointOwnershipError.code))
    monkeypatch.setattr(engine_local_server, "ensure_local_engine_server", lambda *a, **kw: pytest.fail("started"))
    monkeypatch.setattr(engine_local_endpoint, "local_engine_endpoint", lambda *a, **kw: (
        _ for _ in ()).throw(EndpointOwnershipError("unowned")))
    with pytest.raises(preflight.GenerationReadinessError) as error:
        preflight.select_generation_target("managed", start_managed=True)
    assert error.value.to_envelope()["issues"][0]["details"]["engine_code"] == "endpoint_ownership"


def test_attached_target_does_not_start_managed_engine(monkeypatch):
    from astrid.core.generation.backends.comfy_target import ComfyTargetClient
    monkeypatch.setattr(engine_local_server, "start_verified_stopped_engine",
                        lambda *a, **kw: pytest.fail("started managed engine"))
    monkeypatch.setattr(ComfyTargetClient, "request_json",
                        lambda *a, **kw: {"system": {"comfyui_version": "0.43.2"}})
    assert preflight.select_generation_target("http://127.0.0.1:8188", start_managed=True).target.kind == "attached"
