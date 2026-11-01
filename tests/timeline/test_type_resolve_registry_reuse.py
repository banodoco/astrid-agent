"""Focused controls for type-resolution registry reuse and admission."""

from __future__ import annotations

from dataclasses import replace

import pytest

from astrid.core.contracts.artifact_types import ARTIFACT_TYPE_REGISTRY
from astrid.core.contracts.schema import Output
from astrid.core.element import catalog
from astrid.core.element import registry as element_registry_module
from astrid.core.element.registry import ElementRegistry, load_default_registry
from astrid.core.timeline import banodoco_schema
from astrid.core.timeline.validators._type_resolve import (
    is_visual_clip_element,
    resolve_clip_to_artifact_type,
)
from astrid.core.timeline.validators.timeline import validate_timeline


def _timeline(clips: list[dict[str, object]]) -> dict[str, object]:
    return {
        "tracks": [{"id": "v1", "kind": "visual", "label": "Visual"}],
        "clips": clips,
    }


def _clip(clip_type: str = "media", **fields: object) -> dict[str, object]:
    clip: dict[str, object] = {
        "id": "clip-1",
        "at": 0,
        "track": "v1",
        "clipType": clip_type,
        "hold": 2,
    }
    clip.update(fields)
    return clip


@pytest.fixture(autouse=True)
def clear_registry_caches():
    catalog._clear_registry_cache()
    yield
    catalog._clear_registry_cache()


def test_default_validation_assembles_one_registry_for_media_clip(monkeypatch):
    element_loads: list[dict[str, object]] = []
    catalog_loads: list[dict[str, object]] = []
    load_element_registry = element_registry_module.load_default_registry
    load_catalog_registry = catalog.load_default_registry

    def counted_element_load(**kwargs):
        element_loads.append(kwargs)
        return load_element_registry(**kwargs)

    def counted_catalog_load(**kwargs):
        catalog_loads.append(kwargs)
        return load_catalog_registry(**kwargs)

    monkeypatch.setattr(element_registry_module, "load_default_registry", counted_element_load)
    monkeypatch.setattr(catalog, "load_default_registry", counted_catalog_load)

    validate_timeline(_timeline([_clip()]))

    assert len(element_loads) == 1
    assert catalog_loads == []


def test_empty_validation_performs_zero_registry_assemblies(monkeypatch):
    element_loads: list[dict[str, object]] = []
    catalog_loads: list[dict[str, object]] = []
    load_element_registry = element_registry_module.load_default_registry
    load_catalog_registry = catalog.load_default_registry

    monkeypatch.setattr(
        element_registry_module,
        "load_default_registry",
        lambda **kwargs: element_loads.append(kwargs) or load_element_registry(**kwargs),
    )
    monkeypatch.setattr(
        catalog,
        "load_default_registry",
        lambda **kwargs: catalog_loads.append(kwargs) or load_catalog_registry(**kwargs),
    )

    validate_timeline(_timeline([]))

    assert element_loads == []
    assert catalog_loads == []


def test_default_visual_resolution_does_not_reassemble_catalog(monkeypatch):
    def fail_catalog_membership(*_args, **_kwargs):
        raise AssertionError("default type resolution must reuse its loaded registry")

    monkeypatch.setattr(catalog, "list_element_ids", fail_catalog_membership)

    assert is_visual_clip_element("text-card", None)


def test_explicit_registry_keeps_independent_catalog_membership_guard():
    default = load_default_registry()
    definition = default.get("effects", "text-card")
    caller_only = replace(definition, id="caller-only-effect")
    supplied = ElementRegistry([caller_only])

    assert (
        resolve_clip_to_artifact_type(
            "caller-only-effect", None, supplied, ARTIFACT_TYPE_REGISTRY
        )
        is None
    )
    assert not is_visual_clip_element(
        "caller-only-effect", None, supplied, ARTIFACT_TYPE_REGISTRY
    )


def test_explicit_registry_override_still_controls_type_output():
    default = load_default_registry()
    definition = default.get("effects", "text-card")
    override = replace(
        definition,
        outputs=(Output(name="rendered", type="file", artifact_type="custom/output"),),
    )
    supplied = ElementRegistry([override])

    assert (
        resolve_clip_to_artifact_type("text-card", None, supplied, ARTIFACT_TYPE_REGISTRY)
        == "custom/output"
    )


def test_transition_validation_still_refuses_when_catalog_guard_fails(monkeypatch):
    refusal = RuntimeError("transition catalog integrity failure")

    def refuse_catalog():
        raise refusal

    monkeypatch.setattr(banodoco_schema, "_transition_ids", refuse_catalog)

    with pytest.raises(RuntimeError, match="integrity failure"):
        validate_timeline(
            _timeline(
                [
                    _clip(transition="cross-fade"),
                    _clip(at=2, id="clip-2"),
                ]
            )
        )
