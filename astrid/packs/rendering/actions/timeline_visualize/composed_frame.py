"""Bounded composed-frame capture, cache, and worker lifecycle.

The visualizer plans cards before it asks the renderer for pixels.  This module
keeps that seam small: exact renders still use ffmpeg extraction, while a
capture authority asks the trusted Remotion backend for only the planned
frames.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
import threading
import uuid
import zlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

RENDERER_IDENTITY = "astrid.remotion.frame-capture.v1|backend=1.0.0|remotion=4.0.509|fonts=local-bundle-v1"
DEFAULT_MAX_ENTRIES = 256
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
_EPHEMERAL_ATTEMPT_PATH = re.compile(r"astrid-attempt-[^/]+(?=/)")
_SCENE_CHUNK = b"asTr"


class CaptureSceneLabels:
    """One capture's observer, supplied with frozen inputs by the source owner.

    This does not admit sources. The owner must bind final Root props, the
    canonical render basis and exact projected IDs/addresses to its retained
    revision/candidate before passing ``observe`` to ``on_scene_props``. Call
    ``retain`` only with that successful batch's fresh output, never PNG-cache
    entries. Cached rows require the cache owner's retained fresh-write binding.
    """

    def __init__(self, *, source_identity: str, props: Mapping[str, Any],
                 basis: Mapping[str, Any], addresses: Mapping[str, str]) -> None:
        if not source_identity:
            raise ValueError("capture source identity required")
        self._source_identity = source_identity
        self._props = copy.deepcopy(dict(props))
        self._basis = copy.deepcopy(dict(basis))
        self._addresses = dict(addresses)
        self._answers: dict[int, dict[str, Any]] = {}
        self._frames: tuple[int, ...] | None = None
        self._retained = False

    def observe(self, props: Mapping[str, Any], frames: Sequence[int]) -> None:
        """Query the public host once, inside the existing capture lifetime."""
        from astrid.core.rendering.scene_answers import (
            SceneQueryUnavailable,
            host_frames_at,
        )

        if self._frames is not None:
            self._retained = False
            self._answers.clear()
            raise SceneQueryUnavailable("scene observer already used for a capture")
        self._frames = tuple(frames)
        if (props != self._props or not frames
                or any(type(frame) is not int or frame < 0 for frame in frames)
                or tuple(sorted(set(frames))) != self._frames):
            raise SceneQueryUnavailable("capture props or selected frames mismatch")
        clips = props.get("timeline", {}).get("clips", [])
        ids = [clip["id"] for clip in clips]
        if len(set(ids)) != len(ids) or set(ids) != set(self._addresses):
            raise SceneQueryUnavailable("exact projected clip address mapping required")
        answers = host_frames_at(props, self._basis, frames)
        if ([answer.get("frame") for answer in answers] != list(frames)
                or any(answer.get("width") != self._basis["width"]
                       or answer.get("height") != self._basis["height"] for answer in answers)
                or any(layer["clipId"] not in self._addresses
                       for answer in answers for layer in answer["layers"])):
            raise SceneQueryUnavailable("capture scene response mismatch")
        self._answers = copy.deepcopy(dict(zip(frames, answers)))

    def retain(self, produced: Mapping[int, Path]) -> None:
        """Publish answers only after the same entire fresh capture succeeds."""
        from astrid.core.rendering.scene_answers import SceneQueryUnavailable

        if (self._retained or not self._answers or set(produced) != set(self._answers)
                or any(not Path(path).is_file() for path in produced.values())):
            self._retained = False
            self._answers.clear()
            raise SceneQueryUnavailable("scene requires the complete successful fresh capture")
        self._retained = True

    def frame(self, frame: int, *, source_identity: str) -> dict[str, Any]:
        from astrid.core.rendering.scene_answers import SceneQueryUnavailable

        if not self._retained or source_identity != self._source_identity or frame not in self._answers:
            raise SceneQueryUnavailable("scene unavailable for this capture source/frame")
        return copy.deepcopy(self._answers[frame])

    def _frame_facts(self, frame: int, *, source_identity: str) -> dict[str, Any]:
        """Return retained public facts for an internal visualize consumer."""
        answer = self.frame(frame, source_identity=source_identity)
        return {
            "answer": answer,
            "basis": {"width": self._basis["width"], "height": self._basis["height"],
                      "fps": self._basis["fps"]},
            "addresses": dict(self._addresses),
        }

    def labels(self, frame: int, *, source_identity: str):
        from .overlays.labels import labels_for_scene

        answer = self.frame(frame, source_identity=source_identity)
        return labels_for_scene(answer, self._addresses), {key: answer[key] for key in ("width", "height")}

    def _cache_payload(self, frame: int) -> dict[str, Any]:
        from astrid.core.rendering.scene_answers import SceneQueryUnavailable

        if not self._props:
            raise SceneQueryUnavailable("cached scene rows cannot mint a fresh payload")
        answer = self.frame(frame, source_identity=self._source_identity)
        return {"source_identity": self._source_identity, "basis": self._basis,
                "addresses": self._addresses, "answer": answer,
                "root_source": hashlib.sha256(json.dumps(
                    self._props, sort_keys=True, separators=(",", ":"), allow_nan=False,
                ).encode()).hexdigest()}

    @classmethod
    def _from_cache(cls, payload, *, source_identity, basis, addresses, frame):
        """Restore only after the cache verifies its own successful fresh write."""
        answer = payload["answer"]
        if (payload["source_identity"] != source_identity or payload["basis"] != basis
                or payload["addresses"] != addresses
                or not re.fullmatch(r"[0-9a-f]{64}", payload["root_source"])
                or type(answer["frame"]) is not int or answer["frame"] != frame
                or any(answer[key] != basis[key] for key in ("width", "height"))
                or any(layer["clipId"] not in addresses for layer in answer["layers"])):
            raise ValueError("cached scene source/frame mismatch")
        capture = cls(source_identity=source_identity, props={}, basis=basis, addresses=addresses)
        capture._frames = (frame,)
        capture._answers = {frame: copy.deepcopy(answer)}
        capture._retained = True
        capture.labels(frame, source_identity=source_identity)
        return capture


def _split_scene_entry(data: bytes) -> tuple[bytes, bytes | None]:
    """An optional private ancillary PNG chunk keeps pixels and rows atomic."""
    if _SCENE_CHUNK not in data:
        return data, None
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid scene PNG")
    offset = 8
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        end = offset + 12 + length
        kind = data[offset + 4:offset + 8]
        if end > len(data):
            if kind == _SCENE_CHUNK:
                raise ValueError("truncated scene PNG chunk")
            break
        if kind == _SCENE_CHUNK:
            chunk = data[offset + 8:end - 4]
            if (end != len(data) - 12 or data[end + 4:end + 8] != b"IEND"
                    or zlib.crc32(kind + chunk) != int.from_bytes(data[end - 4:end], "big")):
                raise ValueError("invalid scene PNG chunk")
            return data[:offset] + data[end:], chunk
        offset = end
    return data, None


def _close_renderer(renderer: object, *, force: bool = False) -> None:
    close = getattr(renderer, "close", None)
    if not callable(close):
        return
    try:
        close(force=force)
    except TypeError:
        # Keep the injected test seam compatible with the historical zero-arg
        # close() contract.
        close()


def _digest_candidates(paths: Sequence[tuple[str, Path]]) -> list[tuple[str, Path, Path]]:
    found: list[tuple[str, Path, Path]] = []
    seen: set[Path] = set()
    for label, root in paths:
        root = root.expanduser()
        candidates = [root] if root.is_file() else sorted(root.rglob("*")) if root.is_dir() else []
        for path in candidates:
            if (
                not path.is_file()
                or path in seen
                or any(part in {".git", "__pycache__"} for part in path.parts)
                or (
                    "node_modules" in path.parts
                    and path.name != "effects.generated.ts"
                    and not label.endswith("-source")
                )
            ):
                continue
            seen.add(path)
            found.append((label, root, path))
    return found


# {stat signature: content digest}: hashing every renderer source per frame cost
# 30-120 ms a call; a stat walk tells whether anything changed since the last digest.
_DIGEST_MEMO: dict[str, str] = {}


def _digest_paths(paths: Sequence[tuple[str, Path]]) -> str:
    """Digest admitted renderer inputs that can change rendered pixels."""
    candidates = _digest_candidates(paths)
    signature = hashlib.sha256()
    for label, root, path in candidates:
        try:
            stat = path.stat()
            signature.update(f"{label}|{path}|{stat.st_size}|{stat.st_mtime_ns}\0".encode("utf-8"))
        except OSError:
            signature.update(f"{label}|{path}|missing\0".encode("utf-8"))
    memo_key = signature.hexdigest()
    if memo_key in _DIGEST_MEMO:
        return _DIGEST_MEMO[memo_key]
    digest = hashlib.sha256()
    for label, root, path in candidates:
        try:
            relative = path.relative_to(root) if root.is_dir() else Path(path.name)
            digest.update(f"{label}/{relative.as_posix()}\0".encode("utf-8"))
            digest.update(path.read_bytes())
        except OSError:
            digest.update(f"{label}/{path.name}\0missing".encode("utf-8"))
    if len(_DIGEST_MEMO) > 16:
        _DIGEST_MEMO.clear()
    _DIGEST_MEMO[memo_key] = digest.hexdigest()
    return _DIGEST_MEMO[memo_key]


def element_source_roots(checkout: Path) -> list[tuple[str, Path]]:
    """Every pack's elements: a component or motion change must change the frame cache key."""
    packs = Path(checkout) / "astrid" / "packs"
    roots = sorted({*packs.glob("*/elements"), *packs.glob("*/rendering/elements")})
    return [(f"elements:{root.relative_to(packs).as_posix()}", root) for root in roots if root.is_dir()]


