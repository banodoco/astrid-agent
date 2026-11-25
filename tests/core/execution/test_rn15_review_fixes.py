"""RN-15 S-review regressions: inherited pipes and bounded cleanup policy."""

import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.core.execution.process_group import CleanupUncertainError
from tests.test_generic_host import FakeRuntime

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux retained-custody pipe probe")


def _render_host(tmp_path, *, inherited_pipe=False, returncode=0):
    pack = tmp_path / "render-pack"
    pack.mkdir()
    code = (
        "from pathlib import Path; import hashlib,json,subprocess,sys,time; "
        "out=Path('{out}'); out.mkdir(parents=True,exist_ok=True); "
        "video=out/'render.mp4'; video.write_bytes(b'verified-render'); "
        "manifest={'schema_version':1,'kind':'video','created':'now','inputs':{},'warnings':[],"
        "'outputs':[{'path':'render.mp4','name':'video','ordinal':0,'role':'result','is_primary':True,"
        "'content_hash':'sha256:'+hashlib.sha256(video.read_bytes()).hexdigest(),'bytes':15}]}; "
        "(out/'manifest.json').write_text(json.dumps(manifest)); "
    )
    if inherited_pipe:
        # The review's defining probe: a descendant inherits both pipes and
        # remains alive after the original render command exits successfully.
        code += "subprocess.Popen(['sleep','20']); time.sleep(1); "
    code += f"print('render exit diagnostic', file=sys.stderr, flush=True); sys.exit({returncode})"
    (pack / "executor.yaml").write_text(json.dumps({
        "schema_version": 1, "id": "rendering.render", "name": "Pipe probe",
        "kind": "external", "version": "1.0",
        "command": {"argv": ["{python_exec}", "-c", code]},
        "outputs": [{"name": "video", "type": "file", "artifact_type": "video/clip"}],
        "metadata": {"output_result_manifest": True, "resource_keys": ["cpu"], "adapter_family": "cpu"},
    }))
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[pack], client=runtime, attempt_base=tmp_path / "attempts")
    host.cleanup_latch_path = tmp_path / "cleanup.json"
    host.discover()
    task = {"task": {
        "id": "render-task", "capability": "rendering.render", "project_id": "demo",
        "attempt_id": "render-attempt", "fence": 1, "spec": {"spec": {"inputs": {}}},
    }}
    runtime.tasks["render-task"] = task
    return host, runtime, task


@pytest.fixture
def launched(monkeypatch):
    processes = []
    original = generic_host.popen_owned_group

    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(generic_host, "popen_owned_group", launch)
    yield processes
    for process in processes:
        process.wait(timeout=5)
        process._astrid_custody.release_pidfds()


def _uncertain_cleanup(_process):
    raise CleanupUncertainError("synthetic command-scope cleanup uncertainty")


def test_render_with_long_lived_child_holding_stdout_settles_once(tmp_path, launched):
    """This exact probe must pass on both the destination and repaired head."""
    host, runtime, task = _render_host(tmp_path, inherited_pipe=True)
    result = host.run_task(task, lease_token="lease")
    assert len(launched) == 1
    assert result["task"]["status"] == "completed"
    assert len(runtime.settlements) == 1
    assert not runtime.failures
    payload = runtime.settlements[0][2]
    assert "cleanup_uncertain" not in payload["result"]
    media = [row for row in payload["outputs"] if row.get("role") == "result"]
    assert len(media) == 1
    assert runtime.uploaded_objects[media[0]["digest"]]["data"] == b"verified-render"
    assert not host._active_processes
    assert not (tmp_path / "attempts/render-task-render-attempt").exists()


def test_uncertain_cleanup_preserves_verified_output_and_warns_on_inherited_pipe(tmp_path, monkeypatch, launched):
    host, runtime, task = _render_host(tmp_path, inherited_pipe=True)
    terminate = generic_host._terminate_process_group
    monkeypatch.setattr(generic_host, "_terminate_process_group", _uncertain_cleanup)
    try:
        result = host.run_task(task, lease_token="lease")
        assert result["task"]["status"] == "completed"
        assert len(runtime.settlements) == 1
        assert not runtime.failures
        payload = runtime.settlements[0][2]
        assert payload["result"]["cleanup_uncertain"] is True
        assert any("reader_did_not_stop" in warning for warning in payload["result"]["warnings"])
        media = [row for row in payload["outputs"] if row.get("role") == "result"]
        assert media[0]["digest"] == hashlib.sha256(b"verified-render").hexdigest()
        assert runtime.uploaded_objects[media[0]["digest"]]["data"] == b"verified-render"
        assert host.cleanup_latch_path.is_file()
        assert host.managed_tool_session._active.fenced
        assert launched[0] in host._active_processes
    finally:
        # Real retained identity-based cleanup ends the test's sleeping child.
        for process in launched:
            terminate(process)


