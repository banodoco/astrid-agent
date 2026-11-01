"""Validation and deterministic normalization for the public H3 request."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class H3RequestError(ValueError):
    """A fail-closed request error with a user-facing path."""


@dataclass(frozen=True)
class H3Request:
    value: dict[str, Any]
    digest: str


_OPERATIONS = {"edit", "continue", "generate"}
_ACTIONS = {"generate", "preserve"}
_PURPOSES = {"appearance", "motion", "pose", "keyframe", "audio", "style"}
_STEMS = {"mix", "voice", "effects", "music"}
_OVERRIDES = {"model", "steps", "seed", "sampler", "guidance"}


def _asset_ids(request: H3Request) -> list[str]:
    value = request.value
    if value.get("version") == 2:
        result = [str(item["asset"]) for item in value["media"]]
        for item in value["media"]:
            for edit in item.get("edit", []):
                mask = edit.get("mask", {})
                if isinstance(mask, Mapping) and "asset" in mask:
                    result.append(str(mask["asset"]))
        return list(dict.fromkeys(result))
    result: list[str] = []
    source = value.get("source")
    if isinstance(source, Mapping):
        result.append(source["asset"])
    result.extend(reference["asset"] for reference in value["references"])
    for domain in ("video", "audio"):
        for change in value["changes"][domain]:
            if "mask_asset" in change:
                result.append(change["mask_asset"])
            area = change.get("area", {})
            if "mask_asset" in area:
                result.append(area["mask_asset"])
    return list(dict.fromkeys(result))


def _object(value: Any, path: str, *, nullable: bool = False) -> dict[str, Any] | None:
    if value is None and nullable:
        return None
    if not isinstance(value, Mapping):
        raise H3RequestError(f"{path} must be an object")
    return dict(value)


def _unknown(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise H3RequestError(f"{path} contains unsupported field(s): {', '.join(unknown)}")


def _nonblank(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise H3RequestError(f"{path} must be a non-empty string")
    return value.strip()


def _number(value: Any, path: str, *, minimum: float | None = None, exclusive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise H3RequestError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise H3RequestError(f"{path} must be finite")
    if minimum is not None and (result <= minimum if exclusive else result < minimum):
        operator = ">" if exclusive else ">="
        raise H3RequestError(f"{path} must be {operator} {minimum}")
    return result


def _interval(value: Any, path: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise H3RequestError(f"{path} must be [start, end]")
    start = _number(value[0], f"{path}[0]", minimum=0)
    end = _number(value[1], f"{path}[1]", minimum=0)
    if end <= start:
        raise H3RequestError(f"{path} must have end > start")
    return [start, end]


def _area(value: Any, path: str) -> dict[str, Any]:
    area = _object(value, path) or {}
    allowed = {"full_frame", "mask_asset", "subjects", "rectangle", "polygon"}
    _unknown(area, allowed, path)
    if not area:
        raise H3RequestError(f"{path} must select a region")
    if area.get("full_frame") is True:
        if len(area) != 1:
            raise H3RequestError(f"{path}.full_frame cannot be combined with another selector")
        return {"full_frame": True}
    if area.get("full_frame") not in (None, False):
        raise H3RequestError(f"{path}.full_frame must be true when present")
    selectors = [key for key in ("mask_asset", "subjects", "rectangle", "polygon") if key in area]
    if len(selectors) != 1:
        raise H3RequestError(f"{path} must contain exactly one non-full-frame selector")
    selector = selectors[0]
    if selector == "mask_asset":
        result = {selector: _nonblank(area[selector], f"{path}.mask_asset")}
    elif selector == "subjects":
        subjects = area[selector]
        if not isinstance(subjects, list) or not subjects or not all(isinstance(item, str) and item.strip() for item in subjects):
            raise H3RequestError(f"{path}.subjects must be a non-empty list of strings")
        result = {selector: [item.strip() for item in subjects]}
    elif selector == "rectangle":
        points = area[selector]
        if not isinstance(points, list) or len(points) != 4:
            raise H3RequestError(f"{path}.rectangle must be [x, y, width, height]")
        result = {selector: [_number(item, f"{path}.rectangle[{index}]") for index, item in enumerate(points)]}
    else:
        points = area[selector]
        if not isinstance(points, list) or len(points) < 3:
            raise H3RequestError(f"{path}.polygon must contain at least three points")
        result = {selector: [[_number(coord, f"{path}.polygon[{index}][{axis}]") for axis, coord in enumerate(point)] for index, point in enumerate(points)]}
    return result


def _video_change(value: Any, index: int) -> dict[str, Any]:
    path = f"changes.video[{index}]"
    item = _object(value, path) or {}
    _unknown(item, {"during", "area", "action", "mask_asset"}, path)
    result: dict[str, Any] = {
        "during": _interval(item.get("during"), f"{path}.during"),
        "area": _area(item.get("area"), f"{path}.area"),
        "action": item.get("action"),
    }
    if result["action"] not in _ACTIONS:
        raise H3RequestError(f"{path}.action must be generate or preserve")
    if "mask_asset" in item:
        result["mask_asset"] = _nonblank(item["mask_asset"], f"{path}.mask_asset")
    return result


def _audio_change(value: Any, index: int) -> dict[str, Any]:
    path = f"changes.audio[{index}]"
    item = _object(value, path) or {}
    _unknown(item, {"during", "action", "dialogue", "mask_asset", "stem"}, path)
    result: dict[str, Any] = {
        "during": _interval(item.get("during"), f"{path}.during"),
        "action": item.get("action"),
    }
    if result["action"] not in _ACTIONS:
        raise H3RequestError(f"{path}.action must be generate or preserve")
    for field in ("dialogue", "mask_asset"):
        if field in item:
            result[field] = _nonblank(item[field], f"{path}.{field}")
    if "stem" in item:
        stem = item["stem"]
        if stem not in _STEMS:
            raise H3RequestError(f"{path}.stem must be one of {sorted(_STEMS)}")
        result["stem"] = stem
    return result


def _normalize_v1(raw: Mapping[str, Any]) -> H3Request:
    if not isinstance(raw, Mapping):
        raise H3RequestError("request must be an object")
    _unknown(raw, {"version", "operation", "source", "output", "content", "changes", "references", "overrides"}, "request")
    if raw.get("version") != 1:
        raise H3RequestError("version must be 1")
    operation = raw.get("operation")
    if operation not in _OPERATIONS:
        raise H3RequestError("operation must be edit, continue, or generate")

    source_raw = _object(raw.get("source"), "source", nullable=True)
    source: dict[str, Any] | None = None
    if source_raw is not None:
        _unknown(source_raw, {"asset", "range"}, "source")
        source = {"asset": _nonblank(source_raw.get("asset"), "source.asset")}
        if "range" in source_raw:
            source["range"] = _interval(source_raw["range"], "source.range")
    if operation in {"edit", "continue"} and source is None:
        raise H3RequestError(f"source is required for {operation}")
    if operation == "generate" and source is not None:
        raise H3RequestError("source must be omitted or null for generate")
    output_raw = _object(raw.get("output", {}), "output") or {}
    _unknown(output_raw, {"duration"}, "output")
    output: dict[str, Any] = {}
    if "duration" in output_raw:
        output["duration"] = _number(output_raw["duration"], "output.duration", minimum=0, exclusive=True)

    content_raw = _object(raw.get("content"), "content") or {}
    _unknown(content_raw, {"prompt"}, "content")
    content = {"prompt": _nonblank(content_raw.get("prompt"), "content.prompt")}

    changes_raw = _object(raw.get("changes"), "changes") or {}
    _unknown(changes_raw, {"video", "audio"}, "changes")
    video = changes_raw.get("video", [])
    audio = changes_raw.get("audio", [])
    if not isinstance(video, list) or not isinstance(audio, list):
        raise H3RequestError("changes.video and changes.audio must be arrays")
    changes = {
        "video": [_video_change(item, index) for index, item in enumerate(video)],
        "audio": [_audio_change(item, index) for index, item in enumerate(audio)],
    }

    references_raw = raw.get("references", [])
    if not isinstance(references_raw, list):
        raise H3RequestError("references must be an array")
    references: list[dict[str, str]] = []
    for index, value in enumerate(references_raw):
        path = f"references[{index}]"
        item = _object(value, path) or {}
        _unknown(item, {"asset", "purpose"}, path)
        purpose = item.get("purpose")
        if purpose not in _PURPOSES:
            raise H3RequestError(f"{path}.purpose must be one of {sorted(_PURPOSES)}")
        references.append({"asset": _nonblank(item.get("asset"), f"{path}.asset"), "purpose": purpose})

    if operation == "generate":
        from .generation import IMAGE_REFERENCE_CAPACITY, generation_timing

        if "duration" not in output:
            raise H3RequestError("output.duration is required for generate")
        try:
            generation_timing(output["duration"])
        except ValueError as exc:
            raise H3RequestError(str(exc)) from exc
        if changes["video"] or changes["audio"]:
            raise H3RequestError("generate creates the full audiovisual timeline; changes/masks require a source-backed operation")
        if not 1 <= len(references) <= IMAGE_REFERENCE_CAPACITY:
            raise H3RequestError("generate requires one to nine image references")
        if any(item["purpose"] in {"audio", "keyframe"} for item in references):
            raise H3RequestError("generate supports image guidance, not audio references or timed keyframes")

    overrides_raw = _object(raw.get("overrides", {}), "overrides") or {}
    _unknown(overrides_raw, _OVERRIDES, "overrides")
    overrides: dict[str, Any] = {}
    if "model" in overrides_raw:
        overrides["model"] = _nonblank(overrides_raw["model"], "overrides.model")
    if "steps" in overrides_raw:
        steps = overrides_raw["steps"]
        if type(steps) is not int or not 1 <= steps <= 200:
            raise H3RequestError("overrides.steps must be an integer from 1 to 200")
        overrides["steps"] = steps
    if "seed" in overrides_raw:
        seed = overrides_raw["seed"]
        if type(seed) is not int or seed < 0:
            raise H3RequestError("overrides.seed must be a non-negative integer")
        overrides["seed"] = seed
    if "sampler" in overrides_raw:
        overrides["sampler"] = _nonblank(overrides_raw["sampler"], "overrides.sampler")
    if "guidance" in overrides_raw:
        overrides["guidance"] = _number(overrides_raw["guidance"], "overrides.guidance", minimum=0)

    value = {
        "version": 1,
        "operation": operation,
        "source": source,
        "output": output,
        "content": content,
        "changes": changes,
        "references": references,
        "overrides": overrides,
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return H3Request(value=value, digest=hashlib.sha256(encoded).hexdigest())


def normalize_request(
    raw: Mapping[str, Any], *, asset_modalities: Mapping[str, str] | None = None
) -> H3Request:
    """Normalize either the retained parent-v1 or native-v2 contract.

    The v1 branch intentionally remains unchanged, including source-free
    generation. Native-v2 is a separate media-list contract and is imported
    lazily so the parent-v1 path has no new dependency surface.
    """
    if not isinstance(raw, Mapping):
        raise H3RequestError("request must be an object")
    if raw.get("version") == 2:
        from .request_v2 import normalize_v2

        return normalize_v2(raw, asset_modalities=asset_modalities)
    return _normalize_v1(raw)


def load_request_document(path: str | Path) -> dict[str, Any]:
    """Read the caller's JSON/YAML mapping without applying normalization.

    Orchestrators that pass a request to a child must preserve the caller's
    document. The normalized v2 form contains derived fields that are not
    valid in a fresh public request.
    """
    source = Path(path).expanduser().resolve(strict=True)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise H3RequestError(f"cannot read request {source}: {exc}") from exc
    try:
        if source.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore
            except ImportError as exc:
                raise H3RequestError("YAML requests require PyYAML; use JSON or install PyYAML") from exc
            raw = yaml.safe_load(text)
        else:
            raw = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise H3RequestError(f"request is not valid JSON/YAML: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise H3RequestError("request must be an object")
    return dict(raw)


def load_request(
    path: str | Path, *, asset_modalities: Mapping[str, str] | None = None
) -> H3Request:
    return normalize_request(
        load_request_document(path), asset_modalities=asset_modalities
    )
