"""CPU-only Phase B view. Public documents still use the v1/v2 normalizers.

The typed input below is an adapter to that validation path, not another JSON
request grammar. It describes intent and ordered dependencies, never GPU proof.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, NoReturn

from .request import H3Request, H3RequestError, _nonblank
from .request_v2 import FPS, _interval


class H3SpecError(H3RequestError):
    """G21 envelope, also catchable by existing H3 request callers."""


def _fail(code: str, message: str, path: str, fix: str) -> NoReturn:
    raise H3SpecError(message, code=f"h3.spec.{code}", field=path, fix=fix)


@dataclass(frozen=True)
class Source:
    asset: str
    range: tuple[float, float]


@dataclass(frozen=True)
class Reference:
    name: str
    asset: str


@dataclass(frozen=True)
class Region:
    name: str
    mask_asset: str | None = None
    box: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class Operation:
    op: str
    during: tuple[float, float]
    region: str | None = None  # None selects the full frame.
    refs: tuple[str, ...] = ()
    stream: str = "video"


@dataclass(frozen=True)
class SamplingPass:
    operation: Operation
    reads: str  # source, previous_verified_output, or none
    preserves: tuple[Operation, ...]  # always constraints on original_source
    preserve_reads: str = "original_source"


@dataclass(frozen=True)
class H3Spec:
    prompt: str
    duration: float
    source: Source | None = None
    refs: tuple[Reference, ...] = ()
    regions: tuple[Region, ...] = ()
    ops: tuple[Operation, ...] = ()
    settings: Mapping[str, Any] = field(default_factory=dict)

    @property
    def capabilities(self) -> dict[str, dict[str, Any]]:
        return {
            op: {"available": False, "reason": "Requires recorded GPU proof"}
            for op in ("generate", "extend", "regenerate", "preserve")
        }

    def sampling_passes(self) -> tuple[SamplingPass, ...]:
        """Planning dependencies only; later editable pixels win after verification."""
        spec = normalize_spec(self)
        constraints = tuple(op for op in spec.ops if op.op == "preserve")
        result = []
        for op in spec.ops:
            if op.op == "preserve":
                continue
            reads = "previous_verified_output" if result else ("source" if spec.source else "none")
            result.append(SamplingPass(op, reads, constraints))
        return tuple(result)


def _names(items: tuple, path: str) -> dict:
    result = {}
    for index, item in enumerate(items):
        name = _nonblank(item.name, f"{path}[{index}].name")
        if name != item.name:
            _fail(
                "invalid_name",
                "Names cannot contain surrounding whitespace",
                path,
                "Trim the name and its uses",
            )
        if name in result:
            _fail("duplicate_name", f"Duplicate name {name!r}", path, "Use distinct names")
        result[name] = item
    return result


def _mask(region: Region, path: str) -> dict[str, Any]:
    if (region.mask_asset is None) == (region.box is None):
        _fail(
            "region",
            "A static region needs exactly one mask file or box",
            path,
            "Supply mask_asset or box [x, y, width, height]",
        )
    if region.mask_asset is not None:
        return {"asset": _nonblank(region.mask_asset, f"{path}.mask_asset")}
    box = region.box
    if not isinstance(box, (tuple, list)) or len(box) != 4:
        _fail("region", "box must be [x, y, width, height]", path, "Supply four pixel coordinates")
    # The existing mask normalizer checks numeric finiteness; positivity is a
    # permission constraint (a zero or negative box cannot authorize pixels).
    from .request_v2 import _mask as normalize_mask

    mask = normalize_mask({"rectangle": list(box)}, path, None)
    x, y, width, height = mask["rectangle"]
    if min(x, y) < 0 or min(width, height) <= 0:
        _fail(
            "region",
            "box needs nonnegative origin and positive size",
            path,
            "Correct the box coordinates",
        )
    return {"rectangle": mask["rectangle"]}


def _overlap(a: Operation, b: Operation, regions: Mapping[str, Region]) -> bool:
    if a.stream != b.stream and "av" not in (a.stream, b.stream):
        return False
    if max(a.during[0], b.during[0]) >= min(a.during[1], b.during[1]):
        return False
    if a.region is None or b.region is None or a.region == b.region:
        return True
    left, right = regions[a.region], regions[b.region]
    if left.box is not None and right.box is not None:
        x, y, w, h = left.box
        xx, yy, ww, hh = right.box
        return max(x, xx) < min(x + w, xx + ww) and max(y, yy) < min(y + h, yy + hh)
    # No pixel inspection in normalization: distinct files are not proof of
    # disjointness. Refuse ambiguous edit/preserve permission combinations.
    _fail(
        "conflicting_permissions",
        "Mask-file permissions cannot be proven disjoint",
        "ops",
        "Use disjoint time intervals or explicit disjoint boxes",
    )


def _normalize_spec(spec: H3Spec, modalities: Mapping[str, str] | None) -> H3Spec:
    if not isinstance(spec, H3Spec):
        _fail(
            "input",
            "Canonical input must be a typed H3Spec",
            "spec",
            "Import a v1/v2 request or construct H3Spec",
        )
    if spec.source is not None and not isinstance(spec.source, Source):
        _fail(
            "source_count", "At most one source is supported", "source", "Supply one Source or None"
        )
    refs = _names(spec.refs, "refs")
    regions = _names(spec.regions, "regions")
    masks = {name: _mask(region, f"regions.{name}") for name, region in regions.items()}
    for name, region in regions.items():
        if (
            region.mask_asset is not None
            and modalities is not None
            and modalities.get(region.mask_asset) not in (None, "image")
        ):
            _fail(
                "unsupported_region",
                "Only static image mask files are supported",
                f"regions.{name}",
                "Supply an image mask or box",
            )
    edits = []
    ops = []
    for index, op in enumerate(spec.ops):
        path = f"ops[{index}]"
        if (
            not isinstance(op, Operation)
            or not isinstance(op.op, str)
            or op.op not in {"generate", "extend", "regenerate", "preserve"}
        ):
            _fail(
                "unsupported_op",
                "Unsupported operation",
                path,
                "Use generate, extend, regenerate, or preserve",
            )
        if op.stream not in {"video", "audio", "av"}:
            _fail(
                "unsupported_op", "Unsupported stream", f"{path}.stream", "Use video, audio, or av"
            )
        during, _ = _interval(op.during, f"{path}.during", FPS)
        op = replace(op, during=tuple(during))
        if op.region is not None and op.region not in regions:
            _fail(
                "dangling_region",
                f"Unknown region {op.region!r}",
                f"{path}.region",
                "Declare the named static region",
            )
        if any(name not in refs for name in op.refs):
            _fail(
                "dangling_reference",
                "Unknown reference name",
                f"{path}.refs",
                "Declare every referenced name in refs",
            )
        if during[1] > spec.duration:
            _fail(
                "window",
                "Operation ends after duration",
                f"{path}.during",
                "Keep operations inside the output interval",
            )
        if op.op in {"generate", "extend"}:
            if op.refs and op.refs != tuple(refs):
                _fail(
                    "unsupported_reference",
                    "Generation/extension uses the global reference order",
                    f"{path}.refs",
                    "Omit refs or use every reference in declaration order",
                )
            expected = (
                (0, spec.duration)
                if spec.source is None
                else (spec.source.range[1] - spec.source.range[0], spec.duration)
            )
            if op.region is not None or op.stream != "av" or tuple(during) != expected:
                _fail(
                    "unsupported_op",
                    "Generation/extension must cover the full audiovisual window",
                    path,
                    "Use one full-frame av operation over the generated interval",
                )
        elif spec.source is None:
            _fail("source", "Source-backed operation needs a source", path, "Supply one source")
        elif during[1] > spec.source.range[1] - spec.source.range[0]:
            _fail(
                "window",
                "Region resolution beyond the source is unsupported",
                path,
                "Keep source edits and preservation inside the source window",
            )
        if (
            op.op == "generate"
            and spec.source is not None
            or op.op == "extend"
            and spec.source is None
        ):
            _fail(
                "source",
                "Operation has incompatible source permissions",
                path,
                "Use generate without a source; extend with a source",
            )
        if op.op == "regenerate" and op.stream != "video":
            _fail(
                "unsupported_op",
                "Only video regeneration is supported",
                path,
                "Use a video regeneration operation",
            )
        if op.op == "preserve" and op.refs:
            _fail(
                "unsupported_reference",
                "Preservation has no conditioning references",
                f"{path}.refs",
                "Remove refs from the preservation constraint",
            )
        if op.stream != "video" and op.region is not None:
            _fail(
                "unsupported_region",
                "Spatial regions apply to video only",
                path,
                "Remove the region from audio/av permissions",
            )
        ops.append(op)
        if op.op in {"regenerate", "preserve"}:
            for stream in ("video", "audio") if op.stream == "av" else (op.stream,):
                edit = {
                    "stream": stream,
                    "during": list(during),
                    "action": "preserve" if op.op == "preserve" else "generate",
                }
                if stream == "video":
                    edit["mask"] = masks[op.region] if op.region else {"full_frame": True}
                if op.refs:
                    edit["guides"] = list(op.refs)
                edits.append(edit)
    generators = [op for op in ops if op.op in {"generate", "extend"}]
    if generators and (len(generators) != 1 or any(op.op == "regenerate" for op in ops)):
        _fail(
            "unsupported_op",
            "This combination needs multiple generation windows",
            "ops",
            "Use standalone generation/extension or ordered source edits",
        )
    if spec.source is None and (len(ops) != 1 or not generators):
        _fail(
            "source",
            "Source-free input requires one generate operation",
            "ops",
            "Declare one full-window av generate operation",
        )
    if (
        spec.source is not None
        and not generators
        and spec.duration > spec.source.range[1] - spec.source.range[0]
    ):
        _fail(
            "window",
            "Output extends beyond the source without extension permissions",
            "duration",
            "Use an explicit extend operation",
        )
    if len(refs) > 9:
        _fail(
            "unsupported_reference",
            "Single-window requests support at most nine image references",
            "refs",
            "Use at most nine references",
        )
    for index, op in enumerate(ops):
        for other in ops[index + 1 :]:
            if (op.op == "preserve") != (other.op == "preserve") and _overlap(op, other, regions):
                _fail(
                    "conflicting_permissions",
                    "Edit and preserve permissions overlap",
                    "ops",
                    "Make explicit preservation disjoint from editable pixels and intervals",
                )
    from .generation import generation_timing

    generation_timing(spec.duration)  # Existing single-window bound; no splitting.
    media = [
        {"id": name, "asset": ref.asset, "role": "reference", "modality": "image"}
        for name, ref in refs.items()
    ]
    if spec.source is not None:
        media.insert(
            0,
            {
                "id": "__source__",
                "asset": spec.source.asset,
                "role": "timeline",
                "modality": "video",
                "at": {"frame": 0},
                "range": list(spec.source.range),
                "edit": edits,
            },
        )
    from .request import normalize_request

    request = normalize_request(
        {
            "version": 2,
            "prompt": spec.prompt,
            "duration": spec.duration,
            "media": media,
            "settings": dict(spec.settings),
            "continuation": bool(generators and generators[0].op == "extend"),
        },
        asset_modalities=modalities,
    )
    result = _from_v2(request)
    return replace(result, prompt=spec.prompt)  # v1's prompt bytes remain significant.


def normalize_spec(spec: H3Spec, *, asset_modalities: Mapping[str, str] | None = None) -> H3Spec:
    """Validate typed intent by lowering through the existing v2 normalizer."""
    try:
        return _normalize_spec(spec, asset_modalities)
    except H3SpecError:
        raise
    except (H3RequestError, ValueError, TypeError, AttributeError, KeyError) as exc:
        _fail(
            "invalid",
            str(exc),
            "spec",
            "Correct the declared fields using the v1/v2 request contract",
        )


def _region(mask: Mapping, regions: list[Region]) -> str | None:
    if mask.get("full_frame") is True and set(mask) <= {"full_frame", "polarity"}:
        return None
    if set(mask) - {"asset", "rectangle", "polarity", "edge"} or mask.get("edge", {}) not in (
        {},
        {"dilate": 0, "feather": 0},
    ):
        _fail(
            "unsupported_region",
            "Only static mask files and boxes are representable",
            "regions",
            "Remove tracking, polygons, ranges, and edge transforms",
        )
    candidate = Region(
        "",
        mask_asset=mask.get("asset"),
        box=tuple(mask["rectangle"]) if "rectangle" in mask else None,
    )
    for region in regions:
        if replace(region, name="") == candidate:
            return region.name
    name = f"region-{len(regions) + 1}"
    regions.append(replace(candidate, name=name))
    return name


def _from_v2(request: H3Request) -> H3Spec:
    value = request.value
    timelines = [item for item in value["media"] if item["role"] == "timeline"]
    if len(timelines) > 1:
        _fail("source_count", "At most one source is supported", "media", "Use one timeline source")
    refs = []
    names = {}
    for item in value["media"]:
        if item["role"] != "reference":
            continue
        if item.get("modality") != "image" or any(key in item for key in ("range", "audio")):
            _fail(
                "unsupported_reference",
                "Only untimed image references are representable",
                "media",
                "Use image references",
            )
        name = f"ref-{len(refs) + 1}"
        names[item["occurrence_id"]] = name
        refs.append(Reference(name, item["asset"]))
    source = None
    ops = []
    regions: list[Region] = []
    if timelines:
        item = timelines[0]
        if (
            item.get("modality") != "video"
            or item["resolved_at"]["value"] != 0
            or "range" not in item
            or item.get("hard")
            or "latent_pin" in item
        ):
            _fail(
                "unsupported_source",
                "Source must be a bounded video at frame zero",
                "media",
                "Use one bounded video without hard anchors",
            )
        source = Source(item["asset"], tuple(item["range"]))
        for edit in item["edit"]:
            if (
                set(edit) - {"stream", "during", "resolved", "action", "mask", "guides", "channels"}
                or edit.get("channels", "all") != "all"
            ):
                _fail(
                    "unsupported_op",
                    "Dialogue and channel conditioning are outside SPEC-MIN",
                    "media.edit",
                    "Use video edits or whole-stream preservation",
                )
            region = _region(edit["mask"], regions) if edit["stream"] == "video" else None
            default_guides = names if edit["action"] != "preserve" else ()
            guides = tuple(names[name] for name in edit.get("guides", default_guides))
            ops.append(
                Operation(
                    "preserve" if edit["action"] == "preserve" else "regenerate",
                    tuple(edit["during"]),
                    region,
                    guides,
                    edit["stream"],
                )
            )
    if source is None:
        ops = [
            Operation("generate", (0, value["duration"]), refs=tuple(names.values()), stream="av")
        ]
    elif value["continuation"]:
        if any(op.op != "preserve" for op in ops):
            _fail(
                "unsupported_op",
                "Extension cannot contain source regeneration",
                "media.edit",
                "Use standalone extension",
            )
        ops.append(
            Operation(
                "extend",
                (source.range[1] - source.range[0], value["duration"]),
                refs=tuple(names.values()),
                stream="av",
            )
        )
    return H3Spec(
        value["prompt"],
        value["duration"],
        source,
        tuple(refs),
        tuple(regions),
        tuple(ops),
        value["settings"],
    )


def canonical_spec(
    request: H3Request, *, asset_modalities: Mapping[str, str] | None = None
) -> H3Spec:
    """Import normalized legacy intent; never replace its schema or digest."""
    try:
        if request.value.get("version") == 2:
            return normalize_spec(_from_v2(request), asset_modalities=asset_modalities)
        value = request.value
        if value.get("version") != 1:
            _fail(
                "version", "Unsupported request version", "version", "Normalize a v1 or v2 request"
            )
        source = (
            Source(value["source"]["asset"], tuple(value["source"]["range"]))
            if value["source"]
            else None
        )
        refs = []
        for ref in value["references"]:
            if ref["purpose"] != "appearance":
                _fail(
                    "unsupported_reference",
                    "Only appearance image references are equivalent",
                    "references",
                    "Use untimed appearance references",
                )
            refs.append(Reference(f"ref-{len(refs) + 1}", ref["asset"]))
        regions: list[Region] = []
        ops = []
        for stream in ("video", "audio"):
            for change in value["changes"][stream]:
                if set(change) - {"during", "action", "area"}:
                    _fail(
                        "unsupported_op",
                        "Advanced legacy changes are outside SPEC-MIN",
                        "changes",
                        "Use static video permissions or whole-stream preservation",
                    )
                area = change.get("area", {})
                mask = {"asset" if key == "mask_asset" else key: val for key, val in area.items()}
                region = _region(mask, regions) if stream == "video" else None
                ops.append(
                    Operation(
                        "preserve" if change["action"] == "preserve" else "regenerate",
                        tuple(change["during"]),
                        region,
                        tuple(ref.name for ref in refs) if change["action"] != "preserve" else (),
                        stream,
                    )
                )
        duration = value["output"].get("duration")
        if duration is None:
            from ..actions.prepare.masks import _bounded_duration

            duration = _bounded_duration(request)
        if value["operation"] == "generate":
            ops = [
                Operation(
                    "generate", (0, duration), refs=tuple(ref.name for ref in refs), stream="av"
                )
            ]
        elif value["operation"] == "continue":
            start = source.range[1] - source.range[0]
            edits = [op for op in ops if op.op != "preserve"]
            if (
                len(edits) != 2
                or {op.stream for op in edits} != {"video", "audio"}
                or any(op.region or op.during != (start, duration) for op in edits)
            ):
                _fail(
                    "unsupported_op",
                    "Continuation must generate one audiovisual suffix",
                    "changes",
                    "Use matching full-frame video/audio suffix permissions",
                )
            ops = [op for op in ops if op.op == "preserve"] + [
                Operation(
                    "extend", (start, duration), refs=tuple(ref.name for ref in refs), stream="av"
                )
            ]
        settings = dict(value["overrides"])
        if value["operation"] == "edit":
            settings = {
                "model": "minimax_h3_fl2va_pruned_fp8_scaled.safetensors",
                "steps": 20,
                "seed": 0,
                **settings,
            }
        return normalize_spec(
            H3Spec(
                value["content"]["prompt"],
                duration,
                source,
                tuple(refs),
                tuple(regions),
                tuple(ops),
                settings,
            ),
            asset_modalities=asset_modalities,
        )
    except H3SpecError:
        raise
    except (H3RequestError, ValueError, TypeError, KeyError) as exc:
        _fail(
            "invalid",
            str(exc),
            "request",
            "Use a bounded request in the static single-window subset",
        )
