"""Shared helpers for generation executors.

Provides :func:`build_generation_manifest` so image and video executors
don't each duplicate the same manifest dict literal, plus a family of
validation, prompt, seed, backend, and CLI-coercion helpers that were
previously copy-pasted between the two ``run.py`` files.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any, Callable

from astrid.core._shared.result_manifest import build_manifest
from astrid.core.contracts.errors import AstridError
from astrid.core.generation.backends import (
    BackendAdapter,
    GenerationBackendRegistry,
)

# ---------------------------------------------------------------------------
# Manifest helper (original _common.py content)
# ---------------------------------------------------------------------------


def build_generation_manifest(
    *,
    kind: str,
    inputs: dict[str, Any],
    outputs: list[dict[str, Any]],
    created: str,
    warnings: list[dict[str, str]],
    modality: str,
    model: str,
    mode_used: str,
    model_actual: str,
    execution: str,
    request: dict[str, Any],
    seed: int,
    dropped_features: list[str] | None = None,
    applied_features: list[str] | None = None,
    cost_usd: float | None = None,
    duration_ms: int = 0,
    request_id: str | None = None,
    source_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Build a generation manifest via the shared contract (schema v2).

    Wraps :func:`build_manifest` with ``schema_version=2`` and generation-
    specific fields passed through as ``**extras``.  The top-level ``seed``
    remains the submitted request summary; ``seed_used`` is derived only from
    per-output attestation fields.
    """
    extras: dict[str, Any] = {
        "modality": modality,
        "model": model,
        "mode_used": mode_used,
        "model_actual": model_actual,
        "execution": execution,
        "request": request,
        "seed": seed,
        "seed_used": _attested_seed_summary(outputs),
    }
    if dropped_features:
        extras["dropped_features"] = dropped_features
    if applied_features:
        extras["applied_features"] = applied_features
    if cost_usd is not None:
        extras["cost_usd"] = cost_usd
    if duration_ms:
        extras["duration_ms"] = duration_ms
    if request_id:
        extras["request_id"] = request_id
    if source_urls:
        extras["source_urls"] = source_urls

    return build_manifest(
        kind=kind,
        inputs=inputs,
        outputs=outputs,
        created=created,
        schema_version=2,
        warnings=warnings,
        **extras,
    )


def _attested_seed_summary(outputs: list[dict[str, Any]]) -> int | None:
    """Summarize output seed attestations when they all agree."""
    if not outputs:
        return None
    seeds = [output.get("seed_used") for output in outputs]
    if any(type(value) is not int or value < 0 for value in seeds):
        return None
    first = seeds[0]
    return first if all(value == first for value in seeds[1:]) else None


# ---------------------------------------------------------------------------
# Feature validation (per-mode)
# ---------------------------------------------------------------------------


def _feature_is_missing(feature: str, value: Any) -> bool:
    """Return ``True`` when a supplied feature value should count as missing."""
    if value is None:
        return True
    if feature == "count":
        return not bool(value)
    if isinstance(value, str):
        return not value.strip()
    return False


def _check_required(
    mode_spec: Any,
    mode_name: str,
    model_id: str,
    requested_params: dict[str, Any],
) -> None:
    """Hard-fail if any feature in *mode_spec.requires* is missing."""
    missing: list[str] = []
    for req in mode_spec.requires:
        if _feature_is_missing(req, requested_params.get(req)):
            missing.append(req)

    if missing:
        raise SystemExit(
            f"model {model_id!r} mode {mode_name!r} requires: "
            f"{', '.join(sorted(missing))}. "
            f"Provide {'it' if len(missing) == 1 else 'them'} "
            f"and retry."
        )


