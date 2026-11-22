"""R13 authored transitions and phase effects are admitted by Remotion."""

import pytest

from astrid.packs.rendering.rendering.renderers.remotion import run as remotion


@pytest.fixture(scope="module")
def registry():
    return remotion._load_render_element_registry()


@pytest.mark.parametrize("identity", ["crossfade", "wipe", "slide-push", "zoom-through"])
def test_r13_transition_aliases_and_renderer_ids_preflight(registry, identity):
    data = {"clips": [{"id": "clip", "transition": {"type": identity, "duration": 0.4}}]}
    resolved = remotion._resolve_timeline_element_references(data, theme_path=None, registry=registry)
    expected_id = "cross-fade" if identity == "crossfade" else identity
    assert [(row["element_id"], row["source_pack_id"]) for row in resolved["transitions"]] == [
        (expected_id, "rendering"),
    ]


@pytest.mark.parametrize(
    "slot,identity",
    [
        *[("entrance", identity) for identity in (
            "slide-down", "slide-right", "zoom-in", "zoom-spin", "pulse", "flip", "bounce", "meteorite",
        )],
        *[("exit", identity) for identity in ("slide-down", "zoom-out", "flip", "fade-out", "shrink", "dissolve")],
        *[("continuous", identity) for identity in ("ken-burns", "float", "glitch", "slow-zoom", "drift")],
    ],
)
def test_r13_phase_effect_ids_preflight_in_their_original_slots(registry, slot, identity):
    data = {"clips": [{"id": "clip", slot: {"type": identity}}]}
    resolved = remotion._resolve_timeline_element_references(data, theme_path=None, registry=registry)
    assert resolved == {"animations": [], "transitions": []}


def test_unknown_transition_and_phase_effect_ids_remain_refused(registry):
    with pytest.raises(ValueError, match="unregistered transition 'crossfadee'"):
        remotion._resolve_timeline_element_references(
            {"clips": [{"id": "clip", "transition": {"type": "crossfadee"}}]},
            theme_path=None, registry=registry,
        )
    with pytest.raises(ValueError, match="unregistered animation 'unknown-r13-phase'"):
        remotion._resolve_timeline_element_references(
            {"clips": [{"id": "clip", "entrance": {"type": "unknown-r13-phase"}}]},
            theme_path=None, registry=registry,
        )
