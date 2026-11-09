"""Guard against a journey going green on admission, stale readiness or PID reuse."""

import json
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import time

import pytest

from scripts import ci_windows_journey as journey
from scripts import ci_journey as shared_journey
from scripts import windows_update_journey as update_journey


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "windows-journey.yml"


def test_tst_b1_windows_alias_requires_owned_roots_before_install(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "ci_journey", shared_journey)
    monkeypatch.setattr(sys, "argv", ["ci_windows_journey.py", "journey", "--leg", "fresh-install",
                                   "--app-checkout", str(tmp_path)])
    monkeypatch.setattr(shared_journey, "bootstrap", lambda *unused: (_ for _ in ()).throw(
        AssertionError("bootstrap must not run")))
    with pytest.raises(SystemExit) as failure:
        journey.main()
    assert failure.value.code == 2


def test_tst_b1_windows_alias_returns_shared_driver_failure(monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "ci_journey", shared_journey)
    monkeypatch.setattr(sys, "argv", ["ci_windows_journey.py", "journey"])
    monkeypatch.setattr(shared_journey, "main", lambda unused: 1)
    assert journey.main() == 1


def test_windows_update_stop_context_records_task_states_without_inferring_stop_outcome(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    monkeypatch.setattr(update_journey.windows, "powershell", lambda *args, **kwargs: "Ready")

    update_journey.capture_stop_task_state(evidence, "before", "Running")
    update_journey.capture_stop_task_state(evidence, "after")

    receipt = json.loads((evidence / update_journey.STOP_TASK_STATE).read_text(encoding="utf-8"))
    assert receipt["task_states"] == {
        "before": {"status": "observed", "state": "Running"},
        "after": {"status": "observed", "state": "Ready"},
    }
    assert receipt["stop_outcome"] == {"status": "not_observed", "value": None}


def test_m4_4a_backend_uses_windows_powershell_51_admission_route():
    command = journey.powershell5_command(r"C:\Program Files\nodejs\node.exe",
                                          ["-e", "process.exit(0)", r"C:\Pinokio\api\Astrid"])
    assert command[:5] == ["powershell.exe", "-NoProfile", "-NonInteractive",
                           "-ExecutionPolicy", "Bypass"]
    assert "& 'C:\\Program Files\\nodejs\\node.exe'" in command[-1]
    assert "process.exit(0)" in command[-1]
    assert "pwsh" not in command[0].lower()


def test_bootstrap_process_capture_keeps_identity_exit_and_streams_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path / "evidence")
    log = tmp_path / "update-bootstrap.log"
    with pytest.raises(RuntimeError, match="update-bootstrap.log: exit 7"):
        journey.run_bootstrap_process(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"],
            log, "update",
        )

    evidence = tmp_path / "evidence"
    receipt = json.loads((evidence / "update-bootstrap-process.json").read_text())
    assert receipt["pid"] > 0
    assert receipt["executable"] == sys.executable
    assert receipt["started_at"] and receipt["exited_at"]
    assert receipt["exit_code"] == 7
    assert receipt["timeout_seconds"] == 3600
    assert receipt["stdout"]["status"] == receipt["stderr"]["status"] == "captured"
    assert (evidence / "update-bootstrap-stdout.log").read_text().strip() == "out"
    assert (evidence / "update-bootstrap-stderr.log").read_text().strip() == "err"
    assert "=== stdout ===" in log.read_text() and "=== stderr ===" in log.read_text()


