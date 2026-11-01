"""The host-bound observation reaches the real renderer Timeline consumer."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import jsonschema
import pytest
import runtime_protocol

from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.rendering import _local_schema, _schema_observation as observation, transport
from astrid.core.timeline import banodoco_schema

RUN = "0123456789abcdef0123456789abcdef"
SCHEMA = REPO_ROOT / _local_schema.SCHEMA_RELATIVE_PATH


@pytest.fixture
def pipe():
    read, write = os.pipe()
    os.set_blocking(read, False)
    os.set_blocking(write, False)
    yield read, write
    os.close(read)
    os.close(write)


def read_events(fd):
    try:
        return [json.loads(line) for line in os.read(fd, 65536).splitlines()]
    except BlockingIOError:
        return []


def test_host_binding_replaces_overlay_and_uses_only_admitted_run(pipe):
    _, fd = pipe
    env = {observation.FD_ENV: "9", observation.RUN_ENV: "forged", observation.RENDER_ENV: "1"}
    assert observation.bind_host_context("rendering.render", RUN, env, {observation.FD_ENV: str(fd)}) == (fd,)
    assert env == {observation.FD_ENV: str(fd), observation.RUN_ENV: RUN}
    for capability, run, host in [("other", RUN, {observation.FD_ENV: str(fd)}),
                                  ("rendering.render", None, {observation.FD_ENV: str(fd)}),
                                  ("rendering.render", RUN, {})]:
        env = {observation.FD_ENV: str(fd), observation.RUN_ENV: RUN}
        assert observation.bind_host_context(capability, run, env, host) == ()
        assert env == {}


@pytest.mark.parametrize("verb,expected", [("render", True), ("support", False), ("plan", False)])
def test_transport_only_inherits_context_for_render(monkeypatch, tmp_path, pipe, verb, expected):
    _, fd = pipe
    monkeypatch.setenv(observation.FD_ENV, str(fd))
    monkeypatch.setenv(observation.RUN_ENV, RUN)
    captured = {}
    def launch(argv, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("launch captured")
    monkeypatch.setattr(transport, "popen_owned_process", launch)
    with pytest.raises(RuntimeError, match="launch captured"):
        transport.CommandTransport("rendering.remotion").run(
            verb, [sys.executable], request_path=tmp_path / "request", result_path=tmp_path / "result", cwd=tmp_path)
    assert captured["pass_fds"] == ((fd,) if expected else ())
    assert captured["env"].get(observation.RUN_ENV) == (RUN if expected else None)
    assert captured["env"].get(observation.FD_ENV) == (str(fd) if expected else None)
    assert captured["env"].get(observation.RENDER_ENV) == ("1" if expected else None)


def test_renderer_timeline_consumer_emits_exact_live_identity_and_document(tmp_path, pipe):
    read, write = pipe
    installed = tmp_path / "installed-agent"
    installed.mkdir()
    shutil.copytree(REPO_ROOT / "astrid", installed / "astrid", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(REPO_ROOT / "runtime.lock.json", installed / "runtime.lock.json")
    shutil.copy2(REPO_ROOT / "pyproject.toml", installed / "pyproject.toml")
    shutil.copytree(Path(runtime_protocol.__file__).parent, installed / "runtime_protocol",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    installed_schema = installed / _local_schema.SCHEMA_RELATIVE_PATH
    timeline = tmp_path / "timeline.json"
    timeline.write_text('{"clips":[],"tracks":[]}')
    env = dict(os.environ, ASTRID_TIMELINE_SCHEMA_PYTHONPATH=str(installed_schema),
               ASTRID_HOST_EXECUTION_PROFILE="installed")
    assert observation.bind_host_context("rendering.render", RUN, env, {observation.FD_ENV: str(write)}) == (write,)
    env[observation.RENDER_ENV] = "1"  # Set only by the renderer transport.
    script = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
              "from astrid.packs.rendering.shared.rendering_backend import _serialize_timeline; "
              "import astrid; assert sys.flags.isolated and Path(astrid.__file__).is_relative_to(Path(sys.argv[1])); "
              "_serialize_timeline(Path(sys.argv[2])); "
              "print('consumed', flush=True); sys.stdin.read(1)")
    before = time.monotonic_ns()
    process = subprocess.Popen([sys.executable, "-I", "-c", script, str(installed), str(timeline)], env=env,
                               pass_fds=(write,), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "consumed", process.stderr.read()
        after = time.monotonic_ns()
        events = read_events(read)
        assert events
        start = int(Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        schema = json.loads(installed_schema.read_text())
        digest = hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        origin = hashlib.sha256(str(installed / "astrid/core/timeline/banodoco_schema.py").encode()).hexdigest()
        for event in events:
            assert event == {"event": "timeline_schema_consumed", "run_id": RUN,
                             "monotonic_ns": event["monotonic_ns"], "pid": process.pid,
                             "start_ticks": start, "boot_id": boot, "schema_sha256": digest,
                             "schema_reader_origin_sha256": origin}
            assert before <= event["monotonic_ns"] <= after
    finally:
        process.communicate(input="x", timeout=15)
    assert process.returncode == 0


@pytest.mark.parametrize("run", [None, "", "task-1", "A" * 32, "0" * 31, "0" * 33])
def test_missing_or_malformed_kernel_run_emits_nothing(monkeypatch, pipe, run):
    read, write = pipe
    monkeypatch.setenv(observation.FD_ENV, str(write))
    monkeypatch.setenv(observation.RENDER_ENV, "1")
    if run is None:
        monkeypatch.delenv(observation.RUN_ENV, raising=False)
    else:
        monkeypatch.setenv(observation.RUN_ENV, run)
    observation.emit_schema_consumed({}, str(SCHEMA))
    assert read_events(read) == []


def test_missing_bad_or_unavailable_sink_refuses_without_breaking(monkeypatch, pipe, tmp_path):
    read, write = pipe
    monkeypatch.setenv(observation.RUN_ENV, RUN)
    monkeypatch.setenv(observation.RENDER_ENV, "1")
    regular = tmp_path / "not-a-pipe"
    with regular.open("w") as handle:
        for raw in [None, "", "0", "-1", "abc", str(read), str(handle.fileno()), "999999999999999999999"]:
            if raw is None:
                monkeypatch.delenv(observation.FD_ENV, raising=False)
            else:
                monkeypatch.setenv(observation.FD_ENV, raw)
            observation.emit_schema_consumed({}, str(SCHEMA))
        assert regular.read_text() == ""
    assert read_events(read) == []
    os.set_blocking(write, True)
    assert observation.observation_context(RUN, str(write)) is None
    os.set_blocking(write, False)
    monkeypatch.setenv(observation.FD_ENV, str(write))
    while True:
        try:
            os.write(write, b"x" * 4096)
        except BlockingIOError:
            break
    observation.emit_schema_consumed({}, str(SCHEMA))
    assert b'timeline_schema_consumed' not in os.read(read, 65536)


def test_refuses_bad_process_identity_and_failed_validation(monkeypatch, pipe):
    read, write = pipe
    monkeypatch.setenv(observation.FD_ENV, str(write))
    monkeypatch.setenv(observation.RUN_ENV, RUN)
    monkeypatch.setenv(observation.RENDER_ENV, "1")
    original = Path.read_text
    def bad_boot(path, *args, **kwargs):
        return "bad" if str(path) == "/proc/sys/kernel/random/boot_id" else original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", bad_boot)
    observation.emit_schema_consumed({}, str(SCHEMA))
    assert read_events(read) == []
    monkeypatch.setattr(Path, "read_text", original)
    monkeypatch.setenv("ASTRID_TIMELINE_SCHEMA_PYTHONPATH", str(SCHEMA))
    with pytest.raises(jsonschema.ValidationError):
        banodoco_schema._validate_shared_timeline({"clips": "bad", "tracks": []})
    assert read_events(read) == []


def test_probe_and_failed_schema_verification_do_not_emit(monkeypatch, pipe):
    read, write = pipe
    monkeypatch.setenv(observation.FD_ENV, str(write))
    monkeypatch.setenv(observation.RUN_ENV, RUN)
    monkeypatch.setenv(observation.RENDER_ENV, "1")
    _local_schema.load_verified_schema(REPO_ROOT, str(SCHEMA))
    assert read_events(read) == []
    monkeypatch.setenv("ASTRID_TIMELINE_SCHEMA_PYTHONPATH", "/foreign/schema.json")
    with pytest.raises(ValueError):
        banodoco_schema._validate_shared_timeline({"tracks": [], "clips": []})
    assert read_events(read) == []


def test_action_validation_is_not_renderer_observation(monkeypatch, pipe):
    read, write = pipe
    monkeypatch.setenv(observation.FD_ENV, str(write))
    monkeypatch.setenv(observation.RUN_ENV, RUN)
    monkeypatch.setenv("ASTRID_TIMELINE_SCHEMA_PYTHONPATH", str(SCHEMA))
    monkeypatch.delenv(observation.RENDER_ENV, raising=False)
    banodoco_schema._validate_shared_timeline({"tracks": [], "clips": []})
    assert read_events(read) == []


def test_malformed_schema_or_origin_and_broken_pipe_refuse(monkeypatch, pipe):
    read, write = pipe
    monkeypatch.setenv(observation.FD_ENV, str(write))
    monkeypatch.setenv(observation.RUN_ENV, RUN)
    monkeypatch.setenv(observation.RENDER_ENV, "1")
    for schema, origin in [(None, str(SCHEMA)), ({}, ""), ({}, "relative"),
                           ({"bad": float("nan")}, str(SCHEMA))]:
        observation.emit_schema_consumed(schema, origin)
    assert read_events(read) == []
    dead_read, dead_write = os.pipe()
    os.set_blocking(dead_write, False)
    os.close(dead_read)
    try:
        monkeypatch.setenv(observation.FD_ENV, str(dead_write))
        observation.emit_schema_consumed({}, str(SCHEMA))
    finally:
        os.close(dead_write)
