"""C1 workspace observations, executed by the selected installed interpreter.

Mutations use Runtime APIs. The timeline document read is deliberately read-only:
the public timeline projection no longer serves the retired composition bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ACTOR = {"actor": "tst-c1-user"}


def seed(service, project: str, generation: str, task_id: str | None,
         create_project: bool = False) -> dict:
    """Write user data through the selected Runtime service API.

    The authentic release-0 journey owns its project seed too, rather than
    relying on the installed CLI's convenience project creation step.
    """
    if create_project:
        selected = service.create_project(
            {"slug": project, "name": "Release-0 user project " + generation,
             "metadata": {"seed": "release0-to-release1"}},
            idempotency_key="project-" + generation)
    else:
        selected = service.get_project(project)
    project_id = selected["id"]
    timeline = "tst-c1-" + generation
    service.create_timeline_document(project_id, {
        "timeline_id": timeline, "name": "User edit " + generation,
        "config": {"tracks": [], "clips": [], "metadata": {"user_note": generation}},
        "registry": {"assets": {}},
    }, idempotency_key=timeline)
    preferences = service.get_preferences("user", identity=ACTOR)
    service.update_preferences("user", "Keep user changes " + generation,
                               preferences["version"], "preferences-" + generation,
                               identity=ACTOR)
    payload = ("C1 object " + generation + "\n").encode()
    service.ingest_object(payload, media_type="text/plain", original_name=generation + ".txt",
                          idempotency_key="object-" + generation)
    result = {"project": project_id, "timeline": timeline,
              "object_digest": hashlib.sha256(payload).hexdigest()}
    if task_id is not None:
        result["task_id"] = task_id
    return result


def snapshot(service, keys: dict) -> dict:
    from runtime_protocol.store import SCHEMA_VERSION
    project = service.get_project(keys["project"])
    timeline = service.store.conn.execute(
        "SELECT name FROM timelines WHERE id=? AND project_id=?",
        (keys["timeline"], keys["project"])).fetchone()
    document = service.store.conn.execute(
        "SELECT content_json FROM project_documents WHERE id=? AND project_id=?",
        ("timeline:" + keys["timeline"], keys["project"])).fetchone()
    if timeline is None or document is None:
        raise ValueError("Seeded timeline content is missing")
    preferences = service.get_preferences("user", identity=ACTOR)
    _metadata, payload = service.object(keys["object_digest"])
    task = None
    if "task_id" in keys:
        task = service.store.get_task(keys["task_id"])["task"]
        if task["status"] != "completed":
            raise ValueError("Seeded task is no longer settled successfully")
    schema = service.store.conn.execute("SELECT version FROM runtime_schema WHERE id=1").fetchone()[0]
    result = {"project": {key: project[key] for key in ("id", "slug", "name")},
              "timeline": {"name": timeline["name"], "content": json.loads(document["content_json"])},
              "preferences": {key: preferences[key] for key in ("content", "version")},
              "cas_sha256": hashlib.sha256(payload).hexdigest(), "schema_version": schema,
              "target_schema_version": SCHEMA_VERSION}
    if task is not None:
        result["task"] = {key: task.get(key) for key in ("id", "status", "spec", "result")}
    return result


def busy(service) -> dict:
    """Claim a harmless test-only task through real lease APIs, without a worker."""
    store = service.store
    capability, executor, token = "tst.c1.busy", "tst-c1-executor", "tst-c1-lease"
    epoch = store._current_runtime_epoch()
    store.upsert_executor(executor, [capability], runtime_epoch=epoch)
    task = store.create_task(capability, {"note": "C1 controlled busy admission"})["task"]
    claimed = store._claim_task(task["id"], executor, token, runtime_epoch=epoch)["task"]
    if claimed["status"] != "running":
        raise ValueError("Test busy task was not claimed")
    store.heartbeat_task(task["id"], token, fence=claimed["lease_fence"], lease_seconds=7200)
    return {"task_id": task["id"], "lease_token": token,
            "fence": claimed["lease_fence"], "attempt_id": claimed["attempt_id"]}


def release(service, keys: dict) -> dict:
    service.store.fail_task(keys["task_id"], keys["lease_token"], "C1 test lease released",
                            fence=keys["fence"], attempt_id=keys["attempt_id"])
    return {"released": True}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seed", "snapshot", "busy", "release"))
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--realm", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    run_root, realm = args.run_root, args.realm
    if (not run_root.is_absolute() or run_root != run_root.resolve()
            or not (run_root / "tst-b1-owner.json").is_file()
            or not realm.is_absolute() or realm != realm.resolve()
            or not realm.is_relative_to(run_root)):
        raise ValueError("Workspace helper requires the B1 owned run and realm")
    from runtime_protocol.service import RuntimeService
    service = RuntimeService(realm)
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        if args.action == "seed":
            result = seed(service, **request)
        elif args.action == "snapshot":
            result = snapshot(service, request)
        elif args.action == "busy":
            result = busy(service)
        else:
            result = release(service, request)
        print(json.dumps(result, sort_keys=True))
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
