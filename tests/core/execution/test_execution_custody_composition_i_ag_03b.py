"""Faults at the real host launch and completion boundaries."""
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host as gh
from astrid.core.execution import _child_import_receipt as receipts
from astrid.core.execution.generic_host import HostError, HostCancelled
from tests.test_generic_host_result_json import host_case, _success_boundary


def test_concurrent_failures_share_one_completion(host_case, monkeypatch):
    case = host_case
    entered, release, joining = threading.Event(), threading.Event(), threading.Event()
    calls, results, errors = [], [], []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        entered.set()
        assert release.wait(5)
        return {"accepted": True}

    monkeypatch.setattr(case.runtime, "fail", fail)

    def exercise(*args, _terminal, **kwargs):
        def submit(message):
            try:
                if message == "other":
                    joining.set()
                results.append(_terminal["completion_call"](case.runtime.fail, "task-result", "lease-result", message))
            except BaseException as exc:
                errors.append(exc)
        first = threading.Thread(target=submit, args=("original",))
        second = threading.Thread(target=submit, args=("other",))
        first.start()
        assert entered.wait(5)
        second.start()
        assert joining.wait(5)
        # A guard must stay free while the completion RPC is in flight.
        _terminal["force_abort"]("timeout")
        assert _terminal["cancel_signal"].is_set()
        release.set()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive()
        assert _terminal["completion_state"] == "acknowledged"
        return {"ok": True}

    monkeypatch.setattr(case.host, "_run_task_impl", exercise)
    case.host.run_task(case.task, lease_token="lease-result")
    assert len(calls) == 1
    assert errors == []
    assert results == [{"accepted": True}] * 2


def test_lost_success_reply_does_not_issue_failure(host_case, monkeypatch):
    case = host_case
    _success_boundary(case, monkeypatch)
    calls = []

    def lost_reply(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("reply lost")

    monkeypatch.setattr(case.runtime, "settle", lost_reply)
    with pytest.raises(OSError, match="reply lost"):
        case.host.run_task(case.task, lease_token="lease-result")
    assert len(calls) == 1
    assert case.runtime.failures == []
    receipt = json.loads((case.root / "result.json").read_text())
    assert receipt["completion_diagnostic"] == {"status": "unconfirmed"}
    assert receipt["publication"]["status"] == "unconfirmed"
    assert not receipt["ok"]
    with pytest.raises(HostError, match="gate is closed or unresolved"):
        case.host._handoff_begin_direct_attempt()


@pytest.mark.parametrize("success_first", [True, False])
def test_success_abort_race_has_one_completion_decision(host_case, monkeypatch, success_first):
    case = host_case
    entered, release = threading.Event(), threading.Event()
    calls, errors = [], []

    def blocked(kind):
        def operation(*args, **kwargs):
            calls.append(kind)
            entered.set()
            assert release.wait(5)
            return {"kind": kind}
        return operation

    monkeypatch.setattr(case.runtime, "settle", blocked("settle"))
    monkeypatch.setattr(case.runtime, "fail", blocked("fail"))

    def exercise(*args, _terminal, **kwargs):
        call = _terminal["completion_call"]
        if not success_first:
            # Abort reserves failure first.
            first = lambda: _terminal["force_abort"]("timeout")
        else:
            first = lambda: call(case.runtime.settle, "task-result", "lease-result", outputs=[])
        def submit():
            try:
                first()
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=submit)
        thread.start()
        assert entered.wait(5)
        if success_first:
            # Abort must return and revoke authority before the pending RPC returns.
            _terminal["force_abort"]("timeout")
            assert not release.is_set()
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        if not success_first:
            with pytest.raises(HostCancelled):
                call(case.runtime.settle, "task-result", "lease-result")
        assert errors == []
        case.host._write_task_result(_terminal)
        return {"ok": True}

    monkeypatch.setattr(case.host, "_run_task_impl", exercise)
    case.host.run_task(case.task, lease_token="lease-result")
    assert calls == ["settle" if success_first else "fail"]
    receipt = json.loads((case.root / "result.json").read_text())
    assert receipt["status"] == "timeout"
    assert receipt["publication"]["status"] == ("settled" if success_first else "not_settled")
    assert receipt["outputs"]["artifacts"] == []


