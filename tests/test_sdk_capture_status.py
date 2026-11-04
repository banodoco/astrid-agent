"""Synthetic checks for the journey's bounded, status-only SDK observations."""
import json
import hashlib
import subprocess
import sys

import pytest

from scripts import ci_journey as journey


STATUS = "sdk-capture-status.json"
POPEN = subprocess.Popen


def receipt(evidence, name=STATUS):
    return journey.SDK_CAPTURE_RECEIPTS[evidence.absolute()][name]


def call_capture(monkeypatch, evidence, code, timeout=5):
    def synthetic(_command, **kwargs):
        return POPEN([sys.executable, "-c", code], **kwargs)
    monkeypatch.setattr(journey.subprocess, "Popen", synthetic)
    return journey.installed_cli_with_capture({"python": sys.executable, "env": {}}, [], evidence,
                                              "sdk-invocation.json", status_name=STATUS, timeout=timeout)


def test_success_classifies_without_publishing_response(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence" / "fresh"
    secret = "private-response-value"
    payload = {"ok": True, "details": {"astridTaskRefs": [{"task_id": secret}]}}
    output = call_capture(monkeypatch, evidence, f"print({json.dumps(json.dumps(payload))})")
    assert json.loads(output) == payload
    status = receipt(evidence)
    assert status == {"schema": "astrid.sdk-capture-status.v1", "exit_condition": "success",
                      "response_size": "within_limit", "json_object": True,
                      "ok": True, "task_refs": "one"}
    journey.write_json(evidence / "journey-result.json", {"status": "pass", "job_id": "example"})
    journey.retain_named_evidence(evidence, "after-cleanup")
    staged = tmp_path / "evidence-upload" / "after-cleanup"
    assert secret not in (staged / STATUS).read_text()
    assert not (staged / "sdk-invocation.json").exists()
    assert journey.read_json(staged / "journey-result.json") == {"status": "pass", "job_id": "example"}


@pytest.mark.parametrize("stream", [1, 2])
def test_over_limit_is_bounded_and_fails_closed(monkeypatch, tmp_path, stream):
    with pytest.raises(RuntimeError, match="over limit"):
        call_capture(monkeypatch, tmp_path,
                     f"import os; os.write({stream}, b'x' * {journey.SDK_CAPTURE_LIMIT + 10})")
    assert (tmp_path / "sdk-invocation.json").stat().st_size == journey.SDK_CAPTURE_LIMIT
    status = receipt(tmp_path)
    assert status["response_size"] == "over_limit"
    assert status["exit_condition"] == "success"
    assert status["json_object"] is None
    assert status["task_refs"] == "unknown"


def test_failure_classifies_complete_json_without_publishing_content(monkeypatch, tmp_path):
    code = "import sys; print('{\"ok\": false, \"secret\": \"private\"}'); sys.exit(4)"
    with pytest.raises(RuntimeError, match="exit 4"):
        call_capture(monkeypatch, tmp_path, code)
    status = receipt(tmp_path)
    assert status["exit_condition"] == "failure"
    assert status["response_size"] == "within_limit"
    assert status["json_object"] is True and status["ok"] is False
    assert "private" not in json.dumps(status)


def test_timeout_records_partial_size_without_interpreting_or_exposing_it(monkeypatch, tmp_path):
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        call_capture(monkeypatch, tmp_path, "import time; print('private', flush=True); time.sleep(5)", 0.2)
    assert exc.value.output is None
    assert (tmp_path / "sdk-invocation.json").read_text() == "private\n"
    status = receipt(tmp_path)
    assert status["exit_condition"] == "timeout"
    assert status["response_size"] == "within_limit"
    assert status["json_object"] is None and status["task_refs"] == "unknown"
    assert "private" not in json.dumps(status)


def test_diagnostic_write_failure_does_not_replace_cli_outcome(monkeypatch, tmp_path):
    class FailingReceipts(dict):
        def setdefault(self, *_args):
            raise OSError("diagnostic unavailable")
    monkeypatch.setattr(journey, "SDK_CAPTURE_RECEIPTS", FailingReceipts())
    assert call_capture(monkeypatch, tmp_path, "print('ok')") == "ok\n"
    with pytest.raises(subprocess.TimeoutExpired):
        call_capture(monkeypatch, tmp_path, "import time; time.sleep(5)", 0.1)


def test_staging_rejects_injected_and_stale_receipts(tmp_path):
    evidence = tmp_path / "evidence" / "fresh"
    evidence.mkdir(parents=True)
    staged = tmp_path / "evidence-upload" / "before-cleanup"
    staged.mkdir(parents=True)
    (staged / STATUS).write_text("stale")
    journey.retain_named_evidence(evidence, "before-cleanup")
    assert not (staged / STATUS).exists()
    journey.write_json(evidence / STATUS, {"schema": "astrid.sdk-capture-status.v1", "private": "secret"})
    journey.retain_named_evidence(evidence, "before-cleanup")
    assert not (staged / STATUS).exists()


def test_task_clears_previous_capture_before_failed_start(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence" / "fresh"
    evidence.mkdir(parents=True)
    for phase in ("before-cleanup", "after-cleanup"):
        staged = tmp_path / "evidence-upload" / phase
        staged.mkdir(parents=True)
        for name in journey.SDK_CAPTURE_STATUS_NAMES:
            (staged / name).write_text("stale")
    for name in journey.SDK_CAPTURE_STATUS_NAMES:
        (evidence / name).write_text("stale")
    for name in ("project.json", "sdk-invocation.json"):
        (evidence / name).write_text("stale private data")
    def failed_start(*_args, **_kwargs):
        raise FileNotFoundError("synthetic start failure")
    monkeypatch.setattr(journey.subprocess, "Popen", failed_start)
    with pytest.raises(FileNotFoundError, match="synthetic start failure"):
        journey.task({"python": sys.executable, "env": {}}, evidence, "project")
    assert not (evidence / "project.json").exists()
    assert not (evidence / "sdk-invocation.json").exists()
    assert receipt(evidence, "project-create-capture-status.json")["exit_condition"] == "failure"
    assert STATUS not in journey.SDK_CAPTURE_RECEIPTS[evidence.absolute()]
    for phase in ("before-cleanup", "after-cleanup"):
        for name in journey.SDK_CAPTURE_STATUS_NAMES:
            assert not (tmp_path / "evidence-upload" / phase / name).exists()


def test_inherited_open_pipe_is_unavailable(monkeypatch, tmp_path):
    code = ("import subprocess, sys; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)']); "
            "print('private')")
    with pytest.raises(RuntimeError, match="incomplete"):
        call_capture(monkeypatch, tmp_path, code)
    status = receipt(tmp_path)
    assert status["exit_condition"] == "success"
    assert status["response_size"] == "unavailable"
    assert status["json_object"] is None
    assert not (tmp_path / "sdk-invocation.json").exists()


@pytest.mark.parametrize("code,timeout,error", [
    ("import sys; print('private'); sys.exit(7)", 5, RuntimeError),
    ("import time; print('private', flush=True); time.sleep(5)", 0.2, subprocess.TimeoutExpired),
])
def test_local_write_failure_does_not_replace_child_failure(monkeypatch, tmp_path, code, timeout, error):
    from pathlib import Path
    def fail_write(_path, _data):
        raise OSError("synthetic write failure")
    monkeypatch.setattr(Path, "write_bytes", fail_write)
    with pytest.raises(error) as exc:
        call_capture(monkeypatch, tmp_path, code, timeout)
    if error is RuntimeError:
        assert "exit 7" in str(exc.value)
    assert receipt(tmp_path)["response_size"] == "within_limit"


@pytest.mark.parametrize("failure", ["thread-start", "interrupt"])
def test_exception_reaps_child_before_it_can_write(monkeypatch, tmp_path, failure):
    import os
    import signal
    import threading
    import time

    ready, release, marker = (tmp_path / name for name in ("ready", "release", "late"))
    code = ("from pathlib import Path\nimport time\n"
            f"Path({str(ready)!r}).touch()\n"
            f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
            f"Path({str(marker)!r}).touch()\n")
    children = []

    def spawn(_command, **kwargs):
        process = POPEN([sys.executable, "-c", code], **kwargs)
        children.append(process)
        deadline = time.monotonic() + 3
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        if failure == "interrupt":
            original_wait = process.wait
            def interrupt_once(*args, **kwargs):
                process.wait = original_wait
                raise KeyboardInterrupt("interrupted wait")
            process.wait = interrupt_once
        return process

    monkeypatch.setattr(journey.subprocess, "Popen", spawn)
    if failure == "thread-start":
        def fail_start(_thread):
            raise RuntimeError("thread start failed")
        monkeypatch.setattr(threading.Thread, "start", fail_start)
    try:
        with pytest.raises(RuntimeError if failure == "thread-start" else KeyboardInterrupt):
            journey.installed_cli_with_capture({"python": sys.executable, "env": {}}, [], tmp_path,
                                               "sdk-invocation.json", status_name=STATUS)
        child = children[0]
        release.touch()
        assert child.returncode == -signal.SIGKILL
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
        assert not marker.exists()
        assert receipt(tmp_path)["exit_condition"] == "failure"
        assert receipt(tmp_path)["response_size"] == "unavailable"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            POPEN.wait(child, timeout=3)
            child.stdout.close()


def test_timeout_cleanup_failure_preserves_primary_and_reports_it(monkeypatch, tmp_path):
    children = []

    def spawn(_command, **kwargs):
        process = POPEN([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(process)
        original_kill = process.kill
        def fail_once():
            process.kill = original_kill
            raise OSError("synthetic kill failure")
        process.kill = fail_once
        return process

    monkeypatch.setattr(journey.subprocess, "Popen", spawn)
    try:
        with pytest.raises(subprocess.TimeoutExpired) as exc:
            journey.installed_cli_with_capture({"python": sys.executable, "env": {}}, [], tmp_path,
                                               "sdk-invocation.json", status_name=STATUS, timeout=0.1)
        assert any("synthetic kill failure" in note for note in exc.value.__notes__)
        assert children[0].returncode is not None
        assert receipt(tmp_path)["exit_condition"] == "timeout"
        assert receipt(tmp_path)["response_size"] == "unavailable"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            POPEN.wait(child, timeout=3)
            child.stdout.close()


def test_cleanup_failure_is_reported_without_replacing_primary(monkeypatch, tmp_path):
    import threading

    children = []
    def spawn(_command, **kwargs):
        process = POPEN([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(process)
        def fail_kill():
            raise OSError("synthetic termination failure")
        process.kill = fail_kill
        return process

    def fail_start(_thread):
        raise RuntimeError("primary thread failure")

    monkeypatch.setattr(journey.subprocess, "Popen", spawn)
    monkeypatch.setattr(threading.Thread, "start", fail_start)
    try:
        with pytest.raises(RuntimeError, match="primary thread failure") as exc:
            journey.installed_cli_with_capture({"python": sys.executable, "env": {}}, [], tmp_path,
                                               "sdk-invocation.json", status_name=STATUS)
        assert any("synthetic termination failure" in note for note in exc.value.__notes__)
        assert receipt(tmp_path)["exit_condition"] == "failure"
    finally:
        for child in children:
            if child.poll() is None:
                POPEN.kill(child)
            POPEN.wait(child, timeout=3)
            child.stdout.close()


def test_failed_release1_task_retains_bounded_invocation_and_closed_source(monkeypatch, tmp_path):
    monkeypatch.setattr(journey.sys, "platform", "linux")
    evidence = tmp_path / "run" / "evidence"
    task_evidence = evidence / "release1-working-task"
    task_evidence.mkdir(parents=True)
    invocation = b'{"ok":false,"error":"not_found"}\n'
    (task_evidence / "sdk-invocation.json").write_bytes(invocation)
    version_root = tmp_path / "program" / "versions" / "v1"
    version_root.mkdir(parents=True)
    manifest = {"release": 1, "agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40}}
    raw_manifest = json.dumps(manifest, separators=(",", ":")).encode()
    (version_root / "manifest.json").write_bytes(raw_manifest)

    journey.capture_failed_release1_task(evidence, {"root": str(version_root)})
    journey.retain_named_evidence(evidence, "before-cleanup")

    staged = tmp_path / "evidence-upload" / "before-cleanup"
    assert (staged / "release1-task-sdk-invocation.json").read_bytes() == invocation
    source = json.loads((staged / "release1-task-installed-source.json").read_text())
    assert source == {
        "schema": "astrid.failed-release1-task-source.v1",
        "release": 1,
        "agent_sha": "a" * 40,
        "runtime_sha": "b" * 40,
        "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
    }
    assert str(version_root) not in json.dumps(source)


def test_failed_release1_task_capture_omits_oversized_invocation(monkeypatch, tmp_path):
    monkeypatch.setattr(journey.sys, "platform", "darwin")
    evidence = tmp_path / "evidence"
    task_evidence = evidence / "release1-working-task"
    task_evidence.mkdir(parents=True)
    (task_evidence / "sdk-invocation.json").write_bytes(b"x" * (journey.SDK_CAPTURE_LIMIT + 1))
    version_root = tmp_path / "version"
    version_root.mkdir()
    (version_root / "manifest.json").write_text(json.dumps({
        "release": 1, "agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40},
    }))

    journey.capture_failed_release1_task(evidence, {"root": str(version_root)})

    assert not (evidence / "release1-task-sdk-invocation.json").exists()
    assert (evidence / "release1-task-installed-source.json").is_file()


def test_buffer_allocation_failure_reaps_launched_child(monkeypatch, tmp_path):
    children = []
    def spawn(_command, **kwargs):
        process = POPEN([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(process)
        return process

    def fail_allocation():
        raise MemoryError("synthetic allocation failure")

    monkeypatch.setattr(journey.subprocess, "Popen", spawn)
    monkeypatch.setattr(journey, "bytearray", fail_allocation, raising=False)
    try:
        with pytest.raises(MemoryError, match="synthetic allocation failure"):
            journey.installed_cli_with_capture({"python": sys.executable, "env": {}}, [], tmp_path,
                                               "sdk-invocation.json", status_name=STATUS)
        assert children[0].returncode is not None
        assert receipt(tmp_path)["response_size"] == "unavailable"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            POPEN.wait(child, timeout=3)
            child.stdout.close()
