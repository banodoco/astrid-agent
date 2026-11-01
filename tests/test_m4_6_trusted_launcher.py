"""Focused tests for the provisional M4-6 trusted-launcher boundary."""

from __future__ import annotations

import copy
import hashlib
import os
import pickle
import socket
import struct
import threading
import time

import pytest

from scripts import m4_6_trusted_launcher as launcher
from tests.lanes import unavailable


def _candidate(**overrides: str) -> launcher.CandidateBinding:
    values = {
        "app": "1" * 40,
        "agent": "2" * 40,
        "runtime": "3" * 40,
        "pinokio": "4" * 40,
        "manifest_sha256": "5" * 64,
    }
    values.update(overrides)
    return launcher.CandidateBinding(**values)


def _binding(**overrides: object) -> launcher.TrustedBinding:
    values: dict[str, object] = {
        "repository_owner": "example-owner",
        "repository_owner_id": "1234",
        "repository": "example-owner/astrid",
        "repository_id": "5678",
        "workflow_ref": "example-owner/astrid/.github/workflows/m4-6.yml@refs/heads/main",
        "workflow_id": "9876",
        "workflow_sha": "a" * 40,
        "run_id": "100200",
        "run_attempt": 2,
        "check_run_id": "300300",
        "job_id": "300400",
        "slot_id": "direct-linux",
        "candidate": _candidate(),
    }
    values.update(overrides)
    return launcher.TrustedBinding(**values)  # type: ignore[arg-type]


class _ManualTimer:
    def __init__(self, delay: float, callback: object) -> None:
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.callback()  # type: ignore[operator]


class _Child(launcher._ChildControl):
    def __init__(self) -> None:
        self.live = True
        self.reaped = False
        self.identity_matches = True
        self.exit_status: int | None = 0
        self.finish_succeeds = True
        self.cleanup_succeeds = True
        self.finished: list[launcher._ChildIdentity] = []
        self.terminated: list[launcher._ChildIdentity] = []
        self.finish_entered = threading.Event()
        self.release_finish = threading.Event()
        self.block_finish = False

    def is_same_live_child(self, identity: launcher._ChildIdentity) -> bool:
        return (
            self.identity_matches
            and self.live
            and identity.pid == 41
            and identity.birth_id == "boot:123"
        )

    def finish_and_reap_exact_child(
        self, identity: launcher._ChildIdentity, *, timeout_seconds: float
    ) -> int | None:
        self.finished.append(identity)
        assert 0 < timeout_seconds <= launcher._CHILD_FINISH_TIMEOUT_SECONDS
        self.finish_entered.set()
        if self.block_finish:
            assert self.release_finish.wait(timeout=1.0)
        if not self.identity_matches or not self.finish_succeeds:
            return None
        self.live = False
        self.reaped = True
        return self.exit_status

    def terminate_and_reap_exact_child(
        self, identity: launcher._ChildIdentity, *, timeout_seconds: float
    ) -> bool:
        self.terminated.append(identity)
        assert 0 < timeout_seconds <= launcher._CHILD_FINISH_TIMEOUT_SECONDS
        if not self.identity_matches:
            return False
        if self.cleanup_succeeds:
            self.live = False
            self.reaped = True
        return self.reaped


def _child_identity() -> launcher._ChildIdentity:
    return launcher._ChildIdentity(
        pid=41,
        birth_id="boot:123",
        executable="/usr/bin/python3",
        executable_sha256=hashlib.sha256(b"python").hexdigest(),
        parent_pid=40,
        parent_birth_id="boot:122",
    )


def _fixture(
    *,
    lifetime_seconds: float = 60.0,
    idle_timeout_seconds: float | None = None,
    clock: object | None = None,
    timer_factory: object | None = None,
) -> tuple[launcher.TrustedCollectorContext, socket.socket, bytes, _Child]:
    child = _Child()
    options: dict[str, object] = {}
    if clock is not None:
        options["clock"] = clock
    if idle_timeout_seconds is not None:
        options["idle_timeout_seconds"] = idle_timeout_seconds
    if timer_factory is not None:
        options["timer_factory"] = timer_factory
    context, peer, key = launcher._fixture_context_for_tests(
        _binding(), _child_identity(), child, lifetime_seconds=lifetime_seconds, **options
    )
    return context, peer, key, child