@pytest.fixture
def launch_case(host_case, monkeypatch):
    case = host_case
    case.root.mkdir(parents=True)
    (case.root / "outputs").mkdir()
    record = case.host.capabilities["test.echo"]
    case.record = replace(record, definition=replace(record.definition, id="vibecomfy.validate"))
    case.env = {"PROVIDER_TOKEN": "fake-custody-secret", "PYTHONPATH": ""}
    case.secrets = {"PROVIDER_TOKEN": "fake-custody-secret"}
    case.events = []
    case.process = SimpleNamespace(pid=123456, returncode=0, poll=lambda: 0)
    # Production tracks process objects in a set; retain a hashable process double.
    class Process:
        pid = 123456
        returncode = 0
        def poll(self):
            return 0
    case.process = Process()
    monkeypatch.setattr(case.host, "_child_environment", lambda *a, **kw: (case.env, case.secrets))
    monkeypatch.setattr(gh, "_network_sandbox_argv", lambda argv, *a: argv)
    monkeypatch.setattr(gh, "_terminate_process_group", lambda p: case.events.append(("contained", p)))
    monkeypatch.setattr(gh, "spawn_task_scope", lambda *a, **kw: case.process)

    class Receipt:
        def __init__(self, *a, **kw):
            self.finished = False
        def prepare(self, env):
            case.events.append("prepared")
            return (91,)
        def launched(self, process):
            assert process in case.host._active_processes
            case.events.append("launched")
        def finish(self):
            if not self.finished:
                case.events.append("receipt_closed")
                self.finished = True
            return {"persistence": "written", "launch_id": "fixture"}
    case.receipt_class = Receipt
    monkeypatch.setattr(receipts, "ChildImportReceipt", Receipt)

    class Capture:
        error = None
        def __init__(self, attempt, secrets, scrub):
            case.stream_secrets = secrets
        def start(self, process):
            assert process in case.host._active_processes
            case.events.append("capture_started")
        def finish(self):
            case.events.append("capture_finished")
        def persist(self, **kw):
            pass
        def tails(self):
            return "", ""
    case.capture_class = Capture
    monkeypatch.setattr(gh, "_TaskOutputCapture", Capture)
    case.command = lambda **kw: case.host._run_command_definition(case.record, {}, case.root / "outputs", case.root, **kw)
    return case


@pytest.mark.parametrize("fault", ["argv", "receipt", "spawn", "renderer", "payload"])
def test_prelaunch_faults_close_acquired_resources(launch_case, monkeypatch, fault):
    case = launch_case
    error = RuntimeError(fault)
    def fail(*a, **kw):
        raise error
    if fault == "argv":
        monkeypatch.setattr(gh, "_command_worker_pythonpath", fail)
    elif fault == "receipt":
        monkeypatch.setattr(case.receipt_class, "prepare", fail)
    elif fault == "spawn":
        monkeypatch.setattr(gh, "spawn_task_scope", fail)
    elif fault in {"renderer", "payload"}:
        def lease(*a):
            case.events.append("lease_acquired")
            return SimpleNamespace(inherited_fd=lambda: 92, close=lambda: case.events.append("lease_closed"))
        monkeypatch.setattr(case.host, "_renderer_task_lease", fail if fault == "renderer" else lease)
        if fault == "renderer":
            case.record = replace(case.record, definition=replace(case.record.definition, id="rendering.timeline_visualize"))
        else:
            original = Path.write_text
            def write(path, *a, **kw):
                if path.name == ".astrid-capability-request.json":
                    raise error
                return original(path, *a, **kw)
            monkeypatch.setattr(Path, "write_text", write)
    with pytest.raises(RuntimeError) as caught:
        if fault == "payload":
            case.host.invoke_capability(capability_kind="executor", capability_id="rendering.timeline_visualize",
                request={}, attempt=case.root, child_env=case.env)
        else:
            case.command()
    assert caught.value is error
    assert "launched" not in case.events
    assert case.host._active_processes == set()
    if fault in {"receipt", "spawn"}:
        assert case.events.count("receipt_closed") == 1
    if fault == "payload":
        assert case.events.count("lease_closed") == 1
        # invoke_capability copies a caller-owned environment.
        assert case.env["PROVIDER_TOKEN"] == "fake-custody-secret"
    else:
        assert case.env == case.secrets == {}


