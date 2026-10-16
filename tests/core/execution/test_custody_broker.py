"""CPU-only ownership protocol proof; no kernel signaling qualification."""

from concurrent.futures import ThreadPoolExecutor
import base64
import json
import hashlib
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace

import pytest

from astrid.core.execution import custody_broker as custody


def _inventory_pipe_pair():
    request_read,request_write=os.pipe()
    reply_read,reply_write=os.pipe()
    left=custody._PipeChannel(reply_read,request_write)
    right=custody._PipeChannel(request_read,reply_write)
    binding=custody._canonical([custody._pipe_identity(reply_read,os.O_RDONLY),
                               custody._pipe_identity(request_write,os.O_WRONLY)]).decode()
    return left,right,binding


class _WrapperBroker:
    """A private fake peer; never supplies native custody authority."""

    def __init__(self, mode):
        self.mode = mode
        self.frame = None
        self.error = None
        self.directory = tempfile.TemporaryDirectory(
            prefix="astrid-wrapper-proof-", dir="/private/tmp" if sys.platform == "darwin" else "/tmp",
        )
        self.path = str(Path(self.directory.name) / "s")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(1)
        self.listener.settimeout(3)
        self.thread = threading.Thread(target=self._serve)
        self.thread.start()

    def _serve(self):
        try:
            connection, _ = self.listener.accept()
            with connection:
                connection.settimeout(3)
                self.frame = custody._read_frame(connection)
                ack = {"status": "registered", "run_id": self.frame["run_id"],
                       "role": self.frame["role"], "pid": self.frame["pid"],
                       "registration_digest": custody._digest_bytes(custody._canonical(self.frame))}
                if self.mode in {"status", "run_id", "role", "pid", "registration_digest"}:
                    ack[self.mode] = -1 if self.mode == "pid" else "wrong"
                if self.mode == "malformed":
                    connection.sendall(b"{\n")
                elif self.mode == "oversized":
                    connection.sendall(b"x" * (custody.FRAME_LIMIT + 1))
                elif self.mode == "truncated":
                    connection.sendall(b'{"status":')
                elif self.mode != "eof":
                    custody._send_frame(connection, ack)
        except (socket.timeout, OSError):
            # No connection is expected from the prematurely hooked wrapper.
            pass
        except BaseException as exc:
            self.error = exc

    def close(self):
        self.listener.close()
        self.thread.join(4)
        assert not self.thread.is_alive(), "fake registration peer did not exit"
        self.directory.cleanup()
        assert self.error is None


def _wrapper_environment(tmp_path, broker, *, role="command", target=None, port=8188):
    hook = tmp_path / "hook"
    hook.mkdir(exist_ok=True)
    (hook / "sitecustomize.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['HOOK_MARKER']).write_text('loaded')\n"
        "from astrid.core.execution.network_policy import install_from_environment\n"
        "install_from_environment()\n", encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("ASTRID_", "PYTHON"))}
    target = target or [sys.executable, "-c", "raise AssertionError('target must not execute')"]
    env.update({
        "PYTHONPATH": os.pathsep.join((str(hook), str(Path(custody.__file__).resolve().parents[3]))),
        "HOOK_MARKER": str(tmp_path / "hook-loaded"),
        "TARGET_MARKER": str(tmp_path / "target-result.json"),
        "CAPTURE_PATH": str(tmp_path / "captured-spawn.json"),
        "ASTRID_NETWORK_EVIDENCE": str(tmp_path / "network-evidence.json"),
        "ASTRID_NETWORK_POLICY": json.dumps({"allowed_protocols": ["dns", "tcp"],
                                             "allowed_destinations": [f"127.0.0.1:{port}"]}),
        "ASTRID_RUNTIME_CUSTODY_SOCKET": broker.path,
        "ASTRID_RUNTIME_CUSTODY_RUN_ID": "wrapper-fixture",
        "ASTRID_RUNTIME_CUSTODY_ROLE": role,
        "ASTRID_RUNTIME_CUSTODY_START_SESSION": "0",
        "ASTRID_RUNTIME_CUSTODY_TIMEOUT_SECONDS": "2",
        "ASTRID_RUNTIME_CUSTODY_TARGET_B64": base64.b64encode(custody._canonical(target)).decode(),
    })
    return env


def test_isolated_wrapper_reaches_fake_peer_without_workload_startup_hook(tmp_path):
    broker = _WrapperBroker("status")
    try:
        env = _wrapper_environment(tmp_path, broker)
        ordinary = subprocess.run(
            [sys.executable, "-u", custody.__file__, "--custody-exec"], env=env,
            capture_output=True, text=True, timeout=5,
        )
        assert ordinary.returncode != 0
        assert "network policy denied tcp destination" in ordinary.stderr
        assert broker.frame is None
        (tmp_path / "hook-loaded").unlink()
        isolated = subprocess.run(custody.custody_wrapper_argv(sys.executable), env=env,
                                  capture_output=True, text=True, timeout=5)
        assert isolated.returncode != 0
        assert "custody registration acknowledgement is invalid" in isolated.stderr
        assert broker.frame["command"] == "register_pre_exec"
        assert broker.frame["ppid"] == os.getpid()
        assert not (tmp_path / "hook-loaded").exists()
        assert not (tmp_path / "target-result.json").exists()
    finally:
        broker.close()


@pytest.mark.parametrize("mode", ["status", "run_id", "role", "pid", "registration_digest",
                                  "malformed", "oversized", "truncated", "eof"])
def test_isolated_wrapper_invalid_peer_never_executes_target(tmp_path, mode):
    broker = _WrapperBroker(mode)
    try:
        result = subprocess.run(custody.custody_wrapper_argv(sys.executable),
                                env=_wrapper_environment(tmp_path, broker),
                                capture_output=True, text=True, timeout=5)
        assert result.returncode != 0
        assert broker.frame["run_id"] == "wrapper-fixture"
        assert not (tmp_path / "target-result.json").exists()
        assert not (tmp_path / "hook-loaded").exists()
    finally:
        broker.close()


