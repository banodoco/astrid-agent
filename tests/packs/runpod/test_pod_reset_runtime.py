"""Plan 32 T5f: the warm pod's one Runtime is reset in place, for real.

The pod-side ``pod_reset.reset`` runs against a real Runtime daemon: the first
reset takes the empty-realm template, a job then dirties the realm (a project,
its events, a CAS object), and the next reset restores the template beside the
realm and replaces it in the same daemon. Afterwards the realm holds zero
tasks, runs, events and objects, the CAS is empty, the old root is gone, and the
prepared connection opens in under a second (median of three).
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import time
from pathlib import Path

import pytest

from astrid.packs.runpod import pod_reset

#: The realm store's tables for tasks, runs, events and objects: a reset leaves each at zero.
EMPTY_TABLES = ("tasks", "runs", "events", "objects")


def _counts(realm: Path) -> dict[str, int]:
    """Row counts of the realm store's tables, read from a private copy of the database."""
    connection = sqlite3.connect(f"file:{realm / 'realm.sqlite3'}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
    finally:
        connection.close()


def _cas(realm: Path) -> list[Path]:
    root = realm / "cas" / "sha256"
    return [path for path in root.rglob("*") if path.is_file()] if root.exists() else []


def test_the_pod_reset_empties_the_one_runtime_in_place(real_runtime, tmp_path):
    daemon = real_runtime()
    realm = tmp_path / "realm"
    pod = tmp_path / "pod"
    paths = dict(template=pod / "realm-template", lock=pod / "reset.lock", prepared=pod / "prepared-connection.json",
                 ready=pod / "ready.json", active=pod / "active-job.json", data_root=pod / "runtime-data")
    ensures = []

    def ensure():
        # The launcher's ensure_runtime: the live daemon's connection (its endpoint moves on replace).
        ensures.append(daemon.endpoint)
        return {"endpoint": daemon.endpoint, "credential_file": str(daemon.credential_path),
                "realm_id": daemon.service.realm["id"], "actor_id": "owner"}

    # A fresh pod: the realm pod_bootstrap created is empty; the first reset takes the template.
    first = pod_reset.reset(ensure=ensure, realm=realm, **paths)
    assert "template" in first and paths["template"].is_dir()
    assert json.loads(paths["ready"].read_text())["realm_id"] == daemon.service.realm["id"]
    assert paths["prepared"].stat().st_mode & 0o777 == 0o600
    template_counts = _counts(realm)

    # A job: on the prepared connection it writes a project, a CAS object, and one
    # task a real pack host claims and settles (its run, attempt and events).
    from tests.packs.test_vibecomfy_runtime_real_service_integration import _new_host

    run_dir, job_dir = pod / "runs" / "r1", pod / "jobs" / "j1"
    paths["active"].write_text(json.dumps({"run_id": "r1", "task": "t", "attempt": "a",
                                           "run_dir": str(run_dir), "job_dir": str(job_dir)}))
    client = pod_reset.open_prepared(paths["prepared"])
    project = client.projects.create(name="Job", slug="job", idempotency_key="project")
    assert project.ok, project.error
    project_id = project.data["project_id"]
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"1": {"class_type": "LoadImage", "inputs": {"image": "in.png"}},
                                  "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0],
                                                                              "filename_prefix": "out/job"}}}))
    media = client.media.import_file(project=project_id, path=source, idempotency_key="media")
    assert media.ok, media.error
    host = _new_host(daemon, attempt_root=tmp_path / "attempt", executor_id="pod-job-host")
    task = client.tasks.create(
        project_id=project_id, capability="vibecomfy.import",
        spec={"inputs": {"workflow_id": "job"}, "input_digests": [{"name": "source", "digest": media.data["digest"]}],
              "transition_kind": "origin", "parent_task_id": None, "origin_task_id": None},
        input_manifest=[media.data["object_id"]], idempotency_key="task")
    assert task.ok, task.error
    settled = host.run(once=True)
    assert [row.state for row in settled] == ["succeeded"]
    host.shutdown()
    client.__exit__(None, None, None)
    dirty = _counts(realm)
    assert _cas(realm), "the job wrote no CAS object"
    assert all(dirty[table] >= 1 for table in EMPTY_TABLES), dirty
    attempt = paths["data_root"] / "host-support" / "host-output" / "attempts" / "attempt-1"
    log = paths["data_root"] / "runtime" / "logs" / "host.log"
    log.parent.mkdir(parents=True)
    log.write_text("job log\n")
    for folder in (run_dir / "out", job_dir, attempt):
        folder.mkdir(parents=True)
        (folder / "f").write_text("x")
    (job_dir / "command.sh").write_text("true\n")  # the pod agent's own job folder
    old_inode = realm.stat().st_ino

    # A dirty realm never becomes the template: not while a job is active, nor once it wrote rows.
    current = ensure()
    token = Path(current["credential_file"]).read_text().strip()
    with pytest.raises(pod_reset.ResetError, match="a job was active"):
        pod_reset.take_template(current["endpoint"], token, pod / "dirty-template", realm_root=realm,
                                active=paths["active"])
    with pytest.raises(pod_reset.ResetError, match="the realm is not empty"):
        pod_reset.take_template(current["endpoint"], token, pod / "dirty-template", realm_root=realm,
                                active=pod / "no-active-job.json")
    assert not (pod / "dirty-template").exists()

    # The claim's redo (no folders passed): the reset reads them from active-job.json.
    started = time.perf_counter()
    steps = pod_reset.reset(ensure=ensure, realm=realm, runs_root=pod / "runs", **paths)
    elapsed = time.perf_counter() - started
    assert steps["replace"]["state"] == "complete" and "template" not in steps

    # The same daemon and realm id; the old root moved aside and deleted.
    assert steps["replace"]["realm_id"] == daemon.service.realm["id"]
    assert realm.stat().st_ino != old_inode
    assert not list(realm.parent.glob(".realm.superseded-*")) and not list(realm.parent.glob(".realm.candidate-*"))
    superseded = steps["replace"].get("superseded_root")
    assert not superseded or not Path(superseded).exists()
    # Zero tasks, runs, events and objects; an empty CAS; the job's folders gone.
    after = _counts(realm)
    assert after == template_counts
    assert {table: after[table] for table in EMPTY_TABLES} == dict.fromkeys(EMPTY_TABLES, 0)
    assert after["projects"] == 0
    assert _cas(realm) == []
    assert not run_dir.exists() and not job_dir.exists() and not attempt.exists()
    assert log.exists() and log.read_text() == ""
    assert not paths["active"].exists() and paths["ready"].exists()

    # The next job's only way in: the prepared connection, under a second (median of three).
    timings = []
    for _ in range(3):
        began = time.perf_counter()
        opened = pod_reset.open_prepared(paths["prepared"])
        listed = opened.projects.list()
        timings.append(time.perf_counter() - began)
        assert listed.ok and not listed.data[0], listed.data
        opened.__exit__(None, None, None)
    assert statistics.median(timings) < 1.0, timings
    print(json.dumps({"reset_seconds": round(elapsed, 3), "prepared_open_seconds": [round(t, 3) for t in timings],
                      "dirty": {table: dirty[table] for table in (*EMPTY_TABLES, "projects")},
                      "after": {table: after[table] for table in (*EMPTY_TABLES, "projects")}}))


