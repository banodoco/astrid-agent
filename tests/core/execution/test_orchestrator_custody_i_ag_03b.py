"""Command-runner custody uses existing local launch/receipt interfaces."""
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrid.core.execution import _child_import_receipt, process_group
from astrid.core.execution.orchestrator import runner
from tests.core.test_orchestrator_runner_errors import _command_orchestrator


class FinalizerFault(BaseException):
    pass


@pytest.fixture
def command_case(tmp_path, monkeypatch):
    definition = _command_orchestrator(
        orchestrator_id="h3_av.transform", metadata={"local_orchestrator": True},
        env={"DECLARED": "declared-value"},
    )
    request = runner.OrchestratorRunRequest(definition.id, run_root=tmp_path)
    expanded = {"DECLARED": "declared-value"}
    effective = {**expanded, "PROVIDER_TOKEN": "runner-private-sentinel"}
    events = []
    process = SimpleNamespace(_astrid_custody=object())
    reference = {"observation": "recorded", "persistence": "written", "launch_id": "local-launch"}
    case = SimpleNamespace(
        definition=definition, request=request, expanded=expanded, effective=effective,
        events=events, process=process, reference=reference,
    )

    class Receipt:
        def __init__(self, context, capability):
            assert capability == definition.id
            assert context.executor_id == "unavailable"
            assert context.runtime_state == {}
            assert context._child_import_host_context == {"execution_profile": "local_orchestrator"}

        def prepare(self, env):
            assert env is effective
            events.append("prepare")
            return (81,)

        def launched(self, child):
            assert child is process
            events.append("launched")

        def finish(self):
            events.append("finish")
            return reference.copy()

    def spawn(*args, **kwargs):
        assert kwargs["env"] is effective
        assert kwargs["pass_fds"] == (81,)
        events.append("spawn")
        return process

    process.wait = lambda: events.append("wait") or 0
    monkeypatch.setattr(runner, "_expand_command_runtime", lambda *a: (("fixture-command",), None, expanded))
    monkeypatch.setattr(runner, "_command_subprocess_env", lambda *a: effective)
    monkeypatch.setattr(_child_import_receipt, "ChildImportReceipt", Receipt)
    monkeypatch.setattr(process_group, "popen_owned_process", spawn)
    monkeypatch.setattr(process_group, "verify_tree_absent", lambda child: events.append("verify"))
    monkeypatch.setattr(process_group, "terminate_tree", lambda child: events.append("terminate"))
    monkeypatch.setattr(runner, "open_run_log_capture", lambda *a: nullcontext(SimpleNamespace(stdout=None, stderr=None)))
    case.receipt_type = Receipt
    return case


@pytest.mark.parametrize("fault", ["environment", "prepare", "partial_spawn", "launched", "wait", "verify", "capture", "subprocess"])
@pytest.mark.parametrize("error_type", [OSError, FinalizerFault])
def test_command_runner_clears_owned_environments_on_launch_failure(command_case, monkeypatch, fault, error_type):
    case = command_case
    primary = error_type("original execution failure")

    def fail(*args, **kwargs):
        case.events.append("fault")
        if fault == "partial_spawn":
            primary.process = case.process
        raise primary

    if fault == "environment":
        monkeypatch.setattr(runner, "_command_subprocess_env", fail)
    elif fault in {"prepare", "launched"}:
        monkeypatch.setattr(case.receipt_type, fault, fail)
    elif fault == "partial_spawn":
        monkeypatch.setattr(process_group, "popen_owned_process", fail)
    elif fault == "wait":
        case.process.wait = fail
    elif fault == "verify":
        monkeypatch.setattr(process_group, "verify_tree_absent", fail)
    else:
        case.definition = replace(case.definition, metadata={})
        if fault == "capture":
            monkeypatch.setattr(runner, "run_subprocess_with_capture", fail)
        else:
            case.request = replace(case.request, run_root=None)
            monkeypatch.setattr(runner.subprocess, "run", fail)

    with pytest.raises(error_type) as raised:
        runner._run_command_orchestrator(case.definition, case.request, {})
    assert raised.value is primary
    assert case.expanded == {}
    assert case.effective == ({} if fault != "environment" else {
        "DECLARED": "declared-value", "PROVIDER_TOKEN": "runner-private-sentinel",
    })  # A map never returned by the builder has not transferred ownership.
    launched = fault in {"partial_spawn", "launched", "wait", "verify"}
    assert case.events.count("terminate") == int(launched)
    assert case.events.count("finish") == int(fault not in {"environment", "capture", "subprocess"})
    if launched:
        assert primary.process is case.process
        assert primary.custody is case.process._astrid_custody


