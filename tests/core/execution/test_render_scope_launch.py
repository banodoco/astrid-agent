from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, HostCancelled
from tests.test_generic_host import _write_manifest


def test_capability_launch_failure_clears_secret_environment_and_request(tmp_path, monkeypatch):
    """A failed Popen has no child finalizer, so the host owns cleanup."""
    _write_manifest(tmp_path / "echo")
    host = GenericPackHost(pack_roots=[tmp_path])
    definition, admission = host.admit("executor", "test.echo")
    attempt = tmp_path / "attempt"
    seen = {}

    def fail_launch(*_args, **kwargs):
        seen["env"] = kwargs["env"]
        seen["secret_seen"] = kwargs["env"]["PROVIDER_TOKEN"] == "launch-only-secret"
        raise OSError("simulated Popen failure")

    monkeypatch.setattr(generic_host, "spawn_task_scope", fail_launch)
    with pytest.raises(OSError, match="simulated Popen failure"):
        host.invoke_capability(
            capability_kind="executor", capability_id="test.echo", request={}, attempt=attempt,
            definition=definition, admission=admission,
            child_env={"PROVIDER_TOKEN": "launch-only-secret"},
        )
    assert seen["secret_seen"]
    assert seen["env"] == {}
    assert not (attempt / ".astrid-capability-request.json").exists()
    assert not (attempt / ".astrid-capability-result.json").exists()


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("entrypoint", ["command", "capability"])
def test_render_command_keeps_inherited_group_and_observes_until_cleanup(tmp_path, monkeypatch, cancelled, entrypoint):
    _write_manifest(tmp_path / "render", capability_id="rendering.render")
    host = GenericPackHost(pack_roots=[tmp_path])
    record = host.discover()[0]
    attempt = tmp_path / "attempt"
    output = attempt / "outputs"
    output.mkdir(parents=True)
    events = []

    class RenderProcess:
        pid = 19450
        stdout = BytesIO()
        stderr = BytesIO()
        returncode = None
        polls = 0

        def poll(self):
            self.polls += 1
            if self.polls > 1:
                events.append("completed")
                (output / "answer.txt").write_text("render finished")
                (attempt / ".astrid-capability-result.json").write_text(json.dumps({"ok": True, "returncode": 0, "outputs": []}))
                self.returncode = 0
            return self.returncode

    process = RenderProcess()

    def launch(argv, **kwargs):
        assert kwargs["env"]["ASTRID_RENDER_INHERIT_PROCESS_GROUP"] == "1"
        events.append("owned launch")
        return process

    def cleanup(owned):
        assert owned is process
        events.append("contained")

    monkeypatch.setattr(generic_host, "popen_owned_group", launch)
    monkeypatch.setattr(generic_host, "spawn_task_scope", lambda *_a, **_k: pytest.fail("render used group-only custody"))
    monkeypatch.setattr(generic_host, "observe_tree", lambda owned: events.append("observed"))
    monkeypatch.setattr(generic_host, "_terminate_process_group", cleanup)
    monkeypatch.setattr(generic_host, "_network_sandbox_argv", lambda argv, *_a: argv)
    monkeypatch.setattr(generic_host.time, "sleep", lambda _: None)
    monkeypatch.setattr(host, "_child_environment", lambda *_a, **_k: ({}, {}))

    def run():
        cancel = (lambda: True) if cancelled else None
        if entrypoint == "command":
            return host._run_command_definition(record, {}, output, attempt, cancelled=cancel)
        return host.invoke_capability(capability_kind="executor", capability_id="rendering.render", request={}, attempt=attempt, cancelled=cancel)

    if cancelled:
        with pytest.raises(HostCancelled):
            run()
        assert events == ["owned launch", "observed", "contained"]
    else:
        result = run()
        assert result.returncode == 0
        if entrypoint == "command":
            assert result.outputs
        assert events == ["owned launch", "observed", "completed", "contained"]
    assert not host._active_processes


