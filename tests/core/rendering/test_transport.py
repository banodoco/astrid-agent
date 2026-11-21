from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astrid.core.execution import custody_broker, process_group
from astrid.core.pack import source_setup
from astrid.core.rendering import RenderPlan, RenderResult, SupportReport
from astrid.core.rendering import transport as transport_module
from astrid.core.rendering.errors import (
    RendererBinaryMissingError,
    RendererInternalError,
    RendererProtocolError,
    RendererTimeoutError,
)
from astrid.core.rendering.transport import CommandTransport, _resolve_executable

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
WIRE_FIXTURE_DIR = FIXTURE_DIR / "v1"
BACKEND_SCRIPT = FIXTURE_DIR / "transport_backend.py"
RENDERER_ID = "acme.visual"


def test_absolute_executable_symlink_is_preserved(tmp_path: Path) -> None:
    executable = tmp_path / "python"
    executable.symlink_to(sys.executable)

    resolved = _resolve_executable(
        str(executable),
        cwd=tmp_path,
        child_env={"PATH": os.defpath},
        backend=RENDERER_ID,
    )

    assert resolved == str(executable)


def _wire_fixture(name: str) -> dict[str, Any]:
    return json.loads((WIRE_FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _request(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps({**payload, "launch_gate": str(tmp_path / "launch.gate")}), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _backend_waits_for_custody(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the fixture backend until custody has made its first observation of it.

    A renderer that exits first is the Linux custody gap owned in
    the custody broker (no stopped launch); under CPU load every instantly
    exiting backend here hit it. These tests are about the transport's wire and
    lifecycle behaviour, so the backend (see ``launch_gate`` in
    fixtures/transport_backend.py) starts only once ``popen_owned_process`` returns.
    """
    launch = transport_module.popen_owned_process

    def gated(argv, *args, **kwargs):
        process = launch(argv, *args, **kwargs)
        argv = [str(part) for part in argv]
        if "--request" in argv:
            request = Path(argv[argv.index("--request") + 1])
            try:
                gate = json.loads(request.read_text(encoding="utf-8")).get("launch_gate")
            except (OSError, ValueError):
                gate = None
            if gate:
                Path(gate).touch()
        return process

    monkeypatch.setattr(transport_module, "popen_owned_process", gated)


def _run(
    tmp_path: Path,
    payload: dict[str, Any],
    *,
    verb: str = "render",
    backend: str = RENDERER_ID,
    timeout: float | None = 5,
    env: dict[str, str] | None = None,
    transport: CommandTransport | None = None,
):
    selected = transport or CommandTransport(backend, termination_grace=0.15)
    result_path = tmp_path / "result.json"
    value = selected.run(
        verb,
        [sys.executable, BACKEND_SCRIPT],
        request_path=_request(tmp_path, payload),
        result_path=result_path,
        cwd=FIXTURE_DIR,
        env=env,
        timeout=timeout,
    )
    return selected, value


def test_test_harness_isolates_pack_source_inventory(tmp_path: Path) -> None:
    """Transport tests must not inherit the developer's installed-source registry."""
    source_state = source_setup.source_state_path()
    assert source_state.is_relative_to(tmp_path)
    assert not source_state.exists()
    assert Path(os.environ["HOME"]).is_relative_to(tmp_path)
    assert Path(os.environ["XDG_STATE_HOME"]).is_relative_to(tmp_path)
    fake_bin = tmp_path / "empty-bin"
    fake_bin.mkdir()
    environment = dict(os.environ, PATH=str(fake_bin))
    assert custody_broker._inventory_launch_policy(environment, str(tmp_path)) is None


def _assert_pid_disappears(pid: int, *, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    pytest.fail(f"process {pid} survived process-group cleanup")


def _tree_request(tmp_path: Path) -> tuple[dict[str, Any], Path, Path]:
    parent_pid_path = tmp_path / "parent.pid"
    child_pid_path = tmp_path / "child.pid"
    return (
        {
            "action": "sleep-tree",
            "ignore_term": True,
            "parent_pid_path": str(parent_pid_path),
            "child_pid_path": str(child_pid_path),
        },
        parent_pid_path,
        child_pid_path,
    )


def test_successful_render_uses_authoritative_result_file(tmp_path: Path) -> None:
    transport, result = _run(
        tmp_path,
        {"action": "result", "payload": _wire_fixture("result.json")},
    )

    assert isinstance(result, RenderResult)
    assert result.video.path == "outputs/visual.mp4"
    assert transport.last_logs == {"stdout": "", "stderr": ""}


def test_success_waits_for_owned_cleanup_after_detached_leader_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A success DTO is not returned until an observed escaped worker is gone.

    This is deliberately a pure transport orchestration test. Native custody
    admission and per-process signalling are exercised by the integration test
    below, but this test proves the success path waits at the cleanup barrier.
    """
    events: list[tuple[str, Any]] = []
    result_path = tmp_path / "result.json"

    class ExitedLeader:
        pid = 91801
        returncode = 0
        _astrid_process_group_id = pid

        def poll(self):
            return self.returncode

        def communicate(self, *, timeout):
            events.append(("communicate", timeout))
            if not result_path.exists():
                result_path.write_text(
                    json.dumps(_wire_fixture("result.json")), encoding="utf-8"
                )
            return "", ""

    process = ExitedLeader()

    def launch(_argv, **_kwargs):
        events.append(("launch", process.pid))
        return process

    def observe(owned, **_kwargs):
        assert owned is process
        events.append(("observe", process.pid))
        # The known worker is in a new session and has already outlived the
        # leader. Its birth-fenced identity is what retained cleanup consumes.
        return {91802: "worker-birth-identity"}

    def terminate_tree(owned, **_kwargs):
        assert owned is process
        events.append(("terminate_tree", process.pid))

    def terminate_group(owned, **_kwargs):
        assert owned is process
        events.append(("terminate_group", process.pid))

    def verify_absent(owned, **_kwargs):
        assert owned is process
        assert ("terminate_tree", process.pid) in events
        assert ("terminate_group", process.pid) in events
        events.append(("verify_absent", process.pid))

    monkeypatch.setattr(transport_module, "_resolve_executable", lambda value, **_: str(value))
    monkeypatch.setattr(transport_module, "popen_owned_process", launch)
    monkeypatch.setattr(transport_module, "observe_tree", observe)
    monkeypatch.setattr(transport_module, "terminate_tree", terminate_tree)
    monkeypatch.setattr(transport_module, "terminate_group", terminate_group)
    monkeypatch.setattr(transport_module, "verify_tree_absent", verify_absent)
    monkeypatch.delenv("ASTRID_RENDER_INHERIT_PROCESS_GROUP", raising=False)

    result = CommandTransport(RENDERER_ID).run(
        "render", [sys.executable, BACKEND_SCRIPT],
        request_path=_request(tmp_path, {"action": "detached-success"}),
        result_path=result_path, cwd=FIXTURE_DIR, timeout=5,
    )

    assert isinstance(result, RenderResult)
    verify_index = events.index(("verify_absent", process.pid))
    final_communicate_index = max(i for i, event in enumerate(events) if event[0] == "communicate")
    assert verify_index < final_communicate_index
    assert final_communicate_index == len(events) - 1


@pytest.mark.posix_native  # POSIX-only process-group API
def test_detached_worker_is_cleaned_after_successful_leader_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native custody integration: worker exits only after transport cleanup.

    The fixture's leader writes the successful protocol result and exits after
    the test confirms custody observed its worker in a separate session. The
    worker ignores TERM and keeps writing, so the normal-success cleanup path
    must retain and KILL that exact incarnation before returning the DTO.
    """
    registry = tmp_path / "detached-identities"
    release_gate = tmp_path / "release-leader.gate"
    payload = {
        "action": "detached-success",
        "leader_release_gate": str(release_gate),
        "payload": _wire_fixture("result.json"),
    }
    sibling = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    original_launch = transport_module.popen_owned_process
    original_observe = transport_module.observe_tree
    original_verify = transport_module.verify_tree_absent
    original_killpg = os.killpg
    observed: list[subprocess.Popen[str]] = []
    verified: list[int] = []
    released = False

    def launch_after_observation(argv, *args, **kwargs):
        process = original_launch(argv, *args, **kwargs)
        observed.append(process)
        argv_text = [str(part) for part in argv]
        request = json.loads(
            Path(argv_text[argv_text.index("--request") + 1]).read_text(encoding="utf-8")
        )
        pid_path = registry / "detached-worker.pid"
        ready_path = registry / "detached-worker.ready"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            live = original_observe(process)
            if pid_path.is_file() and ready_path.is_file():
                worker_pid = int(pid_path.read_text(encoding="utf-8"))
                if process.pid in live and worker_pid in live:
                    Path(request["leader_release_gate"]).touch()
                    nonlocal released
                    released = True
                if released and process.poll() is not None:
                    # Once the leader has exited, the worker must remain known
                    # through retained ancestry evidence, despite setsid().
                    assert worker_pid in original_observe(process)
                    return process
            if process.poll() is not None and not released:
                pytest.fail("renderer leader exited before detached worker was observed")
            time.sleep(0.01)
        pytest.fail("detached worker did not reach the custody-observed state")

    def verify_after_cleanup(process):
        original_verify(process)
        verified.append(process.pid)

    def guarded_killpg(pgid, signum):
        assert pgid != os.getpgrp(), "renderer cleanup signalled the host process group"
        return original_killpg(pgid, signum)

    monkeypatch.setattr(transport_module, "popen_owned_process", launch_after_observation)
    monkeypatch.setattr(transport_module, "verify_tree_absent", verify_after_cleanup)
    monkeypatch.setattr(os, "killpg", guarded_killpg)
    try:
        assert sibling.poll() is None
        result = CommandTransport(RENDERER_ID, termination_grace=0.05).run(
            "render",
            [sys.executable, FIXTURE_DIR / "owned_tree_backend.py", "detached-success", tmp_path, registry],
            request_path=_request(tmp_path, payload),
            result_path=tmp_path / "detached-result.json",
            cwd=FIXTURE_DIR,
            timeout=5,
        )
        assert isinstance(result, RenderResult)
        assert len(observed) == 1 and observed[0].returncode == 0
        assert verified == [observed[0].pid]
        with pytest.raises(ChildProcessError):
            os.waitpid(observed[0].pid, os.WNOHANG)
        worker_pid = int((registry / "detached-worker.pid").read_text(encoding="utf-8"))
        _assert_pid_disappears(worker_pid)
        last_write = (registry / "detached-worker.last-write").read_text(encoding="utf-8")
        time.sleep(0.1)
        assert (registry / "detached-worker.last-write").read_text(encoding="utf-8") == last_write
        assert sibling.poll() is None
        assert os.getpid() in process_group._process_snapshot()
    finally:
        try:
            for process in observed:
                transport_module._terminate_process_group(process, grace=0.05)
        finally:
            sibling.terminate()
            sibling.wait(timeout=3)


@pytest.mark.parametrize("verb", ["render", "support"])
@pytest.mark.parametrize("timeout_mode", ["omitted", "explicit_none"])
def test_unbounded_timeout_keeps_polling_and_parses_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    timeout_mode: str,
) -> None:
    observed = []
    original_observe = transport_module.observe_tree

    release_gate = tmp_path / "release.gate"

    def record_observation(process, **kwargs):
        observed.append((process.pid, kwargs["deadline"]))
        live = original_observe(process, **kwargs)
        if len(observed) >= 2:
            release_gate.touch()  # the backend may finish only after two polls
        return live

    monkeypatch.setattr(transport_module, "observe_tree", record_observation)
    payload_name = "support.json" if verb == "support" else "result.json"
    kwargs = {"timeout": None} if timeout_mode == "explicit_none" else {}
    result = CommandTransport(RENDERER_ID).run(
        verb,
        [sys.executable, BACKEND_SCRIPT],
        request_path=_request(
            tmp_path,
            {
                "action": "delayed-result",
                "release_gate": str(release_gate),
                "payload": _wire_fixture(payload_name),
            },
        ),
        result_path=tmp_path / "result.json",
        cwd=FIXTURE_DIR,
        **kwargs,
    )

    expected_type = SupportReport if verb == "support" else RenderResult
    assert isinstance(result, expected_type)
    assert len(observed) >= 2
    assert all(deadline is None for _, deadline in observed)


def test_bare_python3_uses_the_runtime_interpreter_not_child_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    fake_python.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))

    result = CommandTransport(RENDERER_ID).run(
        "render",
        ["python3", BACKEND_SCRIPT],
        request_path=_request(
            tmp_path,
            {"action": "result", "payload": _wire_fixture("result.json")},
        ),
        result_path=tmp_path / "result.json",
        cwd=FIXTURE_DIR,
        timeout=5,
    )

    assert isinstance(result, RenderResult)


@pytest.mark.parametrize("command_name", ["python3.11", "custom-python"])
def test_explicit_or_versioned_python_command_is_not_rewritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_name: str,
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_python = fake_bin / command_name
    fake_python.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    fake_python.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    command = str(fake_python) if command_name == "custom-python" else command_name

    with pytest.raises(RendererInternalError) as caught:
        CommandTransport(RENDERER_ID).run(
            "render",
            [command, BACKEND_SCRIPT],
            request_path=_request(tmp_path, {"action": "absent"}),
            result_path=tmp_path / "result.json",
            cwd=FIXTURE_DIR,
            timeout=5,
        )

    assert caught.value.details["returncode"] == 97


@pytest.mark.parametrize(
    ("verb", "fixture_name", "backend", "result_type"),
    [
        ("support", "support.json", "acme.visual", SupportReport),
        ("plan", "plan.json", "fixture.planner", RenderPlan),
        ("finalize", "result.json", "rendering.ffmpeg-finalizer", RenderResult),
    ],
)
def test_each_protocol_verb_uses_its_frozen_result_dto(
    tmp_path: Path,
    verb: str,
    fixture_name: str,
    backend: str,
    result_type: type,
) -> None:
    _, result = _run(
        tmp_path,
        {"action": "result", "payload": _wire_fixture(fixture_name)},
        verb=verb,
        backend=backend,
    )

    assert isinstance(result, result_type)


def test_missing_binary_is_renderer_qualified(tmp_path: Path) -> None:
    request_path = _request(tmp_path, {"action": "absent"})

    with pytest.raises(RendererBinaryMissingError) as caught:
        CommandTransport(RENDERER_ID).run(
            "render",
            ["astrid-renderer-that-does-not-exist"],
            request_path=request_path,
            result_path=tmp_path / "result.json",
            cwd=FIXTURE_DIR,
            timeout=1,
        )

    assert caught.value.error.kind == "binary_missing"
    assert caught.value.error.backend == RENDERER_ID


def test_nested_child_failure_surfaces_stderr_and_exit_code(tmp_path: Path) -> None:
    with pytest.raises(RendererInternalError) as caught:
        _run(tmp_path, {"action": "child-failure"})

    assert "Browser not available" in str(caught.value)
    assert "exit 23" in str(caught.value)
    assert "Browser not available" in caught.value.to_dict()["details"]["stderr"]
    assert "exit 23" in caught.value.to_dict()["message"]


def test_nonzero_exit_is_internal_and_captures_both_streams(tmp_path: Path) -> None:
    with pytest.raises(RendererInternalError) as caught:
        _run(
            tmp_path,
            {
                "action": "nonzero",
                "returncode": 23,
                "stdout": "renderer stdout",
                "stderr": "renderer stderr",
            },
        )

    assert caught.value.error.kind == "internal"
    assert caught.value.error.backend == RENDERER_ID
    assert caught.value.details["returncode"] == 23
    assert "renderer stdout" in caught.value.details["stdout"]
    assert "renderer stderr" in caught.value.details["stderr"]


def test_npm_diagnostics_are_filtered_before_bounded_tail(tmp_path: Path) -> None:
    warnings = "".join(f"npm warn dependency-{index}: noisy package warning\n" for index in range(30))
    with pytest.raises(RendererInternalError) as caught:
        _run(
            tmp_path,
            {
                "action": "nonzero",
                "returncode": 23,
                "stdout": warnings + "stdout real error\n",
                "stderr": warnings + "stderr real error\n",
            },
        )

    assert "stderr real error" in str(caught.value)
    details = caught.value.to_dict()["details"]
    for stream in ("stdout", "stderr"):
        assert f"{stream} real error" in details[stream]
        assert "npm warn" not in details[stream]
        assert len(details[stream].splitlines()) <= 40
        assert len(details[stream].encode("utf-8")) <= 8 * 1024


def test_nested_child_streams_are_filtered_and_redacted(tmp_path: Path) -> None:
    warnings = "".join(f"npm warn package {index}\n" for index in range(30))
    with pytest.raises(RendererInternalError) as caught:
        _run(tmp_path, {
            "action": "noisy-child-failure",
            "stdout_child": warnings + "stdout error token=secret-value\n",
            "stderr_child": "stderr error\n" + warnings + "npm notice upgrade\n",
        })
    details = caught.value.to_dict()["details"]
    assert "stdout error" in str(caught.value)
    assert "stderr error" in str(caught.value)
    assert "secret-value" not in str(caught.value)
    assert "secret-value" not in details["stdout"]
    assert "npm warn" not in details["stdout"]
    assert "npm warn" not in details["stderr"]
    assert "npm notice" not in details["stderr"]


@pytest.mark.posix_native  # POSIX-only process-group API
def test_timeout_kills_process_group_and_reaps_direct_child(tmp_path: Path, monkeypatch) -> None:
    payload, parent_pid_path, child_pid_path = _tree_request(tmp_path)
    deadlines = []
    original_observe = transport_module.observe_tree
    def record_deadline(process, **kwargs):
        deadlines.append(kwargs["deadline"])
        return original_observe(process, **kwargs)
    monkeypatch.setattr(transport_module, "observe_tree", record_deadline)

    with pytest.raises(RendererTimeoutError) as caught:
        _run(tmp_path, payload, timeout=0.5)

    # Renderer expiry must not cancel an in-flight custody transaction.
    assert deadlines and all(deadline is None for deadline in deadlines)
    parent_pid = int(parent_pid_path.read_text(encoding="utf-8"))
    assert caught.value.error.kind == "timeout"
    assert caught.value.error.backend == RENDERER_ID
    with pytest.raises(ChildProcessError):
        os.waitpid(parent_pid, os.WNOHANG)
    _assert_pid_disappears(parent_pid)


def test_sigterm_ignoring_child_is_escalated_and_reaped(tmp_path: Path) -> None:
    """A child tree that ignores SIGTERM must still be SIGKILLed and reaped."""
    payload, parent_pid_path, child_pid_path = _tree_request(tmp_path)

    with pytest.raises(RendererTimeoutError) as caught:
        _run(tmp_path, payload, timeout=0.5)

    assert caught.value.error.kind == "timeout"
    parent_pid = int(parent_pid_path.read_text(encoding="utf-8"))
    _assert_pid_disappears(parent_pid)


@pytest.mark.parametrize("timeout", [10, None])
@pytest.mark.posix_native  # POSIX-only process-group API
def test_sigint_kills_process_group_reaps_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: float | None
) -> None:
    payload, parent_pid_path, child_pid_path = _tree_request(tmp_path)
    original_observe = transport_module.observe_tree
    interrupted = []

    def interrupt_after_capture(process, **kwargs):
        live = original_observe(process, **kwargs)
        if not interrupted and child_pid_path.exists():
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            if process.pid in live and child_pid in live:
                interrupted.append(True)
                # Deliver SIGINT synchronously after ancestry has been
                # captured, rather than racing the census subprocess itself.
                os.kill(os.getpid(), signal.SIGINT)
        return live

    monkeypatch.setattr(transport_module, "observe_tree", interrupt_after_capture)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run(tmp_path, payload, timeout=timeout)

    parent_pid = int(parent_pid_path.read_text(encoding="utf-8"))
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    assert interrupted
    assert caught.value.renderer_error.kind == "interrupted"
    assert caught.value.renderer_error.backend == RENDERER_ID
    with pytest.raises(ChildProcessError):
        os.waitpid(parent_pid, os.WNOHANG)
    _assert_pid_disappears(parent_pid)
    _assert_pid_disappears(child_pid)


@pytest.mark.parametrize("inherited", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_detached_tree_timeout_and_interrupt_preserve_host_and_sibling(
    tmp_path, monkeypatch, inherited, interrupted,
):
    registry = tmp_path / "identities"
    fixture = FIXTURE_DIR / "owned_tree_backend.py"
    if inherited:
        monkeypatch.setenv("ASTRID_RENDER_INHERIT_PROCESS_GROUP", "1")
    else:
        monkeypatch.delenv("ASTRID_RENDER_INHERIT_PROCESS_GROUP", raising=False)
    sibling = subprocess.Popen(
        [sys.executable, "-c", "import os, signal; os.write(1, b'ready\\n'); signal.pause()"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    original_observe = transport_module.observe_tree
    original_launch = transport_module.popen_owned_process
    original_killpg = os.killpg
    observed = []
    raised_interrupt = []

    def observe(process, **kwargs):
        live = original_observe(process, **kwargs)
        if (
            interrupted
            and not raised_interrupt
            and len(live) == 4
            and (registry / "ready.txt").is_file()
        ):
            raised_interrupt.append(True)
            raise KeyboardInterrupt()
        return live

    def launch_after_readiness(*args, **kwargs):
        process = original_launch(*args, **kwargs)
        observed.append(process)
        while True:
            live = original_observe(process)
            try:
                tree_pids = {
                    int((registry / f"{role}.pid").read_text())
                    for role in ("backend", "node", "browser", "worker")
                }
            except (OSError, ValueError):
                tree_pids = set()
            if (
                len(tree_pids) == 4
                and set(live) == tree_pids
                and (registry / "last-write.txt").is_file()
                and (registry / "ready.txt").is_file()
            ):
                return process
            if process.poll() is not None:
                pytest.fail("owned fixture command exited before tree readiness")
            time.sleep(0.01)

    def killpg(pgid, sig):
        assert pgid != os.getpgrp(), "transport signalled its containing group"
        original_killpg(pgid, sig)

    monkeypatch.setattr(transport_module, "observe_tree", observe)
    monkeypatch.setattr(transport_module, "popen_owned_process", launch_after_readiness)
    monkeypatch.setattr(os, "killpg", killpg)
    try:
        assert sibling.stdout is not None
        assert sibling.stdout.readline() == b"ready\n"
        expected = KeyboardInterrupt if interrupted else RendererTimeoutError
        with pytest.raises(expected):
            CommandTransport(RENDERER_ID, termination_grace=0.05).run(
                "render", [sys.executable, fixture, "backend", tmp_path, registry],
                request_path=_request(tmp_path, {}), result_path=tmp_path / "result.json",
                cwd=FIXTURE_DIR, timeout=3 if interrupted else 0.5,
            )
        assert len(observed[0]._astrid_tree_members) == 4
        for pid in observed[0]._astrid_tree_members:
            _assert_pid_disappears(pid)
        assert all(
            (registry / f"{role}.pid").is_file()
            for role in ("backend", "node", "browser", "worker")
        )
        assert (registry / "last-write.txt").is_file()
        assert sibling.poll() is None
        assert os.getpid() in process_group._process_snapshot()
        stopped_write = (registry / "last-write.txt").read_text()
        time.sleep(0.1)
        assert (registry / "last-write.txt").read_text() == stopped_write
        assert hasattr(observed[0], "_astrid_process_group_id") is not inherited
    finally:
        try:
            for process in observed:
                snapshot = process_group._process_snapshot()
                for pid, birth in reversed(tuple(getattr(process, "_astrid_tree_members", {}).items())):
                    info = snapshot.get(pid)
                    if info is not None and info.birth == birth:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass  # An ancestor may reap/exit after its child is killed.
                process.wait(timeout=3)
        finally:
            sibling.terminate()
            try:
                sibling.wait(timeout=3)
            finally:
                if sibling.stdout is not None:
                    sibling.stdout.close()


def test_owned_tree_worker_ready_marker_follows_identity_and_first_write(tmp_path: Path) -> None:
    """Verify the leaf handshake with a directly owned worker, without custody launch."""
    registry = tmp_path / "identities"
    registry.mkdir()
    for role in ("backend", "node", "browser"):
        (registry / f"{role}.pid").write_text("test-parent")
    worker = subprocess.Popen(
        [sys.executable, FIXTURE_DIR / "owned_tree_backend.py", "worker", tmp_path, registry],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        while not (registry / "ready.txt").is_file():
            if worker.poll() is not None:
                pytest.fail("owned fixture worker exited before readiness")
            time.sleep(0.01)
        assert (registry / "ready.txt").read_text() == "ready"
        assert (registry / "last-write.txt").is_file()
        assert all(
            (registry / f"{role}.pid").is_file()
            for role in ("backend", "node", "browser", "worker")
        )
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait()


def test_absent_result_file_is_protocol_failure(tmp_path: Path) -> None:
    with pytest.raises(RendererProtocolError) as caught:
        _run(tmp_path, {"action": "absent"})

    assert caught.value.error.kind == "protocol"
    assert caught.value.error.backend == RENDERER_ID


def test_malformed_result_json_is_protocol_failure(tmp_path: Path) -> None:
    with pytest.raises(RendererProtocolError) as caught:
        _run(tmp_path, {"action": "malformed"})

    assert caught.value.error.kind == "protocol"
    assert caught.value.error.backend == RENDERER_ID


def test_incompatible_result_version_is_protocol_failure(tmp_path: Path) -> None:
    payload = _wire_fixture("result.json")
    payload["schema_version"] = 2

    with pytest.raises(RendererProtocolError) as caught:
        _run(tmp_path, {"action": "result", "payload": payload})

    assert caught.value.error.kind == "protocol"
    assert caught.value.error.backend == RENDERER_ID
    assert caught.value.details["received"] == 2


def test_success_logs_capture_and_redact_stdout_and_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "transport-log-secret-value"
    monkeypatch.setenv("TRANSPORT_LOG_SECRET", secret)

    transport, result = _run(
        tmp_path,
        {
            "action": "result",
            "payload": _wire_fixture("result.json"),
            "stdout": "render complete",
            "stderr": f"OPENAI_API_KEY={secret}",
        },
    )

    assert isinstance(result, RenderResult)
    assert any("render complete" in log for log in result.logs)
    assert secret not in json.dumps(result.logs)
    assert "[redacted]" in transport.last_logs["stderr"]


def test_environment_is_allowlisted_and_host_secrets_are_not_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRANSPORT_HOST_SECRET_TOKEN", "must-not-reach-child")

    _, result = _run(
        tmp_path,
        {
            "action": "environment",
            "name": "TRANSPORT_HOST_SECRET_TOKEN",
            "safe_name": "LANG",
            "payload": _wire_fixture("result.json"),
        },
        env={
            "TRANSPORT_HOST_SECRET_TOKEN": "overlay-must-not-reach-child",
            "LANG": "transport-safe-locale",
        },
    )

    assert isinstance(result, RenderResult)
    assert result.metadata == {
        "secret_value": "absent",
        "safe_value": "transport-safe-locale",
    }


def test_pinned_renderer_runtime_is_propagated_to_protocol_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRID_NODE_EXECUTABLE", "/srv/astrid/node")
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", "/srv/astrid/remotion")
    monkeypatch.setenv(
        "ASTRID_TIMELINE_SCHEMA_PYTHONPATH", "/srv/astrid/timeline-schema"
    )

    _, result = _run(
        tmp_path,
        {
            "action": "environment",
            "name": "ASTRID_NODE_EXECUTABLE",
            "safe_name": "ASTRID_REMOTION_PROJECT_DIR",
            "payload": _wire_fixture("result.json"),
        },
    )

    assert isinstance(result, RenderResult)
    assert result.metadata == {
        "secret_value": "/srv/astrid/node",
        "safe_value": "/srv/astrid/remotion",
    }


@pytest.mark.posix_native  # POSIX-only process-group API
def test_renderer_cleanup_never_substitutes_numeric_group_signal(monkeypatch):
    calls = []
    process = SimpleNamespace(_astrid_process_group_id=12, communicate=lambda **_: ("", ""))
    monkeypatch.setattr(transport_module, "terminate_tree", lambda owned, **_: calls.append(("tree", owned)))
    monkeypatch.setattr(transport_module, "terminate_group", lambda owned, **_: calls.append(("group", owned)))
    monkeypatch.setattr(transport_module, "verify_tree_absent", lambda owned: calls.append(("verified", owned)))
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("numeric PID signal"))
    monkeypatch.setattr(os, "killpg", lambda *_: pytest.fail("numeric PGID signal"))
    transport_module._terminate_process_group(process, grace=0)
    assert calls == [("tree", process), ("group", process), ("verified", process)]


def test_host_progress_is_inherited_only_with_retained_attempt_containment(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    progress = tmp_path / ".astrid-progress.json"
    monkeypatch.setenv("ASTRID_PROGRESS_PATH", str(progress))
    _, result = _run(tmp_path, {"action": "environment", "name": "TRANSPORT_HOST_SECRET_TOKEN", "safe_name": "ASTRID_PROGRESS_PATH", "payload": _wire_fixture("result.json")})
    assert result.metadata["safe_value"] == str(progress)
    foreign = tmp_path / "foreign"; foreign.mkdir()
    monkeypatch.setenv("ASTRID_PROGRESS_PATH", str(foreign / ".astrid-progress.json"))
    with pytest.raises(RendererProtocolError, match="retained attempt"):
        _run(tmp_path, {"action": "absent"})
