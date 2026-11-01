"""Native-v2 media-list normalization kept beside the parent-v1 contract."""

from __future__ import annotations

import hashlib
import json
import math
import re
from fractions import Fraction
from typing import Any, Mapping

from .request import H3Request, H3RequestError, _nonblank, _number, _object, _unknown

FPS = Fraction(24, 1)
SAMPLE_RATE = 48000
PROFILE = "h3_av.native.v2"
REF2VA_MODEL = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
CHARACTER_SWAP_ID = "character_swap_v1"
_NATIVE_SAMPLERS = frozenset({
    "euler", "euler_cfg_pp", "euler_ancestral", "euler_ancestral_cfg_pp",
    "heun", "heunpp2", "exp_heun_2_x0", "exp_heun_2_x0_sde", "dpm_2",
    "dpm_2_ancestral", "lms", "dpm_fast", "dpm_adaptive",
    "dpmpp_2s_ancestral", "dpmpp_2s_ancestral_cfg_pp", "dpmpp_sde",
    "dpmpp_sde_gpu", "dpmpp_2m", "dpmpp_2m_cfg_pp", "dpmpp_2m_sde",
    "dpmpp_2m_sde_gpu", "dpmpp_2m_sde_heun", "dpmpp_2m_sde_heun_gpu",
    "dpmpp_3m_sde", "dpmpp_3m_sde_gpu", "ddpm", "lcm", "ipndm",
    "ipndm_v", "deis", "res_multistep", "res_multistep_cfg_pp",
    "res_multistep_ancestral", "res_multistep_ancestral_cfg_pp",
    "gradient_estimation", "gradient_estimation_cfg_pp", "er_sde",
    "seeds_2", "seeds_3", "sa_solver", "sa_solver_pece", "ddim",
    "uni_pc", "uni_pc_bh2",
})
_SUPPORTED_SAMPLERS = frozenset(name for name in _NATIVE_SAMPLERS if not name.endswith("cfg_pp"))
_NATIVE_SCHEDULERS = frozenset({
    "normal", "karras", "exponential", "sgm_uniform", "simple",
    "ddim_uniform", "beta", "linear_quadratic", "kl_optimal",
})
_MODALITIES = {"image", "video", "audio"}
_ROLES = {"timeline", "reference"}
_PLACEHOLDER = re.compile(r"(?:\{\{?[^{}]+\}?\}|<[^>]+>|\b(?:TODO|FIXME|PLACEHOLDER)\b)", re.I)


def _fraction(value: Any, path: str, *, minimum: Fraction = Fraction(0)) -> Fraction:
    if isinstance(value, bool):
        raise H3RequestError(f"{path} must be a rational number")
    try:
        if isinstance(value, int):
            result = Fraction(value)
        elif isinstance(value, float) and math.isfinite(value):
            result = Fraction(str(value))
        elif isinstance(value, str) and re.fullmatch(r"\s*[+-]?\d+(?:/\d+)?\s*", value):
            result = Fraction(value.replace(" ", ""))
        else:
            raise ValueError
    except (ValueError, ZeroDivisionError):
        raise H3RequestError(f"{path} must be a rational number") from None
    if result < minimum:
        raise H3RequestError(f"{path} must be >= {float(minimum):g}")
    return result


def _json_number(value: Fraction) -> int | float:
    return value.numerator if value.denominator == 1 else float(value)


