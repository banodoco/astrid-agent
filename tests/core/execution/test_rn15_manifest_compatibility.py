"""No-manifest render compatibility remains the original declared-file path."""

import json
import sys

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.core.execution.process_group import CleanupUncertainError
from tests.test_generic_host import FakeRuntime

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux retained-custody render proof")


def _legacy_render(tmp_path, *, manifest=None, required=False, returncode=0, inherited_pipe=False):
    pack = tmp_path / "pack"
    pack.mkdir()
    code = (
        "from pathlib import Path; import json,subprocess,sys,time; "
        "out=Path('{out}'); out.mkdir(parents=True,exist_ok=True); "
        "(out/'answer.txt').write_bytes(b'legacy-output'); "
    )
    if manifest == "malformed":
        code += "(out/'manifest.json').write_text('not JSON'); "
    elif manifest == "hash":
        code += (
            "(out/'manifest.json').write_text(json.dumps({'schema_version':1,'kind':'result',"
            "'created':'now','inputs':{},'warnings':[],'outputs':[{'path':'answer.txt','name':'answer',"
            "'ordinal':0,'role':'result','is_primary':True,'bytes':13,'content_hash':'sha256:'+'0'*64}]})); "
        )
    elif manifest == "directory":
        code += "(out/'manifest.json').mkdir(); "
    if inherited_pipe:
        code += "subprocess.Popen(['sleep','20']); time.sleep(1); "
    code += f"print('legacy exit diagnostic',file=sys.stderr,flush=True); sys.exit({returncode})"
    (pack / "executor.yaml").write_text(json.dumps({
        "schema_version": 1, "id": "rendering.render", "name": "Legacy declared-file render",
        "kind": "external", "version": "1.0", "command": {"argv": ["{python_exec}", "-c", code]},
        "outputs": [{"name": "answer", "type": "file", "path_template": "{out}/answer.txt", "artifact_type": "text/plain"}],
        "metadata": {"resource_keys": ["cpu"], "adapter_family": "cpu", "output_result_manifest": required},
    }))
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[pack], client=runtime, attempt_base=tmp_path / "attempts")
    host.cleanup_latch_path = tmp_path / "cleanup.json"
    host.discover()
    task = {"task": {"id": "legacy", "capability": "rendering.render", "project_id": "demo",
                     "attempt_id": "attempt", "fence": 1, "spec": {"spec": {"inputs": {}}}}}
    runtime.tasks["legacy"] = task
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


@pytest.mark.parametrize("inherited_pipe", [False, True])
def test_no_manifest_render_keeps_legacy_cleanup_and_settlement(tmp_path, monkeypatch, launched, inherited_pipe):
    """The same test and assertions pass on pre-RN-15 base and repaired head."""
    host, runtime, task = _legacy_render(tmp_path, inherited_pipe=inherited_pipe)
    events = []
    terminate = generic_host._terminate_process_group
    finish = generic_host._TaskOutputCapture.finish
    harvest = generic_host.harvest_staged_outputs

    def cleanup(*args, **kwargs):
        events.append("cleanup")
        return terminate(*args, **kwargs)

    def drain(*args, **kwargs):
        events.append("finish")
        return finish(*args, **kwargs)

    def collect(*args, **kwargs):
        events.append("harvest")
        return harvest(*args, **kwargs)

    monkeypatch.setattr(generic_host, "_terminate_process_group", cleanup)
    monkeypatch.setattr(generic_host._TaskOutputCapture, "finish", drain)
    monkeypatch.setattr(generic_host, "harvest_staged_outputs", collect)
    result = host.run_task(task, lease_token="lease", keep_attempt=True)
    assert len(launched) == 1
    assert result["task"]["status"] == "completed"
    assert len(runtime.settlements) == 1
    assert not runtime.failures
    assert events.index("cleanup") < events.index("finish") < events.index("harvest")
    payload = runtime.settlements[0][2]
    assert "cleanup_uncertain" not in payload["result"]
    media = [row for row in payload["outputs"] if row.get("role") == "result"]
    assert len(media) == 1
    assert media[0]["filename"] == "answer.txt"
    assert runtime.uploaded_objects[media[0]["digest"]]["data"] == b"legacy-output"
    assert not list((tmp_path / "attempts").glob("**/.verified-render-*"))
    assert not host._active_processes


def test_no_manifest_render_cleanup_uncertainty_still_fails(tmp_path, monkeypatch, launched):
    host, runtime, task = _legacy_render(tmp_path)

    def uncertain(_process):
        raise CleanupUncertainError("legacy command cleanup uncertainty")

    monkeypatch.setattr(generic_host, "_terminate_process_group", uncertain)
    with pytest.raises(HostError, match="owned cleanup incomplete"):
        host.run_task(task, lease_token="lease")
    assert len(launched) == 1
    assert not runtime.settlements
    assert runtime.failures
    assert host.cleanup_latch_path.is_file()
    assert not list((tmp_path / "attempts").glob("**/.verified-render-*"))


def test_no_manifest_nonzero_render_keeps_exit_error(tmp_path, launched):
    host, runtime, task = _legacy_render(tmp_path, returncode=3)
    with pytest.raises(HostError, match="exited 3: legacy exit diagnostic"):
        host.run_task(task, lease_token="lease")
    assert len(launched) == 1
    assert not runtime.settlements
    assert runtime.failures


def test_no_manifest_capture_failure_keeps_capture_error(tmp_path, monkeypatch, launched):
    host, runtime, task = _legacy_render(tmp_path)
    finish = generic_host._TaskOutputCapture.finish

    def broken(capture):
        finish(capture)
        capture._reader_errors["stdout"] = "legacy_reader_failure"

    monkeypatch.setattr(generic_host._TaskOutputCapture, "finish", broken)
    with pytest.raises(HostError, match="output capture failed: stdout:legacy_reader_failure"):
        host.run_task(task, lease_token="lease")
    assert len(launched) == 1
    assert not runtime.settlements
    assert runtime.failures


def test_missing_required_manifest_keeps_harvest_error(tmp_path, launched):
    host, runtime, task = _legacy_render(tmp_path, required=True)
    with pytest.raises(HostError, match="missing result manifest receipt"):
        host.run_task(task, lease_token="lease")
    assert len(launched) == 1
    assert not runtime.settlements
    assert runtime.failures


@pytest.mark.parametrize("manifest", ["malformed", "hash", "directory"])
def test_present_invalid_manifest_never_falls_back_to_declared_files(tmp_path, launched, manifest):
    host, runtime, task = _legacy_render(tmp_path, manifest=manifest)
    with pytest.raises(HostError):
        host.run_task(task, lease_token="lease")
    assert len(launched) == 1
    assert not runtime.settlements
    assert runtime.failures
