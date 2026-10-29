#!/usr/bin/env python3
"""Generate videos from text prompts using local (vibecomfy) or cloud (fal) backends.


v2: model → mode → backend taxonomy.  ``--mode`` is required.
Backend dispatch goes through ``BackendAdapter``.
Features are validated per-mode.

Per-mode requires:
  t2v → prompt
  i2v → prompt + image_ref
  flf → prompt + image_ref + image_end_ref

v2v and video-edit are not wired this sprint.
"""

from __future__ import annotations

from astrid.core.pack.entrypoint import guard_canonical_entrypoint, warn_if_unledgered

guard_canonical_entrypoint('generation.generate_video')
import argparse
import hashlib
import json
import logging
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.core._shared.result_manifest import complete_output_metadata
from astrid.core.cli_choices import add_choice_arg
from astrid.core.contracts.errors import AstridError
from astrid.core.foundation.atomic_io import write_json_atomic
from astrid.core.generation import GENERATION_RESULT_KEY
from astrid.core.generation.backends import (
    BackendAdapter,
    GenerationBackendRegistry,
    GenerationResult,
    load_default_generation_backend_registry,
)
from astrid.core.media import ffprobe_metadata
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.packs.generation.shared._common import (
    _PROMPT_ENTRY_CONTROL_KEYS,
    _available_backend_ids,
    _check_required,
    _create_backend_adapter,
    _drop_unsupported,
    _feature_is_missing,
    _load_prompts,
    _manifest_path_for_run_dir,
    _normalise_prompts,
    _resolve_seed,
    build_generation_manifest,
)
from astrid.packs.generation.shared._common import (
    _build_requested_params as _build_requested_params_base,
)
from astrid.packs.generation.shared._common import (
    _coerce_args as _coerce_args_base,
)
from astrid.packs.generation.shared._common import (
    _request_to_argv as _request_to_argv_base,
)

logger = logging.getLogger(__name__)

_VIDEO_CLI_FEATURES: tuple[str, ...] = (
    "prompt",
    "negative_prompt",
    "seed",
    "count",
    "resolution",
    "image_ref",
    "image_end_ref",
    "video_ref",
    "frames",
    "fps",
    "duration",
    "guidance_scale",
    "steps",
    "shift",
    "loras",
    "enable_safety_checker",
    "enable_prompt_expansion",
    "acceleration",
    "driving_type",
    "subject_type",
)

_VIDEO_ARGV_FLAG_NAMES: tuple[str, ...] = (
    "mode",
    "prompt",
    "prompts_file",
    "model",
    "image_ref",
    "image_end_ref",
    "video_ref",
    "execution",
    "count",
    "seed",
    "negative_prompt",
    "resolution",
    "frames",
    "fps",
    "duration",
    "guidance_scale",
    "steps",
    "shift",
    "loras",
    "enable_safety_checker",
    "enable_prompt_expansion",
    "acceleration",
    "driving_type",
    "subject_type",
    "env_file",
)


# ---------------------------------------------------------------------------
# Video-only: LoRA parsing (':' separator, returns list[dict]|None)
# ---------------------------------------------------------------------------


def _parse_loras_arg(value: Any) -> list[dict[str, Any]] | None:
    """Parse the --loras CLI value into a list[{path, scale}] (or None).

    Accepted shapes:
      - already a list (from a prompts-file entry): returned as-is
      - JSON array string: ``[{\"path\": \"...\", \"scale\": 1.0}, ...]``
      - comma-separated ``url:scale`` entries:
        ``\"https://x/a.safetensors:0.8,https://x/b.safetensors:1.0\"``
    """
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return value if value else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith("["):
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise SystemExit("--loras: JSON value must be a list")
        return parsed if parsed else None
    items: list[dict[str, Any]] = []
    for raw in text.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if ":" in entry and not entry.startswith(("http://", "https://")):
            path, scale = entry.rsplit(":", 1)
            items.append({"path": path.strip(), "scale": float(scale.strip())})
        elif entry.count(":") >= 2:
            # url with scale appended: https://...safetensors:0.8
            head, scale = entry.rsplit(":", 1)
            items.append({"path": head.strip(), "scale": float(scale.strip())})
        else:
            items.append({"path": entry, "scale": 1.0})
    return items or None


