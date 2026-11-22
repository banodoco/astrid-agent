"""Default SDK selection materializes ready workflows only when attempted."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import entries
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.sdk.generation import GenerationFacade


class _StopAtSelectedCandidate(RuntimeError):
    pass


def _configure_command(monkeypatch, registry=None, *, patch_preferences=True, patch_registry=True):
    import astrid.sdk as sdk
    from astrid.core.generation import defaults, placement, settings
    from astrid.core.generation import availability, preflight

    if patch_registry:
        monkeypatch.setattr(sdk, "_load_model_registry", lambda **kwargs: registry)
    monkeypatch.setattr(settings, "read", lambda client=None: {"defaults": {}})
    if patch_preferences:
        monkeypatch.setattr(defaults, "ordered_ids", lambda *args, **kwargs: [])
        monkeypatch.setattr(defaults, "pinned_entry", lambda *args, **kwargs: None)
    monkeypatch.setattr(availability, "require_model_available", lambda *args, **kwargs: None)
    monkeypatch.setattr(availability, "require_entry_available", lambda *args, **kwargs: None)
    monkeypatch.setattr(preflight, "select_generation_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(preflight, "require_generation_target_readiness", lambda *args, **kwargs: None)
    local = SimpleNamespace(kind="local", machine=None, device="cpu",
                            policy=SimpleNamespace(requested_idle_timeout_s=None, gpu_types=()))
    monkeypatch.setattr(placement, "request_from_selector", lambda value: value)
    monkeypatch.setattr(placement, "resolve_placement", lambda *args, **kwargs: local)
    monkeypatch.setattr(placement, "validate_placement", lambda value: value)
    monkeypatch.setattr(placement, "comfy_selector", lambda value: "local")


def _observe_graph_loads(monkeypatch):
    from vibecomfy.registry import ready as ready_registry

    calls = []
    original = ready_registry.workflow_from_ready

    def observed(template, **kwargs):
        calls.append(template)
        return original(template, **kwargs)

    monkeypatch.setattr(ready_registry, "workflow_from_ready", observed)
    return calls


def test_catalog_first_auto_command_does_not_load_ready_workflow_graphs(monkeypatch):
    from astrid.core.generation import defaults

    registry = ModelRegistry.load_default()
    _configure_command(monkeypatch, registry)
    monkeypatch.setattr(defaults, "ordered_ids", lambda *args, **kwargs: ("z-image",))
    graph_loads = _observe_graph_loads(monkeypatch)
    selected = []

    def stop_after_catalog_selection(**kwargs):
        selected.append(kwargs["candidate"].id)
        raise _StopAtSelectedCandidate

    monkeypatch.setattr("astrid.sdk.generation._prepare_catalog_target_workflow",
                        stop_after_catalog_selection)

    with pytest.raises(_StopAtSelectedCandidate):
        GenerationFacade().image(mode="t2i", placement="local", prompt="a boat", dry_run=True)

    assert selected
    assert graph_loads == []


def test_auto_fallback_loads_only_first_two_ready_workflows(monkeypatch):
    registry = ModelRegistry([])
    expected_rows = [row for row in entries._ready_entry_rows(registry=registry)
                     if row.get("kind") == "image" and "t2i" in row.get("modes", [])]
    assert len(expected_rows) >= 3
    expected_ids = sorted(row["id"] for row in expected_rows)
    ids_by_bundle = {str(Path(row["bundle"]).resolve()): row["id"] for row in expected_rows}
    _configure_command(monkeypatch, registry)
    graph_loads = _observe_graph_loads(monkeypatch)

    @contextmanager
    def passthrough_snapshot(path):
        yield path

    monkeypatch.setattr("astrid.sdk.generation._snapshot_generation_workflow", passthrough_snapshot)
    attempted = []

    def prepare(path, **kwargs):
        identifier = ids_by_bundle[str(Path(path).resolve())]
        attempted.append(identifier)
        if len(attempted) == 1:
            raise ValueError("first ready workflow is locally incompatible")
        raise _StopAtSelectedCandidate

    monkeypatch.setattr(entries, "prepare", prepare)

    with pytest.raises(_StopAtSelectedCandidate):
        GenerationFacade().image(mode="t2i", placement="local", prompt="a boat", dry_run=True)

    assert len(attempted) == 2
    assert attempted == expected_ids[:2]
    assert graph_loads == attempted


def test_saved_catalog_pin_uses_one_registry_load_and_no_ready_graphs(monkeypatch):
    from astrid.core.generation import defaults, settings

    original_load = ModelRegistry.load_default.__func__
    registry_loads = []

    def counted_load(cls, **kwargs):
        registry_loads.append(kwargs)
        return original_load(cls, **kwargs)

    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(counted_load))
    _configure_command(monkeypatch, patch_preferences=False, patch_registry=False)
    snapshot = settings.Snapshot(
        {"defaults": {"image.t2i": {"pack": "generation", "entry": "z-image"}}},
        1, "runtime")
    monkeypatch.setattr(settings, "read", lambda client=None: snapshot)
    monkeypatch.setattr(defaults, "pack_candidates", lambda registry=None: [])
    graph_loads = _observe_graph_loads(monkeypatch)
    selected = []

    def stop_after_catalog_selection(**kwargs):
        selected.append(kwargs["candidate"].id)
        raise _StopAtSelectedCandidate

    monkeypatch.setattr("astrid.sdk.generation._prepare_catalog_target_workflow",
                        stop_after_catalog_selection)

    with pytest.raises(_StopAtSelectedCandidate):
        GenerationFacade().image(mode="t2i", placement="local", prompt="a boat", dry_run=True)

    assert selected == ["z-image"]
    assert len(registry_loads) == 1
    assert graph_loads == []
