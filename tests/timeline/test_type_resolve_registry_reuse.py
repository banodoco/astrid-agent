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


def test_known_unannotated_animation_and_transition_do_not_refresh_registry(monkeypatch):
    default = load_default_registry()
    animation = replace(default.get("animations", "fade"), outputs=())
    transition = replace(
        default.get("transitions", "fade"),
        id="known-unannotated-transition",
        outputs=(),
    )
    supplied = ElementRegistry([animation, transition])
    loads: list[object] = []
    clears: list[bool] = []

    def load(**_kwargs):
        loads.append(True)
        return supplied

    monkeypatch.setattr(element_registry_module, "load_default_registry", load)
    monkeypatch.setattr(
        element_registry_module,
        "clear_default_registry_cache",
        lambda: clears.append(True),
    )

    assert not is_visual_clip_element("fade", None)
    assert not is_visual_clip_element("known-unannotated-transition", None)

    assert len(loads) == 2
    assert clears == []


def test_stable_absent_custom_element_miss_does_not_rediscover(monkeypatch):
    empty = ElementRegistry([])
    loads: list[ElementRegistry] = []
    clears: list[bool] = []

    def load(**_kwargs):
        loads.append(empty)
        return empty

    monkeypatch.setattr(element_registry_module, "load_default_registry", load)
    monkeypatch.setattr(
        element_registry_module,
        "clear_default_registry_cache",
        lambda: clears.append(True),
    )

    assert not is_visual_clip_element("custom-after-start", None)
    assert len(loads) == 1
    assert clears == []


def test_explicit_registry_is_a_fixed_snapshot(monkeypatch):
    default = load_default_registry()
    definition = replace(default.get("effects", "text-card"), id="caller-snapshot-effect")
    supplied = ElementRegistry([definition])

    def fail_default_lookup(*_args, **_kwargs):
        raise AssertionError("explicit registry must not consult the default registry cache")

    monkeypatch.setattr(element_registry_module, "lookup_default_registry", fail_default_lookup)
    monkeypatch.setattr(
        catalog,
        "list_element_ids",
        lambda kind, **_kwargs: ["caller-snapshot-effect"] if kind == "effects" else [],
    )

    assert is_visual_clip_element(
        "caller-snapshot-effect", None, supplied, ARTIFACT_TYPE_REGISTRY
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


def test_repeated_unknown_clip_types_do_not_rediscover(monkeypatch):
    load_default_registry()
    calls = []
    discover = element_registry_module.discover_pack_metadata

    def counted(*args, **kwargs):
        calls.append(True)
        return discover(*args, **kwargs)

    monkeypatch.setattr(element_registry_module, "discover_pack_metadata", counted)
    clips = [_clip("unknown-heavy-type", id=f"clip-{i}") for i in range(136)]
    validate_timeline(_timeline(clips))
    assert calls == []


def test_six_and_136_text_clips_share_one_pack_discovery_and_root_scan(monkeypatch):
    """Text clip validation must not rediscover the same element corpus per clip."""
    import time
    from pathlib import Path

    discoveries = []
    root_checks = []
    stat_counts = []
    observations = []
    original_discover = element_registry_module.discover_pack_metadata
    original_root_state = element_registry_module._root_state

    def count_discovery(*args, **kwargs):
        discoveries.append(True)
        return original_discover(*args, **kwargs)

    def count_root_state(roots):
        root_checks.append(tuple(roots))
        counts = {root: 0 for root in roots}
        original_stat = Path.stat

        def count_stat(path, *args, **kwargs):
            if path in counts:
                counts[path] += 1
            return original_stat(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "stat", count_stat)
            result = original_root_state(roots)
        stat_counts.append(counts)
        return result

    monkeypatch.setattr(element_registry_module, "discover_pack_metadata", count_discovery)
    monkeypatch.setattr(element_registry_module, "_root_state", count_root_state)
    for clip_count in (6, 136):
        catalog._clear_registry_cache()
        load_default_registry()  # Lookup traffic below starts from a warm corpus.
        discovery_start = len(discoveries)
        root_check_start = len(root_checks)
        clips = [
            _clip("text", id=f"title-{index}", text={"content": f"Title {index}"})
            for index in range(clip_count)
        ]
        started = time.perf_counter()
        validate_timeline(_timeline(clips))
        elapsed = time.perf_counter() - started
        observations.append((
            len(discoveries) - discovery_start,
            len(root_checks) - root_check_start,
            elapsed,
        ))

    for clip_count, (discovery_count, root_check_count, elapsed) in zip((6, 136), observations):
        print(
            f"{clip_count} text clips: discoveries={discovery_count}, "
            f"root_state_checks={root_check_count}, elapsed_seconds={elapsed:.6f}"
        )
        assert discovery_count == 0
    assert observations[0][1] == observations[1][1] == 1
    assert stat_counts and all(set(counts.values()) == {1} for counts in stat_counts)
    print(f"stats per root: {stat_counts[-1]}")