@pytest.mark.parametrize("role", ["command", "engine"])
def test_isolated_wrapper_target_restores_network_hook_and_original_environment(tmp_path, role):
    # A shim replaces only native suspended SETEXEC, so this proves startup
    # preservation without claiming a kernel admission/resume qualification.
    endpoint = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    endpoint.bind(("127.0.0.1", 0))
    endpoint.listen(1)
    port = endpoint.getsockname()[1]
    broker = _WrapperBroker("valid")
    target_source = (
        "import os,socket,json; from pathlib import Path; "
        f"s=socket.create_connection(('127.0.0.1',{port})); s.close(); "
        "denied=False\n"
        "try: socket.create_connection(('127.0.0.1',1))\n"
        "except Exception as e: denied=type(e).__name__=='NetworkPolicyError'\n"
        "fd=os.environ.get('ASTRID_ENGINE_CONTROL_FD'); "
        "[os.fstat(p['fd']) for p in json.loads(fd)] if fd else None; "
        "Path(os.environ['TARGET_MARKER']).write_text(json.dumps({'denied':denied,"
        "'custody_keys':[k for k in os.environ if k.startswith('ASTRID_RUNTIME_CUSTODY_')],"
        "'engine_fd':bool(fd)}))"
    )
    target = [sys.executable, "-c", target_source]
    env = _wrapper_environment(tmp_path, broker, role=role, target=target, port=port)
    pipe_left,pipe_right,pipe_binding=_inventory_pipe_pair()
    pass_fds=()
    if role=='engine':
        env[custody._BOOTSTRAP_PIPES]=pipe_binding
        pass_fds=(pipe_left.read_fd,pipe_left.write_fd)
    shim = (
        "import runpy,os,json; from pathlib import Path; "
        f"m=runpy.run_path({custody.__file__!r},run_name='wrapper_fixture'); "
        "g=m['child_exec_from_environment'].__globals__\n"
        "def spawn(argv,environment):\n"
        " Path(os.environ['CAPTURE_PATH']).write_text(json.dumps({'argv':argv,'env':environment}))\n"
        " os.execve(argv[0],argv,environment)\n"
        "g['_posix_spawn_setexec_suspended']=spawn\n"
        "m['child_exec_from_environment']()\n"
    )
    try:
        result = subprocess.run([sys.executable, "-I", "-S", "-u", "-c", shim], env=env,
                                pass_fds=pass_fds, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        captured = json.loads((tmp_path / "captured-spawn.json").read_text())
        assert captured["argv"] == target
        assert captured["env"]["PYTHONPATH"] == env["PYTHONPATH"]
        assert captured["env"]["ASTRID_NETWORK_POLICY"] == env["ASTRID_NETWORK_POLICY"]
        assert (tmp_path / "hook-loaded").read_text() == "loaded"
        observed = json.loads((tmp_path / "target-result.json").read_text())
        assert observed == {"denied": True, "custody_keys": [], "engine_fd": role == "engine"}
    finally:
        broker.close()
        endpoint.close()
        pipe_left.close();pipe_right.close()


@pytest.fixture
def graph(tmp_path, monkeypatch):
    identities = {pid: {"pid": pid, "uid": 501, "birth_id": f"birth-{pid}"} for pid in (101, 102, 103)}
    tokens = {pid: {"pid": pid, "uid": 501, "pidversion": pid + 1,
                    "sha256": f"sha256:token-{pid}", "words": [pid] * 8} for pid in identities}
    monkeypatch.setattr(custody, "audit_token_details", lambda words: {k: v for k, v in tokens[words[0]].items() if k != "words"})
    observe = identities.get
    actors = {pid: custody.AuthenticatedCleanupActor(lambda pid=pid: tokens[pid], observe) for pid in (101, 102)}
    authority = custody.RoleCustodyAuthority(tmp_path / "scope", "engine", timeout=0.2)
    authority.designate_pending(actor=actors[101], identity=identities[103], token=tokens[103], owner_epoch="runtime-A")
    authority.bind_target(actor=actors[101], generation=1, identity=identities[103], token=tokens[103])
    return authority, actors, identities, tokens


def transfer(graph, *, transition_id="handoff-1", dead_actor=None):
    authority, actors, _, _ = graph
    return authority.transfer(actor=actors[102] if dead_actor else actors[101], next_actor=actors[102], generation=1,
                              transition_id=transition_id, owner_epoch="runtime-B", dead_actor=dead_actor)


def fire(graph, *, actor_pid=101, generation=1, observe=None, signal_provider=None):
    authority, actors, identities, _ = graph
    authority.signal(actor=actors[actor_pid], generation=generation, expected_target=authority.reference()["target"],
                     signum=signal.SIGTERM, identity_provider=observe or identities.get,
                     signal_provider=signal_provider or (lambda words, signum: None))


def test_pending_cleanup_owner_is_durable_but_cannot_signal(tmp_path, graph):
    _, actors, identities, tokens = graph
    authority = custody.RoleCustodyAuthority(tmp_path / "pending", "host")
    authority.designate_pending(actor=actors[101], identity=identities[103], token=tokens[103], owner_epoch="runtime-A")
    with pytest.raises(custody.CustodyError, match="pending"):
        authority.signal(actor=actors[101], generation=1, expected_target=authority.reference()["target"],
                         signum=signal.SIGKILL, identity_provider=identities.get, signal_provider=lambda *_: pytest.fail("pending signal"))


def test_suspended_launch_resume_is_durable_exact_and_one_shot(graph):
    authority, actors, identities, tokens = graph
    calls = []
    target = authority.reference()["target"]
    authority.resume_suspended_target(
        actor=actors[101], generation=1, expected_target=target,
        release_id="launch-1", identity_provider=identities.get,
        resume_provider=lambda words: calls.append(tuple(words)),
    )
    assert calls == [tuple(tokens[103]["words"])]
    record = authority._read()
    assert record["transitions"][-1]["intent"]["action"] == "resume-suspended-launch"
    assert record["transitions"][-1]["ack"] == {"status": "resume-sent"}
    with pytest.raises(custody.CustodyError, match="already consumed"):
        authority.resume_suspended_target(
            actor=actors[101], generation=1, expected_target=target,
            release_id="launch-1", identity_provider=identities.get,
            resume_provider=lambda *_: pytest.fail("one-shot release replayed"),
        )
    with pytest.raises(custody.CustodyError, match="outside the admitted set"):
        authority.signal(actor=actors[101], generation=1, expected_target=target,
                         signum=signal.SIGCONT, identity_provider=identities.get,
                         signal_provider=lambda *_: pytest.fail("SIGCONT entered general signal path"))


def test_failed_suspended_resume_consumes_intent_without_retry(graph):
    authority, actors, identities, _ = graph
    target = authority.reference()["target"]
    calls = []

    def fail(words):
        calls.append(tuple(words))
        raise OSError("injected resume failure")

    with pytest.raises(custody.CustodyError, match="remains unresolved"):
        authority.resume_suspended_target(
            actor=actors[101], generation=1, expected_target=target,
            release_id="launch-failed", identity_provider=identities.get,
            resume_provider=fail,
        )
    assert len(calls) == 1
    assert authority._read()["transitions"][-1]["ack"]["status"] == "resume-call-failed"
    with pytest.raises(custody.CustodyError, match="already consumed"):
        authority.resume_suspended_target(
            actor=actors[101], generation=1, expected_target=target,
            release_id="launch-failed", identity_provider=identities.get,
            resume_provider=lambda *_: pytest.fail("failed one-shot release retried"),
        )


def test_pre_spawn_obligation_binds_only_exact_admission_before_exec(tmp_path, graph):
    _, actors, identities, tokens = graph
    authority = custody.RoleCustodyAuthority(tmp_path / "launch", "relay")
    authority.reserve_before_spawn(actor=actors[101], owner_epoch="runtime-A", admission_id="launch-1")
    assert authority.reference()["target"] == {"admission_id": "launch-1"}
    with pytest.raises(custody.CustodyError, match="differs"):
        authority.bind_pending(actor=actors[101], admission_id="other-launch", identity=identities[103], token=tokens[103])
    authority.bind_pending(actor=actors[101], admission_id="launch-1", identity=identities[103], token=tokens[103])
    authority.bind_target(actor=actors[101], generation=1, identity=identities[103], token=tokens[103])
    with pytest.raises(custody.CustodyError, match="differs"):
        authority.bind_pending(actor=actors[101], admission_id="launch-1", identity=identities[103], token=tokens[103])


def test_signal_holds_shared_guard_until_kernel_call_completes(graph):
    authority, _, _, _ = graph
    in_kernel, release, attempted, transferred = (threading.Event() for _ in range(4))
    events = []

    def kernel(*_):
        in_kernel.set()
        assert release.wait(1)
        events.append("kernel-complete")

    def move():
        attempted.set()
        result = transfer(graph)
        events.append("transfer-durable")
        transferred.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        signaling = pool.submit(fire, graph, signal_provider=kernel)
        assert in_kernel.wait(1)
        moving = pool.submit(move)
        assert attempted.wait(1)
        assert not transferred.wait(0.03)
        release.set()
        signaling.result(timeout=1)
        assert moving.result(timeout=1)["generation"] == 2
    assert events == ["kernel-complete", "transfer-durable"]
    assert authority.reference()["generation"] == 2


@pytest.mark.parametrize("actor_pid,generation", [(101, 1), (101, 2), (102, 1)])
def test_old_actor_or_generation_cannot_signal_after_transfer(graph, actor_pid, generation):
    transfer(graph)
    with pytest.raises(custody.CustodyError, match="stale"):
        fire(graph, actor_pid=actor_pid, generation=generation, signal_provider=lambda *_: pytest.fail("stale signal"))
    fire(graph, actor_pid=102, generation=2)


def test_transfer_before_durable_commit_leaves_old_owner(graph, monkeypatch):
    authority, _, _, _ = graph
    write = authority._write
    monkeypatch.setattr(authority, "_write", lambda _: (_ for _ in ()).throw(OSError("before write")))
    with pytest.raises(OSError, match="before write"):
        transfer(graph)
    assert authority.reference()["generation"] == 1
    fire(graph)
    monkeypatch.setattr(authority, "_write", write)
    assert transfer(graph)["generation"] == 2


def test_commit_then_lost_ack_recovers_exact_durable_replay(graph, monkeypatch):
    authority, _, _, _ = graph
    write = authority._write

    def lose_ack(record):
        write(record)
        raise OSError("lost ACK")

    monkeypatch.setattr(authority, "_write", lose_ack)
    with pytest.raises(OSError, match="lost ACK"):
        transfer(graph)
    assert authority.reference()["generation"] == 2
    monkeypatch.setattr(authority, "_write", lambda _: pytest.fail("replay must not recommit"))
    ack = transfer(graph)
    assert transfer(graph) == ack
    with pytest.raises(custody.CustodyError, match="conflicts"):
        authority.transfer(actor=graph[1][101], next_actor=graph[1][102], generation=1,
                           transition_id="handoff-1", owner_epoch="different")


class Child:
    pid = 101
    exit_code = None

    def poll(self):
        return self.exit_code


def test_positive_owned_child_exit_allows_recovery_but_unknown_does_not(graph):
    child = Child()
    retained = custody.RetainedChildActor(child, graph[1][101])
    with pytest.raises(custody.CustodyError, match="positive"):
        transfer(graph, dead_actor=retained)
    child.exit_code = 0
    assert transfer(graph, dead_actor=retained)["generation"] == 2
    fire(graph, actor_pid=102, generation=2)


def test_pid_reuse_or_absence_never_creates_actor_death_proof(graph):
    _, actors, identities, _ = graph
    child = Child()
    retained = custody.RetainedChildActor(child, actors[101])
    identities[101] = {"pid": 101, "birth_id": "reused", "uid": 501}
    with pytest.raises(custody.CustodyError, match="positive"):
        transfer(graph, dead_actor=retained)
    identities.pop(101)
    with pytest.raises(custody.CustodyError, match="positive"):
        transfer(graph, dead_actor=retained)
    assert graph[0].reference()["generation"] == 1


@pytest.mark.parametrize("observed", [None, {"pid": 103, "birth_id": "reused", "uid": 501}])
def test_unknown_or_reused_target_never_signals(graph, observed):
    with pytest.raises(custody.CustodyError, match="unknown"):
        fire(graph, observe=lambda _: observed, signal_provider=lambda *_: pytest.fail("unknown target signal"))


def test_runtime_epoch_transition_does_not_seize_delegated_roles(graph):
    authority, actors, identities, tokens = graph
    relay = custody.RoleCustodyAuthority(authority.root, "relay")
    relay.designate_pending(actor=actors[101], identity=identities[103], token=tokens[103], owner_epoch="runtime-A")
    relay.bind_target(actor=actors[101], generation=1, identity=identities[103], token=tokens[103])
    relay.transfer(actor=actors[101], next_actor=actors[102], generation=1, transition_id="runtime-transfer", owner_epoch="runtime-B")
    assert relay.reference()["generation"] == 2
    assert authority.reference()["generation"] == 1
    fire(graph)


def test_bounded_exclusive_lock_timeout_leaves_authority_unchanged(graph):
    authority = graph[0]
    with authority._guard(exclusive=False):
        with pytest.raises(custody.CustodyError, match="timed out"):
            transfer(graph)
    assert authority.reference()["generation"] == 1


def establish_anchor(graph, *, start=None, clock=None, capture=None):
    authority, actors, _, tokens = graph
    observed = {"pid": 103, "uid": 501, "start_abstime": 100,
                "start_sec": 123, "start_usec": 456, "status": 4}
    reader = start or (lambda _: dict(observed))
    authority.establish_start_anchor(
        actor=actors[101], generation=1, expected_target=authority.reference()["target"],
        authenticated_token=tokens[103], start_provider=reader,
        clock_provider=clock or (lambda: 101), token_provider=capture or tokens.get,
    )
    return reader


def rotate_token(graph, version):
    tokens = graph[3]
    tokens[103] = {**tokens[103], "pidversion": version, "sha256": f"sha256:rotation-{version}",
                   "words": [103] * 7 + [version]}


def fresh_fire(graph, reader, *, expected=None, capture=None, kernel=None,
               actor_pid=101, generation=1):
    authority, actors, identities, tokens = graph
    return authority.signal(
        actor=actors[actor_pid], generation=generation,
        expected_target=expected or authority.reference()["target"], signum=signal.SIGTERM,
        identity_provider=identities.get, start_provider=reader,
        token_provider=capture or tokens.get, signal_provider=kernel or (lambda *_: None),
    )


def test_strict_birth_fence_precedes_exact_authenticated_recapture(graph):
    ticks = iter((100, 101))
    calls = []
    reader = establish_anchor(graph, clock=lambda: next(ticks),
                              capture=lambda pid: calls.append(pid) or graph[3][pid])
    assert calls == [103]
    intent = graph[0]._read()["transitions"][-1]["intent"]
    assert intent["clock_fence"] > intent["start_anchor"]["start_abstime"]
    assert graph[0]._live_start_anchor == custody._start_anchor(reader(103))


@pytest.mark.parametrize("failure", ["clock-rollback", "token-rotation", "birth-change", "not-stopped"])
def test_anchor_cannot_upgrade_unproven_or_changed_admission(graph, failure):
    observed = {"pid": 103, "uid": 501, "start_abstime": 100,
                "start_sec": 123, "start_usec": 456, "status": 4}
    calls = 0

    def start(_):
        nonlocal calls
        calls += 1
        if failure == "birth-change" and calls > 1:
            return {**observed, "start_abstime": 200}
        return {**observed, "status": 2 if failure == "not-stopped" else 4}

    def token(pid):
        if failure == "token-rotation":
            rotate_token(graph, 500)
        return graph[3][pid]

    with pytest.raises(custody.CustodyError):
        establish_anchor(graph, start=start, clock=lambda: 99 if failure == "clock-rollback" else 101,
                         capture=token)
    assert graph[0]._live_start_anchor is None
    assert graph[0]._read()["transitions"] == []


def test_multiple_exec_refresh_persists_exact_token_and_rejects_old_reference(graph):
    reader = establish_anchor(graph)
    original = graph[0].reference()["target"]
    calls = []
    for version in (500, 501):
        rotate_token(graph, version)

        def kernel(words, signum):
            durable = graph[0]._read()
            assert durable["target"]["audit_token_words"] == list(words)
            assert durable["generation"] == 1
            calls.append((tuple(words), signum))

        reference = fresh_fire(graph, reader, kernel=kernel)
        assert reference["target"]["audit_token_pidversion"] == version
    with pytest.raises(custody.CustodyError, match="stale"):
        fresh_fire(graph, reader, expected=original, kernel=lambda *_: pytest.fail("stale reference signalled"))
    assert len(calls) == 2
    assert set(reference["target"]) == {"pid", "uid", "birth_id", "audit_token_sha256", "audit_token_pidversion"}


@pytest.mark.parametrize("failure", ["birth-before", "birth-after", "capture", "persistence", "actor-change"])
def test_refresh_failure_never_signals_or_adopts_replacement(graph, monkeypatch, failure):
    reader = establish_anchor(graph)
    initial = graph[0].reference()
    reads = 0

    def start(pid):
        nonlocal reads
        reads += 1
        value = dict(reader(pid))
        if failure == "birth-before" or (failure == "birth-after" and reads > 1):
            value["start_abstime"] += 1
        return value

    def capture(pid):
        if failure == "capture":
            raise custody.CustodyError("injected capture failure")
        if failure == "actor-change":
            graph[2][101] = {**graph[2][101], "birth_id": "changed-actor"}
        return graph[3][pid]

    rotate_token(graph, 600)
    if failure == "persistence":
        monkeypatch.setattr(graph[0], "_write", lambda _: (_ for _ in ()).throw(OSError("injected fsync failure")))
    with pytest.raises((custody.CustodyError, OSError)):
        fresh_fire(graph, start, capture=capture, kernel=lambda *_: pytest.fail("failed refresh signalled"))
    assert graph[0].reference() == initial


def test_only_definitely_unsuccessful_exec_signal_is_retried(graph):
    reader = establish_anchor(graph)
    calls = []
    rotate_token(graph, 700)

    def kernel(words, _):
        calls.append(tuple(words))
        if len(calls) == 1:
            rotate_token(graph, 701)
            raise custody.AuditTokenSignalError(3, 3)

    result = fresh_fire(graph, reader, kernel=kernel)
    assert len(calls) == 2
    assert result["target"]["audit_token_pidversion"] == 701
    for error in (custody.AuditTokenSignalError(1, 1), OSError("ambiguous transport failure")):
        calls.clear()

        def rejected(words, _):
            calls.append(tuple(words))
            raise error

        with pytest.raises((custody.CustodyError, OSError)):
            fresh_fire(graph, reader, kernel=rejected)
        assert len(calls) == 1


def test_exec_retry_is_bounded_and_transfer_still_fences_actor_generation(graph):
    reader = establish_anchor(graph)
    calls = []

    def kernel(words, _):
        calls.append(tuple(words))
        raise custody.AuditTokenSignalError(3, 3)

    with pytest.raises(custody.AuditTokenSignalError):
        fresh_fire(graph, reader, kernel=kernel)
    assert len(calls) == 3
    transfer(graph)
    for actor_pid, generation in ((101, 1), (101, 2), (102, 1)):
        with pytest.raises(custody.CustodyError, match="stale"):
            fresh_fire(graph, reader, actor_pid=actor_pid, generation=generation,
                       kernel=lambda *_: pytest.fail("stale actor/generation signalled"))


def test_disk_recovery_cannot_resurrect_live_refresh_anchor(graph):
    establish_anchor(graph)
    authority, actors, identities, tokens = graph
    original_words = list(tokens[103]["words"])
    recovered = custody.RoleCustodyAuthority(authority.root, authority.role)
    rotate_token(graph, 800)
    calls = []
    recovered.signal(actor=actors[101], generation=1, expected_target=recovered.reference()["target"],
                     signum=signal.SIGTERM, identity_provider=identities.get,
                     token_provider=lambda _: pytest.fail("disk record was refreshed"),
                     signal_provider=lambda words, _: calls.append(list(words)))
    assert calls == [original_words]


@pytest.mark.parametrize("kind", ["live-root", "exited-root", "exited-descendant", "persistence-error"])
def test_only_positive_retained_direct_child_exit_settles_absent_signal(kind):
    broker = object.__new__(custody.RoleBoundCustodyBroker)

    def signal_target(*_, **__):
        if kind == "persistence-error":
            raise OSError("injected persistence failure")
        raise custody.DarwinProcessExitedError("exited during exact signal capture")

    broker.signal = signal_target
    owner = object.__new__(custody.ProcessCustody)
    owner._lock = threading.RLock()
    owner.process = SimpleNamespace(pid=103, poll=lambda: None if kind == "live-root" else 0)
    pid = 104 if kind == "exited-descendant" else 103
    owner._handles = {pid: ("birth", broker)}
    if kind == "exited-root":
        owner.signal(pid, "birth", signal.SIGKILL)
    else:
        with pytest.raises((custody.CustodyError, OSError)):
            owner.signal(pid, "birth", signal.SIGKILL)


@pytest.mark.parametrize("case", [
    "waited-root", "live-root", "waited-other-root", "observed-descendant",
    "birth-replaced", "foreign-actor", "stale-generation", "persistence-error", "linux",
])
def test_real_authority_absence_reconciles_only_positive_retained_root(graph, monkeypatch, case):
    authority, actors, identities, _ = graph
    monkeypatch.setattr(custody.sys, "platform", "linux" if case == "linux" else "darwin")
    monkeypatch.setattr(custody, "default_process_identity", lambda pid, **_kwargs: identities.get(pid))
    monkeypatch.setattr(custody, "_darwin_process_records", lambda *_: pytest.fail("fake signal vector queried native census"))
    reference = authority.reference()
    real_signal = authority.signal
    authority.signal = lambda **kwargs: real_signal(
        **kwargs, signal_provider=lambda *_: pytest.fail("absence or invalid binding reached kernel")
    )
    broker = object.__new__(custody.RoleBoundCustodyBroker)
    broker.state, broker.registration = "sealed", {"identity": dict(identities[103])}
    broker.role_authority, broker.cleanup_actor = authority, actors[101]
    broker.custody_generation, broker.identity_provider, broker._cleanup_deadline = 1, identities.get, None
    prior_uncertainty = custody.CustodyError("independent descendant obligation")
    owner = object.__new__(custody.ProcessCustody)
    owner._lock, owner._uncertain = threading.RLock(), prior_uncertainty
    owner.actor = actors[101]
    owner.process = SimpleNamespace(
        pid=100 if case in {"waited-other-root", "observed-descendant"} else 103,
        poll=lambda: None if case == "live-root" else 0,
    )
    owner._handles = {103: ("birth-103", (authority, reference) if case == "observed-descendant" else broker)}
    if case == "birth-replaced":
        identities[103] = {**identities[103], "birth_id": "replacement"}
    else:
        identities.pop(103)
    if case == "foreign-actor":
        broker.cleanup_actor = actors[102]
    elif case == "stale-generation":
        broker.custody_generation = 2
    elif case == "persistence-error":
        monkeypatch.setattr(authority, "_read_current", lambda: (_ for _ in ()).throw(OSError("ledger unavailable")))
    if case == "waited-root":
        owner.signal(103, "birth-103", signal.SIGTERM)
    else:
        with pytest.raises((custody.CustodyError, OSError)) as error:
            owner.signal(103, "birth-103", signal.SIGTERM)
        if case in {"live-root", "waited-other-root", "observed-descendant"}:
            assert isinstance(error.value, custody.DarwinProcessExitedError)
        else:
            assert not isinstance(error.value, custody.DarwinProcessExitedError)
    assert 103 in owner._handles and owner._uncertain is prior_uncertainty


def test_anchor_persistence_failure_never_creates_live_refresh_authority(graph, monkeypatch):
    monkeypatch.setattr(graph[0], "_write", lambda _: (_ for _ in ()).throw(OSError("anchor fsync failure")))
    with pytest.raises(OSError, match="fsync"):
        establish_anchor(graph)
    assert graph[0]._live_start_anchor is None
    assert graph[0]._read()["transitions"] == []


def test_live_anchor_owner_loss_is_fenced_even_after_authority_transfer(graph):
    reader = establish_anchor(graph)
    transfer(graph)
    graph[2][101] = {**graph[2][101], "birth_id": "dead-original-owner"}
    with pytest.raises(custody.CustodyError, match="actor incarnation"):
        fresh_fire(graph, reader, actor_pid=102, generation=2,
                   kernel=lambda *_: pytest.fail("lost live scope refreshed target"))


@pytest.mark.parametrize("has_error,validated,registered", [(False, True, True), (True, False, True), (True, True, False)])
def test_failed_admission_cannot_gain_signal_authority_from_refresh(has_error, validated, registered):
    broker = object.__new__(custody.RoleBoundCustodyBroker)
    broker.error = custody.CustodyError("admission failure") if has_error else None
    broker._post_exec_authority_validated = validated
    broker.registration = {"identity": {"pid": 103}} if registered else None
    broker.role_authority = SimpleNamespace(signal=lambda **_: pytest.fail("unadmitted child signalled"))
    with pytest.raises(custody.CustodyError, match="no validated post-exec authority"):
        broker.signal_failed_admission(signal.SIGTERM, expected_pid=103)


def listener_signal_case(*, version=custody.ENGINE_CONTROL_SIGNAL_V2):
    target = {"pid": 103, "uid": 501, "birth_id": "birth-103",
              "audit_token_sha256": "sha256:" + "a" * 64, "audit_token_pidversion": 104}
    reference = {"version": custody.ROLE_REFERENCE_VERSION, "scope_root": "/private/custody",
                 "role": "engine_listener", "generation": 1, "target": target}
    request = {"version": version, "command": "signal_owned_listener", "operation_id": "operation",
               "channel_id": "channel", "owner_epoch": "epoch", "role": "engine_listener",
               "generation": 1, "target": dict(target), "signal": 15}
    actor = SimpleNamespace(verify=lambda: {"pid": 101})
    calls = []
    def signal_target(**kwargs):
        calls.append(kwargs)
        return reference
    listener = SimpleNamespace(role_authority=SimpleNamespace(signal=signal_target),
                               cleanup_actor=actor, identity_provider=lambda _: None)
    return request, reference, actor, listener, calls


def deliver_listener_signal(request, actor, listener):
    return custody.signal_owned_listener(request, engine_actor=actor, launch_parent=actor,
                                         listener=listener, operation_id="operation", channel_id="channel",
                                         owner_epoch="epoch")


def test_listener_signal_v1_exact_echo_bytes_remain_unchanged():
    request, reference, actor, listener, calls = listener_signal_case(version=custody.ENGINE_CONTROL_VERSION)
    reference["target"] = {**reference["target"], "audit_token_sha256": "sha256:" + "b" * 64}
    ack = deliver_listener_signal(request, actor, listener)
    assert custody._canonical(ack) == custody._canonical(
        {**{k: v for k, v in request.items() if k != "command"}, "status": "signaled"})
    assert "current_reference" not in ack and len(calls) == 1


def test_listener_signal_v2_returns_exact_successful_authority_reference():
    request, reference, actor, listener, calls = listener_signal_case()
    reference["target"] = {**reference["target"], "audit_token_sha256": "sha256:" + "b" * 64,
                           "audit_token_pidversion": 105}
    ack = deliver_listener_signal(request, actor, listener)
    assert len(calls) == 1 and ack["current_reference"] is reference
    assert ack["target"] == request["target"]
    assert not {"exited", "cleaned", "reaped", "transferred"}.intersection(ack)


def test_listener_signal_v2_foreign_authenticated_engine_never_signals():
    request, _, actor, listener, calls = listener_signal_case()
    with pytest.raises(custody.CustodyError, match="foreign"):
        custody.signal_owned_listener(request, engine_actor=SimpleNamespace(verify=lambda: {"pid": 102}),
                                      launch_parent=actor, listener=listener, operation_id="operation",
                                      channel_id="channel", owner_epoch="epoch")
    assert calls == []


@pytest.mark.parametrize("field,value", [
    ("version", "other"), ("command", "observe_owned_listener_exit"), ("role", "engine"),
    ("operation_id", "other"), ("channel_id", "other"), ("owner_epoch", "other"),
    ("signal", 15.0), ("signal", True), ("signal", 30), ("signal", int(signal.SIGCONT)),
    ("generation", True), ("generation", 1.0), ("generation", 0),
    ("target", {}), ("target", []),
])
def test_listener_signal_v2_invalid_binding_never_reaches_authority(field, value):
    request, _, actor, listener, calls = listener_signal_case()
    request[field] = value
    with pytest.raises(custody.CustodyError):
        deliver_listener_signal(request, actor, listener)
    assert calls == []


@pytest.mark.parametrize("change", ["extra", "missing", "target-extra", "target-missing", "pid-float", "uid-bool"])
def test_listener_signal_v2_rejects_exact_shape_and_scalar_type_changes(change):
    request, _, actor, listener, calls = listener_signal_case()
    if change == "extra":
        request["extra"] = True
    elif change == "missing":
        request.pop("channel_id")
    elif change == "target-extra":
        request["target"]["extra"] = True
    elif change == "target-missing":
        request["target"].pop("uid")
    elif change == "pid-float":
        request["target"]["pid"] = 103.0
    else:
        request["target"]["uid"] = True
    with pytest.raises(custody.CustodyError):
        deliver_listener_signal(request, actor, listener)
    assert calls == []


@pytest.mark.parametrize("error", [custody.CustodyError("stale target"), OSError("fsync failure"),
                                 custody.AuditTokenSignalError(1, 1), custody.AuditTokenSignalError(3, 3)])
def test_listener_signal_v2_authority_failure_has_no_success_ack(error):
    request, _, actor, listener, _ = listener_signal_case()
    def fail(**kwargs):
        raise error
    listener.role_authority.signal = fail
    with pytest.raises(type(error)) as raised:
        deliver_listener_signal(request, actor, listener)
    assert raised.value is error


@pytest.mark.parametrize("field,value", [("generation", 2), ("generation", 1.0),
                                       ("pid", 104), ("uid", 502), ("birth_id", "replaced")])
def test_listener_signal_v2_conflicting_return_is_uncertain_after_kernel_call(field, value):
    request, reference, actor, listener, calls = listener_signal_case()
    if field == "generation":
        reference[field] = value
    else:
        reference["target"] = {**reference["target"], field: value}
    with pytest.raises(custody.CustodyError, match="conflicting"):
        deliver_listener_signal(request, actor, listener)
    assert len(calls) == 1


def test_listener_signal_v2_exact_refresh_term_then_stale_kill_then_current_kill(graph, tmp_path):
    _, actors, identities, tokens = graph
    tokens[103]["sha256"] = "sha256:" + "a" * 64
    authority = custody.RoleCustodyAuthority(tmp_path / "listener", "engine_listener")
    authority.designate_pending(actor=actors[101], identity=identities[103], token=tokens[103], owner_epoch="epoch")
    authority.bind_target(actor=actors[101], generation=1, identity=identities[103], token=tokens[103])
    selected_graph = (authority, actors, identities, tokens)
    reader = establish_anchor(selected_graph)
    initial = authority.reference()
    calls = []
    real_signal = authority.signal
    def fake_kernel(words, signum):
        assert authority._read()["target"]["audit_token_words"] == list(words)
        calls.append(signum)
    authority.signal = lambda **kwargs: real_signal(**kwargs, start_provider=reader,
                                                   token_provider=tokens.get, signal_provider=fake_kernel)
    listener = SimpleNamespace(role_authority=authority, cleanup_actor=actors[101], identity_provider=identities.get)
    request, _, _, _, _ = listener_signal_case()
    request["target"] = initial["target"]
    rotate_token(selected_graph, 500)
    tokens[103]["sha256"] = "sha256:" + "b" * 64
    term = deliver_listener_signal(request, actors[101], listener)
    with pytest.raises(custody.CustodyError, match="stale"):
        deliver_listener_signal({**request, "signal": 9}, actors[101], listener)
    kill = deliver_listener_signal({**request, "target": term["current_reference"]["target"], "signal": 9},
                                   actors[101], listener)
    assert calls == [15, 9] and kill["current_reference"] == term["current_reference"]


def cr1a_ready(graph, monkeypatch, *, commit=True):
    authority, actors, identities, tokens = graph
    reader = establish_anchor(graph)
    monkeypatch.setattr(custody, "_darwin_process_start", reader)
    authority.resume_suspended_target(actor=actors[101], generation=1,
        expected_target=authority.reference()["target"], release_id="cr1a-launch",
        identity_provider=identities.get, resume_provider=lambda _: None)
    rotate_token(graph, 600)
    base = authority.bind_target(actor=actors[101], generation=1,
        identity=identities[103], token=tokens[103], final_ready=True)
    if commit:
        authority.commit_ready()
    return reader, base


@pytest.mark.parametrize("count", [0, 1, 2])
def test_cr1a_chain_preserves_admission_resume_ready_baseline_and_revisions(graph, monkeypatch, count):
    reader, base = cr1a_ready(graph, monkeypatch)
    for version in range(601, 601 + count):
        rotate_token(graph, version)
        fresh_fire(graph, reader)
    authority, actors, identities, _ = graph
    del identities[103]  # Historical cleanup does not observe the dead listener.
    monkeypatch.setattr(custody, "_darwin_process_start", lambda _: pytest.fail("listener start observation"))
    monkeypatch.setattr(custody, "current_process_audit_token", lambda _: pytest.fail("listener token observation"))
    monkeypatch.setattr(authority, "_write", lambda _: pytest.fail("token_chain wrote state"))
    proof = authority.token_chain(actor=actors[101], generation=1, base_target=base["target"])
    assert proof["revision"] == count
    assert authority.ready_reference() == base
    assert [x["intent"]["action"] for x in authority._admission_prefix] == ["establish-live-start-anchor", "resume-suspended-launch"]
    assert authority._admission_prefix[-1]["ack"] == {"status": "resume-sent"}
    assert proof["current_reference"] == authority.reference()
    assert proof["refresh_proof_digest"].startswith("sha256:")


def test_cr1a_pre_ready_refresh_is_admission_and_staged_interleave_does_not_reset_base(graph, monkeypatch):
    authority, actors, identities, tokens = graph
    reader = establish_anchor(graph)
    monkeypatch.setattr(custody, "_darwin_process_start", reader)
    rotate_token(graph, 500); fresh_fire(graph, reader)
    authority.resume_suspended_target(actor=actors[101], generation=1, expected_target=authority.reference()["target"],
        release_id="pre-ready", identity_provider=identities.get, resume_provider=lambda _: None)
    base = authority.bind_target(actor=actors[101], generation=1, identity=identities[103], token=tokens[103], final_ready=True)
    with pytest.raises(custody.CustodyError, match="unavailable"):
        authority.token_chain(actor=actors[101], generation=1, base_target=base["target"])
    rotate_token(graph, 501); fresh_fire(graph, reader)
    authority.commit_ready()
    proof = authority.token_chain(actor=actors[101], generation=1, base_target=base["target"])
    assert proof["revision"] == 1
    assert len(authority._admission_prefix) == 3
    assert authority.ready_reference() == base
    with pytest.raises(custody.CustodyError, match="rebound"):
        authority.bind_target(actor=actors[101], generation=1, identity=identities[103], token=tokens[103], final_ready=True)
    with pytest.raises(custody.CustodyError, match="after readiness"):
        authority.resume_suspended_target(actor=actors[101], generation=1, expected_target=authority.reference()["target"],
            release_id="again", identity_provider=identities.get, resume_provider=lambda _: pytest.fail("resume replay"))


@pytest.mark.parametrize("change", ["prefix", "missing", "extra", "reordered", "previous", "pid", "uid", "birth", "float-pid"])
def test_cr1a_disk_history_tampering_is_permanently_fenced(graph, monkeypatch, change):
    import copy
    reader, base = cr1a_ready(graph, monkeypatch)
    authority, actors, _, _ = graph
    for version in (601, 602):
        rotate_token(graph, version); fresh_fire(graph, reader)
    original = authority._read(); changed = copy.deepcopy(original)
    if change == "prefix": changed["transitions"][1]["ack"] = {"status": "resume-intent-durable"}
    elif change == "missing": changed["transitions"].pop()
    elif change == "extra": changed["transitions"].append(copy.deepcopy(changed["transitions"][-1]))
    elif change == "reordered": changed["transitions"][-2:] = list(reversed(changed["transitions"][-2:]))
    elif change == "previous": changed["transitions"][-1]["intent"]["previous_target"] = base["target"]
    elif change == "float-pid": changed["target"]["pid"] = float(changed["target"]["pid"])
    else: changed["target"]["birth_id" if change == "birth" else change] = "replacement" if change == "birth" else 999
    authority._write(changed)
    with pytest.raises(custody.CustodyError): authority.token_chain(actor=actors[101], generation=1, base_target=base["target"])
    authority._write(original)  # Restoring disk cannot restore retained proof.
    with pytest.raises(custody.CustodyError, match="unavailable"):
        authority.token_chain(actor=actors[101], generation=1, base_target=base["target"])


@pytest.mark.parametrize("change", ["generation", "requester", "anchor", "actor", "base", "float-generation", "fresh-object"])
def test_cr1a_chain_rejects_substitution_and_fresh_disk_bootstrap(graph, monkeypatch, change):
    _, base = cr1a_ready(graph, monkeypatch)
    authority, actors, _, _ = graph
    actor, generation, target = actors[101], 1, dict(base["target"])
    if change == "generation": authority._anchor_transition["intent"]["generation"] = 2
    elif change == "requester": authority._anchor_transition["intent"]["requester"] = actors[102].verify()
    elif change == "anchor": authority._live_start_anchor = {**authority._live_start_anchor, "start_abstime": 999}
    elif change == "actor": actor = actors[102]
    elif change == "base": target["audit_token_pidversion"] += 1
    elif change == "float-generation": generation = 1.0
    else: authority = custody.RoleCustodyAuthority(authority.root, authority.role)
    with pytest.raises(custody.CustodyError): authority.token_chain(actor=actor, generation=generation, base_target=target)


@pytest.mark.parametrize("rotate", [False, True])
def test_cr1a_signal_checkpoint_failure_disables_proof_and_never_appends_uncommitted_ledger(graph, monkeypatch, rotate):
    reader, base = cr1a_ready(graph, monkeypatch)
    authority, actors, _, _ = graph
    if rotate: rotate_token(graph, 601)
    write = authority._write
    monkeypatch.setattr(authority, "_write", lambda _: (_ for _ in ()).throw(OSError("checkpoint")))
    with pytest.raises(OSError): fresh_fire(graph, reader, kernel=lambda *_: pytest.fail("signal after fsync failure"))
    assert authority._refresh_ledger == []
    monkeypatch.setattr(authority, "_write", write)
    with pytest.raises(custody.CustodyError, match="unavailable"):
        authority.token_chain(actor=actors[101], generation=1, base_target=base["target"])


def test_cr1a_ambiguous_signal_keeps_committed_refresh_without_replay(graph, monkeypatch):
    reader, base = cr1a_ready(graph, monkeypatch)
    rotate_token(graph, 601)
    with pytest.raises(OSError): fresh_fire(graph, reader, kernel=lambda *_: (_ for _ in ()).throw(OSError("ambiguous")))
    proof = graph[0].token_chain(actor=graph[1][101], generation=1, base_target=base["target"])
    assert proof["revision"] == 1
    assert proof["chain"][0]["ack"]["target"] == graph[0].reference()["target"]


@pytest.mark.parametrize("checkpoint", ["admission", "anchor", "resume-intent", "resume-sent", "ready", "broker-ready"])
def test_cr1a_every_failed_checkpoint_permanently_disables_proof(graph, monkeypatch, checkpoint):
    authority, actors, identities, tokens = graph
    write = authority._write
    calls = []
    def fail(record):
        write(record)
        raise OSError("lost checkpoint ACK")
    if checkpoint == "admission":
        authority = custody.RoleCustodyAuthority(authority.root / "new", "engine")
        write = authority._write
        monkeypatch.setattr(authority, "_write", fail)
        with pytest.raises(OSError): authority.designate_pending(actor=actors[101], identity=identities[103], token=tokens[103], owner_epoch="runtime-A")
    elif checkpoint == "anchor":
        monkeypatch.setattr(authority, "_write", fail)
        with pytest.raises(OSError): establish_anchor(graph)
    elif checkpoint.startswith("resume"):
        establish_anchor(graph)
        def fail_resume(record):
            if record["transitions"][-1]["ack"]["status"] == ("resume-sent" if checkpoint == "resume-sent" else "resume-intent-durable"):
                fail(record)
            else: write(record)
        monkeypatch.setattr(authority, "_write", fail_resume)
        with pytest.raises(OSError): authority.resume_suspended_target(actor=actors[101], generation=1, expected_target=authority.reference()["target"],
            release_id="failed", identity_provider=identities.get, resume_provider=lambda _: calls.append("resume"))
        assert calls == (["resume"] if checkpoint == "resume-sent" else [])
    elif checkpoint == "ready":
        reader = establish_anchor(graph); monkeypatch.setattr(custody, "_darwin_process_start", reader)
        authority.resume_suspended_target(actor=actors[101], generation=1, expected_target=authority.reference()["target"],
            release_id="ready-failure", identity_provider=identities.get, resume_provider=lambda _: None)
        monkeypatch.setattr(authority, "_write", fail)
        with pytest.raises(OSError): authority.bind_target(actor=actors[101], generation=1, identity=identities[103], token=tokens[103], final_ready=True)
    else:
        _, base = cr1a_ready(graph, monkeypatch, commit=False)
        broker = custody.RoleBoundCustodyBroker.__new__(custody.RoleBoundCustodyBroker)
        broker.role_authority = authority
        monkeypatch.setattr(broker, "_persist_unfenced", lambda _: (_ for _ in ()).throw(OSError("broker checkpoint")))
        with pytest.raises(OSError): broker._persist("readiness_token_bound")
        with pytest.raises(custody.CustodyError): authority.commit_ready()
    assert authority._ledger_broken
    with pytest.raises(custody.CustodyError): authority.token_chain(actor=actors[101], generation=1, base_target=graph[0].reference()["target"])


def test_cr1a_broker_ready_checkpoint_interleave_publishes_exact_staged_base(graph, monkeypatch):
    authority, actors, identities, tokens = graph
    reader = establish_anchor(graph)
    monkeypatch.setattr(custody, "_darwin_process_start", reader)
    authority.resume_suspended_target(actor=actors[101], generation=1, expected_target=authority.reference()["target"],
        release_id="broker-ready", identity_provider=identities.get, resume_provider=lambda _: None)
    rotate_token(graph, 600)
    broker = custody.RoleBoundCustodyBroker.__new__(custody.RoleBoundCustodyBroker)
    broker.role_authority, broker.cleanup_actor, broker.custody_generation = authority, actors[101], 1
    broker.state, broker.sequence = "sealed", 2
    broker.identity_provider = identities.get
    broker.registration = {"identity": identities[103], "audit_token_sha256": "provisional", "audit_token_pidversion": 104}
    stages = []
    def persist(event):
        stages.append(event)
        with pytest.raises(custody.CustodyError, match="unavailable"):
            authority.token_chain(actor=actors[101], generation=1, base_target=authority._ready_base["target"])
        rotate_token(graph, 601)
        fresh_fire(graph, reader)
        with pytest.raises(custody.CustodyError): authority.ready_reference()
    monkeypatch.setattr(broker, "_persist", persist)
    broker.bind_ready_token(expected_pid=103, expected_identity=identities[103], token_provider=tokens.get)
    assert stages == ["readiness_token_bound"]
    base = authority.ready_reference()
    assert base["target"]["audit_token_pidversion"] == 600
    proof = authority.token_chain(actor=actors[101], generation=1, base_target=base["target"])
    assert proof["revision"] == 1
    assert proof["current_reference"]["target"]["audit_token_pidversion"] == 601


def test_cr1a_failed_broker_ready_persistence_never_publishes_baseline(graph, monkeypatch):
    authority, actors, identities, tokens = graph
    reader = establish_anchor(graph)
    monkeypatch.setattr(custody, "_darwin_process_start", reader)
    authority.resume_suspended_target(actor=actors[101], generation=1, expected_target=authority.reference()["target"],
        release_id="ready-broker-failure", identity_provider=identities.get, resume_provider=lambda _: None)
    broker = custody.RoleBoundCustodyBroker.__new__(custody.RoleBoundCustodyBroker)
    broker.role_authority, broker.cleanup_actor, broker.custody_generation = authority, actors[101], 1
    broker.state, broker.sequence, broker.identity_provider = "sealed", 2, identities.get
    broker.registration = {"identity": identities[103]}
    monkeypatch.setattr(broker, "_persist", lambda _: (_ for _ in ()).throw(OSError("readiness lost ACK")))
    with pytest.raises(OSError): broker.bind_ready_token(expected_pid=103, expected_identity=identities[103], token_provider=tokens.get)
    with pytest.raises(custody.CustodyError): authority.ready_reference()
    with pytest.raises(custody.CustodyError): authority.commit_ready()
    assert not authority._ready_committed and authority._ledger_broken


@pytest.mark.parametrize("phase", ["reserve", "bind-pending", "provisional"])
def test_cr1a_admission_checkpoint_failures_cannot_bootstrap_proof(graph, monkeypatch, phase):
    authority, actors, identities, tokens = graph
    authority = custody.RoleCustodyAuthority(authority.root / phase, "engine_listener")
    write = authority._write
    def fail(record):
        write(record)
        raise OSError("admission ACK lost")
    if phase != "reserve": authority.reserve_before_spawn(actor=actors[101], owner_epoch="runtime-A", admission_id="child")
    if phase == "provisional": authority.bind_pending(actor=actors[101], admission_id="child", identity=identities[103], token=tokens[103])
    monkeypatch.setattr(authority, "_write", fail)
    with pytest.raises(OSError):
        if phase == "reserve": authority.reserve_before_spawn(actor=actors[101], owner_epoch="runtime-A", admission_id="child")
        elif phase == "bind-pending": authority.bind_pending(actor=actors[101], admission_id="child", identity=identities[103], token=tokens[103])
        else: authority.bind_target(actor=actors[101], generation=1, identity=identities[103], token=tokens[103])
    assert authority._ledger_broken
    with pytest.raises(custody.CustodyError): authority.token_chain(actor=actors[101], generation=1, base_target=authority.reference()["target"])


def test_cr1a_suspended_admission_cannot_publish_ready_before_consumed_resume(graph, monkeypatch):
    reader = establish_anchor(graph)
    monkeypatch.setattr(custody, "_darwin_process_start", reader)
    authority, actors, identities, tokens = graph
    with pytest.raises(custody.CustodyError, match="completed suspended"):
        authority.bind_target(actor=actors[101], generation=1, identity=identities[103], token=tokens[103], final_ready=True)
    assert authority._ready_base is None


@pytest.mark.parametrize("refreshes", [0, 1, 2])
def test_admission_chain_keeps_authenticated_pre_ready_suffix(graph, monkeypatch, refreshes):
    import copy
    authority, actors, _, tokens = graph
    reader = establish_anchor(graph)
    monkeypatch.setattr(custody, "_darwin_process_start", reader)
    base = copy.deepcopy(authority.reference()["target"])
    authority.resume_suspended_target(actor=actors[101], generation=1, expected_target=base,
        release_id="admission-test", identity_provider=graph[2].get, resume_provider=lambda _: None)
    for version in range(200, 200 + refreshes):
        rotate_token(graph, version); fresh_fire(graph, reader)
    proof = authority.admission_chain(actor=actors[101], generation=1, base_target=base)
    assert proof["revision"] == refreshes
    assert proof["current_reference"] == authority.reference()
    assert authority._ready_base is None
    with pytest.raises(custody.CustodyError): authority.ready_reference()
    with pytest.raises(custody.CustodyError): authority.token_chain(actor=actors[101], generation=1, base_target=base)
    if refreshes:
        suffix = authority.admission_chain(actor=actors[101], generation=1, base_target=proof["chain"][0]["ack"]["target"])
        assert suffix["revision"] == refreshes - 1
    authority.bind_target(actor=actors[101], generation=1, identity=graph[2][103], token=tokens[103], final_ready=True)
    authority.commit_ready()
    with pytest.raises(custody.CustodyError): authority.admission_chain(actor=actors[101], generation=1, base_target=base)
    assert authority.ready_reference() == authority.reference()


@pytest.mark.parametrize("mutation", ["foreign-base", "generation", "actor", "disk-extra", "mirror", "anchor", "broken"])
def test_admission_chain_rejects_unretained_or_foreign_history(graph, mutation):
    import copy
    authority, actors, _, _ = graph
    reader = establish_anchor(graph)
    base = copy.deepcopy(authority.reference()["target"])
    rotate_token(graph, 200); fresh_fire(graph, reader)
    generation, actor = 1, actors[101]
    if mutation == "foreign-base": base["audit_token_pidversion"] += 99
    elif mutation == "generation": generation = 2
    elif mutation == "actor": actor = actors[102]
    elif mutation == "disk-extra":
        record = authority._read(); record["transitions"].append(copy.deepcopy(record["transitions"][-1])); authority._write(record)
    elif mutation == "mirror": authority._admission_refresh_ledger.clear()
    elif mutation == "anchor": authority._anchor_transition["intent"]["requester"] = {}
    else: authority.break_token_chain()
    with pytest.raises(custody.CustodyError): authority.admission_chain(actor=actor, generation=generation, base_target=base)


def test_pending_admission_chain_only_proves_exact_never_spawned_obligation(tmp_path, graph):
    authority = custody.RoleCustodyAuthority(tmp_path / "pending-admission", "engine")
    actor = graph[1][101]
    authority.reserve_before_spawn(actor=actor, owner_epoch="epoch", admission_id="pending")
    proof = authority.admission_chain(actor=actor, generation=1, base_target={"admission_id": "pending"})
    assert proof["revision"] == 0 and proof["chain"] == []
    assert authority._ready_base is None
    with pytest.raises(custody.CustodyError): authority.admission_chain(actor=actor, generation=1, base_target={"admission_id": "other"})


@pytest.fixture
def observed_descendant(graph, tmp_path, monkeypatch):
    """Real role authority, controlled identity/token providers, no kernel calls."""
    _, actors, identities, tokens = graph
    monkeypatch.setattr(custody.sys, "platform", "darwin")
    monkeypatch.setattr(custody, "default_process_identity", identities.get)
    def fake_observation(pid):
        try:
            identity = custody.default_process_identity(pid)
            return {"outcome": "LIVE", "identity": identity} if identity else {"outcome": "ABSENT"}
        except Exception as error:
            return {"outcome": "OBSERVATION_ERROR", "error": error}
    monkeypatch.setattr(custody, "ordinary_candidate_observation", fake_observation)
    monkeypatch.setattr(custody, "current_process_audit_token",
                        lambda pid: {**tokens[pid], "words": list(tokens[pid]["words"])})
    monkeypatch.setattr(custody, '_DarwinExitObservation', lambda observation: SimpleNamespace(
        queue=None, facts={'authority': 'NONE', 'outcome': 'SYNTHETIC_NOT_REGISTERED'}))
    owner = object.__new__(custody.ProcessCustody)
    owner.root = tmp_path / "observed-custody"
    owner.owner_epoch = "fixture-owner"
    owner.actor = actors[101]
    owner._lock = threading.RLock()
    owner._handles = {}
    owner._uncertain = None
    owner.process = SimpleNamespace(pid=100, poll=lambda: 0)
    return owner, actors, identities, tokens


def test_ordinary_observed_descendant_retains_fixed_token_without_exit_race_anchor(observed_descendant, monkeypatch):
    owner, _, identities, tokens = observed_descendant
    def forbidden_anchor(*_args, **_kwargs):
        raise custody.DarwinProcessExitedError("late live query would lose this short-lived helper")
    monkeypatch.setattr(custody.RoleCustodyAuthority, "establish_start_anchor", forbidden_anchor)
    monkeypatch.setattr(custody, "_darwin_process_start", lambda _: pytest.fail("ordinary descendant refresh queried"))
    owner.observe({103: "birth-103"})
    authority, reference = owner._handles[103][1]
    assert authority._live_start_anchor is None
    assert reference["generation"] == 1 and reference["target"]["pid"] == 103
    assert authority._read()["target"]["audit_token_words"] == tokens[103]["words"]
    assert authority._read()["transitions"] == []
    # A later valid census may observe absence; this does not manufacture a
    # parent wait/reap record or drop the retained fixed-token obligation.
    identities.pop(103)
    owner.observe({})
    assert owner._uncertain is None and 103 in owner._handles
    assert not hasattr(owner, "descendant_wait_exit")


@pytest.mark.parametrize("failure", ["missing-token", "malformed-words", "capture-change", "identity-change", "pre-bind-exit", "persistence"])
def test_observed_descendant_registration_failures_latch_uncertainty(observed_descendant, monkeypatch, failure):
    owner, _, identities, tokens = observed_descendant
    if failure == "missing-token":
        monkeypatch.setattr(custody, "current_process_audit_token", lambda _: {})
    elif failure == "malformed-words":
        monkeypatch.setattr(custody, "current_process_audit_token", lambda _: {**tokens[103], "words": "invalid"})
    elif failure == "capture-change":
        def changed_capture(_pid):
            raise custody.CustodyError("final-ready Runtime audit token changed while binding")
        monkeypatch.setattr(custody, "current_process_audit_token", changed_capture)
    elif failure in {"identity-change", "pre-bind-exit"}:
        observations = iter((identities[103], None if failure == "pre-bind-exit" else {**identities[103], "birth_id": "replacement"}))
        monkeypatch.setattr(custody, "default_process_identity", lambda _: next(observations))
    else:
        def failed_write(*_args, **_kwargs):
            raise OSError("injected designation persistence failure")
        monkeypatch.setattr(custody.RoleCustodyAuthority, "_write", failed_write)
    with pytest.raises((custody.CustodyError, OSError)):
        owner.observe({103: "birth-103"})
    assert owner._uncertain is not None and 103 not in owner._handles
    with pytest.raises(custody.CustodyError, match="unresolved"):
        owner.observe({})


@pytest.mark.parametrize("change", ["actor", "generation", "reference", "pid", "birth", "uid"])
def test_observed_fixed_token_signal_preserves_all_existing_fences(observed_descendant, change):
    owner, actors, identities, _ = observed_descendant
    owner.observe({103: "birth-103"})
    authority, reference = owner._handles[103][1]
    calls = []
    real_signal = authority.signal
    authority.signal = lambda **kwargs: real_signal(**kwargs, signal_provider=lambda *args: calls.append(args))
    if change == "actor":
        owner.actor = actors[102]
    elif change == "generation":
        owner._handles[103] = ("birth-103", (authority, {**reference, "generation": 2}))
    elif change == "reference":
        owner._handles[103] = ("birth-103", (authority, {**reference, "target": {**reference["target"], "pid": 999}}))
    else:
        key = {"pid": "pid", "birth": "birth_id", "uid": "uid"}[change]
        identities[103] = {**identities[103], key: "replacement" if change == "birth" else 999}
    with pytest.raises(custody.CustodyError):
        owner.signal(103, "birth-103", signal.SIGTERM)
    assert calls == [] and 103 in owner._handles


def test_observed_postcapture_exec_keeps_original_token_and_rejects_uncertain_cleanup(observed_descendant, monkeypatch):
    owner, _, _, tokens = observed_descendant
    owner.observe({103: "birth-103"})
    authority, reference = owner._handles[103][1]
    original_words = list(tokens[103]["words"])
    tokens[103] = {**tokens[103], "pidversion": 999, "sha256": "sha256:post-exec", "words": [999] * 8}
    monkeypatch.setattr(custody, "current_process_audit_token", lambda _: pytest.fail("untransferred descendant token refreshed"))
    calls = []
    def reject_stale(words, signum):
        calls.append((list(words), signum))
        raise custody.AuditTokenSignalError(3, 0)
    real_signal = authority.signal
    authority.signal = lambda **kwargs: real_signal(**kwargs, signal_provider=reject_stale)
    with pytest.raises(custody.AuditTokenSignalError):
        owner.signal(103, "birth-103", signal.SIGTERM)
    assert calls == [(original_words, signal.SIGTERM)]
    assert owner._handles[103][1][1] == reference
    assert authority._live_start_anchor is None
    assert authority._read()["target"]["audit_token_words"] == original_words
    # The direct child is positively exited in the fixture. That cannot
    # settle an independent descendant's rejected signal or custody.
    assert owner.process.poll() == 0 and 103 in owner._handles


def _census_record(pid=123, *, ppid=1, pgid=123, uid=501, status=3, sec=1791421200, usec=123456):
    item = custody._DarwinKInfoProc()
    item.proc.pid, item.proc.status = pid, status
    item.proc.un.start.tv_sec, item.proc.un.start.tv_usec = sec, usec
    item.eproc.ppid, item.eproc.pgid, item.eproc.ucred.uid = ppid, pgid, uid
    return bytes(item)


class _FakeSysctl:
    def __init__(self, raw, *, failure=None, size=None, used=None):
        self.raw, self.failure, self.size, self.used = raw, failure, size, used
        self.calls = []

    def __call__(self, mib, count, buffer, size_pointer, _new, _new_size):
        import ctypes
        self.calls.append(tuple(mib))
        size = ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_size_t))
        phase = "size" if buffer is None else "fill"
        if self.failure == phase:
            ctypes.set_errno(12 if phase == "fill" else 1)
            return -1
        if buffer is None:
            size.contents.value = len(self.raw) if self.size is None else self.size
        else:
            ctypes.memmove(buffer, self.raw, len(self.raw))
            size.contents.value = len(self.raw) if self.used is None else self.used
        return 0


