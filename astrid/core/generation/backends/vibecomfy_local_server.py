"""Attach-only helpers for a user's already-running, loopback ComfyUI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .comfy_target import (
    ComfyTarget,
    ComfyTargetClient,
    validate_identity,
)
from .comfy_target import (
    validate_attached_url as validate_comfy_url,
)

__all__ = [
    "cleanup_prompts", "upload_image", "upload_media", "validate_comfy_url",
    "validate_identity", "validate_output_directory",
]


def validate_output_directory(value: str, attempt_root: Path) -> str:
    if not value:
        return ""
    path = Path(value)
    if (not path.is_absolute() or not path.is_dir()
            or any(part.is_symlink() for part in (path, *path.parents))):
        raise ValueError("comfy_output_dir must be an absolute, existing, non-symlink directory")
    resolved = path.resolve(strict=True)
    if resolved.is_relative_to(attempt_root.resolve()):
        raise ValueError("comfy_output_dir must be outside the attempt root")
    return str(resolved)


def _request(url: str, path: str, payload: Any = None) -> Any:
    client = ComfyTargetClient(ComfyTarget(url, "attached"))
    if path == "/prompt" and payload is not None:
        return client.submit_prompt(payload)
    if path.startswith("/history/") and payload is None:
        return client.history(path.removeprefix("/history/"))
    return client.request_json(path, payload, timeout=5)


def upload_image(url: str, source: Path, *, task_id: str) -> str:
    return upload_media(url, source, task_id=task_id, kind="image")


def upload_media(
    url: str, source: Path, *, task_id: str, kind: str = "image",
    expected_sha256: str | None = None,
) -> str:
    """Upload verified media using the shared attach-only HTTP client."""
    return ComfyTargetClient(ComfyTarget(url, "attached")).upload_media(
        source, task_id=task_id, kind=kind, expected_sha256=expected_sha256,
    )


def cleanup_prompts(url: str, destination: Path) -> None:
    """Cancel only prompts witnessed in this attempt's private VibeComfy journal.

    This is safe to repeat after child termination: it neither clears a queue
    nor frees models. The host repeats it after the child has stopped, covering
    SIGKILL/crashes and acceptance that raced with an earlier cancel request.
    """
    client = ComfyTargetClient(ComfyTarget(url, "attached"))
    runs = destination / ".vibecomfy-runtime" / "out" / "runs"
    for path in runs.glob("*/attempt.json"):
        if path.is_symlink() or not path.resolve().is_relative_to(destination.resolve()):
            raise ValueError("local_server attempt journal escaped attempt custody")
        attempt = json.loads(path.read_text(encoding="utf-8"))
        acceptance = attempt.get("queue_acceptance", {})
        prompt_id = acceptance.get("prompt_id")
        if acceptance.get("status") != "accepted" or not isinstance(prompt_id, str) or not prompt_id:
            continue
        client.witness_prompt(prompt_id)
        client.cancel_prompt(prompt_id)
