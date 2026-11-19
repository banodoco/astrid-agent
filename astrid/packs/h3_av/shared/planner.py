"""Bounded CPU planning over the canonical spec and existing H3 compiler.

Artifacts live under the caller's output directory. No runtime, engine endpoint,
worker selection or journal is opened here. Compiler restrictions remain visible
as blocking diagnostics; planning does not add new executable routes.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from .compile import CompilationError, compile_preparation
from .engine_facts import facts
from .generation import generation_timing
from .prepare import prepare_request, write_preparation
from .request import H3Request, H3RequestError
from .request_v2 import json_times
from .spec import H3Spec, canonical_spec, sampling_request
from .static_regions import resolve_static_region
from .timing import plan_continuation


def _timing(spec: H3Spec, compilation: Mapping[str, Any]) -> dict[str, Any]:
    continuation = compilation.get("continuation_timing")
    if any(op.op == "extend" for op in spec.ops):
        if continuation is None:
            continuation = plan_continuation(
                source_end=spec.source.range[1] - spec.source.range[0],
                output_duration=spec.duration,
            ).to_dict()
        details = dict(continuation)
        requested = details["requested_output_frames"]
        raw = details["raw_extension_frames"]
        output = details["expected_graph_output_frames"]
        context = details["context_frames"]
    else:
        details = dict(compilation.get("capabilities", {}).get("generation_timing")
                       or generation_timing(spec.duration))
        requested, raw = details["requested_frames"], details["raw_frames"]
        output, context = raw, 0
    fps, trim = details["fps"], details["trim_tail_frames"]
    return {
        "fps": fps,
        "requested": {"frames": requested, "duration": requested / fps},
        "raw": {"frames": raw, "duration": raw / fps, "context_frames": context,
                "output_frames": output, "output_duration": output / fps},
        "trim": {"tail_frames": trim, "duration": trim / fps},
        "details": details,
    }


def _ignored_fields(request: H3Request, spec: H3Spec, compilation: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expose validated constants and disconnected controls of a compiled route."""
    value = request.value
    ignored = []
    if value["version"] == 1:
        supported = compilation.get("capabilities", {}).get("overrides")
        if supported is not None:
            for name, setting in value["overrides"].items():
                if name not in supported:
                    ignored.append({"field": f"overrides.{name}", "value": setting,
                                    "reason": "Validated graph constant; no runtime input"})
    elif compilation.get("continuation_timing"):
        settings = value["settings"]
        for name in ("sampler", "scheduler"):
            ignored.append({"field": f"settings.{name}", "value": settings[name],
                            "reason": "Validated graph constant; no runtime input"})
        for name in ("enabled", "strength"):
            ignored.append({"field": f"settings.turbo.{name}", "value": settings["turbo"][name],
                            "reason": "Validated graph constant; no runtime input"})
    elif value["settings"]["turbo"]["enabled"] is False:
        ignored.append({"field": "settings.turbo.strength", "value": value["settings"]["turbo"]["strength"],
                        "reason": "Turbo is disabled; its strength is disconnected"})
    return ignored