def _send(
    peer: socket.socket,
    key: bytes,
    message: bytes | None = None,
    *,
    binding: launcher.TrustedBinding | None = None,
    sequence: int = 1,
    kind: launcher._FrameKind = launcher._FrameKind.DATA,
    mac: bytes | None = None,
    magic: bytes = launcher._FRAME_MAGIC,
) -> None:
    frame = launcher._fixture_encode_frame_for_tests(
        binding or _binding(),
        key,
        (b"" if kind is launcher._FrameKind.TERMINAL else b"ok") if message is None else message,
        sequence=sequence,
        kind=kind,
        mac=mac,
        magic=magic,
    )
    peer.sendall(frame)


def test_public_launcher_fails_closed_before_any_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_socketpair() -> tuple[socket.socket, socket.socket]:
        raise AssertionError("public hosted refusal must happen before channel or child creation")

    monkeypatch.setattr(launcher.socket, "socketpair", unexpected_socketpair)
    with pytest.raises(launcher.HostedAdmissionNeeds) as exc_info:
        launcher.launch_trusted_collector(_binding())

    assert exc_info.value.needs == (
        "NEEDS: fixed B/platform verification of OIDC issuer, audience, expiry, JTI, repository owner/name IDs, workflow_ref/SHA, run_id/run_attempt, check_run_id, and concordant REST workflow_id/job_id",
        "NEEDS: authenticated collector slot and candidate/checkouts/manifest binding plus single-use attempt replay prevention",
        "NEEDS: platform supervisor that invalidates the parent attempt if collector lineage is forked or lost",
        "NEEDS: authenticated immutable runner image identity for the selected slot",
        "NEEDS: fixed OS launcher that creates the child and independently proves executable identity, parent identity, process birth, and exact-child finish/abort/reap status",
    )


def test_binding_rejects_invalid_candidate_slot_ids_and_claim_consistency() -> None:
    with pytest.raises(ValueError, match="Git SHA"):
        _candidate(app="short")
    with pytest.raises(ValueError, match="slot registry"):
        _binding(slot_id="caller-selected-slot")
    with pytest.raises(ValueError, match="numeric ID"):
        _binding(repository_id="repo-id")
    with pytest.raises(ValueError, match="owner differs"):
        _binding(repository="different-owner/astrid")
    with pytest.raises(ValueError, match="workflow file"):
        _binding(workflow_ref="different-owner/astrid/.github/workflows/m4.yml@refs/heads/main")


@pytest.mark.parametrize(
    "field",
    (
        "repository_owner_id",
        "repository_id",
        "workflow_id",
        "run_id",
        "check_run_id",
        "job_id",
    ),
)
def test_all_immutable_platform_ids_are_numeric(field: str) -> None:
    with pytest.raises(ValueError, match="positive immutable numeric ID"):
        _binding(**{field: "not-numeric"})


def test_fixture_context_has_no_authority_boolean_and_refuses_binding() -> None:
    context, peer, _key, child = _fixture()
    try:
        assert repr(context) == "TrustedCollectorContext(<opaque>)"
        assert "example-owner" not in repr(context)
        assert not hasattr(context, "hosted_admissible")
        with pytest.raises(AttributeError):
            context.hosted_admissible = True  # type: ignore[attr-defined]
        with pytest.raises(TypeError, match="fixed launcher"):
            launcher.TrustedCollectorContext()
        with pytest.raises(TypeError, match="serialized"):
            pickle.dumps(context)
        with pytest.raises(TypeError, match="copied"):
            copy.copy(context)
        with pytest.raises(TypeError, match="copied"):
            copy.deepcopy(context)
        with pytest.raises(launcher.HostedAdmissionNeeds, match="no authenticated production"):
            context.assert_binding(_binding())
        assert child.terminated == [_child_identity()]
    finally:
        peer.close()


