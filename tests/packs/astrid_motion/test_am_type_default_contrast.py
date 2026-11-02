"""add_text on the default canvas: the default type colour must read on the default background.

add_text writes a bare ``am-type`` clip with only its text. The colour and the background are therefore the
element's declared default and the canvas background, both read here from the sources the renderer uses:
the element.yaml defaults (stored style), the am-type component fallback, and the canvas background of the
Remotion composition and the builtin default theme. Explicit colours must still win over the default.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from astrid.core.element.schema import load_element_definition
from astrid.core.theme._schema import BUILTIN_DEFAULT_THEME
from astrid.core.timeline.cuts import occurrences_from_snapshot
from astrid.packs.rendering.actions.timeline_visualize.motion import model

ROOT = Path(__file__).resolve().parents[3]
AM_TYPE = ROOT / "astrid" / "packs" / "astrid_motion" / "rendering" / "elements" / "effects" / "am-type"
COMPOSITION = ROOT / "remotion" / "src" / "ThreeTimelineComposition.tsx"
FPS = 30
AA_NORMAL_TEXT = 4.5


def _linear(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(colour: str) -> float:
    colour = colour.lstrip("#")
    red, green, blue = (int(colour[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _linear(red) + 0.7152 * _linear(green) + 0.0722 * _linear(blue)


def contrast(foreground: str, background: str) -> float:
    """WCAG 2.x contrast ratio between two six-digit hex colours."""
    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _canvas_backgrounds() -> dict[str, str]:
    """The backgrounds an add_text clip can sit on by default (builtin theme, composition fallback)."""
    source = COMPOSITION.read_text(encoding="utf-8")
    fallback = re.search(r"const DEFAULT_BACKGROUND = '(#[0-9A-Fa-f]{6})';", source)
    assert fallback, "the composition must declare DEFAULT_BACKGROUND as a six-digit hex colour"
    return {
        "builtin default theme bg": BUILTIN_DEFAULT_THEME["visual"]["color"]["bg"],
        "composition DEFAULT_BACKGROUND": fallback.group(1),
    }


def _am_type_definition():
    return load_element_definition(AM_TYPE, kind="effects", source="pack:astrid_motion", editable=False, priority=30)


def _add_text_snapshot(params: dict) -> dict:
    """A visualize snapshot holding one clip exactly as ``Timeline.add_text`` writes it."""
    clip = {
        "id": "chapter-one", "clipType": "am-type", "kind": "visual", "track": "titles",
        "at": 1.0, "hold": 3.0, "duration": 3.0, "params": params,
        "occurrence_id": "occ-1", "shot_occurrence_id": "occ-1", "shot_id": "1", "shot_name": "ONE",
        "start_frame": 30, "end_frame": 120,
    }
    return {
        "project_slug": "demo", "timeline_id": "tl", "timeline_name": "Film", "render_run_id": "run",
        "video_digest": "sha256:" + "d" * 64, "fps_rational": [FPS, 1], "duration_frames": 150,
        "clips": [clip],
        "tracks": [{"id": "titles", "kind": "visual"}],
        "occurrences": [{"occurrence_id": "occ-1", "shot_id": "1", "shot_name": "ONE",
                         "start": 0.0, "end": 5.0, "start_frame": 0, "end_frame": 150}],
        "registry": {"assets": {}}, "scripts": [], "metadata": {"selection": "composed_frame_capture"},
    }


def _visualized_element(params: dict):
    """The am-type element the visualize path builds from an add_text clip."""
    elements = model.elements_from_occurrences(occurrences_from_snapshot(_add_text_snapshot(params)), {})
    found = [element for element in elements if element.id == "chapter-one"]
    assert len(found) == 1, "the visualize path must carry the add_text clip as one element"
    return found[0]


def _effective_colour(element) -> str:
    """The colour the component paints: an explicit params colour, else the declared default."""
    return element.params.get("color") or _am_type_definition().defaults["color"]


def test_default_text_on_default_canvas_meets_aa_normal_text():
    element = _visualized_element({"text": "Chapter one"})
    colour = _effective_colour(element)
    for name, background in _canvas_backgrounds().items():
        ratio = contrast(colour, background)
        assert ratio >= AA_NORMAL_TEXT, f"default {colour} on {name} {background} is {ratio:.2f}:1, need 4.5:1"


def test_default_colour_is_declared_once_and_matches_the_component():
    definition = _am_type_definition()
    source = (AM_TYPE / "component.tsx").read_text(encoding="utf-8")
    declared = re.search(r"const DEFAULT_COLOR = '(#[0-9A-Fa-f]{6})';", source)
    assert declared, "the am-type component must declare DEFAULT_COLOR"
    assert declared.group(1) == definition.defaults["color"]
    assert "params.color ?? DEFAULT_COLOR" in source


def test_explicit_colour_still_wins_over_the_default():
    element = _visualized_element({"text": "Chapter one", "color": "#FF6B23"})
    assert element.params["color"] == "#FF6B23"
    assert _effective_colour(element) == "#FF6B23"
    source = (AM_TYPE / "component.tsx").read_text(encoding="utf-8")
    assert "params.color ?? DEFAULT_COLOR" in source


def test_default_text_is_not_stored_with_a_colour_of_its_own():
    # add_text sends no colour, so the default is what the element paints; no backing box is added.
    element = _visualized_element({"text": "Chapter one"})
    assert "color" not in element.params
    assert "background" not in element.params


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