def test_a_missing_prepared_connection_fails_closed_without_up(tmp_path, monkeypatch):
    """The warm job never falls back to the launcher: no prepared file means the pod must reset."""
    from astrid.sdk.client import AstridClient
    from astrid.sdk.exceptions import ServiceUnavailableError

    monkeypatch.setenv(pod_reset.PREPARED_ENV, str(tmp_path / "absent.json"))
    monkeypatch.setattr("astrid.sdk.autobootstrap.ensure_runtime",
                        lambda **kw: pytest.fail("a warm job ran ensure_runtime (up/connect)"))
    with pytest.raises(ServiceUnavailableError) as raised:
        AstridClient.open_from_launcher()
    assert raised.value.details["reason"] == "prepared_connection_unavailable"


def test_pending_dirs_only_names_the_jobs_own_folders(tmp_path):
    runs = tmp_path / "runs"
    run, job, other = runs / "r1", tmp_path / "jobs" / "j1", tmp_path / "elsewhere"
    for folder in (run, job, other):
        folder.mkdir(parents=True)
    (job / "command.sh").write_text("true\n")
    active = tmp_path / "active-job.json"
    active.write_text(json.dumps({"run_dir": str(run), "job_dir": str(job)}))
    assert pod_reset.pending_dirs(active, runs_root=runs) == (run, job)
    active.write_text(json.dumps({"run_dir": str(other), "job_dir": str(other)}))
    assert pod_reset.pending_dirs(active, runs_root=runs) == (None, None)
    active.write_text(json.dumps({"run_dir": str(runs / ".." / "elsewhere"), "job_dir": "relative"}))
    assert pod_reset.pending_dirs(active, runs_root=runs) == (None, None)
    assert pod_reset.pending_dirs(tmp_path / "absent.json", runs_root=runs) == (None, None)
