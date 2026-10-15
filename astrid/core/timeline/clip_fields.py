"""V3 clip look fields declared in ``clip_fields.yaml``.

This module is the shared codec for SDK edits, timeline sheets, CLI flags and
render checks. It keeps the receiving tree's simple clip dictionaries and
top-level look fields while reading the donor's declarative field contract.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from astrid.core.timeline import audio_curves, motion_curves

FPS_DEFAULT = 30
_YAML_PATH = Path(__file__).with_name("clip_fields.yaml")
_PACKS_ROOT = Path(__file__).resolve().parents[2] / "packs"
_ANIMATIONS_DIR = _PACKS_ROOT / "rendering" / "rendering" / "elements" / "animations"
_NUMBER = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_NUM_RE = re.compile(rf"\s*({_NUMBER})\s*")
_DB_RE = re.compile(rf"\s*({_NUMBER})\s*dB\s*", re.IGNORECASE)
_DUR_RE = re.compile(rf"\s*({_NUMBER})\s*([fFsS]?)\s*")
_MUTE_WORDS = frozenset({"-inf", "-infdb", "mute"})
_EASINGS = tuple(motion_curves.MOTION_CURVES)
_CROP_SUM_MAX = 0.99


class ClipFieldError(ValueError):
    """A clip look value does not match its declared contract."""


def declare_keyframe_origin(clip: Mapping[str, Any], source: str) -> dict[str, Any]:
    """Declare an existing key domain, retaining every key byte/value and trim.

    The shared Composition adapter reads this offset. No origin is inferred from
    media ``from``, ``to`` or ``speed``. Calling again preserves an explicit origin.
    """
    if source not in ("app-seconds", "sprite-frames"):
        raise ClipFieldError(f"unknown keyframe source {source!r}")
    origin = clip.get("keyframeOrigin", -clip.get("at", 0) if source == "app-seconds" else 0)
    if isinstance(origin, bool) or not isinstance(origin, (int, float)) or not math.isfinite(origin):
        raise ClipFieldError("keyframeOrigin must be finite seconds relative to clip start")
    return dict(clip, keyframeOrigin=origin)


@dataclass(frozen=True)
class FieldSpec:
    name: str
    applies_to: str
    stored_at: str
    default: Any
    unit: str
    schema: Mapping[str, Any]
    kind: str
    write: str
    render: Mapping[str, Mapping[str, Any]]
    stored_at_audio: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: float | None = None
    suffix: str | None = None
    phase: str | None = None
    values: Mapping[str, Any] = field(default_factory=dict)
    visual_value: str | None = None
    notes: str = ""

    def to_stored(self, raw: Any, *, fps: float = FPS_DEFAULT, audio: bool = False) -> Any:
        return _coerce_one(self, raw, fps=fps, audio=audio)[1]

    def from_stored(self, value: Any, *, fps: float = FPS_DEFAULT) -> str | None:
        return _human(self, value, fps)


ClipField = FieldSpec


def _spec_from_yaml(name: str, body: Mapping[str, Any]) -> FieldSpec:
    applies = body["applies"]
    applies_list = [applies] if isinstance(applies, str) else list(applies)
    visual = any(value in ("visual", "footage") for value in applies_list)
    audio = "audio" in applies_list
    applies_to = "both" if visual and audio else ("visual" if visual else "audio")
    visual_block = body.get("visual") if isinstance(body.get("visual"), Mapping) else None
    audio_block = body.get("audio") if isinstance(body.get("audio"), Mapping) else None
    return FieldSpec(
        name=name,
        applies_to=applies_to,
        unit=str(body["unit"]),
        stored_at=str(visual_block["store"] if visual_block else body["store"]),
        stored_at_audio=str(audio_block["store"]) if audio_block else None,
        default=body.get("default"),
        schema=MappingProxyType(dict(body.get("schema", {}))),
        kind=str(body["type"]),
        write=str(body["write"]),
        render=MappingProxyType({key: MappingProxyType(dict(value)) for key, value in body["render"].items()}),
        minimum=body.get("min"),
        maximum=body.get("max"),
        exclusive_minimum=body.get("exclusive_min"),
        suffix=body.get("suffix"),
        phase=body.get("phase"),
        values=MappingProxyType(dict(body.get("values", {}))),
        visual_value=visual_block.get("value") if visual_block else None,
        notes=str(body.get("notes", "")),
    )


@lru_cache(maxsize=1)
def _load() -> tuple[Mapping[str, FieldSpec], frozenset[str]]:
    try:
        raw = yaml.safe_load(_YAML_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ClipFieldError(f"cannot read {_YAML_PATH.name}: {exc}") from exc
    if not isinstance(raw, Mapping) or raw.get("version") != 1 or not isinstance(raw.get("fields"), Mapping):
        raise ClipFieldError(f"{_YAML_PATH.name}: expected version: 1 and a fields mapping")
    specs = {str(name): _spec_from_yaml(str(name), body) for name, body in raw["fields"].items()}
    return MappingProxyType(specs), frozenset(raw.get("reserved", ()))


FIELDS: Mapping[str, FieldSpec] = _load()[0]


def fields() -> dict[str, FieldSpec]:
    return dict(_load()[0])


def reserved_names() -> frozenset[str]:
    return _load()[1]


def is_reserved(name: str) -> bool:
    return name in _load()[1]


@lru_cache(maxsize=1)
def _animations() -> Mapping[str, frozenset[str]]:
    """Allowed animation identifiers and phases from the V3 pack manifests."""
    out: dict[str, frozenset[str]] = {}
    for path in sorted(_ANIMATIONS_DIR.glob("*/element.yaml")):
        try:
            meta = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        coverage = meta.get("metadata") if isinstance(meta, Mapping) else None
        phase = coverage.get("phase") if isinstance(coverage, Mapping) else None
        if phase is None or phase == "any":
            phases = frozenset({"entrance", "exit"})
        elif isinstance(phase, str):
            phases = frozenset({phase})
        else:
            phases = frozenset(str(value) for value in phase)
        if isinstance(meta, Mapping) and isinstance(meta.get("id"), str):
            out[meta["id"]] = phases
    return MappingProxyType(out)


def _animation_ids(phase: str) -> list[str]:
    return sorted(name for name, phases in _animations().items() if phase in phases)


def _spec(name: str) -> FieldSpec:
    if name in FIELDS:
        return FIELDS[name]
    if is_reserved(name):
        raise ClipFieldError(f"{name!r} is reserved but not declared yet; declared: {', '.join(sorted(FIELDS))}")
    raise ClipFieldError(f"unknown clip field {name!r}; allowed: {', '.join(sorted(FIELDS))}")


def _seconds(raw: Any, fps: float, label: str) -> float:
    if isinstance(raw, bool):
        raise ClipFieldError(f"{label} must be a duration like 8f, 0.4s or 0.4")
    if isinstance(raw, (int, float)):
        value, unit = float(raw), "s"
    elif isinstance(raw, str):
        match = _DUR_RE.fullmatch(raw)
        if not match:
            raise ClipFieldError(f"{label} {raw!r} is not a duration; write 8f, 0.4s or 0.4")
        value, unit = float(match.group(1)), match.group(2).lower() or "s"
    else:
        raise ClipFieldError(f"{label} must be a duration like 8f, 0.4s or 0.4")
    seconds = value / fps if unit == "f" else value
    if not math.isfinite(seconds) or seconds <= 0:
        raise ClipFieldError(f"{label} must be a positive duration; got {raw!r}")
    return round(seconds, 4)


def _frames(raw: Any, fps: float, label: str) -> int:
    seconds = _seconds(raw, fps, label)
    match = _DUR_RE.fullmatch(raw) if isinstance(raw, str) else None
    if match and match.group(2).lower() == "f":
        return int(round(float(match.group(1))))
    return max(1, int(round(seconds * fps)))


def _fmt_seconds(seconds: float, fps: float) -> str:
    frames = seconds * fps
    rounded = round(frames)
    if rounded > 0 and abs(frames - rounded) < 0.01:
        return f"{rounded}f"
    return f"{seconds:g}s"


def _number(raw: Any, spec: FieldSpec) -> float:
    if isinstance(raw, bool):
        raise ClipFieldError(f"{spec.name} must be a number like {spec.write}")
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if spec.suffix and text.lower().endswith(spec.suffix):
            text = text[: -len(spec.suffix)]
        match = _NUM_RE.fullmatch(text)
        if not match:
            suffix = f" (or {spec.write})" if spec.suffix else ""
            raise ClipFieldError(f"{spec.name} {raw!r} is not a number; write {spec.write}{suffix}")
        value = float(match.group(1))
    else:
        raise ClipFieldError(f"{spec.name} must be a number like {spec.write}")
    if not math.isfinite(value):
        raise ClipFieldError(f"{spec.name} must be finite; got {raw!r}")
    if spec.minimum is not None and value < spec.minimum:
        raise ClipFieldError(f"{spec.name} {raw!r} is below {spec.minimum:g}; write e.g. {spec.write}")
    if spec.exclusive_minimum is not None and value <= spec.exclusive_minimum:
        raise ClipFieldError(f"{spec.name} {raw!r} must be above {spec.exclusive_minimum:g}; write e.g. {spec.write}")
    if spec.maximum is not None and value > spec.maximum:
        raise ClipFieldError(f"{spec.name} {raw!r} is above {spec.maximum:g}; write e.g. {spec.write}")
    return value


def _gain(raw: Any, spec: FieldSpec) -> float:
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in _MUTE_WORDS:
            return 0.0
        match = _DB_RE.fullmatch(raw)
        if match:
            decibels = float(match.group(1))
            if decibels > 12:
                raise ClipFieldError(f"volume {raw!r} is above +12 dB; allowed up to +12dB, e.g. {spec.write}")
            return round(10 ** (decibels / 20), 4)
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ClipFieldError(f"volume must be a gain or a dB value like {spec.write}, mute or -inf")
    try:
        value = float(raw)
    except ValueError:
        raise ClipFieldError(f"volume {raw!r} is not a gain or dB; write 0.5, {spec.write}, mute or -inf") from None
    if not math.isfinite(value):
        if value == float("-inf"):
            return 0.0
        raise ClipFieldError(f"volume {raw!r} is not a finite gain")
    if value < 0 or (spec.maximum is not None and value > spec.maximum):
        raise ClipFieldError(f"volume {raw!r} is outside 0..{spec.maximum:g}; write a gain or {spec.write}")
    return round(value, 4)


def _easing_name(text: str) -> str:
    try:
        return motion_curves.motion_curve_name(text)
    except ValueError:
        return text.strip().lower()


def _easing(raw: Any, label: str) -> str:
    name = _easing_name(str(raw))
    if name not in _EASINGS:
        raise ClipFieldError(f"{label} {raw!r} is not an easing; allowed: {', '.join(_EASINGS)}")
    return name


def _take_ease(spec: FieldSpec, tokens: list[str]) -> tuple[list[str], str | None]:
    found = [index for index, token in enumerate(tokens) if token.lower().startswith("ease=")]
    if not found:
        return tokens, None
    if len(found) > 1:
        raise ClipFieldError(f"{spec.name} has two ease= tokens; give one, e.g. 'ease=out'")
    index = found[0]
    easing = _easing(tokens[index][5:], f"{spec.name} ease")
    return tokens[:index] + tokens[index + 1 :], easing


def _params(spec: FieldSpec, raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        raise ClipFieldError(f"{spec.name}.params must be an object, e.g. {{'easing': 'out'}}")
    params = dict(raw)
    if "easing" in params:
        params["easing"] = _easing(params["easing"], f"{spec.name}.params.easing")
    return params or None


def _stored_easing(value: Mapping[str, Any]) -> str | None:
    params = value.get("params")
    reference = _easing(value["easing"], "animation.easing") if "easing" in value else None
    authored = _easing(params["easing"], "animation.params.easing") if isinstance(params, Mapping) and "easing" in params else None
    if reference is not None and authored is not None and reference != authored:
        raise ClipFieldError("animation: conflicting reference.easing and params.easing")
    return reference if reference is not None else authored


def _animation_value(spec: FieldSpec, raw: Any, fps: float) -> Any:
    if raw is None or (isinstance(raw, str) and raw.strip().lower() == "none"):
        return None
    if isinstance(raw, Mapping):
        extra = set(raw) - {"type", "duration", "intensity", "params", "easing"}
        if extra:
            raise ClipFieldError(f"{spec.name} has unknown key {sorted(extra)[0]!r}; allowed: type, duration")
        name = raw.get("type")
        duration = None if raw.get("duration") is None else _seconds(raw["duration"], fps, f"{spec.name}.duration")
        extras = {key: raw[key] for key in ("intensity", "params") if key in raw}
        if "easing" in raw:
            easing = _easing(raw["easing"], f"{spec.name}.easing")
            params = _params(spec, extras.get("params", {})) or {}
            if "easing" in params and params["easing"] != easing:
                raise ClipFieldError(f"{spec.name}: conflicting reference.easing and params.easing")
            extras["params"] = {**params, "easing": easing}
    elif isinstance(raw, str):
        tokens, easing = _take_ease(spec, raw.split())
        if not tokens or len(tokens) > 2:
            raise ClipFieldError(f"{spec.name} {raw!r} must be '<animation> <duration> [ease=E]', e.g. {spec.write!r}")
        name = tokens[0]
        duration = _seconds(tokens[1], fps, f"{spec.name} duration") if len(tokens) == 2 else None
        extras = {} if easing is None else {"params": {"easing": easing}}
    else:
        raise ClipFieldError(f"{spec.name} must be 'none' or '<animation> <duration>', e.g. {spec.write!r}")
    allowed = _animation_ids(spec.phase or "entrance")
    if name not in allowed:
        raise ClipFieldError(f"{spec.name} {name!r} is not a {spec.phase} animation; allowed: {', '.join(allowed)}")
    value: dict[str, Any] = {"type": name}
    if duration is not None:
        value["duration"] = duration
    if "intensity" in extras:
        value["intensity"] = extras["intensity"]
    if "params" in extras:
        params = _params(spec, extras["params"])
        if params:
            value["params"] = params
    return value


def _visual_fade_value(spec: FieldSpec, raw: Any, fps: float) -> Any:
    kind = spec.visual_value or "fade"
    if raw is None or (isinstance(raw, str) and raw.strip().lower() == "none"):
        return None
    if isinstance(raw, Mapping):
        if raw.get("type", kind) != kind:
            raise ClipFieldError(f"{spec.name} is a {kind} only; {raw.get('type')!r} belongs in {spec.stored_at}")
        return _animation_value(spec, {**raw, "type": kind}, fps)
    if isinstance(raw, str):
        tokens, easing = _take_ease(spec, raw.split())
        if tokens and tokens[0] == kind:
            tokens = tokens[1:]
        if len(tokens) > 1:
            raise ClipFieldError(f"{spec.name} {raw!r} must be a duration, e.g. {spec.write} or '{spec.write} ease=out'")
        value: dict[str, Any] = {"type": kind}
        if tokens:
            value["duration"] = _seconds(tokens[0], fps, f"{spec.name} duration")
        if easing is not None:
            params = _params(spec, {"easing": easing})
            if params:
                value["params"] = params
        return value
    raise ClipFieldError(f"{spec.name} must be a duration like {spec.write}, or none")


def _audio_fade_value(spec: FieldSpec, raw: Any, fps: float) -> Any:
    if raw is None or (isinstance(raw, str) and raw.strip().lower() == "none"):
        return None
    if isinstance(raw, Mapping):
        raise ClipFieldError(f"{spec.name} on audio is a duration in seconds, e.g. {spec.write}")
    if isinstance(raw, str) and any(token.lower().startswith("ease=") for token in raw.split()):
        raise ClipFieldError(f"{spec.name} ease= is for visual fades only; audio fades use curve=NAME, with linear volume ramp as the default")
    raw, _curve = _audio_fade_parts(raw)
    return _seconds(raw, fps, spec.name)


def _audio_fade_parts(raw: Any) -> tuple[Any, str]:
    if not isinstance(raw, str):
        return raw, "linear"
    tokens = raw.split()
    curves = [token[6:] for token in tokens if token.startswith("curve=")]
    if not curves:
        return raw, "linear"
    if len(curves) != 1 or len(tokens) != 2:
        raise ClipFieldError("audio fade: write a duration and one curve=NAME")
    try:
        audio_curves.validate(curves[0])
    except ValueError as exc:
        raise ClipFieldError(str(exc)) from None
    return next(token for token in tokens if not token.startswith("curve=")), curves[0]


def _transition_value(spec: FieldSpec, raw: Any, fps: float) -> Any:
    words = dict(spec.values)
    stored_ids = {value for value in words.values() if value}
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in ("cut", "none")):
        return None
    if isinstance(raw, Mapping):
        name = raw.get("id", raw.get("type"))
        if name not in words and name not in stored_ids:
            raise ClipFieldError(f"transition {name!r} is unknown; allowed: {', '.join(words)}")
        stored_id = words.get(name, name)
        value: dict[str, Any] = {"id": stored_id}
        if raw.get("durationFrames") is not None:
            value["durationFrames"] = int(round(float(raw["durationFrames"])))
        elif raw.get("duration") is not None:
            value["durationFrames"] = _frames(raw["duration"], fps, "transition duration")
        return value
    if not isinstance(raw, str):
        raise ClipFieldError(f"transition must be e.g. {spec.write!r} or cut")
    tokens = raw.split()
    if not tokens or len(tokens) > 2:
        raise ClipFieldError(f"transition {raw!r} must be '<name> <duration>', e.g. {spec.write!r}")
    if tokens[0] not in words or words[tokens[0]] is None:
        allowed = ", ".join(name for name, value in words.items() if value)
        raise ClipFieldError(f"transition {tokens[0]!r} is unknown; allowed: {allowed}, cut")
    value = {"id": words[tokens[0]]}
    if len(tokens) == 2:
        value["durationFrames"] = _frames(tokens[1], fps, "transition duration")
    return value


def _coerce_one(spec: FieldSpec, raw: Any, *, fps: float, audio: bool) -> tuple[str, Any]:
    if audio and spec.applies_to == "visual":
        raise ClipFieldError(f"{spec.name} is a visual field; an audio clip cannot take it")
    if not audio and spec.applies_to == "audio":
        raise ClipFieldError(f"{spec.name} is an audio field; a visual clip cannot take it")
    if spec.kind == "fade":
        if audio and spec.stored_at_audio:
            return spec.stored_at_audio, _audio_fade_value(spec, raw, fps)
        return spec.stored_at, _visual_fade_value(spec, raw, fps)
    if spec.kind == "animation":
        return spec.stored_at, _animation_value(spec, raw, fps)
    if spec.kind == "transition":
        return spec.stored_at, _transition_value(spec, raw, fps)
    if spec.kind == "gain":
        return spec.stored_at, _gain(raw, spec)
    if spec.kind == "number":
        return spec.stored_at, _number(raw, spec)
    raise ClipFieldError(f"{spec.name}: unsupported field type {spec.kind!r}")


def coerce(name: str, raw: Any, *, fps: float = FPS_DEFAULT, audio: bool = False) -> tuple[str, Any]:
    """Return ``(stored dotted path, value)``; a None value removes the key."""
    return _coerce_one(_spec(name), raw, fps=fps, audio=audio)


def _crop_sums(values: Mapping[str, Any]) -> None:
    for left, right in (("cropLeft", "cropRight"), ("cropTop", "cropBottom")):
        total = float(values.get(left) or 0) + float(values.get(right) or 0)
        if total > _CROP_SUM_MAX:
            raise ClipFieldError(f"{left} + {right} = {total:g} is above {_CROP_SUM_MAX:g}; the frame would be empty")


def coerce_many(values: Mapping[str, Any], *, fps: float = FPS_DEFAULT, audio: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    owners: dict[str, str] = {}
    for name, raw in values.items():
        path, stored = coerce(name, raw, fps=fps, audio=audio)
        if path in owners:
            raise ClipFieldError(f"{owners[path]} and {name} both set {path}; give one")
        owners[path] = name
        out[path] = stored
    crop_values = {path: out[path] for path in out if path.startswith("crop")}
    if crop_values:
        _crop_sums({**dict.fromkeys(("cropLeft", "cropRight", "cropTop", "cropBottom"), 0), **crop_values})
    return out


def _get(clip: Mapping[str, Any], path: str) -> Any:
    node: Any = clip
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _set(clip: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = clip
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = node[part] = {}
        if not isinstance(child, dict):
            raise ClipFieldError(f"{path}: {part!r} is not an object in this clip")
        node = child
    node[parts[-1]] = value


def _delete(clip: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    node: Any = clip
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return
        node = node[part]
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def _path(spec: FieldSpec, audio: bool) -> str:
    return spec.stored_at_audio if audio and spec.stored_at_audio else spec.stored_at


def read(clip_data: Mapping[str, Any], name: str, *, fps: float = FPS_DEFAULT, audio: bool = False) -> Any:
    spec = _spec(name)
    if audio and spec.applies_to == "visual":
        raise ClipFieldError(f"{name} is a visual field; an audio clip has no {name}")
    if not audio and spec.applies_to == "audio":
        raise ClipFieldError(f"{name} is an audio field; a visual clip has no {name}")
    return _get(clip_data, _path(spec, audio))


def write(clip_data: dict[str, Any], name: str, value: Any, *, fps: float = FPS_DEFAULT, audio: bool = False) -> None:
    spec = _spec(name)
    path, stored = (_path(spec, audio), None) if value is None else _coerce_one(spec, value, fps=fps, audio=audio)
    if path.startswith("crop"):
        current = {key: _get(clip_data, key) for key in ("cropLeft", "cropRight", "cropTop", "cropBottom")}
        current[path] = stored
        _crop_sums(current)
    if stored is None:
        _delete(clip_data, path)
    else:
        _set(clip_data, path, stored)
    if audio and spec.stored_at_audio and spec.kind == "fade":
        _duration, curve = _audio_fade_parts(value)
        curve_path = "params.fadeInCurve" if name == "fade_in" else "params.fadeOutCurve"
        if stored is None or curve == "linear":
            _delete(clip_data, curve_path)
        else:
            _set(clip_data, curve_path, curve)


def _human(spec: FieldSpec, value: Any, fps: float) -> str | None:
    if value is None or value == spec.default:
        return None
    if spec.kind == "fade":
        if not isinstance(value, Mapping):
            return _fmt_seconds(float(value), fps)
        parts = [] if value.get("duration") is None else [_fmt_seconds(float(value["duration"]), fps)]
        easing = _stored_easing(value)
        if easing:
            parts.append(f"ease={easing}")
        return " ".join(parts) or None
    if spec.kind == "animation":
        if not isinstance(value, Mapping):
            return str(value)
        parts = [str(value.get("type"))]
        if value.get("duration") is not None:
            parts.append(_fmt_seconds(float(value["duration"]), fps))
        easing = _stored_easing(value)
        if easing:
            parts.append(f"ease={easing}")
        return " ".join(parts)
    if spec.kind == "transition":
        words = {value: name for name, value in spec.values.items() if value}
        name = words.get(value.get("id"), value.get("id"))
        frames = value.get("durationFrames")
        return f"{name} {int(frames)}f" if frames is not None else str(name)
    if spec.kind == "gain":
        gain = float(value)
        return "mute" if gain == 0 else f"{20 * math.log10(gain):.1f}dB"
    if spec.kind == "number":
        return f"{float(value):g}{spec.suffix or ''}"
    return str(value)


def describe(clip: Mapping[str, Any], *, fps: float = FPS_DEFAULT) -> dict[str, str]:
    """Human forms of non-default fields, retaining audio fades in their nested V3 path."""
    out: dict[str, str] = {}
    for name, spec in FIELDS.items():
        if spec.stored_at_audio and name in ("fade_in", "fade_out"):
            value = _get(clip, spec.stored_at_audio)
            text = None if value is None else _fmt_seconds(float(value), fps)
            curve = _get(clip, "params.fadeInCurve" if name == "fade_in" else "params.fadeOutCurve")
            if text is not None and curve and curve != "linear":
                text += f" curve={curve}"
        else:
            text = _human(spec, _get(clip, spec.stored_at), fps)
        if text is not None:
            out[name] = text
    return out


def _version(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(text).split("."))
    except ValueError:
        return (0,)


@lru_cache(maxsize=1)
def _composition_version() -> str:
    lock = _PACKS_ROOT.parent.parent / "remotion" / "package-lock.json"
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
        version = data.get("packages", {}).get("node_modules/@banodoco/timeline-composition", {}).get("version")
        return str(version or "0.0.6")
    except (OSError, ValueError, AttributeError):
        return "0.0.6"


def render_status(element_kind: str, name: str, composition_version: str | None = None) -> str:
    """Return ``renders``, ``unrendered`` or ``unknown`` for a V3 clip kind."""
    spec = FIELDS.get(name)
    if spec is None:
        return "unknown"
    entry = spec.render.get(element_kind)
    if entry is None:
        kind = element_kind if element_kind in ("media", "audio") else "element"
        entry = spec.render.get(kind)
    if entry is None:
        return "unknown"
    if entry.get("status") != "works":
        return "unrendered"
    since = entry.get("since", "0.0.0")
    current = composition_version or _composition_version()
    return "renders" if _version(current) >= _version(str(since)) else "unrendered"
