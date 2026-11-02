#!/usr/bin/env python3
"""Generate and verify one source-bound Pinokio journey bundle.

This is a compatibility check, not a journey receipt, custody assertion, or
qualification decision.  It executes the Pinokio request serializer, Agent
consumer parser, and Agent materializer route builder against immutable local
checkouts before a hosted request is composed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

try:
    from . import ci_journey_provider as provider
    from . import ci_journey_r24 as r24
    from . import ci_journey_r24_materializer as materializer
    from . import ci_journey_r24_push_admission as push_admission
    from . import pinokio_linux_mode_actions as mode_actions
except ImportError:
    import ci_journey_provider as provider
    import ci_journey_r24 as r24
    import ci_journey_r24_materializer as materializer
    import ci_journey_r24_push_admission as push_admission
    import pinokio_linux_mode_actions as mode_actions


SCHEMA = "astrid.pinokio.bundle-compatibility.v1"
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_PUBLISHED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_CANDIDATE_FIELDS = {"app", "agent", "runtime", "pinokio", "manifest_sha256"}
_TUPLE_FIELDS = _CANDIDATE_FIELDS | {"artifacts", "published_at"}
_ARTIFACT_PATHS = {
    "public/install": "public/install",
    "public/install.sh": "public/install.sh",
    "public/install.ps1": "public/install.ps1",
}
_RUNNER_SOURCES = (
    ".github/workflows/r24-linux-live.yml",
    "scripts/ci_journey.py",
    "scripts/ci_journey_provider.py",
    "scripts/ci_journey_r24.py",
    "scripts/ci_journey_r24_materializer.py",
    "scripts/ci_journey_r24_push_admission.py",
    "scripts/pinokio_bundle_compat.py",
    "scripts/pinokio_linux_mode_actions.py",
    "scripts/pinokio_r24_journey_seam.py",
)


class CompatibilityError(RuntimeError):
    """The local immutable bundle is not compatible."""


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise CompatibilityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                CompatibilityError("invalid JSON constant")
            ),
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CompatibilityError("invalid JSON") from exc


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise CompatibilityError(f"missing regular file: {path}")
    return path.read_bytes()


def load_current_tuple(path: Path) -> tuple[bytes, dict[str, Any], dict[str, str]]:
    if not path.is_absolute() or path.is_symlink():
        raise CompatibilityError("absolute nonsymlink CURRENT-TUPLE.json required")
    raw = _regular(path.resolve(strict=True))
    value = _decode(raw)
    if not isinstance(value, dict) or set(value) != _TUPLE_FIELDS:
        raise CompatibilityError("CURRENT-TUPLE fields differ")
    candidate = {key: value[key] for key in _CANDIDATE_FIELDS}
    if any(not isinstance(candidate[key], str) or not _HEX40.fullmatch(candidate[key])
           for key in ("app", "agent", "runtime", "pinokio")):
        raise CompatibilityError("CURRENT-TUPLE commit identity differs")
    if (not isinstance(candidate["manifest_sha256"], str)
            or not _HEX64.fullmatch(candidate["manifest_sha256"])):
        raise CompatibilityError("CURRENT-TUPLE manifest identity differs")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != set(_ARTIFACT_PATHS):
        raise CompatibilityError("CURRENT-TUPLE artifact inventory differs")
    if any(not isinstance(digest, str) or not _HEX64.fullmatch(digest)
           for digest in artifacts.values()):
        raise CompatibilityError("CURRENT-TUPLE artifact identity differs")
    if (not isinstance(value["published_at"], str)
            or not _PUBLISHED.fullmatch(value["published_at"])):
        raise CompatibilityError("CURRENT-TUPLE publication time differs")
    return raw, value, candidate


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args], stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CompatibilityError("Git identity validation failed") from exc


def _checkout(path: Path, ref: str) -> Path:
    if (not path.is_absolute() or path.is_symlink() or not path.is_dir()
            or not _HEX40.fullmatch(ref)):
        raise CompatibilityError("absolute checkout and immutable ref required")
    root = path.resolve(strict=True)
    top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").strip())).resolve()
    head = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    dirty = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if top != root or head != ref or dirty:
        raise CompatibilityError(f"checkout identity or cleanliness differs: {path.name}")
    return root


def _bound_sources(root: Path, ref: str, sources: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative, expected in sources.items():
        working = _regular(root / relative)
        committed = _git(root, "show", f"{ref}:{relative}")
        if _sha(working) != expected or _sha(committed) != expected:
            raise CompatibilityError(f"source digest differs: {relative}")
        result[relative] = expected
    return result


def _runner_sources(root: Path, ref: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in _RUNNER_SOURCES:
        working = _regular(root / relative)
        committed = _git(root, "show", f"{ref}:{relative}")
        if working != committed:
            raise CompatibilityError(f"runner source differs from commit: {relative}")
        result[relative] = _sha(working)
    return result


def _producer_module(root: Path) -> ModuleType:
    source = root / "pinokio/providers/r24-linux.py"
    spec = importlib.util.spec_from_file_location("pinokio_bundle_r24_producer", source)
    if spec is None or spec.loader is None:
        raise CompatibilityError("producer module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def boundary() -> dict[str, object]:
    return {
        "authenticated_custody": False,
        "qualification": "not-evaluated",
        "m4_6_receipt": False,
        "live_action_executed": False,
    }


def generate(args: argparse.Namespace) -> dict[str, Any]:
    tuple_raw, full_tuple, candidate = load_current_tuple(args.current_tuple)
    if candidate != r24.PUBLISHED_CANDIDATE or candidate != provider.LIVE_CANDIDATE:
        raise CompatibilityError("runner candidate differs from CURRENT-TUPLE")
    if not (args.producer_ref == provider.LIVE_PRODUCER_REF
            == mode_actions.MODE_PRODUCER_REF == push_admission.PRODUCER_REF):
        raise CompatibilityError("producer refs do not form one bundle")

    runner = _checkout(args.runner_checkout, args.runner_ref)
    producer = _checkout(args.producer_checkout, args.producer_ref)
    mode_producer = _checkout(args.mode_producer_checkout, args.producer_ref)
    if len({runner, producer, mode_producer}) != 3:
        raise CompatibilityError("runner and producer checkouts must be distinct")
    checkouts = {
        "app": _checkout(args.app_checkout, candidate["app"]),
        "agent": _checkout(args.agent_checkout, candidate["agent"]),
        "runtime": _checkout(args.runtime_checkout, candidate["runtime"]),
        "pinokio": _checkout(args.pinokio_checkout, candidate["pinokio"]),
    }
    if len({runner, producer, mode_producer, *checkouts.values()}) != 7:
        raise CompatibilityError("all bundle checkouts must be distinct")

    manifest = _regular(checkouts["app"] / "public/install-manifest.json")
    parsed_manifest = _decode(manifest)
    if (_sha(manifest) != candidate["manifest_sha256"]
            or not isinstance(parsed_manifest, dict)
            or parsed_manifest.get("release") != 1
            or any((parsed_manifest.get(name) or {}).get("sha") != candidate[name]
                   for name in ("agent", "runtime"))):
        raise CompatibilityError("App manifest differs from CURRENT-TUPLE")
    for name, relative in _ARTIFACT_PATHS.items():
        if _sha(_regular(checkouts["app"] / relative)) != full_tuple["artifacts"][name]:
            raise CompatibilityError(f"App artifact differs from CURRENT-TUPLE: {name}")

    producer_sources = {
        **provider.LIVE_PRODUCER_FILES,
        **provider.LIVE_INTERFACE_FILES,
        **mode_actions.MODE_PRODUCER_FILES,
    }
    _bound_sources(producer, args.producer_ref, producer_sources)
    _bound_sources(mode_producer, args.producer_ref, producer_sources)
    binding = _decode(_regular(producer / "pinokio/providers/r24-linux-binding.json"))
    if (not isinstance(binding, dict) or binding.get("candidate") != candidate
            or binding.get("stop_sha256") != provider.LIVE_STOP["sha256"]):
        raise CompatibilityError("producer binding differs from CURRENT-TUPLE")
    stop = _regular(checkouts["pinokio"] / "pinokio/stop.js")
    if _sha(stop) != provider.LIVE_STOP["sha256"]:
        raise CompatibilityError("published Pinokio Stop differs from producer binding")

    request_data = {
        "schema": provider.REQUEST_SCHEMA,
        "candidate": candidate,
        "provider": {
            "ref": args.producer_ref,
            "sources": dict(provider.LIVE_PRODUCER_FILES),
        },
        "stop": dict(provider.LIVE_STOP),
        "config_sha256": _sha(_canonical({
            "compatibility": SCHEMA,
            "tuple_sha256": _sha(tuple_raw),
        })),
        "run_id": _sha(tuple_raw)[:32],
    }
    producer_module = _producer_module(producer)
    if producer_module.BINDING != binding:
        raise CompatibilityError("loaded producer binding differs")
    producer_request = producer_module.canonical(request_data)
    consumed_request = provider.ProviderRequest.parse(producer_request)
    if consumed_request.canonical != producer_request:
        raise CompatibilityError("producer and consumer canonical requests differ")

    route = materializer.route_argv(
        runner, args.scratch_root, args.scratch_root / "candidate.json",
        args.scratch_root / "config.json", producer, checkouts,
        mode="backend", mode_producer_root=mode_producer,
    )
    if route[3:5] != ["--shared-executor", "r25-linux-mode-actions"]:
        raise CompatibilityError("runner materializer route differs")
    admission = push_admission.validate_admission(
        {
            "GITHUB_REPOSITORY": push_admission.REPOSITORY,
            "GITHUB_SHA": args.runner_ref,
            "GITHUB_WORKFLOW_SHA": args.runner_ref,
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/lane/M-PIN/R-PIN-INTEGRATOR",
            "GITHUB_WORKFLOW_REF": (
                f"{push_admission.REPOSITORY}/{push_admission.WORKFLOW_PATH}"
                "@refs/heads/lane/M-PIN/R-PIN-INTEGRATOR"
            ),
        },
        runner_ref=args.runner_ref,
        producer_ref=args.producer_ref,
        confirmation=push_admission.CONFIRMATION,
    )
    return {
        "schema": SCHEMA,
        "status": "compatible",
        "generated_from": {
            "path": str(args.current_tuple.resolve(strict=True)),
            "sha256": _sha(tuple_raw),
        },
        "tuple": full_tuple,
        "runner": {
            "ref": args.runner_ref,
            "sources": _runner_sources(runner, args.runner_ref),
            "shared_executor": "r25-linux-mode-actions",
        },
        "producer": {
            "ref": args.producer_ref,
            "sources": producer_sources,
            "binding_sha256": _sha(_regular(
                producer / "pinokio/providers/r24-linux-binding.json"
            )),
        },
        "compatibility": {
            "producer_request_sha256": consumed_request.sha256,
            "producer_serializer_executed": True,
            "consumer_parser_executed": True,
            "materializer_route_built": True,
            "hosted_admission_inputs": admission,
            "route": route,
        },
        "boundary": boundary(),
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--current-tuple", type=Path, required=True)
    value.add_argument("--runner-checkout", type=Path, required=True)
    value.add_argument("--runner-ref", required=True)
    value.add_argument("--producer-checkout", type=Path, required=True)
    value.add_argument("--mode-producer-checkout", type=Path, required=True)
    value.add_argument("--producer-ref", required=True)
    value.add_argument("--app-checkout", type=Path, required=True)
    value.add_argument("--agent-checkout", type=Path, required=True)
    value.add_argument("--runtime-checkout", type=Path, required=True)
    value.add_argument("--pinokio-checkout", type=Path, required=True)
    value.add_argument("--scratch-root", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.scratch_root.is_absolute() or not args.scratch_root.is_dir():
        raise CompatibilityError("absolute existing scratch root required")
    result = generate(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(json.dumps(
        result, indent=2, sort_keys=True, allow_nan=False,
    ).encode("ascii") + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
