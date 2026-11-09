"""Private execution support for the typed VibeComfy media actions.

This is a thin adapter: the media compiler owns request semantics, the
production engine owns profile execution, and GenericPackHost owns lifecycle,
CAS, storage, and settlement.  If a pinned ready template/profile is absent,
the command fails closed; it never emits a synthetic media artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Mapping

from astrid.pack_api import RecoverableArgumentParser, StaticChoices
from astrid.core.generation.storage_policy import RUNTIME_OBJECT_MAX_BYTES

from .compiler import (  # noqa: E402
    CharacterAnimationRequest,
    VideoEnhanceRequest,
    compile_character_animation,
    compile_video_enhance,
    ensure_media_capability_supported,
)
from .direct_executor import DirectVibeMediaExecutor  # noqa: E402


def _bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"expected a boolean, got {value!r}")


def _read_profile(path: str, digest: str) -> tuple[Mapping[str, Any], dict[str, Any]]:
    if path == "-":
        profile: Mapping[str, Any] = {}
    else:
        raw = Path(path).read_bytes()
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        if digest == "-" or actual != digest:
            raise RuntimeError("readiness profile hash does not match the supplied profile")
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, Mapping):
            raise RuntimeError("readiness profile is not an object")
        profile = parsed
    facts = profile.get("verified_facts") if isinstance(profile, Mapping) else None
    exact = facts.get("exact") if isinstance(facts, Mapping) else {}
    if not isinstance(exact, Mapping):
        exact = {}
    runtime = profile.get("runtime") if isinstance(profile, Mapping) else {}
    if not isinstance(runtime, Mapping):
        runtime = {}
    return profile, {
        "profile_digest": digest if digest != "-" else None,
        "model_bytes_digest": exact.get("model_digest"),
        "runtime_instance_id": runtime.get("runtime_instance_id"),
        "environment_fingerprint": profile.get("environment_fingerprint") or exact.get("engine_lock"),
        "declared_root": exact.get("root"),
        "declared_port": exact.get("port"),
    }


def _assert_runtime_object_size(path: Path, *, label: str) -> None:
    size = path.stat().st_size
    if size > RUNTIME_OBJECT_MAX_BYTES:
        raise RuntimeError(
            f"{label} exceeds the Runtime per-object limit of "
            f"{RUNTIME_OBJECT_MAX_BYTES} bytes"
        )


def _run(
    *,
    capability: str,
    request: VideoEnhanceRequest | CharacterAnimationRequest,
    task_identity: str,
    profile_id: str,
    readiness_profile_path: str,
    readiness_profile_hash: str,
    out: Path,
) -> None:
    # Validate the bounded graph profile ahead of readiness/profile resolution,
    # scratch creation, and the runner. A rejected typed media control must not
    # create an attempt directory or touch a GPU session.
    ensure_media_capability_supported(capability)
    if capability == "vibecomfy.video_enhance":
        compile_video_enhance(request, profile=profile_id)  # type: ignore[arg-type]
    elif capability == "vibecomfy.character_animation":
        compile_character_animation(request, profile=profile_id)  # type: ignore[arg-type]
    profile_document, runtime_context = _read_profile(readiness_profile_path, readiness_profile_hash)
    out.mkdir(parents=True, exist_ok=True)

    from astrid.packs.vibecomfy.production_engine import _load_workflow, _run_profile

    def runner(
        workflow: Mapping[str, Any],
        *,
        capability_id: str,
        model_identity: str,
        profile_id: str,
        task_identity: str,
    ) -> Mapping[str, Any]:
        resolved = _load_workflow(workflow, {}, out)
        raw_outputs = _run_profile(
            resolved,
            profile_id,
            profile_document,
            model_id=model_identity,
            template_id=str(workflow.get("template_id") or capability_id),
            task_identity=task_identity,
            destination=out,
        )
        if not raw_outputs:
            raise RuntimeError("VibeComfy produced no output artifacts")
        return {"paths": [str(Path(value).resolve(strict=True)) for value in raw_outputs]}

    executor = DirectVibeMediaExecutor(profile_id, runner)
    if capability == "vibecomfy.video_enhance":
        result = executor.execute_video_enhance(
            request, task_identity=task_identity, runtime_context=runtime_context  # type: ignore[arg-type]
        )
    elif capability == "vibecomfy.character_animation":
        result = executor.execute_character_animation(
            request, task_identity=task_identity, runtime_context=runtime_context  # type: ignore[arg-type]
        )
    else:
        raise RuntimeError(f"unsupported typed Vibe media capability: {capability}")
    paths = result.outputs.get("paths")
    if not isinstance(paths, list) or not paths:
        raise RuntimeError("typed Vibe media runner returned no output paths")
    source = Path(str(paths[0])).resolve(strict=True)
    destination = (out / "output.mp4").resolve()
    if source != destination:
        shutil.copy2(source, destination)
    _assert_runtime_object_size(destination, label="typed Vibe output")
    output_name = {
        "vibecomfy.video_enhance": "enhanced_video",
        "vibecomfy.character_animation": "animated_video",
    }[capability]
    result_payload = result.to_dict()
    # The GenericPackHost receipt is a file inventory, not the executor's
    # internal ``outputs`` mapping. Preserve the latter under an explicit
    # execution key while emitting the canonical list consumed by CAS harvest.
    result_payload["execution_result"] = dict(result_payload["outputs"])
    result_payload["outputs"] = [{
        "path": "output.mp4",
        "name": output_name,
        "ordinal": 0,
        "role": "result",
        "is_primary": True,
        "content_hash": "sha256:" + hashlib.sha256(destination.read_bytes()).hexdigest(),
        "bytes": destination.stat().st_size,
    }]
    (out / "manifest.json").write_text(
        json.dumps(
            result_payload
            | {"schema_version": 1, "kind": "video", "inputs": {}, "output": "output.mp4"},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _assert_runtime_object_size(out / "manifest.json", label="typed Vibe manifest")


def build_parser(capability: str) -> argparse.ArgumentParser:
    parser = RecoverableArgumentParser()
    parser.add_argument("--capability", required=True, choices=StaticChoices((capability,)))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--task-identity", required=True)
    parser.add_argument(
        "--profile",
        choices=StaticChoices(("pip_embedded", "checkout_server")),
        required=True,
    )
    parser.add_argument("--readiness-profile-path", default="-")
    parser.add_argument("--readiness-profile-hash", default="-")
    parser.add_argument("--video-ref")
    parser.add_argument("--enable-upscale", type=_bool, default=True)
    parser.add_argument("--scale", type=float, default=2.0)
    parser.add_argument("--color-fix", type=_bool, default=False)
    parser.add_argument("--output-quality", default="maximum")
    parser.add_argument("--reference-image-ref")
    parser.add_argument("--driving-video-ref")
    parser.add_argument("--mode")
    parser.add_argument("--resolution")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--seed", type=int, default=42)
    return parser