@pytest.mark.parametrize("route", ["command", "python"])
def test_partial_spawn_retains_original_process_and_error(launch_case, monkeypatch, route):
    case = launch_case
    error = RuntimeError("native allocation failed")
    error.process = case.process
    def partial(*a, **kw):
        raise error
    monkeypatch.setattr(gh, "spawn_task_scope", partial)
    with pytest.raises(RuntimeError) as caught:
        if route == "command":
            case.command()
        else:
            case.host.invoke_capability(capability_kind="executor", capability_id="test.echo", request={},
                                        attempt=case.root, child_env=case.env)
    assert caught.value is error
    assert case.events.count(("contained", case.process)) == 1
    assert case.host._active_processes == set()


@pytest.mark.parametrize("fault", ["receipt", "capture"])
def test_postspawn_setup_fault_keeps_process_custody(launch_case, monkeypatch, fault):
    case = launch_case
    error = RuntimeError(fault)
    def fail(self, process):
        assert process in case.host._active_processes
        raise error
    monkeypatch.setattr(case.receipt_class if fault == "receipt" else case.capture_class,
                        "launched" if fault == "receipt" else "start", fail)
    with pytest.raises(RuntimeError) as caught:
        case.command()
    assert caught.value is error
    assert case.events.count(("contained", case.process)) == 1
    assert case.events.count("receipt_closed") == 1
    assert case.host._active_processes == set()
    assert case.env == case.secrets == {}


@pytest.mark.parametrize("fault", ["stream", "receipt", "lease"])
def test_finalizer_failures_do_not_skip_cleanup_or_resettle(launch_case, monkeypatch, fault):
    case = launch_case
    error = RuntimeError("original execution failure")
    monkeypatch.setattr(case.capture_class, "start", lambda *a: (_ for _ in ()).throw(error))
    def finish(*a):
        case.events.append("bad_finalizer")
        raise OSError("finalizer fault")
    if fault == "stream":
        monkeypatch.setattr(case.capture_class, "finish", finish)
    elif fault == "receipt":
        monkeypatch.setattr(case.receipt_class, "finish", finish)
    else:
        case.record = replace(case.record, definition=replace(case.record.definition, id="rendering.timeline_visualize"))
        monkeypatch.setattr(case.host, "_renderer_task_lease", lambda *a: SimpleNamespace(
            inherited_fd=lambda: 92, launched=lambda: None, close=finish))
    with pytest.raises(RuntimeError) as caught:
        case.command()
    assert caught.value is error
    assert case.events.count(("contained", case.process)) == 1
    assert "bad_finalizer" in case.events
    assert case.env == case.secrets == case.stream_secrets == {}
    assert case.host._active_processes == set()
    assert case.runtime.failures == case.runtime.settlements == []


@pytest.mark.parametrize("action_fails", [False, True])
@pytest.mark.parametrize("receipt_fault", ["raise", "persistence"])
def test_receipt_and_logger_faults_preserve_outcome_and_clear_secrets(
    launch_case, monkeypatch, action_fails, receipt_fault,
):
    case = launch_case
    from tests.test_generic_host_result_json import _legacy_output
    _legacy_output(case, case.root / "outputs")
    original = RuntimeError("original execution failure")
    if action_fails:
        monkeypatch.setattr(case.capture_class, "start", lambda *a: (_ for _ in ()).throw(original))
    receipt_calls, log_calls = [], []

    def finish(receipt):
        receipt_calls.append(receipt)
        if receipt_fault == "raise":
            raise OSError("fake-custody-secret")
        return {"persistence": "failed", "launch_id": "fixture"}

    def warn(*args):
        log_calls.append(args)
        raise RuntimeError("diagnostic sink failed")

    monkeypatch.setattr(case.receipt_class, "finish", finish)
    monkeypatch.setattr(gh._LOGGER, "warning", warn)
    if action_fails:
        with pytest.raises(RuntimeError) as raised:
            case.command()
        assert raised.value is original
    else:
        assert case.command().returncode == 0
    assert len(receipt_calls) == len(log_calls) == 1
    assert "fake-custody-secret" not in repr(log_calls)
    assert case.events.count(("contained", case.process)) == 1
    assert case.events.count("capture_finished") == 1
    assert case.host._active_processes == set()
    assert case.env == case.secrets == case.stream_secrets == {}
    assert case.runtime.failures == case.runtime.settlements == []


def test_setup_failure_terminalizes_exact_attempt_without_launch(host_case, monkeypatch):
    case = host_case
    error = RuntimeError("admission rejected")
    monkeypatch.setattr(case.host, "_run_command_definition", lambda *a, **kw: pytest.fail("launched"))
    with pytest.raises(RuntimeError) as caught:
        case.host.run_task(case.task, lease_token="lease-result", _setup_error=error)
    assert caught.value is error
    assert len(case.runtime.failures) == 1
    assert case.runtime.failures[0][0:2] == ("task-result", "lease-result")
    assert case.runtime.failures[0][3]["attempt_id"] == "attempt-result"
    assert case.runtime.failures[0][3]["fence"] == 3
    assert (case.root / "result.json").is_file()


