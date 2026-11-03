#!/usr/bin/env python3
"""Generate audio from text prompts using local (vibecomfy) or cloud (fal) backends.

v2: model → mode → backend taxonomy.  ``--mode`` is required.
Backend dispatch goes through ``BackendAdapter``.
Features are validated per-mode.

The executor's backend loop is music-only. The typed SDK facade routes
``mode='tts'`` to the exact-text speech action before this executor is
admitted; direct action invocation still rejects non-music modes.
"""

from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint
from astrid.core.pack.entrypoint import warn_if_unledgered

guard_canonical_entrypoint('generation.generate_audio')
import argparse
import hashlib
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.core._shared.result_manifest import complete_output_metadata
from astrid.pack_api import add_choice_arg
from astrid.pack_api import AstridError
from astrid.pack_api import write_json_atomic
from astrid.core.generation import GENERATION_RESULT_KEY
from astrid.core.generation.backends import (
    GenerationResult,
    load_default_generation_backend_registry,
)
from astrid.core.media import ffprobe_metadata
from astrid.pack_api import ModelRegistry
from astrid.packs.generation.shared._common import (
    _available_backend_ids,
    _check_required,
    _create_backend_adapter,
    _drop_unsupported,
    _load_prompts,
    _manifest_path_for_run_dir,
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

_AUDIO_CLI_FEATURES: tuple[str, ...] = (
    "prompt",
    "negative_prompt",
    "seed",
    "count",
    "duration",
    "guidance_scale",
    "steps",
    "lyrics_prompt",
    "instrumental",
    "output_format",
)

_AUDIO_ARGV_FLAG_NAMES: tuple[str, ...] = (
    "mode",
    "prompt",
    "prompts_file",
    "model",
    "execution",
    "count",
    "seed",
    "negative_prompt",
    "duration",
    "guidance_scale",
    "steps",
    "lyrics_prompt",
    "instrumental",
    "output_format",
    "env_file",
)


# ---------------------------------------------------------------------------
# Audio-only: bool-string coercion
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
# Audio-only: mode validation
# ---------------------------------------------------------------------------


_VALID_MODES = {"music"}
_UNWIRED_MODES = {"tts", "sfx"}


def _validate_mode(mode: str) -> str:
    """Validate direct action mode; typed SDK TTS routing happens upstream."""
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
# Thin wrappers that fill in audio-specific parameters
# ---------------------------------------------------------------------------


# Audio-specific feature transforms for _build_requested_params
_AUDIO_FEATURE_TRANSFORMS: dict[str, Any] = {
    "instrumental": _parse_bool_str,
}


def _build_requested_params(
    args: argparse.Namespace,
    *,
    prompt_text: str | None,
    prompt_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge prompt-entry and CLI feature values into canonical params (audio)."""
    return _build_requested_params_base(
        args,
        prompt_text=prompt_text,
        prompt_entry=prompt_entry,
        cli_features=_AUDIO_CLI_FEATURES,
        feature_transforms=_AUDIO_FEATURE_TRANSFORMS,
    )


def _request_to_argv(request: Any) -> list[str]:
    """Translate an executor-style request object into CLI argv (audio)."""
    return _request_to_argv_base(request, _AUDIO_ARGV_FLAG_NAMES)


def _coerce_args(
    args_or_request: argparse.Namespace | list[str] | tuple[str, ...] | Any | None,
) -> argparse.Namespace:
    """Return a parsed args namespace from CLI argv, a namespace, or a request (audio)."""
    return _coerce_args_base(args_or_request, build_parser, _AUDIO_ARGV_FLAG_NAMES)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate audio from text prompts via local or cloud backends.",
    )
    p.add_argument(
        "--mode",
        required=True,
        help="Direct action mode: music. The typed SDK routes tts to generate_speech.",
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
        help="Model ID from the registry (e.g. 'stable-audio-3-medium', 'minimax-music-v2.6').",
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
        help="Number of audio clips to generate (default 1).",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Deterministic seed (base_seed + i for sequential clips).",
    )
    p.add_argument(
        "--negative-prompt",
        dest="negative_prompt",
        help="Text describing what to avoid.",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Audio duration in seconds.",
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
        "--lyrics-prompt",
        dest="lyrics_prompt",
        default=None,
        help="Lyrics prompt for models that support vocals (e.g. MiniMax).",
    )
    add_choice_arg(
        p,
        "--instrumental",
        values=("true", "false"),
        dest="instrumental",
        default=None,
        help="Request instrumental output (string-encoded bool).",
    )
    p.add_argument(
        "--output-format",
        dest="output_format",
        default=None,
        help="Output audio format, e.g. 'mp3', 'wav', 'flac' (backend-dependent).",
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
            "generation.generate_audio requires an explicit --out staging directory; "
            "invoke through the SDK/runtime for managed output",
            recovery_command="invoke generation.generate_audio with a runtime project, or pass --out for explicit isolated tooling",
        )

    # --- validate mode (hard-reject tts/sfx before anything) -----------------
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

    # --- setup output directory ----------------------------------------------
    out = args.out.expanduser().resolve()
    audio_dir = out / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

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

    # --- sequential N=1 generation loop -------------------------------------
    all_outputs: list[dict[str, Any]] = []
    generated_paths: list[Path] = []
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
                out_dir=audio_dir,
            )
            generated_paths.extend(result.image_paths)

            # Convert GenerationResult to manifest output dicts
            for audio_path in result.image_paths:
                content_hash = (
                    "sha256:"
                    + hashlib.sha256(audio_path.read_bytes()).hexdigest()
                )
                rel = str(audio_path.relative_to(out))

                # ffprobe best-effort metadata
                probe = ffprobe_metadata(audio_path)

                output_entry: dict[str, Any] = {
                    "path": rel,
                    "name": "generated_audio",
                    "ordinal": len(all_outputs),
                    "role": "result",
                    "is_primary": not all_outputs,
                    "content_hash": content_hash,
                    "bytes": audio_path.stat().st_size,
                    "duration_seconds": probe.duration_seconds,
                }
                all_outputs.append(output_entry)

            final_seed = result.seed_used
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
                    )
                    manifest = build_generation_manifest(
                        kind="generation.generate_audio",
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
    )
    manifest = build_generation_manifest(
        kind="generation.generate_audio",
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

    return GenerationResult(
        image_paths=generated_paths,
        seed_used=final_seed,
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
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build inputs and request dicts for the audio generation manifest."""
    requested_prompt = prompt_text or getattr(args, "prompt", None)
    request: dict[str, Any] = {
        "prompt": requested_prompt,
        "negative_prompt": getattr(args, "negative_prompt", None),
        "seed": seed,
        "count": max(1, args.count or 1),
        "duration": getattr(args, "duration", None),
    }
    inputs: dict[str, Any] = {
        "model": entry.id,
        "mode": mode_name,
        "execution": args.execution,
        "prompt": requested_prompt,
        "seed": seed,
        "count": max(1, args.count or 1),
    }
    for key in ("negative_prompt", "duration", "guidance_scale", "steps",
                "lyrics_prompt", "instrumental", "output_format"):
        val = getattr(args, key, None)
        if val is not None:
            inputs[key] = val
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
