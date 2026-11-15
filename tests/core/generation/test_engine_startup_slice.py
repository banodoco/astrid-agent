import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from astrid.core._shared import run_logs
from astrid.core.generation import engine_custody as custody


def _child(script):
    return subprocess.Popen([sys.executable, "-u", "-c", script], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)


def _wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def _supervisor(tmp_path, healthy):
    supervisor = custody._Supervisor.__new__(custody._Supervisor)
    supervisor.root = str(tmp_path)
    supervisor.identity = {"engine": "generic"}
    supervisor.recipe = SimpleNamespace(start=SimpleNamespace(port=0),
                                        health=SimpleNamespace(timeout_seconds=1, poll_interval_seconds=0.01))
    supervisor.unresolved = False
    supervisor.engine = None
    supervisor.token = "owner"
    supervisor.valid_launch = lambda launch: True
    supervisor.source_identity = lambda deadline, channel: ("revision", "digest")
    supervisor.launch = {"argv": [sys.executable], "cwd": "/", "env": {}}
    supervisor.base_launch = supervisor.launch
    # The supervisor binds its launch at admission from the platform adapter (13b4bf569);
    # this adapter yields the same fixed launch the payload asks for.
    supervisor.adapter = SimpleNamespace(start_request=lambda recipe, root, *, deadline, models_root: SimpleNamespace(
        argv=tuple(supervisor.launch["argv"]), cwd=supervisor.launch["cwd"], env=dict(supervisor.launch["env"])))

    def spawn(request, *, output_log=None):
        process = _child("import sys; sys.stdout.write('startup import error\\n'); sys.stdin.readline()")
        return SimpleNamespace(process=process,
                               stdout=custody._Output(process.stdout, 1024, output_log),
                               stderr=custody._Output(process.stderr, 1024, output_log))

    def health(channel, deadline):
        path = custody.engine_log_path(supervisor.root, "generic")
        _wait_for(lambda: "startup import error" in open(path).read())
        if not healthy:
            raise custody.EndpointHealthError("not ready")
        return {"status": "healthy"}

    def stop():
        supervisor.engine.process.stdin.write(b"stop\n")
        supervisor.engine.process.stdin.flush()
        supervisor.engine.process.wait(timeout=5)
        supervisor.engine.stdout.thread.join(5)
        supervisor.engine.stderr.thread.join(5)

    supervisor.spawn = spawn
    supervisor.health = health
    supervisor.stop_engine = stop
    supervisor.publish_registry = lambda source: None
    supervisor.check = lambda channel, deadline: None
    return supervisor


def _start(supervisor):
    payload = {"schema": custody._SCHEMA, "engine": "generic", "launch": supervisor.launch,
               "owner_id": None, "timeout_seconds": 1, "poll_interval_seconds": 0.01}
    return supervisor.service("start", payload, time.monotonic() + 5, None)


def test_startup_metadata_covers_launch_to_health(tmp_path):
    supervisor = _supervisor(tmp_path, healthy=True)
    path = custody.engine_log_path(str(tmp_path), "generic")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as stream:
        stream.write(b"old log")
    _start(supervisor)
    metadata = json.loads((tmp_path / "runtime/out/sessions/generic-logs/launch.json").read_text())
    assert metadata == {"schema": "engine-launch.v1", "launch": 7,
                        "ready": len(b"old logstartup import error\n")}
    supervisor.stop_engine()


def test_startup_slice_is_declared_and_collected(tmp_path):
    log = Path(custody.engine_log_path(str(tmp_path), "generic"))
    log.parent.mkdir(parents=True)
    log.write_bytes(b"a" * 100 + b"startup".ljust(50, b"!") + b"b" * 30)
    (log.parent / "launch.json").write_text(json.dumps({"schema": "engine-launch.v1", "launch": 100, "ready": 150}))
    out = tmp_path / "run"
    custody.declare_engine_log(out, tmp_path, "generic")
    declarations = {row.name: row for row, _ in run_logs.read_declared(out)}
    assert declarations["engine"].start == 180
    assert declarations["engine.startup"].start == 100
    assert declarations["engine.startup"].end == 150
    rows = run_logs.collect(out)
    startup = next(row for row in rows if row["name"] == "engine.startup")
    assert (out / "logs" / startup["file"]).read_bytes() == b"startup".ljust(50, b"!")


def test_missing_or_corrupt_metadata_declares_only_engine(tmp_path, monkeypatch):
    log = tmp_path / "engine.log"
    log.write_bytes(b"engine")
    monkeypatch.setattr(custody, "engine_log_path", lambda root, engine: str(log))
    for content in (None, "{"):
        out = tmp_path / ("run-missing" if content is None else "run-corrupt")
        metadata = tmp_path / "launch.json"
        if content is None:
            metadata.unlink(missing_ok=True)
        else:
            metadata.write_text(content)
        custody.declare_engine_log(out, tmp_path, "generic")
        assert [row.name for row, _ in run_logs.read_declared(out)] == ["engine"]


def test_failed_health_declares_open_startup_slice(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, healthy=False)
    log = custody.engine_log_path(str(tmp_path), "generic")
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    with open(log, "wb") as stream:
        stream.write(b"previous")
    try:
        _start(supervisor)
    except custody.EndpointHealthError:
        pass
    else:
        raise AssertionError("health failure was expected")
    metadata = json.loads((tmp_path / "runtime/out/sessions/generic-logs/launch.json").read_text())
    assert metadata == {"schema": "engine-launch.v1", "launch": 8, "ready": None}
    monkeypatch.setattr(custody, "engine_log_path", lambda root, engine: log)
    out = tmp_path / "run"
    custody.declare_engine_log(out, tmp_path, "generic")
    declarations = {row.name: row for row, _ in run_logs.read_declared(out)}
    assert declarations["engine.startup"].start == 8
    assert declarations["engine.startup"].end == len(b"previousstartup import error\n")


def test_a_failure_after_health_still_stops_the_engine_and_keeps_the_ready_offset(tmp_path):
    supervisor = _supervisor(tmp_path, healthy=True)
    stopped = []
    real_stop = supervisor.stop_engine

    def stop():
        stopped.append(True)
        real_stop()

    def refuse(source):
        raise custody.IntegrityError("registry refused")

    supervisor.stop_engine = stop
    supervisor.publish_registry = refuse
    try:
        _start(supervisor)
    except custody.IntegrityError:
        pass
    else:
        raise AssertionError("the registry failure was expected to propagate")
    assert stopped == [True]
    metadata = json.loads((tmp_path / "runtime/out/sessions/generic-logs/launch.json").read_text())
    assert metadata["ready"] == len(b"startup import error\n")
