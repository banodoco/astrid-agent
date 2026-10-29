"""HTTP operations and artifact custody for an already-selected Comfy target.

This client owns neither a server nor a pod. Target discovery, attestation and
lifecycle remain with their existing owners; a pod's injected transport retains
the claimed HTTPS route and chunk-integrity boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from astrid.core.generation.engine_contracts import TransportError
from astrid.core.path_utils import has_url_scheme, is_native_absolute_path

MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024


def validate_attached_url(value: str) -> str:
    match = re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):([0-9]+)/?", value)
    if match is None or not 1 <= int(match[1]) <= 65535:
        raise ValueError("comfy_url must be http://127.0.0.1:<port> or http://localhost:<port>")
    return f"http://127.0.0.1:{int(match[1])}"


def validate_identity(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9._:-]+", value) is None or value.strip("-") != value:
        raise ValueError("local_server requires an identity preserved by VibeComfy normalization")
    if value in {".", ".."}:
        raise ValueError("local_server requires a safe task identity")
    return value


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("checkout_server remote request redirected unexpectedly")


def open_comfy_http(request: urllib_request.Request, *, timeout: float) -> Any:
    """Refuse redirects; bypass proxies only on the local HTTP leg."""
    handlers: list[Any] = [_NoRedirectHandler]
    host = urllib_parse.urlsplit(request.full_url).hostname or ""
    if host == "localhost" or host.startswith("127.") or host == "::1":
        handlers.append(urllib_request.ProxyHandler({}))
    return urllib_request.build_opener(*handlers).open(request, timeout=timeout)


@dataclass(frozen=True)
class ComfyTarget:
    url: str
    kind: Literal["managed", "attached", "pod"]
    facts: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in {"managed", "attached", "pod"}:
            raise ValueError("unknown Comfy target kind")
        if not isinstance(self.facts, Mapping):
            raise ValueError("Comfy target facts must be an object")
        # Pod URLs include /engines/<id> and are validated against the injected
        # attested transport before its gateway is started.
        if self.kind == "attached":
            object.__setattr__(self, "url", validate_attached_url(self.url))
        elif self.kind == "managed":
            object.__setattr__(self, "url", validate_managed_url(self.url))
        elif not isinstance(self.url, str) or not self.url:
            raise ValueError("pod Comfy target requires its attested endpoint URL")
        object.__setattr__(self, "facts", MappingProxyType(dict(self.facts)))


class ComfyTargetClient:
    """Bounded HTTP and custody for one attempt, with no lifecycle ownership.

    A client submits at most one prompt, regardless of whether its acceptance
    reply arrives. Reconciliation or a new attempt belongs to the caller.
    """

    def __init__(self, target: ComfyTarget, *, endpoint_transport=None, opener=None):
        if not isinstance(target, ComfyTarget):
            raise TypeError("ComfyTargetClient requires a ComfyTarget")
        if target.kind == "pod":
            from astrid.core.generation.engine_transport import EngineEndpointTransport

            if (not isinstance(endpoint_transport, EngineEndpointTransport)
                    or target.url != endpoint_transport.endpoint.url):
                raise ValueError("pod Comfy target requires its exact attested EngineEndpointTransport")
        elif endpoint_transport is not None:
            raise ValueError("only a pod Comfy target accepts an endpoint transport")
        self.target = target
        self.endpoint_transport = endpoint_transport
        self._opener = opener or open_comfy_http
        self._prompt_lock = threading.Lock()
        self._prompt_attempted = False
        self._witnessed_prompts: set[str] = set()

    @property
    def http_origin(self) -> str:
        return self.target.url if self.endpoint_transport is None else self.endpoint_transport.server_url

    @staticmethod
    def _prompt_id(value: object) -> str:
        if (not isinstance(value, str) or not value
                or re.fullmatch(r"[A-Za-z0-9._:-]+", value) is None or value in {".", ".."}):
            raise ValueError("Comfy prompt identity is unsafe")
        return value

    def witness_prompt(self, prompt_id: str) -> None:
        """Import acceptance witnessed in this attempt's private journal only."""
        self._witnessed_prompts.add(self._prompt_id(prompt_id))

    @staticmethod
    def _read_bounded(response, limit: int, deadline: float) -> bytes:
        blocks, size = [], 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Comfy response deadline exceeded")
            block = response.read(min(64 * 1024, limit + 1 - size))
            if not block:
                return b"".join(blocks)
            size += len(block)
            if size > limit:
                raise ValueError("checkout_server response is too large")
            blocks.append(block)

    def _json(self, path, payload=None, *, timeout=10.0, response_limit=64 * 1024,
              acknowledge_empty=True):
        timeout = float(timeout)
        if not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError("Comfy request timeout must be finite and at most 30 seconds")
        if not isinstance(response_limit, int) or not 0 < response_limit <= MAX_JSON_BYTES:
            raise ValueError("invalid Comfy response bound")
        body = None if payload is None else json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if body is not None and len(body) > MAX_JSON_BYTES:
            raise ValueError("Comfy request is too large")
        request = urllib_request.Request(
            self.http_origin + path, data=body,
            headers={"Accept-Encoding": "identity", **({"Content-Type": "application/json"} if body is not None else {})},
            method="GET" if body is None else "POST",
        )
        deadline = time.monotonic() + timeout
        with self._opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if not 200 <= status < 300:
                raise ValueError(f"checkout_server {path} returned a non-2xx status")
            raw = self._read_bounded(response, response_limit, deadline)
        if not raw and acknowledge_empty:
            return {"acknowledged": True}
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            if acknowledge_empty:
                return {"acknowledged": True}
            raise ValueError("Comfy response is not JSON") from None

    def request_json(self, path: str, payload: Any = None, *, timeout: float = 10.0,
                     response_limit: int = 64 * 1024) -> Any:
        """Owner control seam; attached targets permit only scoped cancellation."""
        if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
                or "?" in path or "#" in path or "\\" in path
                or "%" in path or any(part in {".", ".."} for part in path.split("/"))
                or any(char.isspace() or ord(char) < 32 for char in path)):
            raise ValueError("unsafe Comfy request path")
        if payload is not None and path in {"/prompt", "/api/prompt"}:
            raise ValueError("use submit_prompt to preserve prompt acceptance")
        if payload is None:
            allowed = path in {"/queue", "/history", "/system_stats", "/object_info"}
            if path.startswith("/history/"):
                self._prompt_id(path.removeprefix("/history/"))
                allowed = True
        else:
            allowed = path in {"/queue", "/interrupt", "/api/free"}
        if not allowed:
            raise ValueError("unsupported Comfy HTTP operation")
        if self.target.kind == "attached" and payload is not None:
            scoped_delete = (path == "/queue" and isinstance(payload, dict)
                             and set(payload) == {"delete"} and isinstance(payload["delete"], list)
                             and bool(payload["delete"])
                             and all(isinstance(item, str) and item in self._witnessed_prompts
                                     for item in payload["delete"]))
            scoped_interrupt = (path == "/interrupt" and isinstance(payload, dict)
                                and set(payload) == {"prompt_id"}
                                and isinstance(payload["prompt_id"], str)
                                and payload["prompt_id"] in self._witnessed_prompts)
            if not (scoped_delete or scoped_interrupt):
                raise ValueError("attached Comfy target refuses unowned control operations")
        try:
            return self._json(path, payload, timeout=timeout, response_limit=response_limit)
        except (OSError, urllib_error.URLError) as exc:
            raise ValueError(f"checkout_server {path} request failed") from exc

    def object_info(self) -> dict[str, Any]:
        """Capture schema through this target's existing attested transport."""
        if self.endpoint_transport is not None:
            payload = self.endpoint_transport.object_info(deadline=time.monotonic() + 30)
        else:
            payload = self.request_json("/object_info", timeout=30, response_limit=MAX_JSON_BYTES)
        if not isinstance(payload, dict):
            raise ValueError("Comfy object_info response is not an object")
        return payload

    def submit_prompt(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("Comfy prompt payload must be an object")
        # Freeze and bound before marking the attempt; there is no retry after
        # the first wire request, including a lost acceptance acknowledgement.
        body = json.loads(json.dumps(dict(payload)))
        if len(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()) > MAX_JSON_BYTES:
            raise ValueError("Comfy prompt request is too large")
        with self._prompt_lock:
            if self._prompt_attempted:
                raise TransportError("prompt already attempted; do not resubmit; reconcile the original attempt")
            self._prompt_attempted = True
        try:
            result = self._json("/prompt", body, timeout=30, acknowledge_empty=False)
            if not isinstance(result, dict):
                raise ValueError("Comfy prompt acceptance is not an object")
            self.witness_prompt(result.get("prompt_id"))
            return result
        except urllib_error.HTTPError as exc:
            if exc.code in {400, 401, 403, 404, 422}:
                raise
            raise self._unknown_acceptance() from exc
        except Exception as exc:
            raise self._unknown_acceptance() from exc

    @staticmethod
    def _unknown_acceptance():
        error = TransportError("prompt acceptance unknown; do not resubmit; reconcile the original attempt")
        error.acceptance_known = False
        error.prompt_id = None
        return error

    def history(self, prompt_id: str) -> dict[str, Any]:
        prompt_id = self._prompt_id(prompt_id)
        result = self._json("/history/" + urllib_parse.quote(prompt_id, safe=""), timeout=30,
                            response_limit=MAX_JSON_BYTES, acknowledge_empty=False)
        if not isinstance(result, dict):
            raise ValueError("Comfy history response is not an object")
        return result

    def cancel_prompt(self, prompt_id: str) -> None:
        prompt_id = self._prompt_id(prompt_id)
        if prompt_id not in self._witnessed_prompts:
            raise ValueError("Comfy cancellation requires witnessed prompt acceptance")
        self.request_json("/queue", {"delete": [prompt_id]})
        queue = self.request_json("/queue")
        running = queue.get("queue_running", []) if isinstance(queue, dict) else []
        if any(isinstance(item, list) and len(item) > 1 and item[1] == prompt_id for item in running):
            self.request_json("/interrupt", {"prompt_id": prompt_id})

    def upload_media(self, source: Path, *, task_id: str, kind: str = "image",
                     expected_sha256: str | None = None) -> str:
        if kind not in {"image", "video", "audio"}:
            raise ValueError(f"unsupported local_server managed asset kind: {kind}")
        source = Path(source)
        subfolder = f"astrid/{validate_identity(task_id)}"
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"managed {kind} is not a regular file: {source}")
        media_type = mimetypes.guess_type(source.name)[0] or ""
        if not media_type.startswith(f"{kind}/"):
            if kind == "image":
                raise ValueError("local_server managed assets must be images")
            raise ValueError(f"local_server managed {kind} asset must have a {kind}/* media type")
        with source.open("rb") as handle:
            data = handle.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("managed media upload is too large")
        digest = hashlib.sha256(data).hexdigest()
        if (kind != "image" or expected_sha256 is not None) and (
                expected_sha256 is None or digest != _digest_hex(expected_sha256)):
            raise ValueError(f"managed {kind} failed hash verification: {source.name}")
        filename = source.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        if self.endpoint_transport is not None:
            root = self._pod_directory("input_directory")
            # Only chunk put can enforce the claimed agent's exact input-file
            # permission. Freeze the verified bytes so a source race cannot
            # change the upload after hashing.
            with tempfile.TemporaryDirectory(prefix="astrid-comfy-upload-") as staging:
                frozen = Path(staging) / filename
                frozen.write_bytes(data)
                self.endpoint_transport.put(frozen, str(root / subfolder / filename))
            return f"{subfolder}/{filename}"
        boundary = uuid.uuid4().hex
        body = bytearray()
        for name, value in (("subfolder", subfolder), ("type", "input")):
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\nContent-Type: {media_type}\r\n\r\n'.encode())
        body.extend(data)
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        request = urllib_request.Request(self.http_origin + "/upload/image", data=bytes(body),
                                        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                                 "Accept-Encoding": "identity"})
        deadline = time.monotonic() + 30
        with self._opener(request, timeout=30) as response:
            if not 200 <= getattr(response, "status", 200) < 300:
                raise ValueError("Comfy upload returned a non-2xx status")
            result = json.loads(self._read_bounded(response, 64 * 1024, deadline))
        name = result.get("name") if isinstance(result, dict) else None
        if (not isinstance(name, str) or PurePosixPath(name).name != name
                or name in {"", ".", ".."} or "\\" in name
                or result.get("subfolder") != subfolder or result.get("type") != "input"):
            raise ValueError("ComfyUI returned an unsafe uploaded image reference")
        return f"{subfolder}/{name}"

    def _pod_directory(self, name: str) -> Path:
        from runpod_lifecycle.pod_agent import observation_path

        value = self.target.facts.get(name)
        observation_path(value)
        return Path(value)

    def download_output(self, descriptor: object, destination: Path, *,
                        expected_sha256: str | None = None) -> Path:
        filename, subfolder, output_type = validate_output_descriptor(descriptor)
        expected = None if expected_sha256 is None else _digest_hex(expected_sha256)
        destination = Path(destination)
        if destination.is_symlink():
            raise ValueError("Comfy custody destination must not be a symlink")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".checkout-download-")
            os.close(fd)
            temporary_path = Path(name)
            digest = hashlib.sha256()
            size = 0
            if self.endpoint_transport is not None:
                if output_type != "output":
                    raise ValueError("remote final artifact must belong to the configured output root")
                remote = self._pod_directory("output_directory") / subfolder / filename
                # Existing chunk custody verifies the transfer; its own atomic
                # replacement may change the inode, so fsync the retrieved file.
                self.endpoint_transport.get(str(remote), temporary_path)
                with temporary_path.open("r+b") as retrieved:
                    for block in iter(lambda: retrieved.read(_CHUNK_BYTES), b""):
                        size += len(block)
                        if size > MAX_OUTPUT_BYTES:
                            raise ValueError("Comfy output is too large")
                        digest.update(block)
                    os.fsync(retrieved.fileno())
            else:
                query = urllib_parse.urlencode({"filename": filename, "subfolder": subfolder, "type": output_type})
                request = urllib_request.Request(self.http_origin + "/view?" + query,
                                                 headers={"Accept-Encoding": "identity"}, method="GET")
                deadline = time.monotonic() + 30
                with self._opener(request, timeout=30) as response, temporary_path.open("wb") as temporary:
                    if getattr(response, "status", 200) != 200:
                        raise ValueError("checkout_server /view returned a non-200 status")
                    headers = getattr(response, "headers", {})
                    encoding = headers.get("Content-Encoding", "identity")
                    if encoding.lower() != "identity":
                        raise ValueError("Comfy output encoding is unsupported")
                    declared_length = headers.get("Content-Length")
                    if declared_length is not None:
                        if not declared_length.isdecimal() or int(declared_length) > MAX_OUTPUT_BYTES:
                            raise ValueError("Comfy output length is invalid or too large")
                        declared_length = int(declared_length)
                    while True:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Comfy output deadline exceeded")
                        block = response.read(_CHUNK_BYTES)
                        if not block:
                            break
                        size += len(block)
                        if size > MAX_OUTPUT_BYTES:
                            raise ValueError("Comfy output is too large")
                        digest.update(block)
                        temporary.write(block)
                    if declared_length is not None and size != declared_length:
                        raise ValueError("Comfy output length differs from response framing")
                    temporary.flush()
                    os.fsync(temporary.fileno())
            if expected is not None and digest.hexdigest() != expected:
                raise ValueError(f"checkout_server downloaded output {filename!r} changed during custody")
            os.replace(temporary_path, destination)
            temporary_path = None
            return destination
        except (OSError, urllib_error.URLError) as exc:
            raise ValueError(f"checkout_server could not download output {filename!r}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _digest_hex(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid Comfy artifact SHA-256")
    value = value.removeprefix("sha256:")
    if re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError("invalid Comfy artifact SHA-256")
    return value.lower()


