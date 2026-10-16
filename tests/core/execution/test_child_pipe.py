"""Focused child pipe bootstrap, endpoint and lifetime regressions."""

import copy
import hashlib
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import _child_pipe as pipe
from astrid.core.execution._child_bridge import ChildBridgeError, HostChildBridge
from astrid.core.execution.process_group import process_birth_identity


def binding(channel, *, pid=None, birth=None, parent=None):
    return dict(
        protocol=pipe.VERSION,
        context=dict(
            task_id="task",
            run_id="run",
            project_id="project",
            attempt_id="attempt",
            fence=1,
            runtime_epoch=1,
        ),
        executor_id="executor",
        pid=os.getpid() if pid is None else pid,
        birth=process_birth_identity() if birth is None else birth,
        parent_pid=os.getppid() if parent is None else parent,
        launch_digest="a" * 64,
        pipes=[
            pipe.pipe_identity(channel.read_fd, os.O_RDONLY),
            pipe.pipe_identity(channel.write_fd, os.O_WRONLY),
        ],
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", -1),
        ("parent_pid", -1),
        ("birth", "stale"),
        ("protocol", "wrong"),
        ("launch_digest", "wrong"),
        ("executor_id", ""),
        ("authority", {"kind": "host_owned"}),
    ],
)
def test_wrong_bootstrap_identity_is_refused(field, value):
    host, child = pipe.pipe_pair()
    try:
        selected = binding(child)
        selected[field] = value
        with pytest.raises(ValueError):
            pipe.validate_binding(selected, child)
    finally:
        host.close()
        child.close()


@pytest.mark.parametrize(
    "field,value",
    [("fence", 0), ("runtime_epoch", True), ("attempt_id", ""), ("spec", {}), ("environment", {})],
)
def test_caller_authority_and_invalid_attempt_shape_are_refused(field, value):
    host, child = pipe.pipe_pair()
    try:
        selected = binding(child)
        selected["context"][field] = value
        with pytest.raises(ValueError):
            pipe.validate_binding(selected, child)
    finally:
        host.close()
        child.close()


def test_pipe_substitution_and_reversed_direction_are_refused(tmp_path):
    host, child = pipe.pipe_pair()
    left, right = socket.socketpair()
    ordinary = tmp_path / "ordinary"
    ordinary.write_text("synthetic")
    fd = os.open(ordinary, os.O_RDONLY)
    try:
        selected = binding(child)
        pipe.validate_binding(selected, child)
        changed = copy.deepcopy(selected)
        changed["pipes"][0]["device"] += 1
        with pytest.raises(ValueError):
            pipe.validate_binding(changed, child)
        for wrong, direction in (
            (left.fileno(), os.O_RDONLY),
            (fd, os.O_RDONLY),
            (child.read_fd, os.O_WRONLY),
            (child.write_fd, os.O_RDONLY),
        ):
            with pytest.raises(ValueError):
                pipe.pipe_identity(wrong, direction)
    finally:
        os.close(fd)
        left.close()
        right.close()
        host.close()
        child.close()


@pytest.mark.parametrize("stale", [False, True])
def test_actual_consumer_checks_bootstrap_before_code_and_closes_endpoints(tmp_path, stale):
    host, child = pipe.pipe_pair()
    child_fds = (child.read_fd, child.write_fd)
    marker = tmp_path / "workload"
    from astrid.core.execution.custody_broker import _CHILD_BRIDGE_CONSUMER

    endpoints = [
        dict(fd=fd, **pipe.pipe_identity(fd, direction))
        for fd, direction in ((child.read_fd, os.O_RDONLY), (child.write_fd, os.O_WRONLY))
    ]
    wire = dict(
        endpoints=endpoints,
        purpose="child_bridge",
        role="command",
        argv_digest="sha256:" + "a" * 64,
        owner_epoch="b" * 64,
        run_id="run",
        claim_digest="c" * 64,
    )
    script = (
        "import sys;sys.path.insert(0,sys.argv[1]);"
        "from astrid.core.execution._child_pipe import install_bridge;"
        "from pathlib import Path;b=install_bridge(sys.argv[3]);"
        "print('ordinary diagnostic',flush=True);Path(sys.argv[2]).write_text('ran');b.close()"
    )
    import json

    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            script,
            str(Path(__file__).resolve().parents[3]),
            str(marker),
            json.dumps(list(child_fds)),
        ],
        pass_fds=child_fds,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, _CHILD_BRIDGE_CONSUMER: json.dumps(wire)},
    )
    try:
        selected = binding(
            child, pid=process.pid, birth=process_birth_identity(process.pid), parent=os.getpid()
        )
        selected["launch_digest"] = hashlib.sha256(pipe.encode(wire)).hexdigest()
        if stale:
            selected["birth"] = "stale"
        digest = hashlib.sha256(pipe.encode(selected)).hexdigest()
        child.close()
        host.settimeout(5)
        host.sendall(pipe.encode(dict(binding=selected, digest=digest)))
        if stale:
            assert host.recv(4096) == b""
        else:
            assert pipe.read_frame(host) == {"ready": digest}
            host.sendall(pipe.encode({"accepted": digest}))
            assert host.recv(4096) == b""
        output, error = process.communicate(timeout=5)
        assert (process.returncode == 0) is not stale
        assert marker.exists() is not stale
        if not stale:
            assert b"ordinary diagnostic" in output
            assert error == b""
        for fd in child_fds:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        host.close()
        child.close()
        process.stdout.close()
        process.stderr.close()


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}\n', b"{}\n{}\n", b"[]\n", b"x" * (pipe.LIMIT + 1)])
def test_malformed_bootstrap_is_bounded_and_refused(raw):
    class Channel:
        def recv(self, size):
            nonlocal raw
            part, raw = raw[:size], raw[size:]
            return part

    with pytest.raises((ValueError, EOFError)):
        pipe.read_frame(Channel())


