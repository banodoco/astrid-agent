"""Invocation-scoped staging for neutral-runtime managed media objects.

The generic host supplies verified object bytes in an attempt-local mapping.
This module verifies their digests, copies them to a disposable render stage,
and exposes only that stage over loopback HTTP. URLs, project paths, and CAS
locators are rejected before any bytes are opened.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

from astrid.core.foundation.hash import validate_digest

@dataclass
class MaterializedAsset:
    """One registry asset resolved for the lifetime of a render invocation."""

    key: str
    kind: str
    metadata: dict[str, Any]
    local_path: Path | None = None
    local_url: str | None = None


class AssetURLResolver(Protocol):
    """Resolve one staged asset to the URL visible to a renderer."""

    def local_url(self, staged_path: str | Path) -> str:
        """Return a renderer-local URL for a verified staged file."""


def _contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


_MEDIA_SUFFIXES = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}


def _media_suffix(entry: Mapping[str, Any], value: str | Path | bytes) -> str:
    """Return a safe extension so the loopback server emits a useful MIME type."""

    declared = entry.get("mime_type") or entry.get("media_type") or entry.get("content_type")
    if isinstance(declared, str):
        suffix = _MEDIA_SUFFIXES.get(declared.split(";", 1)[0].strip().lower())
        if suffix is not None:
            return suffix
    kind = entry.get("type")
    if isinstance(kind, str):
        suffix = _MEDIA_SUFFIXES.get(kind.strip().lower())
        if suffix is not None:
            return suffix

    payload: bytes
    if isinstance(value, bytes):
        payload = value[:16]
    else:
        try:
            with Path(value).open("rb") as source:
                payload = source.read(16)
        except OSError:
            payload = b""
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ".webp"
    if len(payload) >= 8 and payload[4:8] == b"ftyp":
        return ".mp4"
    return ""


def _safe_staging_name(key: str, object_id: str, index: int, suffix: str = "") -> str:
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", object_id).strip("._-") or "object"
    candidate = candidate[-120:]
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"{index:04d}-{digest}-{candidate}{suffix}"


class AssetMaterializer:
    """Resolve and stage the assets needed by one render invocation.

    Construction eagerly materializes the registry so the instance is useful
    both as a context manager and with an explicit :meth:`close` call.  Any
    construction failure removes the partially-created staging directory.
    """

    def __init__(
        self,
        registry_path: str | Path,
        *,
        materialized_objects: Mapping[str, str | Path | bytes] | None = None,
        materialized_root: str | Path | None = None,
        staging_parent: str | Path | None = None,
        allow_derived_files: bool = False,
        reuse_materialized_paths: bool = False,
    ) -> None:
        requested_registry = Path(registry_path).expanduser()
        if not requested_registry.exists():
            raise FileNotFoundError("hype.assets.json missing — did you run cut.py first?")
        self.registry_path = requested_registry.resolve(strict=True)

        self.materialized_objects = dict(materialized_objects or {})
        self.allow_derived_files = bool(allow_derived_files)
        self.reuse_materialized_paths = bool(reuse_materialized_paths)
        path_values = [value for value in self.materialized_objects.values() if not isinstance(value, bytes)]
        if path_values and materialized_root is None:
            raise ValueError("materialized_root is required for path-backed runtime objects")
        self.materialized_root = (
            Path(materialized_root).expanduser().resolve(strict=True)
            if materialized_root is not None
            else None
        )
        if self.materialized_root is not None and not self.materialized_root.is_dir():
            raise NotADirectoryError(f"materialized object root is not a directory: {self.materialized_root}")

        parent: Path | None = None
        if staging_parent is not None:
            parent = Path(staging_parent).expanduser().resolve(strict=False)
            parent.mkdir(parents=True, exist_ok=True)
        self.staging_dir = Path(
            tempfile.mkdtemp(
                prefix="astrid-render-assets-",
                dir=None if parent is None else str(parent),
            )
        ).resolve()
        self._closed = False
        self.registry: dict[str, Any] = {}
        self.assets: dict[str, MaterializedAsset] = {}
        try:
            self._materialize()
        except BaseException:
            self.close()
            raise

    @property
    def needs_server(self) -> bool:
        return any(asset.local_path is not None for asset in self.assets.values())

    def _materialize_managed_object(
        self,
        key: str,
        reference: str,
        digest: str,
        value: str | Path | bytes,
        index: int,
        suffix: str = "",
    ) -> Path:
        """Verify and stage bytes already materialized by the runtime host."""

        try:
            digest = validate_digest(digest.removeprefix("sha256:"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Asset {key!r} has an invalid managed digest") from exc
        destination = self.staging_dir / _safe_staging_name(key, reference, index, suffix)
        if isinstance(value, bytes):
            payload = value
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError(f"Asset {key!r} managed object failed integrity check")
            destination.write_bytes(payload)
            return destination
        source = Path(value).expanduser()
        if source.is_symlink():
            raise ValueError(f"Asset {key!r} runtime materialization may not be a symlink")
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise FileNotFoundError(f"Asset {key!r} runtime materialization is unavailable") from exc
        if self.materialized_root is None or not _contained(resolved, self.materialized_root) or not resolved.is_file():
            raise ValueError(f"Asset {key!r} is outside the runtime materialized-object root")
        payload = resolved.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError(f"Asset {key!r} managed object failed integrity check")
        if self.reuse_materialized_paths:
            # The runtime task adapter has already made this an invocation-owned,
            # inode-isolated copy. Reusing it avoids a second full copy into the
            # renderer stage, which matters for large timelines under the task
            # scratch envelope. Integrity and root checks above still apply.
            return resolved
        destination.write_bytes(payload)
        return destination

    def _materialize(self) -> None:
        try:
            loaded = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("managed asset registry is not valid JSON") from exc
        if not isinstance(loaded, Mapping):
            raise ValueError("managed asset registry must be an object")
        self.registry = copy.deepcopy(loaded)
        assets = loaded.get("assets", {})
        if not isinstance(assets, Mapping):
            raise ValueError("asset registry assets must be an object")
        for index, (key, entry) in enumerate(assets.items()):
            if not isinstance(entry, Mapping):
                raise ValueError(f"Asset {key!r} must be an object")
            forbidden = [
                field
                for field in (
                    "url", "sourceUrl", "remoteUrl", "thumbnailUrl", "thumbnail_url",
                    "path", "source_path", "locator", "realm",
                )
                if field in entry
            ]
            if "file" in entry and not self.allow_derived_files:
                forbidden.append("file")
            if forbidden:
                raise ValueError(
                    f"Asset {key!r} contains retired media locator field(s): {', '.join(forbidden)}"
                )
            object_id = entry.get("object_id") or entry.get("media_id")
            if not isinstance(object_id, str) or not object_id.strip():
                raise ValueError(f"Asset {key!r} requires a runtime-managed object_id")
            raw_digest = entry.get("digest") or entry.get("content_sha256") or entry.get("sha256") or entry.get("hash")
            if not isinstance(raw_digest, str) or not raw_digest.strip():
                raise ValueError(f"Asset {key!r} requires a runtime-managed digest")
            if self.allow_derived_files and isinstance(entry.get("file"), str):
                source = Path(entry["file"]).expanduser()
                if self.materialized_root is None:
                    raise ValueError("materialized_root is required for derived asset files")
                try:
                    resolved = source.resolve(strict=True)
                except OSError as exc:
                    raise FileNotFoundError(f"Asset {key!r} derived file is unavailable") from exc
                if source.is_symlink() or not _contained(resolved, self.materialized_root) or not resolved.is_file():
                    raise ValueError(f"Asset {key!r} derived file is outside the materialized root")
                staged_path = self._materialize_managed_object(
                    key, object_id, raw_digest, resolved, index, _media_suffix(entry, resolved)
                )
                self.assets[key] = MaterializedAsset(
                    key=key, kind="managed", metadata=copy.deepcopy(dict(entry)), local_path=staged_path
                )
                continue
            candidates = (object_id, raw_digest, raw_digest.removeprefix("sha256:"), f"sha256:{raw_digest.removeprefix('sha256:')}")
            materialized = next((self.materialized_objects[candidate] for candidate in candidates if candidate in self.materialized_objects), None)
            if materialized is None:
                raise FileNotFoundError(f"Asset {key!r} has no runtime materialized object")
            staged_path = self._materialize_managed_object(
                key, object_id, raw_digest, materialized, index, _media_suffix(entry, materialized)
            )
            self.assets[key] = MaterializedAsset(
                key=key,
                kind="managed",
                metadata=copy.deepcopy(dict(entry)),
                local_path=staged_path,
            )

    def resolved_registry(
        self,
        server: "AssetURLResolver | None" = None,
    ) -> dict[str, Any]:
        """Return a cloned render registry with attempt-local file URLs."""

        if self._closed:
            raise RuntimeError("Asset materializer is closed")
        resolved = copy.deepcopy(self.registry)
        for key, entry in resolved["assets"].items():
            asset = self.assets[key]
            if server is None:
                raise RuntimeError("a running InvocationAssetServer is required for managed assets")
            if asset.local_path is None:  # pragma: no cover - invariant guard
                raise RuntimeError(f"Materialized asset {key!r} has no staged path")
            asset.local_url = server.local_url(asset.local_path)
            entry["file"] = asset.local_url
        return resolved

    @property
    def serving_root(self) -> Path:
        """Directory containing every path returned by ``resolved_registry``."""

        if self._closed:
            raise RuntimeError("Asset materializer is closed")
        if not self.reuse_materialized_paths:
            return self.staging_dir
        if self.materialized_root is None:
            raise RuntimeError("materialized_root is required when reusing materialized paths")
        for asset in self.assets.values():
            if asset.local_path is None or not _contained(asset.local_path, self.materialized_root):
                # Byte-backed or derived assets still live in the disposable
                # stage; mixed registries must not widen the serving root.
                return self.staging_dir
        return self.materialized_root

    def close(self) -> None:
        if self._closed:
            return
        try:
            shutil.rmtree(self.staging_dir)
        except FileNotFoundError:
            pass
        self._closed = True

    def __enter__(self) -> "AssetMaterializer":
        if self._closed:
            raise RuntimeError("Asset materializer is closed")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
REMOTION_BROWSER_ORIGIN = "http://localhost:3000"


class RangeHTTPRequestHandler(SimpleHTTPRequestHandler):
    """File-only HTTP handler with single-range byte serving."""

    def __init__(
        self,
        *args: Any,
        allowed_origin: str = REMOTION_BROWSER_ORIGIN,
        **kwargs: Any,
    ) -> None:
        self.allowed_origin = allowed_origin
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _send_renderer_cors_headers(self) -> None:
        """Allow only the server-owned Remotion browser to read an asset.

        The renderer's Chromium page is served from one exact, invocation-owned
        local origin.  Do not reflect arbitrary ``Origin`` values: this server
        exposes invocation-scoped media and is intentionally not a general
        CORS endpoint.
        """

        origin = self.headers.get("Origin")
        if origin == self.allowed_origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
        """Answer browser preflight without widening the asset origin policy."""

        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._send_renderer_cors_headers()
        if self.headers.get("Origin") == self.allowed_origin:
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
            self.send_header(
                "Access-Control-Expose-Headers",
                "Accept-Ranges, Content-Length, Content-Range",
            )
        self.end_headers()

    def _send_416(self, size: int) -> None:
        self.send_response(416, "Range Not Satisfiable")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self._send_renderer_cors_headers()
        self.end_headers()

    def _resolved_file(self) -> Path | None:
        root = Path(self.directory).resolve(strict=True)
        translated = Path(self.translate_path(self.path))
        try:
            resolved = translated.resolve(strict=True)
        except (FileNotFoundError, OSError):
            self.send_error(404, "File not found")
            return None
        if not _contained(resolved, root) or not resolved.is_file():
            self.send_error(404, "File not found")
            return None
        return resolved

    @staticmethod
    def _range_bounds(header: str, size: int) -> tuple[int, int] | None:
        match = _RANGE_RE.fullmatch(header)
        if match is None or size <= 0:
            return None
        start_text, end_text = match.groups()
        if not start_text and not end_text:
            return None
        try:
            if not start_text:
                suffix_length = int(end_text)
                if suffix_length <= 0:
                    return None
                return max(0, size - suffix_length), size - 1
            start = int(start_text)
            if start >= size:
                return None
            end = size - 1 if not end_text else min(int(end_text), size - 1)
        except ValueError:
            return None
        if end < start:
            return None
        return start, end

    @staticmethod
    def _content_type(path: Path) -> str:
        """Infer media MIME types for immutable extensionless runtime objects."""

        guessed, _encoding = mimetypes.guess_type(str(path))
        if guessed and guessed != "application/octet-stream":
            return guessed
        try:
            with path.open("rb") as source:
                payload = source.read(16)
        except OSError:
            payload = b""
        if payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if payload.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if payload.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
            return "image/webp"
        if len(payload) >= 8 and payload[4:8] == b"ftyp":
            return "video/mp4"
        if payload.startswith(b"\x1a\x45\xdf\xa3"):
            return "video/webm"
        return guessed or "application/octet-stream"

    def send_head(self):
        path = self._resolved_file()
        if path is None:
            return None
        try:
            source = path.open("rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        try:
            size = os.fstat(source.fileno()).st_size
        except OSError:
            source.close()
            self.send_error(500, "File stat failed")
            return None

        range_header = self.headers.get("Range")
        if range_header is not None:
            bounds = self._range_bounds(range_header, size)
            if bounds is None:
                source.close()
                self._send_416(size)
                return None
            start, end = bounds
            length = end - start + 1
            source.seek(start)
            self._range_limit = length
            self.send_response(206)
            self.send_header("Content-Type", self._content_type(path))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self._send_renderer_cors_headers()
            self.end_headers()
            return source

        self._range_limit = None
        self.send_response(200)
        self.send_header("Content-Type", self._content_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size))
        self._send_renderer_cors_headers()
        self.end_headers()
        return source

    def copyfile(self, source: Any, outputfile: Any) -> None:
        limit = getattr(self, "_range_limit", None)
        if limit is None:
            try:
                super().copyfile(source, outputfile)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        remaining = limit
        try:
            while remaining > 0:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


class InvocationAssetServer:
    """Serve exactly one materializer staging directory over loopback HTTP."""

    host = "127.0.0.1"
    bind_port = 0

    def __init__(
        self,
        staging_dir: str | Path,
        *,
        allowed_origin: str = REMOTION_BROWSER_ORIGIN,
    ) -> None:
        self.staging_dir = Path(staging_dir).resolve(strict=True)
        if not self.staging_dir.is_dir():
            raise NotADirectoryError(f"Asset staging path is not a directory: {self.staging_dir}")
        self._server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._closed = False
        self.allowed_origin = allowed_origin

    @property
    def port(self) -> int:
        if self._server is None:
            return 0
        return int(self._server.server_address[1])

    @property
    def server_address(self) -> tuple[str, int]:
        return (self.host, self.port)

    @property
    def base_url(self) -> str:
        if self._server is None or self.port == 0:
            raise RuntimeError("Invocation asset server has not been started")
        return f"http://{self.host}:{self.port}"

    def start(self) -> "InvocationAssetServer":
        if self._closed:
            raise RuntimeError("Invocation asset server is closed")
        if self._server is not None:
            return self
        handler = partial(
            RangeHTTPRequestHandler,
            directory=str(self.staging_dir),
            allowed_origin=self.allowed_origin,
        )
        # Remotion opens several browser workers and can request many timeline
        # assets concurrently. TCPServer's default listen backlog is only five,
        # and a large timeline can exceed the previous 128-entry safeguard
        # before handler threads are created. The kernel then resets a pending
        # connection, which Node surfaces as `socket hang up`. Keep the server
        # reusable and let its request threads die with the invocation so a
        # canceled render cannot hold the executor open during cleanup.
        ThreadingHTTPServer.allow_reuse_address = True
        ThreadingHTTPServer.daemon_threads = True
        ThreadingHTTPServer.request_queue_size = max(
            int(getattr(ThreadingHTTPServer, "request_queue_size", 5)), 1024
        )
        server = ThreadingHTTPServer((self.host, self.bind_port), handler)
        try:
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"astrid-asset-server-{server.server_port}",
                daemon=True,
            )
            self._server = server
            self.thread = thread
            thread.start()
        except BaseException:
            server.server_close()
            self._server = None
            self.thread = None
            raise
        return self

    def local_url(self, staged_path: str | Path) -> str:
        if self._server is None:
            raise RuntimeError("Invocation asset server has not been started")
        path = Path(staged_path).resolve(strict=True)
        if not path.is_file() or not _contained(path, self.staging_dir):
            raise ValueError(f"Asset path is outside the invocation staging directory: {path}")
        relative = path.relative_to(self.staging_dir).as_posix()
        return f"{self.base_url}/{urllib.parse.quote(relative, safe='/')}"

    def local_urls(
        self,
        assets: Mapping[str, MaterializedAsset],
    ) -> dict[str, str]:
        return {
            key: self.local_url(asset.local_path)
            for key, asset in assets.items()
            if asset.local_path is not None
        }

    def close(self) -> None:
        if self._closed:
            return
        server = self._server
        thread = self.thread
        if server is None:
            self._closed = True
            return
        cleanup_error: BaseException | None = None
        try:
            if thread is not None and thread.is_alive():
                server.shutdown()
        except BaseException as exc:  # noqa: BLE001 - cleanup must preserve all primary failures
            cleanup_error = exc
        try:
            server.server_close()
        except BaseException as exc:  # noqa: BLE001 - cleanup must preserve all primary failures
            if cleanup_error is None:
                cleanup_error = exc
        try:
            if thread is not None and thread is not threading.current_thread():
                thread.join()
        except BaseException as exc:  # noqa: BLE001 - cleanup must preserve all primary failures
            if cleanup_error is None:
                cleanup_error = exc
        if cleanup_error is not None:
            raise cleanup_error
        self._server = None
        self._closed = True

    def __enter__(self) -> "InvocationAssetServer":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


__all__ = [
    "AssetMaterializer",
    "InvocationAssetServer",
    "MaterializedAsset",
    "RangeHTTPRequestHandler",
    "REMOTION_BROWSER_ORIGIN",
]
