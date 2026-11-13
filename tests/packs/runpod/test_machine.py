"""Plan 32 T5: RunPod as a machine on the real lease owner, with the pod and provider faked.

Money rules ported from the remote placement wrapper: one claim per job; a
verified failed job settles warm within the original limits;
the detached job is checkpointed before start, so a crash is cancelled by
``recover()`` before the pod is deleted.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from runpod_lifecycle.https import CHUNK, HttpsPodTransport

from astrid.core.execution.machine_run import MachineJob, MachineRunCancelled, run_on_machine
from astrid.packs.runpod import machine as rm
from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401  (fixture)
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401  (fixture)
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401  (fixture)
from tests.packs.runpod.test_warm_session import (  # noqa: F401  (fixtures)
    lease_selection_case as lease_selection_case, small_lease as small_lease,
)
from dataclasses import replace

BASE = dict(account="acct", gpu_type="NVIDIA RTX 5090", image="img@sha256:" + "1" * 64,
            volume={"id": "vol", "mount_path": "/workspace", "cache_device": 64768, "mount_id": 7},
            source_digest="0123456789abcdef")


def test_host_shutdown_releases_before_closing_owner():
    from types import SimpleNamespace
    from astrid.packs.runpod.adapter import TargetProviderBindings

    calls = []
    bindings = TargetProviderBindings(pack_roots=(), holds=None, credentials=None, environ={})
    owner = SimpleNamespace(release=lambda **kw: calls.append(("release", kw)),
                            close=lambda: calls.append(("close", None)))
    bindings.provider = SimpleNamespace(lease_owner=owner)
    bindings.close()
    assert calls == [("release", {"reason": "host_shutdown"}), ("close", None)]


# ----------------------------------------------------------------------------
# Custody key
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("field, value, expected", [
    ("gpu_type", "NVIDIA H100", ["gpu_type"]),
    ("image", "img@sha256:" + "2" * 64, ["image"]),
    ("source_digest", "fedcba9876543210", ["source_digest"]),
    ("account", "other", ["account"]),
    ("volume", {**BASE["volume"], "id": "vol2"}, ["volume.id"]),
    ("volume", {**BASE["volume"], "mount_id": 8}, ["volume.mount_id"]),
    ("volume", {**BASE["volume"], "cache_device": 1}, ["volume.cache_device"]),
    ("volume", {**BASE["volume"], "mount_path": "/mnt"}, ["volume.mount_path"]),
])
def test_each_custody_field_refuses_warm_reuse(field, value, expected):
    before = rm.machine_custody(**BASE)
    after = rm.machine_custody(**{**BASE, field: value})
    assert rm.custody_differences(before, after) == expected
    assert before["digest"] != after["digest"]


def test_an_unchanged_key_allows_reuse_and_a_missing_one_refuses():
    assert rm.custody_differences(rm.machine_custody(**BASE), rm.machine_custody(**BASE)) == []
    assert rm.custody_differences(None, rm.machine_custody(**BASE)) == ["custody"]


def test_the_pod_side_command_is_the_local_run_with_its_progress_path():
    command = rm.generation_command("cpu-image", "/root/astrid/runs/abc", mode="t2i")
    assert "export ASTRID_PROGRESS_PATH=/root/astrid/runs/abc/progress.json" in command
    argv = rm.generation_argv("cpu-image", "/root/astrid/runs/abc", mode="t2i")
    assert argv[1:5] == ["generate", "cpu-image", "--placement", "local"]
    assert "--port" not in command and "17340" not in command
    # T5f: the warm job opens the prepared connection under the reset lock; no up, no connect.
    assert "ensure_runtime" not in command and "astrid-runtime" not in command
    assert "flock -s 9" in command and "export ASTRID_PREPARED_CONNECTION=/root/astrid/prepared-connection.json" in command
    assert "ASTRID_RUNTIME_DATA_ROOT=/root/astrid/runtime-data" in command and "ASTRID_RUNTIME_DOOR_PORT=0" in command
    assert command.index("generate list --json") < command.index("--placement local")


# ----------------------------------------------------------------------------
# The machine on the real owner (the retained fixture's adapter and fake pod)
# ----------------------------------------------------------------------------


@pytest.fixture
def machine_case(retained, tmp_path, monkeypatch):
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle

    c = retained
    root = tmp_path / "canonical"
    root.mkdir()
    source = root / "source.json"
    source.write_text(json.dumps({"nodes": [{"id": 1, "type": "SaveImage", "inputs": [],
                                              "widgets_values": ["proof"]}], "links": []}))
    workflow = VibeWorkflow("machine-proof", WorkflowSource("machine-proof"))
    workflow.add_node("SaveImage", _id="1", uid="sink", filename_prefix="proof")
    workflow.register_input("filename_prefix", "1", "filename_prefix", "proof", type="STRING")
    emit_bundle(workflow, root / "workflow.py", {"operation": "authored"})
    # The fake pod advertises cpu-image; give the controller the same real,
    # model-free entry so capacity reads its manifest instead of an unknown ID.
    from astrid.core.generation import entries
    original_entries = entries.user_entries
    monkeypatch.setattr(entries, "user_entries", lambda: [*original_entries(), {
        "id": "cpu-image", "kind": "image", "modes": ["t2i"],
        "engine": "comfyui", "bundle": str(root / "workflow.py"),
    }])
    deadline = (datetime.fromtimestamp(c.clock(), timezone.utc) + timedelta(hours=2)).isoformat()
    monkeypatch.setattr("astrid.packs.runpod.adapter.prevalidate_task", lambda *a, **k: None)
    policy = {"kind": "runpod", "engine": "comfyui", "preferences": c.prefs.as_mapping(),
              "offer_digest": c.grant.offer_digest, "consent": list(c.grant.consent), "max_usd": c.grant.max_usd,
              "deadline": c.grant.deadline, "preparation_seconds": 60, "execution_seconds": 60,
              "idle_seconds": 1800}
    assert deadline  # the grant's own deadline is the disclosed one
    c.active = []
    c.machine = rm.MACHINE.for_host(c.adapter, begin=lambda lease: c.active.append(("begin", lease.generation)),
                                    done=lambda lease: c.active.append(("done", lease.generation)))

    def job(n=1):
        attempt = tmp_path / f"attempt-{n}"
        attempt.mkdir(exist_ok=True)
        return MachineJob(entry="cpu-image", mode="t2i", policy=policy,
                          inputs={"python": str(root / "workflow.py"), "companion": str(root / "workflow.vibe.json"),
                                  "source": str(source), "workflow_inputs": "{}"},
                          invocation_id=f"task:attempt:{n}", task_id="task", attempt_id=f"attempt-{n}",
                          scratch=attempt, progress_path=attempt / ".astrid-progress.json")
    c.job = job
    return c


def _claim_machine(c):
    """Establish paid machine authority before submitting a generation job."""
    if c.owner.path.exists():
        return
    c.host.start_fresh_claim()
    c.host.claim(replace(c.grant, invocation_id="manual-claim", preparation_seconds=0,
                         execution_seconds=0), requested_idle_seconds=c.grant.idle_seconds)


def test_a_job_runs_ships_fetches_and_settles_warm(machine_case, tmp_path):
    c = machine_case
    _claim_machine(c)
    job = c.job()
    fetched = run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: False,
                             sleep=lambda s: None)
    assert (job.scratch / "outputs" / "artifacts" / "cpu.png").read_bytes() == b"png-bytes"
    assert set(fetched["files"]) == {"artifacts/cpu.png", "manifest.json"}
    assert json.loads(job.progress_path.read_text()) == {"phase": "fetch"}
    record = c.read()
    assert record["lease"]["state"] == "idle" and record["lease"]["job"]["state"] == "exited"
    # Claim on a fresh pod: the reset takes the empty-realm template and writes ready.json.
    assert record["lease"]["preparation"]["pod_ready"] == {"entry": None, "ready": True, "found": "absent",
                                                           "reset": "taken"}
    # The job's one command extracts the selection and checks the entry, then runs.
    generation = next(line for line in c.pod.job_log if " generate cpu-image " in line)
    assert "tar -xzf" in generation and "generate list --json" in generation
    # After the fetch, before settle: the reset removed the run and job folders.
    reset = c.pod.resets[-1]
    assert reset["ok"] and reset["active"] == {"run_id": reset["dirs"][0].rsplit("/", 1)[1], "task": "task",
                                               "attempt": "attempt-1", "run_dir": reset["dirs"][0],
                                               "job_dir": reset["dirs"][1]}
    assert reset["dirs"][0].startswith("/root/astrid/runs/") and reset["dirs"][1].startswith("/workspace/.astrid-jobs/")
    assert not [path for path in c.pod.files if path.startswith(reset["dirs"][0] + "/")]
    assert c.creates == 1 and c.deletes == []
    assert [kind for kind, _ in c.active] == ["begin", "done"]
    # Inputs shipped inside the job directory, each digest-checked after upload.
    shipped = [path for path in c.pod.put_log if "/in/" in path]
    assert len(shipped) == 3 and any(path.endswith("job.json") for path in c.pod.put_log)
    # A second job on the warm machine fetches and installs nothing again.
    jobs = len(c.pod.job_log)
    second = c.job(2)
    run_on_machine(c.machine, second, destination=second.scratch / "outputs", cancelled=lambda: False,
                   sleep=lambda s: None)
    new = c.pod.job_log[jobs:]
    # Warm: no claim-time reset (ready.json is there), the job, then its own reset.
    assert len(new) == 2 and " generate cpu-image " in new[0] and "pod_reset reset --run-dir" in new[1]
    assert c.read()["lease"]["preparation"]["pod_ready"]["reset"] == "skipped"
    assert c.creates == 1 and c.read()["lease"]["state"] == "idle"
    c.host.release()
    assert c.deletes == ["pod-1"]


@pytest.mark.parametrize(('requested', 'bound', 'expected'), [(600, 60, 60), (0, 60, 0), (15, 60, 15)])
def test_claim_caps_idle_and_reports_the_acquired_timeout(machine_case, requested, bound, expected):
    from dataclasses import replace
    c = machine_case
    _claim_machine(c)
    original = c.job()
    job = replace(original, policy={**original.policy, 'placement_idle_seconds': requested, 'idle_seconds': bound})
    lease = c.machine.claim(job)
    try:
        assert c.read()['lease']['idle_seconds'] == expected
        assert c.machine.describe(lease)['idle_timeout_s'] == expected
    finally:
        c.machine.release(lease, False)


def test_a_failing_runtime_check_keeps_the_verified_machine_for_retry(machine_case):
    c = machine_case
    _claim_machine(c)
    c.pod.listed = []  # the pod's runtime does not list the entry from the shipped selection
    job = c.job()
    with pytest.raises(rm.MachineError, match="category=unknown; code=unknown"):
        run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: False,
                       sleep=lambda s: None)
    record = c.read()
    assert record["state"] == "claimed" and record["lease"]["state"] == "idle"
    assert record["lease"]["last_job"]["outcome"] == "failed" and c.deletes == []
    assert any(deadline is not None for deadline in c.transport_deadlines)
    # The check is the job command's first step: the run itself never produced outputs.
    assert not any("/out/" in path for path in c.pod.files) and len(c.pod.resets) == 2
    c.pod.listed = ["cpu-image"]
    retry = c.job(2)
    run_on_machine(c.machine, retry, destination=retry.scratch / "outputs", cancelled=lambda: False,
                   sleep=lambda s: None)
    assert c.creates == 1 and c.read()["lease"]["last_job"]["outcome"] == "success"


def test_diagnostic_read_failure_still_settles_and_never_records_secret(machine_case, monkeypatch):
    c = machine_case
    _claim_machine(c)
    c.pod.listed = []
    secret = "SENTINEL_TOKEN https://private.example/secret"
    original = c.pod.exec

    def unreadable(command, timeout=600):
        if command.startswith("tail -c") and command.endswith("/stdout"):
            raise RuntimeError(secret)
        return original(command, timeout)

    monkeypatch.setattr(c.pod, "exec", unreadable)
    job = c.job()
    with pytest.raises(rm.MachineError, match="remote failure diagnostic unavailable") as caught:
        run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: False,
                       sleep=lambda s: None)
    assert secret not in str(caught.value) and secret not in json.dumps(c.read())
    assert c.read()["lease"]["state"] == "idle" and c.creates == 1 and c.deletes == []


def test_a_failed_reset_releases_the_pod_instead_of_settling_it(machine_case):
    """T5f: the outputs were fetched, but a reset that fails never leaves a dirty warm pod."""
    c = machine_case
    _claim_machine(c)
    c.pod.fail_jobs = {"--run-dir"}  # only the release-time reset carries the job's run directory
    job = c.job()
    fetched = run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: False,
                             sleep=lambda s: None)
    assert set(fetched["files"]) == {"artifacts/cpu.png", "manifest.json"}
    record = c.read()
    assert [r["ok"] for r in c.pod.resets] == [True, False]
    assert record["state"] == "cleanup_complete" and record["lease"]["ended"] == "reset_failed"
    assert record["lease"]["job"]["state"] == "exited" and c.deletes == ["pod-1"]


def test_recoverable_claim_preparation_failure_keeps_attested_pod(machine_case, monkeypatch):
    c = machine_case
    _claim_machine(c)
    first = c.machine.claim(c.job())
    c.host.settle(first.lease)
    original = c.placement.begin_execution
    with monkeypatch.context() as patch:
        patch.setattr(c.placement, "begin_execution", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("ordinary failure")))
        with pytest.raises(RuntimeError, match="ordinary failure"):
            c.machine.claim(c.job(2))
    record = c.read()
    assert record["state"] == "claimed" and record["lease"]["state"] == "idle"
    assert record["lease"]["last_job"]["outcome"] == "failed"
    assert record["lease"]["last_job"]["failure"]["stage"] == "preparation"
    assert c.creates == 1 and c.deletes == []
    assert original is not None


@pytest.mark.parametrize("unsafe", ["probe_unavailable", "changed_mount"])
def test_unverified_current_storage_probe_releases_previous_warm_pod(machine_case, monkeypatch, unsafe):
    c = machine_case
    _claim_machine(c)
    first = c.machine.claim(c.job())
    c.host.settle(first.lease)
    if unsafe == "probe_unavailable":
        monkeypatch.setattr(c.placement, "begin_claimed",
                            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("probe unavailable")))
        expected = "probe unavailable"
    else:
        c.mount_id += 1
        expected = "warm machine custody differs"
    with pytest.raises(Exception, match=expected):
        c.machine.claim(c.job(2))
    assert c.read()["state"] == "cleanup_complete" and c.deletes == ["pod-1"]
    assert c.creates == 1


def test_unverified_installation_failure_keeps_exact_pod(machine_case, monkeypatch):
    c = machine_case
    _claim_machine(c)
    with monkeypatch.context() as patch:
        patch.setattr(c.placement, "prepare_claimed", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad install")))
        with pytest.raises(RuntimeError, match="bad install"):
            c.machine.claim(c.job())
    assert c.read()["state"] == "claimed" and c.deletes == []
    assert c.read()["lease"]["state"] == "idle"
    assert c.read()["lease"]["preparation"]["state"] == "stale_after_failure"


@pytest.mark.parametrize("unsafe", ["paid_expired", "changed_receipt"])
def test_unsafe_failed_job_verification_releases_exact_pod(machine_case, monkeypatch, unsafe):
    c = machine_case
    _claim_machine(c)
    job = c.job()
    lease = c.machine.claim(job)
    if unsafe == "changed_receipt":
        run = c.machine.start(lease, job)
        c.machine._checkpoint(lease, {"id": run.job_id, "state": "exited"})
    else:
        c.machine._checkpoint(lease, {"id": "f" * 32, "state": "exited"})
    paid = c.read()["lease"]["paid_expires_at"]

    def unverified(*args, **kwargs):
        if unsafe == "paid_expired":
            c.clock.now = paid
        else:
            lease.state.installed = {**lease.state.installed, "receipt_sha256": "sha256:" + "f" * 64}
        return True

    monkeypatch.setattr(c.placement, "_canonical_installed", unverified)
    c.machine.release(lease, False, failure=rm.MachineError("job failed"))
    if unsafe == "paid_expired":
        assert c.read()["state"] == "cleanup_complete"
        assert c.read()["lease"]["ended"] == "paid_limit_exhausted"
        assert c.deletes == ["pod-1"]
    else:
        record = c.read()
        assert record["lease"]["state"] == "idle" and not c.deletes
        assert record["lease"]["preparation"]["installed"] == lease.state.installed
        assert record["lease"]["idle_expires_at"] == min(c.clock() + record["lease"]["idle_seconds"], paid)
        assert c.pod.resets[-1]["ok"]
    assert c.creates == 1


def test_unreadable_invocation_lease_releases_known_exact_pod(machine_case, monkeypatch):
    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    monkeypatch.setattr(c.host, "preparation_claim", lambda invocation: (_ for _ in ()).throw(RuntimeError("lost lease")))
    c.machine.release(lease, False, failure=rm.MachineError("job failed"))
    assert c.read()["state"] == "cleanup_complete" and c.deletes == ["pod-1"]
    assert c.creates == 1


def test_blocked_failure_verifier_does_not_hold_owner_lock_or_revive_lease(machine_case, monkeypatch):
    import threading
    import time

    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    c.machine._checkpoint(lease, {"id": "a" * 32, "state": "exited"})
    entered, resume = threading.Event(), threading.Event()
    errors = []

    def blocked(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        return True

    monkeypatch.setattr(c.placement, "_canonical_installed", blocked)

    def settle_failure():
        try:
            c.machine.release(lease, False, failure=rm.MachineError("job failed"))
        except Exception as exc:
            errors.append(exc)

    verifier = threading.Thread(target=settle_failure)
    verifier.start()
    assert entered.wait(5)
    releaser = threading.Thread(target=lambda: c.host.release(
        reason="paid_deadline", _expected_operation=lease.lease.operation_id,
        expected_machine_id=lease.claim.pod_id))
    releaser.start()
    for _ in range(100):
        if c.read()["lease"]["state"] == "releasing":
            break
        time.sleep(0.01)
    assert c.read()["lease"]["state"] == "releasing"
    resume.set()
    verifier.join(5)
    releaser.join(5)
    assert not verifier.is_alive() and not releaser.is_alive() and not errors
    assert c.read()["state"] == "cleanup_complete" and c.deletes == ["pod-1"]


def test_paid_timer_drains_real_https_slow_drip_without_late_warm_settlement(machine_case, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Event, Thread
    import time
    from astrid.packs.runpod.remote_placement import _HttpsOps

    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    c.machine._checkpoint(lease, {"id": "d" * 32, "state": "exited"})
    with c.host.lock:
        current = c.host._read()
        shortened = {**current["lease"], "paid_expires_at": c.clock() + 10}
        c.host._arm(c.host._save(current, shortened))

    entered = Event()
    requests, writes = [], []
    class Slow(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            except (ValueError, TypeError):
                body = {}
            requests.append((self.path, body.get('path')))
            entered.set()
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            for _ in range(1000):
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    writes.append(1)
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.025)
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(_HttpsOps, "_bounded_call", c.real_bounded_call)
    lease.claim.ops = _HttpsOps("pod1", "x" * 32,
                               base_url=f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(c.placement, "_canonical_installed",
                        lambda state, **kwargs: state.claim.ops.job_poll("d" * 32))
    errors = []
    def release_failure():
        try:
            c.machine.release(lease, False, failure=rm.MachineError("private failure"))
        except Exception as exc:
            errors.append(exc)
    verifier = Thread(target=release_failure)
    try:
        verifier.start()
        assert entered.wait(5)
        for _ in range(100):
            if len(writes) > 1:
                break
            time.sleep(0.01)
        assert requests and requests[0][0] == '/download-chunk'
        assert isinstance(requests[0][1], str) and ('d' * 32) in requests[0][1]
        assert len(writes) > 1
        c.clock.advance(10)  # Real fake timer invokes exact release during the HTTP drip.
        verifier.join(3)
        assert not verifier.is_alive() and not errors
        assert c.read()["state"] == "cleanup_complete"
        assert c.deletes == ["pod-1"] and c.creates == 1
        assert c.read()["lease"]["state"] == "releasing"
    finally:
        server.shutdown()
        server.server_close()


def test_exact_cleanup_has_its_own_finite_budget_and_keeps_pending_custody(machine_case, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Event, Thread
    from types import SimpleNamespace
    import multiprocessing
    import time
    from astrid.packs.runpod import remote_placement, worker_owner

    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    c.machine._checkpoint(lease, {"id": "e" * 32, "state": "running"})
    operation = lease.lease.operation_id
    entered = Event()

    class Slow(BaseHTTPRequestHandler):
        def do_POST(self):
            entered.set()
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            for _ in range(1000):
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.025)
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(remote_placement._HttpsOps, "_bounded_call", c.real_bounded_call)
    monkeypatch.setattr(worker_owner, "EXACT_CLEANUP_SECONDS", 10.0)
    ops = remote_placement._HttpsOps("pod1", "x" * 32,
                                    base_url=f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(remote_placement, "connect_claimed_for_preparation",
                        lambda record: SimpleNamespace(ops=ops))
    children_before = {child.pid for child in multiprocessing.active_children()}
    try:
        start = time.monotonic()
        with pytest.raises(remote_placement.RemotePlacementError) as caught:
            c.host.release(reason="paid_deadline", _expected_operation=operation,
                           expected_machine_id=lease.claim.pod_id)
        assert entered.is_set() and time.monotonic() - start < 11.5, str(caught.value)
        record = c.read()
        assert record["operation_id"] == operation and record["state"] == "cleanup_pending"
        assert record["lease"]["state"] == "releasing"
        assert record["lease"]["job"] == {"id": "e" * 32, "state": "running"}
        assert c.deletes == [] and c.creates == 1
        assert {child.pid for child in multiprocessing.active_children()} == children_before
    finally:
        server.shutdown()
        server.server_close()


def test_unreaped_observer_fences_custody_until_it_is_gone(machine_case, monkeypatch):
    from astrid.packs.runpod import remote_placement
    from astrid.packs.runpod.worker_preparation import AcquisitionUnresolvedError

    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    c.machine._checkpoint(lease, {"id": "d" * 32, "state": "exited"})

    class Child:
        alive = True
        closed = False
        def is_alive(self): return self.alive
        def join(self, timeout=0): pass
        def close(self): self.closed = True

    child = Child()
    def unreaped(self, *_args, **_kwargs):
        self.unreaped_child = child
        raise remote_placement.BoundedObserverUnreaped("observer could not be reaped")

    monkeypatch.setattr(remote_placement._HttpsOps, "_bounded_call", unreaped)
    monkeypatch.setattr(c.placement, "_canonical_installed",
                        lambda state, **kwargs: state.claim.ops.job_poll("d" * 32))
    with pytest.raises(AcquisitionUnresolvedError, match="custody is pending"):
        c.machine.release(lease, False, failure=rm.MachineError("private failure"))
    record = c.read()
    assert record["state"] == "cleanup_pending" and record["lease"]["state"] == "releasing"
    assert c.deletes == [] and c.creates == 1
    with pytest.raises(AcquisitionUnresolvedError, match="observer still lives"):
        c.host.release(reason="recovery", _expected_operation=lease.lease.operation_id)
    child.alive = False
    c.host.release(reason="recovery", _expected_operation=lease.lease.operation_id)
    assert child.closed and c.read()["state"] == "cleanup_complete" and c.deletes == ["pod-1"]


def test_stale_release_identity_cannot_abort_another_observer(machine_case):
    from threading import Event

    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    stale = c.read()
    old_key, old_event = c.host.begin_failure_observation(lease.lease, lease.claim.pod_id)
    replacement_key = ("f" * 32, "e" * 32, lease.lease.generation + 1, "pod-replacement")
    replacement_event = Event()
    with c.host._observer_lock:
        c.host._failure_observers[replacement_key] = replacement_event
    try:
        c.host._abort_failure_observation(stale)
        assert old_event.is_set() and not replacement_event.is_set()
        c.host._abort_failure_observation({**stale, "operation_id": replacement_key[0],
                                           "pod_id": replacement_key[3],
                                           "lease": {**stale["lease"], "owner_epoch": replacement_key[1],
                                                     "generation": replacement_key[2]}})
        assert replacement_event.is_set()
    finally:
        c.host.end_failure_observation(old_key)
        c.host.end_failure_observation(replacement_key)
        c.host.release()


def test_exact_cleanup_cancel_does_not_hold_owner_lock(machine_case, monkeypatch):
    from threading import Event, Thread

    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    c.machine._checkpoint(lease, {"id": "e" * 32, "state": "running"})
    entered, resume = Event(), Event()
    original = c.pod.job_cancel
    errors = []

    def blocked(job_id):
        entered.set()
        assert resume.wait(5)
        return original(job_id)

    monkeypatch.setattr(c.pod, "job_cancel", blocked)
    def cleanup():
        try:
            c.host.release(reason="paid_deadline", _expected_operation=lease.lease.operation_id,
                           expected_machine_id=lease.claim.pod_id)
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=cleanup)
    try:
        thread.start()
        assert entered.wait(5)
        assert c.host.lock.acquire(timeout=0.5)
        try:
            assert c.read()["state"] == "cleanup_pending"
        finally:
            c.host.lock.release()
    finally:
        resume.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert c.read()["state"] == "cleanup_complete" and c.deletes == ["pod-1"]


def test_exact_cleanup_shares_one_deadline_across_both_job_cancels(machine_case):
    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    with c.host.lock:
        record = c.host._read()
        current = record["lease"]
        preparation = {**current["preparation"], "state": "unresolved",
                       "job": {"id": "a" * 32, "state": "running"}}
        c.host._save(record, {**current, "preparation": preparation,
                              "job": {"id": "b" * 32, "state": "running"}})
    before = len(c.transport_deadlines)
    c.host.release(reason="explicit", _expected_operation=lease.lease.operation_id,
                   expected_machine_id=lease.claim.pod_id)
    scoped = [deadline for deadline in c.transport_deadlines[before:] if deadline is not None]
    assert len(scoped) == 2 and scoped[0] == scoped[1]
    assert c.pod.cancelled[-2:] == ["a" * 32, "b" * 32]
    assert c.read()["state"] == "cleanup_complete" and c.deletes == ["pod-1"]


def test_paid_expiry_during_failure_reset_stdout_read_prevents_settlement(machine_case, monkeypatch):
    c = machine_case
    _claim_machine(c)
    c.pod.listed = []
    original = c.pod.exec

    def expiry_at_stdout(command, timeout=600):
        if command.startswith("cat ") and "/stdout" in command and len(c.pod.resets) >= 2:
            c.clock.now = c.read()["lease"]["paid_expires_at"]
        return original(command, timeout)

    monkeypatch.setattr(c.pod, "exec", expiry_at_stdout)
    job = c.job()
    with pytest.raises(rm.MachineError, match="remote generation failed"):
        run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: False,
                       sleep=lambda s: None)
    assert c.read()["state"] == "cleanup_complete" and c.deletes == ["pod-1"]
    assert c.read()["lease"]["ended"] == "reset_failed"


def test_lost_reset_start_ack_keeps_exact_job_for_release_reconciliation(machine_case, monkeypatch):
    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    c.machine._checkpoint(lease, {"id": "e" * 32, "state": "exited"})
    original = c.pod.job_start

    def lost_ack(command, *, job_id=None):
        result = original(command, job_id=job_id)
        if "pod_reset reset" in command:
            raise TimeoutError("private reset response")
        return result

    monkeypatch.setattr(c.pod, "job_start", lost_ack)
    c.machine.release(lease, False, failure=rm.MachineError("generation failed"))
    record = c.read()
    assert record["state"] == "cleanup_complete" and record["lease"]["ended"] == "reset_failed"
    reset_id = record["lease"]["job"]["id"]
    assert reset_id in c.pod.cancelled and c.deletes == ["pod-1"] and c.creates == 1


def test_wrong_reset_poll_identity_cannot_mark_the_journaled_job_exited(machine_case, monkeypatch):
    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    c.machine._checkpoint(lease, {"id": "e" * 32, "state": "exited"})
    original = c.pod.job_poll

    def wrong_reset_poll(job_id):
        result = original(job_id)
        return {**result, "id": "f" * 32} if job_id != "e" * 32 else result

    monkeypatch.setattr(c.pod, "job_poll", wrong_reset_poll)
    c.machine.release(lease, False, failure=rm.MachineError("generation failed"))
    record = c.read()
    assert record["state"] == "cleanup_complete" and record["lease"]["ended"] == "reset_failed"
    reset_id = record["lease"]["job"]["id"]
    assert reset_id != "e" * 32 and reset_id in c.pod.cancelled
    assert c.deletes == ["pod-1"] and c.creates == 1


def test_structural_node_fact_survives_owner_journal_and_machine_listing(machine_case):
    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    lease.node_ids = frozenset({"17"})
    lease.failure_diagnostic = {"code": "generation_failed", "category": "execution",
                                "node_id": "17", "node_class": "KSampler", "field": "seed"}
    c.machine._checkpoint(lease, {"id": "c" * 32, "state": "exited"})
    c.machine.release(lease, False, failure=rm.MachineError("private output"), stage="fetch")
    record = c.read()
    summary = record["lease"]["last_job"]["failure"]
    assert summary["node_id"] == "17" and summary["field"] == "seed"
    assert rm.lease_row(record)["last_job"]["failure"] == summary
    assert "private output" not in json.dumps(record) and c.creates == 1


def test_a_pending_active_job_at_the_next_claim_redoes_the_reset(machine_case):
    c = machine_case
    _claim_machine(c)
    job = c.job()
    run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: False,
                   sleep=lambda s: None)
    # A job that never reached its reset: its run and agent folders are still on the pod.
    lost_run, lost_job = "/root/astrid/runs/lost", "/workspace/.astrid-jobs/lost"
    c.pod.files.update({lost_run + "/out/x": b"x", lost_job + "/stdout": b""})
    c.pod.active_job = {"run_id": "lost", "task": "task", "attempt": "attempt-0", "run_dir": lost_run,
                        "job_dir": lost_job}
    second = c.job(2)
    run_on_machine(c.machine, second, destination=second.scratch / "outputs", cancelled=lambda: False,
                   sleep=lambda s: None)
    ready = c.read()["lease"]["preparation"]["pod_ready"]
    assert ready == {"entry": None, "ready": True, "found": "pending", "reset": "restored"}
    assert [r["active"] and r["active"]["run_id"] for r in c.pod.resets][2] == "lost"
    assert c.pod.resets[2]["dirs"] == [lost_run, lost_job]
    assert not [path for path in c.pod.files if path.startswith((lost_run + "/", lost_job + "/"))]
    assert c.creates == 1 and c.read()["lease"]["state"] == "idle"


def test_cancel_during_shipping_stops_within_one_chunk_and_launches_nothing(machine_case, monkeypatch):
    from threading import Event
    import httpx
    from astrid.packs.runpod.remote_placement import _HttpsOps

    c = machine_case
    _claim_machine(c)
    original = c.job()
    large = original.scratch / "video.bin"
    large.write_bytes(b"v" * (3 * CHUNK))
    job = replace(original, inputs={**original.inputs, "source_video": str(large)})
    abort = Event()
    uploads, chunks, failures = {}, [], []
    original_transport = _HttpsOps._transport

    def upload(request):
        if request.method == "POST" and request.url.path == "/uploads":
            metadata = json.loads(request.content)
            upload_id = f"{len(uploads) + 1:032x}"
            uploads[upload_id] = metadata
            c.pod.files[metadata["path"]] = b""
            c.pod.put_log.append(metadata["path"])
            return httpx.Response(200, json={"id": upload_id, "offset": 0, "complete": False})
        assert request.method == "PUT"
        metadata = uploads[request.url.path.rsplit("/", 1)[-1]]
        path = metadata["path"]
        assert int(request.url.params["offset"]) == len(c.pod.files[path])
        c.pod.files[path] += request.content
        if path.endswith(".bin"):
            chunks.append(len(request.content))
            abort.set()  # The first in-flight chunk finishes after force_abort sets its signal.
        size = len(c.pod.files[path])
        return httpx.Response(200, json={"offset": size, "complete": size == metadata["size"]})

    def transport(ops):
        if not armed:
            return original_transport(ops)
        remote = HttpsPodTransport("pod1", "x" * 32, base_url="http://localhost")
        remote.http.close()
        remote.http = httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(upload))
        # Use the real chunk upload loop and request hooks; other operations stay on the fake pod.
        remote.execute_command = c.pod.exec
        remote.detached_start = c.pod.job_start
        remote.detached_poll = c.pod.job_poll
        remote.detached_cancel = c.pod.job_cancel
        remote.get = c.pod.get
        return remote

    armed = False
    jobs_before_shipping = []
    def claimed(lease):
        nonlocal armed
        armed = True
        jobs_before_shipping.extend(c.pod.job_log)

    monkeypatch.setattr(_HttpsOps, "_transport", transport)
    with pytest.raises(MachineRunCancelled):
        run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=abort.is_set,
                       on_claim=claimed, on_failure=failures.append, sleep=lambda s: None)
    assert chunks == [CHUNK]
    assert c.pod.job_log == jobs_before_shipping
    assert not c.pod.cancelled  # No generation job was posted.
    assert [kind for kind, _ in c.active] == ["begin", "done"]
    assert len(failures) == 1 and isinstance(failures[0]["failure"], MachineRunCancelled)
    assert c.read()["lease"]["state"] == "idle"
    assert c.read()["lease"]["last_job"]["outcome"] == "cancelled"


@pytest.mark.parametrize("lost_ack", [False, True])
def test_cancel_after_launch_stops_and_checkpoints_before_settlement(machine_case, monkeypatch, lost_ack):
    from threading import Event

    c = machine_case
    _claim_machine(c)
    job = c.job()
    lease = c.machine.claim(job)
    abort = Event()
    original_start = c.pod.job_start
    posted = []
    checkpoints = []
    jobs_before_start = list(c.pod.job_log)
    original_checkpoint = c.machine._checkpoint

    def checkpoint(current_lease, record):
        original_checkpoint(current_lease, record)
        checkpoints.append(dict(record))

    def launch(command, *, job_id):
        started = original_start(command, job_id=job_id)
        posted.append(job_id)
        abort.set()
        if lost_ack:
            raise TimeoutError("launch acknowledgement lost after abort")
        return started

    monkeypatch.setattr(c.pod, "job_start", launch)
    monkeypatch.setattr(c.machine, "_checkpoint", checkpoint)
    failure = None
    try:
        with pytest.raises(MachineRunCancelled) as caught:
            c.machine.start(lease, job, cancelled=abort.is_set)
        failure = caught.value
        assert len(posted) == 1
        assert len(c.pod.job_log) == len(jobs_before_start) + 1
    finally:
        monkeypatch.setattr(c.pod, "job_start", original_start)
        c.machine.release(lease, False, failure=failure, stage="start")
    assert c.pod.cancelled == posted
    assert {"id": posted[0], "state": "cancelled"} in checkpoints
    assert len(c.pod.job_log) == len(jobs_before_start) + 2  # Generation, then the mandatory reset.
    assert "pod_reset reset --run-dir" in c.pod.job_log[-1]
    assert c.read()["lease"]["state"] == "idle"
    assert c.read()["lease"]["last_job"]["outcome"] == "cancelled"
    assert [kind for kind, _ in c.active] == ["begin", "done"]


def test_cancel_during_first_poll_stops_before_fetch(machine_case, monkeypatch):
    from threading import Event

    c = machine_case
    _claim_machine(c)
    job = c.job()
    abort = Event()
    original_poll = c.pod.job_poll
    generation = []

    def poll(job_id):
        record = original_poll(job_id)
        if not generation and c.read()["lease"].get("job") == {"id": job_id, "state": "running"}:
            generation.append(job_id)
            abort.set()
        return record

    def fetch(*args):
        pytest.fail("cancelled generation proceeded to fetch")

    monkeypatch.setattr(c.pod, "job_poll", poll)
    monkeypatch.setattr(c.machine, "fetch", fetch)
    with pytest.raises(MachineRunCancelled):
        run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=abort.is_set,
                       sleep=lambda s: None)
    assert generation and c.pod.cancelled == generation
    assert c.read()["lease"]["last_job"]["outcome"] == "cancelled"
    assert c.read()["lease"]["state"] == "idle"


def test_cancel_stops_the_job_checkpoints_it_then_keeps_verified_machine(machine_case, monkeypatch):
    c = machine_case
    _claim_machine(c)
    job = c.job()
    original = c.pod.job_poll
    generation_job = [None]
    original_start = c.pod.job_start

    def launch(command, *, job_id):
        if " generate " in command:
            generation_job[0] = job_id
        return original_start(command, job_id=job_id)

    # Start can now observe abort before the first poll. Track the exact
    # generation at launch so its cleanup reset is never mistaken for it.
    monkeypatch.setattr(c.pod, "job_start", launch)
    def running(job_id):  # the generation job keeps running; preparation jobs finish
        if generation_job[0] is None and c.read()["lease"].get("job", {}).get("id") == job_id:
            generation_job[0] = job_id
        if generation_job[0] == job_id:
            return {"id": job_id, "state": "running", "exit_code": ""}
        return original(job_id)
    c.pod.job_poll = running
    polls = iter([False, True])
    def cancelled():
        if c.owner.path.exists() and c.read()["lease"].get("job"):
            return next(polls, True)
        return False
    with pytest.raises(MachineRunCancelled):
        run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=cancelled,
                       sleep=lambda s: None)
    c.pod.job_poll = original
    record = c.read()
    generation = next(line for line in c.pod.job_log if " generate " in line)
    assert generation and c.pod.cancelled, "the remote job was not cancelled"
    assert record["lease"]["job"]["state"] == "exited"  # verified reset followed the exact stop
    assert record["state"] == "claimed" and record["lease"]["state"] == "idle"
    assert record["lease"]["last_job"]["outcome"] == "cancelled" and c.deletes == []


@pytest.mark.parametrize("bad_ack", [{"id": "other-job", "state": "cancelled"},
                                         {"state": "cancelled"}])
def test_unverified_cancel_ack_never_settles_warm(machine_case, monkeypatch, bad_ack):
    c = machine_case
    _claim_machine(c)
    job = c.job()
    lease = c.machine.claim(job)
    run = c.machine.start(lease, job)
    original = c.pod.job_cancel
    replies = [bad_ack]

    def cancel(job_id):
        return replies.pop(0) if replies else original(job_id)

    monkeypatch.setattr(c.pod, "job_cancel", cancel)
    with pytest.raises(rm.MachineError, match="exact remote job stop could not be verified"):
        c.machine.cancel(run)
    c.machine.release(lease, False, failure=rm.MachineError("unverified stop"), stage="cancel")
    assert c.read()["state"] == "cleanup_complete" and c.deletes == ["pod-1"]
    assert c.creates == 1


def test_generation_policy_waits_for_stock_through_the_real_machine_claim(machine_case, monkeypatch):
    from dataclasses import replace
    from astrid.packs.runpod import target_selection

    c = machine_case
    job = c.job()
    policy = {**job.policy, "offer_digest": None, "deadline": None, "max_hourly_usd": 1.5,
              "policy_digest": target_selection.policy_consent_digest(c.owner.preferences, 1.5),
              "capacity_timeout_seconds": 30, "wait_for_capacity": True}
    calls = []
    original = c.owner.observe_offer

    def observe():
        calls.append(1)
        if len(calls) < 3:
            raise target_selection.OfferObservationError("stock_unavailable")
        return original()

    async def sleep(seconds):
        c.clock.advance(seconds)

    c.owner.observe_offer, c.owner.sleep = observe, sleep
    c.owner.observe_offers = lambda: (observe(),)
    c.owner.monotonic = c.clock
    monkeypatch.setattr(target_selection.random, "uniform", lambda *args: 0)
    c.host.start_fresh_claim()
    c.host.claim(replace(c.grant, invocation_id="manual-claim", offer_digest=None, deadline=None,
                         policy_digest=target_selection.policy_consent_digest(c.owner.preferences, 1.5),
                         max_hourly_usd=1.5, capacity_timeout_seconds=30, wait_for_capacity=True,
                         preparation_seconds=0, execution_seconds=0),
                 requested_idle_seconds=c.grant.idle_seconds)
    run_on_machine(c.machine, replace(job, policy={"kind": policy["kind"], "engine": policy["engine"],
                                                    "preferences": policy["preferences"]}),
                   destination=job.scratch / "outputs", cancelled=lambda: False,
                   sleep=lambda seconds: None)
    assert len(calls) == 3 and c.creates == 1 and c.read()["lease"]["state"] == "idle"


def test_a_crash_after_job_start_is_cancelled_by_recover_before_delete(machine_case):
    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    run = c.machine.start(lease, c.job())
    assert c.read()["lease"]["job"] == {"id": run.job_id, "state": "running"}
    # The host dies here: no poll, fetch or release. A replacement owner recovers.
    c.host.close()
    replacement = c.fresh()
    replacement.recover()
    record = c.read()
    assert c.pod.cancelled == [run.job_id]
    assert record["lease"]["job"] == {"id": run.job_id, "state": "cancelled"}
    assert record["state"] == "cleanup_complete" and c.deletes == ["pod-1"]


def test_uncertain_job_stop_fences_reuse_until_exact_cancel_and_delete(machine_case):
    """A lost pod worker may have an unconfirmed preparation or prompt."""
    from astrid.packs.runpod.worker_preparation import AcquisitionUnresolvedError
    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    run = c.machine.start(lease, c.job())
    c.host.close()
    replacement = c.fresh()
    cancel = c.pod.job_cancel
    attempted = []

    def uncertain(job_id):
        attempted.append(job_id)
        return {"id": job_id, "state": "running"}

    c.pod.job_cancel = uncertain
    with pytest.raises(AcquisitionUnresolvedError, match="exact machine job stop remains unresolved"):
        replacement.recover()
    record = c.read()
    assert attempted == [run.job_id]
    assert record["state"] != "cleanup_complete" and record["lease"]["state"] != "idle"
    assert c.deletes == [] and c.creates == 1
    c.pod.job_cancel = cancel
    replacement.recover()
    record = c.read()
    assert c.pod.cancelled == [run.job_id]
    assert record["lease"]["job"] == {"id": run.job_id, "state": "cancelled"}
    assert record["state"] == "cleanup_complete" and c.deletes == ["pod-1"]


def test_a_tampered_output_is_refused_and_the_pod_released(machine_case):
    c = machine_case
    _claim_machine(c)
    c.pod.corrupt_get = "cpu.png"
    job = c.job()
    with pytest.raises(rm.DigestMismatch):
        run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: False,
                       sleep=lambda s: None)
    assert not (job.scratch / "outputs" / "artifacts" / "cpu.png").exists()
    assert c.read()["state"] == "cleanup_complete" and c.deletes == ["pod-1"]


def test_an_unsafe_manifest_path_is_refused(machine_case, tmp_path):
    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    run = c.machine.start(lease, c.job())
    out = run.remote_root + "/out"
    body = json.dumps({"schema_version": 1, "files": {"../escape": {"sha256": "0" * 64, "size": 1}}}) + "\n"
    c.pod.files[f"{out}/{rm.MANIFEST_NAME}"] = body.encode()
    import hashlib
    stdout = f"{rm.RESULT_MARK}{{}}\n{rm.MANIFEST_MARK}{hashlib.sha256(body.encode()).hexdigest()}\n"
    c.pod.files[run.directory + "/stdout"] = stdout.encode()
    with pytest.raises(rm.DigestMismatch, match="unsafe path"):
        c.machine.fetch(run, tmp_path / "dest")
    c.machine.release(lease, False)
    assert c.deletes == ["pod-1"]


def test_liveness_ends_a_dead_idle_lease_with_provider_terminated(machine_case):
    """T5d: a host poll that finds the pod gone ends the lease; it stops being charged and leaves reuse."""
    c = machine_case
    _claim_machine(c)
    job = c.job()
    run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: False,
                   sleep=lambda s: None)
    assert c.host.observe_liveness(observe=lambda pod: "live") == "live"
    assert c.read()["lease"]["state"] == "idle" and c.deletes == []
    assert c.host.observe_liveness(observe=lambda pod: "gone") == "provider_terminated"
    record = c.read()
    assert record["state"] == "cleanup_complete" and record["lease"]["ended"] == "provider_terminated"
    assert c.host.observe_liveness(observe=lambda pod: "gone") is None  # nothing live is left
    ended = json.loads(c.host.ended_path().read_text())
    assert [(row["id"], row["ended"], row["live"]) for row in ended] == [("pod-1", "provider_terminated", False)]
    assert rm.lease_rows(c.host)[0]["ended"] == "provider_terminated"


def test_a_job_pinned_to_an_ended_machine_is_refused_with_the_reason(machine_case):
    from astrid.packs.runpod.target_selection import PaidSelectionError

    c = machine_case
    _claim_machine(c)
    job = c.job()
    run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: False,
                   sleep=lambda s: None)
    c.host.observe_liveness(observe=lambda pod: "gone")
    pinned = c.job(2)
    pinned = type(pinned)(**{**pinned.__dict__, "policy": {**pinned.policy, "machine": "pod-1"}})
    creates = c.creates
    with pytest.raises(PaidSelectionError, match=r"no live runpod lease for machine 'pod-1' "
                                                 r"\(ended: provider_terminated\); live leases: none"):
        c.machine.claim(pinned)
    assert c.creates == creates


def test_the_host_observes_machines_on_its_polls_throttled(monkeypatch):
    from types import SimpleNamespace
    from astrid.core.execution.generic_host import GenericPackHost

    calls = []
    host = SimpleNamespace(_target_bindings=SimpleNamespace(observe=lambda: calls.append(1)))
    host._handoff_accounting_owner = host
    monkeypatch.setenv("ASTRID_MACHINE_LIVENESS_SECONDS", "10")
    for now in (100.0, 105.0, 111.0):
        GenericPackHost._observe_machines(host, now)
    assert calls == [1, 1]
    host._target_bindings = SimpleNamespace(observe=lambda: (_ for _ in ()).throw(RuntimeError("provider down")))
    GenericPackHost._observe_machines(host, 200.0)  # never raises into the claim loop


def test_an_entry_from_a_pack_missing_from_the_shipped_selection_is_refused_before_the_claim(machine_case, tmp_path):
    from types import SimpleNamespace

    c = machine_case
    installed = SimpleNamespace(id="studio.workflows", scope="installed", path=tmp_path / "studio" / "w.yaml",
                                fields={"entries": [{"id": "studio-look"}]})
    builtin = SimpleNamespace(id="vibecomfy.ready", scope="built-in",
                              path=rm._agent_packs() / "vibecomfy" / "extensions" / "w.yaml",
                              fields={"entries": [{"id": "z-image"}]})
    registry = SimpleNamespace(contributions=lambda point: [installed, builtin])
    reason = rm.unshipped_entry_reason("studio-look", registry=registry, action_rows=[])
    assert "studio.workflows (installed pack" in reason and "outside the active pack selection" in reason
    assert rm.unshipped_entry_reason("z-image", registry=registry, action_rows=[]) is None
    assert rm.unshipped_entry_reason(None, registry=registry, action_rows=[]) is None
    assert rm.unshipped_entry_reason("h3", registry=SimpleNamespace(contributions=lambda p: []),
                                     action_rows=[{"id": "h3", "action": "h3_av.transform"}]) is None
    assert "pack 'elsewhere'" in rm.unshipped_entry_reason(
        "x", registry=SimpleNamespace(contributions=lambda p: []), action_rows=[{"id": "x", "action": "elsewhere.run"}])
    # Through the machine: refused before any offer observation, lock or create.
    import astrid.packs.runpod.machine as module
    original = module.unshipped_entry_reason
    module.unshipped_entry_reason = lambda entry: original(entry, registry=registry, action_rows=[])
    try:
        job = c.job()
        job = type(job)(**{**job.__dict__, "entry": "studio-look"})
        quotes = len(c.quotes)
        with pytest.raises(rm.MachineError, match="outside the active pack selection"):
            c.machine.claim(job)
    finally:
        module.unshipped_entry_reason = original
    assert c.creates == 0 and len(c.quotes) == quotes and not c.owner.path.exists()


def test_the_selection_ships_active_sources_and_the_user_entry_folder(tmp_path):
    """T5e: build_selection pins each active source and copies the --as-entry folder."""
    import tarfile
    from types import SimpleNamespace

    pack = tmp_path / "studio"
    (pack / "extensions" / "vibecomfy.workflows").mkdir(parents=True)
    (pack / "pack.yaml").write_text("id: studio\n")
    (pack / ".git").mkdir()
    (pack / ".git" / "HEAD").write_text("ref")
    entries = tmp_path / "entries" / "mine"
    entries.mkdir(parents=True)
    (entries / "entry.json").write_text(json.dumps({"id": "mine"}))
    (entries / "workflow.py").write_text("# bundle")
    source = SimpleNamespace(pack_id="studio", pack_root=pack, revision="a" * 40, repository="file:///studio",
                             tree_sha256="b" * 64, manifest_sha256="c" * 64)
    archive, manifest = rm.build_selection("mine", tmp_path / "stage", sources=[source],
                                           entries_dir=tmp_path / "entries")
    assert manifest["packs"][0]["tree_sha256"] == "b" * 64 and "pack.yaml" in manifest["packs"][0]["files"]
    assert manifest["entries"] == [{"id": "mine", "folder": "mine", "files": {
        "entry.json": rm.sha256_file(entries / "entry.json"), "workflow.py": rm.sha256_file(entries / "workflow.py")}}]
    with tarfile.open(archive) as handle:
        names = set(handle.getnames())
    assert {"packs/studio/pack.yaml", "entries/mine/entry.json", "entries/mine/workflow.py", "selection.json"} <= names
    assert not any(".git" in name for name in names)
    env = rm.selection_env("/root/astrid/runs/x")
    assert "ASTRID_PACKS_PATH=/root/astrid/runs/x/selection/packs" in env
    assert "VIBECOMFY_ENTRIES_DIR=/root/astrid/runs/x/selection/entries" in env
    assert "ASTRID_SOURCE_STATE=" in env and "ASTRID_USER_PACKS=" in env


def test_the_job_records_its_run_and_agent_folders_in_active_job_json(tmp_path):
    """T5f follow-up: the active-job line, run by bash as the agent runs command.sh, names both folders."""
    import subprocess
    import sys

    command = rm.generation_command("cpu-image", "/root/astrid/runs/abc", mode="t2i",
                                    active_job={"run_id": "abc", "task": "t", "attempt": "a"})
    (line,) = [row for row in command.splitlines()
               if row.startswith(f"{rm.POD_VENV}/bin/python -c ") and "active-job.json" in row]
    active = tmp_path / "active-job.json"
    line = line.replace(f"{rm.POD_VENV}/bin/python", sys.executable).replace("/root/astrid/active-job.json", str(active))
    job_dir = tmp_path / "agent-job"
    job_dir.mkdir()
    script = job_dir / "command.sh"
    script.write_text(line + "\n")
    subprocess.run(["bash", str(script)], check=True)
    assert json.loads(active.read_text()) == {"run_id": "abc", "task": "t", "attempt": "a",
                                              "run_dir": "/root/astrid/runs/abc", "job_dir": str(job_dir)}


def test_claim_sizes_the_volume_by_the_job_manifest_rows(machine_case, monkeypatch):
    """Plan 32 T5h: the claim hands the job's manifest rows to the volume check, not an empty tuple."""
    from astrid.core.generation import entries as catalog
    from astrid.packs.runpod import generation as runpod_generation

    c = machine_case
    _claim_machine(c)
    rows = ({"name": "need.safetensors", "subdir": "vae", "size": 10 ** 12},)
    monkeypatch.setattr(catalog, "find_entry", lambda entry_id: {"id": entry_id})
    monkeypatch.setattr(catalog, "requirements", lambda entry, inputs=None: {"models": list(rows)})
    captured = {}

    def capture(*args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after the volume requirement is captured")

    monkeypatch.setattr(runpod_generation, "prepare_retained_generation", capture)
    with pytest.raises(RuntimeError, match="stop after"):
        c.machine.claim(c.job())
    assert tuple(captured["manifest_rows"]) == rows
    assert c.machine._require().installation.model_bytes(captured["manifest_rows"]) == 10 ** 12


@pytest.mark.parametrize("state,lease_state,display", [
    ("claimed", "preparing", "preparing"),
    ("allocated_unverified", "preparing", "unresolved"),
    ("cleanup_pending", "releasing", "releasing"),
])
def test_unfinished_machine_custody_is_live(state, lease_state, display):
    row = rm.lease_row({"pod_id": "pod-exact", "state": state,
                        "reconciliation_required": True, "lease": {"state": lease_state}})
    assert row["live"] is True and row["state"] == display and row["ended"] is None


def test_live_machine_row_shows_idle_release_and_accrued_estimated_cost(monkeypatch):
    monkeypatch.setattr(rm.time, "time", lambda: 2_000.0)
    row = rm.lease_row({"pod_id": "pod-idle", "state": "claimed", "estimated_cost_usd": 2.5,
                        "lease": {"state": "idle", "paid_expires_at": 5_000.0,
                                  "idle_expires_at": 2_600.0, "hourly_usd": 1.2,
                                  "accounted_at": 1_100.0}})

    assert row["release_at"] == 2_600.0
    assert row["minutes_left"] == 10.0
    assert row["cost_usd"] == 2.8
    assert row["hourly_usd"] == 1.2


def test_active_machine_uses_paid_limit_and_cost_grows_with_clock(monkeypatch):
    record = {"pod_id": "pod-active", "state": "claimed", "estimated_cost_usd": 3.0,
              "lease": {"state": "active", "paid_expires_at": 3_800.0,
                        "idle_expires_at": 2_100.0, "hourly_usd": 3.6,
                        "accounted_at": 1_900.0}}
    monkeypatch.setattr(rm.time, "time", lambda: 2_000.0)
    first = rm.lease_row(record)
    monkeypatch.setattr(rm.time, "time", lambda: 2_300.0)
    second = rm.lease_row(record)

    assert first["release_at"] == 3_800.0
    assert first["minutes_left"] == 30.0
    assert first["cost_usd"] == 3.1
    assert second["cost_usd"] == 3.4


def test_ended_machine_row_keeps_recorded_cost_without_live_deadline(monkeypatch):
    monkeypatch.setattr(rm.time, "time", lambda: 2_000.0)
    row = rm.lease_row({"pod_id": "pod-ended", "state": "cleanup_complete",
                        "estimated_cost_usd": 7.25,
                        "lease": {"state": "releasing", "paid_expires_at": 3_800.0,
                                  "idle_expires_at": 2_100.0, "hourly_usd": 3.6,
                                  "accounted_at": 1_900.0, "ended": "released"}})

    assert row["live"] is False and row["state"] == "ended"
    assert row["cost_usd"] == 7.25
    assert row["release_at"] is None and row["minutes_left"] is None
    assert row["ended"] == "released"


def test_machines_table_and_json_show_lifetime_fields(monkeypatch, capsys):
    from astrid.core import machines_cli

    row = {"id": "pod-idle", "kind": "runpod", "gpu": "RTX 5090", "state": "idle",
           "live": True, "release_at": 2_600.0, "minutes_left": 10.0,
           "cost_usd": 2.8, "hourly_usd": 1.2, "ended": None}
    monkeypatch.setattr(machines_cli, "_leases", lambda: [row])

    assert machines_cli.main([]) == 0
    table = capsys.readouterr().out
    assert "RELEASES (UTC)" in table and "MIN LEFT" in table
    assert "COST EST. (USD)" in table and "RATE (USD/HR)" in table
    assert "10.0" in table and "2.80" in table and "1.20" in table

    assert machines_cli.main(["--json"]) == 0
    import json
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["release_at"] == 2_600.0
    assert payload[0]["minutes_left"] == 10.0
    assert payload[0]["cost_usd"] == 2.8
    assert payload[0]["hourly_usd"] == 1.2


def test_machines_setup_saves_ordered_choices_and_volume(monkeypatch, capsys):
    from astrid.core import machines_cli
    from astrid.core.generation import settings

    saved = {"provider_account_ref": "acct", "image": "registry/image@sha256:" + "a" * 64}
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: saved)
    saved_policy = {}
    volumes = [{"id": "vol-1", "name": "data", "dataCenterId": "US-GA-1",
                "size_gb": 100}]
    assert machines_cli.main(["setup", "--volume", "data", "--gpu", "RTX A", "--gpu", "RTX B",
                              "--budget-usd", "8", "--json"],
                             volumes=lambda: volumes,
                             offered_gpus=lambda dc, has_volume: ["RTX A", "RTX B"],
                             setup=lambda policy: saved_policy.update(policy) or type("Result", (), {"ok": True})()) == 0
    import json
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert saved_policy["gpu_types"] == ["RTX A", "RTX B"]
    assert saved_policy["datacenter_id"] == "US-GA-1"
    assert saved_policy["volume"]["id"] == "vol-1"
    assert saved_policy["budget_cap_usd"] == 8


