"""Attach-only helpers for a user's already-running, loopback ComfyUI."""

from __future__ import annotations

import json
import mimetypes
import re
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
from typing import Any


def validate_comfy_url(value: str) -> str:
    match = re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):([0-9]+)/?", value)
    if match is None or not 1 <= int(match[1]) <= 65535:
        raise ValueError("comfy_url must be http://127.0.0.1:<port> or http://localhost:<port>")
    return f"http://127.0.0.1:{int(match[1])}"


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


def validate_identity(value: str) -> str:
    # VibeComfy normalizes identifiers. Reject lossy values so task/attempt
    # fencing and the upload namespace retain the exact host-issued identity.
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9._:-]+", value) is None or value.strip("-") != value:
        raise ValueError("local_server requires an identity preserved by VibeComfy normalization")
    if value in {".", ".."}:
        raise ValueError("local_server requires a safe task identity")
    return value


def _request(url: str, path: str, payload: Any = None) -> Any:
    request = urllib.request.Request(
        validate_comfy_url(url) + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={} if payload is None else {"Content-Type": "application/json"},
    )
    # Never inherit proxies or follow a redirect to another destination.
    with _opener().open(request, timeout=5) as response:
        data = response.read()
    return json.loads(data) if data else {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def upload_image(url: str, source: Path, *, task_id: str) -> str:
    subfolder = f"astrid/{validate_identity(task_id)}"
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"managed image is not a regular file: {source}")
    media_type = mimetypes.guess_type(source.name)[0] or ""
    if not media_type.startswith("image/"):
        raise ValueError("local_server managed assets must be images")
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in (("subfolder", subfolder), ("type", "input")):
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    filename = source.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\nContent-Type: {media_type}\r\n\r\n'.encode())
    body.extend(source.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        validate_comfy_url(url) + "/upload/image", data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with _opener().open(request, timeout=30) as response:
        result = json.load(response)
    name = result.get("name")
    if (not isinstance(name, str) or PurePosixPath(name).name != name
            or name in {"", ".", ".."} or "\\" in name
            or result.get("subfolder") != subfolder or result.get("type") != "input"):
        raise ValueError("ComfyUI returned an unsafe uploaded image reference")
    return f"{subfolder}/{name}"


def cleanup_prompts(url: str, destination: Path) -> None:
    """Cancel only prompts witnessed in this attempt's private VibeComfy journal.

    This is safe to repeat after child termination: it neither clears a queue
    nor frees models. The host repeats it after the child has stopped, covering
    SIGKILL/crashes and acceptance that raced with an earlier cancel request.
    """
    url = validate_comfy_url(url)
    runs = destination / ".vibecomfy-runtime" / "out" / "runs"
    for path in runs.glob("*/attempt.json"):
        if path.is_symlink() or not path.resolve().is_relative_to(destination.resolve()):
            raise ValueError("local_server attempt journal escaped attempt custody")
        attempt = json.loads(path.read_text(encoding="utf-8"))
        acceptance = attempt.get("queue_acceptance", {})
        prompt_id = acceptance.get("prompt_id")
        if acceptance.get("status") != "accepted" or not isinstance(prompt_id, str) or not prompt_id:
            continue
        _request(url, "/queue", {"delete": [prompt_id]})
        queue = _request(url, "/queue")
        running = queue.get("queue_running", [])
        if any(isinstance(item, list) and len(item) > 1 and item[1] == prompt_id for item in running):
            # Recent ComfyUI also guards the check/interrupt race server-side.
            _request(url, "/interrupt", {"prompt_id": prompt_id})
