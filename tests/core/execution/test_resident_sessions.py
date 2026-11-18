"""LS-8: admitted resident pack actions retain one owned, fenced worker."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from astrid.core.pack.canonical import CanonicalPackValidationError, validate_canonical_pack


SESSION = {"kind": "resident", "protocol": "astrid.pack-session.v1"}


def write_pack(tmp_path, *, session=SESSION, code=None):
    root = tmp_path / "packs" / "resident_probe"
    (root / "actions").mkdir(parents=True, exist_ok=True)
    (root / "actions" / "run.py").write_text(
        code or "def run(value='default'):\n    return value\n", encoding="utf-8"
    )
    action = {
        "description": "An admitted resident test action.",
        "invocation": {"kind": "python", "path": "actions/run.py", "function": "run"},
        "inputs": [{"name": "value", "type": "string", "required": False, "default": "default"}],
        "outputs": {},
        "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"], "project_scope": "optional"},
    }
    if session is not None:
        action["session"] = session
    (root / "pack.yaml").write_text(json.dumps({
        "schema_version": 3, "id": "resident_probe", "name": "Resident probe",
        "version": "1.0.0", "pack_api": "2.0", "actions": {"run": action},
    }), encoding="utf-8")
    return root


def test_manifest_declares_resident_protocol_without_importing_pack(tmp_path):
    marker = tmp_path / "imported"
    root = write_pack(tmp_path, code=f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef run(value):\n    return value\n")
    entry = validate_canonical_pack(root)
    assert dict(entry.definition.actions["run"]["session"]) == SESSION
    assert not marker.exists()


@pytest.mark.parametrize("session", [
    {"kind": "resident", "protocol": "unknown"},
    {"kind": "resident", "protocol": "astrid.pack-session.v1", "concurrent": True},
    {"kind": "resident"},
    "resident",
])
def test_manifest_refuses_unknown_resident_contract(tmp_path, session):
    with pytest.raises(CanonicalPackValidationError):
        validate_canonical_pack(write_pack(tmp_path, session=session))


def test_ordinary_manifest_has_no_resident_declaration(tmp_path):
    entry = validate_canonical_pack(write_pack(tmp_path, session=None))
    assert "session" not in entry.definition.actions["run"]


@pytest.fixture
def resident_host(tmp_path, monkeypatch):
    from astrid.core.execution.generic_host import GenericPackHost
    from tests.test_generic_host import FakeRuntime

    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "no-source-state.json"))
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "runtime-data"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    root = write_pack(tmp_path, code="""import os
calls = 0
def run(value='default'):
    global calls
    calls += 1
    return {'value': value, 'pid': os.getpid(), 'calls': calls}