def _fake_census(monkeypatch, raw, **kwargs):
    monkeypatch.setattr(custody.sys, "platform", "darwin")
    call = _FakeSysctl(raw, **kwargs)
    monkeypatch.setattr(custody.ctypes, "CDLL", lambda *_a, **_kw: SimpleNamespace(sysctl=call))
    monkeypatch.setattr(custody.subprocess, "run", lambda *_a, **_kw: pytest.fail("observer spawned helper process"))
    return call


def test_native_census_retains_protected_uid_zombie_and_kernel_topology(monkeypatch):
    raw = (_census_record(0, ppid=0, pgid=0, uid=0, sec=0, usec=0)
           + _census_record(123, uid=0)
           + _census_record(7988, ppid=7981, pgid=7981, uid=501, status=5))
    call = _fake_census(monkeypatch, raw)
    rows = custody._darwin_process_records()
    assert [row["pid"] for row in rows] == [0, 123, 7988]
    assert rows[1]["uid"] == 0 and rows[2]["status"] == 5
    assert rows[2]["parent_pid"] == 7981 and rows[2]["start_usec"] == 123456
    assert call.calls == [(1, 14, 0, 0)] * 2


def test_native_identity_preserves_public_shape_birth_and_microsecond_compatibility(monkeypatch):
    call = _fake_census(monkeypatch, _census_record())
    identity = custody.default_process_identity(123)
    assert identity == {"pid": 123, "uid": 501, "parent_pid": 1,
                        "birth_id": custody._ps_lstart_birth(1791421200)}
    assert call.calls == [(1, 14, 1, 123)] * 2
    # Legacy birth strings stay second-resolution compatibility data; changing
    # microseconds is not falsely advertised as a new authority guarantee.
    _fake_census(monkeypatch, _census_record(usec=999999))
    assert custody.default_process_identity(123) == identity


def test_native_selected_absence_is_observation_and_zombie_is_not_live_authority(monkeypatch):
    _fake_census(monkeypatch, b"")
    assert custody.default_process_identity(123) is None
    _fake_census(monkeypatch, _census_record(status=5))
    with pytest.raises(custody.DarwinProcessExitedError, match="zombie"):
        custody.default_process_identity(123)


@pytest.mark.parametrize("failure", ["size", "fill", "partial-size", "partial-fill", "oversize", "empty", "duplicate", "status", "birth", "usec", "selector"])
def test_native_census_failures_never_become_empty_success(monkeypatch, failure):
    raw, kwargs = _census_record(), {}
    if failure in {"size", "fill"}: kwargs["failure"] = failure
    elif failure == "partial-size": kwargs["size"] = 1
    elif failure == "partial-fill": kwargs["used"] = 1
    elif failure == "oversize": kwargs["size"] = 648 * 30000
    elif failure == "empty": raw = b""
    elif failure == "duplicate": raw += raw
    elif failure == "status": raw = _census_record(status=0)
    elif failure == "birth": raw = _census_record(sec=0)
    elif failure == "usec": raw = _census_record(usec=1000000)
    else: raw = _census_record(pid=124)
    _fake_census(monkeypatch, raw, **kwargs)
    with pytest.raises(custody.CustodyError):
        custody._darwin_process_records(123 if failure == "selector" else None)


@pytest.mark.parametrize("pid", [0, -1, True, "123", 1 << 31])
def test_native_census_rejects_non_user_selectors(monkeypatch, pid):
    _fake_census(monkeypatch, _census_record())
    with pytest.raises(custody.CustodyError, match="selector"):
        custody._darwin_process_records(pid)


def test_native_census_rejects_platform_and_pointer_abi(monkeypatch):
    monkeypatch.setattr(custody.sys, "platform", "linux")
    with pytest.raises(custody.CustodyError, match="ABI"):
        custody._darwin_process_records()
    monkeypatch.setattr(custody.sys, "platform", "darwin")
    real_sizeof = custody.ctypes.sizeof
    monkeypatch.setattr(custody.ctypes, "sizeof", lambda item: 4 if item is custody.ctypes.c_void_p else real_sizeof(item))
    with pytest.raises(custody.CustodyError, match="ABI"):
        custody._darwin_process_records()


@pytest.mark.parametrize("dst,hour", [(0, 1), (1, 2)])
def test_birth_formatter_retains_fixed_english_day_padding_in_utc(monkeypatch, dst, hour):
    import time
    monkeypatch.setattr(custody.time, "localtime", lambda sec: pytest.fail("birth used local time"))
    monkeypatch.setattr(custody.time, "gmtime", lambda sec: time.struct_time((2026, 10, 8, hour, 3, 4, 3, 281, dst)))
    assert custody._ps_lstart_birth(1791421200) == f"ps-lstart:Thu Oct  8 {hour:02d}:03:04 2026"


@pytest.mark.parametrize("stage", ["initial-identity", "token-capture", "post-token-identity"])
def test_descendant_diagnostic_preserves_pid_phase_and_cause(observed_descendant, monkeypatch, stage):
    owner, _, identities, _ = observed_descendant
    if stage == "initial-identity":
        monkeypatch.setattr(custody, "default_process_identity", lambda _: None)
    elif stage == "token-capture":
        monkeypatch.setattr(custody, "current_process_audit_token", lambda _: (_ for _ in ()).throw(custody.CustodyError("token probe failed")))
    else:
        values = iter((identities[103], None))
        monkeypatch.setattr(custody, "default_process_identity", lambda _: next(values))
    with pytest.raises(custody.CustodyError) as error:
        owner.observe({103: "birth-103"})
    assert f"stage={stage}" in str(error.value) and "candidate_pid=103" in str(error.value)
    assert "expected_birth=birth-103" in str(error.value)
    assert error.value.__cause__ is not None and owner._uncertain is not None


@pytest.mark.parametrize("outcome", ["absent", "birth-mismatch", "zombie"])
def test_initial_descendant_identity_outcomes_are_distinct_and_sticky(observed_descendant, monkeypatch, outcome):
    owner, _, identities, _ = observed_descendant
    if outcome == "absent":
        identities.pop(103)
    elif outcome == "birth-mismatch":
        identities[103] = {**identities[103], "birth_id": "replacement-birth"}
    else:
        monkeypatch.setattr(custody, "default_process_identity", lambda _: (_ for _ in ()).throw(
            custody.DarwinProcessExitedError("Darwin process identity observed a zombie; exit/reap remains unproved")
        ))
    with pytest.raises(custody.CustodyError) as error:
        owner.observe({103: "birth-103"})
    assert "stage=initial-identity" in str(error.value)
    assert outcome in str(error.value)
    assert 103 not in owner._handles and owner._uncertain is not None
    with pytest.raises(custody.CustodyError, match="unresolved"):
        owner.observe({})


def test_registration_timeout_diagnostic_retains_launch_pid_and_phase():
    broker = object.__new__(custody.RoleBoundCustodyBroker)
    broker.timeout = 0
    broker._ready = threading.Event()
    broker._observation_phase, broker._launch_pid = "accept", 123
    with pytest.raises(custody.CustodyError, match="stage=accept.*launch_pid=123"):
        broker.wait_until_sealed()


def _ordinary_census_fixture(observed_descendant):
    owner, _, identities, _ = observed_descendant
    broker = object.__new__(custody.RoleBoundCustodyBroker)
    broker.role = "command"
    owner._launch_broker = broker
    owner._handles[100] = ("root", broker)
    owner._unadmitted_absent = {}
    census = {100: {"birth_id": "root", "parent_pid": 1, "pgid": 100},
              103: {"birth_id": "birth-103", "parent_pid": 100, "pgid": 103}}
    identities.pop(103)
    identities[102]["parent_pid"] = 100
    return owner, identities, census


def test_unadmitted_absence_retains_durable_non_authority_and_continues_other_admission(observed_descendant):
    owner, _, census = _ordinary_census_fixture(observed_descendant)
    census[102] = {"birth_id": "birth-102", "parent_pid": 100, "pgid": 102}
    live = owner.observe_census({100: "root", 103: "birth-103", 102: "birth-102"},
                               census=census, fresh_census=lambda: {k: v for k, v in census.items() if k != 103})
    assert live == {100: "root", 102: "birth-102"}
    assert owner._uncertain is None and 103 not in owner._handles
    evidence = json.loads((owner.root / "observed-containment.json").read_text())
    record = evidence["unadmitted_candidates"][0]
    assert record["disposition"] == "UNADMITTED_ABSENT"
    assert record["capability"] == "NONE" and not record["admitted"]
    assert record["exit_reap"] == record["subtree"] == "UNPROVED"
    assert record["census_parent_pid"] == 100 and record["root"]["pid"] == 100
    assert record["observer"]["pid"] == 101 and record["observation_sequence"] == 1
    assert evidence["complete_descendant_history"] == "UNPROVED"
    assert (owner.root / "observed-containment.json").stat().st_mode & 0o777 == 0o600
    assert not list(owner.root.glob("designation-descendant_103_*.json"))
    with pytest.raises(custody.CustodyError, match="lacks a retained exact"):
        owner.signal(103, "birth-103", signal.SIGTERM)


@pytest.mark.parametrize("contradiction", ["reappeared", "reused", "missing-parent-child", "empty", "admitted-reuse", "lookup-error", "initial-persist", "final-persist"])
def test_unadmitted_absence_reconciliation_failures_are_sticky(observed_descendant, monkeypatch, contradiction):
    owner, identities, census = _ordinary_census_fixture(observed_descendant)
    fresh = {100: census[100]}
    if contradiction == "reappeared":
        fresh[103] = census[103]
    elif contradiction == "reused":
        fresh[103] = {**census[103], "birth_id": "different"}
    elif contradiction == "missing-parent-child":
        fresh[104] = {"birth_id": "child", "parent_pid": 103, "pgid": 104}
    elif contradiction == "empty":
        fresh = {}
    elif contradiction == "admitted-reuse":
        fresh[100] = {**census[100], "birth_id": "different"}
    elif contradiction == "lookup-error":
        monkeypatch.setattr(custody, "default_process_identity", lambda _: (_ for _ in ()).throw(OSError("lookup failed")))
    elif contradiction in {"initial-persist", "final-persist"}:
        original = custody._atomic_owner_json
        writes = []
        def write(path, value):
            writes.append(value)
            if contradiction == "initial-persist" or len(writes) == 2:
                raise OSError("evidence persistence failed")
            original(path, value)
        monkeypatch.setattr(custody, "_atomic_owner_json", write)
    with pytest.raises((custody.CustodyError, OSError)):
        owner.observe_census({100: "root", 103: "birth-103"}, census=census, fresh_census=lambda: fresh)
    assert owner._uncertain is not None and 103 not in owner._handles
    with pytest.raises(custody.CustodyError, match="unresolved"):
        owner.observe_census({}, census={100: census[100]}, fresh_census=lambda: {100: census[100]})


@pytest.mark.parametrize("change", ["reappearance", "reuse", "child"])
def test_unadmitted_tombstone_survives_future_census_and_cannot_be_upgraded(observed_descendant, change):
    owner, _, census = _ordinary_census_fixture(observed_descendant)
    fresh = {100: census[100]}
    owner.observe_census({100: "root", 103: "birth-103"}, census=census, fresh_census=lambda: fresh)
    if change == "child":
        fresh[104] = {"birth_id": "child", "parent_pid": 103, "pgid": 104}
    else:
        fresh[103] = {**census[103], "birth_id": "birth-103" if change == "reappearance" else "replacement"}
    with pytest.raises(custody.CustodyError):
        owner.observe_census({}, census=fresh, fresh_census=lambda: fresh)
    assert owner._uncertain is not None and 103 not in owner._handles
    assert owner._unadmitted_absent[103]["capability"] == "NONE"


@pytest.mark.parametrize("role", ["engine", "engine_listener", "runtime_owner"])
def test_anchored_roles_do_not_gain_unadmitted_absence_disposition(observed_descendant, role):
    owner, _, census = _ordinary_census_fixture(observed_descendant)
    owner._launch_broker.role = role
    with pytest.raises(custody.CustodyError, match="identity_outcome=absent"):
        owner.observe_census({103: "birth-103"}, census=census, fresh_census=lambda: {100: census[100]})
    assert owner._uncertain is not None and not owner._unadmitted_absent


def test_unadmitted_disposition_never_settles_existing_uncertainty_or_unseen_fork_history(observed_descendant):
    owner, _, census = _ordinary_census_fixture(observed_descendant)
    fresh = {100: census[100]}
    owner.observe_census({100: "root", 103: "birth-103"}, census=census, fresh_census=lambda: fresh)
    # No census contains a hypothetical detached unseen fork. The accepted
    # disposition still explicitly cannot assert complete subtree history.
    assert owner._unadmitted_absent[103]["subtree"] == "UNPROVED"
    owner._uncertain = custody.CustodyError("prior admitted obligation unresolved")
    with pytest.raises(custody.CustodyError, match="unresolved"):
        owner.observe_census({}, census=fresh, fresh_census=lambda: fresh)


def test_initial_census_vanished_pair_is_reconciled_individually(observed_descendant):
    owner, _, census = _ordinary_census_fixture(observed_descendant)
    census[104] = {"birth_id": "short-lived-child", "parent_pid": 103, "pgid": 104}
    result = owner.observe_census({100: "root", 103: "birth-103", 104: "short-lived-child"},
                                 census=census, fresh_census=lambda: {100: census[100]})
    assert result == {100: "root"}
    assert set(owner._unadmitted_absent) == {103, 104}
    assert all(row["capability"] == "NONE" for row in owner._unadmitted_absent.values())
    assert owner._uncertain is None and set(owner._handles) == {100}
    retained = json.loads((owner.root / "observed-containment.json").read_text())
    assert {row["pid"] for row in retained["observed_candidates"]} == {103, 104}


def test_observed_census_does_not_change_linux_pidfd_admission(observed_descendant, monkeypatch):
    from astrid.core.execution import process_group as groups
    owner, _, census = _ordinary_census_fixture(observed_descendant)
    monkeypatch.setattr(custody.sys, "platform", "linux")
    monkeypatch.setattr(groups, "process_birth_identity", lambda *_: "birth-103")
    monkeypatch.setattr(custody.os, "pidfd_open", lambda *_: 77, raising=False)
    inherited = []
    monkeypatch.setattr(custody.os, "set_inheritable", lambda *args: inherited.append(args))
    assert owner.observe_census({103: "birth-103"}, census=census,
                                fresh_census=lambda: pytest.fail("Linux gained candidate reconciliation")) == {103: "birth-103"}
    assert owner._handles[103] == ("birth-103", 77) and inherited == [(77, False)]
    assert not owner._unadmitted_absent


def test_postcapture_absence_without_native_precondition_remains_fail_closed(observed_descendant, monkeypatch):
    owner, identities, census = _ordinary_census_fixture(observed_descendant)
    identity = {"pid": 103, "uid": 501, "birth_id": "birth-103", "parent_pid": 100}
    values = iter((identity, None))
    monkeypatch.setattr(custody, "default_process_identity", lambda _: next(values))
    with pytest.raises(custody.CustodyError, match="pre-capture candidate differs from precise native census"):
        owner.observe_census({103: "birth-103"}, census=census,
                             fresh_census=lambda: pytest.fail("postcapture absence was accepted"))
    assert owner._uncertain is not None and not owner._unadmitted_absent


@pytest.mark.parametrize("entry", ["register", "observe"])
def test_legacy_entry_cannot_upgrade_unadmitted_tombstone(observed_descendant, monkeypatch, entry):
    owner, _, census = _ordinary_census_fixture(observed_descendant)
    owner.observe_census({100: "root", 103: "birth-103"}, census=census,
                         fresh_census=lambda: {100: census[100]})
    monkeypatch.setattr(custody, "default_process_identity", lambda *_: pytest.fail("tombstone identity query"))
    monkeypatch.setattr(custody, "current_process_audit_token", lambda *_: pytest.fail("tombstone token capture"))
    with pytest.raises(custody.CustodyError, match="cannot gain registration authority"):
        if entry == "register":
            owner.register(103, "birth-103")
        else:
            owner.observe({103: "birth-103"})
    assert owner._uncertain is not None and 103 not in owner._handles
    assert not list(owner.root.glob("designation-descendant_103_*.json"))


def test_fresh_reconciliation_child_is_admitted_and_retained_through_reparenting(observed_descendant):
    owner, _, census = _ordinary_census_fixture(observed_descendant)
    fresh = {100: census[100], 102: {"birth_id": "birth-102", "parent_pid": 100, "pgid": 102}}
    reparented = {100: census[100], 102: {**fresh[102], "parent_pid": 1}}
    samples = iter((fresh, reparented))
    live = owner.observe_census({100: "root", 103: "birth-103"}, census=census,
                               fresh_census=lambda: next(samples))
    assert live == {100: "root", 102: "birth-102"} and 102 in owner._handles
    assert owner._unadmitted_absent[103]["disposition"] == "UNADMITTED_ABSENT"


def test_unadmitted_candidate_never_captures_token(observed_descendant, monkeypatch):
    owner, _, census = _ordinary_census_fixture(observed_descendant)
    monkeypatch.setattr(custody, "current_process_audit_token", lambda *_: pytest.fail("unadmitted token capture"))
    owner.observe_census({100: "root", 103: "birth-103"}, census=census,
                         fresh_census=lambda: {100: census[100]})
    assert 103 not in owner._handles


def _zombie_candidate(observed_descendant, monkeypatch):
    owner, identities, census = _ordinary_census_fixture(observed_descendant)
    native = {"pid": 103, "uid": 501, "parent_pid": 100, "pgid": 103,
              "status": 5, "start_sec": 1791433449, "start_usec": 345678}
    birth = custody._ps_lstart_birth(native["start_sec"])
    census[103] = {"birth_id": birth, "parent_pid": 100, "pgid": 103,
                   "status": 2, "start_sec": native["start_sec"], "start_usec": native["start_usec"]}
    monkeypatch.setattr(custody, "ordinary_candidate_observation", lambda _: {
        "outcome": "ZOMBIE", "native": dict(native),
        "identity": {"pid": 103, "uid": 501, "parent_pid": 100, "birth_id": birth}})
    monkeypatch.setattr(custody, "current_process_audit_token", lambda *_: pytest.fail("terminal observation captured token"))
    return owner, census, native, birth


def test_birth_bound_zombie_is_observation_until_absence_not_reap(observed_descendant, monkeypatch):
    owner, census, native, birth = _zombie_candidate(observed_descendant, monkeypatch)
    present = {100: census[100], 103: {**census[103], "status": 5}}
    live = owner.observe_census({100: "root", 103: birth}, census=census, fresh_census=lambda: present)
    assert live == {100: "root"} and 103 not in owner._handles
    record = owner._unadmitted_absent[103]
    assert record["outcome"] == "ZOMBIE" and record["disposition"] == "UNADMITTED_ZOMBIE"
    assert record["terminal_observation"] == native and not record["absence_observed"]
    assert record["exit_reap"] == record["subtree"] == "UNPROVED"
    with pytest.raises(custody.CustodyError, match="lacks a retained exact"):
        owner.signal(103, birth, signal.SIGTERM)
    absent = {100: census[100]}
    assert owner.observe_census({100: "root"}, census=absent, fresh_census=lambda: absent) == {100: "root"}
    persisted = json.loads((owner.root / "observed-containment.json").read_text())["unadmitted_candidates"][0]
    assert persisted["absence_observed"] and persisted["exit_reap"] == "UNPROVED"
    assert owner._uncertain is None


@pytest.mark.parametrize("change", ["live", "microsecond", "second", "malformed", "unknown-child", "reappearance"])
def test_terminal_candidate_contradictions_remain_sticky(observed_descendant, monkeypatch, change):
    owner, census, _, birth = _zombie_candidate(observed_descendant, monkeypatch)
    present = {100: census[100], 103: {**census[103], "status": 5}}
    owner.observe_census({100: "root", 103: birth}, census=census, fresh_census=lambda: present)
    bad = {pid: dict(row) for pid, row in present.items()}
    if change == "live": bad[103]["status"] = 2
    elif change == "microsecond": bad[103]["start_usec"] += 1
    elif change == "second": bad[103]["start_sec"] += 1
    elif change == "malformed": bad[103].pop("start_usec")
    elif change == "unknown-child": bad[104] = {"birth_id": "child", "parent_pid": 103, "pgid": 104}
    else:
        absent = {100: census[100]}
        owner.observe_census({100: "root"}, census=absent, fresh_census=lambda: absent)
    with pytest.raises(custody.CustodyError):
        owner.observe_census({}, census=bad, fresh_census=lambda: bad)
    assert owner._uncertain is not None and 103 not in owner._handles


def test_initial_terminal_birth_microseconds_must_match_census(observed_descendant, monkeypatch):
    owner, census, _, birth = _zombie_candidate(observed_descendant, monkeypatch)
    census[103]["start_usec"] += 1
    with pytest.raises(custody.CustodyError, match="birth/status"):
        owner.observe_census({100: "root", 103: birth}, census=census, fresh_census=lambda: pytest.fail("contradiction reconciled"))
    assert owner._uncertain is not None and not owner._unadmitted_absent


def test_inner_terminal_observation_never_settles_outer_admission(observed_descendant, monkeypatch, tmp_path):
    outer, actors, identities, tokens = observed_descendant
    birth = custody._ps_lstart_birth(1791433449)
    identities[103] = {**identities[103], "birth_id": birth}
    outer.register(103, birth)
    outer_handle = outer._handles[103]
    outer_record = outer_handle[1][0]._read()
    inner = object.__new__(custody.ProcessCustody)
    inner.root, inner.owner_epoch, inner.actor = tmp_path / "inner", "inner-epoch", actors[101]
    inner._lock, inner._handles, inner._uncertain = threading.RLock(), {}, None
    inner.process = SimpleNamespace(pid=100, poll=lambda: 0)
    owner, census, _, observed_birth = _zombie_candidate((inner, actors, identities, tokens), monkeypatch)
    present = {100: census[100], 103: {**census[103], "status": 5}}
    owner.observe_census({100: "root", 103: observed_birth}, census=census, fresh_census=lambda: present)
    assert outer._handles[103] is outer_handle and outer_handle[1][0]._read() == outer_record
    assert outer._uncertain is None and 103 not in inner._handles
    # No inner evidence is a receipt or transfer of the outer retained obligation.
    assert inner._unadmitted_absent[103]["admitted"] is False
    assert outer_handle[1][1]["generation"] == 1


def test_terminal_candidate_presence_blocks_final_census(observed_descendant, monkeypatch):
    owner, census, _, birth = _zombie_candidate(observed_descendant, monkeypatch)
    present = {100: census[100], 103: {**census[103], "status": 5}}
    owner.observe_census({100: "root", 103: birth}, census=census, fresh_census=lambda: present)
    with pytest.raises(custody.CustodyError, match="terminal candidate remains"):
        owner.validate_final_census({103: {**present[103], "parent_pid": 1},
                                     999: {"birth_id": "other", "parent_pid": 1, "pgid": 999}})
    assert owner._uncertain is not None


@pytest.mark.parametrize("outcome", ["LIVE", "ABSENT", "ZOMBIE", "OBSERVATION_ERROR"])
def test_native_candidate_observation_has_explicit_non_authority_outcome(monkeypatch, outcome):
    row = {"pid": 103, "uid": 501, "parent_pid": 100, "pgid": 103,
           "status": 5 if outcome == "ZOMBIE" else 2, "start_sec": 1791433449, "start_usec": 123}
    def rows(_):
        if outcome == "OBSERVATION_ERROR": raise OSError("native lookup failed")
        return [] if outcome == "ABSENT" else [row]
    monkeypatch.setattr(custody, "_darwin_process_records", rows)
    def precise(_):
        if outcome in {'ABSENT', 'ZOMBIE'}:
            raise custody.DarwinProcessExitedError('synthetic terminal observation')
        return row
    monkeypatch.setattr(custody, '_darwin_process_start', precise)
    observation = custody.ordinary_candidate_observation(103)
    assert observation["outcome"] == outcome
    assert "words" not in observation and "capability" not in observation
    if outcome in {"LIVE", "ZOMBIE"}: assert observation["native"] == row


@pytest.mark.parametrize("phase", ["task_name_for_pid", "task_info_first", "task_info_second", "pid_for_task", "mach_port_deallocate", "primary-and-release"])
def test_mach_capture_diagnostics_preserve_primary_and_release_without_secrets(monkeypatch, phase):
    class Function:
        def __init__(self, call): self.call = call
        def __call__(self, *args): return self.call(*args)
    calls = []
    def task_name(_, pid, out):
        out._obj.value = 88
        return 5 if phase in {"task_name_for_pid", "primary-and-release"} else 0
    def task_info(_, __, token, count):
        calls.append("read")
        current = "task_info_first" if len(calls) == 1 else "task_info_second"
        return 17 if phase == current else 0
    def pid_for_task(_, out):
        out._obj.value = 103
        return 9 if phase == "pid_for_task" else 0
    # A primary token read failure can also be accompanied by a port release failure.
    if phase == "primary-and-release":
        def task_name(_, pid, out): out._obj.value = 88; return 0
        def task_info(*_): return 17
    library = SimpleNamespace(task_name_for_pid=Function(task_name), task_info=Function(task_info),
                              pid_for_task=Function(pid_for_task),
                              mach_port_deallocate=Function(lambda *_: 3 if phase in {"mach_port_deallocate", "primary-and-release"} else 0))
    monkeypatch.setattr(custody.sys, "platform", "darwin")
    monkeypatch.setattr(custody.ctypes, "CDLL", lambda *_, **__: library)
    monkeypatch.setattr(custody.ctypes.c_uint32, "in_dll", lambda *_: SimpleNamespace(value=44))
    monkeypatch.setattr(custody, "audit_token_details", lambda _: {"pid": 103, "pidversion": 1, "uid": 501, "sha256": "private"})
    with pytest.raises(custody.CustodyError) as caught:
        custody.current_process_audit_token(103)
    message = str(caught.value)
    assert "candidate_pid=103" in message and "kern_return=" in message
    assert "private" not in message and "words" not in message
    if phase == "primary-and-release":
        assert "substage=task_info_first" in message
        assert "substage=mach_port_deallocate" in str(caught.value.port_release_error)
        assert caught.value.capture_failure["port_release_succeeded"] is False
    else: assert f"substage={phase}" in message
    if phase in {"task_info_first", "task_info_second", "pid_for_task"}:
        assert caught.value.capture_failure["port_release_succeeded"] is True
    if phase in {"task_name_for_pid", "mach_port_deallocate"}:
        assert "port_release_succeeded" not in caught.value.capture_failure


