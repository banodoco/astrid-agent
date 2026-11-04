#!/usr/bin/env python3
"""Claude-powered arrangement composer over a brief-agnostic source pool."""


from __future__ import annotations

from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('editorial.arrange')
import argparse
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import AuditContext
from astrid.pack_api import load_theme
from astrid.pack_api import ARRANGEMENT_VERSION, is_all_generative_arrangement, load_arrangement, load_pool, save_arrangement, validate_arrangement, validate_arrangement_duration_window
from astrid.pack_api import ClaudeClient, build_claude_client
from astrid.pack_api import utc_now_seconds

FORBIDDEN_TIME_KEYS = frozenset({"start", "end", "timestamp", "seconds", "time", "src_start", "src_end", "from", "to", "at"})
RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "target_duration_sec": {"type": "number", "minimum": 70, "maximum": 95},
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "order": {"type": "integer", "minimum": 1},
                    "audio_source": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "properties": {
                            "pool_id": {"type": "string"},
                            "trim_sub_range": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 2,
                                "maxItems": 2,
                            },
                        },
                        "required": ["pool_id", "trim_sub_range"],
                    },
                    "visual_source": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "properties": {
                            "pool_id": {"type": "string"},
                            "role": {"type": "string", "enum": ["primary", "overlay", "stinger"]},
                            "params": {"type": "object"},
                        },
                        "required": ["pool_id", "role"],
                    },
                    "text_overlay": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "properties": {
                            "content": {"type": "string"},
                            "style_preset": {"type": "string"},
                        },
                        "required": ["content"],
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["order", "audio_source", "visual_source", "text_overlay", "rationale"],
            },
        }
    },
    "required": ["target_duration_sec", "clips"],
}
REVISE_RESPONSE_SCHEMA = json.loads(json.dumps(RESPONSE_SCHEMA))
REVISE_RESPONSE_SCHEMA["properties"]["clips"]["items"]["properties"]["uuid"] = {
    "type": "string",
    "pattern": "^[0-9a-f]{8}$",
}
SYSTEM_PROMPT_PREFIX = (
    "You are composing a brief-specific arrangement from a reusable pool of source clips. "
    "Use only the provided pool ids. "
    "Each audio_source must name one dialogue pool_id plus a tight absolute trim_sub_range inside that pool entry. "
    "Each visual_source must declare pool_id and role. "
    "Return seconds only inside audio_source.trim_sub_range."
)
GENERATIVE_PROMPT_EXTENSION = (
    " Pool entries have kind=source for existing media candidates and kind=generative for render effects. "
    "Favor generative entries when the brief needs quote cards, titles, emphasis beats, or when source footage is not needed. "
    "Generative visual_source may also include params matching that effect schema."
)
REVISE_SYSTEM_PREFIX = (
    "Here is your previous arrangement. Here is editor feedback. Make MINIMAL changes that address the notes "
    "\u2014 preserve shape, total duration, and any clips the editor did not flag. "
    "Honor swap/reorder/insert-stinger actions exactly; apply micro-fix trim changes. "
    "Each prior clip has a `uuid`. Copy the same `uuid` verbatim for any clip you preserve "
    "(even if reordered). Omit `uuid` for newly inserted or swapped clips."
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _clip_description(entry: dict[str, Any]) -> str:
    for field in ("text", "subject", "event_label", "bed_kind"):
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    tags: list[str] = []
    for field in ("motion_tags", "mood_tags"):
        value = entry.get(field)
        if isinstance(value, list):
            tags.extend(str(item).strip() for item in value if str(item).strip())
    if tags:
        return ", ".join(tags[:4])
    source_ids = entry.get("source_ids", {})
    if isinstance(source_ids, dict):
        scene_id = source_ids.get("scene_id")
        if isinstance(scene_id, str) and scene_id:
            return scene_id
    return entry.get("category") or entry["kind"]


def _score_summary(entry: dict[str, Any]) -> str:
    scores = entry.get("scores", {})
    if not isinstance(scores, dict) or not scores:
        return "scores=none"
    pairs = []
    for key in sorted(scores):
        value = scores[key]
        if isinstance(value, (int, float)):
            pairs.append(f"{key}={float(value):.2f}")
    return "scores=" + (", ".join(pairs) if pairs else "none")


def pool_digest(pool: dict[str, Any], *, include_generative: bool = True) -> str:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in pool.get("entries", []):
        if entry.get("excluded") is True:
            continue
        if entry.get("kind") == "generative":
            if not include_generative:
                continue
            grouped.setdefault("generative", []).append(entry)
        else:
            grouped.setdefault(str(entry.get("category")), []).append(entry)
    lines: list[str] = []
    for kind in ("dialogue", "visual", "reaction", "applause", "music"):
        entries = grouped.get(kind, [])
        if not entries:
            continue
        lines.append(f"{kind.upper()}:")
        for entry in entries:
            lines.append(
                "- "
                f"{entry['id']}: "
                f"[{float(entry['src_start']):.1f}, {float(entry['src_end']):.1f}] "
                f"dur={float(entry['duration']):.1f}s | "
                f"{_clip_description(entry)} | {_score_summary(entry)}"
            )
        lines.append("")
    generative_entries = grouped.get("generative", [])
    if generative_entries:
        lines.append("GENERATIVE:")
        for entry in generative_entries:
            meta = entry.get("meta", {}) if isinstance(entry.get("meta"), dict) else {}
            schema = entry.get("param_schema", {}) if isinstance(entry.get("param_schema"), dict) else {}
            lines.append(
                "- "
                f"{entry['id']}: effect={entry.get('effect_id')} | "
                f"{meta.get('whenToUse', meta.get('description', 'generative visual effect'))} | "
                f"params={json.dumps(schema.get('properties', schema), sort_keys=True)}"
            )
        lines.append("")
    return "\n".join(lines).strip()


def _response_schema_for(target_duration_sec: float | None, *, revise: bool = False) -> dict[str, Any]:
    schema = json.loads(json.dumps(REVISE_RESPONSE_SCHEMA if revise else RESPONSE_SCHEMA))
    duration_schema = schema["properties"]["target_duration_sec"]
    if target_duration_sec is None:
        duration_schema["minimum"] = 70
        duration_schema["maximum"] = 95
    else:
        duration_schema["minimum"] = float(target_duration_sec) * 0.95
        duration_schema["maximum"] = float(target_duration_sec) * 1.05
    return schema


def _eligible_pool_ids(pool: dict[str, Any], *, include_generative: bool = True) -> set[str]:
    return {
        entry["id"]
        for entry in pool.get("entries", [])
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and entry.get("excluded") is False
        and (include_generative or entry.get("kind") != "generative")
    }


def _generative_pool_ids(pool: dict[str, Any]) -> set[str]:
    return {
        entry["id"]
        for entry in pool.get("entries", [])
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry.get("kind") == "generative"
    }


def _validate_no_generative_visual_sources(arrangement: dict[str, Any], pool: dict[str, Any]) -> None:
    generative_ids = _generative_pool_ids(pool)
    for index, clip in enumerate(arrangement.get("clips", []), start=1):
        visual_source = clip.get("visual_source") if isinstance(clip, dict) else None
        if not isinstance(visual_source, dict):
            continue
        pool_id = visual_source.get("pool_id")
        if pool_id in generative_ids:
            raise AstridError(
                f"clip {index} uses generative visual_source {pool_id!r}, which is disabled for source-cut arrangements",
                valid_options=["--allow-generative-effects"],
                recovery_command="re-run with --allow-generative-effects flag to enable generative visual sources",
            )


def _validate_generative_params(arrangement: dict[str, Any], pool: dict[str, Any]) -> None:
    try:
        import jsonschema  # type: ignore[import-not-found]
    except ImportError:
        return
    entries = {entry["id"]: entry for entry in pool.get("entries", []) if isinstance(entry, dict) and "id" in entry}
    for index, clip in enumerate(arrangement.get("clips", [])):
        visual_source = clip.get("visual_source") if isinstance(clip, dict) else None
        if not isinstance(visual_source, dict):
            continue
        entry = entries.get(visual_source.get("pool_id"))
        if not isinstance(entry, dict) or entry.get("kind") != "generative":
            continue
        schema = entry.get("param_schema")
        if isinstance(schema, dict):
            jsonschema.validate(visual_source.get("params", {}), schema)


def _assign_clip_uuids(clips: list[Any], prior_uuids: set[str] | None = None) -> None:
    prior = set(prior_uuids or set())
    used: set[str] = set()
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        clip_uuid = clip.get("uuid")
        if isinstance(clip_uuid, str) and clip_uuid in prior and clip_uuid not in used:
            used.add(clip_uuid)
        else:
            clip.pop("uuid", None)
    for clip in clips:
        if not isinstance(clip, dict) or "uuid" in clip:
            continue
        while True:
            clip_uuid = uuid.uuid4().hex[:8]
            if clip_uuid not in used:
                break
        clip["uuid"] = clip_uuid
        used.add(clip_uuid)


def _build_user_prompt(
    brief_text: str,
    target_duration_sec: float | None,
    *,
    allow_generative_effects: bool,
    no_audio: bool = False,
) -> str:
    target_line = (
        f"Use target_duration_sec approximately {float(target_duration_sec):.1f} seconds; this is visual-only and no audio track will be rendered."
        if no_audio and target_duration_sec is not None
        else f"Use target_duration_sec approximately {float(target_duration_sec):.1f} seconds, anchored to the rant audio."
        if target_duration_sec is not None
        else "Choose target_duration_sec between 75.0 and 90.0 seconds based on the brief."
    )
    constraint_lines = [
        "Brief:",
        brief_text.strip(),
        "",
        "Return JSON only.",
        target_line,
        "Hard constraints:",
        (
            f"- Final total duration must land within 5% of {float(target_duration_sec):.1f} seconds."
            if target_duration_sec is not None
            else "- Final total duration must land between 75.0 and 90.0 seconds."
        ),
    ]
    if no_audio:
        constraint_lines.extend(
            [
                "- Every clip must set audio_source to null.",
                "- Every clip must use a generative visual_source.",
                "- visual_source.role may be primary or stinger; prefer primary for full-screen text-card beats.",
                "- Visual-only clips should be 2.0 to 10.0 seconds long and carry the pacing without dialogue.",
            ]
        )
    else:
        constraint_lines.extend(
            [
                "- Every dialogue-driven clip must use audio_source.trim_sub_range with a duration from 4.0 to 10.0 seconds.",
                "- Every stinger clip must be 2.0 to 8.0 seconds long (use the longer end for title cards/closers).",
                "- Pick the punchiest 4-10 seconds inside each dialogue pool item instead of reusing the full paragraph.",
                "- audio_source.pool_id must come from DIALOGUE entries only.",
                "- If you use the same dialogue pool_id in multiple clips, their trim_sub_ranges must not overlap.",
            ]
        )
    constraint_lines.extend(
        [
        (
            "- visual_source.pool_id may come from VISUAL source entries or GENERATIVE entries."
            if allow_generative_effects
            else "- visual_source.pool_id must come from VISUAL source entries only."
        ),
        *(
            ["- For generative visual_source entries, include params.content when using the text-card effect."]
            if allow_generative_effects
            else []
        ),
        ]
    )
    if not no_audio:
        constraint_lines.extend(
            [
                "- For primary dialogue clips (audio + speaker visible), leave visual_source as null. The speaker auto-shows at the audio timestamp.",
                "- Set visual_source ONLY for overlay beats (b-roll layered on top of primary dialogue) or stinger beats (visual-only, no audio).",
                "- When you do set visual_source, role must be overlay (has audio underneath) or stinger (audio_source is null).",
                "- Overlays must play at least 2.5s (or the full audio slot, whichever is shorter); if no pool visual is >=2.5s and fits the moment, skip the overlay and use primary instead.",
                "- audio_source may be null only for stinger beats.",
            ]
        )
    constraint_lines.extend(
        [
            "- Use text_overlay only when the brief clearly wants on-screen copy.",
            "- rationale must explain why the clip fits the brief's beat.",
            "",
            "Return clips ordered for the final edit.",
        ]
    )
    return "\n".join(constraint_lines)


def _hard_constraint_prompt_lines(
    target_duration_sec: float | None,
    *,
    allow_generative_effects: bool,
    no_audio: bool = False,
) -> list[str]:
    target_line = (
        f"Use target_duration_sec approximately {float(target_duration_sec):.1f} seconds; this is visual-only and no audio track will be rendered."
        if no_audio and target_duration_sec is not None
        else f"Use target_duration_sec approximately {float(target_duration_sec):.1f} seconds, anchored to the rant audio."
        if target_duration_sec is not None
        else "Choose target_duration_sec between 75.0 and 90.0 seconds based on the brief."
    )
    lines = [
        target_line,
        "Hard constraints:",
        (
            f"- Final total duration must land within 5% of {float(target_duration_sec):.1f} seconds."
            if target_duration_sec is not None
            else "- Final total duration must land between 75.0 and 90.0 seconds."
        ),
    ]
    if no_audio:
        lines.extend(
            [
                "- Every clip must set audio_source to null.",
                "- Every clip must use a generative visual_source.",
                "- visual_source.role may be primary or stinger; prefer primary for full-screen text-card beats.",
                "- Visual-only clips should be 2.0 to 10.0 seconds long and carry the pacing without dialogue.",
            ]
        )
    else:
        lines.extend(
            [
                "- Every dialogue-driven clip must use audio_source.trim_sub_range with a duration from 4.0 to 10.0 seconds.",
                "- Every stinger clip must be 2.0 to 8.0 seconds long (use the longer end for title cards/closers).",
                "- Pick the punchiest 4-10 seconds inside each dialogue pool item instead of reusing the full paragraph.",
                "- audio_source.pool_id must come from DIALOGUE entries only.",
                "- If you use the same dialogue pool_id in multiple clips, their trim_sub_ranges must not overlap.",
            ]
        )
    lines.extend(
        [
        (
            "- visual_source.pool_id may come from VISUAL source entries or GENERATIVE entries."
            if allow_generative_effects
            else "- visual_source.pool_id must come from VISUAL source entries only."
        ),
        *(
            ["- For generative visual_source entries, include params.content when using the text-card effect."]
            if allow_generative_effects
            else []
        ),
        ]
    )
    if not no_audio:
        lines.extend(
            [
                "- For primary dialogue clips (audio + speaker visible), leave visual_source as null. The speaker auto-shows at the audio timestamp.",
                "- Set visual_source ONLY for overlay beats (b-roll layered on top of primary dialogue) or stinger beats (visual-only, no audio).",
                "- When you do set visual_source, role must be overlay (has audio underneath) or stinger (audio_source is null).",
                "- Overlays must play at least 2.5s (or the full audio slot, whichever is shorter); if no pool visual is >=2.5s and fits the moment, skip the overlay and use primary instead.",
                "- audio_source may be null only for stinger beats.",
            ]
        )
    lines.extend(
        [
            "- Use text_overlay only when the brief clearly wants on-screen copy.",
            "- rationale must explain why the clip fits the brief's beat.",
        ]
    )
    return lines


def _validated_arrangement(
    response: dict[str, Any],
    pool: dict[str, Any],
    brief_text: str,
    target_duration_sec: float | None,
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise AstridError(
            "Claude arrangement response must be an object",
            recovery_command="retry the Claude API call; the model returned a malformed response",
        )
    clips = response.get("clips")
    if not isinstance(clips, list):
        raise AstridError(
            "Claude arrangement response is missing clips",
            recovery_command="retry the Claude API call; the response was missing the clips field",
        )
    response_target_duration = response.get("target_duration_sec")
    if not isinstance(response_target_duration, (int, float)):
        raise AstridError(
            "Claude arrangement response is missing target_duration_sec",
            recovery_command="retry the Claude API call; the response was missing target_duration_sec",
        )
    response_target_duration = float(response_target_duration)
    if target_duration_sec is not None and abs(response_target_duration - float(target_duration_sec)) > float(target_duration_sec) * 0.05:
        raise AstridError(
            f"Claude arrangement response fell outside the requested target_duration_sec tolerance "
            f"({response_target_duration:.1f}s != {float(target_duration_sec):.1f}s)",
            recovery_command="retry the Claude API call; the response target_duration_sec was outside the 5% tolerance window",
        )
    payload: dict[str, Any] = {
        "version": ARRANGEMENT_VERSION,
        "generated_at": utc_now_seconds(),
        "brief_text": brief_text,
        "target_duration_sec": response_target_duration,
        "clips": clips,
    }
    source_slug = pool.get("source_slug")
    if isinstance(source_slug, str) and source_slug:
        payload["source_slug"] = source_slug
    return payload


def _system_prompt_prefix(*, allow_generative_effects: bool) -> str:
    return SYSTEM_PROMPT_PREFIX + (GENERATIVE_PROMPT_EXTENSION if allow_generative_effects else "")


def _resolve_theme_path(theme_value: str | None) -> Path | None:
    if theme_value is None:
        return None
    candidate = Path(theme_value).expanduser().resolve()
    if candidate.is_dir():
        candidate = candidate / "theme.json"
    if not candidate.is_file() or candidate.name != "theme.json":
        raise AstridError(
            "arrange requires an explicit runtime-materialized theme.json file; "
            "theme slugs and workspace theme fallback are not supported",
            recovery_command="Pass --theme <attempt-local theme.json>, or omit theme-dependent guidance",
        )
    return candidate


def _voice_prompt_block(theme: dict[str, Any] | None) -> str:
    if theme is None:
        return ""
    voice = theme.get("voice") if isinstance(theme.get("voice"), dict) else {}
    pacing = theme.get("pacing") if isinstance(theme.get("pacing"), dict) else {}
    lines: list[str] = []
    tone = voice.get("tone") if isinstance(voice, dict) else None
    if isinstance(tone, str) and tone.strip():
        lines.append(f"- Tone: {tone.strip()}")
    lexicon_prefer = voice.get("lexicon_prefer") if isinstance(voice, dict) else None
    if isinstance(lexicon_prefer, list) and lexicon_prefer:
        joined = ", ".join(str(item).strip() for item in lexicon_prefer if str(item).strip())
        if joined:
            lines.append(f"- Prefer lexicon: {joined}")
    lexicon_avoid = voice.get("lexicon_avoid") if isinstance(voice, dict) else None
    if isinstance(lexicon_avoid, list) and lexicon_avoid:
        joined = ", ".join(str(item).strip() for item in lexicon_avoid if str(item).strip())
        if joined:
            lines.append(f"- Avoid lexicon: {joined}")
    overlay_copy_style = voice.get("overlay_copy_style") if isinstance(voice, dict) else None
    if isinstance(overlay_copy_style, str) and overlay_copy_style.strip():
        lines.append(f"- Overlay copy style: {overlay_copy_style.strip()}")
    default_clip_sec = pacing.get("default_clip_sec") if isinstance(pacing, dict) else None
    if isinstance(default_clip_sec, (int, float)):
        lines.append(f"- Pacing hint: aim for ~{float(default_clip_sec):.1f}s clips when duration is unconstrained.")
    cut_tempo = pacing.get("cut_tempo") if isinstance(pacing, dict) else None
    if isinstance(cut_tempo, str) and cut_tempo.strip():
        lines.append(f"- Cut tempo: {cut_tempo.strip()}")
    return "Theme voice and pacing:\n" + "\n".join(lines) if lines else ""


def _assets_prompt_block(theme: dict[str, Any] | None) -> str:
    if theme is None:
        return ""
    generation = theme.get("generation") if isinstance(theme.get("generation"), dict) else {}
    assets = generation.get("assets") if isinstance(generation, dict) else None
    if not isinstance(assets, list):
        return ""
    lines: list[str] = []
    for item in assets:
        if not isinstance(item, dict):
            continue
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            continue
        asset_id = item.get("id") if isinstance(item.get("id"), str) and item.get("id") else item.get("file")
        include = item.get("always_include")
        suffix = " always_include=true" if include is True else " always_include=false" if include is False else ""
        lines.append(f"- {asset_id}: {description.strip()}{suffix}")
    return "Theme generation assets available for deliberate placement:\n" + "\n".join(lines) if lines else ""


def _system_prompt(
    pool: dict[str, Any],
    *,
    allow_generative_effects: bool,
    theme: dict[str, Any] | None,
    revise: bool = False,
) -> str:
    prompt_parts = [
        (
            f"{REVISE_SYSTEM_PREFIX} {GENERATIVE_PROMPT_EXTENSION if allow_generative_effects else ''}".strip()
            if revise
            else _system_prompt_prefix(allow_generative_effects=allow_generative_effects)
        ),
        "Pool digest:",
        pool_digest(pool, include_generative=allow_generative_effects),
    ]
    if theme is not None:
        prompt_parts.extend([_voice_prompt_block(theme), _assets_prompt_block(theme)])
    return "\n\n".join(part for part in prompt_parts if part)


def fill_arrangement_envelope(
    arrangement: dict[str, Any],
    *,
    source_slug: str,
    brief_slug: str,
    pool_sha256: str,
    brief_sha256: str,
) -> dict[str, Any]:
    enriched = dict(arrangement)
    enriched["source_slug"] = source_slug
    enriched["brief_slug"] = brief_slug
    enriched["pool_sha256"] = pool_sha256
    enriched["brief_sha256"] = brief_sha256
    return enriched


def build_arrangement(
    pool: dict[str, Any],
    brief_text: str,
    *,
    client: ClaudeClient,
    model: str = "claude-sonnet-4-6",
    target_duration_sec: float | None = None,
    allow_generative_effects: bool = False,
    no_audio: bool = False,
    theme: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from astrid.pack_api import compile_arrangement_plan

    system_prompt = _system_prompt(pool, allow_generative_effects=allow_generative_effects, theme=theme)
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": _build_user_prompt(
                brief_text,
                target_duration_sec,
                allow_generative_effects=allow_generative_effects,
                no_audio=no_audio,
            ),
        }
    ]
    last_arrangement: dict[str, Any] | None = None
    last_error: Exception | None = None
    for attempt in range(2):
        response = client.complete_json(
            model=model,
            system=system_prompt,
            messages=messages,
            response_schema=_response_schema_for(target_duration_sec),
            max_tokens=4000,
        )
        arrangement = _validated_arrangement(response, pool, brief_text, target_duration_sec)
        _assign_clip_uuids(arrangement["clips"])
        validate_arrangement(arrangement, _eligible_pool_ids(pool, include_generative=allow_generative_effects))
        if allow_generative_effects:
            _validate_generative_params(arrangement, pool)
        else:
            _validate_no_generative_visual_sources(arrangement, pool)
        if not is_all_generative_arrangement(arrangement, pool):
            validate_arrangement_duration_window(arrangement)
        try:
            compile_arrangement_plan(arrangement, pool)
            return arrangement
        except ValueError as exc:
            last_arrangement = arrangement
            last_error = exc
            if attempt == 1:
                break
            messages.append({"role": "assistant", "content": json.dumps(response)})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"The arrangement failed compile-time validation: {exc}. "
                        "Return a revised arrangement that fixes this specific clip while preserving the rest. "
                        "Remember: every clip must use audio_source null and a generative visual_source."
                        if no_audio
                        else (
                            "Remember: primary/overlay dialogue clips must be 4-10s, stingers 2-8s, "
                            "overlay visuals must have dur >= 0.8x the audio trim."
                        )
                    ),
                }
            )
    assert last_arrangement is not None and last_error is not None
    raise last_error


