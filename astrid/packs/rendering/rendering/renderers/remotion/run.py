#!/usr/bin/env python3
"""Remotion renderer and raw rendering-protocol v1 command adapter.

The command-line entry point is the leaf backend protocol used by the generic
renderer transport: it reads one request file and writes exactly one result or
structured error file.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import json
import os
import re
import select
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Any, Mapping, Sequence

from astrid.core import timeline
from astrid.core.audit import AuditContext
from astrid.core.element.registry import load_default_registry
from astrid.core.element.schema import ElementDefinition
from astrid.core.foundation.atomic_io import write_json_atomic
from astrid.core.foundation.paths import REPO_ROOT, WORKSPACE_ROOT
from astrid.core.rendering import assets as _rendering_assets
from astrid.core.rendering.artifacts import validate_render_result
from astrid.core.rendering.storage import remotion_frame_format
from astrid.core.rendering.assets import (
    AssetMaterializer,
    InvocationAssetServer,
)
from astrid.core.rendering.contracts import (
    SCHEMA_VERSION,
    AudioOwnership,
    RenderRequest,
    RenderResult,
    SupportReport,
    VideoArtifact,
)
from astrid.core.rendering.errors import (
    RendererException,
    make_renderer_error,
    raise_unsupported_error,
)
from astrid.core.rendering.publication import publish_render_result
from astrid.core.rendering.remotion_runtime import (
    TIMELINE_SCHEMA_PYTHONPATH_ENV,
    RemotionRuntimeTools,
    resolve_remotion_runtime_tools,
)
from astrid.core.rendering.storage import h264_encoder_bitrates
from astrid.packs.rendering.rendering.renderers.remotion.progress import RemotionProgressRelay
from astrid.core.subprocess_env import build_child_subprocess_env
from astrid.packs.rendering.shared import rendering_backend as _shared
from astrid.packs.rendering.shared.rendering_backend import (
    _alpha_output_name,
    _canonical_profile,
    _duration_frames,
    _input_path,
    _load_registry_mapping,
    _parse_min_free_gb,
    _profile_mismatches,
    _reject_unknown_config,
    _remotion_scaled_dimensions,
    _remotion_mux_profile,
    _review_output_profile,
    _render_provenance_payload,
    _resolved_theme_for_render,
    _serialize_timeline,
    _timeline_alpha,
)
from astrid.packs.rendering.rendering.renderers.remotion import lock as remotion_lock

# Release wheels intentionally exclude authoring scripts.  A provisioned
# server-owned Remotion bundle carries its generated registry outputs.
try:
    gen_effect_registry: ModuleType | None = importlib.import_module(
        "scripts.gen_effect_registry"
    )
except ModuleNotFoundError:  # pragma: no cover - exercised by wheel installs
    gen_effect_registry = None


_RangeHTTPRequestHandler = _rendering_assets.RangeHTTPRequestHandler


# Keep these helpers bound on this module because direct backend tests and
# protocol callers use them as the Remotion provenance/theme seam.
def _active_pack_order_for_provenance() -> list[dict[str, Any]]:
    """Pack order bound to THIS module's REPO_ROOT (patchable in tests)."""
    return _shared._active_pack_order_for_provenance(project_root=REPO_ROOT)


_active_theme_for_provenance = _shared._active_theme_for_provenance
_theme_for_props = _shared._theme_for_props


BACKEND_ID = "rendering.remotion"
BACKEND_VERSION = "1.0.0"

# The timeline schema keeps ``clipType`` open for forward-compatible values. These
# built-in spellings all mean an ordinary asset-backed media clip; Remotion's
# VisualClip/AudioTrack dispatch already handles them identically.  Keep them
# out of effect resolution so intuitive image/video/audio annotations do not
# become false "unregistered effect" failures.
_BUILTIN_MEDIA_CLIP_TYPES = frozenset({"media", "video", "image", "audio"})
DEFAULT_COMPOSITION_ID = "TimelineComposition"
_REGISTRY_STATE_PATH = ".astrid-registry-state.json"
_CONFIG_KEYS = frozenset(
    {"project_dir", "composition_id", "composition", "theme_path", "min_free_gb"}
)


@dataclass(frozen=True)
class _RenderSettings:
    project_dir: Path
    composition_id: str
    theme_path: Path | None
    min_free_gb: float | None


@dataclass(frozen=True)
class _ExecutionDetails:
    active_theme: dict[str, Any]
    registry_state: dict[str, Any]
    stage_summary: dict[str, Any]
    runtime: dict[str, str] = field(default_factory=dict)


class _FrameOwnerSubmissionRefused(RuntimeError):
    def __init__(self, message: str, returncode: int) -> None:
        super().__init__(message)
        self.returncode = returncode