@pytest.mark.parametrize("failure", ["generic-token-error", "partial-token", "post-token-absence", "designation-failure"])
def test_ordinary_live_candidate_never_reconciles_token_or_partial_binding_failure(observed_descendant, monkeypatch, failure):
    owner, _, identities, tokens = observed_descendant
    identity = dict(identities[103])
    owner, _, census = _ordinary_census_fixture(observed_descendant)
    identities[103] = identity
    if failure == "generic-token-error":
        monkeypatch.setattr(custody, "current_process_audit_token", lambda _: (_ for _ in ()).throw(custody.CustodyError("kernel access denied")))
    elif failure == "partial-token":
        monkeypatch.setattr(custody, "current_process_audit_token", lambda _: {"pid": 103})
    elif failure == "post-token-absence":
        samples = iter((identity, None))
        monkeypatch.setattr(custody, "default_process_identity", lambda _: next(samples))
    else:
        monkeypatch.setattr(custody.RoleCustodyAuthority, "bind_target", lambda *_, **__: (_ for _ in ()).throw(custody.CustodyError("partial designation failure")))
    with pytest.raises(custody.CustodyError):
        owner.observe_census({100: "root", 103: "birth-103"}, census=census,
                             fresh_census=lambda: pytest.fail("captured/token failure was reconciled"))
    assert owner._uncertain is not None and not owner._unadmitted_absent and 103 not in owner._handles


@pytest.mark.parametrize("role", ["engine", "engine_listener", "runtime_owner"])
def test_anchored_zombie_uses_strict_identity_api_not_candidate_observation(observed_descendant, monkeypatch, role):
    owner, _, census = _ordinary_census_fixture(observed_descendant)
    owner._launch_broker.role = role
    monkeypatch.setattr(custody, "ordinary_candidate_observation", lambda *_: pytest.fail("anchored role gained terminal disposition"))
    monkeypatch.setattr(custody, "default_process_identity", lambda _: (_ for _ in ()).throw(custody.DarwinProcessExitedError("zombie")))
    with pytest.raises(custody.CustodyError, match="zombie"):
        owner.observe_census({103: "birth-103"}, census=census, fresh_census=lambda: pytest.fail("anchored zombie reconciled"))
    assert owner._uncertain is not None and not owner._unadmitted_absent


def test_new_candidate_cannot_use_zombie_admitted_parent_ancestry(observed_descendant, monkeypatch):
    owner, census, _, birth = _zombie_candidate(observed_descendant, monkeypatch)
    census[100]["status"] = 5
    with pytest.raises(custody.CustodyError, match="zombie admitted-parent ancestry"):
        owner.observe_census({100: "root", 103: birth}, census=census,
                             fresh_census=lambda: pytest.fail("zombie parent ancestry reconciled"))
    assert owner._uncertain is not None and 103 not in owner._handles


def _capture_terminal_fixture(observed_descendant, monkeypatch, *, change=None):
    owner, census, native, birth = _zombie_candidate(observed_descendant, monkeypatch)
    census[103].update(pid=103, uid=501)
    pre = {**native, "status": 2}
    if change == "parent": pre["parent_pid"] = 999
    post = {"outcome": "ZOMBIE", "native": dict(native),
            "identity": {"pid": 103, "uid": 501, "parent_pid": 100, "birth_id": birth}}
    if change == "live": post["outcome"], post["native"]["status"] = "LIVE", 2
    elif change == "absent": post = {"outcome": "ABSENT"}
    elif change == "error": post = {"outcome": "OBSERVATION_ERROR", "error": OSError("native observation refused")}
    elif change == "microsecond": post["native"]["start_usec"] += 1
    elif change == "uid": post["native"]["uid"] += 1
    elif change == "pid": post["native"]["pid"] = 104
    elif change == "census-microsecond": census[103]["start_usec"] += 1
    elif change == "missing-uid": census[103].pop("uid")
    observations = iter(({"outcome": "LIVE", "native": pre,
                          "identity": {"pid": 103, "uid": 501, "parent_pid": 100, "birth_id": birth}}, post))
    monkeypatch.setattr(custody, "ordinary_candidate_observation", lambda _: next(observations))
    def capture(_):
        raise custody.NativeTokenCaptureError("initial Mach lookup failed", substage=(
            "task_info_first" if change == "later-phase" else "task_name_for_pid"),
            kern_return=5, port_acquired=change in {"partial-port", "later-phase"})
    monkeypatch.setattr(custody, "current_process_audit_token", capture)
    if change == "partial-designation":
        owner.root.mkdir(parents=True)
        role = "descendant_103_" + hashlib.sha256(birth.encode()).hexdigest()[:12]
        (owner.root / ("designation-" + role + ".json")).write_text("{}")
    return owner, census, native, birth


def test_initial_capture_zombie_uses_original_production_native_census(observed_descendant, monkeypatch):
    from astrid.core.execution import process_group

    owner, _, native, birth = _capture_terminal_fixture(observed_descendant, monkeypatch)
    root_native = {"pid": 100, "uid": 501, "parent_pid": 1, "pgid": 100,
                   "status": 2, "start_sec": native["start_sec"] - 1, "start_usec": 123456}
    root_birth = custody._ps_lstart_birth(root_native["start_sec"])
    root_broker = owner._handles[100][1]
    owner._handles[100] = (root_birth, root_broker)
    records = [root_native, {**native, "status": 2}]
    monkeypatch.setattr(process_group, "_darwin_process_records", lambda: records)
    original = process_group._census_evidence(process_group._process_snapshot())
    # No fields are patched into the serialized production census.
    assert original[103]["pid"] == native["pid"]
    assert original[103]["uid"] == native["uid"]
    assert original[103]["start_sec"] == native["start_sec"]
    assert original[103]["start_usec"] == native["start_usec"]
    records[1] = dict(native)
    assert owner.observe_census(
        {pid: row["birth_id"] for pid, row in original.items()}, census=original,
        fresh_census=lambda: process_group._census_evidence(process_group._process_snapshot()),
    ) == {100: root_birth}
    assert set(owner._handles) == {100} and owner._uncertain is None
    assert owner._unadmitted_absent[103]["capability"] == "NONE"
    evidence = json.loads((owner.root / "observed-containment.json").read_text())
    assert evidence["capture_failures"][0]["census"]["103"] == original[103]
    assert not list(owner.root.glob("designation-descendant_103_*.json"))
    with pytest.raises(custody.CustodyError, match="lacks a retained exact"):
        owner.signal(103, birth, signal.SIGTERM)


def test_initial_native_capture_failure_same_precise_zombie_retains_zero_authority(observed_descendant, monkeypatch):
    owner, census, native, birth = _capture_terminal_fixture(observed_descendant, monkeypatch)
    root_handle = owner._handles[100]
    present = {100: census[100], 103: {**census[103], "status": 5}}
    assert owner.observe_census({100: "root", 103: birth}, census=census,
                                fresh_census=lambda: present) == {100: "root"}
    assert owner._handles == {100: root_handle} and owner._uncertain is None
    record = owner._unadmitted_absent[103]
    assert record["stage"] == "token-capture" and record["outcome"] == "ZOMBIE"
    assert record["capability"] == "NONE" and record["exit_reap"] == record["subtree"] == "UNPROVED"
    evidence = json.loads((owner.root / "observed-containment.json").read_text())
    capture = evidence["capture_failures"][0]
    assert capture["capture_failure"]["port_acquired"] is False
    assert capture["observations"]["pre"]["native"]["status"] == 2
    assert capture["observations"]["post"]["native"] == native
    assert capture["census"]["103"] == census[103]
    assert "words" not in json.dumps(evidence)
    with pytest.raises(custody.CustodyError, match="lacks a retained exact"):
        owner.signal(103, birth, signal.SIGTERM)
    with pytest.raises(custody.CustodyError, match="terminal candidate remains"):
        owner.validate_final_census(present)


@pytest.mark.parametrize("change", ["live", "error", "microsecond", "uid", "pid",
                                     "parent", "census-microsecond", "missing-uid", "partial-port",
                                     "later-phase", "partial-designation"])
def test_postcapture_nonqualifying_observations_stay_sticky(observed_descendant, monkeypatch, change):
    owner, census, _, birth = _capture_terminal_fixture(observed_descendant, monkeypatch, change=change)
    with pytest.raises(custody.CustodyError):
        owner.observe_census({100: "root", 103: birth}, census=census,
                             fresh_census=lambda: pytest.fail("ineligible capture failure reconciled"))
    assert owner._uncertain is not None and 103 not in owner._handles and not owner._unadmitted_absent
    evidence = json.loads((owner.root / "observed-containment.json").read_text())
    assert evidence["capture_failures"][0]["observations"]["pre"]["outcome"] == "LIVE"
    with pytest.raises(custody.CustodyError, match="unresolved"):
        owner.observe_census({}, census={}, fresh_census=lambda: {})


def test_capture_failure_post_observation_precedes_persistence_and_persistence_cannot_clear_failure(observed_descendant, monkeypatch):
    owner, census, _, birth = _capture_terminal_fixture(observed_descendant, monkeypatch)
    def refuse(path, value):
        assert value["capture_failures"][0]["observations"]["post"]["outcome"] == "ZOMBIE"
        raise OSError("evidence write refused")
    monkeypatch.setattr(custody, "_atomic_owner_json", refuse)
    with pytest.raises(custody.CustodyError, match="diagnostic persistence failed") as caught:
        owner.observe_census({100: "root", 103: birth}, census=census,
                             fresh_census=lambda: pytest.fail("failed persistence reconciled"))
    assert isinstance(caught.value.__cause__.__cause__, custody.NativeTokenCaptureError)
    assert owner._uncertain is not None and 103 not in owner._handles and not owner._unadmitted_absent


def test_capture_terminal_candidate_cannot_hide_new_descendant(observed_descendant, monkeypatch):
    owner, census, _, birth = _capture_terminal_fixture(observed_descendant, monkeypatch)
    census[104] = {"birth_id": "child", "parent_pid": 103, "pgid": 104}
    with pytest.raises(custody.CustodyError, match="requires unadmitted absent ancestry"):
        owner.observe_census({100: "root", 103: birth}, census=census,
                             fresh_census=lambda: pytest.fail("unproved subtree accepted"))
    assert owner._uncertain is not None and 103 not in owner._handles


def test_capture_terminal_observation_leaves_other_admitted_obligation_unchanged(observed_descendant, monkeypatch):
    owner, census, _, birth = _capture_terminal_fixture(observed_descendant, monkeypatch)
    admitted_handle = object()
    owner._handles[102] = ("birth-102", admitted_handle)
    census[102] = {"birth_id": "birth-102", "parent_pid": 100, "pgid": 102}
    present = {100: census[100], 102: census[102], 103: {**census[103], "status": 5}}
    live = owner.observe_census({100: "root", 102: "birth-102", 103: birth}, census=census,
                                fresh_census=lambda: present)
    assert live == {100: "root", 102: "birth-102"}
    assert owner._handles[102] == ("birth-102", admitted_handle)
    assert 102 not in owner._unadmitted_absent and 103 not in owner._handles


def test_capture_terminal_observation_rechecks_authenticated_actor(observed_descendant, monkeypatch):
    owner, census, _, birth = _capture_terminal_fixture(observed_descendant, monkeypatch)
    actor = owner.actor.verify()
    calls = []
    def changed_actor():
        calls.append(True)
        return actor if len(calls) == 1 else {**actor, "uid": 999}
    monkeypatch.setattr(owner.actor, "verify", changed_actor)
    with pytest.raises(custody.CustodyError, match="observer or retained root changed"):
        owner.observe_census({100: "root", 103: birth}, census=census, fresh_census=lambda: {})
    assert owner._uncertain is not None and not owner._unadmitted_absent


def test_capture_terminal_rule_never_clears_prior_uncertainty(observed_descendant, monkeypatch):
    owner, census, _, birth = _capture_terminal_fixture(observed_descendant, monkeypatch)
    prior = custody.CustodyError("previous admitted obligation unresolved")
    owner._uncertain = prior
    with pytest.raises(custody.CustodyError, match="unresolved"):
        owner.observe_census({100: "root", 103: birth}, census=census, fresh_census=lambda: {})
    assert owner._uncertain is prior and not owner._unadmitted_absent


def test_capture_terminal_candidate_serializes_concurrent_observers(observed_descendant, monkeypatch):
    owner, census, _, birth = _capture_terminal_fixture(observed_descendant, monkeypatch)
    present = {100: census[100], 103: {**census[103], "status": 5}}
    entered, proceed = threading.Event(), threading.Event()
    original = custody._atomic_owner_json
    def gated_write(path, value):
        if value.get("capture_failures") and not entered.is_set():
            entered.set()
            assert proceed.wait(3)
        return original(path, value)
    monkeypatch.setattr(custody, "_atomic_owner_json", gated_write)
    errors = []
    def observe(snapshot):
        try:
            owner.observe_census({100: "root", 103: birth}, census=snapshot, fresh_census=lambda: present)
        except BaseException as error: errors.append(error)
    first = threading.Thread(target=observe, args=(census,))
    second = threading.Thread(target=observe, args=(present,))
    first.start(); assert entered.wait(3); second.start(); proceed.set()
    first.join(3); second.join(3)
    assert not first.is_alive() and not second.is_alive() and not errors
    assert 103 not in owner._handles and owner._uncertain is None
    assert len(owner._capture_failure_observations) == 1


def _procargs_packet(argv, environment=b"SECRET_TOKEN=never-record-this\0"):
    return (len(argv).to_bytes(4, sys.byteorder, signed=True) + os.fsencode(argv[0] or "/observed/exe") + b"\0\0"
            + b"\0".join(os.fsencode(arg) for arg in argv) + b"\0" + environment)


def test_command_diagnostic_decodes_argc_excludes_environment_and_redacts_secret_flags():
    argv = ["/bin/bash", "-c", "exec node script.mjs", "--api-key", "secret-value", "--token", "other-secret", ""]
    evidence = custody._diagnostic_argv(_procargs_packet(argv))
    assert evidence["argc"] == len(argv)
    assert evidence["argv"] == ["/bin/bash", "-c", "<opaque:" + custody._digest_bytes(b"exec node script.mjs") + ">", "--api-key", "<redacted>", "--token", "<redacted>", ""]
    assert evidence["argv_sha256"] == custody._digest_bytes(custody._canonical(argv))
    rendered = json.dumps(evidence)
    assert "secret-value" not in rendered and "other-secret" not in rendered and "never-record-this" not in rendered
    assert evidence["environment"] == "EXCLUDED"


@pytest.mark.parametrize("packet", [b"", b"x" * (65536 + 1), (257).to_bytes(4, sys.byteorder) + b"x\0", _procargs_packet(["a", "b"])[:10]])
def test_command_diagnostic_malformed_or_overlimit_args_are_explicit(packet):
    with pytest.raises(custody.CustodyError):
        custody._diagnostic_argv(packet)


@pytest.mark.parametrize("change", [None, "exec-metadata", "microsecond", "parent", "absent", "refused"])
def test_command_diagnostic_brackets_actual_parent_and_image_without_authority(change):
    before = {"pid": 103, "uid": 501, "parent_pid": 100, "status": 2, "start_sec": 1000, "start_usec": 13}
    after = dict(before)
    if change == "microsecond": after["start_usec"] += 1
    if change == "parent": after["parent_pid"] += 1
    records = iter(([before], [] if change == "absent" else [after]))
    first = {"executable": "/bin/bash", "argv": ["/bin/bash", "-c", "exec node script.mjs"], "argv_sha256": "a"}
    second = {**first, "executable": "/node"} if change == "exec-metadata" else first
    commands = iter((first, second))
    def reader(_):
        if change == "refused": raise OSError("public metadata denied")
        return next(commands)
    evidence = custody._command_observation(103, native_reader=lambda _: next(records), command_reader=reader)
    assert evidence["authority"] == "NONE"
    if change == "refused":
        assert evidence["outcome"] == "OBSERVATION_ERROR" and evidence["error_type"] == "OSError"
    else:
        assert evidence["outcome"] == ("BRACKETED_METADATA" if change is None else "CHANGED_DURING_OBSERVATION")
        assert evidence["native_before"]["parent_pid"] == 100
        assert evidence["metadata_first"] == first
        assert "UNPROVED" in evidence["exec_transition"]


def test_opted_in_command_diagnostic_keeps_live_capture_failure_and_zero_authority(observed_descendant, monkeypatch):
    owner, census, _, birth = _capture_terminal_fixture(observed_descendant, monkeypatch, change="live")
    owner.command_diagnostics = True
    owner._command_observations = []
    seen = []
    def command(pid):
        seen.append(pid)
        return {"outcome": "OBSERVATION_ERROR", "authority": "NONE"}
    monkeypatch.setattr(custody, "_command_observation", command)
    with pytest.raises(custody.CustodyError, match="candidate is LIVE"):
        owner.observe_census({100: "root", 103: birth}, census=census, fresh_census=lambda: pytest.fail("LIVE failure reconciled"))
    assert seen == [103, 100, 103, 100] and set(owner._handles) == {100} and owner._uncertain is not None
    evidence = json.loads((owner.root / "observed-containment.json").read_text())
    assert evidence["command_observations"][0]["stage"] == "before_initial_token_capture"
    assert evidence["capture_failures"][0]["capture_failure"]["substage"] == "task_name_for_pid"
    assert evidence["command_diagnostic_authority"] == "NONE"


def test_command_diagnostic_is_disabled_for_existing_owners(observed_descendant, monkeypatch):
    owner, census, _, birth = _capture_terminal_fixture(observed_descendant, monkeypatch, change="live")
    monkeypatch.setattr(custody, "_command_observation", lambda _: pytest.fail("default owner queried command metadata"))
    with pytest.raises(custody.CustodyError, match="candidate is LIVE"):
        owner.observe_census({100: "root", 103: birth}, census=census, fresh_census=lambda: pytest.fail("LIVE failure reconciled"))
    assert "command_observations" not in json.loads((owner.root / "observed-containment.json").read_text())


@pytest.mark.parametrize("argv", [["", "argument"], ["", "/observed/exe"], ["/bin/bash", "TOKEN=secret"]])
def test_command_diagnostic_rejects_ambiguous_leading_argument_or_environment_boundary(argv):
    with pytest.raises(custody.CustodyError, match="boundary is ambiguous"):
        custody._diagnostic_argv(_procargs_packet(argv))


def test_command_diagnostic_inflated_argc_cannot_persist_environment():
    packet = _procargs_packet(["/bin/bash", "--version"])
    inflated = (3).to_bytes(4, sys.byteorder) + packet[4:]
    with pytest.raises(custody.CustodyError, match="boundary is ambiguous"):
        custody._diagnostic_argv(inflated)


@pytest.mark.parametrize("payload", ["TOKEN=secret exec node worker.mjs", '--props={"url":"https://host/?token=secret"}', "console.log('secret')"])
def test_command_diagnostic_opaque_payload_is_digest_only(payload):
    if "=" in payload:
        with pytest.raises(custody.CustodyError, match="boundary is ambiguous"):
            custody._diagnostic_argv(_procargs_packet(["/bin/bash", "-c", payload]))
        return
    evidence = custody._diagnostic_argv(_procargs_packet(["/bin/bash", "-c", payload]))
    assert payload not in json.dumps(evidence) and "secret" not in json.dumps(evidence)
    assert evidence["argv"][2] == "<opaque:" + custody._digest_bytes(os.fsencode(payload)) + ">"


@pytest.mark.parametrize("flag", ["-lc", "-ic", "-xc", "-cl"])
def test_shell_combined_options_cannot_expose_path_shaped_command(flag):
    evidence = custody._diagnostic_argv(_procargs_packet(["/bin/bash", flag, "/bin/echo secret"]))
    assert "secret" not in json.dumps(evidence)
    assert evidence["argv"][2].startswith("<opaque:")


@pytest.mark.parametrize("entry", [b"AUTH-TOKEN=secret\0", b"=secret\0", b"--props=secret\0"])
def test_ambiguous_environment_entries_are_rejected_before_hashing(entry, monkeypatch):
    packet = _procargs_packet(["/bin/bash", "--version"], environment=entry)
    inflated = (3).to_bytes(4, sys.byteorder) + packet[4:]
    monkeypatch.setattr(custody, "_digest_bytes", lambda _: pytest.fail("ambiguous environment was hashed"))
    with pytest.raises(custody.CustodyError, match="boundary is ambiguous"):
        custody._diagnostic_argv(inflated)


@pytest.mark.parametrize('environment,explicit,enabled', [
    (None, None, False), ('1', None, True), ('0', None, False),
    ('true', None, False), (' 1', None, False), ('1', False, False), ('0', True, True),
])
def test_command_diagnostics_requires_exact_per_run_opt_in(tmp_path, monkeypatch, environment, explicit, enabled):
    monkeypatch.setattr(custody.AuthenticatedCleanupActor, 'current', lambda: object())
    if environment is None:
        monkeypatch.delenv('ASTRID_CUSTODY_COMMAND_DIAGNOSTICS', raising=False)
    else:
        monkeypatch.setenv('ASTRID_CUSTODY_COMMAND_DIAGNOSTICS', environment)
    owner = custody.ProcessCustody(scope_root=tmp_path, command_diagnostics=explicit)
    assert owner.command_diagnostics is enabled
    assert not owner._handles and owner._uncertain is None


def test_opted_in_success_records_metadata_without_granting_extra_authority(observed_descendant, monkeypatch):
    owner, _, _, _ = observed_descendant
    owner.command_diagnostics = True
    owner._command_observations = []
    owner._unadmitted_absent = {}
    monkeypatch.setattr(custody, '_command_observation', lambda pid: {'outcome': 'OBSERVATION_ERROR', 'authority': 'NONE'})
    owner.observe({103: 'birth-103'})
    assert set(owner._handles) == {103} and owner._uncertain is None
    evidence = json.loads((owner.root / 'observed-containment.json').read_text())
    assert evidence['command_observations'][0]['candidate_pid'] == 103
    assert evidence['command_diagnostic_authority'] == 'NONE'
    assert evidence['complete_descendant_history'] == 'UNPROVED'
    assert evidence['command_diagnostic_persist_error'] is None


def test_optional_diagnostic_write_failure_preserves_acquired_custody(observed_descendant, monkeypatch):
    owner, _, _, _ = observed_descendant
    owner.command_diagnostics = True
    owner._command_observations = []
    monkeypatch.setattr(custody, '_command_observation', lambda pid: {'outcome': 'OBSERVATION_ERROR', 'authority': 'NONE'})
    monkeypatch.setattr(owner, '_write_observed_containment', lambda: (_ for _ in ()).throw(OSError('secret diagnostic write message')))
    owner.observe({103: 'birth-103'})
    assert set(owner._handles) == {103} and owner._uncertain is None
    assert owner._command_diagnostic_persist_error == 'OSError'
    assert 'secret diagnostic write message' not in json.dumps(owner._command_observations)


def _capture_absence_fixture(observed_descendant, monkeypatch):
    owner, census, native, birth = _capture_terminal_fixture(observed_descendant, monkeypatch, change="absent")
    root = {"pid": 100, "uid": 501, "parent_pid": 1, "pgid": 100, "status": 2,
            "start_sec": native["start_sec"] - 1, "start_usec": 123456}
    root_birth = custody._ps_lstart_birth(root["start_sec"])
    root["birth_id"] = root_birth
    owner._handles[100] = (root_birth, owner._handles[100][1])
    census[100] = root
    return owner, census, native, birth


def test_no_authority_post_absence_uses_production_census_and_retains_pending_evidence(observed_descendant, monkeypatch):
    from astrid.core.execution import process_group
    owner, census, _, birth = _capture_absence_fixture(observed_descendant, monkeypatch)
    records = [{key: value for key, value in row.items() if key != "birth_id"} for row in census.values()]
    monkeypatch.setattr(process_group, "_darwin_process_records", lambda: records)
    original = process_group._census_evidence(process_group._process_snapshot())
    def fresh():
        evidence = json.loads((owner.root / "observed-containment.json").read_text())
        candidate = evidence["unadmitted_candidates"][0]
        assert candidate["disposition"] == "PENDING_RECONCILIATION"
        assert candidate["outcome"] == "ABSENT" and candidate["terminal_observation"] is None
        records.pop()
        return process_group._census_evidence(process_group._process_snapshot())
    result = owner.observe_census({pid: row["birth_id"] for pid, row in original.items()},
                                  census=original, fresh_census=fresh)
    assert result == {100: census[100]["birth_id"]} and set(owner._handles) == {100}
    record = owner._unadmitted_absent[103]
    assert record["stage"] == "token-capture-no-authority-absence"
    assert record["disposition"] == "UNADMITTED_ABSENT" and record["absence_observed"]
    assert record["capability"] == "NONE" and not record["admitted"]
    assert record["exit_reap"] == record["subtree"] == "UNPROVED" and record["terminal_observation"] is None
    assert not list(owner.root.glob("designation-descendant_103_*.json"))
    with pytest.raises(custody.CustodyError, match="lacks a retained exact"):
        owner.signal(103, birth, signal.SIGTERM)
    owner.validate_final_census({200: {"birth_id": "other", "parent_pid": 1}})


@pytest.mark.parametrize("change", ["actor", "root-handle", "missing-root", "root-zombie", "root-birth",
                                     "root-microsecond", "root-uid", "ancestry-parent", "ancestry-missing",
                                     "candidate-reappeared", "unknown-child", "empty"])
def test_post_absence_revalidates_fresh_actor_root_and_ancestry(observed_descendant, monkeypatch, change):
    owner, census, native, birth = _capture_absence_fixture(observed_descendant, monkeypatch)
    # Original candidate ancestry is live, precise, and reaches the retained root.
    ancestor = {"pid": 102, "uid": 501, "parent_pid": 100, "pgid": 100, "status": 2,
                "start_sec": native["start_sec"] - 1, "start_usec": 234567}
    ancestor["birth_id"] = custody._ps_lstart_birth(ancestor["start_sec"])
    owner._handles[102] = (ancestor["birth_id"], object())
    census[102] = ancestor
    census[103]["parent_pid"] = 102
    pre = {**native, "parent_pid": 102, "status": 2}
    samples = iter(({"outcome": "LIVE", "native": pre,
                     "identity": {"pid": 103, "uid": 501, "parent_pid": 102, "birth_id": birth}},
                    {"outcome": "ABSENT"}))
    monkeypatch.setattr(custody, "ordinary_candidate_observation", lambda _: next(samples))
    actor = owner.actor.verify()
    fresh_rows = {pid: dict(row) for pid, row in census.items() if pid != 103}
    def fresh():
        if change == "actor": monkeypatch.setattr(owner.actor, "verify", lambda: {**actor, "uid": 999})
        elif change == "root-handle": owner._handles[100] = (census[100]["birth_id"], object())
        elif change == "missing-root": fresh_rows.pop(100)
        elif change == "root-zombie": fresh_rows[100]["status"] = 5
        elif change == "root-birth": fresh_rows[100]["birth_id"] = "reused"
        elif change == "root-microsecond": fresh_rows[100]["start_usec"] += 1
        elif change == "root-uid": fresh_rows[100]["uid"] = 999
        elif change == "ancestry-parent": fresh_rows[102]["parent_pid"] = 1
        elif change == "ancestry-missing": fresh_rows.pop(102)
        elif change == "candidate-reappeared": fresh_rows[103] = census[103]
        elif change == "unknown-child": fresh_rows[104] = {"birth_id": "unknown", "parent_pid": 103}
        elif change == "empty": return {}
        return fresh_rows
    with pytest.raises(custody.CustodyError):
        owner.observe_census({100: census[100]["birth_id"], 102: ancestor["birth_id"], 103: birth},
                            census=census, fresh_census=fresh)
    assert owner._uncertain is not None and 103 not in owner._handles
    assert owner._unadmitted_absent[103]["disposition"] == "PENDING_RECONCILIATION"
    assert owner._unadmitted_absent[103]["capability"] == "NONE"