def test_success_completes_and_releases_each_resource_once(launch_case, monkeypatch):
    from tests.test_generic_host_result_json import _legacy_output
    case = launch_case
    _legacy_output(case, case.root / "outputs")
    result = case.host.run_task(case.task, lease_token="lease-result")
    assert result["task"]["status"] == "completed"
    assert len(case.runtime.settlements) == 1
    assert case.runtime.failures == []
    assert case.events.count(("contained", case.process)) == 1
    assert case.events.count("capture_finished") == 1
    assert case.host._active_processes == set()
    assert case.env == case.secrets == case.stream_secrets == {}
    assert json.loads((case.root / "result.json").read_text())["publication"]["status"] == "settled"


@pytest.mark.parametrize("stage", ["admission", "execution", "collection"])
@pytest.mark.parametrize("reason", ["timeout", "cancelled"])
def test_deadline_and_cancellation_span_all_stages(host_case, monkeypatch, stage, reason):
    from tests.test_generic_host_result_json import _legacy_output
    case = host_case
    original = case.host._run_task_impl
    terminals = []
    def trigger():
        terminal = terminals[0]
        assert terminal["stage"] == stage
        terminal["force_abort"](reason)
    def execute(_record, _inputs, outputs, _attempt, **kw):
        if stage == "execution":
            trigger()
        _legacy_output(case, outputs)
        return SimpleNamespace(payload={}, process_id=12345, returncode=0)
    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    upload = case.host._upload_outputs
    def collect(*args, **kwargs):
        trigger()
        return upload(*args, **kwargs)
    if stage == "collection":
        monkeypatch.setattr(case.host, "_upload_outputs", collect)
    def run(*args, _terminal, **kwargs):
        terminals.append(_terminal)
        if stage == "admission":
            trigger()
        return original(*args, _terminal=_terminal, **kwargs)
    monkeypatch.setattr(case.host, "_run_task_impl", run)
    try:
        case.host.run_task(case.task, lease_token="lease-result")
    except HostCancelled:
        pass
    assert case.runtime.settlements == []
    assert len(case.runtime.failures) == 1
    terminal = terminals[0]
    assert terminal["cancel_signal"].is_set()
    assert not terminal["pump_thread"].is_alive()
    assert json.loads((case.root / "result.json").read_text())["status"] == reason