def _checkout_root() -> Path:
    return Path(__file__).resolve().parents[5]


def renderer_environment_identity() -> str:
    """Return a content fingerprint for renderer, generated effects, and fonts."""
    checkout = _checkout_root()
    project_dir = os.environ.get("ASTRID_REMOTION_PROJECT_DIR")
    # The composition project is always digested: without ASTRID_REMOTION_PROJECT_DIR the bundle uses <checkout>/remotion.
    project = Path(project_dir) if project_dir else checkout / "remotion"
    source_root = os.environ.get("ASTRID_RENDERER_SOURCE_ROOT")
    paths: list[tuple[str, Path]] = [
        # every pack's elements, not only local/ and rendering/: an astrid_motion
        # component change used to return cached frames ("capture 0 s")
        *element_source_roots(checkout),
        ("local-elements", checkout / "astrid/packs/local/rendering/elements"),
        ("renderer-backend", checkout / "astrid/packs/rendering/rendering/renderers/remotion/run.py"),
        ("renderer-root-preparation", checkout / "astrid/packs/rendering/rendering/renderers/remotion/root_preparation.py"),
        ("rendering-elements", checkout / "astrid/packs/rendering/rendering/elements"),
        ("capture-provider", checkout / "astrid/packs/rendering/actions/timeline_visualize/composed_frame.py"),
        ("element-catalog", checkout / "astrid/packs/rendering/rendering/elements/catalog.ts"),
        ("rendering-core", checkout / "astrid/core/rendering"),
        ("remotion-source", checkout / "remotion/src"),
        ("remotion-config", checkout / "remotion/remotion.config.ts"),
        ("fonts", checkout / "remotion/public/fonts"),
    ]
    if source_root:
        paths.append(("explicit-source-root", Path(source_root)))
    if project.resolve(strict=False).parent != checkout.resolve(strict=False):
        # the bundle imports the elements beside the project, not this checkout's
        paths.extend((f"project-{label}", root) for label, root in element_source_roots(project.parent))
    paths.append(("remotion-project-source", project / "src"))
    paths.append(("remotion-project-config", project / "remotion.config.ts"))
    paths.append(("package-lock", project / "package-lock.json"))
    paths.append((
        "generated-effects",
        project / "src/effects.generated.ts",
    ))
    # "-source" labels admit the node_modules files (see _digest_candidates).
    paths.append((
        "timeline-composition-source",
        project / "node_modules/@banodoco/timeline-composition/dist",
    ))
    paths.append((
        "timeline-composition-package-source",
        project / "node_modules/@banodoco/timeline-composition/package.json",
    ))
    parts = {
        "renderer": RENDERER_IDENTITY,
        "project_dir": project_dir,
        "node": os.environ.get("ASTRID_NODE_EXECUTABLE"),
        "theme": os.environ.get("ASTRID_ACTIVE_THEME"),
        "font_bundle": os.environ.get("ASTRID_FONT_BUNDLE_ID"),
        "sources": _digest_paths(paths),
    }
    return json.dumps(parts, sort_keys=True, separators=(",", ":"))


