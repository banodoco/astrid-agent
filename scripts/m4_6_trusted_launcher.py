"""Provisional trusted-launcher boundary for the M4-6 hosted collector.

Producer-local collection still refuses public hosted admission until its OS
process-identity backend is implemented.  The distinct running-checker and
later-admission roles are bounded one-shot handoffs minted only from registered
receipts of the fixed GitHub attestation verifier. A private channel controls
communication after producer launch, but is not itself proof of workflow,
code, image, or process integrity.

The future B observer remains responsible for lifecycle observations and loss
detection. This module makes no gap-free observation claim.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import struct
import threading
import time
import weakref
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Callable, Final, Protocol

_SHA1_RE: Final = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_NUMERIC_ID_RE: Final = re.compile(r"^[1-9][0-9]*$")
_MAX_MESSAGE_BYTES: Final = 64 * 1024
_FRAME_MAGIC: Final = b"M4C1"
_FRAME_HEADER_BYTES: Final = 4 + 1 + 8 + 32 + 4
_FRAME_MAC_BYTES: Final = hashlib.sha256().digest_size
_MAX_FRAME_BYTES: Final = _FRAME_HEADER_BYTES + _MAX_MESSAGE_BYTES + _FRAME_MAC_BYTES
_MAX_LIFETIME_SECONDS: Final = 15 * 60
_CHILD_FINISH_TIMEOUT_SECONDS: Final = 5.0
_CHANNEL_EOF_TIMEOUT_SECONDS: Final = 0.25
_CHANNEL_RECEIVE_POLL_SECONDS: Final = 0.05
_IDLE_TIMEOUT_SECONDS: Final = 60.0
_FIXTURE_KEY: Final = object()
_SUPPORTED_SLOTS: Final = frozenset(
    {
        "direct-macos",
        "direct-linux",
        "direct-windows",
        "pinokio-backend-macos",
        "pinokio-backend-linux",
        "pinokio-backend-windows",
        "pinokio-fully-local-macos",
        "pinokio-fully-local-linux",
        "pinokio-fully-local-windows",
    }
)

_CONTEXT_REGISTRY_LOCK = threading.Lock()
_CONTEXT_REGISTRY: weakref.WeakSet[object] = weakref.WeakSet()
_ROLE_CONTEXT_RECORDS: dict[int, tuple[weakref.ReferenceType[object], object]] = {}
_ROLE_CONTEXT_MAX_LIFETIME_SECONDS: Final = 5 * 60


class HostedAdmissionNeeds(RuntimeError):
    """Hosted admission cannot proceed because a required trust source is absent."""

    def __init__(self, *needs: str) -> None:
        if not needs or any(not value.startswith("NEEDS:") for value in needs):
            raise ValueError("each missing prerequisite must be an explicit NEEDS")
        self.needs = tuple(needs)
        super().__init__("; ".join(self.needs))


class ContextRejected(RuntimeError):
    """A context or message is stale, replayed, mismatched, or already closed."""


class _State(Enum):
    ACTIVE = "active"
    ABORTING = "aborting"
    FINALIZING = "finalizing"
    ABORTED = "aborted"
    FINALIZED = "finalized"


class _FrameKind(Enum):
    DATA = 1
    TERMINAL = 2


class _Timer(Protocol):
    daemon: bool

    def start(self) -> None: ...

    def cancel(self) -> None: ...


_TimerFactory = Callable[[float, Callable[[], None]], _Timer]


@dataclass(frozen=True, slots=True)
class CandidateBinding:
    """Exact admitted product tuple; hashes are lower-case immutable digests."""

    app: str
    agent: str
    runtime: str
    pinokio: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        for field_name in ("app", "agent", "runtime", "pinokio"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not _SHA1_RE.fullmatch(value):
                raise ValueError(f"{field_name} must be a full lowercase Git SHA")
        if not isinstance(self.manifest_sha256, str) or not _SHA256_RE.fullmatch(
            self.manifest_sha256
        ):
            raise ValueError("manifest_sha256 must be a full lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class TrustedBinding:
    """Requested origin tuple; claims are constraints, never authority.

    `check_run_id` is a workflow OIDC claim. `job_id` is the distinct numeric
    job identifier corroborated through the authenticated Actions REST API.
    `workflow_id` is also REST metadata; `workflow_ref` and `workflow_sha` are
    workflow identity claims.
    """

    repository_owner: str
    repository_owner_id: str
    repository: str
    repository_id: str
    workflow_ref: str
    workflow_id: str
    workflow_sha: str
    run_id: str
    run_attempt: int
    check_run_id: str
    job_id: str
    slot_id: str
    candidate: CandidateBinding

    def __post_init__(self) -> None:
        for name in ("repository_owner", "repository", "workflow_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
        for name in (
            "repository_owner_id",
            "repository_id",
            "workflow_id",
            "run_id",
            "check_run_id",
            "job_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not _NUMERIC_ID_RE.fullmatch(value):
                raise ValueError(f"{name} must be a positive immutable numeric ID")
        if self.repository.count("/") != 1:
            raise ValueError("repository must be the exact owner/name pair")
        if self.repository != f"{self.repository_owner}/{self.repository.rsplit('/', 1)[-1]}":
            raise ValueError("repository owner differs from repository claim")
        workflow_prefix = f"{self.repository}/.github/workflows/"
        workflow_path, separator, workflow_ref = self.workflow_ref.partition("@")
        if (
            not separator
            or not workflow_ref
            or not workflow_path.startswith(workflow_prefix)
            or not workflow_path[len(workflow_prefix) :]
        ):
            raise ValueError("workflow_ref must identify a workflow file in this repository")
        if not isinstance(self.workflow_sha, str) or not _SHA1_RE.fullmatch(self.workflow_sha):
            raise ValueError("workflow_sha must be a full lowercase Git SHA")
        if type(self.run_attempt) is not int or self.run_attempt < 1:
            raise ValueError("run_attempt must be a positive integer")
        if not isinstance(self.candidate, CandidateBinding):
            raise ValueError("candidate must be a frozen CandidateBinding")
        if self.slot_id not in _SUPPORTED_SLOTS:
            raise ValueError("slot_id is not in the fixed M4-6 slot registry")


def _binding_digest(binding: TrustedBinding) -> bytes:
    encoded = json.dumps(asdict(binding), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).digest()


@dataclass(frozen=True, slots=True)
class _ChildIdentity:
    """OS-observed child identity supplied only by the fixed launcher backend."""

    pid: int
    birth_id: str
    executable: str
    executable_sha256: str
    parent_pid: int
    parent_birth_id: str

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid < 1:
            raise ValueError("child PID must be positive")
        if type(self.parent_pid) is not int or self.parent_pid < 1:
            raise ValueError("parent PID must be positive")
        if not self.birth_id or not self.parent_birth_id:
            raise ValueError("child and parent birth identities are required")
        if not os.path.isabs(self.executable):
            raise ValueError("child executable path must be absolute")
        if not _SHA256_RE.fullmatch(self.executable_sha256):
            raise ValueError("child executable digest must be SHA-256")


class _ChildControl:
    """Exact-incarnation wait/cleanup implemented by the fixed OS backend."""

    def is_same_live_child(self, identity: _ChildIdentity) -> bool:
        raise NotImplementedError

    def finish_and_reap_exact_child(
        self, identity: _ChildIdentity, *, timeout_seconds: float
    ) -> int | None:
        """Return known exit status after exact reap, or None if status is unknown."""
        raise NotImplementedError

    def terminate_and_reap_exact_child(
        self, identity: _ChildIdentity, *, timeout_seconds: float
    ) -> bool:
        """Boundedly terminate/reap this exact incarnation, never a reused PID."""
        raise NotImplementedError


class _Channel:
    """Private socketpair receiver with an exact framed, authenticated protocol."""

    __slots__ = ("_socket", "_key", "_owner_pid", "_next_sequence", "_closed")

    def __init__(self, endpoint: socket.socket, key: bytes, owner_pid: int) -> None:
        self._socket = endpoint
        self._key = key
        self._owner_pid = owner_pid
        self._next_sequence = 1
        self._closed = False

    def receive_message(
        self,
        *,
        deadline: float,
        now: object,
        expected_binding_digest: bytes,
    ) -> tuple[_FrameKind, bytes]:
        """Read one frame from the owned socket; caller bytes are never authority."""
        if self._closed or os.getpid() != self._owner_pid:
            raise ContextRejected("channel is closed or belongs to another process")
        if len(expected_binding_digest) != 32:
            raise ContextRejected("expected binding digest is malformed")
        (frame_length,) = struct.unpack(">I", self._recv_exact(4, deadline=deadline, now=now))
        if frame_length < _FRAME_HEADER_BYTES + _FRAME_MAC_BYTES:
            raise ContextRejected("channel frame is truncated or malformed")
        if frame_length > _MAX_FRAME_BYTES:
            raise ContextRejected("channel frame exceeds the bounded size")
        frame = self._recv_exact(frame_length, deadline=deadline, now=now)
        magic = frame[:4]
        try:
            kind = _FrameKind(frame[4])
        except ValueError as exc:
            raise ContextRejected("channel frame kind is unknown") from exc
        sequence = int.from_bytes(frame[5:13], "big")
        binding_digest = frame[13:45]
        message_length = int.from_bytes(frame[45:49], "big")
        message_end = _FRAME_HEADER_BYTES + message_length
        if (
            magic != _FRAME_MAGIC
            or message_length > _MAX_MESSAGE_BYTES
            or len(frame) != message_end + _FRAME_MAC_BYTES
        ):
            raise ContextRejected("channel frame envelope is not exact")
        message = frame[_FRAME_HEADER_BYTES:message_end]
        mac = frame[message_end:]
        expected_mac = hmac.new(self._key, frame[:message_end], hashlib.sha256).digest()
        if not hmac.compare_digest(binding_digest, expected_binding_digest):
            raise ContextRejected("channel frame has a different origin or candidate binding")
        if sequence != self._next_sequence:
            raise ContextRejected("channel sequence is stale, skipped, or replayed")
        if not hmac.compare_digest(mac, expected_mac):
            raise ContextRejected("channel message authentication failed")
        if kind is _FrameKind.TERMINAL and message:
            raise ContextRejected("terminal frame must have an empty payload")
        self._next_sequence += 1
        return kind, message

    def _recv_exact(self, size: int, *, deadline: float, now: object) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            remaining = deadline - now()  # type: ignore[operator]
            if remaining <= 0:
                raise ContextRejected("channel receive deadline expired")
            self._socket.settimeout(min(remaining, _CHANNEL_RECEIVE_POLL_SECONDS))
            try:
                chunk = self._socket.recv(size - len(chunks))
            except TimeoutError as exc:
                if self._closed:
                    raise ContextRejected("private channel was aborted during receive") from exc
                if now() >= deadline:  # type: ignore[operator]
                    raise ContextRejected("channel receive deadline expired") from exc
                continue
            except OSError as exc:
                raise ContextRejected("private channel closed while reading") from exc
            if not chunk:
                raise ContextRejected("channel closed before a complete frame arrived")
            chunks.extend(chunk)
        return bytes(chunks)

    def require_clean_eof(self, *, timeout_seconds: float) -> None:
        """Require peer close after terminal/reap; reject bytes or missing EOF."""
        if self._closed or os.getpid() != self._owner_pid:
            raise ContextRejected("channel is closed or belongs to another process")
        if not 0 < timeout_seconds <= _CHANNEL_EOF_TIMEOUT_SECONDS:
            raise ContextRejected("channel EOF deadline is invalid")
        self._socket.settimeout(timeout_seconds)
        try:
            trailing = self._socket.recv(1)
        except TimeoutError as exc:
            raise ContextRejected("channel EOF was not observed before its deadline") from exc
        except OSError as exc:
            raise ContextRejected("channel failed while awaiting clean EOF") from exc
        if trailing:
            raise ContextRejected("channel contains trailing bytes after terminal frame")

    def _close(self) -> None:
        if not self._closed:
            self._closed = True
            self._socket.close()
            self._key = b""


def _thread_timer(delay: float, callback: Callable[[], None]) -> _Timer:
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    return timer


class TrustedCollectorContext:
    """Process-local collector session, not independent proof of origin.

    Only the missing fixed platform backend could create a production session.
    The fixture seam creates a test session that `assert_binding` always aborts
    and rejects. Python object structure is not a security boundary against code
    already executing in this interpreter.
    """

    __slots__ = (
        "__weakref__",
        "_binding",
        "_child_identity",
        "_child_control",
        "_channel",
        "_session_id",
        "_hard_deadline",
        "_idle_deadline",
        "_idle_timeout",
        "_clock",
        "_timer_factory",
        "_timer",
        "_timer_generation",
        "_lock",
        "_state",
        "_owner_pid",
        "_binding_digest",
        "_terminal_received",
        "_receive_in_flight",
        "_child_reaped",
        "_failure",
        "_cleanup_failure",
    )

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("TrustedCollectorContext can only be minted by the fixed launcher")

    @classmethod
    def _mint_fixture(
        cls,
        key: object,
        *,
        binding: TrustedBinding,
        child_identity: _ChildIdentity,
        child_control: _ChildControl,
        channel: _Channel,
        lifetime_seconds: float,
        idle_timeout_seconds: float,
        clock: Callable[[], float],
        timer_factory: _TimerFactory,
    ) -> TrustedCollectorContext:
        if key is not _FIXTURE_KEY:
            raise TypeError("no authenticated production minting backend is installed")
        if not 0 < lifetime_seconds <= _MAX_LIFETIME_SECONDS:
            raise ValueError("context lifetime must be at most fifteen minutes")
        if not 0 < idle_timeout_seconds <= lifetime_seconds:
            raise ValueError("idle timeout must be positive and no longer than lifetime")
        context = object.__new__(cls)
        now = clock()
        context._binding = binding
        context._child_identity = child_identity
        context._child_control = child_control
        context._channel = channel
        context._session_id = secrets.token_bytes(32)
        context._hard_deadline = now + lifetime_seconds
        context._idle_deadline = now + idle_timeout_seconds
        context._idle_timeout = idle_timeout_seconds
        context._clock = clock
        context._timer_factory = timer_factory
        context._timer = None
        context._timer_generation = 0
        context._lock = threading.RLock()
        context._state = _State.ACTIVE
        context._owner_pid = os.getpid()
        context._binding_digest = _binding_digest(binding)
        context._terminal_received = False
        context._receive_in_flight = False
        context._child_reaped = False
        context._failure = None
        context._cleanup_failure = None
        with context._lock:
            context._arm_idle_timer_locked()
        with _CONTEXT_REGISTRY_LOCK:
            _CONTEXT_REGISTRY.add(context)
        return context

    @property
    def binding(self) -> TrustedBinding:
        self._check_process_before_lock()
        with self._lock:
            self._assert_live_locked()
            return self._binding

    @property
    def cleanup_failure(self) -> str | None:
        self._check_process_before_lock()
        with self._lock:
            self._assert_owner_locked()
            return self._cleanup_failure

    @property
    def failure(self) -> str | None:
        self._check_process_before_lock()
        with self._lock:
            self._assert_owner_locked()
            return self._failure

    def assert_binding(self, expected: TrustedBinding) -> None:
        self._check_process_before_lock()
        with self._lock:
            self._assert_live_locked()
            if expected != self._binding:
                self._abort_locked()
                raise ContextRejected("context binding differs from the exact tuple")
            cleanup_failure = self._abort_locked()
            needs = ["NEEDS: no authenticated production context minting backend is installed"]
            if cleanup_failure:
                needs.append(cleanup_failure)
            raise HostedAdmissionNeeds(*needs)

    def receive_message(self) -> bytes | None:
        """Receive data, or `None` for the authenticated terminal frame."""
        self._check_process_before_lock()
        with self._lock:
            self._assert_live_locked()
            if self._receive_in_flight:
                self._abort_locked()
                raise ContextRejected("a channel receive is already in flight")
            if self._terminal_received:
                self._abort_locked()
                raise ContextRejected("frame received after terminal frame")
            self._receive_in_flight = True
            deadline = min(self._hard_deadline, self._idle_deadline)

        try:
            kind, message = self._channel.receive_message(
                deadline=deadline,
                now=self._clock,
                expected_binding_digest=self._binding_digest,
            )
            with self._lock:
                self._assert_live_locked()
                if kind is _FrameKind.TERMINAL:
                    self._terminal_received = True
                    return None
                if not self._child_control.is_same_live_child(self._child_identity):
                    self._abort_locked()
                    raise ContextRejected("collector child identity changed during data frame")
                self._idle_deadline = min(self._hard_deadline, self._clock() + self._idle_timeout)
                self._arm_idle_timer_locked()
                return message
        except ContextRejected:
            self.abort()
            raise
        finally:
            with self._lock:
                self._receive_in_flight = False

    def finalize(self) -> None:
        self._check_process_before_lock()
        with self._lock:
            self._assert_live_locked()
            if not self._terminal_received:
                self._abort_locked()
                raise ContextRejected("authenticated terminal frame is required before finalize")
            self._state = _State.FINALIZING
            self._cancel_timer_locked()
            timeout_seconds = min(
                _CHILD_FINISH_TIMEOUT_SECONDS,
                max(0.0, self._hard_deadline - self._clock()),
            )
            try:
                exit_status = None
                if timeout_seconds > 0:
                    exit_status = self._child_control.finish_and_reap_exact_child(
                        self._child_identity,
                        timeout_seconds=timeout_seconds,
                    )
                if type(exit_status) is not int:
                    raise HostedAdmissionNeeds(
                        "NEEDS: OS backend could not prove bounded reap and exact child exit status"
                    )
                self._child_reaped = True
                if exit_status != 0:
                    raise ContextRejected(
                        f"collector child exited with nonzero status {exit_status}"
                    )
                eof_timeout = min(
                    _CHANNEL_EOF_TIMEOUT_SECONDS,
                    max(0.0, self._hard_deadline - self._clock()),
                )
                if eof_timeout <= 0:
                    raise ContextRejected("channel EOF deadline expired after terminal frame")
                self._channel.require_clean_eof(timeout_seconds=eof_timeout)
            except BaseException as exc:
                if isinstance(exc, HostedAdmissionNeeds):
                    self._failure = str(exc)
                elif isinstance(exc, ContextRejected):
                    self._failure = f"NEEDS: {exc}"
                else:
                    self._failure = (
                        f"NEEDS: exact collector child finalization failed ({type(exc).__name__})"
                    )
                cleanup_failure = self._abort_locked()
                if cleanup_failure:
                    self._cleanup_failure = cleanup_failure
                raise
            self._state = _State.FINALIZED
            self._cancel_timer_locked()
            self._channel._close()
            self._session_id = b""
            self._unregister_locked()

    def abort(self) -> None:
        self._check_process_before_lock()
        with self._lock:
            self._assert_owner_locked()
            if self._state in (_State.ABORTED, _State.FINALIZED):
                if self._cleanup_failure:
                    raise HostedAdmissionNeeds(self._cleanup_failure)
                return
            cleanup_failure = self._abort_locked()
            if cleanup_failure:
                raise HostedAdmissionNeeds(cleanup_failure)

    def close(self) -> None:
        """Explicitly abort and reap the child; no destructor performs cleanup."""
        self.abort()

    def __enter__(self) -> TrustedCollectorContext:
        self._check_process_before_lock()
        with self._lock:
            self._assert_live_locked()
            return self

    def __exit__(self, exc_type: object, _exc: object, _traceback: object) -> bool:
        if exc_type is None:
            self.finalize()
        else:
            self.abort()
        return False

    def _arm_idle_timer_locked(self) -> None:
        self._cancel_timer_locked()
        self._timer_generation += 1
        generation = self._timer_generation
        next_deadline = min(self._hard_deadline, self._idle_deadline)
        delay = max(0.0, next_deadline - self._clock())
        timer = self._timer_factory(delay, lambda: self._supervise_expiry(generation))
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _cancel_timer_locked(self) -> None:
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.cancel()

    def _supervise_expiry(self, generation: int) -> None:
        if os.getpid() != self._owner_pid:
            self._invalidate_fork_copy_unlocked()
            return
        with self._lock:
            if generation != self._timer_generation or self._state is not _State.ACTIVE:
                return
            now = self._clock()
            deadline = min(self._hard_deadline, self._idle_deadline)
            if now < deadline:
                self._arm_idle_timer_locked()
                return
            self._failure = "NEEDS: collector idle/lifetime deadline elapsed"
            cleanup_failure = self._abort_locked()
            if cleanup_failure:
                self._cleanup_failure = cleanup_failure

    def _assert_live_locked(self) -> None:
        self._assert_owner_locked()
        if self._state is not _State.ACTIVE:
            if self._cleanup_failure:
                raise HostedAdmissionNeeds(self._cleanup_failure)
            raise ContextRejected(f"context is terminal ({self._state.value})")
        if self._clock() >= min(self._hard_deadline, self._idle_deadline):
            self._failure = "NEEDS: collector idle/lifetime deadline elapsed"
            cleanup_failure = self._abort_locked()
            if cleanup_failure:
                self._cleanup_failure = cleanup_failure
            raise ContextRejected("collector context expired")

    def _assert_owner_locked(self) -> None:
        if os.getpid() != self._owner_pid:
            self._invalidate_fork_copy_unlocked()
            self._raise_parent_fork_needs()

    def _check_process_before_lock(self) -> None:
        if os.getpid() != self._owner_pid:
            self._invalidate_fork_copy_unlocked()
            self._raise_parent_fork_needs()

    def _raise_parent_fork_needs(self) -> None:
        raise HostedAdmissionNeeds(
            "NEEDS: platform supervisor must invalidate the parent attempt after collector fork"
        )

    def _invalidate_fork_copy_unlocked(self) -> None:
        # In a fork child, avoid inherited locks that may have been owned by
        # vanished threads. This only invalidates the local descriptor/key copy.
        self._lock = threading.RLock()
        self._timer = None
        self._timer_generation += 1
        self._channel._close()
        self._session_id = b""
        self._state = _State.ABORTED
        self._cleanup_failure = (
            "NEEDS: platform supervisor must invalidate the parent attempt after collector fork"
        )

    def _abort_locked(self) -> str | None:
        if self._state in (_State.ABORTED, _State.FINALIZED):
            return self._cleanup_failure
        self._state = _State.ABORTING
        self._cancel_timer_locked()
        self._channel._close()
        self._session_id = b""
        try:
            cleanup_ok = self._child_reaped or self._child_control.terminate_and_reap_exact_child(
                self._child_identity,
                timeout_seconds=_CHILD_FINISH_TIMEOUT_SECONDS,
            )
            if not cleanup_ok:
                self._cleanup_failure = (
                    "NEEDS: OS backend could not prove exact collector child termination and reap"
                )
        except Exception as exc:  # noqa: BLE001 - preserve and report cleanup failure
            self._cleanup_failure = (
                f"NEEDS: exact collector child cleanup failed ({type(exc).__name__})"
            )
        finally:
            self._state = _State.ABORTED
            self._unregister_locked()
        return self._cleanup_failure

    def _unregister_locked(self) -> None:
        with _CONTEXT_REGISTRY_LOCK:
            _CONTEXT_REGISTRY.discard(self)

    def __repr__(self) -> str:
        return "TrustedCollectorContext(<opaque>)"

    def __reduce__(self) -> object:
        raise TypeError("TrustedCollectorContext cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("TrustedCollectorContext cannot be serialized")

    def __copy__(self) -> TrustedCollectorContext:
        raise TypeError("TrustedCollectorContext cannot be copied")

    def __deepcopy__(self, _memo: object) -> TrustedCollectorContext:
        raise TypeError("TrustedCollectorContext cannot be copied")


class _OneShotRoleContext:
    """Process-local, expiring, one-shot handoff owned by the fixed verifier.

    The class is only a routing boundary inside the reviewed product TCB.  It is
    not a sandbox against hostile Python code already executing in this process.
    Authority comes from the verifier's registered receipt, not from object
    shape, caller-supplied fields, or the private batch itself.
    """

    __slots__ = (
        "__weakref__",
        "_clock",
        "_consumed",
        "_deadline",
        "_lock",
        "_owner_pid",
    )

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(f"{type(self).__name__} can only be minted by the fixed verifier")

    def _release_once(self) -> object:
        self._check_process_before_lock()
        with self._lock:
            if self._consumed:
                raise ContextRejected(f"{type(self).__name__} is already consumed or closed")
            if self._clock() >= self._deadline:
                self._consume_locked()
                raise ContextRejected(f"{type(self).__name__} expired before release")
            with _CONTEXT_REGISTRY_LOCK:
                registered = _ROLE_CONTEXT_RECORDS.pop(id(self), None)
                _CONTEXT_REGISTRY.discard(self)
            self._consumed = True
            if registered is None or registered[0]() is not self:
                raise ContextRejected(
                    f"{type(self).__name__} was not minted from a registered verifier receipt"
                )
            batch = registered[1]
            return batch

    def close(self) -> None:
        self._check_process_before_lock()
        with self._lock:
            self._consume_locked()

    def _consume_locked(self) -> None:
        self._consumed = True
        with _CONTEXT_REGISTRY_LOCK:
            _ROLE_CONTEXT_RECORDS.pop(id(self), None)
            _CONTEXT_REGISTRY.discard(self)

    def _check_process_before_lock(self) -> None:
        if os.getpid() != self._owner_pid:
            self._invalidate_fork_copy_unlocked()
            raise ContextRejected(f"{type(self).__name__} cannot cross a process boundary")

    def _invalidate_fork_copy_unlocked(self) -> None:
        self._lock = threading.Lock()
        self._consumed = True
        self._deadline = 0.0

    def __enter__(self) -> _OneShotRoleContext:
        self._check_process_before_lock()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> bool:
        self.close()
        return False

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<opaque>)"

    def __reduce__(self) -> object:
        raise TypeError(f"{type(self).__name__} cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError(f"{type(self).__name__} cannot be serialized")

    def __copy__(self) -> _OneShotRoleContext:
        raise TypeError(f"{type(self).__name__} cannot be copied")

    def __deepcopy__(self, _memo: object) -> _OneShotRoleContext:
        raise TypeError(f"{type(self).__name__} cannot be copied")


class ArtifactCheckContext(_OneShotRoleContext):
    """Running-checker authority for producer bytes, never checker success."""

    __slots__ = ()

    def _release_m4_6_artifact_check_batch(self) -> object:
        return self._release_once()


class PostCheckAdmissionContext(_OneShotRoleContext):
    """Later authority for terminal checker success and exact output binding."""

    __slots__ = ()

    def _release_m4_6_post_check_admission_batch(self) -> object:
        return self._release_once()


def _artifact_check_context_from_verified_receipt(
    receipt: object,
) -> ArtifactCheckContext:
    """Atomically consume one verifier receipt into the running-checker role."""

    from scripts import m4_6_artifact_provenance as provenance
    from scripts import m4_6_github_attestation as verifier

    batch = verifier._claim_artifact_check_receipt(receipt)
    if type(batch) is not provenance._ArtifactCheckBatch:
        raise ContextRejected("verifier returned no exact artifact-check batch")
    context = object.__new__(ArtifactCheckContext)
    context._clock = time.monotonic
    context._consumed = False
    context._deadline = time.monotonic() + _ROLE_CONTEXT_MAX_LIFETIME_SECONDS
    context._lock = threading.Lock()
    context._owner_pid = os.getpid()
    with _CONTEXT_REGISTRY_LOCK:
        _CONTEXT_REGISTRY.add(context)
        _ROLE_CONTEXT_RECORDS[id(context)] = (weakref.ref(context), batch)
    return context


def _post_check_admission_context_from_verified_receipt(
    receipt: object,
) -> PostCheckAdmissionContext:
    """Atomically consume one later verifier receipt into admission authority."""

    from scripts import m4_6_artifact_provenance as provenance
    from scripts import m4_6_github_attestation as verifier

    batch = verifier._claim_post_check_receipt(receipt)
    if type(batch) is not provenance._PostCheckAdmissionBatch:
        raise ContextRejected("verifier returned no exact post-check admission batch")
    context = object.__new__(PostCheckAdmissionContext)
    context._clock = time.monotonic
    context._consumed = False
    context._deadline = time.monotonic() + _ROLE_CONTEXT_MAX_LIFETIME_SECONDS
    context._lock = threading.Lock()
    context._owner_pid = os.getpid()
    with _CONTEXT_REGISTRY_LOCK:
        _CONTEXT_REGISTRY.add(context)
        _ROLE_CONTEXT_RECORDS[id(context)] = (weakref.ref(context), batch)
    return context


def _before_fork() -> None:
    _CONTEXT_REGISTRY_LOCK.acquire()


def _after_fork_parent() -> None:
    _CONTEXT_REGISTRY_LOCK.release()


def _after_fork_child() -> None:
    contexts = tuple(_CONTEXT_REGISTRY)
    for context in contexts:
        context._invalidate_fork_copy_unlocked()
    _CONTEXT_REGISTRY.clear()
    _ROLE_CONTEXT_RECORDS.clear()
    _CONTEXT_REGISTRY_LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_parent,
        after_in_child=_after_fork_child,
    )


def launch_trusted_collector(binding: TrustedBinding) -> TrustedCollectorContext:
    """Public fixed hosted entry point; refuses until trust backends exist.

    `binding` is only the caller's requested tuple. It is not evidence. The
    launcher deliberately does not spawn a process when either independently
    authenticated platform claims or OS process/image identity are unavailable.
    """
    if not isinstance(binding, TrustedBinding):
        raise TypeError("binding must be an exact TrustedBinding request")
    raise HostedAdmissionNeeds(
        "NEEDS: fixed B/platform verification of OIDC issuer, audience, expiry, JTI, repository owner/name IDs, workflow_ref/SHA, run_id/run_attempt, check_run_id, and concordant REST workflow_id/job_id",
        "NEEDS: authenticated collector slot and candidate/checkouts/manifest binding plus single-use attempt replay prevention",
        "NEEDS: platform supervisor that invalidates the parent attempt if collector lineage is forked or lost",
        "NEEDS: authenticated immutable runner image identity for the selected slot",
        "NEEDS: fixed OS launcher that creates the child and independently proves executable identity, parent identity, process birth, and exact-child finish/abort/reap status",
    )


def _fixture_context_for_tests(
    binding: TrustedBinding,
    child_identity: _ChildIdentity,
    child_control: _ChildControl,
    *,
    lifetime_seconds: float = 60.0,
    idle_timeout_seconds: float | None = None,
    clock: object = time.monotonic,
    timer_factory: _TimerFactory = _thread_timer,
) -> tuple[TrustedCollectorContext, socket.socket, bytes]:
    """Test-only fixture seam; never authenticates claims or enables hosted use."""
    if type(lifetime_seconds) not in (int, float) or not (
        0 < lifetime_seconds <= _MAX_LIFETIME_SECONDS
    ):
        raise ValueError("context lifetime must be at most fifteen minutes")
    if idle_timeout_seconds is None:
        idle_timeout_seconds = min(_IDLE_TIMEOUT_SECONDS, float(lifetime_seconds))
    if type(idle_timeout_seconds) not in (int, float) or not (
        0 < idle_timeout_seconds <= lifetime_seconds
    ):
        raise ValueError("idle timeout must be positive and no longer than lifetime")
    channel_parent, channel_child = socket.socketpair()
    key = secrets.token_bytes(32)
    channel = _Channel(channel_parent, key, os.getpid())
    try:
        context = TrustedCollectorContext._mint_fixture(
            _FIXTURE_KEY,
            binding=binding,
            child_identity=child_identity,
            child_control=child_control,
            channel=channel,
            lifetime_seconds=float(lifetime_seconds),
            idle_timeout_seconds=float(idle_timeout_seconds),
            clock=clock,  # type: ignore[arg-type]
            timer_factory=timer_factory,
        )
    except BaseException:
        channel_parent.close()
        channel_child.close()
        raise
    return context, channel_child, key


def _fixture_encode_frame_for_tests(
    binding: TrustedBinding,
    key: bytes,
    message: bytes,
    *,
    sequence: int = 1,
    kind: _FrameKind = _FrameKind.DATA,
    bind_to: TrustedBinding | None = None,
    magic: bytes = _FRAME_MAGIC,
    mac: bytes | None = None,
    message_length: int | None = None,
) -> bytes:
    """Test-only child-side frame encoder; never called by production context."""
    binding_digest = _binding_digest(bind_to or binding)
    claimed_length = len(message) if message_length is None else message_length
    authenticated = (
        magic
        + bytes((kind.value,))
        + sequence.to_bytes(8, "big")
        + binding_digest
        + claimed_length.to_bytes(4, "big")
        + message
    )
    signature = hmac.new(key, authenticated, hashlib.sha256).digest() if mac is None else mac
    frame = authenticated + signature
    return struct.pack(">I", len(frame)) + frame