def _drop_unsupported(
    mode_spec: Any,
    mode_name: str,
    model_id: str,
    requested_params: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    """Drop caller-supplied features absent from *mode_spec.supports*.

    Returns ``(filtered_params, warnings, dropped_features)``.
    """
    filtered_params: dict[str, Any] = {}
    warnings: list[dict[str, str]] = []
    dropped: list[str] = []
    for feature, value in requested_params.items():
        if _feature_is_missing(feature, value):
            continue
        if feature not in mode_spec.supports:
            warnings.append(
                {
                    "feature": feature,
                    "reason": (
                        f"not supported by model {model_id!r} "
                        f"mode {mode_name!r}"
                    ),
                }
            )
            dropped.append(feature)
            continue
        filtered_params[feature] = value
    return filtered_params, warnings, dropped


# ---------------------------------------------------------------------------
# Seed resolution
# ---------------------------------------------------------------------------


def _resolve_seed(requested: int | None, index: int) -> int:
    """Return ``requested + index`` if *requested* is set, else a random seed."""
    if requested is not None:
        return requested + index
    return random.randint(0, 2**31 - 1)


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------


def _load_prompts(path: Path, model: str, mode: str) -> list[dict[str, Any]]:
    """Load generation requests from a JSON or JSONL file.

    Each line is a JSON object that may override ``prompt``, ``seed``,
    ``count``, and modality-specific features (``size``, ``resolution``,
    ``image_ref``, etc.) plus ``model``.  A bare JSON array is also
    accepted.

    Per-entry ``model`` overrides must include an explicit ``mode`` field
    matching CLI ``--mode`` or be rejected.
    """
    from astrid.core.execution._prompt_snapshot import _retained_prompt_bytes

    content = _retained_prompt_bytes(path)
    if content is None:
        content = path.read_bytes()
    return _parse_prompts_bytes(content, model, mode, source=str(path))


def _parse_prompts_bytes(
    content: bytes, model: str, mode: str, *, source: str = "prompts file"
) -> list[dict[str, Any]]:
    """Parse the exact immutable bytes of a JSON/JSONL prompt snapshot."""
    if type(content) is not bytes:
        raise TypeError("prompt content must be immutable bytes")
    text = content.decode("utf-8").strip()
    if text.startswith("["):
        data = json.loads(text)
        if isinstance(data, list):
            return _normalise_prompts(data, model, mode)
        raise SystemExit(f"{source}: top-level JSON must be an array or JSONL")

    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        entries.append(json.loads(line))
    if not entries:
        raise SystemExit(f"{source}: no valid entries found")
    return _normalise_prompts(entries, model, mode)


def _normalise_prompts(
    raw: list[dict[str, Any]], model: str, mode: str
) -> list[dict[str, Any]]:
    """Ensure every entry has a ``prompt`` and a ``model`` key.

    Per-entry ``model`` overrides are validated for mode consistency:
    a ``mode`` field matching CLI ``--mode`` is required.
    """
    result: list[dict[str, Any]] = []
    for i, entry in enumerate(raw):
        if "prompt" not in entry:
            raise SystemExit(
                f"Entry {i} in prompts file is missing required 'prompt' key"
            )
        entry.setdefault("model", model)

        # Per-entry model override must have explicit mode field
        # matching CLI --mode (or no override at all).
        entry_model = entry.get("model", model)
        if entry_model != model:
            entry_mode = entry.get("mode")
            if entry_mode is None:
                raise SystemExit(
                    f"Entry {i} overrides model to {entry_model!r} but "
                    f"has no 'mode' field.  Per-entry model overrides must "
                    f"include 'mode' matching CLI --mode ({mode!r})."
                )
            if entry_mode != mode:
                raise SystemExit(
                    f"Entry {i} has mode {entry_mode!r} which does not match "
                    f"CLI --mode {mode!r}.  Per-entry model overrides must "
                    f"use the same mode."
                )

        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Parameter building (parameterized by feature set)
# ---------------------------------------------------------------------------


def _build_requested_params(
    args: argparse.Namespace,
    *,
    prompt_text: str | None,
    prompt_entry: dict[str, Any] | None = None,
    cli_features: tuple[str, ...],
    feature_transforms: dict[str, Callable[[Any], Any]] | None = None,
) -> dict[str, Any]:
    """Merge prompt-entry and CLI feature values into canonical params.

    *cli_features* is the modality-specific tuple of feature names
    (e.g. ``_IMAGE_CLI_FEATURES`` or ``_VIDEO_CLI_FEATURES``).

    *feature_transforms* is an optional ``{feature_name: callable}``
    mapping applied to raw CLI values before storing (used by video
    for ``loras`` / bool-string coercion).
    """
    params: dict[str, Any] = {}
    if prompt_entry:
        for key, value in prompt_entry.items():
            if key in _PROMPT_ENTRY_CONTROL_KEYS:
                continue
            params[key] = value

    if prompt_text is not None:
        params["prompt"] = prompt_text
    elif "prompt" not in params:
        params["prompt"] = getattr(args, "prompt", None)

    for feature in cli_features:
        if feature == "prompt":
            continue
        if feature not in params:
            value = getattr(args, feature, None)
            if feature_transforms and feature in feature_transforms:
                value = feature_transforms[feature](value)
            params[feature] = value

    return {
        feature: value
        for feature, value in params.items()
        if value is not None
    }


def _request_to_argv(
    request: Any,
    flag_names: tuple[str, ...],
) -> list[str]:
    """Translate an executor-style request object into CLI argv.

    *flag_names* is the modality-specific tuple of CLI flags.
    """
    argv: list[str] = []
    inputs = getattr(request, "inputs", {}) or {}
    for name in flag_names:
        value = inputs.get(name)
        if value in (None, ""):
            continue
        if name == "loras" and not isinstance(value, str):
            value = json.dumps(value)
        argv.extend([f"--{name.replace('_', '-')}", str(value)])
    out = getattr(request, "out", None)
    if out not in (None, ""):
        argv.extend(["--out", str(out)])
    return argv


def _coerce_args(
    args_or_request: argparse.Namespace | list[str] | tuple[str, ...] | Any | None,
    build_parser: Callable[[], argparse.ArgumentParser],
    flag_names: tuple[str, ...],
) -> argparse.Namespace:
    """Return a parsed args namespace from CLI argv, a namespace, or a request.

    *build_parser* and *flag_names* are modality-specific.
    """
    if isinstance(args_or_request, argparse.Namespace):
        return args_or_request
    if args_or_request is None or isinstance(args_or_request, (list, tuple)):
        return build_parser().parse_args(args_or_request)
    if hasattr(args_or_request, "inputs"):
        return build_parser().parse_args(_request_to_argv(args_or_request, flag_names))
    raise TypeError(
        "generate_core expected argparse.Namespace, argv list/tuple, or executor request"
    )


# ---------------------------------------------------------------------------
# Manifest / backend helpers
# ---------------------------------------------------------------------------


def _manifest_path_for_run_dir(run_dir: Path | None) -> Path:
    if run_dir is None:
        raise AstridError(
            "generation result is missing run_dir",
            recovery_command="run generation through generate_core/main so the executor can write manifest metadata",
        )
    return run_dir / "manifest.json"


def _available_backend_ids(mode_spec: Any) -> tuple[str, ...]:
    """Return the backend ids available for one resolved mode."""
    return tuple(sorted(mode_spec.backends))


def _create_backend_adapter(
    backend_registry: GenerationBackendRegistry,
    execution: str,
    *,
    env_file: Path | None,
) -> BackendAdapter:
    """Instantiate the selected backend with CLI-friendly errors."""
    try:
        return backend_registry.create(execution, env_file=env_file)
    except KeyError as exc:
        raise AstridError(
            f"generation backend {execution!r} is not registered: {exc}",
            recovery_command="check available backends and retry with a registered backend",
        ) from exc
    except Exception as exc:
        raise AstridError(
            f"failed to initialize generation backend {execution!r}: {exc}",
            recovery_command="check backend configuration and environment, then retry",
        ) from exc


# ---------------------------------------------------------------------------
# Shared sentinel
# ---------------------------------------------------------------------------

_PROMPT_ENTRY_CONTROL_KEYS = frozenset({"model", "mode"})
