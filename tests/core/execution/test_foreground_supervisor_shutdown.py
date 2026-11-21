"""A completed foreground cleanup failure must reach an idle JSONL caller.

The CLI, installed native stop handler, supervisor and stdin pipe are real.
Only bootstrap/client fixtures and the controlled cleanup error are supplied.
The parent records the open-pipe outcome before closing stdin for recovery.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest


def _wait_for(path, process, timeout=15):
    deadline = time.monotonic() + timeout
    while not path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    return path.exists()


@pytest.mark.parametrize("mode", ["empty", "partial", "transcript", "eof"])
def test_foreground_supervisor_observes_shutdown_with_open_stdin(
    mode, tmp_path, owned_process_groups,
):
    root = str(Path(__file__).resolve().parents[3])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
    stdout_path, stderr_path = tmp_path / "stdout.log", tmp_path / "stderr.log"
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        child = owned_process_groups.popen(
            [sys.executable, "-u", str(Path(__file__).resolve()), mode, str(tmp_path)],
            env=env, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, text=True,
            **options,
        )
        observation = {"mode": mode, "model": "actual CLI/native stop/JSONL/open pipe; injected cleanup error"}
        try:
            assert _wait_for(tmp_path / "input-waiting", child), stderr_path.read_text()
            error_case = mode in ("empty", "partial")
            if error_case:
                if mode == "partial":
                    child.stdin.write('{"op":"status"')
                    child.stdin.flush()
                started = time.monotonic()
                child.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
                assert _wait_for(tmp_path / "cleanup-completed", child, timeout=2), stderr_path.read_text()
                try:
                    child.wait(timeout=2)
                    exited_while_open = True
                except subprocess.TimeoutExpired:
                    exited_while_open = False
                observation.update(
                    exited_while_open=exited_while_open,
                    client_stdin_open=not child.stdin.closed,
                    cleanup_completed=True,
                    observation_seconds=time.monotonic() - started,
                    result_before_eof=(json.loads((tmp_path / "result.json").read_text())
                                       if (tmp_path / "result.json").exists() else None),
                )
                # This original outcome is durable before any EOF recovery.
                (tmp_path / "open-pipe-observation.json").write_text(json.dumps(observation, indent=2))
                if not exited_while_open:
                    child.stdin.close()
                    child.wait(timeout=5)
            else:
                payload = json.dumps({"op": "status", "task_id": "missing"}) + "\n"
                if mode == "transcript":
                    payload += 'not-json\n' + ('x' * 200) + '\n'
                    payload += json.dumps({"op": "start", "task_id": "missing"}) + "\n"
                    payload += json.dumps({"op": "shutdown"}) + "\n"
                else:
                    payload += 'unterminated'
                child.stdin.write(payload)
                child.stdin.flush()
                if mode == "eof":
                    child.stdin.close()
                child.wait(timeout=5)
                observation["client_stdin_open"] = not child.stdin.closed
            observation["child_returncode"] = child.returncode
            observation["final_result"] = json.loads((tmp_path / "result.json").read_text())
            observation["stdout"] = stdout_path.read_text()
            observation["stderr"] = stderr_path.read_text()
            (tmp_path / "observation.json").write_text(json.dumps(observation, indent=2))
            print(json.dumps(observation, sort_keys=True))
            result = observation["final_result"]
            responses = [json.loads(line) for line in observation["stdout"].splitlines()]
            responses = [row for row in responses if "event" not in row]
            if error_case:
                assert observation["client_stdin_open"] and observation["cleanup_completed"], observation
                assert observation["exited_while_open"], observation
                assert observation["result_before_eof"]["same_cleanup_error"], observation
                assert result["same_cleanup_error"] and result["exception_type"] == "CustodyError", observation
                assert observation["child_returncode"] == 1 and result["cleanup_calls"] == 1, observation
                assert result["events"].index("cleanup_completed") < result["events"].index("cli_failed"), observation
                assert responses == [], observation
            else:
                assert observation["child_returncode"] == 0 and result["cli_exit"] == 0, observation
                assert result["cleanup_calls"] == 0 and result["exception_type"] is None, observation
                assert responses[0] == {"ok": True, "status": "unknown", "task_id": "missing"}, observation
                if mode == "transcript":
                    assert observation["client_stdin_open"] and len(responses) == 5, observation
                    assert responses[1]["ok"] is False, observation
                    assert responses[2] == {"ok": False, "error": "frame exceeds configured bound"}, observation
                    assert responses[3]["ok"] is False and "lease/fence requires" in responses[3]["error"], observation
                    assert responses[4] == {"ok": True, "status": "closed"}, observation
                else:
                    assert not observation["client_stdin_open"] and len(responses) == 2, observation
                    assert responses[1] == {"ok": False, "error": "JSONL frame must end with newline"}, observation
        finally:
            if child.stdin is not None and not child.stdin.closed:
                child.stdin.close()
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def _probe(mode: str, directory: Path) -> None:
    from astrid.core.execution import generic_host
    from astrid.core._shared.boot_manifest import build_manifest, load_boot_manifest_hash
    from tests.core.execution.test_generic_host_actions_v3 import pack

    events, cleanup_calls = [], 0
    original_input = sys.stdin
    error = generic_host.CleanupUncertainError("controlled idle-supervisor cleanup failure")

    class ObservedInput:
        def __iter__(self):
            return self

        def __next__(self):
            (directory / "input-waiting").write_text("waiting")
            return next(original_input)

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("ASTRID_SOURCE_STATE", str(directory / "absent-state.json"))
        for name in ("ASTRID_PACKS_PATH", "ASTRID_EXECUTION_TARGET_JSON", "ASTRID_OUTPUT_ROOT",
                     "ASTRID_HOST_READINESS_PROFILE_PATH", "ASTRID_PACK_HOST_SERVICE_MODE"):
            patch.delenv(name, raising=False)
        root, host, runtime, _, _ = pack(directory)
        boot = directory / "boot-manifest.json"
        boot.write_text(json.dumps(build_manifest(registry={}, fixtures=(), profile_order=())))
        digest = "sha256:" + load_boot_manifest_hash(boot, support_root=directory)
        credential = directory / "worker.token"
        credential.write_text("fixture-worker")
        credential.chmod(0o600)
        patch.setattr(sys, "argv", ["host", "supervise", "--pack-root", str(root),
            "--runtime-endpoint", "http://127.0.0.1:1", "--credential-file", str(credential),
            "--supervisor-state", str(directory / "supervisor.jsonl"), "--max-frame-bytes", "128"])
        patch.setattr(generic_host, "_compose_cli_boot_manifest", lambda *a: (boot, digest))
        patch.setattr(generic_host, "_startup_identity_attestation", lambda **k: {"source": {"closure_digest": "3" * 64}})
        patch.setattr(generic_host, "RuntimeProtocolClient", lambda *a, credential_file=None: runtime)
        patch.setattr(generic_host, "GenericPackHost", lambda **k: host)
        patch.setattr(sys, "stdin", ObservedInput())
        original_shutdown = host.shutdown

        def shutdown():
            nonlocal cleanup_calls
            cleanup_calls += 1
            events.append("cleanup_entered")
            original_shutdown()
            events.append("cleanup_completed")
            (directory / "cleanup-completed").write_text("completed")
            raise error

        patch.setattr(host, "shutdown", shutdown)
        caught, cli_exit = None, None
        try:
            cli_exit = generic_host._cli()
        except BaseException as exc:
            caught = exc
            events.append("cli_failed")
        else:
            events.append("cli_returned")
        result = {"mode": mode, "cli_exit": cli_exit,
                  "exception_type": type(caught).__name__ if caught is not None else None,
                  "same_cleanup_error": caught is error, "cleanup_calls": cleanup_calls,
                  "events": events, "caller_thread": threading.get_ident()}
        (directory / "result.json").write_text(json.dumps(result, indent=2))
        if caught is not None:
            raise caught


if __name__ == "__main__":
    _probe(sys.argv[1], Path(sys.argv[2]))
