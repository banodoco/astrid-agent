"""Managed-runtime readiness: a registered managed engine is ready without an Agent-env client."""

from __future__ import annotations

from types import SimpleNamespace

from astrid.core.generation import preflight
from astrid.core.generation.engine_local_endpoint import DEFAULT_PLATFORM


def _entry():
    backend = SimpleNamespace(template="image-template")
    mode = SimpleNamespace(backends={"local": backend})
    return SimpleNamespace(id="model-1", modes={"image": mode})


def test_registered_managed_engine_is_ready_without_client(monkeypatch):
    import astrid.core.generation.engine_local_endpoint as endpoint_module

    monkeypatch.setattr(endpoint_module, "recorded_engine_root", lambda engine: "/opt/astrid/engines/comfyui")
    result = preflight.check_local_generation_readiness(
        _entry(), "image", require_managed_runtime=True, legacy_local=False,
    )
    assert result.ready is True


def test_registered_engine_endpoint_is_loopback_from_the_recipe():
    import astrid.core.generation.engine_local_endpoint as endpoint_module

    original = endpoint_module.recorded_engine_root
    endpoint_module.recorded_engine_root = lambda engine: "/opt/astrid/engines/comfyui"
    try:
        registered = preflight._registered_managed_engine()
    finally:
        endpoint_module.recorded_engine_root = original
    assert registered is not None
    assert registered["owner"] == "astrid"
    assert registered["endpoint"].startswith("http://127.0.0.1:")


def test_no_registered_managed_engine_is_not_ready(monkeypatch):
    import astrid.core.generation.engine_local_endpoint as endpoint_module

    monkeypatch.setattr(endpoint_module, "recorded_engine_root", lambda engine: None)
    result = preflight.check_local_generation_readiness(
        _entry(), "image", require_managed_runtime=True, legacy_local=False,
    )
    assert result.ready is False
    assert "no managed engine is installed or registered" in result.reason


def test_client_present_skips_the_managed_runtime_requirement(monkeypatch):
    import astrid.core.generation.engine_local_endpoint as endpoint_module

    monkeypatch.setattr(endpoint_module, "recorded_engine_root", lambda engine: None)
    result = preflight.check_local_generation_readiness(
        _entry(), "image", require_managed_runtime=False, legacy_local=False,
    )
    assert result.ready is True