class PersistentRemotionFrameSession:
    """Lease the process-independent Remotion frame owner for one batch.

    Managed executor children lease the original stdio Node child retained by
    their GenericPackHost across task boundaries. The inherited private channel
    carries no Runtime capability authority. Unmanaged legacy callers keep the
    existing socket owner; this does not adopt old handles.
    """

    def __init__(self) -> None:
        self._host_identity: str | None = None
        self._socket_path: Path | None = None
        self._identity: str | None = None
        self._owner_cleanup_error: BaseException | None = None
        self._owner_boundary_diagnostics: list[dict[str, Any]] = []
        self.findings: list[dict[str, Any]] = []
        self.diagnostics: dict[str, Any] | None = None  # ASTRID_FRAME_DIAGNOSTICS=1 probes of the last request

    def _record_owner_boundary(self, stage: str, **facts: Any) -> None:
        # Diagnostics describe observed boundaries, never cleanup authority.
        self._owner_boundary_diagnostics.append({"stage": stage, **facts})
        del self._owner_boundary_diagnostics[:-16]

    @staticmethod
    def _owner_paths(identity: str) -> tuple[Path, Path]:
        # macOS derives tempfile.gettempdir() from a long per-user sandbox
        # path. Unix-domain sockets have a small platform path limit, so keep
        # this owner directory deliberately short while still namespacing it.
        owner_root = Path("/tmp") / "astrid-rfo"
        owner_root.mkdir(parents=True, exist_ok=True)
        owner_root.chmod(0o700)
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        return owner_root / f"owner-{key}.sock", owner_root / f"owner-{key}.lock"

    @staticmethod
    def _owner_label(socket_path: Path) -> str:
        return f"astrid-rfo-{socket_path.stem.removeprefix('owner-')}"

    @classmethod
    def _remove_owner_job(cls, socket_path: Path) -> dict[str, Any]:
        """Remove the launchd job after a forced owner shutdown.

        ``launchctl submit`` keeps the label around after the submitted
        process exits on macOS.  Without removing the label here, launchd
        respawns an idle worker every time its bounded lease expires, turning
        a successful shutdown into a process leak.
        """
        if sys.platform != "darwin" or not Path("/bin/launchctl").is_file():
            return {"removal_attempted": False, "native_cleanup": "UNPROVED"}
        completed = subprocess.run(
            ["/bin/launchctl", "remove", cls._owner_label(socket_path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"removal_attempted": True, "returncode": completed.returncode,
                "native_cleanup": "UNPROVED"}

    @classmethod
    def _launch_owner(
        cls,
        *,
        socket_path: Path,
        project_dir: Path,
        node_executable: Path,
        helper: Path,
        child_environment: Mapping[str, str],
    ) -> None:
        """Start the owner outside GenericPackHost's per-task process tree."""
        worker_root = Path(child_environment["ASTRID_FRAME_WORKER_ROOT"])
        configured_idle = child_environment.get("ASTRID_TIMELINE_FRAME_IDLE_SECONDS")
        if sys.platform == "darwin" and Path("/bin/launchctl").is_file():
            label = cls._owner_label(socket_path)
            # launchctl submit creates a user launchd child rather than a
            # descendant of the managed executor. GenericPackHost can then
            # clean up the short-lived executor without killing this bounded
            # browser lease between managed visualization requests.
            subprocess.run(
                ["/bin/launchctl", "remove", label],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            env_assignments = [f"ASTRID_FRAME_WORKER_ROOT={worker_root}"]
            if configured_idle is not None:
                env_assignments.append(f"ASTRID_TIMELINE_FRAME_IDLE_SECONDS={configured_idle}")
            warm_budget = child_environment.get("ASTRID_REMOTION_WARM_MEMORY_BUDGET_BYTES")
            if warm_budget is not None:
                env_assignments.append(f"ASTRID_REMOTION_WARM_MEMORY_BUDGET_BYTES={warm_budget}")
            command = [
                "/bin/launchctl", "submit", "-l", label,
                "-o", "/dev/null", "-e", "/dev/null", "--", "/usr/bin/env",
                *env_assignments, "/bin/sh", "-c",
                'cd "$1" && exec "$2" "$3" --server "$4"',
                label, str(project_dir), str(node_executable), str(helper), str(socket_path),
            ]
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=10.0,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise _FrameOwnerSubmissionRefused(
                    f"could not launch Remotion frame owner{': ' + detail if detail else ''}", completed.returncode
                )
            return
        subprocess.Popen(
            [str(node_executable), str(helper), "--server", str(socket_path)],
            cwd=str(project_dir),
            env=build_child_subprocess_env(explicit_env=dict(child_environment)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @staticmethod
    def _connect(path: Path, *, timeout_seconds: float = 300.0) -> socket.socket:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(timeout_seconds)
        try:
            connection.connect(str(path))
        except BaseException:
            connection.close()
            raise
        return connection

    @classmethod
    def _wait_for_owner(cls, socket_path: Path, *, timeout_seconds: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                connection = cls._connect(socket_path, timeout_seconds=1.0)
            except OSError as exc:
                last_error = exc
                time.sleep(0.05)
                continue
            else:
                connection.close()
                return
        raise TimeoutError(f"Remotion frame owner did not become ready: {socket_path}") from last_error

    def _ensure_owner(
        self,
        *,
        socket_path: Path,
        project_dir: Path,
        node_executable: Path,
        environment: Mapping[str, str],
    ) -> None:
        if self._owner_cleanup_error is not None:
            raise RuntimeError("original Remotion frame owner cleanup is unresolved") from self._owner_cleanup_error
        if socket_path == self._socket_path:
            try:
                connection = self._connect(socket_path, timeout_seconds=1.0)
            except OSError as error:
                # Endpoint absence never proves that a retained original
                # owner/browser graph exited. Do not unlink and replace it.
                self._owner_cleanup_error = error
                raise RuntimeError("retained Remotion frame owner is unreachable; replacement refused") from error
            else:
                connection.close()
                self._record_owner_boundary("reused_bound_ready", socket=str(socket_path))
                return
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.parent.chmod(0o700)
        lock_path = socket_path.with_suffix(".lock")
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    connection = self._connect(socket_path, timeout_seconds=1.0)
                except OSError:
                    socket_path.unlink(missing_ok=True)
                    helper = project_dir / "src" / "astrid-frame-worker.mjs"
                    if not helper.is_file():
                        raise FileNotFoundError(f"Remotion frame worker is missing: {helper}")
                    child_environment = dict(environment)
                    child_environment["ASTRID_REMOTION_FRAME_WORKER"] = "1"
                    child_environment["ASTRID_FRAME_WORKER_ROOT"] = str(
                        Path(tempfile.gettempdir()) / "astrid-frame-workers"
                    )
                    from astrid.packs.rendering.actions.timeline_visualize.composed_frame import warm_owner_idle_seconds
                    child_environment["ASTRID_TIMELINE_FRAME_IDLE_SECONDS"] = str(warm_owner_idle_seconds())
                    warm_budget = os.environ.get("ASTRID_REMOTION_WARM_MEMORY_BUDGET_BYTES")
                    if warm_budget is not None:
                        child_environment["ASTRID_REMOTION_WARM_MEMORY_BUDGET_BYTES"] = warm_budget
                    configured_idle = os.environ.get("ASTRID_TIMELINE_FRAME_IDLE_SECONDS")
                    if configured_idle is not None:
                        child_environment["ASTRID_TIMELINE_FRAME_IDLE_SECONDS"] = str(warm_owner_idle_seconds())
                    Path(child_environment["ASTRID_FRAME_WORKER_ROOT"]).mkdir(
                        parents=True, exist_ok=True
                    )
                    self._record_owner_boundary("submission_requested", socket=str(socket_path),
                                                previously_bound=self._socket_path == socket_path)
                    try:
                        self._launch_owner(
                            socket_path=socket_path,
                            project_dir=project_dir,
                            node_executable=node_executable,
                            helper=helper,
                            child_environment=child_environment,
                        )
                    except _FrameOwnerSubmissionRefused as error:
                        self._record_owner_boundary("submission_refused", returncode=error.returncode)
                        raise
                    except BaseException as error:
                        # No successful return: do not acquire by planned identity.
                        self._record_owner_boundary("submission_unresolved", error_type=type(error).__name__)
                        raise
                    # Retain successful submission before readiness can fail.
                    self._socket_path = socket_path
                    self._record_owner_boundary("submitted", socket=str(socket_path))
                else:
                    connection.close()
                    self._record_owner_boundary("existing_owner_discovered", socket=str(socket_path))
                try:
                    self._wait_for_owner(socket_path)
                except BaseException as error:
                    self._record_owner_boundary("readiness_failed", error_type=type(error).__name__)
                    raise
                self._record_owner_boundary("ready", socket=str(socket_path))
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_response(connection: socket.socket) -> dict[str, Any]:
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        line = b"".join(chunks).split(b"\n", 1)[0]
        if not line:
            raise RuntimeError("Remotion frame owner closed without a response")
        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Remotion frame owner returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise RuntimeError("Remotion frame owner returned a non-object response")
        if response.get("ok") is not True:
            # This stderr belongs to the managed task and is already retained
            # and secret-scrubbed by GenericPackHost. The launchd owner's own
            # stderr is intentionally not a durable diagnostic sink.
            def details(value: Any, depth: int = 0) -> dict[str, Any] | None:
                if not isinstance(value, dict):
                    return None
                result = {key: value[key][:limit] for key, limit in
                          (("name", 128), ("message", 2048), ("stack", 4096))
                          if isinstance(value.get(key), str)}
                if depth < 2 and isinstance(value.get("cause"), dict):
                    result["cause"] = details(value["cause"], depth + 1)
                return result
            diagnostic = {"diagnostic": "astrid.remotion_frame_owner_failure",
                          "error": str(response.get("error") or "Remotion frame owner failed")[:2048],
                          "cleanup_uncertain": response.get("cleanup_uncertain") is True,
                          "cleanup_error": str(response.get("cleanup_error"))[:2048]
                          if response.get("cleanup_error") is not None else None,
                          "error_details": details(response.get("error_details")),
                          "cleanup_error_details": details(response.get("cleanup_error_details"))}
            print(json.dumps(diagnostic, sort_keys=True), file=sys.stderr, flush=True)
            error = RuntimeError(str(response.get("error") or "Remotion frame owner failed"))
            error.frame_owner_failure = diagnostic
            raise error
        return response

    def _request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if self._socket_path is None:
            raise RuntimeError("Remotion frame owner is not configured")
        connection = self._connect(self._socket_path)
        try:
            connection.sendall(json.dumps(dict(request), separators=(",", ":")).encode("utf-8") + b"\n")
            return self._read_response(connection)
        finally:
            connection.close()

    def render(
        self,
        *,
        project_dir: Path,
        composition_id: str,
        node_executable: Path,
        remotion_cli: Path,
        props_path: Path,
        output_dir: Path,
        frames: Sequence[int],
        resolution: tuple[int, int] | None,
        port: int,
        environment: Mapping[str, str],
        identity: str,
    ) -> None:
        from astrid.core.execution.remotion_host_session import host_remotion_client
        # A bench run owns a separate browser and private bundle, so its first
        # request is genuinely cold without disturbing the live shared owner.
        bench_id = os.environ.get("ASTRID_FRAME_BENCH_ID")
        if bench_id:
            identity = f"{identity}|bench:{bench_id}"
        host_client = host_remotion_client()
        if host_client is not None:
            self._host_identity = identity
            host_request = {
                "type": "render", "projectDir": str(project_dir),
                "compositionId": composition_id, "nodeExecutable": str(node_executable),
                "remotionCli": str(remotion_cli), "propsPath": str(props_path),
                "outputDir": str(output_dir), "frames": [int(frame) for frame in frames],
                "resolution": list(resolution) if resolution is not None else None,
                "port": int(port), "environment": dict(environment),
            }
            if os.environ.get("ASTRID_FRAME_DIAGNOSTICS") == "1":
                host_request["diagnostics"] = True
            response = host_client.request(identity, host_request, self._read_response)
            self.findings = [dict(item) for item in response.get("findings", []) if isinstance(item, dict)][:16]
            self.diagnostics = response.get("diagnostics")
            return
        socket_path, _lock_path = self._owner_paths(identity)
        try:
            self._ensure_owner(
                socket_path=socket_path,
                project_dir=project_dir,
                node_executable=node_executable,
                environment=environment,
            )
        except BaseException as operation_error:
            if self._socket_path == socket_path:
                try:
                    self.close(force=True)
                except BaseException as cleanup_error:
                    self._record_owner_boundary("cleanup_failed", error_type=type(cleanup_error).__name__)
                    raise operation_error from cleanup_error
            raise
        self._socket_path = socket_path
        self._identity = identity
        request = {
            "type": "render",
            "projectDir": str(project_dir),
            "compositionId": composition_id,
            "nodeExecutable": str(node_executable),
            "remotionCli": str(remotion_cli),
            "propsPath": str(props_path),
            "outputDir": str(output_dir),
            "frames": [int(frame) for frame in frames],
            "resolution": list(resolution) if resolution is not None else None,
            "port": int(port),
            "environment": dict(environment),
        }
        if os.environ.get("ASTRID_FRAME_DIAGNOSTICS") == "1":
            request["diagnostics"] = True
        try:
            response = self._request(request)
            self.findings = [dict(item) for item in response.get("findings", []) if isinstance(item, dict)][:16]
            self.diagnostics = response.get("diagnostics")
        except BaseException as operation_error:
            try:
                self.close(force=True)
            except BaseException as cleanup_error:
                raise operation_error from cleanup_error
            raise

    def retain_staged_public_root(self, root: Path) -> None:
        # The owner consumes each invocation's staged files before replying;
        # the renderer backend removes the root after that response.  This
        # method remains a compatibility hook for the old local-session seam.
        return None

    def close(self, *, force: bool = False) -> None:
        if self._host_identity is not None:
            from astrid.core.execution.remotion_host_session import host_remotion_client
            client = host_remotion_client()
            if client is None:
                raise RuntimeError("original host renderer lease is unavailable")
            client.request(self._host_identity, {"type": "release", "force": force}, self._read_response)
            self._host_identity = None
            return
        socket_path = self._socket_path
        if not force and self._owner_cleanup_error is not None:
            raise RuntimeError("original Remotion frame owner cleanup is unresolved") from self._owner_cleanup_error
        if force and socket_path is not None:
            try:
                connection = self._connect(socket_path, timeout_seconds=5.0)
                try:
                    connection.sendall(b'{"type":"release","force":true}\n')
                    response = self._read_response(connection)
                    if response.get("released") is not True or response.get("exit") is not True:
                        raise RuntimeError("original frame owner did not acknowledge release")
                finally:
                    connection.close()
            except BaseException as error:
                # Keep the same original session obligation. A timeout,
                # transport close or cleanup_uncertain reply is no drain.
                if self._owner_cleanup_error is None:
                    self._owner_cleanup_error = error
                self._record_owner_boundary("cleanup_unresolved", socket=str(socket_path),
                                            error_type=type(error).__name__)
                raise RuntimeError("original Remotion frame owner cleanup is unresolved") from error
            removal = self._remove_owner_job(socket_path)
            self._record_owner_boundary("removal_observed", socket=str(socket_path),
                                        result=removal, native_cleanup="UNPROVED")
        self._socket_path = None
        self._identity = None



def _validate_project_dir(project_dir: Path) -> RemotionRuntimeTools:
    if not project_dir.exists():
        raise FileNotFoundError(f"Remotion project directory not found: {project_dir}")
    package_json = project_dir / "package.json"
    if not package_json.exists():
        raise FileNotFoundError(f"Remotion project is missing package.json: {package_json}")
    node_modules = project_dir / "node_modules"
    if not node_modules.exists():
        raise FileNotFoundError(
            f"Remotion project is missing node_modules: {node_modules}; "
            "see docs/setup/release-recipe.md and docs/reference/render-adapter.md for the selected installed Remotion source and adapter package instructions"
        )

    banodoco_root = node_modules / "@banodoco"
    required_packages = (
        "timeline-composition",
        "timeline-schema",
        "timeline-theme-2rp",
    )
    missing = [
        f"@banodoco/{package}"
        for package in required_packages
        if not (banodoco_root / package).is_dir()
    ]
    if missing:
        raise FileNotFoundError(
            f"Missing @banodoco render package(s) in {node_modules}: {', '.join(missing)}. "
            "These packages are adapter-required and not published to a public npm registry. "
            "See docs/setup/release-recipe.md and docs/reference/render-adapter.md for the selected installed source and adapter instructions."
        )
    runtime_tools, tools_error = resolve_remotion_runtime_tools(project_dir)
    if tools_error:
        raise FileNotFoundError(tools_error)
    assert runtime_tools is not None
    return runtime_tools


def _timeline_composition_src(project_dir: Path) -> Path | None:
    composition_src = (
        project_dir / "node_modules" / "@banodoco" / "timeline-composition" / "typescript" / "src"
    )
    return composition_src if composition_src.is_dir() else None


def _resize_frame_outputs(
    output_dir: Path,
    resolution: tuple[int, int] | None,
) -> None:
    """Downscale native Remotion frames without changing composition layout.

    Remotion must render against the authored composition dimensions because
    manual-layout clips use those dimensions when computing their bounds.  A
    thumbnail resolution is therefore an output concern, not a composition
    override.
    """

    if resolution is None:
        return
    width, height = (int(resolution[0]), int(resolution[1]))
    if width <= 0 or height <= 0:
        raise ValueError("frame resolution must contain positive dimensions")
    from PIL import Image

    target = (width, height)
    for path in sorted(output_dir.glob("frame-*.png")):
        with Image.open(path) as image:
            if image.size == target:
                continue
            mode = "RGBA" if "A" in image.getbands() else "RGB"
            resized = image.convert(mode).resize(target, Image.Resampling.LANCZOS)
            resized.save(path, format="PNG")


def _registry_output_paths(project_dir: Path) -> list[Path]:
    composition_src = _timeline_composition_src(project_dir)
    package_src = composition_src or (
        WORKSPACE_ROOT / "packages" / "timeline-composition" / "typescript" / "src"
    )
    paths = [
        package_src / f"{kind}.generated.ts" for kind in ("effects", "animations", "transitions")
    ]
    return paths


def _registry_outputs_exist(project_dir: Path) -> bool:
    return all(path.exists() for path in _registry_output_paths(project_dir))


def _registry_output_content_hashes(project_dir: Path) -> dict[str, str]:
    """Return sha256 of each on-disk generated registry.

    The registry-state cache compares the *computed* generator state against a
    recorded snapshot.  That only detects when the generator's inputs changed;
    it never detects an on-disk registry that drifted from what the generator
    last wrote (e.g. a stale ``node_modules`` copy left by an older generator).
    Reading the actual output content closes that gap: the cache is only valid
    when every on-disk ``*.generated.ts`` matches the recorded content hash.
    """
    content_hashes: dict[str, str] = {}
    for kind in ("effects", "animations", "transitions"):
        path = _registry_output_paths(project_dir)[["effects", "animations", "transitions"].index(kind)]
        if not path.is_file():
            content_hashes[kind] = ""
            continue
        digest = hashlib.sha256()
        digest.update(path.read_bytes())
        content_hashes[kind] = digest.hexdigest()
    return content_hashes


def _registry_outputs_match_state(
    project_dir: Path,
    cached_state: dict[str, Any],
) -> bool:
    """True only when the on-disk registries match the cached state snapshot.

    ``cached_state`` records per-kind content hashes of what the generator last
    produced.  Comparing against the live files catches a stale ``node_modules``
    copy whose content no longer matches the cached snapshot, even when the
    generator's *inputs* are unchanged (the ``hash`` still matches).
    """
    if not _registry_outputs_exist(project_dir):
        return False
    recorded = cached_state.get("content_hashes")
    if not isinstance(recorded, dict):
        return False
    on_disk = _registry_output_content_hashes(project_dir)
    # Verify every kind the snapshot recorded.  A kind absent from the
    # snapshot (e.g. a minimal state recording only effects) is validated by
    # the existence check above, not its content hash — this keeps the guard
    # backward compatible with callers that track a subset of the registry.
    for kind, recorded_hash in recorded.items():
        if on_disk.get(kind) != recorded_hash:
            return False
    return True


def _effective_registry_state(theme_path: Path | None) -> dict[str, Any]:
    if gen_effect_registry is None:
        return {"version": 1, "hash": "server-provisioned"}
    del theme_path
    return gen_effect_registry.compute_generated_registry_state()


def _read_registry_state(project_dir: Path) -> dict[str, Any] | None:
    state_path = project_dir / _REGISTRY_STATE_PATH
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_registry_state(project_dir: Path, state: dict[str, Any]) -> None:
    state_path = project_dir / _REGISTRY_STATE_PATH
    state_path.write_text(
        json.dumps(state, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _regenerate_element_registries(
    project_dir: Path,
    theme_path: Path | None,
) -> None:
    if remotion_lock.remotion_render_lock_held():
        _regenerate_element_registries_locked(project_dir, theme_path)
        return
    with remotion_lock.remotion_render_lock():
        _regenerate_element_registries_locked(project_dir, theme_path)


def _regenerate_element_registries_locked(
    project_dir: Path,
    theme_path: Path | None,
) -> None:
    """Regenerate shared registries while the caller owns the Remotion lock."""

    from astrid.core.env_vars import ASTRID_REMOTION_VALIDATION_LAUNCH
    if ASTRID_REMOTION_VALIDATION_LAUNCH in os.environ:
        from astrid.core.rendering.remotion_runtime import _validated_runtime
        from astrid.core.element.registry import clear_default_registry_cache
        validated = _validated_runtime(project_dir)
        registry = validated['registry']
        if registry['mode'] == 'source_settled':
            clear_default_registry_cache()
            fresh = _effective_registry_state(theme_path)
            if (fresh != registry['expected_state']
                    or _read_registry_state(project_dir) != fresh
                    or _registry_output_content_hashes(project_dir) != fresh['content_hashes']):
                raise RuntimeError('launch-bound registry drift refuses regeneration')
        # Provisioned wheel output bindings are checked above; never invent a
        # source generator for a wheel. Launch-bound consumers never rewrite.
        return

    if gen_effect_registry is None:
        if not _registry_outputs_exist(project_dir):
            raise RuntimeError("installed Remotion bundle is missing generated element registries")
        return

    state = _effective_registry_state(theme_path)
    cached_state = _read_registry_state(project_dir)
    if (
        cached_state is not None
        and cached_state.get("hash") == state.get("hash")
        and _registry_outputs_match_state(project_dir, cached_state)
    ):
        return

    generator = REPO_ROOT / "scripts" / "gen_effect_registry.py"
    cmd = [sys.executable, str(generator)]
    env: dict[str, str] = {}
    composition_src = _timeline_composition_src(project_dir)
    if composition_src is not None:
        env["ASTRID_TIMELINE_COMPOSITION_SRC"] = str(composition_src)
    env.update(remotion_lock.remotion_render_lock_child_env())
    subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=build_child_subprocess_env(explicit_env=env),
        capture_output=True,
        check=True,
        text=True,
    )
    _write_registry_state(project_dir, state)


_EFFECT_IDS_RE = re.compile(r"EFFECT_IDS\s*=\s*\[([^\]]*)\]\s*as const")


def _generated_effect_registry_ids(project_dir: Path) -> set[str]:
    """Parse the EFFECT_IDS that the active composition's registry exposes."""
    effects_path = _registry_output_paths(project_dir)[0]
    if not effects_path.is_file():
        return set()
    text = effects_path.read_text(encoding="utf-8")
    match = _EFFECT_IDS_RE.search(text)
    if match is None:
        raise RuntimeError(
            f"generated effect registry {effects_path} does not declare EFFECT_IDS "
            "(expected `export const EFFECT_IDS = [...] as const;`)"
        )
    return set(re.findall(r"'([a-z0-9][a-z0-9-]*)'", match.group(1)))


def _validate_renderer_effect_registry(
    project_dir: Path,
    timeline_data: Mapping[str, Any],
) -> None:
    """Fail closed when a referenced effect has no renderable component.

    The authoring catalog (``astrid.core.element.catalog``) reports every
    effect the pack layout knows about, but the Remotion composition only
    mounts effects present in its generated ``EFFECT_REGISTRY``.  A catalog
    entry that is missing from the generated registry is a catalog/renderer
    divergence: the timeline validates but renders only the base plate.  This
    cross-reference surfaces that divergence before the render is admitted,
    naming the offending effect and the fix.

    Only effects actually referenced by ``timeline_data`` are checked so a
    catalog effect that is simply not used by this timeline never blocks a
    render (it is not part of the render's admission contract).  A timeline
    that references no catalog effect is a no-op even if the generated
    registry is a placeholder (it cannot silently drop an effect it never
    references).
    """
    from astrid.core.element import catalog as element_catalog

    clips = timeline_data.get("clips") if isinstance(timeline_data, dict) else None
    if not isinstance(clips, list):
        return

    catalog_ids = set(element_catalog.list_effect_ids())
    referenced_catalog_effects = {
        clip.get("clipType")
        for clip in clips
        if isinstance(clip, Mapping)
        and isinstance(clip.get("clipType"), str)
        and clip.get("clipType") not in _BUILTIN_MEDIA_CLIP_TYPES
        and clip.get("clipType") in catalog_ids
    }
    if not referenced_catalog_effects:
        return

    # A referenced catalog effect is only renderable if the active generated
    # EFFECT_REGISTRY mounts it.  Parse the registry lazily so a timeline that
    # references nothing (or only built-ins) never trips on a placeholder file.
    generated_ids = _generated_effect_registry_ids(project_dir)
    missing = sorted(referenced_catalog_effects - generated_ids)
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            "timeline references effect(s) the active composition's generated "
            f"EFFECT_REGISTRY cannot mount: {joined}. The catalog knows these "
            "effects but the renderer registry is missing their components, so "
            "the render would silently drop to the base plate. Regenerate the "
            f"registry (`python scripts/gen_effect_registry.py`) so these "
            "components are bundled, then retry."
        )


def _render_asset_stage_hash(
    timeline_path: Path,
    assets_path: Path,
    out_path: Path,
) -> str:
    digest = hashlib.sha256()
    for path in (timeline_path, assets_path):
        resolved = path.resolve()
        digest.update(str(resolved).encode("utf-8"))
        digest.update(b"\0")
        if resolved.exists():
            digest.update(resolved.read_bytes())
        digest.update(b"\0")
    digest.update(str(out_path.resolve()).encode("utf-8"))
    return digest.hexdigest()[:16]


def _effect_registry_for_assets(
    theme_path: Path | None,
    *,
    registry: Any | None = None,
) -> tuple[dict[str, ElementDefinition], dict[str, str]]:
    # Element discovery is pack-owned.  A style document only contributes
    # visual/generation props and cannot alter the executable registry.
    del theme_path
    if registry is None:
        registry = load_default_registry(project_root=REPO_ROOT)
    effects = {element.id: element for element in registry.list(kind="effects")}
    aliases: dict[str, str] = {}
    if "text-card" in effects:
        aliases["text"] = "text-card"
    for effect_id, element in effects.items():
        raw_aliases = element.metadata.get("clipTypeAliases")
        if not isinstance(raw_aliases, list):
            continue
        for alias in raw_aliases:
            if isinstance(alias, str) and alias:
                aliases[alias] = effect_id
    return effects, aliases


def _element_reference_ids(value: Any) -> tuple[str, ...]:
    """Normalize a timeline animation/transition reference to element ids."""
    values = value if isinstance(value, list) else [value]
    ids: list[str] = []
    for item in values:
        if isinstance(item, str) and item:
            ids.append(item)
        elif isinstance(item, Mapping):
            raw = item.get("id", item.get("type", item.get("kind")))
            if isinstance(raw, str) and raw:
                ids.append(raw)
    return tuple(ids)


def _resolve_timeline_element_references(
    timeline_data: Mapping[str, Any],
    *,
    theme_path: Path | None,
    registry: Any | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Resolve every requested animation/transition before Remotion runs.

    The generated TypeScript registry throws only when the affected clip is
    mounted.  Support probing this same registry up front prevents a typo or
    an unavailable external pack from becoming a successful-looking stock
    render.
    """
    if registry is None:
        del theme_path
        registry = load_default_registry(project_root=REPO_ROOT)
    clips = timeline_data.get("clips")
    if not isinstance(clips, list):
        return {"animations": [], "transitions": []}
    resolved: dict[str, list[dict[str, Any]]] = {"animations": [], "transitions": []}
    seen: set[tuple[str, str, str | None]] = set()
    for clip in clips:
        if not isinstance(clip, Mapping):
            continue
        clip_id = str(clip.get("id") or "")
        ref = clip.get("elementRef")
        if isinstance(ref, Mapping) and "packId" in ref:
            owner = ref.get("packId")
            if not isinstance(owner, str) or not owner:
                raise ValueError("elementRef.packId must be a nonempty string")
            try:
                registry.get(str(ref.get("kind")), str(ref.get("id")), pack_id=owner)
            except (KeyError, ValueError) as exc:
                raise ValueError(f"timeline uses unregistered owned element {dict(ref)!r}") from exc
        for slot_name, kind in (
            ("entrance", "animations"),
            ("exit", "animations"),
            ("continuous", "animations"),
            ("animations", "animations"),
            ("transition", "transitions"),
        ):
            for element_id in _element_reference_ids(clip.get(slot_name)):
                owner = (
                    ref.get("packId") if isinstance(ref, Mapping)
                    and ref.get("id") == element_id
                    and str(ref.get("kind")) == kind[:-1] else None
                )
                key = (kind, element_id, owner)
                if key in seen:
                    for item in resolved[kind]:
                        if item.get("element_id") == element_id and (owner is None or item.get("source_pack_id") == owner):
                            if clip_id and clip_id not in item["clip_ids"]:
                                item["clip_ids"].append(clip_id)
                            break
                    continue
                seen.add(key)
                try:
                    element = registry.get(kind, element_id, pack_id=owner) if owner is not None else registry.get(kind, element_id)
                except (KeyError, ValueError) as exc:
                    raise ValueError(
                        f"timeline uses unregistered {kind[:-1]} {element_id!r}"
                    ) from exc
                resolved[kind].append(
                    {
                        "element_id": element_id,
                        "source_pack_id": _source_pack_id(element),
                        "source": element.source,
                        "element_root": str(element.root),
                        "clip_ids": [clip_id] if clip_id else [],
                    }
                )
    return resolved


def _effect_id_for_clip(
    clip: dict[str, Any],
    effects: dict[str, ElementDefinition],
    aliases: dict[str, str],
) -> str | None:
    clip_type = clip.get("clipType")
    if not isinstance(clip_type, str) or clip_type == "effect-layer":
        return None
    if clip_type in effects:
        return clip_type
    return aliases.get(clip_type)


def _source_pack_id(element: ElementDefinition) -> str:
    pack_id = element.metadata.get("pack_id")
    if isinstance(pack_id, str) and pack_id:
        return pack_id
    if element.source.startswith("pack:"):
        return element.source.split(":", 1)[1]
    return element.source


def _inject_clip_asset_params(
    clip: dict[str, Any],
    staged_assets: dict[str, str],
) -> None:
    params = clip.get("params")
    next_params = dict(params) if isinstance(params, dict) else {}
    next_params["__astridAssets"] = staged_assets
    clip["params"] = next_params


def _resolve_end_spanning_card_assets(
    clip: Mapping[str, Any],
    asset_registry: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Resolve authored registry keys through the invocation's materializer."""
    params = clip.get("params")
    overrides = params.get("cardAssets") if isinstance(params, Mapping) else None
    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping):
        raise ValueError("end-spanning-layer cardAssets must be an object")
    assets = (asset_registry or {}).get("assets", {})
    resolved: dict[str, str] = {}
    for card, asset_id in overrides.items():
        if card not in {f"card{i}" for i in range(6)} or not isinstance(asset_id, str) or not asset_id.strip():
            raise ValueError("end-spanning-layer cardAssets requires card0–card5 registry keys")
        entry = assets.get(asset_id) if isinstance(assets, Mapping) else None
        file = entry.get("file") if isinstance(entry, Mapping) else None
        if not isinstance(file, str) or not file.strip():
            raise ValueError(f"end-spanning-layer card {card!r} references unresolved asset {asset_id!r}")
        resolved[card] = file
    return resolved


def _stage_effect_assets_for_timeline(
    timeline_data: dict[str, Any],
    *,
    project_dir: Path,
    theme_path: Path | None,
    render_hash: str,
    composition_clip_types: frozenset[str] = frozenset(),
    asset_registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    # Resolve one immutable element registry for this render. Effects and
    # animation/transition references must agree even if the filesystem pack
    # inventory changes between support probing and execution; loading the
    # registry twice here also made the combined render path needlessly
    # discover every local pack twice.
    registry = load_default_registry(project_root=REPO_ROOT)
    effects, aliases = _effect_registry_for_assets(theme_path, registry=registry)
    resolved_elements = _resolve_timeline_element_references(
        timeline_data,
        theme_path=theme_path,
        registry=registry,
    )
    clips = timeline_data.get("clips")
    if not isinstance(clips, list):
        return {"root": None, "effects": [], **resolved_elements}

    unknown_clip_types = sorted(
        {
            str(clip.get("clipType"))
            for clip in clips
            if isinstance(clip, Mapping)
            and isinstance(clip.get("clipType"), str)
            and clip.get("clipType") not in _BUILTIN_MEDIA_CLIP_TYPES
            and clip.get("clipType") not in composition_clip_types
            and clip.get("clipType") != "effect-layer"
            and clip.get("clipType") not in effects
            and clip.get("clipType") not in aliases
        }
    )
    if unknown_clip_types:
        raise ValueError(
            "timeline uses unregistered effect clip type(s): " + ", ".join(unknown_clip_types)
        )

    used_effects: dict[tuple[str, str], ElementDefinition] = {}
    clip_effects: dict[int, tuple[str, str]] = {}
    clip_ids_by_effect: dict[tuple[str, str], list[str]] = {}
    for index, clip in enumerate(clips):
        if not isinstance(clip, dict):
            continue
        effect_id = _effect_id_for_clip(clip, effects, aliases)
        ref = clip.get("elementRef")
        if isinstance(ref, Mapping) and ref.get("kind") == "effect":
            effect_id = ref.get("id")
        if effect_id is None:
            continue
        try:
            element = registry.get("effects", effect_id, pack_id=ref.get("packId")) if isinstance(ref, Mapping) and "packId" in ref else registry.get("effects", effect_id)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"timeline uses unregistered effect {effect_id!r}") from exc
        key = (_source_pack_id(element), element.id)
        used_effects[key] = element
        clip_effects[index] = key
        clip_id = clip.get("id")
        if isinstance(clip_id, str) and clip_id:
            clip_ids_by_effect.setdefault(key, []).append(clip_id)

    if not used_effects:
        return {"root": None, "effects": [], **resolved_elements}

    public_root = project_dir / "public" / "astrid-effects" / render_hash
    staged_by_effect: dict[tuple[str, str], dict[str, str]] = {}
    for key, element in sorted(used_effects.items()):
        staged_assets: dict[str, str] = {}
        for asset in element.assets:
            source = (element.root / asset.path).resolve()
            # Different owners of the same ID must never share asset targets.
            relative_target = Path(key[0]) / element.id / asset.path
            target = public_root / relative_target
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            staged_assets[asset.name] = f"astrid-effects/{render_hash}/{relative_target.as_posix()}"
        staged_by_effect[key] = staged_assets

    for index, key in clip_effects.items():
        clip = clips[index]
        if isinstance(clip, dict):
            staged_assets = dict(staged_by_effect[key])
            if key == ("local", "end-spanning-layer"):
                staged_assets.update(_resolve_end_spanning_card_assets(clip, asset_registry))
            if staged_assets:
                _inject_clip_asset_params(clip, staged_assets)
    return {
        "root": str(public_root),
        "effects": [
            {
                "effect_id": key[1],
                "source_pack_id": key[0],
                "source": element.source,
                "element_root": str(element.root),
                "clip_ids": sorted(clip_ids_by_effect.get(key, ())),
                "staged_asset_ids": sorted(staged_by_effect[key]),
                "staged_assets": dict(sorted(staged_by_effect[key].items())),
            }
            for key, element in sorted(used_effects.items())
        ],
        **resolved_elements,
    }


def _assert_outside_public(path: Path, project_dir: Path) -> None:
    """Refuse Remotion scratch inside the project's public directory.

    Remotion copies ``public/`` into every bundle it writes. A scratch or
    bundle directory under ``public/`` therefore makes each bundle copy the
    previous one, which grows without bound and fills the disk.
    """

    public = (Path(project_dir) / "public").resolve(strict=False)
    if Path(path).resolve(strict=False).is_relative_to(public):
        raise RuntimeError(
            f"Remotion scratch {path} is inside the public directory {public}; "
            "Remotion would copy it into every bundle. Stage the render under the "
            "attempt root instead."
        )


def _stderr_tail(stderr: str) -> str:
    lines = stderr.splitlines()
    tail = lines[-40:] if len(lines) > 40 else lines
    return "\n".join(tail).strip()


def _require_free_space(path: Path, min_free_gb: float | None) -> None:
    if min_free_gb is None or min_free_gb <= 0:
        return
    target = path if path.exists() else path.parent
    usage = shutil.disk_usage(target)
    min_free = int(min_free_gb * 1024 * 1024 * 1024)
    if usage.free < min_free:
        free_gb = usage.free / (1024 * 1024 * 1024)
        raise RuntimeError(
            f"Remotion render needs at least {min_free_gb:.1f} GiB free at {target}; "
            f"only {free_gb:.1f} GiB is available"
        )


def _available_remotion_port() -> int:
    """Select an available loopback port for Remotion's browser server."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _execute_remotion(
    timeline_path: Path,
    assets_path: Path,
    staged_video: Path,
    *,
    provenance_out_path: Path,
    project_dir: Path,
    composition_id: str,
    theme_path: Path | None,
    min_free_gb: float | None,
    review: Mapping[str, Any] | None = None,
    render_scale: float | None = None,
    materialized_root: Path | None = None,
    staging_parent: Path | None = None,
    materialized_objects: Mapping[str, str] | None = None,
    compiled_audio: Mapping[str, Any] | None = None,
    timeline_authority: Mapping[str, Any] | None = None,
    composition_clip_types: frozenset[str] = frozenset(),
    encode_aac_at_stitch: bool = False,
    frame_numbers: Sequence[int] | None = None,
    frame_output_dir: Path | None = None,
    frame_resolution: tuple[int, int] | None = None,
    frame_session: PersistentRemotionFrameSession | None = None,
    frame_session_identity: str | None = None,
) -> _ExecutionDetails:
    """Render one private video and return the data needed for provenance."""

    final_path = provenance_out_path.resolve()
    requested_stage = staged_video.resolve()
    if requested_stage != final_path:
        with remotion_lock.remotion_render_lock():
            return _execute_remotion_locked(
                timeline_path,
                assets_path,
                requested_stage,
                provenance_out_path=provenance_out_path,
                project_dir=project_dir,
                composition_id=composition_id,
                theme_path=theme_path,
                min_free_gb=min_free_gb,
                review=review,
                render_scale=render_scale,
                materialized_root=materialized_root,
                staging_parent=staging_parent,
                materialized_objects=materialized_objects,
                compiled_audio=compiled_audio,
                timeline_authority=timeline_authority,
                composition_clip_types=composition_clip_types,
                encode_aac_at_stitch=encode_aac_at_stitch,
                frame_numbers=frame_numbers,
                frame_output_dir=frame_output_dir,
                frame_resolution=frame_resolution,
                frame_session=frame_session,
                frame_session_identity=frame_session_identity,
            )

    # Direct backend callers may pass the final output as the staging path.
    # Never let Remotion write the published path directly: create a unique
    # sibling on the same filesystem and atomically publish it only after a
    # successful, verified render.  This also avoids HFS+ temporary-path
    # aliases observed with the platform tempfile implementation.
    final_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{final_path.name}.remotion-stage-",
        suffix=final_path.suffix,
        dir=str(final_path.parent),
    )
    os.close(descriptor)
    isolated_stage = Path(temporary_name)
    isolated_stage.unlink(missing_ok=True)
    try:
        with remotion_lock.remotion_render_lock():
            details = _execute_remotion_locked(
                timeline_path,
                assets_path,
                isolated_stage,
                provenance_out_path=provenance_out_path,
                project_dir=project_dir,
                composition_id=composition_id,
                theme_path=theme_path,
                min_free_gb=min_free_gb,
                review=review,
                render_scale=render_scale,
                materialized_root=materialized_root,
                staging_parent=staging_parent,
                materialized_objects=materialized_objects,
                compiled_audio=compiled_audio,
                timeline_authority=timeline_authority,
                composition_clip_types=composition_clip_types,
                encode_aac_at_stitch=encode_aac_at_stitch,
                frame_numbers=frame_numbers,
                frame_output_dir=frame_output_dir,
                frame_resolution=frame_resolution,
                frame_session=frame_session,
                frame_session_identity=frame_session_identity,
            )
        os.replace(isolated_stage, final_path)
        return details
    finally:
        isolated_stage.unlink(missing_ok=True)


def _execute_remotion_locked(
    timeline_path: Path,
    assets_path: Path,
    staged_video: Path,
    *,
    provenance_out_path: Path,
    project_dir: Path,
    composition_id: str,
    theme_path: Path | None,
    min_free_gb: float | None,
    review: Mapping[str, Any] | None = None,
    render_scale: float | None = None,
    materialized_root: Path | None = None,
    staging_parent: Path | None = None,
    materialized_objects: Mapping[str, str] | None = None,
    compiled_audio: Mapping[str, Any] | None = None,
    timeline_authority: Mapping[str, Any] | None = None,
    composition_clip_types: frozenset[str] = frozenset(),
    encode_aac_at_stitch: bool = False,
    frame_numbers: Sequence[int] | None = None,
    frame_output_dir: Path | None = None,
    frame_resolution: tuple[int, int] | None = None,
    frame_session: PersistentRemotionFrameSession | None = None,
    frame_session_identity: str | None = None,
) -> _ExecutionDetails:
    """Execute one render while the caller owns the non-recursive outer lock."""

    runtime_tools = _validate_project_dir(project_dir)
    _regenerate_element_registries(project_dir, theme_path)
    # Render admission: the catalog and the active composition's generated
    # EFFECT_REGISTRY must agree.  A catalog effect that the registry cannot
    # mount would otherwise pass validation and silently drop to the base
    # plate; fail closed here with an actionable diagnostic.
    from astrid.core.rendering.compiled_audio import attach_compiled_audio
    from astrid.core.rendering.envelope_pcm import requires_envelope_pcm
    timeline_data = attach_compiled_audio(
        json.loads(timeline_path.read_text(encoding="utf-8")), compiled_audio, timeline_authority,
        renderer_config=_serialize_timeline(timeline_path))
    _validate_renderer_effect_registry(project_dir, timeline_data)
    registry_state = _effective_registry_state(theme_path)
    _require_free_space(provenance_out_path.parent, min_free_gb)
    remotion_port = _available_remotion_port()
    remotion_origin = f"http://localhost:{remotion_port}"
    props_path = (provenance_out_path.parent / ".remotion-props.json").resolve()
    render_hash = _render_asset_stage_hash(
        timeline_path,
        assets_path,
        provenance_out_path,
    )
    staged_public_root = project_dir / "public" / "astrid-effects" / render_hash
    with ExitStack() as asset_lifecycle:
        try:
            temp_parent = (staging_parent or provenance_out_path.parent).resolve()
            temp_parent.mkdir(parents=True, exist_ok=True)
            remotion_temp_root = Path(
                asset_lifecycle.enter_context(
                    TemporaryDirectory(prefix=".remotion-runtime-", dir=str(temp_parent))
                )
            )
            _assert_outside_public(remotion_temp_root, project_dir)
            materializer = asset_lifecycle.enter_context(
                AssetMaterializer(
                    assets_path,
                    materialized_objects=materialized_objects,
                    materialized_root=materialized_root,
                    allow_derived_files=materialized_root is not None,
                    reuse_materialized_paths=materialized_root is not None and not requires_envelope_pcm(timeline_data),
                    staging_parent=staging_parent,
                )
            )
            asset_server = None
            if materializer.needs_server:
                try:
                    asset_server = asset_lifecycle.enter_context(
                        InvocationAssetServer(
                            materializer.serving_root,
                            allowed_origin=remotion_origin,
                        )
                    )
                except OSError as exc:
                    raise RuntimeError(
                        f"Permission denied (1100): local HTTP asset server blocked: {exc}"
                    ) from exc
            resolved_registry = materializer.resolved_registry(asset_server)
            # ``theme_path`` is either the host's absolute materialized
            # document or None, which selects the intentional built-in style.
            theme_for_props = _resolved_theme_for_render(timeline_path, theme_path)
            render_timeline = attach_compiled_audio(
                json.loads(timeline_path.read_text(encoding="utf-8")), compiled_audio, timeline_authority,
                renderer_config=_serialize_timeline(timeline_path, default_theme=str(theme_for_props.get("id") or "banodoco-default")))
            from astrid.core.rendering.envelope_pcm import prepare_envelope_pcm
            render_timeline = prepare_envelope_pcm(render_timeline, materializer, asset_server)
            merged_props = {
                "timeline": render_timeline,
                "assets": resolved_registry,
                "theme": theme_for_props,
                "review": review,
            }
            # Batch 4 (layer stack): the service stamps
            # ``metadata.astrid_layer.alpha = z > 0`` onto the materialized
            # timeline of z-layer segments.  When the stamp says alpha, emit
            # TRANSPARENT output (PNG frames -> ProRes 4444/yuva444p12le in
            # MOV -- remotion 4.0.509 muxes NO alpha plane for vp9/webm, so
            # ProRes 4444 is the alpha path); the unstamped path keeps
            # today's jpeg/h264/MP4 contract exactly.
            alpha = _timeline_alpha(merged_props["timeline"])
            # Remotion 4.0.509 otherwise compresses the mix to raw ADTS AAC
            # before copying it into MP4, losing encoder priming metadata.
            # Keep this correction explicit and scoped to opaque MP4 capture.
            # PCM requires a .mov/.mkv CLI name; the Astrid config hook writes
            # MP4 bytes to this invocation-private .mov, then we stage them at
            # the original path without changing the published output profile.
            frame_mode = frame_numbers is not None
            if frame_mode:
                if not frame_numbers or any(
                    type(frame) is not int or frame < 0 for frame in frame_numbers
                ):
                    raise ValueError("frame capture requires non-negative integer frames")
                if frame_output_dir is None:
                    raise ValueError("frame capture requires an output directory")
                frame_output_dir = frame_output_dir.resolve()
                frame_output_dir.mkdir(parents=True, exist_ok=True)
            pcm_aac_mp4 = (
                encode_aac_at_stitch
                and not frame_mode
                and not alpha
                and staged_video.suffix.lower() == ".mp4"
            )
            cli_video = (
                frame_output_dir if frame_mode else
                remotion_temp_root / "capture.mov" if pcm_aac_mp4 else staged_video
            )
            if alpha:
                # Theme background neutralization: the DOM
                # TimelineComposition paints ``theme.visual.color.bg`` as an
                # opaque AbsoluteFill (node_modules/@banodoco/...:272), so a
                # stamped top layer would still emit opaque corners even with
                # the threejs <color> skip.  Making the serialized theme's bg
                # transparent keeps BOTH compositions from painting anything
                # opaque.  This mutates only the per-run props copy; the
                # resolved theme dict is built fresh for this render.
                theme_color = merged_props["theme"].setdefault("visual", {}).setdefault("color", {})
                theme_color["bg"] = "transparent"
            stage_summary = _stage_effect_assets_for_timeline(
                merged_props["timeline"],
                project_dir=project_dir,
                theme_path=theme_path,
                render_hash=render_hash,
                composition_clip_types=composition_clip_types,
                asset_registry=resolved_registry,
            )
            staged_video.parent.mkdir(parents=True, exist_ok=True)
            props_path.write_text(json.dumps(merged_props), encoding="utf-8")
            remotion_env_additions: dict[str, str] = {
                # Keep Remotion's pre-encode files on the admitted attempt
                # filesystem and let the lifecycle remove them on every exit.
                "TMPDIR": str(remotion_temp_root),
                "TMP": str(remotion_temp_root),
                "TEMP": str(remotion_temp_root),
            }
            if pcm_aac_mp4:
                remotion_env_additions["ASTRID_REMOTION_PCM_AAC_OUTPUT"] = str(cli_video)
            # No machine-RAM heuristic. Unallocated renders use ordinary
            # disk fallback, which the admission envelope fully reserves.
            allocation = os.environ.get("ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES", "0")
            try:
                allocation_bytes = int(allocation)
            except ValueError:
                raise ValueError("invalid owner streaming memory allocation") from None
            if allocation_bytes < 0:
                raise ValueError("invalid owner streaming memory allocation")
            if not alpha and not frame_mode and allocation_bytes > 0:
                remotion_env_additions["ASTRID_REMOTION_STREAM_FRAMES"] = "1"
                remotion_env_additions["ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES"] = str(allocation_bytes)
            schema_pythonpath = os.environ.get(TIMELINE_SCHEMA_PYTHONPATH_ENV)
            if schema_pythonpath:
                # The renderer receives only the validated server-owned schema
                # root.  Ambient PYTHONPATH entries can point at editable
                # worktrees and must never be inherited by the render child.
                remotion_env_additions["PYTHONPATH"] = schema_pythonpath
            composition_src = _timeline_composition_src(project_dir)
            if composition_src is not None:
                remotion_env_additions["ASTRID_TIMELINE_COMPOSITION_SRC"] = str(composition_src)
            remotion_args = [
                str(runtime_tools.node_executable),
                str(runtime_tools.remotion_cli),
                "render",
                composition_id,
                "--props",
                str(props_path),
                "--output",
                str(cli_video),
                "--allow-html-in-canvas",
                f"--port={remotion_port}",
                # Remotion otherwise starts multiple Chromium workers, each
                # requesting the full managed registry at once.  The media
                # server is invocation-scoped and intentionally lightweight;
                # one worker keeps the request burst bounded for large
                # timelines while preserving the same renderer and output.
                "--concurrency=1",
                # The generated package registry is part of the renderer
                # source closure. Remotion's filesystem cache is keyed by
                # the entry/config bundle rather than generated registry
                # contents, so reusing it can silently omit a newly admitted
                # effect (for example the canonical end-spanning layer).
                # Disable only this bundle cache; media and frame caches stay
                # invocation-scoped and bounded by the existing lifecycle.
                "--bundle-cache=false",
            ]
            if frame_mode:
                remotion_args += [
                    "--sequence",
                    "--image-format=png",
                    "--image-sequence-pattern=frame-[frame].[ext]",
                    "--frames=" + ",".join(str(frame) for frame in frame_numbers),
                ]
            elif alpha:
                # ProRes 4444 is the only engine-native alpha mux in remotion
                # 4.0.509: vp9/webm emits plain yuv420p (probed, dead path).
                # The CLI pixel-format is yuva444p10le; the muxed artifact is
                # probed as yuva444p12le (see _remotion_mux_profile).
                remotion_args += [
                    "--enforce-audio-track",
                    "--image-format=png",
                    "--pixel-format=yuva444p10le",
                    "--codec=prores",
                    "--prores-profile=4444",
                ]
            else:
                remotion_args.append(f"--image-format={remotion_frame_format(review=review is not None)}")
                remotion_args.append("--enforce-audio-track")
                profile = _canonical_profile(
                    timeline_path,
                    _load_registry_mapping(assets_path),
                    theme_path,
                )
                if render_scale is not None:
                    width, height = _remotion_scaled_dimensions(profile, render_scale)
                    profile = replace(profile, width=width, height=height)
                max_rate_bps, buffer_size_bps = h264_encoder_bitrates(
                    width=profile.width,
                    height=profile.height,
                    fps_rational=profile.fps_rational,
                )
                remotion_args += [
                    "--crf=18",
                    f"--max-rate={max_rate_bps // 1000}K",
                    f"--buffer-size={buffer_size_bps // 1000}K",
                    "--audio-bitrate=320K",
                ]
                if pcm_aac_mp4:
                    # A MOV filename otherwise makes the CLI infer ProRes.
                    remotion_args += [
                        "--codec=h264",
                        "--audio-codec=pcm-16",
                        "--sample-rate=48000",
                    ]
            if render_scale is not None and render_scale != 1:
                remotion_args.append(f"--scale={render_scale:.15g}")
            diagnostic_acceptance = any(clip.get('app', {}).get('liveScene', {}).get('__l1bTrace') is True for clip in merged_props['timeline'].get('clips', []))
            diagnostic_trace = diagnostic_acceptance or any('<!-- astrid-l1b-diagnostic -->' in str(clip.get('app', {}).get('liveScene', {}).get('html', '')) for clip in merged_props['timeline'].get('clips', []))
            diagnostic_root = project_dir.parent / '.otto' / 'l1b-fixtures'
            diagnostic_label = 'acceptance' if diagnostic_acceptance else 'trace'
            if diagnostic_trace:
                remotion_args.append('--log=verbose')
                (diagnostic_root / f'render-{diagnostic_label}-command.json').write_text(json.dumps({'args': remotion_args, 'cwd': str(project_dir)}))
            if frame_mode and frame_session is not None:
                frame_session.render(
                    project_dir=project_dir,
                    composition_id=composition_id,
                    node_executable=runtime_tools.node_executable,
                    remotion_cli=runtime_tools.remotion_cli,
                    props_path=props_path,
                    output_dir=frame_output_dir,
                    frames=frame_numbers,
                    resolution=frame_resolution,
                    port=remotion_port,
                    environment=remotion_env_additions,
                    identity=frame_session_identity or render_hash,
                )
                frame_session.retain_staged_public_root(staged_public_root)
            else:
                progress_path = os.environ.get("ASTRID_PROGRESS_PATH") if os.environ.get("ASTRID_INTERNAL_INVOCATION") == "1" else None
                if progress_path:
                    log_path = remotion_temp_root / "frame-progress.log"
                    with log_path.open("w") as stdout_log, RemotionProgressRelay(log_path, progress_path=progress_path):
                        completed = subprocess.run(
                            remotion_args, cwd=str(project_dir),
                            env=build_child_subprocess_env(explicit_env=remotion_env_additions),
                            stdout=stdout_log, stderr=subprocess.PIPE, check=False, text=True)
                else:
                    completed = subprocess.run(
                        remotion_args, cwd=str(project_dir),
                        env=build_child_subprocess_env(explicit_env=remotion_env_additions),
                        capture_output=True, check=False, text=True)
                if diagnostic_trace:
                    diagnostic_stdout = completed.stdout
                    if diagnostic_stdout is None and progress_path:
                        with log_path.open("rb") as log:
                            log.seek(max(0, log_path.stat().st_size - 65536))
                            diagnostic_stdout = log.read().decode("utf-8", "replace")
                    (diagnostic_root / f'render-{diagnostic_label}-stdout.log').write_text(diagnostic_stdout or "")
                    (diagnostic_root / f'render-{diagnostic_label}-stderr.log').write_text(completed.stderr)
                if completed.returncode != 0:
                    stderr_tail = _stderr_tail(completed.stderr)
                    message = f"Remotion render failed with exit code {completed.returncode}"
                    if stderr_tail:
                        message = f"{message}\n{stderr_tail}"
                    raise RuntimeError(message)
            if frame_mode:
                _resize_frame_outputs(frame_output_dir, frame_resolution)
                produced = sorted(frame_output_dir.glob("frame-*.png"))
                if not produced:
                    raise RuntimeError("Remotion frame capture did not produce PNG frames")
                return _ExecutionDetails(
                    active_theme=theme_for_props,
                    registry_state=registry_state,
                    stage_summary=stage_summary,
                    runtime={
                        "node_executable": str(runtime_tools.node_executable),
                        "node_version": runtime_tools.node_version,
                        "remotion_cli": str(runtime_tools.remotion_cli),
                        "mode": "image_sequence",
                        "findings": getattr(frame_session, "findings", []),
                    },
                )
            if not cli_video.is_file() or cli_video.stat().st_size <= 0:
                raise RuntimeError("Remotion render did not produce a non-empty video")
            if pcm_aac_mp4:
                os.replace(cli_video, staged_video)
            return _ExecutionDetails(
                active_theme=theme_for_props,
                registry_state=registry_state,
                stage_summary=stage_summary,
                runtime={
                    "node_executable": str(runtime_tools.node_executable),
                    "node_version": runtime_tools.node_version,
                    "remotion_cli": str(runtime_tools.remotion_cli),
                },
            )
        finally:
            props_path.unlink(missing_ok=True)
            # The external owner has finished consuming the staged request
            # before it replies. Always remove invocation-scoped media after
            # the response; retaining it would leak one public root per task.
            shutil.rmtree(staged_public_root, ignore_errors=True)


def capture_remotion_frames(
    timeline_path: Path,
    assets_path: Path,
    output_dir: Path,
    frame_numbers: Sequence[int],
    *,
    project_dir: Path,
    composition_id: str = DEFAULT_COMPOSITION_ID,
    theme_path: Path | None = None,
    materialized_root: Path | None = None,
    materialized_objects: Mapping[str, str] | None = None,
    compiled_audio: Mapping[str, Any] | None = None,
    timeline_authority: Mapping[str, Any] | None = None,
    staging_parent: Path | None = None,
    frame_resolution: tuple[int, int] | None = None,
    frame_session: PersistentRemotionFrameSession | None = None,
    frame_session_identity: str | None = None,
) -> dict[int, Path]:
    """Capture selected composition frames through the trusted Remotion path.

    This is deliberately a batch API: the caller supplies the exact frame set
    and Remotion renders one image sequence, avoiding a full video and keeping
    all media/effect/font staging on the existing renderer authority path.
    """
    numbers = tuple(sorted(set(frame_numbers)))
    if not numbers:
        raise ValueError("frame capture requires at least one frame")
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob("frame-*.png"):
        stale.unlink(missing_ok=True)
    marker = output_dir / ".capture-placeholder.mp4"
    try:
        with remotion_lock.remotion_render_lock():
            _execute_remotion_locked(
                Path(timeline_path),
                Path(assets_path),
                marker,
                provenance_out_path=marker,
                project_dir=Path(project_dir),
                composition_id=composition_id,
                theme_path=theme_path,
                min_free_gb=None,
                materialized_root=materialized_root,
                staging_parent=staging_parent,
                materialized_objects=materialized_objects,
                compiled_audio=compiled_audio,
                timeline_authority=timeline_authority,
                frame_numbers=numbers,
                frame_output_dir=output_dir,
                frame_resolution=frame_resolution,
                frame_session=frame_session,
                frame_session_identity=frame_session_identity,
            )
    finally:
        marker.unlink(missing_ok=True)
    result: dict[int, Path] = {}
    for path in output_dir.glob("frame-*.png"):
        match = re.search(r"frame-(\d+)\.png$", path.name)
        if match:
            result[int(match.group(1))] = path
    missing = [frame for frame in numbers if frame not in result]
    if missing:
        raise RuntimeError(f"Remotion frame capture omitted requested frames: {missing}")
    return result


def _settings_from_request(request: RenderRequest, workspace: Path) -> _RenderSettings:
    config = dict(request.backend_config.get(BACKEND_ID, {}))
    _reject_unknown_config(config, _CONFIG_KEYS, BACKEND_ID)

    # The managed task adapter supplies this deployment value both as the
    # selected backend namespace and as the server-owned environment.  Keep
    # the namespace authoritative when present, but retain the environment
    # fallback across the generic-host worker hop; callers cannot set this
    # environment through the public render request.
    project_value = config.get(
        "project_dir",
        os.environ.get("ASTRID_REMOTION_PROJECT_DIR") or (REPO_ROOT / "remotion"),
    )
    if not isinstance(project_value, (str, os.PathLike)):
        raise TypeError("project_dir must be a path string")
    project_dir = _input_path(os.fspath(project_value), workspace)

    composition_value = config.get(
        "composition_id",
        config.get("composition", DEFAULT_COMPOSITION_ID),
    )
    if not isinstance(composition_value, str) or not composition_value.strip():
        raise TypeError("composition_id must be a non-empty string")

    theme_value = config.get("theme_path")
    if theme_value is None:
        theme_path = None
    elif isinstance(theme_value, (str, os.PathLike)):
        candidate = Path(os.fspath(theme_value)).expanduser()
        if not candidate.is_absolute() or candidate.name != "theme.json" or not candidate.is_file():
            raise FileNotFoundError(
                f"theme file not found or invalid: {candidate}; "
                "expected an existing runtime-materialized theme.json file"
            )
        theme_path = candidate.resolve()
    else:
        raise TypeError("theme_path must be a path string or null")

    min_free_gb = _parse_min_free_gb(config.get("min_free_gb"))

    return _RenderSettings(
        project_dir=project_dir,
        composition_id=composition_value,
        theme_path=theme_path,
        min_free_gb=min_free_gb,
    )


def support(request: RenderRequest, *, workspace: Path) -> SupportReport:
    """Return request-specific evidence for the timeline Remotion can render."""

    reasons: list[str] = []
    features: dict[str, bool | str] = {
        "timeline_composition": True,
        "full_timeline": True,
        "windows": False,
        "effects": True,
        "asset_serving": "invocation-scoped",
        "alpha_output": True,
    }
    try:
        settings = _settings_from_request(request, workspace)
    except (TypeError, ValueError) as exc:
        settings = _RenderSettings(
            project_dir=REPO_ROOT / "remotion",
            composition_id=DEFAULT_COMPOSITION_ID,
            theme_path=None,
            min_free_gb=None,
        )
        reasons.append(str(exc))

    if request.window is not None:
        reasons.append("rendering.remotion accepts complete timelines, not native frame windows")

    timeline_path = _input_path(request.timeline_path, workspace)
    assets_path = (
        _input_path(request.assets_registry_path, workspace)
        if request.assets_registry_path is not None
        else None
    )
    timeline_data: dict[str, Any] | None = None
    assets_data: dict[str, Any] | None = None
    try:
        from astrid.core.rendering.compiled_audio import attach_compiled_audio
        timeline_data = attach_compiled_audio(json.loads(timeline_path.read_text(encoding="utf-8")), request.compiled_audio, request.timeline_authority, renderer_config=_serialize_timeline(timeline_path))
    except Exception as exc:  # noqa: BLE001 - support report normalizes renderer failures
        reasons.append(f"timeline is not renderable: {exc}")
    try:
        assets_data = _load_registry_mapping(assets_path)
    except Exception as exc:  # noqa: BLE001 - support report normalizes renderer failures
        reasons.append(f"assets registry is not renderable: {exc}")
    if assets_path is not None and assets_data is not None:
        # Validate local sources during the service's support probe, before a
        # kernel run is admitted.  AssetMaterializer applies the project-root
        # boundary plus the exact kernel-owned managed-media allowlist; it
        # stages only in its disposable probe directory and closes immediately.
        try:
            with AssetMaterializer(
                assets_path,
                materialized_objects=request.materialized_objects,
                materialized_root=request.materialized_root,
                allow_derived_files=request.materialized_root is not None,
            ):
                pass
        except Exception as exc:  # noqa: BLE001 - support report normalizes registry failures
            reasons.append(f"local assets are not renderable: {exc}")

    if timeline_data is not None and assets_data is not None:
        registered_assets = assets_data.get("assets", {})
        missing_asset_ids = sorted(
            {
                str(clip.get("asset"))
                for clip in timeline_data.get("clips", [])
                if isinstance(clip, dict)
                and isinstance(clip.get("asset"), str)
                and clip.get("asset") not in registered_assets
            }
        )
        if missing_asset_ids:
            reasons.append("timeline references missing asset ids: " + ", ".join(missing_asset_ids))
        dynamic_clip_types = sorted(
            {
                str(clip.get("clipType"))
                for clip in timeline_data.get("clips", [])
                if isinstance(clip, dict)
                and clip.get("clipType", "media") not in _BUILTIN_MEDIA_CLIP_TYPES
            }
        )
        if dynamic_clip_types:
            try:
                effects, aliases = _effect_registry_for_assets(settings.theme_path)
            except Exception as exc:  # noqa: BLE001 - support report normalizes registry failures
                reasons.append(f"Remotion element registry cannot be resolved: {exc}")
            else:
                unknown_clip_types = [
                    clip_type
                    for clip_type in dynamic_clip_types
                    if clip_type not in effects and clip_type not in aliases
                ]
                if unknown_clip_types:
                    reasons.append(
                        "timeline uses unregistered Remotion clip types: "
                        + ", ".join(unknown_clip_types)
                    )
        try:
            _resolve_timeline_element_references(
                timeline_data,
                theme_path=settings.theme_path,
            )
        except Exception as exc:  # noqa: BLE001 - support report normalizes render failures
            reasons.append(str(exc))
        try:
            canonical = _canonical_profile(timeline_path, assets_data, settings.theme_path)
        except Exception as exc:  # noqa: BLE001 - support report normalizes profile failures
            reasons.append(f"canonical Remotion profile cannot be resolved: {exc}")
        else:
            # Remotion ALWAYS muxes an audio track (silent when the timeline
            # has none) and always muxes at the 90 kHz timescale; support must
            # describe the same contract render() implements.
            features["audio_ownership"] = AudioOwnership.RENDERED.value
            if request.audio is not None and request.audio is not AudioOwnership.RENDERED:
                reasons.append(
                    f"audio={request.audio.value!r} is incompatible with "
                    f"Remotion's always-rendered audio output"
                )
            if request.profile is not None:
                # The profile Remotion produces depends on the alpha stamp:
                # z>0 layer timelines render ProRes 4444/yuva444p12le/MOV,
                # everything else the frozen H.264/yuv420p/MP4 contract.
                alpha = _timeline_alpha(timeline_data)
                render_profile = _remotion_mux_profile(canonical, alpha=alpha)
                mismatches = _profile_mismatches(request.profile, render_profile)
                if mismatches:
                    reasons.append(
                        "requested profile is not produced by Remotion: " + "; ".join(mismatches)
                    )

    try:
        _validate_project_dir(settings.project_dir)
    except (FileNotFoundError, OSError) as exc:
        reasons.append(str(exc))

    return SupportReport(
        schema_version=SCHEMA_VERSION,
        supported=not reasons,
        reasons=reasons,
        features=features,
        alternatives=[],
        backend=BACKEND_ID,
        backend_version=BACKEND_VERSION,
    )


def _protocol_render(request: RenderRequest, *, workspace: Path) -> RenderResult:
    report = support(request, workspace=workspace)
    if not report.supported:
        raise_unsupported_error(
            backend=BACKEND_ID,
            message="Remotion does not support this render request",
            recovery_command="resolve the reported support reasons and retry",
            details={"reasons": report.reasons, "features": report.features},
        )

    settings = _settings_from_request(request, workspace)
    timeline_path = _input_path(request.timeline_path, workspace)
    requested_assets_path = (
        _input_path(request.assets_registry_path, workspace)
        if request.assets_registry_path is not None
        else None
    )
    outputs_dir = workspace / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    # The service hardcodes ``segment-NNNN.mp4`` (service.py:1362) but
    # Remotion rejects .mp4 output names for --codec=prores; remap the actual
    # artifact to .mov when the alpha stamp is present.  Every downstream
    # path (staged video, declared artifact, provenance) derives from the
    # remapped name so the artifact's declared path points at the real file.
    alpha = _timeline_alpha(_serialize_timeline(timeline_path))
    output_name = _alpha_output_name(request.output_name) if alpha else request.output_name
    output_path = outputs_dir / output_name

    with ExitStack() as lifecycle:
        if requested_assets_path is None:
            empty_assets_tmp = lifecycle.enter_context(
                TemporaryDirectory(prefix=".remotion-empty-assets-", dir=str(workspace))
            )
            assets_path = Path(empty_assets_tmp) / "assets.json"
            timeline.save_registry({"assets": {}}, assets_path)
        else:
            assets_path = requested_assets_path
        assets_data = _load_registry_mapping(assets_path)
        canonical = _canonical_profile(timeline_path, assets_data, settings.theme_path)
        # The declared profile must match what ffprobe sees on the real
        # artifact: alpha-stamped timelines render ProRes 4444/yuva444p12le
        # in MOV, everything else stays H.264/yuv420p in MP4 (strict
        # validation compares every field against the probed file).
        review = (
            json.loads(request.metadata["review"])
            if "review" in request.metadata
            else None
        )
        render_scale = None
        profile_for_output = request.profile or canonical
        if request.profile is None and review is not None:
            profile_for_output, render_scale = _review_output_profile(canonical, alpha=alpha)
        declared_profile = (
            profile_for_output
            if render_scale is not None
            else _remotion_mux_profile(profile_for_output, alpha=alpha)
        )
        # Remotion keeps the authored composition dimensions in
        # ``useVideoConfig()`` when ``--scale`` is used.  Carry the actual
        # emitted dimensions alongside review props so the overlay can make
        # its low-resolution badge decision from the artifact dimensions.
        if isinstance(review, dict):
            review = {
                **review,
                "render_dimensions": {
                    "width": declared_profile.width,
                    "height": declared_profile.height,
                },
            }
        # Remotion always muxes an audio track into its output (silent when
        # the timeline has none), so ownership is effectively 'rendered'.
        ownership = AudioOwnership.RENDERED
        descriptor, staged_name = tempfile.mkstemp(
            prefix=f".{output_name}.remotion-stage-",
            suffix=Path(output_name).suffix,
            dir=str(outputs_dir),
        )
        os.close(descriptor)
        staged_video = Path(staged_name)
        staged_video.unlink(missing_ok=True)
        try:
            details = _execute_remotion(
                timeline_path,
                assets_path,
                staged_video,
                provenance_out_path=output_path,
                project_dir=settings.project_dir,
                composition_id=settings.composition_id,
                theme_path=settings.theme_path,
                min_free_gb=settings.min_free_gb,
                review=review,
                render_scale=render_scale,
                materialized_root=request.materialized_root,
                staging_parent=workspace,
                materialized_objects=request.materialized_objects,
                compiled_audio=request.compiled_audio,
                timeline_authority=request.timeline_authority,
            )
            os.replace(staged_video, output_path)
        finally:
            staged_video.unlink(missing_ok=True)

    try:
        backend_provenance = _render_provenance_payload(
            project_dir=settings.project_dir,
            composition_id=settings.composition_id,
            theme_path=settings.theme_path,
            active_theme=details.active_theme,
            registry_state=details.registry_state,
            stage_summary=details.stage_summary,
            runtime=details.runtime or None,
            active_pack_order=_active_pack_order_for_provenance(),
        )
        video = VideoArtifact.from_file(
            path=output_path,
            workspace_root=workspace,
            profile=declared_profile,
            duration_frames=_duration_frames(output_path, declared_profile),
            audio=ownership,
        )
        result = RenderResult(
            schema_version=SCHEMA_VERSION,
            video=video,
            audio_ownership=ownership,
            backend_fragments={
                BACKEND_ID: {
                    "renderer": "remotion",
                    "renderer_version": BACKEND_VERSION,
                    "composition": settings.composition_id,
                    **backend_provenance,
                    "review": request.metadata.get("review"),
                    "review_scale": render_scale,
                }
            },
            normalization=[],
            logs=[],
            metadata=request.metadata,
        )
        validate_render_result(
            result,
            expected_profile=declared_profile,
            workspace_root=workspace,
        )
        return result
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise


def _load_request(path: Path) -> RenderRequest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("render request must contain a JSON object")
    return RenderRequest.from_dict(payload).for_backend(BACKEND_ID)


def _write_failure(result_path: Path, exc: BaseException, *, kind: str) -> None:
    if isinstance(exc, RendererException):
        error_kind = exc.error.kind
        message = exc.error.message
        recovery = exc.error.recovery_command
        details = exc.error.details
    else:
        error_kind = kind
        message = str(exc) or type(exc).__name__
        recovery = None
        details = {"error_type": type(exc).__name__}
    error = make_renderer_error(
        error_kind,
        backend=BACKEND_ID,
        message=message,
        recovery_command=recovery,
        details=details,
    )
    write_json_atomic(result_path, error.to_dict())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("verb", choices=("render", "support"))
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        request_path = args.request.resolve(strict=True)
        result_path = args.result.resolve()
        if request_path == result_path:
            raise ValueError("--request and --result must be different paths")
        request = _load_request(request_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, RendererException) as exc:
        _write_failure(args.result.resolve(), exc, kind="protocol")
        return 0

    try:
        workspace = request_path.parent
        response: RenderResult | SupportReport
        if args.verb == "support":
            response = support(request, workspace=workspace)
        else:
            response = _protocol_render(request, workspace=workspace)
        write_json_atomic(result_path, response.to_dict())
    except RendererException as exc:
        _write_failure(result_path, exc, kind=exc.error.kind)
    except FileNotFoundError as exc:
        _write_failure(result_path, exc, kind="binary_missing")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        _write_failure(result_path, exc, kind="protocol")
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        _write_failure(result_path, exc, kind="internal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BACKEND_ID",
    "BACKEND_VERSION",
    "DEFAULT_COMPOSITION_ID",
    "main",
    "render",
    "support",
]
