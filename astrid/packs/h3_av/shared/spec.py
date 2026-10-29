"""Composable H3 semantics, imported through the retained request normalizers.

``normalize_spec`` accepts a public v1/v2 request, a sealed ``H3Request``, or
``spec_version: 1`` with source, refs (a name/object mapping), regions (a
name/selector mapping), prompt, duration, settings and ordered operations.
Operations use op=generate/extend/regenerate/preserve, stream=video/audio,
during, region (a name), refs (names), and optional text. Region selectors
are the existing mask_asset/rectangle/full_frame selectors. Boxes use source
pixel [x, y, width, height]; rasterization and asset inspection belong to prepare.

This is a view, not an execution route: requests retain their original digests.
Bindings describe the verified-output dependency; they do not attest a run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from astrid.core.contracts.errors import AstridValueError

from .request import H3Request, H3RequestError, _area, _object, _unknown, normalize_request
from .request_v2 import _settings


@dataclass(frozen=True)
class H3Spec:
    value: dict[str, Any]
    requests: tuple[H3Request, ...]


def _fail(code: str, message: str, field: str, fix: str) -> None:
    raise AstridValueError(message, code=f"h3.spec.{code}", field=field, fix=fix)


def _region(area: Mapping[str, Any], field: str) -> dict[str, Any]:
    selector = _area(area, field)
    if not set(selector) <= {"full_frame", "mask_asset", "rectangle"}:
        _fail("unsupported_region", "Only static masks and boxes are composable regions", field,
              "Use mask_asset or rectangle, or retain the legacy request entry point.")
    if "rectangle" in selector:
        x, y, width, height = selector["rectangle"]
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            _fail("invalid_region", "A box needs nonnegative coordinates and positive dimensions", field,
                  "Use source pixel [x, y, width, height].")
    return selector


def _import(raw: Mapping[str, Any], modalities: Mapping[str, str] | None) -> H3Request:
    """Resolve names only; all request syntax/timing/settings use native v2."""
    _unknown(raw, {"spec_version", "source", "refs", "regions", "prompt", "duration", "settings", "operations"}, "spec")
    if raw.get("spec_version") != 1:
        _fail("version", "spec_version must be 1", "spec_version", "Use spec_version: 1.")
    source = raw.get("source")
    if isinstance(source, list):
        _fail("source_count", "A composable spec has at most one source", "source", "Use one source object or null.")
    source = _object(source, "source", nullable=True)
    if source is not None:
        _unknown(source, {"asset", "range"}, "source")
    refs = _object(raw.get("refs", {}), "refs") or {}
    regions = _object(raw.get("regions", {}), "regions") or {}
    selectors = {name: _region(_object(area, f"regions.{name}") or {}, f"regions.{name}")
                 for name, area in regions.items()}
    media: list[dict[str, Any]] = []
    for name, value in refs.items():
        ref = _object(value, f"refs.{name}") or {}
        _unknown(ref, {"asset", "modality", "range", "audio"}, f"refs.{name}")
        media.append({**ref, "id": name, "role": "reference"})
    operations = raw.get("operations")
    if not isinstance(operations, list) or not operations:
        _fail("operations", "operations must be a non-empty array", "operations", "Declare generate, extend, regenerate or preserve.")
    edits: list[dict[str, Any]] = []
    mode = "edit"
    for index, value in enumerate(operations):
        field = f"operations[{index}]"
        op = _object(value, field) or {}
        _unknown(op, {"op", "stream", "during", "region", "refs", "text"}, field)
        kind = op.get("op")
        if kind not in {"generate", "extend", "regenerate", "preserve"}:
            _fail("unsupported_operation", f"Unsupported operation {kind!r}", f"{field}.op",
                  "Use generate, extend, regenerate or preserve.")
        if kind in {"generate", "extend"}:
            if len(operations) != 1 or set(op) != {"op"}:
                _fail("unsupported_combination", f"{kind} must be a single whole-timeline operation", field,
                      "Use one operation containing only op; specify prompt and duration at spec level.")
            if (kind == "generate") != (source is None):
                _fail("source_count", f"{kind} has an incompatible source", "source",
                      "Omit source for generate; supply one source for extend.")
            mode = kind
            continue
        if source is None:
            _fail("source_required", f"{kind} requires an original source", field, "Supply one source object.")
        stream = op.get("stream", "video")
        edit = {"stream": stream, "during": op.get("during"),
                "action": "preserve" if kind == "preserve" else "generate"}
        if "region" in op:
            name = op["region"]
            if not isinstance(name, str) or name not in selectors:
                _fail("dangling_region", f"Unknown region {name!r}", f"{field}.region", "Declare the name in regions.")
            if stream != "video":
                _fail("conflict", "Audio cannot select a spatial region", f"{field}.region", "Remove region from the audio operation.")
            area = selectors[name]
            edit["mask"] = {"asset": area["mask_asset"]} if "mask_asset" in area else area
        names = op.get("refs", [])
        if not isinstance(names, list) or any(not isinstance(name, str) or name not in refs for name in names):
            _fail("dangling_ref", "Operation refs must name declared references", f"{field}.refs", "Declare every name in refs.")
        if names:
            edit["guides"] = names
        if "text" in op:
            edit["text"] = op["text"]
        if kind == "preserve" and (names or "text" in op):
            _fail("conflict", "Preserve cannot request conditioning or replacement text", field,
                  "Remove refs/text; preserve constrains the original input and never samples.")
        edits.append(edit)
    if source is not None:
        media.insert(0, {**source, "id": "__source__", "role": "timeline", "modality": "video",
                         "at": {"frame": 0}, "edit": edits})
    return normalize_request({"version": 2, "prompt": raw.get("prompt"), "duration": raw.get("duration"),
                              "settings": raw.get("settings", {}), "continuation": mode == "extend", "media": media},
                             asset_modalities=modalities)


def _mask_area(mask: Mapping[str, Any], field: str) -> dict[str, Any]:
    # These fields carry semantics the static canonical view cannot discard.
    if any(key in mask for key in ("track", "range", "threshold")) or any(mask.get("edge", {}).values()):
        _fail("unsupported_region", "This mask needs the legacy preparation route", field,
              "Use a static binary mask without tracking, frame ranges, thresholds or feathering.")
    area = {"mask_asset": mask["asset"]} if "asset" in mask else {key: mask[key] for key in ("full_frame", "rectangle", "polygon") if key in mask}
    return _region(area, field)


def _overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if "full_frame" in left or "full_frame" in right:
        return True
    if "rectangle" in left and "rectangle" in right:
        x, y, w, h = left["rectangle"]
        a, b, c, d = right["rectangle"]
        return x < a + c and a < x + w and y < b + d and b < y + h
    # Mask intersection cannot be proven without the B4 asset inspection.
    return True


def _view(request: H3Request) -> dict[str, Any]:
    value = request.value
    refs: list[dict[str, Any]] = []
    regions: dict[str, dict[str, Any]] = {}
    operations: list[dict[str, Any]] = []

    def add(stream: str, change: Mapping[str, Any], area: Mapping[str, Any] | None = None) -> None:
        op: dict[str, Any] = {"op": "preserve" if change["action"] == "preserve" else "regenerate",
                              "stream": stream, "during": list(change["during"])}
        if area is not None:
            selector = _region(area, "operations.region")
            name = next((key for key, held in regions.items() if held == selector), f"region-{len(regions) + 1}")
            regions[name] = selector
            op["region"] = name
        if "text" in change or "dialogue" in change:
            op["text"] = change.get("text", change.get("dialogue"))
        if change.get("guides"):
            op["refs"] = [ref_ids[name] for name in change["guides"]]
        operations.append(op)

    if value["version"] == 1:
        from ..actions.prepare.masks import build_mask_schedule

        schedule = build_mask_schedule(request)
        source, duration, prompt = value["source"], schedule["duration"], value["content"]["prompt"].strip()
        settings = _settings(value["overrides"])
        mode = {"generate": "generate", "continue": "extend", "edit": "edit"}[value["operation"]]
        for ref in value["references"]:
            if ref["purpose"] != "appearance":
                _fail("unsupported_reference", "The canonical view currently supports appearance references", "references",
                      "Keep other conditioning purposes on the legacy entry point.")
            refs.append({"asset": ref["asset"], "modality": "image"})
        ref_ids: dict[str, str] = {}
        for stream in ("video", "audio"):
            for change in value["changes"][stream]:
                if "mask_asset" in change or "stem" in change:
                    _fail("unsupported_operation", "Per-stream masks/stems require the legacy route", "changes", "Use a named video area mask or an all-channel audio operation.")
                add(stream, change, change["area"] if stream == "video" else None)
    else:
        timeline = [item for item in value["media"] if item["role"] == "timeline"]
        if len(timeline) > 1:
            _fail("source_count", "A composable spec has at most one source", "media", "Use one timeline source.")
        item = timeline[0] if timeline else None
        if item and (item.get("modality") != "video" or item["resolved_at"]["value"] != 0 or item["hard"]):
            _fail("unsupported_source", "The source must be one video placed at frame zero", "media", "Retain other timeline layouts on the legacy entry point.")
        source = {key: item[key] for key in ("asset", "range") if key in item} if item else None
        duration, prompt, settings = value["duration"], value["prompt"], value["settings"]
        mode = "extend" if value["continuation"] else "edit" if source else "generate"
        ref_ids = {}
        for ref in value["media"]:
            if ref["role"] != "reference":
                continue
            ref_ids[ref["occurrence_id"]] = f"ref-{len(refs) + 1}"
            refs.append({key: ref[key] for key in ("asset", "modality", "range", "audio") if key in ref})
        for change in item["edit"] if item else []:
            if change.get("channels", "all") != "all":
                _fail("unsupported_operation", "Channel selection requires the legacy route", "media.edit.channels", "Use channels=all.")
            add(change["stream"], change, _mask_area(change["mask"], "media.edit.mask") if change["stream"] == "video" else None)
    if source is None and mode != "generate":
        _fail("source_required", "This operation requires an original source", "source", "Supply one source.")
    if mode in {"generate", "extend"}:
        if mode == "extend":
            if not source or "range" not in source:
                _fail("source_range", "Extend needs a bounded source range", "source.range", "Declare the source range.")
            span = source["range"][1] - source["range"][0]
            if duration <= span:
                _fail("conflict", "Extend must increase the source duration", "duration", "Choose duration greater than the selected source span.")
            if (value["version"] == 1 or operations) and sorted(op["stream"] for op in operations) != ["audio", "video"]:
                _fail("unsupported_combination", "Extend requires exactly one video and one audio suffix permission", "operations",
                      "Declare both generated suffixes, or use the native-v2 implicit continuation form.")
            for op in operations:
                area = regions.get(op.get("region"), {})
                if op["op"] != "regenerate" or op["during"] != [span, duration] or (op["stream"] == "video" and area != {"full_frame": True}) or "text" in op or "refs" in op:
                    _fail("unsupported_combination", "Extend supports one whole audiovisual suffix", "operations", "Use a full-frame generated suffix without separate edit conditioning.")
        operations = [{"op": mode}]
        regions = {}
    # Bounds use the existing mask scheduler for v1; v2 clocks were resolved by
    # its normalizer, but delivery bounds and original-source constraints remain.
    for index, op in enumerate(operations):
        if "during" not in op:
            continue
        if op["during"][1] > duration:
            _fail("conflict", "Operation ends after output duration", f"operations[{index}].during", "Keep the interval within duration.")
        span = source["range"][1] - source["range"][0] if source and "range" in source else duration
        if op["op"] == "regenerate" and op["during"][1] > span:
            _fail("unsupported_combination", "Regenerate cannot select frames absent from the original source", f"operations[{index}].during",
                  "Keep edits inside the selected source span; use extend for a generated suffix.")
        if op["op"] == "preserve":
            if op["during"][1] > span:
                _fail("conflict", "Preserve cannot protect frames absent from the original source", f"operations[{index}].during", "Keep preserve inside the selected original source span.")
            if "text" in op or "refs" in op:
                _fail("conflict", "Preserve cannot replace or condition content", f"operations[{index}]", "Remove conditioning and text from preserve.")
            for other in operations:
                if other["op"] != "regenerate" or other["stream"] != op["stream"]:
                    continue
                if max(op["during"][0], other["during"][0]) < min(op["during"][1], other["during"][1]) and (op["stream"] == "audio" or _overlap(regions[op["region"]], regions[other["region"]])):
                    _fail("conflict", "Edit permissions overlap original-input preservation", f"operations[{index}]", "Use disjoint intervals/boxes; mask intersections require asset inspection.")
    previous: int | None = None
    for index, op in enumerate(operations):
        op["input"] = ("original" if source is not None else None) if previous is None or op["op"] == "preserve" else {"verified_output_of": previous}
        op["sampling"] = op["op"] != "preserve"
        if op["sampling"]:
            previous = index
    return {"spec_version": 1, "source": source, "duration": duration, "prompt": prompt,
            "refs": {f"ref-{index + 1}": ref for index, ref in enumerate(refs)}, "regions": regions,
            "operations": operations, "settings": settings,
            "overlap_policy": "later_edit_wins", "preserve_input": "original",
            "capability": {"available": False, "reason": "Execution requires proof for the selected GPU configuration."}}


def normalize_spec(raw: Mapping[str, Any] | H3Request, *, asset_modalities: Mapping[str, str] | None = None) -> H3Spec:
    """Return canonical semantics and unchanged, digest-bearing legacy input.

    Unrepresentable legacy features remain valid at their old entry points;
    this deliberately narrow static/single-source view rejects them explicitly.
    """
    try:
        request = raw if isinstance(raw, H3Request) else _import(raw, asset_modalities) if isinstance(raw, Mapping) and "spec_version" in raw else normalize_request(raw, asset_modalities=asset_modalities)
        return H3Spec(value=_view(request), requests=(request,))
    except H3RequestError as exc:
        message = str(exc)
        _fail("invalid_request", message, message.split(" ", 1)[0], "Correct the indicated field using the retained H3 request contract.")
    except ValueError as exc:
        if isinstance(exc, AstridValueError):
            raise
        _fail("conflict", str(exc), "operations", "Keep operations inside the declared output and source bounds.")


__all__ = ["H3Spec", "normalize_spec"]