# ---------------------------------------------------------------------------
# Video-only: bool-string coercion
# ---------------------------------------------------------------------------


def _parse_bool_str(value: Any) -> bool | None:
    """Coerce 'true'/'false' (or already-bool) into a bool, ``None`` if unset."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return None


# ---------------------------------------------------------------------------
# Video-only: mode validation
# ---------------------------------------------------------------------------

_VALID_MODES = {"t2v", "i2v", "flf", "v2v"}
_UNWIRED_MODES: set[str] = {"video-edit"}


def _validate_mode(mode: str) -> str:
    """Validate --mode: accept t2v/i2v/flf/v2v, reject video-edit with clear message."""
    if mode in _VALID_MODES:
        return mode
    if mode in _UNWIRED_MODES:
        raise SystemExit(
            f"Mode {mode!r} is not wired this sprint. "
            f"Available modes: {', '.join(sorted(_VALID_MODES))}."
        )
    raise SystemExit(
        f"Unknown mode {mode!r}. Must be one of: "
        f"{', '.join(sorted(_VALID_MODES))}."
    )


# ---------------------------------------------------------------------------
# Thin wrappers that fill in video-specific parameters
# ---------------------------------------------------------------------------


# Video-specific feature transforms for _build_requested_params
_VIDEO_FEATURE_TRANSFORMS: dict[str, Any] = {
    "loras": _parse_loras_arg,
    "enable_safety_checker": _parse_bool_str,
    "enable_prompt_expansion": _parse_bool_str,
}


def _build_requested_params(
    args: argparse.Namespace,
    *,
    prompt_text: str | None,
    prompt_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge prompt-entry and CLI feature values into canonical params (video)."""
    return _build_requested_params_base(
        args,
        prompt_text=prompt_text,
        prompt_entry=prompt_entry,
        cli_features=_VIDEO_CLI_FEATURES,
        feature_transforms=_VIDEO_FEATURE_TRANSFORMS,
    )


def _request_to_argv(request: Any) -> list[str]:
    """Translate an executor-style request object into CLI argv (video)."""
    return _request_to_argv_base(request, _VIDEO_ARGV_FLAG_NAMES)


