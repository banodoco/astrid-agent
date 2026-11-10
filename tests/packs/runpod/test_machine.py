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
    polls = iter([False, False, True])
    with pytest.raises(MachineRunCancelled):
        run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: next(polls),
                       sleep=lambda s: None)
    c.pod.job_poll = original
    record = c.read()
    generation = next(line for line in c.pod.job_log if " generate " in line)
    assert generation and c.pod.cancelled, "the remote job was not cancelled"
    assert record["lease"]["job"] == {"id": c.pod.cancelled[0], "state": "cancelled"}
    assert record["state"] == "cleanup_complete" and c.deletes == ["pod-1"]


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