def test_lease_observer_spans_collection_and_stops_on_exit(host_case, monkeypatch):
    from datetime import datetime, timedelta, timezone
    case = host_case
    _success_boundary(case, monkeypatch)
    case.task["task"]["lease_expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    case.task["task"]["state"] = "running"
    monkeypatch.setattr(gh, "_LEASE_POLL_SECONDS", 0.01)
    original = case.host._run_task_impl
    terminal = {}
    saw_collection = threading.Event()
    def run(*args, _terminal, **kwargs):
        terminal.update(value=_terminal)
        return original(*args, _terminal=_terminal, **kwargs)
    monkeypatch.setattr(case.host, "_run_task_impl", run)
    calls = []
    def heartbeat(*args, **kwargs):
        calls.append((threading.current_thread().name, dict(kwargs)))
        if terminal and terminal["value"]["stage"] == "collection" and threading.current_thread().name.startswith("astrid-heartbeat"):
            saw_collection.set()
        return {"task_id": "task-result", "attempt_id": "attempt-result", "fence": 3,
                "lease_id": "lease-result", "lease_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}
    monkeypatch.setattr(case.runtime, "heartbeat", heartbeat)
    upload = case.host._upload_outputs
    def collection(*args, **kwargs):
        assert saw_collection.wait(5), "lease pump stopped during collection"
        return upload(*args, **kwargs)
    monkeypatch.setattr(case.host, "_upload_outputs", collection)
    case.host.run_task(case.task, lease_token="lease-result")
    assert saw_collection.is_set()
    assert terminal["value"]["lease_diagnostic"]["last_successful_heartbeat_expiry"]
    assert not terminal["value"]["pump_thread"].is_alive()
    assert len(case.runtime.settlements) == 1


@pytest.mark.parametrize("exit_kind", ["success", "prelaunch", "cancel", "finalizer"])
def test_secret_maps_and_credential_files_clear_on_every_exit(launch_case, monkeypatch, exit_kind):
    from tests.test_generic_host_result_json import _legacy_output
    case = launch_case
    _legacy_output(case, case.root / "outputs")
    if exit_kind == "prelaunch":
        monkeypatch.setattr(gh, "spawn_task_scope", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("spawn")))
    elif exit_kind == "cancel":
        monkeypatch.setattr(case.capture_class, "start", lambda *a: (_ for _ in ()).throw(HostCancelled("cancel")))
    elif exit_kind == "finalizer":
        monkeypatch.setattr(case.capture_class, "finish", lambda *a: (_ for _ in ()).throw(OSError("finish")))
    try:
        case.command()
    except (HostError, RuntimeError, OSError):
        pass
    assert case.env == case.secrets == {}
    assert getattr(case, "stream_secrets", {}) == {}
    for path in case.root.rglob("*"):
        if path.is_file():
            assert b"fake-custody-secret" not in path.read_bytes()


def test_import_receipt_failure_preserves_execution_outcome(launch_case, monkeypatch):
    from tests.test_generic_host_result_json import _legacy_output
    case = launch_case
    _legacy_output(case, case.root / "outputs")
    def fail(*a):
        raise OSError("diagnostic disk failed")
    monkeypatch.setattr(case.receipt_class, "finish", fail)
    result = case.command()
    assert result.returncode == 0
    assert result.payload["child_import_receipt"]["persistence"] == "failed"
    assert case.events.count(("contained", case.process)) == 1
    assert case.env == case.secrets == {}


def test_uncertain_native_cleanup_retains_scope_and_blocks_replacement(launch_case, monkeypatch):
    case = launch_case
    def uncertain(process):
        assert process is case.process
        raise gh.CleanupUncertainError("original tree cannot be proved absent")
    monkeypatch.setattr(gh, "_terminate_process_group", uncertain)
    with pytest.raises(gh.CommandCleanupUnresolved):
        case.command()
    assert case.process in case.host._active_processes
    assert case.root.exists()
    with pytest.raises(HostError, match="closed or unresolved"):
        case.host._handoff_begin_direct_attempt()
    assert case.env == case.secrets == {}


@pytest.mark.parametrize("renderer", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_worker_observes_before_captured_and_ordinary_entry(monkeypatch, renderer, fails):
    from astrid.core.execution import generic_host_worker as worker
    from astrid.core.execution import remotion_host_session
    events = []
    monkeypatch.setattr(worker.sys, "argv", worker.sys.argv.copy())
    monkeypatch.setattr(worker.sys, "path", worker.sys.path.copy())
    bridge = SimpleNamespace(close=lambda: events.append("closed"))
    monkeypatch.setattr(worker, "_install_inherited_bridge", lambda fd: bridge)
    monkeypatch.setattr(remotion_host_session, "install_host_remotion_client", lambda fd: bridge)
    monkeypatch.setattr(receipts, "observe_action_entry", lambda phase: events.append(phase))
    def execute(*a, **kw):
        assert events == ["worker_entry"]
        events.append("action")
        if fails:
            raise RuntimeError("action failed")
    monkeypatch.setattr(worker.runpy, "run_module", execute)
    if fails:
        with pytest.raises(RuntimeError, match="action failed"):
            worker._run_child_command("91", json.dumps(["-X", "utf8", "-m", "fixture"]), renderer=renderer)
    else:
        assert worker._run_child_command("91", json.dumps(["-m", "fixture"]), renderer=renderer) == 0
    assert events == ["worker_entry", "action", "closed"]


@pytest.mark.parametrize("fault", ["write", "spawn", "parse"])
def test_runner_preserves_capture_receipt_and_local_identity(tmp_path, monkeypatch, fault):
    from astrid.core.execution.orchestrator import runner
    from tests.core.test_orchestrator_runner_errors import _python_orchestrator
    definition = _python_orchestrator()
    request = runner.OrchestratorRunRequest(definition.id, run_root=tmp_path)
    env = {"PROVIDER_TOKEN": "runner-private-sentinel"}
    monkeypatch.setattr(runner, "build_child_subprocess_env", lambda **kw: env)
    if fault == "write":
        original = Path.write_bytes
        def write(path, content):
            original(path, content)
            raise OSError("partial request write")
        monkeypatch.setattr(Path, "write_bytes", write)
    elif fault == "spawn":
        monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("spawn")))
    else:
        monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    with pytest.raises((OSError, runner.OrchestratorRunnerError)):
        runner._run_python_orchestrator_subprocess(definition, request)
    assert not list(tmp_path.glob(".astrid-python-orchestrator.*"))
    if fault != "write":
        assert env == {}