@pytest.mark.parametrize("change", ["pre-microsecond", "pre-parent", "census-uid", "partial-port", "partial-designation", "persist"])
def test_post_absence_rejects_precise_precondition_and_acquisition_errors(observed_descendant, monkeypatch, change):
    owner, census, native, birth = _capture_absence_fixture(observed_descendant, monkeypatch)
    if change.startswith("pre-"):
        pre = {**native, "status": 2}
        pre["start_usec" if change == "pre-microsecond" else "parent_pid"] += 1
        observations = iter(({"outcome": "LIVE", "native": pre,
                             "identity": {"pid": 103, "uid": 501, "parent_pid": pre["parent_pid"], "birth_id": birth}},
                            {"outcome": "ABSENT"}))
        monkeypatch.setattr(custody, "ordinary_candidate_observation", lambda _: next(observations))
    elif change == "census-uid": census[103]["uid"] = 999
    elif change == "partial-port":
        def capture(_):
            raise custody.NativeTokenCaptureError("failed", substage="task_name_for_pid", kern_return=5, port_acquired=True)
        monkeypatch.setattr(custody, "current_process_audit_token", capture)
    elif change == "partial-designation":
        owner.root.mkdir(parents=True)
        role = "descendant_103_" + hashlib.sha256(birth.encode()).hexdigest()[:12]
        (owner.root / ("designation-" + role + ".json")).write_text("{}")
    elif change == "persist":
        monkeypatch.setattr(custody, "_atomic_owner_json", lambda *_: (_ for _ in ()).throw(OSError("no evidence")))
    with pytest.raises((custody.CustodyError, OSError)):
        owner.observe_census({100: census[100]["birth_id"], 103: birth}, census=census,
                            fresh_census=lambda: pytest.fail("ineligible absence reconciled"))
    assert owner._uncertain is not None and 103 not in owner._handles


def test_post_absence_preserves_reparented_admitted_child_and_outer_obligation(observed_descendant, monkeypatch, tmp_path):
    outer, actors, identities, tokens = observed_descendant
    birth = custody._ps_lstart_birth(1791433449)
    identities[103] = {**identities[103], "birth_id": birth}
    outer.register(103, birth)
    outer_handle = outer._handles[103]
    outer_record = outer_handle[1][0]._read()
    inner = object.__new__(custody.ProcessCustody)
    inner.root, inner.owner_epoch, inner.actor = tmp_path / "inner", "inner-epoch", actors[101]
    inner._lock, inner._handles, inner._uncertain = threading.RLock(), {}, None
    inner.process = SimpleNamespace(pid=100, poll=lambda: 0)
    owner, census, native, birth = _capture_absence_fixture((inner, actors, identities, tokens), monkeypatch)
    other_handle = object()
    owner._handles[102] = ("other", other_handle)
    census[102] = {"birth_id": "other", "parent_pid": 100}
    fresh = {100: census[100], 102: {"birth_id": "other", "parent_pid": 1}}
    owner.observe_census({100: census[100]["birth_id"], 103: birth}, census=census, fresh_census=lambda: fresh)
    assert owner._handles[102] == ("other", other_handle)
    # A separately admitted outer designation is neither consumed nor settled.
    assert outer._handles[103] is outer_handle and outer_handle[1][0]._read() == outer_record
    assert not getattr(outer, "_unadmitted_absent", {}) and outer._uncertain is None
    for bad in ({**fresh, 103: census[103]}, {**fresh, 104: {"birth_id": "child", "parent_pid": 103}}):
        with pytest.raises(custody.CustodyError): owner.validate_final_census(bad)
    assert outer._handles[103] is outer_handle and outer_handle[1][0]._read() == outer_record


def test_reference_verification_uses_real_independent_authority_without_writes(graph, monkeypatch):
    authority, actors, identities, tokens = graph
    reference = authority.reference()
    before = authority.path.read_bytes()
    consumer = custody.RoleCustodyAuthority(authority.root, authority.role)
    monkeypatch.setattr(custody, "_atomic_owner_json", lambda *_: pytest.fail("read-only verification wrote a designation"))
    monkeypatch.setattr(custody, "signal_audit_token", lambda *_: pytest.fail("read-only verification signalled"))
    assert consumer.verify_reference(reference, expected_actor=actors[101].verify(), owner_epoch="runtime-A",
                                    identity_provider=identities.get, token_provider=tokens.get) == reference
    assert consumer._retained_record is None and consumer._live_start_anchor is None
    assert authority.path.read_bytes() == before


@pytest.mark.parametrize("mismatch", ["reference", "actor", "epoch", "actor_birth", "target_birth",
                                     "actor_token", "target_token", "actor_missing", "target_missing"])
def test_reference_verification_rejects_changed_binding_and_kernel_identity(graph, monkeypatch, mismatch):
    authority, actors, identities, tokens = graph
    reference = authority.reference()
    actor = actors[101].verify()
    epoch = "runtime-A"
    observed = {pid: dict(row) for pid, row in identities.items()}
    captured = {pid: dict(row) for pid, row in tokens.items()}
    if mismatch == "reference": reference["generation"] += 1
    elif mismatch == "actor": actor = actors[102].verify()
    elif mismatch == "epoch": epoch = "runtime-B"
    elif mismatch.endswith("_birth"):
        observed[101 if mismatch.startswith("actor") else 103]["birth_id"] = "changed-birth"
    elif mismatch.endswith("_token"):
        captured[101 if mismatch.startswith("actor") else 103]["pidversion"] += 1
    else: observed.pop(101 if mismatch.startswith("actor") else 103)
    before = authority.path.read_bytes()
    consumer = custody.RoleCustodyAuthority(authority.root, authority.role)
    monkeypatch.setattr(custody, "_atomic_owner_json", lambda *_: pytest.fail("failed verification wrote"))
    monkeypatch.setattr(custody, "signal_audit_token", lambda *_: pytest.fail("failed verification signalled"))
    with pytest.raises(custody.CustodyError):
        consumer.verify_reference(reference, expected_actor=actor, owner_epoch=epoch,
                                  identity_provider=observed.get, token_provider=captured.get)
    assert authority.path.read_bytes() == before
    assert consumer._retained_record is None and consumer._live_start_anchor is None


def test_reference_verification_rejects_pending_corrupt_and_lost_retained_history(tmp_path, graph):
    authority, actors, identities, tokens = graph
    pending = custody.RoleCustodyAuthority(tmp_path / "pending-verification", "host")
    pending.designate_pending(actor=actors[101], identity=identities[103], token=tokens[103], owner_epoch="runtime-A")
    with pytest.raises(custody.CustodyError, match="designation differs"):
        pending.verify_reference(pending.reference(), expected_actor=actors[101].verify(), owner_epoch="runtime-A",
                                 identity_provider=identities.get, token_provider=tokens.get)
    reference = authority.reference()
    consumer = custody.RoleCustodyAuthority(authority.root, authority.role)
    authority.path.write_bytes(b"corrupt")
    with pytest.raises(custody.CustodyError):
        consumer.verify_reference(reference, expected_actor=actors[101].verify(), owner_epoch="runtime-A",
                                  identity_provider=identities.get, token_provider=tokens.get)
    assert consumer._ledger_broken and authority.path.read_bytes() == b"corrupt"
    with pytest.raises(custody.CustodyError, match="history is unresolved"):
        consumer.verify_reference(reference, expected_actor=actors[101].verify(), owner_epoch="runtime-A",
                                  identity_provider=identities.get, token_provider=tokens.get)


def test_reference_verification_does_not_ignore_retained_designation_drift(graph):
    authority, actors, identities, tokens = graph
    reference = authority.reference()
    record = authority._read()
    record["owner_epoch"] = "runtime-B"
    authority._write(record)  # Valid disk digest does not replace retained history.
    with pytest.raises(custody.CustodyError, match="retained admission history"):
        authority.verify_reference(reference, expected_actor=actors[101].verify(), owner_epoch="runtime-B",
                                   identity_provider=identities.get, token_provider=tokens.get)
    assert authority._ledger_broken



def _successful_capture_terminal_fixture(observed_descendant, monkeypatch, outcome):
    owner, census, native, birth = _capture_absence_fixture(observed_descendant, monkeypatch)
    pre = {**native, "status": 2}
    identity = {"pid": 103, "uid": 501, "parent_pid": 100, "birth_id": birth}
    post = ({"outcome": "ABSENT"} if outcome == "ABSENT" else
            {"outcome": "ZOMBIE", "native": dict(native), "identity": dict(identity)})
    samples = iter(({"outcome": "LIVE", "native": pre, "identity": identity}, post))
    calls = []
    def observe(pid):
        assert pid == 103
        calls.append("observation")
        return next(samples)
    def capture(pid):
        assert pid == 103
        calls.append("capture")
        return {"pid": 103, "uid": 501, "pidversion": 7, "sha256": "sha256:" + "f" * 64, "words": [0] * 8}
    monkeypatch.setattr(custody, "ordinary_candidate_observation", observe)
    monkeypatch.setattr(custody, "current_process_audit_token", capture)
    return owner, census, native, birth, post, calls


@pytest.mark.parametrize("outcome", ["ABSENT", "ZOMBIE"])
def test_post_token_terminal_candidate_keeps_zero_authority_and_original_ancestry(
    observed_descendant, monkeypatch, outcome,
):
    owner, census, native, birth, _, calls = _successful_capture_terminal_fixture(observed_descendant, monkeypatch, outcome)
    fresh = {100: dict(census[100])}
    if outcome == "ZOMBIE": fresh[103] = {**census[103], **native, "birth_id": birth}
    result = owner.observe_census({103: birth}, census=census, fresh_census=lambda: fresh)
    assert calls == ["observation", "capture", "observation"]
    assert result == {100: census[100]["birth_id"]}
    assert set(owner._handles) == {100} and owner._uncertain is None
    proof = owner._unadmitted_absent[103]
    assert proof["disposition"] == "UNADMITTED_" + outcome
    assert proof["stage"] == ("post-token-no-authority-absence" if outcome == "ABSENT" else "post-token-identity")
    assert not proof["admitted"] and proof["capability"] == "NONE"
    assert proof["exit_reap"] == proof["subtree"] == "UNPROVED"
    assert not list(owner.root.glob("designation-descendant_103_*.json"))
    with pytest.raises(custody.CustodyError, match="lacks a retained exact"):
        owner.signal(103, birth, signal.SIGTERM)


@pytest.mark.parametrize("change", ["live-birth", "live-parent", "unknown", "zombie-microsecond", "zombie-uid", "zombie-parent", "designation-present"])
def test_post_token_terminal_candidate_rejects_identity_or_authority_drift(
    observed_descendant, monkeypatch, change,
):
    owner, census, _, birth, post, calls = _successful_capture_terminal_fixture(observed_descendant, monkeypatch, "ZOMBIE")
    if change.startswith("live-"):
        post["outcome"] = "LIVE"
        post["native"]["status"] = 2
        post["identity"]["birth_id" if change == "live-birth" else "parent_pid"] = "foreign-birth" if change == "live-birth" else 999
    elif change == "unknown": post.update(outcome="OBSERVATION_ERROR", error=OSError("controlled unknown observation"))
    elif change == "zombie-microsecond": post["native"]["start_usec"] += 1
    elif change == "zombie-uid": post["native"]["uid"] += 1
    elif change == "zombie-parent": post["native"]["parent_pid"] = 999
    else:
        owner.root.mkdir(parents=True)
        role = "descendant_103_" + hashlib.sha256(birth.encode()).hexdigest()[:12]
        (owner.root / ("designation-" + role + ".json")).write_text("{}")
    with pytest.raises(custody.CustodyError):
        owner.observe_census({103: birth}, census=census, fresh_census=lambda: pytest.fail("invalid candidate reconciled"))
    assert calls == ["observation", "capture", "observation"]
    assert owner._uncertain is not None and 103 not in owner._handles and not owner._unadmitted_absent


@pytest.mark.parametrize("change", ["missing-root", "root-zombie", "root-birth", "child-of-absent"])
def test_post_token_absence_preserves_fail_closed_reconciliation(observed_descendant, monkeypatch, change):
    owner, census, _, birth, _, _ = _successful_capture_terminal_fixture(observed_descendant, monkeypatch, "ABSENT")
    fresh = {100: dict(census[100])}
    if change == "missing-root": fresh = {}
    elif change == "root-zombie": fresh[100]["status"] = 5
    elif change == "root-birth": fresh[100]["birth_id"] = "foreign"
    else: census[104] = {"parent_pid": 103, "birth_id": "unproved-child"}
    with pytest.raises(custody.CustodyError):
        owner.observe_census({103: birth}, census=census, fresh_census=lambda: fresh)
    assert owner._uncertain is not None and 103 not in owner._handles
    assert owner._unadmitted_absent[103]["capability"] == "NONE"


def _failed_command_fixture(observed_descendant, monkeypatch):
    owner, census, native, birth = _capture_terminal_fixture(observed_descendant, monkeypatch, change="live")
    owner.command_diagnostics = True
    owner._command_observations = [{"candidate_pid": 1000 + i} for i in range(64)]
    census[100].update(pid=100, uid=501, status=2,
                       start_sec=native["start_sec"] - 1, start_usec=123456)
    census[100]["birth_id"] = custody._ps_lstart_birth(census[100]["start_sec"])
    owner._handles[100] = (census[100]["birth_id"], owner._handles[100][1])
    def command(pid):
        row = {**census[pid], "status": 2}
        metadata = custody._diagnostic_argv(_procargs_packet(
            ["/bin/bash", "--credential-file", "/private/secret-owner.token", "-c", "secret command"]))
        return {"outcome": "BRACKETED_METADATA", "authority": "NONE",
                "native_before": row, "native_after": dict(row),
                "metadata_first": metadata, "metadata_second": dict(metadata),
                "exec_transition": "UNPROVED_PUBLIC_METADATA_IS_NOT_ATOMIC_EXEC_IDENTITY"}
    monkeypatch.setattr(custody, "_command_observation", command)
    return owner, census, birth, command


def _observe_failed_command(owner, census, birth):
    with pytest.raises(custody.CustodyError, match="candidate is LIVE") as caught:
        owner.observe_census({100: census[100]["birth_id"], 103: birth}, census=census,
                             fresh_census=lambda: pytest.fail("LIVE failure reconciled"))
    assert owner._uncertain is not None and set(owner._handles) == {100}
    assert not owner._unadmitted_absent
    cause = caught.value
    while cause is not None and not isinstance(cause, custody.NativeTokenCaptureError):
        cause = cause.__cause__
    assert isinstance(cause, custody.NativeTokenCaptureError)
    evidence = json.loads((owner.root / "observed-containment.json").read_text())
    return evidence["capture_failures"][0]["failure_command_observations"], caught.value, cause


def test_failed_candidate_diagnostics_bypass_saturated_history_and_preserve_redaction(observed_descendant, monkeypatch):
    owner, census, birth, _ = _failed_command_fixture(observed_descendant, monkeypatch)
    history = list(owner._command_observations)
    diagnostic, error, cause = _observe_failed_command(owner, census, birth)
    assert owner._command_observations == history and owner._command_diagnostics_truncated is True
    assert diagnostic["candidate_pid"] == 103 and diagnostic["parent_pid"] == 100
    for key in ("target", "parent"):
        assert diagnostic[key]["classification"] == "MATCHED_PRE_POST_CENSUS_METADATA"
        assert diagnostic[key]["authority"] == "NONE" and diagnostic[key]["exec_identity"] == "UNPROVED"
    rendered = json.dumps(diagnostic)
    assert "secret-owner" not in rendered and "secret command" not in rendered
    assert "<redacted>" in rendered and "<opaque:" in rendered
    assert cause.capture_failure["port_acquired"] is False
    with pytest.raises(custody.CustodyError, match="lacks a retained exact"):
        owner.signal(103, birth, signal.SIGTERM)


@pytest.mark.parametrize("member,field", [(member, field) for member in (103, 100)
                                        for field in ("pid", "uid", "parent_pid", "pgid", "start_sec", "start_usec")])
def test_failed_candidate_command_metadata_identity_mismatch_remains_unproved(observed_descendant, monkeypatch, member, field):
    owner, census, birth, original = _failed_command_fixture(observed_descendant, monkeypatch)
    def command(pid):
        result = original(pid)
        if pid == member:
            result["native_before"][field] += 1
            result["native_after"][field] += 1
        return result
    monkeypatch.setattr(custody, "_command_observation", command)
    diagnostic, _, _ = _observe_failed_command(owner, census, birth)
    assert diagnostic["target" if member == 103 else "parent"]["classification"] == "UNPROVED"
    assert diagnostic["authority"] == "NONE"


@pytest.mark.parametrize("outcome", ["NOT_LIVE", "CHANGED_DURING_OBSERVATION", "OBSERVATION_ERROR"])
def test_failed_candidate_unavailable_or_vanished_command_metadata_never_changes_failure(observed_descendant, monkeypatch, outcome):
    owner, census, birth, _ = _failed_command_fixture(observed_descendant, monkeypatch)
    monkeypatch.setattr(custody, "_command_observation", lambda _: {"outcome": outcome, "authority": "NONE"})
    diagnostic, _, _ = _observe_failed_command(owner, census, birth)
    assert diagnostic["target"]["classification"] == diagnostic["parent"]["classification"] == "UNPROVED"
    assert diagnostic["authority"] == "NONE"


def test_failed_candidate_metadata_io_occurs_only_after_immediate_native_post(observed_descendant, monkeypatch):
    owner, census, birth, original = _failed_command_fixture(observed_descendant, monkeypatch)
    native_observer = custody.ordinary_candidate_observation
    events = []
    def observation(pid):
        result = native_observer(pid)
        events.append("native")
        return result
    def capture(pid):
        events.append("capture_failure")
        raise custody.NativeTokenCaptureError("retained original failure", substage="task_name_for_pid",
                                             kern_return=5, port_acquired=False)
    def command(pid):
        events.append("command")
        return original(pid)
    monkeypatch.setattr(custody, "ordinary_candidate_observation", observation)
    monkeypatch.setattr(custody, "current_process_audit_token", capture)
    monkeypatch.setattr(custody, "_command_observation", command)
    diagnostic, _, cause = _observe_failed_command(owner, census, birth)
    assert events == ["native", "capture_failure", "native", "command", "command"]
    assert diagnostic["target"]["classification"] == "MATCHED_PRE_POST_CENSUS_METADATA"
    assert str(cause) == "retained original failure"


@pytest.mark.parametrize("saturated", [True, False])
def test_failed_candidate_optional_observer_exception_cannot_mask_original_capture(observed_descendant, monkeypatch, saturated):
    owner, census, birth, _ = _failed_command_fixture(observed_descendant, monkeypatch)
    if not saturated:
        owner._command_observations = []
    def denied(_):
        raise OSError("private diagnostic secret")
    monkeypatch.setattr(custody, "_command_observation", denied)
    diagnostic, _, cause = _observe_failed_command(owner, census, birth)
    assert cause.capture_failure["substage"] == "task_name_for_pid"
    assert diagnostic["observation_error_type"] == "OSError"
    assert diagnostic["target"]["classification"] == "UNPROVED"
    assert "private diagnostic secret" not in json.dumps(diagnostic)


@pytest.mark.parametrize("phase", ["attachment", "serialization"])
def test_optional_failure_metadata_attachment_preserves_original_capture_and_journal(observed_descendant, monkeypatch, phase):
    owner, census, birth, _ = _failed_command_fixture(observed_descendant, monkeypatch)
    def broken(*_):
        if phase == "attachment":
            raise OSError("private attachment error")
        return {"unsupported": object()}
    monkeypatch.setattr(custody, "_capture_failure_command_evidence", broken)
    diagnostic, _, cause = _observe_failed_command(owner, census, birth)
    assert cause.capture_failure["substage"] == "task_name_for_pid"
    assert diagnostic["classification"] == "UNPROVED" and diagnostic["authority"] == "NONE"
    assert diagnostic["error_type"] == ("OSError" if phase == "attachment" else "TypeError")
    assert "private attachment error" not in json.dumps(diagnostic)


def test_mandatory_failure_journal_error_retains_original_capture_chain_and_uncertainty(observed_descendant, monkeypatch):
    owner, census, birth, _ = _failed_command_fixture(observed_descendant, monkeypatch)
    monkeypatch.setattr(owner, "_write_observed_containment", lambda: (_ for _ in ()).throw(OSError("private journal error")))
    with pytest.raises(custody.CustodyError, match="diagnostic persistence failed") as caught:
        owner.observe_census({100: census[100]["birth_id"], 103: birth}, census=census,
                             fresh_census=lambda: pytest.fail("journal failure reconciled"))
    error = caught.value
    while error is not None and not isinstance(error, custody.NativeTokenCaptureError):
        error = error.__cause__
    assert isinstance(error, custody.NativeTokenCaptureError)
    assert error.capture_failure["substage"] == "task_name_for_pid"
    assert owner._uncertain is not None and set(owner._handles) == {100}
    assert caught.value.__cause__.evidence_error_type == "OSError"


def _new_round_local_live_child(identities, tokens, pid):
    identities[pid] = {"pid": pid, "uid": 501, "birth_id": f"birth-{pid}", "parent_pid": 100}
    tokens[pid] = {"pid": pid, "uid": 501, "pidversion": pid + 1,
                   "sha256": f"sha256:token-{pid}", "words": [pid] * 8}
    return {"birth_id": f"birth-{pid}", "parent_pid": 100, "pgid": pid}


def test_historical_tombstone_does_not_chase_continuous_new_live_children(observed_descendant):
    owner, identities, census = _ordinary_census_fixture(observed_descendant)
    tokens = observed_descendant[3]
    root = {100: census[100]}
    owner.observe_census({100: "root", 103: "birth-103"}, census=census, fresh_census=lambda: root)
    old_record = dict(owner._unadmitted_absent[103])
    current = {**root, 104: _new_round_local_live_child(identities, tokens, 104)}
    calls = []
    def continuously_growing():
        pid = 105 + len(calls)
        calls.append(pid)
        current[pid] = _new_round_local_live_child(identities, tokens, pid)
        return dict(current)
    live = owner.observe_census({100: "root", 104: "birth-104"}, census=current, fresh_census=continuously_growing)
    assert live == {100: "root", 104: "birth-104"} and calls == []
    assert 104 in owner._handles and 103 not in owner._handles and owner._uncertain is None
    assert owner._unadmitted_absent[103] == old_record
    assert old_record["capability"] == "NONE" and old_record["exit_reap"] == "UNPROVED"


def test_new_absence_is_finalized_before_fresh_live_child_without_fixed_point_chase(observed_descendant):
    owner, identities, census = _ordinary_census_fixture(observed_descendant)
    tokens = observed_descendant[3]
    current = {100: census[100]}
    calls = []
    def continuously_growing():
        pid = 104 + len(calls)
        calls.append(pid)
        current[pid] = _new_round_local_live_child(identities, tokens, pid)
        return dict(current)
    live = owner.observe_census({100: "root", 103: "birth-103"}, census=census, fresh_census=continuously_growing)
    assert live == {100: "root", 104: "birth-104"} and calls == [104]
    assert owner._uncertain is None and 104 in owner._handles
    record = owner._unadmitted_absent[103]
    assert record["disposition"] == "UNADMITTED_ABSENT" and record["capability"] == "NONE"
    assert record["exit_reap"] == record["subtree"] == "UNPROVED"
    # The admitted child stays retained when its actual parent later exits.
    assert owner._handles[104][0] == "birth-104"
    reparented = {100: census[100], 104: {**current[104], "parent_pid": 1}}
    assert owner.observe_census({100: "root", 104: "birth-104"}, census=reparented,
                                 fresh_census=lambda: pytest.fail("historical absence forced fresh chase")) == {100: "root", 104: "birth-104"}


def test_genuinely_new_absence_every_round_still_exhausts_same_bound_and_stays_sticky(observed_descendant):
    owner, _, census = _ordinary_census_fixture(observed_descendant)
    calls = []
    def disappearing_every_round():
        pid = 104 + len(calls)
        calls.append(pid)
        return {100: census[100], pid: {"birth_id": f"birth-{pid}", "parent_pid": 100, "pgid": pid}}
    with pytest.raises(custody.CustodyError, match="did not stabilize within its bound"):
        owner.observe_census({100: "root", 103: "birth-103"}, census=census, fresh_census=disappearing_every_round)
    assert len(calls) == 8 and owner._uncertain is not None
    assert set(owner._handles) == {100}
    assert all(row["capability"] == "NONE" and row["exit_reap"] == "UNPROVED"
               for row in owner._unadmitted_absent.values())
    with pytest.raises(custody.CustodyError, match="unresolved"):
        owner.observe_census({100: "root"}, census={100: census[100]}, fresh_census=lambda: pytest.fail("uncertainty cleared"))


def test_command_inventory_stream_consumes_exact_frames_without_coalescing():
    import socket
    from astrid.core.execution import custody_broker as cb
    left, right = socket.socketpair()
    try:
        left.sendall(b'{"first":1}\n{"second":2}\n')
        assert cb._read_inventory_frame(right) == {"first":1}
        assert cb._read_inventory_frame(right) == {"second":2}
        import threading
        sender=threading.Thread(target=lambda:left.sendall(b'x' * (cb.FRAME_LIMIT + 1)))
        sender.start()
        with pytest.raises(cb.CustodyError, match="hard limit"):
            cb._read_inventory_frame(right)
        sender.join(2)
        assert not sender.is_alive()
    finally: left.close(); right.close()


@pytest.mark.parametrize("fault", ["epoch", "generation", "target", "operation", "replay", "concurrent", "closed", "checkout", "cwd", "environment"])
def test_command_inventory_rejects_bound_operation_before_helper_spawn(tmp_path, monkeypatch, fault):
    import os, socket
    from types import SimpleNamespace
    from astrid.core.execution import custody_broker as cb, process_group as pg
    checkout = tmp_path / "checkout"; checkout.mkdir()
    state = tmp_path / "state.json"; state.write_text("{}")
    git = tmp_path / "git"; git.write_text("test-only executable pin")
    policy = {"state_path":str(state),"state_sha256":cb._digest_bytes(state.read_bytes()),
        "roots":{str(checkout):[str(checkout)]},"git":str(git),"git_sha256":cb._digest_bytes(git.read_bytes()),
        "cwd":str(tmp_path),"environment":dict(os.environ)}
    custody = SimpleNamespace(root=tmp_path,owner_epoch="test-epoch")
    owner = cb._CommandInventoryOwner(custody,policy)
    binding = {"run_id":"test-run","owner_epoch":"test-epoch","generation":1,"target":{"pid":123}}
    monkeypatch.setattr(owner,"_authenticate",lambda channel:dict(binding))
    monkeypatch.setattr(pg,"popen_owned_process",lambda *a,**k:pytest.fail("invalid frame spawned a helper"))
    frame={"version":cb._COMMAND_INVENTORY_VERSION,"command":"inventory_git","binding":dict(binding),
        "operation_id":1,"operation":"revision","checkout":str(checkout),"cwd":str(tmp_path),"environment":dict(os.environ)}
    if fault in {"epoch","generation","target"}:
        key={"epoch":"owner_epoch","generation":"generation","target":"target"}[fault]
        frame["binding"][key]="changed"
    elif fault=="operation":frame["operation"]="clone"
    elif fault=="replay":owner.last_operation=1
    elif fault=="concurrent":owner.pending=1
    elif fault=="closed":owner.accepting=False
    elif fault=="checkout":frame["checkout"]=str(tmp_path / "foreign")
    elif fault=="cwd":frame["cwd"]="/"
    elif fault=="environment":frame["environment"]["GIT_CONFIG_GLOBAL"]="/foreign"
    left,right=socket.socketpair()
    try:
        with pytest.raises(cb.CustodyError):owner._execute(frame,right)
        assert owner.helpers=={}
    finally:left.close();right.close()


@pytest.mark.parametrize("fault", ["wrong_pid","wrong_uid","wrong_birth"])
def test_command_inventory_canonical_peer_predicate_rejects_foreign_root(tmp_path, monkeypatch, fault):
    from types import SimpleNamespace
    from astrid.core.execution import custody_broker as cb
    identity={"pid":123,"uid":501,"birth_id":"controlled-birth"}
    observed=dict(identity);observed[{"wrong_pid":"pid","wrong_uid":"uid","wrong_birth":"birth_id"}[fault]]="different"
    # Only the native peer surface is controlled; the canonical predicate runs.
    channel=object()
    broker=SimpleNamespace(operation_channel=channel,retained_pipe_actor=lambda:SimpleNamespace(verify=lambda:observed),registration={"identity":identity},bind_ready_token=lambda **kw:pytest.fail("foreign peer advanced readiness"))
    owner=cb._CommandInventoryOwner(SimpleNamespace(_launch_broker=broker,process=SimpleNamespace(pid=123)),{})
    with pytest.raises(cb.CustodyError,match="original root"):owner._authenticate(channel)


@pytest.mark.parametrize("fault", ["pending", "uncertain"])
def test_command_inventory_final_close_never_clears_unresolved_operations(tmp_path, fault):
    from types import SimpleNamespace
    from astrid.core.execution import custody_broker as cb
    owner=cb._CommandInventoryOwner(SimpleNamespace(),{})
    owner.done.set()
    if fault=="pending":owner.pending=1
    else:owner.error=cb.CustodyError("controlled uncertainty")
    with pytest.raises(cb.CustodyError,match="closure is uncertain"):owner.finish(cancel=True)
    assert not owner.accepting and owner.cancelled.is_set()
    assert owner.pending==1 if fault=="pending" else owner.error is not None


def test_command_inventory_client_rejects_stale_reply_and_latches_transport():
    import os,socket,threading
    from astrid.core.execution import custody_broker as cb
    left,right,descriptor=_inventory_pipe_pair()
    binding={"run_id":"fixture","owner_epoch":"fixture-epoch","generation":1,"target":{"pid":os.getpid()}}
    def server():
        cb._read_inventory_frame(right)
        cb._send_frame(right,{"version":cb._COMMAND_INVENTORY_VERSION,"status":"ready","binding":binding})
        cb._read_inventory_frame(right)
        cb._send_frame(right,{"version":cb._COMMAND_INVENTORY_VERSION,"operation_id":9,"binding":binding,"status":"completed","returncode":0})
    thread=threading.Thread(target=server);thread.start()
    try:
        client=cb._CommandInventoryClient(descriptor)
        with pytest.raises(cb.CustodyError):client.run("revision","/fixture","/fixture",{})
        assert client.error is not None
        with pytest.raises(cb.CustodyError,match="remains uncertain"):client.run("revision","/fixture","/fixture",{})
    finally:thread.join(2);right.close()