@pytest.fixture
def bridge(tmp_path):
    task = dict(
        id="task",
        run_id="run",
        project_id="project",
        attempt_id="attempt",
        state="running",
        runtime_epoch=1,
        spec={"child_delegation": {"limits": {}}},
    )
    host = SimpleNamespace(client=SimpleNamespace(task=lambda _: task), executor_id="executor")
    host._attempt_claim_authority = SimpleNamespace(
        fields=tuple(
            dict(
                task_id="task",
                project_id="project",
                attempt_id="attempt",
                lease_id="lease",
                fence=1,
                runtime_epoch=1,
                executor_id="executor",
            ).items()
        )
    )
    instance = HostChildBridge(
        host,
        task,
        attempt_id="attempt",
        lease_id="lease",
        fence=1,
        runtime_epoch=1,
        output_root=tmp_path / "outputs",
        cancelled=lambda: False,
    )
    try:
        yield instance, task
    finally:
        instance.revoke()


@pytest.mark.parametrize("selection", ["extra", "wrong", "socket", "duplicate"])
def test_arbitrary_inherited_descriptors_never_select_bridge(bridge, selection):
    from astrid.core.execution.process_group import CleanupUncertainError, popen_owned_process

    instance, _ = bridge
    left, right = socket.socketpair()
    try:
        fd = left.fileno() if selection == "socket" else instance.child_channel.read_fd
        descriptors = (fd, fd) if selection == "duplicate" else (fd,)
        with pytest.raises(CleanupUncertainError):
            popen_owned_process(
                [sys.executable, "-c", 'raise AssertionError("workload")'], pass_fds=descriptors
            )
        assert instance._thread.ident is None
    finally:
        left.close()
        right.close()


def test_launch_binding_and_closed_pipes_never_revive(bridge):
    instance, _ = bridge
    selected = instance.launch_binding()
    assert selected.descriptors == (instance.child_channel.read_fd, instance.child_channel.write_fd)
    fds = (
        instance.channel.read_fd,
        instance.channel.write_fd,
        instance.child_channel.read_fd,
        instance.child_channel.write_fd,
    )
    with pytest.raises(ChildBridgeError):
        instance.launch_binding()
    instance.revoke()
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    with pytest.raises(ValueError):
        selected.verify()
    assert instance._attempt_fd is None


@pytest.mark.parametrize(
    "field,value", [("attempt_id", "another"), ("state", "cancelled"), ("runtime_epoch", 2)]
)
def test_stale_attempt_is_refused_before_launch_and_closes_pipes(bridge, field, value):
    instance, task = bridge
    selected = instance.launch_binding()
    task[field] = value
    with pytest.raises(ChildBridgeError):
        selected.verify()
    assert instance._thread.ident is None
    assert instance._attempt_fd is None


def test_binding_detects_closed_and_reused_pipe_number(bridge):
    instance, _ = bridge
    selected = instance.launch_binding()
    fd = selected.descriptors[0]
    retained = os.dup(fd)
    replacement = os.pipe()
    try:
        os.dup2(replacement[0], fd)
        with pytest.raises(ValueError, match="replaced"):
            selected.verify()
    finally:
        os.dup2(retained, fd)
        for owned in (retained, *replacement):
            os.close(owned)


def test_missing_authenticated_projection_cannot_create_binding(bridge):
    instance, _ = bridge
    instance.host._attempt_claim_authority = None
    with pytest.raises(ValueError, match="authenticated host claim"):
        instance.launch_binding()
    assert instance._thread.ident is None


