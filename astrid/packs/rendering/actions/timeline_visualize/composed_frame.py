"""Bounded composed-frame capture, cache, and worker lifecycle.

The visualizer plans cards before it asks the renderer for pixels.  This module
keeps that seam small: exact renders still use ffmpeg extraction, while a
capture authority asks the trusted Remotion backend for only the planned
frames.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import Timer
from typing import Any

RENDERER_IDENTITY = "astrid.remotion.frame-capture.v1|backend=1.0.0|remotion=4.0.509|fonts=local-bundle-v1"
DEFAULT_IDLE_SECONDS = 45.0
DEFAULT_MAX_ENTRIES = 256
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
_EPHEMERAL_ATTEMPT_PATH = re.compile(r"astrid-attempt-[^/]+(?=/)")


def warm_owner_idle_seconds() -> float:
    """Retain five minutes only with an explicit owner memory allocation."""
    try:
        budget = int(os.environ.get("ASTRID_REMOTION_WARM_MEMORY_BUDGET_BYTES", "0"))
        limit = 300.0 if budget >= 512 * 1024**2 else DEFAULT_IDLE_SECONDS
        configured = float(os.environ.get("ASTRID_TIMELINE_FRAME_IDLE_SECONDS", str(limit)))
        return min(limit, max(0.0, configured)) if math.isfinite(configured) else limit
    except ValueError:
        return DEFAULT_IDLE_SECONDS


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
            # Extracting/replacing package files can preserve size and mtime.
            # ctime and inode identity force a fresh content digest in that case.
            signature.update(
                f"{label}|{path}|{stat.st_dev}|{stat.st_ino}|{stat.st_size}|"
                f"{stat.st_mtime_ns}|{stat.st_ctime_ns}\0".encode("utf-8")
            )
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
        ("rendering-elements", checkout / "astrid/packs/rendering/rendering/elements"),
        ("capture-provider", checkout / "astrid/packs/rendering/actions/timeline_visualize/composed_frame.py"),
        ("element-catalog", checkout / "astrid/packs/rendering/rendering/elements/catalog.ts"),
        ("rendering-core", checkout / "astrid/core/rendering"),
    ]
    if source_root:
        paths.append(("explicit-source-root", Path(source_root)))
    if project.resolve(strict=False).parent != checkout.resolve(strict=False):
        # the bundle imports the elements beside the project, not this checkout's
        paths.extend((f"project-{label}", root) for label, root in element_source_roots(project.parent))
    paths.append(("remotion-project-source", project / "src"))
    paths.append(("remotion-project-config", project / "remotion.config.ts"))
    paths.append(("fonts", project / "public/fonts"))
    paths.append(("package-lock", project / "package-lock.json"))
    # "-source" labels admit the node_modules files (see _digest_candidates).
    # Hash the installed package bytes, including generated registries and any
    # compiled/exported entrypoints, rather than its version or src alone.
    paths.append((
        "timeline-composition-package-source",
        project / "node_modules/@banodoco/timeline-composition",
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
    """Small, bounded, content-addressed PNG cache."""

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

    def path_for(self, key: str) -> Path:
        if not key or any(ch not in "0123456789abcdef" for ch in key):
            raise ValueError("invalid frame cache key")
        return self.root / f"{key}.png"

    def read(self, key: str) -> bytes | None:
        path = self.path_for(key)
        with self._lock:
            try:
                data = path.read_bytes()
            except OSError:
                return None
            try:
                os.utime(path, None)  # another process may have pruned it just now
            except OSError:
                pass
            return data

    def write(self, key: str, data: bytes) -> Path:
        path = self.path_for(key)
        # Unique per process and per call: two processes writing one key never share a temp file.
        temporary = self.root / f"{key}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        with self._lock:
            try:
                temporary.write_bytes(data)
                os.replace(temporary, path)
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
                    continue
                kept += 1
                total += size


class FrameCaptureWorker:
    """Serialize capture batches and retire the worker after idle time.

    The callable is intentionally injected. Production uses one Remotion CLI
    image-sequence session with one lazily started Chromium browser; tests can
    supply a deterministic renderer without starting Chromium.
    """

    def __init__(
        self,
        renderer: Callable[[Sequence[int], Path, Sequence[int] | None], Mapping[int, Path]],
        *,
        idle_seconds: float | None = None,
    ) -> None:
        self.renderer = renderer
        self.idle_seconds = warm_owner_idle_seconds() if idle_seconds is None else max(0.0, float(idle_seconds))
        self._lock = threading.RLock()
        self._timer: Timer | None = None
        self._timer_generation = 0
        self._closed = False
        self.started = False
        self.capture_batches = 0

    def _expire(self, generation: int | None = None) -> None:
        with self._lock:
            if generation is not None and generation != self._timer_generation:
                return
            self.started = False
            self._timer = None
            _close_renderer(self.renderer)

    def _arm(self) -> None:
        if self.idle_seconds <= 0:
            self._timer_generation += 1
            self._expire(self._timer_generation)
            return
        if self._timer is not None:
            self._timer.cancel()
        self._timer_generation += 1
        generation = self._timer_generation
        self._timer = Timer(self.idle_seconds, lambda: self._expire(generation))
        self._timer.daemon = True
        self._timer.start()

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
            self._arm()
            return result

    def close(self, *, force: bool = False) -> None:
        with self._lock:
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._timer_generation += 1
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
    ) -> None:
        from astrid.packs.rendering.rendering.renderers.remotion.run import (
            PersistentRemotionFrameSession,
        )

        self.timeline_path = timeline_path
        self.assets_path = assets_path
        self.project_dir = project_dir
        self.materialized_root = materialized_root
        self.materialized_objects = materialized_objects
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
            frame_session_identity=renderer_environment_identity(),
        )

    def close(self, *, force: bool = False) -> None:
        self.session.close(force=force)


class _SharedRemotionOwner:
    """Own the one process-local browser and serialize all capture requests."""

    def __init__(self) -> None:
        from astrid.packs.rendering.rendering.renderers.remotion.run import PersistentRemotionFrameSession

        self.session = PersistentRemotionFrameSession()
        self.lock = threading.RLock()
        self.references = 0
        self.timer: Timer | None = None
        self.timer_generation = 0
        self.closed = False
        self.idle_seconds = warm_owner_idle_seconds()

    def acquire(self, context: Mapping[str, Any]) -> "_SharedRemotionHandle":
        with self.lock:
            if self.closed:
                raise RuntimeError("shared Remotion owner is closed")
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
            self.timer_generation += 1
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
                frame_session_identity=renderer_environment_identity(),
            )

    def release(self, *, force: bool = False) -> None:
        with self.lock:
            self.references = max(0, self.references - 1)
            if self.references or self.closed:
                return
            if force or self.idle_seconds <= 0:
                self._close_locked()
                return
            if self.timer is not None:
                self.timer.cancel()
            self.timer_generation += 1
            generation = self.timer_generation
            self.timer = Timer(self.idle_seconds, lambda: self._expire(generation))
            self.timer.daemon = True
            self.timer.start()

    def _expire(self, generation: int | None = None) -> None:
        with self.lock:
            if generation is not None and generation != self.timer_generation:
                return
            self.timer = None
            if not self.references:
                self._close_locked()

    def _close_locked(self) -> None:
        self.closed = True
        self.timer_generation += 1
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
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
        cache: FrameCaptureCache | None = None,
        renderer: Callable[[Sequence[int], Path, Sequence[int] | None], Mapping[int, Path]] | None = None,
        idle_seconds: float | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.timeline_path = Path(timeline_path)
        self.assets_path = Path(assets_path)
        self.project_dir = Path(project_dir)
        self.materialized_root = Path(materialized_root) if materialized_root else None
        self.materialized_objects = materialized_objects
        self.cache = cache or FrameCaptureCache()
        if renderer is None:
            renderer = _acquire_shared_remotion_handle(
                context={
                    "timeline_path": self.timeline_path,
                    "assets_path": self.assets_path,
                    "project_dir": self.project_dir,
                    "materialized_root": self.materialized_root,
                    "materialized_objects": self.materialized_objects,
                }
            )

        self.worker = FrameCaptureWorker(renderer, idle_seconds=idle_seconds)
        self.wants_bounds = snapshot_wants_bounds(snapshot)

    def close(self, *, force: bool = False) -> None:
        self.worker.close(force=force)

    def capture(
        self,
        cards: Sequence[Mapping[str, Any]],
        out_root: Path,
        resolution: Sequence[int] | None = None,
    ) -> dict[str, Any]:
        out_root = Path(out_root)
        frames_dir = out_root / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        misses: list[tuple[int, str, Path]] = []
        cached = 0
        for card in cards:
            frame = int(card["frame"])
            key = frame_cache_key(self.snapshot, frame, resolution)
            destination = out_root / str(card["image"])
            # The cache stores PNGs only; a cached PNG carries no bounds, so bounds captures render every frame.
            data = None if self.wants_bounds else self.cache.read(key)
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
                produced = self.worker.capture(
                    [frame for frame, _key, _destination in misses], temporary, resolution
                )
                notes = _renderer_notes(self.worker.renderer)
                for frame, key, destination in misses:
                    source = Path(produced[frame])
                    data = source.read_bytes()
                    self.cache.write(key, data)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                    if self.wants_bounds:
                        bounds_source = source.with_name(source.stem + ".bounds.json")
                        if bounds_source.is_file():
                            shutil.copyfile(bounds_source, destination.with_name(destination.stem + ".bounds.json"))
                    fresh += 1
        except BaseException as operation_error:
            # Cleanup is still mandatory, but its failure must not replace the
            # original native request error (including its structured reply).
            try:
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
                "idle_seconds": self.worker.idle_seconds,
                "serialized": True,
                "owner": "process_shared" if isinstance(self.worker.renderer, _SharedRemotionHandle) else "provider_local",
            },
        }