def test_machines_setup_fresh_uses_pinned_default_and_saved_credential_reference(monkeypatch):
    from astrid.core import machines_cli
    from astrid.core.generation import settings
    from astrid.packs.runpod.target_selection import DEFAULT_RUNPOD_WORKER_IMAGE

    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: {
        "credential_ref": "env:RUNPOD_API_KEY",
    })
    saved = {}
    result = machines_cli.main(
        ["setup", "--gpu", "RTX A", "--budget-usd", "7", "--json"],
        volumes=lambda: [], offered_gpus=lambda *_: ["RTX A"],
        setup=lambda policy: saved.update(policy) or type("Result", (), {"ok": True})(),
    )
    assert result == 0
    assert saved["image"] == DEFAULT_RUNPOD_WORKER_IMAGE
    assert saved["provider_account_ref"] == "env:RUNPOD_API_KEY"
    assert saved["credential_ref"] == "env:RUNPOD_API_KEY"


def test_machines_setup_preserves_saved_image_and_accepts_per_setup_override(monkeypatch):
    from astrid.core import machines_cli
    from astrid.core.generation import settings

    stored_image = "registry/saved@sha256:" + "a" * 64
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: {
        "provider_account_ref": "account", "credential_ref": "env:RUNPOD_API_KEY",
        "image": stored_image, "gpu_types": ["RTX A"], "budget_cap_usd": 5,
    })
    saved = []
    kwargs = {"volumes": lambda: [], "offered_gpus": lambda *_: ["RTX A"],
              "setup": lambda policy: saved.append(policy) or type("Result", (), {"ok": True})()}
    assert machines_cli.main(["setup", "--json"], **kwargs) == 0
    assert saved[-1]["image"] == stored_image
    override = "registry/override@sha256:" + "b" * 64
    assert machines_cli.main(["setup", "--image", override, "--json"], **kwargs) == 0
    assert saved[-1]["image"] == override


