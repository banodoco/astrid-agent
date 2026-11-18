"""Lead rework for LS-8: installation/authority, routes, evidence and shutdown.

Items 4, 6 and 7 from the original review belong to LS-8.1 per the Lead's
correction. Existing packet assertions remain in test_resident_sessions.py.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.managed_tool_session import StaleAdmissionError
from astrid.core.execution.resident_sessions import ResidentPackAdapter, ResidentProtocolError
from astrid.core.pack.identity import pack_digest, write_pin
from tests.core.execution.test_resident_sessions import (
    install_action,
    run_job,
)
from tests.core.execution.test_resident_sessions import (
    resident_host as base_resident_host,
)

resident_host = base_resident_host


def _pin(root: Path, revision: str) -> None:
    write_pin(root / "PIN", {
        "version": 1, "source": "https://example.invalid/resident-probe.git",
        "commit": revision * 40, "digest_rule": "pack-tree-v1",
        "digest": pack_digest(root), "origin": "user",
    })


def _action_metadata(root: Path, **fields) -> None:
    manifest = json.loads((root / "pack.yaml").read_text(encoding="utf-8"))
    manifest["actions"]["run"].update(fields)
    (root / "pack.yaml").write_text(json.dumps(manifest), encoding="utf-8")


def _wait_for(path: Path, future) -> str:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if path.exists() and (value := path.read_text(encoding="utf-8")):
            return value
        if future.done():
            future.result()
            pytest.fail("action ended before the expected checkpoint")
        time.sleep(0.01)
    pytest.fail("action did not reach its checkpoint")


def test_same_bytes_reinstalled_with_another_pin_start_a_new_worker(resident_host):
    root, host, runtime = resident_host
    install_action(resident_host, """import json
import os
from pathlib import Path
revision = json.loads((Path(__file__).parents[1] / 'PIN').read_text())['commit']
def run(value='default'):
    return {'pid': os.getpid(), 'revision': revision}
""")
    _pin(root, "a")
    digest = pack_digest(root)
    first = run_job(host, runtime, 1)["action_result"]
    old = host.managed_tool_session.current_adapter
    _pin(root, "b")
    assert pack_digest(root) == digest
    host.discover()
    host.register(deliberate=True)
    second = run_job(host, runtime, 2)["action_result"]
    assert first["revision"] == "a" * 40
    assert second["revision"] == "b" * 40
    assert first["pid"] != second["pid"]
    assert old.process.poll() is not None


def test_pin_replacement_during_a_job_fences_it_before_success(resident_host, tmp_path):
    root, host, runtime = resident_host
    entered, finish = tmp_path / "entered", tmp_path / "finish"
    install_action(resident_host, f"""import time
from pathlib import Path
def run(value='default'):
    Path({str(entered)!r}).write_text('entered')
    while not Path({str(finish)!r}).exists():
        time.sleep(0.01)
    return 'old installation'
""")
    _pin(root, "a")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_job, host, runtime, 1)
        try:
            assert _wait_for(entered, future) == "entered"
            old = host.managed_tool_session.current_adapter
            _pin(root, "b")
            finish.touch()
            with pytest.raises(StaleAdmissionError, match="installation|observation"):
                future.result(timeout=15)
        finally:
            finish.touch()
    assert not runtime.settlements
    assert [row[0] for row in runtime.failures] == ["resident-task-1"]
    assert old.process.poll() is not None and not host.managed_tool_session.occupied


def test_pin_replacement_after_capture_cannot_launch_the_previous_installation(resident_host, monkeypatch):
    root, host, runtime = resident_host
    _pin(root, "a")
    select = generic_host.open_resident_task

    def replace_after_capture(*args, **kwargs):
        _pin(root, "b")
        return select(*args, **kwargs)

    monkeypatch.setattr(generic_host, "open_resident_task", replace_after_capture)
    with pytest.raises(StaleAdmissionError, match="installation"):
        run_job(host, runtime, 1)
    assert not runtime.settlements
    assert [row[0] for row in runtime.failures] == ["resident-task-1"]
    assert not host.managed_tool_session.occupied


@pytest.mark.parametrize("phase", ["dispatch", "running", "collection"])
@pytest.mark.parametrize("authority", ["manifest_secret", "runtime_credential"])
def test_current_authority_is_checked_through_success(
    resident_host, monkeypatch, tmp_path, phase, authority,
):
    root, host, runtime = resident_host
    entered, finish = tmp_path / "entered", tmp_path / "finish"
    monkeypatch.setenv("LS8_AUTH", "fixture-secret-A")
    runtime.credential = "fixture-runtime-A"
    _action_metadata(root, secrets=["LS8_AUTH"])
    install_action(resident_host, f"""import os
import time
from pathlib import Path
def run(value='default'):
    Path({str(entered)!r}).write_text('entered')
    while not Path({str(finish)!r}).exists():
        time.sleep(0.01)
    return {{'used_original': os.environ['LS8_AUTH'] == 'fixture-secret-A'}}
