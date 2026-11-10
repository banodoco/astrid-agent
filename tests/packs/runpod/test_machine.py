"""Plan 32 T5: RunPod as a machine on the real lease owner, with the pod and provider faked.

Money rules ported from the remote placement wrapper: one claim per job; any
failure after the claim releases the pod; only a successful job settles warm;
the detached job is checkpointed before start, so a crash is cancelled by
``recover()`` before the pod is deleted.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from astrid.core.execution.machine_run import MachineJob, MachineRunCancelled, run_on_machine
from astrid.packs.runpod import machine as rm
from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401  (fixture)
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401  (fixture)
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401  (fixture)
from tests.packs.runpod.test_warm_session import (  # noqa: F401  (fixtures)
    lease_selection_case as lease_selection_case, small_lease as small_lease,
)

BASE = dict(account="acct", gpu_type="NVIDIA RTX 5090", image="img@sha256:" + "1" * 64,
            volume={"id": "vol", "mount_path": "/workspace", "cache_device": 64768, "mount_id": 7},
            source_digest="0123456789abcdef")


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


def test_a_job_runs_ships_fetches_and_settles_warm(machine_case, tmp_path):
    c = machine_case
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
    original = c.job()
    job = replace(original, policy={**original.policy, 'placement_idle_seconds': requested, 'idle_seconds': bound})
    lease = c.machine.claim(job)
    try:
        assert c.read()['lease']['idle_seconds'] == expected
        assert c.machine.describe(lease)['idle_timeout_s'] == expected
    finally:
        c.machine.release(lease, False)


def test_a_failing_runtime_check_releases_the_lease_and_never_settles_warm(machine_case):
    c = machine_case
    c.pod.listed = []  # the pod's runtime does not list the entry from the shipped selection
    job = c.job()
    with pytest.raises(rm.MachineError, match="not listed by the pod's runtime"):
        run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: False,
                       sleep=lambda s: None)
    record = c.read()
    assert record["state"] == "cleanup_complete" and c.deletes == ["pod-1"]
    assert record["lease"].get("ended") == "task_failed"
    # The check is the job command's first step: the run itself never produced outputs.
    assert not any("/out/" in path for path in c.pod.files) and len(c.pod.resets) == 1


def test_a_failed_reset_releases_the_pod_instead_of_settling_it(machine_case):
    """T5f: the outputs were fetched, but a reset that fails never leaves a dirty warm pod."""
    c = machine_case
    c.pod.fail_jobs = {"--run-dir"}  # only the release-time reset carries the job's run directory
    job = c.job()
    fetched = run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: False,
                             sleep=lambda s: None)
    assert set(fetched["files"]) == {"artifacts/cpu.png", "manifest.json"}
    record = c.read()
    assert [r["ok"] for r in c.pod.resets] == [True, False]
    assert record["state"] == "cleanup_complete" and record["lease"]["ended"] == "reset_failed"
    assert record["lease"]["job"]["state"] == "exited" and c.deletes == ["pod-1"]


def test_a_pending_active_job_at_the_next_claim_redoes_the_reset(machine_case):
    c = machine_case
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


def test_cancel_stops_the_job_checkpoints_it_then_releases(machine_case):
    c = machine_case
    job = c.job()
    original = c.pod.job_poll
    def running(job_id):  # the generation job keeps running; preparation jobs finish
        if c.read()["lease"].get("job", {}).get("id") == job_id:
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
    assert record["lease"]["job"] == {"id": c.pod.cancelled[0], "state": "cancelled"}
    assert record["state"] == "cleanup_complete" and c.deletes == ["pod-1"]


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
    c.owner.monotonic = c.clock
    monkeypatch.setattr(target_selection.random, "uniform", lambda *args: 0)
    run_on_machine(c.machine, replace(job, policy=policy), destination=job.scratch / "outputs",
                   cancelled=lambda: False, sleep=lambda seconds: None)
    assert len(calls) == 3 and c.creates == 1 and c.read()["lease"]["state"] == "idle"


def test_a_crash_after_job_start_is_cancelled_by_recover_before_delete(machine_case):
    c = machine_case
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
    c.pod.corrupt_get = "cpu.png"
    job = c.job()
    with pytest.raises(rm.DigestMismatch):
        run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: False,
                       sleep=lambda s: None)
    assert not (job.scratch / "outputs" / "artifacts" / "cpu.png").exists()
    assert c.read()["state"] == "cleanup_complete" and c.deletes == ["pod-1"]


def test_an_unsafe_manifest_path_is_refused(machine_case, tmp_path):
    c = machine_case
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


def test_machines_setup_refuses_missing_pinned_identity(monkeypatch, capsys):
    from astrid.core import machines_cli
    from astrid.core.generation import settings

    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: {"gpu_types": ["RTX A"]})
    assert machines_cli.main(["setup", "--json"], volumes=lambda: [],
                             offered_gpus=lambda *_: []) == 2
    output = capsys.readouterr().out
    assert "provider_account_ref" in output and "image" in output
    assert "astrid machines setup" in output
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
        if mismatch and len(confirmations) == 3:
            raise worker_preparation.TargetObservationError("GPU mismatch", gate="GPU")
        return "RTX 5090", contract.datacenter_id
    monkeypatch.setattr(worker_preparation, "confirm_acquired_machine", confirm)
    first = c.job()
    first = replace(first, policy={**first.policy, "preferences": c.prefs.as_mapping(), "offer_digest": c.grant.offer_digest})
    run_on_machine(c.machine, first, destination=first.scratch / "outputs", cancelled=lambda: False,
                   sleep=lambda s: None)
    second = c.job(2)
    second = replace(second, policy={**second.policy, "preferences": c.prefs.as_mapping(), "offer_digest": c.grant.offer_digest})
    if mismatch:
        with pytest.raises(Exception, match="GPU mismatch"):
            run_on_machine(c.machine, second, destination=second.scratch / "outputs", cancelled=lambda: False,
                           sleep=lambda s: None)
        assert c.read()["state"] == "cleanup_pending"
        assert sum(" generate cpu-image " in line for line in c.pod.job_log) == 1
        c.host.release()
        assert c.deletes == ["pod-1"]
    else:
        run_on_machine(c.machine, second, destination=second.scratch / "outputs", cancelled=lambda: False,
                       sleep=lambda s: None)
        assert c.read()["lease"]["preparation"]["custody"]["gpu_type"] == "RTX 5090"
        assert c.creates == 1 and c.read()["lease"]["state"] == "idle"


@pytest.mark.parametrize("paid_remaining", [None, 20])
def test_execution_gets_its_own_deadline_after_preparation(machine_case, monkeypatch, paid_remaining):
    c = machine_case
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


def test_execution_timeout_cancels_and_releases_without_another_claim(machine_case, monkeypatch):
    c = machine_case
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
    assert record["lease"]["job"] == {"id": c.pod.cancelled[0], "state": "cancelled"}
    assert record["state"] == "cleanup_complete"
    assert c.deletes == ["pod-1"] and c.creates == 1


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


@pytest.mark.parametrize("tamper", [None, "receipt", "report"])
def test_two_jobs_retain_verified_managed_launch_refresh(machine_case, monkeypatch, tamper):
    """An untouched controller reuses the receipt legitimately refreshed by job one."""
    import hashlib
    from astrid.core.generation import engine_runner
    c = machine_case
    original = c.pod._generate
    refreshed = []

    def generate(command):
        key = c.installation.root + '/.astrid-engine/install.json'
        raw = c.context.files[key]
        before = c.pod.files[key]
        runner = engine_runner._Runner(c.installation.plan(), c.context, c.context.adapter,
                                       c.context.deadline)
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
