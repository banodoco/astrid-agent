"""Guard against a journey going green on admission, stale readiness or PID reuse."""

import json
from pathlib import Path, PureWindowsPath
import subprocess
import sys

import pytest

from scripts import ci_windows_journey as journey


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "windows-journey.yml"


@pytest.fixture
def stub_installed_profile_binding(monkeypatch):
    monkeypatch.setattr(journey, "assert_installed_profile_binding", lambda: None)


def sdk_invocation(argv, task_id="task-1"):
    request = json.loads(argv[0])
    assert journey.os.environ["ASTRID_HOST_EXECUTION_PROFILE"] == "installed"
    assert request == {
        "capability_id": "chiptune.sfx",
        "kind": "executor",
        "project": "windows-journey",
        "inputs": {"kind": "blip", "variant": 1, "pitch": 0, "duration_s": 0.1},
    }
    return json.dumps({
        "content": [{"type": "text", "text": "{}"}],
        "details": {"astridTaskRefs": [{"task_id": task_id}]},
    })


def test_journey_runs_for_main_windows_branches_and_main_pull_requests():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'branches: [main, "windows/**"]' in workflow
    assert "pull_request:\n    branches: [main]" in workflow
    assert "name: Windows task host journey (${{ matrix.installer-shell }})" in workflow
    assert "exploratory" not in workflow


def test_journey_resolves_companion_refs_from_durable_defaults():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    lock = json.loads((WORKFLOW.parents[2] / "runtime.lock.json").read_text(encoding="utf-8"))
    assert len(lock["source_commit"]) == 40
    assert "$runtimeRef = $lock.source_commit" in workflow
    assert "ref: ${{ steps.runtime-ref.outputs.ref }}" in workflow
    assert "runtime_ref:" not in workflow
    assert "ref: ${{ inputs.app_ref || 'main' }}" in workflow
    assert "default: windows/task-host" not in workflow
    assert "Run an installed-profile SDK invocation through settlement" in workflow


def test_manifest_replaces_released_sources_and_keeps_app_tools(tmp_path, monkeypatch):
    app = tmp_path / "app"
    (app / "public").mkdir(parents=True)
    (app / "public" / "install-manifest.json").write_text(json.dumps({
        "schema_version": 1, "agent": {"sha": "released-agent"},
        "runtime": {"sha": "released-runtime", "schema_digest": "released-schema"},
        "tools": {"python": {"version": "3.11.13"}},
    }), encoding="utf-8")
    monkeypatch.setattr(journey, "source", lambda repo: {"sha": repo.name, "repo": str(repo)})
    manifest = journey.install_manifest(tmp_path / "agent", tmp_path / "runtime", app)
    assert manifest["agent"] == {"sha": "agent", "repo": str(tmp_path / "agent")}
    assert manifest["runtime"] == {"sha": "runtime", "repo": str(tmp_path / "runtime")}
    assert manifest["tools"] == {"python": {"version": "3.11.13"}}


def process(pid, ppid, created, exe="", command=""):
    return {"pid": pid, "ppid": ppid, "created": created, "exe": exe, "command": command}


def test_processes_include_generic_descendants_and_surviving_orphans():
    root = PureWindowsPath(r"C:\Users\runner\AppData\Local\Astrid")
    host = process(10, 1, "2026-10-26T10:00:00Z", exe=str(root / "py" / "python.exe").upper())
    child = process(11, 10, "2026-10-26T10:00:01Z", exe=r"C:\uv\python.exe")
    grandchild = process(12, 11, "2026-10-26T10:00:02Z", exe=r"C:\cache\chrome.exe")
    # Child-first ordering, plus a reused parent PID that belongs to an older child.
    unrelated = process(13, 10, "2026-10-26T09:00:00Z", exe=r"C:\other.exe")
    selected = journey.astrid_processes([grandchild, unrelated, child, host], [root])
    assert {row["pid"] for row in selected} == {10, 11, 12}
    assert journey.astrid_processes([grandchild], [root], {journey.process_key(grandchild)}) == [grandchild]
    reused = {**grandchild, "created": "2026-10-26T11:00:00Z"}
    assert journey.astrid_processes([reused], [root], {journey.process_key(grandchild)}) == []