def _coerce_args(
    args_or_request: argparse.Namespace | list[str] | tuple[str, ...] | Any | None,
) -> argparse.Namespace:
    """Return a parsed args namespace from CLI argv, a namespace, or a request (video)."""
    return _coerce_args_base(args_or_request, build_parser, _VIDEO_ARGV_FLAG_NAMES)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate videos from text prompts via local or cloud backends.",
    )
    p.add_argument(
        "--mode",
        required=True,
        help="Generation mode: t2v (text-to-video), i2v (image-to-video), "
        "flf (first-last-frame), v2v (video-to-video). video-edit is not wired this sprint.",
    )
    prompt_group = p.add_mutually_exclusive_group()
    prompt_group.add_argument(
        "--prompt",
        help="Text prompt for generation.",
    )
    prompt_group.add_argument(
        "--prompts-file",
        type=Path,
        help="JSONL file of prompts (one object per line).",
    )
    p.add_argument(
        "--model",
        required=True,
        help="Model ID from the registry (e.g. 'wan-2.2', 'ltx-2.3').",
    )
    p.add_argument(
        "--image-ref",
        dest="image_ref",
        help="Reference image path or URL for i2v/flf modes.",
    )
    p.add_argument(
        "--image-end-ref",
        dest="image_end_ref",
        help="End-frame reference image path or URL for flf mode.",
    )
    p.add_argument(
        "--video-ref",
        dest="video_ref",
        help="Driving motion video path or URL for v2v mode.",
    )
    p.add_argument(
        "--execution",
        required=True,
        help="Backend: 'local' (vibecomfy) or 'cloud' (fal).",
    )
    p.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of videos to generate (default 1).",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Deterministic seed (base_seed + i for sequential videos).",
    )
    p.add_argument(
        "--negative-prompt",
        dest="negative_prompt",
        help="Text describing what to avoid.",
    )
    p.add_argument(
        "--resolution",
        help="Output resolution, e.g. '1280x720'.",
    )
    p.add_argument(
        "--frames",
        type=int,
        default=None,
        help="Number of frames to generate.",
    )
    p.add_argument(
        "--fps",
        type=int,
        default=None,
        help="Frames per second.",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Duration in seconds (alternative to --frames; requires --fps).",
    )
    p.add_argument(
        "--guidance-scale",
        type=float,
        dest="guidance_scale",
        default=None,
        help="Classifier-free guidance scale.",
    )
    p.add_argument(
        "--steps",
        type=int,
        default=None,
        help="Number of sampling steps.",
    )
    p.add_argument(
        "--shift",
        type=float,
        default=None,
        help="Flow / timestep shift (wan and ltx accept this).",
    )
    p.add_argument(
        "--loras",
        default=None,
        help=(
            "LoRAs to attach. Accepts a JSON array "
            "([{\"path\": \"...\", \"scale\": 1.0}, ...]) "
            "or a comma-separated list of 'url:scale' entries."
        ),
    )
    add_choice_arg(
        p,
        "--enable-safety-checker",
        values=("true", "false"),
        dest="enable_safety_checker",
        default=None,
        help="Toggle the safety checker on/off (string-encoded bool).",
    )
    add_choice_arg(
        p,
        "--enable-prompt-expansion",
        values=("true", "false"),
        dest="enable_prompt_expansion",
        default=None,
        help="Toggle prompt expansion on/off (wan-only).",
    )
    add_choice_arg(
        p,
        "--acceleration",
        values=("none", "regular", "high"),
        default=None,
        help="Inference acceleration preset (wan-only).",
    )
    add_choice_arg(
        p,
        "--driving-type",
        values=("end_to_end", "pose"),
        dest="driving_type",
        default=None,
        help="SCAIL-2 driving signal: end_to_end (default) or pose.",
    )
    add_choice_arg(
        p,
        "--subject-type",
        values=("human", "animal"),
        dest="subject_type",
        default=None,
        help="SCAIL-2 subject type: human (default) or animal.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Explicit output/staging directory (required; the runtime supplies this for managed tasks).",
    )
    p.add_argument(
        "--env-file",
        type=Path,
        help="Env file holding FAL_KEY (for cloud execution).",
    )
    return p


# ---------------------------------------------------------------------------
# core entrypoints
# ---------------------------------------------------------------------------