def test_machines_setup_missing_credential_names_credential_command(monkeypatch, capsys):
    from astrid.core import machines_cli
    from astrid.core.generation import settings
    from astrid.core.util.credentials_scope import CredentialsScope

    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: {})
    def missing(*args, **kwargs):
        raise RuntimeError("credential absent")
    monkeypatch.setattr(CredentialsScope, "resolve_local", missing)
    assert machines_cli.main(["setup", "--json"]) == 2
    output = capsys.readouterr().out
    assert "astrid credential set runpod" in output
    assert "credential absent" not in output


def test_machines_setup_never_discloses_resolved_credential_sentinel(monkeypatch, capsys, caplog):
    import json
    from types import SimpleNamespace
    from astrid.core import machines_cli
    from astrid.core.generation import settings
    from astrid.core.util.credentials_scope import CredentialsScope
    from astrid.packs.runpod import adapter

    sentinel = "FAKE_RUNPOD_KEY_SENTINEL_DO_NOT_LEAK_73C4"
    inventory_keys = []
    saved_preferences = {}
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: saved_preferences)
    monkeypatch.setattr(CredentialsScope, "resolve_local",
                        lambda *args, **kwargs: SimpleNamespace(value=sentinel))

    async def fake_volumes(api_key):
        inventory_keys.append(api_key)
        return []

    async def fake_gpu_types(api_key, *, datacenter_id, require_network_volume):
        inventory_keys.append(api_key)
        return ["RTX A"]

    monkeypatch.setattr(adapter, "setup_volumes", fake_volumes)
    monkeypatch.setattr(adapter, "setup_gpu_types", fake_gpu_types)
    assert machines_cli.main(["setup", "--gpu", "RTX A", "--budget-usd", "4", "--json"],
                             setup=lambda policy: saved_preferences.update(policy)
                             or SimpleNamespace(ok=True)) == 0
    setup_output = capsys.readouterr().out
    assert inventory_keys == [sentinel, sentinel]
    assert sentinel not in setup_output
    assert sentinel not in caplog.text
    assert sentinel not in json.dumps(saved_preferences, sort_keys=True)

    def fail_with_sentinel(_policy):
        raise RuntimeError(sentinel)

    assert machines_cli.main(["setup", "--gpu", "RTX A", "--budget-usd", "4", "--json"],
                             setup=fail_with_sentinel) == 1
    setup_error = capsys.readouterr().out
    assert sentinel not in setup_error
    assert sentinel not in caplog.text

    assert machines_cli.main(["setup", "--show", "--json"],
                             show_location=lambda: {"data_root": "/tmp/astrid-data", "realm": "realm-x"}) == 0
    shown = capsys.readouterr().out
    assert sentinel not in shown
    assert sentinel not in caplog.text