def build_revised_arrangement(
    pool: dict[str, Any],
    prior_arrangement: dict[str, Any],
    editor_notes: dict[str, Any],
    *,
    client: ClaudeClient,
    model: str,
    allow_generative_effects: bool = False,
    no_audio: bool = False,
    theme: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from astrid.pack_api import compile_arrangement_plan

    brief_text = str(prior_arrangement.get("brief_text") or "")
    target_duration_sec = prior_arrangement.get("target_duration_sec")
    if not isinstance(target_duration_sec, (int, float)):
        target_duration_sec = None
    prior_uuids = {
        str(clip["uuid"])
        for clip in prior_arrangement.get("clips", [])
        if isinstance(clip, dict) and isinstance(clip.get("uuid"), str)
    }
    system_prompt = _system_prompt(pool, allow_generative_effects=allow_generative_effects, theme=theme, revise=True)
    user_prompt = "\n".join(
        [
            f"PRIOR ARRANGEMENT:\n{json.dumps(prior_arrangement['clips'])}",
            "",
            f"EDITOR NOTES:\n{json.dumps(editor_notes['notes'])}",
            "",
            "Return JSON only.",
            *_hard_constraint_prompt_lines(
                float(target_duration_sec) if target_duration_sec is not None else None,
                allow_generative_effects=allow_generative_effects,
                no_audio=no_audio,
            ),
        ]
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
    last_arrangement: dict[str, Any] | None = None
    last_error: Exception | None = None
    max_attempts = 4
    for attempt in range(max_attempts):
        response = client.complete_json(
            model=model,
            system=system_prompt,
            messages=messages,
            response_schema=_response_schema_for(float(target_duration_sec) if target_duration_sec is not None else None, revise=True),
            max_tokens=4000,
        )
        arrangement = _validated_arrangement(response, pool, brief_text, target_duration_sec)
        _assign_clip_uuids(arrangement["clips"], prior_uuids=prior_uuids)
        validate_arrangement(arrangement, _eligible_pool_ids(pool, include_generative=allow_generative_effects))
        if allow_generative_effects:
            _validate_generative_params(arrangement, pool)
        else:
            _validate_no_generative_visual_sources(arrangement, pool)
        if not is_all_generative_arrangement(arrangement, pool):
            validate_arrangement_duration_window(arrangement)
        try:
            compile_arrangement_plan(arrangement, pool)
            return arrangement
        except ValueError as exc:
            last_arrangement = arrangement
            last_error = exc
            if attempt == max_attempts - 1:
                break
            messages.append({"role": "assistant", "content": json.dumps(response)})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"The revised arrangement failed compile-time validation: {exc}. "
                        "Return a corrected revised arrangement that stays inside the 70-95s total-duration window. "
                        "If the editor requested inserts that push over budget, shorten existing dialogue trims or drop "
                        "the lowest-impact inserts rather than violating duration bounds. Preserve the highest-priority "
                        "editor fixes (mid-sentence trims, framing fixes, visible gaps) above optional stinger inserts."
                    ),
                }
            )
    assert last_arrangement is not None and last_error is not None
    raise last_error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compose a brief-specific arrangement from a reusable pool.")
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--brief", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-slug")
    parser.add_argument("--brief-slug")
    parser.add_argument("--env-file", dest="env_file", type=Path)
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--target-duration", dest="target_duration", type=float)
    parser.add_argument("--allow-generative-effects", action="store_true")
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--theme", help="Theme id, theme directory, or path to theme.json.")
    parser.add_argument("--revise", action="store_true")
    parser.add_argument("--from-arrangement", dest="from_arrangement", type=Path)
    parser.add_argument("--editor-notes", dest="editor_notes", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.revise:
        missing = [
            flag
            for flag, value in (
                ("--from-arrangement", args.from_arrangement),
                ("--editor-notes", args.editor_notes),
            )
            if value is None
        ]
        if missing:
            parser.error(f"--revise requires {', '.join(missing)}")
    elif args.from_arrangement is not None or args.editor_notes is not None:
        parser.error("--from-arrangement and --editor-notes require --revise")
    pool_path = args.pool.resolve()
    brief_path = args.brief.resolve()
    pool_raw = pool_path.read_bytes()
    brief_raw = brief_path.read_bytes()
    pool = load_pool(pool_path)
    brief_text = brief_raw.decode("utf-8")
    theme_path = _resolve_theme_path(args.theme)
    theme = load_theme(theme_path) if theme_path is not None else None
    client = build_claude_client(args.env_file)
    if args.revise:
        prior_arrangement = load_arrangement(
            args.from_arrangement.resolve(),
            _eligible_pool_ids(pool, include_generative=args.allow_generative_effects),
            assign_missing_uuids=True,
        )
        editor_notes = json.loads(args.editor_notes.resolve().read_text(encoding="utf-8"))
        arrangement = build_revised_arrangement(
            pool,
            prior_arrangement,
            editor_notes,
            client=client,
            model=args.model,
            allow_generative_effects=args.allow_generative_effects,
            no_audio=args.no_audio,
            theme=theme,
        )
    else:
        arrangement = build_arrangement(
            pool,
            brief_text,
            client=client,
            model=args.model,
            target_duration_sec=args.target_duration,
            allow_generative_effects=args.allow_generative_effects,
            no_audio=args.no_audio,
            theme=theme,
        )
    source_slug = args.source_slug or str(pool.get("source_slug") or pool_path.parent.name)
    brief_slug = args.brief_slug or brief_path.stem
    arrangement = fill_arrangement_envelope(
        arrangement,
        source_slug=source_slug,
        brief_slug=brief_slug,
        pool_sha256=_sha256_bytes(pool_raw),
        brief_sha256=_sha256_bytes(brief_raw),
    )
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "arrangement.json"
    if not is_all_generative_arrangement(arrangement, pool):
        validate_arrangement_duration_window(arrangement)
    save_arrangement(arrangement, out_path, _eligible_pool_ids(pool, include_generative=args.allow_generative_effects))
    audit = AuditContext.from_env()
    if audit is not None:
        pool_id = audit.register_asset(kind="pool", path=pool_path, label="Input pool", stage="arrange")
        brief_id = audit.register_prompt_ref(path=brief_path, label="Brief", stage="arrange")
        arrangement_id = audit.register_asset(
            kind="arrangement",
            path=out_path,
            label="Arrangement",
            parents=[pool_id, brief_id],
            stage="arrange",
            metadata={
                "model": args.model,
                "clips": len(arrangement.get("clips", [])),
                "revise": bool(args.revise),
                "source_slug": source_slug,
                "brief_slug": brief_slug,
            },
        )
        audit.register_decision(
            stage="arrange",
            label="Selected arrangement clips",
            selected=[str(clip.get("uuid") or clip.get("order")) for clip in arrangement.get("clips", [])],
            metadata={"arrangement_asset": arrangement_id},
        )
        audit.register_node(stage="arrange", label="Build arrangement", parents=[pool_id, brief_id], outputs=[arrangement_id])
    print(out_path)

    # --- universal result manifest -----------------------
    manifest_path = out_dir / "manifest.json"
    manifest = build_manifest(
        kind="arrange",
        inputs={
            "pool": str(pool_path),
            "brief": str(brief_path),
        },
        outputs=[
            {"path": out_path.name, "type": "file"},
        ],
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(manifest_path, manifest)
    # -------------------------------------------------------------------------

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