@pytest.mark.parametrize("command", [
    r"C:\Temp\astrid-install-123\venv\Scripts\python.exe -m astrid install",
    r"C:\uv\python.exe -I -m astrid install",
])
def test_partial_install_and_unmarked_job_child_are_not_missed(command):
    installer = process(20, 1, "10:00", command=command)
    child = process(21, 20, "10:01", exe=r"C:\cache\python.exe")
    assert journey.astrid_processes([child, installer], []) == [child, installer]


@pytest.mark.parametrize("state,instance,expected", [
    ("starting", "current", False), ("running", "stale", False), ("running", "current", True),
])
def test_ready_file_must_belong_to_running_service(tmp_path, monkeypatch, state, instance, expected):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(journey, "data_root", lambda: tmp_path)
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "generic-host.ready.json").write_text(json.dumps({
        "status": "ready", "runtime_instance_id": instance,
    }), encoding="utf-8")
    monkeypatch.setattr(journey, "health", lambda: {
        "status": "ok", "runtime_instance_id": "current", "services": [{"name": "pack-host", "state": state}],
    })
    assert journey.host_ready() is expected


@pytest.mark.parametrize("state,outputs,expected", [
    ("queued", [], False), ("failed", [], False), ("succeeded", [], False),
    ("succeeded", [{"name": "sfx", "digest": "sha256:" + "a" * 64, "size": 9644}], False),
    ("succeeded", [{"name": name, "digest": "sha256:" + "a" * 64, "size": 100}
                   for name in ("sfx", "sfx_meta")], True),
])
def test_admission_is_not_task_settlement(monkeypatch, stub_installed_profile_binding, state, outputs, expected):
    def cli(module, argv, name, **kwargs):
        if module == "astrid.omp_invoke":
            return sdk_invocation(argv)
        if argv[:2] == ["tasks", "show"]:
            return json.dumps({"ok": True, "data": {"state": state, "result": {"outputs": outputs}}})
        return json.dumps({"ok": True, "data": {"task_id": "task-1"}})

    monkeypatch.setattr(journey, "cli", cli)
    if expected:
        journey.task()
    else:
        with pytest.raises(RuntimeError, match="did not settle"):
            journey.task()


def test_failed_follow_cancels_and_settles_before_returning_failure(tmp_path, monkeypatch,
                                                                    stub_installed_profile_binding):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    calls = []
    state = "running"
    follow_count = 0

    def cli(module, argv, name, **kwargs):
        nonlocal state, follow_count
        if module == "astrid.omp_invoke":
            return sdk_invocation(argv)
        calls.append(tuple(argv[:2]))
        if argv[:2] == ["projects", "create"]:
            return json.dumps({"ok": True})
        if argv[:2] == ["tasks", "create"]:
            return json.dumps({"ok": True, "data": {"task_id": "task-1"}})
        if argv[:2] == ["tasks", "follow"]:
            follow_count += 1
            if follow_count == 1:
                raise RuntimeError("task-follow.jsonl: exit 1")
            state = "cancelled"
            raise RuntimeError("task-cancel-follow.jsonl: exit 1")
        if argv[:2] == ["tasks", "show"]:
            return json.dumps({"ok": True, "data": {"state": state, "result": {"outputs": []}}})
        if argv[:2] == ["tasks", "cancel"]:
            state = "cancel_requested"
            return json.dumps({"ok": True})
        if argv[:2] == ["tasks", "events"]:
            return json.dumps({"ok": True, "data": []})
        raise AssertionError(argv)

    monkeypatch.setattr(journey, "cli", cli)
    monkeypatch.setattr(journey, "snapshot", lambda name: [process(
        22, 21, "10:02", command=(r"C:\Users\runner\py\Scripts\python.exe -X utf8 -m "
                                   r"astrid.packs.chiptune.actions.sfx.run --out "
                                   r"C:\Users\runner\AppData\Local\Temp\astrid-attempt-task-1-token\outputs")
    )])
    monkeypatch.setattr(journey, "capture_sfx_output", lambda task_id, rows: None)
    with pytest.raises(RuntimeError, match="reached terminal state 'cancelled'"):
        journey.task()

    assert ("tasks", "cancel") in calls
    assert follow_count == 2
    assert calls.index(("tasks", "events")) < calls.index(("tasks", "cancel"))
    assert calls[-2:] == [("tasks", "show"), ("tasks", "events")]