def _text(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _ticks(value: Fraction, clock: Fraction, path: str) -> int:
    resolved = value * clock
    if resolved.denominator != 1:
        raise H3RequestError(f"{path} is not representable on the native clock")
    return resolved.numerator


def _interval(value: Any, path: str, clock: Fraction) -> tuple[list[int | float], list[int]]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise H3RequestError(f"{path} must be [start, end]")
    start = _fraction(value[0], f"{path}[0]")
    end = _fraction(value[1], f"{path}[1]")
    if end <= start:
        raise H3RequestError(f"{path} must have end > start")
    return [_json_number(start), _json_number(end)], [_ticks(start, clock, f"{path}[0]"), _ticks(end, clock, f"{path}[1]")]


def _asset(value: Any, path: str, modalities: Mapping[str, str] | None) -> tuple[str, str | None]:
    declared: str | None = None
    if isinstance(value, Mapping):
        _unknown(value, {"id", "asset", "modality", "inspection"}, path)
        raw = value.get("id", value.get("asset"))
        declared = value.get("modality")
        inspection = value.get("inspection")
        inspected = inspection.get("modality") if isinstance(inspection, Mapping) else None
        if declared is not None and inspected is not None and declared != inspected:
            raise H3RequestError(f"{path}.modality contradicts asset inspection")
        declared = declared or inspected
    else:
        raw = value
    asset_id = _nonblank(raw, path)
    inspected = modalities.get(asset_id) if modalities is not None else None
    if declared is not None and inspected is not None and declared != inspected:
        raise H3RequestError(f"{path}.modality contradicts asset inspection")
    modality = declared or inspected
    if modality is not None and modality not in _MODALITIES:
        raise H3RequestError(f"{path}.modality must be image, video, or audio")
    return asset_id, modality


def _at(value: Any, path: str) -> tuple[dict[str, int | float], int]:
    item = _object(value, path) or {}
    _unknown(item, {"frame", "seconds"}, path)
    if len(item) != 1:
        raise H3RequestError(f"{path} must contain exactly one of frame or seconds")
    if "frame" in item:
        frame = _fraction(item["frame"], f"{path}.frame")
        if frame.denominator != 1:
            raise H3RequestError(f"{path}.frame must be an integer")
        return {"frame": frame.numerator}, frame.numerator
    seconds = _fraction(item["seconds"], f"{path}.seconds")
    return {"seconds": _json_number(seconds)}, _ticks(seconds, FPS, f"{path}.seconds")


def _mask(value: Any, path: str, modalities: Mapping[str, str] | None) -> dict[str, Any]:
    if isinstance(value, str):
        asset, modality = _asset(value, path, modalities)
        if modality not in (None, "image", "video"):
            raise H3RequestError(f"{path} must reference an image/video mask")
        return {"asset": asset, "polarity": "black_preserve_white_edit"}
    item = _object(value, path) or {}
    _unknown(item, {"asset", "full_frame", "rectangle", "polygon", "range", "polarity", "shape"}, path)
    geometry = [key for key in ("full_frame", "rectangle", "polygon") if key in item]
    if "asset" in item and geometry:
        raise H3RequestError(f"{path} cannot combine an asset with geometry")
    if "asset" not in item and not geometry:
        raise H3RequestError(f"{path} must contain an asset or geometry")
    result: dict[str, Any] = {}
    if "asset" in item:
        asset, modality = _asset(item["asset"], f"{path}.asset", modalities)
        if modality not in (None, "image", "video"):
            raise H3RequestError(f"{path}.asset must reference an image/video mask")
        result["asset"] = asset
    elif item.get("full_frame") is True:
        result["full_frame"] = True
    elif "rectangle" in item:
        values = item["rectangle"]
        if not isinstance(values, list) or len(values) != 4:
            raise H3RequestError(f"{path}.rectangle must be [x, y, width, height]")
        result["rectangle"] = [_number(value, f"{path}.rectangle[{i}]") for i, value in enumerate(values)]
    else:
        points = item["polygon"]
        if not isinstance(points, list) or len(points) < 3:
            raise H3RequestError(f"{path}.polygon must contain at least three points")
        result["polygon"] = [[_number(coord, f"{path}.polygon[{i}][{axis}]") for axis, coord in enumerate(point)] for i, point in enumerate(points)]
    if "range" in item:
        raw_range = item["range"]
        if not isinstance(raw_range, list) or len(raw_range) != 2 or any(type(value) is not int for value in raw_range) or raw_range[0] < 0 or raw_range[1] <= raw_range[0]:
            raise H3RequestError(f"{path}.range must be a non-negative integer frame interval")
        result["range"] = list(raw_range)
        result["resolved_range"] = list(raw_range)
    if item.get("polarity", "black_preserve_white_edit") not in {"black_preserve_white_edit", "black=preserve_white=editable", "black_protected_white_editable"}:
        raise H3RequestError(f"{path}.polarity must be black-preserve/white-edit")
    result["polarity"] = "black_preserve_white_edit"
    return result


def _settings(value: Any) -> dict[str, Any]:
    item = _object(value, "settings") or {}
    _unknown(item, {"model", "steps", "sampler", "scheduler", "seed", "turbo", "adapter", "guidance_scale", "guidance"}, "settings")
    aliases = [name for name in ("guidance", "guidance_scale") if name in item]
    if len(aliases) > 1:
        raise H3RequestError("settings.guidance and settings.guidance_scale are duplicate Turbo aliases")
    turbo = _object(item.get("turbo", {}), "settings.turbo") or {}
    _unknown(turbo, {"enabled", "strength"}, "settings.turbo")
    if aliases and "strength" in turbo:
        raise H3RequestError("legacy guidance and settings.turbo.strength are duplicate spellings")
    result: dict[str, Any] = {
        "model": REF2VA_MODEL,
        "steps": 8,
        "sampler": "res_multistep",
        "scheduler": "simple",
        "seed": 123456789,
        "turbo": {"enabled": True, "strength": 0.95},
    }
    if "model" in item: result["model"] = _nonblank(item["model"], "settings.model")
    if "sampler" in item: result["sampler"] = _nonblank(item["sampler"], "settings.sampler")
    if result["sampler"] not in _NATIVE_SAMPLERS:
        raise H3RequestError("settings.sampler is not supported by the native KSamplerSelect contract")
    if result["sampler"] not in _SUPPORTED_SAMPLERS:
        raise H3RequestError("settings.sampler requires unconditional CFG predictions; H3 uses BasicGuider, not CFG")
    if "scheduler" in item: result["scheduler"] = _nonblank(item["scheduler"], "settings.scheduler")
    if result["scheduler"] not in _NATIVE_SCHEDULERS:
        raise H3RequestError("settings.scheduler is not supported by the native BasicScheduler contract")
    if "steps" in item and (type(item["steps"]) is not int or not 1 <= item["steps"] <= 200): raise H3RequestError("settings.steps must be an integer from 1 to 200")
    if "steps" in item: result["steps"] = item["steps"]
    if "seed" in item and (type(item["seed"]) is not int or not 0 <= item["seed"] <= 2**64 - 1): raise H3RequestError("settings.seed must be an unsigned 64-bit integer")
    if "seed" in item: result["seed"] = item["seed"]
    if "enabled" in turbo:
        if type(turbo["enabled"]) is not bool:
            raise H3RequestError("settings.turbo.enabled must be boolean")
        result["turbo"]["enabled"] = turbo["enabled"]
    if aliases:
        alias = aliases[0]
        result["turbo"]["strength"] = _number(item[alias], f"settings.{alias}", minimum=0)
    elif "strength" in turbo:
        result["turbo"]["strength"] = _number(turbo["strength"], "settings.turbo.strength", minimum=0)
    if "adapter" in item:
        adapter = _object(item["adapter"], "settings.adapter") or {}
        _unknown(adapter, {"id", "strength"}, "settings.adapter")
        if adapter.get("id") != CHARACTER_SWAP_ID:
            raise H3RequestError(f"settings.adapter.id must be {CHARACTER_SWAP_ID!r}")
        strength = _number(adapter.get("strength", 1.0), "settings.adapter.strength", minimum=0)
        if strength > 2:
            raise H3RequestError("settings.adapter.strength must be in the range 0..2")
        if result["model"] != REF2VA_MODEL:
            raise H3RequestError(f"settings.adapter {CHARACTER_SWAP_ID} requires Ref2VA model {REF2VA_MODEL!r}; the selected model is not changed")
        result["adapter"] = {"id": CHARACTER_SWAP_ID, "strength": strength}
    if not result["turbo"]["enabled"] and result["model"] != REF2VA_MODEL:
        raise H3RequestError("settings.turbo.enabled=false requires the supported native Ref2VA base; no non-Turbo recipe is declared for this model")
    return result


def effective_settings(request: H3Request) -> dict[str, Any]:
    """Project canonical execution settings without rewriting sealed requests."""
    if request.value.get("version") != 2:
        raise H3RequestError("canonical effective settings require an H3 v2 request")
    settings = _settings(request.value.get("settings", {}))
    media = request.value.get("media")
    if "adapter" in settings:
        if not isinstance(media, list):
            raise H3RequestError("character_swap_v1 requires normalized v2 media")
        _validate_character_swap_media(settings, media, request.value.get("continuation", False))
    return settings


def _validate_character_swap_media(settings: Mapping[str, Any], media: list[dict[str, Any]], continuation: bool) -> None:
    if "adapter" not in settings:
        return
    if not isinstance(media, list) or any(not isinstance(item, Mapping) for item in media):
        raise H3RequestError("character_swap_v1 requires normalized v2 media")
    references = [item for item in media if item.get("role") == "reference"]
    if len(references) != 2 or sorted(str(item.get("modality")) for item in references) != ["image", "video"]:
        raise H3RequestError("character_swap_v1 requires exactly one typed video reference and one typed replacement image reference")
    if continuation:
        raise H3RequestError("character_swap_v1 does not support continuation")
    timeline = [item for item in media if item.get("role") == "timeline"]
    if any(item.get("modality") == "audio" for item in timeline) and not any(item.get("modality") == "video" for item in timeline):
        raise H3RequestError("character_swap_v1 does not support an audio-only timeline")
    videos = [item for item in timeline if item.get("modality") == "video"]
    if any(item.get("modality") != "video" for item in timeline):
        raise H3RequestError("character_swap_v1 supports at most one video timeline source; separate timeline audio or image anchors are unsupported")
    if len(videos) > 1:
        raise H3RequestError("character_swap_v1 supports at most one timeline video source")
    if videos:
        source = videos[0]
        resolved_at = source.get("resolved_at")
        if not isinstance(resolved_at, Mapping) or resolved_at.get("value") != 0:
            raise H3RequestError("character_swap_v1 requires its timeline video at frame 0")
        if not isinstance(source.get("resolved_range"), list) or len(source["resolved_range"]) != 2:
            raise H3RequestError("character_swap_v1 requires a bounded timeline video range")
        edits = source.get("edit", [])
        if not isinstance(edits, list) or not any(
            isinstance(edit, Mapping) and edit.get("stream") == "video" for edit in edits
        ):
            raise H3RequestError("character_swap_v1 does not support the implicit continuation route; declare the timeline's video edit permissions")


def normalize_v2(raw: Mapping[str, Any], *, asset_modalities: Mapping[str, str] | None = None) -> H3Request:
    _unknown(raw, {"version", "prompt", "duration", "media", "settings", "continuation"}, "request")
    if raw.get("version") != 2: raise H3RequestError("version must be 2")
    prompt = _nonblank(raw.get("prompt"), "prompt")
    media_raw = raw.get("media")
    if not isinstance(media_raw, list) or not media_raw: raise H3RequestError("media must be a non-empty array")
    continuation = raw.get("continuation", False)
    if type(continuation) is not bool: raise H3RequestError("continuation must be boolean")
    duration_fraction = (
        None if raw.get("duration") is None else _fraction(raw["duration"], "duration")
    )
    if duration_fraction == 0: raise H3RequestError("duration must be > 0 when supplied")
    duration = None if duration_fraction is None else _json_number(duration_fraction)
    occurrences: list[dict[str, Any]] = []
    ids: set[str] = set()
    reference_counts = {"image": 0, "video": 0, "audio": 0}
    for index, raw_item in enumerate(media_raw):
        path = f"media[{index}]"
        item = _object(raw_item, path) or {}
        _unknown(item, {"id", "asset", "role", "modality", "at", "range", "edit", "audio", "hard", "latent_pin"}, path)
        role = item.get("role")
        if role not in _ROLES: raise H3RequestError(f"{path}.role must be timeline or reference")
        asset, inspected = _asset(item.get("asset"), f"{path}.asset", asset_modalities)
        modality = item.get("modality", inspected)
        if modality is not None and modality not in _MODALITIES: raise H3RequestError(f"{path}.modality must be image, video, or audio")
        identifier = _nonblank(item["id"], f"{path}.id") if "id" in item else f"occurrence-{index + 1}"
        if identifier in ids: raise H3RequestError(f"{path}.occurrence_id {identifier!r} is duplicated")
        ids.add(identifier)
        occurrence: dict[str, Any] = {"occurrence_id": identifier, "asset": asset, "role": role}
        if "id" in item: occurrence["id"] = identifier
        if modality is not None: occurrence["modality"] = modality
        else: occurrence["inspection"] = "deferred"
        if role == "reference":
            if any(key in item for key in ("at", "edit", "hard", "latent_pin")): raise H3RequestError(f"{path} reference entries cannot contain placement or edit fields")
            if modality in {"image", "video", "audio"}:
                reference_counts[modality] += 1
                label = {"image": "Picture", "video": "Video", "audio": "Audio"}[modality]
                occurrence["model_tag"] = f"<{label} {reference_counts[modality]}>"
            if "audio" in item:
                if modality != "video" or type(item["audio"]) is not bool: raise H3RequestError(f"{path}.audio is only valid for video references")
                occurrence["audio"] = item["audio"]
            elif modality == "video": occurrence["audio"] = True
            if "range" in item:
                if modality != "video": raise H3RequestError(f"{path}.range is unsupported for {modality or 'unresolved'} references")
                occurrence["range"], occurrence["resolved_range"] = _interval(item["range"], f"{path}.range", FPS)
        else:
            if "at" not in item: raise H3RequestError(f"{path}.at is required for timeline media")
            at, at_frame = _at(item["at"], f"{path}.at")
            occurrence.update({"at": at, "resolved_at": {"unit": "frames", "value": at_frame}, "hard": item.get("hard", False), "edit": []})
            if type(occurrence["hard"]) is not bool: raise H3RequestError(f"{path}.hard must be boolean")
            if "range" in item:
                if modality not in {"video", "audio"}:
                    raise H3RequestError(f"{path}.range is unsupported for {modality or 'unresolved'} timeline media")
                clock = FPS if modality == "video" else Fraction(SAMPLE_RATE)
                occurrence["range"], occurrence["resolved_range"] = _interval(item["range"], f"{path}.range", clock)
            edits = item.get("edit", [])
            if not isinstance(edits, list): raise H3RequestError(f"{path}.edit must be an array")
            for edit_index, raw_edit in enumerate(edits):
                edit_path = f"{path}.edit[{edit_index}]"
                edit = _object(raw_edit, edit_path) or {}
                _unknown(edit, {"stream", "during", "mask", "guides", "text", "dialogue", "channels", "action", "keep"}, edit_path)
                stream = edit.get("stream")
                if stream not in {"video", "audio"}: raise H3RequestError(f"{edit_path}.stream must be video or audio")
                action = "preserve" if edit.get("keep") is True else edit.get("action", "generate")
                if action not in {"generate", "preserve"}: raise H3RequestError(f"{edit_path}.action must be generate or preserve")
                clock = FPS if stream == "video" else Fraction(SAMPLE_RATE)
                if "during" not in edit: raise H3RequestError(f"{edit_path}.during is required")
                during, resolved = _interval(edit["during"], f"{edit_path}.during", clock)
                normalized: dict[str, Any] = {"stream": stream, "during": during, "resolved": {"unit": "frames" if stream == "video" else "samples", "range": resolved}, "action": action}
                if stream == "video": normalized["mask"] = _mask(edit.get("mask", {"full_frame": True}), f"{edit_path}.mask", asset_modalities)
                if stream == "audio": normalized["channels"] = edit.get("channels", "all")
                if "text" in edit or "dialogue" in edit:
                    text = _nonblank(edit.get("text", edit.get("dialogue")), f"{edit_path}.text")
                    if _PLACEHOLDER.search(text): raise H3RequestError(f"{edit_path}.text must be literal dialogue; unresolved placeholder found")
                    normalized["text"] = text
                if "guides" in edit:
                    guides = edit["guides"] if isinstance(edit["guides"], list) else [edit["guides"]]
                    normalized["guides"] = [_nonblank(guide, f"{edit_path}.guides[{i}]") for i, guide in enumerate(guides)]
                occurrence["edit"].append(normalized)
        occurrences.append(occurrence)
    by_id = {item.get("id", item["occurrence_id"]): item for item in occurrences}
    for item in occurrences:
        if item["role"] != "timeline": continue
        if item["resolved_at"]["value"] in {other["resolved_at"]["value"] for other in occurrences if other is not item and other["role"] == "timeline"}: raise H3RequestError("conflicting hard anchors share the same placement")
        for edit in item["edit"]:
            for guide in edit.get("guides", []):
                target = by_id.get(guide)
                if target is None: raise H3RequestError(f"guide {guide!r} is dangling")
                if target["role"] != "reference": raise H3RequestError(f"guide {guide!r} points to timeline media, not a reference")
    if duration_fraction is None:
        ends: list[Fraction] = []
        for item in occurrences:
            if item["role"] != "timeline":
                continue
            start = Fraction(item["resolved_at"]["value"], int(FPS))
            resolved_range = item.get("resolved_range")
            if isinstance(resolved_range, list) and len(resolved_range) == 2:
                range_clock = SAMPLE_RATE if item.get("modality") == "audio" else int(FPS)
                span = Fraction(resolved_range[1] - resolved_range[0], range_clock)
            else:
                span = Fraction(1, int(FPS))
            ends.append(start + span)
        if not ends: raise H3RequestError("duration is required for a wholly generative request")
        duration_fraction = max(ends)
        duration = _json_number(duration_fraction)
    _ticks(duration_fraction, FPS, "duration")
    settings = _settings(raw.get("settings", {}))
    _validate_character_swap_media(settings, occurrences, continuation)
    value = {"version": 2, "prompt": prompt, "duration": duration, "media": occurrences, "settings": settings, "continuation": continuation, "profile": PROFILE, "output_count": 1, "model_tags": {item["occurrence_id"]: item["model_tag"] for item in occurrences if "model_tag" in item}}
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return H3Request(value=value, digest=hashlib.sha256(encoded).hexdigest())


def read_prepared_request(raw: Mapping[str, Any], expected_digest: Any, *, require_normalized_v2: bool = False) -> H3Request:
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest): raise H3RequestError("prepared request digest is missing or malformed")
    if raw.get("version") == 2 and raw.get("profile") == PROFILE and raw.get("output_count") == 1:
        encoded = json.dumps(dict(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        request = H3Request(value=dict(raw), digest=hashlib.sha256(encoded).hexdigest())
    else:
        request = normalize_v2(raw) if raw.get("version") == 2 else H3Request(value=dict(raw), digest=expected_digest)
    if request.digest != expected_digest: raise H3RequestError("prepared request digest does not match its normalized request")
    if request.value.get("version") == 2:
        effective_settings(request)
    if require_normalized_v2 and request.value.get("version") != 2: raise H3RequestError("prepared v2 request is missing normalized derived fields")
    return request


def branch_for(request: H3Request) -> str:
    if request.value.get("version") != 2: raise H3RequestError("native-v2 branch selection requires a v2 request")
    media = request.value["media"]
    timelines = [item for item in media if item["role"] == "timeline"]
    if not timelines: return "source_free"
    if all(item.get("modality") == "audio" for item in timelines): return "audio_only"
    if request.value.get("continuation") and any(item.get("modality") == "video" for item in timelines): return "extension_context"
    return "source_backed_v2v"


__all__ = ["PROFILE", "SAMPLE_RATE", "FPS", "branch_for", "normalize_v2", "read_prepared_request"]