@pytest.mark.parametrize("fault", ["cancel", "disconnect", "timeout", "overflow", "cleanup"])
def test_command_inventory_execute_retains_original_helper_on_each_failure(tmp_path, monkeypatch, fault):
    import os, socket
    from types import SimpleNamespace
    from astrid.core.execution import custody_broker as cb, process_group as pg
    checkout=tmp_path/"checkout";checkout.mkdir();state=tmp_path/"state";state.write_text("{}")
    git=tmp_path/"git";git.write_text("#!/bin/sh\nexit 0\n");git.chmod(0o700)
    environment=dict(os.environ,PATH=str(tmp_path))
    policy={"state_path":str(state),"state_sha256":cb._digest_bytes(state.read_bytes()),
        "roots":{str(checkout):[str(checkout)]},"git":str(git),"git_sha256":cb._digest_bytes(git.read_bytes()),
        "cwd":str(tmp_path),"environment":environment}
    owner=cb._CommandInventoryOwner(SimpleNamespace(root=tmp_path,owner_epoch="fixture"),policy)
    binding={"run_id":"fixture","owner_epoch":"fixture","generation":1,"target":{"pid":123}}
    monkeypatch.setattr(owner,"_authenticate",lambda channel:binding)
    left,right=socket.socketpair();events=[]
    class Helper:
        pid=456;returncode=None;_astrid_custody=SimpleNamespace(_observer=None)
        def __init__(self):
            self.stdout=os.fdopen(os.pipe()[0],"rb",buffering=0)
            self.stderr=os.fdopen(os.pipe()[0],"rb",buffering=0)
        def poll(self):return self.returncode
        def wait(self,timeout):events.append("original_wait");self.returncode=-15;return -15
    # Keep both read endpoints at EOF while poll remains LIVE. This exercises
    # cancellation after pipe closure rather than a blocking wait shortcut.
    def closed_pipe():
        read,write=os.pipe();os.close(write);return os.fdopen(read,"rb",buffering=0)
    helper=Helper.__new__(Helper);helper.stdout=closed_pipe();helper.stderr=closed_pipe()
    def launch(*args,**kwargs):
        journal=[__import__("json").loads(line) for line in (tmp_path/"custody.journal.jsonl").read_text().splitlines()]
        assert owner.pending==1 and journal[-1]["event"]=="command_inventory_pending"
        events.append("spawn_after_pending")
        if fault=="cancel":owner.cancelled.set()
        if fault=="disconnect":left.close()
        return helper
    monkeypatch.setattr(pg,"popen_owned_process",launch)
    monkeypatch.setattr(pg,"terminate_tree",lambda proc:events.append("same_helper_stop"))
    def verify(proc):
        assert proc is helper;events.append("closure")
        if fault=="cleanup":raise cb.CustodyError("controlled helper cleanup failure")
    monkeypatch.setattr(pg,"verify_tree_absent",verify)
    if fault=="timeout":
        clock=iter([0,121]);monkeypatch.setattr(cb.time,"monotonic",lambda:next(clock,121))
    elif fault in {"overflow","cleanup"}:
        owner.cancelled.set() if fault=="cleanup" else None
        if fault=="overflow":monkeypatch.setattr(cb.os,"read",lambda fd,n:b"x"*(16*1024*1024+1))
    frame={"version":cb._COMMAND_INVENTORY_VERSION,"command":"inventory_git","binding":binding,
        "operation_id":1,"operation":"revision","checkout":str(checkout),"cwd":str(tmp_path),"environment":environment}
    try:
        with pytest.raises(cb.CustodyError,match="ownership remains uncertain"):owner._execute(frame,right)
        assert owner.helpers=={1:helper} and owner.pending==1 and owner.error is not None
        assert events.count("same_helper_stop")==1 and events.count("original_wait")==1
        assert events.index("original_wait")<events.index("closure")
        assert helper.stdout.closed and helper.stderr.closed
    finally:left.close();right.close()


@pytest.mark.parametrize("fault", ["malformed", "idle_eof", "pending_eof", "abort_release"])
def test_command_inventory_owner_channel_end_never_erases_non_eof_failure(tmp_path, fault):
    import socket
    from types import SimpleNamespace
    from astrid.core.execution import custody_broker as cb
    left,right=socket.socketpair()
    owner=cb._CommandInventoryOwner(SimpleNamespace(_launch_broker=SimpleNamespace(operation_channel=right),
        process=SimpleNamespace(poll=lambda:0)),{})
    owner.start()
    if fault=="abort_release":owner.abort_before_release()
    else:
        if fault=="pending_eof":owner.pending=1
        owner.released.set()
        if fault=="malformed":left.sendall(b'{"invalid":true}\n')
        else:left.close()
    try:
        assert owner.done.wait(2)
        if fault=="idle_eof":
            assert owner.channel_closed and owner.error is None
            owner.finish()
        else:
            assert owner.error is not None
            with pytest.raises(cb.CustodyError):owner.finish()
    finally:left.close();right.close()


@pytest.mark.parametrize("key", ["DYLD_INSERT_LIBRARIES", "LD_PRELOAD", "CUSTOM_UNAUTHORIZED"])
def test_command_inventory_rejects_every_unsupported_environment_delta(tmp_path, key):
    import os
    from types import SimpleNamespace
    from astrid.core.execution import custody_broker as cb
    root=tmp_path/"root";root.mkdir();state=tmp_path/"state";state.write_text("{}")
    policy={"state_path":str(state),"state_sha256":cb._digest_bytes(state.read_bytes()),
        "roots":{str(root):[str(root)]},"cwd":str(tmp_path),"environment":dict(os.environ)}
    owner=cb._CommandInventoryOwner(SimpleNamespace(),policy)
    with pytest.raises(cb.CustodyError,match="environment differs"):
        owner._validate_policy(str(root),str(tmp_path),dict(os.environ,**{key:"changed"}),"revision")


def test_command_inventory_binds_multiple_pack_subpaths_operation_specifically(tmp_path):
    import os
    from types import SimpleNamespace
    from astrid.core.execution import custody_broker as cb
    root=tmp_path/"root";root.mkdir();one=root/"one";one.mkdir();two=root/"two";two.mkdir()
    state=tmp_path/"state";state.write_text("{}")
    git=tmp_path/"git";git.write_text("#!/bin/sh\nexit 0\n");git.chmod(0o700)
    env=dict(os.environ,PATH=str(tmp_path))
    policy={"state_path":str(state),"state_sha256":cb._digest_bytes(state.read_bytes()),
        "roots":{str(root):[str(one),str(two)]},"cwd":str(tmp_path),"environment":env,
        "git":str(git),"git_sha256":cb._digest_bytes(git.read_bytes())}
    owner=cb._CommandInventoryOwner(SimpleNamespace(),policy)
    for pack in (one,two):
        owner._validate_policy(str(pack),str(tmp_path),env,"tracked_files")
        with pytest.raises(cb.CustodyError):owner._validate_policy(str(pack),str(tmp_path),env,"revision")
    owner._validate_policy(str(root),str(tmp_path),env,"revision")
    with pytest.raises(cb.CustodyError):owner._validate_policy(str(root),str(tmp_path),env,"tracked_files")


@pytest.mark.parametrize("fault", ["wrong_root", "forked_client"])
def test_command_inventory_client_endpoint_is_noninheritable_and_root_bound(fault, monkeypatch):
    import os,socket,threading
    from astrid.core.execution import custody_broker as cb
    left,right,descriptor=_inventory_pipe_pair()
    binding={"run_id":"fixture","owner_epoch":"fixture","generation":1,"target":{"pid":os.getpid()+(1 if fault=="wrong_root" else 0)}}
    def serve():
        cb._read_inventory_frame(right)
        cb._send_frame(right,{"version":cb._COMMAND_INVENTORY_VERSION,"status":"ready","binding":binding})
    thread=threading.Thread(target=serve);thread.start()
    try:
        if fault=="wrong_root":
            with pytest.raises(cb.CustodyError,match="authenticated root"):cb._CommandInventoryClient(descriptor)
            with pytest.raises(OSError):os.fstat(left.read_fd)
        else:
            client=cb._CommandInventoryClient(descriptor)
            assert not os.get_inheritable(left.read_fd) and not os.get_inheritable(left.write_fd)
            monkeypatch.setattr(cb.os,"getpid",lambda:client.creator_pid+1)
            with pytest.raises(cb.CustodyError,match="inherited"):client.run("revision","/root","/root",{})
            client.channel.close()
    finally:thread.join(2);right.close()


@pytest.mark.parametrize("persist_failure", [False, True])
def test_command_inventory_first_failure_retains_primary_chain_without_secret_values(tmp_path, monkeypatch, persist_failure):
    import json
    from types import SimpleNamespace
    from astrid.core.execution import custody_broker as cb
    owner=cb._CommandInventoryOwner(SimpleNamespace(root=tmp_path,owner_epoch="fixture-epoch",
        process=SimpleNamespace(pid=123),_launch_broker=SimpleNamespace(run_id="fixture-run")),{})
    owner.failure_stage="policy_environment"
    original=cb.CustodyError("private credential value not-for-output")
    original.__cause__=ValueError("private underlying token not-for-output")
    records=[]
    def append(path,row):
        records.append(row)
        if persist_failure:raise PermissionError("private persistence path not-for-output")
    monkeypatch.setattr(cb,"_append_owner_jsonl",append)
    owner._retain_first_failure(original)
    first=owner.first_failure
    owner.failure_stage="helper_closure";owner._retain_first_failure(RuntimeError("secondary cleanup"))
    assert owner.first_failure is first and len(records)==1
    assert first["substage"]=="policy_environment" and first["root_pid"]==123 and first["operation_id"]==1
    assert first["chain"]==[{"error_type":"CustodyError","message_sha256":cb._digest_bytes(str(original).encode())},
        {"error_type":"ValueError","message_sha256":cb._digest_bytes(str(original.__cause__).encode())}]
    assert "not-for-output" not in json.dumps(first) and first["raw_message_emitted"] is False
    assert first["authority"]=="NONE" and first["code"]=="COMMAND_INVENTORY_FAILED"
    assert owner.diagnostic_failure_type==("PermissionError" if persist_failure else None)
    assert original.__cause__.__class__ is ValueError


@pytest.mark.parametrize("fault", ["binding", "state", "cwd", "environment", "git", "optional_persistence"])
def test_command_inventory_actual_request_validation_journals_first_failure_before_spawn(tmp_path, monkeypatch, fault):
    import os,socket,json
    from types import SimpleNamespace
    from astrid.core.execution import custody_broker as cb, process_group as pg
    root=tmp_path/"checkout";root.mkdir();state=tmp_path/"state";state.write_text("{}")
    git=tmp_path/"git";git.write_text("#!/bin/sh\nexit 0\n");git.chmod(0o700)
    env=dict(os.environ,PATH=str(tmp_path))
    policy={"state_path":str(state),"state_sha256":cb._digest_bytes(state.read_bytes()),
        "roots":{str(root):[str(root)]},"cwd":str(tmp_path),"environment":env,
        "git":str(git),"git_sha256":cb._digest_bytes(git.read_bytes())}
    left,right=socket.socketpair()
    owner=cb._CommandInventoryOwner(SimpleNamespace(root=tmp_path,owner_epoch="fixture-epoch",
        process=SimpleNamespace(pid=123,poll=lambda:0),_launch_broker=SimpleNamespace(run_id="fixture-run",operation_channel=right)),policy)
    binding={"run_id":"fixture-run","owner_epoch":"fixture-epoch","generation":1,"target":{"pid":123}}
    # The controlled OS authentication surface is isolated to this negative
    # fixture; actual request/policy/server/journal and closure code runs.
    monkeypatch.setattr(owner,"_authenticate",lambda channel:binding)
    monkeypatch.setattr(pg,"popen_owned_process",lambda *a,**k:pytest.fail("rejected request spawned helper"))
    frame={"version":cb._COMMAND_INVENTORY_VERSION,"command":"inventory_git","binding":binding,
        "operation_id":1,"operation":"revision","checkout":str(root),"cwd":str(tmp_path),"environment":dict(env)}
    expected={"binding":"request_binding","state":"policy_source_state","cwd":"policy_cwd", "environment":"policy_environment","git":"policy_git_artifact","optional_persistence":"policy_environment"}[fault]
    if fault=="binding":frame["operation_id"]=99
    elif fault=="state":state.write_text("changed")
    elif fault=="cwd":frame["cwd"]="/"
    elif fault in {"environment","optional_persistence"}:frame["environment"]["DYLD_INSERT_LIBRARIES"]="private-secret-not-for-output"
    elif fault=="git":policy["git_sha256"]="changed";owner.policy["git_sha256"]="changed"
    if fault=="optional_persistence":
        def denied(path,row):raise PermissionError("secondary persistence error")
        monkeypatch.setattr(cb,"_append_owner_jsonl",denied)
    owner.start();owner.released.set()
    try:
        cb._send_frame(left,{"version":cb._COMMAND_INVENTORY_VERSION,"command":"inventory_hello"})
        assert cb._read_inventory_frame(left)["status"]=="ready"
        cb._send_frame(left,frame)
        assert owner.done.wait(2)
        assert owner.error is not None and owner.pending is None and owner.helpers=={}
        assert owner.first_failure["substage"]==expected
        if fault in {"environment","optional_persistence"}:
            assert owner.first_failure["detail"]=={"field_name_sha256":[cb._digest_bytes(b"DYLD_INSERT_LIBRARIES")],"different_field_count":1}
            assert "DYLD_INSERT_LIBRARIES" not in json.dumps(owner.first_failure)
        with pytest.raises(cb.CustodyError) as failure:owner.finish()
        assert failure.value.__cause__ is owner.error
        if fault=="optional_persistence":assert owner.diagnostic_failure_type=="PermissionError"
        else:
            journal=[json.loads(line) for line in (tmp_path/"custody.journal.jsonl").read_text().splitlines()]
            assert [row["event"] for row in journal]==["command_inventory_first_failure"]
            assert "private-secret" not in json.dumps(journal)
    finally:left.close();right.close()


def test_command_inventory_reply_failure_is_bound_to_completed_original_operation(tmp_path):
    import json
    from types import SimpleNamespace
    from astrid.core.execution import custody_broker as cb
    owner=cb._CommandInventoryOwner(SimpleNamespace(root=tmp_path,owner_epoch="fixture-epoch",
        process=SimpleNamespace(pid=123),_launch_broker=SimpleNamespace(run_id="fixture-run")),{})
    owner.last_operation=4;owner.pending=None;owner.failure_stage="reply"
    original=BrokenPipeError("private response channel")
    owner._retain_first_failure(original)
    assert owner.first_failure["operation_id"]==4
    assert owner.first_failure["substage"]=="reply"
    rows=[json.loads(line) for line in (tmp_path/"custody.journal.jsonl").read_text().splitlines()]
    assert len(rows)==1 and rows[0]["operation_id"]==4
    assert "private response channel" not in json.dumps(rows)


@pytest.mark.parametrize('phase', ['task_name_for_pid', 'task_info_first', 'task_info_second', 'pid_for_task'])
@pytest.mark.parametrize('outcome', ['ABSENT', 'ZOMBIE'])
def test_capture_substage_terminal_with_clean_release_retains_zero_authority(observed_descendant, monkeypatch, phase, outcome):
    owner, census, native, birth = _capture_absence_fixture(observed_descendant, monkeypatch)
    pre = {'outcome': 'LIVE', 'native': {**native, 'status': 2},
           'identity': {'pid': 103, 'uid': 501, 'parent_pid': 100, 'birth_id': birth}}
    post = {'outcome': outcome}
    if outcome == 'ZOMBIE': post['native'] = native
    samples = iter((pre, post))
    monkeypatch.setattr(custody, 'ordinary_candidate_observation', lambda _: next(samples))
    def capture(_):
        error = custody.NativeTokenCaptureError('controlled capture failure', substage=phase,
            kern_return=4 if phase.startswith('task_info') else 5, port_acquired=phase != 'task_name_for_pid')
        if phase != 'task_name_for_pid': error.capture_failure['port_release_succeeded'] = True
        raise error
    monkeypatch.setattr(custody, 'current_process_audit_token', capture)
    fresh = {100: census[100]}
    if outcome == 'ZOMBIE': fresh[103] = {**census[103], 'status': 5}
    owner.observe_census({pid: row['birth_id'] for pid, row in census.items()}, census=census, fresh_census=lambda: fresh)
    record = owner._unadmitted_absent[103]
    assert record['disposition'] == 'UNADMITTED_' + outcome
    assert record['capability'] == 'NONE' and record['admitted'] is False
    assert record['exit_reap'] == record['subtree'] == 'UNPROVED'
    assert 103 not in owner._handles and owner._uncertain is None
    assert not list(owner.root.glob('designation-descendant_103_*.json'))
    with pytest.raises(custody.CustodyError, match='lacks a retained exact'):
        owner.signal(103, birth, signal.SIGTERM)
    if outcome == 'ZOMBIE':
        with pytest.raises(custody.CustodyError, match='terminal candidate remains'):
            owner.validate_final_census(fresh)


@pytest.mark.parametrize('defect', ['missing-release', 'failed-release', 'release-error', 'token-consistency', 'zero-kern-pid', 'partial-task-name'])
def test_capture_substage_release_or_identity_failure_stays_sticky(observed_descendant, monkeypatch, defect):
    owner, census, _, birth = _capture_absence_fixture(observed_descendant, monkeypatch)
    def capture(_):
        phase = 'token_consistency' if defect == 'token-consistency' else ('pid_for_task' if defect == 'zero-kern-pid' else ('task_name_for_pid' if defect == 'partial-task-name' else 'task_info_first'))
        error = custody.NativeTokenCaptureError('controlled sticky failure', substage=phase,
            kern_return=0 if defect in {'token-consistency', 'zero-kern-pid'} else 4, port_acquired=True)
        if defect not in {'missing-release', 'partial-task-name'}:
            error.capture_failure['port_release_succeeded'] = defect != 'failed-release'
        if defect == 'release-error': error.port_release_error = custody.CustodyError('deallocation failed')
        raise error
    monkeypatch.setattr(custody, 'current_process_audit_token', capture)
    with pytest.raises(custody.CustodyError):
        owner.observe_census({pid: row['birth_id'] for pid, row in census.items()}, census=census,
            fresh_census=lambda: pytest.fail('sticky capture was reconciled'))
    assert owner._uncertain is not None and 103 not in owner._handles and not owner._unadmitted_absent


def test_real_owned_terminal_capture_substage_releases_mach_port(tmp_path, monkeypatch):
    import ctypes, select, subprocess, sys, time
    import astrid.core.execution.process_group as groups
    assert sys.platform == 'darwin'
    assert groups.ProcessCustody is custody.ProcessCustody
    program = '''import os, sys
child = None
released = False
for command in sys.stdin:
    command = command.strip()
    if command == "spawn":
        if child is not None: raise RuntimeError("duplicate spawn")
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(write_fd)
            os.read(read_fd, 1)
            os.close(read_fd)
            os._exit(0)
        os.close(read_fd)
        print(child, flush=True)
    elif command == "release":
        if child is None or released: raise RuntimeError("invalid release")
        os.write(write_fd, b"x")
        os.close(write_fd)
        released = True
    elif command == "reap":
        if child is not None:
            if not released:
                os.write(write_fd, b"x")
                os.close(write_fd)
            waited, status = os.waitpid(child, 0)
            if waited != child or not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
                raise RuntimeError("child original wait failed")
        print("reaped", flush=True)
        break
else:
    if child is not None:
        if not released:
            os.write(write_fd, b"x")
            os.close(write_fd)
        os.waitpid(child, 0)
'''
    process = groups.popen_owned_process([sys.executable, '-I', '-B', '-c', program],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True, _delegate_inventory=False)
    owner = process._astrid_custody
    trace = {'injection': 'task_info_first kern4 after real owned IPC exit',
             'natural_kernel_race': False, 'acquisitions': [], 'deallocations': []}
    target = {'pid': None, 'port': None, 'injected': False, 'birth': None}
    real_cdll = ctypes.CDLL
    def send(command):
        process.stdin.write(command + '\n'); process.stdin.flush()
    def line(timeout=3.0):
        assert select.select([process.stdout], [], [], timeout)[0], 'owned stdout timeout'
        return process.stdout.readline().strip()
    class ForwardCall:
        def __init__(self, original, invoke):
            object.__setattr__(self, '_original', original)
            object.__setattr__(self, '_invoke', invoke)
        def __getattr__(self, name): return getattr(self._original, name)
        def __setattr__(self, name, value): setattr(self._original, name, value)
        def __call__(self, *args): return self._invoke(self._original, *args)
    def instrumented_cdll(path, *args, **kwargs):
        library = real_cdll(path, *args, **kwargs)
        if path != '/usr/lib/libSystem.B.dylib': return library
        def name_call(original, task, pid, output):
            result = int(original(task, pid, output))
            if int(pid) == target['pid']:
                port = int(ctypes.cast(output, ctypes.POINTER(ctypes.c_uint32)).contents.value)
                trace['acquisitions'].append({'kern_return': result})
                assert result == 0 and port > 0, 'actual child Mach acquisition failed'
                target['port'] = port
            return result
        def info_call(original, port, flavor, output, count):
            if int(port) == target['port'] and not target['injected']:
                target['injected'] = True
                send('release')
                deadline = time.monotonic() + 2.0
                while True:
                    observation = custody.ordinary_candidate_observation(target['pid'])
                    if observation.get('outcome') == 'ZOMBIE':
                        assert observation['identity']['birth_id'] == target['birth']
                        assert all(observation['native'][k] == trace['pre_capture_observation']['native'][k]
                                   for k in ('start_sec','start_usec','uid','parent_pid'))
                        trace['post_exit_observation'] = observation
                        break
                    assert time.monotonic() < deadline, 'actual same-child zombie not observed'
                    time.sleep(0.01)
                return 4
            return original(port, flavor, output, count)
        def release_call(original, task, port):
            result = int(original(task, port))
            if int(port) == target['port']: trace['deallocations'].append({'kern_return': result})
            return result
        library.task_name_for_pid = ForwardCall(library.task_name_for_pid, name_call)
        library.task_info = ForwardCall(library.task_info, info_call)
        library.mach_port_deallocate = ForwardCall(library.mach_port_deallocate, release_call)
        return library
    real_capture = custody.current_process_audit_token
    def capture_and_record(pid):
        try: return real_capture(pid)
        except custody.NativeTokenCaptureError as error:
            if pid == target['pid']: trace['capture_failure'] = dict(error.capture_failure)
            raise
    try:
        with owner._lock:
            send('spawn'); target['pid'] = int(line())
            first = custody.ordinary_candidate_observation(target['pid'])
            assert first['outcome'] == 'LIVE'
            target['birth'] = first['identity']['birth_id']
            trace['pre_capture_observation'] = first
            with monkeypatch.context() as scoped:
                scoped.setattr(custody.ctypes, 'CDLL', instrumented_cdll)
                scoped.setattr(custody, 'current_process_audit_token', capture_and_record)
                groups.observe_tree(process)
            assert target['injected'] is True
            assert len(trace['acquisitions']) == len(trace['deallocations']) == 1
            assert trace['acquisitions'][0]['kern_return'] == trace['deallocations'][0]['kern_return'] == 0
            record = owner._unadmitted_absent[target['pid']]
            trace['retained_candidate'] = record
            assert record['disposition'] == 'UNADMITTED_ZOMBIE'
            assert record['capability'] == 'NONE' and record['admitted'] is False
            assert record['exit_reap'] == record['subtree'] == 'UNPROVED'
            assert trace['capture_failure']['port_release_succeeded'] is True
            assert target['pid'] not in owner._handles and owner._uncertain is None
            assert not list(owner.root.glob('designation-descendant_%s_*.json' % target['pid']))
            with pytest.raises(custody.CustodyError, match='lacks a retained exact'):
                owner.signal(target['pid'], target['birth'], signal.SIGTERM)
            # Final-census refusal is covered on independent unit fixtures. Calling
            # it here deliberately poisons this live owner before actual reaping.
            send('reap'); assert line() == 'reaped'
        process.stdin.close()
        assert process.wait(timeout=5.0) == 0
        groups.verify_tree_absent(process)
        trace['original_root_wait'] = 0; trace['final_tree_absent'] = True
    finally:
        if process.poll() is None:
            send('reap'); process.stdin.close(); process.wait(timeout=5.0)
        (tmp_path/'native-capture-trace.json').write_text(json.dumps(trace,indent=2,default=str))

@pytest.mark.parametrize('outcome', ['success','second-live-failure','second-absent','second-zombie','post-token-microsecond','recursive-pre-replacement','after-admission-census-failure'])
def test_one_same_transaction_live_capture_retry_never_defers_history(observed_descendant,monkeypatch,outcome):
    owner,census,native,birth=_capture_absence_fixture(observed_descendant,monkeypatch)
    _,_,identities,tokens=observed_descendant
    native={**native,'status':2}
    census[103]={**census[103],'status':2}
    identity={'pid':103,'uid':501,'parent_pid':100,'birth_id':birth}
    identities[103]=dict(identity)
    captures=[]
    observation_calls=[]
    def observation(pid):
        assert pid==103
        observation_calls.append(pid)
        if len(captures)==2 and outcome=='second-absent':return {'outcome':'ABSENT'}
        if len(captures)==2 and outcome=='second-zombie':return {'outcome':'ZOMBIE','identity':identity,'native':{**native,'status':5}}
        current=dict(native)
        if outcome=='recursive-pre-replacement' and len(observation_calls)>=4:current['start_usec']+=1
        if len(captures)==2 and outcome=='post-token-microsecond':current['start_usec']+=1
        return {'outcome':'LIVE','identity':dict(identity),'native':current}
    def capture(pid):
        assert pid==103;captures.append(pid)
        if len(captures)==1 or outcome=='second-live-failure':
            error=custody.NativeTokenCaptureError('controlled invalid destination',substage='task_info_first',kern_return=0x10000003,port_acquired=True)
            error.capture_failure['port_release_succeeded']=True
            raise error
        return {**tokens[103],'words':list(tokens[103]['words'])}
    monkeypatch.setattr(custody,'ordinary_candidate_observation',observation)
    monkeypatch.setattr(custody,'current_process_audit_token',capture)
    fresh_calls=[]
    def fresh():
        fresh_calls.append(1)
        if outcome=='after-admission-census-failure' and len(captures)==2:
            raise custody.CustodyError('controlled final census failure')
        return {pid:dict(row) for pid,row in census.items()}
    if outcome=='success':
        result=owner.observe_census({pid:row['birth_id'] for pid,row in census.items()},census=census,fresh_census=fresh)
        assert 103 in result and 103 in owner._handles and owner._uncertain is None
        assert len(captures)==2 and len(fresh_calls)>=2
        assert not owner._unadmitted_absent
        authority,reference=owner._handles[103][1]
        assert authority._live_start_anchor is None and reference['target']['pid']==103
    else:
        with pytest.raises(custody.CustodyError):
            owner.observe_census({pid:row['birth_id'] for pid,row in census.items()},census=census,fresh_census=fresh)
        assert len(captures)==(1 if outcome=='recursive-pre-replacement' else 2) and owner._uncertain is not None
        assert not owner._unadmitted_absent
        if outcome=='after-admission-census-failure':
            assert 103 in owner._handles
            assert list(owner.root.glob('designation-descendant_103_*.json'))
        else:
            assert 103 not in owner._handles
            assert not list(owner.root.glob('designation-descendant_103_*.json'))
        with pytest.raises(custody.CustodyError,match='unresolved'):
            owner.observe_census({},census={},fresh_census=lambda:{})
    assert len(owner._capture_failure_observations)>=1
    first=owner._capture_failure_observations[0]
    assert first['capture_failure']['kern_return']==0x10000003
    assert first['observations']['pre']['outcome']==first['observations']['post']['outcome']=='LIVE'


