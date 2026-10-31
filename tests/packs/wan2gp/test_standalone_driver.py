"""CPU-only tests for the pack's retained Wan session worker."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[3]
BIRTH = "birth-1"
IDENTITY = {"token_id": "token", "invocation_id": "run-1", "generation": 1}


@pytest.fixture
def worker(tmp_path):
    root = tmp_path / "engine"
    (root / "shared").mkdir(parents=True)
    (root / "shared/__init__.py").write_text("")
    (root / "wgp_config.json").write_text('{"config": 42}')
    (root / "shared/api.py").write_text('''
import json, os, threading
from pathlib import Path
from types import SimpleNamespace
state = Path(os.environ["FAKE_STATE"])
def record(event, **kwargs):
    with state.open("a") as f:
        f.write(json.dumps(dict(event=event, **kwargs)) + "\\n")
class Job:
    def __init__(self, settings, spool):
        self.settings, self.spool = settings, spool
        self.events = self
        self.cancelled = threading.Event()
    def iter(self):
        yield SimpleNamespace(kind="progress", data=SimpleNamespace(phase="sampling", status="running", progress=42, current_step=2, total_steps=5))
        if self.settings.get("wait"):
            self.cancelled.wait(5)
    def cancel(self):
        record("cancel")
        self.cancelled.set()
    def result(self):
        if self.settings.get("raise_result"):
            raise RuntimeError("fake result failure")
        output = Path(self.settings.get("output", self.spool / (self.settings.get("name", "out") + ".mp4")))
        if not self.settings.get("no_write"):
            output.write_bytes(b"video")
        failed = self.settings.get("failed", False)
        return SimpleNamespace(success=not failed, generated_files=[str(output)], errors=["generation failed"] if failed else [], total_tasks=1, successful_tasks=int(not failed), failed_tasks=int(failed))
class Session:
    def __init__(self, spool): self.spool = spool
    def submit_task(self, settings):
        record("submit", settings=settings)
        if settings.get("raise_submit"): raise RuntimeError("fake submit failure")
        return Job(settings, self.spool)
    def close(self): record("close")
def init(*, root, config_path, output_dir, **kwargs):
    print("upstream noise")
    os.write(1, b"native noise\\n")
    record("init", root=root, config_path=config_path, config=Path(config_path).read_text(), output_dir=output_dir, cwd=os.getcwd(), options=kwargs)
    if os.environ.get("FAKE_INIT_FAIL"): raise RuntimeError("fake init failure")
    return Session(Path(output_dir))
''')
    spool = tmp_path / "owner" / "spool"
    state = tmp_path / "state.jsonl"
    processes = []

    def start(*, init_fail=False, options=None, allow_user_env=True):
        # Existing session tests explicitly authorize setup in the first frame.
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
        env["FAKE_STATE"] = str(state)
        if init_fail:
            env["FAKE_INIT_FAIL"] = "1"
        proc = subprocess.Popen([sys.executable, "-m", "astrid.packs.wan2gp.src.driver", "--wan-session"],
                                cwd=tmp_path, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(proc)
        send(proc, {"birth": BIRTH, "root": str(root), "allow_user_env": allow_user_env,
                    "init": options or {
            "root": str(root), "config_path": str(root / "wgp_config.json"),
            "output_dir": str(spool), "console_output": False}})
        return proc

    yield start, root, spool, state
    for proc in processes:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=5)


def send(proc, frame):
    proc.stdin.write(json.dumps(frame) + "\n")
    proc.stdin.flush()


def read(proc):
    line = proc.stdout.readline()
    assert line, "worker closed protocol unexpectedly"
    return json.loads(line)


def request(proc, settings, identity=IDENTITY):
    send(proc, {"birth": BIRTH, "op": "run", "identity": identity, "settings": settings})


def records(state):
    return [json.loads(line) for line in state.read_text().splitlines()]


def test_serial_runs_progress_snapshots_and_close(worker):
    start, root, spool, state = worker
    proc = start()
    assert read(proc) == {"birth": BIRTH, "kind": "ready", "pid": proc.pid}
    for name in ("one", "two"):
        identity = {**IDENTITY, "invocation_id": name}
        request(proc, {"name": name}, identity)
        started, progress, terminal = read(proc), read(proc), read(proc)
        assert started["kind"] == "job_started" and started["identity"] == identity
        assert progress["event"] == {"kind": "progress", "data": {"phase": "sampling", "status": "running", "progress": 42, "current_step": 2, "total_steps": 5}}
        assert all(frame["birth"] == BIRTH and frame["native_job_id"] == started["native_job_id"] and frame["identity"] == identity for frame in (progress, terminal))
        assert terminal["kind"] == "result" and terminal["result"]["success"] is True
        assert terminal["result"]["generated_files"] == [str(spool / (name + ".mp4"))]
        assert terminal["output_snapshots"][0]["sha256"]
    send(proc, {"birth": BIRTH, "op": "close"})
    assert proc.wait(timeout=5) == 0
    events = records(state)
    assert [e["event"] for e in events] == ["init", "submit", "submit", "close"]
    init = events[0]
    assert init["root"] == init["cwd"] == str(root)
    assert init["output_dir"] == str(spool) and init["config"] == '{"config": 42}'
    scratch = Path(init["config_path"])
    assert scratch.name == "wgp_config.json" and not scratch.is_relative_to(root)
    assert not scratch.exists() and (root / "wgp_config.json").read_text() == '{"config": 42}'
    assert init["options"]["console_output"] is False
    assert "allow_user_env" not in init["options"]


def test_eof_and_cooperative_cancel(worker):
    start, _, _, state = worker
    proc = start()
    read(proc)
    request(proc, {"wait": True})
    started, progress = read(proc), read(proc)
    assert started["kind"] == "job_started" and progress["kind"] == "event"
    send(proc, {"birth": BIRTH, "op": "cancel", "identity": IDENTITY})
    assert read(proc)["kind"] == "cancel_requested"
    assert read(proc)["kind"] == "result"
    proc.stdin.close()
    proc.stdin = None
    assert proc.wait(timeout=5) == 0
    assert [e["event"] for e in records(state)] == ["init", "submit", "cancel", "close"]


def test_malformed_requests_and_submit_failure(worker):
    start, _, _, _ = worker
    proc = start()
    read(proc)
    proc.stdin.write('not json\n')
    proc.stdin.flush()
    assert "control request failed" in read(proc)["error"]
    send(proc, {"birth": "wrong", "op": "run"})
    assert "birth" in read(proc)["error"]
    request(proc, [])
    assert "settings object" in read(proc)["error"]
    request(proc, {"raise_submit": True})
    assert "submit_task failed: fake submit failure" in read(proc)["error"]
    send(proc, {"birth": BIRTH, "op": "close"})
    assert proc.wait(timeout=5) == 0


def test_containment_and_upstream_failures(worker, tmp_path):
    start, _, spool, _ = worker
    proc = start()
    read(proc)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    for path in (outside, spool / "link.mp4"):
        if path.parent == spool:
            path.symlink_to(outside)
        request(proc, {"output": str(path), "no_write": True})
        assert read(proc)["kind"] == "job_started"
        assert read(proc)["kind"] == "event"
        error = read(proc)
        assert error["kind"] == "error" and "output observation failed" in error["error"]
    request(proc, {"raise_result": True})
    read(proc); read(proc)
    assert "fake result failure" in read(proc)["error"]
    request(proc, {"failed": True})
    read(proc); read(proc)
    result = read(proc)
    assert result["result"]["success"] is False and result["result"]["errors"] == ["generation failed"]
    send(proc, {"birth": BIRTH, "op": "close"})
    assert proc.wait(timeout=5) == 0


def test_initialization_failure(worker):
    start, _, _, state = worker
    proc = start(init_fail=True)
    error = read(proc)
    assert error["kind"] == "error" and "fake init failure" in error["error"]
    assert proc.wait(timeout=5) == 1
    assert [e["event"] for e in records(state)] == ["init"]


def test_consent_fix_retries_only_a_new_explicitly_consented_session(worker):
    start, root, spool, state = worker
    proc = start(allow_user_env=False)
    error = read(proc)
    assert error["code"] == "wan2gp_init_requires_consent"
    assert error["status"] == "blocked" and error["kind"] == "error"
    fix, = error["fixes"]
    assert fix["scope"] == "user_env" and fix["requires_consent"] is True
    assert fix["root"] == str(root)
    assert proc.wait(timeout=5) == 1
    assert not state.exists() and not spool.exists()

    # The caller explicitly authorizes the returned fix for a fresh session.
    proc = start(**fix["retry"])
    assert read(proc)["kind"] == "ready"
    send(proc, {"birth": BIRTH, "op": "close"})
    assert proc.wait(timeout=5) == 0
    assert [event["event"] for event in records(state)] == ["init", "close"]
    assert "allow_user_env" not in records(state)[0]["options"]

    # A previous consented session does not grant permission to the next one.
    proc = start(allow_user_env=None)
    assert read(proc)["code"] == "wan2gp_init_requires_consent"
    assert proc.wait(timeout=5) == 1
    assert [event["event"] for event in records(state)] == ["init", "close"]