""")
    class Runtime(FakeRuntime):
        def heartbeat(self, task_id, lease_token, *, attempt_id, fence, progress=None):
            super().heartbeat(task_id, lease_token, attempt_id=attempt_id, fence=fence, progress=progress)
            return {"task_id": task_id, "lease_id": lease_token, "attempt_id": attempt_id,
                    "fence": fence, "runtime_epoch": 1,
                    "lease_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}

    runtime = Runtime()
    host = GenericPackHost(pack_roots=[root], client=runtime, attempt_base=tmp_path / "attempts")
    assert [row.id for row in host.discover()] == ["resident_probe.run"]
    host.register()
    try:
        yield root, host, runtime
    finally:
        if getattr(runtime, "expect_unproved_release", False):
            from astrid.core.execution.pipe_io import close_pipe_workers
            from astrid.core.execution.process_group import CleanupUncertainError

            adapter = host.managed_tool_session.current_adapter
            with pytest.raises(CleanupUncertainError, match="cleanup remains unresolved"):
                host.shutdown()
            assert adapter.process.poll() is not None
            assert host.managed_tool_session.occupied and not host.managed_tool_session.active
            # Test-only disposal after the independent host scope stop. Do not
            # clear the poisoned slot or fabricate successful release evidence.
            close_pipe_workers(adapter._reader, adapter._writer, adapter._stderr_reader)
            for stream in (adapter.process.stdin, adapter.process.stdout, adapter.process.stderr):
                stream.close()
            adapter._captures.close()
        else:
            host.shutdown()


def run_job(host, runtime, number, *, value=None, project="demo", keep_attempt=False, authority=None):
    task_id = f"resident-task-{number}"
    task = {"task": {
        "id": task_id, "capability": "resident_probe.run", "project_id": project,
        "attempt_id": f"attempt-{number}", "fence": number, "runtime_epoch": 1,
        "status": "running", "spec": {"spec": {"inputs": {"value": value or str(number)}}},
    }}
    runtime.tasks[task_id] = task
    if authority is not None:
        task["task"]["spec"]["spec"]["authority_context"] = authority
    assert host.run_task(task, lease_token=f"lease-{number}", keep_attempt=keep_attempt)["task"]["status"] == "completed"
    return next(item[2]["result"] for item in runtime.settlements if item[0] == task_id)


def test_two_admitted_jobs_reuse_one_worker_without_another_process_start(resident_host, monkeypatch):
    import subprocess

    _, host, runtime = resident_host
    from astrid.core.execution import generic_host, task_scope

    started = []
    all_starts = []
    spawn = task_scope.spawn
    popen_init = subprocess.Popen.__init__

    def record_start(self, *args, **kwargs):
        all_starts.append(args[0] if args else kwargs.get("args"))
        popen_init(self, *args, **kwargs)

    def track_spawn(*args, **kwargs):
        process = spawn(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(task_scope, "spawn", track_spawn)
    monkeypatch.setattr(generic_host, "spawn_task_scope", track_spawn)
    monkeypatch.setattr(subprocess.Popen, "__init__", record_start)
    first = run_job(host, runtime, 1)["action_result"]
    births_after_first = len(started)
    processes_after_first = len(all_starts)
    second = run_job(host, runtime, 2)["action_result"]
    assert first["pid"] != os.getpid()
    assert second["pid"] == first["pid"]
    assert (first["calls"], second["calls"]) == (1, 2)
    assert (first["value"], second["value"]) == ("1", "2")
    assert births_after_first == 1 and len(started) == 1
    assert len(all_starts) == processes_after_first, all_starts[processes_after_first:]


@pytest.mark.parametrize("change", ["source", "installation", "kill", "project"])
def test_changed_identity_or_dead_worker_requires_a_fresh_session(resident_host, change, tmp_path):
    from astrid.core.execution import generic_host
    from astrid.core._shared.boot_manifest import build_manifest

    root, host, runtime = resident_host
    if change == "installation":
        host.boot_manifest_path = tmp_path / "support" / "astrid-host" / "boot-manifest.json"
        host.boot_manifest_path.parent.mkdir(parents=True)
        manifest = build_manifest(registry={}, fixtures=(), profile_order=())
        host.boot_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    first = run_job(host, runtime, 1)["action_result"]
    old = host.managed_tool_session.current_adapter
    snapshot = old.record.source_root
    assert snapshot != root and snapshot.is_dir()
    assert old.process.poll() is None
    if change == "source":
        path = root / "actions" / "run.py"
        path.write_text(path.read_text(encoding="utf-8").replace("'value': value", "'value': 'changed:' + value"), encoding="utf-8")
        host.discover()
        host.register(deliberate=True)
    elif change == "installation":
        manifest["registry_digest"] = "2" * 64
        host.boot_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif change == "kill":
        generic_host._terminate_process_group(old.process, grace_seconds=0.2)
    second = run_job(host, runtime, 2, project="another" if change == "project" else "demo")["action_result"]
    assert old.process.poll() is not None
    assert not snapshot.exists()
    assert second["pid"] != first["pid"] and second["calls"] == 1
    assert second["value"] == ("changed:2" if change == "source" else "2")
    assert host.managed_tool_session.current_adapter is not old


def test_undeclared_action_gets_a_new_process_for_every_job(resident_host, monkeypatch):
    from astrid.core.execution import generic_host

    root, host, runtime = resident_host
    manifest = root / "pack.yaml"
    declaration = json.loads(manifest.read_text(encoding="utf-8"))
    del declaration["actions"]["run"]["session"]
    manifest.write_text(json.dumps(declaration), encoding="utf-8")
    host.discover()
    host.register(deliberate=True)
    started = []
    spawn = generic_host.spawn_task_scope
    def track(*args, **kwargs):
        process = spawn(*args, **kwargs)
        started.append(process)
        return process
    monkeypatch.setattr(generic_host, "spawn_task_scope", track)
    first = run_job(host, runtime, 1)["action_result"]
    second = run_job(host, runtime, 2)["action_result"]
    assert first["pid"] != second["pid"]
    assert first["calls"] == second["calls"] == 1
    assert len(started) == 2 and all(process.poll() is not None for process in started)
    assert not host.managed_tool_session.occupied


def test_canonical_request_authority_change_cannot_reuse_the_previous_worker(resident_host):
    _, host, runtime = resident_host
    first = run_job(host, runtime, 1, authority={"scope": "one"})["action_result"]
    old = host.managed_tool_session.current_adapter
    second = run_job(host, runtime, 2, authority={"scope": "two"})["action_result"]
    assert second["pid"] != first["pid"] and second["calls"] == 1
    assert old.process.poll() is not None


def test_runtime_envelope_authority_change_cannot_reuse_the_previous_worker(resident_host, monkeypatch):
    _, host, runtime = resident_host
    run_task = host.run_task

    def with_runtime_authority(task, *args, **kwargs):
        spec = task["task"]["spec"]
        spec["authority_context"] = spec["spec"].pop("authority_context")
        return run_task(task, *args, **kwargs)

    monkeypatch.setattr(host, "run_task", with_runtime_authority)
    first = run_job(host, runtime, 1, authority={"scope": "one"})["action_result"]
    old = host.managed_tool_session.current_adapter
    second = run_job(host, runtime, 2, authority={"scope": "two"})["action_result"]
    assert second["pid"] != first["pid"] and second["calls"] == 1
    assert old.process.poll() is not None


def test_manifest_named_secret_is_redacted_from_each_jobs_log(resident_host, monkeypatch):
    root, host, runtime = resident_host
    secret = "fixture-private-value-12345"
    monkeypatch.setenv("LS8_AUTH", secret)
    manifest = json.loads((root / "pack.yaml").read_text(encoding="utf-8"))
    manifest["actions"]["run"]["secrets"] = ["LS8_AUTH"]
    (root / "pack.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    install_action(resident_host, """import os