def test_timeout_stream_tails_are_captured_before_cancellation_and_bounded(tmp_path, monkeypatch,
                                                                           stub_installed_profile_binding):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    monkeypatch.setattr(journey, "data_root", lambda: tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "service-pack-host.log").write_text("host at timeout\n", encoding="utf-8")
    attempt = tmp_path / "astrid-attempt-task-1-token"
    attempt.mkdir()
    stdout = "x" * (journey.TASK_OUTPUT_TAIL_BYTES + 19)
    (attempt / "task-stdout-tail.log").write_text(stdout, encoding="utf-8")
    (attempt / "task-stderr-tail.log").write_text("worker stderr at timeout\n", encoding="utf-8")
    metadata = {
        "schema_version": 1,
        "streams": {
            "stdout": {"file": "task-stdout-tail.log", "available": True,
                       "byte_count": journey.TASK_OUTPUT_TAIL_BYTES, "truncated": True, "error": None},
            "stderr": {"file": "task-stderr-tail.log", "available": True,
                       "byte_count": 26, "truncated": False, "error": "OSError"},
        },
    }
    (attempt / "task-output-capture.json").write_text(json.dumps(metadata) + "\n", encoding="utf-8")
    calls = []
    state = "running"

    def cli(module, argv, name, **kwargs):
        nonlocal state
        if module == "astrid.omp_invoke":
            return sdk_invocation(argv)
        calls.append(tuple(argv[:2]))
        if argv[:2] == ["projects", "create"]:
            return json.dumps({"ok": True})
        if argv[:2] == ["tasks", "create"]:
            return json.dumps({"ok": True, "data": {"task_id": "task-1"}})
        if argv[:2] == ["tasks", "follow"]:
            raise RuntimeError("follow timed out")
        if argv[:2] == ["tasks", "show"]:
            return json.dumps({"ok": True, "data": {"state": state}})
        if argv[:2] == ["tasks", "cancel"]:
            assert (tmp_path / "task-timeout-task-stdout-tail.log").read_text(encoding="utf-8") == (
                stdout[-journey.TASK_OUTPUT_TAIL_BYTES:]
            )
            assert (tmp_path / "task-timeout-task-stderr-tail.log").read_text(
                encoding="utf-8",
            ) == "worker stderr at timeout\n"
            evidence = json.loads((tmp_path / "task-logs-before-cancel.json").read_text(encoding="utf-8"))
            assert evidence["task_child_output"]["available"] is True
            assert evidence["task_child_output"]["missing"] == []
            assert evidence["task_child_output"]["metadata"]["status"] == "valid"
            assert evidence["task_child_output"]["metadata"]["streams"]["stderr"]["error"] == "OSError"
            assert evidence["task_child_output"]["streams"] == [
                {"source": "task-stdout-tail.log", "evidence": "task-timeout-task-stdout-tail.log",
                 "bytes": journey.TASK_OUTPUT_TAIL_BYTES, "truncated": True},
                {"source": "task-stderr-tail.log", "evidence": "task-timeout-task-stderr-tail.log",
                 "bytes": len("worker stderr at timeout\n"), "truncated": False},
            ]
            state = "cancelled"
            return json.dumps({"ok": True})
        if argv[:2] == ["tasks", "events"]:
            return json.dumps({"ok": True, "data": []})
        raise AssertionError(argv)

    monkeypatch.setattr(journey, "cli", cli)
    command = (f"python -m astrid.packs.chiptune.actions.sfx.run --out "
               f"{attempt / 'outputs'}")
    monkeypatch.setattr(journey, "snapshot", lambda name: [process(
        22, 21, "10:02", command=command,
    )])
    monkeypatch.setattr(journey, "capture_sfx_output", lambda task_id, rows: None)
    with pytest.raises(RuntimeError, match="reached terminal state 'cancelled'"):
        journey.task()

    assert calls.index(("tasks", "cancel")) > calls.index(("tasks", "show"))
    assert (tmp_path / "task-timeout-service-pack-host.log").read_text(encoding="utf-8") == "host at timeout\n"
    assert json.loads((tmp_path / "task-timeout-task-output-capture.json").read_text()) == metadata