def test_machines_setup_show_reports_sanitized_preferences_and_store_location(monkeypatch, capsys):
    from astrid.core import machines_cli
    from astrid.core.generation import settings

    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: {
        "provider_account_ref": "account", "credential_ref": "env:RUNPOD_API_KEY",
        "gpu_types": ["RTX A"], "image": "registry/x@sha256:" + "a" * 64,
        "api_key": "must-never-print-this-key",
    })
    assert machines_cli.main(["setup", "--show", "--json"],
                             show_location=lambda: {"data_root": "/tmp/astrid-data", "realm": "realm-x"}) == 0
    output = capsys.readouterr().out
    assert "user/generation" in output
    assert "/tmp/astrid-data" in output and "realm-x" in output
    assert "env:RUNPOD_API_KEY" in output
    assert "must-never-print-this-key" not in output


def test_machines_setup_datacenter_filters_inventory_and_saves_choice(monkeypatch, capsys):
    from astrid.core import machines_cli
    from astrid.core.generation import settings

    saved = {"provider_account_ref": "acct", "image": "registry/image@sha256:" + "a" * 64}
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: saved)
    observed = []
    persisted = {}

    def gpu_inventory(datacenter, has_volume):
        observed.append((datacenter, has_volume))
        return ["RTX in region"] if datacenter == "US-TX-1" else ["RTX everywhere"]

    assert machines_cli.main(["setup", "--datacenter", "US-TX-1", "--gpu", "RTX in region",
                              "--budget-usd", "5", "--json"],
                             volumes=lambda: [], offered_gpus=gpu_inventory,
                             setup=lambda policy: persisted.update(policy) or type("Result", (), {"ok": True})()) == 0
    import json
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert observed == [("US-TX-1", False)]
    assert persisted["datacenter_id"] == "US-TX-1"
    assert persisted["gpu_types"] == ["RTX in region"]