def test_bootstrap_process_timeout_cleans_up_and_keeps_receipt_and_streams(
        tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path / "evidence")
    original_popen = subprocess.Popen
    processes = []

    def tracked_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(journey.subprocess, "Popen", tracked_popen)
    log = tmp_path / "install-bootstrap.log"
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        journey.run_bootstrap_process(
            [sys.executable, "-c",
             "import sys, time; print('before timeout', flush=True); "
             "print('error before timeout', file=sys.stderr, flush=True); time.sleep(30)"],
            log, "install", timeout=0.2,
        )

    assert failure.value.timeout == 0.2
    assert len(processes) == 1 and processes[0].poll() is not None
    evidence = tmp_path / "evidence"
    receipt = json.loads((evidence / "install-bootstrap-process.json").read_text())
    assert receipt["pid"] == processes[0].pid
    assert receipt["started_at"] and receipt["exited_at"]
    assert receipt["timed_out"] is True
    assert receipt["timeout_seconds"] == 0.2
    assert receipt["exit_code"] == processes[0].returncode
    assert receipt["stdout"]["status"] == receipt["stderr"]["status"] == "captured"
    assert (evidence / "install-bootstrap-stdout.log").read_text().strip() == "before timeout"
    assert (evidence / "install-bootstrap-stderr.log").read_text().strip() == "error before timeout"
    assert "before timeout" in log.read_text() and "error before timeout" in log.read_text()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_bootstrap_interruption_stops_owned_child(tmp_path, monkeypatch, interruption):
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(journey, "EVIDENCE", evidence)
    started, completed = tmp_path / "started", tmp_path / "completed"
    original_popen = subprocess.Popen
    processes = []

    def interrupted_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        real_wait = process.wait
        def wait(*args, **kwargs):
            if kwargs.get("timeout") is not None:
                deadline = time.monotonic() + 5
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert started.exists(), "child did not start"
                raise interruption("cancelled")
            return real_wait(*args, **kwargs)
        process.wait = wait
        return process

    monkeypatch.setattr(journey.subprocess, "Popen", interrupted_popen)
    code = ("import pathlib, sys, time; pathlib.Path(sys.argv[1]).touch(); "
            "time.sleep(2); pathlib.Path(sys.argv[2]).touch()")
    with pytest.raises(interruption, match="cancelled"):
        journey.run_bootstrap_process([sys.executable, "-c", code, str(started), str(completed)],
                                      tmp_path / "install-bootstrap.log", "install")
    assert processes[0].poll() is not None
    assert not completed.exists()
    receipt = json.loads((evidence / "install-bootstrap-process.json").read_text())
    assert receipt["launch_or_wait_error"] == interruption.__name__
    assert receipt["exit_code"] == processes[0].returncode
    assert receipt["exited_at"]


def test_bootstrap_receipt_write_failure_does_not_stop_healthy_child(tmp_path, monkeypatch, capsys):
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(journey, "EVIDENCE", evidence)
    real_save = journey.save
    calls = []
    def save(name, receipt):
        calls.append(name)
        if len(calls) == 2:
            raise PermissionError("receipt locked")
        real_save(name, receipt)
    monkeypatch.setattr(journey, "save", save)
    completed = tmp_path / "completed"
    code = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('done'); print('finished')"
    output = journey.run_bootstrap_process([sys.executable, "-c", code, str(completed)],
                                            tmp_path / "install-bootstrap.log", "install")
    assert completed.read_text() == "done"
    assert "finished" in output
    receipt = json.loads((evidence / "install-bootstrap-process.json").read_text())
    assert receipt["exit_code"] == 0
    assert receipt["diagnostic_errors"] == ["receipt save: PermissionError: receipt locked"]
    assert "receipt locked" in capsys.readouterr().err