@pytest.mark.parametrize("partial", [False, True])
def test_failed_outputs_are_verified_evidence_without_republication(host_case, monkeypatch, partial):
    from astrid.core.contracts.errors import AstridError
    from tests.test_generic_host_result_json import _legacy_output
    case = host_case
    error = AstridError("provider interrupted", code="provider_failed", field="prompt", fix="retry")
    def fail(_record, _inputs, outputs, _attempt, **kwargs):
        if partial:
            _legacy_output(case, outputs)
        raise error
    monkeypatch.setattr(case.host, "_run_command_definition", fail)
    with pytest.raises(AstridError) as raised:
        case.host.run_task(case.task, lease_token="lease-result")
    assert raised.value is error
    receipt = json.loads((case.root / "result.json").read_text())
    assert receipt["status"] == ("partial" if partial else "failed")
    assert receipt["error"]["code"] == "provider_failed"
    assert len(receipt["outputs"]["artifacts"]) == int(partial)
    assert receipt["publication"]["status"] == "not_settled"
    assert len(case.runtime.failures) == 1
    assert case.runtime.settlements == []


def test_child_environment_failure_clears_acquired_secret_maps(host_case, monkeypatch):
    case = host_case
    record = case.host.capabilities["test.echo"]
    monkeypatch.setattr(gh, "_required_secret_names", lambda record: ("PROVIDER_TOKEN",))
    monkeypatch.setattr(gh, "_resolve_credential_value", lambda *a, **kw: "environment-private-sentinel")
    acquired = []
    def fail(**kwargs):
        acquired.append(kwargs["secret_values"])
        raise OSError("environment construction failed")
    monkeypatch.setattr(gh, "build_child_subprocess_env", fail)
    with pytest.raises(OSError):
        case.host._child_environment(record, case.root)
    assert acquired == [{}]


def test_outer_finalizer_failure_preserves_original_and_stops_observer(host_case, monkeypatch):
    case = host_case
    terminal = {}
    error = RuntimeError("original action failure")
    def run(*args, _terminal, **kwargs):
        terminal.update(value=_terminal)
        raise error
    def fail(*a, **kw):
        raise OSError("receipt disk failure")
    monkeypatch.setattr(case.host, "_run_task_impl", run)
    monkeypatch.setattr(case.host, "_write_task_result", fail)
    with pytest.raises(RuntimeError) as raised:
        case.host.run_task(case.task, lease_token="lease-result")
    assert raised.value is error
    assert not terminal["value"]["pump_thread"].is_alive()
    assert len(case.runtime.failures) == 1


def test_acknowledged_success_is_not_resettled_when_release_fails(host_case, monkeypatch):
    case = host_case
    _success_boundary(case, monkeypatch)
    def release(**kwargs):
        raise OSError("release failed")
    monkeypatch.setattr(case.host.managed_tool_session, "release", release)
    with pytest.raises(HostError, match="owned cleanup incomplete"):
        case.host.run_task(case.task, lease_token="lease-result")
    assert len(case.runtime.settlements) == 1 and case.runtime.failures == []
    receipt = json.loads((case.root / "result.json").read_text())
    assert receipt["publication"]["status"] == "settled"


@pytest.mark.parametrize("fails", [False, True])
def test_force_kill_credential_copy_is_removed_before_attempt_retention(host_case, monkeypatch, fails):
    case = host_case
    original = case.host.capabilities["test.echo"]
    identifier = "generation.generate_image_codex"
    record = replace(original, definition=replace(original.definition, id=identifier))
    case.host.capabilities[identifier] = record
    case.task["task"]["capability"] = identifier
    _success_boundary(case, monkeypatch)
    execute = case.host._run_command_definition
    def action(*args, **kwargs):
        credential = case.root / "codex-home" / "auth.json"
        credential.parent.mkdir(parents=True, exist_ok=True)
        credential.write_text('{"token":"credential-copy-sentinel"}')
        if fails:
            raise RuntimeError("child force-killed")
        return execute(*args, **kwargs)
    monkeypatch.setattr(case.host, "_run_command_definition", action)
    if fails:
        with pytest.raises(RuntimeError, match="force-killed"):
            case.host.run_task(case.task, lease_token="lease-result", keep_attempt=True)
    else:
        case.host.run_task(case.task, lease_token="lease-result", keep_attempt=True)
    assert not (case.root / "codex-home" / "auth.json").exists()
    assert case.root.exists()
    for path in case.root.rglob("*"):
        if path.is_file():
            assert b"credential-copy-sentinel" not in path.read_bytes()