def test_nonzero_render_with_inherited_pipe_keeps_exit_diagnostic(tmp_path, launched):
    host, runtime, task = _render_host(tmp_path, inherited_pipe=True, returncode=3)
    with pytest.raises(HostError, match="exited 3: render exit diagnostic"):
        host.run_task(task, lease_token="lease")
    assert len(launched) == 1
    assert not runtime.settlements
    assert runtime.failures
    assert not host._active_processes


def test_capture_failure_without_cleanup_uncertainty_still_refuses_success(tmp_path, monkeypatch, launched):
    host, runtime, task = _render_host(tmp_path)
    finish = generic_host._TaskOutputCapture.finish

    def broken_capture(capture):
        finish(capture)
        capture._reader_errors["stdout"] = "synthetic_read_error"

    monkeypatch.setattr(generic_host._TaskOutputCapture, "finish", broken_capture)
    with pytest.raises(HostError, match="output capture failed: stdout:synthetic_read_error"):
        host.run_task(task, lease_token="lease")
    assert len(launched) == 1
    assert not runtime.settlements
    assert runtime.failures


def test_verified_command_uncertainty_defers_release_and_keeps_session_fenced(tmp_path, monkeypatch, launched):
    host, runtime, task = _render_host(tmp_path)
    release_calls = []
    monkeypatch.setattr(generic_host, "_terminate_process_group", _uncertain_cleanup)
    monkeypatch.setattr(host.managed_tool_session, "release", lambda **kwargs: release_calls.append(kwargs))
    result = host.run_task(task, lease_token="lease")
    assert result["task"]["status"] == "completed"
    assert len(runtime.settlements) == 1
    assert len(launched) == 1
    assert not release_calls
    assert host.managed_tool_session._active.fenced
    assert host.managed_tool_session.occupied
    assert host.last_cleanup_receipt["errors"] == ["unresolved command task scope; attempt retained"]


@pytest.mark.parametrize("failure", ["network broker", "managed fence", "retained observation"])
def test_verified_render_does_not_suppress_independent_cleanup_errors(tmp_path, monkeypatch, launched, failure):
    host, runtime, task = _render_host(tmp_path)
    calls = []

    def fail(*_args, **_kwargs):
        assert len(runtime.settlements) == 1
        calls.append(failure)
        raise RuntimeError("independent cleanup failure")

    monkeypatch.setattr(generic_host, "_terminate_process_group", _uncertain_cleanup)
    if failure == "network broker":
        monkeypatch.setattr(host, "_start_network_broker", lambda *_a, **_k: SimpleNamespace(stop=fail))
        command = host._run_command_definition

        def no_network_command(*args, **kwargs):
            # Isolate the broker's final stop failure from command networking.
            kwargs["network_broker"] = None
            return command(*args, **kwargs)

        monkeypatch.setattr(host, "_run_command_definition", no_network_command)
        monkeypatch.setattr(host, "_network_evidence", lambda *_a, **_k: None)
    elif failure == "managed fence":
        monkeypatch.setattr(host.managed_tool_session, "fence", fail)
    else:
        monkeypatch.setattr(generic_host, "_strict_root_exists", fail)
    with pytest.raises(HostError, match=f"owned cleanup incomplete: .*{failure}"):
        host.run_task(task, lease_token="lease")
    assert len(launched) == 1
    assert calls == [failure]
    assert len(runtime.settlements) == 1
    assert any(f"{failure}: independent cleanup failure" in error for error in host.last_cleanup_receipt["errors"])
    assert host.cleanup_latch_path.is_file()


def test_independent_managed_release_error_still_raises(tmp_path, monkeypatch, launched):
    host, runtime, task = _render_host(tmp_path)

    def fail_release(**_kwargs):
        assert len(runtime.settlements) == 1
        raise RuntimeError("independent release failure")

    monkeypatch.setattr(host.managed_tool_session, "release", fail_release)
    with pytest.raises(HostError, match="managed release: independent release failure"):
        host.run_task(task, lease_token="lease")
    assert len(launched) == 1
    assert len(runtime.settlements) == 1
    assert host.last_cleanup_receipt["status"] == "uncertain"


def test_observer_uncertainty_latched_at_original_exit_refuses_snapshot(tmp_path, monkeypatch, launched):
    host, runtime, task = _render_host(tmp_path)
    launch = generic_host.popen_owned_group

    def uncertain_exit(*args, **kwargs):
        process = launch(*args, **kwargs)
        process.wait(timeout=5)

        def poll():
            # Deterministically model the observer's final-poll race without
            # replacing a successful original process wait with PID absence.
            process._astrid_tree_uncertain = "observer failed just before original exit"
            return process.returncode

        monkeypatch.setattr(process, "poll", poll)
        return process

    monkeypatch.setattr(generic_host, "popen_owned_group", uncertain_exit)
    with pytest.raises(HostError):
        host.run_task(task, lease_token="lease")
    assert len(launched) == 1
    assert not runtime.settlements
    assert runtime.failures
    assert not list((tmp_path / "attempts").glob("**/.verified-render-*"))
    assert host.cleanup_latch_path.is_file()