def generate_core(
    args_or_request: argparse.Namespace | list[str] | tuple[str, ...] | Any | None,
) -> GenerationResult:
    args = _coerce_args(args_or_request)

    if args.out in (None, ""):
        raise AstridError(
            "generation.generate_video requires an explicit --out staging directory; "
            "invoke through the SDK/runtime for managed output",
            recovery_command="invoke generation.generate_video with a runtime project, or pass --out for explicit isolated tooling",
        )

    # --- validate mode (hard-reject v2v/video-edit before anything) ----------
    mode_name: str = _validate_mode(args.mode)

    # --- load backend registry ----------------------------------------------
    try:
        backend_registry = load_default_generation_backend_registry()
    except Exception as exc:
        raise AstridError(
            f"Failed to load generation backend registry: {exc}",
            recovery_command="verify the generation backend packages are installed and retry",
        ) from exc

    # --- load registry -------------------------------------------------------
    try:
        registry = ModelRegistry.load_default()
    except Exception as exc:
        raise AstridError(
            f"Failed to load model registry: {exc}",
            recovery_command="verify the model catalog is installed and retry",
        ) from exc

    # --- validate (model, mode) pair exists ----------------------------------
    try:
        entry, mode_spec = registry.get_by_mode(args.model, mode_name)
    except KeyError as exc:
        raise AstridError(
            f"model {args.model!r} mode {mode_name!r} not found: {exc}",
            recovery_command="inspect the generation capability manifest and retry with a valid model:mode pair",
        ) from exc

    # --- check backend availability for --execution --------------------------
    if not registry.backend_available(args.model, mode_name, args.execution):
        available_ids = _available_backend_ids(mode_spec)
        available = ", ".join(available_ids)
        raise AstridError(
            f"model {args.model!r} mode {mode_name!r} has no "
            f"{args.execution!r} backend. Available backends: {available}",
            valid_options=list(available_ids),
            recovery_command=f"retry with one of the available backends: {available}",
        )

    warnings: list[dict[str, str]] = []
    dropped_features: list[str] = []

    # --- derive duration→frames deterministically ---------------------------
    if args.duration is not None and args.frames is None and args.fps is not None:
        args.frames = round(args.duration * args.fps)

    # --- setup output directory ----------------------------------------------
    out = args.out.expanduser().resolve()
    videos_dir = out / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)

    # --- load prompts --------------------------------------------------------
    if args.prompts_file:
        prompts = _load_prompts(args.prompts_file, args.model, mode_name)
    else:
        if not args.prompt:
            raise AstridError(
                "either --prompt or --prompts-file is required",
                recovery_command="provide --prompt '<text>' or --prompts-file <path> and retry",
            )
        prompts = [{"prompt": args.prompt, "model": args.model}]

    # --- build adapter (dispatch through BackendAdapter) -------------
    adapter = _create_backend_adapter(
        backend_registry,
        args.execution,
        env_file=args.env_file,
    )

    # --- resolve image_ref path (for manifest tracking) ----------------------
    image_ref_resolved: str | None = None
    if args.image_ref:
        ref = args.image_ref
        ref_path = Path(ref)
        if ref_path.exists():
            image_ref_resolved = str(ref_path.resolve())
        else:
            image_ref_resolved = ref

    # --- resolve image_end_ref path (for manifest tracking) ------------------
    image_end_ref_resolved: str | None = None
    if args.image_end_ref:
        ref = args.image_end_ref
        ref_path = Path(ref)
        if ref_path.exists():
            image_end_ref_resolved = str(ref_path.resolve())
        else:
            image_end_ref_resolved = ref

    # --- resolve video_ref path (for manifest tracking) ----------------------
    video_ref_resolved: str | None = None
    if args.video_ref:
        ref = args.video_ref
        ref_path = Path(ref)
        if ref_path.exists():
            video_ref_resolved = str(ref_path.resolve())
        else:
            video_ref_resolved = ref

    # --- sequential N=1 generation loop -------------------------------------
    all_outputs: list[dict[str, Any]] = []
    generated_paths: list[Path] = []
    output_seeds: dict[Path, int] = {}
    count = max(1, args.count or 1)
    prompt_text: str | None = None
    final_seed: int = 0
    model_actual: str = ""
    cost_usd: float | None = None
    duration_ms: int = 0
    request_id: str | None = None
    source_urls: list[str] | None = None
    all_applied_features: list[str] = []
    result_error = None

    for i in range(count):
        # Determine the prompt entry for this iteration
        if args.prompts_file:
            prompt_entry = prompts[i % len(prompts)]
            prompt_text = prompt_entry["prompt"]

            # Per-entry model override — re-validate (model, mode, backend)
            entry_override_model: str = prompt_entry.get("model", args.model)
            if entry_override_model != args.model:
                # Mode field already validated in _normalise_prompts
                skip_reason: str | None = None
                try:
                    entry, mode_spec = registry.get_by_mode(
                        entry_override_model, mode_name
                    )
                except KeyError as exc:
                    skip_reason = str(exc)
                if skip_reason is None and not registry.backend_available(
                    entry_override_model, mode_name, args.execution
                ):
                    available = ", ".join(sorted(mode_spec.backends))
                    skip_reason = (
                        f"model {entry_override_model!r} mode "
                        f"{mode_name!r} has no {args.execution!r} backend. "
                        f"Available: {available}"
                    )
                if skip_reason is not None:
                    print(
                        f"Warning: skipping row {i} ({prompt_text!r}): "
                        f"{skip_reason}",
                        file=sys.stderr,
                    )
                    continue

            requested_params = _build_requested_params(
                args,
                prompt_text=prompt_text,
                prompt_entry=prompt_entry,
            )
            try:
                _check_required(
                    mode_spec,
                    mode_name,
                    entry.id,
                    requested_params,
                )
            except SystemExit as exc:
                print(
                    f"Warning: skipping row {i} ({prompt_text!r}): {exc.code}",
                    file=sys.stderr,
                )
                continue
            params, extra_warns, extra_drops = _drop_unsupported(
                mode_spec,
                mode_name,
                entry.id,
                requested_params,
            )
            warnings.extend(extra_warns)
            dropped_features.extend(extra_drops)
            seed = _resolve_seed(params.get("seed", args.seed), i)
        else:
            prompt_text = args.prompt
            requested_params = _build_requested_params(args, prompt_text=prompt_text)
            _check_required(mode_spec, mode_name, entry.id, requested_params)
            params, extra_warns, extra_drops = _drop_unsupported(
                mode_spec,
                mode_name,
                entry.id,
                requested_params,
            )
            warnings.extend(extra_warns)
            dropped_features.extend(extra_drops)
            seed = _resolve_seed(args.seed, i)

        # --- build canonical params dict for adapter -------------------------
        params["seed"] = seed
        params["count"] = 1  # N=1 per loop iteration

        # --- dispatch to adapter ------------------------------------
        try:
            result: GenerationResult = adapter.generate(
                entry=entry,
                mode=mode_name,
                params=params,
                out_dir=videos_dir,
            )
            generated_paths.extend(result.video_paths)

            # Convert GenerationResult to manifest output dicts
            for vid_path in result.video_paths:
                content_hash = (
                    "sha256:"
                    + hashlib.sha256(vid_path.read_bytes()).hexdigest()
                )
                rel = str(vid_path.relative_to(out))

                # ffprobe best-effort metadata
                probe = ffprobe_metadata(vid_path)
                attested_seed = result.output_seeds.get(vid_path)

                output_entry: dict[str, Any] = {
                    "path": rel,
                    "name": "generated_videos",
                    "ordinal": len(all_outputs),
                    "role": "result",
                    "is_primary": not all_outputs,
                    "content_hash": content_hash,
                    "bytes": vid_path.stat().st_size,
                    "duration_seconds": probe.duration_seconds,
                    "fps": probe.fps,
                    "resolution": probe.resolution,
                    "seed_used": (
                        attested_seed
                        if isinstance(attested_seed, int)
                        and not isinstance(attested_seed, bool)
                        and attested_seed >= 0
                        else None
                    ),
                }
                all_outputs.append(output_entry)
                if output_entry["seed_used"] is not None:
                    output_seeds[vid_path] = output_entry["seed_used"]

            final_seed = seed
            model_actual = result.model_actual
            cost_usd = result.cost_usd
            duration_ms = result.duration_ms
            request_id = result.request_id
            source_urls = result.source_urls
            result_error = result.error
            if result.applied_features:
                all_applied_features = list(result.applied_features)
        except BaseException:
            if all_outputs:
                try:
                    inputs, request = _build_inputs_request(
                        args, entry, mode_name, final_seed, prompt_text,
                        image_ref_resolved, image_end_ref_resolved,
                        video_ref_resolved,
                    )
                    manifest = build_generation_manifest(
                        kind="generation.generate_video",
                        inputs=inputs,
                        outputs=all_outputs,
                        created=datetime.now(timezone.utc).isoformat(),
                        warnings=warnings,
                        modality=entry.modality,
                        model=entry.id,
                        mode_used=mode_name,
                        model_actual=model_actual,
                        execution=args.execution,
                        request=request,
                        seed=final_seed,
                        dropped_features=dropped_features if dropped_features else None,
                        applied_features=all_applied_features if all_applied_features else None,
                        cost_usd=cost_usd,
                        duration_ms=duration_ms,
                        request_id=request_id,
                        source_urls=source_urls,
                    )
                    manifest_path = out / "manifest.json"
                    # Route output metadata through the shared contract.
                    manifest["outputs"] = complete_output_metadata(
                        manifest["outputs"], root_dir=out,
                    )
                    write_json_atomic(manifest_path, manifest)
                except Exception:
                    pass
            raise

    # --- emit manifest -------------------------------------------------------
    inputs, request = _build_inputs_request(
        args, entry, mode_name, final_seed, prompt_text,
        image_ref_resolved, image_end_ref_resolved,
        video_ref_resolved,
    )
    manifest = build_generation_manifest(
        kind="generation.generate_video",
        inputs=inputs,
        outputs=all_outputs,
        created=datetime.now(timezone.utc).isoformat(),
        warnings=warnings,
        modality=entry.modality,
        model=entry.id,
        mode_used=mode_name,
        model_actual=model_actual,
        execution=args.execution,
        request=request,
        seed=final_seed,
        dropped_features=dropped_features if dropped_features else None,
        applied_features=all_applied_features if all_applied_features else None,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        request_id=request_id,
        source_urls=source_urls,
    )
    manifest_path = out / "manifest.json"
    # Route output metadata through the shared contract.
    manifest["outputs"] = complete_output_metadata(
        manifest["outputs"], root_dir=out,
    )
    write_json_atomic(manifest_path, manifest)

    # Video paths live in ``image_paths`` (the canonical GenerationResult
    # field for all media).  ``video_paths`` is a read-only property alias
    # that returns ``image_paths``, so video consumers see them via either
    # attribute.  See ``GenerationResult`` docstring in
    # ``astrid.core.generation.backends.base``.
    return GenerationResult(
        image_paths=generated_paths,
        seed_used=manifest["seed_used"],
        output_seeds=output_seeds,
        model_actual=model_actual,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        applied_features=list(all_applied_features),
        dropped_features=list(dropped_features),
        request_id=request_id,
        source_urls=source_urls,
        error=result_error,
        manifest=manifest,
        run_dir=out,
    )