def frame_cache_key(
    snapshot: Mapping[str, Any],
    frame: int,
    resolution: Sequence[int] | None,
    *,
    renderer_identity: str = RENDERER_IDENTITY,
) -> str:
    """Return a content key for one exact composition/frame request."""

    def canonicalize(value: Any) -> Any:
        """Remove attempt-directory identity from persisted managed-object paths."""
        if isinstance(value, Mapping):
            return {str(key): canonicalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [canonicalize(item) for item in value]
        if isinstance(value, tuple):
            return [canonicalize(item) for item in value]
        if isinstance(value, str):
            return _EPHEMERAL_ATTEMPT_PATH.sub("astrid-attempt-<ephemeral>", value)
        return value

    identity = {
        "composition": canonicalize(snapshot),
        "frame": int(frame),
        "resolution": list(resolution) if resolution is not None else None,
        "renderer": renderer_identity,
        "environment": renderer_environment_identity(),
    }
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_cache_root() -> Path:
    configured = os.environ.get("ASTRID_TIMELINE_FRAME_CACHE")
    return Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "astrid-timeline-frame-cache"


class FrameCaptureCache:
    """Small, bounded PNG cache with optional fresh-callback scene rows."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.root = (root or default_cache_root()).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1, int(max_bytes))
        self._lock = threading.RLock()
        # Disk metadata cannot prove that rows came from a callback. Trust only
        # complete entries minted by this owner; another owner must render fresh.
        self._scene_digests: dict[str, str] = {}

    def path_for(self, key: str) -> Path:
        if not key or any(ch not in "0123456789abcdef" for ch in key):
            raise ValueError("invalid frame cache key")
        return self.root / f"{key}.png"

    def read(self, key: str) -> bytes | None:
        entry = self.read_entry(key)
        return entry[0] if entry is not None else None

    def read_entry(self, key: str) -> tuple[bytes, dict | None] | None:
        path = self.path_for(key)
        with self._lock:
            try:
                data = path.read_bytes()
                png, chunk = _split_scene_entry(data)
                trusted = self._scene_digests.get(key)
                payload = None
                if chunk is not None:
                    if trusted != hashlib.sha256(data).hexdigest():
                        return None
                    payload = json.loads(chunk)
                    if (payload["key"] != key
                            or payload["png_sha256"] != hashlib.sha256(png).hexdigest()):
                        return None
                elif trusted is not None:
                    return None
            except (OSError, ValueError, TypeError, KeyError):
                return None
            try:
                os.utime(path, None)  # another process may have pruned it just now
            except OSError:
                pass
            return png, payload

    def write(self, key: str, data: bytes, *, scene: CaptureSceneLabels | None = None,
              frame: int | None = None) -> Path:
        path = self.path_for(key)
        if scene is not None:
            if frame is None or not data.startswith(b"\x89PNG\r\n\x1a\n") or data[-8:-4] != b"IEND":
                raise ValueError("scene cache requires a PNG")
            payload = {"key": key, "png_sha256": hashlib.sha256(data).hexdigest(),
                       "scene": scene._cache_payload(frame)}
            chunk = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            packet = (struct.pack(">I", len(chunk)) + _SCENE_CHUNK + chunk
                      + struct.pack(">I", zlib.crc32(_SCENE_CHUNK + chunk)))
            data = data[:-12] + packet + data[-12:]
        # Unique per process and per call: two processes writing one key never share a temp file.
        temporary = self.root / f"{key}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        with self._lock:
            self._scene_digests.pop(key, None)
            try:
                temporary.write_bytes(data)
                os.replace(temporary, path)
                if scene is not None:
                    self._scene_digests[key] = hashlib.sha256(data).hexdigest()
            finally:
                temporary.unlink(missing_ok=True)
            self.prune()
        return path

    def prune(self) -> None:
        """Keep the newest entries within the caps. Files another process removes meanwhile are skipped."""
        with self._lock:
            entries: list[tuple[float, int, Path]] = []
            for path in self.root.glob("*.png"):
                try:
                    stat = path.stat()
                except OSError:
                    continue  # vanished between glob and stat
                entries.append((stat.st_mtime, stat.st_size, path))
            entries.sort(key=lambda entry: entry[0], reverse=True)
            total = 0
            kept = 0
            for _mtime, size, path in entries:
                if kept >= self.max_entries or total + size > self.max_bytes:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self._scene_digests.pop(path.stem, None)
                    continue
                kept += 1
                total += size
            self._scene_digests = {key: digest for key, digest in self._scene_digests.items()
                                   if self.path_for(key).is_file()}


class FrameCaptureWorker:
    """Serialize capture batches through one renderer.

    The callable is intentionally injected. Production leases the pack host's
    Remotion frame owner, which the host keeps warm and idles out through its
    warm hold; tests can supply a deterministic renderer without starting
    Chromium.
    """

    def __init__(
        self,
        renderer: Callable[[Sequence[int], Path, Sequence[int] | None], Mapping[int, Path]],
    ) -> None:
        self.renderer = renderer
        self._lock = threading.RLock()
        self._closed = False
        self.started = False
        self.capture_batches = 0

    def capture(
        self,
        frames: Sequence[int],
        output_dir: Path,
        resolution: Sequence[int] | None,
    ) -> Mapping[int, Path]:
        requested = tuple(sorted(set(int(frame) for frame in frames)))
        if not requested:
            return {}
        with self._lock:
            if self._closed:
                raise RuntimeError("frame capture worker is closed")
            self.started = True
            self.capture_batches += 1
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            try:
                result = self.renderer(requested, output_dir, resolution)
            except BaseException as operation_error:
                # Retain the renderer failure when cleanup independently fails.
                try:
                    self.close(force=True)
                except BaseException as cleanup_error:
                    raise operation_error from cleanup_error
                raise
            return result

    def close(self, *, force: bool = False) -> None:
        with self._lock:
            self._closed = True
            self.started = False
            _close_renderer(self.renderer, force=force)


class _PersistentRemotionRenderer:
    """Adapt the backend's persistent browser session to the worker seam."""

    def __init__(
        self,
        *,
        timeline_path: Path,
        assets_path: Path,
        project_dir: Path,
        materialized_root: Path | None,
        materialized_objects: Mapping[str, str] | None,
        theme_path: Path | None = None,
        on_scene_props: Callable | None = None,
    ) -> None:
        from astrid.packs.rendering.rendering.renderers.remotion.run import (
            PersistentRemotionFrameSession,
        )

        self.timeline_path = timeline_path
        self.assets_path = assets_path
        self.project_dir = project_dir
        self.materialized_root = materialized_root
        self.materialized_objects = materialized_objects
        self.theme_path = theme_path
        self.on_scene_props = on_scene_props
        self.session = PersistentRemotionFrameSession()

    def __call__(
        self,
        frames: Sequence[int],
        output_dir: Path,
        resolution: Sequence[int] | None,
    ) -> Mapping[int, Path]:
        from astrid.packs.rendering.rendering.renderers.remotion.run import capture_remotion_frames

        return capture_remotion_frames(
            self.timeline_path,
            self.assets_path,
            output_dir,
            frames,
            project_dir=self.project_dir,
            materialized_root=self.materialized_root,
            materialized_objects=self.materialized_objects,
            frame_resolution=tuple(resolution) if resolution is not None else None,
            frame_session=self.session,
            theme_path=self.theme_path,
            on_scene_props=self.on_scene_props,
            frame_session_identity=renderer_environment_identity(),
        )

    def close(self, *, force: bool = False) -> None:
        self.session.close(force=force)


class _SharedRemotionOwner:
    """Share this process's host renderer lease and serialize all capture requests."""

    def __init__(self) -> None:
        from astrid.packs.rendering.rendering.renderers.remotion.run import (
            PersistentRemotionFrameSession,
        )

        self.session = PersistentRemotionFrameSession()
        self.lock = threading.RLock()
        self.references = 0
        self.closed = False

    def acquire(self, context: Mapping[str, Any]) -> "_SharedRemotionHandle":
        with self.lock:
            if self.closed:
                raise RuntimeError("shared Remotion owner is closed")
            self.references += 1
            return _SharedRemotionHandle(self, context)

    def render(
        self,
        context: Mapping[str, Any],
        frames: Sequence[int],
        output_dir: Path,
        resolution: Sequence[int] | None,
    ) -> Mapping[int, Path]:
        from astrid.packs.rendering.rendering.renderers.remotion.run import capture_remotion_frames

        with self.lock:
            if self.closed:
                raise RuntimeError("shared Remotion owner is closed")
            return capture_remotion_frames(
                Path(context["timeline_path"]),
                Path(context["assets_path"]),
                output_dir,
                frames,
                project_dir=Path(context["project_dir"]),
                materialized_root=context.get("materialized_root"),
                materialized_objects=context.get("materialized_objects"),
                frame_resolution=tuple(resolution) if resolution is not None else None,
                frame_session=self.session,
                theme_path=context.get("theme_path"),
                on_scene_props=context.get("on_scene_props"),
                frame_session_identity=renderer_environment_identity(),
            )

    def release(self, *, force: bool = False) -> None:
        with self.lock:
            self.references = max(0, self.references - 1)
            # The host keeps the frame owner warm and idles it out; only a
            # failed capture force-closes it from here.
            if self.references or self.closed or not force:
                return
            self.closed = True
            self.session.close(force=True)


class _SharedRemotionHandle:
    def __init__(self, owner: _SharedRemotionOwner, context: Mapping[str, Any]) -> None:
        self.owner = owner
        self.context = dict(context)
        self.closed = False

    def __call__(
        self,
        frames: Sequence[int],
        output_dir: Path,
        resolution: Sequence[int] | None,
    ) -> Mapping[int, Path]:
        return self.owner.render(self.context, frames, output_dir, resolution)

    def close(self, *, force: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        self.owner.release(force=force)


_SHARED_REMOTION_LOCK = threading.RLock()
_SHARED_REMOTION_OWNER: _SharedRemotionOwner | None = None


def _acquire_shared_remotion_handle(*, context: Mapping[str, Any]) -> _SharedRemotionHandle:
    global _SHARED_REMOTION_OWNER
    with _SHARED_REMOTION_LOCK:
        if _SHARED_REMOTION_OWNER is None or _SHARED_REMOTION_OWNER.closed:
            _SHARED_REMOTION_OWNER = _SharedRemotionOwner()
        return _SHARED_REMOTION_OWNER.acquire(context)


def _renderer_notes(renderer: Any) -> list[str]:
    """Take the frame owner's notes for findings (e.g. a cold-browser retry)."""
    session = getattr(getattr(renderer, "owner", None), "session", None) or getattr(renderer, "session", None)
    notes = getattr(session, "notes", None)
    if not isinstance(notes, list) or not notes:
        return []
    taken = [str(note) for note in notes]
    notes.clear()
    return taken


def materialized_timeline_matches(timeline_path: Path, config: Mapping[str, Any]) -> bool:
    """Return whether the materialized timeline file is the retained capture config.

    Both sides are compared at the JSON boundary, the form that is written to disk:
    ``Timeline.load`` stores a clip's ``from`` trim under ``from_`` in memory while
    ``from_config`` keeps ``from``, so comparing in-memory configs rejects valid files.
    """
    from astrid.core.timeline import Timeline

    return (Timeline.load(timeline_path).to_json_data()
            == Timeline.from_config(config).to_json_data())


def visualized_duration_frames(config: Mapping[str, Any], fps: float) -> int:
    """Return the composition frame count the visualizer reviews for a saved config."""
    from astrid.core.timeline.duration import (
        timeline_duration_frames,
        timeline_occurrence_extent_frames,
    )

    return max(timeline_duration_frames(config, fps), timeline_occurrence_extent_frames(config, fps))


def snapshot_wants_bounds(snapshot: Mapping[str, Any]) -> bool:
    """True when the snapshot's timeline config sets the top-level ``app.astrid_bounds`` flag.

    The fast lane keeps its config under ``fast_lane.config``; other snapshots carry it at ``config``.
    """
    config = snapshot.get("config")
    if config is None and isinstance(snapshot.get("fast_lane"), Mapping):
        config = snapshot["fast_lane"].get("config")
    app = config.get("app") if isinstance(config, Mapping) else None
    return isinstance(app, Mapping) and app.get("astrid_bounds") is True


class RemotionFrameProvider:
    """Provide PNG frames from an admitted, immutable capture snapshot."""

    def __init__(
        self,
        snapshot: Mapping[str, Any],
        *,
        timeline_path: Path,
        assets_path: Path,
        project_dir: Path,
        materialized_root: Path | None = None,
        materialized_objects: Mapping[str, str] | None = None,
        theme_path: Path | None = None,
        cache: FrameCaptureCache | None = None,
        renderer: Callable[[Sequence[int], Path, Sequence[int] | None], Mapping[int, Path]] | None = None,
    ) -> None:
        self.snapshot = copy.deepcopy(dict(snapshot))
        self.timeline_path = Path(timeline_path)
        self.assets_path = Path(assets_path)
        self.project_dir = Path(project_dir)
        self.materialized_root = Path(materialized_root) if materialized_root else None
        self.materialized_objects = materialized_objects
        self.theme_path = Path(theme_path) if theme_path else None
        self.scene_capture: CaptureSceneLabels | None = None
        self.fresh_scene_frames: set[int] = set()
        self._cached_scenes: dict[int, CaptureSceneLabels] = {}
        self._pending_scene: CaptureSceneLabels | None = None
        self._fresh_requested: tuple[int, ...] = ()
        self._scene_clips = self.snapshot.get("metadata", {}).get("scene_clips")
        self.source_identity = self.snapshot.get("metadata", {}).get("capture_identity", "")
        if self._scene_clips is not None:
            from astrid.packs.rendering.shared.rendering_backend import (
                _canonical_profile,
                _load_registry_mapping,
                _resolved_theme_for_render,
                _serialize_timeline,
                _timeline_alpha,
            )

            authority = self.snapshot["metadata"]["capture_authority"]
            retained = {"authority": authority, "config": self.snapshot["config"], "registry": self.snapshot["registry"]}
            identity = "sha256:" + hashlib.sha256(json.dumps(
                retained, sort_keys=True, separators=(",", ":"), default=str,
            ).encode()).hexdigest()
            if identity != self.source_identity or not authority.get("parent_revision_id"):
                raise ValueError("capture source identity mismatch")
            if not materialized_timeline_matches(self.timeline_path, self.snapshot["config"]):
                raise ValueError("materialized timeline differs from retained capture source")
            from astrid.core.timeline.astrid_ns import read_app

            clips = self.snapshot["config"]["clips"]
            if len({clip["id"] for clip in clips}) != len(clips) or set(self._scene_clips) != {clip["id"] for clip in clips}:
                raise ValueError("capture projected clip mapping mismatch")
            for clip in clips:
                source = clip.get("app", {}).get("astrid_shot_composition", {})
                app = read_app(clip)
                cut, layer = app.get("cut"), app.get("layer")
                source_id = source.get("source_clip_id", clip["id"])
                if self._scene_clips[clip["id"]] != {
                    "address": f"{cut}.{layer}" if cut and layer else source_id,
                    "source_clip_id": source_id, "occurrence_id": source.get("occurrence_id"),
                }:
                    raise ValueError("capture projected source/occurrence mapping mismatch")
            # The host adds a per-entry ``file`` locator when it materializes
            # managed media. It is a host-side detail, not part of the retained
            # source identity, so it is left out of this comparison.
            self._registry = _load_registry_mapping(self.assets_path)
            for entry in self._registry.get("assets", {}).values():
                if isinstance(entry, dict):
                    entry.pop("file", None)
            if self._registry != self.snapshot["registry"]:
                raise ValueError("materialized registry differs from retained capture source")
            self.profile = _canonical_profile(self.timeline_path, self._registry, self.theme_path)
            self._theme = _resolved_theme_for_render(self.timeline_path, self.theme_path)
            self._timeline = _serialize_timeline(self.timeline_path, default_theme=self._theme["id"])
            if _timeline_alpha(self._timeline):
                self._theme.setdefault("visual", {}).setdefault("color", {})["bg"] = "transparent"
            self._source_bytes = {path: path.read_bytes() for path in (
                self.timeline_path, self.assets_path, *((self.theme_path,) if self.theme_path else ()),
            )}
            # Planning and the public query share Root's whole-canvas selection,
            # including rational fps and partial-override field defaults.
            fps = self.profile.fps_rational[0] / self.profile.fps_rational[1]
            snapshot["fps_rational"] = list(self.profile.fps_rational)
            snapshot["duration_frames"] = visualized_duration_frames(self.snapshot["config"], fps)
            self.snapshot["fps_rational"] = snapshot["fps_rational"]
            self.snapshot["duration_frames"] = snapshot["duration_frames"]
            self.snapshot["resolved_theme"] = self._theme
        # A caller that retains an existing cache owner may opt into the
        # source-bound rows.  The normal provider creates a fresh owner and
        # keeps the established PNG-only cache format so a later provider can
        # reuse its bytes without pretending to trust persisted rows.
        self._cache_scene_payloads = cache is not None
        self.cache = cache if cache is not None else FrameCaptureCache()
        if renderer is None:
            renderer = _acquire_shared_remotion_handle(
                context={
                    "timeline_path": self.timeline_path,
                    "assets_path": self.assets_path,
                    "project_dir": self.project_dir,
                    "materialized_root": self.materialized_root,
                    "materialized_objects": self.materialized_objects,
                    "theme_path": self.theme_path,
                    "on_scene_props": self._observe_scene if self._scene_clips is not None else None,
                }
            )

        self.worker = FrameCaptureWorker(renderer)
        self.wants_bounds = snapshot_wants_bounds(snapshot)

    def _observe_scene(self, props: Mapping[str, Any], frames: Sequence[int]) -> None:
        from astrid.core.rendering.assets import managed_asset_digest
        from astrid.core.rendering.profile import resolve_render_profile
        from astrid.core.rendering.scene_answers import SceneQueryUnavailable

        if self._pending_scene is not None or tuple(frames) != self._fresh_requested:
            raise SceneQueryUnavailable("capture callback frames mismatch or already observed")
        if any(path.read_bytes() != data for path, data in self._source_bytes.items()):
            raise SceneQueryUnavailable("frozen capture source changed during capture")
        if props.get("theme") != self._theme:
            raise SceneQueryUnavailable("capture resolved theme mismatch")
        actual = copy.deepcopy(props.get("timeline"))
        if not isinstance(actual, dict) or not isinstance(props.get("assets"), Mapping):
            raise SceneQueryUnavailable("capture timeline or assets missing")
        expected_timeline = copy.deepcopy(self._timeline)
        # Staging adds only this reserved parameter. Check its bytes against the
        # declared pack assets before comparing the entire serialized timeline.
        for clip, expected_clip in zip(actual.get("clips", []), expected_timeline["clips"]):
            params = clip.get("params", {})
            self._validate_staged_assets(clip, props["assets"])
            if "__astridAssets" in params:
                expected_clip.setdefault("params", {})["__astridAssets"] = params["__astridAssets"]
        if actual != expected_timeline:
            raise SceneQueryUnavailable("capture serialized timeline mismatch")
        registry = copy.deepcopy(props.get("assets"))
        if not isinstance(registry, dict) or set(registry.get("assets", {})) != set(self._registry["assets"]):
            raise SceneQueryUnavailable("capture materialized assets mismatch")
        for key, entry in registry["assets"].items():
            url = urlsplit(entry.pop("file", ""))
            if (self.materialized_root is None or url.scheme != "http" or url.hostname != "127.0.0.1"
                    or not url.port or url.query or url.fragment):
                raise SceneQueryUnavailable("capture asset is not invocation materialized")
            path = (self.materialized_root / unquote(url.path).lstrip("/")).resolve()
            if not path.is_relative_to(self.materialized_root.resolve()) or not path.is_file():
                raise SceneQueryUnavailable("capture asset outside materialized source")
            expected = self._registry["assets"][key]
            try:
                digest = managed_asset_digest(expected)
            except ValueError:
                digest = None
            if not isinstance(digest, str):
                raise SceneQueryUnavailable("capture materialized asset has no media record digest")
            if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != "sha256:" + digest.removeprefix("sha256:"):
                raise SceneQueryUnavailable("capture materialized asset digest mismatch")
        if registry != self._registry:
            raise SceneQueryUnavailable("capture asset metadata mismatch")
        profile = resolve_render_profile(props["timeline"], props["assets"], theme=props["theme"])
        if (profile.width, profile.height, profile.fps_rational) != (
                self.profile.width, self.profile.height, self.profile.fps_rational):
            raise SceneQueryUnavailable("capture canonical basis mismatch")
        capture = CaptureSceneLabels(
            source_identity=self.source_identity, props=props,
            basis={"width": self.profile.width, "height": self.profile.height,
                   "fps": self.profile.fps_rational[0] / self.profile.fps_rational[1]},
            addresses={key: value["address"] for key, value in self._scene_clips.items()},
        )
        self._pending_scene = capture
        capture.observe(props, frames)

    def _validate_staged_assets(self, clip: Mapping[str, Any], registry: Mapping[str, Any]) -> None:
        from astrid.core.rendering.scene_answers import SceneQueryUnavailable
        from astrid.packs.rendering.rendering.renderers.remotion.run import (
            REPO_ROOT,
            _effect_id_for_clip,
            _effect_registry_for_assets,
            _resolve_end_spanning_card_assets,
            _source_pack_id,
            load_default_registry,
        )

        catalog = load_default_registry(project_root=REPO_ROOT)
        effects, aliases = _effect_registry_for_assets(self.theme_path, registry=catalog)
        ref = clip.get("elementRef")
        effect_id = ref.get("id") if isinstance(ref, Mapping) and ref.get("kind") == "effect" else _effect_id_for_clip(clip, effects, aliases)
        staged = clip.get("params", {}).get("__astridAssets", {})
        if effect_id is None:
            if staged:
                raise SceneQueryUnavailable("capture staged assets on a non-effect clip")
            return
        element = catalog.get("effects", effect_id, pack_id=ref.get("packId")) if isinstance(ref, Mapping) and "packId" in ref else catalog.get("effects", effect_id)
        cards = _resolve_end_spanning_card_assets(clip, registry) if (_source_pack_id(element), element.id) == ("local", "end-spanning-layer") else {}
        if not isinstance(staged, Mapping) or set(staged) != {asset.name for asset in element.assets} | set(cards):
            raise SceneQueryUnavailable("capture staged element assets mismatch")
        for asset in element.assets:
            if asset.name in cards:
                continue
            path = (self.project_dir / "public" / staged[asset.name]).resolve()
            if (not path.is_relative_to((self.project_dir / "public" / "astrid-effects").resolve())
                    or path.read_bytes() != (element.root / asset.path).read_bytes()):
                raise SceneQueryUnavailable("capture staged element bytes mismatch")
        if any(staged[key] != value for key, value in cards.items()):
            raise SceneQueryUnavailable("capture staged card assets mismatch")

    def scene_for_frame(self, frame: int) -> CaptureSceneLabels | None:
        return self.scene_capture if frame in self.fresh_scene_frames else self._cached_scenes.get(frame)

    def _cached_scene(self, payload, frame: int) -> CaptureSceneLabels:
        if self._scene_clips is None or any(path.read_bytes() != data for path, data in self._source_bytes.items()):
            raise ValueError("cached scene requires the frozen capture source")
        return CaptureSceneLabels._from_cache(
            payload["scene"], source_identity=self.source_identity, frame=frame,
            basis={"width": self.profile.width, "height": self.profile.height,
                   "fps": self.profile.fps_rational[0] / self.profile.fps_rational[1]},
            addresses={key: value["address"] for key, value in self._scene_clips.items()},
        )

    def close(self, *, force: bool = False) -> None:
        if force:
            self.scene_capture = self._pending_scene = None
            self.fresh_scene_frames.clear()
            self._cached_scenes.clear()
        self.worker.close(force=force)

    def capture(
        self,
        cards: Sequence[Mapping[str, Any]],
        out_root: Path,
        resolution: Sequence[int] | None = None,
    ) -> dict[str, Any]:
        self.scene_capture = self._pending_scene = None
        self.fresh_scene_frames.clear()
        self._cached_scenes.clear()
        out_root = Path(out_root)
        frames_dir = out_root / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        misses: list[tuple[int, str, Path]] = []
        cached = 0
        for card in cards:
            frame = int(card["frame"])
            key = frame_cache_key(self.snapshot, frame, resolution)
            destination = out_root / str(card["image"])
            # Bounds sidecars still require a fresh capture. PNG-only entries
            # retain their separately qualified legacy label fallback.
            entry = None if self.wants_bounds else self.cache.read_entry(key)
            data = entry[0] if entry is not None else None
            if entry is not None and entry[1] is not None:
                try:
                    self._cached_scenes[frame] = self._cached_scene(entry[1], frame)
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    data = None
            if data is not None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                cached += 1
            else:
                misses.append((frame, key, destination))
        fresh = 0
        notes: list[str] = []
        temporary = None
        try:
            if misses:
                temporary = Path(tempfile.mkdtemp(prefix="astrid-frame-capture-", dir=out_root.parent))
                self._fresh_requested = tuple(sorted({frame for frame, _key, _destination in misses}))
                produced = self.worker.capture(
                    [frame for frame, _key, _destination in misses], temporary, resolution
                )
                notes = _renderer_notes(self.worker.renderer)
                if self._scene_clips is not None and self._pending_scene is None:
                    raise RuntimeError("capture did not observe final scene props")
                for frame, _key, destination in misses:
                    source = Path(produced[frame])
                    data = source.read_bytes()
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                    if self.wants_bounds:
                        bounds_source = source.with_name(source.stem + ".bounds.json")
                        if bounds_source.is_file():
                            shutil.copyfile(bounds_source, destination.with_name(destination.stem + ".bounds.json"))
                    fresh += 1
                if self._pending_scene is not None:
                    self._pending_scene.retain(produced)
                    self.scene_capture = self._pending_scene
                    self.fresh_scene_frames = set(produced)
                for frame, key, destination in misses:
                    self.cache.write(
                        key, destination.read_bytes(),
                        scene=self._pending_scene if self._cache_scene_payloads else None,
                        frame=frame,
                    )
        except BaseException as operation_error:
            # Cleanup is still mandatory, but its failure must not replace the
            # original native request error (including its structured reply).
            try:
                try:
                    for _frame, _key, destination in misses:
                        destination.unlink(missing_ok=True)
                        destination.with_name(destination.stem + ".bounds.json").unlink(missing_ok=True)
                finally:
                    self.close(force=True)
            except BaseException as cleanup_error:
                raise operation_error from cleanup_error
            raise
        finally:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)
        return {
            "evidence_source": "cache" if fresh == 0 else "fresh_capture" if cached == 0 else "mixed",
            "renderer": RENDERER_IDENTITY,
            "renderer_environment": renderer_environment_identity(),
            "findings": list(getattr(
                getattr(self.worker.renderer, "session", None)
                or getattr(getattr(self.worker.renderer, "owner", None), "session", None),
                "findings", [])),
            "cached_frames": cached,
            "fresh_frames": fresh,
            "requested_frames": [int(card["frame"]) for card in cards],
            "resolution": list(resolution) if resolution is not None else None,
            "cache_root": str(self.cache.root),
            "notes": notes,
            "worker": {
                "batches": self.worker.capture_batches,
                "serialized": True,
                "owner": "process_shared" if isinstance(self.worker.renderer, _SharedRemotionHandle) else "provider_local",
            },
        }
