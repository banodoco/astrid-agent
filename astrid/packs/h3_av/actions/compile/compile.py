"""Compile honest H3 audiovisual requests into sealed VibeComfy bindings."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from astrid.packs.vibecomfy.asset_manifest import build_asset_manifest

from astrid.packs.h3_av.shared.request import H3Request, H3RequestError, normalize_request
from astrid.packs.h3_av.shared.request_v2 import CHARACTER_SWAP_ID, REF2VA_MODEL, branch_for, effective_settings, read_prepared_request
from astrid.packs.h3_av.shared.timing import ContinuationTimingError, plan_continuation
from astrid.packs.h3_av.shared.errors import CompilationError
from astrid.packs.h3_av.shared.input_bundle import _canonical_bytes, _write_asset_bundle



_PROFILE_ID = "h3_av.native.v1"
_LANPAINT_WORKFLOW = Path("workflows/lanpaint_h3_av_generalized")
_NATIVE_WORKFLOW = Path("workflows/native_h3_continuation")
_REFERENCE_WORKFLOW = Path("workflows/native_h3_continuation_refs")
_DEFAULTS = {
    "model": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
    "steps": 8,
    "seed": 123456789,
    "sampler": "res_multistep",
    "guidance": 0.95,
}



def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pack_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolved_request(preparation: Mapping[str, Any]) -> H3Request:
    if preparation.get("kind") != "h3_av_preparation":
        raise CompilationError("preparation.kind must be h3_av_preparation")
    if preparation.get("status") != "prepared" or preparation.get("runtime_submission") != "eligible":
        raise CompilationError("preparation is not eligible for runtime submission")
    raw = preparation.get("request")
    if not isinstance(raw, Mapping):
        raise CompilationError("preparation is missing its normalized request")
    expected_digest = preparation.get("request_digest")
    if isinstance(raw, Mapping) and raw.get("version") == 2:
        try:
            request = read_prepared_request(raw, expected_digest, require_normalized_v2=True)
        except H3RequestError as exc:
            raise CompilationError(str(exc)) from exc
    else:
        request = normalize_request(raw)
        if request.digest != expected_digest:
            raise CompilationError("preparation request digest does not match its request")
    return request


def _asset_paths(preparation: Mapping[str, Any]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    records = preparation.get("assets")
    if not isinstance(records, list):
        raise CompilationError("preparation.assets must be an array")
    for index, record in enumerate(records):
        if not isinstance(record, Mapping) or not isinstance(record.get("asset"), str):
            raise CompilationError(f"preparation.assets[{index}] is malformed")
        asset_id = str(record["asset"])
        if record.get("status") != "resolved" or not isinstance(record.get("path"), str):
            raise CompilationError(f"asset {asset_id!r} is unresolved")
        path = Path(record["path"]).expanduser().resolve()
        if path.is_symlink() or not path.is_file():
            raise CompilationError(f"asset {asset_id!r} is not a regular file")
        if record.get("sha256") != _sha256(path) or record.get("size") != path.stat().st_size:
            raise CompilationError(f"asset {asset_id!r} changed after preparation")
        previous = result.get(asset_id)
        if previous is not None and previous != path:
            raise CompilationError(f"asset {asset_id!r} resolves to multiple files")
        result[asset_id] = path
    return result


def _asset_member(binding: str, path: Path, digest: str) -> str:
    suffix = path.suffix.lower()
    safe = "".join(character if character.isalnum() or character in "._-" else "-" for character in path.stem).strip(".-")
    safe = safe or binding.replace("/", "-") or "asset"
    return f"assets/{digest[:16]}-{safe}{suffix}"



def _copy_workflow_bundle(resource_root: Path, destination: Path) -> dict[str, Path]:
    members = {name: resource_root / name for name in ("workflow.py", "workflow.vibe.json", "source.json")}
    for name, member in members.items():
        if not member.is_file():
            raise CompilationError(f"selected packaged workflow is missing {name}")
    destination.mkdir(parents=True, exist_ok=True)
    frozen: dict[str, Path] = {}
    for name, source in members.items():
        target = destination / name
        shutil.copy2(source, target)
        if _sha256(target) != _sha256(source):
            raise CompilationError(f"copied workflow member {name} failed integrity validation")
        frozen[name] = target
    return frozen


def _member_by_binding(manifest: Mapping[str, Any]) -> dict[str, str]:
    records = manifest.get("assets")
    if not isinstance(records, list):
        raise CompilationError("managed asset manifest is malformed")
    return {str(record["binding"]): PurePosixPath(str(record["member"])).name for record in records}


def _asset_bindings(request: H3Request, assets: Mapping[str, Path], *, include_masks: bool = False) -> tuple[dict[str, Path], dict[str, str]]:
    value = request.value
    bindings: dict[str, Path] = {}
    asset_binding: dict[str, str] = {}

    def add(asset_id: str, preferred: str) -> str:
        if asset_id not in assets:
            raise CompilationError(f"asset {asset_id!r} is missing from preparation")
        if asset_id in asset_binding:
            return asset_binding[asset_id]
        binding = preferred
        suffix = 1
        while binding in bindings:
            suffix += 1
            binding = f"{preferred}_{suffix}"
        bindings[binding] = assets[asset_id]
        asset_binding[asset_id] = binding
        return binding

    source = value.get("source")
    if isinstance(source, Mapping):
        add(str(source["asset"]), "source_video")
    for index, reference in enumerate(value["references"]):
        add(str(reference["asset"]), f"reference_{index}")
    if include_masks:
        for index, change in enumerate(value["changes"]["video"]):
            area = change.get("area", {})
            mask_id = change.get("mask_asset") or area.get("mask_asset")
            if mask_id:
                add(str(mask_id), f"video_mask_{index}")
        for index, change in enumerate(value["changes"]["audio"]):
            if change.get("mask_asset"):
                add(str(change["mask_asset"]), f"audio_mask_{index}")
    return bindings, asset_binding


def _request_dialogue_prompt(request: H3Request) -> str:
    prompt = request.value["content"]["prompt"]
    dialogue = [
        change for change in request.value["changes"]["audio"]
        if change.get("action") == "generate" and change.get("dialogue")
    ]
    if not dialogue:
        return prompt
    lines = [prompt, "", "Exact dialogue timing:"]
    lines.extend(f"{item['during'][0]:g}-{item['during'][1]:g}s: {item['dialogue']}" for item in dialogue)
    return "\n".join(lines)


def _frame_index(seconds: float, path: str) -> int:
    frame = round(seconds * 24)
    if abs(frame / 24 - seconds) > 1e-6:
        raise CompilationError(f"{path} must be aligned to the selected graph's 24 fps frame grid")
    return frame


def _lanpaint_masks(request: H3Request, *, asset_binding: Mapping[str, str], internal_full_frame: str, internal_preserve: str) -> tuple[str, str, dict[str, Any]]:
    value = request.value
    events: dict[int, str] = {0: internal_preserve}
    generated_video: list[list[float]] = []
    active_end = -1.0
    for index, change in enumerate(sorted(value["changes"]["video"], key=lambda item: item["during"])):
        if change["action"] != "generate":
            continue
        start, end = map(float, change["during"])
        if start < active_end:
            raise CompilationError("LanPaint compilation requires non-overlapping generated video intervals")
        area = change["area"]
        mask_id = change.get("mask_asset") or area.get("mask_asset")
        if area.get("full_frame"):
            binding = internal_full_frame if mask_id is None else asset_binding.get(str(mask_id))
        else:
            if mask_id is None:
                raise CompilationError(f"changes.video[{index}] regional edits require an explicit mask asset")
            binding = asset_binding.get(str(mask_id))
        if binding is None:
            raise CompilationError(f"video mask asset {mask_id!r} is missing from the managed asset bundle")
        start_frame = _frame_index(start, f"changes.video[{index}].during[0]")
        end_frame = _frame_index(end, f"changes.video[{index}].during[1]")
        if start_frame in events and events[start_frame] != internal_preserve:
            raise CompilationError("video mask keyframes collide on the 24 fps frame grid")
        events[start_frame] = binding
        if end_frame in events and events[end_frame] not in {internal_preserve, binding}:
            raise CompilationError("video mask keyframes collide on the 24 fps frame grid")
        events[end_frame] = internal_preserve
        active_end = end
        generated_video.append([start, end])

    audio_intervals: list[dict[str, float]] = []
    for index, change in enumerate(value["changes"]["audio"]):
        if change.get("mask_asset"):
            raise CompilationError(f"changes.audio[{index}].mask_asset is not an input of the LanPaint AV graph")
        if change.get("stem", "mix") != "mix":
            raise CompilationError(f"changes.audio[{index}].stem requires a real stem/separation graph")
        if change["action"] == "generate":
            start, end = map(float, change["during"])
            audio_intervals.append({"start": start, "end": end})
    keyframes = json.dumps({str(frame): events[frame] for frame in sorted(events)}, separators=(",", ":"))
    intervals = json.dumps(audio_intervals, separators=(",", ":"))
    return keyframes, intervals, {"video": generated_video, "audio": [[x["start"], x["end"]] for x in audio_intervals]}


def _require_lanpaint(request: H3Request) -> None:
    value = request.value
    if value["operation"] != "edit":
        raise CompilationError("the LanPaint graph is selected only for source-backed operation=edit")
    source = value.get("source")
    duration = value.get("output", {}).get("duration")
    if not isinstance(source, Mapping) or "range" not in source:
        raise CompilationError("LanPaint source-backed edits require source.range")
    if duration is None or source["range"][0] != 0 or float(source["range"][1]) != float(duration):
        raise CompilationError("LanPaint requires source.range=[0, output.duration]; arbitrary source windows are unsupported")
    if value["references"]:
        raise CompilationError("the LanPaint AV graph has no reference-image input")
    for name in ("sampler", "guidance"):
        if name in value["overrides"] and value["overrides"][name] != _DEFAULTS[name]:
            raise CompilationError(f"overrides.{name} is not a public input of the LanPaint graph")


def _require_native(request: H3Request) -> tuple[float, float]:
    value = request.value
    if value["operation"] != "continue":
        raise CompilationError("native Seitanism graphs support operation=continue only")
    source = value.get("source")
    if not isinstance(source, Mapping) or "range" not in source:
        raise CompilationError("native continuation requires source.range")
    source_start, source_end = map(float, source["range"])
    duration = value.get("output", {}).get("duration")
    if source_start != 0 or duration is None or float(duration) <= source_end:
        raise CompilationError("native continuation requires source.range starting at 0 and output.duration greater than its end")
    suffix = [source_end, float(duration)]
    video = value["changes"]["video"]
    audio = value["changes"]["audio"]
    if len(video) != 1 or video[0].get("during") != suffix or video[0].get("area") != {"full_frame": True} or video[0].get("action") != "generate" or video[0].get("mask_asset"):
        raise CompilationError("native continuation requires one full-frame generated video suffix")
    if len(audio) != 1 or audio[0].get("during") != suffix or audio[0].get("action") != "generate":
        raise CompilationError("native continuation requires one generated audio suffix matching the video suffix")
    if audio[0].get("mask_asset") or audio[0].get("stem", "mix") != "mix":
        raise CompilationError("native continuation has no independent audio mask or stem binding")
    for reference in value["references"]:
        if reference["purpose"] == "audio":
            raise CompilationError("native reference slots are image references; audio references are unsupported")
    for name in ("sampler", "guidance"):
        if name in value["overrides"] and value["overrides"][name] != _DEFAULTS[name]:
            raise CompilationError(f"overrides.{name} is not a public input of the native graph")
    return source_end, float(duration)


def _compile_manifest(
    *,
    request: H3Request,
    destination: Path,
    resource_root: Path,
    workflow_inputs: dict[str, Any],
    asset_bindings: Mapping[str, Path],
    capabilities: Mapping[str, Any],
    limitations: list[str],
    asset_lineage: Mapping[str, Any] | None = None,
    continuation_timing: Mapping[str, Any] | None = None,
    profile_id: str = _PROFILE_ID,
    extra_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    public_generation = capabilities.get("public_generation")
    if not isinstance(public_generation, Mapping):
        raise CompilationError("selected H3 graph is missing its sealed public generation contract")
    selectors = public_generation.get("selectors")
    if not isinstance(selectors, list) or not selectors:
        raise CompilationError("sealed H3 generation contract must declare at least one public selector")
    frozen = _copy_workflow_bundle(resource_root, destination / "workflow-bundle")
    managed_path = destination / "managed-assets.zip"
    asset_manifest = _write_asset_bundle(
        managed_path,
        asset_bindings,
        workflow_inputs=workflow_inputs,
        lineage=asset_lineage,
    )
    graph_identity = {name: {"path": str(frozen[name]), "sha256": _sha256(frozen[name])} for name in sorted(frozen)}
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "h3_av_compilation",
        "status": "compiled",
        "profile": profile_id,
        "request_digest": request.digest,
        "workflow": graph_identity,
        "workflow_inputs": workflow_inputs,
        "managed_assets": {"path": str(managed_path), "sha256": _sha256(managed_path), "manifest": asset_manifest},
        "capabilities": dict(capabilities),
        "limitations": limitations,
    }
    if extra_manifest:
        manifest.update(dict(extra_manifest))
    if continuation_timing is not None:
        manifest["continuation_timing"] = dict(continuation_timing)
    digest_payload = {
        **{key: value for key, value in manifest.items() if key not in {"workflow", "managed_assets", "compilation_digest"}},
        "workflow": {name: {"sha256": value["sha256"]} for name, value in graph_identity.items()},
        "managed_assets": {"sha256": manifest["managed_assets"]["sha256"], "manifest": asset_manifest},
    }
    manifest["compilation_digest"] = hashlib.sha256(_canonical_bytes(digest_payload)).hexdigest()
    manifest_path = destination / "compilation.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _compile_native(request: H3Request, assets: Mapping[str, Path], destination: Path) -> dict[str, Any]:
    source_end, output_duration = _require_native(request)
    try:
        timing = plan_continuation(
            source_end=source_end,
            output_duration=output_duration,
        )
    except ContinuationTimingError as exc:
        raise CompilationError(str(exc)) from exc
    bindings, asset_binding = _asset_bindings(request, assets)
    reference_count = len(request.value["references"])
    if reference_count > 2:
        raise CompilationError("the selected Seitanism graph has two image-reference slots; more references cannot be bound")
    resource_root = _NATIVE_WORKFLOW if reference_count == 0 else _REFERENCE_WORKFLOW
    managed = dict(bindings)
    if "source_video" not in managed:
        raise CompilationError("source video is missing from the managed asset bundle")
    destination.mkdir(parents=True, exist_ok=True)
    asset_manifest = _write_asset_bundle(destination / "managed-assets.zip", managed)
    members = _member_by_binding(asset_manifest)
    workflow_inputs: dict[str, Any] = {
        "model": request.value["overrides"].get("model", _DEFAULTS["model"]),
        "seed": request.value["overrides"].get("seed", _DEFAULTS["seed"]),
        "steps": request.value["overrides"].get("steps", _DEFAULTS["steps"]),
        "prompt": _request_dialogue_prompt(request),
        # The graph duration controls raw H3 latent length. Its first 39
        # frames are preserved AV context, so the requested suffix alone can
        # produce a fully preserved no-op. Ask for enough raw capacity and
        # trim the H3 block-rounding tail during composition.
        "duration": timing.workflow_duration,
        "source_start": 0.0,
        "source_frames": timing.source_frames,
        "source_video": members["source_video"],
    }
    if reference_count:
        first = members[asset_binding[request.value["references"][0]["asset"]]]
        second = members[asset_binding[request.value["references"][1]["asset"]]] if reference_count == 2 else first
        workflow_inputs.update({"reference_0": first, "reference_1": second})
    return _compile_manifest(
        request=request,
        destination=destination,
        resource_root=_pack_root() / resource_root,
        workflow_inputs=workflow_inputs,
        asset_bindings=managed,
        capabilities={"operation": "continue", "prompt": "bound", "references": reference_count, "reference_capacity": 2, "video_mask": "full-frame generated suffix only", "audio_mask": "generated suffix only", "source_range": "bound to 24fps loader frame cap", "output_duration": "exact after deterministic tail trim", "output_contract": "muxed_av_full_timeline", "public_generation": {"modality": "video", "selectors": [{"selector": "main-0", "ordinal": 0, "variant_key": "original", "required": True}], "internal_outputs": []}, "overrides": ["model", "seed", "steps"]},
        limitations=["continuation is full-frame only; spatial continuation masks are not present in the Seitanism graph", "reference slots are image guidance only and support at most two references", "sampler and guidance remain graph constants", "H3 raw lengths are quantized to 5 + 17k frames; excess generated tail frames are removed during composition"],
        continuation_timing=timing.to_dict(),
    )


def _compile_lanpaint(
    request: H3Request,
    assets: Mapping[str, Path],
    destination: Path,
    *,
    profile_id: str = _PROFILE_ID,
    native_v2_branch: str | None = None,
) -> dict[str, Any]:
    _require_lanpaint(request)
    bindings, asset_binding = _asset_bindings(request, assets, include_masks=True)
    resource_root = _pack_root() / _LANPAINT_WORKFLOW
    full_mask = resource_root / "mask_full_frame.png"
    preserve_mask = resource_root / "mask_preserve.png"
    if not full_mask.is_file() or not preserve_mask.is_file():
        raise CompilationError("packaged LanPaint mask resources are missing")
    bindings["mask_full_frame"] = full_mask
    bindings["mask_preserve"] = preserve_mask
    keyframes, audio_intervals, schedule = _lanpaint_masks(request, asset_binding=asset_binding, internal_full_frame="mask_full_frame", internal_preserve="mask_preserve")
    asset_manifest = _write_asset_bundle(destination / "managed-assets.zip", bindings)
    members = _member_by_binding(asset_manifest)
    source_id = request.value["source"]["asset"]
    workflow_inputs = {
        "model": request.value["overrides"].get("model", "minimax_h3_fl2va_pruned_fp8_scaled.safetensors"),
        "steps": request.value["overrides"].get("steps", 20),
        "seed": request.value["overrides"].get("seed", 0),
        "prompt": _request_dialogue_prompt(request),
        "duration": float(request.value["output"]["duration"]),
        "source_video": members["source_video"],
        "mask_keyframes": json.dumps({str(k): members.get(v, v) for k, v in json.loads(keyframes).items()}, separators=(",", ":")),
        "audio_intervals": audio_intervals,
    }
    capabilities = {"operation": "edit", "prompt": "bound", "source_range": request.value["source"]["range"], "output_duration": float(request.value["output"]["duration"]), "video_mask": "LanPaint frame keyframes", "audio_mask": "independent sample intervals", "references": 0, "output_contract": "separate_av_full_timeline", "public_generation": {"modality": "video", "selectors": [{"selector": "main-0", "ordinal": 0, "variant_key": "original", "required": True}], "internal_outputs": ["audio"]}, "overrides": ["model", "seed", "steps"]}
    extra_manifest = None
    if native_v2_branch is not None:
        capabilities.update({
            "native_v2_branch": native_v2_branch,
            "source_binding": "source_video",
            "edit_binding": ["mask_keyframes", "audio_intervals"],
        })
        extra_manifest = {"branch": native_v2_branch}
    result = _compile_manifest(
        request=request,
        destination=destination,
        resource_root=resource_root,
        workflow_inputs=workflow_inputs,
        asset_bindings=bindings,
        capabilities=capabilities,
        limitations=["regional masks must be supplied image masks; rectangle, polygon, and semantic regions are not rasterized", "source-backed edits use source.range=[0, output.duration] because LanPaint has no arbitrary source-window input", "audio stems and audio mask assets require a separator graph"],
        profile_id=profile_id,
        extra_manifest=extra_manifest,
    )
    result["mask_schedule"] = schedule
    return result


def _compile_source_backed_v2(
    request: H3Request, assets: Mapping[str, Path], destination: Path
) -> dict[str, Any]:
    """Adapt non-continuation permissions to the graph's actual AV masks.

    The continuation graph only samples a suffix. LanPaint instead edits the
    source timeline with independent frame keyframes and audio intervals.
    Keep the native request identity while reusing the established edit binder.
    """
    value = request.value
    timelines = [item for item in value["media"] if item["role"] == "timeline"]
    if len(timelines) != 1 or timelines[0].get("modality") != "video":
        raise CompilationError("native-v2 source-backed edits require one timeline video; no separate audio input is bound")
    source = timelines[0]
    if source["resolved_at"]["value"] != 0:
        raise CompilationError("native-v2 source-backed edits require their timeline video at frame 0")
    if not isinstance(source.get("range"), list) or len(source["range"]) != 2:
        raise CompilationError("native-v2 source-backed edits require a bounded timeline video range")
    changes: dict[str, list[dict[str, Any]]] = {"video": [], "audio": []}
    for edit in source.get("edit", []):
        stream = edit["stream"]
        change = {"during": edit["during"], "action": edit["action"]}
        if stream == "video":
            if edit["mask"].get("full_frame") is not True:
                raise CompilationError("native-v2 source-backed composition supports only full_frame masks")
            change["area"] = {"full_frame": True}
        else:
            if edit.get("channels", "all") != "all":
                raise CompilationError("native-v2 source-backed edits have no channel-specific audio mask binding")
            if "text" in edit:
                change["dialogue"] = edit["text"]
        changes[stream].append(change)
    settings = effective_settings(request)
    if settings["scheduler"] != "simple":
        raise CompilationError("native-v2 source-backed LanPaint graph has no scheduler input")
    if not settings["turbo"]["enabled"]:
        raise CompilationError("native-v2 source-backed LanPaint graph cannot disable its existing model adapter")
    edit_value = {
        "version": 1,
        "operation": "edit",
        "source": {"asset": source["asset"], "range": source.get("range")},
        "output": {"duration": value["duration"]},
        "content": {"prompt": _native_v2_prompt(request)},
        "changes": changes,
        "references": [{"asset": item["asset"], "purpose": "appearance"}
                       for item in value["media"] if item["role"] == "reference"],
        "overrides": {"model": settings["model"], "steps": settings["steps"],
                      "seed": settings["seed"], "sampler": settings["sampler"],
                      "guidance": settings["turbo"]["strength"]},
    }
    return _compile_lanpaint(
        H3Request(value=edit_value, digest=request.digest), assets, destination,
        profile_id="h3_av.native.v2", native_v2_branch="source_backed_v2v",
    )


def _compile_character_swap_v2(
    request: H3Request,
    assets: Mapping[str, Path],
    destination: Path,
    *,
    branch: str,
) -> dict[str, Any]:
    """Compile the opt-in adapter through the current H3 AV generation path.

    Timeline sources stay in the preparation/compose/verify path. The sampling
    graph receives only the typed Character Swap references, so composition
    continues to own the exact source audio and delivery-mask permissions.
    """
    from astrid.packs.h3_av.shared.generation import (
        CHARACTER_SWAP_FILENAME,
        CHARACTER_SWAP_REPOSITORY,
        CHARACTER_SWAP_REVISION,
        TURBO_FILENAME,
        character_swap_asset_sha256,
        build_generation_workflow,
        generation_timing,
    )
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import emit_bundle

    value = request.value
    settings = effective_settings(request)
    if branch not in {"source_free", "source_backed_v2v"}:
        raise CompilationError(f"character_swap_v1 does not support the {branch} branch")
    if settings["model"] != REF2VA_MODEL:
        raise CompilationError(f"character_swap_v1 requires Ref2VA model {REF2VA_MODEL!r}; the selected model is not changed")
    if settings.get("adapter", {}).get("id") != CHARACTER_SWAP_ID:
        raise CompilationError("character_swap_v1 route is missing its selected adapter")
    try:
        adapter_sha256 = character_swap_asset_sha256()
    except ValueError as exc:
        raise CompilationError(str(exc)) from exc

    # The existing composition engine applies temporal permissions, supports
    # supplied image masks, and preserves source audio. It cannot rasterize
    # rectangle/polygon geometry, so keep those inputs fail-closed.
    for item in value["media"]:
        if item["role"] != "timeline":
            continue
        for edit_index, edit in enumerate(item.get("edit", [])):
            if edit.get("stream") == "video":
                mask = edit.get("mask", {})
                if mask.get("full_frame") is not True and not (
                    isinstance(mask, Mapping)
                    and isinstance(mask.get("asset"), str)
                    and mask.get("asset") in assets
                ):
                    raise CompilationError(
                        f"character_swap_v1 media[{item['occurrence_id']}].edit[{edit_index}] "
                        "requires a full_frame mask or a resolved image mask asset; "
                        "rectangle and polygon masks are not rasterized"
                    )
            if edit.get("stream") == "audio" and edit.get("channels", "all") != "all":
                raise CompilationError(
                    f"character_swap_v1 media[{item['occurrence_id']}].edit[{edit_index}] requests channel-specific audio; the current composer supports whole-stream audio intervals only"
                )

    references = [item for item in value["media"] if item["role"] == "reference"]
    images = [item for item in references if item.get("modality") == "image"]
    videos = [item for item in references if item.get("modality") == "video"]
    if len(images) != 1 or len(videos) != 1 or len(references) != 2:
        raise CompilationError("character_swap_v1 requires one typed video reference and one typed replacement image reference")
    bindings: dict[str, Path] = {}
    for index, item in enumerate(images):
        asset_id = str(item["asset"])
        if asset_id not in assets:
            raise CompilationError(f"character_swap_v1 image reference {asset_id!r} is missing from preparation")
        bindings[f"reference_{index}"] = assets[asset_id]
    video_reference_rows: list[dict[str, Any]] = []
    for index, item in enumerate(videos):
        asset_id = str(item["asset"])
        if asset_id not in assets:
            raise CompilationError(f"character_swap_v1 video reference {asset_id!r} is missing from preparation")
        binding = f"video_reference_{index}"
        bindings[binding] = assets[asset_id]
        raw_range = item.get("range")
        resolved_range = item.get("resolved_range")
        video_reference_rows.append({
            "binding": binding,
            "start_time": float(raw_range[0]) if isinstance(raw_range, list) else 0.0,
            "frame_load_cap": int(resolved_range[1]) - int(resolved_range[0]) if isinstance(resolved_range, list) else 0,
            "audio": item.get("audio", True),
        })

    try:
        timing = generation_timing(float(value["duration"]))
    except ValueError as exc:
        raise CompilationError(str(exc)) from exc
    prompt = _native_v2_prompt(request)
    turbo = settings["turbo"]
    adapter = settings["adapter"]
    graph = build_generation_workflow(
        references=[f"reference_{index}" for index in range(len(images))],
        video_references=video_reference_rows,
        prompt=prompt,
        frames=timing["raw_frames"],
        model=settings["model"],
        steps=settings["steps"],
        seed=settings["seed"],
        sampler=settings["sampler"],
        scheduler=settings["scheduler"],
        guidance=turbo["strength"],
        adapter_strength=adapter["strength"],
        turbo_enabled=turbo["enabled"],
        character_swap_sha256=adapter_sha256,
    )

    # Reconcile the declaration against connected loaders. This prevents a
    # disabled Turbo node from leaving a stale model download requirement.
    from vibecomfy.model_assets import resolve_referenced_assets

    active_assets, unresolved = resolve_referenced_assets(graph)
    if unresolved:
        raise CompilationError(f"H3 model requirements are unresolved: {unresolved!r}")
    declared_rows = graph.metadata.get("model_assets", [])
    declared_names = {
        str(row["name"]) for row in declared_rows if isinstance(row, Mapping) and isinstance(row.get("name"), str)
    }
    active_names = {str(row["name"]) for row in active_assets}
    requirement_rows = graph.requirements.models
    requirement_names = {
        str(row["name"]) for row in requirement_rows if isinstance(row, Mapping) and isinstance(row.get("name"), str)
    }
    if declared_names != active_names or requirement_names != active_names:
        raise CompilationError("H3 workflow model requirements do not match the connected model chain")
    assets_by_name = {str(row["name"]): dict(row) for row in active_assets}
    adapter_asset = assets_by_name.get(CHARACTER_SWAP_FILENAME)
    if adapter_asset is None or adapter_asset.get("sha256") != adapter_sha256 or adapter_asset.get("hf_revision") != CHARACTER_SWAP_REVISION:
        raise CompilationError("connected Character Swap loader does not resolve to the immutable pinned asset")
    if bool(TURBO_FILENAME in assets_by_name) != bool(turbo["enabled"]):
        raise CompilationError("Turbo loader and managed model requirement disagree")

    bindings_manifest = build_asset_manifest(bindings)
    members = _member_by_binding(bindings_manifest)
    destination.mkdir(parents=True, exist_ok=True)
    workflow_resource = destination / "character-swap-bundle"
    emit_bundle(graph, workflow_resource / "workflow.py", provenance=Provenance.AGENT_AUTHORED)
    (workflow_resource / "source.json").write_text(json.dumps({
        "kind": "authored_h3_character_swap_adapter",
        "node": "MiniMaxH3ReferenceToVideo",
        "comfy_commit": "ee71d5c4993f29086b27fde1629a945ae48425bf",
    }, sort_keys=True) + "\n", encoding="utf-8")
    workflow_inputs: dict[str, Any] = {
        "model": settings["model"],
        "steps": settings["steps"],
        "seed": settings["seed"],
        "sampler": settings["sampler"],
        "scheduler": settings["scheduler"],
        "prompt": prompt,
        **members,
    }
    if turbo["enabled"]:
        workflow_inputs["guidance"] = turbo["strength"]
    asset_manifest = _write_asset_bundle(destination / "managed-assets.zip", bindings, workflow_inputs=workflow_inputs)
    workflow_identity = {name: {"path": str(path), "sha256": _sha256(path)} for name, path in {
        "workflow.py": workflow_resource / "workflow.py",
        "workflow.vibe.json": workflow_resource / "workflow.vibe.json",
        "source.json": workflow_resource / "source.json",
    }.items()}
    effective_configuration = {
        "schema_version": 1,
        "base_model": assets_by_name[settings["model"]],
        "adapter": {
            "id": CHARACTER_SWAP_ID,
            "repository": CHARACTER_SWAP_REPOSITORY,
            "revision": CHARACTER_SWAP_REVISION,
            "filename": CHARACTER_SWAP_FILENAME,
            "sha256": adapter_sha256,
            "strength": adapter["strength"],
            "asset": adapter_asset,
        },
        "turbo": {
            "enabled": turbo["enabled"],
            "strength": turbo["strength"] if turbo["enabled"] else None,
            "asset": assets_by_name.get(TURBO_FILENAME) if turbo["enabled"] else None,
        },
        "sampling": {key: settings[key] for key in ("steps", "seed", "sampler", "scheduler")},
        "model_processing": {
            "attention": "comfy kitchen attention",
            "sigma_shift": {"video": 12, "audio": 3},
            "denoise": 1,
        },
        "connected_model_assets": [dict(row) for row in active_assets],
    }
    public_generation = {"modality": "video", "selectors": [
        {"selector": "main-0", "ordinal": 0, "variant_key": "original", "required": True}],
        "internal_outputs": []}
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "h3_av_compilation",
        "status": "compiled",
        "profile": "h3_av.native.v2",
        "request_digest": request.digest,
        "workflow": workflow_identity,
        "workflow_inputs": workflow_inputs,
        "managed_assets": {"path": str(destination / "managed-assets.zip"), "sha256": _sha256(destination / "managed-assets.zip"), "manifest": asset_manifest},
        "capabilities": {
            "operation": "transform",
            "native_v2_branch": branch,
            "character_swap": {"id": CHARACTER_SWAP_ID, "strength": adapter["strength"]},
            "references": 2,
            "reference_video": 1,
            "reference_image": 1,
            "reference_tags": {"video": "<Video 1>", "image": "<Picture 1>"},
            "output_contract": "muxed_av_full_timeline",
            "source_audio": "preserved by existing composition permissions when a timeline video is present",
            "masks": "preserved by normalized native-v2 preparation and existing composition permissions",
            "public_generation": public_generation,
            "overrides": ["model", "steps", "seed", "sampler", "scheduler", "turbo"],
        },
        "limitations": [
            "Character Swap is opt-in; this compilation does not claim inference quality or live GPU acceptance",
            "the source timeline remains a composition baseline; reference conditioning uses the declared video and replacement image",
            "continuation, audio-only timelines, separate timeline audio, and image timeline anchors are unsupported for this adapter",
        ],
        "effective_configuration": effective_configuration,
        "generation_timing": timing,
        "branch": branch,
    }
    digest_payload = {
        **{key: item for key, item in manifest.items() if key not in {"workflow", "managed_assets", "compilation_digest"}},
        "workflow": {name: {"sha256": row["sha256"]} for name, row in workflow_identity.items()},
        "managed_assets": {"sha256": manifest["managed_assets"]["sha256"], "manifest": asset_manifest},
    }
    manifest["compilation_digest"] = hashlib.sha256(_canonical_bytes(digest_payload)).hexdigest()
    manifest_path = destination / "compilation.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _compile_native_v2(
    request: H3Request, assets: Mapping[str, Path], destination: Path
) -> dict[str, Any]:
    """Bind native-v2 media semantics to existing declared Vibe inputs."""
    branch = branch_for(request)
    settings = effective_settings(request)
    if not assets:
        raise CompilationError("native-v2 requires resolved media assets")

    if settings.get("adapter", {}).get("id") == CHARACTER_SWAP_ID:
        return _compile_character_swap_v2(request, assets, destination, branch=branch)

    if branch == "source_free":
        references = [item for item in request.value["media"] if item["role"] == "reference"]
        if not 1 <= len(references) <= 9:
            raise CompilationError("native-v2 source-free generation requires one to nine references")
        if any(item.get("modality") != "image" for item in references):
            raise CompilationError("native-v2 source-free generation accepts image references only")
        generation_value = {
            "version": 1,
            "operation": "generate",
            "source": None,
            "output": {"duration": request.value["duration"]},
            "content": {"prompt": request.value["prompt"]},
            "changes": {"video": [], "audio": []},
            "references": [{"asset": item["asset"], "purpose": "appearance"} for item in references],
            "overrides": {
                "model": settings["model"],
                "steps": settings["steps"],
                "sampler": settings["sampler"],
                "seed": settings["seed"],
                "guidance": settings["turbo"]["strength"],
            },
        }
        return _compile_generation(
            H3Request(value=generation_value, digest=request.digest),
            assets,
            destination,
            profile_id="h3_av.native.v2",
            native_v2_branch=branch,
            native_settings=settings,
        )

    if branch == "audio_only":
        raise CompilationError(
            "native-v2 audio-only is unsupported by the selected continuation "
            "workflow: its source_video port is video-semantic and feeds "
            "VHS_LoadVideoFFmpeg; no timeline-audio input is bound"
        )

    if branch == "source_backed_v2v":
        return _compile_source_backed_v2(request, assets, destination)

    destination.mkdir(parents=True, exist_ok=True)
    if settings["sampler"] != _DEFAULTS["sampler"]:
        raise CompilationError("native-v2 continuation graph has no sampler input")
    if settings["scheduler"] != "simple":
        raise CompilationError("native-v2 continuation graph has no scheduler input")
    if settings["turbo"]["strength"] != _DEFAULTS["guidance"] or not settings["turbo"]["enabled"]:
        raise CompilationError("native-v2 continuation graph has no guidance input")

    timelines = [item for item in request.value["media"] if item["role"] == "timeline"]
    references = [item for item in request.value["media"] if item["role"] == "reference"]
    if len(references) > 2:
        raise CompilationError("native-v2 continuation graph has two image-reference slots")
    if any(item.get("modality") != "image" for item in references):
        raise CompilationError("native-v2 continuation graph reference ports accept image references only")
    video_timelines = [item for item in timelines if item.get("modality") == "video"]
    if len(video_timelines) != 1:
        raise CompilationError("native-v2 continuation binding requires one timeline video asset")
    if any(item.get("modality") == "audio" for item in timelines):
        raise CompilationError("native-v2 continuation graph has no separate audio input port")
    source = video_timelines[0]
    if source["resolved_at"]["value"] != 0:
        raise CompilationError(
            "native-v2 continuation requires its timeline video at frame 0"
        )

    source_id = str(source["asset"])
    if source_id not in assets:
        raise CompilationError(f"native-v2 asset {source_id!r} is missing from preparation")
    bindings: dict[str, Path] = {"source_video": assets[source_id]}
    reference_ids: list[str] = []
    for item in references:
        asset_id = str(item["asset"])
        if asset_id not in assets:
            raise CompilationError(f"native-v2 asset {asset_id!r} is missing from preparation")
        reference_ids.append(asset_id)

    for item in request.value["media"]:
        for edit in item.get("edit", []):
            if edit.get("stream") == "video":
                mask = edit.get("mask", {})
                if not isinstance(mask, Mapping) or mask.get("full_frame") is not True:
                    raise CompilationError(
                        "native-v2 continuation graph has no declared spatial-mask input; only full_frame masks are supported"
                    )

    source_range = source.get("resolved_range")
    if not isinstance(source_range, list) or len(source_range) != 2:
        raise CompilationError(
            "native-v2 continuation requires a bounded timeline video range"
        )
    source_start = float(source["range"][0])
    source_frames = int(source_range[1]) - int(source_range[0])
    source_end = source_frames / 24.0

    try:
        timing = plan_continuation(
            source_end=source_end,
            output_duration=float(request.value["duration"]),
        )
    except ContinuationTimingError as exc:
        raise CompilationError(str(exc)) from exc
    continuation_timing: dict[str, Any] | None = timing.to_dict()
    workflow_duration = timing.workflow_duration

    if reference_ids:
        bindings["reference_0"] = assets[reference_ids[0]]
        bindings["reference_1"] = assets[reference_ids[1] if len(reference_ids) == 2 else reference_ids[0]]
    members = _member_by_binding(build_asset_manifest(bindings))
    workflow_inputs: dict[str, Any] = {
        "model": settings["model"],
        "steps": settings["steps"],
        "seed": settings["seed"],
        "prompt": _native_v2_prompt(request),
        "duration": workflow_duration,
        "source_start": source_start,
        "source_frames": source_frames,
        "source_video": members["source_video"],
    }
    if reference_ids:
        workflow_inputs.update({"reference_0": members["reference_0"], "reference_1": members["reference_1"]})
    resource_root = _REFERENCE_WORKFLOW if reference_ids else Path("workflows/native_h3_continuation")
    return _compile_manifest(
        request=request,
        destination=destination,
        resource_root=_pack_root() / resource_root,
        workflow_inputs=workflow_inputs,
        asset_bindings=bindings,
        capabilities={
            "operation": "transform",
            "native_v2_branch": branch,
            "source_binding": "source_video",
            "audio_binding": "source_audio_from_source_video",
            "timing_binding": ["source_start", "source_frames", "duration"],
            "edit_binding": "prompt schedule",
            "mask_binding": "full_frame through graph context",
            "references": len(reference_ids),
            "reference_capacity": 2,
            "output_contract": "muxed_av_full_timeline",
            "public_generation": {"modality": "video", "selectors": [
                {"selector": "main-0", "ordinal": 0, "variant_key": "original", "required": True}],
                "internal_outputs": []},
        },
        limitations=[
            "native-v2 branch binding is CPU-verifiable; model quality and GPU compatibility remain unqualified",
            "the selected continuation graph exposes no separate audio or spatial-mask port; audio-only and non-full-frame inputs fail closed",
            "raw H3 output remains internal lineage and must pass composition before publication",
        ],
        continuation_timing=continuation_timing,
        profile_id="h3_av.native.v2",
        extra_manifest={"branch": branch},
    )


def _native_v2_prompt(request: H3Request) -> str:
    """Carry normalized edit/audio/timing semantics through the graph prompt port."""

    schedule = []
    for item in request.value["media"]:
        schedule.append({
            key: item[key]
            for key in ("occurrence_id", "role", "modality", "at", "range", "edit")
            if key in item
        })
    return f"{request.value['prompt']}\n\nNative-v2 media schedule:\n{json.dumps(schedule, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"


def compile_preparation(preparation: Mapping[str, Any], *, out_dir: str | Path) -> dict[str, Any]:
    """Compile one prepared request into immutable runtime bindings and assets."""
    request = _resolved_request(preparation)
    assets = _asset_paths(preparation)
    destination = Path(out_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if request.value.get("version") == 2:
        return _compile_native_v2(request, assets, destination)
    if request.value["operation"] == "generate":
        return _compile_generation(request, assets, destination)
    if request.value["operation"] == "edit":
        return _compile_lanpaint(request, assets, destination)
    if request.value["operation"] == "continue":
        return _compile_native(request, assets, destination)
    raise CompilationError(f"unsupported H3 operation: {request.value['operation']!r}")


def _compile_generation(
    request: H3Request,
    assets: Mapping[str, Path],
    destination: Path,
    *,
    profile_id: str = _PROFILE_ID,
    native_v2_branch: str | None = None,
    native_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from PIL import Image
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import emit_bundle
    from astrid.packs.h3_av.shared.generation import IMAGE_REFERENCE_CAPACITY, build_generation_workflow, generation_timing

    references = request.value["references"]
    # Keep slots ordered even for repeated assets; the archive deduplicates bytes.
    bindings = {f"reference_{index}": assets[item["asset"]] for index, item in enumerate(references)}
    for binding, path in bindings.items():
        try:
            with Image.open(path) as image:
                if getattr(image, "n_frames", 1) != 1:
                    raise CompilationError(f"{binding} must be a still image")
                image.verify()
        except (OSError, ValueError) as exc:
            raise CompilationError(f"{binding} must be a decodable still image") from exc
    manifest = _write_asset_bundle(destination / "managed-assets.zip", bindings)
    members = _member_by_binding(manifest)
    if native_settings is None:
        settings = {**_DEFAULTS, **request.value["overrides"]}
        scheduler = "simple"
        turbo_enabled = True
        adapter_strength = None
    else:
        settings = {
            "model": native_settings["model"],
            "steps": native_settings["steps"],
            "seed": native_settings["seed"],
            "sampler": native_settings["sampler"],
            "guidance": native_settings["turbo"]["strength"],
        }
        scheduler = native_settings["scheduler"]
        turbo_enabled = native_settings["turbo"]["enabled"]
        adapter_strength = native_settings.get("adapter", {}).get("strength")
    timing = generation_timing(request.value["output"]["duration"])
    prompt = request.value["content"]["prompt"]
    graph = build_generation_workflow(
        references=list(members.values()), prompt=prompt,
        frames=timing["raw_frames"], model=settings["model"],
        steps=settings["steps"], seed=settings["seed"],
        sampler=settings["sampler"], guidance=settings["guidance"],
        scheduler=scheduler, turbo_enabled=turbo_enabled,
        adapter_strength=adapter_strength,
    )
    if len(graph.outputs) != 1 or graph.outputs[0].expected_cardinality != "one":
        raise CompilationError("generation adapter must expose exactly one output")
    resource = destination / "generation-bundle"
    emit_bundle(graph, resource / "workflow.py", provenance=Provenance.AGENT_AUTHORED)
    (resource / "source.json").write_text(json.dumps({
        "kind": "authored_h3_reference_adapter", "node": "MiniMaxH3ReferenceToVideo",
        "comfy_commit": "ee71d5c4993f29086b27fde1629a945ae48425bf",
    }, sort_keys=True) + "\n")
    capabilities = {
        "operation": "generate",
        "reference_capacity": IMAGE_REFERENCE_CAPACITY,
        "references": len(references),
        "reference_order": [item["asset"] for item in references],
        "reference_binding": "ordered image conditioning; Picture 1..N",
        "generation_timing": timing,
        "output_contract": "muxed_av_full_timeline",
        "public_generation": {"modality": "video", "selectors": [
            {"selector": "main-0", "ordinal": 0, "variant_key": "original", "required": True}],
            "internal_outputs": []},
        "validation": "CPU structural; live GPU acceptance pending",
    }
    extra_manifest = None
    if native_v2_branch is not None:
        capabilities.update({"operation": "transform", "native_v2_branch": native_v2_branch})
        extra_manifest = {"branch": native_v2_branch}
    workflow_inputs = {**settings, "prompt": prompt, **members}
    workflow_inputs["scheduler"] = scheduler
    if native_settings is not None:
        if not turbo_enabled:
            workflow_inputs.pop("guidance", None)
    return _compile_manifest(
        request=request, destination=destination, resource_root=resource,
        workflow_inputs=workflow_inputs, asset_bindings=bindings,
        capabilities=capabilities,
        limitations=["image references only; no timed keyframes or source-free masks", "single pass, up to 362 frames at 24 fps"],
        profile_id=profile_id,
        extra_manifest=extra_manifest,
    )


__all__ = ["CompilationError", "compile_preparation"]