def _build_inputs_request(
    args: argparse.Namespace,
    entry: Any,
    mode_name: str,
    seed: int,
    prompt_text: str | None,
    image_ref_resolved: str | None,
    image_end_ref_resolved: str | None,
    video_ref_resolved: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build inputs and request dicts for the video generation manifest."""
    requested_prompt = prompt_text or getattr(args, "prompt", None)
    request: dict[str, Any] = {
        "prompt": requested_prompt,
        "negative_prompt": getattr(args, "negative_prompt", None),
        "seed": seed,
        "count": max(1, args.count or 1),
        "image_ref_resolved": image_ref_resolved,
        "image_end_ref_resolved": image_end_ref_resolved,
        "video_ref_resolved": video_ref_resolved,
        "frames": getattr(args, "frames", None),
        "fps": getattr(args, "fps", None),
        "duration": getattr(args, "duration", None),
        "resolution": getattr(args, "resolution", None),
    }
    inputs: dict[str, Any] = {
        "model": entry.id,
        "mode": mode_name,
        "execution": args.execution,
        "prompt": requested_prompt,
        "seed": seed,
        "count": max(1, args.count or 1),
    }
    for key in ("negative_prompt", "resolution", "frames", "fps", "duration",
                "image_ref", "image_end_ref", "video_ref", "guidance_scale",
                "steps", "shift", "mode", "driving_type", "subject_type"):
        val = getattr(args, key, None)
        if val is not None:
            inputs[key] = val
    if image_ref_resolved is not None:
        inputs["image_ref_resolved"] = image_ref_resolved
    if image_end_ref_resolved is not None:
        inputs["image_end_ref_resolved"] = image_end_ref_resolved
    if video_ref_resolved is not None:
        inputs["video_ref_resolved"] = video_ref_resolved
    return inputs, request


# ---------------------------------------------------------------------------
# SDK / CLI entrypoints
# ---------------------------------------------------------------------------


def run_sdk(argv: list[str] | None = None) -> dict[str, Any]:
    """In-process entrypoint returning a JSON-safe payload dict."""
    try:
        result = generate_core(argv)
    except AstridError as exc:
        diagnostic: dict[str, Any] = {
            "type": "AstridError",
            "cause": exc.cause,
            "recovery_command": exc.recovery_command,
        }
        if exc.valid_options:
            diagnostic["valid_options"] = exc.valid_options
        return {"returncode": 1, "error": diagnostic}
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int) and code != 0:
            returncode = code
        elif code and str(code):
            returncode = 1
        else:
            returncode = 0
        return {
            "returncode": returncode,
            "error": {
                "type": "SystemExit",
                "message": str(code) if code else "",
            },
        }
    except Exception as exc:
        return {
            "returncode": 1,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
    return {"returncode": 0, GENERATION_RESULT_KEY: result}


def main(argv: list[str] | None = None) -> int:
    warn_if_unledgered()
    result = generate_core(argv)
    manifest_path = _manifest_path_for_run_dir(result.run_dir)
    print(f"manifest={manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