@pytest.mark.parametrize(
    "other_binding",
    [
        _binding(repository_owner_id="9999"),
        _binding(
            repository="example-owner/other",
            workflow_ref="example-owner/other/.github/workflows/m4-6.yml@refs/heads/main",
        ),
        _binding(repository_id="9998"),
        _binding(workflow_ref="example-owner/astrid/.github/workflows/other.yml@refs/heads/main"),
        _binding(workflow_id="9997"),
        _binding(workflow_sha="b" * 40),
        _binding(run_id="100201"),
        _binding(run_attempt=3),
        _binding(check_run_id="300301"),
        _binding(job_id="300401"),
        _binding(slot_id="direct-windows"),
        _binding(candidate=_candidate(app="f" * 40)),
        _binding(candidate=_candidate(agent="f" * 40)),
        _binding(candidate=_candidate(runtime="f" * 40)),
        _binding(candidate=_candidate(pinokio="f" * 40)),
        _binding(candidate=_candidate(manifest_sha256="f" * 64)),
    ],
)
def test_socket_rejects_origin_or_candidate_mismatch(
    other_binding: launcher.TrustedBinding,
) -> None:
    context, peer, key, child = _fixture()
    _send(peer, key, binding=other_binding)

    with pytest.raises(launcher.ContextRejected, match="different origin or candidate"):
        context.receive_message()

    assert child.terminated == [_child_identity()]
    peer.close()


def test_socket_frame_sequence_is_single_use_and_bounded() -> None:
    context, peer, key, child = _fixture()
    frame = launcher._fixture_encode_frame_for_tests(_binding(), key, b"phase-one")
    peer.sendall(frame)
    assert context.receive_message() == b"phase-one"
    peer.sendall(frame)

    with pytest.raises(launcher.ContextRejected, match="stale, skipped, or replayed"):
        context.receive_message()
    assert child.terminated == [_child_identity()]
    peer.close()

    context, peer, key, child = _fixture()
    peer.sendall(struct.pack(">I", launcher._MAX_FRAME_BYTES + 1))
    with pytest.raises(launcher.ContextRejected, match="exceeds the bounded size"):
        context.receive_message()
    assert child.terminated
    peer.close()


def test_socket_rejects_truncated_bad_mac_and_nonexact_envelopes() -> None:
    context, peer, key, child = _fixture()
    frame = launcher._fixture_encode_frame_for_tests(_binding(), key, b"truncated")
    peer.sendall(frame[:-3])
    peer.close()
    with pytest.raises(launcher.ContextRejected, match="closed before a complete frame"):
        context.receive_message()
    assert child.terminated

    context, peer, key, child = _fixture()
    _send(peer, key, mac=b"x" * 32)
    with pytest.raises(launcher.ContextRejected, match="authentication"):
        context.receive_message()
    assert child.terminated
    peer.close()

    context, peer, key, child = _fixture()
    malformed = launcher._fixture_encode_frame_for_tests(
        _binding(), key, b"body", message_length=100
    )
    peer.sendall(malformed)
    with pytest.raises(launcher.ContextRejected, match="envelope is not exact"):
        context.receive_message()
    assert child.terminated
    peer.close()


def test_cross_context_key_replay_fails_even_with_identical_binding() -> None:
    context_a, peer_a, key_a, child_a = _fixture()
    context_b, peer_b, _key_b, child_b = _fixture()
    frame_from_a = launcher._fixture_encode_frame_for_tests(_binding(), key_a, b"from-a")
    peer_a.sendall(frame_from_a)
    assert context_a.receive_message() == b"from-a"
    peer_b.sendall(frame_from_a)

    with pytest.raises(launcher.ContextRejected, match="authentication"):
        context_b.receive_message()

    assert child_a.terminated == []
    assert child_b.terminated == [_child_identity()]
    context_a.close()
    peer_a.close()
    peer_b.close()