""")
    observed = []

    def rotate():
        observed.append(host.managed_tool_session.current_adapter)
        if authority == "manifest_secret":
            monkeypatch.setenv("LS8_AUTH", "fixture-secret-B")
        else:
            runtime.credential = "fixture-runtime-B"

    if phase == "dispatch":
        invoke = ResidentPackAdapter.invoke

        def rotate_at_dispatch(self, token, **kwargs):
            rotate()
            return invoke(self, token, **kwargs)

        monkeypatch.setattr(ResidentPackAdapter, "invoke", rotate_at_dispatch)
    elif phase == "collection":
        collect = host._typed_outputs

        def rotate_at_collection(*args, **kwargs):
            result = collect(*args, **kwargs)
            if not observed:
                rotate()  # One authority transition; later failure-log collection is not a new rotation.
            return result

        monkeypatch.setattr(host, "_typed_outputs", rotate_at_collection)
    if phase != "running":
        finish.touch()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_job, host, runtime, 1)
        try:
            if phase == "running":
                assert _wait_for(entered, future) == "entered"
                rotate()
                finish.touch()
            with pytest.raises(StaleAdmissionError, match="authority|observation"):
                future.result(timeout=30)
        finally:
            finish.touch()
    assert len(observed) == 1
    assert not runtime.settlements
    assert [row[0] for row in runtime.failures] == ["resident-task-1"]
    assert observed[0].process.poll() is not None
    assert not host.managed_tool_session.occupied
    if phase == "dispatch":
        assert not entered.exists()


def test_admitted_route_override_keeps_the_disposable_path(resident_host, monkeypatch):
    root, host, runtime = resident_host
    manifest = json.loads((root / "pack.yaml").read_text(encoding="utf-8"))
    manifest["actions"]["run"]["metadata"]["network_policy"] = {
        "allowed_protocols": ["tcp"], "allowed_destinations": ["127.0.0.1:8188"],
    }
    (root / "pack.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    host.discover()
    host.register(deliberate=True)
    select = generic_host.open_resident_task
    decisions = []

    def with_admitted_route(*args, **kwargs):
        # Same host-owned override as dynamic target admission; no connection
        # or VibeComfy implementation is required to prove selection.
        assert kwargs["admission"]["allowed_routes"] == ["127.0.0.1:8188"]
        kwargs["admission"]["allowed_routes"] = ["127.0.0.1:8199"]
        decision = select(*args, **kwargs)
        decisions.append(decision)
        return decision

    monkeypatch.setattr(generic_host, "open_resident_task", with_admitted_route)
    first = run_job(host, runtime, 1)["action_result"]
    second = run_job(host, runtime, 2)["action_result"]
    assert decisions == [None, None]
    assert first["pid"] != second["pid"]
    assert first["calls"] == second["calls"] == 1
    assert not host.managed_tool_session.occupied


def test_legal_skip_has_complete_evidence_without_importing_the_action(resident_host):
    root, host, runtime = resident_host
    _action_metadata(root, conditions=[{"kind": "skip_if_input", "input": "value"}])
    install_action(resident_host, "raise AssertionError('a skipped action must never import')\n")
    result = run_job(host, runtime, 1)
    assert result["skipped"] is True
    assert result["skipped_reason"] == "input 'value' is set"
    assert result["process_evidence"]["returncode"] == 0
    assert result["managed_tool_session"]["terminal"]["kind"] == "settled"
    assert not runtime.failures


@pytest.mark.parametrize("damage", ["empty_success", "missing_returncode", "missing_payload"])
def test_incomplete_terminal_evidence_retires_without_consuming_the_token(
    resident_host, monkeypatch, damage,
):
    _, host, runtime = resident_host
    read = ResidentPackAdapter._read
    settled, adapters = [], []
    settle = host.managed_tool_session.settle

    def observe_settlement(token, **kwargs):
        settled.append(token)
        return settle(token, **kwargs)

    def damage_result(self, deadline, guard):
        result = read(self, deadline, guard)
        if result["kind"] == "result":
            adapters.append(self)
            if damage == "empty_success":
                result["result"] = {"ok": True}
            else:
                result["result"].pop("returncode" if damage == "missing_returncode" else "payload")
        return result

    monkeypatch.setattr(host.managed_tool_session, "settle", observe_settlement)
    monkeypatch.setattr(ResidentPackAdapter, "_read", damage_result)
    with pytest.raises(ResidentProtocolError, match="terminal|evidence|returncode"):
        run_job(host, runtime, 1)
    assert not settled and not runtime.settlements
    assert [row[0] for row in runtime.failures] == ["resident-task-1"]
    assert len(adapters) == 1 and adapters[0].process.poll() is not None
    assert not host.managed_tool_session.occupied


def test_shutdown_drains_the_live_admission_under_its_owner(resident_host, monkeypatch, tmp_path):
    _, host, runtime = resident_host
    entered = tmp_path / "entered"
    install_action(resident_host, f"""import time
from pathlib import Path
from astrid.sdk import current_pack_job
def run(value='default'):
    job = current_pack_job()
    Path({str(entered)!r}).write_text(str(job.attempt_root))
    while not job.cancelled():
        time.sleep(0.01)
    return 'cancelled'
""")
    task = {"task": {
        "id": "resident-task-1", "capability": "resident_probe.run", "project_id": "demo",
        "attempt_id": "attempt-1", "fence": 1, "runtime_epoch": 1, "status": "running",
        "spec": {"spec": {"inputs": {"value": "shutdown"}}},
    }}
    runtime.tasks["resident-task-1"] = task
    cancelled = []
    cancel = host.managed_tool_session.cancel

    def observe_cancel(token, **kwargs):
        adapter = host.managed_tool_session.adapter_for(token)
        assert adapter.process.poll() is not None
        cancelled.append(token)
        return cancel(token, **kwargs)

    monkeypatch.setattr(host.managed_tool_session, "cancel", observe_cancel)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(host.run_task, task, lease_token="lease-1")
        attempt = Path(_wait_for(entered, future))
        adapter = host.managed_tool_session.current_adapter
        capture = adapter.record.source_root
        host.shutdown()
        assert future.result(timeout=15)["status"] == "cancelled"
    assert len(cancelled) == 1
    assert not runtime.settlements
    assert adapter.process.poll() is not None and not host.managed_tool_session.occupied
    assert not capture.exists() and not attempt.exists()
    assert host._handoff_active_attempts == 0
    assert not host._active_processes and not host._host_lifetime_processes