@pytest.mark.parametrize("receipt_fault", ["finish", "persistence"])
@pytest.mark.parametrize("execution_fails", [False, True])
def test_command_runner_receipt_and_logger_faults_preserve_outcome(command_case, monkeypatch, receipt_fault, execution_fails):
    case = command_case
    primary = OSError("original execution failure")
    if execution_fails:
        def fail_wait():
            raise primary
        case.process.wait = fail_wait

    if receipt_fault == "finish":
        def fail_finish(self):
            case.events.append("finish")
            raise FinalizerFault("receipt unavailable")
        monkeypatch.setattr(case.receipt_type, "finish", fail_finish)
    else:
        case.reference["persistence"] = "failed"

    def fail_log(*args, **kwargs):
        raise FinalizerFault("logger unavailable")
    monkeypatch.setattr(runner.logging.getLogger(runner.__name__), "warning", fail_log)
    if execution_fails:
        with pytest.raises(OSError) as raised:
            runner._run_command_orchestrator(case.definition, case.request, {})
        assert raised.value is primary
        reference = primary.child_import_receipt
    else:
        result = runner._run_command_orchestrator(case.definition, case.request, {})
        assert result.returncode == 0 and result.ok
        reference = result.child_import_receipt
        assert "runner-private-sentinel" not in str(result.to_dict())
    assert reference == ({"observation": "unavailable", "persistence": "failed"}
                         if receipt_fault == "finish" else case.reference)
    assert case.events.count("finish") == 1
    assert case.events.count("terminate") == int(execution_fails)
    assert case.expanded == case.effective == {}


@pytest.mark.parametrize("route", ["dry_run", "local", "capture", "subprocess"])
def test_command_runner_results_retain_declared_environment_only(command_case, monkeypatch, route):
    case = command_case
    caller_env = case.definition.runtime.command.env
    if route == "dry_run":
        case.request = replace(case.request, dry_run=True)
    elif route != "local":
        case.definition = replace(case.definition, metadata={})
        if route == "capture":
            monkeypatch.setattr(runner, "run_subprocess_with_capture", lambda *a, **kw: 0)
        else:
            case.request = replace(case.request, run_root=None)
            monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))

    result = runner._run_command_orchestrator(case.definition, case.request, {})
    assert result.ok
    assert result.dry_run == (route == "dry_run")
    assert result.env == caller_env == {"DECLARED": "declared-value"}
    assert result.env is not case.expanded
    assert "runner-private-sentinel" not in str(result.to_dict())
    assert case.expanded == {}
    assert case.effective == ({} if route != "dry_run" else {
        "DECLARED": "declared-value", "PROVIDER_TOKEN": "runner-private-sentinel",
    })
    assert case.events.count("finish") == int(route == "local")
    assert case.events.count("terminate") == 0


def test_command_runner_cleanup_uncertainty_keeps_original_process(command_case, monkeypatch):
    case = command_case
    primary = OSError("original execution failure")
    cleanup = FinalizerFault("tree absence unproven")

    def fail_wait():
        raise primary

    def fail_terminate(child):
        assert child is case.process
        case.events.append("terminate")
        raise cleanup

    case.process.wait = fail_wait
    monkeypatch.setattr(process_group, "terminate_tree", fail_terminate)
    with pytest.raises(OSError) as raised:
        runner._run_command_orchestrator(case.definition, case.request, {})
    assert raised.value is primary
    assert primary.__cause__ is cleanup
    assert primary.process is case.process
    assert primary.custody is case.process._astrid_custody
    assert primary.child_import_receipt == case.reference
    assert case.events.count("terminate") == case.events.count("finish") == 1
    assert case.expanded == case.effective == {}