from astrid.sdk import current_pack_job
def run(value='default'):
    print(os.environ['LS8_AUTH'])
    return {'root': str(current_pack_job().attempt_root), 'pid': os.getpid()}
""")
    first = run_job(host, runtime, 1, keep_attempt=True)["action_result"]
    second = run_job(host, runtime, 2, keep_attempt=True)["action_result"]
    assert first["pid"] == second["pid"]
    for item in (first, second):
        log = (Path(item["root"]) / "task-stdout-tail.log").read_text(encoding="utf-8")
        assert secret not in log
        assert "<redacted>" in log


def test_source_edit_retires_idle_worker_even_when_new_admission_is_refused(resident_host):
    from astrid.core.execution.generic_host import HostError

    root, host, runtime = resident_host
    run_job(host, runtime, 1)
    old = host.managed_tool_session.current_adapter
    path = root / "actions" / "run.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# changed source\n", encoding="utf-8")
    host.discover()
    host._runtime_registration_fence = "registration invalidated after observed source change"
    with pytest.raises(HostError, match="registration invalidated|source.*changed"):
        run_job(host, runtime, 2)
    assert old.process.poll() is not None
    assert not host.managed_tool_session.occupied
    assert len(runtime.settlements) == 1


@pytest.mark.parametrize("field", ["token_id", "invocation_id", "generation", "binding_identity", "attempt_id", "fence", "runtime_epoch"])
def test_another_jobs_terminal_evidence_cannot_settle_this_job(resident_host, monkeypatch, field):
    from astrid.core.execution.resident_sessions import ResidentPackAdapter, ResidentProtocolError

    _, host, runtime = resident_host
    read = ResidentPackAdapter._read
    first_frame = None
    def wrong_evidence(self, *args):
        nonlocal first_frame
        frame = read(self, *args)
        if frame["kind"] == "result":
            if first_frame is None:
                first_frame = json.loads(json.dumps(frame))
            elif frame["job"]["attempt_id"] == "attempt-2":
                if field in {"attempt_id", "fence", "runtime_epoch"}:
                    frame["job"][field] = (2 if field == "runtime_epoch" else first_frame["job"][field])
                elif field == "generation":
                    frame["identity"][field] += 1
                elif field == "binding_identity":
                    frame["identity"][field][0] = "another-session"
                else:
                    frame["identity"][field] = first_frame["identity"][field]
        return frame
    monkeypatch.setattr(ResidentPackAdapter, "_read", wrong_evidence)
    first = run_job(host, runtime, 1)["action_result"]
    old = host.managed_tool_session.current_adapter
    with pytest.raises(ResidentProtocolError, match="another admission"):
        run_job(host, runtime, 2)
    assert [row[0] for row in runtime.settlements] == ["resident-task-1"]
    assert [row[0] for row in runtime.failures] == ["resident-task-2"]
    assert old.process.poll() is not None
    third = run_job(host, runtime, 3)["action_result"]
    assert third["pid"] != first["pid"] and third["calls"] == 1


def test_consumed_token_cannot_cancel_or_dispatch_the_next_job(resident_host, monkeypatch):
    from astrid.core.execution.managed_tool_session import StaleAdmissionError
    from astrid.core.execution.resident_sessions import ResidentPackAdapter

    _, host, runtime = resident_host
    invoke, tokens = ResidentPackAdapter.invoke, []
    def checked(self, token, **kwargs):
        if tokens:
            with pytest.raises(StaleAdmissionError):
                host.managed_tool_session.cancel(tokens[0], outcome="confirmed")
            with pytest.raises(StaleAdmissionError):
                host.managed_tool_session.adapter_for(tokens[0], begin=True)
            with pytest.raises(StaleAdmissionError):
                host.managed_tool_session.fence_admission(tokens[0], reason="late failure")
        tokens.append(token)
        return invoke(self, token, **kwargs)
    monkeypatch.setattr(ResidentPackAdapter, "invoke", checked)
    first = run_job(host, runtime, 1)["action_result"]
    second = run_job(host, runtime, 2)["action_result"]
    assert first["pid"] == second["pid"] and second["calls"] == 2
    assert tokens[0].token_id != tokens[1].token_id


def test_idle_expiry_cannot_release_a_leased_resident_worker(resident_host, monkeypatch):
    from astrid.core.execution.warm_hold import WarmHolds
    from tests.core.execution.test_warm_hold import Timers

    _, host, runtime = resident_host
    timers = Timers()
    host.warm_holds.close()
    host.warm_holds = WarmHolds(timer=timers)
    first = run_job(host, runtime, 1)["action_result"]
    old = host.managed_tool_session.current_adapter
    expire = timers.live().callback
    invoke = old.invoke
    def during_job(*args, **kwargs):
        expire()  # A cancelled timer already waiting on the owner's lock.
        assert old.process.poll() is None
        assert host.warm_holds.status()[0]["leases"] == 1
        return invoke(*args, **kwargs)
    monkeypatch.setattr(old, "invoke", during_job)
    assert run_job(host, runtime, 2)["action_result"]["pid"] == first["pid"]
    timers.fire()
    assert old.process.poll() is not None and not host.managed_tool_session.occupied
    assert not old.record.source_root.exists()
    assert run_job(host, runtime, 3)["action_result"]["pid"] != first["pid"]


def test_unproved_release_keeps_the_slot_poisoned_and_occupied(resident_host, monkeypatch):
    from astrid.core.execution import generic_host
    from astrid.core.execution.generic_host import HostError
    from astrid.core.execution.managed_tool_session import SessionCapacityError

    _, host, runtime = resident_host
    run_job(host, runtime, 1)
    manager = host.managed_tool_session
    adapter, generation = manager.current_adapter, manager.generation
    terminate = generic_host._terminate_process_group
    with monkeypatch.context() as inject:
        def unproved(process, **kwargs):
            assert process is adapter.process
            raise RuntimeError("injected native absence uncertainty")
        inject.setattr(generic_host, "_terminate_process_group", unproved)
        receipt = host.release_managed_session(expected_binding=adapter.binding, expected_generation=generation)
        assert receipt == {"ok": False, "released": False, "release_pending": True}
    runtime.expect_unproved_release = True
    assert manager.occupied and not manager.active and manager.current_adapter is adapter
    assert adapter.process.poll() is None and adapter.record.source_root.exists()
    with pytest.raises((SessionCapacityError, HostError), match="release|cleanup|registration"):
        run_job(host, runtime, 2)
    assert manager.current_adapter is adapter and manager.occupied
    assert len(runtime.settlements) == 1
    # Native host shutdown still stops the owned scope, but does not clear the
    # poisoned session or turn the failed proof into permission to start B.
    terminate(adapter.process, grace_seconds=0.2)


@pytest.mark.parametrize("raw", [b'{}\n', b'{"protocol":"astrid.pack-session.v1","x":1,"x":2}\n',
                                  b'{"protocol":"astrid.pack-session.v1","x":NaN}\n', b'{"protocol":"astrid.pack-session.v1"}'])
def test_protocol_refuses_unversioned_ambiguous_nonfinite_or_partial_frames(raw):
    from astrid.core.execution.resident_sessions import ResidentProtocolError, decode_frame

    with pytest.raises(ResidentProtocolError):
        decode_frame(raw)


def test_protocol_bounds_requests_before_dispatch():
    from astrid.core.execution.resident_sessions import MAX_FRAME_BYTES, PROTOCOL, ResidentProtocolError, encode_frame

    with pytest.raises(ResidentProtocolError, match="bound"):
        encode_frame({"protocol": PROTOCOL, "request": "x" * MAX_FRAME_BYTES})


def install_action(resident_host, code):
    root, host, _ = resident_host
    (root / "actions" / "run.py").write_text(code, encoding="utf-8")
    host.discover()
    host.register(deliberate=True)


def test_job_context_environment_and_network_authority_do_not_leak_between_jobs(resident_host, monkeypatch):
    _, host, runtime = resident_host
    runtime.endpoint, runtime.credential = "http://fixture.invalid", "fixture-runtime-credential"
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", runtime.credential)
    monkeypatch.setenv("UNDECLARED_SECRET", "fixture-undisclosed-secret")
    install_action(resident_host, """import os
