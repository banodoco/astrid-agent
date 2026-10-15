"""Backend-neutral rendering helpers shared by the Remotion and Three.js backends.

Only ``astrid.core`` is imported here: this module is the seam that lets
``rendering.threejs`` reuse the Remotion backend's side-effect-free
serialization/profile/provenance helpers without importing the Remotion
backend module itself.

Deliberately NOT included here (backend-specific, see the C0 plan's LEAVE
table): ``_execute_remotion`` (owns the Remotion render lock and the
trusted Node + project-local Remotion CLI invocation), settings parsers,
project validation,
and the ffmpeg backend's probe-form ``_duration_frames``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

from astrid.core import timeline
from astrid.core.foundation.paths import REPO_ROOT, WORKSPACE_ROOT
from astrid.core.media import ffprobe_metadata_strict
from astrid.core.pack.discovery import discover_pack_metadata
from astrid.core.rendering.contracts import RenderProfile
from astrid.core.rendering.profile import resolve_render_profile
from astrid.core.theme import builtin_theme, load_runtime_theme


# Review exports are deliberately small enough for a browser/editorial pass
# while retaining the authored composition canvas in the Remotion props.
REVIEW_MAX_WIDTH = 640
REVIEW_MAX_HEIGHT = 360


def _input_path(raw_path: str, workspace: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    return (candidate if candidate.is_absolute() else workspace / candidate).resolve()


def _load_registry_mapping(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"assets": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("assets"), dict):
        raise ValueError("assets registry must be an object containing an assets object")
    return data


def _serialize_timeline(
    timeline_path: Path,
    *,
    default_theme: str = "banodoco-default",
) -> dict[str, Any]:
    return (
        timeline.Timeline.load(timeline_path).for_render(default_theme=default_theme).to_json_data()
    )


def _resolve_theme_path(theme_path: Path) -> Path:
    """Require the one explicit runtime-materialized theme document."""

    candidate = Path(theme_path).expanduser()
    if not candidate.is_absolute() or candidate.name != "theme.json" or not candidate.is_file():
        raise FileNotFoundError(
            f"theme file not found or invalid: {candidate}; "
            "expected an existing runtime-materialized theme.json file"
        )
    return candidate


def _theme_for_props(theme_path: Path) -> dict[str, Any]:
    resolved = _resolve_theme_path(theme_path)
    if not resolved.is_file():
        raise FileNotFoundError(
            f"theme file not found or invalid: {resolved}; "
            "expected an existing runtime-materialized theme.json file"
        )
    theme_data = load_runtime_theme(resolved)
    return {"id": theme_data["id"], "visual": theme_data["visual"]}


def _theme_slug_for_render_default(theme_path: Path) -> str:
    return str(_theme_for_props(theme_path)["id"])


def _resolved_theme_for_render(
    timeline_path: Path,
    runtime_theme_path: Path | None,
) -> dict[str, Any]:
    """Return runtime-pinned theme truth with authored overrides merged.

    The timeline's historical theme slug is metadata only.  It is never used
    to search a checkout or filesystem theme directory during a render.
    """

    loaded = timeline.Timeline.load(timeline_path)
    timeline_config = loaded.to_config()
    merged = (
        _theme_for_props(runtime_theme_path)
        if runtime_theme_path is not None
        else builtin_theme()
    )
    overrides = timeline_config.get("theme_overrides")
    if isinstance(overrides, Mapping):
        visual = merged.get("visual")
        override_visual = overrides.get("visual")
        if isinstance(visual, Mapping) and isinstance(override_visual, Mapping):
            merged["visual"] = dict(visual)
            for key, value in override_visual.items():
                if isinstance(merged["visual"].get(key), Mapping) and isinstance(value, Mapping):
                    merged["visual"][key] = {
                        **merged["visual"][key],
                        **value,
                    }
                else:
                    merged["visual"][key] = value
    return {
        "id": merged.get("id") or merged.get("visual", {}).get("id") or "theme",
        "visual": merged["visual"],
    }


def _active_pack_order_for_provenance(
    project_root: Path | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "id": discovered.id,
            "source_kind": discovered.source_kind,
            "priority_index": discovered.priority_index,
            "root": str(discovered.pack_dir),
        }
        for discovered in discover_pack_metadata(
            project_root=project_root if project_root is not None else REPO_ROOT
        )
    ]


def _active_theme_for_provenance(
    theme_path: Path | None,
    active_theme: dict[str, Any] | None,
) -> dict[str, Any] | None:
    theme_id = active_theme.get("id") if isinstance(active_theme, dict) else None
    if theme_path is None:
        return {"id": theme_id or "banodoco-default", "path": None}
    resolved = _resolve_theme_path(theme_path)
    return {"id": theme_id or resolved.parent.name, "path": str(resolved)}


def _render_provenance_payload(
    *,
    project_dir: Path,
    composition_id: str,
    theme_path: Path | None,
    active_theme: dict[str, Any] | None,
    registry_state: dict[str, Any],
    stage_summary: dict[str, Any],
    runtime: Mapping[str, Any] | None = None,
    segments: list[dict[str, float | str]] | None = None,
    segment_provenance: list[dict[str, Any]] | None = None,
    active_pack_order: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    effects = list(stage_summary.get("effects") or [])
    animations = list(stage_summary.get("animations") or [])
    transitions = list(stage_summary.get("transitions") or [])
    payload: dict[str, Any] = {
        "project_dir": str(project_dir.resolve()),
        "composition_id": composition_id,
        "active_pack_order": (
            active_pack_order
            if active_pack_order is not None
            else _active_pack_order_for_provenance()
        ),
        "active_theme": _active_theme_for_provenance(theme_path, active_theme),
        "registry_hash": registry_state.get("hash"),
        "registry_state": registry_state,
        "resolved_effect_ids": [
            str(effect["effect_id"]) for effect in effects if "effect_id" in effect
        ],
        "resolved_effects": effects,
        "resolved_animation_ids": [
            str(item["element_id"]) for item in animations if "element_id" in item
        ],
        "resolved_animations": animations,
        "resolved_transition_ids": [
            str(item["element_id"]) for item in transitions if "element_id" in item
        ],
        "resolved_transitions": transitions,
        "source_pack_ids": sorted(
            {
                str(effect["source_pack_id"])
                for effect in effects
                if isinstance(effect, dict) and effect.get("source_pack_id")
            }
            | {
                str(item["source_pack_id"])
                for item in (*animations, *transitions)
                if isinstance(item, dict) and item.get("source_pack_id")
            }
        ),
        "element_roots": sorted(
            {
                str(effect["element_root"])
                for effect in effects
                if isinstance(effect, dict) and effect.get("element_root")
            }
            | {
                str(item["element_root"])
                for item in (*animations, *transitions)
                if isinstance(item, dict) and item.get("element_root")
            }
        ),
        "staged_asset_ids": sorted(
            {
                str(asset_id)
                for effect in effects
                if isinstance(effect, dict)
                for asset_id in effect.get("staged_asset_ids", ())
            }
        ),
        "staged_asset_root": stage_summary.get("root"),
    }
    if runtime is not None:
        payload["runtime"] = dict(runtime)
    if segments is not None:
        payload["segment_windows"] = segments
    if segment_provenance is not None:
        payload["segment_provenance"] = segment_provenance
    return payload


def _canonical_profile(
    timeline_path: Path,
    assets_data: Mapping[str, Any],
    theme_path: Path | None,
) -> RenderProfile:
    active_theme = _resolved_theme_for_render(timeline_path, theme_path)
    return resolve_render_profile(
        timeline_path,
        assets_data,
        theme=active_theme,
    )


def _profile_mismatches(
    requested: RenderProfile,
    canonical: RenderProfile,
) -> list[str]:
    requested_data = requested.to_dict()
    canonical_data = canonical.to_dict()
    mismatches: list[str] = []
    for field, expected in canonical_data.items():
        if field == "duration_tolerance":
            continue
        actual = requested_data[field]
        if actual != expected:
            mismatches.append(f"{field}={actual!r} (requires {expected!r})")
    return mismatches


def _duration_frames(video_path: Path, profile: RenderProfile) -> int:
    probe = ffprobe_metadata_strict(video_path)
    if probe.duration_rational is not None:
        duration = Fraction(*probe.duration_rational)
    elif probe.duration_seconds is not None:
        duration = Fraction(str(probe.duration_seconds))
    else:
        raise RuntimeError("ffprobe did not report a video duration")
    frames = duration * Fraction(*profile.fps_rational)
    return max(1, int(frames + Fraction(1, 2)))


def _timeline_alpha(timeline_data: Mapping[str, Any]) -> bool:
    """True when a serialized timeline's metadata stamps ``astrid_layer.alpha``.

    The service stamps ``metadata.astrid_layer = {z, alpha: z > 0}`` onto the
    materialized timeline of every z-layer segment (batch 2).  Backends read
    THIS dict to decide whether the segment must emit transparent output; an
    unstamped timeline (or ``alpha`` false/absent) keeps today's opaque path.
    """
    metadata = timeline_data.get("metadata")
    if not isinstance(metadata, Mapping):
        return False
    layer = metadata.get("astrid_layer")
    if not isinstance(layer, Mapping):
        return False
    return layer.get("alpha") is True


def _alpha_output_name(output_name: str) -> str:
    """Remap a stamped segment's output name to the ProRes container name.

    Remotion rejects ``.mp4`` output names for ``--codec=prores`` (it requires
    a ``.mov`` container), while the service hardcodes ``segment-NNNN.mp4``
    (service.py:1362).  The BACKEND therefore remaps the extension when the
    alpha stamp is present; the resulting artifact path/provenance stays
    consistent because every path downstream (staged video, published output,
    declared artifact, provenance payload) is derived from the remapped name.
    Unstamped names are returned unchanged (frozen .mp4 contract).
    """
    path = Path(output_name)
    if path.suffix.lower() == ".mov":
        return output_name
    return str(path.with_suffix(".mov"))


def _remotion_mux_profile(profile: RenderProfile, *, alpha: bool = False) -> RenderProfile:
    """The profile Remotion's muxer actually produces for this timeline.

    Opaque renders (unstamped / ``alpha=False``) are the frozen contract:
    H.264/yuv420p in an MP4 at the 90 kHz timescale with an always-muxed AAC
    track.  Alpha renders (``alpha=True``) switch the encoder to
    ProRes 4444 in a MOV container (``--codec=prores --prores-profile=4444
    --pixel-format=yuva444p10le --image-format=png``) which the host probed
    emits as ``yuva444p12le`` -- a REAL alpha plane (vp9/webm in remotion
    4.0.509 muxes plain yuv420p and is a dead path).  The audio/time_base
    fields below are pinned from a real probed ProRes artifact (see
    .oracle/findings/batch-4-rework-exec.txt).
    """
    if alpha:
        return replace(
            profile,
            time_base=(1, 90000),
            container="mov",
            video_codec="prores",
            video_profile=None,
            video_level=None,
            pixel_format="yuva444p12le",
            # Remotion's ProRes MOV path truthfully probes as PCM regardless
            # of whether the source timeline itself carries audio.  Do not
            # inherit the opaque MP4 profile's AAC declarations here.
            audio_codec="pcm_s16le",
            audio_sample_rate=48000,
            audio_channel_layout="stereo",
        )
    return replace(
        profile,
        time_base=(1, 90000),
        audio_codec=profile.audio_codec or "aac",
        audio_sample_rate=profile.audio_sample_rate or 48000,
        audio_channel_layout=profile.audio_channel_layout or "stereo",
    )


def _js_round(value: float) -> int:
    """Remotion dimension rounding for positive render dimensions."""
    return int(value + 0.5)


def _remotion_scaled_dimensions(
    profile: RenderProfile, scale: float, *, even: bool = True
) -> tuple[int, int]:
    """Mirror Remotion's scaled dimensions, including H.264 even rounding."""

    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale <= 0:
        raise ValueError("Remotion render scale must be a positive number")

    def scaled_dimension(source: int) -> int:
        candidate = source
        # Remotion adjusts the authored dimension down until its scaled output
        # is even for the H.264 codecs used by opaque MP4 renders.
        while even and candidate > 1 and _js_round(candidate * scale) % 2:
            candidate -= 1
        return max(1, _js_round(candidate * scale))

    return scaled_dimension(profile.width), scaled_dimension(profile.height)


def _review_render_scale(profile: RenderProfile) -> float:
    """Return the backend scale that fits the authored canvas into 640x360."""

    return min(1.0, REVIEW_MAX_WIDTH / profile.width, REVIEW_MAX_HEIGHT / profile.height)


def _review_output_profile(
    profile: RenderProfile, *, alpha: bool = False
) -> tuple[RenderProfile, float]:
    """Return the actual Remotion review profile and the ``--scale`` value."""

    scale = _review_render_scale(profile)
    width, height = _remotion_scaled_dimensions(profile, scale, even=not alpha)
    return _remotion_mux_profile(replace(profile, width=width, height=height), alpha=alpha), scale


def _reject_unknown_config(
    config: Mapping[str, Any],
    allowed: frozenset[str],
    backend_id: str,
) -> None:
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise ValueError(f"unknown {backend_id} configuration: {', '.join(unknown)}")


def _parse_min_free_gb(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("min_free_gb must be a number or null")
    min_free_gb = float(value)
    if min_free_gb < 0:
        raise ValueError("min_free_gb must not be negative")
    return min_free_gb