def test_machines_setup_any_is_unrestricted_and_volume_dc_conflict_refuses(monkeypatch, capsys):
    from astrid.core import machines_cli
    from astrid.core.generation import settings

    saved = {"provider_account_ref": "acct", "image": "registry/image@sha256:" + "a" * 64}
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: saved)
    observed = []
    persisted = {}
    assert machines_cli.main(["setup", "--datacenter", "any", "--gpu", "RTX A",
                              "--budget-usd", "5", "--json"], volumes=lambda: [],
                             offered_gpus=lambda dc, has_volume: observed.append((dc, has_volume)) or ["RTX A"],
                             setup=lambda policy: persisted.update(policy) or type("Result", (), {"ok": True})()) == 0
    assert observed == [(None, False)]
    assert "datacenter_id" not in persisted
    volumes = [{"id": "vol", "name": "data", "dataCenterId": "US-TX-1"}]
    assert machines_cli.main(["setup", "--volume", "vol", "--datacenter", "EU-RO-1",
                              "--gpu", "RTX A", "--budget-usd", "5", "--json"],
                             volumes=lambda: volumes, offered_gpus=lambda *_: ["RTX A"],
                             setup=lambda *_: None) == 2
    assert "fixed to a different datacenter" in capsys.readouterr().out


def test_machines_setup_explicit_any_clears_saved_datacenter(monkeypatch):
    from astrid.core import machines_cli
    from astrid.core.generation import settings

    monkeypatch.setattr(settings, "machine_policy", lambda *a, **kw: {
        "provider_account_ref": "acct", "image": "registry/image@sha256:" + "a" * 64,
        "datacenter_id": "US-TX-1", "gpu_types": ["RTX A"], "budget_cap_usd": 5,
    })
    for json_flag in ([], ["--json"]):
        saved, observed = {}, []
        assert machines_cli.main(["setup", "--datacenter", "any", *json_flag],
            volumes=lambda: [], datacenters=lambda: pytest.fail("unexpected datacenter lookup"),
            offered_gpus=lambda dc, volume: observed.append((dc, volume)) or ["RTX A"],
            setup=lambda policy: saved.update(policy),
            input_fn=lambda prompt: "" if prompt.startswith("Volume") else pytest.fail("unexpected prompt")) == 0
        assert observed == [(None, False)]
        assert "datacenter_id" not in saved

    saved = {}
    assert machines_cli.main(["setup", "--volume", "vol", "--datacenter", "any", "--json"],
        volumes=lambda: [{"id": "vol", "name": "data", "dataCenterId": "US-TX-1", "size_gb": 50}],
        offered_gpus=lambda *_: ["RTX A"], setup=lambda policy: saved.update(policy)) == 0
    assert saved["datacenter_id"] == "US-TX-1"


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
@pytest.mark.parametrize("failure", ["inventory", "gpu_inventory", "action_exception", "action_result"])
def test_machines_setup_hides_external_error_details(monkeypatch, capsys, caplog, json_flag, failure):
    from types import SimpleNamespace
    from astrid.core import machines_cli
    from astrid.core.generation import settings

    secret = "credential-sentinel-never-disclose"
    monkeypatch.setattr(settings, "machine_policy", lambda *a, **kw: {
        "provider_account_ref": "acct", "image": "registry/image@sha256:" + "a" * 64,
        "gpu_types": ["RTX A"], "budget_cap_usd": 5,
    })
    def inventory():
        if failure == "inventory":
            raise ValueError(secret)
        return []
    def setup(_policy):
        if failure == "action_exception":
            raise ValueError(secret)
        return SimpleNamespace(ok=False, error={"message": secret})
    def gpus(*_args):
        if failure == "gpu_inventory":
            raise ValueError(secret)
        return ["RTX A"]

    result = machines_cli.main(["setup", "--datacenter", "any", *json_flag],
        volumes=inventory, offered_gpus=gpus, setup=setup,
        input_fn=lambda _: "")
    assert result != 0
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    if json_flag:
        assert json.loads(captured.out)["ok"] is False
    assert secret not in caplog.text


def test_machines_setup_hides_async_datacenter_error(monkeypatch, capsys, caplog):
    from types import SimpleNamespace
    from astrid.core import machines_cli
    from astrid.core.generation import settings
    from astrid.core.util.credentials_scope import CredentialsScope
    from astrid.packs.runpod import adapter

    secret = "async-credential-sentinel"
    monkeypatch.setattr(settings, "machine_policy", lambda *a, **kw: {})
    monkeypatch.setattr(CredentialsScope, "resolve_local", lambda *a, **kw: SimpleNamespace(value=secret))
    async def volumes(key):
        assert key == secret
        return []
    async def datacenters(key):
        assert key == secret
        raise ValueError(secret)
    async def gpus(*args, **kwargs):
        pytest.fail("GPU lookup must not follow failed datacenter lookup")
    monkeypatch.setattr(adapter, "setup_volumes", volumes)
    monkeypatch.setattr(adapter, "setup_datacenters", datacenters)
    monkeypatch.setattr(adapter, "setup_gpu_types", gpus)
    assert machines_cli.main(["setup"], input_fn=lambda _: "") == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err + caplog.text


def test_setup_saves_gpu_offered_but_out_of_stock_in_volume_datacenter(capsys, monkeypatch):
    from astrid.core import machines_cli
    from astrid.core.generation import settings

    monkeypatch.setattr(settings, "machine_policy", lambda *a, **kw: {
        "provider_account_ref": "acct", "image": "registry/image@sha256:" + "a" * 64,
        "gpu_types": ["RTX 5090"], "budget_cap_usd": 5,
    })

    class Choices(list):
        stock_missing = ("RTX 5090",)
    choices = Choices(["RTX 5090"])
    saved = {}
    assert machines_cli.main(["setup", "--volume", "vol", "--gpu", "RTX 5090", "--budget-usd", "5"],
        volumes=lambda: [{"id": "vol", "name": "data", "dataCenterId": "EUR-NO-1", "size_gb": 50}],
        offered_gpus=lambda dc, has_volume: choices,
        setup=lambda policy: saved.update(policy)) == 0
    output = capsys.readouterr().out
    assert saved["gpu_types"] == ["RTX 5090"]
    assert "RTX 5090 is not in stock in EUR-NO-1" in output


def test_setup_refuses_gpu_not_offered_in_volume_datacenter(capsys):
    from astrid.core import machines_cli

    assert machines_cli.main(["setup", "--volume", "vol", "--gpu", "RTX 4090"],
        volumes=lambda: [{"id": "vol", "name": "data", "dataCenterId": "EUR-NO-1", "size_gb": 50}],
        offered_gpus=lambda *_: ["RTX 5090"], setup=lambda _: pytest.fail("must refuse")) != 0
    output = capsys.readouterr().out
    assert "RTX 4090" in output and "EUR-NO-1" in output


def test_setup_warns_when_datacenter_availability_rows_are_missing(capsys, monkeypatch):
    from astrid.core import machines_cli
    from astrid.core.generation import settings

    monkeypatch.setattr(settings, "machine_policy", lambda *a, **kw: {
        "provider_account_ref": "acct", "image": "registry/image@sha256:" + "a" * 64,
        "gpu_types": ["GPU A"], "budget_cap_usd": 5,
    })

    class Choices(list):
        availability_fallback = True
    choices = Choices(["GPU A"])
    assert machines_cli.main(["setup", "--datacenter", "DC-A", "--gpu", "GPU A", "--budget-usd", "5"], volumes=lambda: [],
        offered_gpus=lambda *_: choices, setup=lambda _: None,
        input_fn=lambda _: "") == 0
    assert "no datacenter GPU availability rows" in capsys.readouterr().out


def test_machines_setup_failed_action_keeps_saved_policy(monkeypatch, capsys):
    from types import SimpleNamespace
    from astrid.core import machines_cli
    from astrid.core.generation import settings

    saved = {"provider_account_ref": "acct", "image": "registry/image@sha256:" + "a" * 64,
             "gpu_types": ["RTX A"], "budget_cap_usd": 5}
    before = dict(saved)
    secret = "failed-action-sentinel"
    monkeypatch.setattr(settings, "machine_policy", lambda *a, **kw: saved)
    assert machines_cli.main(["setup", "--datacenter", "any", "--json"], volumes=lambda: [],
        offered_gpus=lambda *_: ["RTX A"],
        setup=lambda _: SimpleNamespace(ok=False, error={"message": secret})) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is False
    assert secret not in captured.out + captured.err
    assert saved == before