def test_snapshot_failure_does_not_prevent_cancel_or_hide_follow_failure(tmp_path, monkeypatch,
                                                                        stub_installed_profile_binding):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    monkeypatch.setattr(journey, "data_root", lambda: tmp_path)
    (tmp_path / "logs").mkdir()
    calls = []
    state = "running"

    def cli(module, argv, name, **kwargs):
        nonlocal state
        if module == "astrid.omp_invoke":
            return sdk_invocation(argv)
        calls.append(tuple(argv[:2]))
        if tuple(argv[:2]) in (("projects", "create"), ("tasks", "events")):
            return json.dumps({"ok": True, "data": []})
        if argv[:2] == ["tasks", "follow"]:
            raise RuntimeError("follow timed out")
        if argv[:2] == ["tasks", "show"]:
            return json.dumps({"ok": True, "data": {"state": state}})
        if argv[:2] == ["tasks", "cancel"]:
            manifest = json.loads((tmp_path / "task-logs-before-cancel.json").read_text())
            assert manifest["task_child_output"]["metadata"]["status"] == "missing"
            state = "cancelled"
            return json.dumps({"ok": True})
        raise AssertionError(argv)

    monkeypatch.setattr(journey, "cli", cli)
    monkeypatch.setattr(
        journey, "snapshot", lambda name: (_ for _ in ()).throw(RuntimeError("snapshot failed")),
    )
    with pytest.raises(RuntimeError, match="reached terminal state 'cancelled'"):
        journey.task()

    assert calls.index(("tasks", "cancel")) > calls.index(("tasks", "show"))
    assert (tmp_path / "processes-after-task-follow-error.json").is_file()


def test_malformed_task_metadata_is_reported_without_blocking_cancellation(tmp_path, monkeypatch,
                                                                          stub_installed_profile_binding):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    monkeypatch.setattr(journey, "data_root", lambda: tmp_path)
    (tmp_path / "logs").mkdir()
    attempt = tmp_path / "astrid-attempt-task-1-token"
    attempt.mkdir()
    (attempt / "task-output-capture.json").write_text("{broken", encoding="utf-8")
    calls = []
    state = "running"

    def cli(module, argv, name, **kwargs):
        nonlocal state
        if module == "astrid.omp_invoke":
            return sdk_invocation(argv)
        calls.append(tuple(argv[:2]))
        if argv[:2] == ["projects", "create"]:
            return json.dumps({"ok": True})
        if argv[:2] == ["tasks", "create"]:
            return json.dumps({"ok": True, "data": {"task_id": "task-1"}})
        if argv[:2] == ["tasks", "follow"]:
            raise RuntimeError("follow timed out")
        if argv[:2] == ["tasks", "show"]:
            return json.dumps({"ok": True, "data": {"state": state}})
        if argv[:2] == ["tasks", "cancel"]:
            manifest = json.loads((tmp_path / "task-logs-before-cancel.json").read_text())
            assert manifest["task_child_output"]["metadata"]["status"] == "malformed"
            state = "cancelled"
            return json.dumps({"ok": True})
        if argv[:2] == ["tasks", "events"]:
            return json.dumps({"ok": True, "data": []})
        raise AssertionError(argv)

    command = f"python -m astrid.packs.chiptune.actions.sfx.run --out {attempt / 'outputs'}"
    monkeypatch.setattr(journey, "cli", cli)
    monkeypatch.setattr(journey, "snapshot", lambda name: [process(22, 21, "10:02", command=command)])
    monkeypatch.setattr(journey, "capture_sfx_output", lambda task_id, rows: None)
    with pytest.raises(RuntimeError, match="reached terminal state 'cancelled'"):
        journey.task()

    assert ("tasks", "cancel") in calls
    assert not (tmp_path / "task-timeout-task-output-capture.json").exists()


