"""F-3: reuse only a command-owned default model registry."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.core.generation.backends.comfy_target import ComfyModelMissing
from astrid.core.generation.backends.vibecomfy import VibeComfyBackend
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.sdk import generation
from tests.core.generation.backends.test_comfy_model_resolution import (
    install_target,
    target_info,
)
from tests.core.generation.backends.test_comfy_target import (  # noqa: F401
    engine_configuration,
    network,
    observation_configuration,
)


def _stub_catalog_helper(monkeypatch):
    monkeypatch.setattr(generation, "_validate_catalog_target_request", lambda **_: {})
    monkeypatch.setattr(
        "astrid.core.generation.entries.apply_workflow_inputs",
        lambda workflow, **_: (workflow, {"bindings": {"seed": 7}, "unbound": []}, {}),
    )


@pytest.mark.parametrize("backend_kind", ["kwargs", "keyword_only", "legacy", "opaque"])
def test_catalog_helper_dispatches_registry_only_when_supported(
    monkeypatch, backend_kind,
):
    _stub_catalog_helper(monkeypatch)
    registry = SimpleNamespace(_astrid_default_generation_registry=True)
    calls = []

    if backend_kind == "kwargs":
        def prepare(*args, **kwargs):
            calls.append((args, kwargs))
            return "workflow", {"roles": {"vae": "exact.vae"}}, None
    elif backend_kind == "keyword_only":
        def prepare(*args, registry=None, **kwargs):
            calls.append((args, {**kwargs, "registry": registry}))
            return "workflow", {"roles": {"vae": "exact.vae"}}, None
    elif backend_kind == "legacy":
        def prepare(candidate, mode, inputs, *, resolve_target=False,
                    retain_source=None, align="check"):
            calls.append(((candidate, mode, inputs), {
                "resolve_target": resolve_target,
                **({"retain_source": retain_source} if retain_source is not None else {}),
                **({"align": align} if align != "check" else {}),
            }))
            return "workflow", {"roles": {"vae": "exact.vae"}}, None
    else:
        class OpaqueCallable:
            @property
            def __signature__(self):
                raise ValueError("signature unavailable")

            def __call__(self, *args, **kwargs):
                assert "registry" not in kwargs
                calls.append((args, kwargs))
                return "workflow", {"roles": {"vae": "exact.vae"}}, None

        prepare = OpaqueCallable()

    result = generation._prepare_catalog_target_workflow(
        registry=registry,
        candidate=object(),
        mode="t2i",
        modality="image",
        inputs={"seed": 7},
        target_client=None,
        backend=SimpleNamespace(prepare_catalog_workflow=prepare),
        defer_target=True,
        reuse_default_registry=True,
    )

    assert len(calls) == 1
    if backend_kind in {"kwargs", "keyword_only"}:
        assert calls[0][1]["registry"] is registry
    else:
        assert "registry" not in calls[0][1]
    assert result[1]["roles"] == {"vae": "exact.vae"}
    assert result[1]["bindings"] == {"seed": 7}


def test_catalog_helper_does_not_retry_backend_type_error(monkeypatch):
    _stub_catalog_helper(monkeypatch)
    registry = SimpleNamespace(_astrid_default_generation_registry=True)
    calls = []

    def prepare(*args, **kwargs):
        calls.append(kwargs)
        raise TypeError("backend implementation failed")

    with pytest.raises(TypeError, match="backend implementation failed"):
        generation._prepare_catalog_target_workflow(
            registry=registry,
            candidate=object(),
            mode="t2i",
            modality="image",
            inputs={},
            target_client=None,
            backend=SimpleNamespace(prepare_catalog_workflow=prepare),
            defer_target=True,
            reuse_default_registry=True,
        )
    assert len(calls) == 1
    assert calls[0]["registry"] is registry


def test_scoped_or_unmarked_registry_keeps_backend_legacy_path(monkeypatch):
    _stub_catalog_helper(monkeypatch)
    registry = SimpleNamespace(_astrid_default_generation_registry=False)
    prepare = Mock(return_value=("workflow", {}, None))
    generation._prepare_catalog_target_workflow(
        registry=registry,
        candidate=object(),
        mode="t2i",
        modality="image",
        inputs={},
        target_client=None,
        backend=SimpleNamespace(prepare_catalog_workflow=prepare),
        defer_target=True,
        reuse_default_registry=True,
    )
    assert "registry" not in prepare.call_args.kwargs


def test_model_registry_loader_marks_only_unscoped_default(monkeypatch):
    registry = ModelRegistry([])
    load = Mock(return_value=registry)
    monkeypatch.setattr(ModelRegistry, "load_default", load)

    assert generation._load_model_registry() is registry
    assert registry._astrid_default_generation_registry is True
    assert generation._load_model_registry(project_root="/project") is registry
    assert registry._astrid_default_generation_registry is False


def test_backend_registry_reuse_preserves_plan_bindings_approval_and_missing_alternatives(
    network, monkeypatch,
):
    from vibecomfy.workflow_bundle import load_bundle

    client, _requests = install_target(network, target_info())
    registry = ModelRegistry.load_default()
    candidate = registry.get("z-image")
    backend = VibeComfyBackend(target_client=client)
    params = {"prompt": "a red kite above the coast", "seed": 19}

    real_loader = ModelRegistry.load_default
    loads = []

    def counted_loader(*args, **kwargs):
        loads.append((args, kwargs))
        return real_loader(*args, **kwargs)

    monkeypatch.setattr(ModelRegistry, "load_default", counted_loader)
    legacy_workflow, legacy_plan, legacy_schema = backend.prepare_catalog_workflow(
        candidate, "t2i", params, resolve_target=True,
    )
    assert len(loads) == 1

    reused_workflow, reused_plan, reused_schema = backend.prepare_catalog_workflow(
        candidate, "t2i", params, resolve_target=True, registry=registry,
    )
    assert len(loads) == 1
    assert reused_plan == legacy_plan
    assert reused_plan["roles"] == legacy_plan["roles"]

    legacy_approval = load_bundle(legacy_workflow).compile(schema_provider=legacy_schema)
    reused_approval = load_bundle(reused_workflow).compile(schema_provider=reused_schema)
    assert reused_approval.api_digest == legacy_approval.api_digest
    assert reused_approval.to_dict()["api_projection"] == legacy_approval.to_dict()["api_projection"]

    bad_info = target_info(unets=("a different model.safetensors",))
    missing_client, _ = install_target(network, bad_info)
    legacy_error = None
    reused_error = None
    try:
        backend_for_missing = VibeComfyBackend(target_client=missing_client)
        backend_for_missing.prepare_catalog_workflow(
            candidate, "t2i", params, resolve_target=True,
        )
    except ComfyModelMissing as exc:
        legacy_error = exc
    try:
        backend_for_missing.prepare_catalog_workflow(
            candidate, "t2i", params, resolve_target=True, registry=registry,
        )
    except ComfyModelMissing as exc:
        reused_error = exc
    assert isinstance(legacy_error, ComfyModelMissing)
    assert isinstance(reused_error, ComfyModelMissing)
    assert str(reused_error) == str(legacy_error)
    assert reused_error.failures == legacy_error.failures
    assert reused_error.alternatives == legacy_error.alternatives


def test_facade_reuses_default_registry_within_each_command_only(network, monkeypatch):
    from astrid.core.generation import preflight
    from astrid.sdk.generation import GenerationFacade

    client, _requests = install_target(network, target_info())
    monkeypatch.setattr(preflight, "_module_available", lambda name: name == "vibecomfy")
    monkeypatch.setattr(
        preflight, "_comfyui_runtime_available",
        Mock(side_effect=AssertionError("managed installation probed")),
    )
    real_loader = ModelRegistry.load_default
    loads = []

    def counted_loader(*args, **kwargs):
        loads.append((args, kwargs))
        return real_loader(*args, **kwargs)

    monkeypatch.setattr(ModelRegistry, "load_default", counted_loader)
    facade = GenerationFacade()
    first = facade.image(
        model="z-image", mode="t2i", placement=client.target.url,
        prompt="a red kite", seed=19, dry_run=True,
    )
    assert len(loads) == 1
    second = facade.image(
        model="z-image", mode="t2i", placement=client.target.url,
        prompt="a red kite", seed=19, dry_run=True,
    )
    assert len(loads) == 2
    assert first == second
    assert first["entry"]["approval"]["api_digest"]


def test_explicit_none_registry_uses_legacy_load(network, monkeypatch):
    client, _ = install_target(network, target_info())
    candidate = ModelRegistry.load_default().get("z-image")
    real_loader = ModelRegistry.load_default
    loads = []

    def counted_loader(*args, **kwargs):
        loads.append(None)
        return real_loader(*args, **kwargs)

    monkeypatch.setattr(ModelRegistry, "load_default", counted_loader)
    VibeComfyBackend(target_client=client).prepare_catalog_workflow(
        candidate, "t2i", {"prompt": "a red kite", "seed": 19},
        resolve_target=True, registry=None,
    )
    assert len(loads) == 1