@pytest.mark.parametrize("fault", ["broker", "cancel_and_fence", "fence", "release", "hold_and_logger"])
@pytest.mark.parametrize("action_fails", [False, True])
def test_finalizer_baseexceptions_cannot_skip_credentials_or_resettle(host_case, monkeypatch, fault, action_fails):
    case = host_case
    original_record = case.host.capabilities["test.echo"]
    identifier = "generation.generate_image_codex"
    case.host.capabilities[identifier] = replace(original_record, definition=replace(original_record.definition, id=identifier))
    case.task["task"]["capability"] = identifier
    _success_boundary(case, monkeypatch)
    execute = case.host._run_command_definition
    original = RuntimeError("original action failure")
    events, terminal = [], {}
    run_impl = case.host._run_task_impl

    class FinalizerAbort(BaseException):
        def __str__(self):
            raise AssertionError("cleanup errors must not be formatted")

    def abort(name):
        def fail(*a, **kw):
            events.append(name)
            raise FinalizerAbort()
        return fail

    def run(*a, _terminal, **kw):
        terminal.update(value=_terminal)
        return run_impl(*a, _terminal=_terminal, **kw)

    def action(*a, **kw):
        credential = case.root / "codex-home" / "auth.json"
        credential.parent.mkdir(parents=True, exist_ok=True)
        credential.write_text('{"token":"credential-copy-sentinel"}')
        if fault == "cancel_and_fence" and action_fails:
            # Exercise the cancellation cleanup branch without asking Runtime
            # for an opposite completion after acknowledged success.
            terminal["value"]["cancel_signal"].set()
        if action_fails:
            raise original
        return execute(*a, **kw)

    monkeypatch.setattr(case.host, "_run_task_impl", run)
    monkeypatch.setattr(case.host, "_run_command_definition", action)
    monkeypatch.setattr(case.host, "_start_network_broker", lambda *a, **kw: SimpleNamespace(
        stop=abort("broker") if fault == "broker" else lambda: events.append("broker")))
    if fault in {"cancel_and_fence", "fence"}:
        monkeypatch.setattr(case.host.managed_tool_session, "cancel", abort("cancel"))
        monkeypatch.setattr(case.host.managed_tool_session, "fence", abort("fence"))
    release = case.host.managed_tool_session.release
    def release_session(**kw):
        events.append("release")
        if fault == "release":
            raise FinalizerAbort()
        return release(**kw)
    monkeypatch.setattr(case.host.managed_tool_session, "release", release_session)
    if fault == "hold_and_logger":
        monkeypatch.setattr(case.host, "_settle_session_hold", abort("hold"))
        monkeypatch.setattr(gh._LOGGER, "exception", abort("logger"))
    expects_cleanup_error = fault in {"broker", "release"} or action_fails
    if action_fails or expects_cleanup_error:
        with pytest.raises(RuntimeError if action_fails else HostError) as raised:
            case.host.run_task(case.task, lease_token="lease-result", keep_attempt=True)
        if action_fails:
            assert raised.value is original
    else:
        case.host.run_task(case.task, lease_token="lease-result", keep_attempt=True)
    assert events.count("broker") == events.count("release") == 1
    if fault in {"cancel_and_fence", "fence"} and action_fails:
        assert events.count("fence") == 1
        assert events.count("cancel") == int(fault == "cancel_and_fence")
    if fault == "hold_and_logger":
        assert events.count("hold") == events.count("logger") == 1
    assert not (case.root / "codex-home" / "auth.json").exists()
    assert not terminal["value"]["pump_thread"].is_alive()
    assert case.host._handoff_active_attempts == 0
    assert len(case.runtime.settlements) == int(not action_fails)
    assert len(case.runtime.failures) == int(action_fails)
    receipt = json.loads((case.root / "result.json").read_text())
    assert receipt["publication"]["status"] == ("not_settled" if action_fails else "settled")
    assert "credential-copy-sentinel" not in json.dumps(receipt)


