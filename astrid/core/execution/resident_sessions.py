"""Retained pack-worker adapter for the host's existing managed session slot.

This module owns no admissions, task queue, Runtime client or restart policy.
ManagedToolSession owns its token/generation; task_scope owns the native child.
Only the immutable source capture, bounded wire protocol and job context differ
from the disposable worker. The initial pack protocol is deliberately serial.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, Callable, Mapping

from .managed_tool_session import (
    AdmissionToken,
    CapabilityDescriptor,
    SessionBinding,
    StaleAdmissionError,
)
from .pipe_io import PipeReader, PipeWriter, close_pipe_workers

PROTOCOL = "astrid.pack-session.v1"
MAX_FRAME_BYTES = 1024 * 1024
_DECLARATION = {"kind": "resident", "protocol": PROTOCOL}


class ResidentProtocolError(RuntimeError):
    """The retained worker cannot supply evidence for the admitted job."""


@dataclass(frozen=True)
class ResidentJobContext:
    """Job-local data only; never a Runtime credential or publication client."""

    task_id: str
    attempt_id: str
    fence: int
    runtime_epoch: int | None
    attempt_root: Path
    output_root: Path
    cancelled: Callable[[], bool]


_JOB: ContextVar[ResidentJobContext | None] = ContextVar("astrid_resident_job", default=None)


def current_job() -> ResidentJobContext:
    """Return this action's context; retained/background code has no job grant."""
    value = _JOB.get()
    if value is None or value.cancelled():
        raise ResidentProtocolError("no resident job is active in this context")
    return value


def declared_resident(record: Any) -> bool:
    metadata = record.definition.metadata
    declaration = metadata.get("action_declaration")
    return (isinstance(declaration, Mapping)
            and declaration.get("session") == _DECLARATION
            and isinstance(metadata.get("action_invocation"), Mapping)
            and metadata["action_invocation"].get("kind") == "python"
            and record.definition.command is None)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def token_identity(token: AdmissionToken) -> dict[str, Any]:
    return {"token_id": token.token_id, "invocation_id": token.invocation_id,
            "session_id": token.session_id, "generation": token.generation,
            "binding_identity": list(token.binding_identity)}


def encode_frame(frame: Mapping[str, Any]) -> bytes:
    data = json.dumps(frame, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    if len(data) > MAX_FRAME_BYTES:
        raise ResidentProtocolError("resident frame exceeds its bound")
    return data


def decode_frame(raw: bytes) -> dict[str, Any]:
    if not raw.endswith(b"\n") or len(raw) > MAX_FRAME_BYTES:
        raise ResidentProtocolError("resident frame is incomplete or exceeds its bound")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ResidentProtocolError("resident frame repeats a field")
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ResidentProtocolError("non-finite resident value")))
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
        raise ResidentProtocolError("unsupported resident protocol")
    return value


def installation_identity(host: Any) -> dict[str, Any]:
    """Existing verified install/launch generations, with interpreter identity.

    These are invalidation inputs, not a replacement for Runtime install custody.
    Development sources are separately content-checked on every dispatch.
    """
    executable = Path(sys.executable).resolve(strict=True)
    info = executable.stat()
    root = Path(__file__).resolve().parents[3]
    lock = root / "runtime.lock.json"
    boot = host.boot_manifest_path
    return {
        "interpreter": str(executable), "prefix": str(Path(sys.prefix).resolve()),
        "interpreter_stat": [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns],
        "agent_root": str(root),
        "runtime_lock": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.is_file() else None,
        "boot_manifest": host.boot_manifest_hash,
        "boot_bytes": hashlib.sha256(boot.read_bytes()).hexdigest() if boot is not None else None,
        "source_inventory": host.source_inventory_identity,
        "launch_generation": host.runtime_state.get("launch_generation"),
        "worker_context": dict(host._child_import_host_context),
    }