def test_terminal_frame_is_authenticated_and_must_be_empty() -> None:
    context, peer, key, child = _fixture()
    child.live = False  # Terminal frame may be followed by immediate child exit.
    _send(peer, key, kind=launcher._FrameKind.TERMINAL)
    assert context.receive_message() is None
    peer.close()
    context.finalize()
    assert child.reaped is True
    assert child.exit_status == 0

    context, peer, key, child = _fixture()
    _send(peer, key, message=b"not-empty", kind=launcher._FrameKind.TERMINAL)
    with pytest.raises(launcher.ContextRejected, match="terminal frame must have an empty"):
        context.receive_message()
    assert child.terminated == [_child_identity()]
    peer.close()


def test_finalize_requires_terminal_and_zero_known_exit_status() -> None:
    context, peer, _key, child = _fixture()
    with pytest.raises(launcher.ContextRejected, match="terminal frame is required"):
        context.finalize()
    assert child.terminated == [_child_identity()]
    peer.close()

    for status, match in [(None, "exact child exit status"), (7, "nonzero status 7")]:
        context, peer, key, child = _fixture()
        _send(peer, key, kind=launcher._FrameKind.TERMINAL)
        assert context.receive_message() is None
        child.exit_status = status
        with pytest.raises((launcher.HostedAdmissionNeeds, launcher.ContextRejected), match=match):
            context.finalize()
        assert child.terminated == ([] if status == 7 else [_child_identity()])
        assert context.failure is not None
        peer.close()


def test_finalize_rejects_trailing_bytes_and_missing_clean_eof() -> None:
    context, peer, key, child = _fixture()
    terminal = launcher._fixture_encode_frame_for_tests(
        _binding(), key, b"", kind=launcher._FrameKind.TERMINAL
    )
    peer.sendall(terminal + b"trailing")
    peer.close()
    assert context.receive_message() is None
    with pytest.raises(launcher.ContextRejected, match="trailing bytes"):
        context.finalize()
    assert child.reaped is True
    assert child.terminated == []
    assert context.failure is not None

    context, peer, key, child = _fixture()
    _send(peer, key, kind=launcher._FrameKind.TERMINAL)
    assert context.receive_message() is None
    with pytest.raises(launcher.ContextRejected, match="EOF was not observed"):
        context.finalize()
    assert child.reaped is True
    assert child.terminated == []
    peer.close()


def test_context_manager_finalizes_or_aborts_and_idle_timer_cleans_abandoned_context() -> None:
    context, peer, key, child = _fixture()
    _send(peer, key, kind=launcher._FrameKind.TERMINAL)
    with context as entered:
        assert entered is context
        assert context.receive_message() is None
        peer.close()
    assert child.finished == [_child_identity()]
    assert child.reaped is True

    context, peer, _key, child = _fixture()
    with pytest.raises(ValueError, match="fixture failure"):
        with context:
            raise ValueError("fixture failure")
    assert child.terminated == [_child_identity()]
    assert child.reaped is True
    peer.close()

    now = [10.0]
    timers: list[_ManualTimer] = []

    def timer_factory(delay: float, callback: object) -> _ManualTimer:
        timer = _ManualTimer(delay, callback)
        timers.append(timer)
        return timer

    context, peer, _key, child = _fixture(
        lifetime_seconds=50,
        idle_timeout_seconds=5,
        clock=lambda: now[0],
        timer_factory=timer_factory,
    )
    assert timers[-1].delay == 5
    now[0] = 15.0
    timers[-1].fire()
    assert child.terminated == [_child_identity()]
    assert child.reaped is True
    assert context.failure == "NEEDS: collector idle/lifetime deadline elapsed"
    assert context.cleanup_failure is None
    assert context._channel._closed is True
    assert context._channel._key == b""
    peer.close()