import threading
import socket
from contextvars import copy_context
from astrid.sdk import current_pack_job as current_job, PackSessionError as ResidentProtocolError
old = None
def run(value='default'):
    global old
    job = current_job()
    expired = None
    if old is not None:
        try:
            old.run(current_job)
        except ResidentProtocolError:
            expired = True
        else:
            expired = False
    background = []
    def probe():
        try:
            current_job()
        except ResidentProtocolError:
            background.append('no job')
        try:
            with socket.socket() as connection:
                connection.connect(('127.0.0.1', 9))
        except RuntimeError:
            background.append(False)
        else:
            background.append(True)
    thread = threading.Thread(target=probe)
    thread.start()
    thread.join()
    previous = os.environ.get('LS8_JOB_VALUE')
    os.environ['LS8_JOB_VALUE'] = value
    old = copy_context()
    print('stdout for ' + job.attempt_id)
    return {'pid': os.getpid(), 'task': job.task_id, 'attempt': job.attempt_id,
            'fence': job.fence, 'epoch': job.runtime_epoch, 'root': str(job.attempt_root),
            'out': str(job.output_root), 'cwd': os.getcwd(), 'previous': previous,
            'runtime_credential': os.environ.get('ASTRID_RUNTIME_CREDENTIAL'),
            'undeclared': os.environ.get('UNDECLARED_SECRET'), 'expired': expired,
            'background': background}
