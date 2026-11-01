"""GEN-owned composition for the ordinary typed image publication route.

The helper in this module is deliberately a compose-only boundary.  It
validates the small ordinary text-to-image request, resolves the shipped
capability and model registries, and returns the complete admission
document that the host adapter can pass to Runtime.  It does not admit a
task, contact a provider, read a project, or execute a capability.

The command-line entrypoint reads exactly one JSON object from stdin and emits
one JSON object to stdout.  Source roots, registries, and the interpreter are
chosen by the configured host process; none are request-controlled.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrid.core.generation.preflight import validate_generation_request
from astrid.core.generation.storage_policy import (
    CLOUD_T2I_STORAGE_POLICY,
    ImageStoragePolicyError,
)

from .exceptions import (
    AstridSDKError,
    CapabilityValidationError,
)
from .generation import _load_model_registry
from .invocation import (
    _generation_publish_effect,
    _validate_generation_intent,
    get_capability,
)


CAPABILITY_ID = "generation.generate_image"
SCHEMA_VERSION = "1"
GROUP_KEY = "main"
PARTIAL_SUCCESS_POLICY = "reject"
_MAX_SEED = 2_147_483_647
_MAX_GUIDANCE_SCALE = 100.0
_MAX_STEPS = 1_000
_CAPABILITY_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")

# This is the ordinary UI surface.  In particular, it excludes path-bearing
# LoRA and recipe inputs, output selectors, arbitrary executor inputs, and
# every publication/effect field.  Values are checked below instead of being
# forwarded merely because a capability happens to declare a port.
_REQUIRED_PARAM_KEYS = (
    "model",
    "mode",
    "execution",
    "prompt",
    "count",
    "size",
)
_OPTIONAL_CONTROL_KEYS = (
    "seed",
    "negative_prompt",
    "guidance_scale",
    "steps",
)
_PARAM_KEYS = frozenset((*_REQUIRED_PARAM_KEYS, *_OPTIONAL_CONTROL_KEYS))


def _fail(message: str) -> CapabilityValidationError:
    return CapabilityValidationError(message)


def _require_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(f"{label} must be an object")
    return value


def _validate_project(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail("generation publication requires a non-empty project")
    if any(ord(char) < 0x20 for char in value):
        raise _fail("generation publication project contains control characters")
    return value


def _validate_digest(value: Any) -> str:
    if not isinstance(value, str) or _CAPABILITY_DIGEST_RE.fullmatch(value) is None:
        raise _fail("capability_digest must be sha256:<64 lowercase hex characters>")
    return value


def _validate_params(raw: Any) -> dict[str, Any]:
    params = _require_mapping(raw, label="params")
    unknown = sorted(str(key) for key in params if key not in _PARAM_KEYS)
    if unknown:
        raise _fail(
            "ordinary image route contains unsupported parameter(s): "
            + ", ".join(unknown)
        )

    missing = [key for key in _REQUIRED_PARAM_KEYS if key not in params]
    if missing:
        raise _fail("ordinary image route is missing: " + ", ".join(missing))

    model = params["model"]
    mode = params["mode"]
    execution = params["execution"]
    prompt = params["prompt"]
    size = params["size"]
    count = params["count"]
    if not isinstance(model, str) or not model.strip():
        raise _fail("params.model must be a non-empty string")
    if mode != "t2i":
        raise _fail("ordinary image route requires mode='t2i'")
    if execution != "cloud":
        raise _fail("ordinary image route requires execution='cloud'")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _fail("params.prompt must be a non-empty string")
    if isinstance(count, bool) or not isinstance(count, int):
        raise _fail("params.count must be an integer")
    if not 1 <= count <= CLOUD_T2I_STORAGE_POLICY.max_count:
        raise _fail(
            f"params.count must be between 1 and {CLOUD_T2I_STORAGE_POLICY.max_count}"
        )
    if not isinstance(size, str) or not size.strip():
        raise _fail("params.size must be a non-empty string")

    normalized: dict[str, Any] = {
        "model": model,
        "mode": mode,
        "execution": execution,
        "prompt": prompt,
        "count": count,
        "size": size,
    }
    for key in _OPTIONAL_CONTROL_KEYS:
        if key not in params or params[key] is None:
            continue
        value = params[key]
        if key == "seed":
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_SEED
            ):
                raise _fail(
                    f"params.seed must be an integer from 0 through {_MAX_SEED}"
                )
        elif key == "negative_prompt":
            if not isinstance(value, str):
                raise _fail("params.negative_prompt must be a string")
        elif key == "guidance_scale":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0 <= float(value) <= _MAX_GUIDANCE_SCALE
            ):
                raise _fail(
                    "params.guidance_scale must be a finite number from 0 through "
                    f"{_MAX_GUIDANCE_SCALE:g}"
                )
        elif key == "steps":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_STEPS:
                raise _fail(f"params.steps must be an integer from 1 through {_MAX_STEPS}")
        normalized[key] = value

    try:
        CLOUD_T2I_STORAGE_POLICY.validate_task_request(
            model=model,
            mode=mode,
            execution=execution,
            params=normalized,
        )
    except ImageStoragePolicyError as exc:
        raise _fail(str(exc)) from exc
    return normalized


def _stable_generation_intent(count: int) -> dict[str, Any]:
    """Build the sole GEN-owned group and its explicit output identities."""
    selectors = []
    for ordinal in range(count):
        # ``ordinal`` is the producer identity.  The labels are stable
        # provenance strings and are never used by the host as list identity.
        variant_key = "original" if ordinal == 0 else f"variant-{ordinal}"
        selectors.append(
            {
                "selector": f"main-{ordinal}",
                "ordinal": ordinal,
                "variant_key": variant_key,
            }
        )
    intent = {
        "version": 1,
        "modality": "image",
        "partial_success_policy": PARTIAL_SUCCESS_POLICY,
        "groups": [{"group_key": GROUP_KEY, "selectors": selectors}],
    }
    return _validate_generation_intent(intent, modality="image")


def _capability_digest(capability: Any) -> str:
    """Return the exact digest emitted by GenericPackHost registration.

    Public ``Capability.definition`` is an SDK projection.  The host registers
    the manifest definition after folder/pack provenance is attached, so
    hashing the projection directly produces a different identity.  Reuse the
    host's loader, provenance attachment, and digest helper instead of
    reconstructing that serialization in this compose-only boundary.
    """
    definition = getattr(capability, "definition", None)
    if not isinstance(definition, Mapping):
        raise _fail("resolved image capability has no mapping definition")
    metadata = definition.get("metadata")
    if not isinstance(metadata, Mapping):
        raise _fail("resolved image capability has no host discovery metadata")
    raw_pack_root = metadata.get("pack_root")
    if not isinstance(raw_pack_root, str) or not raw_pack_root.strip():
        raise _fail("resolved image capability has no pack root")

    try:
        from astrid.core.execution.generic_host import (
            _capability_digest as host_capability_digest,
        )
        from astrid.core.pack.discovery import discover_pack_metadata
        from astrid.sdk.actions import action_executor_definition

        # Schema-v3 packs register capabilities from pack.yaml ``actions``
        # (GenericPackHost.discover); mirror that projection exactly.
        pack_root = Path(raw_pack_root).expanduser().resolve()
        host_definition = None
        for discovered_pack in discover_pack_metadata(
            project_root=pack_root, extra_pack_roots=(str(pack_root),)
        ):
            pack = discovered_pack.pack
            if pack.root.resolve() != pack_root:
                continue
            for local_id, declaration in pack.actions.items():
                candidate = action_executor_definition(discovered_pack, local_id, declaration)
                if candidate.id == CAPABILITY_ID:
                    host_definition = candidate
                    break
            if host_definition is not None:
                break
    except (ImportError, OSError, TypeError, ValueError) as exc:
        raise _fail("could not resolve the host-registered image capability") from exc

    if host_definition is None or host_definition.id != CAPABILITY_ID:
        raise _fail("resolved host capability does not match the ordinary image route")
    return host_capability_digest(host_definition.to_dict())


def compose_image_publication_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Compose one closed, deterministic image publication request.

    The only accepted input keys are ``project``, ``capability_digest``, and
    ``params``.  The caller cannot provide a generation intent, settlement
    effect, selector, output path, capability id, module, or command.
    """
    request = _require_mapping(request, label="request")
    expected_keys = {"project", "capability_digest", "params"}
    unknown = sorted(str(key) for key in request if key not in expected_keys)
    if unknown:
        raise _fail("compose request contains unsupported field(s): " + ", ".join(unknown))
    missing = sorted(expected_keys - set(request))
    if missing:
        raise _fail("compose request is missing: " + ", ".join(missing))

    project = _validate_project(request["project"])
    expected_digest = _validate_digest(request["capability_digest"])
    params = _validate_params(request["params"])

    capability = get_capability(CAPABILITY_ID, kind="executor", include_elements=False)
    actual_digest = _capability_digest(capability)
    if expected_digest != actual_digest:
        raise _fail(
            "capability_digest does not match the configured GEN capability "
            f"{CAPABILITY_ID!r}"
        )

    registry = _load_model_registry()
    try:
        _entry, mode_spec = validate_generation_request(
            registry,
            model=params["model"],
            mode=params["mode"],
            execution=params["execution"],
            inputs=params,
            modality="image",
        )
    except Exception as exc:
        if isinstance(exc, CapabilityValidationError):
            raise
        raise _fail(str(exc)) from exc
    unsupported_controls = sorted(
        key for key in params if key in _OPTIONAL_CONTROL_KEYS and key not in mode_spec.supports
    )
    if unsupported_controls:
        raise _fail(
            f"model {params['model']!r} mode {params['mode']!r} does not declare: "
            + ", ".join(unsupported_controls)
        )

    generation_intent = _stable_generation_intent(params["count"])
    effect = _generation_publish_effect(
        capability,
        project=project,
        generation_intent=generation_intent,
    )
    return {
        "project": project,
        "capability_id": CAPABILITY_ID,
        "capability_digest": actual_digest,
        "schema_version": SCHEMA_VERSION,
        "input_object_ids": [],
        "spec": {
            "family": CAPABILITY_ID,
            "params": params,
            "output_policy": {},
        },
        "storage_estimate": dict(CLOUD_T2I_STORAGE_POLICY.estimate),
        "generation_intent": generation_intent,
        "settlement_effect": effect,
    }


def _error_payload(exc: Exception) -> dict[str, Any]:
    return {
        "code": "validation_error" if isinstance(exc, CapabilityValidationError) else "compose_error",
        "message": str(exc),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="read one JSON request from stdin and write one JSON response",
    )
    args = parser.parse_args(argv)
    if not args.json:
        parser.error("the fixed host adapter requires --json")
    try:
        raw = json.load(sys.stdin)
        response = {"ok": True, "request": compose_image_publication_request(raw)}
    except (AstridSDKError, TypeError, ValueError, KeyError) as exc:
        response = {"ok": False, "error": _error_payload(exc)}
        print(json.dumps(response, sort_keys=True, separators=(",", ":")))
        return 2
    print(json.dumps(response, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI test
    raise SystemExit(main())


__all__ = ["CAPABILITY_ID", "compose_image_publication_request", "main"]
