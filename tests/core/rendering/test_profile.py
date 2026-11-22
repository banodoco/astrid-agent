from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.rendering.contracts import AudioOwnership
from astrid.core.rendering.profile import resolve_render_profile
from astrid.core.theme import ThemeValidationError, builtin_theme


def _write_theme(root: Path, slug: str, *, width: int, height: int, fps: object) -> None:
    theme_dir = root / slug
    theme_dir.mkdir(parents=True)
    theme = builtin_theme()
    theme["id"] = slug
    theme["visual"]["canvas"] = {"width": width, "height": height, "fps": fps}
    (theme_dir / "theme.json").write_text(
        json.dumps(theme),
        encoding="utf-8",
    )


def _timeline(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {"theme": "cinema", "tracks": [], "clips": []}
    value.update(updates)
    return value


def test_profile_uses_theme_canvas_when_timeline_has_no_override(tmp_path: Path) -> None:
    _write_theme(tmp_path, "cinema", width=1280, height=720, fps=24)

    profile = resolve_render_profile(_timeline(), theme=tmp_path / "cinema" / "theme.json")

    assert (profile.width, profile.height) == (1280, 720)
    assert profile.fps_rational == (24, 1)
    assert profile.time_base == (1, 12288)
    assert profile.container == "mp4"
    assert profile.video_codec == "h264"
    assert profile.pixel_format == "yuv420p"
    assert profile.has_audio is False
    assert profile.duration_tolerance == 1


def test_profile_accepts_exact_authored_fps_object():
    rate = {"numerator": 30000, "denominator": 1001}
    timeline = _timeline(output={"resolution": "640x360", "file": "film.mp4", "fps": rate})
    profile = resolve_render_profile(timeline)
    assert profile.fps_rational == (30000, 1001)


def test_profile_mirrors_remotion_partial_override_precedence(tmp_path: Path) -> None:
    _write_theme(tmp_path, "cinema", width=1024, height=576, fps=24)
    timeline = _timeline(
        theme_overrides={"visual": {"canvas": {"fps": 25}}},
    )

    profile = resolve_render_profile(timeline, theme=tmp_path / "cinema" / "theme.json")

    # Root.tsx selects the entire override canvas before the merged theme, so
    # missing override dimensions use Remotion's defaults rather than 1024x576.
    assert (profile.width, profile.height) == (1920, 1080)
    assert profile.fps_rational == (25, 1)
    assert profile.time_base == (1, 12800)


def test_profile_preserves_explicit_rational_fps_without_float_drift() -> None:
    profile = resolve_render_profile(
        _timeline(
            theme_overrides={
                "visual": {
                    "canvas": {"width": 1920, "height": 1080, "fps": [30000, 1001]}
                }
            }
        )
    )

    assert profile.fps_rational == (30000, 1001)
    assert profile.time_base == (1, 30000)


def test_decimal_fps_is_rationalized_from_authored_text_not_binary_float() -> None:
    profile = resolve_render_profile(
        _timeline(
            theme_overrides={
                "visual": {"canvas": {"width": 640, "height": 360, "fps": 29.97}}
            }
        )
    )

    assert profile.fps_rational == (2997, 100)


def test_only_referenced_audio_track_assets_enable_rendered_audio() -> None:
    assets = {
        "assets": {
            "used": {"type": "audio/wav", "file": "used.wav"},
            "unreferenced": {"type": "audio/wav", "file": "unused.wav"},
        }
    }
    timeline = _timeline(
        tracks=[{"id": "audio", "kind": "audio", "label": "Audio"}],
        clips=[
            {
                "id": "music",
                "track": "audio",
                "clipType": "media",
                "asset": "used",
                "at": 0,
                "from": 0,
                "to": 2,
            }
        ],
    )

    profile = resolve_render_profile(timeline, assets)

    assert profile.audio_codec == "aac"
    assert profile.audio_sample_rate == 48000
    assert profile.audio_channel_layout == "stereo"


def test_unreferenced_audio_inventory_does_not_enable_audio() -> None:
    profile = resolve_render_profile(
        _timeline(),
        {"assets": {"music": {"type": "audio/wav", "file": "music.wav"}}},
    )

    assert profile.has_audio is False


def test_explicit_passthrough_profile_is_visual_only_despite_audio_clip() -> None:
    timeline = _timeline(
        tracks=[{"id": "audio", "kind": "audio", "label": "Audio"}],
        clips=[
            {
                "id": "music",
                "track": "audio",
                "clipType": "media",
                "asset": "music",
            }
        ],
    )

    profile = resolve_render_profile(
        timeline,
        audio_ownership=AudioOwnership.PASSTHROUGH,
        duration_tolerance=2,
    )

    assert profile.has_audio is False
    assert profile.duration_tolerance == 2


def test_explicit_theme_mapping_is_merged_with_full_timeline_override() -> None:
    profile = resolve_render_profile(
        _timeline(
            theme_overrides={
                "visual": {"canvas": {"width": 800, "height": 800, "fps": "24000/1001"}}
            }
        ),
        theme={"id": "inline", "visual": {"canvas": {"width": 640, "height": 480, "fps": 30}}},
    )

    assert (profile.width, profile.height) == (800, 800)
    assert profile.fps_rational == (24000, 1001)
    assert profile.time_base == (1, 24000)


def test_omitted_theme_uses_the_intentional_default_canvas() -> None:
    profile = resolve_render_profile(
        {"tracks": [], "clips": []},
    )

    assert (profile.width, profile.height) == (1920, 1080)
    assert profile.fps_rational == (30, 1)


def test_explicit_missing_theme_path_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "theme.json"

    with pytest.raises(ThemeValidationError, match="theme file not found or invalid"):
        resolve_render_profile(
            {"tracks": [], "clips": []},
            theme=missing,
        )


def test_explicit_output_canvas_preserves_theme_content():
    from astrid.core.rendering.profile import _resolve_merged_theme, _remotion_canvas
    theme = builtin_theme()
    timeline = _timeline(output={'resolution': '1280x720', 'fps': 24, 'file': 'out.mp4'})
    merged = _resolve_merged_theme(timeline, theme=theme)
    assert merged == theme
    assert _remotion_canvas(timeline, merged) == {'width': 1280, 'height': 720, 'fps': 24}
