"""Process-isolated generic executor host for the local runtime protocol.

This module deliberately contains no runtime/database/storage imports.  It is a
pack-side process: discovery and execution happen here, while admission,
leases, reservations, and settlement remain protocol operations on the daemon.
"""

from __future__ import annotations

import argparse
import base64
import copy
import faulthandler
import hashlib
import heapq
import hmac
import importlib.util
import io
import ipaddress
import json
import logging
import math
import mimetypes
import os
import queue
import select
import re
import secrets as secrets_module
import shutil
import signal
import socket
import stat
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence
from urllib.parse import quote, urlsplit

# Startup observations use only the existing private stderr sink. Regular-file
# latency is not bounded; this diagnostic never changes descriptor flags.
_STARTUP_OBSERVATION_ENABLED = __name__ == "__main__" and "--ready-file" in sys.argv
_STARTUP_OBSERVATION_STARTED = time.monotonic()
_STARTUP_OBSERVATION_PHASES = frozenset({
    "module_import_begin", "module_import_done", "cli_args_parsed",
    "startup_identity_begin", "startup_identity_done",
    "host_constructor_begin", "host_constructor_done", "discovery_begin",
    "discovery_done", "preflight_begin", "preflight_done",
    "registration_begin", "registration_done", "ready_written",
})


def _emit_startup_phase(phase: str) -> None:
    """One best-effort fixed row to an existing owner-only regular fd2.

    A file write can stall before startup/cleanup. No nonblocking guarantee,
    new sink, retry, descriptor flag change or diagnostic thread is implied.
    """
    if not _STARTUP_OBSERVATION_ENABLED or type(phase) is not str or phase not in _STARTUP_OBSERVATION_PHASES:
        return
    try:
        info = os.fstat(2)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            return
        elapsed = max(0, int((time.monotonic() - _STARTUP_OBSERVATION_STARTED) * 1000))
        row = {"diagnostic": "astrid.host_startup_phase", "phase": phase,
               "process_id": os.getpid(), "elapsed_ms": elapsed}
        encoded = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        if len(encoded) <= 512:
            os.write(2, encoded)
    except Exception:
        # Observation must never mask the original startup branch/exception.
        pass


_emit_startup_phase("module_import_begin")

from astrid.core._shared.capability_common import _output_value, _stringify_value
from astrid.core._shared.result_manifest import (
    HarvestError,
    harvest_staged_outputs,
    outputs_required,
)
from astrid.core.contracts.binding import (
    BindingError,
    assert_provided_inputs_bound,
    expand_command,
)
from astrid.core.contracts.errors import AstridError
from astrid.core.env_vars import (
    ASTRID_INTERNAL_INVOCATION,
    ASTRID_PACKS_PATH,
)
from astrid.core.execution.capability_ledger import load_capability_ledger
from astrid.core.execution.host_log import start_stdio_rotator
from astrid.core.execution.guards import (
    EVIDENCE_STATUS_NAME,
    EvidenceCapError,
    ExecutionGuardError,
    ExecutionGuardPolicy,
    write_evidence_status,
)
from astrid.core.execution.managed_inputs import managed_file_digest
from astrid.core.execution.declared_executables import (
    ExecutableAdmission, ExecutableAdmissionError, ExecutableBinding,
)
from astrid.core.execution.managed_tool_session import (
    SessionCapacityError,
    CapabilityDescriptor,
    AdmissionToken,
    StaleAdmissionError,
    ManagedToolSession,
    SessionBinding,
)
from astrid.core.execution.process_group import (
    CLEANUP_LATCH_NAME,
    CleanupUncertainError,
    _process_snapshot,
    observe_tree,
    popen_owned_group,
    terminate_tree,
    verify_tree_absent,
    write_cleanup_latch,
)
from astrid.core.execution.process_group import (
    group_exists as _owned_group_exists,
)
from astrid.core.execution.process_group import (
    process_birth_identity as _process_birth_identity,
)
from astrid.core.execution.process_group import (
    release_group as _release_owned_group,
)
from astrid.core.execution.process_group import (
    signal_group as _signal_owned_group,
)
from astrid.core.execution.process_group import (
    terminate_group as _terminate_owned_group,
)
from astrid.core.execution.provider_failure import ProviderFailure, PROVIDER_FAILURE_FILE, provider_failure
from astrid.core.execution.provider_route_grant import (
    ProviderRouteGrantAuthority,
    ProviderRouteGrantError,
)
from astrid.core.execution.thumbnails import (
    THUMBNAIL_RECIPE_VERSION,
    ThumbnailError,
    extract_thumbnail,
    is_visual_media_type,
)
from astrid.core.generation.model_root import (
    MODEL_ROOT_BINDING_ENV,
    MODEL_ROOT_ENV,
    MODEL_ROOT_QUALIFICATION_RECEIPT_DIR_ENV,
    ModelRootBinding,
    ModelRootBindingError,
    model_root_binding_from_profile,
)
from astrid.core.generation.vibecomfy_dependency import (
    VIBECOMFY_ATTESTED_CONTENT_DIGEST_ENV,
    VIBECOMFY_ATTESTED_REVISION_ENV,
    configured_vibecomfy_checkout,
    dependency_pythonpath,
)
from astrid.core.subprocess_env import build_child_subprocess_env
from astrid.core.util.secrets import load_local_api_key_with_source
from astrid.sdk.execution_request import normalize_execution_request
from astrid.sdk.workspace_client import WorkspaceClientError, validate_runtime_endpoint

if TYPE_CHECKING:
    from astrid.core.execution.executor.schema import ExecutorDefinition


class HostError(RuntimeError):
    """A controlled host-side error suitable for a worker diagnostic."""


class HostCancelled(HostError):
    """The runtime cancelled the attempt while the subprocess was running."""


_VIDEO_SUFFIX_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
}
_SUFFIX_MEDIA_TYPES = {
    **_VIDEO_SUFFIX_MEDIA_TYPES,
    ".wav": "audio/wav",
}
_ACTIVATION_VERSION = "runtime.local-worker-activation/v1"
_ACTIVATION_ACCEPTED_VERSION = "astrid.local-worker-activation-accepted/v1"
_ACTIVATION_RECEIPT_MODE = "runtime-owner-receipt/v1"
_ACTIVATION_BOOTSTRAP_MODE = "runtime-worker-bootstrap/v1"
_ACTIVATION_REQUEST_VERSION = "astrid.local-worker-activation-request/v1"
_ACTIVATION_RECEIPT_VERSION = "runtime.local-worker-activation-recorded/v1"
_ACTIVATION_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ACTIVATION_FRAME_LIMIT = 64 * 1024

_SETTLEMENT_OUTPUT_METADATA_FIELDS = (
    "metadata",
    "role",
    "is_primary",
    "producer",
    "provenance",
    "durability",
    "regeneration",
    "coverage",
)

_RUNTIME_OUTPUT_NAMESPACES = frozenset(
    (
        "images",
        "videos",
        "video-windows",
        "audio",
        "tiles",
        "frames",
        "cache",
        "segments",
        "outputs",
        "artifacts",
        "agent-view",
        "filmstrip-view",
    )
)


# Runtime limits (Runtime/runtime_protocol/server.py MAX_BODY_BYTES and
# service.py OBJECT_MAX_BYTES). Outputs above INLINE_OUTPUT_MAX_BYTES, or
# beyond the inline budget, are uploaded as objects before settlement.
RUNTIME_OBJECT_LIMIT_BYTES = 5 * 1024 ** 3
INLINE_SETTLEMENT_BUDGET_BYTES = 40 * 1024 * 1024
INLINE_OUTPUT_MAX_BYTES = 8 * 1024 * 1024


def _settlement_media_type(descriptor: Mapping[str, Any]) -> str:
    """Publish a MIME media type while retaining internal artifact semantics."""

    explicit = descriptor.get("media_type")
    if isinstance(explicit, str) and explicit.strip():
        explicit = explicit.strip()
        if explicit.lower() != "application/octet-stream":
            return explicit
    artifact_type = str(descriptor.get("artifact_type") or "")
    filename = descriptor.get("filename")
    if isinstance(filename, str) and filename:
        suffix_media_type = _SUFFIX_MEDIA_TYPES.get(Path(filename).suffix.lower())
        if suffix_media_type is not None:
            return suffix_media_type
        guessed_media_type = mimetypes.guess_type(filename)[0]
        if guessed_media_type and guessed_media_type.lower() != "application/octet-stream":
            return guessed_media_type
    return artifact_type or "application/octet-stream"


def _runtime_output_filename(value: str) -> str:
    """Map a safe staged output name to Runtime's direct-leaf wire name.

    Producers keep known namespaces in their private attempt spool (for
    example ``images/output_000.png`` or ``agent-view/structure.md``).
    Runtime's managed output contract carries only a direct filename, so
    strip exactly one known producer namespace at the upload boundary.
    Arbitrary nesting is not a filename mapping mechanism and remains
    rejected.
    """
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(char) < 32 for char in value)
        or "\\" in value
        or Path(value).is_absolute()
        or ".." in Path(value).parts
        or Path(value).as_posix() != value
    ):
        raise HostError("generated output has an invalid managed filename")
    parts = Path(value).parts
    if not parts or parts[-1] in {".", ".."}:
        raise HostError("generated output has an invalid managed filename")
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2 and parts[0] in _RUNTIME_OUTPUT_NAMESPACES:
        return parts[1]
    raise HostError("generated output has an invalid managed filename")


def _generation_output_port(record: Any, intent: Mapping[str, Any] | None) -> str | None:
    """Resolve the primary generated output port from the admitted schema."""
    if not isinstance(intent, Mapping):
        return None
    modality = intent.get("modality")
    if modality not in {"image", "video", "audio"}:
        return None
    expected = {"image": "generated_images", "video": "generated_videos", "audio": "generated_audio"}[modality]
    definition = getattr(record, "definition", None)
    metadata = getattr(definition, "metadata", {})
    publication = metadata.get("generation_publication") if isinstance(metadata, Mapping) else None
    if isinstance(publication, Mapping) and "output_port" in publication:
        port = publication["output_port"]
        selected = [output for output in (getattr(definition, "outputs", ()) or ()) if output.name == port]
        if (type(publication.get("version")) is not int or publication["version"] != 1
                or publication.get("modality") != modality
                or not isinstance(port, str) or not port.strip() or port != port.strip()
                or len(selected) != 1 or selected[0].type != "file"
                or port == "manifest" or port.endswith("_manifest")
                or getattr(selected[0], "artifact_type", None) == "metadata/result-manifest"):
            raise HostError("generation publication has an invalid explicit output declaration")
        return port
    if publication is not None and not isinstance(publication, Mapping):
        raise HostError("generation publication metadata must be a mapping")

    candidates = [
        output.name for output in (getattr(getattr(record, "definition", None), "outputs", ()) or ())
        if getattr(output, "type", None) == "file"
        and not str(getattr(output, "name", "")).endswith("_manifest")
        and getattr(output, "artifact_type", None)
    ]
    if candidates.count(expected) == 1:
        return expected
    if len(candidates) == 1:
        return candidates[0]
    untyped_candidates = [
        output.name for output in (getattr(getattr(record, "definition", None), "outputs", ()) or ())
        if getattr(output, "type", None) == "file"
        and not str(getattr(output, "name", "")).endswith("_manifest")
    ]
    if len(untyped_candidates) == 1:
        return untyped_candidates[0]
    raise HostError(
        f"generation capability must declare exactly one primary {modality!r} output"
    )


def _generation_selector_declarations(
    record: Any,
    intent: Mapping[str, Any] | None,
) -> tuple[str | None, tuple[dict[str, Any], ...]]:
    """Flatten admitted selector declarations without changing their order."""
    output_port = _generation_output_port(record, intent)
    if output_port is None:
        return None, ()
    declarations: list[dict[str, Any]] = []
    for group in intent["groups"]:
        for selector in group["selectors"]:
            declarations.append({
                "group_key": group["group_key"],
                "selector": selector["selector"],
                "ordinal": selector["ordinal"],
                "variant_key": selector["variant_key"],
                "output_port": output_port,
                **({"required": selector["required"]} if "required" in selector else {}),
            })
    return output_port, tuple(declarations)


class StorageEnvelopeError(HostError):
    """A live attempt exceeded its admitted scratch/output envelope."""

    def __init__(self, message: str, *, diagnostic: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.diagnostic = dict(diagnostic)


def _cleanup_ephemeral_attempt(root: Path) -> None:
    """Remove an owned attempt root and verify that no residue remains."""
    try:
        shutil.rmtree(root)
    except FileNotFoundError:
        if _strict_root_exists(root):
            raise HostError(f"owned attempt cleanup was not verified: {root}")
        return
    except OSError as exc:
        raise HostError(f"owned attempt cleanup failed: {root}") from exc
    if _strict_root_exists(root):
        raise HostError(f"owned attempt cleanup was not verified: {root}")


def _strict_root_exists(root: Path) -> bool:
    """Observe an owned root without suppressing filesystem errors."""
    try:
        root.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise HostError(f"owned attempt observation failed: {root}") from exc
    return True


class _ManagedTaskAdapter:
    """Host-owned lifecycle adapter for one claimed task.

    Engine-specific adapters may add stronger process/session custody, but the
    generic host still needs a concrete fence/release surface around every
    claimed task.  The manager's token is therefore part of the task's
    completion proof even for CPU executors.
    """

    def __init__(
        self,
        cancel_signal: threading.Event,
        process_census: Callable[[], bool] | None = None,
    ) -> None:
        self._cancel_signal = cancel_signal
        self._process_census = process_census
        self.fenced = False

    def fence(self, *, reason: str) -> dict[str, Any]:
        self.fenced = True
        self._cancel_signal.set()
        return {"ok": True, "fenced": True, "reason": reason}

    def cancel(self, *, reason: str) -> dict[str, Any]:
        self._cancel_signal.set()
        if self._process_census is not None and self._process_census():
            return {
                "ok": False,
                "cancelled": False,
                "reason": reason,
                "active_processes": True,
            }
        return {"ok": True, "cancelled": True, "reason": reason}

    def release(self, *, reason: str) -> dict[str, Any]:
        if self._process_census is not None and self._process_census():
            return {"ok": False, "released": False, "reason": reason, "active_processes": True}
        return {"ok": True, "released": True, "reason": reason}


class _ManagedVibeSessionAdapter:
    """Bridge the manager lifecycle to the reviewed checkout adapter."""

    def __init__(self, backend: Any, native_graph: Any = None) -> None:
        self.backend = backend
        self._captured_backend = backend
        self.native_graph = native_graph
        self._captured_native_graph = native_graph
        self.cpu_handoff_identity: tuple[Any, ...] | None = None
        self.cpu_handoff_token: AdmissionToken | None = None
        self.cpu_handoff_failed = False
        # SDK-launched hosts do not own the schema creator's native graph.
        # This is a residency-control binding, never a process capability.
        self.sdk_session_identity: str | None = None
        self._sdk_session_bound = False

    @classmethod
    def from_host_session(cls, *, native_graph: Any = None, **binding: Any) -> Any:
        from astrid.core.generation.backends.vibecomfy import CheckoutServerAdapter

        backend = CheckoutServerAdapter.from_host_session(**binding)
        adapter = cls(backend, native_graph)
        if native_graph is None:
            adapter.sdk_session_identity = adapter._sdk_session_identity()
            if adapter.sdk_session_identity is None:
                raise HostError("verified SDK residency binding is incomplete")
            adapter._sdk_session_bound = True
        return adapter

    def _sdk_session_identity(self) -> str | None:
        from astrid.core.generation.backends.vibecomfy import CheckoutServerAdapter

        backend = self.backend
        if not isinstance(backend, CheckoutServerAdapter):
            return None
        session = backend._host_session
        profile = backend._host_profile
        if (
            not isinstance(session, Mapping) or not session
            or not isinstance(profile, Mapping)
            or profile.get("schema_version") != "hc03-worker-readiness.v1"
            or profile.get("status") != "ready"
            or not backend._bound_fingerprint
            or not backend._bound_model_id or not backend._bound_template_id
            or not backend._invocation_identity
        ):
            return None
        return _canonical_digest({
            "session": session,
            "profile": profile,
            "model_id": backend._bound_model_id,
            "template_id": backend._bound_template_id,
            "fingerprint": backend._bound_fingerprint,
            "invocation_identity": backend._invocation_identity,
            "runtime_instance_id": backend._runtime_instance_id,
        })

    def residency_identity(self) -> Any:
        if self.backend is not self._captured_backend:
            return None
        if self._captured_native_graph is not None:
            return (self._captured_native_graph
                    if self.native_graph is self._captured_native_graph else None)
        if (self.sdk_session_identity is not None
                and self._sdk_session_identity() == self.sdk_session_identity):
            return ("verified_sdk_session", self.sdk_session_identity)
        return None

    @property
    def residency_bound(self) -> bool:
        return self._captured_native_graph is not None or self._sdk_session_bound

    @staticmethod
    def _native_evidence(
        evidence: Any,
        *,
        kind: str,
        native_key: str,
        reason: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(evidence, Mapping)
            or evidence.get("ok") is not True
            or evidence.get(native_key) is not True
        ):
            return {
                "ok": False,
                kind: False,
                "reason": reason,
                "native": dict(evidence) if isinstance(evidence, Mapping) else evidence,
            }
        return {"ok": True, kind: True, "reason": reason, "native": dict(evidence)}

    def observe(self, *, binding: SessionBinding) -> dict[str, Any]:
        del binding
        self.backend._revalidate_host_session()
        return {"ok": True, "observed": True}

    def fence(self, *, reason: str) -> dict[str, Any]:
        try:
            if reason == "cpu_residency_eviction" and self.residency_identity() is None:
                raise HostError("native CPU handoff session identity changed")
            evidence = self.backend.cancel()
            result = self._native_evidence(
                evidence, kind="fenced", native_key="cancelled", reason=reason
            )
            if reason == "cpu_residency_eviction" and result["ok"] is not True:
                raise HostError("native CPU handoff fencing was not verified")
            return result
        except BaseException:
            if reason == "cpu_residency_eviction":
                self.cpu_handoff_failed = True
            raise

    def cancel(self, *, reason: str) -> dict[str, Any]:
        evidence = self.backend.cancel()
        return self._native_evidence(
            evidence, kind="cancelled", native_key="cancelled", reason=reason
        )

    def release(self, *, reason: str) -> dict[str, Any]:
        if reason == "capacity_replacement" and self.residency_bound:
            raise HostError("native engine replacement requires a fresh Runtime-selected preparation; custody retained")
        if reason == "cpu_residency_eviction" and (
            self.residency_identity() is None or self.cpu_handoff_failed
        ):
            raise HostError("native CPU handoff release remains unresolved")
        try:
            evidence = self.backend.release(reason=reason)
            result = self._native_evidence(
                evidence, kind="released", native_key="released", reason=reason
            )
            if reason == "cpu_residency_eviction" and result["ok"] is not True:
                self.cpu_handoff_failed = True
            return result
        except BaseException:
            if reason == "cpu_residency_eviction":
                self.cpu_handoff_failed = True
            raise


class _ManagedWanChildAdapter:
    """One MTS-owned interpreter, using the existing owned-process/JSONL seam."""

    MAX_FRAME_BYTES = 1048576

    def __init__(self, *, spec: Mapping[str, Any], binding: SessionBinding,
                 readiness_timeout: float, release_timeout: float,
                 track: Callable[[subprocess.Popen], None],
                 untrack: Callable[[subprocess.Popen], None]) -> None:
        self.spec = dict(spec)
        self.binding = binding
        self.readiness_timeout = readiness_timeout
        self.release_timeout = release_timeout
        self.track = track
        self.untrack = untrack
        self.process: subprocess.Popen | None = None
        self.ready = False
        self.fenced = threading.Event()
        self._buffer = bytearray()
        self._command_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._active_identity: dict[str, Any] | None = None
        self._native_job_id: str | None = None
        self._cancel_requested = False
        self._terminal_frame: dict[str, Any] | None = None
        self._owner_lock: Any = None
        self._diagnostic: Any = None

    def start(self) -> None:
        # The native creation-control experiment below supplies a narrower
        # real-launch policy, not an admitted engine or crash-recovery proof.
        # In particular, losing the designated cleanup actor still prevents
        # safe settlement/reacquisition. Keep the original descendant canaries
        # and refusal until those independent obligations are demonstrated.
        raise HostError("retained Wan launch has no compatible confinement descriptor contract")

    def _write(self, frame: Mapping[str, Any], deadline: float) -> None:
        data = json.dumps(frame, sort_keys=True).encode() + b"\n"
        if len(data) > self.MAX_FRAME_BYTES:
            raise HostError("Wan control frame exceeds bound")
        if self.process is None or self.process.stdin is None:
            raise HostError("Wan child is absent")
        with self._write_lock:
            fd = self.process.stdin.fileno()
            while data:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Wan child write timed out")
                if self.process.poll() is not None:
                    raise HostError("Wan child exited before command")
                if select.select([], [fd], [], min(0.05, remaining))[1]:
                    try:
                        data = data[os.write(fd, data):]
                    except BlockingIOError:
                        pass

    def _read(self, deadline: float) -> dict[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise HostError("Wan child is absent")
        while b"\n" not in self._buffer:
            if self.fenced.is_set():
                raise HostError("Wan invocation cancelled or fenced")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Wan child observation timed out")
            fd = self.process.stdout.fileno()
            if select.select([fd], [], [], min(0.05, remaining))[0]:
                data = os.read(fd, 65536)
                if not data:
                    raise HostError("Wan child exited without terminal evidence")
                self._buffer.extend(data)
                if len(self._buffer) > self.MAX_FRAME_BYTES:
                    raise HostError("Wan child frame exceeds bound")
        line, _, remainder = self._buffer.partition(b"\n")
        self._buffer = bytearray(remainder)
        frame = json.loads(line)
        if not isinstance(frame, dict) or frame.get("birth") != self.binding.process_birth_id:
            raise HostError("stale Wan child session frame")
        return frame

    @staticmethod
    def _event_progress(event: Mapping[str, Any]) -> dict[str, Any] | None:
        if event.get("kind") != "progress":
            return None
        data = event.get("data")
        if not isinstance(data, Mapping):
            return None
        progress: dict[str, Any] = {}
        for key in ("phase", "status"):
            if data.get(key) is not None:
                progress[key] = str(data[key])
        for key in ("progress", "current_step", "total_steps"):
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                progress[{"progress": "percent", "current_step": "current", "total_steps": "total"}[key]] = value
        return progress or None

    def _record_event(self, frame: Mapping[str, Any], progress_path: Path | None) -> None:
        if frame.get("identity") != self._active_identity:
            raise HostError("stale or unexpected Wan invocation frame")
        native_job_id = frame.get("native_job_id")
        if not isinstance(native_job_id, str) or not native_job_id:
            raise HostError("Wan frame lacks native job identity")
        if frame.get("kind") == "job_started":
            if self._native_job_id is not None:
                raise HostError("duplicate native Wan job start")
            self._native_job_id = native_job_id
        elif native_job_id != self._native_job_id:
            raise HostError("native Wan job identity changed during invocation")
        if progress_path is not None:
            event = frame.get("event")
            if isinstance(event, Mapping):
                progress = self._event_progress(event)
                if progress:
                    progress_path.parent.mkdir(parents=True, exist_ok=True)
                    progress_path.write_text(json.dumps(progress, sort_keys=True), encoding="utf-8")

    @staticmethod
    def identity(token: AdmissionToken) -> dict[str, Any]:
        return {"token_id": token.token_id, "invocation_id": token.invocation_id,
                "session_id": token.session_id,
                "generation": token.generation, "binding_identity": list(token.binding_identity)}

    def invoke(self, token: AdmissionToken, settings: Mapping[str, Any], *, timeout: float,
               cancelled: Callable[[], bool], progress_path: str | Path | None = None) -> dict[str, Any]:
        if not self._command_lock.acquire(blocking=False):
            raise HostError("Wan child already has an executing command")
        try:
            if cancelled() or self.observe(binding=self.binding).get("ok") is not True:
                raise HostError("Wan child is not admissible")
            identity = self.identity(token)
            self._active_identity = identity
            self._native_job_id = None
            self._cancel_requested = False
            self._terminal_frame = None
            progress = Path(progress_path).expanduser().resolve() if progress_path is not None else None
            events: list[dict[str, Any]] = []
            deadline = time.monotonic() + timeout
            self._write({"birth": self.binding.process_birth_id, "op": "run",
                         "identity": identity, "settings": dict(settings)}, deadline)
            while True:
                if cancelled() and not self._cancel_requested:
                    self._send_cancel(identity, deadline)
                    self._cancel_requested = True
                read_deadline = min(deadline, time.monotonic() + 0.1)
                try:
                    frame = self._read(read_deadline)
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        raise
                    # Keep the control pipe responsive while native result()
                    # is still running so a Runtime cancellation can reach
                    # SessionJob.cancel cooperatively.
                    continue
                kind = frame.get("kind")
                if kind in {"job_started", "event", "event_error", "cancel_requested"}:
                    self._record_event(frame, progress)
                    if kind == "event":
                        events.append(dict(frame))
                    continue
                if frame.get("identity") != identity or kind not in {"result", "error"}:
                    raise HostError("stale or unexpected Wan invocation frame")
                if kind == "result" or self._native_job_id is not None:
                    self._record_event(frame, None)
                self._terminal_frame = frame
                result = frame.get("result") if kind == "result" else {
                    "success": False,
                    "errors": [frame.get("error", "native Wan job failed")],
                    "generated_files": [],
                }
                return {**identity, "native_job_id": self._native_job_id,
                        "events": events, "result": result,
                        "birth": self.binding.process_birth_id,
                        "process_id": self.process.pid,
                        # The retained child stays alive; this is the native
                        # command's terminal status, not a child exit code.
                        "returncode": 0 if kind == "result" and isinstance(result, Mapping)
                        and result.get("success") is True else 1,
                        "terminal": kind == "result",
                        "source_root": self.spec["init"]["output_dir"],
                        "output_snapshots": frame.get("output_snapshots"),
                        "native_error": frame.get("error")}
        finally:
            self._active_identity = None
            self._command_lock.release()

    def _send_cancel(self, identity: Mapping[str, Any], deadline: float) -> None:
        self._write({"birth": self.binding.process_birth_id, "op": "cancel",
                     "identity": dict(identity)}, deadline)

    def observe(self, *, binding: SessionBinding) -> dict[str, Any]:
        return {"ok": self.ready and not self.fenced.is_set()
                and binding == self.binding and self.process is not None and self.process.poll() is None}

    def fence(self, *, reason: str) -> dict[str, Any]:
        self.fenced.set()
        return {"ok": True, "fenced": True, "reason": reason}

    def cancel(self, *, reason: str) -> dict[str, Any]:
        identity = self._active_identity
        if identity is None:
            terminal = self._terminal_frame
            result = terminal.get("result") if isinstance(terminal, Mapping) else None
            cancelled = isinstance(result, Mapping) and not result.get("success", False)
            return {"ok": bool(cancelled), "cancelled": bool(cancelled), "reason": reason,
                    "native_job_id": self._native_job_id}
        try:
            self._send_cancel(identity, time.monotonic() + self.release_timeout)
            self._cancel_requested = True
            # The host remains fenced by ManagedToolSession until the native
            # terminal result is observed by the invocation reader.
            return {"ok": False, "cancelled": False, "cancel_requested": True,
                    "reason": reason, "native_job_id": self._native_job_id}
        except (HostError, OSError, TimeoutError):
            self.fence(reason=reason)
            return {"ok": False, "cancelled": False, "reason": reason}

    def release(self, *, reason: str) -> dict[str, Any]:
        self.fence(reason=reason)
        process = self.process
        if process is not None:
            if process.poll() is None:
                try:
                    self._write({"birth": self.binding.process_birth_id, "op": "close"},
                                time.monotonic() + self.release_timeout)
                    process.wait(timeout=self.release_timeout)
                except (OSError, ValueError, HostError, TimeoutError, subprocess.TimeoutExpired):
                    _terminate_process_group(process, grace_seconds=self.release_timeout)
            _release_owned_group(process)
            # Close() or a clean leader exit alone cannot prove group release.
            snapshot = _process_snapshot()
            if not snapshot:
                raise HostError("Wan child release census unavailable")
            if sys.platform == "darwin":
                # _process_snapshot is the bounded, complete native census on
                # Darwin. A second ps process creates an avoidable descendant.
                group_live = any(info.pgid == process.pid for info in snapshot.values())
            else:
                ps = subprocess.run(["/bin/ps", "-axo", "pid=,pgid="], capture_output=True, timeout=1, check=False)
                if ps.returncode != 0:
                    raise HostError("Wan child release census failed")
                members = [row.split() for row in ps.stdout.decode().splitlines()]
                group_live = any(len(row) == 2 and row[1] == str(process.pid) for row in members)
            if process.poll() is None or group_live or any(info.pgid == process.pid for info in snapshot.values()):
                return {"ok": False, "released": False, "reason": reason}
            # Retire the reader before closing/reusing pipe descriptors. The
            # command may be unwinding concurrently with MTS replacement.
            if not self._command_lock.acquire(timeout=self.release_timeout):
                return {"ok": False, "released": False, "reason": reason}
            try:
                self.untrack(process)
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None:
                        pipe.close()
            finally:
                self._command_lock.release()
        if self._diagnostic is not None:
            self._diagnostic.close()
        if self._owner_lock is not None:
            self._owner_lock.close()
            self._owner_lock = None
        self.ready = False
        return {"ok": True, "released": True, "exit_code": process.returncode if process else None}

class HostRegistrationError(HostError):
    """A typed, request-correlated executor registration failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "registration_failed",
        request_id: str = "",
        status: int = 0,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.status = status
        self.details = dict(details or {})


_EXECUTION_FACT_EXACT_KEYS = frozenset(
    {
        "interpreter",
        "runtime_lock",
        "engine_lock",
        "model_digest",
        "custom_node_digest",
        "driver",
        "root",
        "port",
    }
)
_EXECUTION_FACT_MINIMUM_KEYS = frozenset({"vram_bytes", "scratch_bytes"})
_MAX_EXECUTION_FACT_BYTES = (1 << 53) - 1
_EXECUTOR_REFRESH_SECONDS = 30.0
_HOST_OWNED_ENVELOPE_PORTS = (
    "task_spec_json",
    "input_object_paths_json",
    "task_identity",
    "attempt_identity",
    "execution_identity",
    "engine_python",
    "readiness_profile_json",
    "readiness_profile_path",
)
_TYPED_FAMILIES = {
    "z_image_turbo": "z_image_t2i",
}


def _normalize_verified_facts(value: Any) -> dict[str, dict[str, Any]]:
    """Apply the runtime's small, engine-neutral facts wire contract."""
    if not isinstance(value, Mapping):
        raise HostError("readiness profile verified_facts must be an object")
    unknown = set(value) - {"exact", "minimum"}
    if unknown:
        raise HostError(
            "readiness profile verified_facts contains unsupported fields: "
            + ", ".join(sorted(str(item) for item in unknown))
        )
    exact = value.get("exact", {})
    minimum = value.get("minimum", {})
    if not isinstance(exact, Mapping) or not isinstance(minimum, Mapping):
        raise HostError(
            "readiness profile verified_facts exact and minimum must be objects"
        )
    unknown_exact = set(exact) - _EXECUTION_FACT_EXACT_KEYS
    unknown_minimum = set(minimum) - _EXECUTION_FACT_MINIMUM_KEYS
    if unknown_exact or unknown_minimum:
        unknown_facts = sorted(str(item) for item in unknown_exact | unknown_minimum)
        raise HostError(
            "readiness profile verified_facts contains unsupported facts: "
            + ", ".join(unknown_facts)
        )
    normalized_exact: dict[str, Any] = {}
    for key, fact in exact.items():
        if key == "port":
            if (
                isinstance(fact, bool)
                or not isinstance(fact, (str, int))
                or not fact
                or (isinstance(fact, int) and not 0 <= fact <= 65535)
            ):
                raise HostError("readiness profile verified_facts port is invalid")
        elif not isinstance(fact, str) or not fact:
            raise HostError(
                f"readiness profile verified_facts {key} is invalid"
            )
        normalized_exact[str(key)] = fact
    normalized_minimum: dict[str, int] = {}
    for key, fact in minimum.items():
        if (
            isinstance(fact, bool)
            or not isinstance(fact, int)
            or fact < 0
            or fact > _MAX_EXECUTION_FACT_BYTES
        ):
            raise HostError(
                f"readiness profile verified_facts {key} is invalid"
            )
        normalized_minimum[str(key)] = fact
    return {"exact": normalized_exact, "minimum": normalized_minimum}


def _read_readiness_profile_document() -> Mapping[str, Any] | None:
    """Read the Worker-issued readiness document with its hash fence."""
    profile_path = os.environ.get("ASTRID_HOST_READINESS_PROFILE_PATH")
    if not profile_path:
        return None
    try:
        profile_bytes = Path(profile_path).read_bytes()
        expected_hash = os.environ.get("ASTRID_HOST_READINESS_PROFILE_HASH", "")
        actual_hash = "sha256:" + hashlib.sha256(profile_bytes).hexdigest()
        if not expected_hash or expected_hash != actual_hash:
            raise HostError("readiness profile hash does not match the supplied profile")
        profile = json.loads(profile_bytes.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise HostError(f"readiness profile is unreadable: {exc}") from exc
    if not isinstance(profile, Mapping):
        raise HostError("readiness profile is not an object")
    return profile


def _registration_verified_facts() -> dict[str, dict[str, Any]] | dict[str, Any]:
    """Read configured evidence fail-closed; profile-free hosts publish none."""
    profile = _read_readiness_profile_document()
    if profile is None:
        return {}
    if "verified_facts" not in profile:
        raise HostError("readiness profile is missing verified_facts")
    return _normalize_verified_facts(profile["verified_facts"])


def _mapping_value(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        return value
    return None


def _completed_process_evidence(
    *,
    capability_id: str,
    attempt_id: str | None,
    fence: int | None,
    result: Any,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Stamp subprocess identity fail-closed onto a completed settlement."""
    result_payload = _mapping_value(getattr(result, "payload", None))
    process_id = _first_int(
        getattr(result, "process_id", None),
        result_payload.get("process_id"),
        payload.get("process_id"),
    )
    returncode = _first_int(
        getattr(result, "returncode", None),
        result_payload.get("returncode"),
        payload.get("returncode"),
    )
    if process_id is None or process_id <= 0:
        raise HostError("completed capability is missing process evidence process_id")
    if returncode is None:
        raise HostError("completed capability is missing process evidence returncode")
    if returncode != 0:
        raise HostError(f"completed capability process evidence returncode is {returncode}")
    if not attempt_id or fence is None:
        raise HostError("completed capability is missing process evidence attempt identity")
    return {
        "capability_id": capability_id,
        "attempt_id": attempt_id,
        "fence": fence,
        "child_boundary": "subprocess",
        "process_id": process_id,
        "returncode": returncode,
    }


def _bind_host_owned_command_outputs(
    record: "CapabilityRecord", values: dict[str, Any], *, output_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Keep command inputs intact and resolve separate host-owned harvest paths."""
    root = output_root.resolve()
    outputs = record.definition.outputs
    names = {name for output in outputs for name in (output.name, output.placeholder) if name}
    if names & {"out", "run_root", "python_exec"}:
        raise HostError(f"capability {record.id!r}: output binding overlaps a host runtime value")
    effective = dict(values)
    for port in record.definition.inputs:
        if port.default is not None:
            effective.setdefault(port.name, port.default)
    if any(output.name == "video" for output in outputs):
        effective.setdefault("output_name", "hype.mp4")
    declared_inputs = {}
    for port in record.definition.inputs:
        if port.name in effective:
            declared_inputs[port.name] = effective[port.name]
    # Legitimate declared inputs can supply output-template values. Raw caller
    # output-only names/aliases cannot choose destinations. Earlier resolved
    # outputs replace the seed for later templates, matching the runner.
    bound = {name: value for name, value in effective.items() if name not in names}
    bound.update(declared_inputs)
    placeholders = {name: _stringify_value(value) for name, value in bound.items() if value is not None}
    request = SimpleNamespace(out=root, outputs={})
    for output in outputs:
        path = Path(_output_value(output, request, placeholders, error_cls=HostError)).expanduser()
        path = (path if path.is_absolute() else root / path).resolve()
        if not path.is_relative_to(root):
            raise HostError(f"capability {record.id!r}: declared output {output.name!r} escapes output root")
        value = str(path)
        bound[output.name] = placeholders[output.name] = value
        if output.placeholder:
            bound[output.placeholder] = placeholders[output.placeholder] = value
    # The runner restores inputs for command binding but keeps resolved outputs
    # for harvesting. expand_command also derives input aliases from port names.
    return {**bound, **declared_inputs}, bound


def _bind_host_owned_command_values(
    record: "CapabilityRecord",
    values: dict[str, Any],
    *,
    attempt: Path,
    admission: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fill declared host-owned envelope ports the child command interpolates."""
    declared = {port.name for port in record.definition.inputs}
    if "task_spec_json" in declared and not values.get("task_spec_json"):
        params = values.get("params") if isinstance(values.get("params"), Mapping) else None
        if not isinstance(params, Mapping):
            skip = {
                "out",
                "run_root",
                "python_exec",
                "task_identity",
                "attempt_identity",
                *_HOST_OWNED_ENVELOPE_PORTS,
                "input_object_ids",
                "materialized_root",
                "materialized_objects",
                "family",
                "params",
                "output_policy",
            }
            params = {key: item for key, item in values.items() if key not in skip}
        family = values.get("family") or _TYPED_FAMILIES.get(record.id)
        envelope = {
            "capability_id": record.id,
            "input_object_ids": list(
                values.get("input_object_ids")
                or (admission or {}).get("input_object_ids")
                or []
            ),
            "spec": {
                "family": family,
                "params": params,
                "output_policy": values.get("output_policy") or {"artifact_count": 1},
            },
        }
        values["task_spec_json"] = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    if "input_object_paths_json" in declared:
        values.setdefault("input_object_paths_json", "[]")
    if "task_identity" in declared:
        values["task_identity"] = str(
            (admission or {}).get("task_id") or attempt.name
        )
    if "attempt_identity" in declared:
        values["attempt_identity"] = str(
            (admission or {}).get("attempt_id") or attempt.name
        )
    if "execution_identity" in declared:
        values["execution_identity"] = str(
            (admission or {}).get("execution_identity") or "-"
        )
    if "engine_python" in declared:
        values.setdefault("engine_python", sys.executable)
    if "readiness_profile_json" in declared and not values.get("readiness_profile_json"):
        profile_path = os.environ.get("ASTRID_HOST_READINESS_PROFILE_PATH")
        if profile_path and Path(profile_path).is_file():
            values["readiness_profile_json"] = Path(profile_path).read_text(encoding="utf-8")
        else:
            values["readiness_profile_json"] = "{}"
    if "readiness_profile_path" in declared:
        values["readiness_profile_path"] = os.environ.get(
            "ASTRID_HOST_READINESS_PROFILE_PATH"
        ) or "-"
    if "readiness_profile_hash" in declared:
        values["readiness_profile_hash"] = os.environ.get(
            "ASTRID_HOST_READINESS_PROFILE_HASH"
        ) or "-"
    return values


def _prepare_vibecomfy_execution_identity(
    inputs: Mapping[str, Any],
    scratch: Path,
    readiness_profile: Mapping[str, Any] | None,
) -> tuple[str, str, str, Mapping[str, Any]]:
    """Select and identify the exact VibeComfy input form before launch."""
    from astrid.packs.vibecomfy.shared.bundle_inputs import staged_workflow_path
    from astrid.packs.vibecomfy.production_engine import (
        load_workflow_path,
        loaded_workflow_execution_identity,
        loaded_workflow_session_requirements,
    )

    previous_headless = os.environ.get("VIBECOMFY_HEADLESS")
    os.environ["VIBECOMFY_HEADLESS"] = "1"
    try:
        with staged_workflow_path(
            workflow=inputs.get("workflow"),
            python=inputs.get("python"),
            companion=inputs.get("companion"),
            source=inputs.get("source"),
            scratch=scratch,
        ) as (workflow_path, _authority):
            loaded = load_workflow_path(
                workflow_path,
                scratch / "canonical-loader",
            )
            return (
                loaded_workflow_execution_identity(loaded, readiness_profile),
                loaded.model_id,
                loaded.template_id,
                loaded_workflow_session_requirements(loaded),
            )
    except Exception as exc:
        raise HostError(f"vibecomfy.run canonical input preflight failed: {exc}") from exc
    finally:
        if previous_headless is None:
            os.environ.pop("VIBECOMFY_HEADLESS", None)
        else:
            os.environ["VIBECOMFY_HEADLESS"] = previous_headless


def _dependency_pythonpath() -> tuple[str, ...]:
    """Keep only approved dependency roots across children."""
    return dependency_pythonpath()


def _host_product_import_origins() -> dict[str, str | None]:
    # A remote pack host need not have the Runtime daemon package installed.
    # Preserve that absence as well as existing product import identities.
    origins: dict[str, str | None] = {}
    for name in ("astrid", "runtime_protocol"):
        spec = importlib.util.find_spec(name)
        origins[name] = spec.origin if spec is not None else None
    return origins


def _install_host_vibecomfy_import_root() -> Path | None:
    """Expose one attested dependency in the isolated installed CLI process.

    This runs before host construction or task threads. The path remains for
    this process lifetime; task preflight never changes the global import path.
    Installed product roots keep precedence, and ambient PYTHONPATH is ignored.
    """
    if os.environ.get("ASTRID_HOST_EXECUTION_PROFILE") != "installed":
        return None
    raw = os.environ.get("ASTRID_VIBECOMFY_CHECKOUT")
    if not raw:
        return None
    lexical = Path(raw).expanduser()
    if not lexical.is_absolute() or lexical.resolve() != lexical:
        raise HostError("installed host VibeComfy checkout must be canonical without aliases")
    profile = _read_readiness_profile_document()
    candidate = profile.get("vibecomfy_candidate") if isinstance(profile, Mapping) else None
    session = profile.get("vibecomfy_session") if isinstance(profile, Mapping) else None
    if not isinstance(candidate, Mapping) or not isinstance(session, Mapping):
        raise HostError("installed host VibeComfy checkout requires attested readiness")
    revision = session.get("source_revision")
    content = session.get("source_content_digest")
    if (candidate.get("checkout") != str(lexical)
            or candidate.get("revision") != revision
            or candidate.get("source_content_digest") != content
            or os.environ.get(VIBECOMFY_ATTESTED_REVISION_ENV) != revision
            or os.environ.get(VIBECOMFY_ATTESTED_CONTENT_DIGEST_ENV) != content
            or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)
            or not isinstance(content, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", content)):
        raise HostError("installed host VibeComfy readiness source identity differs")
    try:
        checkout = configured_vibecomfy_checkout(os.environ)
    except ValueError as exc:
        raise HostError(f"installed host VibeComfy dependency is invalid: {exc}") from exc
    if checkout != lexical:
        raise HostError("installed host VibeComfy dependency root differs")
    package = checkout / "vibecomfy"
    for name, module in tuple(sys.modules.items()):
        if name == "vibecomfy" or name.startswith("vibecomfy."):
            origin = getattr(module, "__file__", None)
            if (not origin or Path(origin).resolve() != Path(origin).absolute()
                    or not Path(origin).resolve().is_relative_to(package)):
                raise HostError("installed host has a conflicting cached VibeComfy origin")
    existing = importlib.util.find_spec("vibecomfy")
    expected = package / "__init__.py"
    if existing is not None and (not existing.origin or Path(existing.origin) != expected):
        raise HostError("installed host has a conflicting VibeComfy package resolution")
    products = _host_product_import_origins()
    original_path = sys.path[:]
    try:
        if str(checkout) not in sys.path:
            sys.path.append(str(checkout))
        importlib.invalidate_caches()
        selected = importlib.util.find_spec("vibecomfy")
        if (selected is None or selected.origin != str(expected)
                or expected.resolve() != expected):
            raise HostError("installed host cannot resolve the attested VibeComfy package")
        if _host_product_import_origins() != products:
            raise HostError("installed host dependency changed a product import origin")
    except BaseException:
        sys.path[:] = original_path
        importlib.invalidate_caches()
        raise
    return checkout


@dataclass
class _NetworkBrokerContext:
    """Host-owned strict broker for one admitted network attempt."""

    broker: Any
    policy: dict[str, Any]
    evidence_key: str
    auth_token: str

    def stop(self) -> None:
        stop = getattr(self.broker, "stop", None)
        if callable(stop):
            stop()


@dataclass(frozen=True)
class AdapterSpec:
    family: str
    resource_keys: tuple[str, ...] = ()
    required_binaries: tuple[str, ...] = ()
    required_packages: tuple[str, ...] = ()
    requires_network: bool = False
    requires_remotion: bool = False


class AdapterRegistry:
    """Small explicit family map; adapter code stays behind the host seam."""

    _specs = {
        "cpu": AdapterSpec("cpu", ("cpu",)),
        "provider": AdapterSpec("provider", ("provider",), requires_network=True),
        "render": AdapterSpec("render", ("render",), ("node", "ffmpeg")),
        "local_generation": AdapterSpec("local_generation", ("gpu",), ()),
    }

    @classmethod
    def resolve(cls, definition: ExecutorDefinition) -> AdapterSpec:
        metadata = definition.metadata
        explicit = metadata.get("adapter_family") or metadata.get("adapter")
        if explicit:
            family = str(explicit)
        elif definition.id.startswith("rendering.") or "render" in definition.id:
            family = "render"
        elif definition.id.startswith(("vibecomfy.", "comfy_wrap.")) or metadata.get("vibecomfy_command"):
            family = "local_generation"
        elif metadata.get("api_provider") or metadata.get("env") or metadata.get("secrets_required") or definition.isolation.network:
            family = "provider"
        else:
            family = "cpu"
        base = cls._specs.get(family, AdapterSpec(family))
        return AdapterSpec(
            base.family,
            base.resource_keys,
            base.required_binaries,
            base.required_packages,
            base.requires_network,
            bool(metadata.get("requires_remotion", base.requires_remotion)),
        )

    @classmethod
    def families(cls) -> tuple[str, ...]:
        return tuple(sorted(cls._specs))

    @classmethod
    def from_matrix(cls, definition: ExecutorDefinition, entry: Mapping[str, Any] | None = None) -> AdapterSpec:
        base = cls.resolve(definition)
        entry = entry or {}
        family = str(entry.get("adapter_family") or base.family)
        if family not in cls._specs:
            raise HostError(f"unknown adapter family {family!r} for {definition.id!r}")
        builtin = cls._specs[family]
        return AdapterSpec(
            family,
            tuple(entry["resource_keys"] if "resource_keys" in entry else builtin.resource_keys),
            tuple(entry["required_binaries"] if "required_binaries" in entry else builtin.required_binaries),
            tuple(entry["required_packages"] if "required_packages" in entry else builtin.required_packages),
            builtin.requires_network,
            bool(entry.get("requires_remotion", base.requires_remotion)),
        )


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _capability_digest(value: Any) -> str:
    """Return the wire-format digest required by the Runtime capability contract."""
    from astrid.core.foundation.hash import executor_definition_digest

    return "sha256:" + executor_definition_digest(value)


def _json_safe(value: Any) -> Any:
    """Convert request values to the small JSON wire format used by workers."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _signal_process_group(process: subprocess.Popen, sig: int) -> None:
    _signal_owned_group(process, sig)
    return


def _process_group_exists(process: subprocess.Popen) -> bool:
    return _owned_group_exists(process)


def _terminate_process_group(process: subprocess.Popen, *, grace_seconds: float = 2.0) -> None:
    """Terminate the complete child session and reap the direct child.

    The leader is allowed to exit cleanly on SIGTERM, so waiting only on
    ``process.wait()`` is insufficient: a SIGTERM-resistant descendant could
    otherwise survive after the leader has gone away.  Observe the process
    group independently, escalate the still-live group to SIGKILL, then reap
    the leader.  Descendants are not our children and therefore cannot be
    ``waitpid``-reaped here; killing the owned session is the relevant
    containment guarantee.
    """
    terminate_tree(process, grace_seconds=grace_seconds)
    verify_tree_absent(process)


_REVIEW_SESSION_NAME = ".astrid-human-review-session.json"


def _observe_owned_review_session(process: subprocess.Popen, attempt: Path,
                                  previous: tuple[int, int, bytes] | None = None
                                  ) -> tuple[int, int, bytes] | None:
    """Retain exact private bytes while the original action owner is observable."""
    path = attempt / _REVIEW_SESSION_NAME
    if attempt != attempt.resolve(strict=False):
        raise HostError("review receiver attempt alias")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return previous
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        raw = stream.read(8193)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or len(raw) > 8192):
        raise HostError("review receiver observation is not private")
    snapshot = info.st_dev, info.st_ino, raw
    if previous is not None:
        if snapshot != previous:
            raise HostError("review receiver observation changed owner or bytes")
        return previous
    try:
        row = json.loads(raw)
        url = urlsplit(row["receiver_url"])
        valid = (set(row) == {"schema", "receiver_url", "process_pid", "process_birth_id"}
                 and row["schema"] == "astrid.human-review-session/v1"
                 and type(row["process_pid"]) is int and row["process_pid"] == process.pid
                 and row["process_birth_id"] == getattr(process, "_astrid_process_birth", None)
                 and bool(row["process_birth_id"])
                 and _process_birth_identity(process.pid) == row["process_birth_id"]
                 and url.scheme == "http" and url.hostname == "127.0.0.1" and bool(url.port)
                 and url.path == "/" and not url.fragment and not url.username and not url.password
                 and re.fullmatch(r"token=[0-9a-f]{32}", url.query) is not None)
    except (ValueError, TypeError, KeyError, AttributeError):
        valid = False
    if not valid:
        raise HostError("review receiver observation does not match the original child")
    return snapshot


def _retire_owned_review_session(attempt: Path, snapshot: tuple[int, int, bytes] | None) -> None:
    """Use only a previously retained observation, after positive child cleanup."""
    path = attempt / _REVIEW_SESSION_NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return  # The action completed its own server-close finalizer.
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        raw = stream.read(8193)
    if (snapshot is None or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or (info.st_dev, info.st_ino, raw) != snapshot
            or (path.lstat().st_dev, path.lstat().st_ino) != snapshot[:2]):
        raise HostError("review receiver cleanup owner mismatch; attempt retained")
    path.unlink()


def _confined_cwd(
    raw_cwd: str | Path | None,
    *,
    attempt: Path,
    source_root: Path | None = None,
    values: Mapping[str, Any] | None = None,
) -> Path:
    """Resolve a command cwd inside the attempt or admitted source tree."""
    value = str(raw_cwd or "")
    for key, replacement in (values or {}).items():
        value = value.replace("{" + str(key) + "}", str(replacement))
    candidate = Path(value) if value else attempt
    if not candidate.is_absolute():
        candidate = attempt / candidate
    resolved = candidate.expanduser().resolve()
    allowed = [attempt.resolve()]
    if source_root is not None:
        allowed.append(source_root.expanduser().resolve())
    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed):
        raise HostError(
            f"command cwd escapes the attempt/source scope: {raw_cwd!r}"
        )
    return resolved


def _preflight_unavailable_reason(record: "CapabilityRecord") -> str:
    """Return a stable, secret-free reason for a failed capability preflight."""
    failures: list[str] = []
    for check_name in sorted(record.preflight):
        check = record.preflight[check_name]
        if not isinstance(check, Mapping) or check.get("ok") is not False:
            continue
        missing = check.get("missing")
        if isinstance(missing, (list, tuple)) and missing:
            values = ",".join(sorted(str(value) for value in missing))
            failures.append(f"{check_name}:missing={values}")
        elif check.get("reason"):
            failures.append(f"{check_name}:reason={check['reason']}")
        else:
            failures.append(f"{check_name}:failed")
    if failures:
        return ";".join(failures)
    return str(record.matrix.get("evidence_reason") or "capability preflight is not ready")


_WITHDRAWN_DISPOSITIONS = frozenset({"unsupported", "retired"})


def _is_withdrawn(record: "CapabilityRecord") -> bool:
    """Return whether the ledger withdraws a capability from every admission path."""

    return str(record.matrix.get("disposition", "")) in _WITHDRAWN_DISPOSITIONS


def _withdrawn_reason(record: "CapabilityRecord") -> str:
    return str(
        record.matrix.get("evidence_reason")
        or record.matrix.get("disposition")
        or "withdrawn"
    )


def _required_secret_names(record: "CapabilityRecord") -> tuple[str, ...]:
    """Return the manifest/matrix credential names admitted to one child.

    This is deliberately the one source of truth for both readiness and
    process injection.  In particular, an ambient ``OPENAI_API_KEY`` cannot
    cross the boundary merely because the parent happens to have one.
    """
    matrix_secret_names = tuple(
        str(name) for name in (record.matrix.get("required_env") or ())
        if str(name).upper().endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
    )
    manifest_secret_names = tuple(
        str(name)
        for raw in (
            record.definition.metadata.get("required_env") or (),
            record.definition.metadata.get("env") or (),
        )
        for name in ((raw,) if isinstance(raw, str) else raw)
        if str(name).upper().endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
    )
    return tuple(dict.fromkeys(
        str(name)
        for name in (
            *matrix_secret_names,
            *manifest_secret_names,
            *(record.definition.isolation.secrets_required or ()),
            *(record.definition.metadata.get("secrets_required") or ()),
        )
        if str(name)
    ))


def _required_env_names(record: "CapabilityRecord") -> tuple[str, ...]:
    """Return all manifest-declared environment inputs (public or secret)."""
    values: list[str] = []
    for raw in (
        record.matrix.get("required_env") or (),
        record.definition.metadata.get("required_env") or (),
        record.definition.metadata.get("env") or (),
        _required_secret_names(record),
    ):
        if isinstance(raw, str):
            raw = (raw,)
        values.extend(str(name) for name in raw if str(name))
    return tuple(dict.fromkeys(values))


def _fixed_worker_environment(record: "CapabilityRecord", explicit_env: Mapping[str, str],
                              configuration: Mapping[str, str]) -> dict[str, str]:
    """Pure fixed configuration projection shared by launch and execution."""
    declared = _required_secret_names(record)
    explicit = dict(explicit_env)
    explicit.update({name: value for name in _required_env_names(record)
                     if name not in declared and (value := configuration.get(name))})
    for name in tuple(explicit):
        if name in declared:
            explicit.pop(name, None)
        elif name.upper().endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")):
            raise HostError(f"undeclared secret environment variable {name!r}")
    return explicit


def _launch_worker_probe_environments(pack_roots: tuple[Path, ...], environment: Mapping[str, str],
                                     capability_matrix: Path | None = None) -> list[dict[str, str]]:
    """Project only fixed launch-known inputs; never expand task placeholders."""
    from astrid.core.rendering.remotion_runtime import _NODE_ENV_KEYS, _UNSUPPORTED_PYTHON_STARTUP, _REGISTRY_SELECTORS
    from astrid.core.subprocess_env import _SAFE_BASE_ENV, _is_secret_name, _normalize_secret_names
    from astrid.core.execution.executor.folder import discover_folder_executor_roots, load_folder_executor
    from astrid.core.execution.executor.schema import ExecutorValidationError
    matrix = GenericPackHost._load_matrix(capability_matrix)
    records: dict[str, CapabilityRecord] = {}
    for root in pack_roots:
        for executor_root in discover_folder_executor_roots(root):
            try:
                definition = load_folder_executor(executor_root)
            except (ExecutorValidationError, OSError, ValueError):
                continue  # Same unavailable optional definition as host discovery.
            definition = _attach_pack_metadata(definition, executor_root)
            records[definition.id] = CapabilityRecord(
                definition=definition, capability_digest='', source_digest='',
                source_root=executor_root, matrix=matrix.get(definition.id, {}))
    # Match host discovery's strict V3 projection and exact source roots. A
    # local orchestration action is deliberately not a host worker definition.
    from astrid.core.pack.discovery import discover_pack_metadata
    from astrid.sdk.actions import action_executor_definition
    for discovered_pack in discover_pack_metadata(
        project_root=pack_roots[0] if pack_roots else Path.cwd(),
        extra_pack_roots=tuple(str(root) for root in pack_roots),
    ):
        pack = discovered_pack.pack
        pack_root = pack.root.resolve()
        if not any(pack_root == root or pack_root.is_relative_to(root) for root in pack_roots):
            continue
        for local_id, declaration in pack.actions.items():
            try:
                definition = action_executor_definition(discovered_pack, local_id, declaration)
            except (ExecutorValidationError, OSError, ValueError):
                continue  # Same unavailable optional action as host discovery.
            records[definition.id] = CapabilityRecord(
                definition=definition, capability_digest='', source_digest='',
                source_root=pack_root, matrix=matrix.get(definition.id, {}))
    # The SDK caller can carry import paths removed from the installed host
    # launch. Qualify the explicit host mapping, not that caller's ambient env.
    dependencies = dependency_pythonpath(environment)
    baseline = build_child_subprocess_env(base=environment, parent=environment)
    variants = [baseline]
    for record in records.values():
        # Startup validation has no task credential channel. Legacy manifests
        # can still list credentials in passthrough: omit those names entirely,
        # while retaining their syntax checks without declaring any authority.
        passthrough = record.definition.isolation.env_passthrough
        omitted = tuple(name for name in passthrough if _is_secret_name(name))
        _normalize_secret_names(omitted, 'probe_omitted_secrets')
        public_passthrough = tuple(name for name in passthrough if not _is_secret_name(name))
        command = record.definition.command
        fixed = {str(k): str(v) for k, v in (getattr(command, 'env', {}) or {}).items()
                 if '{' not in str(v) and '}' not in str(v)} if command is not None else {}
        fixed = _fixed_worker_environment(record, fixed, environment)
        relevant = {k: v for k, v in fixed.items()
                    if k in set(_NODE_ENV_KEYS) | set(_SAFE_BASE_ENV) | set(_REGISTRY_SELECTORS) | set(_UNSUPPORTED_PYTHON_STARTUP) | {'XDG_CONFIG_HOME', 'PYTHONPATH'}
                    or k.startswith(('DYLD_', 'LD_'))
                    or k in {'PYTHONEXECUTABLE', '__PYVENV_LAUNCHER__', 'PYTHONHOME',
                             'NODE_OPTIONS', 'NODE_PATH', 'NODE_ICU_DATA', 'OPENSSL_CONF'}}
        projected = build_child_subprocess_env(
            base=environment, parent=environment, explicit_env=relevant,
            passthrough=public_passthrough,
            declared_passthrough=public_passthrough)
        native = dict(projected)
        native[ASTRID_PACKS_PATH] = os.pathsep.join(str(root) for root in pack_roots)
        native['PYTHONPATH'] = os.pathsep.join(dict.fromkeys((
            str(Path(__file__).resolve().parents[3]), *dependencies,
            *(str(root) for root in _admitted_python_roots(record.source_root, record.definition)))))
        variants.append(native)
        # The command builder supplies a canonical default, then command/env
        # and required nonsecret configuration override it, in the real order.
        command_fixed = _fixed_worker_environment(record, {
            'PYTHONPATH': os.pathsep.join((str(Path(__file__).resolve().parents[3]), *dependencies)),
            **fixed}, environment)
        command_relevant = {**relevant, 'PYTHONPATH': command_fixed['PYTHONPATH']}
        command_variant = build_child_subprocess_env(
            base=environment, parent=environment, explicit_env=command_relevant,
            passthrough=public_passthrough,
            declared_passthrough=public_passthrough)
        command_variant['PYTHONPATH'] = _command_worker_pythonpath(record, command_variant['PYTHONPATH'])
        variants.append(command_variant)
    return variants


def _command_worker_pythonpath(record: 'CapabilityRecord', existing: str) -> str:
    source_pack = str(record.definition.metadata.get('source_pack') or '')
    raw = record.definition.metadata.get('pack_root')
    if source_pack and isinstance(raw, str) and raw:
        pack_root = Path(raw).expanduser().resolve()
        builtin_root = (Path(__file__).resolve().parents[3] / 'astrid/packs' / source_pack).resolve()
        if pack_root != builtin_root:
            return os.pathsep.join((str(pack_root), str(pack_root.parent), existing))
    return existing


def _resolve_credential_value(
    source: Mapping[str, str], name: str, *, explicit: bool = False
) -> str | None:
    """Resolve a declared credential without broadening ordinary env access.

    Hivemind's contributor login stores its owner-only credential at the
    standard ``~/.hivemind/key`` path. The child executor already supports
    that path; the host must use the same source for readiness and injection
    or a logged-in contributor would be rejected during preflight.
    """

    # A caller-provided credential mapping is the explicit tier.  The default
    # process environment remains the process tier.  Shared-file lookup is
    # deliberately performed by the canonical resolver in both cases.
    if explicit:
        environ = dict(os.environ)
        for config_name in ("ASTRID_HOME", "ASTRID_ENV_FILE"):
            if config_name in source:
                environ[config_name] = str(source[config_name])
        explicit_value = source.get(name)
    else:
        environ = source
        explicit_value = None
    try:
        value, _source = load_local_api_key_with_source(
            name,
            explicit=explicit_value,
            environ=environ,
        )
    except AstridError:
        value = ""
    if value:
        return str(value)
    if name != "HIVEMIND_CONTRIBUTOR_KEY":
        return None
    key_path = Path.home() / ".hivemind" / "key"
    try:
        value = key_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _network_policy(record: "CapabilityRecord") -> dict[str, Any] | None:
    """Read the optional bounded network policy from the manifest/matrix."""
    raw = record.definition.metadata.get("network_policy")
    if raw is None:
        raw = record.matrix.get("network_policy")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise HostError(f"network_policy for {record.id!r} must be an object")
    return {str(key): value for key, value in raw.items()}


def _provider_routes(record: "CapabilityRecord", inputs: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    """Resolve the exact upstream routes admitted for one provider task."""
    policy = _network_policy(record) or {}
    routes = policy.get("allowed_routes", policy.get("allowed_destinations", ()))
    if isinstance(routes, str):
        routes = (routes,)
    dynamic_names = policy.get("dynamic_url_inputs", ())
    if isinstance(dynamic_names, str):
        dynamic_names = (dynamic_names,)
    dynamic_routes: list[str] = []
    for name in dynamic_names or ():
        raw = (inputs or {}).get(str(name))
        if not isinstance(raw, str) or not raw:
            continue
        parsed = urlsplit(raw)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            dynamic_routes.append(
                f"{parsed.scheme}://{parsed.hostname.lower()}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"
            )
    resolver = str(policy.get("dynamic_route_resolver") or "").strip()
    if resolver == "runpod_pod_handle_ssh":
        handle_names = policy.get("dynamic_tcp_inputs", ("pod_handle",))
        if isinstance(handle_names, str):
            handle_names = (handle_names,)
        from astrid.core.execution.provider_route_resolvers import (
            resolve_runpod_pod_handle_ssh_route,
        )

        for name in handle_names or ():
            raw = (inputs or {}).get(str(name))
            if raw is None:
                raise HostError(
                    f"dynamic provider route input {name!r} is missing"
                )
            dynamic_routes.append(resolve_runpod_pod_handle_ssh_route(raw))
    return tuple(dict.fromkeys([*(str(route) for route in (routes or ())), *dynamic_routes]))


def _host_managed_broker(policy: Mapping[str, Any] | None) -> bool:
    """Whether a network policy has a broker owned and started by the host."""
    if not isinstance(policy, Mapping):
        return False
    descriptor = policy.get("broker")
    return isinstance(descriptor, Mapping) and bool(
        descriptor.get("host_managed", descriptor.get("managed", True))
    )


def _tcp_broker_supports(policy: Mapping[str, Any] | None) -> bool:
    """The built-in broker is HTTP/TCP only; datagrams are not advertised."""
    if not isinstance(policy, Mapping):
        return False
    protocols = policy.get("allowed_protocols", policy.get("protocols", ()))
    if isinstance(protocols, str):
        protocols = (protocols,)
    return not any(str(protocol).lower() in {"udp", "quic"} for protocol in (protocols or ()))


_STRICT_NETWORK_HOOK_SOURCE = (
    "def install_host_network(policy, broker_required):\n"
    "    return policy.install_from_environment(strict=True, broker_required=broker_required)\n"
)

# -I -S keeps automatic site/pack imports out of this admission prerequisite.
# Load only the exact host policy and hook; restore dependency paths afterwards.
_STRICT_NETWORK_STARTUP = r"""
import hashlib, importlib.machinery, json, os, pathlib, sys, types
try:
    config = json.loads(sys.argv[1])
    hook_path = pathlib.Path(config['hook'])
    search = [os.getcwd(), *config['imports'], *sys.path]
    found = importlib.machinery.PathFinder.find_spec('sitecustomize', search)
    if found is None or pathlib.Path(found.origin).resolve() != hook_path.resolve():
        raise RuntimeError('missing or shadowed host hook')
    hook_source = hook_path.read_bytes()
    if hashlib.sha256(hook_source).hexdigest() != config['hook_sha256']:
        raise RuntimeError('host hook changed')
    policy_path = pathlib.Path(config['policy'])
    policy_source = policy_path.read_bytes()
    if hashlib.sha256(policy_source).hexdigest() != config['policy_sha256']:
        raise RuntimeError('host policy changed')
    name = 'astrid.core.execution.network_policy'
    policy = types.ModuleType(name)
    policy.__file__ = str(policy_path)
    sys.modules[name] = policy
    exec(compile(policy_source, str(policy_path), 'exec'), policy.__dict__)
    hook = {}
    exec(compile(hook_source, str(hook_path), 'exec'), hook)
    if hook['install_host_network'](policy, config['broker_required']) is not True:
        raise RuntimeError('network hook did not install')
except BaseException:
    os.write(2, b'astrid network startup failed\n')
    os._exit(78)
# Recreate the admitted interpreter dependency setup after network startup.
# site.main restores venv paths without running another customizable hook.
import site
site.execsitecustomize = lambda: None
site.execusercustomize = lambda: None
site.main()
sys.path[:0] = list(dict.fromkeys(config['imports']))
sys.path.insert(0, os.getcwd())
import runpy
sys.argv = [config['module'], *sys.argv[2:]]
runpy.run_module(config['module'], run_name='__main__', alter_sys=True)
"""


def _network_startup_argv(argv: list[str], attempt: Path, env: Mapping[str, str], *, broker_required: bool) -> list[str]:
    """Host-selected strict D18 startup, independent of action environment flags."""
    arguments = list(argv[1:])
    flags = []
    while arguments and arguments[0] in {"-u", "-B"}:
        flags.append(arguments.pop(0))
    if len(arguments) < 2 or arguments[0] != "-m" or arguments[1] != "astrid.core.execution.generic_host_worker":
        raise HostError("strict network startup requires the host Python worker")
    policy_path = Path(__file__).with_name("network_policy.py").resolve()
    config = {
        "hook": str(attempt / ".astrid-network-hook" / "sitecustomize.py"),
        "hook_sha256": hashlib.sha256(_STRICT_NETWORK_HOOK_SOURCE.encode()).hexdigest(),
        "policy": str(policy_path),
        "policy_sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        "imports": [path for path in env.get("PYTHONPATH", "").split(os.pathsep) if path],
        "module": arguments[1],
        "broker_required": broker_required,
    }
    return [argv[0], *flags, "-I", "-S", "-c", _STRICT_NETWORK_STARTUP,
            json.dumps(config, sort_keys=True), *arguments[2:]]


@dataclass(frozen=True)
class _FilesystemSandboxPolicy:
    """Host-selected experiment configuration, not an authenticated product grant.

    Bootstrap resources are explicit exceptions separate from project reads.
    Existing launch sites do not yet require or supply this configuration.
    """

    read_roots: tuple[Path, ...]
    bootstrap_files: tuple[Path, ...]
    bootstrap_roots: tuple[Path, ...]
    private_write_root: Path


@dataclass(frozen=True)
class _SandboxProjectScope:
    project_root: Path
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class _SandboxTcpEndpoint:
    """An exact IPv4 TCP destination; no DNS, wildcard, listener or port range."""

    address: str
    port: int


@dataclass(frozen=True)
class _SandboxNetworkPolicy:
    outbound_tcp: tuple[_SandboxTcpEndpoint, ...] = ()
    broker: _SandboxTcpEndpoint | None = None


@dataclass(frozen=True)
class _SandboxEnvironmentPolicy:
    # Only inert presentation/locale keys can be selected from parent input.
    inherit_names: tuple[str, ...] = ()
    values: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class _ConfinementPolicy:
    """Trusted host input, NEVER decoded from a delegated request or MCP config.

    This contract does not authenticate a GRANT. The host must resolve authority
    before constructing it. Bootstrap exceptions are separate from project data;
    project writes must target a private attempt copy. Launch sites require this
    contract; absence of an authenticated producer is never an ambient grant.
    """

    attempt_root: Path
    private_write_root: Path
    project: _SandboxProjectScope
    bootstrap_files: tuple[Path, ...]
    bootstrap_roots: tuple[Path, ...]
    environment: _SandboxEnvironmentPolicy
    network: _SandboxNetworkPolicy = field(default_factory=_SandboxNetworkPolicy)
    pass_fds: tuple[int, ...] = ()
    workload_process_creation: bool = True
    claim_binding: tuple[tuple[str, Any], ...] = ()
    bootstrap_search_roots: tuple[Path, ...] = ()
    executable_bindings: tuple[ExecutableBinding, ...] = ()
    executable_directory: Path | None = None


@dataclass(frozen=True)
class _AuthenticatedAttempt:
    # Constructed by the credential-owning Runtime client after live renewal.
    fields: tuple[tuple[str, Any], ...]


class _AttemptPolicyFactory:
    """Trusted code/bootstrap configuration shared by slots, never a policy."""

    def __init__(self, executables: ExecutableAdmission):
        self.executables = executables

    def build(self, authority, *, attempt: Path, record) -> _ConfinementPolicy:
        if type(authority) is not _AuthenticatedAttempt:
            raise HostError("authenticated attempt authority required")
        try:
            bindings = self.executables.require(_binary_requirements(record))
        except ExecutableAdmissionError as exc:
            raise HostError(str(exc)) from exc
        if bindings != record.executable_bindings:
            raise HostError("declared executable readiness binding differs from launch")
        projection = dict(authority.fields)
        if not projection.get("project_id"):
            # Explicit host-owned/unpaired workspace tasks have no project data.
            if projection.get("kind") == "paired_session":
                raise HostError("paired claim lacks selected project")
        # Only Runtime-admitted CAS inputs are materialized here. No task path,
        # environment or selected-project title chooses a host filesystem root.
        project = attempt / "inputs"
        private = attempt / "outputs"
        for root in (project, private):
            root.mkdir(mode=0o700, exist_ok=True)
            if root.is_symlink() or root.resolve() != root or root.stat().st_uid != os.getuid():
                raise HostError("attempt resource root is not canonical and owned")
        _verify_admitted_source({"source_digest": record.source_digest, "source_roots": [str(root) for root in _admitted_source_roots(record.source_root, record.definition)]})
        python = Path(sys.executable).resolve(strict=True)
        prefix = Path(sys.base_prefix).resolve(strict=True)
        files = (python, prefix / "lib/libpython3.11.dylib",
                 Path("/opt/homebrew/opt/gettext/lib/libintl.8.dylib"),
                 Path("/dev/null"), Path("/dev/urandom"), Path("/dev/random"))
        roots = (Path(sysconfig.get_path("stdlib")).resolve(),
                 Path(sysconfig.get_path("purelib")).resolve(),
                 Path(sysconfig.get_path("platlib")).resolve(),
                 Path(__file__).resolve().parents[2],
                 Path(__file__).resolve().parents[3] / "banodoco_workspace_client",
                 *_admitted_source_roots(record.source_root, record.definition),
                 Path("/usr/lib"), Path("/System/Library/Frameworks/CoreFoundation.framework"),
                 Path("/System/Library/Frameworks/CoreServices.framework"),
                 Path("/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld"),
                 Path("/System/Volumes/Preboot/Cryptexes/OS/usr/lib"))
        # Existing installed platform artifacts only. Unsupported hosts are
        # refused by the same required backend check before any workload spawn.
        binary_directory = None
        if bindings:
            binary_directory = attempt / ".astrid-executables"
            # A fresh host-owned namespace, read-only under the child policy.
            binary_directory.mkdir(mode=0o700)
            for binding in bindings:
                (binary_directory / binding.name).symlink_to(binding.executable.resolved)
        policy = _ConfinementPolicy(attempt, private,
            _SandboxProjectScope(project, (project,)),
            tuple(p for p in files if p.exists()), tuple(dict.fromkeys(p for p in roots if p.is_dir())),
            _SandboxEnvironmentPolicy(), claim_binding=authority.fields,
            bootstrap_search_roots=(Path(__file__).resolve().parents[3], Path(record.source_root).resolve().parent),
            executable_bindings=bindings, executable_directory=binary_directory)
        _confined_launch_plan([str(python)], policy=policy)
        return policy


@dataclass(frozen=True)
class _ConfinedLaunchPlan:
    """Immutable launch inputs. Construct with _confined_launch_plan only.

    Consumers must apply ALL subprocess_options, without merging request options.
    No process is launched here; descriptor lifetime and path replacement between
    planning and spawn remain integration/adversarial-proof obligations.
    """

    argv: tuple[str, ...]
    cwd: Path
    environment: tuple[tuple[str, str], ...]
    pass_fds: tuple[int, ...]
    retained_process_only: bool = False
    child_bridge_launch: Any = None

    def subprocess_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {"cwd": str(self.cwd), "env": dict(self.environment),
                "close_fds": True, "pass_fds": self.pass_fds,
                "stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE}
        if self.child_bridge_launch is not None:
            options["pass_fds"] = ()
            options["_child_bridge_launch"] = self.child_bridge_launch
        if self.retained_process_only:
            # No command-inventory IPC may mint helpers outside this topology.
            # Only popen_owned_{process,group} consumes this private option.
            options["_delegate_inventory"] = False
        return options


def _wan_creation_control_launch_plan(
    argv: Sequence[str], *, policy: _ConfinementPolicy,
    parent_environment: Mapping[str, str] | None = None,
) -> _ConfinedLaunchPlan:
    """Bounded native experiment at the retained Wan owned-launch seam.

    Host code selects this function; no engine spec, MCP/config or environment
    switch can weaken it. Production start remains held for engine admission
    and recoverable ownership. No helper operation has yet been admitted.
    """
    if type(policy) is not _ConfinementPolicy:
        raise HostError("required confinement policy is missing or invalid")
    return _confined_launch_plan(
        argv, policy=replace(policy, workload_process_creation=False),
        parent_environment=parent_environment,
    )


def _confined_launch_plan(
    argv: Sequence[str], *, policy: _ConfinementPolicy | None = None,
    cwd: Path | None = None, parent_environment: Mapping[str, str] | None = None,
    broker_endpoint: str | None = None,
) -> _ConfinedLaunchPlan:
    """Compile required host confinement, independently of network requests.

    Caller argv/cwd/environment/endpoint can narrow or fail, never grant access.
    No raw profile, request claims, title, MCP config or optional bypass is read.
    This is the WP1 contract, not an integration of the legacy broker launcher.
    """
    if type(policy) is not _ConfinementPolicy:
        raise HostError("required confinement policy is missing or invalid")
    if type(policy.workload_process_creation) is not bool:
        raise HostError("invalid confinement process creation policy")

    def items(value: Any, name: str) -> tuple:
        if type(value) is not tuple:
            raise HostError(f"invalid confinement {name}: expected immutable tuple")
        return value

    def path(raw: Any, *, directory: bool, character_device: bool = False) -> Path:
        if not isinstance(raw, Path) or not raw.is_absolute() or any(
            ord(c) < 32 or ord(c) == 127 for c in str(raw)
        ):
            raise HostError("confinement paths must be absolute Paths without controls")
        try:
            resolved = raw.resolve(strict=True)
            mode = resolved.stat().st_mode
        except (OSError, ValueError, RuntimeError) as exc:
            raise HostError("confinement path is unavailable") from exc
        valid_kind = stat.S_ISDIR(mode) if directory else (
            stat.S_ISREG(mode) or character_device and stat.S_ISCHR(mode)
        )
        if resolved == Path("/") or not valid_kind:
            raise HostError("invalid confinement path scope or type")
        return resolved

    def within(value: Path, roots: Sequence[Path]) -> bool:
        return any(value.is_relative_to(root) for root in roots)

    def quote(value: Path | str) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    attempt = path(policy.attempt_root, directory=True)
    private = path(policy.private_write_root, directory=True)
    if private == attempt or not private.is_relative_to(attempt):
        raise HostError("private writes must stay inside the private attempt")
    if type(policy.project) is not _SandboxProjectScope:
        raise HostError("invalid confinement selected project")
    project = path(policy.project.project_root, directory=True)
    if private.is_relative_to(project) or project.is_relative_to(private):
        raise HostError("private scratch and selected project must be separate scopes")
    reads = tuple(path(p, directory=True) for p in items(policy.project.read_roots, "project reads"))
    writes = tuple(path(p, directory=True) for p in items(policy.project.write_roots, "project writes"))
    if not reads or any(not p.is_relative_to(project) for p in (*reads, *writes)):
        raise HostError("project paths exceed selected project scope")
    if any(not p.is_relative_to(attempt) or not within(p, reads) for p in writes):
        raise HostError("project writes must stay inside the private attempt and project read scope")
    bootstrap_roots = tuple(path(p, directory=True) for p in items(policy.bootstrap_roots, "bootstrap roots"))
    bootstrap_search_roots = tuple(path(p, directory=True) for p in items(policy.bootstrap_search_roots, "bootstrap searches"))
    bootstrap_files = tuple(path(p, directory=False, character_device=True)
                            for p in items(policy.bootstrap_files, "bootstrap files"))
    bindings = items(policy.executable_bindings, "declared executable bindings")
    declared_files: list[Path] = []
    binary_directory = policy.executable_directory
    if bindings:
        if (not isinstance(binary_directory, Path) or binary_directory.is_symlink()
                or binary_directory != attempt / ".astrid-executables"
                or path(binary_directory, directory=True) != binary_directory
                or binary_directory.stat().st_uid != os.getuid()
                or stat.S_IMODE(binary_directory.stat().st_mode) != 0o700):
            raise HostError("invalid declared executable directory")
        if any(type(binding) is not ExecutableBinding for binding in bindings):
            raise HostError("invalid declared executable binding")
        if len({binding.name for binding in bindings}) != len(bindings) or set(
                p.name for p in binary_directory.iterdir()) != {binding.name for binding in bindings}:
            raise HostError("declared executable namespace differs")
        for binding in bindings:
            try:
                binding.verify()
            except ExecutableAdmissionError as exc:
                raise HostError(str(exc)) from exc
            alias = binary_directory / binding.name
            if (not alias.is_symlink() or alias.lstat().st_uid != os.getuid()
                    or alias.readlink() != binding.executable.resolved):
                raise HostError("declared executable alias replaced")
            declared_files.append(alias)
            for pin in (binding.executable, *binding.libraries):
                declared_files.extend((*pin.aliases, pin.resolved))
            declared_files.extend(binding.system_libraries)
        bootstrap_search_roots = (*bootstrap_search_roots, binary_directory)
    elif binary_directory is not None:
        raise HostError("undeclared executable directory")
    # Keep declared symlink aliases explicit as well as their resolved artifacts.
    file_aliases = tuple(dict.fromkeys((*policy.bootstrap_files, *bootstrap_files, *declared_files)))
    if any(p.is_relative_to(project) or project.is_relative_to(p)
           or p.is_relative_to(private) or private.is_relative_to(p) for p in bootstrap_roots):
        raise HostError("bootstrap roots must not subsume project or private authority")
    if not isinstance(argv, (tuple, list)) or not argv or any(
        type(arg) is not str or "\x00" in arg for arg in argv
    ):
        raise HostError("invalid confinement command")
    executable = path(Path(argv[0]), directory=False)
    if not os.access(executable, os.X_OK):
        raise HostError("confinement command executable is not executable")
    if (executable not in bootstrap_files and not within(executable, bootstrap_roots)
            and executable not in {binding.executable.resolved for binding in bindings}):
        raise HostError("command executable is not an enumerated bootstrap resource")
    working = private if cwd is None else path(cwd, directory=True)
    if not within(working, (*reads, private, *bootstrap_roots)):
        raise HostError("command cwd exceeds confinement read scope")

    if type(policy.network) is not _SandboxNetworkPolicy:
        raise HostError("invalid confinement network policy")

    def endpoint(value: Any) -> str:
        if type(value) is not _SandboxTcpEndpoint or type(value.address) is not str:
            raise HostError("invalid confinement TCP endpoint")
        try:
            address = ipaddress.IPv4Address(value.address)
        except ipaddress.AddressValueError as exc:
            raise HostError("confinement TCP endpoint requires an exact IPv4 address") from exc
        if (str(address) != value.address or address.is_unspecified or address.is_multicast
                or int(address) == 0xFFFFFFFF or type(value.port) is not int or not 1 <= value.port <= 65535):
            raise HostError("invalid confinement TCP endpoint address or port")
        return f"{address}:{value.port}"

    network = [endpoint(p) for p in items(policy.network.outbound_tcp, "outbound TCP")]
    broker = endpoint(policy.network.broker) if policy.network.broker is not None else None
    if broker_endpoint is not None and (broker is None or broker_endpoint != f"http://{broker}"):
        raise HostError("caller broker endpoint is not authorized by host policy")
    if broker is not None:
        network.append(broker)
    if not policy.workload_process_creation and (network or policy.pass_fds):
        raise HostError("retained process policy has no admitted delegation channels")

    if type(policy.environment) is not _SandboxEnvironmentPolicy:
        raise HostError("invalid confinement environment policy")
    safe_names = {"LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM"}
    inherit = items(policy.environment.inherit_names, "environment selection")
    values = items(policy.environment.values, "environment values")
    if any(type(k) is not str or k not in safe_names for k in inherit):
        raise HostError("unsafe confinement environment selection")
    if any(type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str
           or pair[0] not in safe_names for pair in values):
        raise HostError("unsafe confinement environment value")
    names = (*inherit, *(pair[0] for pair in values))
    if len(set(names)) != len(names):
        raise HostError("duplicate confinement environment key")
    source = {} if parent_environment is None else parent_environment
    if not isinstance(source, Mapping):
        raise HostError("invalid parent environment")
    selected = {k: source[k] for k in inherit if k in source}
    selected.update(values)
    if any(type(v) is not str or len(v) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in v)
           for v in selected.values()):
        raise HostError("invalid confinement environment value")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(private), "TMPDIR": str(private),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "LC_ALL": "C"}
    env.update(selected)
    if bindings:
        env["PATH"] = str(binary_directory) + os.pathsep + env["PATH"]

    descriptors = items(policy.pass_fds, "descriptor allowlist")
    if any(type(fd) is not int or fd < 3 for fd in descriptors) or len(set(descriptors)) != len(descriptors):
        raise HostError("invalid confinement descriptor allowlist")
    for fd in descriptors:
        try:
            pipe = stat.S_ISFIFO(os.fstat(fd).st_mode)
        except OSError as exc:
            raise HostError("confinement descriptor is unavailable") from exc
        if not pipe:
            raise HostError("confinement descriptors must be explicit pipe channels")

    if sys.platform != "darwin":
        raise HostError("required confinement needs the macOS sandbox backend")
    sandbox = shutil.which("/usr/bin/sandbox-exec")
    if sandbox != "/usr/bin/sandbox-exec":
        raise HostError("required confinement needs the host sandbox-exec backend")
    process_rules = (["(allow process*)"] if policy.workload_process_creation else
                     ["(allow process-exec)", "(deny process-fork)"])
    # SETEXEC of this same process retains Seatbelt. The existing authenticated
    # monotonic birth fence handles exec token changes during live custody.
    # No mach-lookup, IPC, socket, process-fork or broad process grant is added.
    rules = ["(version 1)", "(deny default)", *process_rules, "(allow sysctl-read)"]
    # Nonrecursive bootstrap metadata exceptions, shared with the spike model.
    ancestors = sorted({p for resource in (*reads, *writes, private, *bootstrap_roots, *file_aliases)
                        for p in resource.parents}, key=str)
    rules.extend(f'(allow file-read-metadata (literal "{quote(p)}"))' for p in ancestors)
    rules.append('(allow file-read* (literal "/"))')
    rules.extend(f'(allow file-read* (subpath "{quote(p)}"))' for p in (*reads, private, *bootstrap_roots))
    rules.extend(f'(allow file-read* (literal "{quote(p)}"))' for p in (*file_aliases, *bootstrap_search_roots))
    rules.extend(f'(allow file-map-executable (subpath "{quote(p)}"))' for p in bootstrap_roots)
    rules.extend(f'(allow file-map-executable (literal "{quote(p)}"))' for p in file_aliases)
    rules.extend(f'(allow file-write* (subpath "{quote(p)}"))' for p in (*writes, private))
    rules.extend(f'(allow network-outbound (remote tcp "{p}"))' for p in dict.fromkeys(network))
    profile = "\n".join(rules) + "\n"
    return _ConfinedLaunchPlan((sandbox, "-p", profile, str(executable), *argv[1:]),
                               working, tuple(sorted(env.items())), descriptors,
                               retained_process_only=not policy.workload_process_creation)


def _filesystem_sandbox_profile(policy: _FilesystemSandboxPolicy, attempt: Path) -> str:
    """Build a deny-default file policy without ambient home/temp allowances."""
    if not isinstance(policy, _FilesystemSandboxPolicy):
        raise HostError("required filesystem sandbox policy is missing or invalid")

    def path(raw: Path, *, directory: bool) -> Path:
        candidate = Path(raw)
        if not candidate.is_absolute() or any(ord(c) < 32 or ord(c) == 127 for c in str(candidate)):
            raise HostError("filesystem sandbox paths must be absolute and contain no controls")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise HostError("filesystem sandbox path is unavailable") from exc
        if resolved == Path("/") or resolved.is_dir() != directory:
            raise HostError("filesystem sandbox path has invalid scope or type")
        return resolved

    def quoted(value: Path) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    writable = path(policy.private_write_root, directory=True)
    if not writable.is_relative_to(path(attempt, directory=True)):
        raise HostError("filesystem sandbox writes must stay inside the private attempt")
    roots = [path(p, directory=True) for p in (*policy.read_roots, *policy.bootstrap_roots)]
    # dyld/exec may inspect both a declared symlink and its resolved artifact.
    files = list(dict.fromkeys(p for raw in policy.bootstrap_files
                               for p in (Path(raw), path(raw, directory=False))))
    rules = ["(version 1)", "(deny default)", "(allow process*)", "(allow sysctl-read)"]
    # Bootstrap needs ancestor metadata and the root directory itself. The
    # root literal never grants its children's file contents or home listings.
    ancestors = sorted({parent for p in (*roots, *files, writable) for parent in p.parents}, key=str)
    rules.extend(f'(allow file-read-metadata (literal "{quoted(p)}"))' for p in ancestors)
    rules.append('(allow file-read* (literal "/"))')
    rules.extend(f'(allow file-read* (subpath "{quoted(p)}"))' for p in (*roots, writable))
    rules.extend(f'(allow file-read* (literal "{quoted(p)}"))' for p in files)
    rules.extend(f'(allow file-map-executable (subpath "{quoted(path(p, directory=True))}"))'
                 for p in policy.bootstrap_roots)
    rules.extend(f'(allow file-map-executable (literal "{quoted(p)}"))' for p in files)
    rules.append(f'(allow file-write* (subpath "{quoted(writable)}"))')
    rules.append('(allow file-write* (literal "/dev/null"))')
    return "\n".join(rules) + "\n"


def _network_sandbox_argv(
    argv: list[str], attempt: Path, endpoint: str | None, *,
    filesystem_policy: _FilesystemSandboxPolicy | None = None,
    require_filesystem_confinement: bool = False,
) -> list[str]:
    """Wrap broker launches, or an explicitly required filesystem experiment.

    Required filesystem configuration is independent of a broker endpoint and
    fails closed here. Broad product launch-site integration remains separate.
    """
    filesystem_required = require_filesystem_confinement or filesystem_policy is not None
    if filesystem_required and filesystem_policy is None:
        raise HostError("required filesystem sandbox policy is missing")
    if not endpoint and not filesystem_required:
        return argv
    if sys.platform != "darwin":
        if filesystem_required:
            raise HostError("required filesystem sandbox needs the macOS backend")
        raise HostError("host-managed provider broker requires an OS network sandbox")
    sandbox = shutil.which("sandbox-exec")
    if not sandbox:
        if filesystem_required:
            raise HostError("required filesystem sandbox needs sandbox-exec")
        raise HostError("host-managed provider broker requires macOS sandbox-exec")
    if filesystem_required:
        assert filesystem_policy is not None
        profile = _filesystem_sandbox_profile(filesystem_policy, attempt)
        if endpoint:
            raise HostError("filesystem sandbox experiment does not authorize a network endpoint")
        return [sandbox, "-p", profile, *argv]
    parsed = urlsplit(endpoint)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port is None:
        raise HostError("host-managed provider broker endpoint must be loopback")
    # sandbox-exec receives the profile as an argument; escape only the paths
    # that are host-selected and never include provider input or credentials.
    def quote(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    profile = (
        '(version 1) (deny default) '
        '(allow process*) (allow file-read*) (allow sysctl-read) '
        # Read-only CFPreferences access. Codex (and other macOS CLIs) load
        # managed preferences through cfprefsd at startup; without these two
        # mach services and read-only cfprefs shared memory they exit with
        # "Failed to synchronize managed preferences" before any model call.
        '(allow mach-lookup (global-name "com.apple.cfprefsd.daemon") (global-name "com.apple.cfprefsd.agent")) '
        '(allow ipc-posix-shm-read-data (ipc-posix-name-prefix "apple.cfprefs")) '
        f'(allow file-write* (subpath "{quote(str(attempt))}")) '
        '(allow file-write* (subpath "/tmp")) '
        f'(allow file-write* (subpath "{quote(str(Path("/tmp").resolve()))}")) '
        '(allow file-write* (literal "/dev/null")) '
        f'(allow network-outbound (remote tcp "localhost:{parsed.port}"))'
    )
    return [sandbox, "-p", profile, *argv]


def _native_network_command(record: "CapabilityRecord") -> bool:
    """Whether a network-required manifest escapes Python hook observability."""
    if record.definition.command is None:
        return False
    metadata = record.definition.metadata
    if bool(metadata.get("native")) or str(metadata.get("execution_kind", "")).lower() == "native":
        return True
    first = str(record.definition.command.argv[0] if record.definition.command.argv else "").lower()
    return first not in {"{python_exec}", sys.executable.lower(), "python", "python3"} and not first.endswith("/python") and not first.endswith("/python3")


def _enforceable_network_gateway(policy: Mapping[str, Any] | None) -> bool:
    """Accept native traffic only when a proxy/broker proves enforcement + observation."""
    if not isinstance(policy, Mapping):
        return False
    enforcement = policy.get("enforcement")
    if isinstance(enforcement, Mapping):
        kind = str(enforcement.get("kind", "")).lower()
        if kind in {"proxy", "broker"} and bool(enforcement.get("enforced")) and bool(enforcement.get("observable")):
            return True
    for key in ("proxy", "broker"):
        value = policy.get(key)
        if isinstance(value, Mapping) and bool(value.get("enforced")) and bool(value.get("observable")):
            return True
    return bool(policy.get("proxy_enforced")) and bool(policy.get("proxy_observable")) and bool(policy.get("proxy"))


def _hivemind_source_preflight(record: "CapabilityRecord") -> dict[str, Any] | None:
    """Require Hivemind to come from a clean, revision-pinned checkout.

    Hivemind is an external provider pack.  Its public read key must not turn
    an arbitrary dirty install into an advertised capability.  The managed
    source inventory performs the immutable revision/digest admission; this
    boundary rechecks checkout cleanliness immediately before readiness.
    """
    if str(record.definition.metadata.get("source_pack") or "") != "hivemind":
        return None
    raw_root = record.definition.metadata.get("pack_root")
    pack_root = Path(str(raw_root)).expanduser().resolve() if raw_root else None
    if pack_root is None or not pack_root.is_dir():
        return {"ok": False, "reason": "hivemind source root is unavailable"}
    checkout = next((candidate for candidate in (pack_root, *pack_root.parents) if (candidate / ".git").exists()), None)
    if checkout is None:
        return {"ok": False, "reason": "hivemind source is not a Git checkout"}
    try:
        status = subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain=v1"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        revision = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "reason": "hivemind source pin could not be verified"}
    if status.stdout.strip():
        return {"ok": False, "reason": "hivemind source checkout is dirty"}
    if not revision:
        return {"ok": False, "reason": "hivemind source checkout has no pinned revision"}
    expected_revision = str(record.definition.metadata.get("commit_sha") or "")
    if expected_revision and revision != expected_revision:
        return {"ok": False, "reason": "hivemind source revision does not match pinned commit", "revision": revision, "expected_revision": expected_revision}
    return {"ok": True, "checkout": str(checkout), "revision": revision}


def _source_digest(root: Path) -> str:
    """Hash the complete executor source tree without retaining a source path in a task."""
    entries: list[tuple[str, str]] = []
    if root.is_file():
        return hashlib.sha256(root.read_bytes()).hexdigest()
    for path in sorted(root.rglob("*")):
        if (not path.is_file() or "__pycache__" in path.relative_to(root).parts
                or path.suffix in {".pyc", ".pyo"}):
            continue
        entries.append((str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest()))
    return _canonical_digest(entries)


def _attempt_tree_bytes(root: Path) -> int:
    """Count owned attempt bytes without following symlink escapes."""
    total = 0
    try:
        for path in root.rglob("*"):
            # Renderers may remove completed frame files or workspaces while
            # the live guard is walking the attempt. ``rglob()``, ``is_file``
            # and ``stat()`` are not atomic; a vanished path is a normal scan
            # race, not a renderer failure.
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                total += int(path.stat().st_size)
            except FileNotFoundError:
                continue
    except FileNotFoundError:
        # A renderer-owned directory can disappear during rglob iteration.
        # Return the bounded point-in-time total rather than failing the task.
        return total
    return total


def _storage_tree_bytes(root: Path) -> int:
    """Count regular files in a bounded subtree without following symlinks."""
    if not root.is_dir():
        return 0
    total = 0
    try:
        for path in root.rglob("*"):
            # Atomic writers use sibling ``*.tmp`` files.  Those bytes are
            # scratch during the write and must not be charged to the final-output
            # bucket while the child is still running.
            # The output tree can also contain renderer-owned intermediates for
            # legacy/direct callers. Treat a path removed during traversal as
            # absent from this point-in-time accounting sample.
            try:
                if path.is_symlink() or not path.is_file() or path.name.endswith(".tmp"):
                    continue
                total += int(path.stat().st_size)
            except FileNotFoundError:
                continue
    except FileNotFoundError:
        return total
    return total


_STORAGE_DIAGNOSTIC_PATH_SAMPLE_LIMIT = 32
_RENDERER_WORKSPACE_MARKERS = (
    ".render-service",
    ".render-inputs-",
    ".remotion-runtime-",
    "astrid-render-assets-",
)


def _storage_path_class(relative: str, *, output: bool) -> str:
    """Classify a relative attempt path without exposing its absolute root."""
    if output:
        return "output"
    parts = relative.split("/")
    if parts[0] == "managed-objects":
        return "managed_inputs"
    if parts[0] == "inputs":
        return "canonical_inputs"
    if any(
        marker in part
        for part in parts
        for marker in _RENDERER_WORKSPACE_MARKERS
    ):
        return "renderer_workspace"
    if parts[0] == "outputs":
        return "output_temporary"
    return "other_scratch"


def _storage_envelope_measurement(
    root: Path,
    output_root: Path,
    *,
    path_sample_limit: int = _STORAGE_DIAGNOSTIC_PATH_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """Capture bounded, path-relative storage evidence for an overrun."""
    root = Path(root)
    output_root = Path(output_root)
    output_resolved = output_root.resolve(strict=False)
    total_bytes = 0
    output_bytes = 0
    scratch_bytes = 0
    vanished_files = 0
    files = 0
    scratch_files = 0
    classes: dict[str, dict[str, int]] = {}
    largest: list[tuple[int, str, str]] = []
    try:
        for path in root.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                size = int(path.stat().st_size)
                relative = path.relative_to(root).as_posix()
                is_output = (
                    not path.name.endswith(".tmp")
                    and path.resolve(strict=False).is_relative_to(output_resolved)
                )
                path_class = _storage_path_class(relative, output=is_output)
                total_bytes += size
                files += 1
                if is_output:
                    output_bytes += size
                else:
                    scratch_bytes += size
                    scratch_files += 1
                bucket = classes.setdefault(path_class, {"files": 0, "bytes": 0})
                bucket["files"] += 1
                bucket["bytes"] += size
                if not is_output and path_sample_limit:
                    candidate = (size, relative, path_class)
                    if len(largest) < path_sample_limit:
                        heapq.heappush(largest, candidate)
                    elif candidate > largest[0]:
                        heapq.heapreplace(largest, candidate)
            except FileNotFoundError:
                vanished_files += 1
                continue
    except FileNotFoundError:
        vanished_files += 1
    except OSError as exc:
        return {
            "measurement_error": type(exc).__name__,
            "vanished_file_count": vanished_files,
        }
    largest.sort(key=lambda item: (-item[0], item[1], item[2]))
    return {
        "observed_total_bytes": total_bytes,
        "observed_output_bytes": output_bytes,
        "observed_scratch_bytes": scratch_bytes,
        "file_count": files,
        "vanished_file_count": vanished_files,
        "path_classes": {key: classes[key] for key in sorted(classes)},
        "largest_scratch_paths": [
            {"path": path, "bytes": size, "classification": classification}
            for size, path, classification in largest[:path_sample_limit]
        ],
        "largest_scratch_paths_truncated": scratch_files > path_sample_limit,
    }


def _fixed_request_scope(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Return a host-owned model/mode/execution scope, if declared."""
    raw = metadata.get("fixed_inputs")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or not raw:
        raise HostError("capability fixed_inputs must be a non-empty object")
    scope = dict(raw)
    if any(not isinstance(key, str) or not key for key in scope):
        raise HostError("capability fixed_inputs keys must be non-empty strings")
    return scope


def _admitted_spec_envelope(raw_spec: Any) -> Mapping[str, Any]:
    """Return the immutable capability request from a runtime task envelope.

    Runtime task snapshots have one stable wrapper around the immutable
    capability request::

        task.spec         = admission envelope
        task.spec.spec    = capability request
        task.spec.spec.inputs

    A few older in-process callers provide the capability request directly.
    Accept that compatibility shape, but never recursively unwrap arbitrary
    mappings or silently accept conflicting duplicate fields.
    """
    if not isinstance(raw_spec, Mapping):
        raise HostError("runtime task is missing its immutable spec envelope")
    nested = raw_spec.get("spec")
    if nested is None:
        return raw_spec
    if not isinstance(nested, Mapping):
        raise HostError("runtime task immutable spec envelope has a non-object spec")
    for key in (
        "capability_id",
        "kind",
        "inputs",
        "params",
        "outputs",
        "runtime_dependencies",
        "authority_context",
    ):
        if key in raw_spec and key in nested and raw_spec[key] != nested[key]:
            raise HostError(f"runtime task spec has conflicting {key!r} values")
    return nested


def _admitted_task_spec(task_data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Decode the canonical immutable request from a task snapshot."""
    return _admitted_spec_envelope(task_data.get("spec"))


def _assert_verified_placement_binding(
    binding: Mapping[str, Any], target: Mapping[str, Any]
) -> None:
    """Require Runtime's exact credential-backed placement projection.

    The host consumes this evidence; it does not mint or upgrade selector,
    config, environment, or self-reported target data into evidence.
    """

    actual = binding.get("actual_target")
    if not isinstance(actual, Mapping):
        raise HostError("task target binding has no credential-verified actual target")
    verification = binding.get("verification")
    if not isinstance(verification, Mapping) or set(verification) != {
        "method", "evidence_digest", "verified",
    }:
        raise HostError("task target binding has no credential-verified placement verification")
    if verification.get("method") != "credential_claim" or verification.get("verified") is not True:
        raise HostError("task target binding placement verification is not a credential claim")
    digest = verification.get("evidence_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise HostError("task target binding placement verification digest is invalid")
    incarnation = binding.get("executor_incarnation")
    if not isinstance(incarnation, str) or not incarnation.strip() or len(incarnation) > 256:
        raise HostError("task target binding has no valid executor incarnation")

    selected_kind = target.get("kind")
    actual_kind = actual.get("kind")
    resolved = binding.get("resolved_target")
    if not isinstance(resolved, Mapping) or resolved != target:
        raise HostError("credential-verified resolved target disagrees with execution_request")
    if selected_kind != "default" and actual_kind != selected_kind:
        raise HostError("credential-verified actual target kind disagrees with execution_request")
    for key, expected in target.items():
        if key != "kind" and actual.get(key) != expected:
            raise HostError(f"credential-verified actual target {key} disagrees with execution_request")


def _execution_contract(
    task_data: Mapping[str, Any],
    *,
    runtime_session_id: str | None = None,
) -> dict[str, Any] | None:
    """Validate the carried request before the worker spends or opens a session."""
    envelope = task_data.get("spec")
    # A small set of legacy in-process callers intentionally provide only the
    # task identity while exercising capability-level admission guards.  With
    # no carried contract there is nothing to validate here; preserve that
    # compatibility path and let the ordinary capability checks continue.
    if envelope is None:
        if task_data.get("execution_request") is not None:
            raise HostError("execution_request requires its immutable spec envelope")
        return None
    if not isinstance(envelope, Mapping):
        raise HostError("runtime task is missing its immutable spec envelope")
    request = task_data.get("execution_request")
    nested = _admitted_spec_envelope(envelope)
    for carried in (envelope.get("execution_request"), nested.get("execution_request")):
        if request is None:
            request = carried
        elif carried is not None and _canonical_digest(request) != _canonical_digest(carried):
            raise HostError("runtime task has conflicting execution_request values")
    if request is None:
        return None
    if not envelope or ("inputs" in nested and not isinstance(nested["inputs"], Mapping)):
        raise HostError("execution_request requires a usable immutable spec envelope")
    try:
        normalized = normalize_execution_request(request)
    except ValueError as exc:
        raise HostError(f"invalid execution_request: {exc}") from exc
    if normalized is None or not isinstance(request, Mapping):
        raise HostError("invalid execution_request")
    if _canonical_digest(dict(request)) != _canonical_digest(normalized):
        raise HostError("runtime task execution_request is not normalized")

    declared = normalized.get("inputs", [])
    authorized = task_data.get("input_object_ids")
    if not isinstance(authorized, (list, tuple)):
        raise HostError("execution_request requires task input_object_ids")
    def object_id(value: Any) -> str:
        if not isinstance(value, str):
            raise HostError("execution_request input object IDs must be strings")
        return value.removeprefix("sha256:")
    expected_ids = [object_id(item["object_id"]) for item in declared]
    actual_ids = [object_id(item) for item in authorized]
    if len(actual_ids) != len(expected_ids) or set(actual_ids) != set(expected_ids):
        raise HostError("task input_object_ids do not match execution_request inputs")
    inputs = nested.get("inputs")
    if not isinstance(inputs, Mapping):
        inputs = {}
    declared_names = {item["name"] for item in declared}
    for name, descriptor in inputs.items():
        if isinstance(descriptor, Mapping) and (
            "digest" in descriptor or "object_id" in descriptor
        ) and name not in declared_names:
            raise HostError(f"task spec managed input {name!r} is absent from execution_request")
    for item in declared:
        name = item["name"]
        descriptor = inputs.get(name)
        if not isinstance(descriptor, Mapping):
            raise HostError(f"task spec input {name!r} is missing its execution_request descriptor")
        # ``object_id`` is the canonical managed-object digest.  ``digest``
        # is an optional repeated witness for callers that want the wire
        # envelope to spell it out, so its absence must not make an otherwise
        # valid frozen input unrunnable.
        digest = descriptor.get("digest") or descriptor.get("object_id")
        if digest is None:
            raise HostError(f"task spec input {name!r} is missing its materialization digest")
        if object_id(digest) != object_id(item["object_id"]):
            raise HostError(f"task spec input {name!r} disagrees with execution_request object_id")
        if descriptor.get("object_id") is not None and object_id(descriptor["object_id"]) != object_id(item["object_id"]):
            raise HostError(f"task spec input {name!r} disagrees with execution_request object_id")
        if descriptor.get("filename") != item["filename"]:
            raise HostError(f"task spec input {name!r} disagrees with execution_request filename")

    workflow = normalized.get("workflow")
    if isinstance(workflow, Mapping):
        workflow_spec = nested.get("workflow")
        workflow_id = nested.get("workflow_id")
        if workflow_id is None and isinstance(workflow_spec, Mapping):
            workflow_id = workflow_spec.get("id")
        if workflow_id is None:
            workflow_id = task_data.get("capability")
        if workflow_id != workflow["id"]:
            raise HostError("task spec workflow id disagrees with execution_request")
        identity = nested.get("workflow_contract_digest")
        if identity is None and isinstance(workflow_spec, Mapping):
            identity = workflow_spec.get("contract_digest")
        if identity != workflow["contract_digest"]:
            raise HostError("task spec workflow contract digest disagrees with execution_request")

    target = normalized["target"]
    binding = task_data.get("execution_binding") or task_data.get("placement_binding") or task_data.get("binding")
    if not isinstance(binding, Mapping):
        raise HostError("execution_request target requires an observed task binding")
    if binding.get("status") not in (None, "claimed"):
        raise HostError("task target binding is not claimed")
    required_identity = {
        "task_id": task_data.get("id") or task_data.get("task_id"),
        "run_id": task_data.get("run_id"),
        "attempt_id": task_data.get("attempt_id"),
        "lease_id": task_data.get("lease_id"),
        "fence": task_data.get("fence"),
        "executor_id": task_data.get("executor_id"),
        "runtime_epoch": task_data.get("runtime_epoch"),
        "capability_id": task_data.get("capability") or task_data.get("capability_id"),
    }
    for field, expected in required_identity.items():
        if expected is not None and binding.get(field) != expected:
            raise HostError(f"task target binding {field} disagrees with the claimed identity")
    if not isinstance(binding.get("binding_id"), str) or not binding["binding_id"]:
        raise HostError("task target binding has no Runtime-issued binding_id")
    if not isinstance(binding.get("session_id"), str) or not binding["session_id"]:
        raise HostError("task target binding has no Runtime session identity")
    expected_session = task_data.get("runtime_session_id") or runtime_session_id
    if expected_session is not None and binding["session_id"] != expected_session:
        raise HostError("task target binding session_id disagrees with the Runtime session")
    resolved_target = binding.get("resolved_target")
    if not isinstance(resolved_target, Mapping):
        raise HostError("task target binding has no resolved target identity")
    kind = target["kind"]
    if binding.get("target_kind") not in (None, kind) and binding.get("kind") not in (None, kind):
        raise HostError("task target binding kind disagrees with execution_request")
    identity_fields = {
        "default": (),
        "profile": (("id", "target_id"), ("profile_alias", "target_id")),
        "machine": (("id", "target_id"), ("machine_id", "target_id")),
        "runpod": (("pod_id", "pod_id"), ("provider_account_ref", "provider_account_ref")),
    }[kind]
    for requested, observed in identity_fields:
        if requested in target and target[requested] is not None and binding.get(observed) != target[requested]:
            raise HostError(f"task target binding {observed} disagrees with execution_request")
    for key in ("profile_revision", "profile_digest", "release_digest"):
        if key in target and binding.get(key) != target[key]:
            raise HostError(f"task target binding {key} disagrees with execution_request")
    for key in ("storage", "mounts"):
        if key in target and resolved_target.get(key) != target[key]:
            raise HostError(f"task target binding {key} disagrees with execution_request")
    _assert_verified_placement_binding(binding, target)
    return normalized


@dataclass
class _ReviewIntervalAccounting:
    """Credit only gaps bracketed by two complete, matching live readbacks."""

    verify: Callable[[], frozenset[tuple[str, ...]]]
    credited_seconds: float = 0.0
    previous_claims: frozenset[tuple[str, ...]] = frozenset()
    previous_end: float | None = None
    _lock: Any = field(default_factory=threading.Lock, repr=False)
    _generation: int = field(default=0, init=False, repr=False)
    _inflight: int = field(default=0, init=False, repr=False)

    def sample(self, *, executing: bool) -> None:
        cycle_start = time.monotonic()
        # Reserve local state only. Runtime reads and callbacks must be able to
        # reenter or overlap without waiting for this sample's verification.
        with self._lock:
            overlapping = self._inflight != 0
            self._generation += 1
            generation = self._generation
            self._inflight += 1
            if overlapping:
                self.previous_claims = frozenset()
                self.previous_end = None
            previous_claims, previous_end = self.previous_claims, self.previous_end
        try:
            claims = self.verify()
            cycle_end = time.monotonic()
        except BaseException:
            # A failure invalidates every outstanding result, including a late
            # success. Propagate the original failure after releasing the lock.
            with self._lock:
                self._inflight -= 1
                self._generation += 1
                self.previous_claims = frozenset()
                self.previous_end = None
            raise
        with self._lock:
            self._inflight -= 1
            if overlapping or generation != self._generation:
                # Neither an overlapping success nor an older result can revive
                # continuity. A later independent cycle must establish it anew.
                self._generation += 1
                self.previous_claims = frozenset()
                self.previous_end = None
                return
            if (executing and previous_end is not None
                    and previous_claims.intersection(claims)):
                # Count a union, even when several reviews overlap. Initial
                # admission, verification and uncertain terminal tails cost time.
                self.credited_seconds += max(0.0, cycle_start - previous_end)
            self.previous_claims = claims if executing else frozenset()
            self.previous_end = cycle_end if executing else None


@dataclass
class _AttemptLifetime:
    """One host-owned decision; only admitted execution can outlive setup."""

    policy: ExecutionGuardPolicy
    started_at: float
    runtime_limit: float | None
    authority: Callable[[], None] | None = None
    phase: str = "setup"
    collection_deadline: float | None = None
    collection_seconds: float | None = None
    review: _ReviewIntervalAccounting | None = None

    def deadline(self) -> float | None:
        ordinary = self.started_at + self.policy.deadline_seconds
        if self.review is not None and self.phase != "setup":
            ordinary += self.review.credited_seconds
        if self.authority is not None and self.phase == "execution":
            deadline = None
        elif self.authority is not None and self.phase == "collection":
            deadline = self.collection_deadline
        else:
            deadline = ordinary
        if self.runtime_limit is not None:
            absolute = self.started_at + self.runtime_limit
            deadline = absolute if deadline is None else min(deadline, absolute)
        if self.collection_deadline is not None:
            deadline = self.collection_deadline if deadline is None else min(deadline, self.collection_deadline)
        return deadline

    def assert_authority(self) -> None:
        if self.authority is not None:
            self.authority()
        if self.review is not None:
            self.review.sample(executing=self.phase == "execution")

    def expired(self) -> bool:
        deadline = self.deadline()
        return deadline is not None and self.policy.deadline_expired(deadline)

    def assert_deadline(self) -> None:
        self.assert_authority()
        deadline = self.deadline()
        if deadline is not None:
            self.policy.assert_deadline(deadline)

    def begin_execution(self) -> None:
        self.assert_deadline()
        self.phase = "execution"

    def begin_collection(self, collection_limit: float | None) -> None:
        # Freeze once at execution completion, never on a poll or SDK wait.
        if self.phase != "execution":
            raise HostError("invalid attempt lifetime phase")
        self.assert_deadline()
        self.phase = "collection"
        if collection_limit is not None or self.authority is not None or self.review is not None:
            self.collection_seconds = collection_limit if collection_limit is not None else self.policy.deadline_seconds
            self.collection_deadline = time.monotonic() + self.collection_seconds

    def receipt(self) -> dict[str, Any]:
        return {
            "lifetime": ("lease_bound_interactive" if self.authority is not None else
                         "verified_review_intervals" if self.review is not None else "bounded"),
            "deadline_seconds": (self.runtime_limit if self.authority is not None else
                                 min(self.policy.deadline_seconds, self.runtime_limit)
                                 if self.runtime_limit is not None else self.policy.deadline_seconds),
            "setup_seconds": min(self.policy.deadline_seconds, self.runtime_limit)
            if self.runtime_limit is not None else self.policy.deadline_seconds,
            "collection_seconds": self.collection_seconds,
            **({"verified_review_seconds": self.review.credited_seconds} if self.review is not None else {}),
        }


def _interactive_admission_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Pin admission while allowing only Runtime's top-level registry updates."""
    if not isinstance(spec, Mapping):
        raise HostError("invalid interactive admission spec")
    return {key: value for key, value in spec.items() if key != "derived_input_registry"}


def _interactive_review_authority(
    host: Any, task: Mapping[str, Any], record: CapabilityRecord, *,
    attempt_id: str, lease_id: str, fence: int, runtime_epoch: int,
) -> Callable[[], None] | None:
    """Fail closed unless public Runtime reads prove the entire M09 conjunction.

    Private lineage stays in this closure. No worker input or environment carries
    it, and no authority issuance or parent heartbeat is needed by a child.
    """
    from ._child_bridge import _snapshot_grant, task_resource
    from astrid.sdk._child_bridge import _validate_child_policy

    parent_id = "iteration.experiment_review_session"
    child_id = "editorial.human_review"
    if record.id not in {parent_id, child_id}:
        return None

    def effective(value: Mapping[str, Any], capability: CapabilityRecord) -> dict[str, Any]:
        inputs = _admitted_task_spec(value).get("inputs", {})
        if not isinstance(inputs, Mapping):
            raise HostError("invalid interactive inputs")
        result = {port.name: port.default for port in capability.definition.inputs if port.default is not None}
        result.update(inputs)
        return result

    def live(value: Mapping[str, Any], expected: Mapping[str, Any], capability: CapabilityRecord) -> Mapping[str, Any]:
        binding = value.get("execution_binding")
        spec = value.get("spec")
        if (not isinstance(binding, Mapping) or binding.get("status") != "claimed"
                or not isinstance(spec, Mapping)
                or value.get("state", value.get("status")) != "running"
                or value.get("task_id", value.get("id")) != expected["task_id"]
                or value.get("attempt_id") != expected["attempt_id"]
                or type(value.get("runtime_epoch")) is not int or value["runtime_epoch"] != runtime_epoch
                or type(value.get("lease_fence")) is not int or value["lease_fence"] != expected["fence"]
                or value.get("capability_id", value.get("capability")) != capability.id
                or value.get("capability_digest") != capability.capability_digest
                or spec.get("capability_digest") != capability.capability_digest
                or spec.get("delegation_closed_attempt_id") == expected["attempt_id"]):
            raise HostError("interactive attempt authority changed")
        for name, pinned in expected.items():
            if binding.get(name) != pinned or (name in {"fence", "runtime_epoch"}
                                               and type(binding.get(name)) is not int):
                raise HostError("interactive attempt fence changed")
        expiry = value.get("lease_expires_at")
        if type(expiry) is not str:
            raise HostError("missing interactive lease deadline")
        parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed <= datetime.now(timezone.utc):
            raise HostError("interactive lease ended")
        contract = _execution_contract(value)
        target = contract.get("target") if contract else None
        if (not isinstance(target, Mapping) or target.get("kind") != "machine"
                or type(target.get("id")) is not str or not target["id"]
                or binding.get("placement_version") is None
                or type(binding["placement_version"]) is not int
                or binding["placement_version"] < 0):
            raise HostError("interactive review requires verified machine placement")
        _assert_verified_placement_binding(binding, target)
        return binding

    try:
        if type(runtime_epoch) is not int or runtime_epoch < 1 or type(fence) is not int:
            return None
        parent_record = host.capabilities[parent_id]
        child_record = host.capabilities[child_id]
        if record.capability_digest != task.get("capability_digest", task.get("spec", {}).get("capability_digest")):
            return None
        own_id = task.get("task_id", task.get("id"))
        own_expected = {"task_id": own_id, "attempt_id": attempt_id, "lease_id": lease_id,
                        "fence": fence, "runtime_epoch": runtime_epoch}
        own = task_resource(host.client.task(own_id))
        own_binding = live(own, own_expected, record)
        if (_canonical_digest(_interactive_admission_spec(own.get("spec")))
                != _canonical_digest(_interactive_admission_spec(task.get("spec")))
                or _canonical_digest(own_binding) != _canonical_digest(task.get("execution_binding"))):
            return None
        own_inputs = effective(own, record)
        if type(own_inputs.get("timeout")) is not int or own_inputs["timeout"] != 0:
            return None
        lineage = own["spec"].get("delegated_parent") if record.id == child_id else None
        if record.id == child_id:
            if (not isinstance(lineage, Mapping) or set(lineage) != {
                    "parent_task_id", "parent_attempt_id", "parent_lease_id", "parent_fence", "runtime_epoch",
                    "executor_id", "parent_placement", "parent_effective_target", "parent_placement_version",
                    "project_id", "policy_digest"}
                    or type(lineage.get("parent_fence")) is not int
                    or type(lineage.get("runtime_epoch")) is not int
                    or type(lineage.get("parent_placement_version")) is not int
                    or not all(type(lineage.get(k)) is str and lineage[k] for k in (
                        "parent_task_id", "parent_attempt_id", "parent_lease_id", "executor_id", "policy_digest"))):
                return None
            parent_expected = {"task_id": lineage["parent_task_id"], "attempt_id": lineage["parent_attempt_id"],
                               "lease_id": lineage["parent_lease_id"], "fence": lineage["parent_fence"],
                               "runtime_epoch": lineage["runtime_epoch"], "executor_id": lineage["executor_id"]}
            parent = task_resource(host.client.task(lineage["parent_task_id"]))
        else:
            parent, parent_expected = own, own_expected
        parent_binding = live(parent, parent_expected, parent_record)
        parent_inputs = effective(parent, parent_record)
        if (type(parent_inputs.get("timeout")) is not int or parent_inputs["timeout"] != 0
                or parent_inputs.get("skip_server") is not False):
            return None
        runs_dir = parent_inputs.get("runs_dir")
        if type(runs_dir) is not str or not Path(runs_dir).is_absolute() or not Path(runs_dir).is_dir():
            return None
        policy = _validate_child_policy(parent["spec"].get("child_delegation"))
        required_limits = {"max_children", "max_active_children", "max_derived_objects", "max_derived_bytes",
                           "max_child_inputs", "max_child_bytes", "max_recoverable_snapshots",
                           "max_recoverable_bytes", "max_snapshot_bytes"}
        if policy is None or set(policy.get("limits", {})) != required_limits:
            return None
        # These are the admitted D18 readback ceilings, not execution defaults.
        ceilings = {"max_children": 4096, "max_active_children": 64, "max_derived_objects": 1024,
                    "max_derived_bytes": 4 * 1024**3, "max_child_inputs": 256, "max_child_bytes": 256 * 1024**2,
                    "max_recoverable_snapshots": 1024, "max_recoverable_bytes": 4 * 1024**3,
                    "max_snapshot_bytes": 64 * 1024**2}
        if any(policy["limits"][key] > ceilings[key] for key in required_limits):
            return None
        caps = policy["capabilities"]
        if any(not isinstance(cap, Mapping) or set(cap) != {"capability_id", "capability_digest"}
               or type(cap.get("capability_id")) is not str or not cap["capability_id"]
               or type(cap.get("capability_digest")) is not str
               or re.fullmatch(r"sha256:[0-9a-f]{64}", cap["capability_digest"]) is None for cap in caps):
            return None
        if len({cap["capability_id"] for cap in caps}) != len(caps):
            return None
        for target in policy["targets"]:
            normalized = normalize_execution_request({"schema_version": 1, "target": target})
            if _canonical_digest(target) != _canonical_digest(normalized["target"]):
                return None
        roots = policy["input_object_ids"]
        if (any(type(oid) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", oid) is None for oid in roots)
                or len(set(roots)) != len(roots) or not set(roots) <= set(parent.get("input_object_ids", []))):
            return None
        grant = {"capability_id": child_id, "capability_digest": child_record.capability_digest,
                 "output_ports": ["state_result"]}
        if policy.get("recoverable_outputs") != [grant]:
            return None
        if record.id == child_id:
            frozen = _snapshot_grant(own)
            expected_grant = {**grant, "limits": {key: policy["limits"][key] for key in (
                "max_snapshot_bytes", "max_recoverable_bytes", "max_recoverable_snapshots")},
                "parent_attempt_id": parent_expected["attempt_id"], "policy_digest": _canonical_digest(policy)}
            placement = {"actual": parent_binding["actual_target"], "verification": parent_binding["verification"],
                         "executor_incarnation": parent_binding["executor_incarnation"]}
            if (frozen != expected_grant or lineage["policy_digest"] != _canonical_digest(policy)
                    or lineage["runtime_epoch"] != runtime_epoch
                    or lineage["project_id"] != parent.get("project_id") or own.get("project_id") != parent.get("project_id")
                    or lineage["parent_placement"] != placement
                    or lineage["parent_effective_target"] != parent_binding.get("effective_target")
                    or lineage["parent_placement_version"] != parent_binding["placement_version"]
                    or own_binding["actual_target"] != parent_binding["actual_target"]
                    or own_binding.get("resolved_target") != parent_binding.get("effective_target")):
                return None
        # Pin immutable admission and binding, while allowing the live expiry to
        # advance on the parent's own heartbeats. Different executors are valid.
        pinned = [(own_id, own_expected, record, json.loads(json.dumps(_interactive_admission_spec(own["spec"]))),
                   json.loads(json.dumps(own_binding)), own.get("execution_request"), own.get("project_id"))]
        if record.id == child_id:
            pinned.append((parent_expected["task_id"], parent_expected, parent_record,
                           json.loads(json.dumps(_interactive_admission_spec(parent["spec"]))), json.loads(json.dumps(parent_binding)),
                           parent.get("execution_request"), parent.get("project_id")))
        pinned = [(tid, expected, capability, spec, binding, json.loads(json.dumps(request)), project)
                  for tid, expected, capability, spec, binding, request, project in pinned]

        def validate() -> None:
            health = host.client.health()
            epoch = health.get("runtime_epoch") if isinstance(health, Mapping) else getattr(health, "runtime_epoch", None)
            if type(epoch) is not int or epoch != runtime_epoch:
                raise HostError("interactive Runtime epoch changed")
            for tid, expected, capability, admitted_spec, admitted_binding, admitted_request, project in pinned:
                current = task_resource(host.client.task(tid))
                binding = live(current, expected, capability)
                if (_canonical_digest(_interactive_admission_spec(current["spec"])) != _canonical_digest(admitted_spec)
                        or _canonical_digest(binding) != _canonical_digest(admitted_binding)
                        or _canonical_digest(current.get("execution_request")) != _canonical_digest(admitted_request)
                        or current.get("project_id") != project):
                    raise HostError("interactive admission or placement changed")

        validate()
        return validate
    except Exception:
        # Missing/malformed admission or failed initial readback gets the
        # ordinary bound. Once admitted, any later loss cancels the attempt.
        return None


class _DiscoveryCollection:
    """Private metadata preparation under one persisted discovery grant.

    The collection-local client caps metadata acquisition before generated typed
    projection, including HTTP errors. Finite reads admit at most the remaining
    byte budget plus one sentinel byte; this is not a process-memory bound.
    No object authority, action input or new transport operation is created here.
    """

    def __init__(self, client: Any, parent: Mapping[str, Any], *, attempt_id: str,
                 lease_id: str, fence: int, runtime_epoch: int,
                 cancelled: Callable[[], bool]):
        from astrid.sdk._child_bridge import _validate_child_policy

        spec = parent.get("spec")
        if not isinstance(spec, Mapping) or "delegated_parent" in spec:
            raise HostError("discovery requires an admitted root parent")
        try:
            policy = _validate_child_policy(spec.get("child_delegation"))
        except Exception as exc:
            raise HostError("invalid persisted discovery policy") from exc
        if policy is None or "discovery_grant" not in policy:
            raise HostError("discovery requires a persisted finite grant")
        self.grant = copy.deepcopy(policy["discovery_grant"])
        if (parent.get("project_id") != self.grant["project_id"]
                or parent.get("capability_id", parent.get("capability")) != self.grant["capability_id"]
                or parent.get("capability_digest") != self.grant["capability_digest"]
                or spec.get("capability_digest") != self.grant["capability_digest"]
                or type(fence) is not int or fence < 1
                or type(runtime_epoch) is not int or runtime_epoch < 1
                or any(type(value) is not str or not value for value in (attempt_id, lease_id))):
            raise HostError("discovery grant disagrees with parent identity")
        self.parent = json.loads(json.dumps(parent, allow_nan=False))
        self.expected = {"task_id": self._id(parent, "task_id", "id"),
                         "attempt_id": attempt_id, "lease_id": lease_id,
                         "fence": fence, "runtime_epoch": runtime_epoch}
        self.cancelled = cancelled
        self.failed = False
        self.read_attempts = 0
        self.examined_rows = 0
        self.raw_metadata_bytes = 0
        self.canonical_metadata_bytes = 0
        self.rejected_read: dict[str, int | str] | None = None
        self._raw: dict[str, Any] | None = None
        self._collected_outputs: tuple[str, ...] | None = None
        self._verified_objects: dict[str, bytes] = {}
        self._prepared_objects: tuple[str, ...] | None = None
        self._object_read = False
        self.acquired_object_bytes = 0
        # Never patch the shared authenticated client used by heartbeat/custody.
        self._reader = copy.copy(client.generated)
        original_request = self._reader._request

        def observed_request(*args: Any, **kwargs: Any) -> Any:
            self._active()
            self.read_attempts += 1
            if not self._object_read:
                limit = self.grant["limits"]["max_discovery_metadata_bytes"]
                if type(limit) is not int or limit <= self.raw_metadata_bytes:
                    self._stop("invalid or exhausted discovery raw metadata budget")
                remaining = limit - self.raw_metadata_bytes

                def metadata_reader(stream: Any) -> bytes:
                    chunks: list[bytes] = []
                    acquired = 0
                    while True:
                        self._active()
                        chunk = stream.read(min(65536, remaining + 1 - acquired))
                        if type(chunk) is not bytes:
                            chunks.clear()
                            chunk = b""
                            self._stop("invalid discovery response body", size=acquired)
                        acquired += len(chunk)
                        if acquired > remaining:
                            chunks.clear()
                            chunk = b""
                            self._stop("discovery raw metadata budget exceeded", size=acquired)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    self.raw_metadata_bytes += acquired
                    body = b"".join(chunks)
                    chunks.clear()
                    try:
                        self._observe_metadata(body)
                    except Exception:
                        body = b""
                        raise
                    return body

                kwargs["response_reader"] = metadata_reader
            try:
                response = original_request(*args, **kwargs)
            except Exception:
                self.failed = True
                raise
            self._active()
            body = response[2]
            if type(body) is not bytes:
                self._stop("invalid discovery response body")
            if self._object_read:
                # Binary bodies are not discovery JSON metadata. The generated
                # transport still acquires them in full before this observer.
                self.acquired_object_bytes += len(body)
                return response
            return response

        self._reader._request = observed_request

    def _observe_metadata(self, body: bytes) -> None:
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in items:
                if key in value:
                    raise ValueError("duplicate discovery JSON key")
                value[key] = item
            return value

        valid = True
        try:
            value = json.loads(body, object_pairs_hook=pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        except (ValueError, UnicodeDecodeError):
            valid = False
        body = b""
        if not valid:
            # Raise outside the decoder's exception context; never retain its
            # truncated document in the terminal policy exception.
            self._stop("invalid discovery metadata JSON")
        if type(value) is not dict:
            self._stop("invalid discovery metadata object")
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.canonical_metadata_bytes += len(canonical)
        if "items" in value:
            if (type(value["items"]) is not list or "next_cursor" not in value
                    or value["next_cursor"] is not None
                    and (type(value["next_cursor"]) is not str or not value["next_cursor"])):
                self._stop("invalid discovery metadata page")
            rows = len(value["items"])
        else:
            rows = 1
        self.examined_rows += rows
        if self.examined_rows > self.grant["limits"]["max_discovery_rows"]:
            self._stop("discovery row budget exceeded")
        self._raw = value

    @staticmethod
    def _id(value: Mapping[str, Any], key: str, alias: str) -> str:
        result = value.get(key, value.get(alias))
        if (type(result) is not str or not result or "*" in result
                or key in value and alias in value and value[key] != value[alias]):
            raise HostError("invalid discovery resource identity")
        return result

    def _stop(self, reason: str, *, size: int = 0) -> None:
        self.failed = True
        if self.rejected_read is None:
            self.rejected_read = {"reason": reason, "attempted_call_index": self.read_attempts,
                                  "accepted_bytes_before_rejection": self.raw_metadata_bytes,
                                  "raw_metadata_bytes_lower_bound": self.raw_metadata_bytes + size}
        raise HostError(reason)

    def _active(self) -> None:
        if self.failed:
            raise HostError("discovery collection already ended")
        if self.cancelled():
            self._stop("discovery parent cancelled")

    def _read(self, operation: str, identifier: str) -> dict[str, Any]:
        self._active()
        self._raw = None
        try:
            getattr(self._reader, operation)(identifier)
            if self._raw is None:
                self._stop("discovery response was not observed")
            return self._raw
        except Exception:
            # Generated parsing, transport and identity failures are terminal;
            # no best-effort retry may bypass a failed budget or cancellation.
            self.failed = True
            raise

    def _parent_authority(self) -> None:
        current = self._read("get_task", self.expected["task_id"])
        binding = current.get("execution_binding")
        spec = current.get("spec")
        if (self._id(current, "task_id", "id") != self.expected["task_id"]
                or current.get("state", current.get("status")) != "running"
                or current.get("attempt_id") != self.expected["attempt_id"]
                or type(current.get("runtime_epoch")) is not int
                or current["runtime_epoch"] != self.expected["runtime_epoch"]
                or type(current.get("lease_fence")) is not int
                or current["lease_fence"] != self.expected["fence"]
                or not isinstance(spec, Mapping) or not isinstance(binding, Mapping)
                or binding.get("status") != "claimed"
                or any(binding.get(key) != value or key in {"fence", "runtime_epoch"}
                       and type(binding.get(key)) is not int for key, value in self.expected.items())
                or spec.get("delegation_closed_attempt_id") == self.expected["attempt_id"]
                or any(current.get(key) != self.parent.get(key) for key in (
                    "project_id", "run_id", "capability_id", "capability_digest", "execution_request"))
                or _canonical_digest(_interactive_admission_spec(spec))
                != _canonical_digest(_interactive_admission_spec(self.parent["spec"]))
                or _canonical_digest(binding) != _canonical_digest(self.parent.get("execution_binding"))):
            self._stop("discovery parent authority changed")
        try:
            expiry = current.get("lease_expires_at")
            if type(expiry) is not str:
                raise ValueError("missing lease expiry")
            deadline = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if deadline.tzinfo is None or deadline <= datetime.now(timezone.utc):
                raise ValueError("expired lease")
        except ValueError:
            self._stop("discovery parent lease ended")

    def collect_metadata(self) -> dict[str, Any]:
        """Resolve only the granted tuple; return private, unprojected metadata.

        Output identities are authoritative historical associations. A newer
        current source-task attempt does not invalidate the granted attempt.
        Staging, byte verification and consumer wiring remain separate steps.
        """
        try:
            self._parent_authority()
            project = self._read("get_project", self.grant["project_id"])
            run = self._read("get_run", self.grant["run_id"])
            source = self._read("get_task", self.grant["task_id"])
            ids = run.get("task_ids")
            if (self._id(project, "project_id", "id") != self.grant["project_id"]
                    or self._id(run, "run_id", "id") != self.grant["run_id"]
                    or run.get("project_id") != self.grant["project_id"]
                    or type(ids) is not list or any(type(value) is not str or not value or "*" in value for value in ids)
                    or len(set(ids)) != len(ids) or self.grant["task_id"] not in ids
                    or self._id(source, "task_id", "id") != self.grant["task_id"]
                    or source.get("run_id") != self.grant["run_id"]
                    or source.get("project_id") != self.grant["project_id"]):
                self._stop("discovery selected ownership changed")
            page = self._read("list_managed_outputs", self.grant["task_id"])
            # This generated operation has no cursor parameter. Never fabricate
            # a custom path or silently accept an incomplete association set.
            if set(page) != {"items", "next_cursor"} or page["next_cursor"] is not None:
                self._stop("discovery managed outputs require a complete page")
            selected = []
            seen: dict[str, dict[str, Any]] = {}
            for row in page["items"]:
                if (type(row) is not dict or any(row.get(key) != self.grant[key]
                        for key in ("project_id", "run_id", "task_id"))
                        or type(row.get("attempt_id")) is not str or not row["attempt_id"]):
                    self._stop("foreign discovery output association")
                association = self._id(row, "association_id", "association_id")
                if association in seen and seen[association] != row:
                    self._stop("discovery association identity changed")
                if association in seen:
                    continue
                seen[association] = row
                if row["attempt_id"] == self.grant["attempt_id"]:
                    exact = self._read("get_managed_output", association)
                    if exact != row:
                        self._stop("discovery association readback changed")
                    selected.append(row)
            self._parent_authority()
            captured = tuple(json.dumps(row, sort_keys=True, separators=(",", ":"),
                                        allow_nan=False) for row in selected)
            if self._collected_outputs is not None and self._collected_outputs != captured:
                self._stop("discovery collected identities changed")
            self._collected_outputs = captured
            return {"project": project, "run": run, "task": source, "outputs": selected}
        except Exception:
            self.failed = True
            raise

    def prepare_objects(self, bridge: Any, *, selected_association_ids: Sequence[str] | None = None) -> tuple[dict[str, Any], ...]:
        """Verify and stage only internally captured historical associations.

        Returned private descriptors carry current-attempt staging provenance,
        not child authority. Later upload/admission must use the existing signed
        Runtime path. Neither source filenames nor caller maps select a path.
        """
        from ._child_bridge import _DIGEST, _MAX_OBJECT

        try:
            self._active()
            if self._collected_outputs is None:
                self._stop("discovery objects require completed collection")
            context = {**self.expected, "project_id": self.parent["project_id"],
                       "run_id": self.parent["run_id"]}
            if bridge.context != context:
                self._stop("discovery staging belongs to a different parent attempt")
            rows = [json.loads(row) for row in self._collected_outputs]
            if selected_association_ids is not None:
                if (type(selected_association_ids) not in (tuple, list)
                        or any(type(value) is not str for value in selected_association_ids)):
                    self._stop("discovery selected associations are not retained")
                selected = set(selected_association_ids)
                if (not selected or len(selected) != len(selected_association_ids)
                        or any(type(value) is not str for value in selected)
                        or not selected <= {row["association_id"] for row in rows}):
                    self._stop("discovery selected associations are not retained")
                rows = [row for row in rows if row["association_id"] in selected]
            objects: dict[str, dict[str, Any]] = {}
            associations: dict[str, list[str]] = {}
            for row in rows:
                oid, size, media = row.get("object_id"), row.get("size"), row.get("media_type")
                lifecycle = row.get("lifecycle")
                if (type(oid) is not str or not _DIGEST.fullmatch(oid) or row.get("digest") != oid
                        or type(size) is not int or not 0 <= size <= _MAX_OBJECT
                        or type(media) is not str or not media or any(ord(c) < 32 for c in media)
                        or row.get("durability") != "durable" or not isinstance(lifecycle, Mapping)
                        or lifecycle.get("state") not in {"available", "promoted"}
                        or row.get("state") != lifecycle["state"]
                        or type(row.get("role")) is not str or not row["role"]
                        or row["role"] in {"derived_input", "recoverable_snapshot"}):
                    self._stop("discovery object descriptor or lifecycle is invalid")
                if oid in objects and any(objects[oid][key] != row[key] for key in ("size", "media_type")):
                    self._stop("discovery CAS identity has conflicting metadata")
                objects[oid] = row
                associations.setdefault(oid, []).append(row["association_id"])
            limits = self.grant["limits"]
            if (len(objects) > limits["max_selected_output_objects"]
                    or sum(row["size"] for row in objects.values()) > limits["max_selected_output_bytes"]):
                self._stop("discovery selected object budget exceeded")
            # Revalidate every association, including those sharing a payload.
            for row in rows:
                self._parent_authority()
                if self._read("get_managed_output", row["association_id"]) != row:
                    self._stop("discovery association changed before object acquisition")
            for oid, row in objects.items():
                self._parent_authority()
                if oid not in self._verified_objects:
                    self._object_read = True
                    try:
                        response = self._reader.get_object(oid)
                    finally:
                        self._object_read = False
                    data = response.data
                    if (response.status != 200 or type(data) is not bytes or len(data) != row["size"]
                            or "sha256:" + hashlib.sha256(data).hexdigest() != oid):
                        self._stop("discovery object bytes failed verification")
                    self._verified_objects[oid] = data
                self._parent_authority()
            for row in rows:
                if self._read("get_managed_output", row["association_id"]) != row:
                    self._stop("discovery association changed during object acquisition")
            prepared = []
            for oid, row in objects.items():
                self._parent_authority()
                descriptor = bridge._stage_discovery_object(
                    self._verified_objects[oid], object_id=oid, size=row["size"],
                    media_type=row["media_type"], expected_context=context,
                    authority=self._parent_authority)
                prepared.append({**descriptor, "source_association_ids": tuple(associations[oid])})
            for row in rows:
                if self._read("get_managed_output", row["association_id"]) != row:
                    self._stop("discovery association changed during staging")
            self._parent_authority()
            self._prepared_objects = tuple(json.dumps(row, sort_keys=True, separators=(",", ":"),
                                                       allow_nan=False) for row in prepared)
            return tuple(prepared)
        except Exception:
            self.failed = True
            raise

    def submit_prepared_child(self, bridge: Any, request: Mapping[str, Any], *,
                              media_bindings: Mapping[str, str], verify_supplied_media: bool = False) -> dict[str, Any]:
        """Private adapter into the existing producer-file submission path.

        Bind declared child file-port names to internally captured association
        IDs, never to caller-supplied descriptors or authority. Historical IDs
        remain host provenance; uploads use the current bridge attempt.
        """
        from ._child_bridge import _KEY, _snapshot

        try:
            self._active()
            if self._prepared_objects is None or type(media_bindings) is not dict or not media_bindings:
                self._stop("prepared child submission requires retained internal objects")
            if bridge.context != {**self.expected, "project_id": self.parent["project_id"],
                                  "run_id": self.parent["run_id"]}:
                self._stop("prepared child submission belongs to a different parent attempt")
            if bridge.policy is None or bridge.policy.get("discovery_grant") != self.grant:
                self._stop("prepared child submission grant changed")
            adapted = copy.deepcopy(dict(request))
            if (set(adapted) != {"v", "request_id", "op", "child", "inputs", "input_descriptors",
                                "child_key", "wait", "timeout_seconds", "poll_seconds"}
                    or adapted["op"] != "submit" or type(adapted["inputs"]) is not dict
                    or type(adapted["input_descriptors"]) is not list or type(adapted["child"]) is not dict
                    or type(adapted["v"]) is not int or adapted["v"] != 1
                    or type(adapted["request_id"]) is not int or adapted["request_id"] < 0
                    or type(adapted["child_key"]) is not str or not _KEY.fullmatch(adapted["child_key"])
                    or type(adapted["wait"]) is not bool
                    or any(type(adapted[key]) not in (int, float) or not 0 < adapted[key] < float("inf")
                           for key in ("timeout_seconds", "poll_seconds"))):
                self._stop("invalid prepared child submission request")
            record = bridge.host.capabilities.get(adapted["child"].get("capability_id"))
            if record is None:
                self._stop("prepared child capability is not admitted")
            ports = {port.name: port for port in record.definition.inputs if port.type == "file"}
            captured = [json.loads(value) for value in self._prepared_objects]
            by_association = {association: value for value in captured
                              for association in value["source_association_ids"]}
            bindings = adapted["input_descriptors"]
            if any(type(value) is not dict for value in bindings):
                self._stop("invalid existing producer binding")
            existing = {value.get("name"): value for value in bindings}
            if len(existing) != len(bindings):
                self._stop("duplicate existing producer binding")
            prepared = {}
            for name, association in media_bindings.items():
                if (type(name) is not str or name not in ports
                        or type(association) is not str or association not in by_association):
                    self._stop("prepared child selection is outside retained associations")
                descriptor = copy.deepcopy(by_association[association])
                if verify_supplied_media:
                    supplied = existing.get(name)
                    if (type(supplied) is not dict or set(supplied) != {
                            "name", "kind", "filename", "output_port", "media_type"}
                            or supplied["kind"] != "producer_file" or supplied["output_port"] != name
                            or supplied["media_type"] != descriptor["media_type"]
                            or adapted["inputs"].get(name) != {key: supplied[key] for key in (
                                "filename", "output_port", "media_type")}):
                        self._stop("supplied prepared media is not an exact producer binding")
                    self._parent_authority()
                    data = _snapshot(bridge.output_root, supplied["filename"], descriptor["size"],
                                     attempt_fd=bridge._attempt_fd)
                    if (len(data) != descriptor["size"] or data != self._verified_objects[descriptor["object_id"]]
                            or "sha256:" + hashlib.sha256(data).hexdigest() != descriptor["object_id"]):
                        self._stop("supplied prepared media bytes disagree with retained identity")
                elif name in adapted["inputs"] or name in existing:
                    self._stop("prepared child selection is outside retained associations")
                descriptor["selected_association_id"] = association
                prepared[name] = descriptor
                value = {"filename": descriptor["filename"], "output_port": name,
                         "media_type": descriptor["media_type"]}
                adapted["inputs"][name] = value
                existing[name] = {"name": name, "kind": "producer_file", **value}
            if set(existing) - set(ports):
                self._stop("prepared child binding is not a declared file port")
            if (len(prepared) > self.grant["limits"]["max_child_media_bindings"]
                    or sum(value["size"] for value in prepared.values())
                    > self.grant["limits"]["max_child_media_bytes"]):
                self._stop("prepared child media budget exceeded")
            adapted["input_descriptors"] = [existing[name] for name in ports if name in existing]
            source_rows = {row["association_id"]: row for row in map(json.loads, self._collected_outputs or ())}

            def authority() -> None:
                self._parent_authority()
                for association in {association for value in prepared.values()
                                    for association in value["source_association_ids"]}:
                    if self._read("get_managed_output", association) != source_rows[association]:
                        self._stop("prepared child source association changed")
                self._active()

            authority()
            return bridge._submit(adapted, prepared=prepared, authority=authority)
        except Exception:
            self.failed = True
            raise


def _iteration_video_discovery(
    host: Any, task: Mapping[str, Any], record: CapabilityRecord, bridge: Any,
    inputs: dict[str, Any], *, attempt_id: str, lease_id: str, fence: int,
    runtime_epoch: int, cancelled: Callable[[], bool],
) -> _DiscoveryCollection | None:
    """Bind one already-admitted frozen CAS document to private discovery custody.

    No pack helper or producer-created locator supplies authority. The command
    receives the retained media file, and only its rendering child is adapted.
    """
    from ._child_bridge import _DIGEST, _MAX_OBJECT, _snapshot

    if record.id != "video_editing.iteration_video":
        return None
    if bridge is None or getattr(bridge, "_iteration_video_submit", None) is not None:
        raise HostError("iteration video requires one claimed child bridge")
    admitted = _admitted_task_spec(task).get("inputs")
    descriptor = admitted.get("frozen_inputs") if isinstance(admitted, Mapping) else None
    try:
        document_id = managed_file_digest(descriptor, "frozen_inputs")
    except ValueError as exc:
        raise HostError("iteration video requires an already-admitted frozen_inputs CAS file") from exc
    roots = task.get("input_object_ids")
    if (not isinstance(roots, (list, tuple)) or document_id not in {
            "sha256:" + value.removeprefix("sha256:") for value in roots if type(value) is str}):
        raise HostError("iteration frozen document is not an admitted input object")
    path = inputs.get("frozen_inputs")
    input_root = bridge.output_root.parent / "inputs"
    if type(path) is not str or not Path(path).is_absolute():
        raise HostError("iteration frozen document was not materialized by this host")
    try:
        relative = Path(path).relative_to(input_root).as_posix()
    except ValueError as exc:
        raise HostError("iteration frozen document escaped the current attempt") from exc
    data = _snapshot(input_root, relative, _MAX_OBJECT, attempt_fd=bridge._attempt_fd)
    if "sha256:" + hashlib.sha256(data).hexdigest() != document_id:
        raise HostError("iteration frozen document bytes disagree with admitted CAS identity")
    if isinstance(descriptor, Mapping) and "size" in descriptor and (
            type(descriptor["size"]) is not int or descriptor["size"] != len(data)):
        raise HostError("iteration frozen document size disagrees with admission")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate frozen JSON key")
            result[key] = value
        return result

    try:
        frozen = json.loads(data, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite frozen JSON")))
    except (ValueError, RecursionError) as exc:
        raise HostError("invalid admitted frozen document") from exc
    collection = _DiscoveryCollection(host.client, task, attempt_id=attempt_id, lease_id=lease_id,
                                      fence=fence, runtime_epoch=runtime_epoch, cancelled=cancelled)
    grant = collection.grant
    if (type(frozen) is not dict or set(frozen) != {
            "schema_version", "project", "target_run_id", "manifest", "quality", "media_bindings"}
            or type(frozen["schema_version"]) is not int or frozen["schema_version"] != 1
            or frozen["project"] != grant["project_id"] or frozen["target_run_id"] != grant["run_id"]
            or inputs.get("project_id") != grant["project_id"] or inputs.get("target_run_id") != grant["run_id"]
            or type(frozen["media_bindings"]) is not list or len(frozen["media_bindings"]) != 1):
        raise HostError("iteration frozen identity or singular binding disagrees with discovery grant")
    binding = frozen["media_bindings"][0]
    if (type(binding) is not dict or set(binding) != {
            "name", "object_id", "sha256", "size", "media_type", "filename", "associations"}
            or type(binding["object_id"]) is not str or not _DIGEST.fullmatch(binding["object_id"])
            or binding["sha256"] != binding["object_id"][7:]
            or type(binding["size"]) is not int or not 0 <= binding["size"] <= _MAX_OBJECT
            or type(binding["media_type"]) is not str or not binding["media_type"]
            or type(binding["associations"]) is not list or not binding["associations"]):
        raise HostError("invalid iteration frozen media binding")
    manifest, quality = frozen["manifest"], frozen["quality"]
    for document in (manifest, quality):
        if (type(document) is not dict or document.get("target_run_id") != grant["run_id"]
                or type(document.get("authority")) is not dict
                or document["authority"].get("project") != grant["project_id"]
                or document["authority"].get("kind") != "runtime"):
            raise HostError("iteration frozen content identity disagrees with discovery grant")
    runs = manifest.get("runs")
    if (type(runs) is not list or any(type(run) is not dict or type(run.get("run_id")) is not str for run in runs)
            or len({run["run_id"] for run in runs}) != len(runs)):
        raise HostError("invalid iteration frozen run identities")
    by_run = {run["run_id"]: run for run in runs}
    captured = collection.collect_metadata()
    by_association = {row["association_id"]: row for row in captured["outputs"]}
    selected, references = [], set()
    for association in binding["associations"]:
        if (type(association) is not dict or set(association) - {
                "project", "run_id", "artifact_index", "task_id", "output_id", "source_association_id"}
                or association.get("project") != grant["project_id"]
                or association.get("run_id") != grant["run_id"]
                or association.get("task_id") != grant["task_id"]
                or type(association.get("artifact_index")) is not int or association["artifact_index"] < 0
                or type(association.get("source_association_id")) is not str
                or association["source_association_id"] not in by_association):
            collection._stop("iteration frozen association is outside authenticated discovery")
        row = by_association[association["source_association_id"]]
        run = by_run.get(association["run_id"], {})
        artifacts = run.get("output_artifacts")
        index = association["artifact_index"]
        if type(artifacts) is not list or index >= len(artifacts) or type(artifacts[index]) is not dict:
            collection._stop("iteration frozen association has no exact artifact")
        artifact = artifacts[index]
        reference = (association["run_id"], index)
        if (reference in references or any(row.get(key) != binding[key] for key in ("object_id", "size", "media_type"))
                or row.get("digest") != binding["object_id"]
                or any(artifact.get(key) != association.get(key) for key in ("task_id", "source_association_id"))
                or "output_id" in artifact and artifact["output_id"] != association.get("output_id")
                or "output_id" in row and row["output_id"] != association.get("output_id")
                or any(key in artifact and artifact[key] != binding[key] for key in (
                    "object_id", "sha256", "size", "media_type"))):
            collection._stop("iteration frozen media identity disagrees with authenticated association")
        references.add(reference)
        selected.append(row["association_id"])
    prepared = collection.prepare_objects(bridge, selected_association_ids=tuple(dict.fromkeys(selected)))
    if len(prepared) != 1 or prepared[0]["object_id"] != binding["object_id"]:
        collection._stop("iteration discovery did not retain one exact media object")
    inputs["media_dependency"] = str(bridge.output_root / prepared[0]["filename"])

    def submit(request: Mapping[str, Any]) -> dict[str, Any]:
        if request.get("child", {}).get("capability_id") != "rendering.render":
            collection._stop("iteration discovery adapter requires rendering.render")
        collection._parent_authority()
        if _snapshot(input_root, relative, _MAX_OBJECT, attempt_fd=bridge._attempt_fd) != data:
            collection._stop("iteration admitted frozen document changed before child submission")
        return collection.submit_prepared_child(bridge, request,
            media_bindings={"media_dependency": selected[0]}, verify_supplied_media=True)

    collection._parent_authority()
    bridge._iteration_video_submit = submit
    return collection


_TRAINING_REVIEW_ARCHIVE_BYTES = 64 * 1024**2
_TRAINING_REVIEW_ARCHIVE_ENTRIES = 4096
_TRAINING_REVIEW_SCOPE_CACHE_ENTRIES = 64


def _training_review_media_scope(
    parent: Mapping[str, Any], child: Mapping[str, Any], admission: Any,
    client: Any, file_ports: Sequence[Any],
) -> tuple[str, ...] | None:
    """Verify the admitted immutable bundle/data closure, without local reads.

    Live claim/lease authority belongs to _training_review_authority. Admission
    task_json predates the claim and is never required to carry its attempt ID.
    Map lineage describes the captured inputs, not historical acquisition or
    completeness of an earlier canonical dataset.
    """
    from ._child_bridge import _MAX_OBJECT, _producer_registration_filename

    def strict_json(payload: Any) -> Any:
        def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in rows:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result
        return json.loads(payload, object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))

    def relative(name: Any) -> str:
        if (type(name) is not str or not name or "\\" in name
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or PurePosixPath(name).is_absolute() or name != PurePosixPath(name).as_posix()
                or any(part in {"", ".", ".."} for part in name.split("/"))):
            raise ValueError("unsafe archive member")
        return name

    def digest(payload: bytes) -> str:
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def identity(value: Any) -> None:
        if (type(value) is not dict or set(value) != {"digest", "size"}
                or type(value["digest"]) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", value["digest"]) is None
                or type(value["size"]) is not int or value["size"] < 0):
            raise ValueError("invalid content identity")

    try:
        original = strict_json(admission.task_json)
        inputs = strict_json(admission.inputs_json)
        descriptors = strict_json(admission.descriptors_json)
        if (type(inputs) is not dict or type(descriptors) is not list
                or type(inputs.get("timeout")) is not int or inputs["timeout"] != 0):
            return None
        absent = lambda value: value is None or (type(value) is str and value in {"", "__none__", "none", "None", "null"})
        if not absent(inputs.get("html")) or not absent(inputs.get("serve")):
            return None
        eligible = {"assets_bundle", "data", "state", "state_schema_bundle"}
        ports = [port for port in file_ports if port.name in eligible]
        names = [port.name for port in ports]
        if (set(names) != eligible or len(names) != 4
                or any(not absent(inputs.get(port.name)) for port in file_ports if port.name not in eligible)
                or [row.get("name") for row in descriptors if type(row) is dict] != names
                or len(descriptors) != 4):
            return None
        policy = parent["spec"]["child_delegation"]
        limits = policy["limits"]
        if len(descriptors) > limits["max_child_inputs"]:
            return None
        total = 0
        by_name = {}
        projected_inputs = dict(inputs)
        transport_names = set()
        for port, row in zip(ports, descriptors):
            producer = inputs.get(port.name)
            if (type(producer) is not dict or set(producer) != {"filename", "output_port", "media_type"}
                    or not all(type(producer[key]) is str and producer[key] for key in producer)
                    or set(row) != {"name", "filename", "output_port", "media_type", "object_id", "size", "required"}
                    or row["filename"] != _producer_registration_filename(producer["filename"])
                    or row["output_port"] != producer["output_port"] or row["media_type"] != producer["media_type"]
                    or type(row["required"]) is not bool or row["required"] != port.required
                    or type(row["object_id"]) is not str
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", row["object_id"]) is None
                    or type(row["size"]) is not int or not 0 <= row["size"] <= _MAX_OBJECT):
                return None
            if row["filename"] in transport_names:
                return None
            transport_names.add(row["filename"])
            total += row["size"]
            by_name[port.name] = row
            # RuntimeProtocolClient.admit_child replaces producer metadata with
            # its ordinary immutable object-reference projection in task.spec.
            projected_inputs[port.name] = {"object_id": row["object_id"], "digest": row["object_id"],
                                          "filename": row["filename"]}
        if (_canonical_digest(_admitted_task_spec(child).get("inputs")) != _canonical_digest(projected_inputs)
                or _canonical_digest(_admitted_task_spec(original).get("inputs")) != _canonical_digest(projected_inputs)):
            return None
        if total > min(limits["max_child_bytes"], limits["max_derived_bytes"]):
            return None
        for value in (original, child):
            if set(value.get("input_object_ids", [])) != {row["object_id"] for row in descriptors}:
                return None
        if (by_name["assets_bundle"]["media_type"] != "application/zip"
                or by_name["data"]["media_type"] != "application/json"
                or by_name["assets_bundle"]["size"] > _TRAINING_REVIEW_ARCHIVE_BYTES):
            return None

        def fetch(name: str) -> bytes:
            row = by_name[name]
            data = client.get_object(row["object_id"][7:])
            if not isinstance(data, (bytes, bytearray)):
                data = getattr(data, "data", None)
            if not isinstance(data, (bytes, bytearray)) or len(data) != row["size"] or len(data) > _MAX_OBJECT:
                raise ValueError("object size mismatch")
            payload = bytes(data)
            if digest(payload) != row["object_id"]:
                raise ValueError("object digest mismatch")
            return payload

        bundle, data_bytes = fetch("assets_bundle"), fetch("data")
        members: dict[str, bytes] = {}
        directories = set()
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            entries = archive.infolist()
            if len(entries) > _TRAINING_REVIEW_ARCHIVE_ENTRIES:
                return None
            extracted = 0
            seen = set()
            for entry in entries:
                name = entry.filename
                if entry.orig_filename != name:
                    return None
                canonical = relative(name[:-1] if entry.is_dir() else name)
                if name in seen or canonical in seen or canonical + "/" in seen:
                    return None
                seen.add(name)
                mode = entry.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if (entry.flag_bits & 1 or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
                        or (kind == stat.S_IFDIR) != entry.is_dir() and kind != 0):
                    return None
                if entry.is_dir():
                    if name not in {"ui/", "media/"}:
                        return None
                    directories.add(name)
                elif (name not in {"human-review-assets.json", "review-media-map.json",
                                   "ui/index.html", "ui/app.js", "ui/styles.css"}
                      and not name.startswith("media/")):
                    return None
                extracted += entry.file_size
                if entry.file_size < 0 or extracted > _TRAINING_REVIEW_ARCHIVE_BYTES:
                    return None
                # Read metadata and directory bytes too; CRC/corruption and every
                # entry count towards the same extracted closure ceiling.
                with archive.open(entry) as stream:
                    payload = stream.read(_TRAINING_REVIEW_ARCHIVE_BYTES + 1)
                if len(payload) != entry.file_size:
                    return None
                if entry.is_dir():
                    if payload:
                        return None
                else:
                    members[name] = payload
        if any("/".join(PurePosixPath(name).parts[:index]) in members
               for name in members for index in range(1, len(PurePosixPath(name).parts))):
            return None
        fixed = {"human-review-assets.json", "review-media-map.json", "ui/index.html", "ui/app.js", "ui/styles.css"}
        if not fixed <= set(members) or directories != {"ui/", "media/"}:
            return None
        if strict_json(members["human-review-assets.json"]) != {"html_root": "ui", "mounts": {"/media": "media"}}:
            return None
        media_map = strict_json(members["review-media-map.json"])
        data = strict_json(data_bytes)
        if (type(media_map) is not dict or set(media_map) != {"schema", "data", "canonical_data", "items"}
                or media_map["schema"] != "training-review-media-map/v1"
                or type(media_map["items"]) is not list or type(data) is not dict or type(data.get("items")) is not list):
            return None
        identity(media_map["data"])
        identity(media_map["canonical_data"])
        if media_map["data"] != {"digest": digest(data_bytes), "size": len(data_bytes)}:
            return None
        rows = {}
        for row in data["items"]:
            if type(row) is not dict or type(row.get("item_id")) is not str or not row["item_id"] or row["item_id"] in rows:
                return None
            rows[row["item_id"]] = row
        referenced = set()
        mapped = set()
        required_source = ("source_type", "source_id", "source_url", "acquired_at", "content_hash", "media_type")
        source_fields = (*required_source, "source_metadata", "derived_from", "scene_index", "clip_start_s", "clip_end_s")
        for item in media_map["items"]:
            if (type(item) is not dict or set(item) != {"item_id", "source", "source_media_path", "media_path", "archive_member", "digest", "size"}
                    or type(item["item_id"]) is not str or item["item_id"] not in rows or item["item_id"] in mapped):
                return None
            row = rows[item["item_id"]]
            if (any(type(row.get(key)) is not str or not row[key] for key in required_source)
                    or re.fullmatch(r"[0-9a-f]{64}", row["content_hash"]) is None
                    or _canonical_digest(item["source"]) != _canonical_digest({key: row[key] for key in source_fields if key in row})):
                return None
            relative(item["source_media_path"])
            member = relative(item["archive_member"])
            if not member.startswith("media/") or member in referenced or member not in members:
                return None
            url = "/media/" + "/".join(quote(part, safe="") for part in member[6:].split("/"))
            if item["media_path"] != url or row.get("media_path") != url:
                return None
            identity({"digest": item["digest"], "size": item["size"]})
            if digest(members[member]) != item["digest"] or len(members[member]) != item["size"]:
                return None
            mapped.add(item["item_id"])
            referenced.add(member)
        if mapped != set(rows) or set(members) != fixed | referenced:
            return None
        return (admission.admission_identity, _canonical_digest(descriptors),
                by_name["assets_bundle"]["object_id"], by_name["data"]["object_id"],
                digest(members["review-media-map.json"]), _canonical_digest(media_map["items"]))
    except Exception:
        return None


def _training_review_authority(
    host: Any, task: Mapping[str, Any], record: CapabilityRecord, bridge: Any, *,
    attempt_id: str, lease_id: str, fence: int, runtime_epoch: int,
) -> _ReviewIntervalAccounting | None:
    """Observe only this bridge's exact Training admissions through public reads."""
    from ._child_bridge import _child_wire_id, _snapshot_grant, task_resource
    from astrid.sdk._child_bridge import _validate_child_policy

    if record.id != "training.dataset_build" or bridge is None:
        return None
    child_record = host.capabilities.get("editorial.human_review")
    if child_record is None or type(fence) is not int or type(runtime_epoch) is not int or runtime_epoch < 1:
        return None
    parent_id = task.get("task_id", task.get("id"))
    expected = {"task_id": parent_id, "attempt_id": attempt_id, "lease_id": lease_id,
                "fence": fence, "runtime_epoch": runtime_epoch}

    def live(value: Mapping[str, Any], identity: Mapping[str, Any], capability: Any) -> Mapping[str, Any]:
        binding = value.get("execution_binding")
        spec = value.get("spec")
        if (not isinstance(binding, Mapping) or binding.get("status") != "claimed"
                or value.get("state", value.get("status")) != "running"
                or value.get("task_id", value.get("id")) != identity["task_id"]
                or value.get("attempt_id") != identity["attempt_id"]
                or type(value.get("lease_fence")) is not int or value["lease_fence"] != identity["fence"]
                or type(value.get("runtime_epoch")) is not int or value["runtime_epoch"] != runtime_epoch
                or value.get("capability_id", value.get("capability")) != capability.id
                or value.get("capability_digest") != capability.capability_digest
                or not isinstance(spec, Mapping) or spec.get("capability_digest") != capability.capability_digest
                or spec.get("delegation_closed_attempt_id") == identity["attempt_id"]):
            raise HostError("Training review authority changed")
        for key, pinned in identity.items():
            if binding.get(key) != pinned or (key in {"fence", "runtime_epoch"} and type(binding.get(key)) is not int):
                raise HostError("Training review claim changed")
        expiry = value.get("lease_expires_at")
        if type(expiry) is not str:
            raise HostError("Training review lease deadline missing")
        parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed <= datetime.now(timezone.utc):
            raise HostError("Training review lease ended")
        contract = _execution_contract(value)
        target = contract.get("target") if contract else None
        if (not isinstance(target, Mapping) or target.get("kind") != "machine"
                or type(binding.get("placement_version")) is not int or binding["placement_version"] < 0):
            raise HostError("Training review machine placement missing")
        _assert_verified_placement_binding(binding, target)
        return binding

    try:
        parent = task_resource(host.client.task(parent_id))
        parent_binding = live(parent, expected, record)
        parent_spec = _interactive_admission_spec(parent["spec"])
        if (_canonical_digest(parent_spec) != _canonical_digest(_interactive_admission_spec(task["spec"]))
                or _canonical_digest(parent_binding) != _canonical_digest(task.get("execution_binding"))):
            return None
        policy = _validate_child_policy(parent["spec"].get("child_delegation"))
        grant = {"capability_id": child_record.id, "capability_digest": child_record.capability_digest,
                 "output_ports": ["state_result"]}
        if (policy is None or {"capability_id": child_record.id, "capability_digest": child_record.capability_digest}
                not in policy["capabilities"] or policy.get("recoverable_outputs") != [grant]):
            return None
        policy_digest = _canonical_digest(policy)
        parent_pin = _canonical_digest({"spec": parent_spec, "binding": parent_binding,
                                       "request": parent.get("execution_request"), "project": parent.get("project_id")})
        placement = {"actual": parent_binding["actual_target"], "verification": parent_binding["verification"],
                     "executor_incarnation": parent_binding["executor_incarnation"]}
        lineage = {"parent_task_id": parent_id, "parent_attempt_id": attempt_id, "parent_lease_id": lease_id,
                   "parent_fence": fence, "runtime_epoch": runtime_epoch, "executor_id": parent_binding["executor_id"],
                   "project_id": parent.get("project_id"), "policy_digest": policy_digest,
                   "parent_placement": placement, "parent_effective_target": parent_binding.get("effective_target"),
                   "parent_placement_version": parent_binding["placement_version"]}
        frozen_grant = {**grant, "parent_attempt_id": attempt_id, "policy_digest": policy_digest,
                        "limits": {key: policy["limits"][key] for key in (
                            "max_snapshot_bytes", "max_recoverable_bytes", "max_recoverable_snapshots")}}
    except Exception:
        return None

    qualified: dict[str, tuple[str, ...]] = {}
    media_cache: dict[str, tuple[str, ...]] = {}

    def verify() -> frozenset[tuple[str, ...]]:
        snapshot = bridge.admitted_children_snapshot()
        if snapshot.closed or snapshot.revoked:
            media_cache.clear()
            if qualified:
                raise HostError("Training child bridge authority ended")
            return frozenset()
        health = host.client.health()
        epoch = health.get("runtime_epoch") if isinstance(health, Mapping) else getattr(health, "runtime_epoch", None)
        if type(epoch) is not int or epoch != runtime_epoch:
            media_cache.clear()
            raise HostError("Training Runtime epoch changed")
        current_parent = task_resource(host.client.task(parent_id))
        binding = live(current_parent, expected, record)
        if _canonical_digest({"spec": _interactive_admission_spec(current_parent["spec"]), "binding": binding,
                              "request": current_parent.get("execution_request"),
                              "project": current_parent.get("project_id")}) != parent_pin:
            raise HostError("Training parent admission changed")
        active = set()
        observed = set()
        cache_keys = set()
        for admitted in snapshot.children:
            original = json.loads(admitted.task_json)
            tid = original.get("task_id", original.get("id"))
            if (original.get("capability_id", original.get("capability")) != child_record.id
                    or original.get("capability_digest") != child_record.capability_digest):
                continue
            observed.add(tid)
            # A failed read is uncertain authority, never forward-projected credit.
            child = task_resource(host.client.task(tid))
            try:
                wire_id = _child_wire_id(project_id=parent.get("project_id"),
                                         parent_task_id=parent_id, parent_attempt_id=attempt_id,
                                         logical_child_key=admitted.child_key)
                if (child.get("task_id", child.get("id")) != tid or child.get("run_id") != original.get("run_id")
                        or child.get("idempotency_key") != wire_id
                        or original.get("idempotency_key") != wire_id
                        or child.get("project_id") != parent.get("project_id")
                        or json.loads(admitted.parent_context_json) != {
                            "task_id": parent_id, "run_id": parent.get("run_id"), "project_id": parent.get("project_id"),
                            "attempt_id": attempt_id, "lease_id": lease_id, "fence": fence, "runtime_epoch": runtime_epoch}):
                    raise HostError("Training child admission identity changed")
                state = child.get("state", child.get("status"))
                if state in {"completed", "succeeded", "failed", "cancelled"}:
                    if tid in qualified:
                        pinned = qualified[tid]
                        terminal_binding = child.get("execution_binding", {})
                        if (child.get("attempt_id") != pinned[3] or terminal_binding.get("lease_id") != pinned[4]
                                or type(child.get("lease_fence")) is not int or str(child["lease_fence"]) != pinned[5]
                                or type(child.get("runtime_epoch")) is not int or str(child["runtime_epoch"]) != pinned[6]
                                or child.get("capability_id", child.get("capability")) != child_record.id
                                or child.get("capability_digest") != child_record.capability_digest):
                            raise HostError("Training child claim changed at completion")
                    qualified.pop(tid, None)
                    continue
                if state != "running":
                    if tid in qualified:
                        raise HostError("Training child lost its running claim")
                    continue
                own = child.get("execution_binding", {})
                identity = {"task_id": tid, "attempt_id": child.get("attempt_id"), "lease_id": own.get("lease_id"),
                            "fence": child.get("lease_fence"), "runtime_epoch": runtime_epoch}
                if not all(type(identity[key]) is str and identity[key] for key in ("task_id", "attempt_id", "lease_id")):
                    raise HostError("Training child claim incomplete")
                child_binding = live(child, identity, child_record)
                if (_canonical_digest(_interactive_admission_spec(child["spec"]))
                        != _canonical_digest(_interactive_admission_spec(original["spec"]))
                        or child["spec"].get("delegated_parent") != lineage
                        or _snapshot_grant(child) != frozen_grant
                        or child_binding["actual_target"] != parent_binding["actual_target"]
                        or child_binding.get("resolved_target") != parent_binding.get("effective_target")):
                    raise HostError("Training child grant, lineage or placement changed")
                inputs = {port.name: port.default for port in child_record.definition.inputs if port.default is not None}
                admitted_inputs = _admitted_task_spec(child).get("inputs", {})
                inputs.update(admitted_inputs)
                if (type(admitted_inputs.get("timeout")) is not int or admitted_inputs["timeout"] != 0
                        or type(inputs.get("timeout")) is not int or inputs["timeout"] != 0):
                    raise HostError("Training child timeout is not exact integer zero")
                cache_key = _canonical_digest({
                    "admission": [admitted.child_key, admitted.admission_identity, admitted.task_json,
                                  admitted.inputs_json, admitted.descriptors_json, admitted.parent_context_json],
                    "claim": identity, "runtime_epoch": runtime_epoch,
                })
                cache_keys.add(cache_key)
                media = media_cache.get(cache_key)
                if media is None:
                    media = _training_review_media_scope(current_parent, child, admitted, host.client,
                        [port for port in child_record.definition.inputs if port.type == "file"])
                    if media is not None:
                        if len(media_cache) >= _TRAINING_REVIEW_SCOPE_CACHE_ENTRIES:
                            media_cache.pop(next(iter(media_cache)))
                        media_cache[cache_key] = media
                if media is None:
                    if tid in qualified:
                        raise HostError("Training review media custody changed")
                    continue
                claim = (admitted.child_key, admitted.admission_identity, tid, identity["attempt_id"],
                         identity["lease_id"], str(identity["fence"]), str(runtime_epoch),
                         _canonical_digest(child_binding), _canonical_digest(child.get("execution_request")),
                         _canonical_digest(media))
                if tid in qualified and qualified[tid] != claim:
                    raise HostError("Training review claim changed after qualification")
                qualified[tid] = claim
                active.add(claim)
            except Exception:
                if tid in qualified:
                    raise
                # Incomplete initial conjunction keeps the ordinary allowance.
        for stale in set(media_cache) - cache_keys:
            media_cache.pop(stale, None)
        if set(qualified) - observed:
            raise HostError("Training qualified child admission disappeared")
        if active:
            completed_snapshot = bridge.admitted_children_snapshot()
            if completed_snapshot.closed or completed_snapshot.revoked:
                media_cache.clear()
                raise HostError("Training child bridge closed during verification")
        return frozenset(active)

    return _ReviewIntervalAccounting(verify)


def _contract_queue_age(task_data: Mapping[str, Any]) -> float:
    for key in ("queued_at", "admitted_at", "created_at"):
        value = task_data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0.0, time.time() - float(value))
        if isinstance(value, str):
            try:
                stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise HostError(f"runtime task {key} is not a valid timestamp") from exc
            if stamp.tzinfo is None:
                raise HostError(f"runtime task {key} must include a timezone")
            return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
    raise HostError("execution_request max_queue_seconds requires task admission timestamp")


def _assert_fixed_request_scope(record: Any, task_data: Mapping[str, Any]) -> None:
    """Reject task parameters that escape a capability's declared profile."""
    scope = _fixed_request_scope(record.definition.metadata)
    if not scope:
        return
    spec = _admitted_task_spec(task_data)
    params = spec.get("params") if isinstance(spec, Mapping) else None
    if not isinstance(params, Mapping):
        raise HostError(f"capability {record.id!r} requires a fixed request scope")
    legacy_inputs = spec.get("inputs") if isinstance(spec, Mapping) else None
    if isinstance(legacy_inputs, Mapping):
        declared_inputs = {port.name for port in record.definition.inputs}
        unsupported_inputs = sorted(
            str(name) for name in legacy_inputs if str(name) not in declared_inputs
        )
        if unsupported_inputs:
            raise HostError(
                f"capability {record.id!r} received unsupported legacy input(s): "
                + ", ".join(unsupported_inputs)
            )
    mismatches = {
        key: {"expected": expected, "actual": params.get(key)}
        for key, expected in scope.items()
        if params.get(key) != expected
    }
    if mismatches:
        raise HostError(
            f"capability {record.id!r} request escapes fixed scope: "
            f"{json.dumps(mismatches, sort_keys=True)}"
        )


def _storage_input_limits(metadata: Mapping[str, Any]) -> dict[str, int]:
    """Read optional per-port byte limits for bounded CAS materialization."""
    raw = metadata.get("storage_input_max_bytes")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise HostError("capability storage_input_max_bytes must be an object")
    limits: dict[str, int] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name:
            raise HostError("capability storage input limit names must be non-empty strings")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HostError(f"capability storage input limit for {name!r} must be a non-negative integer")
        limits[name] = value
    return limits


def _task_storage_estimate(task_data: Mapping[str, Any]) -> dict[str, int] | None:
    """Parse the canonical whole-task storage estimate, if one was admitted."""
    raw = task_data.get("storage_estimate")
    spec = task_data.get("spec")
    if raw is None and isinstance(spec, Mapping):
        raw = spec.get("storage_estimate")
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != {"scratch_bytes", "output_bytes"}:
        raise HostError("task storage_estimate must contain scratch_bytes and output_bytes")
    try:
        scratch_bytes = int(raw["scratch_bytes"])
        output_bytes = int(raw["output_bytes"])
    except (TypeError, ValueError) as exc:
        raise HostError("task storage_estimate values must be integers") from exc
    if scratch_bytes < 0 or output_bytes < 0:
        raise HostError("task storage_estimate values must be non-negative")
    return {"scratch_bytes": scratch_bytes, "output_bytes": output_bytes}


# Waiting reasons that belong to the offered task itself rather than to this
# executor's capacity. Another capability's task may still be claimable.
_TASK_SPECIFIC_WAITING_REASONS = frozenset(
    {
        "capability_unavailable",
        "insufficient_storage",
        "waiting_for_executor_facts",
        "waiting_for_dependencies",
        "provider_state_unknown",
        "execution_binding_missing",
        "execution_binding_mismatch",
        "execution_qualification_mismatch",
        "remote_activation_missing",
    }
)
_CLAIM_HEAD_OF_LINE_SKIPS = 8
# How often a running attempt's storage/evidence guards and Runtime state are
# sampled. Each sample walks the attempt tree; the child poll stays at 50 ms.
_LIVE_GUARD_INTERVAL_SECONDS = 0.5


def _claim_field(claim: Any, name: str) -> Any:
    if isinstance(claim, Mapping):
        return claim.get(name)
    return getattr(claim, name, None)


def _claim_blocked_capability(claim: Any) -> str | None:
    """Return the capability of an offered task that cannot run, else None."""
    if _claim_field(claim, "attempt_id"):
        return None
    reason = _claim_field(claim, "waiting_reason")
    if reason not in _TASK_SPECIFIC_WAITING_REASONS:
        return None
    task = _claim_field(claim, "task")
    capability = _claim_field(task, "capability_id") if task is not None else None
    return str(capability) if capability else None


def _evidence_cap_message(detail: str | None) -> str:
    """Name the failed task's evidence overrun and the next step, for Runtime."""
    if not detail:
        return "generated evidence cap exceeded"
    return f"generated evidence cap exceeded: {detail}"


def _attempt_evidence_cap(policy_cap_bytes: int, storage_estimate: Mapping[str, int] | None) -> int:
    """Return one attempt's evidence cap: the host cap, or its admitted envelope.

    An admitted storage envelope already bounds scratch and output on their
    own (see ``_assert_live_storage_envelope``), and its frame sequence can be
    larger than the generic cap. Raising the cap to that envelope never makes
    the attempt unbounded; it stops a valid render from failing the generic cap.
    """
    if storage_estimate is None:
        return int(policy_cap_bytes)
    envelope = int(storage_estimate["scratch_bytes"]) + int(storage_estimate["output_bytes"])
    return max(int(policy_cap_bytes), envelope)


def _assert_live_storage_envelope(
    estimate: Mapping[str, int] | None,
    root: Path,
    output_root: Path,
) -> None:
    """Fail while a child is writing, before an overrun reaches settlement."""
    if estimate is None:
        return
    total_bytes = _attempt_tree_bytes(root)
    output_bytes = _storage_tree_bytes(output_root)
    scratch_bytes = max(0, total_bytes - output_bytes)
    if output_bytes > int(estimate["output_bytes"]):
        diagnostic = {
            "guard": "storage_envelope",
            "category": "output_overrun",
            "configured_scratch_bytes": int(estimate["scratch_bytes"]),
            "configured_output_bytes": int(estimate["output_bytes"]),
            **_storage_envelope_measurement(root, output_root),
        }
        raise StorageEnvelopeError(
            f"live output bytes {output_bytes} exceed task output limit {estimate['output_bytes']}; "
            "this task exceeded its output budget. Shorten the output or re-admit with a smaller "
            "range. The host does not need a restart.",
            diagnostic=diagnostic,
        )
    if scratch_bytes > int(estimate["scratch_bytes"]):
        diagnostic = {
            "guard": "storage_envelope",
            "category": "scratch_overrun",
            "configured_scratch_bytes": int(estimate["scratch_bytes"]),
            "configured_output_bytes": int(estimate["output_bytes"]),
            **_storage_envelope_measurement(root, output_root),
        }
        raise StorageEnvelopeError(
            f"live scratch bytes {scratch_bytes} exceed task scratch limit {estimate['scratch_bytes']}; "
            f"this task exceeded its scratch budget ({scratch_bytes} of {estimate['scratch_bytes']} bytes). "
            "Re-run as a review render (640x360), render a shorter range, or free disk on the "
            "temporary volume. The host does not need a restart.",
            diagnostic=diagnostic,
        )


def _task_storage_envelope(
    task_data: Mapping[str, Any],
    root: Path,
    staged_outputs: list[Mapping[str, Any]],
) -> dict[str, int] | None:
    """Enforce an explicit whole-task storage ceiling before CAS upload."""
    estimate = _task_storage_estimate(task_data)
    if estimate is None:
        return None
    scratch_limit = estimate["scratch_bytes"]
    output_limit = estimate["output_bytes"]
    output_root = root / "outputs"
    resolved_output_root = output_root.resolve()
    published_output_bytes = 0
    published_paths: set[Path] = set()
    for descriptor in staged_outputs:
        raw_path = descriptor.get("path")
        if not raw_path:
            raise HostError("staged output is missing its path for storage accounting")
        path = Path(str(raw_path))
        if path.is_symlink() or not path.is_file():
            raise HostError("staged output path is not a regular file")
        resolved_path = path.resolve()
        if not resolved_path.is_relative_to(resolved_output_root):
            raise HostError("staged output path escapes the output directory")
        if resolved_path not in published_paths:
            published_paths.add(resolved_path)
            published_output_bytes += int(path.stat().st_size)
    # Use the same stable-output view as the live counter.  This includes the
    # published result manifest and any undeclared stable file, while treating
    # atomic writer temporaries as scratch.  The latter prevents the estimate
    # from changing meaning between polling and final settlement.
    stable_output_paths = {
        path.resolve()
        for path in output_root.rglob("*")
        if not path.is_symlink() and path.is_file() and not path.name.endswith(".tmp")
    }
    output_paths = stable_output_paths | published_paths
    output_bytes = sum(int(path.stat().st_size) for path in output_paths)
    total_bytes = _attempt_tree_bytes(root)
    scratch_bytes = max(0, total_bytes - output_bytes)
    if output_bytes > output_limit:
        raise HostError(
            f"staged output bytes {output_bytes} exceed task output limit {output_limit}"
        )
    if scratch_bytes > scratch_limit:
        raise HostError(
            f"attempt scratch bytes {scratch_bytes} exceed task scratch limit {scratch_limit}; "
            "this task exceeded its scratch budget. Re-run as a review render (640x360) or a "
            "shorter range, or free disk on the temporary volume. The host does not need a restart."
        )
    return {
        "scratch_bytes": scratch_bytes,
        "scratch_limit_bytes": scratch_limit,
        "output_bytes": output_bytes,
        "output_limit_bytes": output_limit,
        "total_bytes": total_bytes,
        "total_limit_bytes": scratch_limit + output_limit,
    }


def source_checkout_digest(checkout: str | Path) -> str:
    """Return the source identity used by the generic-host boundary.

    The checkout itself can contain build artefacts and unrelated work.  The
    pack corpus is the executable input to this process, so hash that exact
    tree (using the same file hashing rules as capability admission).
    """
    pack_root = Path(checkout).expanduser().resolve() / "astrid" / "packs"
    if any(path.is_symlink() for path in pack_root.rglob("*")):
        raise ValueError("source checkout pack root contains a symlink")
    return _source_digest(pack_root)


def source_checkout_closure(checkout: str | Path) -> dict[str, Any]:
    """Measure the executable host closure owned by this checkout.

    The legacy pack digest remains the capability-admission identity.  This
    separate receipt covers the process host, Astrid core, SDK/bootstrap,
    package entrypoints, launcher, and vendored workspace-client files that
    can change what an admitted executable does.
    """
    root = Path(checkout).expanduser().resolve()
    components = {
        "host": root / "astrid" / "core" / "execution" / "generic_host.py",
        "core": root / "astrid" / "core",
        "sdk": root / "astrid" / "sdk",
        "launcher": root / "astrid" / "omp_agent.py",
        "astrid_entrypoint": root / "astrid" / "__init__.py",
        "astrid_version": root / "astrid" / "version.py",
        "astrid_main": root / "astrid" / "__main__.py",
        "astrid_runtime_cli": root / "astrid" / "runtime_cli.py",
        "vendored_workspace_client": root / "banodoco_workspace_client",
    }
    measured: list[dict[str, str]] = []
    for name, path in components.items():
        if path.is_symlink() or not path.exists():
            raise ValueError(f"source closure component is missing or symlinked: {name}")
        if path.is_dir() and any(child.is_symlink() for child in path.rglob("*")):
            raise ValueError(f"source closure component contains a symlink: {name}")
        measured.append({
            "component": name,
            "path": str(path.relative_to(root)),
            "sha256": _source_digest(path),
        })
    measured.sort(key=lambda item: item["component"])
    return {
        "schema_version": 1,
        "components": measured,
        "digest": _canonical_digest(measured),
    }


def source_checkout_closure_digest(checkout: str | Path) -> str:
    """Return the deterministic digest for the executable source closure."""
    return str(source_checkout_closure(checkout)["digest"])


def process_birth_identity(pid: int | None = None) -> str:
    """Return the OS birth token used to distinguish a reused PID."""
    return _process_birth_identity(pid)


def _admitted_source_roots(root: Path, definition: Any) -> tuple[Path, ...]:
    """Return every executable root admitted for one capability."""
    if definition.metadata.get("action_invocation"):
        pack_root = Path(str(definition.metadata["pack_root"])).expanduser().resolve()
        if root.resolve() != pack_root:
            raise HostError("action source root does not match its pack root")
        return (pack_root,)
    roots: list[Path] = [root.resolve()]
    pack_root = _pack_root_for_executor(root)
    if pack_root is not None and str(definition.kind) == "external":
        roots.append(pack_root.resolve())
    return tuple(dict.fromkeys(roots))


def _admitted_python_roots(root: Path, definition: Any) -> tuple[Path, ...]:
    """Return explicit import roots for non-builtin source packs.

    A pack is itself the source root: its Python modules sit alongside the
    manifest-owned ``executors``/``orchestrators`` trees.  Passing the pack
    root (rather than an ancestor) keeps imports useful without admitting an
    unrelated checkout or ambient directory.
    """
    invocation = definition.metadata.get("action_invocation")
    if isinstance(invocation, Mapping) and invocation.get("kind") == "python":
        # Private pack imports use a contained namespace, never PYTHONPATH.
        return ()
    pack_root = _pack_root_for_executor(root)
    if pack_root is None or str(getattr(definition, "kind", "")) != "external":
        return ()
    builtin_packs = (Path(__file__).resolve().parents[3] / "astrid" / "packs").resolve()
    if pack_root == builtin_packs or pack_root.is_relative_to(builtin_packs):
        return ()
    return (pack_root.resolve(),)


def _source_digest_for_roots(roots: tuple[Path, ...] | list[Path]) -> str:
    return _canonical_digest([
        {"root": str(root.resolve()), "digest": _source_digest(root.resolve())}
        for root in roots
    ])


def _verify_action_admission(definition: Mapping[str, Any], admission: Mapping[str, Any]) -> None:
    """Tie Python source authority to the exact admitted whole-pack root."""
    metadata = definition.get("metadata", {})
    invocation = metadata.get("action_invocation") if isinstance(metadata, Mapping) else None
    if not isinstance(invocation, Mapping) or invocation.get("kind") != "python":
        return
    raw_root = metadata.get("pack_root")
    if not isinstance(raw_root, str) or not raw_root:
        raise HostError("Python action requires an admitted pack root")
    root = Path(raw_root).expanduser().resolve()
    roots = tuple(Path(str(value)).expanduser().resolve() for value in admission.get("source_roots", ()))
    if (not root.is_dir() or roots != (root,)
            or Path(str(admission.get("source_root") or "")).expanduser().resolve() != root):
        raise HostError("Python action pack root does not match the admission source fence")
    if (not admission.get("source_digest") or not admission.get("capability_digest")
            or _capability_digest(definition) != admission["capability_digest"]):
        raise HostError("Python action requires a matching admitted definition digest")
    if admission.get("python_path_roots"):
        raise HostError("Python action pack imports must use the contained namespace")



def _verify_admitted_source(admission: Mapping[str, Any]) -> None:
    """Revalidate the source fence immediately before command/provider execution."""
    expected = str(admission.get("source_digest") or "")
    if not expected:
        return
    raw_roots = admission.get("source_roots")
    if isinstance(raw_roots, (list, tuple)) and raw_roots:
        roots = tuple(Path(str(value)).expanduser().resolve() for value in raw_roots)
    elif admission.get("source_root"):
        roots = (Path(str(admission["source_root"])).expanduser().resolve(),)
    else:
        return
    if _source_digest_for_roots(roots) != expected:
        raise HostError("admitted capability source digest changed")


def _vcs_revision(root: Path) -> str:
    """Return the checked-out revision for source-epoch invalidation."""
    checkout = next((candidate for candidate in (root, *root.parents) if (candidate / ".git").exists()), None)
    if checkout is None:
        return "unversioned"
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _dependency_digest(definition: ExecutorDefinition, records: Mapping[str, "CapabilityRecord"]) -> str:
    """Digest graph edges and declared package/runtime dependencies."""
    dependencies = {
        # This is the declared graph identity. Each executor's own task pin
        # binds source separately; preserve the existing definition-only edge
        # semantics rather than cascading source edits through consumers.
        dependency: ("sha256:" + _canonical_digest(records[dependency].definition.to_dict())
                     if dependency in records else "missing")
        for dependency in sorted(definition.graph.depends_on)
    }
    metadata = definition.metadata
    requirements = metadata.get("requirements", definition.isolation.requirements)
    if isinstance(requirements, str):
        requirements = [requirements]
    return _canonical_digest({
        "depends_on": dependencies,
        "requirements": sorted(str(value) for value in (requirements or ())),
        "requirements_source": str(metadata.get("requirements_source", "")),
    })


def _pack_root_for_executor(executor_root: Path) -> Path | None:
    """Find the manifest-owned pack root for a folder executor.

    The generic host discovers folder executors directly rather than importing
    the pack registry.  External pack metadata (especially the import root
    used by ``python -m`` commands) therefore has to be attached here.
    """
    for candidate in (executor_root, *executor_root.parents):
        if (candidate / "pack.yaml").is_file():
            return candidate.resolve()
    return None


def _attach_pack_metadata(definition: "ExecutorDefinition", executor_root: Path) -> "ExecutorDefinition":
    """Attach the source-pack identity used by the normal executor registry."""
    pack_root = _pack_root_for_executor(executor_root)
    if pack_root is None:
        return definition
    metadata = dict(definition.metadata)
    metadata.setdefault("source", "pack")
    # Read the manifest-owned id so the child receives the correct import
    # parent (and never infer identity from an arbitrary directory name).
    pack_id = pack_root.name
    try:
        from astrid.core.pack.loader import _load_manifest_payload

        payload = _load_manifest_payload(pack_root / "pack.yaml")
        if isinstance(payload, Mapping) and payload.get("id"):
            pack_id = str(payload["id"])
    except (ImportError, OSError, TypeError, ValueError):
        pass
    metadata.setdefault("source_pack", pack_id)
    metadata.setdefault("pack_root", str(pack_root))
    return replace(definition, metadata=metadata)


def _binary_requirements(record) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*record.definition.isolation.binaries,
        *(str(binary) for binary in record.matrix.get("required_binaries", ())),
        *record.adapter.required_binaries)))


@dataclass(frozen=True)
class CapabilityRecord:
    definition: ExecutorDefinition
    capability_digest: str
    source_digest: str
    source_root: Path
    dependency_digest: str = ""
    manifest_path: Path | None = None
    preflight: Mapping[str, Any] = field(default_factory=dict)
    ready: bool = False
    executable_bindings: tuple[ExecutableBinding, ...] = ()
    matrix: Mapping[str, Any] = field(default_factory=dict)
    # Set when a discovered capability has no approved capability-matrix row.
    # Such a capability is registered as unavailable with this reason; it is
    # never admitted for execution. Other capabilities are unaffected.
    approval_reason: str | None = None

    @property
    def id(self) -> str:
        return self.definition.id

    @property
    def resource_keys(self) -> tuple[str, ...]:
        metadata = self.definition.metadata
        values = metadata.get("resource_keys", metadata.get("resources", ()))
        if isinstance(values, str):
            values = (values,)
        declared = tuple(str(value) for value in (values or ()))
        adapter = AdapterRegistry.from_matrix(self.definition, self.matrix)
        return tuple(dict.fromkeys((*adapter.resource_keys, *declared)))

    @property
    def adapter(self) -> AdapterSpec:
        return AdapterRegistry.from_matrix(self.definition, self.matrix)

    @property
    def estimated_scratch_bytes(self) -> int:
        return int(self.definition.metadata.get("estimated_scratch_bytes", 0) or 0)

    @property
    def estimated_output_bytes(self) -> int:
        return int(self.definition.metadata.get("estimated_output_bytes", 0) or 0)

    def manifest(self) -> dict[str, Any]:
        source_roots = _admitted_source_roots(self.source_root, self.definition)
        return {
            "id": self.id,
            "definition": self.definition.to_dict(),
            "inputs": [port.__dict__ for port in self.definition.inputs],
            "outputs": [output.__dict__ for output in self.definition.outputs],
            "capability_digest": self.capability_digest,
            "source_digest": self.source_digest,
            "source_type": self.definition.metadata.get("source_type", "local"),
            "commit_sha": self.definition.metadata.get("commit_sha", ""),
            "dependency_digest": self.dependency_digest,
            "source_root": str(self.source_root),
            "source_roots": [str(root) for root in source_roots],
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "resource_keys": list(self.resource_keys),
            "estimated_scratch_bytes": self.estimated_scratch_bytes,
            "estimated_output_bytes": self.estimated_output_bytes,
            "adapter_family": self.adapter.family,
            "disposition": self.matrix.get("disposition", "unclassified"),
            "evidence_reason": self.matrix.get("evidence_reason", ""),
            "preflight": dict(self.preflight),
            "ready": self.ready,
        }


def _prepare_review_resume_input(client: Any, *, source_task_id: str, source_attempt_id: str,
                                 association_id: str, revision: int, expected_project_id: str,
                                 expected_capability_digest: str) -> dict[str, str]:
    """Fresh trusted controller preparation, before ordinary input admission.

    The controller supplies the exact browser-acknowledged historical selector.
    No lease or inherited bridge is used or returned. Failure returns no descriptor.
    """
    from ._child_bridge import (
        ChildBridgeError, _DIGEST, _snapshot_grant, _verify_snapshot_row, resource, task_resource,
    )

    if (any(type(value) is not str or not value for value in (
            source_task_id, source_attempt_id, association_id, expected_project_id))
            or type(revision) is not int or not 0 < revision <= 9007199254740991
            or type(expected_capability_digest) is not str
            or not _DIGEST.fullmatch(expected_capability_digest)):
        raise ChildBridgeError("invalid acknowledged snapshot selector")
    generated = client.generated
    task = task_resource(generated.get_task(source_task_id))
    if (task.get("task_id", task.get("id")) != source_task_id
            or task.get("project_id") != expected_project_id
            or task.get("capability_id", task.get("capability")) != "editorial.human_review"
            or task.get("capability_digest") != expected_capability_digest):
        raise ChildBridgeError("snapshot source capability or project changed")
    grant = _snapshot_grant(task)
    if grant is None or grant["output_ports"] != ["state_result"]:
        raise ChildBridgeError("source has no exact review snapshot grant")
    rows, cursor = generated.list_managed_outputs(source_task_id)
    if cursor is not None or type(rows) is not list or len(rows) > 256:
        raise ChildBridgeError("snapshot output page exceeds bound")
    rows = [resource(row) for row in rows]
    ids = [row.get("association_id") for row in rows]
    if any(type(value) is not str or not value for value in ids) or len(set(ids)) != len(ids):
        raise ChildBridgeError("invalid snapshot association listing")
    matches = [row for row in rows if row["association_id"] == association_id]
    if len(matches) != 1:
        raise ChildBridgeError("acknowledged snapshot is absent from bounded listing")
    row = matches[0]
    if resource(generated.get_managed_output(association_id)) != row:
        raise ChildBridgeError("snapshot list/get metadata changed")
    _verify_snapshot_row(row, task=task, attempt_id=source_attempt_id,
                         project_id=expected_project_id, grant=grant, revision=revision, output_port="state_result")
    data = generated.get_object(row["object_id"]).data
    if (type(data) is not bytes or len(data) != row["size"]
            or "sha256:" + hashlib.sha256(data).hexdigest() != row["object_id"]):
        raise ChildBridgeError("snapshot object bytes failed verification")
    if resource(generated.get_managed_output(association_id)) != row:
        raise ChildBridgeError("snapshot association changed during readback")
    return {"object_id": row["object_id"], "digest": row["object_id"], "filename": "review-state.json"}


class RuntimeProtocolClient:
    """Host adapter composed over the generated workspace client.

    This class intentionally contains no HTTP implementation.  The generated
    client owns transport, envelopes, authentication, and route paths; this
    adapter only translates host lifecycle values to its typed operations.

    Worker credentials are a distinct runtime boundary from the user-facing
    ``AstridClient`` handshake.  The runtime authorizes registration with
    ``worker:register``, claim/attempt operations with ``worker:execute``,
    task reads/writes with the task scopes, and CAS reads/writes with the
    object scopes.  The credential actor is bound to ``executor_id``. This
    adapter therefore never fabricates a user handshake; the runtime's
    worker-token contract is the sole identity check for this process.
    """

    WORKER_SCOPES = (
        "handshake",
        "worker:register",
        "worker:execute",
        "projects:read",
        "tasks:read",
        "objects:read",
        "objects:write",
    )
    # The runtime associates inline settlement bytes with the task project in
    # one transaction.  Workers are intentionally not granted projects:write,
    # so they must not pre-publish an unscoped CAS object and then attempt a
    # forbidden project association.
    INLINE_SETTLEMENT_OUTPUTS = True
    REQUIRES_OUTPUT_BINDING = True

    def __init__(self, endpoint: str, credential: str, *, timeout: float = 30.0):
        try:
            self.endpoint = validate_runtime_endpoint(endpoint)
        except WorkspaceClientError as exc:
            raise HostError(f"runtime endpoint rejected: {exc}") from exc
        self.credential = credential
        self.timeout = timeout
        try:
            from banodoco_workspace_client import WorkspaceClient
            from banodoco_workspace_client.contract_metadata import SCHEMA_DIGEST
        except ImportError as exc:
            raise HostError(
                "generated banodoco_workspace_client is unavailable; reinstall the Astrid package"
            ) from exc
        self.schema_digest = SCHEMA_DIGEST
        self.generated = WorkspaceClient(
            self.endpoint,
            self.credential,
            timeout=self.timeout,
        )
        self.executor_id: str | None = None
        self._runtime_epoch: int | None = None
        # The runtime epoch is part of the claim fence.  Never replace it with
        # a freshly observed epoch while an attempt is in flight: a restart
        # must revoke the old attempt rather than make its lease look current.
        self._attempt_runtime_epochs: dict[str, int] = {}
        self._authenticated_claims: dict[str, dict[str, Any]] = {}
        self._heartbeat_session = secrets_module.token_hex(8)
        self._heartbeat_sequence = 0
        self._heartbeat_lock = threading.RLock()
        # A Runtime child authority pins the exact current lease deadline.
        # Only issuance/admission and heartbeat share this lock; child polls
        # never hold it.
        # Registration is the runtime's executor liveness pulse.  Keep one
        # stable nonce for retries, then rotate it only for an intentional
        # renewal so a new host session cannot replay an old registration
        # receipt while transport retries remain idempotent.
        self._registration_session = secrets_module.token_hex(8)
        self._registration_refresh_deadline = 0.0

    def fork_for_host_slot(self) -> RuntimeProtocolClient:
        """Give one serving slot private transport, lease and heartbeat state."""
        client = RuntimeProtocolClient(self.endpoint, self.credential, timeout=self.timeout)
        client.executor_id = self.executor_id
        return client

    def health(self):
        """Read protocol/schema/runtime epoch through the generated client."""
        value = self.generated.health()
        epoch = value.get("runtime_epoch") if isinstance(value, Mapping) else getattr(value, "runtime_epoch", None)
        if epoch is not None:
            self._runtime_epoch = int(epoch)
        return value

    def _authenticate_worker(self, executor_id: str) -> None:
        """Prove this enabled bearer can execute through the existing handshake."""
        value = self.generated.handshake(
            "astrid-generic-host", "1", ["worker:execute"]
        )

        def field(name: str):
            return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)

        if (
            field("actor_id") != executor_id
            or tuple(field("scopes") or ()) != ("worker:execute",)
            or not field("realm_id")
            or field("schema_digest") != self.schema_digest
            or field("protocol") != "workspace.v1"
        ):
            raise HostError("authenticated Runtime worker readiness is foreign or incomplete")

    def _current_runtime_epoch(self) -> int:
        """Read the live bootstrap epoch before every mutating operation."""
        value = self.health()
        epoch = value.get("runtime_epoch") if isinstance(value, Mapping) else getattr(value, "runtime_epoch", None)
        if epoch is None:
            raise HostError("runtime health returned no runtime_epoch")
        return int(epoch)

    @staticmethod
    def _claim_attempt_id(claim: Any) -> str | None:
        value = claim.get("attempt_id") if isinstance(claim, Mapping) else getattr(claim, "attempt_id", None)
        return str(value) if isinstance(value, str) and value else None

    def _claimed_runtime_epoch(self, attempt_id: str) -> int:
        expected = self._attempt_runtime_epochs.get(str(attempt_id))
        if expected is None:
            raise HostError("attempt runtime epoch was not captured at claim")
        current = self._current_runtime_epoch()
        if current != expected:
            raise HostError(
                "runtime epoch changed after claim; stale attempt is fenced and requires reconciliation"
            )
        return expected

    def register_executor(self, executor_id: str, *, capabilities: list[Mapping[str, Any]], max_concurrency: int, resource_keys: list[str], source_digest: str | None, dependency_digest: str | None = None, source_epoch: str | None = None, protocol_version: str = "workspace.v1", schema_digest: str | None = None, runtime_epoch: int | None = None, verified_facts: Mapping[str, Any] | None = None):
        # Runtime schema identity is negotiated through health/compatibility;
        # source, dependency, and source-epoch identity remain part of the
        # executor registration admission envelope.  ``schema_digest`` is
        # intentionally not sent until the generated runtime client contract
        # exposes that field.
        payload = {
            "executor_id": executor_id,
            "capabilities": capabilities,
            "max_concurrency": max_concurrency,
            "resource_keys": resource_keys,
            "protocol": protocol_version,
            "source_digest": source_digest,
            "source_epoch": source_epoch,
            "dependency_digest": dependency_digest,
            "runtime_epoch": runtime_epoch,
        }
        if verified_facts is not None:
            payload["verified_facts"] = dict(verified_facts)
        registration = self.generated.register_executor(
            payload,
            idempotency_key=(
                f"executor-{executor_id}-{_canonical_digest(payload)}-"
                f"{self._registration_session}"
            ),
        )
        self.executor_id = executor_id
        return registration

    def renew_registration_session(self) -> None:
        """Rotate the host-session nonce before an intentional renewal."""
        self._registration_session = secrets_module.token_hex(8)

    def register_capability(
        self,
        capability_id: str,
        *,
        digest: str,
        required_resource_keys: list[str] | None = None,
        status: str = "ready",
        estimated_scratch_bytes: int = 0,
        estimated_output_bytes: int = 0,
        unavailable_reason: str | None = None,
    ):
        """Publish one capability through the generated runtime client."""
        registration = {
            "required_resource_keys": list(required_resource_keys or []),
            "status": status,
            "estimated_scratch_bytes": int(estimated_scratch_bytes),
            "estimated_output_bytes": int(estimated_output_bytes),
            "unavailable_reason": unavailable_reason,
        }
        return self.generated.register_capability(
            capability_id,
            digest,
            **registration,
            idempotency_key=(
                f"capability-{capability_id}-{digest}-"
                f"{_canonical_digest(registration)}"
            ),
        )

    def withdraw_capability(self, capability_id: str, *, digest: str, reason: str):
        """Mark a removed capability unavailable in the canonical registry."""
        return self.generated.register_capability(
            capability_id,
            digest,
            required_resource_keys=[],
            status="unavailable",
            unavailable_reason=reason,
            idempotency_key=f"capability-withdraw-{capability_id}-{digest}",
        )

    def heartbeat(self, task_id: str, lease_token: str, *, attempt_id: str | None = None, fence: int | None = None, progress: Mapping[str, Any] | None = None):
        if not attempt_id or fence is None:
            raise HostError("generated heartbeat requires attempt_id and fence")
        runtime_epoch = self._claimed_runtime_epoch(attempt_id)
        # A heartbeat extends the lease and is therefore a new mutation, not a
        # replay of the first pulse.  Reusing one idempotency key here makes a
        # long render appear healthy to the host while the runtime repeatedly
        # returns the original receipt and lets the lease expire.
        with self._heartbeat_lock:
            self._heartbeat_sequence += 1
            heartbeat_sequence = self._heartbeat_sequence
        payload = {
            "lease_id": lease_token,
            "fence": int(fence),
            "idempotency_key": (
                f"heartbeat-{attempt_id}-{fence}-{runtime_epoch}-"
                f"{self._heartbeat_session}-{heartbeat_sequence}"
            ),
            "runtime_epoch": runtime_epoch,
        }
        if progress is not None:
            payload["progress"] = dict(progress)
        with self._heartbeat_lock:
            return self.generated.heartbeat_attempt(attempt_id, **payload)

    def claim(self, task_id: str, worker_id: str, lease_token: str):
        raise HostError("per-task claim is not a canonical operation; use claim_task")

    def claim_next(self, *, executor_id: str, capability_ids: list[str], idempotency_key: str, target: Mapping[str, Any] | None = None):
        claim_epoch = self._current_runtime_epoch()
        claim = self.generated.claim_task(
            executor_id=executor_id,
            capability_ids=capability_ids,
            idempotency_key=idempotency_key,
            runtime_epoch=claim_epoch,
            target=target,
        )
        attempt_id = self._claim_attempt_id(claim)
        if attempt_id is not None:
            raw_epoch = claim.get("runtime_epoch") if isinstance(claim, Mapping) else getattr(claim, "runtime_epoch", None)
            self._attempt_runtime_epochs[attempt_id] = int(raw_epoch if raw_epoch is not None else claim_epoch)
            projection = claim.get("claim_authority") if isinstance(claim, Mapping) else getattr(claim, "claim_authority", None)
            if not isinstance(projection, Mapping):
                raise HostError("authenticated Runtime claim has no execution authority")
            self._authenticated_claims[attempt_id] = dict(projection)
        return claim

    def confirm_claim_authority(self, *, task_id, attempt_id, lease_id, fence,
                                runtime_epoch, project_id, operation, executor_id):
        original = self._authenticated_claims.get(attempt_id)
        if not isinstance(original, dict):
            raise HostError("attempt lacks authenticated claim provenance")
        expected = dict(task_id=task_id, attempt_id=attempt_id, lease_id=lease_id,
                        fence=fence, runtime_epoch=runtime_epoch, project_id=project_id,
                        operation=operation, executor_id=executor_id)
        if any(original.get(k) != v for k, v in expected.items()):
            raise HostError("authenticated claim authority binding mismatch")
        reply = self.heartbeat(task_id, lease_id, attempt_id=attempt_id, fence=fence)
        current = reply.get("claim_authority") if isinstance(reply, Mapping) else None
        if not isinstance(current, Mapping):
            raise HostError("Runtime renewal did not confirm claim authority")
        stable = set(original) - {"lease_expires_at"}
        if set(current) != set(original) or any(current[k] != original[k] for k in stable):
            raise HostError("Runtime renewal changed issuing claim authority")
        try:
            from datetime import datetime, timezone
            if datetime.fromisoformat(current["lease_expires_at"]) <= datetime.now(timezone.utc):
                raise HostError("Runtime claim authority expired")
        except (TypeError, ValueError, KeyError) as exc:
            raise HostError("invalid Runtime claim authority deadline") from exc
        return _AuthenticatedAttempt(tuple(sorted(current.items())))

    def task(self, task_id: str):
        return self.generated.get_task(task_id)

    def upload_child_input(self, data: bytes, *, descriptor: Mapping[str, Any],
                           project_id: str | None, run_id: str, task_id: str,
                           attempt_id: str, lease_id: str, fence: int, runtime_epoch: int):
        """Publish an immutable intermediate snapshot, without inline settlement."""
        if self._claimed_runtime_epoch(attempt_id) != runtime_epoch:
            raise HostError("child input upload epoch disagrees with claim")
        if not self.executor_id or not all((run_id, task_id, attempt_id, lease_id)):
            raise HostError("child input upload provenance is incomplete")
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if descriptor.get("object_id") != digest or descriptor.get("size") != len(data):
            raise HostError("child input snapshot identity changed")
        binding = {"project_id": project_id, "run_id": run_id, "task_id": task_id,
                   "attempt_id": attempt_id, "executor_id": self.executor_id,
                   "lease_id": lease_id, "fence": fence, "runtime_epoch": runtime_epoch,
                   "output_key": descriptor["name"], "output_port": descriptor["output_port"],
                   "filename": descriptor["filename"], "digest": digest, "size": len(data),
                   "media_type": descriptor["media_type"]}
        return self.generated.ingest_object(
            data, media_type=binding["media_type"], filename=binding["filename"],
            upload_binding=binding, idempotency_key="output-" + _canonical_digest(binding))

    def admit_child(self, *, child: Mapping[str, Any], inputs: Mapping[str, Any],
                    ordered_inputs: list[Mapping[str, Any]], derived_inputs: list[Mapping[str, Any]],
                    task_id: str, attempt_id: str, lease_id: str, fence: int, runtime_epoch: int,
                    run_id: str, project_id: str | None):
        """Exact D18 issuance/admission pair; signed receipt stays in this frame."""
        from ._child_bridge import ChildBridgeError, resource

        with self._heartbeat_lock:
            self.heartbeat(task_id, lease_id, attempt_id=attempt_id, fence=fence)
            if self._claimed_runtime_epoch(attempt_id) != runtime_epoch:
                raise HostError("child admission epoch disagrees with claim")
            receipt = resource(self.generated.issue_child_authority(
                attempt_id, lease_id=lease_id, fence=fence, runtime_epoch=runtime_epoch,
                child=dict(child), derived_inputs=[dict(ref) for ref in derived_inputs]))
            refs = receipt.get("derived_inputs")
            if (receipt.get("child") != child or receipt.get("parent_task_id") != task_id
                    or receipt.get("parent_attempt_id") != attempt_id or type(refs) is not list
                    or len(refs) != len(derived_inputs) or not isinstance(receipt.get("authority"), str)):
                raise ChildBridgeError("foreign child authority receipt")
            for supplied, ref in zip(derived_inputs, refs):
                if (not isinstance(ref, Mapping) or any(ref.get(k) != v for k, v in supplied.items())
                        or ref.get("parent_attempt_id") != attempt_id or not ref.get("association_id")):
                    raise ChildBridgeError("derived receipt identity or order changed")
            by_name = {ref["name"]: ref for ref in refs}
            descriptors = []
            child_inputs = dict(inputs)
            for original in ordered_inputs:
                required = original.get("required")
                if type(required) is not bool:
                    raise ChildBridgeError("host ordered input requiredness must be boolean")
                ref = by_name.get(original["name"], original)
                descriptor = {k: ref[k] for k in ("name", "object_id", "filename")}
                descriptor["digest"] = ref["object_id"]
                descriptor["required"] = required
                descriptors.append(descriptor)
                child_inputs[ref["name"]] = {k: descriptor[k] for k in ("object_id", "digest", "filename")}
            task = {"capability_id": child["capability_id"], "capability_digest": child["capability_digest"],
                    "input_object_ids": [ref["object_id"] for ref in descriptors],
                    "spec": {"capability_id": child["capability_id"], "kind": "action", "inputs": child_inputs}}
            if descriptors:
                task["execution_request"] = normalize_execution_request({
                    "schema_version": 1, "target": {"kind": "default"}, "inputs": descriptors,
                })
            return self.generated.admit_delegated_task(
                authority=receipt["authority"], task=task, idempotency_key=child["child_id"])

    def child_outputs(self, task_id: str):
        rows, cursor = self.generated.list_managed_outputs(task_id)
        if cursor is not None:
            raise HostError("child managed outputs exceed one bounded page")
        return rows

    def child_output(self, association_id: str):
        return self.generated.get_managed_output(association_id)

    def settle(self, task_id: str, lease_token: str, *, result: Mapping[str, Any], outputs: list[dict[str, Any]], effect: Mapping[str, Any] | None, attempt_id: str | None = None, fence: int | None = None):
        if not attempt_id or fence is None:
            raise HostError("generated settlement requires attempt_id and fence")
        settlement = {
            "attempt_id": attempt_id,
            "lease_id": lease_token,
            "fence": int(fence),
            "outputs": outputs,
            "effect": effect,
            "result": dict(result),
            "runtime_epoch": self._claimed_runtime_epoch(attempt_id),
        }
        return self.generated.settle_attempt(
            attempt_id,
            settlement,
            idempotency_key=f"settle-{attempt_id}-{fence}",
        )

    def fail(
        self,
        task_id: str,
        lease_token: str,
        error: str | Mapping[str, Any],
        *,
        retryable: bool = False,
        attempt_id: str | None = None,
        fence: int | None = None,
        failure_diagnostic: Mapping[str, Any] | None = None,
    ):
        if not attempt_id or fence is None:
            raise HostError("generated failure requires attempt_id and fence")
        payload: Any = dict(error) if isinstance(error, Mapping) else {"message": str(error), "retryable": bool(retryable)}
        if failure_diagnostic:
            payload["diagnostic"] = dict(failure_diagnostic)
        return self.generated.fail_attempt(
            attempt_id,
            lease_id=lease_token,
            fence=int(fence),
            error=payload,
            runtime_epoch=self._claimed_runtime_epoch(attempt_id),
            idempotency_key=f"fail-{attempt_id}-{fence}",
        )

    def cancel(
        self,
        task_id: str,
        *,
        attempt_id: str | None = None,
        fence: int | None = None,
        lease_id: str | None = None,
    ):
        if attempt_id is None or fence is None:
            return self.generated.cancel_task(task_id, idempotency_key=f"cancel-{task_id}")
        task = self.generated.get_task(task_id)
        current_attempt = (
            task.get("attempt_id")
            if isinstance(task, Mapping)
            else getattr(task, "attempt_id", None)
        )
        if current_attempt != attempt_id:
            raise HostError("runtime cancellation target no longer matches attempt")
        version = task.get("version") if isinstance(task, Mapping) else getattr(task, "version", None)
        if isinstance(version, bool) or not isinstance(version, int):
            raise HostError("runtime cancellation target has no version fence")
        return self.generated.cancel_task(
            task_id,
            expected_version=version,
            idempotency_key=f"cancel-{task_id}-{attempt_id}-{int(fence)}",
        )

    def get_object(self, digest: str) -> bytes:
        response = self.generated.get_object(digest)
        return response.data

    def upload_object(
        self,
        path: Path,
        *,
        project_id: str | None,
        media_type: str,
        filename: str | None = None,
        run_id: str,
        task_id: str,
        attempt_id: str,
        lease_id: str,
        fence: int,
        output_key: str,
        output_port: str,
        runtime_epoch: int | None = None,
    ):
        if project_id is not None and (not isinstance(project_id, str) or not project_id.strip()):
            raise HostError("output upload project_id must be a non-empty string or None")
        claim_epoch = self._claimed_runtime_epoch(attempt_id)
        if runtime_epoch is None:
            runtime_epoch = claim_epoch
        elif int(runtime_epoch) != claim_epoch:
            raise HostError("output upload runtime_epoch does not match the claim fence")
        executor_id = self.executor_id
        if any(
            not isinstance(value, str) or not value
            for value in (executor_id, run_id, task_id, attempt_id, lease_id, output_key, output_port, filename)
        ):
            raise HostError("output upload provenance is incomplete")
        binding = {
            "project_id": project_id,
            "run_id": run_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "executor_id": executor_id,
            "lease_id": lease_id,
            "fence": int(fence),
            "runtime_epoch": int(runtime_epoch),
            "output_key": output_key,
            "output_port": output_port,
            "filename": filename,
        }
        with path.open("rb") as stream:
            data = stream.read()
        binding.update({
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "media_type": media_type,
        })
        idempotency_key = "output-" + _canonical_digest(binding)
        # Worker credentials may publish CAS bytes but are deliberately not
        # granted projects:write. Settlement owns that association
        # transactionally; upload carries an explicit, collision-safe
        # provenance binding and only publishes immutable object bytes.
        # Workspace tasks have no project binding and publish the same bytes.
        return self.generated.ingest_object(
            data,
            media_type=media_type,
            idempotency_key=idempotency_key,
            filename=filename,
            upload_binding=binding,
        )

    def publish_timeline_render(
        self,
        attempt_id: str,
        lease_token: str,
        *,
        fence: int,
        timeline_id: str,
        expected_version: int,
        config: Mapping[str, Any],
        registry: Mapping[str, Any],
        render: Mapping[str, Any],
        idempotency_key: str,
    ):
        """Use the worker-scoped Runtime publication checkpoint."""
        return self.generated.publish_timeline_render(
            attempt_id,
            lease_id=lease_token,
            fence=int(fence),
            runtime_epoch=self._claimed_runtime_epoch(attempt_id),
            timeline_id=timeline_id,
            expected_version=int(expected_version),
            config=config,
            registry=registry,
            render=render,
            idempotency_key=idempotency_key,
        )


_LOGGER = logging.getLogger(__name__)
_OPTIONAL_EXTERNAL_MATRIX_PREFIXES = ("discord_local.", "hivemind.", "seedance_local.")


def _unapproved_reason(capability_id: str, matrix_path: Path | None) -> str:
    where = str(matrix_path) if matrix_path is not None else "config/astrid-beta-capabilities.json"
    return (
        f"capability {capability_id!r} has no approved capability-matrix row; "
        f"add a row to {where} (disposition, evidence_reason, adapter_family, resource_keys), "
        "then restart the host and run: python -m astrid.core.execution.capability_ledger approve"
    )


def _quarantine_scope(pack_roots: Any) -> tuple[tuple[Path, ...], frozenset[str]]:
    """Return the quarantined pack folders and ids under the host's pack roots.

    Uses the same per-pack admission as discovery (``astrid.core.pack.loader``):
    fail-closed packs still raise here, every other invalid pack is excluded.
    """
    from astrid.core.pack.loader import scan_packs

    dirs: list[Path] = []
    ids: set[str] = set()
    for root in pack_roots:
        for record in scan_packs(root).quarantined:
            dirs.append(record.pack_dir.resolve())
            ids.add(record.pack_id)
    return tuple(dirs), frozenset(ids)


def _inside_quarantined(path: Path, quarantined_dirs: tuple[Path, ...]) -> bool:
    if not quarantined_dirs:
        return False
    resolved = path.resolve()
    return any(resolved == directory or directory in resolved.parents for directory in quarantined_dirs)


def component_load_failures(pack_roots: Any = None) -> list[dict[str, Any]]:
    """Return executor and orchestrator manifests that discovery skips, with the loader's error.

    Uses the same folder loaders and quarantine scope as ``GenericPackHost``
    discovery, so the doctor section and the pack validator cannot disagree with
    what discovery drops. Read-only and never raises for one bad manifest.
    """
    from astrid.core.execution.executor.folder import discover_folder_executor_roots, load_folder_executor
    from astrid.core.execution.orchestrator.folder import discover_folder_orchestrator_roots, load_folder_orchestrator
    from astrid.core.pack.loader import _default_pack_roots

    roots = tuple(pack_roots) if pack_roots is not None else _default_pack_roots()
    quarantined_dirs, _ids = _quarantine_scope(roots)
    failures: list[dict[str, Any]] = []
    loaders = (
        ("executor", discover_folder_executor_roots, load_folder_executor),
        ("orchestrator", discover_folder_orchestrator_roots, load_folder_orchestrator),
    )
    for kind, discover_roots, load_one in loaders:
        for root in roots:
            for folder in discover_roots(root):
                if _inside_quarantined(folder, quarantined_dirs):
                    continue
                try:
                    load_one(folder)
                except (OSError, ValueError) as exc:
                    failures.append({
                        "kind": kind,
                        "folder": str(folder),
                        "error": str(exc),
                        "fix": f"fix the {kind} manifest, then run: python3 -m astrid.core.pack.cli validate {folder.parents[1]}",
                    })
    return failures


def executor_skip_section(pack_roots: Any = None) -> dict[str, Any]:
    """Doctor section: executors and orchestrators skipped at discovery, with the reason. Never raises."""
    try:
        records = component_load_failures(pack_roots)
    except Exception as exc:  # noqa: BLE001 - doctor reports the failure instead of crashing
        return {"count": 0, "skipped": [], "error": str(exc)}
    return {"count": len(records), "skipped": records, "error": None}


def _configured_claim_target(raw_value: str | None = None) -> dict[str, Any] | None:
    """Return an explicit target for queue claims when one is configured.

    Targeted task admission creates a Runtime-owned binding, and the claim
    boundary must repeat the same target so the Runtime can route the task to
    the intended worker.  Keep this opt-in so existing untargeted hosts retain
    queue-wide claim behavior.
    """
    raw = (
        os.environ.get("ASTRID_EXECUTION_TARGET_JSON", "")
        if raw_value is None
        else raw_value
    ).strip()
    if not raw:
        return None
    try:
        target = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise HostError("ASTRID_EXECUTION_TARGET_JSON must be valid JSON") from exc
    if not isinstance(target, Mapping):
        raise HostError("ASTRID_EXECUTION_TARGET_JSON must be a JSON object")
    try:
        normalized = normalize_execution_request({"target": dict(target)})
    except ValueError as exc:
        raise HostError(f"ASTRID_EXECUTION_TARGET_JSON is not a valid execution target: {exc}") from exc
    if not isinstance(normalized, Mapping) or not isinstance(normalized.get("target"), Mapping):
        raise HostError("ASTRID_EXECUTION_TARGET_JSON did not produce an execution target")
    return dict(normalized["target"])


def _startup_identity_attestation(
    *,
    source_checkout: Path | None,
    source_inventory_identity: str | None,
    expected_source_checkout_digest: str | None = None,
    expected_source_closure_digest: str | None = None,
    boot_manifest_hash: str | None = None,
    require_target: bool = False,
    target_json: str | None = None,
) -> dict[str, Any]:
    """Validate and record the worker identity before discovery or registration.

    A readiness marker is useful only when it proves which source and target
    produced it.  Keep the check small and deterministic: target identity is
    normalized by the execution-request contract, while source identity is
    the exact pack tree used by capability admission.  The expected source
    digest is supplied by the launcher so a stale checkout fails before it can
    advertise readiness.
    """
    target = _configured_claim_target(target_json)
    if target is None and require_target:
        raise HostError(
            "worker startup requires an explicit execution target; set "
            "ASTRID_EXECUTION_TARGET_JSON or pass --execution-target-json"
        )
    if source_checkout is None:
        if expected_source_checkout_digest:
            raise HostError("source checkout digest was supplied without a source checkout")
        if expected_source_closure_digest:
            raise HostError("source closure digest was supplied without a source checkout")
        source_digest = None
        source_closure = None
    else:
        try:
            source_digest = source_checkout_digest(source_checkout)
        except (OSError, ValueError) as exc:
            raise HostError(f"worker source identity could not be verified: {exc}") from exc
        expected = str(expected_source_checkout_digest or "").strip()
        if expected and expected != source_digest:
            raise HostError(
                "worker source identity mismatch: "
                f"expected {expected!r}, observed {source_digest!r}"
            )
        try:
            source_closure = source_checkout_closure(source_checkout)
        except (OSError, ValueError) as exc:
            raise HostError(f"worker executable source closure could not be verified: {exc}") from exc
        expected_closure = str(expected_source_closure_digest or "").strip()
        if expected_closure and expected_closure != source_closure["digest"]:
            raise HostError(
                "worker executable source closure mismatch: "
                f"expected {expected_closure!r}, observed {source_closure['digest']!r}"
            )
    return {
        "schema_version": 1,
        "target": target,
        "target_digest": (
            "sha256:" + _canonical_digest(target) if target is not None else None
        ),
        "source": {
            "checkout": str(source_checkout) if source_checkout is not None else None,
            "checkout_digest": source_digest,
            "closure_digest": source_closure["digest"] if source_closure else None,
            "closure": source_closure["components"] if source_closure else None,
            "inventory_identity": str(source_inventory_identity or ""),
        },
        "boot_manifest_hash": boot_manifest_hash,
    }


class GenericPackHost:
    """Discover, register, preflight, and execute pack capabilities."""

    def __init__(
        self,
        *,
        pack_roots: list[str | Path],
        client: RuntimeProtocolClient | Any | None = None,
        executor_id: str = "astrid-pack-host",
        max_concurrency: int = 1,
        attempt_root: str | Path | None = None,
        attempt_base: str | Path | None = None,
        capability_matrix: str | Path | None = None,
        credential_source: Mapping[str, str] | None = None,
        source_inventory_identity: str | None = None,
        boot_manifest_path: str | Path | None = None,
        boot_manifest_hash: str | None = None,
        execution_policy: ExecutionGuardPolicy | None = None,
        confinement_policy: _ConfinementPolicy | None = None,
        remotion_closure_binding=None,
    ):
        configured_roots = [Path(root).expanduser().resolve() for root in pack_roots]
        # ASTRID_PACKS_PATH is an explicit discovery input, never an implicit
        # directory. Keep it in the same admitted root set so env-only packs
        # receive identical discovery, digest, and child import treatment.
        for raw_root in os.environ.get(ASTRID_PACKS_PATH, "").split(os.pathsep):
            if raw_root:
                configured_roots.append(Path(raw_root).expanduser().resolve())
        self.pack_roots = tuple(dict.fromkeys(configured_roots))
        self.client = client
        self.executor_id = executor_id
        self.max_concurrency = max(1, int(max_concurrency))
        if attempt_root is not None and attempt_base is not None:
            raise ValueError("attempt_root and attempt_base are mutually exclusive")
        self.attempt_root = Path(attempt_root).expanduser().resolve() if attempt_root else None
        self.attempt_base = Path(attempt_base).expanduser().resolve() if attempt_base else None
        self.capabilities: dict[str, CapabilityRecord] = {}
        self.stale_matrix_entries: tuple[str, ...] = ()
        self._registered_digests: dict[str, str] = {}
        self._registered_state: dict[str, dict[str, str]] = {}
        self._registered_runtime_state: dict[str, Any] = {}
        # Idle readiness may reuse only a prior full native validation in the
        # identical host context. Execution never consumes this observation.
        self._remotion_full_preflight: dict[str, tuple[str, dict[str, Any]]] = {}
        self._runtime_registration_fence: str | None = None
        self._registration_refresh_blocked = False
        self.capability_matrix_path = Path(capability_matrix).expanduser().resolve() if capability_matrix else self._default_matrix_path()
        # Keep the mapping live when the default is os.environ so test/runtime
        # credential rotation is observed without snapshotting secret values.
        self.credential_source = os.environ if credential_source is None else credential_source
        # A supplied mapping is the caller's explicit credential tier.  Keep
        # process environment fallback available so shared astrid.env still
        # wins over stale process values and fills an empty mapping.
        self._credential_source_is_explicit = credential_source is not None
        self.ledger = load_capability_ledger(self.capability_matrix_path) if self.capability_matrix_path else {"capabilities": [], "sources": {}}
        self.matrix: dict[str, dict[str, Any]] = self._load_matrix(self.capability_matrix_path)
        self.source_epoch = "uninitialized"
        self.source_inventory_identity = str(source_inventory_identity or "")
        self.runtime_state: dict[str, Any] = {}
        # The application composition root owns emission.  The host only reads
        # this derived stamp when it prepares completion provenance.
        self.boot_manifest_path = (
            # Preserve lexical components until boot-manifest validation.  In
            # particular, resolving here would hide a symlinked parent.
            Path(boot_manifest_path).expanduser()
            if boot_manifest_path is not None
            else None
        )
        if boot_manifest_hash is not None:
            from astrid.core._shared.boot_manifest import normalize_sha256_digest

            self.boot_manifest_hash = normalize_sha256_digest(
                boot_manifest_hash, label="boot manifest hash"
            )
        else:
            self.boot_manifest_hash = None
        self.execution_policy = execution_policy or ExecutionGuardPolicy()
        # Composition-root authority only. Never populate from task/admission,
        # manifest, broker, project-path, MCP or environment configuration.
        self._confinement_policy = confinement_policy
        # Captured from trusted host startup, never a request's PATH/env.
        self._executable_admission = ExecutableAdmission(os.environ.get("PATH", os.defpath))
        self._attempt_policy_factory = _AttemptPolicyFactory(self._executable_admission) if isinstance(client, RuntimeProtocolClient) else None
        self._attempt_claim_authority = None
        # Provider route grants are intentionally scoped to this host process;
        # their signing key never crosses into a child or runtime payload.
        self._provider_grants = ProviderRouteGrantAuthority()
        self._pending_provider_grants: dict[str, str] = {}
        # Engine-neutral lifecycle custody lives beside, not inside, the
        # Runtime client.  Adapters are opened explicitly by the execution
        # path; the host owns shutdown fencing for every opened session.
        self.managed_tool_session = ManagedToolSession(
            manager_id=self.executor_id
        )

        self._active_processes: set[subprocess.Popen] = set()
        self._process_lock = threading.RLock()
        self._child_bridges: set[Any] = set()
        self._remotion_host_session = None
        self._remotion_closure_binding = remotion_closure_binding
        self._renderer_setup_lock = threading.RLock()
        self._host_lifetime_processes: set[subprocess.Popen] = set()
        self._shutdown = threading.Event()
        # An unregistered host must retain the existing claim-loop failure
        # semantics. register() arms the first refresh after success.
        self._registration_refresh_deadline = float("inf")
        self._cleanup_uncertain = False
        self._cleanup_uncertain_reason: str | None = None
        self._cleanup_uncertain_since: str | None = None
        # Set by the host entrypoint.  The latch record is written here so that
        # `astrid doctor` can report a blocked host from another process.
        self.cleanup_latch_path: Path | None = None
        self.evidence_status_path: Path | None = None
        self._evidence_observation_binding: dict[str, Any] = {}
        self._evidence_observation_lock = threading.RLock()
        # Queued tasks already reported as unclaimable, as (task_id, reason).
        self._reported_blocked_claims: set[tuple[str, str]] = set()
        self._last_cleanup_receipt: dict[str, Any] | None = None
        # Handoff observes the real claim/registration/attempt/settlement path.
        # The condition is held only while checking the gate and publishing
        # counters; Runtime and provider RPCs always happen after it is released.
        self._handoff_condition = threading.Condition(threading.RLock())
        self._handoff_accounting_owner = self
        self._handoff_claim_gate_closed = False
        self._handoff_claim_rpc_in_flight = 0
        self._handoff_active_attempts = 0
        self._handoff_pending_settlements = 0
        self._handoff_registration_rpc_in_flight = 0
        self._handoff_registration_authorized_thread: int | None = None
        self._handoff_registration_authorized_id: str | None = None
        self._handoff_attempt_threads: set[int] = set()
        self._handoff_observation_known = True
        # A command child is short-lived while the manager-owned VibeComfy
        # server persists across tasks.  Keep only the last successful,
        # verified session/model hint in the host; it is revalidated by the
        # child adapter against the HC-03 registry before reuse.
        self._vibecomfy_warmth_hint: str | None = None
        self._vibecomfy_current_warmth_hint: str | None = None
        self._vibecomfy_requested_warmth_hint: str | None = None
        self._claim_slots: tuple[GenericPackHost, ...] = ()
        self._claim_loop_lock = threading.Lock()

    def _allocate_attempt_root(self, task_id: str, attempt_id: str) -> Path:
        """Allocate the filesystem namespace for one claimed attempt."""
        if self.attempt_base is not None:
            self.attempt_base.mkdir(parents=True, exist_ok=True)
            if self.attempt_base.is_symlink() or not self.attempt_base.is_dir():
                raise HostError("attempt_base must be an absolute non-symlink directory")
            root = self.attempt_base / f"{task_id}-{attempt_id}"
            if root.exists() or root.is_symlink():
                raise HostError(f"attempt-base allocation already exists: {root.name}")
            root.mkdir()
            return root.resolve()
        root = self.attempt_root or Path(tempfile.mkdtemp(prefix=f"astrid-attempt-{task_id}-")).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _workload_launch_plan(
        self, argv: Sequence[str], *, attempt: Path, cwd: Path | None,
        environment: Mapping[str, str], inherited_fds: tuple[int, ...],
        broker_endpoint: str | None, child_bridge: Any = None,
    ) -> _ConfinedLaunchPlan:
        policy = self._required_confinement_policy(attempt)
        if self._attempt_claim_authority is not None:
            fields = dict(self._attempt_claim_authority.fields)
            self.client.confirm_claim_authority(**{k: fields[k] for k in (
                "task_id", "attempt_id", "lease_id", "fence", "runtime_epoch", "project_id", "operation", "executor_id")})
        if cwd == attempt:
            cwd = policy.private_write_root
        # Only this existing bridge's retained pair can extend this attempt's
        # immutable descriptor policy. Caller pass_fds remain unmatched.
        bridge_launch = None
        if child_bridge is not None:
            if policy.pass_fds:
                raise HostError("child bridge cannot add caller-selected descriptors")
            bridge_launch = child_bridge.launch_binding()
            policy = replace(policy, pass_fds=bridge_launch.descriptors)
            self._confinement_policy = policy
            inherited_fds = bridge_launch.descriptors
        plan = _confined_launch_plan(
            argv, policy=policy, cwd=cwd, parent_environment=environment,
            broker_endpoint=broker_endpoint or None,
        )
        if set(inherited_fds) != set(policy.pass_fds):
            raise HostError("worker descriptors differ from host confinement policy")
        if bridge_launch is not None:
            bridge_launch.select(plan.argv, str(plan.cwd), plan.environment, plan.pass_fds)
        return replace(plan, child_bridge_launch=bridge_launch)

    def _required_confinement_policy(self, attempt: Path) -> _ConfinementPolicy:
        policy = self._confinement_policy
        if type(policy) is not _ConfinementPolicy:
            raise HostError("required confinement policy is missing or invalid")
        if self._attempt_claim_authority is not None and policy.claim_binding != self._attempt_claim_authority.fields:
            raise HostError("confinement policy claim binding changed")
        if not isinstance(policy.attempt_root, Path) or not policy.attempt_root.is_absolute():
            raise HostError("invalid confinement attempt root")
        if policy.attempt_root.resolve() != attempt.resolve():
            raise HostError("confinement policy belongs to a different attempt")
        return policy

    @property
    def last_cleanup_receipt(self) -> dict[str, Any] | None:
        return dict(self._last_cleanup_receipt) if self._last_cleanup_receipt is not None else None

    def _handoff_quiescence_locked(self) -> dict[str, Any]:
        known = bool(self._handoff_observation_known)
        return {
            "claim_gate_closed": self._handoff_claim_gate_closed if known else None,
            "claim_rpc_in_flight": self._handoff_claim_rpc_in_flight if known else None,
            "active_attempts": self._handoff_active_attempts if known else None,
            "pending_settlements": self._handoff_pending_settlements if known else None,
            "registration_rpc_in_flight": self._handoff_registration_rpc_in_flight if known else None,
            "observation_status": "known" if known else "unknown",
        }

    def handoff_quiescence(self) -> dict[str, Any]:
        with self._handoff_condition:
            return self._handoff_quiescence_locked()

    def pause_claim_admission(self) -> tuple[str, dict[str, Any]]:
        """Close claim admission and return measured host-side quiescence."""
        with self._handoff_condition:
            if self._handoff_claim_gate_closed:
                return "conflict", self._handoff_quiescence_locked()
            self._handoff_claim_gate_closed = True
            snapshot = self._handoff_quiescence_locked()
            counters = tuple(snapshot[key] for key in (
                "claim_rpc_in_flight", "active_attempts", "pending_settlements",
                "registration_rpc_in_flight",
            ))
            if snapshot["observation_status"] != "known" or any(value is None for value in counters):
                return "unresolved", snapshot
            if any(value != 0 for value in counters):
                # The known prior gate was open. Reopen only this exact state;
                # the real in-flight work remains counted until it settles.
                self._handoff_claim_gate_closed = False
                return "active_work", self._handoff_quiescence_locked()
            return "paused", self._handoff_quiescence_locked()

    def restore_claim_admission(self) -> tuple[bool, dict[str, Any]]:
        """Rollback a pre-export pause only from an observed zero-work state."""
        with self._handoff_condition:
            snapshot = self._handoff_quiescence_locked()
            if (snapshot["observation_status"] != "known" or not self._handoff_claim_gate_closed
                    or any(snapshot[key] != 0 for key in (
                        "claim_rpc_in_flight", "active_attempts", "pending_settlements",
                        "registration_rpc_in_flight",
                    ))):
                return False, snapshot
            self._handoff_claim_gate_closed = False
            return True, self._handoff_quiescence_locked()

    def _handoff_enter_claim_rpc(self) -> bool:
        with self._handoff_condition:
            if self._handoff_claim_gate_closed or not self._handoff_observation_known:
                return False
            self._handoff_claim_rpc_in_flight += 1
            return True

    def _handoff_leave_claim_rpc(self, thread_id: int, *, claimed: bool) -> None:
        with self._handoff_condition:
            self._handoff_claim_rpc_in_flight -= 1
            if self._handoff_claim_rpc_in_flight < 0:
                self._handoff_observation_known = False
                self._handoff_claim_rpc_in_flight = 0
            if claimed:
                self._handoff_active_attempts += 1
                self._handoff_attempt_threads.add(thread_id)
            self._handoff_condition.notify_all()

    def _handoff_leave_attempt(self, thread_id: int) -> None:
        with self._handoff_condition:
            if thread_id in self._handoff_attempt_threads:
                self._handoff_attempt_threads.remove(thread_id)
                self._handoff_active_attempts -= 1
                if self._handoff_active_attempts < 0:
                    self._handoff_observation_known = False
                    self._handoff_active_attempts = 0
                self._handoff_condition.notify_all()

    def _handoff_begin_direct_attempt(self) -> bool:
        thread_id = threading.get_ident()
        with self._handoff_condition:
            if thread_id in self._handoff_attempt_threads:
                return False
            if self._handoff_claim_gate_closed or not self._handoff_observation_known:
                raise HostError("generic host claim gate is closed or unresolved")
            self._handoff_active_attempts += 1
            self._handoff_attempt_threads.add(thread_id)
            return True

    def _handoff_begin_registration(self) -> None:
        with self._handoff_condition:
            authorized_fenced = (
                self._handoff_claim_gate_closed
                and self._handoff_registration_authorized_thread == threading.get_ident()
                and bool(self._handoff_registration_authorized_id)
            )
            if ((self._handoff_claim_gate_closed and not authorized_fenced)
                    or not self._handoff_observation_known):
                raise HostError("generic host registration is blocked by the handoff claim gate")
            self._handoff_registration_rpc_in_flight += 1

    def _handoff_authorize_registration(self, handoff_id: str) -> None:
        """Authorize one real registration RPC on this thread while fenced."""
        with self._handoff_condition:
            snapshot = self._handoff_quiescence_locked()
            if (not self._handoff_claim_gate_closed or not self._handoff_observation_known
                    or self._handoff_registration_rpc_in_flight != 0
                    or self._handoff_registration_authorized_thread is not None
                    or not handoff_id):
                raise HostError("fenced handoff registration preconditions are unresolved")
            if any(snapshot[key] != 0 for key in (
                "claim_rpc_in_flight", "active_attempts", "pending_settlements",
                "registration_rpc_in_flight",
            )):
                raise HostError("fenced handoff registration requires measured quiescence")
            self._handoff_registration_authorized_thread = threading.get_ident()
            self._handoff_registration_authorized_id = handoff_id

    def _handoff_revoke_registration(self, handoff_id: str) -> None:
        with self._handoff_condition:
            if (self._handoff_registration_authorized_thread == threading.get_ident()
                    and self._handoff_registration_authorized_id == handoff_id):
                self._handoff_registration_authorized_thread = None
                self._handoff_registration_authorized_id = None

    def _handoff_end_registration(self) -> None:
        with self._handoff_condition:
            self._handoff_registration_rpc_in_flight -= 1
            if self._handoff_registration_rpc_in_flight < 0:
                self._handoff_observation_known = False
                self._handoff_registration_rpc_in_flight = 0
            self._handoff_condition.notify_all()

    def _handoff_settlement_call(self, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        with self._handoff_condition:
            if not self._handoff_observation_known or (
                self._handoff_claim_gate_closed and self._handoff_active_attempts == 0
            ):
                raise HostError("settlement is blocked by unresolved handoff quiescence")
            self._handoff_pending_settlements += 1
        try:
            return operation(*args, **kwargs)
        finally:
            with self._handoff_condition:
                self._handoff_pending_settlements -= 1
                if self._handoff_pending_settlements < 0:
                    self._handoff_observation_known = False
                    self._handoff_pending_settlements = 0
                self._handoff_condition.notify_all()

    def _latch_cleanup_uncertainty(self, reason: str) -> None:
        """Block admissions until this host is stopped, keeping the first cause.

        A later failure must not overwrite the reason that started the block.
        The record is also written for ``astrid doctor``; failing to write it
        never changes the in-process block.
        """
        if self._cleanup_uncertain:
            return
        self._cleanup_uncertain = True
        self._cleanup_uncertain_reason = reason or "cleanup outcome unknown"
        self._cleanup_uncertain_since = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if self.cleanup_latch_path is not None:
            write_cleanup_latch(
                self.cleanup_latch_path,
                reason=self._cleanup_uncertain_reason,
                since=self._cleanup_uncertain_since,
                recover_with=self._cleanup_recovery_action(),
            )

    def _cleanup_recovery_action(self) -> str:
        return (
            "use the owner-bound supervisor to stop this host after confirming no owned writer survives, then reopen "
            "the Astrid client to start a fresh pack host: python -c "
            "'from astrid.sdk import AstridClient; AstridClient.open_from_launcher().__enter__()'"
        )

    def _cleanup_block_error(self) -> HostError:
        return HostError(
            "generic host admissions are blocked by cleanup uncertainty: "
            f"{self._cleanup_uncertain_reason}; since {self._cleanup_uncertain_since}; "
            f"recover with: {self._cleanup_recovery_action()}"
        )

    def _cleanup_ephemeral_attempt_or_latch(self, root: Path) -> None:
        """Delete one owned root, latching uncertainty if observation fails."""
        try:
            if self._cleanup_uncertain:
                raise HostError("process cleanup uncertainty; attempt retained")
            _cleanup_ephemeral_attempt(root)
            self.execution_policy.evidence_budget.release(str(Path(root).resolve(strict=False)))
        except Exception as exc:
            self._latch_cleanup_uncertainty(str(exc))
            self._last_cleanup_receipt = {
                "path": str(root),
                "intended_disposition": "deleted",
                "status": "uncertain",
                "errors": [str(exc)],
            }
            raise

    def _track_process(self, process: subprocess.Popen) -> None:
        with self._process_lock:
            self._active_processes.add(process)
        # A signal can arrive between Popen and registration in the set.  Do
        # not let that small window leave an owned child running after host
        # shutdown has begun.
        if self._shutdown.is_set():
            _terminate_process_group(process)

    def _untrack_process(self, process: subprocess.Popen) -> None:
        with self._process_lock:
            self._active_processes.discard(process)

    def _track_host_lifetime_process(self, process: subprocess.Popen) -> None:
        with self._process_lock:
            self._host_lifetime_processes.add(process)
        if self._shutdown.is_set():
            raise HostError("host shutdown began during renderer child acquisition")

    def _untrack_host_lifetime_process(self, process: subprocess.Popen) -> None:
        with self._process_lock:
            self._host_lifetime_processes.discard(process)

    def _renderer_scratch_account(self, root: Path) -> None:
        observed = self.execution_policy.evidence_bytes(root)
        self.execution_policy.evidence_budget.account(
            str(root), observed, self.execution_policy.evidence_cap_bytes)
        self._publish_evidence_status()

    def _renderer_task_lease(self, environment: Mapping[str, str], attempt: Path, *, policy: _ConfinementPolicy):
        from .remotion_host_session import HostRemotionSession
        if policy is not self._required_confinement_policy(attempt):
            raise HostError("renderer lease requires the admitted confinement policy")
        # Validate the same immutable policy before retaining a lease or launching.
        _confined_launch_plan([sys.executable], policy=policy)
        # Never hold _process_lock across owner operations: owner callbacks
        # acquire it when retaining/draining the original child.
        with self._renderer_setup_lock:
            if self._shutdown.is_set() or self._cleanup_uncertain:
                raise HostError("renderer lease requires an active qualified host")
            if self._remotion_host_session is None:
                self._remotion_host_session = HostRemotionSession(
                    track=self._track_host_lifetime_process,
                    untrack=self._untrack_host_lifetime_process,
                    root_parent=str(self.attempt_base) if self.attempt_base else None,
                    account=self._renderer_scratch_account,
                    retire=lambda root: self.execution_policy.evidence_budget.release(str(root)),
                    closure_binding=self._remotion_closure_binding)
            return self._remotion_host_session.lease(
                environment, attempt=attempt, scratch=policy.private_write_root)

    def prepare_wan_session(
        self, *, owner_dir: str | Path, root: str | Path, python: str | Path,
        source_digest: str, config_digest: str, readiness_timeout: float,
        release_timeout: float, init_options: Mapping[str, Any] | None = None,
    ) -> SessionBinding:
        """Prepare one retained child in the host, before admission.

        W2.2 calls this after Runtime claim/attestation, then admits its Runtime
        invocation through this host's MTS. owner_dir must be the deployment's
        stable host-local custody directory across host restarts (not an attempt
        directory). No source discovery, orphan adoption or native install here.
        """
        if self._shutdown.is_set() or self._cleanup_uncertain:
            raise HostError("Wan child requires an active host")
        if readiness_timeout <= 0 or release_timeout <= 0:
            raise ValueError("Wan lifecycle deadlines must be positive")
        options = dict(init_options or {})
        if set(options) - {"config_path", "cli_args", "console_isatty"}:
            raise ValueError("unsupported Wan initialization options")
        owner = Path(owner_dir).expanduser().resolve()
        engine_root = Path(root).expanduser().resolve()
        # Preserve the venv executable path: resolving its symlink would select
        # the base interpreter and lose the independently installed environment.
        interpreter = Path(python).expanduser().absolute()
        if not engine_root.is_dir() or not interpreter.is_file():
            raise HostError("Wan child requires explicit source and interpreter paths")
        spec = {"owner_dir": str(owner), "root": str(engine_root), "python": str(interpreter),
                "source_digest": source_digest, "config_digest": config_digest,
                "runtime_instance_id": str(self.runtime_state.get("runtime_instance_id") or self.executor_id),
                "init": {**options, "root": str(engine_root), "output_dir": str(owner / "spool"),
                         "console_output": False}}
        spec = json.loads(json.dumps(spec, sort_keys=True))
        identity = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        manager = self.managed_tool_session
        current = manager.current_adapter
        if (manager.active and isinstance(current, _ManagedWanChildAdapter)
                and current.binding.execution_identity == identity):
            manager.observe(current.binding)
            return current.binding
        birth = secrets_module.token_hex(16)
        binding = SessionBinding(
            session_id=str(owner), runtime_instance_id=spec["runtime_instance_id"],
            process_birth_id=birth, endpoint="pipe:" + birth,
            source_digest=source_digest, config_digest=config_digest,
            execution_identity=identity, launch_generation=birth, engine_birth_id=birth,
        )
        # Construction is inert. MTS releases the previous slot before start()
        # can allocate any native state, and retains failed startup custody.
        adapter = _ManagedWanChildAdapter(
            spec=spec, binding=binding, readiness_timeout=readiness_timeout,
            release_timeout=release_timeout, track=self._track_process,
            untrack=self._untrack_process,
        )
        manager.open(capability=CapabilityDescriptor("wan2gp.generate_video", warm_reuse_expected=True),
                     binding=binding, adapter=adapter, start=adapter.start)
        manager.observe(binding)
        return binding


    def invoke_wan_session(
        self, token: AdmissionToken, settings: Mapping[str, Any], *,
        timeout: float, cancelled: Callable[[], bool], progress_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Return fenced native evidence; caller retains admission until custody.

        This does not claim, retry, publish or settle Runtime work. Failure or
        cancellation fences the current session; release/replacement remains MTS
        responsibility and must observe exit before another child starts.
        """
        if timeout <= 0:
            raise ValueError("Wan invocation deadline must be positive")
        manager = self.managed_tool_session
        adapter = manager.adapter_for(token, begin=True)
        if not isinstance(adapter, _ManagedWanChildAdapter):
            raise HostError("invocation does not own a Wan child")
        try:
            evidence = adapter.invoke(
                token,
                settings,
                timeout=timeout,
                cancelled=cancelled,
                progress_path=progress_path,
            )
            manager.adapter_for(token)
            return evidence
        except BaseException:
            # A late failure from an old generation cannot poison its successor.
            try:
                manager.fence_admission(token, reason="wan_invocation_failed_or_cancelled")
            except StaleAdmissionError:
                pass
            raise


    def shutdown(self) -> None:
        """Stop the host and every currently owned capability process."""
        with self.managed_tool_session._lock:
            self._shutdown.set()
        errors: list[BaseException] = []
        # Fence every serving slot before any session release can block or
        # report uncertainty. A failed release must not leave a sibling live.
        for slot in self._claim_slots:
            with slot.managed_tool_session._lock:
                slot._shutdown.set()
        for slot in self._claim_slots:
            try:
                slot.shutdown()
            except Exception as exc:
                errors.append(exc)
                slot._latch_cleanup_uncertainty(str(exc))
                self._latch_cleanup_uncertainty(str(exc))
        with self._process_lock:
            bridges = tuple(self._child_bridges)
        for bridge in bridges:
            try:
                bridge.revoke()
            except BaseException as exc:
                errors.append(exc)
        with self._renderer_setup_lock:
            renderer_owner = self._remotion_host_session
            if renderer_owner is not None:
                try:
                    renderer_owner.close()
                except BaseException as exc:
                    errors.append(exc)
                    self._latch_cleanup_uncertainty(str(exc))
        adapter = self.managed_tool_session.current_adapter
        if isinstance(adapter, _ManagedTaskAdapter):
            # The current CPU attempt retains its admission token until its
            # own finally has stopped and verified the original command and
            # released its session. Closing MTS here would clear that token
            # while release still correctly rejects a live command census.
            adapter.fence(reason="host_shutdown")
            owner = self._handoff_accounting_owner
            deadline = time.monotonic() + 5.0
            with owner._handoff_condition:
                while (owner._handoff_observation_known
                       and owner._handoff_active_attempts != 0):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    owner._handoff_condition.wait(timeout=remaining)
                complete = (owner._handoff_observation_known
                            and owner._handoff_active_attempts == 0)
            with self._process_lock:
                complete = complete and not self._active_processes
            if not complete or self._cleanup_uncertain:
                self._latch_cleanup_uncertainty("CPU attempt cleanup remains unresolved")
                raise CleanupUncertainError("CPU attempt cleanup remains unresolved")
        try:
            self.managed_tool_session.close(reason="host_shutdown")
        except BaseException as exc:
            errors.append(exc)
        with self._process_lock:
            active = tuple(self._active_processes | self._host_lifetime_processes)
        for process in active:
            try:
                _terminate_process_group(process, grace_seconds=1.0)
            except BaseException as exc:
                errors.append(exc)
        graph = getattr(self, "_native_engine_graph", None)
        if graph is not None:
            try:
                graph.abort()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            self._latch_cleanup_uncertainty("; ".join(str(error) for error in errors))
            raise CleanupUncertainError("host graph cleanup remains unresolved") from errors[0]

    def boot_manifest_provenance(self) -> dict[str, str] | None:
        """Return completion provenance for the root-owned manifest stamp."""
        if self.boot_manifest_path is None:
            return None
        from astrid.core._shared.boot_manifest import load_boot_manifest_hash

        stamped_manifest_hash = load_boot_manifest_hash(
            self.boot_manifest_path,
            support_root=self.boot_manifest_path.parents[1],
        )
        from astrid.core._shared.boot_manifest import normalize_sha256_digest

        if self.boot_manifest_hash and normalize_sha256_digest(
            stamped_manifest_hash, label="stamped boot manifest hash"
        ) != self.boot_manifest_hash:
            raise HostError("boot manifest changed after host startup")
        return {
            "kind": "astrid.boot_manifest",
            "sha256": stamped_manifest_hash,
        }

    def _client_operation(self, name: str):
        if self.client is None:
            raise HostError(f"runtime client is required for {name}")
        operation = getattr(self.client, name, None)
        if not callable(operation):
            label = "claim-next" if name == "claim_next" else name
            raise HostError(f"runtime client lacks canonical {label} operation")
        return operation

    def _default_matrix_path(self) -> Path | None:
        checkout = Path(__file__).resolve().parents[3]
        candidate = checkout / "config" / "astrid-beta-capabilities.json"
        source_pack_root = checkout / "astrid" / "packs"
        if candidate.is_file() and any(root == source_pack_root for root in self.pack_roots):
            return candidate
        return None

    @staticmethod
    def _load_matrix(path: Path | None) -> dict[str, dict[str, Any]]:
        if path is None:
            return {}
        try:
            payload = load_capability_ledger(path)
        except ValueError as exc:
            raise HostError(str(exc)) from exc
        if payload.get("schema_version") != 1 or not isinstance(payload.get("capabilities"), list):
            raise HostError("capability matrix requires schema_version 1 and a capabilities list")
        allowed = {"required", "optional", "unsupported", "retired"}
        result = {}
        for entry in payload["capabilities"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                raise HostError("capability matrix entries require an id")
            if entry.get("disposition") not in allowed:
                raise HostError(f"invalid capability disposition for {entry.get('id')!r}")
            if not entry.get("evidence_reason"):
                raise HostError(f"capability matrix entry {entry['id']!r} needs evidence_reason")
            if entry["id"] in result:
                raise HostError(f"duplicate capability matrix entry {entry['id']!r}")
            result[entry["id"]] = entry
        return result

    def discover(self) -> tuple[CapabilityRecord, ...]:
        # Folder/schema modules are runtime discovery dependencies.  Keeping
        # them behind the discovery operation prevents their timeline/project
        # compatibility imports from leaking into the host process boundary.
        from astrid.core.execution.executor.folder import (
            discover_folder_executor_roots,
            load_folder_executor,
        )
        from astrid.core.execution.executor.schema import ExecutorValidationError

        records: dict[str, CapabilityRecord] = {}
        quarantined_dirs, quarantined_ids = _quarantine_scope(self.pack_roots)
        for root in self.pack_roots:
            for executor_root in discover_folder_executor_roots(root):
                if _inside_quarantined(executor_root, quarantined_dirs):
                    # A pack whose manifest failed admission runs nothing; it is
                    # reported by quarantine, not registered as a capability.
                    continue
                try:
                    definition = load_folder_executor(executor_root)
                except (ExecutorValidationError, OSError, ValueError):
                    # A broken optional manifest is unavailable, but does not hide
                    # neighboring packs.  The manifest report records the reason.
                    continue
                definition = _attach_pack_metadata(definition, executor_root)
                manifest = next((executor_root / name for name in ("executor.yaml", "executor.yml", "executor.json") if (executor_root / name).is_file()), None)
                matrix_entry = self.matrix.get(definition.id, {})
                source_roots = _admitted_source_roots(executor_root, definition)
                record = CapabilityRecord(definition=definition, capability_digest=_capability_digest(definition.to_dict()), source_digest=_source_digest_for_roots(source_roots), source_root=executor_root, manifest_path=manifest, matrix=matrix_entry)
                records[record.id] = record
        # Use the SDK's exact static projection, preserving host-configured
        # source authority even when shared discovery also sees managed/env roots.
        from astrid.core.pack.discovery import discover_pack_metadata
        from astrid.sdk.actions import action_executor_definition

        discovered_packs = discover_pack_metadata(
            project_root=self.pack_roots[0] if self.pack_roots else Path.cwd(),
            extra_pack_roots=tuple(str(root) for root in self.pack_roots),
        )
        for discovered_pack in discovered_packs:
            pack = discovered_pack.pack
            pack_root = pack.root.resolve()
            if not any(pack_root == root or pack_root.is_relative_to(root) for root in self.pack_roots):
                continue
            for local_id, declaration in pack.actions.items():
                try:
                    definition = action_executor_definition(discovered_pack, local_id, declaration)
                    source_roots = _admitted_source_roots(pack_root, definition)
                    record = CapabilityRecord(
                        definition=definition,
                        capability_digest=_capability_digest(definition.to_dict()),
                        source_digest=_source_digest_for_roots(source_roots),
                        source_root=pack_root,
                        manifest_path=pack_root / "pack.yaml",
                        matrix=self.matrix.get(definition.id, {}),
                    )
                except (ExecutorValidationError, OSError, ValueError):
                    continue
                records[record.id] = record
        if self.matrix:
            discovered = set(records)
            expected = set(self.matrix)
            # A capability with no approved matrix row is kept visible but not
            # executable. The reason names the exact row to add; the rest of the
            # host keeps running. This replaces a whole-host HostError, so one
            # new pack cannot take down every capability.
            for capability_id in sorted(discovered - expected):
                records[capability_id] = CapabilityRecord(**{
                    **records[capability_id].__dict__,
                    "approval_reason": _unapproved_reason(capability_id, self.capability_matrix_path),
                })
            # A matrix row with no discovered executor can only remove
            # capabilities, never add one, so it is reported, not fatal.
            self.stale_matrix_entries = tuple(sorted(
                capability_id
                for capability_id in expected - discovered
                if not capability_id.startswith(_OPTIONAL_EXTERNAL_MATRIX_PREFIXES)
                and capability_id.split(".", 1)[0] not in quarantined_ids
            ))
            if self.stale_matrix_entries:
                _LOGGER.warning(
                    "capability matrix rows not discovered (ignored): %s",
                    ", ".join(self.stale_matrix_entries),
                )
        # Dependencies are digested after the full corpus is known so a graph
        # or dependency source change invalidates the next registration.
        records = {
            key: CapabilityRecord(**{**record.__dict__, "dependency_digest": _dependency_digest(record.definition, records)})
            for key, record in records.items()
        }
        self.capabilities = records
        root = self.pack_roots[0] if self.pack_roots else Path.cwd()
        self.source_epoch = _canonical_digest({
            "vcs_revision": _vcs_revision(root),
            "source_digest": _canonical_digest({key: record.source_digest for key, record in records.items()}),
            "source_inventory_identity": self.source_inventory_identity,
            "matrix_digest": _canonical_digest(self.matrix),
        })
        return tuple(records[key] for key in sorted(records))

    def admit(self, capability_kind: str, capability_id: str) -> tuple[Mapping[str, Any], Mapping[str, str]]:
        """Capture the exact definition and source epoch used by a task child."""
        if capability_kind == "executor":
            if not self.capabilities:
                # Admission of an invocation-selected extra pack is scoped to
                # that pack; the global capability matrix is for registration
                # and must not reject an otherwise valid isolated definition.
                matrix = self.matrix
                self.matrix = {}
                try:
                    self.discover()
                finally:
                    self.matrix = matrix
            record = self.capabilities.get(capability_id)
            if record is None:
                raise HostError(f"capability not discovered: {capability_id}")
            if record.approval_reason:
                raise HostError(f"capability {capability_id!r} is unavailable: {record.approval_reason}")
            return record.definition.to_dict(), {
                "capability_digest": record.capability_digest,
                "source_digest": record.source_digest,
                "dependency_digest": record.dependency_digest,
                "version": record.definition.version,
                "source_root": str(record.source_root),
                "source_roots": [str(root) for root in _admitted_source_roots(record.source_root, record.definition)],
                "python_path_roots": [
                    str(root) for root in _admitted_python_roots(record.source_root, record.definition)
                ],
            }
        if capability_kind == "orchestrator":
            from astrid.core.execution.orchestrator.folder import (
                discover_folder_orchestrator_roots,
                load_folder_orchestrator,
            )

            quarantined_dirs, _quarantined_ids = _quarantine_scope(self.pack_roots)
            for root in self.pack_roots:
                for orchestrator_root in discover_folder_orchestrator_roots(root):
                    if _inside_quarantined(orchestrator_root, quarantined_dirs):
                        continue
                    try:
                        definition = load_folder_orchestrator(orchestrator_root)
                    except (OSError, ValueError):
                        continue
                    if definition.id != capability_id:
                        continue
                    definition = _attach_pack_metadata(definition, orchestrator_root)
                    source_roots = _admitted_source_roots(orchestrator_root, definition)
                    source_digest = _source_digest_for_roots(source_roots)
                    capability_digest = _capability_digest(definition.to_dict())
                    return definition.to_dict(), {
                        "capability_digest": capability_digest,
                        "source_digest": source_digest,
                        "dependency_digest": _canonical_digest({
                            "child_executors": definition.child_executors,
                            "child_orchestrators": definition.child_orchestrators,
                        }),
                        "version": definition.version,
                        "source_root": str(orchestrator_root),
                        "source_roots": [str(root) for root in source_roots],
                        "python_path_roots": [
                            str(root) for root in _admitted_python_roots(orchestrator_root, definition)
                        ],
                    }
            raise HostError(f"capability not discovered: {capability_id}")
        raise HostError(f"unsupported capability kind {capability_kind!r}")

    def _registration_admission_reason(self) -> str | None:
        owner = self._handoff_accounting_owner
        return (owner._runtime_registration_fence or self._runtime_registration_fence
                or ("executor registration refresh is blocked" if
                    owner._registration_refresh_blocked or self._registration_refresh_blocked else None))

    def _remotion_idle_context(self) -> str | None:
        """Bind idle observation reuse, without walking the native installation.

        This is not a validation receipt or execution admission. The small
        launch-reference file and exact effective probe/registry context must
        still match the successful full observation. Tasks always validate
        all installed inputs again.
        """
        from astrid.core.rendering import remotion_runtime as runtime

        env = os.environ
        try:
            context = runtime.probe_context(environment=env, python_executable=sys.executable)
            context["registry_context"] = runtime._registry_context(env, generator=False)
            names = (runtime.REMOTION_PROJECT_DIR_ENV, runtime.NODE_EXECUTABLE_ENV,
                     runtime.TIMELINE_SCHEMA_PYTHONPATH_ENV, *runtime._REF_ENV_KEYS)
            configuration = {name: env[name] for name in names}
            if any(not value for value in configuration.values()):
                return None
            # Constant-size filesystem checks only: no node_modules/schema
            # tree enumeration, imports, subprocess probes, or registry writes.
            selected = {}
            for name in names[:3]:
                path = Path(configuration[name]).expanduser()
                if not path.is_absolute():
                    return None
                resolved = path.resolve(strict=True)
                facts = resolved.stat()
                selected[name] = (str(resolved), facts.st_dev, facts.st_ino,
                                  facts.st_mode, facts.st_uid)
            reference = Path(configuration[runtime.ASTRID_REMOTION_VALIDATION_PATH])
            before = reference.stat()
            if (not reference.is_absolute() or reference.is_symlink()
                    or not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_size > runtime._VALIDATION_MAX_BYTES):
                return None
            raw = reference.read_bytes()
            after = reference.stat()
            def identity(facts):
                return (facts.st_dev, facts.st_ino, facts.st_mode, facts.st_uid,
                        facts.st_size, facts.st_mtime_ns, facts.st_ctime_ns)
            if (identity(before) != identity(after)
                    or hashlib.sha256(raw).hexdigest() != configuration[runtime.ASTRID_REMOTION_VALIDATION_SHA256]):
                return None
            context.update(configuration=configuration, selected=selected,
                           reference_identity=identity(after),
                           interpreter=(sys.prefix, sys.exec_prefix, sys.base_prefix,
                                        sys.base_exec_prefix, sys.version,
                                        sys.implementation.cache_tag, sys.platform,
                                        getattr(sys, "abiflags", "")))
            return _canonical_digest(context)
        except (OSError, KeyError, ValueError, runtime.RemotionValidationError):
            return None

    def _forget_failed_remotion_observation(self, capability_id: str, context: str | None) -> None:
        owner = self._handoff_accounting_owner
        # Share only readiness invalidation, never attempt custody. A later
        # parallel dispatch must not restore a known-failed parent observation.
        with owner._handoff_condition:
            prior = owner._remotion_full_preflight.get(capability_id)
            if context is not None and prior is not None and prior[0] == context:
                owner._remotion_full_preflight.pop(capability_id, None)

    def preflight(self, capability_id: str | None = None) -> tuple[CapabilityRecord, ...]:
        """Full readiness validation for initial/explicit registration or work."""
        return self._preflight(capability_id, full_remotion=True)

    def _preflight(self, capability_id: str | None = None, *, full_remotion: bool) -> tuple[CapabilityRecord, ...]:
        selected = [self.capabilities[capability_id]] if capability_id else list(self.capabilities.values())
        updated: dict[str, CapabilityRecord] = dict(self.capabilities)
        remotion_context = self._remotion_idle_context() if any(record.adapter.requires_remotion for record in selected) else None
        for record in selected:
            checks: dict[str, Any] = {"source": record.source_root.is_dir(), "definition": True}
            adapter = record.adapter
            bindings = []
            missing, binary_errors = [], []
            for binary in _binary_requirements(record):
                try:
                    bindings.extend(self._executable_admission.require((binary,)))
                except ExecutableAdmissionError as exc:
                    missing.append(binary)
                    binary_errors.append(str(exc))
            checks["binaries"] = {"ok": not missing, "bindings": [b.evidence() for b in bindings]}
            if missing:
                checks["binaries"].update(missing=missing, reasons=binary_errors)
            required_env = _required_env_names(record)
            required_credentials = set(_required_secret_names(record))
            missing_env = [
                name for name in required_env
                if not (
                    _resolve_credential_value(
                        self.credential_source,
                        name,
                        explicit=self._credential_source_is_explicit,
                    )
                    if name in required_credentials
                    else self.credential_source.get(name)
                )
            ]
            checks["credentials"] = {"ok": not missing_env, "missing": missing_env}
            required_packages = record.matrix.get("required_packages") or record.definition.metadata.get("required_packages", adapter.required_packages)
            missing_packages = [package for package in (required_packages or ()) if importlib.util.find_spec(str(package)) is None]
            checks["packages"] = {"ok": not missing_packages, "missing": missing_packages}
            # Only the final Remotion compositor needs the JavaScript render
            # tree. Other rendering-pack executors are offline Python/FFmpeg
            # work; the matrix must opt into this requirement explicitly.
            if adapter.requires_remotion:
                if full_remotion:
                    owner = self._handoff_accounting_owner
                    with owner._handoff_condition:
                        self._remotion_full_preflight.pop(record.id, None)
                    try:
                        from astrid.core.rendering.remotion_runtime import remotion_runtime_status
                        status = remotion_runtime_status(require_explicit_project=True)
                    except BaseException:
                        self._forget_failed_remotion_observation(record.id, remotion_context)
                        raise
                    checks["remotion"] = {
                        "ok": status.available,
                        "project_dir": str(status.project_dir) if status.project_dir else None,
                        "node_executable": str(status.node_executable) if status.node_executable else None,
                        "node_version": status.node_version,
                        "remotion_cli": str(status.remotion_cli) if status.remotion_cli else None,
                        "reason": status.reason,
                    }
                    after_context = self._remotion_idle_context()
                    if status.available and remotion_context is not None and after_context == remotion_context:
                        with owner._handoff_condition:
                            self._remotion_full_preflight[record.id] = (remotion_context, dict(checks["remotion"]))
                    else:
                        self._forget_failed_remotion_observation(record.id, remotion_context)
                else:
                    owner = self._handoff_accounting_owner
                    with owner._handoff_condition:
                        prior = owner._remotion_full_preflight.get(record.id)
                    checks["remotion"] = (
                        dict(prior[1]) if remotion_context is not None and prior is not None
                        and prior[0] == remotion_context else {
                            "ok": False,
                            "reason": "full Remotion validation required for current host context",
                        }
                    )
            policy = _network_policy(record)
            if adapter.requires_network and not record.definition.isolation.network:
                checks["network"] = {"ok": False, "reason": "adapter_requires_network"}
            elif record.definition.isolation.network and adapter.family == "provider" and policy is None:
                # Provider capabilities must declare their concrete egress
                # contract.  A boolean ``network: true`` is not an admission
                # policy: without destinations/protocols/redirect handling we
                # cannot observe or constrain the child honestly.
                checks["network"] = {"ok": False, "reason": "provider network_policy is missing"}
            elif record.definition.isolation.network and adapter.family == "provider" and not _host_managed_broker(policy):
                # A Python hook is diagnostic, not an authority boundary: a
                # child can recover native socket capabilities. Provider
                # network work is therefore admissible only through the
                # host-owned broker.
                checks["network"] = {"ok": False, "reason": "provider network requires a host-managed broker"}
            elif record.definition.isolation.network and adapter.family == "provider" and not _tcp_broker_supports(policy):
                checks["network"] = {"ok": False, "reason": "host-managed provider broker does not support UDP/QUIC"}
            elif _native_network_command(record) and record.definition.isolation.network and not _enforceable_network_gateway(policy):
                checks["network"] = {"ok": False, "reason": "native network requires an enforceable observable proxy or broker"}
            else:
                checks["network"] = {"ok": True}
            if record.definition.isolation.network and adapter.family == "provider" and _host_managed_broker(policy):
                sandbox_available = sys.platform == "darwin" and shutil.which("sandbox-exec") is not None
                checks["sandbox"] = {
                    "ok": sandbox_available,
                    **({} if sandbox_available else {"reason": "host-managed provider broker requires an OS network sandbox"}),
                }
            pack_source = _hivemind_source_preflight(record)
            if pack_source is not None:
                checks["pack_source"] = pack_source
            if _is_withdrawn(record):
                checks["disposition"] = {
                    "ok": False,
                    "reason": _withdrawn_reason(record),
                }
            if record.approval_reason:
                checks["approval"] = {"ok": False, "reason": record.approval_reason}
            ready = all(value is True or (isinstance(value, dict) and value.get("ok") is True) for value in checks.values())
            updated[record.id] = CapabilityRecord(**{**record.__dict__, "preflight": checks, "ready": ready,
                                                   "executable_bindings": tuple(bindings)})
        self.capabilities = updated
        return tuple(updated[key] for key in sorted(updated) if capability_id is None or key == capability_id)

    def register(self, *, deliberate: bool = False) -> dict[str, Any]:
        # Cover discovery, preflight, the Runtime transaction, and local state
        # publication as one registration operation. In particular, a fenced
        # handoff cannot observe an old registration while refresh work is in
        # progress or briefly mistake an unpublished success for quiescence.
        self._handoff_begin_registration()
        try:
            return self._register_impl(deliberate=deliberate)
        finally:
            self._handoff_end_registration()

    def _register_impl(self, *, deliberate: bool = False, idle_renewal: bool = False) -> dict[str, Any]:
        if self._runtime_registration_fence and not deliberate:
            raise HostError(self._runtime_registration_fence)
        # A dead/incompatible Runtime must fail before expensive native work.
        runtime_state = self._runtime_compatibility()
        if self._registered_runtime_state and not deliberate:
            changed = [key for key in ("protocol", "schema_digest", "runtime_epoch",
                        "runtime_instance_id", "runtime_session_id", "coordinator_epoch")
                       if self._registered_runtime_state.get(key) != runtime_state.get(key)]
            if changed:
                self._runtime_registration_fence = (
                    "registration invalidated; " + "; ".join({
                        "protocol": "runtime protocol changed", "schema_digest": "runtime schema digest changed",
                        "runtime_epoch": "runtime epoch changed", "runtime_instance_id": "runtime instance changed",
                        "runtime_session_id": "runtime session changed", "coordinator_epoch": "runtime coordinator epoch changed",
                    }[key] for key in changed) + "; deliberate re-registration required"
                )
                raise HostError(self._runtime_registration_fence)
        if not self.capabilities:
            self.discover()
        _emit_startup_phase("preflight_begin")
        if idle_renewal and self._registered_state:
            self._preflight(full_remotion=False)
        else:
            self.preflight()
        _emit_startup_phase("preflight_done")
        state = {
            key: {
                "capability_digest": record.capability_digest,
                "source_digest": record.source_digest,
                "dependency_digest": record.dependency_digest,
            }
            for key, record in self.capabilities.items()
        }
        invalidations: list[str] = []
        removed = sorted(set(self._registered_state) - set(state))
        invalidations.extend(f"capability removed: {key}" for key in removed)
        for key in sorted(set(state) & set(self._registered_state)):
            for digest_name, label in (("capability_digest", "capability"), ("source_digest", "source"), ("dependency_digest", "dependency")):
                if self._registered_state[key].get(digest_name) != state[key][digest_name]:
                    invalidations.append(f"{label} digest changed: {key}")
        if self._registered_runtime_state:
            if self._registered_runtime_state.get("protocol") != runtime_state.get("protocol"):
                invalidations.append("runtime protocol changed")
            if self._registered_runtime_state.get("schema_digest") != runtime_state.get("schema_digest"):
                invalidations.append("runtime schema digest changed")
            if self._registered_runtime_state.get("runtime_epoch") != runtime_state.get("runtime_epoch"):
                invalidations.append("runtime epoch changed")
            if self._registered_runtime_state.get("source_epoch") != self.source_epoch:
                invalidations.append("source epoch changed")
        if invalidations and not deliberate:
            raise HostError("registration invalidated; " + "; ".join(invalidations) + "; deliberate re-registration required")
        verified_facts = _registration_verified_facts()
        if self.client is None:
            with self._handoff_condition:
                self._registered_digests = {key: record.capability_digest for key, record in self.capabilities.items()}
                self._registered_state = state
                self._registered_runtime_state = {**runtime_state, "source_epoch": self.source_epoch}
                self.runtime_state = runtime_state
                self._runtime_registration_fence = None
                self._registration_refresh_blocked = False
            return {"executor_id": self.executor_id, "capabilities": [r.manifest() for r in self.capabilities.values()], "ready": [r.id for r in self.capabilities.values() if r.ready and not _is_withdrawn(r)], "withdrawn_capabilities": removed}
        # Publish capability admission metadata before advertising the executor.
        # A real runtime must be able to validate a task against the exact
        # definition digest/source-derived readiness before it can claim work.
        register_capability = getattr(self.client, "register_capability", None)
        if not callable(register_capability):
            raise HostError("runtime client lacks canonical capability registration operation")
        executor_capabilities: list[dict[str, Any]] = []
        for record in self.capabilities.values():
            disposition = str(record.matrix.get("disposition", ""))
            if disposition in {"unsupported", "retired"}:
                # A declared disposition is authoritative even when the
                # source happens to fail another preflight check; preserve its
                # human-readable reason rather than an incidental local error.
                status = disposition
                unavailable_reason = str(record.matrix.get("evidence_reason") or disposition)
            else:
                status = "ready" if record.ready else "unavailable"
                unavailable_reason = None if record.ready else _preflight_unavailable_reason(record)
            executor_capabilities.append(
                {
                    "capability_id": record.id,
                    "definition_digest": record.capability_digest,
                    "status": status,
                    "required_resource_keys": list(record.resource_keys),
                    "estimated_scratch_bytes": record.estimated_scratch_bytes,
                    "estimated_output_bytes": record.estimated_output_bytes,
                    "unavailable_reason": unavailable_reason,
                    # Forward only the declaration bound into this definition
                    # digest; legacy executors remain summary-only.
                    **({"action_declaration": record.definition.metadata["action_declaration"]}
                       if "action_declaration" in record.definition.metadata else {}),
                }
            )
        all_keys = sorted({key for record in self.capabilities.values() for key in record.resource_keys})
        dependency_digests = {key: record.dependency_digest for key, record in self.capabilities.items()}
        registration_kwargs = {
            "capabilities": sorted(
                executor_capabilities,
                key=lambda item: str(item["capability_id"]),
            ),
            "max_concurrency": self.max_concurrency,
            "resource_keys": all_keys,
            "source_digest": _canonical_digest({key: record.source_digest for key, record in self.capabilities.items()}),
            "dependency_digest": _canonical_digest(dependency_digests),
            "source_epoch": self.source_epoch,
            "protocol_version": runtime_state.get("protocol", "workspace.v1"),
            "schema_digest": runtime_state.get("schema_digest"),
            "runtime_epoch": runtime_state.get("runtime_epoch"),
            "verified_facts": verified_facts,
        }
        try:
            # The runtime owns one durable transaction for the executor and
            # its complete capability descriptors.  Never pre-publish
            # capabilities through separate requests: a failed registration
            # must leave no half-visible executor surface.
            registration = self.client.register_executor(
                self.executor_id, **registration_kwargs
            )
            # Removed capabilities cannot be expressed by the current
            # executor-registration payload.  Withdraw them only after the
            # replacement executor state is committed, so failure is
            # fail-closed rather than exposing a partial new registration.
            if removed:
                self._withdraw_removed_capabilities(removed)
            # Keep the host registration counted until the exact state that
            # Runtime accepted is published under the same claim condition.
            with self._handoff_condition:
                self._registered_digests = {key: record.capability_digest for key, record in self.capabilities.items()}
                self._registered_state = state
                self._registered_runtime_state = {**runtime_state, "source_epoch": self.source_epoch}
                self.runtime_state = runtime_state
                self._runtime_registration_fence = None
                self._registration_refresh_blocked = False
                self._registration_refresh_deadline = time.monotonic() + _EXECUTOR_REFRESH_SECONDS
        except HostRegistrationError:
            raise
        except Exception as exc:
            raw_details = getattr(exc, "details", None)
            details = raw_details if isinstance(raw_details, Mapping) else None
            raw_message = getattr(exc, "message", None)
            message = (
                str(raw_message)
                if raw_message
                else (str(exc) if isinstance(exc, HostError) else "executor registration failed")
            )
            raise HostRegistrationError(
                message[:512],
                code=str(getattr(exc, "code", "registration_failed"))[:128],
                request_id=str(getattr(exc, "request_id", ""))[:128],
                status=int(getattr(exc, "status", 0) or 0),
                details=details,
            ) from exc
        return {"registration": registration, "capabilities": [r.manifest() for r in self.capabilities.values()], "withdrawn_capabilities": removed}

    def _renew_executor_registration(self) -> None:
        """Refresh runtime executor liveness without replaying a receipt."""
        self._handoff_begin_registration()
        try:
            renew = getattr(self.client, "renew_registration_session", None)
            if callable(renew):
                renew()
            self._register_impl(idle_renewal=True)
        except Exception:
            self._registration_refresh_blocked = True
            raise
        finally:
            self._handoff_end_registration()

    def _withdraw_removed_capabilities(self, capability_ids: list[str]) -> None:
        """Make removed capabilities unavailable after replacement commits."""
        for capability_id in capability_ids:
            prior = self._registered_state[capability_id]
            reason = "capability removed from source checkout"
            withdraw = getattr(self.client, "withdraw_capability", None)
            if not callable(withdraw):
                raise HostError(f"runtime cannot withdraw removed capability: {capability_id}")
            withdraw(
                capability_id,
                digest=prior.get("capability_digest", ""),
                reason=reason,
            )

    def refresh(self) -> tuple[CapabilityRecord, ...]:
        """Re-scan source and report digest changes; callers must register again."""
        old = self.capabilities
        self.discover()
        removed = set(old) - set(self.capabilities)
        changed = sorted(removed)
        for key, record in self.capabilities.items():
            if key in old and (
                old[key].capability_digest != record.capability_digest
                or old[key].source_digest != record.source_digest
                or old[key].dependency_digest != record.dependency_digest
            ):
                changed.append(key)
        return tuple(old[key] if key in removed else self.capabilities[key] for key in changed)

    def _runtime_compatibility(self) -> dict[str, Any]:
        """Read and validate the exact runtime protocol/schema/epoch."""
        from banodoco_workspace_client.contract_metadata import PROTOCOL

        expected_protocol = PROTOCOL
        if self.client is None:
            return {
                "protocol": expected_protocol,
                "schema_digest": None,
                "runtime_epoch": None,
            }
        expected_schema = getattr(self.client, "schema_digest", None)
        if not isinstance(expected_schema, str) or not expected_schema:
            raise HostError("runtime client lacks generated schema digest metadata")
        health_operation = getattr(self.client, "health", None)
        if not callable(health_operation):
            raise HostError("runtime client lacks canonical health operation")
        health: Any = health_operation()
        if health is None:
            raise HostError("runtime health returned no protocol document")
        value = dict(health) if isinstance(health, Mapping) else {
            "status": getattr(health, "status", None),
            "protocol": getattr(health, "protocol", None),
            "schema_digest": getattr(health, "schema_digest", None),
            "runtime_epoch": getattr(health, "runtime_epoch", None),
            "runtime_session_id": getattr(health, "runtime_session_id", None),
            "runtime_instance_id": getattr(health, "runtime_instance_id", None),
            "instance_id": getattr(health, "instance_id", None),
            "coordinator_epoch": getattr(health, "coordinator_epoch", None),
        }
        actual_protocol = str(value.get("protocol", ""))
        actual_schema = str(value.get("schema_digest", ""))
        mismatches = []
        actual_status = str(value.get("status", ""))
        if actual_status != "ok":
            mismatches.append(
                f"status expected=ok actual={actual_status or 'missing'}"
            )
        if actual_protocol != expected_protocol:
            mismatches.append(f"protocol expected={expected_protocol} actual={actual_protocol or 'missing'}")
        if expected_schema and actual_schema != expected_schema:
            mismatches.append(f"schema_digest expected={expected_schema} actual={actual_schema or 'missing'}")
        runtime_epoch = value.get("runtime_epoch")
        if isinstance(runtime_epoch, bool) or not isinstance(runtime_epoch, int) or runtime_epoch < 1:
            mismatches.append("runtime_epoch must be a positive integer")
        instance_id = value.get("instance_id")
        runtime_instance_id = value.get("runtime_instance_id") or instance_id
        coordinator_epoch = value.get("coordinator_epoch") or runtime_instance_id
        if (not isinstance(runtime_instance_id, str) or not runtime_instance_id
                or (instance_id is not None and instance_id != runtime_instance_id)
                or (value.get("coordinator_epoch") is not None
                    and value.get("coordinator_epoch") != runtime_instance_id)):
            mismatches.append("Runtime instance fields disagree")
        if mismatches:
            reason = "runtime compatibility blocked: " + "; ".join(mismatches)
            if self._registered_runtime_state:
                self._runtime_registration_fence = reason
            raise HostError(reason)
        return {
            "protocol": actual_protocol,
            "schema_digest": actual_schema,
            "runtime_epoch": runtime_epoch,
            "runtime_session_id": value.get("runtime_session_id"),
            "runtime_instance_id": runtime_instance_id,
            "coordinator_epoch": coordinator_epoch,
        }

    def _materialize_inputs(
        self,
        spec: Mapping[str, Any],
        attempt: Path,
        *,
        authorized_input_object_ids: list[str] | tuple[str, ...] | None = None,
        task_param_ports: tuple[str, ...] | list[str] | None = None,
        cas_param_ports: tuple[str, ...] | list[str] | None = None,
        optional_cas_param_ports: tuple[str, ...] | list[str] | None = None,
        storage_estimate: Mapping[str, int] | None = None,
        input_size_limits: Mapping[str, int] | None = None,
        storage_policy_version: str | None = None,
        continuation_id: str | None = None,
        file_input_names: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """Materialize digest inputs and managed registry objects in *attempt*.

        Runtime object ids/digests are the only authority for live media.  A
        registry supplied as an input is read only to discover those identities;
        every object is fetched through the runtime, hash-checked, and copied
        below ``attempt/managed-objects``.  Renderers receive a derived private
        registry whose ``file`` values point at those attempt-local copies.
        """
        # Runtime admission wraps the immutable capability input document in
        # the task envelope alongside input_object_ids/schema metadata. The
        # nested document is the one canonical managed-input shape shared by
        # task admission and this host.
        raw_authorized = authorized_input_object_ids
        if raw_authorized is None:
            raw_authorized = spec.get("input_object_ids", ())
        if not isinstance(raw_authorized, (list, tuple)):
            raise HostError("runtime task input_object_ids must be a list")
        authorized_digests: set[str] = set()
        for object_id in raw_authorized:
            if not isinstance(object_id, str):
                raise HostError("runtime task input_object_ids must contain strings")
            normalized = object_id.removeprefix("sha256:")
            if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
                raise HostError("runtime task input_object_ids must contain sha256 object IDs")
            if normalized in authorized_digests:
                raise HostError("runtime task input_object_ids must be unique")
            authorized_digests.add(normalized)

        def require_authorized(digest: str, name: str) -> str:
            normalized = str(digest).removeprefix("sha256:")
            if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
                raise HostError(f"invalid managed digest for {name}")
            if normalized not in authorized_digests:
                raise HostError(f"managed input {name!r} is not authorized by task input_object_ids")
            return normalized

        input_spec = _admitted_spec_envelope(spec)
        values = dict(input_spec.get("inputs", {})) if isinstance(input_spec.get("inputs", {}), Mapping) else {}
        params = input_spec.get("params")
        bounded_policy = None
        if storage_policy_version in {
            "astrid.cloud-i2i.z-image.v1",
            "astrid.cloud-edit.qwen-source.v1",
            "astrid.cloud-edit.unified.v1",
        }:
            from astrid.core.generation.storage_policy import (
                CLOUD_EDIT_STORAGE_POLICY,
                CLOUD_I2I_STORAGE_POLICY,
                CLOUD_UNIFIED_EDIT_STORAGE_POLICY,
                ImageStoragePolicyError,
            )

            if not isinstance(params, Mapping):
                raise HostError("bounded cloud image admission requires typed params")
            bounded_policy = (
                CLOUD_I2I_STORAGE_POLICY
                if storage_policy_version == CLOUD_I2I_STORAGE_POLICY.version
                else (
                    CLOUD_UNIFIED_EDIT_STORAGE_POLICY
                    if storage_policy_version == CLOUD_UNIFIED_EDIT_STORAGE_POLICY.version
                    else CLOUD_EDIT_STORAGE_POLICY
                )
            )
            try:
                bounded_policy.validate_admission_request(
                    model=params.get("model"),
                    mode=params.get("mode"),
                    execution=params.get("execution"),
                    params=params,
                )
            except ImageStoragePolicyError as exc:
                raise HostError(str(exc)) from exc
            if values:
                raise HostError(
                    "bounded cloud image admission does not accept legacy spec.inputs authority"
                )
            if input_spec.get("input_digests"):
                raise HostError(
                    "bounded cloud image admission does not accept legacy input_digests authority"
                )
        if task_param_ports is not None:
            if not isinstance(params, Mapping):
                raise HostError("HC-04 task spec params must be an object")
            declared = tuple(str(name) for name in task_param_ports)
            declared_set = set(declared)
            unknown = sorted(str(name) for name in params if str(name) not in declared_set)
            if unknown:
                raise HostError("HC-04 task spec contains undeclared parameter(s): " + ", ".join(unknown))
            for name in declared:
                if name not in params:
                    continue
                if name in values and values[name] != params[name]:
                    raise HostError(f"HC-04 task parameter conflicts with input binding: {name}")
                values[name] = params[name]
        for key in _HOST_OWNED_ENVELOPE_PORTS + ("family", "params", "output_policy"):
            if key in spec and not values.get(key):
                values[key] = spec[key]
            if key in input_spec and not values.get(key):
                values[key] = input_spec[key]
        if bounded_policy is None:
            # Validate caller-owned file values before any attempt writes or
            # Runtime fetches. Paths synthesized below are host-owned, not
            # caller file inputs; bounded image profiles retain their policy.
            def declared_file_digest(value: Any, name: str) -> str:
                try:
                    digest = managed_file_digest(value, name)
                except ValueError as exc:
                    raise HostError(str(exc)) from exc
                require_authorized(digest, name)
                return digest

            for name, value in list(values.items()):
                if name in file_input_names:
                    digest = declared_file_digest(value, str(name))
                    values[name] = {**dict(value), "digest": digest} if isinstance(value, Mapping) else {"digest": digest}
            # Legacy named digest descriptors are inserted below, after host
            # snapshot synthesis. Validate now without changing that precedence.
            input_digests = input_spec.get("input_digests", ())
            for item in input_digests if isinstance(input_digests, list) else ():
                if isinstance(item, Mapping) and item.get("name") and str(item["name"]) in file_input_names:
                    declared_file_digest(item, str(item["name"]))
        if cas_param_ports is not None:
            for name in tuple(str(value) for value in cas_param_ports):
                candidate = values.get(name)
                if candidate is None:
                    continue
                if not isinstance(candidate, Mapping) or not isinstance(candidate.get("digest"), str):
                    raise HostError(
                        f"HC-04 CAS parameter {name!r} must be an object containing a digest"
                    )

        # Runtime continuation admission stores the authoritative child result
        # list below spec.runtime_dependencies.  Materialize that bounded
        # envelope into the attempt before command expansion; callers cannot
        # supply a path or replace the runtime-resolved children.
        runtime_dependencies = input_spec.get("runtime_dependencies")
        if isinstance(runtime_dependencies, Mapping) and "resolved_children" in runtime_dependencies:
            resolved = runtime_dependencies.get("resolved_children")
            if isinstance(resolved, (str, bytes)) or not isinstance(resolved, Sequence):
                raise HostError("runtime continuation resolved_children must be an array")
            resolved_envelope: dict[str, Any] = {
                "family": input_spec.get("family", "stitch_finalization"),
                "continuation_id": continuation_id,
                "resolved_children": _json_safe(resolved),
                "edges": _json_safe(runtime_dependencies.get("edges", [])),
                "aggregation": _json_safe(runtime_dependencies.get("aggregation", {})),
            }
            if continuation_id is None:
                resolved_envelope.pop("continuation_id", None)
            resolved_path = Path(attempt).resolve() / "inputs" / "resolved-children.json"
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            resolved_path.write_text(json.dumps(resolved_envelope, sort_keys=True), encoding="utf-8")
            values["resolved_children"] = str(resolved_path)
        # Timeline visualization tasks carry their canonical registry inside
        # the immutable snapshot rather than as a separate input file. Expose
        # it to the same host-only materialization path used by render tasks.
        snapshot = input_spec.get("timeline_snapshot")
        if not isinstance(snapshot, Mapping):
            # Managed render admission carries the frozen snapshot inside the
            # immutable inputs document.  Materialize it here, under this
            # attempt, rather than allowing SDK code to write a project-side
            # snapshot.
            candidate = values.get("timeline_snapshot")
            snapshot = candidate if isinstance(candidate, Mapping) else None
        if not isinstance(snapshot, Mapping):
            # Timeline visualization keeps its frozen snapshot in the
            # host-owned authority context (filmstrip/input-only) rather than
            # exposing it as a public command input.  It still has to pass
            # through the exact same CAS materialization path, otherwise every
            # source is truthfully rendered as an unavailable placeholder even
            # when its admitted object exists in the runtime CAS.
            # ``authority_context`` lives on the outer task spec in the
            # runtime envelope (the immutable capability spec is nested
            # under ``spec``).  Older direct callers may still place it on
            # the nested input spec, so accept both locations.  Without the
            # outer lookup timeline visualizations lose their frozen registry
            # before materialization and every source preview degrades to a
            # placeholder even when its CAS object was admitted.
            authority_context = input_spec.get("authority_context")
            if not isinstance(authority_context, Mapping):
                candidate = spec.get("authority_context")
                authority_context = candidate if isinstance(candidate, Mapping) else None
            if isinstance(authority_context, Mapping):
                for key in ("filmstrip_snapshot", "input_snapshot", "capture_snapshot"):
                    candidate = authority_context.get(key)
                    if isinstance(candidate, Mapping):
                        snapshot = candidate
                        break
        # The snapshot is an admission envelope, not a renderer input port.
        # Once its derived files are created, do not forward the authoring
        # document as an undeclared child argument.
        values.pop("timeline_snapshot", None)
        if isinstance(snapshot, Mapping) and snapshot.get("compiled_audio") is not None:
            values["compiled_audio"] = snapshot["compiled_audio"]
        snapshot_registry = snapshot.get("registry") if isinstance(snapshot, Mapping) else None
        snapshot_config = snapshot.get("config") if isinstance(snapshot, Mapping) else None
        if isinstance(snapshot_config, Mapping) and "timeline" not in values:
            snapshot_timeline_path = Path(attempt).resolve() / "inputs" / "timeline.json"
            snapshot_timeline_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_timeline_path.write_text(
                json.dumps(snapshot_config, sort_keys=True), encoding="utf-8"
            )
            values["timeline"] = str(snapshot_timeline_path)
        if "assets_registry" not in values and isinstance(snapshot_registry, Mapping):
            snapshot_registry_path = Path(attempt).resolve() / "inputs" / "snapshot-assets.json"
            snapshot_registry_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_registry_path.write_text(json.dumps(snapshot_registry, sort_keys=True), encoding="utf-8")
            values["assets_registry"] = str(snapshot_registry_path)
        if "materialized_root" in values or "materialized_objects" in values:
            raise HostError("materialized media handoff is host-owned and cannot be caller supplied")
        input_digests = input_spec.get("input_digests", ())
        for item in input_digests if isinstance(input_digests, list) else ():
            if isinstance(item, Mapping) and item.get("name") and item.get("digest"):
                name = str(item["name"])
                if task_param_ports is not None and name not in declared_set:
                    raise HostError(
                        f"HC-04 input_digests contains undeclared input: {name}"
                    )
                values.setdefault(name, {"digest": str(item["digest"])})
        declared_cas_ports = (
            tuple(str(value) for value in cas_param_ports)
            if cas_param_ports is not None
            else ()
        )
        # Optional CAS ports are omitted from the ordered role sequence when
        # absent. Every present port still has to match input_object_ids in
        # order; this supports one manifest serving both i2v and flf safely.
        optional_cas = {str(value) for value in (optional_cas_param_ports or ())}
        # Preserve the direct helper contract used by the unified edit
        # profile when callers provide the storage policy without manifest
        # metadata (the mask is optional for source-only edit).
        if storage_policy_version == "astrid.cloud-edit.unified.v1":
            optional_cas.add("mask_ref")
        ordered_cas_ports = tuple(
            name
            for name in declared_cas_ports
            if name not in optional_cas or values.get(name) is not None
        )
        # Multi-source capabilities use the Runtime input-object sequence as
        # the role/order contract.  Do this before fetching bytes so a caller
        # cannot swap (for example) a driving video into the reference-image
        # port while both objects remain individually authorized.
        if ordered_cas_ports:
            if len(authorized_digests) != len(ordered_cas_ports):
                raise HostError(
                    "HC-04 ordered CAS inputs must match the capability's CAS port count"
                )
            ordered_authorized = tuple(
                str(object_id).removeprefix("sha256:")
                for object_id in raw_authorized
            )
            for index, name in enumerate(ordered_cas_ports):
                candidate = values.get(name)
                if not isinstance(candidate, Mapping) or not isinstance(candidate.get("digest"), str):
                    raise HostError(f"HC-04 ordered CAS parameter {name!r} is missing a digest")
                normalized = require_authorized(str(candidate["digest"]), name)
                if normalized != ordered_authorized[index]:
                    raise HostError(
                        f"HC-04 CAS parameter {name!r} does not match ordered input_object_ids[{index}]"
                    )
        for name in values:
            input_name = Path(str(name))
            if not str(name) or input_name.is_absolute() or ".." in input_name.parts:
                raise HostError(f"input name escapes the attempt directory: {name!r}")
        attempt = Path(attempt).resolve()
        managed_root = attempt / "managed-objects"
        managed_root.mkdir(parents=True, exist_ok=True)
        materialized_objects: dict[str, str] = {}
        fetched_objects: dict[tuple[str, str], Path] = {}
        scratch_limit = (
            int(storage_estimate["scratch_bytes"])
            if storage_estimate is not None
            else None
        )
        bounded_input_limits = dict(input_size_limits or {})

        def write_scratch(path: Path, payload: bytes, *, name: str) -> None:
            limit = bounded_input_limits.get(name)
            if limit is not None and len(payload) > limit:
                raise HostError(
                    f"managed input {name!r} is {len(payload)} bytes, "
                    f"exceeding bounded materialization limit {limit}"
                )
            if scratch_limit is not None:
                existing = _attempt_tree_bytes(attempt)
                if existing + len(payload) > scratch_limit:
                    raise HostError(
                        f"materializing {name!r} would exceed task scratch limit "
                        f"{scratch_limit} bytes"
                    )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            _assert_live_storage_envelope(
                storage_estimate,
                attempt,
                attempt / "outputs",
            )

        cas_names = set(ordered_cas_ports)

        def fetch_object(reference: str, digest: str, name: str, *, filename: str | None = None) -> Path:
            if self.client is None:
                raise HostError(f"runtime client is required to materialize managed input {name}")
            normalized = require_authorized(digest, name)
            cache_key = (str(reference), normalized)
            cached = fetched_objects.get(cache_key)
            if cached is not None:
                return cached
            data = self._client_operation("get_object")(normalized)
            if not isinstance(data, (bytes, bytearray)):
                data = getattr(data, "data", None)
            if not isinstance(data, (bytes, bytearray)):
                raise HostError(f"runtime object fetch returned no bytes for {name}")
            payload = bytes(data)
            if hashlib.sha256(payload).hexdigest() != normalized:
                raise HostError(f"input object hash mismatch for {name}")
            destination = managed_root / (
                filename
                if filename is not None
                else f"{len(list(managed_root.iterdir())):04d}-{hashlib.sha256(reference.encode()).hexdigest()[:16]}"
            )
            write_scratch(destination, payload, name=name)
            fetched_objects[cache_key] = destination
            return destination

        registry_input_names = {
            "assets_registry",
            "assets_registry_path",
            "assets",
            "hype_assets",
        }
        for name, value in list(values.items()):
            digest = (
                (value.get("digest") or value.get("object_id"))
                if isinstance(value, Mapping)
                else (
                    value
                    if (
                        name in file_input_names
                        and storage_policy_version not in {
                            "astrid.cloud-i2i.z-image.v1",
                            "astrid.cloud-edit.qwen-source.v1",
                            "astrid.cloud-edit.unified.v1",
                        }
                        and isinstance(value, str)
                        and len(value) == 64
                        and all(character in "0123456789abcdef" for character in value)
                    )
                    else None
                )
            )
            if digest:
                if self.client is None or not callable(getattr(self.client, "get_object", None)):
                    raise HostError(
                        f"runtime client is required to materialize digest input {name}"
                    )
                normalized = require_authorized(str(digest), str(name))
                if str(name) == "theme":
                    destination = fetch_object(
                        str(value.get("object_id") or "theme"),
                        str(digest),
                        "theme",
                        filename="theme.json",
                    )
                    values[name] = str(destination)
                    materialized_objects["theme"] = str(destination)
                    materialized_objects[str(digest).removeprefix("sha256:")] = str(destination)
                    continue
                input_name = Path("theme.json") if str(name) == "theme" else Path(str(name))
                # Preserve a managed file object's safe filename when the
                # capability consumes it as a normal file port.  The logical
                # port name (for example ``source_video``) is not necessarily
                # a usable media suffix; losing ``.mp4`` here makes downstream
                # Comfy loaders reject an otherwise valid managed object.
                if str(name) in file_input_names and isinstance(value, Mapping):
                    raw_filename = value.get("filename")
                    if (
                        isinstance(raw_filename, str)
                        and raw_filename
                        and Path(raw_filename).name == raw_filename
                        and not Path(raw_filename).is_absolute()
                        and ".." not in Path(raw_filename).parts
                    ):
                        input_name = Path(raw_filename)
                if str(name) in cas_names:
                    filename = value.get("filename") if isinstance(value, Mapping) else None
                    if not isinstance(filename, str) or not filename or Path(filename).name != filename:
                        raise HostError(
                            f"HC-04 CAS parameter {name!r} requires a safe filename"
                        )
                    if len(ordered_cas_ports) > 1:
                        # Preserve role identity even when two CAS objects
                        # arrive with the same user filename.
                        filename = f"{name}--{filename}"
                    input_name = Path(filename)
                input_root = (attempt / "inputs").resolve()
                path = (input_root / input_name).resolve()
                if not path.is_relative_to(input_root):
                    raise HostError(f"input name escapes the attempt directory: {name!r}")
                data = self.client.get_object(normalized)
                if not isinstance(data, (bytes, bytearray)):
                    data = getattr(data, "data", None)
                if not isinstance(data, (bytes, bytearray)) or hashlib.sha256(bytes(data)).hexdigest() != normalized:
                    raise HostError(f"input object hash mismatch for {name}")
                write_scratch(path, bytes(data), name=str(name))
                values[name] = str(path)

            if name not in registry_input_names:
                continue
            registry_path = Path(str(values[name])).expanduser()
            if not registry_path.is_absolute():
                registry_path = (attempt / registry_path).resolve()
            try:
                registry = json.loads(registry_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise HostError(f"managed asset registry cannot be read for {name}: {exc}") from exc
            if not isinstance(registry, Mapping) or not isinstance(registry.get("assets"), Mapping):
                raise HostError(f"managed asset registry for {name} is invalid")
            derived = dict(registry)
            derived_assets: dict[str, Any] = {}
            for key, raw in registry["assets"].items():
                if not isinstance(raw, Mapping):
                    raise HostError(f"managed asset {key!r} is invalid")
                forbidden = {"url", "uri", "path", "source_path", "locator", "realm", "file"}
                if forbidden.intersection(raw):
                    raise HostError(f"managed asset {key!r} contains an unmanaged locator")
                object_id = raw.get("object_id") or raw.get("media_id")
                raw_digest = raw.get("digest") or raw.get("content_sha256") or raw.get("sha256") or raw.get("hash")
                if not isinstance(object_id, str) or not object_id.strip() or not isinstance(raw_digest, str):
                    raise HostError(f"managed asset {key!r} requires object_id and digest")
                digest_hex = raw_digest.removeprefix("sha256:")
                destination = fetch_object(object_id, digest_hex, str(key))
                materialized_objects[object_id] = str(destination)
                materialized_objects[digest_hex] = str(destination)
                entry = dict(raw)
                entry["file"] = str(destination)
                derived_assets[str(key)] = entry
            derived["assets"] = derived_assets
            derived_path = attempt / "inputs" / f"{Path(str(name)).name}.materialized.json"
            derived_path.parent.mkdir(parents=True, exist_ok=True)
            derived_path.write_text(json.dumps(derived, sort_keys=True), encoding="utf-8")
            values[name] = str(derived_path)
        if materialized_objects:
            values["materialized_root"] = str(managed_root)
            values["materialized_objects"] = materialized_objects
        return values

    def _start_network_broker(
        self,
        record: CapabilityRecord,
        attempt: Path,
        admission: Mapping[str, Any],
        inputs: Mapping[str, Any] | None = None,
        *,
        private_runtime_context: bool = False,
    ) -> _NetworkBrokerContext | None:
        """Start a strict host-owned broker when the manifest requests one.

        A broker descriptor is deliberately declarative: the child never gets
        to choose its endpoint or admission.  The host creates the loopback
        listener, binds the exact admission/route set, and supplies its
        endpoint only through the child environment.
        """
        policy = _network_policy(record)
        descriptor = policy.get("broker") if isinstance(policy, Mapping) else None
        if not isinstance(descriptor, Mapping):
            return None
        if not bool(descriptor.get("host_managed", descriptor.get("managed", True))):
            return None
        from astrid.core.execution.network_broker import ObservableNetworkBroker

        evidence_key = secrets_module.token_hex(32)
        auth_token = secrets_module.token_urlsafe(32)
        evidence_path = attempt / "broker-evidence.json"
        routes = _provider_routes(record, inputs)
        # Bind the concrete dynamic destinations into the signed admission so
        # route evidence cannot be replayed under a different URL input.
        admission["allowed_routes"] = list(routes)
        # D18 keeps the complete admission and signed observations in host
        # memory until the action and its contained descendants have ended.
        if private_runtime_context:
            evidence_path.unlink(missing_ok=True)
            (attempt / "network-evidence.json").unlink(missing_ok=True)
        broker = ObservableNetworkBroker(response_body=None, defer_evidence=private_runtime_context)
        if record.id == "generation.generate_image_codex":
            broker.tunnel_idle_seconds = min(600, max(15, int((inputs or {}).get("timeout") or 600)))
        broker.register_admission(
            admission,
            allowed_routes=[str(route) for route in (routes or ())],
            evidence_path=evidence_path,
            evidence_key=evidence_key,
            auth_token=auth_token,
        ).start()
        effective = dict(policy)
        # The loopback endpoint is the only network destination the child
        # needs to reach; the broker itself enforces the upstream route set.
        effective["proxy"] = broker.endpoint
        # The child is allowed to connect only to the host-owned loopback
        # broker.  The upstream route set belongs to the broker, not to the
        # provider process: retaining it here would let a Python provider
        # connect directly to an allowlisted upstream and still pass the
        # application socket hook.  The broker separately enforces
        # ``routes`` and signs the observed route evidence.
        effective["allowed_destinations"] = [broker.endpoint]
        effective["broker"] = {**dict(descriptor), "evidence_path": str(evidence_path)}
        return _NetworkBrokerContext(broker=broker, policy=effective, evidence_key=evidence_key, auth_token=auth_token)

    def request_provider_route_grant(
        self,
        task: Mapping[str, Any],
        *,
        ttl_seconds: int = 60,
    ) -> str:
        """Request one short-lived, task-bound provider egress grant.

        This is the explicit actor-facing step.  ``run_task`` consumes the
        returned opaque handle exactly once immediately before broker launch.
        """
        task_data = task.get("task", task)
        task_id = str(task_data.get("id") or task_data.get("task_id") or "")
        capability_id = str(task_data.get("capability") or task_data.get("capability_id") or "")
        if not task_id or not capability_id:
            raise ProviderRouteGrantError("provider route grant requires task and capability identity")
        record = self.capabilities.get(capability_id)
        if record is None:
            self.discover()
            record = self.capabilities.get(capability_id)
        if record is None:
            raise ProviderRouteGrantError(f"capability not discovered: {capability_id}")
        self.preflight(capability_id)
        record = self.capabilities[capability_id]
        policy = _network_policy(record)
        if not record.ready or record.adapter.family != "provider" or not record.definition.isolation.network:
            raise ProviderRouteGrantError("provider route grant requires a ready network provider")
        if not _host_managed_broker(policy) or not _tcp_broker_supports(policy):
            raise ProviderRouteGrantError("provider route grant requires a supported host-managed TCP broker")
        inputs_spec = _admitted_task_spec(task_data)
        inputs = inputs_spec.get("inputs") if isinstance(inputs_spec.get("inputs"), Mapping) else {}
        routes = _provider_routes(record, inputs)
        descriptor = (policy or {}).get("broker", {})
        binding = ProviderRouteGrantAuthority.binding(
            capability_id=record.id,
            capability_digest=record.capability_digest,
            broker=descriptor,
        )
        token = self._provider_grants.issue(
            task_id=task_id,
            capability_id=record.id,
            capability_digest=record.capability_digest,
            routes=routes,
            broker_binding=binding,
            ttl_seconds=ttl_seconds,
        )
        self._pending_provider_grants[task_id] = token
        return token

    def _typed_outputs(
        self,
        record: CapabilityRecord,
        descriptors: Sequence[Mapping[str, Any]],
        attempt: Path,
        generation_intent: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Bind validated harvest descriptors to their declared output ports."""
        outputs: list[dict[str, Any]] = []
        by_name = {output.name: output for output in record.definition.outputs}
        generation_port, declarations = _generation_selector_declarations(
            record, generation_intent
        )
        matched_declarations: set[tuple[str, str, int, str]] = set()
        seen_identities: set[tuple[str, int]] = set()
        seen_paths: dict[Path, int] = {}
        for index, harvested in enumerate(descriptors):
            if not isinstance(harvested, Mapping):
                raise HostError(f"harvested output {index} is not a descriptor")
            name = harvested.get("name")
            if not isinstance(name, str) or name not in by_name:
                raise HostError(
                    f"harvested output {index} names undeclared port {name!r}"
                )
            role = harvested.get("role", "result")
            if role not in {"result", "auxiliary"}:
                raise HostError(f"harvested output {name!r} has invalid role {role!r}")
            is_generation_result = (
                generation_port is not None
                and name == generation_port
                and role == "result"
            )
            # ``harvest_staged_outputs`` always supplies a positional ordinal
            # when the manifest omits one.  That fallback is valid for the
            # universal generic manifest contract, but it is not an identity
            # for an admitted generation selector.  Require the provenance
            # marker so generation publication can never bind by list order.
            if is_generation_result and harvested.get("ordinal_explicit") is not True:
                raise HostError(
                    f"generation output {name!r} must declare its original ordinal"
                )
            ordinal = harvested.get("ordinal", index)
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
                raise HostError(
                    f"harvested output {name!r} has invalid ordinal {ordinal!r}"
                )
            identity = (name, ordinal)
            if identity in seen_identities and not is_generation_result:
                raise HostError(
                    f"harvested output {name!r} repeats ordinal {ordinal}"
                )
            raw_path = harvested.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                raise HostError(f"harvested output {name!r} has no concrete path")
            path = Path(raw_path).resolve()
            if not path.is_file() or not path.is_relative_to(attempt):
                raise HostError(f"declared output {name!r} is not inside the attempt directory")
            output = by_name.get(name)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            declared_hash = harvested.get("content_hash")
            normalized_hash = (
                declared_hash.removeprefix("sha256:")
                if isinstance(declared_hash, str)
                else None
            )
            if normalized_hash != digest:
                raise HostError(f"harvested output {name!r} content hash does not match")
            size = path.stat().st_size
            if harvested.get("bytes") != size:
                raise HostError(f"harvested output {name!r} byte count does not match")
            # Settlement output rows intentionally omit host-local paths, but
            # managed visualization packs need a stable relative name to be
            # reconstructed after this ephemeral attempt is cleaned up. Keep
            # that name in the runtime's existing filename field rather than
            # widening the settlement schema with a new path property.
            output_root = (attempt / "outputs").resolve()
            try:
                relative_filename = path.relative_to(output_root).as_posix()
            except ValueError:
                relative_filename = path.name
            output = {
                "name": name,
                "ordinal": ordinal,
                "artifact_type": getattr(output, "artifact_type", None),
                "digest": f"sha256:{digest}",
                "size": size,
                "path": str(path),
                "filename": relative_filename,
                "role": role,
                "is_primary": bool(harvested.get("is_primary", False)),
                **{
                    field: harvested[field]
                    for field in (
                        "producer", "provenance", "durability", "regeneration", "coverage",
                        "media_type",
                    )
                    if field in harvested
                },
            }

            existing_index = seen_paths.get(path)
            if existing_index is not None:
                existing = outputs[existing_index]
                existing_priority = (
                    bool(existing.get("is_primary")),
                    existing.get("role") == "result",
                )
                current_priority = (
                    bool(output.get("is_primary")),
                    output.get("role") == "result",
                )
                if current_priority == existing_priority:
                    raise HostError(f"harvested outputs repeat concrete path {path}")
                if current_priority < existing_priority:
                    # A manifest directory inventory and an explicit
                    # manifest_path may name the same concrete file. Publish
                    # one managed object, retaining the result/primary alias.
                    continue
                seen_identities.discard(
                    (str(existing["name"]), int(existing["ordinal"]))
                )
                if all(field in existing for field in ("group_key", "variant_key", "ordinal")):
                    matched_declarations = {
                        declaration
                        for declaration in matched_declarations
                        if not (
                            declaration[0] == existing["group_key"]
                            and declaration[2] == existing["ordinal"]
                            and declaration[3] == existing["variant_key"]
                        )
                    }
                outputs[existing_index] = output
            else:
                seen_paths[path] = len(outputs)
            seen_identities.add(identity)

            if is_generation_result:
                if getattr(record.definition, "id", None) == "rendering.render":
                    duration = harvested.get("duration_seconds")
                    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                            or not math.isfinite(duration) or duration <= 0):
                        raise HostError(
                            "final video requires a finite positive intrinsic duration_seconds"
                        )
                    output["metadata"] = {"duration_seconds": duration}
                ordinal_matches = [
                    declaration
                    for declaration in declarations
                    if declaration["ordinal"] == ordinal
                ]
                explicit_fields = (
                    "output_port", "group_key", "variant_key", "selector"
                )
                for field in explicit_fields:
                    if field not in harvested:
                        continue
                    supplied = harvested[field]
                    if field == "selector":
                        if not isinstance(supplied, Mapping) or set(supplied) != {"group_key", "variant_key"}:
                            raise HostError(
                                f"generation output {name!r} selector metadata must be an object"
                            )
                        ordinal_matches = [
                            declaration
                            for declaration in ordinal_matches
                            if declaration["group_key"] == supplied["group_key"]
                            and declaration["variant_key"] == supplied["variant_key"]
                        ]
                    else:
                        ordinal_matches = [
                            declaration
                            for declaration in ordinal_matches
                            if declaration[field] == supplied
                        ]
                if len(ordinal_matches) != 1:
                    reason = "ambiguous" if len(ordinal_matches) > 1 else "mismatch"
                    raise HostError(
                        f"generation output {name!r} ordinal {ordinal} has an unauthorized {reason} binding"
                    )
                declaration = ordinal_matches[0]
                for field in explicit_fields:
                    if field in harvested and field != "selector" and harvested[field] != declaration[field]:
                        raise HostError(
                            f"generation output {name!r} metadata {field!r} disagrees with admitted intent"
                        )
                binding_key = (
                    declaration["group_key"], declaration["selector"],
                    declaration["ordinal"], declaration["variant_key"],
                )
                if binding_key in matched_declarations:
                    raise HostError(
                        f"generation output repeats admitted selector {binding_key!r}"
                    )
                matched_declarations.add(binding_key)
                # Runtime validates these values again against the predeclared
                # effect and derives all generation/variant IDs.
                output["output_port"] = declaration["output_port"]
                output["group_key"] = declaration["group_key"]
                output["variant_key"] = declaration["variant_key"]
                output["selector"] = {
                    "group_key": declaration["group_key"],
                    "variant_key": declaration["variant_key"],
                }
            if existing_index is None:
                outputs.append(output)

        if declarations:
            missing = [
                declaration for declaration in declarations
                if (
                    declaration["group_key"], declaration["selector"],
                    declaration["ordinal"], declaration["variant_key"],
                ) not in matched_declarations
            ]
            policy = generation_intent.get("partial_success_policy")
            required_missing = [
                declaration for declaration in missing
                if declaration.get("required", policy == "reject")
            ]
            if required_missing:
                message = (
                    "generation.publish_v1 reject policy is missing declared selectors"
                    if policy == "reject"
                    else "generation.publish_v1 is missing required declared selectors"
                )
                raise HostError(
                    message
                )
            if not matched_declarations:
                raise HostError(
                    "generation.publish_v1 produced no successful declared outputs"
                )
        return outputs

    def _generation_thumbnail_outputs(
        self,
        outputs: Sequence[Mapping[str, Any]],
        *,
        attempt_root: Path,
        task_data: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Derive Runtime-owned thumbnails for visual generation outputs.

        Thumbnails are host-side auxiliary outputs.  They are deliberately
        appended after ``_typed_outputs`` so the creative output ordinals,
        selectors, and primary flags remain untouched.  Runtime validates the
        source digest and attaches the thumbnail to the corresponding
        generation during settlement.
        """
        expected_effect = task_data.get("expected_effect")
        if not isinstance(expected_effect, Mapping) or expected_effect.get("effect_type") not in {
            "generation.publish_v1",
            "generation.create_with_variant",
            "generation.variant.append",
        }:
            return [], []

        output_root = (attempt_root / "outputs").resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        occupied_filenames = {
            Path(str(output.get("filename") or "")).name
            for output in outputs
            if output.get("filename")
        }
        thumbnails: list[dict[str, Any]] = []
        diagnostics: list[dict[str, str]] = []
        thumbnails_by_digest: dict[str, dict[str, Any]] = {}
        creative_digests = {
            output.get("digest")
            for output in outputs
            if output.get("role", "result") == "result"
            and isinstance(output.get("digest"), str)
        }
        storage_estimate = _task_storage_estimate(task_data)
        thumbnail_index = 0
        for output in outputs:
            if output.get("role", "result") != "result":
                continue
            media_type = _settlement_media_type(output)
            if not is_visual_media_type(media_type):
                continue
            source_object_id = output.get("digest")
            source_path = output.get("path")
            if (
                not isinstance(source_object_id, str)
                or not source_object_id.startswith("sha256:")
                or len(source_object_id) != 71
                or not isinstance(source_path, str)
            ):
                continue

            while True:
                filename = f"thumbnail-{thumbnail_index:04d}.jpg"
                thumbnail_index += 1
                if filename not in occupied_filenames and not (output_root / filename).exists():
                    occupied_filenames.add(filename)
                    break
            destination = output_root / filename
            try:
                result = extract_thumbnail(
                    Path(source_path),
                    destination,
                    media_type,
                )
            except (ThumbnailError, OSError, ValueError, SyntaxError) as exc:
                # A thumbnail is an enhancement.  A decoder or ffmpeg failure
                # must never discard the successfully harvested creative file.
                destination.unlink(missing_ok=True)
                if len(diagnostics) < 16:
                    diagnostics.append(
                        {
                            "code": "thumbnail_extraction_failed",
                            "message": str(exc)[:240],
                            "source_object_id": source_object_id,
                            "media_type": media_type[:120],
                            "action": "inspect the source media and thumbnail decoder",
                        }
                    )
                continue

            thumbnail_digest = "sha256:" + hashlib.sha256(destination.read_bytes()).hexdigest()
            # Runtime rejects two managed outputs with the same digest within
            # one settlement.  A thumbnail equal to its creative source adds
            # no information, so omit this optional auxiliary entirely.
            if thumbnail_digest in creative_digests:
                destination.unlink(missing_ok=True)
                continue
            existing = thumbnails_by_digest.get(thumbnail_digest)
            if existing is not None:
                source_object_ids = existing["provenance"]["thumbnail"]["source_object_ids"]
                if source_object_id not in source_object_ids:
                    source_object_ids.append(source_object_id)
                destination.unlink(missing_ok=True)
                continue

            # Thumbnail extraction is optional.  Do not turn a successful
            # creative publication into an output-budget failure just because
            # its enhancement would exceed the already-admitted envelope.
            if storage_estimate is not None:
                output_bytes = _storage_tree_bytes(output_root)
                if output_bytes > int(storage_estimate["output_bytes"]):
                    destination.unlink(missing_ok=True)
                    continue

            descriptor = {
                "name": "thumbnail",
                "output_port": "thumbnail",
                "path": str(result.path),
                "filename": result.path.name,
                "artifact_type": "image/jpeg",
                "media_type": "image/jpeg",
                "size": result.path.stat().st_size,
                "role": "thumbnail",
                "durability": "durable",
                "provenance": {
                    "thumbnail": {
                        "source_object_ids": [source_object_id],
                        "recipe_version": THUMBNAIL_RECIPE_VERSION,
                    }
                },
            }
            thumbnails.append(descriptor)
            thumbnails_by_digest[thumbnail_digest] = descriptor
        return thumbnails, diagnostics

    def _child_environment(
        self,
        record: CapabilityRecord,
        attempt: Path,
        *,
        explicit_env: Mapping[str, str] | None = None,
        authority_context: Mapping[str, Any] | None = None,
        admission: Mapping[str, Any] | None = None,
        network_broker: _NetworkBrokerContext | None = None,
        network_policy: Mapping[str, Any] | None = None,
        readiness_worker_kind: str = 'native',
        allow_runtime_connection: bool = True,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Build a redacted, manifest-scoped child environment.

        The second return value is the temporary secret map held by the host;
        callers clear it in their ``finally`` block.  No credential is placed
        in request JSON, evidence, or diagnostics.
        """
        declared = _required_secret_names(record)
        secrets = {
            name: value
            for name in declared
            if (
                value := _resolve_credential_value(
                    self.credential_source,
                    name,
                    explicit=self._credential_source_is_explicit,
                )
            )
        }
        explicit = dict(explicit_env or {})
        # A worker may deliberately execute a qualified, local VibeComfy
        # snapshot rather than the normal published dependency pin.  The
        # candidate is selected only by the digest-bound HC-03 profile; never
        # accept an ambient candidate variable from the pod environment.
        if record.id in {"vibecomfy.run", "vibecomfy.validate"}:
            # Canonical bundle loading imports VibeComfy custom-node modules
            # before the child reaches the live checkout server. Keep that
            # host-side preflight headless too; the actual Comfy daemon keeps
            # its normal route-registration environment in its own process.
            explicit["VIBECOMFY_HEADLESS"] = "1"
            profile = _read_readiness_profile_document()
            session = profile.get("vibecomfy_session") if isinstance(profile, Mapping) else None
            if isinstance(session, Mapping):
                explicit["ASTRID_VIBECOMFY_SESSION_OWNERSHIP_ATTESTED"] = "1"
                revision = session.get("source_revision")
                content_digest = session.get("source_content_digest")
                if isinstance(revision, str) and isinstance(content_digest, str):
                    explicit[VIBECOMFY_ATTESTED_REVISION_ENV] = revision
                    explicit[VIBECOMFY_ATTESTED_CONTENT_DIGEST_ENV] = content_digest
            if profile is not None:
                try:
                    model_root_binding = model_root_binding_from_profile(
                        profile, verify_files=False
                    )
                except ModelRootBindingError as exc:
                    raise HostError(f"VibeComfy model-root binding is invalid: {exc}") from exc
                explicit[MODEL_ROOT_ENV] = str(model_root_binding.path)
                explicit[MODEL_ROOT_BINDING_ENV] = json.dumps(
                    model_root_binding.as_dict(), sort_keys=True, separators=(",", ":")
                )
                receipt_dir = os.environ.get(MODEL_ROOT_QUALIFICATION_RECEIPT_DIR_ENV)
                if receipt_dir:
                    explicit[MODEL_ROOT_QUALIFICATION_RECEIPT_DIR_ENV] = receipt_dir
            candidate = profile.get("vibecomfy_candidate") if isinstance(profile, Mapping) else None
            if isinstance(candidate, Mapping) and candidate.get("kind") == "local_snapshot":
                revision = candidate.get("revision")
                content_digest = candidate.get("source_content_digest")
                if isinstance(revision, str) and isinstance(content_digest, str):
                    explicit["ASTRID_VIBECOMFY_CANDIDATE_KIND"] = "local_snapshot"
                    explicit["ASTRID_VIBECOMFY_CANDIDATE_REVISION"] = revision
                    explicit["ASTRID_VIBECOMFY_CANDIDATE_CONTENT_DIGEST"] = content_digest
            if self._vibecomfy_requested_warmth_hint:
                from astrid.core.generation.backends.vibecomfy import (
                    VIBECOMFY_WARMTH_HINT_ENV,
                )

                explicit[VIBECOMFY_WARMTH_HINT_ENV] = (
                    self._vibecomfy_requested_warmth_hint
                )
        explicit = _fixed_worker_environment(record, explicit, self.credential_source)
        # Runtime-backed pack executors (notably timeline visualization) read
        # workspace facts from their isolated child process.  The host already
        # holds the explicit endpoint/worker credential used for task
        # admission, so pass that connection across the same short-lived
        # environment boundary instead of asking the child to rediscover it.
        runtime_endpoint = getattr(self.client, "endpoint", None)
        runtime_credential = getattr(self.client, "credential", None)
        if allow_runtime_connection and runtime_endpoint and runtime_credential:
            explicit["BANODOCO_RUNTIME_ENDPOINT"] = str(runtime_endpoint)
            secrets["BANODOCO_RUNTIME_CREDENTIAL"] = str(runtime_credential)
            declared_secrets = (*declared, "BANODOCO_RUNTIME_CREDENTIAL")
        else:
            declared_secrets = declared
        if isinstance(authority_context, Mapping):
            authority_json = json.dumps(
                dict(authority_context), sort_keys=True, separators=(",", ":")
            )
            if len(authority_json) > 8192:
                authority_path = (attempt / "inputs" / "timeline-visualize-authority-context.json").resolve()
                authority_path.parent.mkdir(parents=True, exist_ok=True)
                authority_path.write_text(authority_json, encoding="utf-8")
                explicit["ASTRID_TIMELINE_VISUALIZE_AUTHORITY_CONTEXT"] = str(authority_path)
            else:
                explicit["ASTRID_TIMELINE_VISUALIZE_AUTHORITY_CONTEXT"] = authority_json
        env = build_child_subprocess_env(
            explicit_env=explicit,
            passthrough=record.definition.isolation.env_passthrough,
            declared_passthrough=record.definition.isolation.env_passthrough,
            secret_values=secrets,
            declared_secrets=declared_secrets,
        )
        if not allow_runtime_connection:
            # A D18 parent has only its inherited three-operation channel.
            # Also remove manifest passthrough aliases of the worker secret.
            for name in ("ASTRID_NETWORK_ADMISSION", "ASTRID_NETWORK_BROKER_EVIDENCE",
                         "ASTRID_NETWORK_ADMISSION_DIGEST", "ASTRID_NETWORK_NONCE",
                         "ASTRID_NETWORK_BROKER_TOKEN"):
                env.pop(name, None)
            for name, value in tuple(env.items()):
                if name in {"BANODOCO_RUNTIME_ENDPOINT", "BANODOCO_RUNTIME_CREDENTIAL"} or (
                    runtime_credential and value == str(runtime_credential)
                ):
                    env.pop(name, None)
        policy = network_broker.policy if network_broker is not None else (dict(network_policy) if network_policy is not None else _network_policy(record))
        if policy is not None:
            hook_root = attempt / ".astrid-network-hook"
            hook_root.mkdir(parents=True, exist_ok=True)
            (hook_root / "sitecustomize.py").write_text(
                _STRICT_NETWORK_HOOK_SOURCE if not allow_runtime_connection else
                "from astrid.core.execution.network_policy import install_from_environment\ninstall_from_environment()\n",
                encoding="utf-8",
            )
            evidence_path = attempt / "network-evidence.json"
            env["ASTRID_NETWORK_POLICY"] = json.dumps(policy, sort_keys=True, separators=(",", ":"))
            env["ASTRID_NETWORK_EVIDENCE"] = str(evidence_path)
            # Only the broker authentication token crosses the process
            # boundary.  The host/broker evidence signing key never does.
            if network_broker is not None:
                env["ASTRID_NETWORK_BROKER_TOKEN"] = network_broker.auth_token
            if not allow_runtime_connection:
                for name in ("ASTRID_NETWORK_ADMISSION", "ASTRID_NETWORK_BROKER_EVIDENCE",
                             "ASTRID_NETWORK_ADMISSION_DIGEST", "ASTRID_NETWORK_NONCE"):
                    env.pop(name, None)
                if network_broker is not None:
                    env["ASTRID_NETWORK_ADMISSION_DIGEST"] = network_broker.broker.expected_admission_digest
                    env["ASTRID_NETWORK_NONCE"] = network_broker.broker.expected_nonce
                if isinstance(policy.get("broker"), Mapping):
                    policy = {**dict(policy), "broker": {key: value for key, value in policy["broker"].items()
                                                        if key != "evidence_path"}}
                    env["ASTRID_NETWORK_POLICY"] = json.dumps(policy, sort_keys=True, separators=(",", ":"))
            else:
                env["ASTRID_NETWORK_ADMISSION"] = json.dumps(dict(admission or {}), sort_keys=True, separators=(",", ":"))
            env["PYTHONPATH"] = str(hook_root) + os.pathsep + env.get("PYTHONPATH", "")
            proxy = policy.get("proxy")
            if isinstance(proxy, str) and proxy:
                parsed_proxy = urlsplit(proxy)
                if parsed_proxy.username or parsed_proxy.password:
                    raise HostError("network policy proxy URL must not contain credentials")
                # Ambient proxy settings are not inherited; only an admitted
                # manifest proxy may be used by the child.
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                    env[key] = proxy
                no_proxy = policy.get("no_proxy")
                if no_proxy:
                    env["NO_PROXY"] = str(no_proxy)
                # Native provider wrappers consume an explicit, host-issued
                # name rather than trusting ambient proxy variables.
                env["ASTRID_BROKER_PROXY"] = proxy
            broker = policy.get("broker")
            if allow_runtime_connection and isinstance(broker, Mapping) and broker.get("evidence_path"):
                env["ASTRID_NETWORK_BROKER_EVIDENCE"] = str(broker["evidence_path"])
        from astrid.core.env_vars import ASTRID_REMOTION_VALIDATION_ROLE, ASTRID_REMOTION_PROJECT_DIR
        if ASTRID_REMOTION_VALIDATION_ROLE in env:
            env[ASTRID_REMOTION_VALIDATION_ROLE] = "worker"
            # Verify the launch-known import context without replacing the
            # actual command environment's host-installed network-policy hook.
            readiness_env = dict(env)
            readiness_path = env.get('PYTHONPATH', '')
            if policy is not None:
                installed_hook = str(hook_root)
                if readiness_path == installed_hook:
                    readiness_path = ''
                elif readiness_path.startswith(installed_hook + os.pathsep):
                    readiness_path = readiness_path[len(installed_hook) + 1:]
                else:
                    raise HostError('host network hook context differs before readiness validation')
            if readiness_worker_kind == 'command':
                readiness_env['PYTHONPATH'] = _command_worker_pythonpath(record, readiness_path)
            elif readiness_worker_kind == 'native':
                readiness_env[ASTRID_PACKS_PATH] = os.pathsep.join(str(root) for root in self.pack_roots)
                readiness_env['PYTHONPATH'] = os.pathsep.join(dict.fromkeys((
                    str(Path(__file__).resolve().parents[3]), *_dependency_pythonpath(),
                    *(str(root) for root in _admitted_python_roots(record.source_root, record.definition)))))
            else:
                raise HostError('unknown internal readiness worker projection')
            from astrid.core.rendering.remotion_runtime import _validated_runtime
            try:
                _validated_runtime(Path(env[ASTRID_REMOTION_PROJECT_DIR]), environment=readiness_env)
            except Exception as exc:
                secrets.clear()
                raise HostError("worker Remotion readiness context is unavailable") from exc
        return env, secrets

    @staticmethod
    def _scrub_secret_text(value: str, secrets: Mapping[str, str]) -> str:
        result = str(value)
        for secret in secrets.values():
            if secret:
                result = result.replace(secret, "<redacted>")
        return result

    @staticmethod
    def _network_evidence(attempt: Path, *, evidence_key: str | None = None, admission: Mapping[str, Any] | None = None, required: bool = False, broker_required: bool = False, broker_context: _NetworkBrokerContext | None = None) -> Mapping[str, Any] | None:
        del evidence_key  # Child evidence keys were retired; only host keys are trusted.
        if not broker_required:
            if required:
                raise HostError("network-required task has no host broker context")
            return None
        if broker_context is None:
            raise HostError("network-required task has no host broker context")
        if broker_required:
            # Re-materialize from broker-owned memory after child exit.  This
            # closes the path-overwrite attack: the child knows the path but
            # never knows the signing secret.
            finalize = getattr(broker_context.broker, "finalize_evidence", None)
            if callable(finalize):
                finalize()
            broker_path = getattr(broker_context.broker, "evidence_path", None)
            try:
                broker = json.loads(Path(broker_path).read_text(encoding="utf-8")) if broker_path else None
            except (OSError, ValueError):
                broker = None
            if not isinstance(broker, Mapping):
                raise HostError("network-required task produced no signed broker route evidence")
            broker_unsigned = {key: item for key, item in broker.items() if key not in {"signature", "signature_algorithm"}}
            broker_canonical = json.dumps(broker_unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            broker_signature = hmac.new(broker_context.evidence_key.encode(), broker_canonical, hashlib.sha256).hexdigest()
            if broker.get("signature_algorithm") != "hmac-sha256" or not hmac.compare_digest(str(broker.get("signature", "")), broker_signature):
                raise HostError("network-required task broker evidence signature is invalid")
            if dict(broker.get("admission") or {}) != dict(admission or {}):
                raise HostError("network-required task broker evidence admission binding is invalid")
            broker_events = broker.get("events")
            if not isinstance(broker_events, list):
                raise HostError("network-required task produced no signed broker route evidence")
            observed = {
                str(event.get("kind"))
                for event in broker_events
                if isinstance(event, Mapping) and str(event.get("detail", "")).endswith("|allowed=true")
            }
            required_events = {"handshake"}
            if broker_context.broker.allowed_routes:
                required_events.add("route")
            if not required_events.issubset(observed):
                raise HostError("network-required task produced incomplete broker route evidence")
            for event in broker_events:
                if not isinstance(event, Mapping) or event.get("kind") != "route" or not str(event.get("detail", "")).endswith("|allowed=true"):
                    continue
                target = str(event.get("detail", "")).rsplit("|allowed=", 1)[0]
                if not any(broker_context.broker._route_allowed(target) for _ in (0,)):
                    raise HostError("network-required task broker evidence contains an unregistered route")
            # Materialize a host-signed envelope from broker-owned events. The
            # child evidence path is deliberately ignored, so a child cannot
            # forge either the signature or the observations in settlement.
            value = {
                "schema_version": 1,
                "admission": dict(broker.get("admission") or {}),
                "events": [
                    {
                        "kind": "broker_handshake" if event.get("kind") == "handshake" else "broker_route",
                        "detail": event.get("detail", ""),
                        "allowed": str(event.get("detail", "")).endswith("|allowed=true"),
                    }
                    for event in broker_events
                    if isinstance(event, Mapping)
                ],
                "broker_evidence": broker,
            }
            unsigned = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            value["signature_algorithm"] = "hmac-sha256"
            value["signature"] = hmac.new(broker_context.evidence_key.encode(), unsigned, hashlib.sha256).hexdigest()
            (attempt / "network-evidence.json").write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
            return value

    def _upload_outputs(
        self,
        outputs: list[dict[str, Any]],
        *,
        project_id: str | None,
        run_id: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        lease_id: str | None = None,
        fence: int | None = None,
        runtime_epoch: int | None = None,
    ) -> list[dict[str, Any]]:
        """Publish staged outputs and return settlement-safe object refs."""
        upload_object = getattr(self.client, "upload_object", None)
        if not callable(upload_object):
            raise HostError(
                "runtime client must provide canonical upload_object for output publication"
            )
        uploaded: list[dict[str, Any]] = []
        seen_filenames: set[str] = set()
        inline = bool(getattr(self.client, "INLINE_SETTLEMENT_OUTPUTS", False))
        # Settlement JSON has a bounded inline budget, separate from the
        # Runtime CAS object limit. Publish larger outputs through the
        # attempt-bound object upload; settlement names their digests.
        sizes = []
        for descriptor in outputs:
            raw = descriptor.get("path")
            try:
                sizes.append(Path(str(raw)).stat().st_size if raw else 0)
            except OSError:
                sizes.append(0)
        for descriptor, size in zip(outputs, sizes):
            if size > RUNTIME_OBJECT_LIMIT_BYTES:
                raise HostError(
                    f"output {descriptor.get('name') or descriptor.get('path')!r} is {size / 2**20:.1f} MiB; the Runtime "
                    f"stores objects up to {RUNTIME_OBJECT_LIMIT_BYTES // 2**20} MiB. Produce less: for timelines "
                    "visualize use --resolution 480x270, a narrower --range, a coarser --every, or --view contact."
                )
        can_upload = all(value is not None for value in (run_id, task_id, attempt_id, lease_id, fence))
        inline_budget = INLINE_SETTLEMENT_BUDGET_BYTES
        if inline and not can_upload:
            encoded = sum(4 * ((size + 2) // 3) for size in sizes)
            if encoded > inline_budget:
                raise HostError(
                    f"outputs total {sum(sizes) / 2**20:.1f} MiB ({encoded / 2**20:.1f} MiB encoded), over the "
                    f"{inline_budget // 2**20} MiB inline settlement budget, and this attempt has no upload binding. "
                    "Produce less (timelines visualize: --resolution 480x270, a narrower --range, a coarser --every)."
                )
        for index, descriptor in enumerate(outputs):
            descriptor = dict(descriptor)
            raw_path = descriptor.pop("path", None)
            staged_filename = descriptor.pop("filename", None)
            if not raw_path:
                raise HostError("generated output is missing its staged path")
            path = Path(str(raw_path))
            if staged_filename is None:
                staged_filename = path.name
            filename = _runtime_output_filename(staged_filename)
            if filename in seen_filenames:
                raise HostError(
                    f"generated outputs collide on managed filename {filename!r}"
                )
            seen_filenames.add(filename)
            media_type = _settlement_media_type({**descriptor, "filename": filename})
            encoded_size = 4 * ((sizes[index] + 2) // 3)
            send_inline = inline and not (
                can_upload and (sizes[index] > INLINE_OUTPUT_MAX_BYTES or encoded_size > inline_budget)
            )
            if send_inline:
                inline_budget -= encoded_size
                data = path.read_bytes()
                descriptor["digest"] = "sha256:" + hashlib.sha256(data).hexdigest()
                descriptor["media_type"] = media_type
                descriptor["size"] = len(data)
                descriptor["kind"] = "object"
                descriptor["data_base64"] = base64.b64encode(data).decode("ascii")
                # The validated ordinal is Runtime-bound output identity,
                # even without generation selectors. Role/primary metadata
                # remains separate from the canonical object fields.
                uploaded_row = {
                    key: descriptor[key]
                    for key in (
                        "name",
                        "kind",
                        "filename",
                        "media_type",
                        "digest",
                        "size",
                        "data_base64",
                    )
                    if key in descriptor
                }
                generation_metadata = (
                    "output_port", "group_key", "variant_key", "selector"
                )
                for field in generation_metadata:
                    if field in descriptor:
                        uploaded_row[field] = descriptor[field]
                if "ordinal" in descriptor:
                    uploaded_row["ordinal"] = descriptor["ordinal"]
                uploaded_row.update(
                    {
                        field: descriptor[field]
                        for field in _SETTLEMENT_OUTPUT_METADATA_FIELDS
                        if field in descriptor
                    }
                )
                uploaded_row["filename"] = filename
                uploaded.append(uploaded_row)
                continue
            upload_kwargs = {
                "project_id": project_id,
                "media_type": media_type,
                "filename": filename,
            }
            if all(value is not None for value in (run_id, task_id, attempt_id, lease_id, fence)):
                upload_kwargs.update(
                    {
                        "run_id": run_id,
                        "task_id": task_id,
                        "attempt_id": attempt_id,
                        "lease_id": lease_id,
                        "fence": fence,
                        "output_key": str(descriptor.get("name") or ""),
                        "output_port": str(descriptor.get("output_port") or descriptor.get("name") or ""),
                    }
                )
                if runtime_epoch is not None:
                    upload_kwargs["runtime_epoch"] = runtime_epoch
            object_row = upload_object(path, **upload_kwargs)
            digest = getattr(object_row, "digest", None)
            if not digest:
                raise HostError("generated object upload returned no canonical digest")
            uploaded_row = {
                "name": descriptor.get("name"),
                "kind": "object",
                "filename": filename,
                "media_type": media_type,
                "digest": digest,
                "size": int(getattr(object_row, "size", descriptor.get("size", 0))),
                **{
                    field: descriptor[field]
                    for field in (
                        "output_port", "group_key", "variant_key", "selector",
                    )
                    if field in descriptor
                },
            }
            uploaded_row.update(
                {
                    field: descriptor[field]
                    for field in _SETTLEMENT_OUTPUT_METADATA_FIELDS
                    if field in descriptor
                }
            )
            if "ordinal" in descriptor:
                uploaded_row["ordinal"] = descriptor["ordinal"]
            uploaded.append(uploaded_row)
        return uploaded

    def _run_command_definition(self, record: CapabilityRecord, inputs: Mapping[str, Any], output_root: Path, attempt: Path, *, cancelled=None, authority_context: Mapping[str, Any] | None = None, admission: Mapping[str, Any] | None = None, network_broker: _NetworkBrokerContext | None = None, storage_estimate: Mapping[str, int] | None = None, child_bridge: Any | None = None, allow_runtime_connection: bool = True) -> Any:
        """Run a manifest command without importing Astrid's project authority.

        Built-in pipeline steps and command capabilities are both runnable from
        an attempt directory alone.
        """
        command = record.definition.command
        if command is None:
            raise HostError(f"capability {record.id!r} has no dispatchable command")
        attempt_output_root = (attempt / "outputs").resolve()
        if admission is not None:
            _verify_admitted_source(admission)
        # Checkout-server VibeComfy is intentionally bound to the manager's
        # HC-03 output root. Keep the task's generated spool inside that
        # attested root instead of handing the child an arbitrary attempt
        # directory, which the adapter must correctly reject.
        if record.id == "vibecomfy.run":
            profile = _read_readiness_profile_document()
            launch = profile.get("launch") if isinstance(profile, Mapping) else None
            profile_output_root = launch.get("output_root") if isinstance(launch, Mapping) else None
            if isinstance(profile_output_root, str) and profile_output_root:
                managed_root = Path(profile_output_root).expanduser().resolve()
                if managed_root.is_symlink() or not managed_root.is_absolute():
                    raise HostError("HC-03 VibeComfy output_root must be an absolute non-symlink path")
                task_label = str(inputs.get("task_identity") or attempt.name)
                safe_label = "".join(char if char.isalnum() or char in "-_." else "_" for char in task_label)
                output_root = managed_root / "astrid-tasks" / safe_label / "outputs"
                output_root.mkdir(parents=True, exist_ok=True)
        # Never interpret a previous command rejection as this attempt.
        (output_root / PROVIDER_FAILURE_FILE).unlink(missing_ok=True)
        values = {**inputs, "out": str(output_root), "run_root": str(attempt), "python_exec": sys.executable}
        for port in record.definition.inputs:
            if port.name not in values and port.default is not None:
                values[port.name] = port.default
        if record.definition.metadata.get("action_invocation"):
            from astrid.sdk.actions import validate_action_inputs_definition
            effective_inputs = {port.name: port.default for port in record.definition.inputs if port.default is not None}
            effective_inputs.update(inputs)
            validate_action_inputs_definition(record.definition, effective_inputs)
        values = _bind_host_owned_command_values(
            record,
            values,
            attempt=attempt,
            admission=admission if isinstance(admission, Mapping) else None,
        )
        # Serialize the final host-owned mapping after binding, because the
        # binder may derive materialized objects from managed registries.
        materialized_objects = values.get("materialized_objects")
        if isinstance(materialized_objects, Mapping):
            materialized_path = (attempt / "inputs" / "materialized-objects.json").resolve()
            materialized_path.parent.mkdir(parents=True, exist_ok=True)
            materialized_path.write_text(
                json.dumps(materialized_objects, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            values["materialized_objects"] = str(materialized_path)
        filmstrip_authority = values.get("filmstrip_authority")
        if isinstance(filmstrip_authority, str) and len(filmstrip_authority) > 8192:
            authority_path = (attempt / "inputs" / "filmstrip-authority.json").resolve()
            authority_path.parent.mkdir(parents=True, exist_ok=True)
            authority_path.write_text(filmstrip_authority, encoding="utf-8")
            values["filmstrip_authority"] = str(authority_path)
        compiled_audio = values.get("compiled_audio")
        if isinstance(compiled_audio, Mapping):
            compiled_path = (attempt / "inputs" / "compiled-audio.json").resolve()
            compiled_path.parent.mkdir(parents=True, exist_ok=True)
            compiled_path.write_text(json.dumps(compiled_audio, sort_keys=True), encoding="utf-8")
            values["compiled_audio"] = str(compiled_path)
        timeline_authority = values.get("timeline_authority")
        if isinstance(timeline_authority, Mapping) or (
            isinstance(timeline_authority, str) and len(timeline_authority) > 8192
        ):
            authority_path = (attempt / "inputs" / "timeline-authority.json").resolve()
            authority_path.parent.mkdir(parents=True, exist_ok=True)
            authority_path.write_text(
                json.dumps(timeline_authority, sort_keys=True, separators=(",", ":"))
                if isinstance(timeline_authority, Mapping)
                else timeline_authority,
                encoding="utf-8",
            )
            values["timeline_authority"] = str(authority_path)
        for output in record.definition.outputs:
            if output.name == "video" and "output_name" not in values:
                values["output_name"] = "hype.mp4"
        values, _ = _bind_host_owned_command_outputs(record, values, output_root=output_root)
        try:
            binding = expand_command(
                command,
                record.definition.inputs,
                values,
                record.definition.metadata,
            )
            assert_provided_inputs_bound(
                binding,
                record.definition.inputs,
                values,
                record.definition.metadata,
            )
        except BindingError as exc:
            raise HostError(f"capability {record.id!r}: {exc}") from exc
        argv = list(binding.argv)
        # Start from the canonical child environment: only safe process
        # variables and manifest-declared provider configuration cross the
        # boundary.  In particular, an unrelated ambient API key must never
        # leak into a provider subprocess.
        package_parent = str(Path(__file__).resolve().parents[3])
        env, secrets = self._child_environment(
            record,
            attempt,
            authority_context=authority_context,
            admission=admission,
            network_broker=network_broker,
            readiness_worker_kind='command',
            allow_runtime_connection=allow_runtime_connection and child_bridge is None,
            explicit_env={
                "PYTHONPATH": os.pathsep.join((package_parent, *_dependency_pythonpath())),
                ASTRID_INTERNAL_INVOCATION: "1",
                **binding.env,
                "ASTRID_PROGRESS_PATH": str(attempt / ".astrid-progress.json"),
            },
        )
        # Pack runtime modules reserve their direct module entry points for
        # the canonical runner.  GenericPackHost is that runner's process
        # boundary, so mark the child invocation just as executor_runner does.
        # An external pack's ``python -m`` command executes directly in this
        # host path (command manifests do not go through the registry runner).
        # Carry the pinned pack root and parent explicitly so a command can
        # import its own ``executors`` package without falling back to an
        # unrelated same-named checkout in ambient site-packages. The parent
        # remains available for packs whose source package uses the checkout
        # directory name as its import root.
        env['PYTHONPATH'] = _command_worker_pythonpath(record, env.get('PYTHONPATH', ''))
        cwd = _confined_cwd(
            binding.cwd,
            attempt=attempt,
            source_root=record.source_root,
            values=values,
        )
        broker_endpoint = network_broker.policy.get("proxy") if network_broker is not None else None
        inherited_fds = ()
        if child_bridge is not None:
            # Bootstrap in the action interpreter before pack code. Private
            # launch arguments contain a descriptor and command, no authority.
            if not argv or Path(argv[0]).resolve() != Path(sys.executable).resolve():
                raise HostError("delegating commands require the admitted Python interpreter")
            arguments = argv[1:]
            flags = []
            while arguments and arguments[0] in {"-u", "-B"}:
                flags.append(arguments[0])
                arguments = arguments[1:]
            if (not arguments or arguments[0].startswith("-") and arguments[0] not in {"-c", "-m"}
                    or arguments[0] in {"-c", "-m"} and len(arguments) < 2):
                raise HostError("unsupported delegating Python command flags")
            inherited_fds = (child_bridge.child_channel.read_fd, child_bridge.child_channel.write_fd)
            argv = [sys.executable, *flags, "-m", "astrid.core.execution.generic_host_worker",
                    "--child-command", json.dumps(inherited_fds), json.dumps(arguments)]
        remotion_lease = None
        remotion_bridge = None
        remotion_completed = False
        if record.id == "rendering.timeline_visualize":
            if child_bridge is not None:
                raise HostError("renderer executors cannot inherit host bridge sockets")
            if not argv or Path(argv[0]).resolve() != Path(sys.executable).resolve():
                raise HostError("renderer command requires the admitted Python interpreter")
            remotion_lease = self._renderer_task_lease(
                env, attempt, policy=self._required_confinement_policy(attempt))
        try:
            if remotion_lease is not None:
                argv = [sys.executable, "-m", "astrid.core.execution.generic_host_worker",
                        "--renderer-command-stdio", json.dumps(argv[1:])]
                startup = (
                    "import json,runpy,sys; sys.path[:0]=json.loads(sys.argv[1]); "
                    "sys.argv=sys.argv[2:]; runpy.run_module('astrid.core.execution.generic_host_worker',run_name='__main__')"
                )
                argv = [sys.executable, "-I", "-S", "-B", "-c", startup,
                        json.dumps(list(dict.fromkeys([package_parent, sysconfig.get_path('purelib'),
                            sysconfig.get_path('platlib'), *_dependency_pythonpath()]))), *argv[2:]]
            if not allow_runtime_connection and (network_broker is not None or _network_policy(record) is not None):
                argv = _network_startup_argv(argv, attempt, env, broker_required=network_broker is not None)
            elif (remotion_lease is None
                    and Path(argv[0]).resolve() == Path(sys.executable).resolve()):
                arguments = list(argv[1:])
                while arguments and arguments[0] in {"-B", "-u", "-I", "-S"}:
                    arguments.pop(0)
                if not arguments or arguments[0].startswith("-") and arguments[0] not in {"-c", "-m"}:
                    raise HostError("unsupported confined Python command")
                startup = (
                    "import json,os,runpy,sys; sys.path[:0]=json.loads(sys.argv[1]); "
                    "args=json.loads(sys.argv[2]); kind=args.pop(0); "
                    "os.environ.update({'ASTRID_INTERNAL_INVOCATION':'1'} if kind=='-m' else {}); "
                    "target=args.pop(0) if kind in ('-c','-m') else kind; "
                    "sys.argv=['-c' if kind=='-c' else target,*args]; "
                    "runpy.run_module(target,run_name='__main__',alter_sys=True) if kind=='-m' else "
                    "exec(compile(target,'<command>','exec'),{'__name__':'__main__'}) if kind=='-c' else "
                    "runpy.run_path(target,run_name='__main__')"
                )
                # Host-selected imports must stay within the supplied verified
                # bootstrap closure. Parent searches grant directory enumeration
                # only, never reads of sibling files or recursive roots.
                bootstrap = self._required_confinement_policy(attempt)
                scopes = tuple(root.resolve() for root in bootstrap.bootstrap_roots)
                searches = list(bootstrap.bootstrap_search_roots)
                imports = []
                for raw in (package_parent, sysconfig.get_path('purelib'), sysconfig.get_path('platlib'),
                            str(record.source_root), str(record.source_root.parent), *_dependency_pythonpath()):
                    candidate = Path(raw).resolve()
                    if any(candidate == root or candidate.is_relative_to(root) for root in scopes):
                        imports.append(str(candidate))
                    elif any(candidate == root.parent for root in scopes):
                        imports.append(str(candidate))
                        searches.append(candidate)
                self._confinement_policy = replace(bootstrap,
                    bootstrap_search_roots=tuple(dict.fromkeys(searches)))
                argv = [sys.executable, '-I', '-S', '-B', '-c', startup,
                        json.dumps(imports), json.dumps(arguments)]
            plan = self._workload_launch_plan(
                argv, attempt=attempt, cwd=cwd, environment=env,
                inherited_fds=inherited_fds, broker_endpoint=broker_endpoint, child_bridge=child_bridge,
            )
            process = popen_owned_group(list(plan.argv), **plan.subprocess_options(), text=True)
            self._track_process(process)
            if child_bridge is not None:
                try:
                    child_bridge.launched(process)
                except BaseException as error:
                    _terminate_process_group(process)
                    self._untrack_process(process)
                    _, diagnostic = process.communicate()
                    secret_values = {key: value for key, value in env.items()
                                     if key.upper().endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))}
                    raise HostError("child pipe bootstrap failed: " + self._scrub_secret_text(
                        (diagnostic or "")[-3500:], secret_values)) from error
        except BaseException:
            if remotion_lease is not None:
                remotion_lease.close()
            raise
        if remotion_lease is not None:
            from .remotion_pipe import HostPipeBridge
            try:
                remotion_bridge = HostPipeBridge(remotion_lease, process,
                    admission={"capability_id": record.id, "capability_digest": record.capability_digest,
                               "version": str(getattr(record.definition, "version", "")),
                               "source": dict(admission or {})},
                    launch_digest=hashlib.sha256(json.dumps([plan.argv, str(plan.cwd), plan.environment]).encode()).hexdigest())
            except BaseException as error:
                _terminate_process_group(process)
                self._untrack_process(process)
                try:
                    failed_bridge = getattr(error, 'remotion_bridge', None)
                    if failed_bridge is not None:
                        failed_bridge.finish(cancelled=True)
                finally:
                    remotion_lease.close()
                raise
        review_session = record.id == "editorial.human_review" and child_bridge is not None
        review_snapshot = None
        try:
            observe_tree(process)
            guards_due = 0.0
            while process.poll() is None:
                if remotion_bridge is not None:
                    remotion_bridge.check()
                observe_tree(process)
                if review_session:
                    review_snapshot = _observe_owned_review_session(process, attempt, review_snapshot)
                # Storage, evidence and Runtime-state checks walk the attempt
                # tree and call the Runtime; sample them at a bounded rate.
                if time.monotonic() >= guards_due:
                    guards_due = time.monotonic() + _LIVE_GUARD_INTERVAL_SECONDS
                    _assert_live_storage_envelope(storage_estimate, attempt, output_root)
                    if cancelled is not None and cancelled():
                        _terminate_process_group(process)
                        raise HostCancelled(f"capability {record.id!r} cancelled")
                time.sleep(0.05)
            if remotion_bridge is not None:
                stdout, stderr = remotion_bridge.finish(cancelled=process.returncode != 0)
                remotion_completed = process.returncode == 0
            else:
                stdout, stderr = process.communicate()
            if cancelled is not None and cancelled():
                raise HostCancelled(f"capability {record.id!r} cancelled")
            returncode = process.returncode
            process_id = process.pid
            if returncode != 0:
                if returncode > 0:
                    rejection_path = output_root / PROVIDER_FAILURE_FILE
                    if (not rejection_path.is_symlink() and rejection_path.is_file()
                            and rejection_path.stat().st_size <= 16384):
                        try:
                            rejection = provider_failure(json.loads(rejection_path.read_text()))
                        except (OSError, ValueError):
                            rejection = None
                        if rejection is not None:
                            raise ProviderFailure(self._scrub_secret_text(str(rejection), secrets),
                                                  provider_id=rejection.provider_id,
                                                  retryable=rejection.retryable,
                                                  retry_after_seconds=rejection.retry_after_seconds)
                detail = self._scrub_secret_text((stderr or stdout).strip(), secrets)
                raise HostError(f"capability {record.id!r} exited {returncode}: {detail}")
            if not isinstance(process_id, int) or process_id <= 0:
                raise HostError(f"capability {record.id!r} child process_id is missing")
            # The managed VibeComfy session must write inside its attested
            # HC-03 release root while running, but Astrid's settlement
            # contract owns an attempt-local output spool. Copy final regular
            # files (including the result manifest) back into that spool before
            # harvesting so typed-output custody remains inside the attempt.
            if output_root != attempt_output_root:
                attempt_output_root.mkdir(parents=True, exist_ok=True)
                for source in output_root.rglob("*"):
                    if source.is_symlink() or not source.is_file():
                        continue
                    relative = source.relative_to(output_root)
                    destination = attempt_output_root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                output_root = attempt_output_root
                values = {**values, "out": str(output_root)}
            try:
                outputs = harvest_staged_outputs(
                    output_root,
                    definition=record.definition,
                    declared_outputs=record.definition.outputs,
                    values=values,
                    require=False,
                )
            except HarvestError as exc:
                raise HostError(f"capability {record.id!r}: {exc}") from exc
            return SimpleNamespace(
                outputs=outputs,
                output_root=output_root,
                payload={
                    "returncode": returncode,
                    "capability_digest": record.capability_digest,
                    "process_id": process_id,
                },
                returncode=returncode,
                process_id=process_id,
            )
        except BaseException as exc:
            # The first census may fail before tree_members exists. Latch
            # before the finalizer can select a successful group-only fallback.
            if isinstance(exc, CleanupUncertainError) or hasattr(process, "_astrid_tree_uncertain"):
                self._latch_cleanup_uncertainty(getattr(process, "_astrid_tree_uncertain", None) or str(exc))
            raise
        finally:
            try:
                # Observe before terminating the original owner, never adopt a
                # post-exit PID. A force-killed action cannot run its finally.
                observation_error = None
                if review_session:
                    try:
                        review_snapshot = _observe_owned_review_session(process, attempt, review_snapshot)
                    except Exception as exc:
                        observation_error = exc
                _terminate_process_group(process)
                if observation_error is not None:
                    raise observation_error
                if review_session:
                    _retire_owned_review_session(attempt, review_snapshot)
            except BaseException as exc:
                self._latch_cleanup_uncertainty(str(exc))
                raise HostError(f"owned command cleanup uncertain: {exc}") from exc
            finally:
                self._untrack_process(process)
                if remotion_lease is not None:
                    try:
                        if remotion_bridge is not None:
                            remotion_bridge.finish(cancelled=not remotion_completed)
                    except BaseException as error:
                        if remotion_lease.owner.uncertain is not None:
                            self._latch_cleanup_uncertainty(str(error))
                        raise
                    finally:
                        remotion_lease.close()
                env.clear()
                secrets.clear()

    def invoke_capability(
        self,
        *,
        capability_kind: str,
        capability_id: str,
        request: Mapping[str, Any],
        attempt: str | Path,
        cancelled=None,
        definition: Mapping[str, Any] | None = None,
        admission: Mapping[str, Any] | None = None,
        child_env: Mapping[str, str] | None = None,
        require_network_startup: bool = False,
        network_broker_required: bool = False,
        storage_estimate: Mapping[str, int] | None = None,
        child_bridge: Any | None = None,
    ) -> Any:
        """Run one pack capability in a dedicated child process.

        The host is deliberately the only process that launches pack runtime
        code.  In particular, callers must not import an executor/orchestrator
        runner and then rely on ambient process state: that leaves mutable
        Python state, monkeypatches, and ledger authority in the caller.  The
        child receives a JSON request, marks itself as an internal invocation,
        and writes a small process-like result beside the request.
        """
        if capability_kind not in {"executor", "orchestrator"}:
            raise HostError(f"unsupported capability kind {capability_kind!r}")
        attempt_path = Path(attempt).expanduser().resolve()
        attempt_path.mkdir(parents=True, exist_ok=True)
        if definition is not None:
            command_data: Mapping[str, Any] | None = None
            if capability_kind == "executor":
                candidate = definition.get("command")
                command_data = candidate if isinstance(candidate, Mapping) else None
            else:
                runtime_data = definition.get("runtime")
                if isinstance(runtime_data, Mapping):
                    candidate = runtime_data.get("command")
                    command_data = candidate if isinstance(candidate, Mapping) else None
            if command_data is not None and command_data.get("cwd"):
                source_root = None
                if isinstance(admission, Mapping) and admission.get("source_root"):
                    source_root = Path(str(admission["source_root"]))
                _confined_cwd(
                    str(command_data["cwd"]),
                    attempt=attempt_path,
                    source_root=source_root,
                    values=request,
                )
        if admission is not None:
            _verify_admitted_source(admission)
        if definition is not None:
            _verify_action_admission(definition, admission or {})
        policy = self._required_confinement_policy(attempt_path)
        # Validate the required platform/backend and all private scopes before
        # writing protocol files or preparing interpreter-dependent startup.
        _confined_launch_plan([sys.executable], policy=policy)
        request_path = policy.private_write_root / ".astrid-capability-request.json"
        result_path = policy.private_write_root / ".astrid-capability-result.json"
        result_path.unlink(missing_ok=True)
        remotion_lease = None
        remotion_bridge = None
        remotion_completed = False
        payload = {
            "remotion_pipe": False,
            "capability_kind": capability_kind,
            "capability_id": capability_id,
            "request": _json_safe(request),
            "definition": _json_safe(definition) if definition is not None else None,
            "admission": _json_safe(admission) if admission is not None else None,
            "result_path": str(result_path),
        }
        env = dict(child_env or build_child_subprocess_env(
            explicit_env={ASTRID_INTERNAL_INVOCATION: "1"},
        ))
        env[ASTRID_INTERNAL_INVOCATION] = "1"
        # The attempt directory is the child cwd; the host worker itself uses
        # this checkout, while external source-pack imports use only the
        # explicitly admitted import parents validated against source roots.
        package_parent = str(Path(__file__).resolve().parents[3])
        import_roots: list[str] = []
        if isinstance(admission, Mapping):
            raw_import_roots = admission.get("python_path_roots")
            if isinstance(raw_import_roots, (list, tuple)):
                source_roots = tuple(
                    Path(str(value)).expanduser().resolve()
                    for value in admission.get("source_roots", ())
                    if value
                )
                for raw_root in raw_import_roots:
                    import_root = Path(str(raw_root)).expanduser().resolve()
                    if not import_root.is_dir() or (
                        source_roots
                        and not any(import_root.is_relative_to(source_root) for source_root in source_roots)
                    ):
                        raise HostError("admitted Python import root is outside the source fence")
                    import_roots.append(str(import_root))
        # Preserve the host-created sitecustomize hook when replacing the
        # worker import path; arbitrary child PYTHONPATH is still excluded.
        network_hook_roots = (str(attempt_path / ".astrid-network-hook"),) if env.get("ASTRID_NETWORK_POLICY") else ()
        env["PYTHONPATH"] = os.pathsep.join(
            dict.fromkeys((*network_hook_roots, package_parent, *_dependency_pythonpath(), *import_roots))
        )
        env[ASTRID_PACKS_PATH] = os.pathsep.join(str(root) for root in self.pack_roots)
        broker_endpoint = str((child_env or {}).get("ASTRID_BROKER_PROXY") or "")
        argv = [sys.executable, "-m", "astrid.core.execution.generic_host_worker", str(request_path)]
        inherited_fds = ()
        try:
            if capability_kind == "executor" and capability_id == "rendering.timeline_visualize":
                if child_bridge is not None:
                    raise HostError("renderer executors cannot inherit host bridge sockets")
                remotion_lease = self._renderer_task_lease(env, attempt_path, policy=policy)
                payload["remotion_pipe"] = True
            request_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            if child_bridge is not None:
                inherited_fds = (child_bridge.child_channel.read_fd, child_bridge.child_channel.write_fd)
                argv.extend(("--child-bridge", json.dumps(inherited_fds)))
            if require_network_startup:
                argv = _network_startup_argv(argv, attempt_path, env, broker_required=network_broker_required)
            else:
                # The immutable environment excludes PYTHONPATH. Bootstrap the
                # known host worker from admitted paths through isolated argv;
                # these imports still require explicit policy read exceptions.
                startup = (
                    "import json,os,runpy,sys; "
                    "sys.path[:0]=json.loads(sys.argv[1]); "
                    "os.environ['ASTRID_INTERNAL_INVOCATION']='1'; "
                    "sys.argv=sys.argv[2:]; "
                    "runpy.run_module('astrid.core.execution.generic_host_worker',run_name='__main__')"
                )
                argv = [sys.executable, "-I", "-S", "-B", "-c", startup,
                        json.dumps(list(dict.fromkeys([
                            package_parent, sysconfig.get_path("purelib"), sysconfig.get_path("platlib"),
                            *_dependency_pythonpath(), *import_roots,
                        ]))), *argv[2:]]
            plan = self._workload_launch_plan(
                argv, attempt=attempt_path, cwd=None, environment=env,
                inherited_fds=inherited_fds, broker_endpoint=broker_endpoint, child_bridge=child_bridge,
            )
            process = popen_owned_group(list(plan.argv), **plan.subprocess_options(), text=True)
            self._track_process(process)
            if child_bridge is not None:
                try:
                    child_bridge.launched(process)
                except BaseException as error:
                    _terminate_process_group(process)
                    self._untrack_process(process)
                    _, diagnostic = process.communicate()
                    secret_values = {key: value for key, value in env.items()
                                     if key.upper().endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))}
                    raise HostError("child pipe bootstrap failed: " + self._scrub_secret_text(
                        (diagnostic or "")[-3500:], secret_values)) from error
        except BaseException:
            if remotion_lease is not None:
                remotion_lease.close()
            raise
        if remotion_lease is not None:
            from .remotion_pipe import HostPipeBridge
            try:
                remotion_bridge = HostPipeBridge(remotion_lease, process,
                    admission={"capability_id": capability_id,
                               "capability_digest": str((admission or {}).get("capability_digest") or _capability_digest(definition or {})),
                               "version": str((definition or {}).get("version", "")), "source": dict(admission or {})},
                    launch_digest=hashlib.sha256(json.dumps([plan.argv, str(plan.cwd), plan.environment]).encode()).hexdigest())
            except BaseException as error:
                _terminate_process_group(process)
                self._untrack_process(process)
                try:
                    failed_bridge = getattr(error, 'remotion_bridge', None)
                    if failed_bridge is not None:
                        failed_bridge.finish(cancelled=True)
                finally:
                    remotion_lease.close()
                raise
        try:
            guards_due = 0.0
            while process.poll() is None:
                if remotion_bridge is not None:
                    remotion_bridge.check()
                if time.monotonic() >= guards_due:
                    guards_due = time.monotonic() + _LIVE_GUARD_INTERVAL_SECONDS
                    _assert_live_storage_envelope(storage_estimate, attempt_path, attempt_path / "outputs")
                    if cancelled is not None and cancelled():
                        _terminate_process_group(process)
                        raise HostCancelled(f"capability {capability_id!r} cancelled")
                time.sleep(0.05)
            if remotion_bridge is not None:
                stdout, stderr = remotion_bridge.finish(cancelled=process.returncode != 0)
                remotion_completed = process.returncode == 0
            else:
                stdout, stderr = process.communicate()
            if cancelled is not None and cancelled():
                raise HostCancelled(f"capability {capability_id!r} cancelled")
            if process.returncode != 0:
                secret_values = {
                    key: value for key, value in env.items()
                    if key.upper().endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
                }
                if process.returncode > 0 and result_path.is_file() and not result_path.is_symlink() and result_path.stat().st_size <= 16384:
                    try:
                        rejection = provider_failure(json.loads(result_path.read_text()).get("error"))
                    except (OSError, ValueError, AttributeError):
                        rejection = None
                    if rejection is not None:
                        raise ProviderFailure(self._scrub_secret_text(str(rejection), secret_values),
                                              provider_id=rejection.provider_id,
                                              retryable=rejection.retryable,
                                              retry_after_seconds=rejection.retry_after_seconds)
                detail = self._scrub_secret_text((stderr or stdout).strip(), secret_values)
                raise HostError(
                    f"capability {capability_id!r} child exited {process.returncode}"
                    + (f": {detail[-3500:]}" if detail else "")
                )
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise HostError(
                    f"capability {capability_id!r} child returned no result"
                ) from exc
            if not isinstance(result, Mapping):
                raise HostError(f"capability {capability_id!r} child result is not an object")
            if not result.get("ok", False):
                rejection = provider_failure(result.get("error"))
                if rejection is not None:
                    raise rejection
                raise HostError(str(result.get("error") or f"capability {capability_id!r} failed"))
            process_id = process.pid
            returncode = result.get("returncode")
            if returncode is None:
                returncode = process.returncode
            child_payload = dict(result.get("payload") or {})
            child_payload.setdefault("process_id", process_id)
            child_payload.setdefault("returncode", returncode)
            return SimpleNamespace(
                ok=True,
                returncode=returncode,
                outputs=result.get("outputs") or [],
                payload=child_payload,
                stdout=self._scrub_secret_text(stdout, {
                    key: value for key, value in env.items()
                    if key.upper().endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
                }),
                stderr=self._scrub_secret_text(stderr, {
                    key: value for key, value in env.items()
                    if key.upper().endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
                }),
                process_id=process_id,
            )
        finally:
            if process.poll() is None:
                _terminate_process_group(process)
            self._untrack_process(process)
            if remotion_lease is not None:
                try:
                    if remotion_bridge is not None:
                        remotion_bridge.finish(cancelled=not remotion_completed)
                except BaseException as error:
                    if remotion_lease.owner.uncertain is not None:
                        self._latch_cleanup_uncertainty(str(error))
                    raise
                finally:
                    remotion_lease.close()
            _release_owned_group(process)
            env.clear()
            request_path.unlink(missing_ok=True)
            result_path.unlink(missing_ok=True)

    def _publish_assembled_timeline(
        self,
        *,
        task_data: Mapping[str, Any],
        attempt_id: str,
        fence: int,
        lease_token: str,
        outputs: Sequence[Mapping[str, Any]],
        attempt_root: Path,
    ) -> Mapping[str, Any]:
        """Publish the authoring result before settling its Runtime attempt."""
        publish = getattr(self.client, "publish_timeline_render", None)
        if not callable(publish):
            raise HostError("runtime client lacks canonical publish_timeline_render operation")
        proposal_descriptor = next(
            (
                item for item in outputs
                if str(item.get("artifact_type", "")) == "timeline/authoring-proposal"
                or Path(str(item.get("path", ""))).name == "authoring-proposal.json"
            ),
            None,
        )
        if proposal_descriptor is None:
            raise HostError("rendering.assemble_timeline produced no authoring proposal")
        proposal_path = Path(str(proposal_descriptor.get("path", ""))).expanduser().resolve()
        if not proposal_path.is_file() or not proposal_path.is_relative_to(Path(attempt_root).resolve()):
            raise HostError("authoring proposal path is outside the current attempt")
        try:
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HostError(f"authoring proposal is unreadable: {exc}") from exc
        if not isinstance(proposal, Mapping):
            raise HostError("authoring proposal must be an object")
        publication = proposal.get("publication")
        timeline = proposal.get("timeline")
        registry = proposal.get("registry")
        if not isinstance(publication, Mapping) or publication.get("authority") != "workspace_runtime":
            raise HostError("authoring proposal is not Runtime-owned")
        if publication.get("render_capability") != "rendering.render":
            raise HostError("authoring proposal must use rendering.render")
        if not isinstance(timeline, Mapping) or not isinstance(registry, Mapping):
            raise HostError("authoring proposal must contain timeline and registry objects")

        admitted = task_data.get("spec")
        if not isinstance(admitted, Mapping):
            raise HostError("authoring task is missing its immutable spec")
        public_spec = admitted.get("spec") if isinstance(admitted.get("spec"), Mapping) else admitted
        dependencies = public_spec.get("runtime_dependencies") if isinstance(public_spec, Mapping) else None
        if not isinstance(dependencies, Mapping):
            raise HostError("authoring task is missing runtime publication settings")
        timeline_id = dependencies.get("timeline_id") or dependencies.get("timeline_ref")
        expected_version = dependencies.get("expected_version")
        publication_settings = dependencies.get("publication")
        if isinstance(publication_settings, Mapping):
            timeline_id = timeline_id or publication_settings.get("timeline_id") or publication_settings.get("timeline_ref")
            expected_version = expected_version if expected_version is not None else publication_settings.get("expected_version")
        if not isinstance(timeline_id, str) or not timeline_id.strip():
            raise HostError("authoring task publication settings require timeline_id")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise HostError("authoring task publication settings require a positive expected_version")

        render = dependencies.get("render")
        if render is None and isinstance(publication_settings, Mapping):
            render = publication_settings.get("render")
        if not isinstance(render, Mapping):
            raise HostError("authoring task publication settings require render configuration")
        render = dict(render)
        if render.get("capability_id") != "rendering.render":
            raise HostError("authoring task render configuration must use rendering.render")
        if not isinstance(render.get("capability_digest"), str) or not render["capability_digest"].startswith("sha256:"):
            render_record = self.capabilities.get("rendering.render")
            if render_record is None:
                raise HostError("rendering.render capability is not registered with this host")
            render["capability_digest"] = render_record.capability_digest
        render.setdefault("schema_version", "1")
        render_spec = render.get("spec")
        if not isinstance(render_spec, Mapping):
            render_spec = {"capability_id": "rendering.render", "kind": "executor", "inputs": {}, "outputs": {}}
        render["spec"] = dict(render_spec)
        idempotency_key = f"timeline-publication-{task_data.get('id', attempt_id)}"
        return publish(
            attempt_id,
            lease_token,
            fence=int(fence),
            timeline_id=timeline_id,
            expected_version=expected_version,
            config=dict(timeline),
            registry=dict(registry),
            render=render,
            idempotency_key=idempotency_key,
        )

    def _wan_session_context(self, task_data: Mapping[str, Any]) -> dict[str, Any]:
        """Read the explicitly admitted Wan source/config/interpreter tuple."""
        raw = task_data.get("wan_session")
        if raw is None and isinstance(task_data.get("spec"), Mapping):
            raw = task_data["spec"].get("wan_session")
        if not isinstance(raw, Mapping):
            raise HostError(
                "wan2gp.generate_video requires an explicit wan_session admission "
                "with root, config_path, python, owner_dir and identity digests"
            )
        required = (
            "root",
            "config_path",
            "python",
            "owner_dir",
            "source_digest",
            "config_digest",
        )
        missing = [key for key in required if not isinstance(raw.get(key), str) or not str(raw[key]).strip()]
        for key in ("readiness_timeout", "release_timeout"):
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                missing.append(key)
        if missing:
            raise HostError("Wan session admission is missing explicit fields: " + ", ".join(missing))
        root = Path(str(raw["root"])).expanduser().resolve()
        config = Path(str(raw["config_path"])).expanduser().resolve()
        python = Path(str(raw["python"])).expanduser().absolute()
        owner = Path(str(raw["owner_dir"])).expanduser().resolve()
        if root.is_symlink() or not root.is_dir() or not (root / "shared" / "api.py").is_file():
            raise HostError("Wan session source root is not an explicit upstream checkout")
        if config.is_symlink() or not config.is_file():
            raise HostError("Wan session config_path is not an explicit regular file")
        if not python.is_file():
            raise HostError("Wan session python is not an explicit interpreter file")
        options = {"config_path": str(config)}
        for key in ("cli_args", "console_isatty"):
            if key in raw:
                options[key] = raw[key]
        return {
            "owner_dir": owner,
            "root": root,
            "python": python,
            "source_digest": str(raw["source_digest"]),
            "config_digest": str(raw["config_digest"]),
            "readiness_timeout": float(raw.get("readiness_timeout", 30.0)),
            "release_timeout": float(raw.get("release_timeout", 30.0)),
            "init_options": options,
        }

    def _run_wan_session(
        self,
        *,
        inputs: Mapping[str, Any],
        output_root: Path,
        attempt: Path,
        managed_token: AdmissionToken,
        execution_deadline: float,
        cancelled: Callable[[], bool],
    ) -> Any:
        from astrid.packs.wan2gp.src.driver import compile_host_settings, materialize_host_result

        settings = compile_host_settings(inputs)
        evidence = self.invoke_wan_session(
            managed_token,
            settings,
            timeout=max(0.1, execution_deadline - time.monotonic()),
            cancelled=cancelled,
            progress_path=attempt / ".astrid-progress.json",
        )
        result = evidence.get("result") if isinstance(evidence, Mapping) else None
        if not isinstance(result, Mapping) or result.get("success") is not True:
            if cancelled():
                return SimpleNamespace(payload={"wan_native": dict(evidence)})
            errors = result.get("errors", []) if isinstance(result, Mapping) else []
            if not errors and isinstance(evidence.get("native_error"), str):
                errors = [evidence["native_error"]]
            raise HostError(
                "native Wan2GP job failed: " + "; ".join(str(item) for item in errors)
            )
        if cancelled():
            raise HostCancelled("Wan attempt cancelled before output custody")
        manager = self.managed_tool_session
        manager.adapter_for(managed_token)
        mapped = materialize_host_result(
            evidence, attempt_root=output_root, inputs=inputs,
            expected_identity=_ManagedWanChildAdapter.identity(managed_token),
        )
        manager.adapter_for(managed_token)
        return SimpleNamespace(
            payload={"wan_native": dict(evidence), "wan_mapping": mapped},
            process_id=evidence.get("process_id"),
            returncode=evidence.get("returncode"),
            output_root=output_root,
        )

    def _native_cpu_handoff_identity(self, adapter: Any, token: AdmissionToken | None) -> Any:
        """One eligibility predicate for recording and consuming residency."""
        if not isinstance(adapter, _ManagedVibeSessionAdapter) or token is None:
            return None
        manager = self.managed_tool_session
        owner = self._handoff_accounting_owner
        active = manager._active
        residency_identity = adapter.residency_identity()
        if (
            active is None or active.adapter is not adapter
            or active.generation != token.generation or manager._generation != active.generation
            or active.binding.identity_key != token.binding_identity
            or active.last_admission_token_id != token.token_id
            or active.fenced or active.in_flight or active.dispatched
            or residency_identity is None
            or adapter.native_graph is not getattr(self, "_native_engine_graph", None)
            or adapter.cpu_handoff_failed or self._cleanup_uncertain or self._shutdown.is_set()
            or not owner._handoff_observation_known or owner._handoff_claim_gate_closed
        ):
            return None
        return (active.binding.identity_key, active.generation, residency_identity)

    def _record_native_cpu_handoff(
        self, adapter: Any, token: AdmissionToken | None, *,
        managed_settled: bool, runtime_settled: bool,
    ) -> None:
        """Bind a handoff only after this exact attempt's successful finally."""
        if not isinstance(adapter, _ManagedVibeSessionAdapter) or token is None:
            return
        manager = self.managed_tool_session
        owner = self._handoff_accounting_owner
        # All nested accesses use admission gate -> MTS, never the reverse.
        with owner._handoff_condition, manager._lock:
            active = manager._active
            if (
                active is None or active.adapter is not adapter
                or active.generation != token.generation
                or active.binding.identity_key != token.binding_identity
                or active.last_admission_token_id != token.token_id
            ):
                return
            adapter.cpu_handoff_identity = None
            adapter.cpu_handoff_token = None
            identity = self._native_cpu_handoff_identity(adapter, token)
            if managed_settled and runtime_settled and identity is not None:
                adapter.cpu_handoff_identity = identity
                adapter.cpu_handoff_token = token

    def _open_managed_task_session(
        self, *, capability: CapabilityDescriptor, binding: SessionBinding, adapter: Any,
    ) -> None:
        manager = self.managed_tool_session
        owner = self._handoff_accounting_owner
        with owner._handoff_condition, manager._lock:
            active = manager._active
            previous = active.adapter if active is not None else None
            cpu_target = (
                isinstance(adapter, _ManagedTaskAdapter)
                and set(capability.resources_claimed) == {"cpu"}
            )
            if (cpu_target and isinstance(previous, _ManagedVibeSessionAdapter)
                    and previous.residency_bound):
                identity = self._native_cpu_handoff_identity(previous, previous.cpu_handoff_token)
                if (
                    identity is None or previous.cpu_handoff_identity != identity
                ):
                    raise HostError("native CPU handoff lacks exact settled ownership")
                expected_token = previous.cpu_handoff_token
                previous.cpu_handoff_identity = None
                previous.cpu_handoff_token = None
                self._vibecomfy_warmth_hint = None
                self._vibecomfy_current_warmth_hint = None
                self._vibecomfy_requested_warmth_hint = None
                # Existing backend residency containment, not legacy owner
                # restart or graph release. Failed fence/release poisons MTS.
                try:
                    manager._replace_quiescent(
                        expected_adapter=previous, expected_token=expected_token,
                        capability=capability, binding=binding, adapter=adapter,
                        reason="cpu_residency_eviction",
                    )
                except Exception as error:
                    # Preserve the first exception chain in the existing log
                    # before task/owner cleanup can raise a secondary error.
                    # Exception messages, paths and caller data stay private.
                    kinds = {HostError: "host", SessionCapacityError: "capacity",
                             RuntimeError: "runtime", ValueError: "value",
                             OSError: "io", TimeoutError: "timeout"}
                    chain, seen = [], set()
                    current: BaseException | None = error
                    while current is not None and id(current) not in seen and len(chain) < 4:
                        seen.add(id(current))
                        chain.append(kinds.get(type(current), "other"))
                        current = current.__cause__ or (
                            None if current.__suppress_context__ else current.__context__)
                    try:
                        _LOGGER.error("CPU residency handoff first failure: %s", ">".join(chain))
                    except Exception:
                        pass
                    raise
                return
            manager.open(capability=capability, binding=binding, adapter=adapter)

    def run_task(
        self,
        task: Mapping[str, Any],
        *,
        lease_token: str,
        attempt_id: str | None = None,
        fence: int | None = None,
        keep_attempt: bool = False,
        provider_route_grant: str | None = None,
    ) -> Mapping[str, Any]:
        owns_attempt = self._handoff_begin_direct_attempt()
        thread_id = threading.get_ident()
        try:
            return self._run_task_impl(
                task, lease_token=lease_token, attempt_id=attempt_id, fence=fence,
                keep_attempt=keep_attempt, provider_route_grant=provider_route_grant,
            )
        finally:
            if owns_attempt:
                self._handoff_leave_attempt(thread_id)

    def _run_task_impl(
        self,
        task: Mapping[str, Any],
        *,
        lease_token: str,
        attempt_id: str | None = None,
        fence: int | None = None,
        keep_attempt: bool = False,
        provider_route_grant: str | None = None,
    ) -> Mapping[str, Any]:
        if self._cleanup_uncertain:
            raise self._cleanup_block_error()
        if self.client is None:
            raise HostError("runtime client is required to execute a task")
        if self._shutdown.is_set():
            raise HostCancelled("generic host is shutting down")
        for operation in ("heartbeat", "task", "settle", "fail", "upload_object"):
            if not callable(getattr(self.client, operation, None)):
                raise HostError(
                    f"runtime client lacks canonical {operation} operation"
                )
        task_data = task.get("task", task)
        task_id = str(task_data["id"])
        capability_id = str(task_data["capability"])
        attempt_id = attempt_id or (str(task_data["attempt_id"]) if task_data.get("attempt_id") else None)
        fence = fence if fence is not None else (int(task_data["fence"]) if task_data.get("fence") is not None else None)
        if not attempt_id or fence is None:
            raise HostError("runtime task execution requires attempt_id and fence")
        record = self.capabilities.get(capability_id)
        if record is None:
            self.discover()
            record = self.capabilities.get(capability_id)
        if record is None:
            raise HostError(f"capability not discovered: {capability_id}")

        def evidence_failure_diagnostic(error: BaseException) -> dict[str, Any] | None:
            if not isinstance(error, EvidenceCapError):
                return None
            raw = getattr(error, "diagnostic", {})
            diagnostic = dict(raw) if isinstance(raw, Mapping) else {}
            diagnostic.update(
                {
                    "guard": "generated_evidence",
                    "attempt_id": attempt_id,
                    "capability_id": capability_id,
                    "capability_digest": record.capability_digest,
                    "source_digest": record.source_digest,
                    "dependency_digest": record.dependency_digest,
                    "configured_cap_bytes": self.execution_policy.evidence_cap_bytes,
                }
            )
            return diagnostic

        def fail_admission(error: Exception) -> None:
            """Fence deterministic admission failures as terminal attempts."""
            try:
                self._handoff_settlement_call(self.client.fail,
                    task_id,
                    lease_token,
                    str(error),
                    retryable=False,
                    attempt_id=attempt_id,
                    fence=fence,
                )
            except Exception as runtime_exc:
                raise HostError(
                    "deterministic admission failure was not recorded by Runtime"
                ) from runtime_exc
            raise HostError(str(error)) from error

        registration_error = self._registration_admission_reason()
        if registration_error:
            fail_admission(HostError(registration_error))
        if _is_withdrawn(record):
            fail_admission(
                HostError(
                    f"capability {capability_id!r} is {_withdrawn_reason(record)}"
                )
            )
        # Idle advertisement reuses only the native observation. Every leased
        # task must prove the complete selected runtime again before any work;
        # exceptions as well as unavailable results settle the exact lease.
        try:
            self.preflight(capability_id)
            record = self.capabilities[capability_id]
            if not record.ready:
                raise HostError(f"capability {capability_id!r} is unavailable: {record.preflight}")
            registration_error = self._registration_admission_reason()
            if registration_error:
                raise HostError(registration_error)
        except Exception as exc:
            fail_admission(exc)

        try:
            _assert_fixed_request_scope(record, task_data)
            execution_contract = _execution_contract(
                task_data,
                runtime_session_id=self.runtime_state.get("runtime_session_id"),
            )
            contract_limits = execution_contract.get("limits", {}) if execution_contract else {}
            queue_limit = contract_limits.get("max_queue_seconds")
            if queue_limit is not None and _contract_queue_age(task_data) > queue_limit:
                raise HostError("execution_request max_queue_seconds exceeded before execution")
            claim_epoch = task_data.get("runtime_epoch", self.runtime_state.get("runtime_epoch"))
            if execution_contract is not None:
                if isinstance(claim_epoch, bool) or not isinstance(claim_epoch, int) or claim_epoch < 1:
                    raise HostError("execution_request requires a claimed runtime_epoch")
                observed_epoch = self.runtime_state.get("runtime_epoch")
                if observed_epoch is not None and observed_epoch != claim_epoch:
                    raise HostError("claimed runtime_epoch disagrees with observed runtime")
            storage_estimate = _task_storage_estimate(task_data)
            if record.definition.metadata.get("storage_estimate_required"):
                if storage_estimate is None:
                    raise HostError(
                        f"capability {capability_id!r} requires a whole-task storage_estimate"
                    )
                expected = {
                    "scratch_bytes": record.estimated_scratch_bytes,
                    "output_bytes": record.estimated_output_bytes,
                }
                if storage_estimate != expected:
                    raise HostError(
                        f"capability {capability_id!r} requires storage_estimate={expected}, "
                        f"got {storage_estimate}"
                    )
            input_size_limits = _storage_input_limits(record.definition.metadata)
        except Exception as exc:
            fail_admission(exc)
        try:
            self.execution_policy.assert_budget_available()
        except ExecutionGuardError as exc:
            self._handoff_settlement_call(self.client.fail,
                task_id,
                lease_token,
                str(exc),
                retryable=False,
                attempt_id=attempt_id,
                fence=fence,
                failure_diagnostic=evidence_failure_diagnostic(exc),
            )
            raise HostError(str(exc)) from exc
        spec = task_data.get("spec", {})
        private_runtime_context = isinstance(spec, Mapping) and (
            "child_delegation" in spec or "delegated_parent" in spec or "delegated_recoverable_outputs" in spec)
        authorized_input_object_ids = task_data.get("input_object_ids")
        ephemeral_attempt_root = self.attempt_root is None and self.attempt_base is None
        # ``attempt_root`` is intentionally an exact caller-owned spool for
        # debug/single-attempt callers. Long-lived hosts use ``attempt_base``
        # so sequential tasks get isolated namespaces.
        root = self._allocate_attempt_root(task_id, attempt_id)
        if self._attempt_policy_factory is not None:
            try:
                confirmation = self.client.confirm_claim_authority(
                    task_id=task_id, attempt_id=attempt_id, lease_id=lease_token, fence=fence,
                    runtime_epoch=claim_epoch, project_id=task_data.get("project_id"),
                    operation=capability_id, executor_id=self.executor_id)
                self._confinement_policy = self._attempt_policy_factory.build(
                    confirmation, attempt=root, record=record)
                self._attempt_claim_authority = confirmation
            except Exception as exc:  # noqa: BLE001 - terminalize every failed owned claim.
                if self.attempt_root is None:
                    shutil.rmtree(root)
                    self._last_cleanup_receipt = {"path": str(root), "status": "deleted",
                        "intended_disposition": "deleted", "observed_exists": False}
                fail_admission(exc)
        attempt_evidence_cap = _attempt_evidence_cap(
            self.execution_policy.evidence_cap_bytes,
            storage_estimate,
        )
        runtime_limit = contract_limits.get("max_runtime_seconds")
        lifetime = _AttemptLifetime(self.execution_policy, time.monotonic(), runtime_limit)
        # Noninteractive native sessions still need their finite submission bound.
        execution_deadline = lifetime.deadline()
        collection_limit = contract_limits.get("collection_seconds")
        warm_receipt = self.execution_policy.warm_expectation()
        evidence_receipt: dict[str, Any] | None = None
        deadline_exceeded = False
        settled = False
        cancelled_attempt = False
        network_broker: _NetworkBrokerContext | None = None
        pump_stop: threading.Event | None = None
        pump_thread: threading.Thread | None = None
        evidence_root: Path | None = None
        immutable_input_baseline: dict[str, tuple[int, str]] = {}
        evidence_cap_exceeded = False
        evidence_failure_message: str | None = None
        evidence_failure_receipt: dict[str, Any] | None = None
        storage_failure_receipt: dict[str, Any] | None = None
        deadline_failed = False
        storage_receipt: dict[str, int] | None = None
        # Every network attempt gets a fresh host-issued nonce.  It is part of
        # the immutable admission presented to an observable broker, so a
        # handshake captured from another task cannot be replayed.
        network_admission = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "fence": fence,
            "capability_digest": record.capability_digest,
            "source_digest": record.source_digest,
            "dependency_digest": record.dependency_digest,
            "version": record.definition.version,
            "source_root": str(record.source_root),
            "source_roots": [str(root) for root in _admitted_source_roots(record.source_root, record.definition)],
            "network_nonce": secrets_module.token_urlsafe(24),
            "allowed_routes": list((_network_policy(record) or {}).get("allowed_routes", (_network_policy(record) or {}).get("allowed_destinations", ()))),
        }
        cancel_signal = threading.Event()
        managed_adapter = _ManagedTaskAdapter(
            cancel_signal,
            process_census=lambda: bool(self._active_processes),
        )
        runtime_instance_id = str(
            self.runtime_state.get("runtime_instance_id")
            or self.runtime_state.get("instance_id")
            or "runtime-local"
        )
        runtime_endpoint = str(
            getattr(self.client, "endpoint", None)
            or getattr(getattr(self.client, "generated", None), "endpoint", None)
            or "runtime://local"
        )
        host_birth_id = process_birth_identity()
        managed_binding = SessionBinding(
            session_id=f"{capability_id}:{task_id}",
            runtime_instance_id=runtime_instance_id,
            process_birth_id=host_birth_id,
            endpoint=runtime_endpoint,
            source_digest=record.source_digest,
            config_digest=_canonical_digest(
                {
                    "capability_digest": record.capability_digest,
                    "dependency_digest": record.dependency_digest,
                    "interpreter": str(Path(sys.executable).resolve()),
                }
            ),
            runtime_epoch=claim_epoch if isinstance(claim_epoch, int) and not isinstance(claim_epoch, bool) else None,
            launch_generation=str(self.runtime_state.get("launch_generation") or host_birth_id),
            engine_birth_id=host_birth_id,
        )
        managed_capability = CapabilityDescriptor(
            capability_id=capability_id,
            residency_support="unsupported",
            resources_claimed=tuple(record.resource_keys),
            warm_reuse_expected=self.execution_policy.warm_reuse_expected,
        )
        readiness_profile = _read_readiness_profile_document()
        vibe_session = (
            readiness_profile.get("vibecomfy_session")
            if isinstance(readiness_profile, Mapping)
            else None
        )
        if capability_id == "vibecomfy.run" and isinstance(vibe_session, Mapping):
            managed_capability = CapabilityDescriptor(
                capability_id=capability_id,
                residency_support="observable_releasable",
                resources_claimed=tuple(record.resource_keys),
                warm_reuse_expected=self.execution_policy.warm_reuse_expected,
            )
            managed_adapter = None
        managed_token = None
        managed_settled = False
        managed_opened = False
        execution_identity = ""
        model_id = "vibecomfy.run"
        template_id = "vibecomfy.run"
        self._vibecomfy_current_warmth_hint = None
        self._vibecomfy_requested_warmth_hint = None

        def current_task_state():
            current = self.client.task(task_id)
            current_task = current.get("task", current) if isinstance(current, Mapping) else current
            if isinstance(current_task, Mapping):
                return current_task.get("state", current_task.get("status"))
            return getattr(current_task, "state", getattr(current_task, "status", None))

        def cancelled():
            nonlocal deadline_exceeded, evidence_cap_exceeded
            nonlocal evidence_failure_receipt, evidence_failure_message
            try:
                lifetime.assert_authority()
            except Exception:
                cancel_signal.set()
                return True
            if lifetime.expired():
                deadline_exceeded = True
                cancel_signal.set()
                return True
            if self._shutdown.is_set() or cancel_signal.is_set():
                return True
            if evidence_root is not None:
                try:
                    self.execution_policy.assert_evidence_cap(
                        evidence_root,
                        immutable_inputs=immutable_input_baseline,
                        cap_bytes=attempt_evidence_cap,
                    )
                except EvidenceCapError as exc:
                    evidence_cap_exceeded = True
                    evidence_failure_receipt = evidence_failure_diagnostic(exc)
                    evidence_failure_message = str(exc)
                    cancel_signal.set()
                    return True
                finally:
                    self._publish_evidence_status()
            try:
                state = current_task_state()
            except Exception:
                # Lost lease/transport is cancellation containment, not a
                # reason to publish output or issue a second fail attempt.
                return True
            # A request contains this owned invocation; it is not a Runtime
            # terminal acknowledgement or proof of provider/session cleanup.
            return state in {"cancel_requested", "cancelled"}

        def terminalize_deadline() -> None:
            """Fence a deadline expiry at Runtime before returning to the worker."""
            nonlocal deadline_failed
            try:
                self._handoff_settlement_call(self.client.fail,
                    task_id,
                    lease_token,
                    "execution deadline exceeded",
                    retryable=False,
                    attempt_id=attempt_id,
                    fence=fence,
                )
                deadline_failed = True
            except Exception as exc:
                try:
                    state = current_task_state()
                except Exception as status_exc:
                    raise HostError("deadline cancellation could not be verified") from status_exc
                if state not in {"cancelled", "failed"}:
                    raise HostError("deadline terminalization was not accepted by Runtime") from exc
                deadline_failed = state == "failed"

        def handle_guard_abort() -> None:
            if evidence_cap_exceeded:
                self._handoff_settlement_call(self.client.fail,
                    task_id,
                    lease_token,
                    _evidence_cap_message(evidence_failure_message),
                    retryable=False,
                    attempt_id=attempt_id,
                    fence=fence,
                    failure_diagnostic=evidence_failure_receipt,
                )
                raise HostError(_evidence_cap_message(evidence_failure_message))
            if deadline_exceeded:
                terminalize_deadline()

        # Lease renewal must cover the complete claimed-attempt lifecycle,
        # including input materialization, canonical identity/attestation,
        # managed-session setup, output harvesting, upload, and settlement.
        # Those steps can be expensive on a cold GPU release (for example,
        # source-integrity attestation may hash thousands of files), and a
        # pump started only immediately before child launch can let an otherwise
        # healthy claim expire before the first heartbeat.
        pump_stop = threading.Event()
        progress_path = root / ".astrid-progress.json"
        latest_progress: Mapping[str, Any] | None = None

        def read_progress() -> Mapping[str, Any] | None:
            nonlocal latest_progress
            try:
                payload = json.loads(progress_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return latest_progress
            if isinstance(payload, Mapping):
                latest_progress = {
                    str(key): value
                    for key, value in payload.items()
                    if str(key) in {"phase", "percent", "current", "total"}
                }
            return latest_progress

        initial_progress = read_progress()
        heartbeat_kwargs = {
            "attempt_id": attempt_id,
            "fence": fence,
        }
        if initial_progress is not None:
            heartbeat_kwargs["progress"] = initial_progress
        self.client.heartbeat(task_id, lease_token, **heartbeat_kwargs)

        def pump_lease():
            while not pump_stop.wait(5.0):
                try:
                    heartbeat_progress = read_progress()
                    heartbeat_kwargs = {
                        "attempt_id": attempt_id,
                        "fence": fence,
                    }
                    if heartbeat_progress is not None:
                        heartbeat_kwargs["progress"] = heartbeat_progress
                    self.client.heartbeat(task_id, lease_token, **heartbeat_kwargs)
                    lifetime.assert_authority()
                    state = current_task_state()
                    if state in {"cancel_requested", "cancelled"}:
                        cancel_signal.set()
                except Exception:
                    # The daemon fences settlement if a heartbeat is lost; the
                    # subprocess is still cleaned up here.
                    cancel_signal.set()

        pump_thread = threading.Thread(
            target=pump_lease,
            name=f"astrid-heartbeat-{task_id}",
            daemon=True,
        )
        pump_thread.start()
        child_bridge = None
        try:
            # Admission readback is bounded transport work under the lease pump.
            # Decide exactly once, retaining the original start and setup bound.
            lifetime.authority = _interactive_review_authority(
                self, task_data, record, attempt_id=attempt_id, lease_id=lease_token,
                fence=fence, runtime_epoch=claim_epoch)
            if isinstance(spec, Mapping) and ("child_delegation" in spec or "delegated_recoverable_outputs" in spec):
                from ._child_bridge import HostChildBridge

                child_bridge = HostChildBridge(
                    self, task_data, attempt_id=attempt_id, lease_id=lease_token, fence=fence,
                    runtime_epoch=claim_epoch, output_root=root / "outputs", cancelled=cancelled)
                with self._process_lock:
                    self._child_bridges.add(child_bridge)
                lifetime.review = _training_review_authority(
                    self, task_data, record, child_bridge, attempt_id=attempt_id, lease_id=lease_token,
                    fence=fence, runtime_epoch=claim_epoch)
            raw_task_param_ports = record.definition.metadata.get("hc04_param_ports")
            task_param_ports = (
                tuple(str(value) for value in raw_task_param_ports)
                if isinstance(raw_task_param_ports, (list, tuple))
                else None
            )
            raw_cas_param_ports = record.definition.metadata.get("hc04_cas_param_ports")
            cas_param_ports = (
                tuple(str(value) for value in raw_cas_param_ports)
                if isinstance(raw_cas_param_ports, (list, tuple))
                else None
            )
            raw_optional_cas_param_ports = record.definition.metadata.get(
                "hc04_optional_cas_param_ports"
            )
            optional_cas_param_ports = (
                tuple(str(value) for value in raw_optional_cas_param_ports)
                if isinstance(raw_optional_cas_param_ports, (list, tuple))
                else None
            )
            inputs = self._materialize_inputs(
                spec,
                root,
                authorized_input_object_ids=authorized_input_object_ids,
                task_param_ports=task_param_ports,
                cas_param_ports=cas_param_ports,
                optional_cas_param_ports=optional_cas_param_ports,
                storage_estimate=storage_estimate,
                input_size_limits=input_size_limits,
                storage_policy_version=(
                    str(record.definition.metadata.get("storage_policy_version"))
                    if record.definition.metadata.get("storage_policy_version") is not None
                    else None
                ),
                continuation_id=task_id,
                file_input_names=frozenset(
                    port.name
                    for port in record.definition.inputs
                    if port.type == "file"
                ),
            )
            _iteration_video_discovery(
                self, task_data, record, child_bridge, inputs, attempt_id=attempt_id,
                lease_id=lease_token, fence=fence, runtime_epoch=claim_epoch, cancelled=cancelled)
            if record.definition.metadata.get("storage_policy_version") in {
                "astrid.cloud-i2i.z-image.v1",
                "astrid.cloud-edit.qwen-source.v1",
                "astrid.cloud-edit.unified.v1",
            }:
                from astrid.core.generation.storage_policy import (
                    CLOUD_EDIT_STORAGE_POLICY,
                    CLOUD_I2I_STORAGE_POLICY,
                    CLOUD_UNIFIED_EDIT_STORAGE_POLICY,
                    ImageStoragePolicyError,
                )

                admitted_spec = spec.get("spec") if isinstance(spec, Mapping) else None
                admitted_params = admitted_spec.get("params") if isinstance(admitted_spec, Mapping) else None
                descriptor = admitted_params.get("image_ref") if isinstance(admitted_params, Mapping) else None
                materialized = inputs.get("image_ref")
                media_type = descriptor.get("media_type") if isinstance(descriptor, Mapping) else None
                if not isinstance(materialized, str) or not isinstance(media_type, str):
                    raise HostError("bounded cloud i2i source materialization is incomplete")
                storage_policy = (
                    CLOUD_I2I_STORAGE_POLICY
                    if record.definition.metadata.get("storage_policy_version")
                    == CLOUD_I2I_STORAGE_POLICY.version
                    else (
                        CLOUD_UNIFIED_EDIT_STORAGE_POLICY
                        if record.definition.metadata.get("storage_policy_version")
                        == CLOUD_UNIFIED_EDIT_STORAGE_POLICY.version
                        else CLOUD_EDIT_STORAGE_POLICY
                    )
                )
                try:
                    if storage_policy is CLOUD_UNIFIED_EDIT_STORAGE_POLICY:
                        mask_descriptor = admitted_params.get("mask_ref") if isinstance(admitted_params, Mapping) else None
                        materialized_mask = inputs.get("mask_ref")
                        mask_media_type = mask_descriptor.get("media_type") if isinstance(mask_descriptor, Mapping) else None
                        storage_policy.validate_materialized_inputs(
                            model=str(admitted_params.get("model")),
                            mode=str(admitted_params.get("mode")),
                            execution=str(admitted_params.get("execution")),
                            params={
                                **dict(admitted_params),
                                "image_ref": materialized,
                                "mask_ref": materialized_mask,
                            },
                            image_media_type=media_type,
                            mask_media_type=mask_media_type,
                        )
                    else:
                        storage_policy.validate_materialized_source(
                            materialized,
                            media_type=media_type,
                        )
                except ImageStoragePolicyError as exc:
                    raise HostError(str(exc)) from exc
            _assert_live_storage_envelope(storage_estimate, root, root / "outputs")
            immutable_input_baseline = {}
            for input_root in (root / "inputs", root / "managed-objects"):
                immutable_input_baseline.update(
                    self.execution_policy.immutable_input_baseline(input_root)
                )
            evidence_root = root
            lifetime.assert_deadline()
            if capability_id == "vibecomfy.run":
                execution_identity, model_id, template_id, workflow_session_requirements = (
                    _prepare_vibecomfy_execution_identity(
                        inputs,
                        root / "workflow-identity",
                        readiness_profile,
                    )
                )
                network_admission["execution_identity"] = execution_identity
                from astrid.packs.vibecomfy.production_engine import session_identity_digest

                exact_facts = (
                    readiness_profile.get("verified_facts", {}).get("exact", {})
                    if isinstance(readiness_profile, Mapping)
                    and isinstance(readiness_profile.get("verified_facts"), Mapping)
                    else {}
                )
                model_digest = (
                    exact_facts.get("model_digest")
                    if isinstance(exact_facts, Mapping)
                    else None
                )
                facts_digest = (
                    readiness_profile.get("verified_facts_digest")
                    if isinstance(readiness_profile, Mapping)
                    else None
                )
                if not isinstance(vibe_session, Mapping):
                    # pip_embedded is a valid local, non-persistent route. It
                    # has no HC-03 session binding, so bind reuse to the
                    # admitted workflow's resident requirements plus the
                    # pinned dependency/source identity instead.
                    session_identity_fields = {
                        "profile": "pip_embedded",
                        "source_digest": record.source_digest,
                        "dependency_digest": record.dependency_digest,
                        "workflow_resident_requirements": workflow_session_requirements,
                    }
                else:
                    session_identity_fields = {
                        "verified_facts_digest": facts_digest,
                        "runtime_instance_id": (
                            readiness_profile.get("runtime", {}).get("runtime_instance_id")
                            if isinstance(readiness_profile.get("runtime"), Mapping)
                            else None
                        ),
                        "source_revision": vibe_session.get("source_revision"),
                        "source_content_digest": vibe_session.get("source_content_digest"),
                        "config_digest": vibe_session.get("config_digest"),
                        "server_url": vibe_session.get("server_url"),
                        "workflow_resident_requirements": workflow_session_requirements,
                    }
                stable_session_identity = session_identity_digest(
                    model_id,
                    model_digest=model_digest,
                    required_identity=session_identity_fields,
                )
                managed_binding = replace(
                    managed_binding,
                    # Workflow/template/attempt identity remains in the
                    # admitted child request.  ManagedToolSession compares
                    # only this stable model/session identity so a compatible
                    # workflow switch stays in the same owned session.
                    execution_identity=stable_session_identity,
                )
                # A changed resident identity must release the current owned
                # server before we construct a replacement adapter.  The
                # owner restart mutates the in-memory HC-03 session binding
                # after verifying the fresh registry; re-read that binding
                # before any new adapter/session object is admitted.
                current_binding = self.managed_tool_session.current_binding
                if (
                    current_binding is not None
                    and current_binding.execution_identity != stable_session_identity
                ):
                    self.managed_tool_session.release(reason="capacity_replacement")
                    readiness_profile = _read_readiness_profile_document()
                    refreshed_session = (
                        readiness_profile.get("vibecomfy_session")
                        if isinstance(readiness_profile, Mapping)
                        else None
                    )
                    if not isinstance(refreshed_session, Mapping):
                        raise HostError("vibecomfy owner restart did not provide a fresh HC-03 session")
                    vibe_session = refreshed_session
                    fresh_facts = (
                        readiness_profile.get("verified_facts", {}).get("exact", {})
                        if isinstance(readiness_profile, Mapping)
                        and isinstance(readiness_profile.get("verified_facts"), Mapping)
                        else {}
                    )
                    fresh_model_digest = (
                        fresh_facts.get("model_digest")
                        if isinstance(fresh_facts, Mapping)
                        else None
                    )
                    fresh_facts_digest = (
                        readiness_profile.get("verified_facts_digest")
                        if isinstance(readiness_profile, Mapping)
                        else None
                    )
                    fresh_identity_fields = {
                        "verified_facts_digest": fresh_facts_digest,
                        "runtime_instance_id": (
                            readiness_profile.get("runtime", {}).get("runtime_instance_id")
                            if isinstance(readiness_profile.get("runtime"), Mapping)
                            else None
                        ),
                        "source_revision": vibe_session.get("source_revision"),
                        "source_content_digest": vibe_session.get("source_content_digest"),
                        "config_digest": vibe_session.get("config_digest"),
                        "server_url": vibe_session.get("server_url"),
                        "workflow_resident_requirements": workflow_session_requirements,
                    }
                    stable_session_identity = session_identity_digest(
                        model_id,
                        model_digest=fresh_model_digest,
                        required_identity=fresh_identity_fields,
                    )
                    managed_binding = replace(
                        managed_binding, execution_identity=stable_session_identity
                    )
            if capability_id == "vibecomfy.run" and isinstance(vibe_session, Mapping):
                managed_adapter = _ManagedVibeSessionAdapter.from_host_session(
                    native_graph=getattr(self, "_native_engine_graph", None),
                    hc03_profile=readiness_profile,
                    model_id=model_id,
                    template_id=template_id,
                    invocation_identity=f"{task_id}:{attempt_id}:{fence}",
                )
                session_id = str(vibe_session.get("session_dir") or "")
                session_birth = str(vibe_session.get("process_birth_id") or "")
                session_endpoint = str(vibe_session.get("server_url") or "")
                session_source = str(vibe_session.get("source_revision") or "")
                session_config = str(vibe_session.get("config_digest") or "")
                if execution_contract is not None and not vibe_session.get("comfy_process_birth_id"):
                    raise HostError("execution_request requires observed VibeComfy engine birth identity")
                managed_binding = SessionBinding(
                    session_id=session_id,
                    runtime_instance_id=str(
                        (readiness_profile.get("runtime") or {}).get("runtime_instance_id")
                        if isinstance(readiness_profile.get("runtime"), Mapping)
                        else runtime_instance_id
                    ),
                    process_birth_id=session_birth,
                    endpoint=session_endpoint,
                    source_digest=session_source,
                    config_digest=session_config,
                    execution_identity=stable_session_identity,
                    runtime_epoch=claim_epoch if isinstance(claim_epoch, int) and not isinstance(claim_epoch, bool) else None,
                    launch_generation=str(vibe_session.get("launch_generation") or session_birth),
                    engine_birth_id=str(vibe_session.get("comfy_process_birth_id") or session_birth),
                )
                from astrid.core.generation.backends.vibecomfy import vibecomfy_warmth_hint

                current_warmth_hint = vibecomfy_warmth_hint(
                    session_id=session_id,
                    process_birth_id=session_birth,
                    comfy_process_birth_id=str(vibe_session.get("comfy_process_birth_id") or ""),
                    runtime_instance_id=managed_binding.runtime_instance_id,
                    model_id=model_id,
                    model_bytes_digest=str(exact_facts.get("model_digest") or ""),
                    facts_digest=str(facts_digest or ""),
                    source_revision=str(vibe_session.get("source_revision") or ""),
                    source_content_digest=str(
                        vibe_session.get("source_content_digest") or ""
                    ),
                    config_digest=str(vibe_session.get("config_digest") or ""),
                )
                self._vibecomfy_current_warmth_hint = current_warmth_hint
                self._vibecomfy_requested_warmth_hint = (
                    current_warmth_hint
                    if self._vibecomfy_warmth_hint == current_warmth_hint
                    else None
                )
            if capability_id == "wan2gp.generate_video":
                managed_binding = self.prepare_wan_session(**self._wan_session_context(task_data))
                managed_adapter = self.managed_tool_session.current_adapter
                managed_capability = CapabilityDescriptor(
                    capability_id=capability_id,
                    residency_support="unknown",
                    resources_claimed=tuple(record.resource_keys),
                    warm_reuse_expected=True,
                )
            else:
                self._open_managed_task_session(
                    capability=managed_capability,
                    binding=managed_binding,
                    adapter=managed_adapter,
                )
            managed_opened = True
            self.managed_tool_session.observe(managed_binding)
            managed_token = self.managed_tool_session.admit(
                capability_id=capability_id,
                invocation_id=f"{task_id}:{attempt_id}:{fence}",
            )
            if record.adapter.family == "provider" and record.definition.isolation.network:
                policy = _network_policy(record)
                descriptor = (policy or {}).get("broker", {})
                binding = ProviderRouteGrantAuthority.binding(
                    capability_id=record.id,
                    capability_digest=record.capability_digest,
                    broker=descriptor,
                )
                token = provider_route_grant or task_data.get("provider_route_grant") or (
                    spec.get("provider_route_grant") if isinstance(spec, Mapping) else None
                )
                try:
                    self._provider_grants.consume(
                        token,
                        task_id=task_id,
                        capability_id=record.id,
                        capability_digest=record.capability_digest,
                        routes=_provider_routes(record, inputs),
                        broker_binding=binding,
                    )
                    self._pending_provider_grants.pop(task_id, None)
                except ProviderRouteGrantError as exc:
                    raise HostError(str(exc)) from exc
            worker_admission = network_admission
            network_broker = self._start_network_broker(record, root, network_admission, inputs,
                                                        private_runtime_context=private_runtime_context)
            output_root = root / "outputs"
            output_root.mkdir(parents=True, exist_ok=True)
            lifetime.assert_deadline()
            try:
                # Setup/attestation may be slow, but a lost claim must never
                # reach native queue submission or child launch.
                if cancelled():
                    handle_guard_abort()
                    cancelled_attempt = True
                    return {"task_id": task_id, "status": "cancelled", "cancelled": True}
                lifetime.begin_execution()
                if capability_id == "wan2gp.generate_video":
                    result = self._run_wan_session(
                        inputs=inputs,
                        output_root=output_root,
                        attempt=root,
                        managed_token=managed_token,
                        execution_deadline=execution_deadline,
                        cancelled=cancelled,
                    )
                    effective_output_root = getattr(result, "output_root", None)
                    if isinstance(effective_output_root, (str, Path)):
                        output_root = Path(effective_output_root).expanduser().resolve()
                elif record.definition.command is not None:
                    result = self._run_command_definition(
                        record,
                        inputs,
                        output_root,
                        root,
                        cancelled=cancelled,
                        authority_context=(
                            (
                                spec.get("authority_context")
                                or (
                                    spec.get("spec", {}).get("authority_context")
                                    if isinstance(spec.get("spec"), Mapping)
                                    else None
                                )
                            )
                            if isinstance(spec, Mapping)
                            else None
                        ),
                        admission=network_admission,
                        network_broker=network_broker,
                        storage_estimate=storage_estimate,
                        child_bridge=child_bridge,
                        allow_runtime_connection=not private_runtime_context,
                    )
                    # Checkout-server VibeComfy commands run inside the
                    # attested HC-03 output root. Carry that effective root
                    # back to the outer settlement layer; otherwise harvest
                    # falls back to the empty attempt-local directory and
                    # reports a missing manifest after Comfy completed.
                    effective_output_root = getattr(result, "output_root", None)
                    if isinstance(effective_output_root, (str, Path)):
                        output_root = Path(effective_output_root).expanduser().resolve()
                else:
                    # Dispatch through the process boundary.  The immutable
                    # admitted definition is serialized for the child so a
                    # registry reload cannot silently select a different pack.
                    worker_admission = network_admission
                    if private_runtime_context:
                        worker_admission = {key: value for key, value in network_admission.items()
                                            if key not in {"task_id", "attempt_id", "fence", "network_nonce", "allowed_routes"}}
                    worker_env, worker_secrets = self._child_environment(
                        record, root, admission=network_admission, network_broker=network_broker,
                        allow_runtime_connection=not private_runtime_context)
                    try:
                        result = self.invoke_capability(
                            capability_kind="executor",
                            capability_id=capability_id,
                            request={
                                "out": str(output_root),
                                "inputs": inputs,
                                "project": task_data.get("project_id"),
                                "project_was_auto_resolved": True,
                                "python_exec": sys.executable,
                                "run_id": None if private_runtime_context else task_id,
                                "run_root": str(root),
                                "invocation": "runtime",
                            },
                            attempt=root,
                            cancelled=cancelled,
                            definition=record.definition.to_dict(),
                            admission=worker_admission,
                            child_env=worker_env,
                            require_network_startup=private_runtime_context and _network_policy(record) is not None,
                            network_broker_required=network_broker is not None,
                            storage_estimate=storage_estimate,
                            child_bridge=child_bridge,
                        )
                    finally:
                        worker_env.clear()
                        worker_secrets.clear()
            finally:
                # The outer run_task finally owns lease-pump shutdown and
                # cleanup; this boundary only preserves child env cleanup.
                pass
            lifetime.begin_collection(collection_limit)
            if child_bridge is not None:
                child_bridge.finish()
            # The periodic lease pump may not get another turn after a fast
            # command writes its terminal checkpoint. Persist that final
            # checkpoint before harvesting and settling so a completed task
            # never leaves the Runtime read model stuck at its last interval.
            final_progress = read_progress()
            if final_progress is not None and not cancelled():
                try:
                    self.client.heartbeat(
                        task_id,
                        lease_token,
                        attempt_id=attempt_id,
                        fence=fence,
                        progress=final_progress,
                    )
                except Exception:
                    cancel_signal.set()
            if cancelled():
                handle_guard_abort()
                if deadline_failed:
                    return {"task_id": task_id, "status": "failed", "deadline_exceeded": True}
                cancelled_attempt = True
                return {"task_id": task_id, "status": "cancelled", "cancelled": True}
            harvest_values = {**inputs, "out": str(output_root), "run_root": str(root), "python_exec": sys.executable}
            if record.definition.command is not None:
                _, harvest_values = _bind_host_owned_command_outputs(record, harvest_values, output_root=output_root)
            if capability_id == "wan2gp.generate_video":
                from astrid.packs.wan2gp.src.driver import verify_host_result

                self.managed_tool_session.adapter_for(managed_token)
                verify_host_result(result.payload["wan_mapping"], attempt_root=output_root)
            try:
                evidence_receipt = self.execution_policy.assert_evidence_cap(
                    root,
                    immutable_inputs=immutable_input_baseline,
                    cap_bytes=attempt_evidence_cap,
                )
                self._publish_evidence_status()
                lifetime.assert_deadline()
            except ExecutionGuardError as exc:
                if isinstance(exc, EvidenceCapError):
                    evidence_failure_receipt = evidence_failure_diagnostic(exc)
                    evidence_failure_message = str(exc)
                raise HostError(str(exc)) from exc
            try:
                harvested = harvest_staged_outputs(
                    output_root,
                    definition=record.definition,
                    declared_outputs=record.definition.outputs,
                    values=harvest_values,
                    require=False,
                )
            except HarvestError as exc:
                raise HostError(f"capability {record.id!r}: {exc}") from exc
            typed_outputs = self._typed_outputs(
                record,
                harvested,
                root,
                generation_intent=(
                    task_data.get("generation_intent")
                    if isinstance(task_data.get("generation_intent"), Mapping)
                    else None
                ),
            )
            thumbnail_outputs, thumbnail_diagnostics = self._generation_thumbnail_outputs(
                typed_outputs,
                attempt_root=root,
                task_data=task_data,
            )
            typed_outputs.extend(thumbnail_outputs)
            publication_result: Mapping[str, Any] | None = None
            if capability_id == "rendering.assemble_timeline":
                publication_result = self._publish_assembled_timeline(
                    task_data=task_data,
                    attempt_id=attempt_id,
                    fence=fence,
                    lease_token=lease_token,
                    outputs=typed_outputs,
                    attempt_root=root,
                )
            result_names = {
                descriptor["name"]
                for descriptor in harvested
                if descriptor.get("role", "result") == "result"
                and Path(str(descriptor.get("path", ""))).name != "manifest.json"
            }
            media_ports = {
                output.name
                for output in record.definition.outputs
                if any(
                    token in str(getattr(output, "artifact_type", "") or "").lower()
                    for token in ("video", "clip", "image", "audio", "media")
                )
            }
            missing_media_ports = sorted(media_ports.difference(result_names))
            if missing_media_ports:
                raise HostError(
                    f"capability {record.id!r} produced no result files for declared "
                    f"media port(s): {', '.join(missing_media_ports)}"
                )
            if outputs_required(record.definition) and not typed_outputs:
                raise HostError(
                    f"capability {record.id!r} produced no typed settled outputs"
                )
            storage_receipt = _task_storage_envelope(task_data, root, typed_outputs)
            project_id = task_data.get("project_id")
            if project_id is not None and (not isinstance(project_id, str) or not project_id.strip()):
                raise HostError("runtime task project_id must be a non-empty string or None")
            run_id = task_data.get("run_id")
            runtime_epoch = task_data.get("runtime_epoch")
            if typed_outputs and getattr(self.client, "REQUIRES_OUTPUT_BINDING", False) and (
                not isinstance(run_id, str)
                or not run_id
                or isinstance(runtime_epoch, bool)
                or not isinstance(runtime_epoch, int)
                or runtime_epoch < 1
            ):
                raise HostError(
                    "runtime task output publication requires run_id and runtime_epoch"
                )
            outputs = self._upload_outputs(
                typed_outputs,
                project_id=project_id,
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                lease_id=lease_token,
                fence=fence,
                runtime_epoch=(
                    runtime_epoch
                    if runtime_epoch is not None
                    else None
                ),
            ) if typed_outputs else []
            if capability_id == "wan2gp.generate_video":
                verify_host_result(result.payload["wan_mapping"], attempt_root=output_root)
            # Cancellation can arrive while staged outputs are being read or
            # uploaded. Never publish a completed settlement after that point.
            if cancelled():
                handle_guard_abort()
                if deadline_failed:
                    return {"task_id": task_id, "status": "failed", "deadline_exceeded": True}
                cancelled_attempt = True
                return {"task_id": task_id, "status": "cancelled", "cancelled": True}
            # Completion provenance is host-owned.  Keep any backend/B6/model
            # evidence returned by the child, then stamp the exact admitted
            # capability/source/dependency identities over it so a child
            # cannot replace the admission evidence in the settlement result.
            payload = {
                "adapter_family": record.adapter.family,
                **(getattr(result, "payload", {}) or {}),
                "capability_digest": record.capability_digest,
                "source_digest": record.source_digest,
                "dependency_digest": record.dependency_digest,
            }
            if publication_result is not None:
                payload["timeline_render_publication"] = dict(publication_result)
            if thumbnail_diagnostics:
                payload["thumbnail_diagnostics"] = thumbnail_diagnostics
            network_evidence = self._network_evidence(
                root,
                admission=network_admission,
                # Provider egress requires host-owned signed broker evidence.
                # A local-generation adapter may have ``isolation.network``
                # solely because it talks to the host-owned Comfy daemon on
                # loopback; that is diagnostic hook traffic, not provider
                # egress, and must not be rejected for lacking a provider
                # broker context.
                required=bool(record.adapter.requires_network),
                broker_required=network_broker is not None,
                broker_context=network_broker,
            )
            if network_evidence is not None:
                payload["network_evidence"] = network_evidence
            provenance = self.boot_manifest_provenance()
            if provenance is not None:
                payload["provenance"] = provenance
            if self.attempt_root is not None:
                scratch_disposition = "caller_owned_pending"
                retained_owner = "caller"
            elif keep_attempt:
                scratch_disposition = "retention_pending"
                retained_owner = "generic-pack-host"
            else:
                scratch_disposition = "cleanup_pending"
                retained_owner = "generic-pack-host"
            payload["execution_guards"] = {
                "evidence": evidence_receipt,
                **lifetime.receipt(),
                "warm_expectation": warm_receipt,
                "evidence_budget": {
                    "run_observed_bytes": self.execution_policy.evidence_budget.charged_bytes,
                    "cap_bytes": self.execution_policy.evidence_cap_bytes,
                },
                "scratch_disposition": scratch_disposition,
                "cleanup_path": str(root),
                "retained_owner": retained_owner,
                "retained_bytes": 0,
                "storage_envelope": storage_receipt,
            }
            payload["process_evidence"] = _completed_process_evidence(
                capability_id=capability_id,
                attempt_id=attempt_id,
                fence=fence,
                result=result,
                payload=payload,
            )
            effect = task_data.get("expected_effect")
            if isinstance(effect, list):
                effect = effect[0] if effect else None
            if cancelled():
                handle_guard_abort()
                if deadline_failed:
                    return {"task_id": task_id, "status": "failed", "deadline_exceeded": True}
                cancelled_attempt = True
                return {"task_id": task_id, "status": "cancelled", "cancelled": True}
            # Re-observe the actual engine session after output custody and
            # immediately before consuming the manager token.  A session
            # restart or identity change cannot become a Runtime settlement.
            self.managed_tool_session.observe(managed_binding)
            managed_envelope = self.managed_tool_session.settle(
                managed_token,
                result_evidence={
                    "token_id": managed_token.token_id,
                    "invocation_id": managed_token.invocation_id,
                    "generation": managed_token.generation,
                    "binding_identity": list(managed_token.binding_identity),
                    "outputs": outputs,
                },
            )
            managed_settled = True
            payload["managed_tool_session"] = managed_envelope.to_dict()
            settlement = self._handoff_settlement_call(self.client.settle,
                task_id,
                lease_token,
                result=payload,
                outputs=outputs,
                effect=effect,
                attempt_id=attempt_id,
                fence=fence,
            )
            settled = True
            if capability_id == "vibecomfy.run" and self._vibecomfy_current_warmth_hint:
                # The hint becomes reusable only after the Runtime settlement
                # and managed-session observation both succeeded.
                self._vibecomfy_warmth_hint = self._vibecomfy_current_warmth_hint
            return settlement
        except HostCancelled:
            handle_guard_abort()
            if deadline_failed:
                return {"task_id": task_id, "status": "failed", "deadline_exceeded": True}
            cancelled_attempt = True
            return {"task_id": task_id, "status": "cancelled", "cancelled": True}
        except Exception as exc:
            # A cancellation/lease-loss race must not be turned into a second
            # runtime failure after child work has been contained.
            if cancelled():
                handle_guard_abort()
                if deadline_failed:
                    return {"task_id": task_id, "status": "failed", "deadline_exceeded": True}
                cancelled_attempt = True
                return {"task_id": task_id, "status": "cancelled", "cancelled": True}
            if isinstance(exc, StorageEnvelopeError):
                storage_failure_receipt = dict(exc.diagnostic)
            failure_result = self._handoff_settlement_call(self.client.fail,
                task_id,
                lease_token,
                exc.to_envelope() if isinstance(exc, ProviderFailure) else str(exc),
                retryable=False,
                attempt_id=attempt_id,
                fence=fence,
                failure_diagnostic=storage_failure_receipt or evidence_failure_receipt,
            )
            if isinstance(exc, ProviderFailure):
                # Runtime has settled this rejection and owns any next
                # attempt. Do not enter the host's transport-error backoff.
                return failure_result
            raise
        finally:
            # Keep the lease pump alive through harvesting, upload, and
            # settlement; only stop it once the claimed attempt is terminal.
            if child_bridge is not None:
                child_bridge.revoke()
                with self._process_lock:
                    self._child_bridges.discard(child_bridge)
            if pump_stop is not None:
                pump_stop.set()
            if pump_thread is not None:
                pump_thread.join(timeout=2)
            cleanup_errors: list[str] = []
            cleanup_receipt: dict[str, Any] = {
                "path": str(root),
                "intended_disposition": (
                    "caller_owned"
                    if self.attempt_root is not None
                    else "retained"
                    if keep_attempt
                    else "deleted"
                ),
                "status": "pending",
            }
            if network_broker is not None:
                try:
                    network_broker.stop()
                except Exception as exc:
                    cleanup_errors.append(f"network broker: {exc}")
            if managed_token is not None and not managed_settled:
                if deadline_exceeded or cancelled_attempt or cancel_signal.is_set() or self._shutdown.is_set():
                    try:
                        self.managed_tool_session.cancel(
                            managed_token,
                            outcome="confirmed",
                        )
                    except Exception as exc:
                        cleanup_errors.append(f"managed cancellation: {exc}")
                        # A missing or stale token is already a fail-closed
                        # condition; release below preserves the poisoned slot.
                        try:
                            self.managed_tool_session.fence(reason="cancel_unconfirmed")
                        except Exception as fence_exc:
                            cleanup_errors.append(f"managed fence: {fence_exc}")
                else:
                    try:
                        self.managed_tool_session.fence(reason="task_failed")
                    except Exception as exc:
                        cleanup_errors.append(f"managed fence: {exc}")
            if managed_opened:
                retain_persistent_session = (
                    (
                        isinstance(managed_adapter, _ManagedVibeSessionAdapter)
                        or isinstance(managed_adapter, _ManagedWanChildAdapter)
                    )
                    and managed_settled
                    and settled
                )
                if not retain_persistent_session:
                    if capability_id == "vibecomfy.run":
                        self._vibecomfy_warmth_hint = None
                        self._vibecomfy_current_warmth_hint = None
                        self._vibecomfy_requested_warmth_hint = None
                    try:
                        self.managed_tool_session.release(
                            reason=(
                                "task_settled"
                                if managed_settled
                                else "task_cancelled"
                                if cancelled_attempt
                                else "task_failed"
                            )
                        )
                    except Exception as exc:
                        cleanup_errors.append(f"managed release: {exc}")
            if capability_id == "vibecomfy.run" and not settled:
                self._vibecomfy_warmth_hint = None
                self._vibecomfy_current_warmth_hint = None
                self._vibecomfy_requested_warmth_hint = None
            if capability_id == "generation.generate_image_codex":
                # A force-killed child cannot run its credential finally.
                # Scrub before retaining any attempt evidence as well.
                try:
                    (root / "codex-home" / "auth.json").unlink(missing_ok=True)
                except OSError as exc:
                    cleanup_errors.append(f"Codex credential cleanup: {exc}")
            # An unverified session release may still own a writer. Latch
            # before deciding whether the attempt can be deleted, sharing the
            # scene process-census retention boundary.
            if cleanup_errors:
                self._latch_cleanup_uncertainty("; ".join(cleanup_errors))
            if keep_attempt:
                try:
                    retained_exists = _strict_root_exists(root)
                except Exception as exc:
                    cleanup_errors.append(f"retained observation: {exc}")
                    retained_exists = False
                if not retained_exists:
                    cleanup_errors.append(f"retained attempt disappeared: {root}")
                else:
                    try:
                        retained_bytes = self.execution_policy.evidence_bytes(
                            root,
                            immutable_inputs=immutable_input_baseline,
                        )
                        cleanup_receipt.update(
                            {
                                "status": "retained",
                                "observed_exists": True,
                                "bytes": retained_bytes,
                            }
                        )
                    except Exception as exc:
                        cleanup_errors.append(f"retained evidence: {exc}")
            elif self.attempt_root is None:
                try:
                    self._cleanup_ephemeral_attempt_or_latch(root)
                    cleanup_receipt.update({"status": "deleted", "observed_absent": True})
                except Exception as exc:
                    cleanup_errors.append(f"attempt root: {exc}")
                    cleanup_receipt.update({"status": "uncertain", "observed_absent": False})
            else:
                try:
                    observed_exists = _strict_root_exists(root)
                except Exception as exc:
                    cleanup_errors.append(f"caller-owned observation: {exc}")
                    observed_exists = False
                cleanup_receipt.update(
                    {"status": "caller_owned", "observed_exists": observed_exists}
                )
                if not observed_exists:
                    cleanup_errors.append(f"caller-owned attempt disappeared: {root}")
            if cleanup_errors:
                cleanup_receipt.update({"status": "uncertain", "errors": list(cleanup_errors)})
                self._latch_cleanup_uncertainty("; ".join(cleanup_errors))
            self._last_cleanup_receipt = cleanup_receipt
            self._publish_evidence_status()
            if cleanup_errors:
                raise HostError("owned cleanup incomplete: " + "; ".join(cleanup_errors))
            self._record_native_cpu_handoff(
                self.managed_tool_session.current_adapter, managed_token,
                managed_settled=managed_settled,
                runtime_settled=settled and (pump_thread is None or not pump_thread.is_alive()),
            )

    def _publish_evidence_status(self) -> None:
        """Expose the live evidence charge for ``astrid doctor``; never fails a task."""
        if self.evidence_status_path is None:
            return
        # Shared slots serialize the measured snapshot and its atomic write,
        # so an older sample cannot overwrite a newer charge observation.
        with self._evidence_observation_lock:
            budget = self.execution_policy.evidence_budget
            try:
                write_evidence_status(
                    self.evidence_status_path,
                    {
                        "pid": os.getpid(),
                        **self._evidence_observation_binding,
                        "observation_kind": "measured_live_evidence_budget",
                        **budget.live_observation(),
                        "cap_bytes": self.execution_policy.evidence_cap_bytes,
                        "updated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    },
                )
            except OSError:
                return

    def cancel_task(
        self,
        task_id: str,
        *,
        attempt_id: str | None = None,
        fence: int | None = None,
        lease_id: str | None = None,
        require_fence: bool = False,
    ) -> Any:
        """Propagate cancellation, requiring identity for orphan reclaim."""
        operation = self._client_operation("cancel")
        if attempt_id is None or fence is None:
            return operation(task_id)
        try:
            return operation(
                task_id,
                attempt_id=attempt_id,
                fence=int(fence),
                lease_id=lease_id,
            )
        except TypeError as exc:
            if require_fence:
                raise HostError("runtime cancellation lacks an attempt/fence operation") from exc
            return operation(task_id)

    def _report_blocked_claim(self, claim: Any, capability: str) -> None:
        """Log a queued task that cannot run, once per task and reason."""
        task = _claim_field(claim, "task")
        task_id = str(_claim_field(task, "task_id") or "") if task is not None else ""
        reason = str(_claim_field(claim, "waiting_reason") or "")
        key = (task_id, reason)
        if key in self._reported_blocked_claims:
            return
        if len(self._reported_blocked_claims) >= 1024:
            self._reported_blocked_claims.clear()
        self._reported_blocked_claims.add(key)
        admitted = _claim_field(task, "capability_digest") if task is not None else None
        registered = (
            self.capabilities[capability].capability_digest
            if capability in self.capabilities
            else None
        )
        detail = ""
        if reason == "capability_unavailable" and admitted and registered and admitted != registered:
            detail = (
                f"; it was admitted against {admitted} but this host serves {registered} "
                "(the capability changed since admission). Cancel it and submit it again"
            )
        print(
            f"generic host: queued task {task_id} ({capability}) cannot be claimed: "
            f"{reason}{detail}. Other capabilities stay claimable.",
            file=sys.stderr,
            flush=True,
        )

    def claim_once(self) -> Mapping[str, Any] | None:
        """Run one claim cycle while publishing host-side quiescence counters."""
        thread_id = threading.get_ident()
        try:
            with self._handoff_condition:
                if self._handoff_claim_gate_closed or not self._handoff_observation_known:
                    return None
            return self._claim_once_impl(thread_id)
        finally:
            self._handoff_leave_attempt(thread_id)

    def _claim_once_impl(self, handoff_thread_id: int) -> Mapping[str, Any] | None:
        """Claim and execute one queued task through the generated boundary."""
        if self._cleanup_uncertain:
            raise self._cleanup_block_error()
        if self._shutdown.is_set():
            return None
        registration_error = self._registration_admission_reason()
        if registration_error:
            raise HostError(registration_error)
        claim_next = self._client_operation("claim_next")
        # Keep dynamic readiness fresh on every poll, but do not rewalk the
        # installed native tree when the Runtime has no work. The leased
        # selected task repeats FULL preflight inside fenced admission.
        if not self.capabilities:
            self.discover()
        ready_records = self._preflight(full_remotion=False)
        try:
            self.execution_policy.assert_budget_available()
        except ExecutionGuardError as exc:
            raise HostError(str(exc)) from exc
        capability_ids = sorted(
            record.id
            for record in ready_records
            if record.ready and not _is_withdrawn(record)
        )
        if not capability_ids:
            return None
        claim_target = _configured_claim_target()
        if not self._handoff_enter_claim_rpc():
            return None
        claim = None
        claimed_attempt = False
        try:
            excluded: set[str] = set()
            claim = None
            for _ in range(_CLAIM_HEAD_OF_LINE_SKIPS + 1):
                candidates = [capability for capability in capability_ids if capability not in excluded]
                if not candidates:
                    return None
                registration_error = self._registration_admission_reason()
                if registration_error:
                    raise HostError(registration_error)
                claim = claim_next(
                    executor_id=self.executor_id,
                    capability_ids=candidates,
                    idempotency_key=f"claim-{self.executor_id}-{time.time_ns()}",
                    target=claim_target,
                )
                if claim is None:
                    return None
                blocked = _claim_blocked_capability(claim)
                if blocked is None or blocked in excluded:
                    break
                self._report_blocked_claim(claim, blocked)
                excluded.add(blocked)
            claimed_attempt = claim is not None and not (
                _claim_field(claim, "waiting_reason") and not _claim_field(claim, "attempt_id")
            )
        finally:
            self._handoff_leave_claim_rpc(handoff_thread_id, claimed=claimed_attempt)
        if claim is None or (_claim_field(claim, "waiting_reason") and not _claim_field(claim, "attempt_id")):
            return None
        claim_data = dict(claim) if isinstance(claim, Mapping) else {
            "attempt_id": getattr(claim, "attempt_id", None),
            "task_id": getattr(claim, "task_id", None),
            "lease_id": getattr(claim, "lease_id", None),
            "fence": getattr(claim, "fence", None),
            "runtime_epoch": getattr(claim, "runtime_epoch", None),
            "executor_id": getattr(claim, "executor_id", None),
            "input_object_ids": list(getattr(claim, "input_object_ids", ()) or ()),
            "spec": getattr(claim, "spec", None),
            "project_id": getattr(claim, "project_id", None),
            "expected_effect": getattr(claim, "expected_effect", None),
            "generation_intent": getattr(claim, "generation_intent", None),
            "execution_request": getattr(claim, "execution_request", None),
            "execution_binding": getattr(claim, "execution_binding", None),
            "claim_authority": getattr(claim, "claim_authority", None),
            "queued_at": getattr(claim, "queued_at", None),
            "admitted_at": getattr(claim, "admitted_at", None),
            "created_at": getattr(claim, "created_at", None),
        }
        if not claim_data.get("task_id"):
            raise HostError("generated claim operation returned no task_id")
        task_id = str(claim_data["task_id"])
        lease_id = str(claim_data.get("lease_id") or "")
        attempt_id = str(claim_data.get("attempt_id") or "")
        fence = claim_data.get("fence")
        if not lease_id or not attempt_id or fence is None:
            raise HostError("generated claim operation returned incomplete lease identity")
        # Claim ownership immediately.  All subsequent task decoding and
        # provider preparation runs under this attempt's lease; a preparation
        # error must reach run_task's fenced failure path rather than escaping
        # claim_once and waiting for the lease to expire.
        self.client.heartbeat(
            task_id,
            lease_id,
            attempt_id=attempt_id,
            fence=int(fence),
        )
        task = self._client_operation("task")(task_id)
        if isinstance(task, Mapping):
            task_data = dict(task.get("task", task))
        else:
            task_data = {
                "id": getattr(task, "task_id", task_id),
                "run_id": getattr(task, "run_id", None),
                "attempt_id": getattr(task, "attempt_id", None),
                "capability": getattr(task, "capability_id", ""),
                "capability_id": getattr(task, "capability_id", None),
                "capability_digest": getattr(task, "capability_digest", None),
                "project_id": getattr(task, "project_id", None),
                "runtime_epoch": getattr(task, "runtime_epoch", claim_data.get("runtime_epoch")),
                "input_object_ids": getattr(task, "input_object_ids", None),
                "spec": getattr(task, "spec", None),
                "expected_effect": getattr(task, "expected_effect", claim_data.get("expected_effect")),
                "generation_intent": getattr(task, "generation_intent", claim_data.get("generation_intent")),
                "execution_request": getattr(task, "execution_request", None),
                "execution_binding": getattr(task, "execution_binding", claim_data.get("execution_binding")),
                "storage_estimate": getattr(task, "storage_estimate", claim_data.get("storage_estimate")),
                "required_facts": getattr(task, "required_facts", claim_data.get("required_facts")),
                "queued_at": getattr(task, "queued_at", None),
                "admitted_at": getattr(task, "admitted_at", None),
                "created_at": getattr(task, "created_at", None),
            }
        def fail_claim_handoff(reason: str) -> None:
            try:
                self._handoff_settlement_call(self.client.fail,
                    task_id, lease_id, reason, retryable=False,
                    attempt_id=attempt_id, fence=int(fence),
                )
            except Exception as exc:
                raise HostError("claim handoff failure could not be recorded") from exc
            raise HostError(reason)

        if task_data.get("id") not in (None, task_id):
            fail_claim_handoff("claimed task_id disagrees with task read")
        # Validate both envelopes independently before retaining the claim.
        # Falsy malformed requests are present-invalid, never a fallback.
        try:
            claim_request = _execution_contract(
                claim_data, runtime_session_id=self.runtime_state.get("runtime_session_id"))
            read_request = _execution_contract(
                task_data, runtime_session_id=self.runtime_state.get("runtime_session_id"))
        except Exception as exc:
            fail_claim_handoff(str(exc))
        if _canonical_digest(claim_request) != _canonical_digest(read_request):
            fail_claim_handoff("claim and task read disagree on execution_request")
        if claim_request is not None:
            claim_ids = claim_data.get("input_object_ids")
            read_ids = task_data.get("input_object_ids")
            if claim_ids != read_ids:
                fail_claim_handoff("claim and task read disagree on input_object_ids")
        claim_binding = claim_data.get("execution_binding") or claim_data.get("placement_binding") or claim_data.get("binding")
        read_binding = task_data.get("execution_binding") or task_data.get("placement_binding") or task_data.get("binding")
        if claim_binding is not None and read_binding is not None and claim_binding != read_binding:
            fail_claim_handoff("claim and task read disagree on execution binding")
        task_data.update(
            {
                "id": task_id,
                "capability": task_data.get("capability", task_data.get("capability_id")),
                "attempt_id": claim_data.get("attempt_id"),
                "lease_id": claim_data.get("lease_id"),
                "executor_id": claim_data.get("executor_id") or getattr(self, "executor_id", None),
                "fence": claim_data.get("fence"),
            }
        )
        if claim_data.get("project_id") is not None:
            task_data["project_id"] = claim_data["project_id"]
        if claim_data.get("runtime_epoch") is not None:
            task_data["runtime_epoch"] = claim_data["runtime_epoch"]
        if claim_binding is not None:
            task_data["execution_binding"] = claim_binding
        for timestamp_key in ("queued_at", "admitted_at", "created_at"):
            if claim_data.get(timestamp_key) is not None:
                task_data[timestamp_key] = claim_data[timestamp_key]
        if claim_data.get("input_object_ids") is not None:
            task_data["input_object_ids"] = claim_data["input_object_ids"]
        # The claim response is the execution snapshot.  Preserve it over
        # the convenience task read so a worker cannot accidentally execute a
        # later/forked spec (and so claim-to-execute is a single immutable
        # handoff).
        if claim_data.get("spec") is not None:
            task_data["spec"] = claim_data["spec"]
        if claim_request is not None:
            task_data["execution_request"] = claim_request
        if claim_data.get("expected_effect") is not None:
            task_data["expected_effect"] = claim_data["expected_effect"]
        if claim_data.get("generation_intent") is not None:
            task_data["generation_intent"] = claim_data["generation_intent"]
        provider_route_grant = (
            task_data.get("provider_route_grant")
            or (
                task_data.get("spec", {}).get("provider_route_grant")
                if isinstance(task_data.get("spec"), Mapping)
                else None
            )
            or self._pending_provider_grants.pop(task_id, None)
        )
        capability_record = self.capabilities.get(str(task_data.get("capability")))
        if (
            provider_route_grant is None
            and capability_record is not None
            and capability_record.adapter.family == "provider"
            and capability_record.definition.isolation.network
        ):
            try:
                provider_route_grant = self.request_provider_route_grant({"task": task_data})
            except Exception as exc:
                try:
                    self._handoff_settlement_call(self.client.fail,
                        task_id,
                        lease_id,
                        str(exc),
                        retryable=False,
                        attempt_id=attempt_id,
                        fence=int(fence),
                    )
                except Exception as runtime_exc:
                    raise HostError(
                        "provider route preparation failed and could not be recorded"
                    ) from runtime_exc
                raise HostError(f"provider route preparation failed: {exc}") from exc
        if self._shutdown.is_set():
            self.client.fail(task_id, lease_id, "host shutdown before execution",
                             retryable=False, attempt_id=attempt_id, fence=int(fence))
            return None
        return self.run_task(
            {"task": task_data},
            lease_token=lease_id,
            attempt_id=attempt_id,
            fence=int(fence),
            provider_route_grant=provider_route_grant,
        )

    def run(self, *, once: bool = False, poll_seconds: float = 1.0, max_tasks: int | None = None) -> list[Mapping[str, Any]]:
        """Run the bounded worker claim loop; ``once`` is the test-friendly form."""
        if not once and self.max_concurrency > 1:
            return self._run_claim_slots(poll_seconds=poll_seconds, max_tasks=max_tasks)
        results: list[Mapping[str, Any]] = []
        consecutive_claim_failures = 0
        while not self._shutdown.is_set() and (max_tasks is None or len(results) < max_tasks):
            try:
                if time.monotonic() >= self._registration_refresh_deadline:
                    self._renew_executor_registration()
                result = self.claim_once()
            except Exception as exc:
                # ``--once`` is a diagnostic/test surface and must preserve the
                # exact claim failure for its caller.  The registered daemon,
                # however, is a durable worker: a transient coordinator fault
                # must not tear down readiness and force every launcher-backed
                # read to restart the host.  Back off exponentially, cap the
                # delay, and log only the first and power-of-two failures so a
                # sustained outage cannot fill the disk with traceback spam.
                if once:
                    raise
                consecutive_claim_failures += 1
                if consecutive_claim_failures == 1 or not (
                    consecutive_claim_failures & (consecutive_claim_failures - 1)
                ):
                    print(
                        "generic host claim failed "
                        f"({consecutive_claim_failures} consecutive): "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                base_delay = max(0.05, float(poll_seconds))
                delay = min(30.0, base_delay * (2 ** min(consecutive_claim_failures - 1, 10)))
                self._shutdown.wait(delay)
                continue
            consecutive_claim_failures = 0
            if result is not None:
                results.append(result)
                if once:
                    break
                continue
            if once:
                break
            if self._shutdown.is_set():
                break
            self._shutdown.wait(max(0.0, float(poll_seconds)))
        return results

    def _new_claim_slot(self) -> GenericPackHost:
        """Retain session warmth in a slot without sharing attempt custody."""
        fork = getattr(self.client, "fork_for_host_slot", None)
        if not callable(fork):
            raise HostError("parallel serving requires a client with private host-slot transport")
        slot = GenericPackHost(
            pack_roots=list(self.pack_roots), client=fork(), executor_id=self.executor_id,
            max_concurrency=self.max_concurrency, attempt_base=self.attempt_base,
            capability_matrix=self.capability_matrix_path, credential_source=self.credential_source,
            source_inventory_identity=self.source_inventory_identity,
            boot_manifest_path=self.boot_manifest_path, boot_manifest_hash=self.boot_manifest_hash,
            execution_policy=self.execution_policy,
            confinement_policy=None,
            remotion_closure_binding=self._remotion_closure_binding,
        )
        # Records are replaced by preflight, never modified in place. The
        # aggregate evidence budget is deliberately shared and already locked.
        slot._attempt_policy_factory = self._attempt_policy_factory
        slot._executable_admission = self._executable_admission
        slot.capabilities = dict(self.capabilities)
        slot.source_epoch = self.source_epoch
        slot.runtime_state = dict(self.runtime_state)
        with self._handoff_condition:
            slot._remotion_full_preflight = copy.deepcopy(self._remotion_full_preflight)
        slot._runtime_registration_fence = self._runtime_registration_fence
        slot._registration_refresh_blocked = self._registration_refresh_blocked
        slot._credential_source_is_explicit = self._credential_source_is_explicit
        slot.cleanup_latch_path = self.cleanup_latch_path
        slot.evidence_status_path = self.evidence_status_path
        slot._evidence_observation_binding = dict(self._evidence_observation_binding)
        slot._evidence_observation_lock = self._evidence_observation_lock
        slot._handoff_accounting_owner = self
        # Parallel slots own private attempts, but claim/handoff accounting
        # must remain visible on the coordinating host without a false zero.
        for name in ("_handoff_enter_claim_rpc", "_handoff_leave_claim_rpc",
                     "_handoff_leave_attempt", "_handoff_begin_direct_attempt",
                     "_handoff_settlement_call"):
            setattr(slot, name, getattr(self, name))

        return slot

    def _run_claim_slots(self, *, poll_seconds: float, max_tasks: int | None) -> list[Mapping[str, Any]]:
        """Dispatch at most one attempt per isolated, reusable serving slot.

        The coordinator alone renews registration. A slot is unavailable until
        claim_once has returned, including lease pumping, settlement and owned
        cleanup. In-flight claims reserve result allowance before acquisition.
        """
        if self.attempt_root is not None:
            raise HostError("parallel serving requires attempt_base or ephemeral attempt roots")
        if self._cleanup_uncertain:
            raise HostError("generic host admissions are blocked by cleanup uncertainty")
        if not self._claim_loop_lock.acquire(blocking=False):
            raise HostError("generic host claim loop is already running")
        completed: queue.SimpleQueue = queue.SimpleQueue()
        active: dict[int, threading.Thread] = {}
        results: list[Mapping[str, Any]] = []
        failures: dict[int, int] = {}
        retry_at: dict[int, float] = {}
        refresh_retry_at = 0.0
        refresh_failures = 0
        slots: tuple[GenericPackHost, ...] = ()
        poll_delay = max(0.01, float(poll_seconds))

        def serve(index: int) -> None:
            try:
                result = slots[index].claim_once()
            except BaseException as exc:
                completed.put((index, None, exc))
            else:
                completed.put((index, result, None))

        def backoff(count: int) -> float:
            return min(30.0, max(0.05, float(poll_seconds)) * (2 ** min(count - 1, 10)))

        def report_failure(exc: BaseException, count: int) -> None:
            if count == 1 or not (count & (count - 1)):
                print(f"generic host claim failed ({count} consecutive): "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

        try:
            if self._shutdown.is_set() or (max_tasks is not None and max_tasks <= 0):
                return results
            # Do not create more retained sessions than a finite run can use.
            count = min(self.max_concurrency, max_tasks) if max_tasks is not None else self.max_concurrency
            slots = tuple(self._new_claim_slot() for _ in range(count))
            self._claim_slots = slots
            while not self._shutdown.is_set():
                now = time.monotonic()
                while not completed.empty():
                    index, result, exc = completed.get()
                    active.pop(index).join()
                    slot = slots[index]
                    if slot._cleanup_uncertain:
                        self._latch_cleanup_uncertainty(slot._cleanup_uncertain_reason or "serving slot cleanup unresolved")
                        self._last_cleanup_receipt = slot.last_cleanup_receipt
                        raise HostError("serving slot cleanup is uncertain; admissions fenced") from exc
                    if exc is not None:
                        if not isinstance(exc, Exception):
                            raise exc
                        failures[index] = failures.get(index, 0) + 1
                        report_failure(exc, failures[index])
                        retry_at[index] = now + backoff(failures[index])
                    else:
                        failures[index] = 0
                        if result is not None:
                            results.append(result)
                        retry_at[index] = now if result is not None else now + poll_delay
                if max_tasks is not None and len(results) >= max_tasks:
                    break
                if now >= max(self._registration_refresh_deadline, refresh_retry_at):
                    try:
                        self._renew_executor_registration()
                    except Exception as exc:
                        refresh_failures += 1
                        report_failure(exc, refresh_failures)
                        refresh_retry_at = now + backoff(refresh_failures)
                    else:
                        refresh_failures = 0
                        refresh_retry_at = 0.0
                # A failed registration refresh blocks new claims while the
                # existing attempts continue their independent lease pumps.
                if refresh_failures == 0:
                    for index, slot in enumerate(slots):
                        if self._shutdown.is_set():
                            break
                        if index in active or now < retry_at.get(index, 0.0):
                            continue
                        if max_tasks is not None and len(results) + len(active) >= max_tasks:
                            break
                        slot.capabilities = dict(self.capabilities)
                        slot.source_epoch = self.source_epoch
                        slot.runtime_state = dict(self.runtime_state)
                        with self._handoff_condition:
                            slot._remotion_full_preflight = copy.deepcopy(self._remotion_full_preflight)
                        slot._runtime_registration_fence = self._runtime_registration_fence
                        slot._registration_refresh_blocked = self._registration_refresh_blocked
                        worker = threading.Thread(target=serve, args=(index,),
                                                  name=f"astrid-claim-slot-{index}", daemon=True)
                        active[index] = worker
                        worker.start()
                self._shutdown.wait(min(0.05, poll_delay))
            return results
        finally:
            # Shutdown fences each slot's bridge/session/process census, then
            # await one common deadline. Never discard a still-live owner.
            # Fence all admissions first: an earlier session close may block
            # while a sibling's outstanding claim returns with a new lease.
            for slot in slots:
                slot._shutdown.set()
            cleanup_errors = []
            for slot in slots:
                try:
                    slot.shutdown()
                except Exception as exc:
                    cleanup_errors.append(str(exc))
            deadline = time.monotonic() + 5.0
            for worker in active.values():
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
            if any(worker.is_alive() for worker in active.values()):
                cleanup_errors.append("serving slot did not terminate within five seconds")
            for slot in slots:
                if slot._cleanup_uncertain:
                    cleanup_errors.append("serving slot retained uncertain owned cleanup")
                    self._last_cleanup_receipt = slot.last_cleanup_receipt
            if not cleanup_errors:
                self._claim_slots = ()
            else:
                self._latch_cleanup_uncertainty("; ".join(cleanup_errors))
            self._claim_loop_lock.release()
            if cleanup_errors:
                raise HostError("claim-loop cleanup incomplete: " + "; ".join(cleanup_errors))

    def persistent_supervisor(
        self,
        state_path: str | Path,
        *,
        max_frame_bytes: int = 1024 * 1024,
    ):
        """Build the durable JSONL wrapper around this host's one-shot ABI.

        ``run`` remains the canonical claim loop.  This adapter is intentionally
        opt-in: JSONL ``run`` frames carry the already admitted task, lease,
        attempt, and fence, then delegate execution to :meth:`run_task`.
        """
        from astrid.core.execution.persistent_supervisor import PersistentJsonlSupervisor

        def launch(frame: Mapping[str, Any]) -> Any:
            task = frame.get("task")
            if not isinstance(task, Mapping):
                raise HostError("persistent run requires an admitted task object")
            lease_id = frame.get("lease_id", frame.get("lease_token"))
            return self.run_task(
                task,
                lease_token=str(lease_id),
                attempt_id=str(frame["attempt_id"]),
                fence=int(frame["fence"]),
                keep_attempt=bool(frame.get("keep_attempt", False)),
            )

        def request_runtime_cancel(frame: Mapping[str, Any]) -> Any:
            task = frame.get("task")
            task_id = frame.get("task_id")
            if task_id is None and isinstance(task, Mapping):
                task_id = task.get("id") or (
                    task.get("task", {}).get("id")
                    if isinstance(task.get("task"), Mapping)
                    else None
                )
            if not task_id:
                raise HostError("persistent cancellation requires task_id")
            return self.cancel_task(
                str(task_id),
                attempt_id=str(frame.get("attempt_id") or ""),
                fence=frame.get("fence"),
                lease_id=frame.get("lease_id"),
                require_fence=bool(frame.get("_reclaim")),
            )

        return PersistentJsonlSupervisor(
            state_path,
            launch=launch,
            cancel=request_runtime_cancel,
            reclaim=request_runtime_cancel,
            max_concurrency=self.max_concurrency,
            max_frame_bytes=max_frame_bytes,
        )


def _compose_cli_boot_manifest(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> tuple[Path, str]:
    """Read an existing manifest bound to the explicit support root."""
    if args.boot_manifest_path is None:
        parser.error("generic host requires explicit --boot-manifest-path")
    if args.support_root is None:
        parser.error("generic host requires explicit --support-root")
    try:
        from astrid.core._shared.boot_manifest import (
            load_boot_manifest_hash,
            normalize_sha256_digest,
            validate_manifest_path,
        )
        manifest_path = validate_manifest_path(args.boot_manifest_path, args.support_root)
        digest = load_boot_manifest_hash(
            manifest_path, support_root=args.support_root
        )
        if args.boot_manifest_hash is not None and normalize_sha256_digest(
            args.boot_manifest_hash, label="boot manifest hash"
        ) != normalize_sha256_digest(digest, label="stamped boot manifest hash"):
            raise RuntimeError("boot manifest hash does not match the existing manifest")
    except (OSError, RuntimeError) as exc:
        parser.error(f"boot manifest composition failed: {exc}")
    return manifest_path, digest


def _write_ready_marker(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically publish one host bootstrap outcome."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(dict(payload), sort_keys=True, default=_json_safe),
        encoding="utf-8",
    )
    temporary.replace(path)


_LOCAL_PREPARATION_VERSION = "runtime.local-execution-host/v1"
CURRENT_ROLES_VERSION = "runtime.local-execution-current-roles/v1"
ADMISSION_CLEANUP_VERSION = "runtime.local-execution-admission-cleanup/v1"
_HANDOFF_VERSION = "runtime.local-execution-handoff/v1"
_HANDOFF_BINDING_FIELDS = {
    "operation_id", "channel_id", "handoff_id", "nonce_digest", "deadline_unix_ms",
    "workspace_uuid", "profile_binding_digest", "launch_evidence_digest",
    "activation_record_digest", "executor_incarnation", "custody_scope",
    "original_owner_epoch", "new_owner_epoch", "new_owner",
    "credential_generation_digest", "original_roles", "intent_digest",
    "source_owner", "source_owner_epoch", "source_relay_reference", "current_roles",
}
_HANDOFF_ROLE_NAMES = {"relay", "host", "engine", "engine_listener"}
_HANDOFF_COMMAND_PAYLOADS = {
    "handoff_prepare": set(),
    "handoff_export_sealed": {"export_metadata", "seal_record", "sealed_record_digest"},
    "handoff_adopt": {"export_digest", "sealed_record_digest", "task_fence_digest", "relay_transfer_ack", "successor_authentication_digest"},
    "handoff_commit": {"new_runtime", "task_fence_digest", "relay_reference"},
    "resume_prepare": {"new_runtime", "task_fence_digest", "registered_state_digest"},
    "resume_commit": {"new_runtime", "task_fence_digest", "registered_state_digest"},
    "handoff_finalize": {"registered_state_digest", "task_fence_digest"},
    "handoff_abort": {"reason_code"},
    "handoff_report": set(),
}


def _handoff_canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HostError("handoff value is not canonical JSON") from exc


def _handoff_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_handoff_canonical(value)).hexdigest()


def _handoff_reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _handoff_reject_constant(_value):
    raise ValueError("non-finite JSON number")


def _handoff_actor_valid(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "pid", "uid", "birth_id", "audit_token_sha256", "audit_token_pidversion",
    }:
        return False
    return (
        isinstance(value.get("pid"), int) and not isinstance(value.get("pid"), bool) and value["pid"] > 0
        and isinstance(value.get("uid"), int) and not isinstance(value.get("uid"), bool) and value["uid"] >= 0
        and isinstance(value.get("birth_id"), str) and bool(value["birth_id"]) and len(value["birth_id"]) <= 256
        and isinstance(value.get("audit_token_sha256"), str) and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", value["audit_token_sha256"]))
        and isinstance(value.get("audit_token_pidversion"), int) and not isinstance(value.get("audit_token_pidversion"), bool) and value["audit_token_pidversion"] > 0
    )


def _handoff_role_valid(value: Any, role: str | None = None) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"version", "scope_root", "role", "generation", "target"}:
        return False
    return (
        value.get("version") == "runtime.role-custody-reference/v1"
        and isinstance(value.get("scope_root"), str) and Path(value["scope_root"]).is_absolute()
        and value.get("role") in _HANDOFF_ROLE_NAMES and (role is None or value.get("role") == role)
        and isinstance(value.get("generation"), int) and not isinstance(value.get("generation"), bool) and value["generation"] > 0
        and _handoff_actor_valid(value.get("target"))
    )


def _handoff_sha256(value: Any, *, prefixed: bool = True) -> bool:
    pattern = r"sha256:[0-9a-f]{64}" if prefixed else r"[0-9a-f]{64}"
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _handoff_runtime_binding_valid(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "endpoint", "protocol", "schema_digest", "runtime_epoch", "runtime_session_id",
        "runtime_instance_id", "coordinator_epoch",
    }:
        return False
    endpoint = value.get("endpoint")
    parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
    return (
        parsed is not None and parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
        and parsed.port is not None and not parsed.username and not parsed.password
        and isinstance(value.get("protocol"), str) and 0 < len(value["protocol"]) <= 256
        and _handoff_sha256(value.get("schema_digest"))
        and isinstance(value.get("runtime_epoch"), int) and not isinstance(value.get("runtime_epoch"), bool)
        and value["runtime_epoch"] > 0
        and all(isinstance(value.get(key), str) and 0 < len(value[key]) <= 256 for key in (
            "runtime_session_id", "runtime_instance_id", "coordinator_epoch",
        ))
    )


def _handoff_live_runtime_binding(owner: "GenericPackHost", *, client: Any = None,
                                 expected_endpoint: str | None = None) -> dict[str, Any] | None:
    client = owner.client if client is None else client
    if client is None:
        return None
    try:
        from banodoco_workspace_client.contract_metadata import PROTOCOL

        schema = getattr(client, "schema_digest", None)
        health_operation = getattr(client, "health", None)
        if not isinstance(schema, str) or not schema or not callable(health_operation):
            return None
        health = health_operation()
        if health is None:
            return None
        value = dict(health) if isinstance(health, Mapping) else {
            "status": getattr(health, "status", None), "protocol": getattr(health, "protocol", None),
            "schema_digest": getattr(health, "schema_digest", None),
            "runtime_epoch": getattr(health, "runtime_epoch", None),
            "runtime_session_id": getattr(health, "runtime_session_id", None),
            "runtime_instance_id": getattr(health, "runtime_instance_id", None),
            "instance_id": getattr(health, "instance_id", None),
            "coordinator_epoch": getattr(health, "coordinator_epoch", None),
        }
        if (value.get("status") != "ok" or value.get("protocol") != PROTOCOL
                or value.get("schema_digest") != schema):
            return None
        epoch = value.get("runtime_epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            return None
        instance_id = value.get("instance_id")
        runtime_instance_id = value.get("runtime_instance_id") or instance_id
        coordinator_epoch = value.get("coordinator_epoch") or runtime_instance_id
        if (not isinstance(runtime_instance_id, str) or not runtime_instance_id
                or (instance_id is not None and instance_id != runtime_instance_id)
                or (value.get("coordinator_epoch") is not None
                    and value.get("coordinator_epoch") != runtime_instance_id)):
            return None
        live = {"protocol": value.get("protocol"), "schema_digest": value.get("schema_digest"),
                "runtime_epoch": epoch, "runtime_session_id": value.get("runtime_session_id"),
                "runtime_instance_id": runtime_instance_id,
                "coordinator_epoch": coordinator_epoch}
    except Exception:
        return None
    endpoint = getattr(client, "endpoint", None)
    if (not isinstance(endpoint, str) or not endpoint
            or (expected_endpoint is not None and endpoint != expected_endpoint)):
        return None
    runtime = {
        "endpoint": endpoint.rstrip("/"), "protocol": live.get("protocol"),
        "schema_digest": live.get("schema_digest"), "runtime_epoch": live.get("runtime_epoch"),
        "runtime_session_id": live.get("runtime_session_id"),
        "runtime_instance_id": live.get("runtime_instance_id"),
        "coordinator_epoch": live.get("coordinator_epoch"),
    }
    return runtime if _handoff_runtime_binding_valid(runtime) else None


def _handoff_registered_state(owner: "GenericPackHost", activation: Mapping[str, Any], *, client: Any = None,
                              expected_endpoint: str | None = None) -> dict[str, Any] | None:
    """Snapshot the real, currently registered host state without coercion."""
    client = owner.client if client is None else client
    if (not isinstance(activation.get("executor_incarnation"), str)
            or not activation["executor_incarnation"] or len(activation["executor_incarnation"]) > 256
            or client is None or not owner._registered_state or not owner._registered_runtime_state):
        return None
    with owner._handoff_condition:
        if (not owner._handoff_observation_known or owner._handoff_registration_rpc_in_flight
                or owner._handoff_claim_rpc_in_flight or owner._handoff_active_attempts
                or owner._handoff_pending_settlements):
            return None
        registered = {key: dict(value) for key, value in owner._registered_state.items()}
        current = {
            key: {
                "capability_digest": record.capability_digest,
                "source_digest": record.source_digest,
                "dependency_digest": record.dependency_digest,
            }
            for key, record in owner.capabilities.items()
        }
        if registered != current:
            return None
        registered_runtime = dict(owner._registered_runtime_state)
        source_epoch = owner.source_epoch
    runtime = _handoff_live_runtime_binding(owner, client=client, expected_endpoint=expected_endpoint)
    if (runtime is None
            or any(registered_runtime.get(key) != runtime[key] for key in (
                "protocol", "schema_digest", "runtime_epoch", "runtime_session_id",
                "runtime_instance_id", "coordinator_epoch",
            ))
            or registered_runtime.get("source_epoch") != source_epoch
            or not isinstance(source_epoch, str) or not source_epoch or len(source_epoch) > 256):
        return None
    capabilities = {}
    for capability_id, values in registered.items():
        if (not isinstance(capability_id, str) or not capability_id
                or set(values) != {"capability_digest", "source_digest", "dependency_digest"}):
            return None
        cap_digest = values["capability_digest"]
        source_digest = values["source_digest"]
        dependency_digest = values["dependency_digest"]
        if (not _handoff_sha256(cap_digest)
                or not _handoff_sha256(source_digest, prefixed=False)
                or not _handoff_sha256(dependency_digest, prefixed=False)):
            return None
        capabilities[capability_id] = {
            "capability_digest": cap_digest,
            # Typed evidence carries algorithm labels; host storage and the
            # existing registration API retain their original raw hex form.
            "source_digest": "sha256:" + source_digest,
            "dependency_digest": "sha256:" + dependency_digest,
        }
    return {
        "executor_incarnation": activation["executor_incarnation"],
        "source_epoch": source_epoch,
        "capabilities": capabilities,
        "runtime": runtime,
    }


def _handoff_historical_registration(owner: "GenericPackHost", activation: Mapping[str, Any],
                                     sealed_state: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate unchanged local A registration against its verified sealed ACK.

    The live endpoint may already advertise B. Its Health is checked when B
    deliberately registers at commit, rather than being confused with A's
    historical registration during adoption. Callers hold the claim condition
    again through durable adoption publication.
    """
    if (not isinstance(sealed_state, Mapping)
            or set(sealed_state) != {"executor_incarnation", "source_epoch", "capabilities", "runtime"}
            or not _handoff_runtime_binding_valid(sealed_state.get("runtime"))):
        return None
    runtime = sealed_state["runtime"]
    with owner._handoff_condition:
        endpoint = getattr(owner.client, "endpoint", None)
        snapshot = owner._handoff_quiescence_locked()
        if (snapshot["observation_status"] != "known" or snapshot["claim_gate_closed"] is not True
                or any(snapshot[key] != 0 for key in ("claim_rpc_in_flight", "active_attempts",
                                                       "pending_settlements", "registration_rpc_in_flight"))
                or owner._handoff_registration_authorized_thread is not None
                or owner._handoff_registration_authorized_id is not None
                or owner.client is None or not owner._registered_state or not owner._registered_runtime_state
                or not isinstance(endpoint, str) or endpoint.rstrip("/") != runtime["endpoint"]
                or getattr(owner.client, "schema_digest", None) != runtime["schema_digest"]
                or sealed_state["executor_incarnation"] != activation.get("executor_incarnation")
                or any(owner._registered_runtime_state.get(key) != runtime[key] for key in (
                    "protocol", "schema_digest", "runtime_epoch", "runtime_session_id",
                    "runtime_instance_id", "coordinator_epoch"))
                or owner._registered_runtime_state.get("source_epoch") != owner.source_epoch
                or sealed_state["source_epoch"] != owner.source_epoch
                or not isinstance(owner.source_epoch, str) or not 0 < len(owner.source_epoch) <= 256):
            return None
        current = {key: {"capability_digest": record.capability_digest,
                         "source_digest": record.source_digest,
                         "dependency_digest": record.dependency_digest}
                   for key, record in owner.capabilities.items()}
        if current != owner._registered_state:
            return None
        capabilities = {}
        for key, values in current.items():
            if (not isinstance(key, str) or not key
                    or not _handoff_sha256(values["capability_digest"])
                    or not _handoff_sha256(values["source_digest"], prefixed=False)
                    or not _handoff_sha256(values["dependency_digest"], prefixed=False)):
                return None
            capabilities[key] = {"capability_digest": values["capability_digest"],
                                 "source_digest": "sha256:" + values["source_digest"],
                                 "dependency_digest": "sha256:" + values["dependency_digest"]}
        expected = {"executor_incarnation": activation.get("executor_incarnation"),
                    "source_epoch": owner.source_epoch, "capabilities": capabilities, "runtime": dict(runtime)}
        return expected if expected == sealed_state else None


def _handoff_read_protected_json(scope: Path, name: str, *, limit: int = 1_048_576) -> dict[str, Any]:
    if (not scope.is_absolute() or scope.is_symlink() or Path(name).name != name
            or any(parent.is_symlink() for parent in scope.parents)):
        raise HostError("handoff evidence scope or filename is unsafe")
    scope_info = scope.stat(follow_symlinks=False)
    if (not scope.is_dir() or scope_info.st_uid != os.getuid() or scope_info.st_mode & 0o077):
        raise HostError("handoff evidence scope is not owner-only")
    path = scope / name
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size <= 0 or info.st_size > limit):
            raise HostError("handoff evidence file is not bounded owner-only state")
        chunks = bytearray()
        while len(chunks) <= limit:
            part = os.read(descriptor, min(65536, limit + 1 - len(chunks)))
            if not part:
                break
            chunks.extend(part)
        if len(chunks) > limit:
            raise HostError("handoff evidence file exceeds its bound")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(
            bytes(chunks).decode("utf-8"),
            object_pairs_hook=_handoff_reject_duplicate_pairs,
            parse_constant=_handoff_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HostError("handoff evidence file is malformed") from exc
    if not isinstance(value, dict):
        raise HostError("handoff evidence record must be an object")
    _handoff_canonical(value)
    return value


def _handoff_read_private_bytes(path: Path, *, limit: int) -> bytes:
    if not path.is_absolute() or path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise HostError("handoff protected file path is unsafe")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size <= 0 or info.st_size > limit):
            raise HostError("handoff protected file is not bounded owner-only state")
        chunks = bytearray()
        while len(chunks) <= limit:
            part = os.read(descriptor, min(65536, limit + 1 - len(chunks)))
            if not part:
                break
            chunks.extend(part)
        if len(chunks) > limit:
            raise HostError("handoff protected file exceeds its bound")
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _handoff_journal_base(value: Any, binding: Mapping[str, Any], binding_digest: str) -> bool:
    base = {"version", "writer_incarnation", "writer_owner_epoch", "writer_generation",
            "phase", "binding_digest", "binding", "entries"}
    optional = {"writer_transfer", "ownership", "sealed_export"}
    return (
        isinstance(value, Mapping)
        and base <= set(value) <= base | optional
        and value.get("version") == _HANDOFF_VERSION
        and _handoff_actor_valid(value.get("writer_incarnation"))
        and isinstance(value.get("writer_owner_epoch"), str) and bool(value["writer_owner_epoch"])
        and isinstance(value.get("writer_generation"), int) and not isinstance(value.get("writer_generation"), bool)
        and value["writer_generation"] > 0
        and value.get("binding_digest") == binding_digest and value.get("binding") == dict(binding)
        and isinstance(value.get("entries"), Mapping)
    )


def _handoff_journal_entry(value: Mapping[str, Any], handoff_id: str, command: str,
                           request_digest: str, binding_digest: str, *, pending: bool = False) -> bool:
    entries = value.get("entries")
    key = f"{handoff_id}:{command}"
    entry = entries.get(key) if isinstance(entries, Mapping) else None
    return (
        isinstance(entry, Mapping) and set(entry) == {"request_digest", "binding_digest", "reply"}
        and entry.get("request_digest") == request_digest and entry.get("binding_digest") == binding_digest
        and ((entry.get("reply") is None) if pending else isinstance(entry.get("reply"), Mapping))
    )


def _handoff_ownership_matches(value: Any, roles: Mapping[str, Any], relay_reference: Mapping[str, Any]) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("relay_reference") == dict(relay_reference)
        and value.get("current_roles") == dict(roles)
    )


def _handoff_runtime_writer_matches(value: Any, binding: Mapping[str, Any]) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("writer_incarnation") == binding.get("new_owner")
        and value.get("writer_owner_epoch") == binding.get("new_owner_epoch")
    )


def _handoff_transfer_writers_match(value: Any, binding: Mapping[str, Any]) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("from_writer") == binding.get("source_owner")
        and value.get("to_writer") == binding.get("new_owner")
    )


def _handoff_designation(scope: Path, role: str, expected: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        record = _handoff_read_protected_json(scope, f"designation-{role}.json", limit=262144)
    except (OSError, HostError, ValueError):
        return None
    if (set(record) != {"version", "role", "generation", "owner_epoch", "state", "actor", "target", "transitions", "digest"}
            or record.get("version") != "runtime.role-custody-designation/v1"
            or record.get("role") != role or record.get("state") != "active"
            or not isinstance(record.get("generation"), int) or isinstance(record.get("generation"), bool)
            or not isinstance(record.get("actor"), Mapping) or not isinstance(record.get("target"), Mapping)
            or not isinstance(record.get("transitions"), list)
            or record.get("digest") != _handoff_digest({key: value for key, value in record.items() if key != "digest"})):
        return None
    reference = {
        "version": "runtime.role-custody-reference/v1", "scope_root": str(scope), "role": role,
        "generation": record["generation"],
        "target": {key: value for key, value in record["target"].items() if key != "audit_token_words"},
    }
    if reference != dict(expected):
        return None
    return record
_LOCAL_PROFILE_REQUIRED = {
    "profile_id", "workspace_uuid", "realm_root", "support_root", "machine_id",
    "worker_executable", "host_executable", "engine_executable", "engine_listener_executable",
    "engine_endpoint", "worker_artifact_digest", "host_artifact_digest", "engine_artifact_digest",
    "engine_listener_artifact_digest", "session_config_digest", "profile_revision", "profile_digest",
    "release_digest", "engine_launch",
}


def _validate_local_engine_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    if not _LOCAL_PROFILE_REQUIRED <= set(profile):
        raise HostError("local preparation lacks an explicitly selected profile")
    for name in ("workspace_uuid", "profile_id", "profile_revision", "machine_id"):
        if not isinstance(profile[name], str) or not profile[name]:
            raise HostError("selected local profile identity is invalid")
    for name in ("profile_digest", "release_digest", "session_config_digest", "engine_artifact_digest", "engine_listener_artifact_digest"):
        if not isinstance(profile[name], str) or not _ACTIVATION_DIGEST.fullmatch(profile[name]):
            raise HostError("selected local profile digest is invalid")
    launch = profile.get("engine_launch")
    fields = {"module", "session_root", "config", "source_revision", "source_content_digest", "listener_argv", "adapter_pins"}
    if not isinstance(launch, Mapping) or set(launch) != fields or launch.get("module") != "vibecomfy.commands.session":
        raise HostError("selected engine launch contract is invalid")
    root = Path(str(launch["session_root"]))
    if (not root.is_absolute() or root.is_symlink() or root.parent.name != "sessions" or root.parent.parent.name != "out"
            or any(part.is_symlink() for part in root.parents)):
        raise HostError("selected engine session root is invalid")
    config = launch["config"]
    endpoint = urlsplit(str(profile["engine_endpoint"]))
    if (not isinstance(config, Mapping) or endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1"
            or endpoint.port != config.get("port") or config.get("locality") != "managed_local_server"
            or config.get("warm_policy") != "auto" or config.get("cwd") != str(root.parents[2])
            or config.get("runtime_root") != str(root.parents[2]) or config.get("server_log_path") != str(root / "comfy.log")
            or not isinstance(config.get("ready_timeout_sec"), (int, float))
            or isinstance(config.get("ready_timeout_sec"), bool) or not 0 < config["ready_timeout_sec"] <= 900):
        raise HostError("selected engine configuration is invalid")
    config_bytes = json.dumps(dict(config), indent=2, sort_keys=True, allow_nan=False).encode("utf-8")
    if "sha256:" + hashlib.sha256(config_bytes).hexdigest() != profile["session_config_digest"]:
        raise HostError("selected engine configuration digest differs")
    pins = launch["adapter_pins"]
    if (not isinstance(pins, Mapping) or set(pins) != {"session_source_sha256", "spawn_sha256", "cleanup_sha256", "stop_sha256", "adapter_source_sha256"}
            or any(not isinstance(value, str) or not _ACTIVATION_DIGEST.fullmatch(value) for value in pins.values())):
        raise HostError("selected engine launch/cleanup artifact pins are incomplete")
    argv = launch["listener_argv"]
    if (not isinstance(argv, list) or not argv or any(not isinstance(value, str) or not value or "\0" in value for value in argv)
            or argv[0] != profile["engine_listener_executable"] or not Path(argv[0]).is_absolute()
            or not isinstance(launch["source_revision"], str) or not launch["source_revision"]
            or not _ACTIVATION_DIGEST.fullmatch(str(launch["source_content_digest"]))):
        raise HostError("selected listener argv or Vibe source pins are invalid")
    return json.loads(json.dumps(dict(profile), sort_keys=True, allow_nan=False))


class LocalExecutionPreparation:
    """Private preparation state; no client, claims or credential consumption."""

    def __init__(self, *, operation_id: str, channel_id: str, graph_factory: Callable | None = None):
        self.operation_id, self.channel_id = operation_id, channel_id
        self.graph_factory = graph_factory or _NativeEngineGraph
        self._intent: str | None = None
        self.graph: Any = None
        self.result: dict[str, Any] | None = None
        self.error: BaseException | None = None
        self._aborted = False
        self._request: dict[str, Any] | None = None
        self._abort_intent: str | None = None
        self._abort_ack: dict[str, Any] | None = None
        self._current_abort_intent: str | None = None
        self._current_abort_ack: dict[str, Any] | None = None
        self._current_abort_running = False
        self._admission_abort_intent: str | None = None
        self._admission_abort_ack: dict[str, Any] | None = None
        self._abort_dispatch_lock = threading.Lock()
        self.control_error: BaseException | None = None
        self._lock = threading.RLock()
        self._prepare_done = threading.Event()
        self._prepare_done.set()
        self._claim_host: GenericPackHost | None = None
        self._activation_grant: dict[str, Any] | None = None
        self._retained_relay_actor: Any | None = None
        self._handoff_binding: dict[str, Any] | None = None
        self._handoff_binding_digest: str | None = None
        self._handoff_phase = "owned"
        self._handoff_replies: dict[str, dict[str, Any]] = {}
        self._handoff_registered_state: dict[str, Any] | None = None
        self._handoff_runtime_binding: dict[str, Any] | None = None
        self._handoff_registered_state_digest: str | None = None
        self._handoff_current_roles: dict[str, Any] | None = None
        self._handoff_rebind_count = 0
        self._handoff_registration_attempted = False
        self._handoff_finalize_persistence_failed = False
        self._handoff_sealed_export: dict[str, Any] | None = None
        self._handoff_authority_snapshot: dict[str, Any] | None = None
        self._handoff_journal_path: Path | None = None
        self._handoff_lock_fd: int | None = None
        self._handoff_journal_ready = False

    def _acquire_handoff_journal(self, scope: Path) -> bool:
        if self._handoff_lock_fd is not None:
            return self._handoff_journal_ready
        lock_fd = None
        try:
            from fcntl import LOCK_EX, LOCK_NB, flock

            if not scope.is_absolute() or any(parent.is_symlink() for parent in scope.parents):
                raise HostError("host handoff custody scope contains a symlink")
            if not scope.exists():
                os.mkdir(scope, 0o700)
            scope_stat = scope.stat(follow_symlinks=False)
            if (not scope.is_dir() or scope.is_symlink() or scope_stat.st_uid != os.getuid()
                    or scope_stat.st_mode & 0o077):
                raise HostError("host handoff custody scope is not owner-only")
            journal = scope / "host-handoff-state.json"
            lock_path = scope / "host-handoff-state.lock"
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            lock_stat = os.fstat(lock_fd)
            if (not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1
                    or lock_stat.st_uid != os.getuid() or lock_stat.st_mode & 0o077):
                raise HostError("host handoff lock file is not owner-only")
            flock(lock_fd, LOCK_EX | LOCK_NB)
            if journal.exists() or journal.is_symlink():
                raise HostError("retained host handoff journal requires authenticated reconciliation")
            self._handoff_journal_path = journal
            self._handoff_lock_fd = lock_fd
            self._handoff_journal_ready = True
            return True
        except (OSError, HostError, ImportError):
            if lock_fd is not None:
                os.close(lock_fd)
            return False

    def attach_claim_host(self, host: "GenericPackHost") -> None:
        """Bind the private control receiver to the process's actual claim owner."""
        journal_ready = False
        if self._request is not None:
            journal_ready = self._acquire_handoff_journal(Path(str(self._request["custody_scope"])))
        with self._lock:
            if self._claim_host is not None and self._claim_host is not host:
                raise HostError("local execution control already has a claim owner")
            self._claim_host = host
            if not journal_ready:
                with host._handoff_condition:
                    host._handoff_claim_gate_closed = True
                    host._handoff_observation_known = False

    def retain_activation_grant(self, grant: Mapping[str, Any]) -> None:
        """Retain the activation accepted over the private Runtime channel."""
        if (not isinstance(grant, Mapping) or grant.get("version") != _ACTIVATION_VERSION
                or grant.get("operation_id") != self.operation_id
                or grant.get("channel_id") != self.channel_id
                or not isinstance(grant.get("executor_incarnation"), str)
                or not isinstance(grant.get("evidence_digest"), str)
                or not _ACTIVATION_DIGEST.fullmatch(grant["evidence_digest"])):
            raise HostError("accepted activation grant cannot be retained for handoff")
        accepted = json.loads(json.dumps(dict(grant), sort_keys=True, allow_nan=False))
        with self._lock:
            if self._activation_grant is not None and self._activation_grant != accepted:
                raise HostError("retained activation grant changed")
            self._activation_grant = accepted

    def _validate_retained_binding(self, binding: Mapping[str, Any]) -> bool:
        request = self._request
        grant = self._activation_grant
        if not isinstance(request, Mapping) or not isinstance(grant, Mapping):
            return False
        profile = request.get("profile")
        owner = request.get("runtime_owner")
        if not isinstance(profile, Mapping) or not isinstance(owner, Mapping):
            return False
        return (
            request.get("operation_id") == binding.get("operation_id") == self.operation_id
            and request.get("channel_id") == binding.get("channel_id") == self.channel_id
            and request.get("owner_epoch") == binding.get("original_owner_epoch")
            and request.get("custody_scope") == binding.get("custody_scope")
            and profile.get("workspace_uuid") == binding.get("workspace_uuid")
            and _handoff_digest(dict(profile)) == binding.get("profile_binding_digest")
            and grant.get("operation_id") == self.operation_id
            and grant.get("channel_id") == self.channel_id
            and grant.get("executor_incarnation") == binding.get("executor_incarnation")
            and grant.get("evidence_digest") == binding.get("launch_evidence_digest")
            and grant.get("host") == {"pid": os.getpid(), "birth_id": process_birth_identity()}
            and owner.get("runtime_instance_id") == binding.get("original_owner_epoch")
            and owner.get("coordinator_epoch") == binding.get("original_owner_epoch")
        )

    def _validated_pause_roles(self, binding: Mapping[str, Any]) -> dict[str, Any] | None:
        """Compile handoff custody without expanding the graph's cleanup scope."""
        from astrid.core.execution.custody_broker import AuthenticatedCleanupActor

        if not self._validate_retained_binding(binding) or self._retained_relay_actor is None:
            return None
        try:
            scope = Path(self._request["custody_scope"])
            delegated = self.known_custody_capabilities()
            requested = binding["current_roles"]
            if (set(delegated) != {"engine", "engine_listener"}
                    or requested != binding["original_roles"]
                    or binding["source_relay_reference"] != requested["relay"]
                    or binding["source_owner_epoch"] != self._request["owner_epoch"]):
                return None
            host_actor = AuthenticatedCleanupActor.current().verify()
            relay_actor = self._retained_relay_actor.verify()
            runtime_owner = self._request["runtime_owner"]
            source_owner = binding["source_owner"]
            if (any(runtime_owner.get(key) != source_owner.get(key) for key in ("pid", "uid", "birth_id"))
                    or {key: host_actor[key] for key in ("pid", "birth_id")} != self._activation_grant["host"]):
                return None
            compiled = {}
            for role in _HANDOFF_ROLE_NAMES:
                reference = delegated.get(role) if role in delegated else requested[role]
                if (not _handoff_role_valid(reference, role) or reference["scope_root"] != str(scope)
                        or reference != requested[role]):
                    return None
                designation = _handoff_designation(scope, role, reference)
                expected_actor = (source_owner if role == "relay" else
                                  relay_actor if role == "host" else host_actor)
                if (designation is None or designation["actor"] != expected_actor
                        or designation["owner_epoch"] != self._request["owner_epoch"]
                        or (role == "relay" and reference["target"] != relay_actor)
                        or (role == "host" and reference["target"] != host_actor)):
                    return None
                # Host/relay refs come from protected designation contents;
                # engine refs remain the exact graph-delegated references.
                compiled[role] = (dict(reference) if role in delegated else {
                    "version": "runtime.role-custody-reference/v1", "scope_root": str(scope),
                    "role": role, "generation": designation["generation"],
                    "target": {key: value for key, value in designation["target"].items()
                               if key != "audit_token_words"},
                })
            return json.loads(json.dumps(compiled, sort_keys=True, allow_nan=False))
        except (OSError, HostError, ValueError, KeyError, TypeError, RuntimeError):
            return None

    def _read_task_fence(self, binding: Mapping[str, Any], digest: str) -> dict[str, Any] | None:
        request, grant = self._request, self._activation_grant
        profile = request.get("profile") if isinstance(request, Mapping) else None
        if not isinstance(profile, Mapping) or not isinstance(grant, Mapping):
            return None
        support_root = profile.get("support_root")
        if not isinstance(support_root, str) or not Path(support_root).is_absolute():
            return None
        try:
            fence = _handoff_read_protected_json(Path(support_root), "local-execution-claim-fence.json")
            credential_file = Path(str(grant.get("credential_file", ""))).expanduser()
            commit_path = credential_file.with_suffix(".commit")
            commit = _handoff_read_private_bytes(commit_path, limit=65536)
        except (OSError, HostError, ValueError):
            return None
        expected_keys = {
            "version", "state", "workspace_uuid", "executor_incarnation", "credential_generation_digest",
            "operation_id", "handoff_id", "intent_digest", "source_owner_epoch", "target_owner_epoch",
            "fence_generation", "release_ack_digest",
        }
        commit_digest = "sha256:" + hashlib.sha256(commit).hexdigest()
        if (
            set(fence) != expected_keys or fence.get("version") != "runtime.local-execution-claim-fence/v1"
            or fence.get("state") != "held" or fence.get("workspace_uuid") != binding.get("workspace_uuid")
            or fence.get("executor_incarnation") != binding.get("executor_incarnation")
            or fence.get("credential_generation_digest") != binding.get("credential_generation_digest")
            or binding.get("credential_generation_digest") != commit_digest
            or fence.get("operation_id") != binding.get("operation_id")
            or fence.get("handoff_id") != binding.get("handoff_id")
            or fence.get("intent_digest") != binding.get("intent_digest")
            or fence.get("source_owner_epoch") != binding.get("source_owner_epoch")
            or fence.get("target_owner_epoch") != binding.get("new_owner_epoch")
            or not isinstance(fence.get("fence_generation"), int)
            or isinstance(fence.get("fence_generation"), bool) or fence["fence_generation"] < 1
            or fence.get("release_ack_digest") is not None or _handoff_digest(fence) != digest
        ):
            return None
        return fence

    def _verify_adoption_evidence(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        binding, payload = request["binding"], request["payload"]
        if (not self._validate_retained_binding(binding) or self._claim_host is None
                or self._retained_relay_actor is None):
            return None
        scope = Path(binding["custody_scope"])
        profile = self._request["profile"]
        if not isinstance(profile, Mapping):
            return None
        try:
            runtime = _handoff_read_protected_json(scope, "runtime-handoff-state.json")
            relay = _handoff_read_protected_json(scope, "relay-handoff-state.json")
            peer = self._retained_relay_actor.verify()
            fence = self._read_task_fence(binding, payload["task_fence_digest"])
        except Exception:
            return None
        binding_digest = _handoff_digest(binding)
        request_digest = _handoff_digest(request)
        handoff_id = binding["handoff_id"]
        source_relay = binding["source_relay_reference"]
        if (not _handoff_journal_base(runtime, binding, binding_digest)
                or not _handoff_journal_base(relay, binding, binding_digest)
                or runtime.get("phase") != "export_sealed" or relay.get("phase") != "export_sealed"
                or not isinstance(fence, Mapping) or not _handoff_journal_entry(
                    relay, handoff_id, "handoff_adopt", request_digest, binding_digest, pending=True)
                or peer != relay.get("writer_incarnation")
                or relay.get("writer_owner_epoch") != binding.get("original_owner_epoch")
                or peer != source_relay.get("target")):
            return None
        # Runtime starts its own adopt entry only after the host has returned;
        # an absent entry is the required pre-forward state.
        runtime_entries = runtime["entries"]
        if f"{handoff_id}:handoff_adopt" in runtime_entries:
            return None
        sealed = runtime.get("sealed_export")
        if not isinstance(sealed, Mapping) or set(sealed) != {"export_metadata", "seal_record", "sealed_record_digest"}:
            return None
        export_metadata, seal_record = sealed.get("export_metadata"), sealed.get("seal_record")
        if (not isinstance(export_metadata, Mapping) or not isinstance(seal_record, Mapping)
                or _handoff_digest(export_metadata) != payload.get("export_digest")
                or _handoff_digest(seal_record) != payload.get("sealed_record_digest")
                or sealed.get("sealed_record_digest") != payload.get("sealed_record_digest")
                or seal_record.get("export_digest") != payload.get("export_digest")
                or export_metadata.get("task_fence_digest") != payload.get("task_fence_digest")
                or seal_record.get("task_fence_digest") != payload.get("task_fence_digest")
                or export_metadata.get("handoff_id") != handoff_id
                or seal_record.get("handoff_id") != handoff_id
                or export_metadata.get("intent_digest") != binding.get("intent_digest")
                or seal_record.get("intent_digest") != binding.get("intent_digest")
                or export_metadata.get("credential_generation_digest") != binding.get("credential_generation_digest")
                or seal_record.get("credential_generation_digest") != binding.get("credential_generation_digest")
                or export_metadata.get("source_owner_epoch") != binding.get("source_owner_epoch")
                or seal_record.get("source_owner_epoch") != binding.get("source_owner_epoch")
                or seal_record.get("target_owner_epoch") != binding.get("new_owner_epoch")
                or export_metadata.get("source_relay_reference") != source_relay
                or seal_record.get("source_relay_reference") != source_relay
                or fence.get("fence_generation") < 1):
            return None
        pause_key = f"{handoff_id}:handoff_prepare"
        pause_entry = runtime_entries.get(pause_key)
        if (not isinstance(pause_entry, Mapping) or set(pause_entry) != {"request_digest", "binding_digest", "reply"}
                or pause_entry.get("binding_digest") != binding_digest or not isinstance(pause_entry.get("reply"), Mapping)
                or export_metadata.get("host_pause_ack_digest") != _handoff_digest(pause_entry["reply"])
                or seal_record.get("host_pause_ack_digest") != _handoff_digest(pause_entry["reply"])):
            return None
        export_entry = relay["entries"].get(f"{handoff_id}:handoff_export_sealed")
        try:
            host_record = _handoff_read_protected_json(scope, "host-handoff-state.json")
        except Exception:
            return None
        host_fields = {"version", "writer_incarnation", "writer_owner_epoch", "handoff_id", "command", "request_digest", "reply"}
        if (not isinstance(export_entry, Mapping) or set(export_entry) != {"request_digest", "binding_digest", "reply"}
                or not isinstance(export_entry.get("reply"), Mapping)
                or set(host_record) != host_fields
                or host_record.get("version") != "runtime.local-execution-handoff-journal/v1"
                or host_record.get("handoff_id") != handoff_id
                or host_record.get("command") != "handoff_export_sealed"
                or host_record.get("request_digest") != export_entry.get("request_digest")
                or host_record.get("reply") != export_entry.get("reply")
                or host_record.get("writer_owner_epoch") != binding.get("original_owner_epoch")):
            return None
        transfer, ownership = runtime.get("writer_transfer"), runtime.get("ownership")
        if (not isinstance(transfer, Mapping) or set(transfer) != {"state", "intent", "relay_ack"}
                or transfer.get("state") != "committed" or not isinstance(transfer.get("intent"), Mapping)
                or not isinstance(ownership, Mapping)
                or set(ownership) != {"owner", "owner_epoch", "relay_reference", "current_roles", "previous_projection_digest"}):
            return None
        intent = transfer["intent"]
        transition_id = "relay-transfer:" + _handoff_digest({
            "handoff_id": handoff_id, "intent_digest": binding["intent_digest"],
            "source_relay_reference": source_relay,
        })
        if (set(intent) != {"transition_id", "binding_digest", "from_writer", "from_epoch", "from_generation",
                            "to_writer", "to_epoch", "source_relay_reference"}
                or intent.get("transition_id") != transition_id or intent.get("binding_digest") != binding_digest
                or not _handoff_transfer_writers_match(intent, binding)
                or intent.get("from_epoch") != binding.get("source_owner_epoch")
                or not isinstance(intent.get("from_generation"), int) or isinstance(intent.get("from_generation"), bool)
                or intent["from_generation"] < 1
                or intent.get("to_epoch") != binding.get("new_owner_epoch")
                or intent.get("source_relay_reference") != source_relay
                or not _handoff_runtime_writer_matches(runtime, binding)
                or runtime.get("writer_owner_epoch") != intent.get("to_epoch")
                or runtime.get("writer_generation") != intent.get("from_generation") + 1
                or ownership.get("owner") != binding.get("new_owner")
                or ownership.get("owner_epoch") != binding.get("new_owner_epoch")
                or not _handoff_sha256(ownership.get("previous_projection_digest"))):
            return None
        relay_ack = transfer.get("relay_ack")
        if (not isinstance(relay_ack, Mapping) or set(relay_ack) != {"version", "transition_id", "role", "generation",
                                                                       "owner_epoch", "actor", "target"}
                or relay_ack.get("version") != "runtime.role-custody-designation/v1"
                or relay_ack.get("transition_id") != transition_id or relay_ack.get("role") != "relay"
                or relay_ack.get("generation") != source_relay.get("generation") + 1
                or relay_ack.get("owner_epoch") != binding.get("new_owner_epoch")
                or relay_ack.get("actor") != binding.get("new_owner")
                or relay_ack.get("target") != source_relay.get("target")
                or payload.get("relay_transfer_ack") != relay_ack):
            return None
        relay_scope = Path(source_relay["scope_root"])
        if relay_scope != scope:
            return None
        designation = _handoff_designation(relay_scope, "relay", {
            **dict(source_relay), "generation": source_relay["generation"] + 1,
        })
        if (designation is None or designation.get("owner_epoch") != binding.get("new_owner_epoch")
                or designation.get("actor") != binding.get("new_owner")
                or designation.get("target", {}).get("pid") != peer.get("pid")
                or designation.get("target", {}).get("birth_id") != peer.get("birth_id")):
            return None
        transitions = [entry for entry in designation["transitions"]
                       if isinstance(entry, Mapping) and entry.get("transition_id") == transition_id]
        expected_role_intent = {
            "transition_id": transition_id,
            "from_generation": source_relay["generation"],
            "requester": binding["source_owner"],
            "next_actor": binding["new_owner"],
            "owner_epoch": binding["new_owner_epoch"],
        }
        if (len(transitions) != 1 or set(transitions[0]) != {"transition_id", "intent", "ack"}
                or transitions[0].get("intent") != expected_role_intent
                or transitions[0].get("ack") != relay_ack):
            return None
        expected_auth_digest = _handoff_digest({
            "version": "runtime.local-execution-successor-auth/v1",
            "binding_digest": binding_digest,
            "successor_peer": binding["new_owner"],
            "relay_peer": source_relay["target"],
        })
        if payload.get("successor_authentication_digest") != expected_auth_digest:
            return None
        host_owner = self._claim_host
        sealed_registered = export_entry["reply"].get("registered_state")
        if (not isinstance(sealed_registered, Mapping)
                or pause_entry["reply"].get("registered_state") != sealed_registered
                or self._handoff_registered_state != sealed_registered
                or self._handoff_registered_state_digest != _handoff_digest(sealed_registered)
                or sealed_registered.get("executor_incarnation") != binding.get("executor_incarnation")
                or sealed_registered.get("runtime", {}).get("coordinator_epoch") != binding.get("source_owner_epoch")):
            return None
        with host_owner._handoff_condition:
            registered_client = host_owner.client
            activation_snapshot = json.loads(json.dumps(self._activation_grant))
            current_registered = _handoff_historical_registration(host_owner, activation_snapshot, sealed_registered)
        if current_registered is None:
            return None
        current_roles = dict(binding["current_roles"])
        current_roles["relay"] = {**dict(source_relay), "generation": source_relay["generation"] + 1}
        if not _handoff_ownership_matches(ownership, current_roles, current_roles["relay"]):
            return None
        for role, reference in current_roles.items():
            role_scope = Path(reference["scope_root"])
            if _handoff_designation(role_scope, role, reference) is None:
                return None
        return {"runtime": runtime, "relay": relay, "fence": fence, "registered_state": current_registered,
                "registered_state_digest": _handoff_digest(current_registered), "relay_ack": dict(relay_ack),
                "relay_reference": current_roles["relay"], "current_roles": current_roles,
                "authority_snapshot": {"writer_transfer": dict(transfer), "ownership": dict(ownership)},
                "sealed_export": dict(sealed),
                "registered_client": registered_client, "activation_snapshot": activation_snapshot}

    def _verify_export_evidence(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        binding, payload = request["binding"], request["payload"]
        if not self._validate_retained_binding(binding) or self._claim_host is None:
            return None
        registered = _handoff_registered_state(self._claim_host, self._activation_grant)
        if registered is None:
            return None
        try:
            scope = Path(binding["custody_scope"])
            runtime = _handoff_read_protected_json(scope, "runtime-handoff-state.json")
            relay = _handoff_read_protected_json(scope, "relay-handoff-state.json")
            fence = self._read_task_fence(binding, payload["seal_record"].get("task_fence_digest"))
            retained_relay = self._retained_relay_actor
            peer = retained_relay.verify() if retained_relay is not None else None
        except Exception:
            return None
        binding_digest = _handoff_digest(binding)
        request_digest = _handoff_digest(request)
        sealed = {key: payload[key] for key in ("export_metadata", "seal_record", "sealed_record_digest")}
        metadata, seal = sealed["export_metadata"], sealed["seal_record"]
        if (not _handoff_journal_base(runtime, binding, binding_digest)
                or not _handoff_journal_base(relay, binding, binding_digest)
                or not isinstance(metadata, Mapping) or not isinstance(seal, Mapping)
                or _handoff_digest(seal) != payload.get("sealed_record_digest")
                or _handoff_digest(metadata) != seal.get("export_digest")
                or payload.get("sealed_record_digest") != seal.get("sealed_record_digest", payload.get("sealed_record_digest"))
                or not isinstance(fence, Mapping)
                or runtime.get("phase") != "host_paused"
                or relay.get("phase") != "host_paused"
                or not _handoff_journal_entry(relay, binding["handoff_id"], "handoff_export_sealed",
                                              request_digest, binding_digest, pending=True)
                or not _handoff_journal_entry(runtime, binding["handoff_id"], "handoff_export_sealed",
                                              request_digest, binding_digest, pending=True)
                or peer is None or peer != relay.get("writer_incarnation")
                or relay.get("writer_owner_epoch") != binding.get("original_owner_epoch")):
            return None
        if (set(metadata) != {"version", "handoff_id", "intent_digest", "nonce_digest", "host_pause_ack_digest",
                              "task_fence_digest", "credential_generation_digest", "source_owner_epoch",
                              "source_relay_reference", "descriptor_identity_digest", "launch_evidence_digest"}
                or set(seal) != {"version", "handoff_id", "intent_digest", "nonce_digest", "export_digest",
                                 "host_pause_ack_digest", "task_fence_digest", "credential_generation_digest",
                                 "source_owner_epoch", "target_owner_epoch", "source_relay_reference",
                                 "successor_incarnation"}
                or metadata.get("version") != "runtime.local-execution-handoff-export/v1"
                or metadata.get("handoff_id") != binding.get("handoff_id")
                or metadata.get("intent_digest") != binding.get("intent_digest")
                or metadata.get("nonce_digest") != binding.get("nonce_digest")
                or metadata.get("credential_generation_digest") != binding.get("credential_generation_digest")
                or metadata.get("source_owner_epoch") != binding.get("source_owner_epoch")
                or metadata.get("source_relay_reference") != binding.get("source_relay_reference")
                or not _handoff_sha256(metadata.get("descriptor_identity_digest"))
                or metadata.get("launch_evidence_digest") != binding.get("launch_evidence_digest")
                or seal.get("version") != "runtime.local-execution-handoff-seal/v1"
                or seal.get("handoff_id") != binding.get("handoff_id")
                or seal.get("intent_digest") != binding.get("intent_digest")
                or seal.get("nonce_digest") != binding.get("nonce_digest")
                or seal.get("credential_generation_digest") != binding.get("credential_generation_digest")
                or seal.get("source_owner_epoch") != binding.get("source_owner_epoch")
                or seal.get("target_owner_epoch") != binding.get("new_owner_epoch")
                or seal.get("source_relay_reference") != binding.get("source_relay_reference")
                or seal.get("successor_incarnation") != binding.get("new_owner")
                or metadata.get("task_fence_digest") != _handoff_digest(fence)
                or seal.get("task_fence_digest") != _handoff_digest(fence)):
            return None
        pause_entry = runtime["entries"].get(f"{binding['handoff_id']}:handoff_prepare")
        if (not isinstance(pause_entry, Mapping) or not isinstance(pause_entry.get("reply"), Mapping)
                or not isinstance(pause_entry.get("request_digest"), str)
                or metadata.get("host_pause_ack_digest") != _handoff_digest(pause_entry["reply"])
                or seal.get("host_pause_ack_digest") != _handoff_digest(pause_entry["reply"])):
            return None
        try:
            host_record = _handoff_read_protected_json(scope, "host-handoff-state.json")
        except Exception:
            return None
        host_fields = {"version", "writer_incarnation", "writer_owner_epoch", "handoff_id", "command", "request_digest", "reply"}
        if (set(host_record) != host_fields
                or host_record.get("version") != "runtime.local-execution-handoff-journal/v1"
                or host_record.get("handoff_id") != binding.get("handoff_id")
                or host_record.get("command") != "handoff_prepare"
                or host_record.get("reply") != pause_entry.get("reply")
                or host_record.get("request_digest") != pause_entry.get("request_digest")
                or host_record.get("writer_owner_epoch") != binding.get("original_owner_epoch")
                or metadata.get("host_pause_ack_digest") != _handoff_digest(host_record["reply"])):
            return None
        if registered != host_record["reply"].get("registered_state"):
            return None
        return {"sealed_export": sealed, "registered_state": registered, "registered_state_digest": _handoff_digest(registered)}

    def _verify_finalization_context(self, request: Mapping[str, Any], *, require_registration: bool,
                                    observed_registration: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        binding, payload = request["binding"], request["payload"]
        if (not self._validate_retained_binding(binding) or self._claim_host is None
                or self._handoff_authority_snapshot is None or self._handoff_current_roles is None):
            return None
        fence_digest = payload.get("task_fence_digest")
        if fence_digest != self._handoff_sealed_export.get("seal_record", {}).get("task_fence_digest"):
            return None
        try:
            scope = Path(binding["custody_scope"])
            runtime = _handoff_read_protected_json(scope, "runtime-handoff-state.json")
            relay = _handoff_read_protected_json(scope, "relay-handoff-state.json")
            fence = self._read_task_fence(binding, fence_digest)
            peer = self._retained_relay_actor.verify() if self._retained_relay_actor is not None else None
        except Exception:
            return None
        binding_digest = _handoff_digest(binding)
        if (not _handoff_journal_base(runtime, binding, binding_digest)
                or not _handoff_journal_base(relay, binding, binding_digest)
                or runtime.get("writer_transfer") != self._handoff_authority_snapshot["writer_transfer"]
                or runtime.get("ownership") != self._handoff_authority_snapshot["ownership"]
                or not _handoff_runtime_writer_matches(runtime, binding)
                or runtime.get("phase") not in {"adopt_prepared", "rebind_committed", "resume_armed", "resumed"}
                or relay.get("phase") not in {"adopt_prepared", "rebind_committed", "resume_armed", "resumed"}
                or not isinstance(fence, Mapping) or peer is None
                or peer != relay.get("writer_incarnation")
                or relay.get("writer_owner_epoch") != binding.get("original_owner_epoch")):
            return None
        relay_ack = self._handoff_authority_snapshot["writer_transfer"]["relay_ack"]
        for role, reference in self._handoff_current_roles.items():
            if role == "relay":
                record = _handoff_designation(Path(reference["scope_root"]), role, reference)
                if (record is None or record.get("actor") != binding.get("new_owner")
                        or record.get("owner_epoch") != binding.get("new_owner_epoch")
                        or not any(isinstance(row, Mapping) and row.get("ack") == relay_ack
                                   for row in record.get("transitions", []))):
                    return None
            elif _handoff_designation(Path(reference["scope_root"]), role, reference) is None:
                return None
        current = (dict(observed_registration) if observed_registration is not None
                   else _handoff_registered_state(self._claim_host, self._activation_grant))
        if require_registration and (current is None or current != self._handoff_registered_state
                                     or _handoff_digest(current) != self._handoff_registered_state_digest):
            return None
        if self._handoff_phase in {"rebind_committed", "resume_armed", "resumed"}:
            if current is None or current != self._handoff_registered_state:
                return None
            if payload.get("registered_state_digest") != self._handoff_registered_state_digest:
                return None
            if (request["command"] != "handoff_finalize"
                    and payload.get("new_runtime") != self._handoff_runtime_binding):
                return None
        return {"runtime": runtime, "relay": relay, "fence": fence, "registered_state": current}

    def _persist_successful_phase(self, request: Mapping[str, Any], phase: str,
                                  evidence: Mapping[str, Any] | None) -> dict[str, Any]:
        if evidence is None or not isinstance(evidence.get("registered_state"), Mapping):
            error = "fence_unresolved" if request["command"] == "handoff_export_sealed" else "identity_unresolved"
            return self._handoff_reply(request, status="unresolved", error_code=error)
        previous = self._handoff_phase
        self._handoff_phase = phase
        candidate = self._handoff_reply(request, status="ok")
        if (candidate.get("registered_state") != evidence.get("registered_state")
                or not self._persist_handoff_reply(request, candidate)):
            self._handoff_phase = previous
            if self._claim_host is not None:
                with self._claim_host._handoff_condition:
                    self._claim_host._handoff_claim_gate_closed = True
                    self._claim_host._handoff_observation_known = False
            return self._handoff_reply(request, status="unresolved", error_code="custody_unresolved")
        self._handoff_registered_state = dict(evidence["registered_state"])
        self._handoff_registered_state_digest = _handoff_digest(self._handoff_registered_state)
        if isinstance(evidence.get("sealed_export"), Mapping):
            self._handoff_sealed_export = dict(evidence["sealed_export"])
        return candidate

    def _persist_adoption_phase(self, request: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
        owner = self._claim_host
        if owner is None:
            return self._handoff_reply(request, status="unresolved", error_code="registration_unresolved",
                                       observe_registered_state=False)
        with owner._handoff_condition:
            registered = _handoff_historical_registration(owner, self._activation_grant, evidence["registered_state"])
            if (registered is None or owner.client is not evidence["registered_client"]
                    or self._activation_grant != evidence["activation_snapshot"]
                    or self._handoff_phase != "export_sealed"
                    or self._handoff_registered_state != registered
                    or self._handoff_registered_state_digest != _handoff_digest(registered)
                    or self._aborted):
                return self._handoff_reply(request, status="unresolved", error_code="registration_unresolved",
                                           observe_registered_state=False)
            candidate = self._handoff_reply(request, status="ok", registered_state=registered,
                                            phase="adopt_prepared", observe_registered_state=False)
            # These four references come from the complete validated transfer
            # evidence, including the current relay generation, not the request.
            candidate["custody_capabilities"] = {role: dict(reference)
                                                  for role, reference in evidence["current_roles"].items()}
            if not self._persist_handoff_reply(request, candidate):
                owner._handoff_claim_gate_closed = True
                owner._handoff_observation_known = False
                return self._handoff_reply(request, status="unresolved", error_code="custody_unresolved",
                                           observe_registered_state=False)
            self._handoff_phase = "adopt_prepared"
            self._handoff_sealed_export = dict(evidence["sealed_export"])
            self._handoff_runtime_binding = dict(registered["runtime"])
            self._handoff_registered_state = registered
            self._handoff_registered_state_digest = evidence["registered_state_digest"]
            self._handoff_current_roles = dict(evidence["current_roles"])
            self._handoff_authority_snapshot = dict(evidence["authority_snapshot"])
            return candidate

    def _commit_rebind(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if self._handoff_phase != "adopt_prepared" or self._claim_host is None:
            return self._handoff_reply(request, status="conflict", error_code="phase_conflict")
        if self._handoff_registration_attempted or self._handoff_rebind_count:
            return self._handoff_reply(request, status="unresolved", error_code="registration_unresolved")
        context = self._verify_finalization_context(request, require_registration=False)
        runtime = request["payload"].get("new_runtime")
        live = _handoff_live_runtime_binding(self._claim_host)
        binding = request["binding"]
        if (context is None or not _handoff_runtime_binding_valid(runtime) or runtime != live
                or runtime.get("coordinator_epoch") != binding.get("new_owner_epoch")
                or request["payload"].get("task_fence_digest")
                    != self._handoff_sealed_export.get("seal_record", {}).get("task_fence_digest")
                or request["payload"].get("relay_reference") != self._handoff_current_roles.get("relay")):
            return self._handoff_reply(request, status="unresolved", error_code="fence_unresolved")
        self._handoff_registration_attempted = True
        owner = self._claim_host
        try:
            owner._handoff_authorize_registration(binding["handoff_id"])
            owner.register(deliberate=True)
        except Exception:
            return self._handoff_reply(request, status="unresolved", error_code="registration_unresolved")
        finally:
            owner._handoff_revoke_registration(binding["handoff_id"])
        registered = _handoff_registered_state(owner, self._activation_grant)
        if registered is None or registered.get("runtime") != runtime:
            return self._handoff_reply(request, status="unresolved", error_code="registration_unresolved")
        self._handoff_rebind_count += 1
        self._handoff_runtime_binding = dict(runtime)
        self._handoff_registered_state = registered
        self._handoff_registered_state_digest = _handoff_digest(registered)
        return self._persist_successful_phase(request, "rebind_committed", {"registered_state": registered})

    def _resume_phase(self, request: Mapping[str, Any], *, expected_phase: str, next_phase: str) -> dict[str, Any]:
        if self._handoff_phase != expected_phase:
            return self._handoff_reply(request, status="conflict", error_code="phase_conflict")
        context = self._verify_finalization_context(request, require_registration=True)
        if context is None:
            return self._handoff_reply(request, status="unresolved", error_code="fence_unresolved")
        return self._persist_successful_phase(request, next_phase, {"registered_state": context["registered_state"]})

    def _finalize_handoff(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if (self._handoff_phase != "resumed" or self._claim_host is None
                or self._handoff_finalize_persistence_failed or self._aborted):
            return self._handoff_reply(request, status="unresolved", error_code="phase_conflict",
                                       observe_registered_state=False)
        owner = self._claim_host
        activation = self._activation_grant
        # Capture the exact local registration and owner identities under the
        # claim condition. Health and protected-file checks run after release.
        with owner._handoff_condition:
            initial = owner._handoff_quiescence_locked()
            if (initial["observation_status"] != "known" or initial["claim_gate_closed"] is not True
                    or any(initial[key] != 0 for key in ("claim_rpc_in_flight", "active_attempts",
                                                         "pending_settlements", "registration_rpc_in_flight"))
                    or owner._handoff_registration_authorized_thread is not None
                    or owner._handoff_registration_authorized_id is not None):
                failed = dict(initial)
                client = None
                registered_before = runtime_before = capabilities_before = None
                source_epoch_before = None
            else:
                failed = None
                client = owner.client
                captured_endpoint = getattr(client, "endpoint", None)
                registered_before = {key: dict(value) for key, value in owner._registered_state.items()}
                runtime_before = dict(owner._registered_runtime_state)
                capabilities_before = {
                    key: {"capability_digest": record.capability_digest,
                          "source_digest": record.source_digest,
                          "dependency_digest": record.dependency_digest}
                    for key, record in owner.capabilities.items()
                }
                source_epoch_before = owner.source_epoch
                captured_phase = self._handoff_phase
                captured_binding_digest = self._handoff_binding_digest
                captured_authority_digest = _handoff_digest(self._handoff_authority_snapshot)
                captured_state_digest = self._handoff_registered_state_digest
                captured_aborted = self._aborted
                captured_persist_failed = self._handoff_finalize_persistence_failed
        if failed is not None or client is None or activation is None or not isinstance(captured_endpoint, str):
            return self._handoff_reply(request, status="unresolved", error_code="quiescence_unresolved",
                                       registered_state=None, quiescence=failed,
                                       observe_registered_state=False)
        observed = _handoff_registered_state(owner, activation, client=client,
                                             expected_endpoint=captured_endpoint)
        if observed is None:
            return self._handoff_reply(request, status="unresolved", error_code="registration_unresolved",
                                       registered_state=None, quiescence=initial,
                                       observe_registered_state=False)
        context = self._verify_finalization_context(request, require_registration=True,
                                                    observed_registration=observed)
        if context is None:
            return self._handoff_reply(request, status="unresolved", error_code="fence_unresolved",
                                       registered_state=None, quiescence=initial,
                                       observe_registered_state=False)
        failed_snapshot = None
        candidate = None
        with owner._handoff_condition:
            snapshot = owner._handoff_quiescence_locked()
            if (snapshot["observation_status"] != "known" or snapshot["claim_gate_closed"] is not True
                    or any(snapshot[key] != 0 for key in ("claim_rpc_in_flight", "active_attempts",
                                                          "pending_settlements", "registration_rpc_in_flight"))
                    or owner._handoff_registration_authorized_thread is not None
                    or owner._handoff_registration_authorized_id is not None):
                failed_snapshot = snapshot
            elif (owner.client is not client or getattr(client, "endpoint", None) != captured_endpoint
                  or self._handoff_phase != captured_phase
                  or captured_phase != "resumed"
                  or self._handoff_binding_digest != captured_binding_digest
                  or captured_binding_digest != _handoff_digest(request["binding"])
                  or self._activation_grant != activation
                  or self._aborted != captured_aborted or captured_aborted
                  or self._handoff_finalize_persistence_failed != captured_persist_failed
                  or captured_persist_failed
                  or _handoff_digest(self._handoff_authority_snapshot) != captured_authority_digest
                  or self._handoff_registered_state_digest != captured_state_digest
                  or owner._registered_state != registered_before
                  or owner._registered_runtime_state != runtime_before
                  or owner.source_epoch != source_epoch_before
                  or {key: {"capability_digest": record.capability_digest,
                            "source_digest": record.source_digest,
                            "dependency_digest": record.dependency_digest}
                      for key, record in owner.capabilities.items()} != capabilities_before
                  or self._handoff_registered_state != context.get("registered_state")
                  or context.get("registered_state") != observed
                  or _handoff_digest(context.get("registered_state")) != self._handoff_registered_state_digest):
                failed_snapshot = snapshot
            else:
                candidate = self._handoff_reply(
                    request, status="ok", registered_state=context["registered_state"],
                    quiescence={**snapshot, "claim_gate_closed": False}, phase="finalized",
                )
                if not self._persist_handoff_reply(request, candidate):
                    self._handoff_finalize_persistence_failed = True
                    owner._handoff_claim_gate_closed = True
                    failed_snapshot = snapshot
                    candidate = None
                else:
                    self._handoff_phase = "finalized"
                    owner._handoff_claim_gate_closed = False
                    owner._handoff_condition.notify_all()
        if candidate is None:
            if failed_snapshot is not None and failed_snapshot.get("observation_status") == "known":
                code = "custody_unresolved" if self._handoff_finalize_persistence_failed else "quiescence_unresolved"
            else:
                code = "quiescence_unresolved"
            return self._handoff_reply(request, status="unresolved", error_code=code,
                                       registered_state=None, quiescence=failed_snapshot,
                                       observe_registered_state=False)
        return candidate

    def _verify_authenticated_replay(self, request: Mapping[str, Any], cached: Mapping[str, Any]) -> bool:
        if self._retained_relay_actor is None:
            return False
        try:
            binding = request["binding"]
            peer = self._retained_relay_actor.verify()
            scope = Path(binding["custody_scope"])
            relay = _handoff_read_protected_json(scope, "relay-handoff-state.json")
            runtime = _handoff_read_protected_json(scope, "runtime-handoff-state.json")
        except Exception:
            return False
        digest, request_digest = _handoff_digest(binding), _handoff_digest(request)
        if (not _handoff_journal_base(relay, binding, digest)
                or not _handoff_journal_base(runtime, binding, digest)
                or peer != relay.get("writer_incarnation")
                or relay.get("writer_owner_epoch") != binding.get("original_owner_epoch")):
            return False
        for journal in (relay, runtime):
            entry = journal["entries"].get(f"{binding['handoff_id']}:{request['command']}")
            if (not isinstance(entry, Mapping) or set(entry) != {"request_digest", "binding_digest", "reply"}
                    or entry.get("request_digest") != request_digest or entry.get("binding_digest") != digest
                    or entry.get("reply") != dict(cached)):
                return False
        return True

    def _persist_handoff_reply(self, request: Mapping[str, Any], reply: Mapping[str, Any]) -> bool:
        """Durably retain one pause receipt before exposing its successful ACK."""
        path = self._handoff_journal_path
        if not self._handoff_journal_ready or path is None:
            return False
        parent = path.parent
        temporary = parent / f".host-handoff-state.{os.getpid()}.{secrets_module.token_hex(8)}.tmp"
        record = {
            "version": "runtime.local-execution-handoff-journal/v1",
            "writer_incarnation": f"{reply['host']['pid']}:{reply['host']['birth_id']}",
            "writer_owner_epoch": request["binding"]["original_owner_epoch"],
            "handoff_id": request["binding"]["handoff_id"],
            "command": request["command"],
            "request_digest": reply["request_digest"],
            "reply": dict(reply),
        }
        encoded = _handoff_canonical(record)
        descriptor = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short host handoff journal write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, path)
            directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return True
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except OSError:
                pass
            return False

    def _handoff_reply(self, request: Mapping[str, Any], *, status: str,
                       error_code: str | None = None,
                       registered_state: Mapping[str, Any] | None = None,
                       quiescence: Mapping[str, Any] | None = None,
                       phase: str | None = None,
                       observe_registered_state: bool = True) -> dict[str, Any]:
        from astrid.core.execution.custody_broker import default_process_identity

        binding = request["binding"]
        identity = default_process_identity(os.getpid())
        if identity is None:
            raise HostError("handoff host process identity is unavailable")
        host = {key: identity[key] for key in ("pid", "birth_id", "uid")}
        owner = self._claim_host
        if owner is None:
            quiescence = {"claim_gate_closed": None, "claim_rpc_in_flight": None,
                          "active_attempts": None, "pending_settlements": None,
                          "registration_rpc_in_flight": None, "observation_status": "unknown"}
        elif quiescence is not None:
            quiescence = dict(quiescence)
        else:
            quiescence = owner.handoff_quiescence()
        activation = self._activation_grant
        if registered_state is None and observe_registered_state:
            registered_state = (
                _handoff_registered_state(owner, activation)
                if owner is not None and activation is not None else None
            )
        elif registered_state is not None:
            registered_state = dict(registered_state)
        capabilities = (dict(self._handoff_current_roles) if self._handoff_current_roles is not None
                       else self.known_custody_capabilities())
        capabilities = {role: ref for role, ref in capabilities.items()
                        if role in _HANDOFF_ROLE_NAMES and _handoff_role_valid(ref, role)}
        return {"version": _HANDOFF_VERSION, "command": request["command"],
                "binding_digest": _handoff_digest(binding),
                "request_digest": _handoff_digest(request), "status": status,
                "phase": phase or self._handoff_phase, "host": host,
                "registered_state": registered_state, "quiescence": quiescence,
                "custody_capabilities": capabilities, "error_code": error_code}

    def _dispatch_handoff(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if set(request) != {"version", "command", "binding", "payload"} or request.get("version") != _HANDOFF_VERSION:
            raise HostError("handoff request envelope is invalid")
        command, binding, payload = request["command"], request["binding"], request["payload"]
        if command not in _HANDOFF_COMMAND_PAYLOADS or not isinstance(binding, Mapping) or set(binding) != _HANDOFF_BINDING_FIELDS:
            raise HostError("handoff command or binding is invalid")
        if not isinstance(payload, Mapping) or set(payload) != _HANDOFF_COMMAND_PAYLOADS[command]:
            raise HostError("handoff command payload is invalid")
        for name in ("operation_id", "channel_id", "handoff_id", "workspace_uuid", "executor_incarnation",
                     "custody_scope", "original_owner_epoch", "new_owner_epoch", "intent_digest", "source_owner_epoch"):
            value = binding.get(name)
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise HostError("handoff binding identity is invalid")
        if (binding["operation_id"] != self.operation_id or binding["channel_id"] != self.channel_id
                or not Path(binding["custody_scope"]).is_absolute()
                or binding["new_owner_epoch"] == binding["original_owner_epoch"]
                or not isinstance(binding["deadline_unix_ms"], int)
                or isinstance(binding["deadline_unix_ms"], bool)
                or not 0 < binding["deadline_unix_ms"] <= 2**63 - 1):
            raise HostError("handoff binding conflicts with the selected launch")
        for key in ("nonce_digest", "profile_binding_digest", "launch_evidence_digest", "activation_record_digest",
                    "credential_generation_digest", "intent_digest"):
            if not isinstance(binding.get(key), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", binding[key]):
                raise HostError("handoff binding digest is invalid")
        if not isinstance(binding["original_roles"], Mapping) or not isinstance(binding["current_roles"], Mapping):
            raise HostError("handoff role bindings are invalid")
        if (not _handoff_actor_valid(binding["new_owner"])
                or not _handoff_actor_valid(binding["source_owner"])
                or not _handoff_role_valid(binding["source_relay_reference"], "relay")
                or set(binding["original_roles"]) != _HANDOFF_ROLE_NAMES
                or any(not _handoff_role_valid(ref, role) for role, ref in binding["original_roles"].items())
                or set(binding["current_roles"]) != _HANDOFF_ROLE_NAMES
                or any(not _handoff_role_valid(ref, role) for role, ref in binding["current_roles"].items())):
            raise HostError("handoff role references are invalid")
        intent_binding = dict(binding)
        supplied_intent = intent_binding.pop("intent_digest")
        expected_intent = _handoff_digest({"version": _HANDOFF_VERSION, "binding": intent_binding})
        if supplied_intent != expected_intent:
            raise HostError("handoff intent digest differs")
        request_digest = _handoff_digest(request)
        binding_digest = _handoff_digest(binding)
        with self._lock:
            if self._handoff_binding_digest not in (None, binding_digest):
                return self._handoff_reply(request, status="conflict", error_code="binding_conflict")
            now_ms = int(time.time() * 1000)
            if binding["deadline_unix_ms"] <= now_ms:
                # Expiry is not a state transition. In particular, an
                # unauthenticated direct-dispatch caller cannot pin a binder,
                # overwrite a saved receipt, or change claim admission by
                # presenting an expired request.
                return self._handoff_reply(request, status="unresolved", error_code="deadline_expired")
            if binding["deadline_unix_ms"] > now_ms + 86_400_000:
                raise HostError("handoff binding deadline exceeds the local horizon")
            self._handoff_binding = dict(binding)
            self._handoff_binding_digest = binding_digest
        if request_digest in self._handoff_replies:
            cached = self._handoff_replies[request_digest]
            if binding["deadline_unix_ms"] <= now_ms:
                if not self._verify_authenticated_replay(request, cached):
                    return self._handoff_reply(request, status="unresolved", error_code="deadline_expired")
            return json.loads(json.dumps(cached))
        if binding["deadline_unix_ms"] <= now_ms:
            return self._handoff_reply(request, status="unresolved", error_code="deadline_expired")

        if command == "handoff_report":
            reply = self._handoff_reply(request, status="ok" if self._claim_host else "unresolved",
                                        error_code=None if self._claim_host else "quiescence_unresolved")
        elif command == "handoff_abort":
            reply = self._handoff_reply(request, status="unresolved" if self._handoff_phase == "host_paused"
                                        else "conflict",
                                        error_code="custody_unresolved" if self._handoff_phase == "host_paused"
                                        else "phase_conflict")
        elif not self._validate_retained_binding(binding) or self._claim_host is None:
            reply = self._handoff_reply(request, status="unresolved", error_code="identity_unresolved")
        elif command == "handoff_prepare":
            if self._handoff_phase != "owned":
                reply = self._handoff_reply(request, status="conflict", error_code="phase_conflict")
            else:
                disposition, _snapshot = self._claim_host.pause_claim_admission()
                if disposition == "active_work":
                    reply = self._handoff_reply(request, status="active_work", error_code="active_work",
                                                registered_state=None, quiescence=_snapshot,
                                                observe_registered_state=False)
                elif disposition == "paused":
                    registered_state = _handoff_registered_state(self._claim_host, self._activation_grant)
                    if registered_state is None:
                        reply = self._handoff_reply(request, status="unresolved",
                                                    error_code="registration_unresolved",
                                                    registered_state=None, quiescence=_snapshot,
                                                    observe_registered_state=False)
                    else:
                        roles = self._validated_pause_roles(binding)
                        if roles is None:
                            with self._claim_host._handoff_condition:
                                self._claim_host._handoff_observation_known = False
                                self._claim_host._handoff_claim_gate_closed = True
                            reply = self._handoff_reply(request, status="unresolved", error_code="custody_unresolved",
                                                        registered_state=None, observe_registered_state=False)
                        else:
                            prior_phase = self._handoff_phase
                            prior_roles = self._handoff_current_roles
                            self._handoff_current_roles = roles
                            self._handoff_phase = "host_paused"
                            candidate = self._handoff_reply(request, status="ok",
                                                            registered_state=registered_state,
                                                            quiescence=_snapshot)
                            if candidate["registered_state"] is not None and self._persist_handoff_reply(request, candidate):
                                reply = candidate
                            else:
                                self._handoff_phase = prior_phase
                                self._handoff_current_roles = prior_roles
                                with self._claim_host._handoff_condition:
                                    self._claim_host._handoff_observation_known = False
                                    self._claim_host._handoff_claim_gate_closed = True
                                reply = self._handoff_reply(request, status="unresolved", error_code="custody_unresolved",
                                                            registered_state=None, observe_registered_state=False)
                else:
                    reply = self._handoff_reply(request, status="unresolved", error_code="quiescence_unresolved",
                                                registered_state=None, quiescence=_snapshot,
                                                observe_registered_state=False)
        elif command == "handoff_export_sealed":
            if self._handoff_phase != "host_paused":
                reply = self._handoff_reply(request, status="conflict", error_code="phase_conflict")
            else:
                exported = self._verify_export_evidence(request)
                reply = self._persist_successful_phase(request, "export_sealed", exported)
        elif command == "handoff_adopt":
            if self._handoff_phase != "export_sealed":
                reply = self._handoff_reply(request, status="conflict", error_code="phase_conflict")
            else:
                adopted = self._verify_adoption_evidence(request)
                if adopted is None:
                    reply = self._handoff_reply(request, status="unresolved", error_code="identity_unresolved")
                else:
                    reply = self._persist_adoption_phase(request, adopted)
        elif command == "handoff_commit":
            reply = self._commit_rebind(request)
        elif command == "resume_prepare":
            reply = self._resume_phase(request, expected_phase="rebind_committed", next_phase="resume_armed")
        elif command == "resume_commit":
            reply = self._resume_phase(request, expected_phase="resume_armed", next_phase="resumed")
        elif command == "handoff_finalize":
            reply = self._finalize_handoff(request)
        else:
            reply = self._handoff_reply(request, status="unresolved", error_code="identity_unresolved")
        self._handoff_replies[request_digest] = reply
        return json.loads(json.dumps(reply))

    def _prepare_host_only(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Parked original host; no engine allocation or fabricated engine roles."""
        profile = request["profile"]
        required = {"execution_mode", "profile_id", "workspace_uuid", "realm_root", "support_root", "machine_id",
            "host_executable", "host_artifact_digest", "session_config_digest", "profile_revision", "profile_digest",
            "release_digest", "host_protocol", "host_launch"}
        if set(profile) - required - {"host_os_executable", "host_os_artifact_digest"} or not required <= set(profile):
            raise HostError("host-only selected profile shape is invalid")
        launch = profile["host_launch"]
        if not isinstance(launch, Mapping) or set(launch) - {"source_checkout", "pack_root", "boot_manifest_path", "boot_manifest_hash", "capability_matrix", "environment", "additional_pack_roots", "max_concurrency"} or not {"source_checkout", "pack_root", "boot_manifest_path", "boot_manifest_hash", "capability_matrix", "environment", "max_concurrency"} <= set(launch):
            raise HostError("host-only launch selection is invalid")
        if type(launch["max_concurrency"]) is not int or not 1 <= launch["max_concurrency"] <= 16:
            raise HostError("host-only concurrency must be explicitly bounded")
        if _handoff_digest(launch) != profile["session_config_digest"]:
            raise HostError("host-only launch configuration digest differs")
        from astrid.core.execution import custody_broker as custody_module
        peer = self._retained_relay_actor
        if peer is None or any(peer.verify().get(k) != request["runtime_owner"].get(k) for k in ("pid", "uid", "birth_id")):
            raise HostError("host-only preparation is not from original Runtime peer")
        for name in ("profile_digest", "release_digest", "session_config_digest", "host_artifact_digest"):
            if not isinstance(profile[name], str) or not _ACTIVATION_DIGEST.fullmatch(profile[name]):
                raise HostError("host-only selected artifact identity is invalid")
        interpreter = Path(profile["host_executable"])
        if not interpreter.is_absolute() or not interpreter.is_file() or "sha256:" + hashlib.sha256(interpreter.read_bytes()).hexdigest() != profile["host_artifact_digest"]:
            raise HostError("host-only selected interpreter differs")
        boot = Path(launch["boot_manifest_path"])
        from astrid.core._shared.boot_manifest import load_boot_manifest_hash, normalize_sha256_digest
        try:
            stamped_boot_hash = load_boot_manifest_hash(boot, support_root=profile["support_root"])
            selected_boot_hash = normalize_sha256_digest(launch["boot_manifest_hash"], label="host-only boot pin")
        except (OSError, RuntimeError) as exc:
            raise HostError("host-only boot input differs") from exc
        if normalize_sha256_digest(stamped_boot_hash) != selected_boot_hash:
            raise HostError("host-only boot input differs")
        # The existing verified import launcher has already bound the full
        # installed closure before executing this module. Recheck the selected
        # source closure and private profile rather than accepting a ready claim.
        closure = source_checkout_closure_digest(Path(launch["source_checkout"]))
        protocol = profile["host_protocol"]
        if (not isinstance(protocol, Mapping) or set(protocol) != {"version", "module", "source_closure_digest"}
                or protocol["version"] != CURRENT_ROLES_VERSION or protocol["module"] != "astrid.core.execution.generic_host"
                or protocol["source_closure_digest"] != closure):
            raise HostError("host-only admitted import closure differs")
        scope = Path(str(request["custody_scope"]))
        if not scope.is_absolute() or scope.is_symlink() or not self._acquire_handoff_journal(scope):
            raise CleanupUncertainError("host-only custody scope is unresolved")
        intent = _canonical_digest(dict(request))
        with self._lock:
            if self._aborted or (self._intent is not None and self._intent != intent):
                raise HostError("host-only preparation replay conflicts")
            self._intent = intent; self._request = json.loads(json.dumps(dict(request)))
            result = {"version": _LOCAL_PREPARATION_VERSION, "status": "prepared",
                "execution_mode": "host_only_cpu", "operation_id": self.operation_id, "channel_id": self.channel_id,
                "owner_epoch": request["owner_epoch"], "session_config_digest": profile["session_config_digest"],
                "processes": {"host": {"pid": os.getpid(), "birth_id": process_birth_identity()}}}
            self.result = result
            return json.loads(json.dumps(result))

    def _dispatch_host_only(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if self._request is None:
            raise HostError("host-only control has no retained preparation")
        bound = {"version": _LOCAL_PREPARATION_VERSION, "operation_id": self.operation_id,
            "channel_id": self.channel_id, "owner_epoch": self._request["owner_epoch"]}
        if any(request.get(k) != v for k, v in bound.items()):
            raise HostError("host-only control owner binding differs")
        command = request.get("command")
        if command == "report_local_execution":
            if set(request) != {*bound, "command"} or self._aborted or self.result is None:
                raise HostError("host-only report unavailable")
            return json.loads(json.dumps(self.result))
        if command != "abort_local_execution":
            raise HostError("host-only nonchild/handoff control is unsupported")
        selected = self._request["profile"]
        abort_bound = {**bound, "profile_digest": selected["profile_digest"],
            "profile_binding_digest": _handoff_digest(selected), "custody_scope": self._request["custody_scope"]}
        capabilities = request.get("custody_capabilities")
        if set(request) != {*abort_bound, "command", "custody_capabilities"} or any(request.get(k) != v for k, v in abort_bound.items()) or not isinstance(capabilities, Mapping) or set(capabilities) != {"host"}:
            raise HostError("host-only cleanup request differs from retained binding")
        from astrid.core.execution import custody_broker as custody_module
        custody_module.RoleCustodyAuthority(Path(abort_bound["custody_scope"]), "host").verify_reference(
            capabilities["host"], expected_actor=self._retained_relay_actor.verify(), owner_epoch=bound["owner_epoch"])
        # Verification authenticates the original Runtime actor and exact host
        # token. The host never adopts authority over its own parent.
        reference_target = capabilities["host"]["target"]
        if reference_target.get("pid") != os.getpid() or reference_target.get("birth_id") != process_birth_identity():
            raise HostError("host-only cleanup reference targets another incarnation")
        intent = _handoff_digest(dict(request))
        with self._lock:
            if self._abort_intent is not None and self._abort_intent != intent:
                raise HostError("host-only cleanup replay conflicts")
            self._abort_intent = intent
            if self._abort_ack is not None:
                return json.loads(json.dumps(self._abort_ack))
        try:
            if self._claim_host is not None:
                self._claim_host.shutdown()
            # Positive acknowledgement follows actual owned-child, bridge and
            # managed-session cleanup. No root exit is treated as this proof.
            reply = {**abort_bound, "custody_capabilities": dict(capabilities), "status": "cleaned",
                     "cleanup": {"host_children": "absent"}}
            with self._lock:
                self._aborted = True; self._abort_ack = reply
            return json.loads(json.dumps(reply))
        except BaseException as exc:
            self.control_error = exc
            return {**abort_bound, "custody_capabilities": dict(capabilities), "status": "unresolved", "error_code": "cleanup_unresolved"}

    def prepare_local_execution(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if getattr(self, '_hosted_binding', None) is not None:
            from astrid.core.execution import hosted_contract, custody_broker
            hosted_contract.verify_preparation(self._hosted_binding, request,
                custody_broker.RoleCustodyAuthority(Path(self._hosted_binding['scope']), 'host'), actor=self._retained_relay_actor)
        required = {"version", "command", "operation_id", "channel_id", "owner_epoch", "runtime_owner", "profile", "custody_scope"}
        if (set(request) != required or request.get("version") != _LOCAL_PREPARATION_VERSION
                or request.get("command") != "prepare_local_execution"
                or request.get("operation_id") != self.operation_id or request.get("channel_id") != self.channel_id):
            raise HostError("local preparation came from the wrong private channel")
        owner, epoch = request["runtime_owner"], request["owner_epoch"]
        if (not isinstance(owner, Mapping) or set(owner) != {"pid", "uid", "birth_id", "runtime_instance_id", "coordinator_epoch"}
                or not isinstance(epoch, str) or not epoch or len(epoch) > 256
                or owner["runtime_instance_id"] != epoch or owner["coordinator_epoch"] != epoch
                or isinstance(owner["pid"], bool) or not isinstance(owner["pid"], int) or owner["pid"] <= 0
                or isinstance(owner["uid"], bool) or not isinstance(owner["uid"], int) or owner["uid"] < 0
                or not isinstance(owner["birth_id"], str) or not owner["birth_id"]):
            raise HostError("local preparation has a stale or invalid Runtime owner epoch")
        if not isinstance(request["profile"], Mapping):
            raise HostError("local preparation profile must be an object")
        if request["profile"].get("execution_mode") == "host_only_cpu":
            return self._prepare_host_only(request)
        profile = _validate_local_engine_profile(request["profile"])
        scope = Path(str(request["custody_scope"]))
        if not scope.is_absolute() or scope.is_symlink():
            raise HostError("local preparation custody scope is invalid")
        if not self._acquire_handoff_journal(scope):
            raise CleanupUncertainError("host handoff journal ownership or retained state is unresolved")
        intent = _canonical_digest(dict(request))
        with self._lock:
            if self._aborted:
                raise HostError("local preparation has been aborted")
            if self._intent is not None:
                if self._intent != intent:
                    raise HostError("local preparation replay conflicts with retained input")
                if self._aborted:
                    raise HostError("local preparation has been aborted")
                if self.error is not None:
                    raise self.error
                if self.result is None:
                    raise HostError("local preparation remains unresolved")
                return json.loads(json.dumps(self.result))
            self._intent = intent  # Before spawn; partial failures retain this obligation.
            self._request = json.loads(json.dumps(dict(request)))
            self._prepare_done.clear()
        try:
            graph = self.graph_factory(request={**dict(request), "profile": profile})
            with self._lock:
                self.graph = graph
            measured = graph.prepare()
            result = {"version": _LOCAL_PREPARATION_VERSION, "status": "prepared",
                      "operation_id": self.operation_id, "channel_id": self.channel_id,
                      "owner_epoch": epoch, **measured}
            with self._lock:
                self.result = result
            return json.loads(json.dumps(result))
        except BaseException as exc:
            with self._lock:
                self.error = exc
            raise
        finally:
            self._prepare_done.set()

    def abort(self) -> None:
        if not self._prepare_done.wait(timeout=5):
            raise CleanupUncertainError("native preparation is still active; custody retained")
        # Main-thread retention and channel cleanup share the same serialization
        # gate. Neither replay nor role-authority locks span process/RPC waits.
        if not self._abort_dispatch_lock.acquire(timeout=5):
            raise CleanupUncertainError("native cleanup is still active; custody retained")
        try:
            with self._lock:
                if self._aborted:
                    return
                graph = self.graph
            if (self._request or {}).get("profile", {}).get("execution_mode") == "host_only_cpu" and self._claim_host is not None:
                self._claim_host.shutdown()
            if graph is not None:
                graph.abort()
            with self._lock:
                self._aborted = True
        finally:
            self._abort_dispatch_lock.release()

    def known_custody_capabilities(self) -> dict[str, Any]:
        if self.graph is None:
            return {}
        if hasattr(self.graph, "known_custody_capabilities"):
            return self.graph.known_custody_capabilities()
        return dict((self.result or {}).get("custody_capabilities") or {})

    def _current_abort_chain(self, base: Mapping[str, Any]) -> dict[str, Any]:
        listener = self.graph.listener
        proof = listener.role_authority.token_chain(actor=listener.cleanup_actor,
                    generation=base["engine_listener"]["generation"],
                    base_target=base["engine_listener"]["target"])
        known = self.known_custody_capabilities()
        if (set(known) != {"engine", "engine_listener"}
                or _handoff_canonical(known["engine"]) != _handoff_canonical(base["engine"])
                or _handoff_canonical(known["engine_listener"]) != _handoff_canonical(proof["current_reference"])):
            raise CleanupUncertainError("current-role cleanup graph differs from retained authority")
        return proof

    def _admission_abort_chains(self, base: Mapping[str, Any]) -> dict[str, Any]:
        if set(base) != {"engine", "engine_listener"}:
            raise CleanupUncertainError("admission cleanup lacks both retained obligations")
        proofs = {}
        for role in base:
            broker = getattr(self.graph, "listener" if role == "engine_listener" else role)
            proofs[role] = broker.role_authority.admission_chain(
                actor=broker.cleanup_actor, generation=base[role]["generation"], base_target=base[role]["target"])
        known = self.known_custody_capabilities()
        if _handoff_canonical(known) != _handoff_canonical({r: p["current_reference"] for r, p in proofs.items()}):
            raise CleanupUncertainError("admission graph differs from retained authority")
        return proofs

    def _dispatch_admission_cleanup(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Opt-in partial preparation cleanup; never creates a ready baseline."""
        echo = {k: v for k, v in request.items() if k != "command"}
        error_code = "binding_conflict"
        retained = False
        try:
            required = {"version", "command", "operation_id", "channel_id", "owner_epoch", "profile_digest",
                        "profile_binding_digest", "custody_scope", "admission_roles", "admission_roles_digest"}
            if self._request is None or self.result is not None or set(request) != required:
                raise HostError("admission cleanup has no exact unresolved preparation")
            profile = self._request["profile"]
            bound = {"version": ADMISSION_CLEANUP_VERSION, "operation_id": self.operation_id,
                     "channel_id": self.channel_id, "owner_epoch": self._request["owner_epoch"],
                     "profile_digest": profile["profile_digest"], "profile_binding_digest": _handoff_digest(profile),
                     "custody_scope": self._request["custody_scope"]}
            base = request["admission_roles"]
            if (request["command"] != "abort_local_execution"
                    or _handoff_canonical({k: request[k] for k in bound}) != _handoff_canonical(bound)
                    or not isinstance(base, Mapping) or set(base) != {"engine", "engine_listener"}
                    or request["admission_roles_digest"] != _handoff_digest(base)):
                raise HostError("admission cleanup binding differs")
            for role, ref in base.items():
                pending = isinstance(ref, Mapping) and isinstance(ref.get("target"), Mapping) and set(ref["target"]) == {"admission_id"}
                if pending:
                    valid = (set(ref) == {"version", "scope_root", "role", "generation", "target"}
                             and ref["version"] == "runtime.role-custody-reference/v1" and ref["role"] == role
                             and type(ref["generation"]) is int and ref["generation"] >= 1
                             and isinstance(ref["target"]["admission_id"], str) and bool(ref["target"]["admission_id"]))
                else:
                    valid = _handoff_role_valid(ref, role)
                if not valid or ref["scope_root"] != bound["custody_scope"]:
                    raise HostError("admission role reference is invalid")
            intent = _handoff_digest(dict(request))
            with self._lock:
                if (self._abort_intent is not None or self._current_abort_intent is not None
                        or self._admission_abort_intent not in (None, intent)):
                    raise HostError("admission cleanup conflicts with retained abort intent")
                if self._admission_abort_ack is not None:
                    return json.loads(_handoff_canonical(self._admission_abort_ack))
            error_code = "cleanup_unresolved"
            if not self._prepare_done.wait(timeout=5) or self.graph is None:
                raise CleanupUncertainError("native preparation remains active")
            self._admission_abort_chains(base)
            with self._lock:
                if (self._abort_intent is not None or self._current_abort_intent is not None
                        or self._admission_abort_intent not in (None, intent)):
                    error_code = "binding_conflict"
                    raise HostError("admission cleanup intent changed")
                self._admission_abort_intent = intent
                retained = True
            cleanup = self.graph.abort()  # No authority/replay guard spans native waits.
            proofs = self._admission_abort_chains(base)
            current = {role: proof["current_reference"] for role, proof in proofs.items()}
            if not isinstance(cleanup, Mapping) or set(cleanup) != set(current):
                raise CleanupUncertainError("admission cleanup lacks both positive proofs")
            for role, ref in current.items():
                proof = cleanup[role]
                pending = set(ref["target"]) == {"admission_id"}
                kind = "never-spawned" if pending else ("retained-child-exit" if role == "engine" else "authenticated-retained-child-exit")
                if (not isinstance(proof, Mapping) or set(proof) != {"generation", "target", "exit_code", "proof_kind"}
                        or proof["proof_kind"] != kind
                        or (proof["exit_code"] is not None if pending else type(proof["exit_code"]) is not int)
                        or _handoff_canonical({k: proof[k] for k in ("generation", "target")})
                        != _handoff_canonical({k: ref[k] for k in ("generation", "target")})):
                    raise CleanupUncertainError("admission cleanup has no exact final exit proof")
            reply = {**echo, "status": "cleaned", "current_roles": current,
                     "revisions": {r: p["revision"] for r, p in proofs.items()},
                     "admission_refresh_digests": {r: p["admission_refresh_digest"] for r, p in proofs.items()},
                     "cleanup": dict(cleanup)}
            with self._lock:
                self._aborted = True
                self._admission_abort_ack = json.loads(_handoff_canonical(reply))
            return json.loads(_handoff_canonical(reply))
        except BaseException as exc:
            self.control_error = exc
            try:
                current = self.known_custody_capabilities()
            except BaseException:
                current = {}
            reply = {**echo, "status": "unresolved", "error_code": error_code, "current_roles": current}
            if retained:
                with self._lock:
                    self._admission_abort_ack = json.loads(_handoff_canonical(reply))
            return reply

    def _dispatch_current_abort(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Reply explicitly to every versioned cleanup failure, without adoption."""
        echo = {k: v for k, v in request.items() if k != "command"}
        intent = None
        running = False
        error_code = "binding_conflict"
        try:
            required = {"version", "command", "operation_id", "channel_id", "owner_epoch", "profile_digest",
                        "profile_binding_digest", "custody_scope", "base_roles_digest", "custody_capabilities"}
            if self._request is None or self.result is None or set(request) != required:
                raise HostError("current-role abort has no exact prepared binding")
            selected = self._request["profile"]
            bound = {"version": CURRENT_ROLES_VERSION, "operation_id": self.operation_id,
                     "channel_id": self.channel_id, "owner_epoch": self._request["owner_epoch"],
                     "profile_digest": selected["profile_digest"],
                     "profile_binding_digest": _handoff_digest(selected),
                     "custody_scope": self._request["custody_scope"]}
            base = self.result["custody_capabilities"]
            supplied = request["custody_capabilities"]
            if (request["command"] != "abort_local_execution"
                    or _handoff_canonical({k: request[k] for k in bound}) != _handoff_canonical(bound)
                    or request["base_roles_digest"] != _handoff_digest(base)
                    or not isinstance(base, Mapping) or set(base) != {"engine", "engine_listener"}
                    or not isinstance(supplied, Mapping) or set(supplied) != set(base)
                    or any(not _handoff_role_valid(base[role], role) or not _handoff_role_valid(supplied[role], role) for role in base)
                    or any(base[role]["scope_root"] != bound["custody_scope"] for role in base)
                    or _handoff_canonical(supplied["engine"]) != _handoff_canonical(base["engine"])):
                raise HostError("current-role abort differs from the prepared base")
            listener = supplied["engine_listener"]
            original = base["engine_listener"]
            if (any(listener[k] != original[k] for k in ("version", "scope_root", "role", "generation"))
                    or any(listener["target"][k] != original["target"][k] for k in ("pid", "uid", "birth_id"))):
                raise HostError("current-role abort substituted listener custody")
            intent = _handoff_digest(dict(request))
            with self._lock:
                if self._abort_intent is not None or self._admission_abort_intent is not None or (self._current_abort_intent is not None and self._current_abort_intent != intent):
                    raise HostError("current-role abort conflicts with a retained abort intent")
                if self._current_abort_ack is not None:
                    return json.loads(_handoff_canonical(self._current_abort_ack))
                if self._current_abort_running:
                    raise CleanupUncertainError("current-role abort remains in progress")
            error_code = "cleanup_unresolved"
            if not self._prepare_done.wait(timeout=5) or self.graph is None:
                raise CleanupUncertainError("local graph preparation is unresolved")
            proof = self._current_abort_chain(base)
            admitted_targets = [original["target"], *[entry["ack"]["target"] for entry in proof["chain"]]]
            if _handoff_canonical(listener["target"]) not in [_handoff_canonical(target) for target in admitted_targets]:
                error_code = "binding_conflict"
                raise HostError("current-role abort supplied a foreign refresh-chain member")
            with self._lock:
                # Recheck after the historical proof; no guard spans graph waits.
                if self._abort_intent is not None or self._admission_abort_intent is not None or (self._current_abort_intent is not None and self._current_abort_intent != intent):
                    error_code = "binding_conflict"
                    raise HostError("current-role abort intent changed during verification")
                if self._current_abort_ack is not None:
                    return json.loads(_handoff_canonical(self._current_abort_ack))
                if self._current_abort_running:
                    raise CleanupUncertainError("current-role abort remains in progress")
                self._current_abort_intent = intent
                self._current_abort_running = running = True
            cleanup = self.graph.abort()
            proof = self._current_abort_chain(base)  # Cleanup may refresh the listener.
            current = {"engine": base["engine"], "engine_listener": proof["current_reference"]}
            if not isinstance(cleanup, Mapping) or set(cleanup) != set(current):
                raise CleanupUncertainError("current-role cleanup lacks the required graph")
            for role, ref in current.items():
                exit_proof = cleanup[role]
                kind = "retained-child-exit" if role == "engine" else "authenticated-retained-child-exit"
                if (not isinstance(exit_proof, Mapping) or set(exit_proof) != {"generation", "target", "exit_code", "proof_kind"}
                        or type(exit_proof.get("exit_code")) is not int or exit_proof.get("proof_kind") != kind
                        or _handoff_canonical({"generation": exit_proof["generation"], "target": exit_proof["target"]})
                        != _handoff_canonical({"generation": ref["generation"], "target": ref["target"]})):
                    raise CleanupUncertainError("current-role cleanup lacks the final authenticated exit proof")
            reply = {**echo, "status": "cleaned", "revision": proof["revision"],
                     "refresh_proof_digest": proof["refresh_proof_digest"], "current_roles": current, "cleanup": dict(cleanup)}
            with self._lock:
                self._aborted = True
                self._current_abort_ack = json.loads(_handoff_canonical(reply))
            return json.loads(_handoff_canonical(reply))
        except BaseException as exc:
            self.control_error = exc
            try:
                roles = self.known_custody_capabilities()
            except BaseException:
                roles = {}
            reply = {**echo, "status": "unresolved", "error_code": error_code, "current_roles": roles}
            if running:
                with self._lock:
                    self._current_abort_ack = json.loads(_handoff_canonical(reply))
            return reply
        finally:
            if running:
                with self._lock:
                    self._current_abort_running = False

    def dispatch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if (request.get("command") != "abort_local_execution"
                or request.get("version") not in {_LOCAL_PREPARATION_VERSION, CURRENT_ROLES_VERSION, ADMISSION_CLEANUP_VERSION}):
            return self._dispatch_bound(request)
        # Serialize both versioned paths without holding the replay lock over
        # kernel/process waits. A concurrent call receives an explicit frame.
        if not self._abort_dispatch_lock.acquire(blocking=False):
            version = request["version"]
            intent = _handoff_digest(dict(request))
            with self._lock:
                intents = {_LOCAL_PREPARATION_VERSION: self._abort_intent,
                           CURRENT_ROLES_VERSION: self._current_abort_intent,
                           ADMISSION_CLEANUP_VERSION: self._admission_abort_intent}
                conflict = any(value is not None for key, value in intents.items() if key != version) or intents[version] not in (None, intent)
            reply = {**{k: v for k, v in request.items() if k != "command"}, "status": "unresolved",
                     "error_code": "binding_conflict" if conflict else "cleanup_unresolved"}
            if version in {CURRENT_ROLES_VERSION, ADMISSION_CLEANUP_VERSION}:
                try:
                    reply["current_roles"] = self.known_custody_capabilities()
                except BaseException:
                    reply["current_roles"] = {}
            return reply
        try:
            return self._dispatch_bound(request)
        finally:
            self._abort_dispatch_lock.release()

    def _dispatch_bound(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if (self._request or {}).get("profile", {}).get("execution_mode") == "host_only_cpu":
            if request.get("command") == "prepare_local_execution":
                return self.prepare_local_execution(request)
            return self._dispatch_host_only(request)
        if request.get("version") == _HANDOFF_VERSION:
            return self._dispatch_handoff(request)
        if request.get("version") == ADMISSION_CLEANUP_VERSION:
            return self._dispatch_admission_cleanup(request)
        if request.get("version") == CURRENT_ROLES_VERSION:
            return self._dispatch_current_abort(request)
        if request.get("command") == "abort_local_execution" and (self._current_abort_intent is not None or self._admission_abort_intent is not None):
            # Keep the v1 implementation below exact; mixed-version intent
            # conflicts receive a frame and cannot kill the persistent channel.
            return {**{k: v for k, v in request.items() if k != "command"},
                    "status": "unresolved", "error_code": "binding_conflict"}
        from astrid.core.execution.custody_broker import _canonical, _digest_bytes
        command = request.get("command")
        if command == "prepare_local_execution":
            return self.prepare_local_execution(request)
        if self._request is None:
            raise HostError("local execution control has no retained preparation")
        bound = {"version": _LOCAL_PREPARATION_VERSION, "operation_id": self.operation_id,
                 "channel_id": self.channel_id, "owner_epoch": self._request["owner_epoch"]}
        if any(request.get(k) != v for k, v in bound.items()):
            raise HostError("local execution control binder conflicts with retained preparation")
        if command == "report_local_execution":
            if set(request) != {*bound, "command"} or self._aborted or self.graph is None or self.result is None:
                raise HostError("local execution report is not available")
            measured = self.graph.report()
            reported = {**bound, "status": "prepared", **measured}
            if reported != self.result:
                raise HostError("local execution report differs from retained prepared graph")
            return reported
        if command != "abort_local_execution":
            raise HostError("unknown local execution control command")
        selected = self._request["profile"]
        abort_bound = {**bound, "profile_digest": selected["profile_digest"],
                       "profile_binding_digest": _digest_bytes(_canonical(selected)),
                       "custody_scope": self._request["custody_scope"]}
        if (set(request) != {*abort_bound, "command", "custody_capabilities"}
                or any(request.get(k) != v for k, v in abort_bound.items())
                or not isinstance(request["custody_capabilities"], Mapping)):
            raise HostError("local execution abort differs from selected profile or custody scope")
        intent = _digest_bytes(_canonical(dict(request)))
        with self._lock:
            if self._abort_intent is not None and self._abort_intent != intent:
                raise HostError("local execution abort replay conflicts")
            self._abort_intent = intent
            if self._abort_ack is not None:
                return json.loads(json.dumps(self._abort_ack))
        known = self.known_custody_capabilities()
        for role, reference in request["custody_capabilities"].items():
            if role not in known or reference != known[role]:
                raise HostError("local execution abort has foreign or stale role custody")
        try:
            if self.graph is None:
                raise CleanupUncertainError("local graph preparation is unresolved")
            cleanup = self.graph.abort()
            if not isinstance(cleanup, Mapping) or set(cleanup) != {"engine", "engine_listener"}:
                raise CleanupUncertainError("whole required native graph cleanup is unproved")
            reply = {**abort_bound, "status": "cleaned", "custody_capabilities": known, "cleanup": dict(cleanup)}
            with self._lock:
                self._aborted, self._abort_ack = True, reply
            return json.loads(json.dumps(reply))
        except BaseException as exc:
            # A response preserves every actual obligation, including ones the
            # relay had not yet received. It makes no exit or cleanup claim.
            self.control_error = exc
            return {**abort_bound, "status": "unresolved", "custody_capabilities": self.known_custody_capabilities(),
                    "error_code": "cleanup_unresolved"}

    def serve_control(self, descriptor: int) -> threading.Thread:
        if descriptor < 3:
            raise HostError("local host-control descriptor is invalid")
        def serve():
            control = socket.socket(fileno=descriptor)
            try:
                from astrid.core.execution import custody_broker as custody_module

                # This is the retained Runtime relay -> host channel. It does
                # not authenticate B; it pins the real relay peer so protected
                # relay designation/transition evidence can be checked later.
                self._retained_relay_actor = custody_module.AuthenticatedCleanupActor.private_peer(control)
                while True:
                    frame = _read_activation_frame(control, limit=1024 * 1024)
                    try:
                        reply = self.dispatch(frame)
                    except BaseException as exc:
                        self.control_error = exc
                        command = frame.get("command")
                        if command not in {"prepare_local_execution", "report_local_execution"}:
                            raise
                        reply = {"version": _LOCAL_PREPARATION_VERSION, "status": "unresolved",
                                 "operation_id": self.operation_id, "channel_id": self.channel_id,
                                 "owner_epoch": (self._request or frame).get("owner_epoch"),
                                 "error_code": "preparation_unresolved" if command == "prepare_local_execution" else "report_unresolved",
                                 "custody_capabilities": self.known_custody_capabilities()}
                    _send_activation_frame(control, reply)
            except BaseException as exc:
                self.control_error = exc
            finally:
                control.close()
        thread = threading.Thread(target=serve, name="astrid-host-control", daemon=True)
        thread.start()
        return thread



def _selected_birth_matches(observed: Any, retained: Any) -> bool:
    """Compare the selected Vibe representation to the same retained birth."""
    if not isinstance(observed, str) or not observed or not isinstance(retained, str) or not retained:
        return False
    if observed == retained:
        return True
    return sys.platform == "darwin" and retained.startswith("ps-lstart:") and observed == retained[len("ps-lstart:"):]


class _NativeEngineGraph:
    """Host-owned engine adapter; required role custody precedes prepared reply."""

    def __init__(self, *, request: Mapping[str, Any], enable_exit_v2: bool = False):
        self.request = dict(request)
        self.profile = dict(request["profile"])
        self.process: subprocess.Popen | None = None
        self.engine = self.listener = self.custody = None
        self._native_error: BaseException | None = None
        self._clean_ack = None
        self._listener_exit = None
        self._engine_spawn_started = False
        self._engine_released = threading.Event()
        self._engine_peer_lock = threading.Lock()
        self._engine_control_actor = None
        self.readiness_profile: dict[str, Any] | None = None
        if type(enable_exit_v2) is not bool:
            raise HostError("listener exit selection must be explicit")
        self._enable_exit_v2 = enable_exit_v2
        self._source_observer = None

    def prepare(self) -> dict[str, Any]:
        # This controller-owned retained engine graph has no host confinement
        # producer yet. Its custody registration environment is not a policy.
        # Keep this dependent path closed pending its separate launch package;
        # request profile/env fields must never reactivate the legacy spawn.
        _confined_launch_plan([sys.executable])
        from astrid.core.execution import custody_broker as custody_module
        from astrid.core.execution import vibecomfy_engine_adapter as engine_adapter
        profile, request = self.profile, self.request
        launch = profile["engine_launch"]
        root = Path(launch["session_root"])
        for role in ("engine", "engine_listener"):
            executable = Path(profile[role + "_executable"])
            if (not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK)
                    or "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest() != profile[role + "_artifact_digest"]):
                raise HostError("selected engine executable artifact differs")
        adapter_path = Path(engine_adapter.__file__).absolute()
        if "sha256:" + hashlib.sha256(adapter_path.read_bytes()).hexdigest() != launch["adapter_pins"]["adapter_source_sha256"]:
            raise HostError("selected engine adapter artifact differs")
        # Derive private pins from the actual selected import root and source
        # contents. Neither profile extensions nor environment hints select it.
        self._source_observer = engine_adapter.SelectedSourceObserver.from_selected_launch(launch)
        registry_names = ("pid", "comfy_pid", "comfy_process_start_identity", "url", "config.json", "source_revision", "source_content_digest", "launch.json", "daemon.log")
        if any((root / name).exists() or (root / name).is_symlink() for name in registry_names):
            raise HostError("pre-existing engine session registry cannot be adopted")
        root.mkdir(parents=True, exist_ok=True)
        scope = Path(request["custody_scope"])
        self.custody = custody_module.ProcessCustody(scope_root=scope, owner_epoch=request["owner_epoch"])
        self.engine = custody_module.RoleBoundCustodyBroker(
            role="engine", identity_provider=custody_module.default_process_identity, ledger_root=scope / "engine-ledger",
            authority_scope_root=scope, authority_journal=scope / "custody.journal.jsonl",
            owner_epoch=request["owner_epoch"], retain_channel=True,
        )
        engine = self.engine

        class EngineParent:
            actor = None
            def verify(inner):
                if engine.operation_channel is None:
                    raise custody_module.CustodyError("engine private parent channel is unavailable")
                if inner.actor is None:
                    inner.actor = self._bind_engine_control_peer(engine.operation_channel)
                return inner.actor.verify()

        self._engine_parent = EngineParent()
        self.listener = custody_module.RoleBoundCustodyBroker(
            role="engine_listener", identity_provider=custody_module.default_process_identity,
            ledger_root=scope / "engine_listener-ledger", authority_scope_root=scope,
            authority_journal=scope / "custody.journal.jsonl", owner_epoch=request["owner_epoch"],
            launch_parent=self._engine_parent,
        )
        nonce = secrets_module.token_hex(16)
        self._launch_nonce = nonce
        self._listener_exit = None
        self._clean_ack = None
        payload = {"engine_launch": launch, "launch_token": nonce,
                   "operation_id": request["operation_id"], "channel_id": request["channel_id"],
                   "owner_epoch": request["owner_epoch"], "custody_scope": str(scope),
                   "source_observation_pins": self._source_observer.pins,
                   "listener_wrapper_executable": profile["engine_executable"],
                   "listener_registration": self.listener.child_environment(launch["listener_argv"], start_new_session=False)}
        argv = [profile["engine_executable"], str(adapter_path), json.dumps(payload, sort_keys=True, separators=(",", ":"))]
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "PYTHONPATH", "CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "COMFYUI_PATH"}}
        env.update(self.engine.child_environment(argv, start_new_session=True))
        with (root / "daemon.log").open("xb") as log:
            self._engine_spawn_started = True
            self.process = subprocess.Popen(custody_module.custody_wrapper_argv(profile["engine_executable"]),
                                            cwd=str(root.parents[2]), env=env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=log, close_fds=True,
                                            pass_fds=self.engine.child_pipe_fds)
        self.process._astrid_custody = self.custody
        self.process._astrid_process_group_id = self.process.pid
        self.custody._launch_broker = self.engine
        self.custody.bind_launch(self.process)
        self.process._astrid_process_birth = self.custody._handles[self.process.pid][0]
        self._start_native_control()
        observe_tree(self.process)
        self.custody.start_observer()
        self.custody.release_launch()
        self._engine_released.set()
        self.listener.wait_until_sealed()
        registration = self.listener.registration
        if registration is None:
            raise HostError("required listener registration is unresolved")
        listener_pid = int(registration["pid"])
        self.custody._handles[listener_pid] = (str(registration["identity"]["birth_id"]), self.listener)
        observe_tree(self.process)
        if set(self.process._astrid_tree_members) != {self.process.pid, listener_pid}:
            raise HostError("engine graph has an unregistered required descendant")
        self.listener.release_suspended_launch(expected_pid=listener_pid)
        deadline = time.monotonic() + launch["config"]["ready_timeout_sec"]
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise HostError("owned engine exited before readiness")
            if self._native_error is not None:
                raise HostError("native listener custody channel is uncertain") from self._native_error
            observe_tree(self.process)
            if set(self.process._astrid_tree_members) != {self.process.pid, listener_pid}:
                raise HostError("engine graph has an unregistered required descendant")
            measured = self._measure_registry(root, nonce, listener_pid)
            if measured is not None:
                self.readiness_profile = {"vibecomfy_session": measured}
                return {"processes": {"host": {"pid": os.getpid(), "birth_id": process_birth_identity()},
                                      "engine": {"pid": self.process.pid, "birth_id": self.process._astrid_process_birth},
                                      "engine_listener": {"pid": listener_pid, "birth_id": registration["identity"]["birth_id"]}},
                        "engine_binding": {"supervisor_pid": self.process.pid, "listener_pid": listener_pid,
                                           "listener_parent_pid": self.process.pid, "socket_owner_pid": listener_pid},
                        "session_config_digest": profile["session_config_digest"],
                        "custody_capabilities": {"engine": self.engine.role_authority.ready_reference(), "engine_listener": self.listener.role_authority.ready_reference()}}
            time.sleep(0.02)
        raise HostError("owned engine did not become ready; custody retained")

    def _bind_engine_control_peer(self, channel: socket.socket):
        # First-frame/release synchronization is bounded and outside authority
        # guards. The private descriptor and actual retained incarnation bind it.
        if not self._engine_released.wait(timeout=5):
            raise HostError("native engine release remains unresolved")
        if channel is not self.engine.operation_channel:
            raise HostError('native engine pipe differs from the host channel')
        with self._engine_peer_lock:
            if self._engine_control_actor is not None:
                self._engine_control_actor.verify()
                return self._engine_control_actor
            actor = self.engine.retained_pipe_actor()
            identity = actor.verify()
            target = self.engine.role_authority.reference()["target"]
            if (self.process is None or identity.get("pid") != self.process.pid
                    or any(identity.get(k) != target.get(k) for k in ("pid", "uid", "birth_id"))):
                raise HostError("native engine control peer differs from the retained incarnation")
            if self.engine.state == "sealed":
                self.engine.bind_ready_token(expected_pid=self.process.pid,
                                             expected_identity=self.engine.registration['identity'])
            target = self.engine.role_authority.reference()["target"]
            if self.engine.state not in {"sealed", "ready-bound"} or any(
                    identity.get(k) != target.get(k) for k in (
                        "pid", "uid", "birth_id", "audit_token_sha256", "audit_token_pidversion")):
                raise HostError("native engine pipe actor differs from ready custody")
            self._engine_control_actor = actor
            return actor

    def _start_native_control(self) -> None:
        from astrid.core.execution import custody_broker as module
        def serve():
            try:
                channel = self.engine.operation_channel
                if channel is None:
                    raise HostError("retained native engine channel is unavailable")
                channel.settimeout(None)
                actor = None
                while True:
                    frame = module._read_frame(channel)
                    if actor is None:
                        actor = self._bind_engine_control_peer(channel)
                        self._engine_parent.actor = actor
                    if frame.get("command") == "observe_selected_source_fact":
                        reply = self._observe_selected_source_fact(frame, actor)
                        module._send_frame(channel, reply)
                        continue
                    if frame.get("command") == "observe_owned_listener_exit":
                        reply = self._record_listener_exit(frame, actor)
                        module._send_frame(channel, reply)
                        continue
                    reply = module.signal_owned_listener(frame, engine_actor=actor, launch_parent=self._engine_parent,
                                                         listener=self.listener, operation_id=self.request["operation_id"],
                                                         channel_id=self.request["channel_id"], owner_epoch=self.request["owner_epoch"])
                    module._send_frame(channel, reply)
            except BaseException as exc:
                self._native_error = exc
        self._native_thread = threading.Thread(target=serve, name="astrid-native-engine-control", daemon=True)
        self._native_thread.start()

    def known_custody_capabilities(self) -> dict[str, Any]:
        result = {}
        for role, broker in (("engine", self.engine), ("engine_listener", self.listener)):
            if broker is not None:
                result[role] = broker.role_authority.reference()
        return result

    def _observe_selected_source_fact(self, frame: Mapping[str, Any], actor: Any) -> dict[str, Any]:
        from astrid.core.execution import vibecomfy_engine_adapter as adapter
        required = {"version", "command", "operation_id", "channel_id", "owner_epoch", "engine_pid", "source_pins_digest", "fact", "pid"}
        if (not isinstance(frame, dict) or set(frame) != required or frame.get("version") != adapter.SOURCE_FACT_VERSION
                or frame.get("command") != "observe_selected_source_fact" or self._source_observer is None
                or any(frame.get(k) != self.request[k] for k in ("operation_id", "channel_id", "owner_epoch"))
                or type(frame.get("engine_pid")) is not int or self.process is None or frame["engine_pid"] != self.process.pid
                or frame.get("source_pins_digest") != _handoff_digest(self._source_observer.pins)):
            raise HostError("selected source observation has an invalid binding")
        identity = actor.verify()
        target = self.engine.role_authority.reference()["target"]
        if (identity != self._engine_parent.verify() or any(identity.get(k) != target.get(k) for k in ("pid", "uid", "birth_id", "audit_token_sha256", "audit_token_pidversion"))):
            raise HostError("selected source observation has a foreign engine actor")
        fact, pid = frame["fact"], frame["pid"]
        if (not isinstance(fact, str) or fact not in adapter._SOURCE_FACTS
                or (fact != "_process_start_identity" and pid is not None)):
            raise HostError("selected source fact is invalid")
        retained = None
        if fact == "_process_start_identity":
            if type(pid) is not int:
                raise HostError("selected process observation lacks a retained identity")
            if pid == self.process.pid:
                retained = target
            elif self.listener is not None and self.listener.registration is not None and pid == self.listener.registration["pid"]:
                retained = self.listener.role_authority.reference()["target"]
            if retained is None or retained.get("pid") != pid or not isinstance(retained.get("birth_id"), str):
                raise HostError("selected process observation differs from admitted custody")
        value = self._source_observer.observe(fact, pid)
        if (actor.verify() != identity or self.engine.role_authority.reference()["target"] != target
                or (retained is not None and not _selected_birth_matches(value, retained["birth_id"]))):
            raise HostError("selected source observation changed its retained incarnation")
        if fact == "_process_start_identity" and pid != self.process.pid and self.listener.role_authority.reference()["target"] != retained:
            raise HostError("selected listener observation changed custody")
        if fact in {"current_source_revision", "current_source_content_digest"}:
            expected = self.profile["engine_launch"]["source_revision" if fact == "current_source_revision" else "source_content_digest"]
            if value is not None and value != expected:
                raise HostError("selected source observation differs from the selected checkout")
        return {**{k: v for k, v in frame.items() if k != "command"}, "status": "observed", "value": value}

    def _record_listener_exit(self, frame: Mapping[str, Any], actor: Any) -> dict[str, Any]:
        from astrid.core.execution import custody_broker as module
        from astrid.core.execution.vibecomfy_engine_adapter import LISTENER_EXIT_V2
        if frame.get("version") == LISTENER_EXIT_V2:
            return self._record_listener_exit_v2(frame, actor)
        required = {"version", "command", "operation_id", "channel_id", "owner_epoch", "role", "generation", "target", "exit_code"}
        if (set(frame) != required or frame.get("version") != module.ENGINE_CONTROL_VERSION
                or frame.get("command") != "observe_owned_listener_exit" or frame.get("role") != "engine_listener"
                or any(frame.get(k) != self.request[k] for k in ("operation_id", "channel_id", "owner_epoch"))
                or isinstance(frame.get("exit_code"), bool) or not isinstance(frame.get("exit_code"), int)
                or actor.verify() != self._engine_parent.verify()):
            raise HostError("native listener exit evidence has an invalid binding")
        reference = self.listener.role_authority.reference()
        if frame["generation"] != reference["generation"] or frame["target"] != reference["target"]:
            raise HostError("native listener exit evidence has stale custody")
        ack = {**{k: v for k, v in frame.items() if k != "command"}, "status": "exit_recorded"}
        if self._listener_exit is not None:
            if self._listener_exit != ack:
                raise HostError("native listener exit evidence replay conflicts")
            return dict(ack)
        self.listener.registration["exit_evidence"] = ack
        self.listener._persist("listener_exit_observed")  # fsynced before ACK
        self._listener_exit = ack
        return dict(ack)

    def _record_listener_exit_v2(self, frame: Mapping[str, Any], actor: Any) -> dict[str, Any]:
        from astrid.core.execution.vibecomfy_engine_adapter import LISTENER_EXIT_V2
        required = {"version", "command", "operation_id", "channel_id", "owner_epoch", "role", "generation", "target", "exit_code"}
        if (not self._enable_exit_v2 or set(frame) != required or frame.get("version") != LISTENER_EXIT_V2
                or frame.get("command") != "observe_owned_listener_exit" or frame.get("role") != "engine_listener"
                or any(frame.get(k) != self.request[k] for k in ("operation_id", "channel_id", "owner_epoch"))
                or type(frame.get("generation")) is not int or type(frame.get("exit_code")) is not int
                or actor.verify() != self._engine_parent.verify()):
            raise HostError("native listener exit-v2 evidence has an invalid binding")
        authority = self.listener.role_authority
        base = authority.ready_reference()  # Never promote an admission prefix.
        proof = authority.token_chain(actor=self.listener.cleanup_actor, generation=base["generation"], base_target=base["target"])
        targets = [base["target"], *[entry["ack"]["target"] for entry in proof["chain"]]]
        if frame["generation"] != base["generation"] or not any(_handoff_canonical(frame["target"]) == _handoff_canonical(target) for target in targets):
            raise HostError("native listener exit-v2 evidence is outside the retained ready chain")
        ack = {**{k: v for k, v in frame.items() if k != "command"}, "status": "exit_recorded",
               "current_reference": proof["current_reference"], "revision": proof["revision"],
               "refresh_proof_digest": proof["refresh_proof_digest"]}
        if self._listener_exit is not None:
            if _handoff_canonical(self._listener_exit) != _handoff_canonical(ack):
                raise HostError("native listener exit evidence replay conflicts")
            return json.loads(json.dumps(ack))
        self.listener.registration["exit_evidence"] = json.loads(json.dumps(ack))
        self.listener._persist("listener_exit_observed_v2")
        after = authority.token_chain(actor=self.listener.cleanup_actor, generation=base["generation"], base_target=base["target"])
        if _handoff_canonical(after) != _handoff_canonical(proof) or actor.verify() != self._engine_parent.verify():
            raise HostError("native listener exit-v2 chain changed before acknowledgement")
        self._listener_exit = ack
        return json.loads(json.dumps(ack))

    def report(self) -> dict[str, Any]:
        if self.process is None or self.process.poll() is not None or self._native_error is not None:
            raise HostError("prepared native graph is no longer observable")
        observe_tree(self.process)
        listener_pid = int(self.listener.registration["pid"])
        measured = self._measure_registry(Path(self.profile["engine_launch"]["session_root"]), self._launch_nonce, listener_pid)
        if measured is None:
            raise HostError("prepared engine readiness registry is incomplete")
        return {"processes": {"host": {"pid": os.getpid(), "birth_id": process_birth_identity()},
                              "engine": {"pid": self.process.pid, "birth_id": self.process._astrid_process_birth},
                              "engine_listener": {"pid": listener_pid, "birth_id": self.listener.registration["identity"]["birth_id"]}},
                "engine_binding": {"supervisor_pid": self.process.pid, "listener_pid": listener_pid, "listener_parent_pid": self.process.pid, "socket_owner_pid": listener_pid},
                "session_config_digest": self.profile["session_config_digest"],
                "custody_capabilities": {"engine": self.engine.role_authority.reference(), "engine_listener": self.listener.role_authority.reference()}}

    def _measure_registry(self, root: Path, nonce: str, listener_pid: int) -> dict[str, Any] | None:
        from astrid.core.execution.custody_broker import default_process_identity
        required = ("pid", "comfy_pid", "comfy_process_start_identity", "url", "config.json", "source_revision", "source_content_digest", "launch.json")
        if any(not (root / name).is_file() or (root / name).is_symlink() for name in required):
            return None
        data = {name: (root / name).read_text(encoding="utf-8").strip() for name in required if name not in {"launch.json", "config.json"}}
        marker = json.loads((root / "launch.json").read_text(encoding="utf-8"))
        launch = self.profile["engine_launch"]
        listener_identity = default_process_identity(listener_pid)
        if (data["pid"] != str(self.process.pid) or data["comfy_pid"] != str(listener_pid)
                or data["url"] != self.profile["engine_endpoint"] or marker.get("launch_token") != nonce
                or marker.get("pid") != self.process.pid or marker.get("url") != data["url"]
                or not _selected_birth_matches(marker.get("process_start_identity"), self.process._astrid_process_birth)
                or not _selected_birth_matches(data["comfy_process_start_identity"], self.custody._handles[listener_pid][0])
                or data["source_revision"] != launch["source_revision"] or data["source_content_digest"] != launch["source_content_digest"]
                or listener_identity is None or listener_identity.get("parent_pid") != self.process.pid
                or "sha256:" + hashlib.sha256((root / "config.json").read_bytes()).hexdigest() != self.profile["session_config_digest"]):
            raise HostError("owned engine readiness registry differs from selected launch")
        endpoint = urlsplit(self.profile["engine_endpoint"])
        observation = subprocess.run(["lsof", *(["-O"] if sys.platform == "darwin" else []), "-nP", "-a", "-p", str(listener_pid), "-iTCP:" + str(endpoint.port), "-sTCP:LISTEN"], capture_output=True, text=True, timeout=2, check=False)
        if observation.returncode != 0 or f":{endpoint.port} (LISTEN)" not in observation.stdout:
            raise HostError("selected listener socket ownership is uncertain")
        for broker in (self.engine, self.listener):
            if broker.state == "sealed":
                broker.bind_ready_token(expected_pid=int(broker.registration["pid"]), expected_identity=broker.registration["identity"])
        return {"session_dir": str(root), "server_url": data["url"], "pid": self.process.pid,
                "comfy_pid": listener_pid, "comfy_process_birth_id": data["comfy_process_start_identity"],
                "launch_token": nonce, "process_birth_id": self.process._astrid_process_birth,
                "source_revision": data["source_revision"], "source_content_digest": data["source_content_digest"],
                "config_digest": self.profile["session_config_digest"]}

    def abort(self) -> dict[str, Any]:
        if self._clean_ack is not None:
            return json.loads(json.dumps(self._clean_ack))
        if self.process is None:
            if self._engine_spawn_started or self.engine is None or self.listener is None:
                raise CleanupUncertainError("required engine graph never sealed; no clean proof")
            cleanup = {}
            for broker in (self.listener, self.engine):
                reference = broker.role_authority.reference()
                if set(reference["target"]) != {"admission_id"}:
                    raise CleanupUncertainError("unbound launch has an unresolved process registration")
                broker.abort_before_spawn()
                broker.state = "never-spawned"
                broker._persist("launch_never_spawned")
                cleanup[broker.role] = {"generation": reference["generation"], "target": reference["target"],
                                        "exit_code": None, "proof_kind": "never-spawned"}
            self._clean_ack = cleanup
            return json.loads(json.dumps(cleanup))
        # Let the selected daemon execute its original listener cleanup and
        # positively await/reap that exact child before the engine exits.
        if self.process.poll() is None:
            self._signal_owned_role(self.engine, signal.SIGTERM, expected_pid=self.process.pid)
        try:
            exit_code = self.process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            if self.listener is not None and self.listener._post_exec_authority_validated:
                self._signal_owned_role(self.listener, signal.SIGKILL, expected_pid=int(self.listener.registration["pid"]))
            raise CleanupUncertainError("engine cleanup deadline expired; obligations retained") from exc
        if self._listener_exit is None:
            try:
                self._cleanup_listener_without_wait_proof()
            except BaseException as exc:
                self._listener_cleanup_error = exc
                raise CleanupUncertainError("engine exited without authenticated retained listener wait proof; listener cleanup unresolved") from exc
            raise CleanupUncertainError("engine exited without authenticated retained listener wait proof")
        verify_tree_absent(self.process)
        root = Path(self.profile["engine_launch"]["session_root"])
        endpoint = urlsplit(self.profile["engine_endpoint"])
        observation = subprocess.run(["lsof", *(["-O"] if sys.platform == "darwin" else []), "-nP", "-t", "-iTCP:" + str(endpoint.port), "-sTCP:LISTEN"], capture_output=True, text=True, timeout=2, check=False)
        if observation.returncode != 1 or observation.stdout.strip() or observation.stderr.strip():
            raise CleanupUncertainError("engine endpoint cleanup is not verified")
        # Registry files are historical evidence only; successful census and
        # retained child wait proofs reconcile the exact recorded incarnations.
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise CleanupUncertainError("retained engine exit code is unavailable")
        engine_reference = self.engine.role_authority.reference()
        listener_exit_reference = self._listener_exit.get("current_reference")
        listener_exit_target = listener_exit_reference["target"] if listener_exit_reference is not None else self._listener_exit["target"]
        self._clean_ack = {"engine": {"generation": engine_reference["generation"], "target": engine_reference["target"],
                                      "exit_code": exit_code, "proof_kind": "retained-child-exit"},
                           "engine_listener": {"generation": self._listener_exit["generation"], "target": listener_exit_target,
                                               "exit_code": self._listener_exit["exit_code"], "proof_kind": "authenticated-retained-child-exit"}}
        if self.engine.operation_channel is not None:
            self.engine.operation_channel.close()
        return json.loads(json.dumps(self._clean_ack))

    @staticmethod
    def _signal_owned_role(broker: Any, signum: int, *, expected_pid: int) -> None:
        # These helpers both enforce the current actor, target and generation
        # through the kernel call. Healthy custody is not failed admission.
        if broker is None:
            raise CleanupUncertainError("native role cleanup authority is unavailable")
        if broker.error is None and broker.state in {"sealed", "ready-bound"}:
            broker.signal(signum, expected_pid=expected_pid)
        elif broker.error is not None and broker._post_exec_authority_validated and broker.registration is not None:
            broker.signal_failed_admission(signum, expected_pid=expected_pid)
        else:
            raise CleanupUncertainError("native role cleanup has no validated current authority")

    def _cleanup_listener_without_wait_proof(self) -> None:
        listener = self.listener
        if listener is None or listener.registration is None:
            raise CleanupUncertainError("listener cleanup lacks retained registration")
        pid = int(listener.registration["pid"])
        expected = listener.registration["identity"]
        self._signal_owned_role(listener, signal.SIGTERM, expected_pid=pid)
        deadline = time.monotonic() + 1.0  # Existing descendant TERM grace.
        while time.monotonic() < deadline:
            # Observation and waits grant no authority and occur outside the
            # signal guard. Absence still cannot supply a retained wait proof.
            observed = listener._observe_registered_identity(pid)
            if observed is None:
                return
            if any(observed.get(key) != expected.get(key) for key in ("pid", "uid", "birth_id")):
                raise CleanupUncertainError("listener incarnation changed during cleanup")
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        self._signal_owned_role(listener, signal.SIGKILL, expected_pid=pid)


def _retain_unresolved_owner(preparation: LocalExecutionPreparation) -> None:
    """Keep the actual owner/control service alive until positive graph cleanup."""
    while not preparation._aborted:
        try:
            preparation.abort()
        except BaseException as exc:
            preparation.control_error = exc
            time.sleep(1)


def _read_activation_frame(control: socket.socket, *, limit: int = _ACTIVATION_FRAME_LIMIT) -> dict[str, Any]:
    """Read one bounded private frame; never consume a second frame silently."""
    frame = bytearray()
    while b"\n" not in frame:
        chunk = control.recv(min(4096, limit + 1 - len(frame)))
        if not chunk:
            raise HostError("parked activation channel closed before a frame")
        frame.extend(chunk)
        if len(frame) > limit:
            raise HostError("parked activation frame is too large")
    encoded, remainder = bytes(frame).split(b"\n", 1)
    if remainder:
        raise HostError("parked activation channel carried multiple frames")
    try:
        value = json.loads(encoded.decode("utf-8"),
                           object_pairs_hook=_handoff_reject_duplicate_pairs,
                           parse_constant=_handoff_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HostError("parked activation frame is malformed") from exc
    if not isinstance(value, dict):
        raise HostError("parked activation frame must be an object")
    json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return value


def _send_activation_frame(control: socket.socket, value: Mapping[str, Any]) -> None:
    control.sendall(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    )


def _await_worker_activation(
    descriptor: int,
    *,
    operation_id: str,
    channel_id: str,
    credential_file: str,
    timeout_seconds: float,
    require_receipt: bool = False,
) -> dict[str, Any]:
    """Accept one Runtime grant over the Worker's inherited private socket.

    This is intentionally the first active operation in parked CLI mode.  The
    credential path is only a reference until the message, channel, and actual
    process birth identity have all been accepted. Local owner grants relay
    acceptance over this same socket: the owner invokes Runtime's bound callback
    and confirms its committed receipt before this receiver sends the final ACK.
    Bootstrap grants only acknowledge private delivery. Runtime enables their
    credential for registration and independently qualifies the host before
    committing activation; this ACK grants no claim authority. Legacy provider
    grants retain their one-grant/one-ACK exchange.
    """

    if descriptor < 3 or not operation_id or not channel_id:
        raise HostError("parked activation channel identity is invalid")
    if timeout_seconds <= 0 or timeout_seconds > 900:
        raise HostError("parked activation timeout is invalid")
    expected_credential = Path(credential_file).expanduser()
    if not expected_credential.is_absolute():
        raise HostError("parked credential reference must be absolute")
    actual_birth = process_birth_identity()
    if not actual_birth:
        raise HostError("parked host process birth identity is unavailable")

    control = socket.socket(fileno=descriptor)
    try:
        control.settimeout(timeout_seconds)
        grant = _read_activation_frame(control)
        identified_grant = "acceptance_mode" in grant
        local_receipt = grant.get("acceptance_mode") == _ACTIVATION_RECEIPT_MODE
        expected_keys = {
            "version", "operation_id", "channel_id", "credential_file",
            "executor_incarnation", "evidence_digest", "host",
        }
        if identified_grant:
            expected_keys |= {"acceptance_mode", "activation_id"}
        if set(grant) != expected_keys:
            raise HostError("parked activation grant has an invalid shape")
        if require_receipt and not local_receipt:
            raise HostError("local parked activation requires a Runtime acceptance receipt")
        if identified_grant and (
            grant["acceptance_mode"] not in {_ACTIVATION_RECEIPT_MODE, _ACTIVATION_BOOTSTRAP_MODE}
            or not isinstance(grant["activation_id"], str)
            or not grant["activation_id"]
            or len(grant["activation_id"]) > 256
        ):
            raise HostError("parked activation receipt identity is invalid")
        if grant["version"] != _ACTIVATION_VERSION:
            raise HostError("parked activation grant version is invalid")
        if grant["operation_id"] != operation_id or grant["channel_id"] != channel_id:
            raise HostError("parked activation grant came from the wrong private channel")
        if Path(str(grant["credential_file"])).expanduser() != expected_credential:
            raise HostError("parked activation credential reference is invalid")
        incarnation = grant["executor_incarnation"]
        if not isinstance(incarnation, str) or not incarnation or len(incarnation) > 256:
            raise HostError("parked activation executor incarnation is invalid")
        digest = grant["evidence_digest"]
        if not isinstance(digest, str) or not _ACTIVATION_DIGEST.fullmatch(digest):
            raise HostError("parked activation evidence digest is invalid")
        host = grant["host"]
        if (
            not isinstance(host, dict)
            or set(host) != {"pid", "birth_id"}
            or host["pid"] != os.getpid()
            or host["birth_id"] != actual_birth
        ):
            raise HostError("parked activation host process identity is invalid")
        if local_receipt:
            request = {
                "version": _ACTIVATION_REQUEST_VERSION,
                "operation_id": operation_id,
                "channel_id": channel_id,
                "grant": {key: grant[key] for key in (
                    "activation_id", "credential_file", "executor_incarnation", "evidence_digest",
                )},
                "host": {"pid": os.getpid(), "birth_id": actual_birth},
            }
            _send_activation_frame(control, request)
            receipt = _read_activation_frame(control)
            if receipt != {**request, "version": _ACTIVATION_RECEIPT_VERSION}:
                raise HostError("parked activation Runtime acceptance receipt is mismatched")
            # The owner half-closes only its send direction after the receipt.
            # EOF fences duplicate or trailing confirmations, including frames
            # delivered in separate reads, while leaving our ACK direction open.
            if control.recv(1):
                raise HostError("parked activation Runtime acceptance receipt was duplicated")
        accepted = {
            "version": _ACTIVATION_ACCEPTED_VERSION,
            "operation_id": operation_id,
            "channel_id": channel_id,
            "executor_incarnation": incarnation,
            "evidence_digest": digest,
            "host": host,
        }
        if identified_grant:
            accepted["activation_id"] = grant["activation_id"]
        try:
            _send_activation_frame(control, accepted)
        except OSError:
            if not local_receipt:
                raise
            # Runtime already committed acceptance. Stay in this incarnation
            # for its independent reobservation and authenticated enablement.
        return grant
    except (OSError, socket.timeout) as exc:
        raise HostError("parked activation channel failed") from exc
    finally:
        control.close()


def _await_worker_activation_socket(
    socket_path: str,
    *,
    operation_id: str,
    channel_id: str,
    credential_file: str,
    timeout_seconds: float,
    require_receipt: bool = False,
) -> dict[str, Any]:
    """Accept one private remote bootstrap over a short-lived Unix socket.

    The GenericPackHost itself owns this listener; no sidecar supervisor or
    ambient network port is created. The coordinator connects through the
    existing SSH command boundary and sends the same bounded activation frame
    used by local parked hosts.
    """
    path = Path(socket_path).expanduser()
    if not path.is_absolute() or str(path) != socket_path or path.is_symlink():
        raise HostError("parked activation socket path must be canonical and absolute")
    if len(os.fsencode(path)) >= 108:
        raise HostError("parked activation socket path exceeds the Linux Unix-socket limit")
    parent = path.parent
    for ancestor in (parent, *parent.parents):
        if ancestor.is_symlink():
            raise HostError("parked activation socket path traverses a symlink")
    if path.exists():
        raise HostError("parked activation socket path already exists")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    created: os.stat_result | None = None
    try:
        parent.mkdir(mode=0o700)
        parent_info = parent.stat()
        if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid()
                or stat.S_IMODE(parent_info.st_mode) != 0o700):
            raise HostError("parked activation socket directory is not private")
        listener.bind(str(path))
        os.chmod(path, 0o600)
        created = path.lstat()
        if (not stat.S_ISSOCK(created.st_mode) or created.st_uid != os.getuid()
                or stat.S_IMODE(created.st_mode) != 0o600):
            raise HostError("parked activation socket identity is unsafe")
        listener.listen(1)
        listener.settimeout(timeout_seconds)
        control, _ = listener.accept()
        return _await_worker_activation(
            control.detach(), operation_id=operation_id, channel_id=channel_id,
            credential_file=credential_file, timeout_seconds=timeout_seconds,
            require_receipt=require_receipt,
        )
    except HostError:
        raise
    except (OSError, socket.timeout) as exc:
        raise HostError("parked activation channel failed") from exc
    finally:
        listener.close()
        if created is not None:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    path.unlink()
            except OSError:
                pass


def _await_enabled_runtime_credential(
    client: "RuntimeProtocolClient", *, timeout_seconds: float,
    executor_id: str = "astrid-pack-host",
) -> None:
    """Hold activated startup until Runtime publishes the disabled bearer."""

    deadline = time.monotonic() + max(0.05, min(float(timeout_seconds), 30.0))
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client._authenticate_worker(executor_id)
            return
        except Exception as exc:  # Runtime owns the short publication race.
            last_error = exc
        time.sleep(0.02)
    raise HostError("activated Runtime credential was not enabled") from last_error


_emit_startup_phase("module_import_done")


def _cli() -> int:

    parser = argparse.ArgumentParser(prog="astrid-generic-host")
    parser.add_argument("command", nargs="?", choices=("run", "supervise"), help="run the worker loop or JSONL supervisor")
    parser.add_argument("--pack-root", action="append", required=True, help="pack/executor root to discover")
    parser.add_argument("--runtime-endpoint")
    parser.add_argument("--credential-file", help="owner-only file containing the worker bearer credential")
    parser.add_argument("--executor-id", default="astrid-pack-host")
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--attempt-root")
    parser.add_argument("--attempt-base", help="host-owned base directory; allocate one isolated child per task attempt")
    parser.add_argument("--capability-matrix")
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--run-task")
    parser.add_argument("--lease-token")
    parser.add_argument("--keep-attempt", action="store_true")
    parser.add_argument("--once", action="store_true", help="claim at most one task and exit")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--max-tasks", type=int)
    parser.add_argument("--supervisor-state", help="persistent JSONL supervisor journal")
    parser.add_argument("--max-frame-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--ready-file", help="write host registration/readiness metadata before entering the claim loop")
    parser.add_argument("--source-checkout", help="absolute source checkout bound to this host")
    parser.add_argument("--source-checkout-digest", help="expected digest for the source checkout's pack tree")
    parser.add_argument("--source-closure-digest", help="expected digest for the executable host/source closure")
    parser.add_argument("--support-root", help="absolute runtime support directory bound to this host")
    parser.add_argument("--runtime-instance-id", help="runtime instance identity bound to this host")
    parser.add_argument("--source-inventory-identity", help="verified managed source inventory identity bound to this host")
    parser.add_argument("--boot-manifest-path", help="existing explicit boot-manifest path")
    parser.add_argument("--remotion-validation-launch-id", help=argparse.SUPPRESS)
    parser.add_argument("--boot-manifest-hash", help="expected SHA-256 hash of the boot manifest")
    parser.add_argument("--readiness-profile-path", help="Worker-published HC-03 readiness profile")
    parser.add_argument("--readiness-profile-hash", help="expected SHA-256 hash of the readiness profile")
    parser.add_argument("--execution-target-json", help="explicit target JSON used for claim binding")
    parser.add_argument(
        "--require-target-attestation",
        action="store_true",
        help="fail startup unless an exact execution target is configured",
    )
    parser.add_argument("--host-control-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--activation-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--activation-socket", help=argparse.SUPPRESS)
    parser.add_argument("--activation-operation-id", help=argparse.SUPPRESS)
    parser.add_argument("--activation-channel-id", help=argparse.SUPPRESS)
    parser.add_argument(
        "--activation-timeout-seconds", type=float, default=120.0, help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    _emit_startup_phase("cli_args_parsed")
    from astrid.core.env_vars import ASTRID_REMOTION_VALIDATION_LAUNCH, ASTRID_REMOTION_VALIDATION_ROLE
    declared_validation = os.environ.get(ASTRID_REMOTION_VALIDATION_LAUNCH)
    if declared_validation != args.remotion_validation_launch_id:
        parser.error("Remotion readiness reference belongs to a foreign launch")
    if declared_validation and os.environ.get(ASTRID_REMOTION_VALIDATION_ROLE) != "host":
        parser.error("Remotion readiness context is not the host launch context")
    if args.attempt_root and args.attempt_base:
        parser.error("--attempt-root and --attempt-base are mutually exclusive")
    if (args.readiness_profile_path is None) != (args.readiness_profile_hash is None):
        parser.error("--readiness-profile-path and --readiness-profile-hash must be supplied together")
    has_activation_transport = args.activation_fd is not None or args.activation_socket is not None
    if (args.activation_fd is not None and args.activation_socket is not None) or (
        has_activation_transport != (args.activation_operation_id is not None
                                     and args.activation_channel_id is not None)
    ) or ((args.activation_operation_id is None) != (args.activation_channel_id is None)):
        parser.error("parked activation arguments must be supplied together")
    target_requested = bool(
        args.execution_target_json is not None
        or os.environ.get("ASTRID_EXECUTION_TARGET_JSON", "").strip()
    )
    if target_requested and not has_activation_transport:
        parser.error("targeted execution requires Worker-supervised activation")
    preparation = None
    if args.host_control_fd is not None:
        if not has_activation_transport or args.host_control_fd < 3 or args.host_control_fd == args.activation_fd:
            parser.error("local host-control requires a distinct parked activation transport")
        from astrid.core.execution import hosted_contract
        hosted_binding = hosted_contract.consume('host', {'control': args.host_control_fd, 'activation': args.activation_fd})
        preparation = LocalExecutionPreparation(operation_id=args.activation_operation_id, channel_id=args.activation_channel_id)
        preparation._hosted_binding = hosted_binding
        preparation.control_thread = preparation.serve_control(args.host_control_fd)
        def parked_shutdown(signum, _frame):
            try:
                preparation.abort()
            except BaseException as exc:
                preparation.control_error = exc
                return  # Unresolved custody keeps this designated owner alive.
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, parked_shutdown)
        signal.signal(signal.SIGINT, parked_shutdown)
    activation = None
    if has_activation_transport:
        if not args.credential_file:
            parser.error("parked activation requires --credential-file")
        try:
            target_json = args.execution_target_json or os.environ.get("ASTRID_EXECUTION_TARGET_JSON")
            target = json.loads(target_json) if target_json else {}
            if not isinstance(target, Mapping):
                raise HostError("parked activation target must be an object")
            activation_args = {
                "operation_id": args.activation_operation_id,
                "channel_id": args.activation_channel_id,
                "credential_file": args.credential_file,
                "timeout_seconds": args.activation_timeout_seconds,
                "require_receipt": target.get("kind") == "machine" or (preparation is not None and
                    (preparation._request or {}).get("profile", {}).get("execution_mode") == "host_only_cpu"),
            }
            if args.activation_fd is not None:
                activation = _await_worker_activation(args.activation_fd, **activation_args)
            else:
                activation = _await_worker_activation_socket(
                    args.activation_socket, **activation_args
                )
            if preparation is not None and (preparation.result is None or preparation._aborted):
                raise HostError("local activation arrived before verified preparation")
            if preparation is not None:
                preparation.retain_activation_grant(activation)
        except (HostError, json.JSONDecodeError) as exc:
            if preparation is not None:
                try:
                    preparation.abort()
                except BaseException as cleanup_error:
                    preparation.control_error = cleanup_error
                    _retain_unresolved_owner(preparation)
            parser.error(str(exc))
    if preparation is not None and preparation.graph is not None:
        base_profile = {}
        if args.readiness_profile_path:
            original = Path(args.readiness_profile_path)
            if original.is_symlink() or not original.is_file():
                parser.error("selected base readiness profile is unavailable")
            raw = original.read_bytes()
            if "sha256:" + hashlib.sha256(raw).hexdigest() != args.readiness_profile_hash:
                parser.error("selected base readiness profile hash differs")
            base_profile = json.loads(raw)
        base_profile.update(preparation.graph.readiness_profile or {})
        generated = Path(preparation._request["custody_scope"]) / "host-readiness-profile.json"
        _write_ready_marker(generated, base_profile)
        args.readiness_profile_path = str(generated)
        args.readiness_profile_hash = "sha256:" + hashlib.sha256(generated.read_bytes()).hexdigest()
    verified_model_root: ModelRootBinding | None = None
    if args.readiness_profile_path is not None:
        readiness_path = Path(args.readiness_profile_path).expanduser()
        if (
            not readiness_path.is_absolute()
            or readiness_path.is_symlink()
            or not readiness_path.is_file()
        ):
            parser.error("--readiness-profile-path must be an absolute non-symlink regular file")
        actual_readiness_hash = "sha256:" + hashlib.sha256(readiness_path.read_bytes()).hexdigest()
        if args.readiness_profile_hash != actual_readiness_hash:
            parser.error("--readiness-profile-hash does not match the readiness profile")
        os.environ["ASTRID_HOST_READINESS_PROFILE_PATH"] = str(readiness_path)
        os.environ["ASTRID_HOST_READINESS_PROFILE_HASH"] = actual_readiness_hash
        # Make an explicitly selected local VibeComfy candidate visible to
        # the host's approved dependency-path resolver as well as its child.
        # Without this, the host would reject the candidate while constructing
        # PYTHONPATH before it ever reached the child boundary.
        try:
            profile = json.loads(readiness_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            parser.error(f"readiness profile is unreadable: {exc}")
        candidate = profile.get("vibecomfy_candidate") if isinstance(profile, Mapping) else None
        session = profile.get("vibecomfy_session") if isinstance(profile, Mapping) else None
        if isinstance(session, Mapping):
            revision = session.get("source_revision")
            content_digest = session.get("source_content_digest")
            if isinstance(revision, str) and isinstance(content_digest, str):
                os.environ[VIBECOMFY_ATTESTED_REVISION_ENV] = revision
                os.environ[VIBECOMFY_ATTESTED_CONTENT_DIGEST_ENV] = content_digest
        try:
            qualification_receipt_dir = (
                Path(args.support_root).expanduser() / "model-verification"
                if args.support_root
                else None
            )
            verified_model_root = model_root_binding_from_profile(
                profile,
                verify_files=True,
                qualification_receipt_dir=qualification_receipt_dir,
            )
        except ModelRootBindingError as exc:
            parser.error(f"readiness profile model-root binding is invalid: {exc}")
        os.environ[MODEL_ROOT_ENV] = str(verified_model_root.path)
        os.environ[MODEL_ROOT_BINDING_ENV] = json.dumps(
            verified_model_root.as_dict(), sort_keys=True, separators=(",", ":")
        )
        if qualification_receipt_dir is not None:
            os.environ[MODEL_ROOT_QUALIFICATION_RECEIPT_DIR_ENV] = str(
                qualification_receipt_dir
            )
        if isinstance(candidate, Mapping) and candidate.get("kind") == "local_snapshot":
            revision = candidate.get("revision")
            content_digest = candidate.get("source_content_digest")
            if isinstance(revision, str) and isinstance(content_digest, str):
                os.environ["ASTRID_VIBECOMFY_CANDIDATE_KIND"] = "local_snapshot"
                os.environ["ASTRID_VIBECOMFY_CANDIDATE_REVISION"] = revision
                os.environ["ASTRID_VIBECOMFY_CANDIDATE_CONTENT_DIGEST"] = content_digest
    ready_path = Path(args.ready_file).expanduser() if args.ready_file else None
    if ready_path is not None and (not ready_path.is_absolute() or ready_path.is_symlink()):
        parser.error("--ready-file must be an absolute non-symlink path")
    credential = None
    credential_path = None
    if args.credential_file:
        credential_path = Path(args.credential_file).expanduser()
        try:
            if (not credential_path.is_absolute() or credential_path.is_symlink()
                    or not credential_path.is_file()
                    or credential_path.stat().st_mode & 0o777 != 0o600):
                raise OSError("credential file must be an absolute owner-only regular file")
            credential = credential_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            parser.error(str(exc))
        if not credential:
            parser.error("credential file is empty")
    boot_manifest, boot_manifest_hash = _compose_cli_boot_manifest(args, parser)
    client = RuntimeProtocolClient(args.runtime_endpoint, credential) if args.runtime_endpoint else None
    if activation is not None:
        if client is None:
            parser.error("parked activation requires --runtime-endpoint")
        try:
            _await_enabled_runtime_credential(
                client, timeout_seconds=args.activation_timeout_seconds,
                executor_id=args.executor_id,
            )
        except HostError as exc:
            parser.error(str(exc))
    if args.source_checkout:
        source_checkout = Path(args.source_checkout).expanduser()
        if not source_checkout.is_absolute() or source_checkout.is_symlink() or not source_checkout.is_dir():
            parser.error("--source-checkout must be an absolute non-symlink directory")
    else:
        source_checkout = None
    if args.support_root:
        support_root = Path(args.support_root).expanduser()
        if not support_root.is_absolute() or support_root.is_symlink() or not support_root.is_dir():
            parser.error("--support-root must be an absolute non-symlink directory")
    else:
        support_root = None
    if support_root is not None and args.command in ("run", "supervise"):
        start_stdio_rotator(support_root)

    if args.execution_target_json is not None:
        os.environ["ASTRID_EXECUTION_TARGET_JSON"] = args.execution_target_json
    _emit_startup_phase("startup_identity_begin")
    try:
        identity_attestation = _startup_identity_attestation(
            source_checkout=source_checkout,
            source_inventory_identity=args.source_inventory_identity,
            expected_source_checkout_digest=args.source_checkout_digest,
            expected_source_closure_digest=args.source_closure_digest,
            boot_manifest_hash=boot_manifest_hash,
            require_target=args.require_target_attestation,
        )
    except HostError as exc:
        parser.error(str(exc))

    _emit_startup_phase("startup_identity_done")
    output_root = os.environ.get("ASTRID_OUTPUT_ROOT")
    if activation is not None and activation.get("acceptance_mode") == _ACTIVATION_BOOTSTRAP_MODE:
        if not output_root or not Path(output_root).is_absolute() or Path(output_root).is_symlink():
            parser.error("activated host requires an absolute non-symlink output root")
        if args.attempt_root or args.attempt_base:
            parser.error("bootstrap host attempt directories are owned by its output root")
        try:
            with tempfile.TemporaryFile(dir=output_root) as output_probe:
                output_probe.write(b"ready")
                output_probe.flush()
        except OSError as exc:
            parser.error(f"activated host output root is not writable: {exc}")
        args.attempt_base = str(Path(output_root) / "attempts")
    elif (preparation is not None and preparation.result is not None
          and not preparation._aborted and preparation._activation_grant == activation
          and activation is not None
          and (preparation._request or {}).get("profile", {}).get("execution_mode") == "host_only_cpu"):
        # The retained preparation and accepted private activation own these paths;
        # an ambient output-root setting alone never grants attempt ownership.
        selected_support = preparation._request["profile"].get("support_root")
        if (support_root is None or str(support_root) != selected_support
                or support_root.resolve() != support_root):
            parser.error("host-only support root differs from retained preparation")
        if args.attempt_root or args.attempt_base:
            parser.error("host-only attempt directories are owned by its output root")
        owned_output = support_root / "host-output"
        if (not output_root or Path(output_root) != owned_output
                or owned_output.resolve() != owned_output or not owned_output.is_dir()
                or owned_output.stat().st_uid != os.getuid()):
            parser.error("host-only output root must be the canonical owned host-output directory")
        owned_attempts = owned_output / "attempts"
        if (owned_attempts.resolve() != owned_attempts
                or (owned_attempts.exists() and (not owned_attempts.is_dir()
                    or owned_attempts.stat().st_uid != os.getuid()))):
            parser.error("host-only attempts directory must be canonical and owned")
        try:
            for directory in (owned_output, owned_attempts) if owned_attempts.exists() else (owned_output,):
                with tempfile.TemporaryFile(dir=directory) as output_probe:
                    output_probe.write(b"ready")
                    output_probe.flush()
        except OSError as exc:
            parser.error(f"host-only attempt directory is not writable: {exc}")
        args.attempt_base = str(owned_attempts)

    _emit_startup_phase("host_constructor_begin")
    _install_host_vibecomfy_import_root()
    host = GenericPackHost(
        pack_roots=args.pack_root,
        client=client,
        executor_id=args.executor_id,
        max_concurrency=args.max_concurrency,
        attempt_root=args.attempt_root,
        attempt_base=args.attempt_base,
        capability_matrix=args.capability_matrix,
        source_inventory_identity=args.source_inventory_identity,
        boot_manifest_path=boot_manifest,
        boot_manifest_hash=boot_manifest_hash,
    )
    _emit_startup_phase("host_constructor_done")
    host._native_engine_graph = preparation.graph if preparation is not None else None
    if preparation is not None:
        preparation.attach_claim_host(host)
    if support_root is not None:
        host.cleanup_latch_path = support_root / CLEANUP_LATCH_NAME
        host.evidence_status_path = support_root / EVIDENCE_STATUS_NAME
        host._evidence_observation_binding = {
            "process_birth_id": process_birth_identity(),
            "runtime_instance_id": args.runtime_instance_id,
            "source_closure_digest": identity_attestation["source"].get("closure_digest"),
            "ready_file": str(ready_path) if ready_path is not None else None,
        }
    _emit_startup_phase("discovery_begin")
    host.discover()
    _emit_startup_phase("discovery_done")
    if not args.register:
        # Registered startup performs its mandatory full preflight inside the
        # registration fence; an eager copy here repeats the same validation.
        _emit_startup_phase("preflight_begin")
        host.preflight()
        _emit_startup_phase("preflight_done")

    def handle_shutdown(_signum, _frame):
        host.shutdown()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    # `kill -USR1 <pid>` writes every thread's Python stack to the host log,
    # so a host that looks stuck can be diagnosed without stopping it.
    if hasattr(signal, "SIGUSR1"):
        try:
            faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
        except (OSError, ValueError, RuntimeError):
            # stderr has no file descriptor (an embedding or test harness).
            pass
    registration = None
    if args.register or not args.run_task:
        _emit_startup_phase("registration_begin")
        try:
            registration = host.register() if args.register else {"capabilities": [record.manifest() for record in host.capabilities.values()]}
        except Exception as exc:
            code = str(getattr(exc, "code", "host_registration_failed"))[:128]
            request_id = str(getattr(exc, "request_id", ""))[:128]
            raw_message = getattr(exc, "message", None)
            message = (
                str(raw_message)
                if raw_message
                else (str(exc) if isinstance(exc, HostError) else "executor registration failed")
            )[:512]
            if credential:
                message = message.replace(credential, "[redacted]")
            failure = {
                "status": "failed",
                "terminal": True,
                "pid": os.getpid(),
                "process_birth_id": process_birth_identity(),
                "error": {
                    "code": code,
                    "request_id": request_id,
                    "message": message,
                },
            }
            if ready_path is not None:
                _write_ready_marker(ready_path, failure)
            print(json.dumps(failure, sort_keys=True), file=sys.stderr, flush=True)
            return 1
        _emit_startup_phase("registration_done")
        # One line per start: the full registration (every capability's definition, ~0.6 MB) is in
        # the ready file; printing it made each host start add megabytes to generic-host.log.
        unready = {record.id: _preflight_unavailable_reason(record) or "unready"
                   for record in host.capabilities.values() if not record.ready}
        print(json.dumps({
            "event": "registered", "executor_id": host.executor_id, "pid": os.getpid(),
            "capabilities": len(host.capabilities), "ready": len(host.capabilities) - len(unready),
            "unready": unready, "withdrawn": registration.get("withdrawn_capabilities") or [],
            "full_registration": str(ready_path) if ready_path is not None else None,
        }, sort_keys=True, default=_json_safe), flush=True)
    if ready_path is not None:
        ready_payload = {
            "status": "ready",
            "remotion_validation": {k: v for k, v in os.environ.items() if k.startswith("ASTRID_REMOTION_VALIDATION_")} or None,
            "python_executable": os.path.abspath(sys.executable),
            "pid": os.getpid(),
            "process_birth_id": process_birth_identity(),
            "endpoint": str(args.runtime_endpoint).rstrip("/") if args.runtime_endpoint else None,
            "executor_id": host.executor_id,
            "output_root": output_root,
            "attempt_base": str(host.attempt_base) if host.attempt_base else None,
            "session_ref": os.environ.get("ASTRID_SESSION_REF"),
            "session_config_digest": os.environ.get("ASTRID_SESSION_CONFIG_DIGEST"),
            "data_root": os.environ.get("BANODOCO_LOCAL_DATA_ROOT"),
            "capability_count": len(host.capabilities),
            "ready_capabilities": sorted(record.id for record in host.capabilities.values() if record.ready),
            "unready_capabilities": sorted(record.id for record in host.capabilities.values() if not record.ready),
            "registration": registration,
            "ready_file": str(ready_path),
            "credential_file": str(credential_path) if credential_path else None,
            "support_root": str(support_root) if support_root else None,
            "source_checkout": str(source_checkout) if source_checkout else None,
            "source_checkout_digest": source_checkout_digest(source_checkout) if source_checkout else None,
            "source_closure_digest": identity_attestation["source"].get("closure_digest"),
            "source_inventory_identity": host.source_inventory_identity,
            "boot_manifest_path": str(boot_manifest),
            "boot_manifest_hash": boot_manifest_hash,
            "source_epoch": host.source_epoch,
            "runtime_instance_id": args.runtime_instance_id,
            "runtime_epoch": host.runtime_state.get("runtime_epoch"),
            "schema_digest": host.runtime_state.get("schema_digest"),
            "identity_attestation": identity_attestation,
            "model_root_binding": (
                verified_model_root.as_dict() if verified_model_root is not None else None
            ),
            "activation": {
                key: activation[key]
                for key in (
                    "version", "operation_id", "channel_id", "activation_id",
                    "executor_incarnation", "evidence_digest", "host",
                )
            }
            if activation is not None
            else None,
        }
        _write_ready_marker(ready_path, ready_payload)
        _emit_startup_phase("ready_written")
    if args.run_task:
        if client is None or not args.lease_token:
            parser.error("--run-task requires --runtime-endpoint, --credential-file, and --lease-token")
        task = client.task(args.run_task)
        print(json.dumps(host.run_task(task, lease_token=args.lease_token, keep_attempt=args.keep_attempt), indent=2, sort_keys=True))
    if args.command == "run":
        if client is None:
            parser.error("run requires --runtime-endpoint and --credential-file")
        if not args.register:
            host.register()
        print(json.dumps(host.run(once=args.once, poll_seconds=args.poll_seconds, max_tasks=args.max_tasks), indent=2, sort_keys=True, default=str))
    if args.command == "supervise":
        if client is None:
            parser.error("supervise requires --runtime-endpoint and --credential-file")
        if not args.supervisor_state:
            parser.error("supervise requires --supervisor-state")
        supervisor = host.persistent_supervisor(
            args.supervisor_state,
            max_frame_bytes=args.max_frame_bytes,
        )
        supervisor.serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