def plan_request(
    request: H3Request, *, asset_map: Mapping[str, str] | None = None, out_dir: str | Path,
) -> dict[str, Any]:
    """Write inspectable ordered single-operation plans without submitting work.

    Use the existing request normalizer before calling. A plan may be blocked by
    the current compiler (for example a box awaiting the native binder); such a
    plan records the error and must not be treated as an executable compilation.
    """
    root = Path(out_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    # A failed replacement must not leave an older successful plan visible.
    plan_path = root / "plan.json"
    plan_path.unlink(missing_ok=True)
    spec = canonical_spec(request)
    passes = spec.sampling_passes()
    if not passes:
        raise H3RequestError(
            "Planning requires a modifying operation", code="h3.plan.single_pass", field="ops",
            fix="Use a generate, extend or regenerate operation plus preservation constraints",
        )
    joint_still = spec.source is not None and spec.source.modality == "image"
    if len(passes) > 1 and not joint_still and request.value["version"] == 1:
        # Every pass uses the existing single-operation planner/compiler. Later
        # compilation here is an inspectable preview: execution must replace its
        # source binding with the preceding composed and verified candidate.
        plans = [plan_request(
            sampling_request(request, index, previous_verified_output=index > 0),
            asset_map=asset_map, out_dir=root / "passes" / f"{index + 1:02d}",
        ) for index in range(len(passes))]
        preparation = prepare_request(request, asset_map=asset_map)
        preparation_path = write_preparation(root / "preparation.json", preparation)
        compiled = all(item["runtime_submission"] == "eligible" for item in plans)
        plan = {
            "schema_version": 1, "kind": "h3_av_plan", "status": "planned",
            "request_digest": request.digest, "spec": asdict(spec),
            "bindings": {"assets": preparation["assets"], "regions": [], "runtime": []},
            "timing": plans[0]["timing"], "previews": [preview for item in plans for preview in item["previews"]],
            "pass_count": len(passes), "passes": [
                {**asdict(sampling_pass), "plan_path": item["plan_path"],
                 "request_digest": item["request_digest"], "compilation": item["compilation"],
                 "source_binding": "original_source" if index == 0 else "deferred_until_previous_verified"}
                for index, (sampling_pass, item) in enumerate(zip(passes, plans))
            ],
            "mask_schedule": preparation["mask_schedule"],
            "warnings": [warning for item in plans for warning in item["warnings"]],
            "ignored_fields": [field for item in plans for field in item["ignored_fields"]],
            "compilation": {"status": "compiled" if compiled else "blocked", "ordered_passes": len(passes)},
            "preparation_path": str(preparation_path),
            "runtime_submission": "eligible" if compiled else "blocked_until_compilation",
            "capabilities": spec.capabilities, "plan_path": str(plan_path),
        }
        plan = json_times(plan)
        facts().write_json_atomic(plan_path, plan)
        return plan
    preparation = prepare_request(request, asset_map=asset_map)
    if preparation["unresolved_assets"]:
        raise H3RequestError(
            f"Plan assets are unresolved: {preparation['unresolved_assets']}",
            code="h3.plan.unresolved_assets", field="asset_map",
            fix="Map every declared asset to an existing local file",
        )
    assets = {row["asset"]: row["path"] for row in preparation["assets"]}
    # Derived views retain host-approved use classifications for inspection;
    # the original joint request alone determines compilation and eligibility.
    pass_views = []
    if request.value["version"] == 2 and len(passes) > 1 and not joint_still:
        for index, sampling_pass in enumerate(passes):
            derived = sampling_request(request, index, previous_verified_output=index > 0)
            directory = root / "passes" / f"{index + 1:02d}"
            directory.mkdir(parents=True, exist_ok=True)
            derived_preparation = deepcopy(preparation)
            derived_preparation["request"] = derived.value
            derived_preparation["request_digest"] = derived.digest
            derived_preparation["kind"] = "h3_av_derived_pass_view"
            derived_preparation["status"] = "inspection_only"
            derived_preparation["runtime_submission"] = "blocked_inspection_only"
            derived_preparation["joint_preparation"] = {
                "path": str(root / "preparation.json"), "request_digest": request.digest,
                "reason": "Only the joint request is admitted and compiled for execution",
            }
            path = write_preparation(directory / "preparation.json", derived_preparation)
            pass_views.append({**asdict(sampling_pass), "request_digest": derived.digest,
                               "preparation_path": str(path),
                               "source_binding": "original_source" if index == 0 else "deferred_until_previous_verified"})
    regions, previews = [], []
    for index, region in enumerate(spec.regions):
        media = preparation["media"]
        if region.mask_asset and request.value["version"] == 2:
            from .compose import probe_mask
            info = probe_mask(Path(assets[region.mask_asset]), mask_id=region.mask_asset)
            if info["kind"] == "video":
                regions.append({"name": region.name, "asset": region.mask_asset,
                                "mask": assets[region.mask_asset], "kind": "video",
                                "frames": info["frames"]})
                continue
        resolved = resolve_static_region(
            region, width=media["width"], height=media["height"], assets=assets,
            polarity="black_preserve_white_edit" if region.mask_asset else None,
        )
        directory = root / "regions"
        directory.mkdir(exist_ok=True)
        mask, preview = directory / f"{index + 1}-mask.png", directory / f"{index + 1}-preview.png"
        mask.write_bytes(resolved.mask_png)
        preview.write_bytes(resolved.preview_png)
        regions.append({"name": region.name, "asset": region.mask_asset, "mask": str(mask),
                        "sha256": hashlib.sha256(resolved.mask_png).hexdigest(),
                        "width": resolved.width, "height": resolved.height})
        previews.append({"region": region.name, "path": str(preview),
                         "sha256": hashlib.sha256(resolved.preview_png).hexdigest()})
    preparation_path = write_preparation(root / "preparation.json", preparation)
    warnings = [{"code": "h3.plan.gpu_unqualified",
                 "message": "CPU planning does not establish GPU compatibility or model quality"}]
    warnings.extend({"code": "h3.plan.preparation_warning", **row} for row in preparation.get("warnings", []))
    compiled: dict[str, Any] = {}
    try:
        compiled = compile_preparation(preparation, out_dir=root / "compilation")
    except CompilationError as exc:
        error = H3RequestError(
            str(exc), code="h3.plan.compile_unavailable", field="request",
            fix="Use inputs supported by the existing compiler before execution",
        )
        compilation = {"status": "blocked", "error": error.to_envelope()}
        warnings.append({"code": "h3.plan.compile_unavailable", "message": str(exc)})
    else:
        compilation = {"status": "compiled", **{
            key: compiled[key] for key in (
                "profile", "compilation_digest", "manifest_path", "workflow_inputs", "capabilities", "limitations"
            )
        }}
        if "branch" in compiled:
            compilation["branch"] = compiled["branch"]
    timing = _timing(spec, compiled)
    if timing["trim"]["tail_frames"]:
        warnings.append({"code": "h3.plan.tail_trim", "message":
                         f"Composition removes {timing['trim']['tail_frames']} excess H3 tail frames"})
    plan = {
        "schema_version": 1, "kind": "h3_av_plan", "status": "planned",
        "request_digest": request.digest, "spec": asdict(spec),
        "bindings": {"assets": preparation["assets"], "regions": regions,
                     "runtime": compiled.get("managed_assets", {}).get("manifest", {}).get("assets", [])},
        "timing": timing, "previews": previews, "pass_count": 1 if joint_still else len(passes),
        "passes": ([{"operations": [asdict(item.operation) for item in passes], "reads": "source"}]
                   if joint_still else (pass_views or [asdict(item) for item in passes])),
        "mask_schedule": preparation["mask_schedule"],
        "warnings": warnings, "ignored_fields": _ignored_fields(request, spec, compiled) if compiled else [],
        "compilation": compilation, "preparation_path": str(preparation_path),
        "runtime_submission": "eligible" if compiled else "blocked_until_compilation",
        "capabilities": spec.capabilities,
        "plan_path": str(plan_path),
    }
    plan = json_times(plan)
    facts().write_json_atomic(plan_path, plan)
    return plan


__all__ = ["plan_request"]