def test_machines_setup_reports_sanitized_action_error_code(monkeypatch, capsys):
    from types import SimpleNamespace
    from astrid.core import machines_cli
    from astrid.core.generation import settings

    monkeypatch.setattr(settings, "machine_policy", lambda *a, **kw: {
        "provider_account_ref": "acct", "image": "registry/image@sha256:" + "a" * 64,
        "gpu_types": ["RTX A"], "budget_cap_usd": 5,
    })
    assert machines_cli.main(["setup", "--datacenter", "any", "--json"],
        volumes=lambda: [], offered_gpus=lambda *_: ["RTX A"],
        setup=lambda _: SimpleNamespace(ok=False, error={"code": "settings_write_refused",
                                                          "message": "provider-secret-body"})) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "settings_write_refused"
    assert "provider-secret-body" not in str(payload)


def test_machines_setup_interactive_datacenter_keeps_gpu_order(monkeypatch, capsys):
    from astrid.core import machines_cli
    from astrid.core.generation import settings

    saved = {"provider_account_ref": "acct", "image": "registry/image@sha256:" + "a" * 64}
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: saved)
    answers = iter(("", "US-TX-1", "RTX B, RTX A", "5"))
    captured = {}
    offered = []
    assert machines_cli.main(["setup"], volumes=lambda: [], datacenters=lambda: ["US-TX-1"],
                             offered_gpus=lambda dc, has_volume: offered.append((dc, has_volume)) or ["RTX A", "RTX B"],
                             input_fn=lambda _: next(answers),
                             setup=lambda policy: captured.update(policy) or type("Result", (), {"ok": True})()) == 0
    capsys.readouterr()
    assert offered == [("US-TX-1", False)]
    assert captured["datacenter_id"] == "US-TX-1"
    assert captured["gpu_types"] == ["RTX B", "RTX A"]


def test_machine_keep_for_uses_recorded_consent_data_and_reports_new_limit(monkeypatch, capsys):
    from types import SimpleNamespace
    from astrid.core import machines_cli

    row = {"id": "pod-keep", "state": "idle", "live": True, "hourly_usd": 2.0,
           "release_at": 2_600.0, "minutes_left": 10.0}
    updated = {**row, "release_at": 7_200.0, "minutes_left": 120.0}
    lease_calls = iter(([row], [updated]))
    monkeypatch.setattr(machines_cli, "_leases", lambda: next(lease_calls))
    monkeypatch.setattr(machines_cli, "_keep_consent_data", lambda machine_id, rate:
                        (object(), "sha256:" + "a" * 64))
    captured = {}

    def keep(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(ok=True)

    assert machines_cli.main(["keep", "pod-keep", "--for", "2h", "--max-usd", "4", "--json"], keep=keep) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["max_usd"] == 4.0
    assert payload["machine"]["release_at"] == 7_200.0
    assert captured["machine_id"] == "pod-keep" and captured["until_release"] is False
    assert captured["consent"].offer_digest == "sha256:" + "a" * 64
    assert captured["consent"].max_usd == 4.0
    assert captured["consent"].idle_seconds == 7200


def test_machine_keep_until_release_requires_cap_and_refuses_unknown_or_ended(monkeypatch, capsys):
    from astrid.core import machines_cli

    with pytest.raises(SystemExit):
        machines_cli.main(["keep", "pod-keep", "--until-release"])

    monkeypatch.setattr(machines_cli, "_leases", lambda: [
        {"id": "pod-ended", "live": False, "state": "ended"},
    ])
    assert machines_cli.main(["keep", "pod-ended", "--until-release", "--max-usd", "5"],
                             keep=lambda **kwargs: pytest.fail("ended machine was kept")) == 2
    assert "no live machine" in capsys.readouterr().out
    assert machines_cli.main(["keep", "unknown", "--until-release", "--max-usd", "5"],
                             keep=lambda **kwargs: pytest.fail("unknown machine was kept")) == 2
    assert "no live machine" in capsys.readouterr().out


def test_machine_keep_until_release_passes_required_cap(monkeypatch, capsys):
    from types import SimpleNamespace
    from astrid.core import machines_cli

    row = {"id": "pod-keep", "state": "idle", "live": True, "hourly_usd": 2.0,
           "release_at": 2_600.0}
    updated = {**row, "release_at": 11_600.0}
    lease_calls = iter(([row], [updated]))
    monkeypatch.setattr(machines_cli, "_leases", lambda: next(lease_calls))
    monkeypatch.setattr(machines_cli, "_keep_consent_data", lambda machine_id, rate:
                        (None, "sha256:" + "b" * 64))
    captured = {}
    assert machines_cli.main(["keep", "pod-keep", "--until-release", "--max-usd", "5"],
                             keep=lambda **kwargs: (captured.update(kwargs), SimpleNamespace(ok=True))[1]) == 0
    capsys.readouterr()
    assert captured["until_release"] is True
    assert captured["consent"].max_usd == 5.0
    assert captured["consent"].idle_seconds == pytest.approx(9000.0)


def test_machine_keep_for_shortens_to_a_lower_spend_cap(monkeypatch, capsys):
    from types import SimpleNamespace
    from astrid.core import machines_cli

    row = {"id": "pod-keep", "state": "idle", "live": True, "hourly_usd": 2.0,
           "release_at": 2_600.0}
    updated = {**row, "release_at": None}
    lease_calls = iter(([row], [updated]))
    monkeypatch.setattr(machines_cli, "_leases", lambda: next(lease_calls))
    monkeypatch.setattr(machines_cli, "_keep_consent_data", lambda machine_id, rate:
                        (None, "sha256:" + "c" * 64))
    captured = {}
    started = datetime.now(timezone.utc).timestamp()

    def keep(**kwargs):
        captured.update(kwargs)
        updated["release_at"] = started + kwargs["consent"].idle_seconds
        return SimpleNamespace(ok=True)

    assert machines_cli.main(["keep", "pod-keep", "--for", "2h", "--max-usd", "1", "--json"],
                             keep=keep) == 0
    payload = json.loads(capsys.readouterr().out)
    consent = captured["consent"]
    assert payload["ok"] is True and payload["max_usd"] == 1.0
    assert payload["machine"]["release_at"] == started + 1800
    assert payload["machine"]["release_at"] <= started + 2 * 3600
    assert consent.max_usd == 1.0 and consent.idle_seconds == pytest.approx(1800.0)
    assert consent.idle_seconds * 2.0 / 3600 <= consent.max_usd


def test_machine_keep_timeout_reports_unknown_and_points_to_machine_list(monkeypatch, capsys):
    from astrid.core import machines_cli

    monkeypatch.setattr(machines_cli, "_leases", lambda: [
        {"id": "pod-keep", "state": "idle", "live": True, "hourly_usd": 2.0},
    ])
    monkeypatch.setattr(machines_cli, "_keep_consent_data", lambda machine_id, rate:
                        (object(), "sha256:" + "a" * 64))
    result = machines_cli.main(["keep", "pod-keep", "--until-release", "--max-usd", "5", "--json"],
                               keep=lambda **kwargs: (_ for _ in ()).throw(TimeoutError("host wait expired")))
    payload = json.loads(capsys.readouterr().out)
    assert result == 1 and payload["unknown"] is True
    assert "unknown" in payload["error"] and "astrid machines" in payload["error"]


@pytest.mark.parametrize("restart", [False, True])
def test_unverified_machine_cli_release_and_recovery_reconcile_exact_pod(small_lease, monkeypatch, capsys, restart):
    from astrid.core import machines_cli
    from astrid.packs.runpod.worker_preparation import write_target_record

    c = small_lease
    owner = c.fresh()
    owner.acquire(c.grant)
    record = c.read()
    record.update(state="allocated_unverified", reconciliation_required=True)
    record["lease"]["state"] = "preparing"
    write_target_record(c.owner.path, record)
    monkeypatch.setattr(machines_cli, "_leases", lambda: rm.lease_rows(owner))
    assert rm.lease_rows(owner)[0]["live"] is True
    if restart:
        owner.close()
        owner = c.fresh()
        owner.recover()
    else:
        assert machines_cli.main(["release", "foreign", "--json"], release=owner.release) == 2
        assert c.deletes == []
        assert machines_cli.main(["release", record["pod_id"], "--json"], release=owner.release) == 0
        assert json.loads(capsys.readouterr().out.splitlines()[-1])["machine"]["state"] == "ended"
    assert c.creates == 1 and c.deletes == [record["pod_id"]]
    ended = c.read()
    assert ended["state"] == "cleanup_complete"
    assert ended["cleanup"]["absence"]["pod_id"] == record["pod_id"]
    assert ended["cleanup"]["absence"]["exact_get_status"] == 404
    assert ended["cleanup"]["absence"]["account_list_absent"] is True
    assert rm.lease_rows(owner)[0]["live"] is False


def test_unverified_live_machine_still_refuses_pinned_reuse(machine_case):
    from astrid.packs.runpod.target_selection import PaidSelectionError
    from astrid.packs.runpod.worker_preparation import write_target_record

    c = machine_case
    c.host.acquire(c.grant)
    record = c.read()
    record.update(state="allocated_unverified", reconciliation_required=True)
    record["lease"]["state"] = "preparing"
    write_target_record(c.host.selection.path, record)
    job = c.job()
    job = type(job)(**{**job.__dict__, "policy": {**job.policy, "machine": record["pod_id"]}})
    assert rm.lease_rows(c.host)[0]["live"] is True
    with pytest.raises(PaidSelectionError, match="no live runpod lease"):
        c.machine.claim(job)
    assert c.creates == 1 and c.deletes == []


@pytest.mark.parametrize("mismatch", [False, True])
def test_warm_gpu_id_uses_confirmed_display_and_refuses_mismatch(machine_case, monkeypatch, mismatch):
    from dataclasses import replace
    from astrid.packs.runpod import worker_preparation
    c = machine_case
    c.prefs = replace(c.prefs, gpu_types=("NVIDIA GeForce RTX 5090",))
    c.owner.preferences = c.prefs
    c.accepted_gpu_id = "NVIDIA GeForce RTX 5090"
    old_observe = c.owner.observe_offer
    c.owner.observe_offer = lambda: replace(old_observe(), gpu_type="NVIDIA GeForce RTX 5090")
    c.grant = replace(c.grant, offer_digest=c.owner.observe_offer().digest(c.prefs))
    confirmations = []
    def confirm(api_key, contract, *, timeout_seconds):
        assert contract.gpu_type == "NVIDIA GeForce RTX 5090"
        confirmations.append(contract)
        if mismatch and len(confirmations) == 4:
            raise worker_preparation.TargetObservationError("GPU mismatch", gate="GPU")
        return "RTX 5090", contract.datacenter_id
    monkeypatch.setattr(worker_preparation, "confirm_acquired_machine", confirm)
    _claim_machine(c)
    first = c.job()
    first = replace(first, policy={**first.policy, "preferences": c.prefs.as_mapping(), "offer_digest": c.grant.offer_digest})
    run_on_machine(c.machine, first, destination=first.scratch / "outputs", cancelled=lambda: False,
                   sleep=lambda s: None)
    second = c.job(2)
    second = replace(second, policy={**second.policy, "preferences": c.prefs.as_mapping(), "offer_digest": c.grant.offer_digest})
    if mismatch:
        with pytest.raises(Exception, match="custody could not be verified"):
            run_on_machine(c.machine, second, destination=second.scratch / "outputs", cancelled=lambda: False,
                           sleep=lambda s: None)
        assert c.read()["state"] == "cleanup_complete"
        assert sum(" generate cpu-image " in line for line in c.pod.job_log) == 1
        assert c.deletes == ["pod-1"]
    else:
        run_on_machine(c.machine, second, destination=second.scratch / "outputs", cancelled=lambda: False,
                       sleep=lambda s: None)
        assert c.read()["lease"]["preparation"]["custody"]["gpu_type"] == "RTX 5090"
        assert c.creates == 1 and c.read()["lease"]["state"] == "idle"


@pytest.mark.parametrize("paid_remaining", [None, 20])
def test_execution_gets_its_own_deadline_after_preparation(machine_case, monkeypatch, paid_remaining):
    c = machine_case
    _claim_machine(c)
    ticks = [0.0]
    c.placement._clock = lambda: ticks[0]
    original = c.placement.prepare_claimed

    def prepare(*args, **kwargs):
        state = original(*args, **kwargs)
        ticks[0] = 55.0
        if paid_remaining is not None:
            c.clock.now = c.read()["lease"]["paid_expires_at"] - paid_remaining
        return state

    monkeypatch.setattr(c.placement, "prepare_claimed", prepare)
    job = c.job()
    lease = c.machine.claim(job)
    ticks[0] = 65.0  # preparation bound passed; execution still has 50s
    run = c.machine.start(lease, job)
    assert c.machine.poll(run)["state"] == "exited"
    c.machine.fetch(run, destination=job.scratch / "outputs")
    deadline = 115.0 if paid_remaining is None else 75.0
    assert c.placement._deadline_at == deadline
    with pytest.raises(rm.RemotePlacementError, match="already established"):
        c.placement.begin_execution(deadline_seconds=60)
    assert c.placement._deadline_at == deadline
    ticks[0] = deadline
    with pytest.raises(rm.RemotePlacementError, match="execution deadline"):
        c.machine.poll(run)


def test_execution_timeout_stops_job_and_keeps_verified_machine(machine_case, monkeypatch):
    c = machine_case
    _claim_machine(c)
    ticks = [0.0]
    c.placement._clock = lambda: ticks[0]
    original = c.machine.poll

    def expired(run):
        ticks[0] = c.placement._deadline_at
        return original(run)

    monkeypatch.setattr(c.machine, "poll", expired)
    job = c.job()
    with pytest.raises(rm.RemotePlacementError, match="execution deadline"):
        run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: False,
                       sleep=lambda s: None)
    record = c.read()
    assert c.pod.cancelled and record["lease"]["job"]["state"] == "exited"
    assert record["state"] == "claimed" and record["lease"]["state"] == "idle"
    assert record["lease"]["last_job"]["outcome"] == "failed"
    assert c.deletes == [] and c.creates == 1


@pytest.mark.parametrize("entry,mode,template", [
    ("qwen-image-2.1-turbo", "t2i", "image/qwen_image_2_1_turbo"),
    ("qwen-image-2.1-turbo-edit", "edit", "edit/qwen_image_2_1_turbo_edit"),
])
def test_catalog_job_capacity_uses_selected_local_workflow(entry, mode, template, monkeypatch):
    from astrid.core.generation import entries
    from astrid.packs.runpod.remote_placement import RunPodInstallation

    assert entries.find_entry(entry) is None
    expected = tuple(entries.requirements(entries.find_entry(template), {}).get("models", ()))
    assert len(expected) == 3 and all(row["size"] > 0 for row in expected)
    original_find = entries.find_entry
    monkeypatch.setattr(entries, "find_entry", lambda name: {"requirements": {
        "engine": "comfyui", "base": None, "models": [], "extra": {}}}
        if name == template else original_find(name))
    rows = rm._job_manifest_rows(entry, {}, mode=mode)
    assert rows == expected
    assert RunPodInstallation.model_bytes(None, rows) == sum(row["size"] for row in expected)