def validate_managed_url(server_url: str) -> str:
    """Return a canonical, origin-only HTTP(S) endpoint."""
    if not isinstance(server_url, str) or not server_url:
        raise ValueError("checkout_server requires an explicit server_url")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in server_url):
        raise ValueError("checkout_server server_url must not contain whitespace or control characters")
    if "\\" in server_url:
        raise ValueError("checkout_server server_url must not contain backslashes")
    if "?" in server_url or "#" in server_url:
        raise ValueError("checkout_server server_url must not contain query or fragment")
    parsed = urllib_parse.urlsplit(server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("checkout_server server_url must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("checkout_server server_url must not contain credentials")
    if parsed.netloc.endswith(":"):
        raise ValueError("checkout_server server_url has an invalid port")
    if parsed.query or parsed.fragment:
        raise ValueError("checkout_server server_url must not contain query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("checkout_server server_url must contain only an origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("checkout_server server_url has an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("checkout_server server_url has an invalid port")

    host = parsed.hostname
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in host):
        raise ValueError("checkout_server server_url has an invalid host")
    if ":" in host:
        # IPv6 literals must use the bracketed URL spelling.
        try:
            import ipaddress

            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise ValueError("checkout_server server_url has an invalid host") from exc
        host_part = f"[{host.lower()}]"
    else:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise ValueError("checkout_server server_url has an invalid host")
        if ".." in host or any(
            not label or label.startswith("-") or label.endswith("-")
            for label in host.split(".")
        ):
            raise ValueError("checkout_server server_url has an invalid host")
        host_part = host.lower()
    return f"{parsed.scheme}://{host_part}{f':{port}' if port is not None else ''}"


def validate_output_field(
    value: object,
    *,
    field: str,
    allow_empty: bool = False,
    reject_slash: bool = False,
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ValueError(f"checkout_server output descriptor has invalid {field}")
    if any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\"
        for char in value
    ):
        raise ValueError(f"checkout_server output descriptor has unsafe {field}")
    if is_native_absolute_path(value) or value.startswith("~") or has_url_scheme(value):
        raise ValueError(f"checkout_server output descriptor has unsafe {field}")
    if ".." in value or (reject_slash and "/" in value):
        raise ValueError(f"checkout_server output descriptor has unsafe {field}")
    if not allow_empty and not value:
        raise ValueError(f"checkout_server output descriptor has invalid {field}")
    return value


def validate_output_descriptor(descriptor: object) -> tuple[str, str, str]:
    if not isinstance(descriptor, dict):
        raise ValueError("checkout_server output descriptor must be an object")
    if not {"filename", "subfolder", "type"}.issubset(descriptor):
        raise ValueError("checkout_server output descriptor has an invalid schema")
    # Comfy/VHS may attach routing metadata to a descriptor, but accepting an
    # arbitrary key set would make the custody contract ambiguous.  Keep the
    # required routing fields plus the documented metadata emitted by Comfy;
    # reject unknown fields so malformed or attacker-controlled descriptors do
    # not silently pass through settlement.
    allowed_metadata = {
        "filename",
        "subfolder",
        "type",
        "format",
        "frame_rate",
        "workflow",
        "fullpath",
        "prompt_id",
        "node_id",
    }
    if set(descriptor).difference(allowed_metadata):
        raise ValueError("checkout_server output descriptor has an invalid schema")
    filename = validate_output_field(
        descriptor.get("filename"), field="filename", reject_slash=True
    )
    subfolder = validate_output_field(
        descriptor.get("subfolder"), field="subfolder", allow_empty=True
    )
    output_type = validate_output_field(descriptor.get("type"), field="type")
    if output_type not in {"output", "temp"}:
        raise ValueError("checkout_server output descriptor has invalid type")
    return filename, subfolder, output_type


def extract_output_descriptors(value: object) -> list[object]:
    "Find Comfy output descriptors nested in node/output containers."
    if isinstance(value, list):
        descriptors: list[object] = []
        for item in value:
            descriptors.extend(extract_output_descriptors(item))
        return descriptors
    if isinstance(value, dict):
        # Comfy's output descriptors include the required routing fields plus
        # optional metadata (format, frame rate, workflow, fullpath, ...).
        # Treat the required subset as a descriptor instead of requiring an
        # exact three-key shape, otherwise valid VHS video outputs disappear
        # at settlement time.
        if {"filename", "subfolder", "type"}.issubset(value):
            return [value]
        descriptors = []
        for item in value.values():
            descriptors.extend(extract_output_descriptors(item))
        return descriptors
    return []


class ComfyModelMissing(ValueError):
    """Required roles have zero or multiple exact target filename matches."""

    code = "COMFY_MODEL_MISSING"

    def __init__(self, model: str, mode: str, failures: dict[str, list[str]],
                 alternatives: list[str]):
        self.model = model
        self.mode = mode
        self.failures = failures
        self.alternatives = alternatives
        super().__init__(
            f"{self.code}: {model}/{mode}: unresolved roles {failures}; "
            f"compatible catalog entries: {', '.join(alternatives) or 'none'}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "model": self.model, "mode": self.mode,
                "roles": self.failures, "alternatives": self.alternatives}


def _resolve_roles(spec: Any, object_info: dict[str, Any]) -> tuple[dict[str, str], dict[str, list[str]]]:
    resolved, failures = {}, {}
    for role, declaration in spec.model_roles.items():
        node = object_info.get(declaration.node_class, {})
        inputs = node.get("input", {}) if isinstance(node, dict) else {}
        choices = None
        for group in ("required", "optional"):
            fields = inputs.get(group, {}) if isinstance(inputs, dict) else {}
            if isinstance(fields, dict) and declaration.input in fields:
                field = fields[declaration.input]
                choices = field[0] if isinstance(field, (tuple, list)) and field else None
                break
        names = choices if isinstance(choices, (tuple, list)) else ()
        matches = [name for name in declaration.names if name in names]
        if len(matches) == 1:
            resolved[role] = matches[0]
        else:
            failures[role] = matches
    return resolved, failures


def _capture_model_resolution(client: ComfyTargetClient, registry: Any,
                              model_id: str, mode: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Keep the observed inventory internal; a returned plan grants no authority."""
    entry, mode_spec = registry.get_by_mode(model_id, mode)
    spec = mode_spec.backends.get("local")
    object_info = client.object_info()
    resolved, failures = (_resolve_roles(spec, object_info) if spec and spec.model_roles
                          else ({}, {"catalog_roles": []}))
    if failures:
        alternatives = []
        for candidate in registry.list_all():
            candidate_mode = candidate.modes.get(mode)
            candidate_spec = candidate_mode.backends.get("local") if candidate_mode else None
            if (candidate.id != model_id and candidate.modality == entry.modality
                    and candidate_spec and candidate_spec.model_roles
                    and set(mode_spec.requires).issubset(candidate_mode.supports)
                    and set(candidate_mode.requires).issubset(mode_spec.requires)
                    and not _resolve_roles(candidate_spec, object_info)[1]):
                from .vibecomfy import _catalog_alternative_is_executable

                candidate_roles = _resolve_roles(candidate_spec, object_info)[0]
                if _catalog_alternative_is_executable(
                    candidate_spec, candidate_roles, object_info, client.http_origin,
                ):
                    alternatives.append(candidate.id)
        raise ComfyModelMissing(model_id, mode, failures, sorted(alternatives))
    plan = {"model": model_id, "mode": mode, "template": spec.template,
            "target": {"url": client.target.url, "kind": client.target.kind},
            "roles": resolved, "model_bytes": "unverified_target_inventory"}
    return plan, object_info


def resolve_comfy_models(client: ComfyTargetClient, registry: Any,
                         model_id: str, mode: str) -> dict[str, Any]:
    """Resolve exact declared roles without submitting or approving a prompt."""
    return _capture_model_resolution(client, registry, model_id, mode)[0]


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    from astrid.core.model_catalog.registry import ModelRegistry

    parser = argparse.ArgumentParser(description="Resolve catalog roles on an attached Comfy target")
    parser.add_argument("model")
    parser.add_argument("mode")
    parser.add_argument("--comfy-url", required=True)
    parser.add_argument("--dry-run", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        from .vibecomfy import VibeComfyBackend

        plan = VibeComfyBackend(target_client=ComfyTargetClient(
            ComfyTarget(args.comfy_url, "attached"),
        )).generate(
            ModelRegistry.load_default().get(args.model), args.mode, {}, Path("."), dry_run=True,
        )
    except ComfyModelMissing as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