def test_oversized_task_metadata_is_rejected_with_a_bounded_read(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path / "evidence")
    attempt = tmp_path / "astrid-attempt-task-1-token"
    attempt.mkdir()
    (attempt / "task-output-capture.json").write_bytes(b"x" * (journey.TASK_OUTPUT_METADATA_BYTES + 100))

    result = journey._task_output_metadata(attempt)

    assert result == {
        "available": False,
        "status": "oversized",
        "source": "task-output-capture.json",
        "bytes": journey.TASK_OUTPUT_METADATA_BYTES + 100,
    }
    assert not (tmp_path / "evidence" / "task-timeout-task-output-capture.json").exists()


def test_sfx_output_capture_restricts_path_to_the_journey_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    command = (r"C:\Users\runner\py\Scripts\python.exe -X utf8 -m "
               r"astrid.packs.chiptune.actions.sfx.run --out "
               r"C:\Users\runner\AppData\Local\Temp\astrid-attempt-task-1-token\outputs")
    journey.capture_sfx_output("task-1", [process(22, 21, "10:02", command=command)])
    evidence = json.loads((tmp_path / "task-sfx-output-evidence.json").read_text())
    assert evidence == {
        "found": True,
        "files": {name: {"exists": False} for name in ("manifest.json", "sfx.json", "sfx.wav")},
    }

    unrelated = command.replace("astrid-attempt-task-1-token", "other-task")
    journey.capture_sfx_output("task-1", [process(22, 21, "10:02", command=unrelated)])
    evidence = json.loads((tmp_path / "task-sfx-output-evidence.json").read_text())
    assert evidence == {"found": False, "files": {}}


def test_collect_preserves_timeout_logs_separately_from_final_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(journey, "data_root", lambda: tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "service-pack-host.log").write_text("host at timeout\n", encoding="utf-8")
    (tmp_path / "logs" / "service-worker.log").write_text("worker at timeout\n", encoding="utf-8")
    attempt = tmp_path / "astrid-attempt-task-1-token"
    attempt.mkdir()
    (attempt / "task-stdout-tail.log").write_text("child at timeout\n", encoding="utf-8")
    (attempt / "task-stderr-tail.log").write_text("child error at timeout\n", encoding="utf-8")
    command = (f"python -m astrid.packs.chiptune.actions.sfx.run --out "
               f"{attempt / 'outputs'}")

    journey.capture_task_logs("task-1", [process(22, 21, "10:02", command=command)])
    (tmp_path / "logs" / "service-pack-host.log").write_text("host after shutdown\n", encoding="utf-8")
    (tmp_path / "logs" / "service-worker.log").write_text("worker after shutdown\n", encoding="utf-8")
    monkeypatch.setattr(journey, "health", lambda: None)
    monkeypatch.setattr(journey, "snapshot", lambda name: [])
    journey.collect()

    evidence = tmp_path / "evidence"
    assert (evidence / "task-timeout-service-pack-host.log").read_text() == "host at timeout\n"
    assert (evidence / "task-timeout-service-worker.log").read_text() == "worker at timeout\n"
    assert (evidence / "task-timeout-task-stdout-tail.log").read_text() == "child at timeout\n"
    assert (evidence / "task-timeout-task-stderr-tail.log").read_text() == "child error at timeout\n"
    assert (evidence / "service-pack-host.log").read_text() == "host after shutdown\n"
    manifest = json.loads((evidence / "task-logs-before-cancel.json").read_text())
    assert manifest["logs"] == [
        {"source": "service-pack-host.log", "evidence": "task-timeout-service-pack-host.log"},
        {"source": "service-worker.log", "evidence": "task-timeout-service-worker.log"},
    ]