def test_real_owned_live_capture_retry_uses_actual_native_token(tmp_path, monkeypatch):
    import ctypes, select, subprocess, sys, time
    import astrid.core.execution.process_group as groups
    assert sys.platform == 'darwin'
    assert groups.ProcessCustody is custody.ProcessCustody
    program = '''import os, sys
child = None
released = False
for command in sys.stdin:
    command = command.strip()
    if command == "spawn":
        if child is not None: raise RuntimeError("duplicate spawn")
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(write_fd)
            os.read(read_fd, 1)
            os.close(read_fd)
            os._exit(0)
        os.close(read_fd)
        print(child, flush=True)
    elif command == "release":
        if child is None or released: raise RuntimeError("invalid release")
        os.write(write_fd, b"x")
        os.close(write_fd)
        released = True
    elif command == "reap":
        if child is not None:
            if not released:
                os.write(write_fd, b"x")
                os.close(write_fd)
            waited, status = os.waitpid(child, 0)
            if waited != child or not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
                raise RuntimeError("child original wait failed")
        print("reaped", flush=True)
        break
else:
    if child is not None:
        if not released:
            os.write(write_fd, b"x")
            os.close(write_fd)
        os.waitpid(child, 0)
'''
    process = groups.popen_owned_process([sys.executable, '-I', '-B', '-c', program],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True, _delegate_inventory=False)
    owner = process._astrid_custody
    trace = {'injection': 'task_info_first INVALID_DEST on real IPC-held LIVE child; actual second native capture',
             'natural_kernel_race': False, 'acquisitions': [], 'deallocations': []}
    target = {'pid': None, 'port': None, 'injected': False, 'birth': None}
    real_cdll = ctypes.CDLL
    def send(command):
        process.stdin.write(command + '\n'); process.stdin.flush()
    def line(timeout=3.0):
        assert select.select([process.stdout], [], [], timeout)[0], 'owned stdout timeout'
        return process.stdout.readline().strip()
    class ForwardCall:
        def __init__(self, original, invoke):
            object.__setattr__(self, '_original', original)
            object.__setattr__(self, '_invoke', invoke)
        def __getattr__(self, name): return getattr(self._original, name)
        def __setattr__(self, name, value): setattr(self._original, name, value)
        def __call__(self, *args): return self._invoke(self._original, *args)
    def instrumented_cdll(path, *args, **kwargs):
        library = real_cdll(path, *args, **kwargs)
        if path != '/usr/lib/libSystem.B.dylib': return library
        def name_call(original, task, pid, output):
            result = int(original(task, pid, output))
            if int(pid) == target['pid']:
                port = int(ctypes.cast(output, ctypes.POINTER(ctypes.c_uint32)).contents.value)
                trace['acquisitions'].append({'kern_return': result})
                assert result == 0 and port > 0, 'actual child Mach acquisition failed'
                target['port'] = port
            return result
        def info_call(original, port, flavor, output, count):
            if int(port) == target['port'] and not target['injected']:
                target['injected'] = True
                return 0x10000003
            return original(port, flavor, output, count)
        def release_call(original, task, port):
            result = int(original(task, port))
            if int(port) == target['port']: trace['deallocations'].append({'kern_return': result})
            return result
        library.task_name_for_pid = ForwardCall(library.task_name_for_pid, name_call)
        library.task_info = ForwardCall(library.task_info, info_call)
        library.mach_port_deallocate = ForwardCall(library.mach_port_deallocate, release_call)
        return library
    real_snapshot = groups._process_snapshot
    def snapshot_and_record():
        result = real_snapshot()
        trace.setdefault('census_after_acquisitions', []).append(len(trace['acquisitions']))
        return result
    real_capture = custody.current_process_audit_token
    def capture_and_record(pid):
        try: return real_capture(pid)
        except custody.NativeTokenCaptureError as error:
            if pid == target['pid']: trace['capture_failure'] = dict(error.capture_failure)
            raise
    try:
        with owner._lock:
            send('spawn'); target['pid'] = int(line())
            first = custody.ordinary_candidate_observation(target['pid'])
            assert first['outcome'] == 'LIVE'
            target['birth'] = first['identity']['birth_id']
            trace['pre_capture_observation'] = first
            with monkeypatch.context() as scoped:
                scoped.setattr(groups, '_process_snapshot', snapshot_and_record)
                scoped.setattr(custody.ctypes, 'CDLL', instrumented_cdll)
                scoped.setattr(custody, 'current_process_audit_token', capture_and_record)
                groups.observe_tree(process)
            assert target['injected'] is True
            assert 2 in trace['census_after_acquisitions']
            assert len(trace['acquisitions']) == len(trace['deallocations']) == 2
            assert all(row['kern_return'] == 0 for row in trace['acquisitions'] + trace['deallocations'])
            assert trace['capture_failure']['port_release_succeeded'] is True
            assert target['pid'] in owner._handles and owner._uncertain is None
            assert target['pid'] not in owner._unadmitted_absent
            authority, reference = owner._handles[target['pid']][1]
            assert authority._live_start_anchor is None
            assert reference['target']['pid'] == target['pid']
            records = [r for r in owner._capture_failure_observations if r['pid'] == target['pid']]
            assert len(records) == 1
            assert records[0]['observations']['pre']['outcome'] == records[0]['observations']['post']['outcome'] == 'LIVE'
            trace['retained_first_failure'] = records[0]
            assert list(owner.root.glob('designation-descendant_%s_*.json' % target['pid']))
            send('reap'); assert line() == 'reaped'
        process.stdin.close()
        assert process.wait(timeout=5.0) == 0
        groups.verify_tree_absent(process)
        trace['original_root_wait'] = 0; trace['final_tree_absent'] = True
    finally:
        if process.poll() is None:
            send('reap'); process.stdin.close(); process.wait(timeout=5.0)
        (tmp_path/'native-capture-trace.json').write_text(json.dumps(trace,indent=2,default=str))

@pytest.mark.parametrize('defect', [
    'wrong-stage','wrong-code','release-false','release-missing','release-error',
    'pre-precision','post-precision','missing-pgid','original-child','fresh-child',
    'unadmitted-parent','parent-birth-drift','actor-drift','root-handle-drift',
    'existing-designation','retry-pre-absent','recursive-pre-replacement',
    'diagnostic-persistence','fresh-census-error',
])
def test_live_capture_retry_rejects_every_unqualified_boundary(observed_descendant,monkeypatch,defect):
    owner,census,native,birth=_capture_absence_fixture(observed_descendant,monkeypatch)
    _,actors,identities,tokens=observed_descendant
    native={**native,'status':2,'pgid':census[103]['pgid']}
    census[103]={**census[103],'status':2}
    identity={'pid':103,'uid':501,'parent_pid':100,'birth_id':birth}
    if defect=='unadmitted-parent':
        native['parent_pid']=census[103]['parent_pid']=identity['parent_pid']=102
    identities[103]=dict(identity)
    original_root=owner._handles[100]
    role='descendant_103_'+hashlib.sha256(birth.encode()).hexdigest()[:12]
    designation=owner.root/('designation-'+role+'.json')
    prior_bytes=b'{"preexisting":"must-remain"}'
    if defect=='existing-designation':
        owner.root.mkdir(parents=True,exist_ok=True);designation.write_bytes(prior_bytes)
    if defect=='original-child':
        census[104]={**census[103],'pid':104,'parent_pid':103,'birth_id':'fixture-child-104','start_usec':native['start_usec']+1}
    captures=[];samples=[]
    def observation(pid):
        assert pid==103;samples.append(pid);index=len(samples)
        if defect=='retry-pre-absent' and index>=3:return {'outcome':'ABSENT'}
        current=dict(native)
        if defect=='pre-precision' and index==1:current['start_usec']+=1
        if defect=='post-precision' and index==2:current['start_usec']+=1
        if defect=='missing-pgid':current.pop('pgid')
        if defect=='recursive-pre-replacement' and index>=4:current['start_usec']+=1
        return {'outcome':'LIVE','identity':dict(identity),'native':current}
    def capture(pid):
        assert pid==103;captures.append(pid)
        if len(captures)==1:
            error=custody.NativeTokenCaptureError('controlled retry qualification failure',
                substage='task_info_second' if defect=='wrong-stage' else 'task_info_first',
                kern_return=5 if defect=='wrong-code' else 0x10000003,port_acquired=True)
            if defect!='release-missing':error.capture_failure['port_release_succeeded']=defect!='release-false'
            if defect=='release-error':error.port_release_error=custody.NativeTokenCaptureError('controlled release failure',substage='mach_port_deallocate',kern_return=4,port_acquired=True)
            raise error
        return {**tokens[103],'words':list(tokens[103]['words'])}
    monkeypatch.setattr(custody,'ordinary_candidate_observation',observation)
    monkeypatch.setattr(custody,'current_process_audit_token',capture)
    if defect=='diagnostic-persistence':
        def failed_write():raise OSError('controlled diagnostic checkpoint failure')
        monkeypatch.setattr(owner,'_write_observed_containment',failed_write)
    def fresh():
        if defect=='fresh-census-error':raise OSError('controlled fresh native census failure')
        rows={pid:dict(row) for pid,row in census.items()}
        if defect=='fresh-child':rows[104]={**rows[103],'pid':104,'parent_pid':103,'birth_id':'fixture-child-104','start_usec':native['start_usec']+1}
        if defect=='parent-birth-drift':rows[100]['birth_id']='replacement-parent'
        if defect=='actor-drift':owner.actor=actors[102]
        if defect=='root-handle-drift':owner._handles[100]=(original_root[0],object())
        return rows
    with pytest.raises((custody.CustodyError,OSError)):
        owner.observe_census({pid:row['birth_id'] for pid,row in census.items()},census=census,fresh_census=fresh)
    assert captures==[103]
    assert owner._uncertain is not None and 103 not in owner._handles
    assert not owner._unadmitted_absent
    if defect=='existing-designation':assert designation.read_bytes()==prior_bytes
    else:assert not designation.exists() and not designation.is_symlink()
    assert len(owner._capture_failure_observations)==1
    if defect=='recursive-pre-replacement':assert len(samples)>=4
    with pytest.raises(custody.CustodyError,match='unresolved'):
        owner.observe_census({},census={},fresh_census=lambda:{})


def _prospective_exec_fixture(observed_descendant, monkeypatch):
    owner, census, native, birth = _capture_absence_fixture(observed_descendant, monkeypatch)
    _, _, identities, tokens = observed_descendant
    native.update(pgid=census[103]['pgid'], status=2)
    census[103].update(status=2)
    identities[103] = {'pid':103, 'uid':501, 'parent_pid':100, 'birth_id':birth}
    start = {'pid':103, 'uid':501, 'start_abstime':100, 'start_sec':native['start_sec'], 'start_usec':native['start_usec'], 'status':2}
    events=[]
    def read_start(pid):
        assert pid==103
        events.append('start')
        return dict(start)
    def clock():
        events.append('clock')
        return 101
    def capture(pid):
        assert pid==103
        events.append('capture')
        return {**tokens[pid], 'words':list(tokens[pid]['words'])}
    monkeypatch.setattr(custody,'_darwin_process_start',read_start)
    monkeypatch.setattr(custody,'_darwin_absolute_time',clock)
    monkeypatch.setattr(custody,'current_process_audit_token',capture)
    monkeypatch.setattr(custody,'ordinary_candidate_observation',lambda pid:{'outcome':'LIVE','identity':dict(identities[pid]),'native':dict(native)})
    return owner,census,native,birth,start,events,tokens


def _register_prospective(owner,census):
    owner.observe_census({pid:row['birth_id'] for pid,row in census.items()},census=census,fresh_census=lambda:{pid:dict(row) for pid,row in census.items()})


def test_prospective_command_descendant_anchors_before_initial_capture_and_refreshes_at_signal(observed_descendant,monkeypatch):
    owner,census,_,birth,start,events,tokens=_prospective_exec_fixture(observed_descendant,monkeypatch)
    original=list(tokens[103]['words'])
    _register_prospective(owner,census)
    first=events.index('capture')
    assert 'start' in events[:first] and 'clock' in events[:first]
    assert events.count('capture')==1
    authority,reference=owner._handles[103][1]
    assert authority._live_start_anchor==custody._start_anchor(start)
    assert authority._anchor_requires_resume is False
    assert authority._read()['target']['audit_token_words']==original
    tokens[103]={**tokens[103],'pidversion':999,'sha256':'sha256:rotation-999','words':[103]*7+[999]}
    calls=[]
    real=authority.signal
    monkeypatch.setattr(authority,'signal',lambda **kw:real(**kw,signal_provider=lambda *args:calls.append(args)))
    owner.signal(103,birth,signal.SIGTERM)
    assert calls==[(tokens[103]['words'],signal.SIGTERM)]
    assert owner._handles[103][1][1]['target']['audit_token_pidversion']==999
    assert owner._uncertain is None


def test_prospective_original_capture_rejects_same_wall_birth_different_monotonic_start(observed_descendant,monkeypatch):
    owner,census,_,_,start,events,_=_prospective_exec_fixture(observed_descendant,monkeypatch)
    monkeypatch.setattr(custody,'_darwin_process_start',lambda pid:{**start,'start_abstime':200 if 'capture' in events else 100})
    with pytest.raises(custody.CustodyError):_register_prospective(owner,census)
    assert events.count('capture')==1
    assert 103 not in owner._handles and owner._uncertain is not None
    assert not list(owner.root.glob('designation-descendant_103_*.json'))


@pytest.mark.parametrize('read_number',[1,2,3])
def test_prospective_optional_target_gone_keeps_original_fixed_capability(observed_descendant,monkeypatch,read_number):
    owner,census,_,_,start,events,tokens=_prospective_exec_fixture(observed_descendant,monkeypatch)
    count=0
    def observe(pid):
        nonlocal count
        if 'capture' in events:
            count+=1
            if count==read_number:raise custody.DarwinProcessExitedError('target only')
        return dict(start)
    monkeypatch.setattr(custody,'_darwin_process_start',observe)
    _register_prospective(owner,census)
    authority,reference=owner._handles[103][1]
    assert authority._live_start_anchor is None
    assert authority._initial_capture_witness is None
    assert authority._read()['transitions']==[]
    assert authority._read()['target']['audit_token_words']==tokens[103]['words']
    assert owner._uncertain is None


@pytest.mark.parametrize('failure',['actor','commit','clock'])
def test_prospective_authority_failures_are_not_target_declines(observed_descendant,monkeypatch,failure):
    owner,census,_,_,_,events,_=_prospective_exec_fixture(observed_descendant,monkeypatch)
    if failure=='actor':
        def fail(*a,**kw):raise custody.DarwinProcessExitedError('actor verification failure')
        monkeypatch.setattr(custody.RoleCustodyAuthority,'establish_start_anchor',fail)
    elif failure=='commit':
        real=custody.RoleCustodyAuthority._commit
        def fail(self,record,**kwargs):
            if any(t['intent']['action']=='establish-live-start-anchor' for t in record.get('transitions', [])):raise OSError('fsync failed')
            return real(self,record,**kwargs)
        monkeypatch.setattr(custody.RoleCustodyAuthority,'_commit',fail)
    else:
        monkeypatch.setattr(custody,'_darwin_absolute_time',lambda:99 if 'capture' in events else 101)
    with pytest.raises((custody.CustodyError,OSError)):_register_prospective(owner,census)
    assert owner._uncertain is not None
    if failure!='clock':assert 103 in owner._handles


@pytest.mark.parametrize('exec_during_capture',[False,True])
def test_real_owned_ordinary_descendant_exec_refreshes_retained_anchor(tmp_path,monkeypatch,exec_during_capture):
    import select
    import astrid.core.execution.process_group as groups
    assert sys.platform=='darwin'
    assert groups.ProcessCustody is custody.ProcessCustody
    program=r'''import os,sys,json,signal
child=None
executed=False
for command in sys.stdin:
    command=command.strip()
    if command=='spawn':
        gate_r,gate_w=os.pipe();ready_r,ready_w=os.pipe()
        child=os.fork()
        if child==0:
            os.close(gate_w);os.close(ready_r)
            if os.read(gate_r,1)!=b'E':os._exit(0)
            os.set_inheritable(gate_r,True);os.set_inheritable(ready_w,True)
            target='import os,sys; os.write(int(sys.argv[2]),b"R"); os.close(int(sys.argv[2])); os.read(int(sys.argv[1]),1); os._exit(0)'
            os.execv(sys.executable,[sys.executable,'-I','-B','-c',target,str(gate_r),str(ready_w)])
        os.close(gate_r);os.close(ready_w)
        print(child,flush=True)
    elif command=='exec':
        os.write(gate_w,b'E')
        if os.read(ready_r,1)!=b'R':raise RuntimeError('exec readiness failed')
        os.close(ready_r);executed=True
        print('exec-ready',flush=True)
    elif command in {'reap','finish'}:
        if child is not None:
            if command=='finish':
                try:os.write(gate_w,b'F' if executed else b'Q')
                except BrokenPipeError:pass
            os.close(gate_w)
            waited,status=os.waitpid(child,0)
            if waited!=child:raise RuntimeError('original wait mismatch')
            if command=='reap':
                if not os.WIFSIGNALED(status) or os.WTERMSIG(status)!=signal.SIGTERM:raise RuntimeError('wrong terminal cause')
                print(json.dumps({'child_original_wait':'SIGTERM','reaped':True}),flush=True)
            elif not ((os.WIFEXITED(status) and os.WEXITSTATUS(status)==0) or (os.WIFSIGNALED(status) and os.WTERMSIG(status)==signal.SIGTERM)):raise RuntimeError('IPC cleanup failed')
        break
else:
    if child is not None:
        try:os.write(gate_w,b'F' if executed else b'Q')
        except BrokenPipeError:pass
        os.close(gate_w);os.waitpid(child,0)
'''
    process=groups.popen_owned_process([sys.executable,'-I','-B','-c',program],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True,_delegate_inventory=False)
    owner=process._astrid_custody
    trace={'proof':'IPC-held ordinary descendant; real exec and retained-owner TERM','exec_during_initial_capture':exec_during_capture,'natural_race_claim':False}
    reaped=False
    def send(command):process.stdin.write(command+'\n');process.stdin.flush()
    def line():
        assert select.select([process.stdout],[],[],5)[0],'owned stdout timeout'
        return process.stdout.readline().strip()
    try:
        with owner._lock:
            send('spawn');pid=int(line())
            obs=custody.ordinary_candidate_observation(pid);assert obs['outcome']=='LIVE'
            birth=obs['identity']['birth_id']
            actual_capture=custody.current_process_audit_token
            executed=False
            if exec_during_capture:
                def capture(target):
                    nonlocal executed
                    token=actual_capture(target)
                    if target==pid and not executed:
                        send('exec');assert line()=='exec-ready';executed=True
                    return token
                monkeypatch.setattr(custody,'current_process_audit_token',capture)
            groups.observe_tree(process)
            authority,before=owner._handles[pid][1]
            assert authority._live_start_anchor is not None
            assert authority._anchor_requires_resume is False
            anchor=dict(authority._live_start_anchor)
            initial=before['target']['audit_token_pidversion']
            if not exec_during_capture:send('exec');assert line()=='exec-ready'
            else:assert executed
            current=actual_capture(pid)
            assert custody._start_anchor(custody._darwin_process_start(pid))==anchor
            assert current['pidversion']!=initial
            owner.signal(pid,birth,signal.SIGTERM)
            assert owner._handles[pid][1][1]['target']['audit_token_pidversion']==current['pidversion']
            assert any(r['intent']['action']=='refresh-exec-token' for r in authority._read()['transitions'])
            send('reap');waited=json.loads(line());assert waited['reaped'];reaped=True
            trace.update(initial_version=initial,refreshed_version=current['pidversion'],retained_anchor=anchor,child_original_wait=waited)
        process.stdin.close();assert process.wait(timeout=5)==0
        groups.verify_tree_absent(process)
        trace.update(root_original_wait=0,final_tree_absent=True)
    finally:
        if process.poll() is None:
            if not reaped:send('finish')
            process.stdin.close();process.wait(timeout=5)
        (tmp_path/'native-exec-trace.json').write_text(json.dumps(trace,indent=2)+'\n')


def _ordinary_signal_fixture(observed_descendant):
    owner, actors, identities, tokens = observed_descendant
    identity = dict(identities[103])
    owner, _, _ = _ordinary_census_fixture(observed_descendant)
    identities[103] = identity
    owner.observe({103: 'birth-103'})
    return owner, actors, identities, tokens


@pytest.mark.parametrize('anchor', [False, True])
@pytest.mark.parametrize('gone', ['none', 'typed-target'])
def test_ordinary_signal_consumes_only_pre_mutation_target_gone_without_settling_custody(observed_descendant, monkeypatch, anchor, gone):
    owner, actors, identities, tokens = _ordinary_signal_fixture(observed_descendant)
    retained = owner._handles[103]
    authority, reference = retained[1]
    if anchor:
        start = {'pid': 103, 'uid': 501, 'start_abstime': 100, 'start_sec': 123, 'start_usec': 456, 'status': 2}
        authority.establish_start_anchor(actor=actors[101], generation=1, expected_target=reference['target'], authenticated_token=tokens[103], require_suspended=False, start_provider=lambda _: dict(start), token_provider=lambda _: dict(tokens[103]), clock_provider=lambda: 101)
        retained = ('birth-103', (authority, authority.reference()))
        owner._handles[103] = retained
    before = authority._read()
    def target_identity(pid):
        if pid == 103:
            if gone == 'typed-target': raise custody.DarwinProcessExitedError('target is zombie')
            return None
        return identities.get(pid)
    monkeypatch.setattr(custody, 'default_process_identity', target_identity)
    monkeypatch.setattr(authority, '_commit', lambda *_: pytest.fail('target-gone committed'))
    real_signal = authority.signal
    monkeypatch.setattr(authority, 'signal', lambda **kwargs: real_signal(**kwargs, signal_provider=lambda *_: pytest.fail('target-gone signalled')))
    owner.signal(103, 'birth-103', signal.SIGTERM)
    assert owner._handles[103] is retained
    assert authority._read() == before
    assert owner._uncertain is None
    assert not getattr(owner, '_unadmitted_absent', {})


@pytest.mark.parametrize('consumer', ['direct-role', 'nonordinary-owner'])
def test_target_gone_marker_does_not_settle_nonordinary_consumers(observed_descendant, monkeypatch, consumer):
    owner, actors, _, _ = _ordinary_signal_fixture(observed_descendant)
    retained = owner._handles[103]
    authority, reference = retained[1]
    monkeypatch.setattr(custody, 'default_process_identity', lambda _: None)
    monkeypatch.setattr(authority, '_commit', lambda *_: pytest.fail('committed'))
    if consumer == 'direct-role':
        with pytest.raises(custody._SignalTargetGone):
            authority.signal(actor=actors[101], generation=1, expected_target=reference['target'], signum=signal.SIGTERM, identity_provider=lambda _: None, signal_provider=lambda *_: pytest.fail('signalled'))
    else:
        owner._launch_broker.role = 'engine'
        with pytest.raises(custody._SignalTargetGone): owner.signal(103, 'birth-103', signal.SIGTERM)
    assert owner._handles[103] is retained


@pytest.mark.parametrize('origin', ['actor', 'guard', 'anchor-start', 'commit', 'kernel', 'generic-target'])
def test_ordinary_signal_never_consumes_exited_from_authority_or_later_phases(observed_descendant, monkeypatch, origin):
    from contextlib import contextmanager
    owner, actors, identities, tokens = _ordinary_signal_fixture(observed_descendant)
    authority, reference = owner._handles[103][1]
    start = {'pid': 103, 'uid': 501, 'start_abstime': 100, 'start_sec': 123, 'start_usec': 456, 'status': 2}
    authority.establish_start_anchor(actor=actors[101], generation=1, expected_target=reference['target'], authenticated_token=tokens[103], require_suspended=False, start_provider=lambda _: dict(start), token_provider=lambda _: dict(tokens[103]), clock_provider=lambda: 101)
    retained = ('birth-103', (authority, authority.reference()))
    owner._handles[103] = retained
    committed, kernel = [], []
    def exited(): raise custody.DarwinProcessExitedError('not the initial target identity boundary')
    def commit(record):
        committed.append(True)
        if origin == 'commit': exited()
        return real_commit(record)
    def signal_call(*args):
        kernel.append(args)
        if origin == 'kernel': exited()
        pytest.fail('unexpected kernel call')
    real_commit = authority._commit
    real_signal = authority.signal
    monkeypatch.setattr(authority, '_commit', commit)
    monkeypatch.setattr(custody, '_darwin_process_start', lambda _: exited() if origin == 'anchor-start' else dict(start))
    if origin == 'actor': monkeypatch.setattr(owner.actor, 'verify', exited)
    elif origin == 'guard':
        @contextmanager
        def broken_guard(**kwargs):
            exited()
            yield
        monkeypatch.setattr(authority, '_guard', broken_guard)
    elif origin == 'generic-target':
        def target_failure(pid):
            if pid == 103: raise custody.CustodyError('identity inspection failed')
            return identities.get(pid)
        monkeypatch.setattr(custody, 'default_process_identity', target_failure)
    monkeypatch.setattr(authority, 'signal', lambda **kwargs: real_signal(**kwargs, signal_provider=signal_call))
    expected = custody.CustodyError if origin == 'generic-target' else custody.DarwinProcessExitedError
    with pytest.raises(expected) as caught: owner.signal(103, 'birth-103', signal.SIGTERM)
    assert not isinstance(caught.value, custody._SignalTargetGone)
    assert owner._handles[103] is retained
    assert len(committed) == (1 if origin in {'commit', 'kernel'} else 0)
    assert len(kernel) == (1 if origin == 'kernel' else 0)

@pytest.mark.parametrize('state', ['LIVE', 'ZOMBIE', 'reused'])
def test_consumed_target_gone_still_rejects_present_or_reused_final_census(observed_descendant, monkeypatch, state):
    owner, _, identities, _ = _ordinary_signal_fixture(observed_descendant)
    retained = owner._handles[103]
    monkeypatch.setattr(custody, 'default_process_identity', lambda pid: None if pid == 103 else identities.get(pid))
    authority = retained[1][0]
    real_signal = authority.signal
    monkeypatch.setattr(authority, 'signal', lambda **kwargs: real_signal(**kwargs, signal_provider=lambda *_: pytest.fail('unexpected kernel signal')))
    owner.signal(103, 'birth-103', signal.SIGTERM)
    row = {'pid': 103, 'uid': 501, 'parent_pid': 100, 'pgid': 103, 'birth_id': 'replacement' if state == 'reused' else 'birth-103', 'status': 5 if state == 'ZOMBIE' else 2}
    with pytest.raises(custody.CustodyError): owner.validate_final_census({103: row})
    assert owner._handles[103] is retained and owner._uncertain is not None
    with pytest.raises(custody.CustodyError, match='unresolved'): owner.validate_final_census({})


def test_consumed_target_gone_accepts_only_fresh_absent_census_without_dropping_handle(observed_descendant, monkeypatch):
    owner, _, identities, _ = _ordinary_signal_fixture(observed_descendant)
    retained = owner._handles[103]
    monkeypatch.setattr(custody, 'default_process_identity', lambda pid: None if pid == 103 else identities.get(pid))
    authority = retained[1][0]
    real_signal = authority.signal
    monkeypatch.setattr(authority, 'signal', lambda **kwargs: real_signal(**kwargs, signal_provider=lambda *_: pytest.fail('unexpected kernel signal')))
    owner.signal(103, 'birth-103', signal.SIGTERM)
    owner.validate_final_census({999: {'pid': 999, 'uid': 501, 'parent_pid': 1, 'pgid': 999, 'birth_id': 'unrelated-999', 'status': 2}})
    assert owner._handles[103] is retained and owner._uncertain is None


def test_real_owned_admitted_zombie_signal_observation_requires_original_reap_and_final_census(tmp_path, monkeypatch):
    import select, time
    import astrid.core.execution.process_group as groups
    assert sys.platform == 'darwin'
    assert groups.ProcessCustody is custody.ProcessCustody
    program = r'''import os, sys, json
child = None
released = False
for command in sys.stdin:
    command = command.strip()
    if command == 'spawn':
        if child is not None: raise RuntimeError('duplicate spawn')
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(write_fd); os.read(read_fd, 1); os.close(read_fd); os._exit(0)
        os.close(read_fd); print(child, flush=True)
    elif command == 'release':
        if child is None or released: raise RuntimeError('invalid release')
        os.write(write_fd, b'x'); os.close(write_fd); released = True
        print('released-without-wait', flush=True)
    elif command == 'reap':
        if child is not None:
            if not released:
                try: os.write(write_fd, b'x')
                except BrokenPipeError: pass
                os.close(write_fd)
            waited, status = os.waitpid(child, 0)
            if waited != child or not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0: raise RuntimeError('original child wait failed')
            print(json.dumps({'reaped': True, 'child_original_wait': 0}), flush=True)
        break
else:
    if child is not None:
        if not released:
            try: os.write(write_fd, b'x')
            except BrokenPipeError: pass
            os.close(write_fd)
        os.waitpid(child, 0)
'''
    process = groups.popen_owned_process([sys.executable, '-I', '-B', '-c', program], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, _delegate_inventory=False)
    owner = process._astrid_custody
    trace = {'proof': 'admitted held child; native zombie; no-signal observation; original waits and final census'}
    reaped = False
    def send(command): process.stdin.write(command + '\n'); process.stdin.flush()
    def line():
        assert select.select([process.stdout], [], [], 5)[0], 'owned stdout timeout'
        return process.stdout.readline().strip()
    try:
        with owner._lock:
            send('spawn'); pid = int(line())
            pre = custody.ordinary_candidate_observation(pid); assert pre['outcome'] == 'LIVE'
            birth = pre['identity']['birth_id']
            groups.observe_tree(process)
            retained = owner._handles[pid]; authority, reference = retained[1]
            before = authority._read()
            send('release'); assert line() == 'released-without-wait'
            deadline = time.monotonic() + 5
            while True:
                terminal = custody.ordinary_candidate_observation(pid)
                if terminal['outcome'] == 'ZOMBIE': break
                assert terminal['outcome'] == 'LIVE', terminal['outcome']
                assert time.monotonic() < deadline, 'owned child did not become native zombie'
            assert terminal['native']['status'] == 5
            for key in ('pid', 'uid', 'start_sec', 'start_usec'): assert terminal['native'][key] == pre['native'][key]
            real_signal = authority.signal
            with monkeypatch.context() as scoped:
                scoped.setattr(authority, '_commit', lambda *_: pytest.fail('zombie observation committed'))
                scoped.setattr(authority, 'signal', lambda **kwargs: real_signal(**kwargs, signal_provider=lambda *_: pytest.fail('zombie observation signalled')))
                owner.signal(pid, birth, signal.SIGTERM)
            assert owner._handles[pid] is retained
            assert authority._read() == before and owner._uncertain is None
            assert process.poll() is None
            trace.update(native_pre=pre['native'], native_terminal=terminal['native'], signal_kernel_calls=0, signal_commits=0, retained_reference_unchanged=True)
            send('reap'); waited = json.loads(line())
            assert waited == {'reaped': True, 'child_original_wait': 0}
            reaped = True; trace['child_original_wait'] = waited
        process.stdin.close(); assert process.wait(timeout=5) == 0
        groups.verify_tree_absent(process)
        assert pid in owner._handles
        trace.update(root_original_wait=0, final_tree_absent=True)
    finally:
        if process.poll() is None:
            if not reaped: send('reap')
            process.stdin.close(); process.wait(timeout=5)
        (tmp_path / 'native-target-gone-trace.json').write_text(json.dumps(trace, indent=2) + '\n')