@pytest.mark.parametrize("observed", [{"runtime_epoch": 8}, {"runtime_epoch": True}, {"lease_id": "foreign"}])
def test_read_ownership_loss_stops_before_success(host_case, monkeypatch, observed):
    case = host_case
    case.task["task"].update(state="running", runtime_epoch=1)
    monkeypatch.setattr(gh, "_LEASE_POLL_SECONDS", 0.0)
    monkeypatch.setattr(case.runtime, "task", lambda *a: {"task": {**case.task["task"], **observed}})
    def run(*args, _terminal, **kwargs):
        assert _terminal["cancel_signal"].wait(5)
        with pytest.raises(HostCancelled):
            _terminal["completion_call"](case.runtime.settle, "task-result", "lease-result")
        assert _terminal["lease_diagnostic"]["category"] == "lease_metadata_invalid"
        return {"stopped": True}
    monkeypatch.setattr(case.host, "_run_task_impl", run)
    case.host.run_task(case.task, lease_token="lease-result")
    assert not case.runtime.settlements
    assert len(case.runtime.failures) == 1


@pytest.mark.parametrize("action_fails", [False, True])
def test_busy_finalizer_does_not_strand_accounting_or_resettle(host_case, monkeypatch, action_fails):
    case = host_case
    terminal = {}
    original = RuntimeError("original action failure")

    def execute(*args, _terminal, **kwargs):
        terminal.update(value=_terminal)
        if action_fails:
            raise original
        return _terminal["completion_call"](case.runtime.settle, "task-result", "lease-result", outputs=[])

    def publish(path, delta):
        if delta == -1:
            raise OSError("busy publication failed")

    monkeypatch.setattr(case.host, "_run_task_impl", execute)
    monkeypatch.setattr(gh, "_publish_busy", publish)
    with pytest.raises(RuntimeError if action_fails else OSError) as raised:
        case.host.run_task(case.task, lease_token="lease-result")
    if action_fails:
        assert raised.value is original
    assert case.host._handoff_active_attempts == 0
    assert not terminal["value"]["pump_thread"].is_alive()
    assert len(case.runtime.settlements) == int(not action_fails)
    assert len(case.runtime.failures) == int(action_fails)


def test_cancellation_read_and_write_share_one_transport_budget(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(gh.time, "monotonic", lambda: now[0])
    client = gh.RuntimeProtocolClient("http://localhost:8000", "fixture")
    budgets = []

    def get_task(task_id):
        now[0] += 1.5
        return {"attempt_id": "attempt", "version": 7}

    def lifecycle(*, deadline=None):
        budgets.append(deadline - now[0])
        return SimpleNamespace(get_task=get_task, cancel_task=lambda *a, **kw: {"state": "cancel_requested"})

    monkeypatch.setattr(client, "_lifecycle_client", lifecycle)
    assert client.cancel("task", attempt_id="attempt", fence=3)["state"] == "cancel_requested"
    assert budgets == [2.0, 0.5]


def test_stream_finalizer_clears_redaction_material_after_persistence_error(tmp_path, monkeypatch):
    capture = gh._TaskOutputCapture(tmp_path, {"TOKEN": "stream-private-sentinel"}, gh.GenericPackHost._scrub_secret_text)

    def persist(**kwargs):
        raise RuntimeError("output persistence failed")

    monkeypatch.setattr(capture, "persist", persist)
    with pytest.raises(RuntimeError, match="output persistence failed"):
        capture.finish()
    assert capture._secrets == () and capture._secret_trie == {}


@pytest.mark.parametrize("reason", ["failure", "cancel"])
def test_renderer_task_leases_release_once_without_draining_peer(tmp_path, monkeypatch, reason):
    from tests.core.execution.test_remotion_host_session import owner, environment, fake_native, request

    manager, events = owner(tmp_path)
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager)
    first = manager.lease(env, attempt=tmp_path)
    peer = manager.lease(env, attempt=tmp_path)
    try:
        result = manager.request(first, request(env))
        # Both failure and cancellation unwind only this task's lease.
        try:
            raise HostCancelled(reason) if reason == "cancel" else HostError(reason)
        except HostError:
            first.close()
        first.close()
        assert not processes[0].waited
        assert manager.request(peer, request(env))["pid"] == result["pid"]
        assert len(processes) == 1
    finally:
        first.close()
        peer.close()
        manager.close()
    assert processes[0].waited and manager.process is None
    assert events == [("track", processes[0]), ("untrack", processes[0])]