@pytest.mark.parametrize(
    "field",
    ["task_id", "attempt_id", "lease_id", "fence", "runtime_epoch", "project_id", "executor_id"],
)
def test_changed_authenticated_claim_never_launches(bridge, field):
    instance, _ = bridge
    fields = {**instance.context, "executor_id": instance.host.executor_id}
    instance.host._attempt_claim_authority = SimpleNamespace(fields=tuple(fields.items()))
    selected = instance.launch_binding()
    fields[field] = "foreign"
    instance.host._attempt_claim_authority = SimpleNamespace(fields=tuple(fields.items()))
    with pytest.raises(ValueError, match="authenticated claim"):
        selected.verify()
    assert instance._thread.ident is None


@pytest.mark.parametrize("field", ["argv", "cwd", "env", "stdin", "stdout", "close_fds", "session"])
def test_bound_executable_and_launch_options_cannot_be_replaced(bridge, tmp_path, field):
    from astrid.core.execution.generic_host import _ConfinedLaunchPlan

    instance, _ = bridge
    selected = instance.launch_binding()
    argv = [sys.executable, "-c", "raise AssertionError('workload')"]
    plan = _ConfinedLaunchPlan(tuple(argv), tmp_path, (("LC_ALL", "C"),), selected.descriptors)
    selected.select(plan.argv, str(plan.cwd), plan.environment, plan.pass_fds)
    options = plan.subprocess_options()
    options["pass_fds"] = ()
    session = True
    if field == "argv":
        argv = [sys.executable, "-c", "pass"]
    elif field == "session":
        session = False
    else:
        options[field] = "foreign"
    with pytest.raises(ValueError, match="admitted executable"):
        selected.verify_launch(argv, options, new_session=session)


def test_dictionary_or_extra_fd_cannot_create_launch_authority(bridge):
    from astrid.core.execution.process_group import CleanupUncertainError, popen_owned_group

    instance, _ = bridge
    with pytest.raises(CleanupUncertainError):
        popen_owned_group(
            [sys.executable, "-c", 'raise AssertionError("workload")'],
            _child_bridge_launch={"purpose": "child_bridge"},
        )
    with pytest.raises(CleanupUncertainError):
        popen_owned_group(
            [sys.executable, "-c", 'raise AssertionError("workload")'],
            _child_bridge_launch=instance.launch_binding(),
            pass_fds=(instance.child_channel.read_fd,),
        )


def test_reply_backpressure_times_out_and_closes_every_endpoint():
    host, child = pipe.pipe_pair()
    fds = (host.read_fd, host.write_fd, child.read_fd, child.write_fd)
    try:
        host.settimeout(0.02)
        with pytest.raises(TimeoutError):
            host.sendall(b"x" * 1048576)
    finally:
        host.close()
        child.close()
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize(
    "field", ["authority", "spec", "env", "lease_id", "attempt_id", "executor_id"]
)
def test_caller_authority_never_crosses_sdk_frame(bridge, field):
    instance, _ = bridge
    request = dict(v=1, request_id=1, op="status", child_key="child", task_id="child-task")
    request[field] = {"kind": "host_owned"}
    with pytest.raises(ChildBridgeError, match="invalid child bridge frame"):
        instance.dispatch(request)
    assert instance._revoked.is_set()
    assert instance._children == {}
    assert instance._attempt_fd is None


@pytest.mark.parametrize(
    "fault", ["role", "purpose", "missing", "extra", "swapped", "claim", "argv", "epoch"]
)
def test_incomplete_or_foreign_custody_bootstrap_is_refused(bridge, fault):
    from astrid.core.execution.custody_broker import CustodyError, _validate_child_bridge_binding

    instance, _ = bridge
    selected = instance.launch_binding()
    wire = dict(
        endpoints=selected.endpoints,
        purpose="child_bridge",
        role="command",
        argv_digest="sha256:" + "a" * 64,
        owner_epoch="b" * 64,
        run_id="run",
        claim_digest="c" * 64,
    )
    if fault == "missing":
        wire.pop("owner_epoch")
    elif fault == "extra":
        wire["authority"] = "foreign"
    elif fault == "swapped":
        wire["endpoints"] = wire["endpoints"][::-1]
    else:
        wire[
            {
                "role": "role",
                "purpose": "purpose",
                "claim": "claim_digest",
                "argv": "argv_digest",
                "epoch": "owner_epoch",
            }[fault]
        ] = "foreign"
    with pytest.raises(CustodyError):
        _validate_child_bridge_binding(wire)
    assert instance._thread.ident is None