""")
    first = run_job(host, runtime, 1, keep_attempt=True)["action_result"]
    second = run_job(host, runtime, 2, keep_attempt=True)["action_result"]
    assert first["pid"] == second["pid"]
    assert first["root"] != second["root"] and first["out"] != second["out"]
    assert first["expired"] is None and second["expired"] is True
    for number, item in enumerate((first, second), 1):
        assert (item["task"], item["attempt"], item["fence"], item["epoch"]) == (
            f"resident-task-{number}", f"attempt-{number}", number, 1)
        assert item["cwd"] == item["root"] and Path(item["out"]).parent == Path(item["root"])
        assert item["previous"] is item["runtime_credential"] is item["undeclared"] is None
        assert item["background"] == ["no job", False]
    logs = [(Path(item["root"]) / "task-stdout-tail.log").read_bytes() for item in (first, second)]
    assert b"stdout for attempt-1" in b"\n".join(logs)
    assert b"stdout for attempt-2" in b"\n".join(logs)
    assert all(not (b"attempt-1" in log and b"attempt-2" in log) for log in logs)


def test_concurrent_callers_keep_serial_dispatch_and_separate_job_contexts(resident_host):
    from concurrent.futures import ThreadPoolExecutor

    _, host, runtime = resident_host
    install_action(resident_host, """import os