@pytest.mark.parametrize("entrypoint", ["command", "capability"])
def test_native_render_scope_collects_completed_child(tmp_path, monkeypatch, entrypoint):
    manifest_path = _write_manifest(tmp_path / "render", capability_id="rendering.render")
    manifest = json.loads(manifest_path.read_text())
    manifest["command"]["argv"][2] += "; print('render finished')"
    manifest_path.write_text(json.dumps(manifest))
    host = GenericPackHost(pack_roots=[tmp_path])
    record = host.discover()[0]
    attempt = tmp_path / "attempt"
    output = attempt / "outputs"
    output.mkdir(parents=True)
    monkeypatch.setattr(generic_host, "_network_sandbox_argv", lambda argv, *_a: argv)

    try:
        if entrypoint == "command":
            result = host._run_command_definition(record, {}, output, attempt)
        else:
            definition, admission = host.admit("executor", "rendering.render")
            result = host.invoke_capability(
                capability_kind="executor", capability_id="rendering.render",
                request={
                    "out": str(output), "python_exec": sys.executable, "invocation": "runtime",
                    "project": "demo", "project_was_auto_resolved": True,
                },
                attempt=attempt, definition=definition, admission=admission,
            )
            assert result.outputs
        assert result.returncode == 0
        assert (output / "answer.txt").read_text() == "ok"
        assert "render finished" in (attempt / "task-stdout-tail.log").read_text()
        assert not host._active_processes
    finally:
        host.shutdown()


def test_render_cleanup_failure_keeps_original_execution_reason(tmp_path, monkeypatch):
    _write_manifest(tmp_path / "render", capability_id="rendering.render")
    host = GenericPackHost(pack_roots=[tmp_path])
    record = host.discover()[0]
    attempt = tmp_path / "attempt"
    (attempt / "outputs").mkdir(parents=True)
    class RenderProcess:
        pid = 19451
        returncode = None

        def poll(self):
            return None

    process = RenderProcess()
    monkeypatch.setattr(host, "_child_environment", lambda *_a, **_k: ({}, {"API_TOKEN": "private-value"}))
    monkeypatch.setattr(generic_host, "popen_owned_group", lambda *_a, **_k: process)
    monkeypatch.setattr(generic_host, "_network_sandbox_argv", lambda argv, *_a: argv)

    def observation(_process):
        raise HostCancelled("render cancelled private-value")

    def cleanup(_process):
        raise RuntimeError("renderer writer survived")

    monkeypatch.setattr(generic_host, "observe_tree", observation)
    monkeypatch.setattr(generic_host, "_terminate_process_group", cleanup)
    with pytest.raises(generic_host.CommandCleanupUnresolved) as caught:
        host._run_command_definition(record, {}, attempt / "outputs", attempt)
    assert "renderer writer survived" in str(caught.value)
    assert "original execution failure: HostCancelled: render cancelled" in str(caught.value)
    assert "private-value" not in str(caught.value)
    # Unresolved custody retains the original process handle for shutdown.
    assert process in host._active_processes


def test_native_render_scope_cancels_detached_renderer_tree(tmp_path, monkeypatch):
    """Real custody integration; a fake renderer and browser both use setsid."""
    fixture = Path(__file__).resolve().parents[1] / "rendering" / "fixtures" / "owned_tree_backend.py"
    manifest = _write_manifest(tmp_path / "render", capability_id="rendering.render")
    registry = tmp_path / "identities"
    definition = json.loads(manifest.read_text())
    definition["command"]["argv"] = ["{python_exec}", str(fixture), "pack", "{out}", str(registry)]
    manifest.write_text(json.dumps(definition))
    host = GenericPackHost(pack_roots=[tmp_path])
    record = host.discover()[0]
    attempt = tmp_path / "attempt"
    output = attempt / "outputs"
    output.mkdir(parents=True)
    # This fixture needs no network or Remotion installation. Actual native
    # custody, descendant observation and termination remain fully enabled.
    monkeypatch.setattr(host, "_child_environment", lambda *_a, **_k: ({}, {}))
    monkeypatch.setattr(generic_host, "_network_sandbox_argv", lambda argv, *_a: argv)
    identities = {}

    def cancel_when_rendering():
        if not (registry / "ready.txt").is_file():
            return False
        from astrid.core.execution.process_group import process_birth_identity
        for path in registry.glob("*.pid"):
            pid = int(path.read_text())
            identities[pid] = process_birth_identity(pid)
        return True

    try:
        with pytest.raises(HostCancelled):
            host._run_command_definition(record, {}, output, attempt, cancelled=cancel_when_rendering)
        from astrid.core.execution.process_group import process_birth_identity
        assert len(identities) == 5
        assert all(not birth or process_birth_identity(pid) != birth for pid, birth in identities.items())
        assert not host._active_processes
        assert not host._unresolved_task_scopes
    finally:
        # Only retained native handles authorize a cleanup retry; never signal
        # a PID obtained after reparenting or through an unfenced census.
        host.shutdown()