def test_timeout_manifest_marks_missing_task_streams(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(journey, "data_root", lambda: tmp_path)
    (tmp_path / "logs").mkdir()

    journey.capture_task_logs("task-1", [])

    manifest = json.loads((tmp_path / "evidence" / "task-logs-before-cancel.json").read_text())
    assert manifest["task_child_output"] == {
        "available": False,
        "streams": [],
        "missing": ["task-stdout-tail.log", "task-stderr-tail.log"],
        "metadata": {
            "available": False,
            "status": "missing",
            "source": "task-output-capture.json",
        },
        "reason": "One or more per-attempt task stream tails were unavailable before cancellation.; "
                  "Task output capture metadata is missing.",
    }


def test_python_stacks_capture_uses_py_spy_for_each_python_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="Thread 0 (active)\n", stderr="")

    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    journey.capture_task_python_stacks([
        process(31, 1, "10:00", exe=r"C:\Astrid\python.exe"),
        process(32, 31, "10:01", command=r'"C:\Astrid\python.exe" -m worker'),
        process(33, 31, "10:02", exe=r"C:\Astrid\node.exe"),
    ])

    assert calls == [["py-spy", "dump", "--pid", "31"], ["py-spy", "dump", "--pid", "32"]]
    assert (tmp_path / "task-timeout-pystack-31.txt").read_text() == "Thread 0 (active)\n"
    assert (tmp_path / "task-timeout-pystack-32.txt").read_text() == "Thread 0 (active)\n"
    assert not (tmp_path / "task-timeout-pystack-33.txt").exists()


def test_python_stack_failure_is_saved_and_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 5, stdout="", stderr="Access denied")

    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    journey.capture_task_python_stacks([process(41, 1, "10:00", exe=r"C:\Astrid\python.exe")])

    assert (tmp_path / "task-timeout-pystack-41.txt").read_text() == (
        "py-spy dump failed with exit 5\nAccess denied"
    )


def test_python_stack_without_pid_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    journey.capture_task_python_stacks([{"ppid": 1, "created": "10:00", "exe": r"C:\Astrid\python.exe"}])

    assert (tmp_path / "task-timeout-pystack-missing-pid.txt").read_text() == (
        "py-spy dump not run: Python process has invalid pid None\n"
    )


def test_native_failure_keeps_log_and_fails_journey(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    with pytest.raises(RuntimeError, match="exit 7"):
        journey.run([sys.executable, "-c", "import sys; print('native error', file=sys.stderr); sys.exit(7)"],
                    "bootstrap.log")
    assert "native error" in (tmp_path / "bootstrap.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("shell", ["powershell.exe", r"C:\Program Files\PowerShell\7\pwsh.exe"])
def test_powershell_child_rebuilds_module_path_without_mutating_environment(tmp_path, monkeypatch, shell):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    monkeypatch.setenv("PSModulePath", r"C:\pwsh\Modules;C:\pwsh\7\Modules")
    monkeypatch.setenv("KEEP_ME", "inherited")
    original = dict(journey.os.environ)
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    journey.run([shell, "-NoProfile"], "powershell.log", show=False)

    assert "PSModulePath" not in captured["env"]
    assert captured["env"]["KEEP_ME"] == "inherited"
    assert dict(journey.os.environ) == original


def test_powershell_explicit_environment_is_copied_and_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    inherited = {"PSModulePath": "pwsh-path", "KEEP_ME": "explicit"}
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    journey.run(["pwsh.exe", "-NoProfile"], "pwsh.log", env=inherited, show=False)

    assert captured["env"] == {"KEEP_ME": "explicit"}
    assert inherited == {"PSModulePath": "pwsh-path", "KEEP_ME": "explicit"}


def test_non_powershell_child_keeps_explicit_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    inherited = {"PSModulePath": "caller-value", "KEEP_ME": "explicit"}
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    journey.run([sys.executable, "-c", "pass"], "python.log", env=inherited, show=False)

    assert captured["env"] is inherited
    assert inherited == {"PSModulePath": "caller-value", "KEEP_ME": "explicit"}