import time
from astrid.sdk import current_pack_job as current_job
active = 0
def run(value='default'):
    global active
    active += 1
    assert active == 1
    job = current_job()
    time.sleep(0.05)
    assert current_job() is job
    active -= 1
    return {'pid': os.getpid(), 'attempt': job.attempt_id, 'value': value}
""")
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(run_job, host, runtime, 1)
        two = pool.submit(run_job, host, runtime, 2)
        first, second = one.result(timeout=30)["action_result"], two.result(timeout=30)["action_result"]
    assert first["pid"] == second["pid"]
    assert first["attempt"] == "attempt-1" and first["value"] == "1"
    assert second["attempt"] == "attempt-2" and second["value"] == "2"


def test_cancel_sends_only_the_current_token_and_proves_stop_before_runtime_ack(resident_host, monkeypatch, tmp_path):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from astrid.core.execution.resident_sessions import ResidentPackAdapter

    _, host, runtime = resident_host
    marker = tmp_path / "entered"
    install_action(resident_host, """import time
from pathlib import Path
from astrid.sdk import current_pack_job as current_job
def run(value='default'):
    job = current_job()
    Path(value).write_text(job.attempt_id)
    while not job.cancelled():
        time.sleep(0.01)
    return {'cancelled': True}
""")
    cancel = threading.Event()
    invoke, write = ResidentPackAdapter.invoke, ResidentPackAdapter._write
    frames, acknowledgements = [], []
    def cancellable(self, token, **kwargs):
        original = kwargs["cancelled"]
        kwargs["cancelled"] = lambda: cancel.is_set() or original()
        return invoke(self, token, **kwargs)
    def record(self, frame, deadline):
        frames.append(frame)
        return write(self, frame, deadline)
    def acknowledge(task_id, **kwargs):
        assert host.managed_tool_session.current_adapter.process.poll() is not None
        acknowledgements.append((task_id, kwargs))
    monkeypatch.setattr(ResidentPackAdapter, "invoke", cancellable)
    monkeypatch.setattr(ResidentPackAdapter, "_write", record)
    runtime.cancel = acknowledge
    task = {"task": {"id": "resident-task-1", "capability": "resident_probe.run", "project_id": "demo",
                     "attempt_id": "attempt-1", "fence": 1, "runtime_epoch": 1, "status": "running",
                     "spec": {"spec": {"inputs": {"value": str(marker)}}}}}
    runtime.tasks["resident-task-1"] = task
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(host.run_task, task, lease_token="lease-1")
        try:
            deadline = time.monotonic() + 15
            while not marker.exists() and time.monotonic() < deadline and not future.done():
                time.sleep(0.01)
            assert marker.read_text() == "attempt-1"
        finally:
            cancel.set()
        assert future.result(timeout=15)["status"] == "cancelled"
    job_frame = next(frame for frame in frames if frame["kind"] == "job")
    cancel_frames = [frame for frame in frames if frame["kind"] == "cancel"]
    assert len(cancel_frames) == 1 and cancel_frames[0]["identity"] == job_frame["identity"]
    assert len(acknowledgements) == 1 and acknowledgements[0][1]["stop_observed"] is True
    assert not runtime.settlements and not host.managed_tool_session.occupied


def test_runtime_selected_source_descriptor_stays_with_the_resident_capture(resident_host, monkeypatch):
    import base64
    import hashlib
    from runtime_protocol.verified_host_launch import canonical
    from astrid.core.execution import generic_host

    root, host, runtime = resident_host
    record = host.capabilities["resident_probe.run"]
    contents = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    chosen = {"path": "pack.yaml", "capability_kind": "executor", "capability_id": record.id,
              "definition_digest": record.capability_digest}
    selected = canonical({"scope": {}, "selected_profile_digest": "0" * 64, "definition": chosen,
                          "source": {"root": str(root), "definitions": [chosen],
                                     "contents": {name: base64.b64encode(data).decode() for name, data in contents.items()},
                                     "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}}})
    # Fixed authority fixture; the production descriptor, native FD/handle
    # transfer, worker byte validation and definition relocation remain real.
    custody = object.__new__(generic_host._RuntimeSourceCustody)
    def verify():
        assert {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()} == contents
    def select(candidate, **kwargs):
        verify()
        assert candidate.id == record.id and candidate.capability_digest == record.capability_digest
        return selected
    custody.verify, custody.select = verify, select
    monkeypatch.setattr(generic_host, "_runtime_external_sources", custody)
    first = run_job(host, runtime, 1)["action_result"]
    adapter = host.managed_tool_session.current_adapter
    assert adapter.record.source_root != root and adapter.record.source_root.is_dir()
    second = run_job(host, runtime, 2)["action_result"]
    assert second["pid"] == first["pid"] and second["calls"] == 2
    host.shutdown()
    assert adapter.process.poll() is not None and not adapter.record.source_root.exists()


@pytest.mark.parametrize("fault", ["source", "installation", "kill"])
def test_inflight_identity_loss_fails_once_without_replay(resident_host, tmp_path, fault):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from astrid.core.execution import generic_host
    from astrid.core.execution.managed_tool_session import StaleAdmissionError
    from astrid.core.execution.resident_sessions import ResidentProtocolError

    root, host, runtime = resident_host
    marker = tmp_path / "dispatches"
    install_action(resident_host, """import os