@pytest.mark.parametrize("entry,mode", [
    ("unknown-catalog-entry", "t2i"),
    ("qwen-image-2.1-turbo", "edit"),
])
def test_unresolved_catalog_job_capacity_fails_closed(entry, mode):
    with pytest.raises((KeyError, ValueError, rm.MachineError)):
        rm._job_manifest_rows(entry, {}, mode=mode)


@pytest.mark.parametrize("failure", ["no-local", "missing-template", "ambiguous-mode"])
def test_catalog_capacity_requires_available_selected_workflow(monkeypatch, failure):
    from types import SimpleNamespace
    from astrid.core.generation import entries
    from astrid.core.model_catalog.registry import ModelRegistry

    local = SimpleNamespace(template="missing/workflow", template_hash="sha256:" + "0" * 64)
    selected = SimpleNamespace(backends={} if failure == "no-local" else {"local": local})
    model = SimpleNamespace(modes={"t2i": selected, "edit": selected})
    monkeypatch.setattr(ModelRegistry, "load_default", lambda: SimpleNamespace(get=lambda entry: model))
    monkeypatch.setattr(entries, "find_entry", lambda entry: None)
    with pytest.raises((rm.MachineError, ValueError, KeyError)):
        rm._job_manifest_rows("catalog-model", {}, mode=None if failure == "ambiguous-mode" else "t2i")