def test_lifetime_validation_precedes_socket_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_socketpair() -> tuple[socket.socket, socket.socket]:
        raise AssertionError("invalid lifetime must be rejected before socket allocation")

    monkeypatch.setattr(launcher.socket, "socketpair", unexpected_socketpair)
    with pytest.raises(ValueError, match="fifteen minutes"):
        launcher._fixture_context_for_tests(
            _binding(), _child_identity(), _Child(), lifetime_seconds=901
        )


def test_concurrent_finalize_and_abort_have_one_terminal_owner() -> None:
    context, peer, key, child = _fixture()
    _send(peer, key, kind=launcher._FrameKind.TERMINAL)
    assert context.receive_message() is None
    peer.close()
    child.block_finish = True
    failures: list[BaseException] = []
    abort_started = threading.Event()

    def finalize() -> None:
        try:
            context.finalize()
        except Exception as exc:  # noqa: BLE001 - surface background thread failure
            failures.append(exc)

    def abort() -> None:
        abort_started.set()
        context.abort()

    finalizer = threading.Thread(target=finalize)
    finalizer.start()
    assert child.finish_entered.wait(timeout=1.0)
    aborter = threading.Thread(target=abort)
    aborter.start()
    assert abort_started.wait(timeout=1.0)
    child.release_finish.set()
    finalizer.join(timeout=1.0)
    aborter.join(timeout=1.0)

    assert not finalizer.is_alive()
    assert not aborter.is_alive()
    assert failures == []
    assert child.finished == [_child_identity()]
    assert child.terminated == []
    assert child.reaped is True


def test_fork_copy_closes_local_channel_and_requires_parent_supervisor() -> None:
    context, peer, _key, child = _fixture()
    with pytest.raises(launcher.HostedAdmissionNeeds, match="platform supervisor"):
        with context._lock:
            context._owner_pid += 1
            context.binding

    assert context._channel._closed is True
    assert context._channel._key == b""
    assert context._session_id == b""
    assert child.terminated == []
    peer.close()


@pytest.mark.posix_native
def test_real_fork_invalidates_child_copy_but_not_parent_context() -> None:
    if not hasattr(os, "fork"):
        unavailable("os.fork is unavailable on the POSIX native lane")
    context, peer, key, child = _fixture()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        peer.close()
        valid = (
            context._channel._closed
            and context._channel._key == b""
            and context._session_id == b""
            and context._state is launcher._State.ABORTED
        )
        os.write(write_fd, b"ok" if valid else b"bad")
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    child_result = os.read(read_fd, 3)
    os.close(read_fd)
    _, wait_status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(wait_status) == 0
    assert child_result == b"ok"
    assert context._channel._closed is False
    assert context._channel._key == key

    _send(peer, key, message=b"parent-still-valid")
    assert context.receive_message() == b"parent-still-valid"
    assert child.terminated == []
    context.close()
    peer.close()


def test_concurrent_receive_aborts_instead_of_interleaving_frames() -> None:
    context, peer, _key, child = _fixture()
    failures: list[BaseException] = []

    def receive() -> None:
        try:
            context.receive_message()
        except BaseException as exc:  # noqa: BLE001 - capture thread result
            failures.append(exc)

    first = threading.Thread(target=receive)
    first.start()
    deadline = time.monotonic() + 1.0
    while not context._receive_in_flight and time.monotonic() < deadline:
        time.sleep(0.001)
    assert context._receive_in_flight is True
    with pytest.raises(launcher.ContextRejected, match="already in flight"):
        context.receive_message()
    first.join(timeout=1.0)

    assert not first.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], launcher.ContextRejected)
    assert child.terminated == [_child_identity()]
    peer.close()


def test_cleanup_failure_is_stored_and_never_becomes_success() -> None:
    context, peer, _key, child = _fixture()
    child.identity_matches = False

    with pytest.raises(launcher.HostedAdmissionNeeds, match="termination and reap"):
        context.close()

    assert context.cleanup_failure == (
        "NEEDS: OS backend could not prove exact collector child termination and reap"
    )
    with pytest.raises(launcher.HostedAdmissionNeeds, match="termination and reap"):
        context.abort()
    assert child.reaped is False
    peer.close()