import time
from pathlib import Path
calls = 0
def run(value='default'):
    global calls
    calls += 1
    if value == 'done':
        return {'pid': os.getpid(), 'calls': calls}
    with Path(value).open('a') as stream:
        stream.write('dispatch\\n')
    while True:
        time.sleep(0.01)
""")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_job, host, runtime, 1, value=str(marker))
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline and not future.done():
            time.sleep(0.01)
        old = host.managed_tool_session.current_adapter
        try:
            assert marker.read_text() == "dispatch\n"
            if fault == "source":
                path = root / "actions" / "run.py"
                path.write_text(path.read_text(encoding="utf-8") + "\n# changed during invocation\n", encoding="utf-8")
            elif fault == "installation":
                host.source_inventory_identity = "changed-installation-inventory"
            else:
                generic_host._terminate_process_group(old.process, grace_seconds=0.2)
            with pytest.raises((generic_host.HostError, StaleAdmissionError, ResidentProtocolError),
                               match="source|installation|exited|terminal|fenced"):
                future.result(timeout=15)
        finally:
            if old is not None and old.process.poll() is None:
                generic_host._terminate_process_group(old.process, grace_seconds=0.2)
    assert marker.read_text() == "dispatch\n"
    assert not runtime.settlements and [row[0] for row in runtime.failures] == ["resident-task-1"]
    assert old.process.poll() is not None
    host.discover()
    host.register(deliberate=True)
    fresh = run_job(host, runtime, 2, value="done")["action_result"]
    assert fresh["pid"] != old.process.pid and fresh["calls"] == 1