def test_bootstrap_timeout_keeps_final_receipt_if_combined_log_fails(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(journey, "EVIDENCE", evidence)
    log = tmp_path / "install-bootstrap.log"
    original_open = Path.open
    def open_with_locked_log(path, *args, **kwargs):
        if path == log:
            raise PermissionError("combined log locked")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", open_with_locked_log)
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        journey.run_bootstrap_process(
            [sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(30)"],
            log, "install", timeout=0.2)
    assert failure.value.timeout == 0.2
    receipt = json.loads((evidence / "install-bootstrap-process.json").read_text())
    assert receipt["timed_out"] is True
    assert receipt["exit_code"] is not None and receipt["exited_at"]
    assert receipt["stdout"]["bytes"] == len(b"partial\n")
    assert receipt["diagnostic_errors"] == ["combined log: PermissionError: combined log locked"]


def test_bootstrap_timeout_survives_stream_close_failure(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(journey, "EVIDENCE", evidence)
    original_open = Path.open
    class BrokenClose:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self.stream
        def __exit__(self, *args):
            self.stream.close()
            raise OSError("stream close failed")
    def open_with_broken_close(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path.name == "install-bootstrap-stderr.log" and args and args[0] == "wb":
            return BrokenClose(stream)
        return stream
    monkeypatch.setattr(Path, "open", open_with_broken_close)
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        journey.run_bootstrap_process([sys.executable, "-c", "import time; time.sleep(30)"],
                                      tmp_path / "install-bootstrap.log", "install", timeout=0.2)
    assert any("stream close failed" in note for note in failure.value.__notes__)
    receipt = json.loads((evidence / "install-bootstrap-process.json").read_text())
    assert receipt["timed_out"] is True and receipt["exit_code"] is not None


def test_bootstrap_receipt_and_cleanup_failures_do_not_mask_lifecycle_error(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(journey, "save", lambda *args: (_ for _ in ()).throw(OSError("receipt locked")))
    class FailedProcess:
        pid = 123
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("bootstrap", timeout) if timeout is not None else OSError("wait failed")
        def kill(self):
            raise OSError("kill failed")
    monkeypatch.setattr(journey.subprocess, "Popen", lambda *args, **kwargs: FailedProcess())
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        journey.run_bootstrap_process(["bootstrap"], tmp_path / "install-bootstrap.log", "install", timeout=1)
    assert any("kill failed" in note for note in failure.value.__notes__)


@pytest.fixture
def stub_installed_profile_binding(monkeypatch):
    monkeypatch.setattr(journey, "assert_installed_profile_binding", lambda: None)
    monkeypatch.setattr(journey, "install_journey_packs", lambda: None)


def test_journey_installs_chiptune_from_the_pinned_source_before_the_task(monkeypatch):
    assert shared_journey.JOURNEY_PACKS == {"chiptune": (
        "https://github.com/banodoco/poms_packs", "2379ab54aca522019ff4f869cfdf9375bdb56dfc")}
    calls = []

    def cli(module, argv, name, **kwargs):
        calls.append(argv)
        return json.dumps({"packs": []}) if argv[:2] == ["packs", "list"] else "{}"

    monkeypatch.setattr(journey, "cli", cli)
    journey.install_journey_packs()
    assert calls[-1] == ["packs", "add", "https://github.com/banodoco/poms_packs", "--revision",
                         "2379ab54aca522019ff4f869cfdf9375bdb56dfc", "--pack-id", "chiptune",
                         "--subpath", "chiptune", "--yes"]


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


def test_journey_runs_for_main_release_windows_branches_and_main_pull_requests():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'branches: [main, release-1, "windows/**"]' in workflow
    assert "pull_request:\n    branches: [main]" in workflow
    assert "name: Windows task host journey (${{ matrix.installer-shell }})" in workflow
    assert "exploratory" not in workflow


def test_journey_runs_for_main_windows_branches_and_main_pull_requests():
    # Retain Timeline's harness guard with the approved Release-1 branch added.
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'branches: [main, release-1, "windows/**"]' in workflow
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
    assert "ref: ${{ steps.app-ref.outputs.ref }}" in workflow
    assert '{ "main" }' in workflow
    assert "^[0-9a-fA-F]{40}$" in workflow
    assert "git -C journey-app rev-parse HEAD" in workflow
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
    manifest = journey.install_manifest(tmp_path / "agent", tmp_path / "runtime", app,
                                        source_override=True)
    assert manifest["agent"] == {"sha": "agent", "repo": str(tmp_path / "agent")}
    assert manifest["runtime"] == {"sha": "runtime", "repo": str(tmp_path / "runtime")}
    assert manifest["tools"] == {"python": {"version": "3.11.13"}}


def test_install_gives_the_unattended_installer_consent_to_the_recommended_plan(tmp_path, monkeypatch):
    # A fresh noninteractive install stops for consent unless --yes is passed.
    app = tmp_path / "journey-app"
    (app / "public").mkdir(parents=True)
    (app / "public" / "install-manifest.json").write_text(json.dumps({
        "schema_version": 1, "agent": {"sha": "a" * 40},
        "runtime": {"sha": "b" * 40, "schema_digest": "released-schema"},
    }), encoding="utf-8")
    monkeypatch.setattr(journey, "ROOT", tmp_path)
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(journey, "source", lambda repo: {"sha": "c" * 40, "repo": str(repo)})

    class Stop(Exception):
        pass

    seen = {}

    def fake_run(argv, name, *, env=None, timeout=300, show=True):
        seen["argv"] = [str(arg) for arg in argv]
        seen["name"] = name
        raise Stop

    monkeypatch.setattr(journey, "run", fake_run)
    with pytest.raises(Stop):
        journey.install("pwsh.exe")
    assert seen["name"] == "bootstrap.log"
    assert seen["argv"][0] == "pwsh.exe"
    assert seen["argv"][-1] == "--yes"


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


def test_windows_update_pack_host_capture_is_bounded_read_only_and_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_journey.sys, "platform", "win32")
    evidence = tmp_path / "evidence"
    support = tmp_path / "data" / "runtime"
    support.mkdir(parents=True)
    registration = support / "generic-host.json"
    registration.write_text(json.dumps({
        "executor_id": "astrid-pack-host", "pid": 123, "support_root": str(support),
        "credential_file": r"C:\\private\\host.token", "runtime_service_launch_id": "launch-1",
        "display_path": r"C:\Users\private person\secret file.json",
        "diagnostic": r"could not read C:\Users\private person\App Data\record.json (access denied)",
        r"C:\Users\private person\record.json": "path-key",
        "credential=KEY-KEY": "key-secret",
    }), encoding="utf-8")
    cleanup = support / "pack-host-job-cleanup.json"
    cleanup.write_text(json.dumps({"version": 1, "service_name": "pack-host", "receipts": [
        {"job_drained": True, "post_cleanup_error": "diagnostic error", "token": "TOPSECRET"},
    ]}), encoding="utf-8")
    original = registration.read_bytes(), cleanup.read_bytes()

    shared_journey._capture_windows_pack_host_update_state(evidence, tmp_path / "data", "before")

    receipt = json.loads((evidence / "windows-pack-host-update-before.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "captured"
    assert receipt["files"]["generic-host.json"]["content"]["executor_id"] == "astrid-pack-host"
    assert (receipt["files"]["generic-host.json"]["content"]
            ["runtime_service_launch_id"] == "launch-1")
    assert receipt["files"]["generic-host.json"]["content"]["support_root"] == "<path>"
    assert receipt["files"]["generic-host.json"]["content"]["credential_file"] == "<redacted>"
    assert receipt["files"]["generic-host.json"]["content"]["display_path"] == "<path>"
    assert receipt["files"]["generic-host.json"]["content"]["diagnostic"] == "could not read <path>"
    assert receipt["files"]["pack-host-job-cleanup.json"]["content"]["receipts"][0]["job_drained"] is True
    assert (receipt["files"]["pack-host-job-cleanup.json"]["content"]["receipts"][0]
            ["post_cleanup_error"] == "diagnostic error")
    encoded = json.dumps(receipt)
    assert all(secret not in encoded for secret in (
        "TOPSECRET", "KEY-KEY", "private person", str(tmp_path),
    ))
    assert "<redacted>" in encoded and "<path>" in encoded
    assert registration.read_bytes() == original[0] and cleanup.read_bytes() == original[1]

    shared_journey._capture_windows_pack_host_update_state(evidence, tmp_path / "data", "after")
    after = json.loads((evidence / "windows-pack-host-update-after.json").read_text(encoding="utf-8"))
    assert after["files"]["generic-host.ready.json"]["status"] == "missing"


@pytest.mark.parametrize("state,outputs,expected", [
    ("queued", [], False), ("failed", [], False), ("succeeded", [], False),
    ("succeeded", [{"name": "sfx", "digest": "sha256:" + "a" * 64, "size": 9644}], False),
    ("succeeded", [{"name": name, "digest": "sha256:" + "a" * 64, "size": 100}
                   for name in ("sfx", "sfx_meta")], True),
])
def test_admission_is_not_task_settlement(monkeypatch, stub_installed_profile_binding, state, outputs, expected):
    def cli(module, argv, name, **kwargs):
        if module == "astrid.omp_invoke":
            assert name == "release1-task-sdk-invocation.json"
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


@pytest.mark.parametrize(
    ("case", "response", "exit_code"),
    [
        ("absent", {"status": "absent", "error": {
            "type": "Microsoft.PowerShell.Cmdletization.Cim.CimJobException",
            "fully_qualified_error_id": "CmdletizationQuery_NotFound_TaskName,Get-ScheduledTask",
            "message": "No matching task", "hresult": -2146233088, "category": "ObjectNotFound"}}, 0),
        ("present", {"status": "present", "tasks": [
            {"path": "\\", "executables": [r"C:\journey\program\python.exe"]}]}, 0),
        ("error", {"status": "error", "error": {
            "type": "System.UnauthorizedAccessException", "fully_qualified_error_id": "AccessDenied",
            "message": "Access denied", "hresult": -2147024891, "category": "PermissionDenied"}}, 1),
    ], ids=["absent", "present", "powershell-error"])
def test_windows_scheduled_task_preflight_classification(tmp_path, monkeypatch, capsys, case, response, exit_code):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    def fake_run(argv, **kwargs):
        assert argv[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
        script = argv[-1]
        assert "Get-ScheduledTask -TaskName 'Astrid Runtime' -ErrorAction Stop" in script
        assert "CmdletizationQuery_NotFound_TaskName,Get-ScheduledTask" in script
        assert "[System.Management.Automation.ErrorCategory]::ObjectNotFound" in script
        assert "SilentlyContinue" not in script
        assert script.count("exit 0") == 2 and "exit 1" in script
        assert kwargs["capture_output"] and kwargs["timeout"] == 30
        return subprocess.CompletedProcess(argv, exit_code, json.dumps(response).encode(), b"diagnostic stderr")
    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    if case == "error":
        with pytest.raises(RuntimeError, match="Scheduled Task preflight failed"):
            journey.scheduled_task_preflight("fresh-install")
    else:
        assert journey.scheduled_task_preflight("fresh-install") == response
    evidence = json.loads((tmp_path / "preflight-scheduled-task-fresh-install.json").read_text())
    assert evidence["status"] == case
    assert evidence["observation"] == response
    assert evidence["exit_code"] == exit_code
    assert evidence["stderr"] == "diagnostic stderr"
    line = capsys.readouterr().out.strip()
    prefix = "Scheduled Task preflight (fresh-install): "
    assert line.startswith(prefix)
    assert json.loads(line[len(prefix):]) == evidence


def test_windows_scheduled_task_preflight_rejects_inconsistent_process_result(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    monkeypatch.setattr(journey.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, b'{"status":"absent"}', b"query failed"))
    with pytest.raises(RuntimeError, match="Scheduled Task preflight failed"):
        journey.scheduled_task_preflight("fresh-install")
    evidence = json.loads((tmp_path / "preflight-scheduled-task-fresh-install.json").read_text())
    assert evidence["status"] == "error"
    assert evidence["protocol_error"] == "Unexpected query result or exit code"
    assert evidence["stderr"] == "query failed"


def test_windows_diagnostics_rejects_failed_origin_even_with_absent_task(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    def run(argv, **kwargs):
        if argv[-1] == journey.SCHEDULED_TASK_PREFLIGHT:
            return subprocess.CompletedProcess(argv, 0, b'{"status":"absent"}', b"")
        return subprocess.CompletedProcess(argv, 1, "", "origin failed")
    monkeypatch.setattr(journey.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="origin exit 1"):
        journey.scheduled_task_diagnostics(leg="fresh-install")
    diagnostic = json.loads((tmp_path / "preflight-scheduled-task.txt").read_text())
    assert diagnostic["origin"]["stderr"] == "origin failed"
    assert diagnostic["query"]["classification"] == "not-found"


def test_windows_preflight_refuses_existing_task_and_allows_only_owned_nonfresh(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_journey.sys, "platform", "win32")
    monkeypatch.setattr(shared_journey, "program_root", lambda: tmp_path / "default" / "program")
    monkeypatch.setattr(shared_journey, "runtime_processes", lambda data: [])
    root, data = Path(r"C:\journey\program"), tmp_path / "data"
    observation = {"status": "present", "tasks": [
        {"path": "\\", "executables": [r"C:\journey\program\python.exe"]}]}
    monkeypatch.setattr(journey, "scheduled_task_diagnostics", lambda **kwargs: observation)
    with pytest.raises(RuntimeError, match="outside this journey"):
        shared_journey.assert_host_available("fresh-install", root, data)
    shared_journey.assert_host_available("stale-update", root, data)
    observation["tasks"][0]["executables"].append(r"C:\other\python.exe")
    with pytest.raises(RuntimeError, match="outside this journey"):
        shared_journey.assert_host_available("stale-update", root, data)


def test_windows_journey_success_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_journey.sys, "platform", "win32")
    monkeypatch.setattr(shared_journey, "execute", lambda args: {"status": "pass"})
    assert shared_journey.main([
        "--leg", "fresh-install", "--app-checkout", str(tmp_path),
        "--run-root", str(tmp_path / "run"), "--root", str(tmp_path / "run" / "program"),
        "--data-root", str(tmp_path / "run" / "data"), "--shell", "powershell.exe",
    ]) == 0
    monkeypatch.setitem(sys.modules, "ci_journey", shared_journey)
    monkeypatch.setattr(sys, "argv", ["ci_windows_journey.py", "journey", "--leg", "fresh-install",
                                  "--app-checkout", str(tmp_path), "--run-root", str(tmp_path / "run"),
                                  "--root", str(tmp_path / "run" / "program"),
                                  "--data-root", str(tmp_path / "run" / "data")])
    assert journey.main() == 0


@pytest.mark.parametrize("status,exit_code", [("absent", 0), ("present", 0), ("error", 1), ("unknown", 0)])
def test_direct_windows_workflow_preflight_invocation(tmp_path, monkeypatch, capsys, status, exit_code):
    workflow = (WORKFLOW.parent / "journey.yml").read_text(encoding="utf-8")
    step = workflow.split("      - name: Capture Windows scheduled-task query diagnostics\n", 1)[1].split(
        "      - name: Retain hosted service-manager prerequisite diagnostics\n", 1)[0]
    assert "JOURNEY_LEG: ${{ matrix.leg }}" in step
    assert "if ($null -ne $record.exception -or $record.exit_code -ne 0) { exit 1 }" in step
    code = step.split('          $code = "', 1)[1].split('"\n', 1)[0]
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("JOURNEY_LEG", "release9-then-install")
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    response = {"status": status}
    if status == "absent":
        # The hosted raw probe returned exit 1 with empty streams for this exact
        # structured no-match result; no separate raw query should decide it.
        response["error"] = {"fully_qualified_error_id": "CmdletizationQuery_NotFound_TaskName,Get-ScheduledTask",
                             "category": "ObjectNotFound"}
    if status == "present":
        response["tasks"] = [{"path": "\\", "executables": [r"C:\journey\python.exe"]}]
    if status == "error":
        response["error"] = {"message": "Access denied"}
    def run(argv, **kwargs):
        if argv[-1].endswith(journey.SCHEDULED_TASK_ORIGIN):
            return subprocess.CompletedProcess(argv, 0, "origin", "")
        assert argv[-1] == journey.SCHEDULED_TASK_PREFLIGHT
        return subprocess.CompletedProcess(argv, exit_code, json.dumps(response).encode(), b"diagnostic stderr")
    monkeypatch.setattr(journey.subprocess, "run", run)
    if status in {"error", "unknown"}:
        with pytest.raises(RuntimeError, match="Scheduled Task preflight failed"):
            exec(code, {})
    else:
        exec(code, {})
    evidence = json.loads((tmp_path / "journey-manager-direct-windows" /
                           "preflight-scheduled-task-release9-then-install.json").read_text())
    assert evidence["status"] == ("error" if status == "unknown" else status)
    if status != "unknown":
        assert evidence["observation"] == response
    assert evidence["exit_code"] == exit_code and evidence["stderr"] == "diagnostic stderr"
    diagnostic = json.loads((tmp_path / "journey-manager-direct-windows" / "preflight-scheduled-task.txt").read_text())
    assert diagnostic["query"]["classification"] == {"absent": "not-found", "present": "present"}.get(status, "error")
    prefix = "Scheduled Task preflight (release9-then-install): "
    assert json.loads(capsys.readouterr().out.strip().removeprefix(prefix)) == evidence


def test_windows_manager_prerequisite_passes_lifecycle_leg(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_journey.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(journey, "scheduled_task_preflight", lambda leg: calls.append(leg))
    monkeypatch.setattr(journey, "powershell", lambda *args, **kwargs: "5.1")
    args = shared_journey.argparse.Namespace(job_id="direct-windows", mode="direct",
                                            leg="release9-then-install")
    result = shared_journey.release1_manager_prerequisite(args, tmp_path)
    assert calls == ["release9-then-install"]
    assert result["outcome"] == "pass" and result["manager"] == "task-scheduler"


def test_windows_scheduled_task_preflight_records_timeout_output(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 30, output=b"partial output", stderr=b"partial error")
    monkeypatch.setattr(journey.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="Scheduled Task preflight failed"):
        journey.scheduled_task_preflight("fresh-install")
    evidence = json.loads((tmp_path / "preflight-scheduled-task-fresh-install.json").read_text())
    assert evidence["status"] == "error" and evidence["exit_code"] is None
    assert evidence["stdout"] == "partial output" and evidence["stderr"] == "partial error"
    assert "Traceback" in evidence["process_error"] and "TimeoutExpired:" in evidence["process_error"]


@pytest.mark.parametrize("failure", ["origin", "query"])
def test_combined_windows_preflight_fails_closed_at_either_checkpoint(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(shared_journey.sys, "platform", "win32")
    monkeypatch.setattr(shared_journey, "program_root", lambda: tmp_path / "default" / "program")
    monkeypatch.setattr(shared_journey, "runtime_processes", lambda data: [])
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path / "evidence")
    calls = []
    def run(argv, **kwargs):
        if argv[-1] == journey.SCHEDULED_TASK_PREFLIGHT:
            calls.append("query")
            if failure == "query":
                return subprocess.CompletedProcess(argv, 1, b'{"status":"error"}', b"access denied")
            return subprocess.CompletedProcess(argv, 0, b'{"status":"absent"}', b"")
        calls.append("origin")
        return subprocess.CompletedProcess(argv, 1 if failure == "origin" else 0, "", "")
    monkeypatch.setattr(journey.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="origin exit 1" if failure == "origin" else "Scheduled Task preflight failed"):
        shared_journey.assert_host_available("fresh-install", tmp_path / "program", tmp_path / "data")
    assert calls == ["origin", "query"]


class _FollowReached(Exception):
    pass


def test_windows_task_admits_sdk_result_after_stderr_diagnostic(monkeypatch):
    """The installed SDK merges a stderr diagnostic into the captured output before its result."""
    diagnostic = json.dumps({
        "diagnostic": "astrid.pack_host_attach_rejected",
        "keys": ["live.check_raised"],
        "mismatches": [{"actual": "'ApiError'", "expected": "'completed'", "key": "live.check_raised"}],
        "pid": 6620,
        "process_birth_id": "windows-creation:134385483390147122",
        "reason": "attach rejected: live.check_raised expected='completed' actual='ApiError'",
        "runtime_service_launch_id": "33abad01b1474e78992f7a765c6fe6fd",
    }, sort_keys=True)
    result = json.dumps({
        "ok": True,
        "content": [{"type": "text", "text": "{\"ok\": true}"}],
        "details": {"astridTaskRefs": [{"task_id": "0da1da62492847a9808c12cec3cf64ea"}]},
    })
    captured = {}

    def fake_cli(module, argv, name, *, timeout=300):
        captured[name] = module
        if name == "project.json":
            return json.dumps({"ok": True})
        if name == "release1-task-sdk-invocation.json":
            return f"{diagnostic}\n{result}\n"
        if name == "task-follow.jsonl":
            raise _FollowReached()
        raise AssertionError(f"unexpected Windows CLI capture {name}")

    monkeypatch.setattr(journey, "assert_installed_profile_binding", lambda: None)
    monkeypatch.setattr(journey, "cli", fake_cli)
    monkeypatch.setattr(journey, "install_journey_packs", lambda: None)
    monkeypatch.setitem(journey.os.environ, "ASTRID_HOST_EXECUTION_PROFILE", "installed")
    with pytest.raises(_FollowReached):
        journey.task("windows-journey")
    assert captured["release1-task-sdk-invocation.json"] == "astrid.omp_invoke"
