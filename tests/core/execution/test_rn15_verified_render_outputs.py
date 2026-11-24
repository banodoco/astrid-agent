"""RN-15: verified render output survives a later cleanup-custody failure."""

import hashlib
import json
from pathlib import Path

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.core.execution.process_group import CleanupUncertainError
from tests.test_generic_host import FakeRuntime


def _render_host(tmp_path, *, failure=None):
    pack = tmp_path / "render-pack"
    pack.mkdir()
    code = (
        "from pathlib import Path; import json,hashlib,sys; "
        "out=Path('{out}'); out.mkdir(parents=True,exist_ok=True); "
        "video=out/'render.mp4'; video.write_bytes(b'verified-render'); "
        "manifest={'schema_version':1,'kind':'video','created':'now','inputs':{},'warnings':[],"
        "'outputs':[{'path':'render.mp4','name':'video','ordinal':0,'role':'result','is_primary':True,"
        "'content_hash':'sha256:'+hashlib.sha256(video.read_bytes()).hexdigest(),'bytes':15}]}; "
    )
    if failure == "hash":
        code += "manifest['outputs'][0]['content_hash']='sha256:'+'0'*64; "
    if failure != "missing-manifest":
        code += "(out/'manifest.json').write_text(json.dumps(manifest)); "
    code += "sys.exit(3)" if failure == "exit" else "sys.exit(0)"
    (pack / "executor.yaml").write_text(json.dumps({
        "schema_version": 1, "id": "rendering.render", "name": "Render fixture",
        "kind": "external", "version": "1.0",
        "command": {"argv": ["{python_exec}", "-c", code]},
        "outputs": [{"name": "video", "type": "file", "artifact_type": "video/clip"}],
        # This synthetic CPU producer exercises the render command boundary;
        # installed Remotion/Chromium acceptance stays in the proof lane.
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


def _release_retained_test_handles(host):
    for process in tuple(host._active_processes):
        process.wait(timeout=5)
        process._astrid_custody.release_pidfds()


def test_verified_render_outputs_settle_and_remain_when_cleanup_is_uncertain(tmp_path, monkeypatch):
    host, runtime, task = _render_host(tmp_path)
    attempt = tmp_path / "attempts/render-task-render-attempt"
    cleanup_calls = []

    def uncertain(process):
        assert process.returncode == 0
        copies = list((attempt / "outputs").glob(".verified-render-*/render.mp4"))
        assert len(copies) == 1
        assert copies[0].read_bytes() == b"verified-render"
        cleanup_calls.append(process)
        # The verified private copy, not an uncertain descendant's mutable
        # original path, must supply the later registration/upload.
        (attempt / "outputs/render.mp4").write_bytes(b"changed during cleanup")
        raise CleanupUncertainError("synthetic descendant custody uncertainty")

    monkeypatch.setattr(generic_host, "_terminate_process_group", uncertain)
    try:
        result = host.run_task(task, lease_token="lease")
        assert result["task"]["status"] == "completed"
        assert len(runtime.settlements) == 1
        assert not runtime.failures
        payload = runtime.settlements[0][2]
        assert payload["result"]["cleanup_uncertain"] is True
        assert "synthetic descendant custody uncertainty" in payload["result"]["cleanup_error"]
        media = [row for row in payload["outputs"] if row.get("role") == "result"]
        assert len(media) == 1
        assert media[0]["digest"] == hashlib.sha256(b"verified-render").hexdigest()
        assert media[0]["filename"] == "render.mp4"
        assert runtime.uploaded_objects[media[0]["digest"]]["data"] == b"verified-render"
        assert attempt.is_dir()
        assert host.last_cleanup_receipt["intended_disposition"] == "retained"
        assert host.last_cleanup_receipt["status"] == "uncertain"
        assert host.cleanup_latch_path.is_file()
        assert "synthetic descendant custody uncertainty" in host.cleanup_latch_path.read_text()
        assert len(cleanup_calls) == 1
        assert cleanup_calls[0] in host._active_processes
        assert host.managed_tool_session._active.fenced
    finally:
        _release_retained_test_handles(host)


def test_verified_render_with_confirmed_cleanup_settles_and_deletes_attempt(tmp_path):
    host, runtime, task = _render_host(tmp_path)
    result = host.run_task(task, lease_token="lease")
    assert result["task"]["status"] == "completed"
    payload = runtime.settlements[0][2]
    assert "cleanup_uncertain" not in payload["result"]
    media = [row for row in payload["outputs"] if row.get("role") == "result"]
    assert media[0]["filename"] == "render.mp4"
    assert media[0]["digest"] == hashlib.sha256(b"verified-render").hexdigest()
    assert not (tmp_path / "attempts/render-task-render-attempt").exists()
    assert not host._active_processes
    assert not host.cleanup_latch_path.exists()


def test_render_changed_during_copy_cannot_become_a_verified_result(tmp_path, monkeypatch):
    host, runtime, task = _render_host(tmp_path)
    original_copy = generic_host.shutil.copyfile

    def corrupt_copy(source, destination, **kwargs):
        result = original_copy(source, destination, **kwargs)
        if Path(destination).name == "render.mp4":
            Path(destination).write_bytes(b"changed while copying")
        return result

    monkeypatch.setattr(generic_host.shutil, "copyfile", corrupt_copy)
    try:
        with pytest.raises(HostError, match="verification failed"):
            host.run_task(task, lease_token="lease")
        assert not runtime.settlements
        assert runtime.failures
    finally:
        _release_retained_test_handles(host)


@pytest.mark.parametrize("failure", ["exit", "hash", "missing-manifest"])
def test_unverified_render_cannot_settle_despite_a_cleanup_failure(tmp_path, monkeypatch, failure):
    host, runtime, task = _render_host(tmp_path, failure=failure)

    def uncertain(_process):
        raise CleanupUncertainError("synthetic cleanup failure")

    monkeypatch.setattr(generic_host, "_terminate_process_group", uncertain)
    try:
        with pytest.raises(HostError):
            host.run_task(task, lease_token="lease")
        assert not runtime.settlements
        assert runtime.failures
        assert (tmp_path / "attempts/render-task-render-attempt").is_dir()
        assert host.cleanup_latch_path.is_file()
    finally:
        _release_retained_test_handles(host)


def test_pre_exit_custody_failure_does_not_turn_render_files_into_verified_outputs(tmp_path, monkeypatch):
    host, runtime, task = _render_host(tmp_path)
    original_launch = generic_host.popen_owned_group
    launched = []

    def launch(*args, **kwargs):
        process = original_launch(*args, **kwargs)
        launched.append(process)
        process.wait(timeout=5)
        # Files and a successful returncode alone are insufficient when the
        # original execution observation failed before its verified boundary.
        def uncertain_poll():
            raise CleanupUncertainError("original execution identity unproved")
        monkeypatch.setattr(process, "poll", uncertain_poll)
        return process

    monkeypatch.setattr(generic_host, "popen_owned_group", launch)
    monkeypatch.setattr(generic_host, "_terminate_process_group", lambda *_:
                        (_ for _ in ()).throw(CleanupUncertainError("retained cleanup unproved")))
    try:
        with pytest.raises(HostError):
            host.run_task(task, lease_token="lease")
        assert not runtime.settlements
        assert runtime.failures
        assert len(launched) == 1
        assert not list((tmp_path / "attempts").glob("**/.verified-render-*"))
    finally:
        _release_retained_test_handles(host)