def _environment(host: Any, record: Any, attempt: Path, admission: Mapping[str, Any]):
    from . import generic_host as core

    env, secrets = host._child_environment(record, attempt, admission=admission,
                                            allow_runtime_connection=False)
    secrets.clear()
    # Startup imports are supplied by the existing isolated worker bootstrap.
    # Per-job network evidence/nonce never becomes process-lifetime state.
    for name in tuple(env):
        if name.startswith("ASTRID_NETWORK_") or name in {
            "PYTHONPATH", "ASTRID_PACKS_PATH", "ASTRID_SELECTED_PACK_MAP",
            "ASTRID_VERIFIED_PACK_RELOCATIONS", "ASTRID_BROKER_PROXY",
            "ASTRID_RUNTIME_ENDPOINT", "ASTRID_RUNTIME_CREDENTIAL",
        }:
            env.pop(name)
    env["ASTRID_INTERNAL_INVOCATION"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    # Preserve the exact source-capture area for child-side custody validation.
    from astrid.sdk.storage_root import resolve_runtime_data_root
    env["ASTRID_RUNTIME_DATA_ROOT"] = str(resolve_runtime_data_root())
    policy = core._network_policy(record)
    return env, dict(policy) if policy is not None else None


def open_resident_task(host: Any, *, record: Any, task: Mapping[str, Any],
                       admission: Mapping[str, Any], terminal: dict[str, Any],
                       binding: SessionBinding, private_runtime_context: bool,
                       machine_policy: Any) -> tuple[CapabilityDescriptor, SessionBinding, Any] | None:
    """Select one adapter in the existing slot, under the host task lock.

    Delegating, brokered/native, and remote work keeps its disposable route.
    Merely declaring residency does not extend any of those authorities.
    """
    from . import generic_host as core

    if not declared_resident(record) or private_runtime_context or machine_policy is not None:
        return None
    policy = core._network_policy(record)
    if (policy is not None and (policy.get("broker") or policy.get("proxy"))
            or record.adapter.family in {"provider", "render"}):
        return None
    relocations = admission.get("verified_pack_relocations")
    selected = admission.get("selected_pack_map")
    if not isinstance(relocations, Mapping) or not relocations or not isinstance(selected, Mapping):
        # An arbitrary mutable import tree is never a retained interpreter.
        return None
    originals = {name: {"root": relocations[name]["original_root"], "source_digest": entry["source_digest"]}
                 for name, entry in selected.items()}
    original_record = getattr(record, "captured_from", None) or record
    environment, policy = _environment(host, record, terminal["root"], admission)
    installation = installation_identity(host)
    scope = {"project_id": task.get("project_id"), "realm_id": task.get("realm_id"),
             "runtime_instance_id": binding.runtime_instance_id, "endpoint": binding.endpoint,
             "runtime_epoch": binding.runtime_epoch,
             "authority_context": core._admitted_task_spec(task).get(
                 "authority_context", task["spec"].get("authority_context")),
             "data_root": environment.get("ASTRID_RUNTIME_DATA_ROOT")}
    key = _digest({"definition": original_record.definition.to_dict(), "source": originals,
                   "dependency": record.dependency_digest, "installation": installation,
                   "scope": scope, "environment": environment, "network_policy": policy})
    manager = host.managed_tool_session
    adapter = manager.current_adapter
    if isinstance(adapter, ResidentPackAdapter) and adapter.key == key and manager.active:
        try:
            if adapter.observe(binding=adapter.binding).get("ok") is not True:
                raise ResidentProtocolError("resident worker is no longer live")
        except Exception:  # noqa: BLE001 - any unverified incarnation must be released, never reused
            manager.release(reason="resident_identity_changed")
        else:
            environment.clear()
            return manager.current_capability, adapter.binding, adapter
    capability = CapabilityDescriptor(record.id, admission_capacity="serial",
                                      resources_claimed=tuple(record.resource_keys), warm_reuse_expected=True)
    session_binding = replace(binding, session_id="resident:" + uuid.uuid4().hex,
                              config_digest=key, source_digest=admission["source_digest"],
                              execution_identity=key, engine_birth_id=uuid.uuid4().hex)
    adapter = ResidentPackAdapter(host=host, key=key, binding=session_binding,
                                  record=record, admission=admission, environment=environment,
                                  policy=policy, originals=originals, installation=installation,
                                  captures=terminal["pack_captures"])
    # open establishes custody *before* start allocates the process or takes
    # the capture stack. A refused replacement leaves captures task-owned.
    manager.open(capability=capability, binding=session_binding, adapter=adapter, start=adapter.start)
    return capability, session_binding, adapter


def retire_changed_session(host: Any) -> None:
    """Revoke stale idle custody even if the next task fails earlier admission.

    The host task lock excludes another dispatch; the existing manager lock
    serializes this observation/release with WarmHolds and explicit shutdown.
    """
    manager = host.managed_tool_session
    with manager._lock:
        adapter = manager.current_adapter
        if isinstance(adapter, ResidentPackAdapter) and manager.active:
            try:
                if adapter.observe(binding=adapter.binding).get("ok") is True:
                    return
            except Exception:  # noqa: BLE001 - failed observation cannot retain an active incarnation
                pass
            manager.release(reason="resident_observation_changed")


class ResidentPackAdapter:
    """A serial, retained child; all admission authority stays in MTS."""

    def __init__(self, *, host, key, binding, record, admission, environment, policy,
                 originals, installation, captures) -> None:
        self.host, self.key, self.binding, self.record = host, key, binding, record
        self.admission = json.loads(json.dumps({key: value for key, value in admission.items()
            if key not in {"task_id", "attempt_id", "fence", "runtime_epoch", "network_nonce", "allowed_routes"}}))
        self.environment, self.policy = environment, policy
        self.originals, self.installation = originals, installation
        self._pending_captures, self._captures = captures, None
        self.process: subprocess.Popen | None = None
        self._birth = ""
        self._reader: PipeReader | None = None
        self._writer: PipeWriter | None = None
        self._stderr_reader: PipeReader | None = None
        self._buffer = bytearray()
        self._diagnostic = bytearray()
        self._fenced = threading.Event()
        self._command_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._active_identity: dict[str, Any] | None = None
        self._completed_identity: dict[str, Any] | None = None
        self._cancelled_identity: dict[str, Any] | None = None
        self._release_error: BaseException | None = None
        self._released = False
        self._root: Path | None = None
        self.ready = False

    def assert_current(self) -> None:
        from astrid.core.pack.imports import validate_selected_pack_map

        from . import generic_host as core

        if self._fenced.is_set():
            raise StaleAdmissionError("resident session is fenced")
        if installation_identity(self.host) != self.installation:
            raise StaleAdmissionError("resident installation changed")
        # This deliberately remains a content check, including on development
        # trees. It is never an mtime-only claim of source immutability.
        try:
            validate_selected_pack_map(self.originals)
            core._verify_admitted_source(self.admission)
            if core._runtime_external_sources is not None:
                core._runtime_external_sources.verify()
        except (OSError, ValueError, core.HostError) as exc:
            raise StaleAdmissionError("resident source identity changed or could not be verified") from exc

    def start(self) -> None:
        from runtime_protocol import platform

        from astrid.sdk.storage_root import resolve_runtime_data_root

        from . import generic_host as core
        from . import task_scope
        from .process_group import process_birth_identity

        self._captures = self._pending_captures.pop_all()
        self._pending_captures = None
        self.assert_current()
        area = resolve_runtime_data_root() / "runtime" / "pack-sessions"
        if area.is_symlink():
            raise ResidentProtocolError("resident session area must not be a symlink")
        platform.private_directory(area)
        self._root = area / self.binding.engine_birth_id
        platform.private_directory(self._root)
        if not platform.is_owner_only(area) or not platform.is_owner_only(self._root):
            raise ResidentProtocolError("resident session area is not private")
        argv = [sys.executable, "-X", "utf8", "-m", "astrid.core.execution.generic_host_worker", "--resident-session"]
        channel = (core._runtime_external_sources.descriptor(self.record)
                   if core._runtime_external_sources is not None else None)
        try:
            if channel is not None:
                argv[5:5] = ["--external-source-fd", str(channel.fileno())]
            argv = core._captured_worker_argv(argv)
            self.process = task_scope.spawn(argv, cwd=str(self._root), env=self.environment,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            pass_fds=() if channel is None else (channel.fileno(),))
            self.host._track_host_lifetime_process(self.process)
        except BaseException as exc:
            retained = getattr(exc, "process", None)
            if retained is not None:
                self.process = retained
                self.host._track_host_lifetime_process(retained)
            raise
        finally:
            if channel is not None:
                channel.close()
        self._birth = process_birth_identity(self.process.pid)
        if not self._birth:
            raise ResidentProtocolError("resident worker birth could not be observed")
        self._reader, self._writer = PipeReader(self.process.stdout), PipeWriter(self.process.stdin)
        def diagnostic(chunk):
            self._diagnostic.extend(chunk)
            del self._diagnostic[:-65536]
        self._stderr_reader = PipeReader(self.process.stderr, sink=diagnostic)
        deadline = time.monotonic() + 30.0
        self._write({"protocol": PROTOCOL, "kind": "init", "binding": list(self.binding.identity_key),
                     "generation": self.host.managed_tool_session.generation,
                     "definition": self.record.definition.to_dict(), "admission": self.admission,
                     "policy": self.policy}, deadline)
        frame = self._read(deadline, lambda: None)
        if (set(frame) != {"protocol", "kind", "binding", "pid", "birth"}
                or frame["kind"] != "ready" or frame["binding"] != list(self.binding.identity_key)
                or frame["pid"] != self.process.pid or frame["birth"] != self._birth):
            raise ResidentProtocolError("resident readiness identity differs")
        self.ready = True

    def _write(self, frame, deadline):
        data = encode_frame(frame)
        if self._writer is None:
            raise ResidentProtocolError("resident worker has no command pipe")
        with self._write_lock:
            self._writer.write(data, deadline=deadline, guard=self._wire_guard)

    def _wire_guard(self):
        if self._fenced.is_set():
            raise StaleAdmissionError("resident session is fenced")
        if self.process is None or self.process.poll() is not None:
            raise ResidentProtocolError("resident worker exited; admitted job is not replayed")

    def _read(self, deadline, guard):
        while b"\n" not in self._buffer:
            self._wire_guard()
            guard()
            data = self._reader.read(deadline=deadline, guard=guard)
            if not data:
                raise ResidentProtocolError("resident worker closed without terminal evidence")
            self._buffer.extend(data)
            if len(self._buffer) > MAX_FRAME_BYTES:
                raise ResidentProtocolError("resident frame exceeds its bound")
        raw, _, rest = self._buffer.partition(b"\n")
        self._buffer = bytearray(rest)
        return decode_frame(bytes(raw) + b"\n")

    def observe(self, *, binding):
        from .process_group import process_birth_identity
        self.assert_current()
        return {"ok": bool(self.ready and binding.identity_key == self.binding.identity_key
                            and self.process is not None and self.process.poll() is None
                            and process_birth_identity(self.process.pid) == self._birth)}

    def invoke(self, token: AdmissionToken, *, request, admission, attempt, cancelled, storage_guard):
        from . import generic_host as core

        if self.host.managed_tool_session.adapter_for(token) is not self:
            raise StaleAdmissionError("resident adapter is not the admission owner")
        identity = token_identity(token)
        job = {key: admission.get(key) for key in ("task_id", "attempt_id", "fence", "runtime_epoch")}
        if (not job["task_id"] or not job["attempt_id"] or type(job["fence"]) is not int
                or job["runtime_epoch"] != self.binding.runtime_epoch
                or token.invocation_id != f"{job['task_id']}:{job['attempt_id']}:{job['fence']}"):
            raise StaleAdmissionError("resident request differs from the admitted attempt")
        frame = {"protocol": PROTOCOL, "kind": "job", "identity": identity,
                 "job": job, "request": dict(request), "attempt": str(attempt)}
        encode_frame(frame)  # Refuse oversize before any dispatch.
        try:
            if not self._command_lock.acquire(blocking=False):
                raise ResidentProtocolError("resident serial adapter already has an invocation")
            try:
                self.assert_current()
                self._active_identity = identity
                self._completed_identity = None
                checked_at = 0.0
                def guard():
                    nonlocal checked_at
                    self._wire_guard()
                    if cancelled is not None and cancelled():
                        self._request_cancel(identity)
                        raise core.HostCancelled("resident action cancelled")
                    if time.monotonic() >= checked_at:
                        checked_at = time.monotonic() + 0.5
                        self.assert_current()
                        storage_guard()
                guard()
                # Runtime's existing lifetime/lease observer supplies the real
                # deadline. This transport ceiling also bounds a broken caller.
                deadline = time.monotonic() + 24 * 60 * 60
                self._write(frame, deadline)
                result = self._read(deadline, guard)
                guard()
                if (set(result) != {"protocol", "kind", "identity", "job", "result"}
                        or result["kind"] != "result" or result["identity"] != identity
                        or result["job"] != job or not isinstance(result["result"], dict)):
                    raise ResidentProtocolError("resident result belongs to another admission")
                payload = result["result"]
                if payload.get("ok") is not True:
                    raise core.HostError(str(payload.get("error") or "resident action failed"))
                self._completed_identity = identity
            finally:
                self._active_identity = None
                self._command_lock.release()
        except core.HostCancelled:
            # The parent must observe native stop before it can report the
            # cancellation. Keep the exact token for MTS.cancel in its normal
            # task-finalization path; release is verified and idempotent.
            self._cancelled_identity = identity
            self.release(reason="resident_job_cancelled")
            raise
        except BaseException as execution_error:
            # MTS remains the single generation owner. Never retry a dispatched
            # job; its existing Runtime attempt takes the ordinary failure path.
            try:
                self.host.managed_tool_session.fence_admission(token, reason="resident_dispatch_failed")
            except StaleAdmissionError:
                pass  # The original job is already fenced; never fence its replacement.
            try:
                self.release(reason="resident_dispatch_failed")
            except Exception as cleanup_error:
                raise core.CommandCleanupUnresolved(
                    "resident worker release remains unproved", execution_error=execution_error,
                ) from cleanup_error
            raise
        self.host.managed_tool_session.adapter_for(token)
        assert self.process is not None  # The wire guard proved native liveness for this result.
        child_payload = dict(payload.get("payload") or {})
        child_payload.update(process_id=self.process.pid, returncode=payload.get("returncode", 0),
                             resident_session={"protocol": PROTOCOL, "session_id": self.binding.session_id})
        return SimpleNamespace(ok=True, returncode=payload.get("returncode", 0),
                               outputs=payload.get("outputs") or [], payload=child_payload,
                               process_id=self.process.pid, stdout=payload.get("stdout", ""),
                               stderr=payload.get("stderr", ""),
                               resident_evidence=MappingProxyType(dict(result["identity"])))

    def finish_admission(self, token):
        if self._completed_identity != token_identity(token):
            raise StaleAdmissionError("resident completion identity differs")
        self._completed_identity = None

    def _request_cancel(self, identity):
        if not self._released and not self._fenced.is_set():
            self._write({"protocol": PROTOCOL, "kind": "cancel", "identity": identity},
                        time.monotonic() + 0.5)

    def cancel_admission(self, *, token, reason):
        identity = token_identity(token)
        if identity not in (self._active_identity, self._completed_identity, self._cancelled_identity):
            return {"ok": False, "reason": "resident cancellation identity is stale"}
        # There is only one job in this version. Retirement is confined to its
        # owned helper scope and never signals an external engine/server.
        self._request_cancel(identity)
        self.fence(reason=reason)
        return self.release(reason=reason)

    def fence(self, *, reason):
        self._fenced.set()
        return {"ok": True, "reason": reason}

    def release(self, *, reason):
        from . import generic_host as core

        self._fenced.set()
        if self._release_error is not None:
            raise ResidentProtocolError("resident release remains unproved") from self._release_error
        if self._released:
            return {"ok": True, "released": True, "reason": reason}
        try:
            if self.process is not None:
                core._terminate_process_group(self.process, grace_seconds=1.0)
                if not self._command_lock.acquire(timeout=3.0):
                    raise ResidentProtocolError("resident invocation still owns its pipes")
                try:
                    close_pipe_workers(self._reader, self._writer, self._stderr_reader)
                    for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                        if stream is not None:
                            stream.close()
                finally:
                    self._command_lock.release()
            if self._captures is not None:
                self._captures.close()
            if self._root is not None:
                self._root.rmdir()
            if self.process is not None:
                self.host._untrack_host_lifetime_process(self.process)
            self.environment.clear()
            self.ready = False
            self._released = True
            return {"ok": True, "released": True, "reason": reason}
        except BaseException as exc:
            self._release_error = exc
            self.host._latch_cleanup_uncertainty(f"resident session release unproved: {type(exc).__name__}")
            raise