def test_catalog_capacity_rejects_changed_template_pin(monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from astrid.core.model_catalog.registry import ModelRegistry

    from astrid.core.generation import entries

    model = ModelRegistry.load_default().get("qwen-image-2.1-turbo")
    monkeypatch.setattr(entries, "find_entry", lambda entry: None)
    selected = model.modes["t2i"]
    changed = replace(selected.backends["local"], template_hash="sha256:" + "0" * 64)
    model = SimpleNamespace(modes={"t2i": SimpleNamespace(backends={"local": changed})})
    monkeypatch.setattr(ModelRegistry, "load_default", lambda: SimpleNamespace(get=lambda entry: model))
    with pytest.raises(ValueError, match="hash|pin|digest"):
        rm._job_manifest_rows("qwen-image-2.1-turbo", {}, mode="t2i")


@pytest.mark.parametrize("entry", [None, "workflow"])
def test_workflow_file_capacity_reads_shipped_bundle(machine_case, entry):
    from vibecomfy.workflow_bundle import emit_bundle
    from astrid.core.generation import entries

    job = machine_case.job()
    workflow = entries._load_entry_workflow(entries.find_entry("image/qwen_image_2_1_turbo"))
    expected = tuple(entries.requirements({"workflow": workflow}, {}).get("models", ()))
    emit_bundle(workflow, job.inputs["python"], {"operation": "authored"})
    rows = rm._job_manifest_rows(entry, {}, execution=job.inputs)
    assert rows == expected and len(rows) == 3


@pytest.mark.parametrize("execution", [{}, {"python": "/missing/workflow.py"}, {"workflow": "/missing/workflow.json"}])
def test_workflow_file_capacity_rejects_missing_bundle(execution):
    with pytest.raises(ValueError):
        rm._job_manifest_rows(None, {}, execution=execution)


def test_claim_reads_workflow_bundle_before_preparing(machine_case, monkeypatch):
    from dataclasses import replace
    from astrid.packs.runpod import generation as runpod_generation

    _claim_machine(machine_case)
    job = replace(machine_case.job(), entry=None)
    captured = {}

    def capture(*args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop before create")

    monkeypatch.setattr(runpod_generation, "prepare_retained_generation", capture)
    with pytest.raises(RuntimeError, match="stop before create"):
        machine_case.machine.claim(job)
    assert tuple(captured["manifest_rows"]) == ()  # a valid model-free workflow
    with pytest.raises(ValueError):
        machine_case.machine.claim(replace(job, inputs={"workflow_inputs": "{}"}))


def _claim_cli_case(monkeypatch):
    from types import SimpleNamespace
    from astrid.core import machines_cli
    from tests.packs.runpod.test_pack_actions import raw_preferences

    preferences = raw_preferences()
    monkeypatch.setattr(machines_cli, "_runtime_storage_root", lambda: Path(__file__).parent)
    rows, calls = [], []
    monkeypatch.setattr(machines_cli, "_leases", lambda: rows)

    def describe(**kwargs):
        calls.append(("describe", kwargs))
        selected = {**preferences, "gpu_types": kwargs["placement"]["policy"]["gpu_types"]} if isinstance(kwargs["placement"], dict) else preferences
        if kwargs.get("datacenter"):
            selected = {**selected, "datacenter_id": kwargs["datacenter"]}
        return {"waiting_policy": {"preferences": selected}, "offer": {"hourly_usd": 1.5}}

    def claim(**kwargs):
        calls.append(("claim", kwargs))
        rows.append({"id": "manual-pod", "state": "idle", "live": True, "release_at": 1800000600.0})
        return SimpleNamespace(ok=True, raw_result={"result": {"payload": {"target_operation": {"pod_id": "manual-pod"}}}})

    return machines_cli, preferences, rows, calls, describe, claim


def test_machine_claim_real_parser_discloses_gpu_storage_and_fresh_policy_consent(monkeypatch, capsys):
    cli, saved, rows, calls, describe, claim = _claim_cli_case(monkeypatch)
    assert cli.main(["claim", "--gpu", "gpu-b", "--datacenter", "DC-OVERRIDE", "--max-usd", "2", "--max-hourly-usd", "2",
                     "--for", "20m", "--capacity-timeout", "10m", "--consent", "paid", "--json"],
                    claim=claim, describe=describe) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] and payload["machine"]["release_at"] == rows[0]["release_at"]
    assert payload["policy"]["preferences"]["datacenter_id"] == "DC-OVERRIDE"
    assert "datacenter_id" not in saved
    grant = calls[-1][1]["consent"]
    assert grant.consent == ("paid",) and grant.offer_digest is None and grant.deadline is None
    assert grant.policy_digest and grant.max_hourly_usd == 2 and grant.idle_seconds == 1200
    assert grant.capacity_timeout_seconds == 600 and grant.wait_for_capacity is True
    assert calls[-1][1]["placement"] == {"kind": "runpod", "policy": {"gpu_types": ["gpu-b"]}}
    assert saved["gpu_types"] == ["gpu-a", "gpu-b"] and saved["volume"]["id"] == "volume-a"


def test_machine_claim_prompt_and_default_retention_and_one_attempt(monkeypatch, capsys):
    cli, saved, rows, calls, describe, claim = _claim_cli_case(monkeypatch)
    prompts = []
    assert cli.main(["claim", "--max-usd", "2", "--no-wait-for-capacity"],
                    claim=claim, describe=describe,
                    input_fn=lambda prompt: (prompts.append(prompt), "yes")[1]) == 0
    grant = calls[-1][1]["consent"]
    assert prompts and grant.max_hourly_usd == 1.5
    assert grant.idle_seconds == saved["idle_timeout_seconds"] == 120
    assert grant.wait_for_capacity is False
    assert "claimed manual-pod" in capsys.readouterr().out


def test_machine_claim_refuses_missing_paid_consent_or_stockless_missing_ceiling(monkeypatch, capsys):
    cli, saved, rows, calls, describe, claim = _claim_cli_case(monkeypatch)
    assert cli.main(["claim", "--max-usd", "2", "--json"], claim=claim, describe=describe) == 2
    assert "--consent paid" in capsys.readouterr().out
    assert not rows and all(name != "claim" for name, _ in calls)
    stockless = lambda **kwargs: {"waiting_policy": {"preferences": saved}}
    assert cli.main(["claim", "--max-usd", "2", "--consent", "paid", "--json"], claim=claim, describe=stockless) == 2
    assert "--max-hourly-usd" in capsys.readouterr().out and not rows


def test_machine_claim_stockless_policy_and_cap_shortens_retention(monkeypatch, capsys):
    cli, saved, rows, calls, describe, claim = _claim_cli_case(monkeypatch)
    stockless = lambda **kwargs: {"waiting_policy": {"preferences": saved}, "availability": {"reason": "stock_unavailable"}}
    assert cli.main(["claim", "--max-usd", "0.1", "--max-hourly-usd", "2", "--for", "2h",
                     "--consent", "paid", "--json"], claim=claim, describe=stockless) == 0
    grant = calls[-1][1]["consent"]
    assert grant.idle_seconds == 180 and grant.max_usd == 0.1
    assert json.loads(capsys.readouterr().out)["policy"]["retention_seconds"] == 180


def test_machine_claim_without_host_refuses_without_bootstrapping(monkeypatch, capsys):
    from astrid.core import machines_cli
    from astrid.sdk import AstridClient
    calls = []

    def unavailable(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("no running host")

    monkeypatch.setattr(AstridClient, "open_from_launcher", unavailable)
    assert machines_cli.main(["claim", "--max-usd", "2", "--max-hourly-usd", "2", "--consent", "paid", "--json"]) == 1
    assert calls == [{"start_pack_host": False}]
    assert "no running host" in capsys.readouterr().out
@pytest.mark.parametrize("tamper", [None, "receipt", "report"])
def test_two_jobs_retain_verified_managed_launch_refresh(machine_case, monkeypatch, tamper):
    """An untouched controller reuses the receipt legitimately refreshed by job one."""
    import hashlib
    from astrid.core.generation import engine_runner
    c = machine_case
    _claim_machine(c)
    original = c.pod._generate
    refreshed = []

    def generate(command):
        key = c.installation.root + '/.astrid-engine/install.json'
        raw = c.context.files[key]
        before = c.pod.files[key]
        # As production does: the installed plan comes from the receipt job one read.
        from astrid.core.generation import engine_recipe
        recipe = engine_recipe.load_recipe(c.installation.engine, c.installation.platform)
        _receipt, plan = engine_runner._plan_receipt(raw, recipe, root=c.installation.root, placement="local")
        runner = engine_runner._Runner(plan, c.context, c.context.adapter, c.context.deadline)
        with c.context.acquire_install_lock(c.installation.root, deadline=c.context.deadline):
            action, facts = runner.start()
            health_action, health_facts = runner.check_health()
            digest = engine_runner.refresh_installed_launch(runner, raw, start_action=action,
                start_facts=facts, health_action=health_action, health_facts=health_facts, check=lambda: None)
        c.pod.files[key] = engine_runner._json(c.context.files[key]).encode()
        assert c.pod.files[key] != before
        refreshed.append(digest)
        output = original(command)
        root = c.pod.active_job['run_dir']
        job = json.loads(c.pod.files[root + '/job.json'])
        report = {'schema': 'astrid.managed-engine-preparation.v1',
                  'task_id': job['task_identity'], 'attempt_id': job['attempt_identity'],
                  'installation': {'root': c.installation.root, 'receipt_sha256': digest,
                                   'recipe_sha256': c.installation.plan().recipe_sha256}}
        if tamper == 'report':
            report['attempt_id'] = 'foreign-attempt'
        if tamper == 'receipt':
            c.context.files[key]['health']['checked_at'] = '2026-01-01T00:00:00Z'
            c.pod.files[key] = engine_runner._json(c.context.files[key]).encode()
        payload = {'task_id': job['task_identity'], 'attempt_id': job['attempt_identity'],
                   'evidence': {'producer': {'attempt': {'record': {'managed_engine_preparation': report}}}}}
        path = root + '/out/manifest.json'
        c.pod.files[path] = json.dumps({'managed_generation_result': payload}).encode()
        manifest_path = root + '/out/' + rm.MANIFEST_NAME
        manifest = json.loads(c.pod.files[manifest_path])
        manifest['files']['manifest.json'] = {'sha256': hashlib.sha256(c.pod.files[path]).hexdigest(),
                                            'size': len(c.pod.files[path])}
        c.pod.files[manifest_path] = json.dumps(manifest).encode()
        output = (output.split(rm.MANIFEST_MARK)[0] + rm.MANIFEST_MARK
                  + hashlib.sha256(c.pod.files[manifest_path]).hexdigest() + '\n')
        return output

    monkeypatch.setattr(c.pod, '_generate', generate)
    if tamper:
        job = c.job()
        with pytest.raises(rm.MachineError, match=('differs from completed-job attestation' if tamper == 'receipt'
                                                    else 'no matching completed-job attestation')):
            run_on_machine(c.machine, job, destination=job.scratch / 'outputs', cancelled=lambda: False,
                           sleep=lambda s: None)
        assert c.read()['state'] == 'cleanup_complete' and c.deletes == ['pod-1']
        return
    for n in (1, 2):
        job = c.job(n)
        run_on_machine(c.machine, job, destination=job.scratch / 'outputs', cancelled=lambda: False,
                       sleep=lambda s: None)
        record = c.read()
        assert record['lease']['state'] == 'idle'
        assert record['lease']['preparation']['installed']['receipt_sha256'] == 'sha256:' + refreshed[-1]
    assert len(refreshed) == 2 and c.creates == 1 and not c.deletes
    c.host.release()


def test_machines_setup_failure_names_safe_action_details_never_its_message(monkeypatch, capsys):
    import json as _json
    from types import MappingProxyType

    from astrid.core import machines_cli
    from astrid.core.generation import settings

    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: {
        "credential_ref": "env:RUNPOD_API_KEY",
    })
    refused = type("Result", (), {"ok": False, "error": MappingProxyType({
        "code": "conflict",
        "message": "provider-secret-body",
        "details": {"error_code": "binding_conflict", "field": "capability",
                    "reason": "free text with provider-secret-body", "secret": {"nested": "x"}},
    })})()
    assert machines_cli.main(
        ["setup", "--gpu", "RTX A", "--budget-usd", "7", "--json"],
        volumes=lambda: [], offered_gpus=lambda *_: ["RTX A"], setup=lambda policy: refused,
    ) == 1
    out = capsys.readouterr().out
    error = _json.loads(out)["error"]
    assert error["code"] == "conflict"
    assert error["details"] == {"error_code": "binding_conflict", "field": "capability"}
    assert "error_code=binding_conflict" in error["message"]
    assert "provider-secret-body" not in out and "nested" not in out


def test_machine_claim_waits_for_stock_no_longer_than_its_retention(monkeypatch, capsys):
    cli, saved, rows, calls, describe, claim = _claim_cli_case(monkeypatch)
    assert cli.main(["claim", "--max-usd", "2", "--max-hourly-usd", "2", "--for", "20m",
                     "--consent", "paid", "--json"], claim=claim, describe=describe) == 0
    grant = calls[-1][1]["consent"]
    assert grant.wait_for_capacity is True and grant.capacity_timeout_seconds == grant.idle_seconds == 1200
    capsys.readouterr()

    def timed_out(**kwargs):
        calls.append(("claim", kwargs))
        raise RuntimeError("target status task failed: RunPod capacity timeout passed; no further create will be attempted")

    assert cli.main(["claim", "--max-usd", "2", "--max-hourly-usd", "2", "--for", "20m",
                     "--consent", "paid", "--json"], claim=timed_out, describe=describe) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["unknown"] is False
    assert payload["error"].startswith("no machine came into stock within 20m; nothing was rented.")
    assert "--capacity-timeout" in payload["error"]


def test_release_during_running_job_cancels_then_releases(machine_case, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from astrid.packs.runpod.adapter import TargetProviderBindings
    from astrid.packs.runpod.worker_owner import RunPodWorkerOwnerError

    c = machine_case
    _claim_machine(c)
    cancel = threading.Event()
    polling = threading.Event()
    checkpointed = threading.Event()
    settlement_waiting = threading.Event()
    allow_settlement = threading.Event()
    settled = threading.Event()
    drain_waiting = threading.Event()
    quiesced = threading.Event()
    bindings = TargetProviderBindings(pack_roots=(), holds=None, credentials=None, environ={})
    bindings.provider = c.adapter
    running = set()
    order = []

    class ProducerDone(threading.Event):
        def wait(self, timeout=None):
            drain_waiting.set()
            return super().wait(timeout)

    def begin(lease):
        bindings.begin(lease, cancel)
        bindings.active["done"] = ProducerDone()

    c.machine = rm.MACHINE.for_host(c.adapter, begin=begin, done=bindings.done)

    def cancel_running(record):
        bindings.cancel_running(record)
        assert cancel.is_set()
        assert checkpointed.wait(timeout=10), "producer did not checkpoint the exact cancellation"
        assert settlement_waiting.wait(timeout=10), "producer did not reach settlement"

    c.host.cancel_running = cancel_running
    receipt_builder = c.host.quiesce

    async def drain(record):
        assert checkpointed.is_set() and settlement_waiting.is_set()
        assert not bindings.active["done"].is_set()
        bindings._quiesce(record)
        quiesced.set()
        order.append("quiesced")
        return await receipt_builder(record)

    c.host.quiesce = drain
    # This fixture's model-free job does not need repository catalog scanning.
    monkeypatch.setattr(rm, "_job_manifest_rows", lambda *args, **kwargs: ())
    original_start = c.pod.job_start
    original_poll = c.pod.job_poll
    original_cancel = c.pod.job_cancel
    original_checkpoint = c.machine._checkpoint
    original_settlement = c.machine.release

    def start(command, *, job_id):
        result = original_start(command, job_id=job_id)
        if " generate " in command:
            running.add(job_id)
        return result

    def poll(job_id):
        if job_id in running and not cancel.is_set():
            return {"id": job_id, "state": "running"}
        return original_poll(job_id)

    def stop(job_id):
        assert cancel.is_set(), "exact remote stop preceded the running attempt's cancel signal"
        order.append("stop")
        return original_cancel(job_id)

    def checkpoint(lease, job):
        original_checkpoint(lease, job)
        if job["id"] in running and job["state"] == "cancelled":
            assert c.read()["lease"]["job"] == job
            order.append("checkpointed")
            checkpointed.set()

    def settlement(lease, warm, *, reason="task_failed", failure=None, stage="execution"):
        assert checkpointed.is_set()
        assert not warm and isinstance(failure, MachineRunCancelled) and stage == "cancel"
        settlement_waiting.set()
        assert allow_settlement.wait(timeout=10), "settlement barrier was not released"
        original_settlement(lease, warm, reason=reason, failure=failure, stage=stage)
        settled.set()

    monkeypatch.setattr(c.pod, "job_start", start)
    monkeypatch.setattr(c.pod, "job_poll", poll)
    monkeypatch.setattr(c.pod, "job_cancel", stop)
    monkeypatch.setattr(c.machine, "_checkpoint", checkpoint)
    monkeypatch.setattr(c.machine, "release", settlement)

    def wait_for_cancel(_seconds):
        polling.set()
        assert cancel.wait(timeout=30), "release did not signal the running attempt"

    job = c.job()
    with ThreadPoolExecutor(max_workers=2) as executor:
        worker = executor.submit(run_on_machine, c.machine, job, destination=job.scratch / "outputs",
                                 cancelled=cancel.is_set, sleep=wait_for_cancel)
        try:
            assert polling.wait(timeout=30), "job did not reach polling"
            before = c.read()
            assert len(running) == 1
            job_id = next(iter(running))
            with pytest.raises(RunPodWorkerOwnerError, match="differs from the requested machine"):
                c.host.release(expected_machine_id="different-pod")
            assert not cancel.is_set() and not c.deletes
            release = executor.submit(c.host.release, expected_machine_id="pod-1")
            assert drain_waiting.wait(timeout=10), "production quiescence did not wait for the producer"
            assert checkpointed.is_set() and settlement_waiting.is_set()
            assert not quiesced.is_set() and not release.done() and not settled.is_set()
            pending = c.read()
            assert pending["state"] == "release_pending" and pending["lease"]["state"] == "releasing"
            assert pending["lease"]["job"] == {"id": job_id, "state": "cancelled"}
            assert "release_quiescence" not in pending and c.deletes == []
            allow_settlement.set()
            with pytest.raises(MachineRunCancelled, match="generation cancelled; the remote job was stopped"):
                worker.result(timeout=15)
            result = release.result(timeout=15)
            assert settled.is_set() and quiesced.is_set() and bindings.active is None
            assert order[:3] == ["stop", "checkpointed", "quiesced"]
            saved = c.read()
            assert saved == result and saved["state"] == "cleanup_complete"
            assert saved["lease"]["job"] == {"id": job_id, "state": "cancelled"}
            receipt = saved["release_quiescence"]
            assert receipt == {
                "schema": "astrid.runpod.lease-drain.v1", "state": "drained", "admission_closed": True,
                **{key: before[key] for key in ("operation_id", "pod_id", "provider_account_ref")},
                **{key: before["lease"][key] for key in
                   ("owner_epoch", "generation", "invocation_id")},
            }
            assert saved["cleanup"]["absence"]["exact_get_status"] == 404
            assert saved["cleanup"]["absence"]["account_list_succeeded"] is True
            assert saved["cleanup"]["absence"]["account_list_absent"] is True
            assert c.deletes == ["pod-1"]
        finally:
            cancel.set()
            allow_settlement.set()


@pytest.mark.parametrize("headroom", [-1, 64 * 1024**2 - 1])
def test_machine_claim_refuses_runtime_volume_below_release_reserve(tmp_path, monkeypatch, capsys, headroom):
    from types import SimpleNamespace
    from astrid.core import machines_cli
    from astrid.runtime_cli import RuntimeCLI
    from tests.packs.runpod.test_pack_actions import raw_preferences

    support = tmp_path / "support"
    realm = tmp_path / "different-volume" / "realm"
    realm.mkdir(parents=True)
    monkeypatch.setattr("astrid.sdk.storage_root.resolve_runtime_data_root", lambda: support)
    monkeypatch.setattr(RuntimeCLI, "inspect", lambda *args, **kwargs:
                        SimpleNamespace(ok=True, data={"realm_root": str(realm)}))
    monkeypatch.setenv("ASTRID_RUNTIME_FREE_DISK_FLOOR_BYTES", str(1024**3))
    measured, paid_calls = [], []

    def disk_usage(path):
        measured.append(path)
        return SimpleNamespace(free=1024**3 + headroom)

    monkeypatch.setattr("shutil.disk_usage", disk_usage)

    def describe(**kwargs):
        paid_calls.append("quote")
        return {"waiting_policy": {"preferences": raw_preferences()}}

    def claim(**kwargs):
        paid_calls.append("create")
        return SimpleNamespace(ok=False, error="fake create reached")

    assert machines_cli.main(["claim", "--max-usd", "2", "--max-hourly-usd", "2",
                              "--consent", "paid", "--json"], claim=claim, describe=describe) == 2
    message = json.loads(capsys.readouterr().out)["error"]
    assert "free" in message and "Runtime" in message and "release" in message
    assert "Free disk space" in message
    assert measured == [realm] and paid_calls == []


@pytest.mark.parametrize("refusal_stage", ["admission", "claim", "follow", "admission_exception"])
def test_machine_release_falls_back_only_for_runtime_low_disk(machine_case, monkeypatch, capsys, refusal_stage):
    from types import SimpleNamespace
    from astrid.core import machines_cli
    from astrid.packs.runpod import target_selection

    c = machine_case
    _claim_machine(c)
    before = c.read()
    monkeypatch.setattr("shutil.disk_usage", lambda _: SimpleNamespace(free=99_332_096))
    monkeypatch.setattr(machines_cli, "_leases", lambda: rm.lease_rows(c.host))
    monkeypatch.setattr(target_selection, "read_runpod_preferences", lambda **kwargs: c.prefs)
    monkeypatch.setattr(target_selection, "canonical_claim_path", lambda *args, **kwargs: c.host.selection.path)
    monkeypatch.setattr("astrid.core.execution.machine_run.resolve_machine", lambda kind: rm.MACHINE)
    error = {"code": "conflict", "details": {"reason": "insufficient_storage"}}
    task = {"task_id": "release-task", "run_id": "release-run", "capability_id": "runpod.release",
            "state": "queued", "waiting_reason": "insufficient_storage"}
    result = SimpleNamespace(ok=False, capability_id="runpod.release", error=error,
                             kernel_attempt_id=None, raw_result={})
    if refusal_stage == "claim":
        # A Runtime claim refusal has no attempt and leaves the task queued.
        result = SimpleNamespace(ok=True, capability_id="runpod.release", error=None,
                                 kernel_attempt_id=None, raw_result={"task": task})
    release = lambda: result
    cancelled_tasks = []
    if refusal_stage in {"follow", "admission_exception"}:
        from contextlib import nullcontext
        from astrid.sdk import AstridClient
        from astrid.sdk.generation import GenerationFacade
        from astrid.sdk.workspace_client import WorkspaceClientError

        def admit(*args, **kwargs):
            assert kwargs["machine_id"] == "pod-1"
            if refusal_stage == "admission_exception":
                raise WorkspaceClientError(409, "conflict", "Runtime storage preflight refused release",
                                           details={"reason": "insufficient_storage"})
            return SimpleNamespace(ok=True, capability_id="runpod.release", capability_type="executor",
                                   native_kind="action", kernel_task_id="release-task", kernel_run_id="release-run",
                                   kernel_attempt_id=None, error=None, raw_result={}, run_id="release-run")

        def cancel_task(task_id, **kwargs):
            assert c.deletes == ["pod-1"] and c.read()["state"] == "cleanup_complete"
            cancelled_tasks.append(task_id)
            return SimpleNamespace(ok=True)

        # Exercise the unchanged SDK follower: its first task read observes a
        # refused claim, so it must hand off rather than wait for a terminal task.
        client = SimpleNamespace(tasks=SimpleNamespace(show=lambda _: SimpleNamespace(ok=True, data=task),
                                                      cancel=cancel_task))
        monkeypatch.setattr(AstridClient, "open_from_launcher", lambda **kwargs: nullcontext(client))
        monkeypatch.setattr(GenerationFacade, "release", admit)
        release = None
    assert machines_cli.main(["release", "pod-1", "--json"], release=release) == 0
    assert cancelled_tasks == (["release-task"] if refusal_stage == "follow" else [])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] and payload["machine"]["state"] == "ended"
    assert "Runtime refused" in captured.err and "low disk" in captured.err
    assert "released pod pod-1 directly" in captured.err and str(c.host.selection.path) in captured.err
    saved = c.read()
    assert saved["state"] == "cleanup_complete" and c.deletes == ["pod-1"] and c.creates == 1
    assert saved["operation_id"] == before["operation_id"] and saved["pod_id"] == "pod-1"
    assert saved["release_quiescence"]["state"] == "drained"
    assert saved["cleanup"]["absence"]["exact_get_status"] == 404
    assert saved["cleanup"]["absence"]["account_list_absent"] is True


@pytest.mark.parametrize("error", [
    {"code": "conflict", "details": {"reason": "executor_busy"}},
    {"code": "permission_denied", "details": {"reason": "insufficient_storage"}},
    {"code": "transport_timeout", "message": "insufficient_storage"},
])
def test_machine_release_other_runtime_refusals_do_not_fall_back(machine_case, monkeypatch, capsys, error):
    from types import SimpleNamespace
    from astrid.core import machines_cli

    c = machine_case
    _claim_machine(c)
    monkeypatch.setattr(machines_cli, "_leases", lambda: rm.lease_rows(c.host))
    monkeypatch.setattr("astrid.core.execution.machine_run.resolve_machine",
                        lambda *_: pytest.fail("non-storage refusal entered direct release"))
    result = SimpleNamespace(ok=False, capability_id="runpod.release", error=error,
                             kernel_attempt_id=None, raw_result={})
    assert machines_cli.main(["release", "pod-1", "--json"], release=lambda: result) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]
    assert c.deletes == [] and c.read()["state"] == "claimed"


@pytest.mark.parametrize("expiry", ["idle", "paid_deadline"])
def test_automatic_machine_release_runs_below_runtime_floor(machine_case, monkeypatch, expiry):
    from types import SimpleNamespace
    from astrid.sdk import generation

    c = machine_case
    _claim_machine(c)
    if expiry == "paid_deadline":
        c.host.acquire(c.grant)
    record = c.read()
    deadline = record["lease"]["idle_expires_at" if expiry == "idle" else "paid_expires_at"]
    monkeypatch.setattr("shutil.disk_usage", lambda _: SimpleNamespace(free=99_332_096))
    monkeypatch.setattr(generation, "_runpod_operation",
                        lambda *args, **kwargs: pytest.fail("automatic release admitted a Runtime task"))
    c.clock.advance(deadline - c.clock())
    saved = c.read()
    assert saved["state"] == "cleanup_complete" and saved["lease"]["ended"] == expiry
    assert saved["operation_id"] == record["operation_id"] and saved["pod_id"] == "pod-1"
    assert saved["release_quiescence"]["state"] == "drained"
    assert saved["cleanup"]["absence"]["exact_get_status"] == 404
    assert c.deletes == ["pod-1"] and c.creates == 1