@pytest.mark.parametrize('phase', ['start-before', 'token', 'start-after'])
def test_first_anchored_target_capture_exit_is_precise_sibling_without_commit_or_kernel(graph, monkeypatch, phase):
    authority = graph[0]
    reader = establish_anchor(graph)
    before = authority._read()
    reads = []
    def start(pid):
        reads.append(pid)
        if (phase == 'start-before' and len(reads) == 1) or (phase == 'start-after' and len(reads) == 2):
            raise custody.DarwinProcessExitedError('target gone at controlled first capture')
        return reader(pid)
    def capture(pid):
        if phase == 'token': raise custody.DarwinProcessExitedError('target gone during first token read')
        return graph[3][pid]
    monkeypatch.setattr(authority, '_commit', lambda *_: pytest.fail('first target exit committed'))
    with pytest.raises(custody._SignalCaptureTargetGone) as error:
        fresh_fire(graph, start, capture=capture, kernel=lambda *_: pytest.fail('first target exit signalled'))
    assert not isinstance(error.value, custody._SignalTargetGone)
    assert authority._read() == before
    assert len(reads) == (2 if phase == 'start-after' else 1)

@pytest.mark.parametrize('result', ['exit', 'still-live', 'plain-exited'])
@pytest.mark.parametrize('marker_type', [custody._SignalCaptureTargetGone, custody._SignalTargetGone])
def test_original_root_capture_exit_uses_only_bounded_original_wait(observed_descendant, monkeypatch, result, marker_type):
    import time
    owner, _, _, _ = observed_descendant
    broker = object.__new__(custody.RoleBoundCustodyBroker)
    broker.role = 'command'
    marker = (custody.DarwinProcessExitedError('plain actor/guard error') if result == 'plain-exited'
              else marker_type('first target capture ended'))
    def signal_call(*args, **kwargs): raise marker
    broker.signal = signal_call
    waits = []
    def wait(*, timeout):
        waits.append(timeout)
        if result == 'still-live': raise subprocess.TimeoutExpired(['owned-root'], timeout)
        return 0
    process = SimpleNamespace(pid=103, poll=lambda: None, wait=wait)
    owner.process, owner._launch_broker = process, broker
    retained = ('birth-103', broker)
    owner._handles = {103: retained}
    deadline = time.monotonic() + .2
    if result == 'exit':
        owner.signal(103, 'birth-103', signal.SIGTERM, cleanup_deadline=deadline)
        assert len(waits) == 1 and 0 <= waits[0] <= .2
    else:
        with pytest.raises(type(marker)) as raised:
            owner.signal(103, 'birth-103', signal.SIGTERM, cleanup_deadline=deadline)
        assert raised.value is marker
        assert len(waits) == (1 if result == 'still-live' else 0)
    assert owner._handles[103] is retained

def test_capture_exit_after_definitely_unsuccessful_kernel_is_not_first_capture_marker(graph):
    reader = establish_anchor(graph)
    reads, kernels = [], []
    def start(pid):
        reads.append(pid)
        if len(reads) == 3: raise custody.DarwinProcessExitedError('second attempt target exited')
        return reader(pid)
    def kernel(words, sig):
        kernels.append(sig)
        raise custody.AuditTokenSignalError(3, 3)
    with pytest.raises(custody.DarwinProcessExitedError) as caught:
        fresh_fire(graph, start, kernel=kernel)
    assert not isinstance(caught.value, custody._SignalCaptureTargetGone)
    assert kernels == [signal.SIGTERM] and len(reads) == 3

@pytest.mark.parametrize('read_number', [1, 2])
def test_ordinary_descendant_does_not_consume_initial_anchor_capture_exit(observed_descendant, monkeypatch, read_number):
    owner, actors, _, tokens = _ordinary_signal_fixture(observed_descendant)
    authority, reference = owner._handles[103][1]
    start = {'pid': 103, 'uid': 501, 'start_abstime': 100, 'start_sec': 123, 'start_usec': 456, 'status': 2}
    authority.establish_start_anchor(actor=actors[101], generation=1, expected_target=reference['target'], authenticated_token=tokens[103], require_suspended=False, start_provider=lambda _: dict(start), token_provider=lambda _: dict(tokens[103]), clock_provider=lambda: 101)
    retained = ('birth-103', (authority, authority.reference()))
    owner._handles[103] = retained
    before = authority._read()
    calls = []
    def read_start(pid):
        calls.append(pid)
        if len(calls) == read_number: raise custody.DarwinProcessExitedError('descendant target exit')
        return dict(start)
    monkeypatch.setattr(custody, '_darwin_process_start', read_start)
    monkeypatch.setattr(authority, '_commit', lambda *_: pytest.fail('descendant target exit committed'))
    real_signal = authority.signal
    monkeypatch.setattr(authority, 'signal', lambda **kwargs: real_signal(**kwargs, signal_provider=lambda *_: pytest.fail('descendant target exit signalled')))
    with pytest.raises(custody._SignalCaptureTargetGone): owner.signal(103, 'birth-103', signal.SIGTERM, cleanup_deadline=0)
    assert owner._handles[103] is retained and authority._read() == before
    assert len(calls) == read_number


def test_command_inventory_actual_client_owner_accepts_only_macos_cf_addition(tmp_path, monkeypatch):
    """Real client/frame/server/policy/journal; only native auth/helper are controlled."""
    import os, socket
    from types import SimpleNamespace
    from astrid.core.execution import custody_broker as cb, process_group as pg
    monkeypatch.setattr(cb.sys, "platform", "darwin")
    checkout=tmp_path/"checkout";checkout.mkdir()
    state=tmp_path/"state";state.write_text("{}")
    git=tmp_path/"git";git.write_text("fixture-only original executable pin");git.chmod(0o700)
    launch={"PATH":str(tmp_path),"HOME":str(tmp_path)}
    requested={**launch,"__CF_USER_TEXT_ENCODING":"0x1F5:0x0:0x0"}
    policy={"state_path":str(state),"state_sha256":cb._digest_bytes(state.read_bytes()),
        "roots":{str(checkout):[str(checkout)]},"cwd":str(tmp_path),"environment":launch,
        "git":str(git),"git_sha256":cb._digest_bytes(git.read_bytes())}
    left,right,descriptor=_inventory_pipe_pair();events=[];launched=[]
    owner=cb._CommandInventoryOwner(SimpleNamespace(root=tmp_path,owner_epoch="fixture-epoch",
        process=SimpleNamespace(pid=os.getpid()),_launch_broker=SimpleNamespace(run_id="fixture-run",operation_channel=right)),policy)
    binding={"run_id":"fixture-run","owner_epoch":"fixture-epoch","generation":1,"target":{"pid":os.getpid()}}
    monkeypatch.setattr(owner,"_authenticate",lambda channel:binding)
    def pipe(value):
        read,write=os.pipe();os.write(write,value);os.close(write)
        return os.fdopen(read,"rb",buffering=0)
    class OriginalHelper:
        pid=456;returncode=0;_astrid_custody=SimpleNamespace(_observer=None)
        stdout=pipe(b"fixture-revision\n");stderr=pipe(b"")
        def poll(self):return 0
        def wait(self,timeout):events.append("original_wait");return 0
    helper=OriginalHelper()
    def spawn(argv,**kwargs):
        launched.append((argv,kwargs))
        assert kwargs["env"]==requested and kwargs["_delegate_inventory"] is False
        assert owner.pending==1
        return helper
    monkeypatch.setattr(pg,"popen_owned_process",spawn)
    monkeypatch.setattr(pg,"terminate_tree",lambda process:pytest.fail("successful fixture must not request stop"))
    monkeypatch.setattr(pg,"verify_tree_absent",lambda process:events.append("same_helper_closure") if process is helper else pytest.fail("wrong helper"))
    owner.start();owner.released.set();client=None
    try:
        client=cb._CommandInventoryClient(descriptor)
        result=client.run("revision",str(checkout),str(tmp_path),requested)
        assert result.returncode==0 and result.stdout=="fixture-revision\n" and result.stderr==""
        assert len(launched)==1 and events==["original_wait","same_helper_closure"]
        assert owner.pending is None and owner.error is None
    finally:
        if client is not None:client.channel.close()
        else:left.close()
        assert owner.done.wait(2)
        owner.finish()
        helper.stdout.close();helper.stderr.close()


@pytest.mark.parametrize("platform,case", [
    ("linux","addition"),("darwin","bound_change"),("darwin","bound_remove"),
    ("darwin","other_addition"),("darwin","mixed_addition")])
def test_command_inventory_cf_exception_never_allows_other_environment_drift(tmp_path, monkeypatch, platform, case):
    from types import SimpleNamespace
    from astrid.core.execution import custody_broker as cb
    monkeypatch.setattr(cb.sys,"platform",platform)
    root=tmp_path/"root";root.mkdir();state=tmp_path/"state";state.write_text("{}")
    launch={"PATH":str(tmp_path),"HOME":str(tmp_path)}
    if case.startswith("bound_"):launch["__CF_USER_TEXT_ENCODING"]="original"
    requested=dict(launch)
    if case=="bound_remove":requested.pop("__CF_USER_TEXT_ENCODING")
    elif case=="bound_change":requested["__CF_USER_TEXT_ENCODING"]="changed"
    elif case=="other_addition":requested["LC_CTYPE"]="C"
    else:
        requested["__CF_USER_TEXT_ENCODING"]="0x1F5:0x0:0x0"
        if case=="mixed_addition":requested["DYLD_INSERT_LIBRARIES"]="fixture-only"
    policy={"state_path":str(state),"state_sha256":cb._digest_bytes(state.read_bytes()),
        "roots":{str(root):[str(root)]},"cwd":str(tmp_path),"environment":launch}
    owner=cb._CommandInventoryOwner(SimpleNamespace(),policy)
    with pytest.raises(cb.CustodyError,match="environment differs"):
        owner._validate_policy(str(root),str(tmp_path),requested,"revision")
    assert owner.failure_stage=="policy_environment"


def _reconciliation_pair(observed_descendant, monkeypatch):
    owner, _, identities, tokens = observed_descendant
    owner, _, _ = _ordinary_census_fixture(observed_descendant)
    census = {}
    for pid, parent in ((100, 1), (103, 100), (104, 103)):
        row = dict(pid=pid, parent_pid=parent, uid=501, pgid=pid, status=2,
                   start_sec=1791433449 + pid, start_usec=pid)
        row['birth_id'] = custody._ps_lstart_birth(row['start_sec'])
        census[pid] = row
        identities[pid] = {k: row[k] for k in ('pid', 'uid', 'parent_pid', 'birth_id')}
        tokens[pid] = dict(pid=pid, uid=501, pidversion=pid + 1,
                           sha256=f'sha256:token-{pid}', words=[pid] * 8)
    owner._handles[100] = (census[100]['birth_id'], owner._launch_broker)
    live = {pid: dict(row) for pid, row in census.items()}
    def observation(pid):
        row = live.get(pid)
        if row is None:
            return {'outcome': 'ABSENT'}
        return {'outcome': 'LIVE', 'identity': dict(identities[pid]), 'native': dict(row)}
    monkeypatch.setattr(custody, 'ordinary_candidate_observation', observation)
    # These are synthetic PIDs; forbid an accidental query against host PIDs.
    monkeypatch.setattr(custody, '_darwin_process_start', lambda pid: (_ for _ in ()).throw(custody.DarwinProcessExitedError('no prospective anchor')))
    return owner, census, live, identities, tokens


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('state', ['parent-gone', 'both-gone', 'parent-live', 'reparented', 'reused', 'observation-error'])
def test_whole_census_accounts_each_precise_child_before_any_refusal(observed_descendant, monkeypatch, reverse, state):
    owner, census, live, identities, _ = _reconciliation_pair(observed_descendant, monkeypatch)
    if state in {'parent-gone', 'both-gone'}:
        live.pop(103)
    if state == 'both-gone':
        live.pop(104)
    if state == 'reparented':
        live[104]['parent_pid'] = identities[104]['parent_pid'] = 1
    if state == 'reused':
        live[104]['start_usec'] += 1
        identities[104]['birth_id'] = 'reused'
    if state == 'observation-error':
        original = custody.ordinary_candidate_observation
        monkeypatch.setattr(custody, 'ordinary_candidate_observation', lambda pid: {'outcome': 'OBSERVATION_ERROR', 'error': OSError('controlled')} if pid == 104 else original(pid))
    members = {pid: row['birth_id'] for pid, row in (reversed(list(census.items())) if reverse else census.items())}
    if state in {'both-gone', 'parent-live'}:
        result = owner.observe_census(members, census=census, fresh_census=lambda: dict(live))
        assert result == {pid: row['birth_id'] for pid, row in live.items()}
        assert owner._uncertain is None
        if state == 'both-gone':
            assert set(owner._unadmitted_absent) == {103, 104}
        else:
            assert set(owner._handles) == {100, 103, 104}
    else:
        with pytest.raises((custody.CustodyError, OSError)):
            owner.observe_census(members, census=census, fresh_census=lambda: dict(live))
        assert owner._uncertain is not None and 104 not in owner._handles
    evidence = json.loads((owner.root / 'observed-containment.json').read_text())
    recorded = {row['pid']: row for row in evidence['observed_candidates']}
    assert recorded[104]['identity'] == census[104]
    assert recorded[104]['parent_edge']['parent_pid'] == 103
    assert recorded[104]['parent_edge']['parent_identity'] == census[103]
    assert recorded[104]['capability'] == 'NONE'


@pytest.mark.parametrize('mutation', ['missing-root', 'root-symlink', 'substitution', 'child-missing', 'child-symlink', 'child-substitution', 'owner'])
def test_explicit_custody_storage_refuses_loss_or_substitution(tmp_path, monkeypatch, mutation):
    from astrid.core.execution import process_group as groups
    root = tmp_path / 'host-lifecycle'
    root.mkdir(mode=0o700)
    st = root.stat()
    binding = custody.CustodyRootBinding(root, lifecycle_identity=(st.st_dev, st.st_ino, st.st_uid), owner_epoch='a' * 64)
    monkeypatch.setattr(custody.tempfile, 'mkdtemp', lambda **kwargs: pytest.fail('confined global temp fallback'))
    owner = custody.ProcessCustody(custody_root=binding)
    assert owner.root.parent == root and owner.owner_epoch == 'a' * 64
    moved = tmp_path / 'retained'
    if mutation in {'missing-root', 'root-symlink', 'substitution'}:
        root.rename(moved)
        if mutation == 'root-symlink':
            root.symlink_to(moved, target_is_directory=True)
        elif mutation == 'substitution':
            root.mkdir(mode=0o700)
    elif mutation.startswith('child-'):
        binding.root.rename(moved)
        if mutation == 'child-symlink':
            binding.root.symlink_to(moved, target_is_directory=True)
        elif mutation == 'child-substitution':
            binding.root.mkdir(mode=0o700)
    else:
        monkeypatch.setattr(groups, 'process_birth_identity', lambda *args: 'substituted-owner')
    with pytest.raises(custody.CustodyError):
        owner.prepare_launch([sys.executable, '-c', 'pass'], start_new_session=True)
    with pytest.raises(custody.CustodyError):
        owner._write_observed_containment()
    # No creation or recursive deletion repairs an absent/substituted path.
    if mutation in {'missing-root', 'child-missing'}:
        assert not (root if mutation == 'missing-root' else binding.root).exists()


def test_owned_group_threads_explicit_binding_without_environment_selection(tmp_path, monkeypatch):
    from astrid.core.execution import process_group as groups
    root = tmp_path / 'host-lifecycle'
    root.mkdir(mode=0o700)
    st = root.stat()
    binding = custody.CustodyRootBinding(root, lifecycle_identity=(st.st_dev, st.st_ino, st.st_uid), owner_epoch='b' * 64)
    calls = []
    monkeypatch.setattr(groups, 'popen_owned_process', lambda argv, **kwargs: calls.append((argv, kwargs)))
    groups.popen_owned_group(['disposable'], custody_root=binding, env={'TMPDIR': '/untrusted-scratch'})
    assert calls[0][1]['custody_root'] is binding and calls[0][1]['start_new_session'] is True
    assert binding.root.parent == root


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin native census/capability discriminator')
@pytest.mark.parametrize('phase', ['before', 'during', 'after-child-admission'])
def test_disposable_exec_session_child_retains_obligation_across_intermediary_exit(tmp_path, monkeypatch, phase, record_property):
    import select
    import time

    from astrid.core.execution import process_group as groups
    program = '''import os, subprocess, sys
middle = None
for command in sys.stdin:
    command = command.strip()
    if command == "spawn":
        leaf_read, leaf_write = os.pipe()
        middle_read, middle_write = os.pipe()
        middle = os.fork()
        if middle == 0:
            os.close(leaf_write); os.close(middle_write)
            ready_read, ready_write = os.pipe()
            leaf = subprocess.Popen([sys.executable, "-I", "-c", "import os; os.setsid(); [os.close(fd) for fd in (0, 1, 2)]; os.write(" + str(ready_write) + ", b'r'); os.close(" + str(ready_write) + "); os.read(" + str(leaf_read) + ", 1)"], pass_fds=(leaf_read, ready_write))
            os.close(ready_write)
            assert os.read(ready_read, 1) == b'r'
            os.close(ready_read)
            os.close(leaf_read)
            print(str(os.getpid()) + " " + str(leaf.pid), flush=True)
            os.read(middle_read, 1)
            os._exit(0)
        os.close(leaf_read); os.close(middle_read)
    elif command == "middle-exit":
        os.write(middle_write, b"x"); os.close(middle_write)
        assert os.waitpid(middle, 0)[1] == 0
        print("middle-waited", flush=True)
    elif command == "leaf-exit":
        os.close(leaf_write)
        print("leaf-released", flush=True)
    elif command == "exit":
        break
'''
    lifecycle = tmp_path / 'host-lifecycle'
    lifecycle.mkdir(mode=0o700)
    st = lifecycle.stat()
    binding = custody.CustodyRootBinding(lifecycle, lifecycle_identity=(st.st_dev, st.st_ino, st.st_uid), owner_epoch='c' * 64)
    process = groups.popen_owned_group([sys.executable, '-I', '-u', '-c', program],
        custody_root=binding, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, _delegate_inventory=False)
    owner = process._astrid_custody
    middle_done = leaf_done = False
    def send(command):
        process.stdin.write(command + '\n')
        process.stdin.flush()
    def line():
        assert select.select([process.stdout], [], [], 5)[0], 'disposable control timeout'
        return process.stdout.readline().strip()
    def exit_middle():
        nonlocal middle_done
        send('middle-exit')
        assert line() == 'middle-waited'
        middle_done = True
    try:
        with owner._lock:
            send('spawn')
            middle, child = map(int, line().split())
            census = groups._census_evidence(groups._process_snapshot())
            assert census[child]['parent_pid'] == middle and census[child]['pgid'] == child
            assert census[middle]['parent_pid'] == process.pid
            assert child not in owner._handles and middle not in owner._handles
            if phase == 'before':
                exit_middle()
            original_capture = custody.current_process_audit_token
            def capture(pid):
                if pid == middle and not middle_done:
                    exit_middle()
                return original_capture(pid)
            if phase != 'before':
                monkeypatch.setattr(custody, 'current_process_audit_token', capture)
            members = {pid: census[pid]['birth_id'] for pid in
                       ((process.pid, child, middle) if phase == 'after-child-admission'
                        else (process.pid, middle, child))}
            def fresh():
                return groups._census_evidence(groups._process_snapshot())
            if phase == 'after-child-admission':
                owner.observe_census(members, census=census, fresh_census=fresh)
                assert child in owner._handles and middle not in owner._handles
                assert owner._unadmitted_absent[middle]['capability'] == 'NONE'
                assert owner._uncertain is None
            else:
                with pytest.raises(custody.CustodyError):
                    owner.observe_census(members, census=census, fresh_census=fresh)
                assert owner._uncertain is not None and child not in owner._handles
            recorded = json.loads((binding.root / 'observed-containment.json').read_text())
            children = {row['pid']: row for row in recorded['observed_candidates']}
            assert children[child]['identity'] == census[child]
            assert children[child]['parent_edge']['parent_pid'] == middle
            assert children[child]['parent_edge']['parent_identity'] == census[middle]
            assert not binding.root.is_relative_to(tmp_path / 'artifact-scratch')
    finally:
        # Cooperative fixture IPC and the original retained root wait only.
        # No numeric signal, broad process scan, renderer or real workload.
        with owner._lock:
            if not middle_done:
                exit_middle()
            if not leaf_done:
                send('leaf-exit')
                assert line() == 'leaf-released'
                leaf_done = True
            deadline = time.monotonic() + 5
            while custody.ordinary_candidate_observation(child)['outcome'] not in {'ABSENT', 'ZOMBIE'}:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            send('exit')
            process.stdin.close()
            assert process.wait(timeout=5) == 0
            assert binding.root.exists()  # Unresolved observations are retained.
        if getattr(owner, '_observer', None):
            owner._observer.join(timeout=2)
            assert not owner._observer.is_alive()
        if phase == 'after-child-admission':
            groups.verify_tree_absent(process)
            groups.release_group(process)
            assert owner._uncertain is None
        process.stdout.close()
        process.stderr.close()
        exit_thread = getattr(owner, '_exit_observer_thread', None)
        if exit_thread:
            exit_thread.join(timeout=2)
            assert not exit_thread.is_alive()
        record_property('disposable_session_topology', json.dumps({
            'phase': phase, 'root_pid': process.pid, 'root_original_wait': process.returncode,
            'middle_pid': middle, 'middle_original_wait': middle_done,
            'grandchild_pid': child, 'grandchild_own_session': census[child]['pgid'] == child,
            'grandchild_closed_stdio': True, 'grandchild_released': leaf_done,
            'custody_unresolved': owner._uncertain is not None,
            'exit_observations': [watch.facts for watch in getattr(owner, '_exit_observations', {}).values()]}))


def test_final_census_retains_new_child_of_terminal_parent_before_refusal(observed_descendant, monkeypatch):
    owner, census, live, _, _ = _reconciliation_pair(observed_descendant, monkeypatch)
    live.pop(103)
    live.pop(104)
    owner.observe_census({pid: row['birth_id'] for pid, row in census.items()},
                         census=census, fresh_census=lambda: live)
    child = {**census[104], 'pid': 105, 'parent_pid': 103, 'start_usec': 105}
    with pytest.raises(custody.CustodyError, match='unverified child'):
        owner.validate_final_census({**live, 105: child})
    evidence = json.loads((owner.root / 'observed-containment.json').read_text())
    record = next(row for row in evidence['observed_candidates'] if row['pid'] == 105)
    assert record['identity'] == child and record['parent_edge']['parent_pid'] == 103
    assert record['capability'] == 'NONE' and 105 not in owner._handles
    with pytest.raises(custody.CustodyError, match='unresolved'):
        owner.observe_census({}, census=live, fresh_census=lambda: live)


def test_requester_loss_and_cancellation_keep_recorded_child_evidence(observed_descendant, monkeypatch):
    owner, census, live, identities, _ = _reconciliation_pair(observed_descendant, monkeypatch)
    live.pop(103)
    with pytest.raises(custody.CustodyError):
        owner.observe_census({pid: row['birth_id'] for pid, row in census.items()},
                             census=census, fresh_census=lambda: live)
    retained = (owner.root / 'observed-containment.json').read_bytes()
    owner.cancel_inventory_owner()
    identities.pop(101)
    with pytest.raises(custody.CustodyError, match='unresolved'):
        owner.observe_census({}, census=live, fresh_census=lambda: live)
    assert (owner.root / 'observed-containment.json').read_bytes() == retained
    assert 104 in owner._observed_candidates and 104 not in owner._handles


def test_failed_storage_check_during_bind_keeps_original_child_obligation(tmp_path):
    lifecycle = tmp_path / 'host-lifecycle'
    lifecycle.mkdir(mode=0o700)
    st = lifecycle.stat()
    binding = custody.CustodyRootBinding(lifecycle, lifecycle_identity=(st.st_dev, st.st_ino, st.st_uid), owner_epoch='d' * 64)
    owner = custody.ProcessCustody(custody_root=binding)
    binding.root.rename(tmp_path / 'retained-custody')
    original = SimpleNamespace(pid=123456)
    with pytest.raises(custody.CustodyError):
        owner.bind_launch(original)
    assert owner.process is original and owner._uncertain is not None
    assert original._astrid_tree_uncertain


@pytest.mark.skipif(sys.platform != 'darwin', reason='requires Darwin libproc and EVFILT_PROC')
@pytest.mark.parametrize('boundary', ['registered', 'before-identity', 'before-registration', 'after-registration', 'pid-reuse'])
def test_disposable_darwin_exit_registration_boundary(tmp_path, monkeypatch, record_property, boundary):
    import time
    child = subprocess.Popen([sys.executable, '-I', '-S', '-c', 'import sys; sys.stdin.buffer.read(1)'],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    watch = None
    try:
        observed = custody.ordinary_candidate_observation(child.pid)
        assert observed['outcome'] == 'LIVE' and observed['native']['pid'] == child.pid
        original_start = custody._darwin_process_start
        calls = 0
        def started(pid):
            nonlocal calls
            calls += 1
            if boundary == 'before-identity' and calls == 1:
                child.stdin.close()
                assert child.wait(timeout=5) == 0
            if boundary == 'after-registration' and calls == 2:
                child.stdin.close()
                assert child.wait(timeout=5) == 0
            value = original_start(pid)
            if boundary == 'before-registration'  and calls == 1:
                child.stdin.close()
                assert child.wait(timeout=5) == 0
            if boundary == 'pid-reuse' and calls == 2:
                # Controlled incarnation counterexample; no PID churn or claim
                # that Darwin actually recycled a PID during this bounded run.
                value = {**value, 'start_usec': value['start_usec'] + 1}
            return value
        monkeypatch.setattr(custody, '_darwin_process_start', started)
        watch = custody._DarwinExitObservation(observed)
        if boundary == 'registered':
            assert watch.facts['outcome'] == 'REGISTERED', watch.facts
            child.stdin.close()
            assert child.wait(timeout=5) == 0
            deadline = time.monotonic() + 2
            while not watch.poll() and watch.facts['outcome'] == 'REGISTERED':
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert watch.facts['outcome'] == 'EXIT_OBSERVED', watch.facts
        elif boundary == 'after-registration':
            assert watch.facts['outcome'] == 'EXIT_EVENT_UNBOUND', watch.facts
            assert watch.facts['event']['pid'] == child.pid
            assert 'registration_bound' not in watch.facts
        elif boundary == 'pid-reuse':
            assert watch.facts['outcome'] == 'INCARNATION_CHANGED'
        else:
            assert watch.facts['outcome'] == 'REGISTRATION_UNAVAILABLE'
            assert 'event' not in watch.facts  # Absence is never invented exit.
        assert watch.facts['authority'] == 'NONE'
        record_property('darwin_registration', json.dumps({'boundary': boundary, **watch.facts}))
    finally:
        if watch:
            watch.close()
        if not child.stdin.closed:
            child.stdin.close()
        assert child.wait(timeout=5) == 0


@pytest.mark.skipif(sys.platform != 'darwin', reason='requires actual Darwin libproc and kqueue')
def test_disposable_kernel_exit_evidence_survives_optional_mach_failure(tmp_path, monkeypatch, record_property):
    child = subprocess.Popen([sys.executable, '-I', '-S', '-c', 'import sys; sys.stdin.buffer.read(1)'],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    owner = custody.ProcessCustody(scope_root=tmp_path / 'custody')
    owner.root.mkdir(mode=0o700)
    try:
        observed = custody.ordinary_candidate_observation(child.pid)
        assert observed['outcome'] == 'LIVE'
        native_capture = 'AVAILABLE'
        try:
            custody.current_process_audit_token(child.pid)
        except custody.NativeTokenCaptureError as error:
            native_capture = error.capture_failure
        owner._observe_candidate_exits({child.pid: observed})
        def denied(pid):
            assert pid == child.pid
            raise custody.NativeTokenCaptureError('injected optional Mach denial',
                substage='task_name_for_pid', kern_return=5, port_acquired=False)
        monkeypatch.setattr(custody, 'current_process_audit_token', denied)
        with pytest.raises(custody.CustodyError, match='token-capture'):
            owner.register(child.pid, observed['identity']['birth_id'], _observation=observed)
        assert child.pid not in owner._handles
        assert custody.ordinary_candidate_observation(child.pid)['native'] == observed['native']
        child.stdin.close()
        assert child.wait(timeout=5) == 0
        owner._exit_observer_thread.join(timeout=3)
        assert not owner._exit_observer_thread.is_alive()
        evidence = json.loads((owner.root / 'observed-containment.json').read_text())
        event = evidence['exit_observations'][0]
        assert event['outcome'] == 'EXIT_OBSERVED', event
        assert event['authority'] == 'NONE' and event['event']['pid'] == child.pid
        with pytest.raises(custody.CustodyError):
            owner.signal(child.pid, observed['identity']['birth_id'], signal.SIGTERM)
        record_property('darwin_optional_control', json.dumps({
            'native_capture': native_capture, 'denial_test': 'INJECTED_AT_MACH_CAPTURE_BOUNDARY',
            'kernel_exit_event': event, 'original_wait': child.returncode}))
    finally:
        if not child.stdin.closed:
            child.stdin.close()
        assert child.wait(timeout=5) == 0
        for watch in getattr(owner, '_exit_observations', {}).values():
            watch.close()
