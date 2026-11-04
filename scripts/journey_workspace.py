"""C1 workspace observations, executed by the selected installed interpreter.

Mutations use Runtime APIs. The timeline document read is deliberately read-only:
the public timeline projection no longer serves the retired composition bytes.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
import secrets
import sqlite3
import stat
from pathlib import Path


ACTOR = {"actor": "tst-c1-user"}


def realm_identity(realm: Path) -> dict:
    """Capture a selected realm and database identity without opening a session."""
    if not realm.is_absolute() or realm != realm.resolve():
        raise ValueError("Realm must be an absolute canonical path")
    directory, database = realm.lstat(), (realm / "realm.sqlite3").lstat()
    if not stat.S_ISDIR(directory.st_mode) or not stat.S_ISREG(database.st_mode):
        raise ValueError("Realm identity requires a directory and regular database")
    uri = (realm / "realm.sqlite3").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute("SELECT id FROM realm").fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
        raise ValueError("Realm identity is invalid")
    after = (realm / "realm.sqlite3").lstat()
    directory_after = realm.lstat()
    if ((database.st_dev, database.st_ino) != (after.st_dev, after.st_ino)
            or (directory.st_dev, directory.st_ino) != (directory_after.st_dev, directory_after.st_ino)):
        raise ValueError("Realm database changed during identity capture")
    return {"path": str(realm), "directory": [directory.st_dev, directory.st_ino],
            "database": [database.st_dev, database.st_ino], "realm_id": rows[0][0]}


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


def busy(service, operation: dict | None = None) -> dict:
    """Atomically claim a fixture without starting a Runtime session."""
    store = service.store
    if operation is None:
        operation = {"id": secrets.token_hex(32), "lease_token": secrets.token_hex(32)}
    validate_operation(operation)
    capability, executor, token = "tst.c1.busy", "tst-c1-" + operation["id"], operation["lease_token"]
    spec = {"note": "C1 controlled busy admission", "operation_id": operation["id"]}
    with store._mutex, store._transaction():
        if any(store._spec(row) == spec for row in store.conn.execute(
                "SELECT * FROM tasks WHERE capability=?", (capability,))):
            raise ValueError("Busy fixture operation already exists")
        epoch = store._current_runtime_epoch()
        store.upsert_executor(executor, [capability], runtime_epoch=epoch)
        task = store.create_task(capability, spec)["task"]
        # Exact fixture claiming must not reap or contain unrelated leases.
        # The store's generic claim normally performs global maintenance first.
        reap = store._reap_expired_leases
        store._reap_expired_leases = lambda: None
        try:
            claimed = store._claim_task(task["id"], executor, token, runtime_epoch=epoch)["task"]
        finally:
            store._reap_expired_leases = reap
        if claimed["status"] != "running":
            raise ValueError("Test busy task was not claimed")
        store.heartbeat_task(task["id"], token, fence=claimed["lease_fence"], lease_seconds=7200)
    return {"task_id": task["id"], "lease_token": token,
            "fence": claimed["lease_fence"], "attempt_id": claimed["attempt_id"],
            "operation_id": operation["id"]}


def validate_operation(operation: dict) -> None:
    if (not isinstance(operation, dict) or set(operation) != {"id", "lease_token"}
            or any(not isinstance(value, str) or len(value) != 64
                   or any(char not in "0123456789abcdef" for char in value)
                   for value in operation.values())):
        raise ValueError("Invalid busy fixture operation")


def reconcile(service, operation: dict) -> dict:
    """Find only the run-bound fixture, even if its result was never delivered."""
    validate_operation(operation)
    store = service.store
    with store._mutex, store._transaction():
        spec = {"note": "C1 controlled busy admission", "operation_id": operation["id"]}
        matches = [row for row in store.conn.execute("SELECT * FROM tasks WHERE capability=?", ("tst.c1.busy",))
                   if store._spec(row) == spec]
        if not matches:
            return {"released": False, "absent": True}
        if len(matches) != 1:
            raise ValueError("Busy fixture operation is ambiguous")
        task = matches[0]
        return release(service, {"task_id": task["id"], "lease_token": operation["lease_token"],
                                 "fence": task["lease_fence"], "attempt_id": task["attempt_id"],
                                 "operation_id": operation["id"]})


def release(service, keys: dict) -> dict:
    store = service.store
    # Hold the write transaction across the checks and settlement. fail_task
    # also touches the run, descendants and continuations, so admit only the
    # isolated fixture created above and its exact attempt.
    with store._mutex, store._transaction():
        task = store.conn.execute("SELECT * FROM tasks WHERE id=?", (keys["task_id"],)).fetchone()
        if (task is None or task["capability"] != "tst.c1.busy"
                or task["attempt_id"] != keys["attempt_id"]
                or task["lease_token"] != keys["lease_token"]
                or task["lease_fence"] != keys["fence"]
                or store._spec(task) != {"note": "C1 controlled busy admission", "operation_id": keys.get("operation_id")}
                or store.conn.execute("SELECT count(*) FROM tasks WHERE run_id=?", (task["run_id"],)).fetchone()[0] != 1
                or store.conn.execute("SELECT 1 FROM task_dependencies WHERE predecessor_task_id=?", (task["id"],)).fetchone()
                or store.delegated_children(task["id"])):
            raise ValueError("Busy fixture ownership or isolation check failed")
        store.fail_task(keys["task_id"], keys["lease_token"], "C1 test lease released",
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
    request = json.loads(args.request.read_text(encoding="utf-8"))
    if args.action in {"busy", "release"}:
        if realm_identity(realm) != request["realm_identity"]:
            raise ValueError("Busy fixture realm identity changed")
    if args.action in {"busy", "release"}:
        from runtime_protocol.store import RealmStore
        from runtime_protocol import platform
        from types import SimpleNamespace
        # Borrow the historical store's directory admission fence so that a
        # renamed/replaced realm cannot be admitted under a different lock.
        descriptor = os.open(realm, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        store = None
        locked = False
        try:
            platform.lock_file(descriptor)
            locked = True
            held = os.fstat(descriptor)
            if ([held.st_dev, held.st_ino] != request["realm_identity"]["directory"]
                    or realm_identity(realm) != request["realm_identity"]):
                raise ValueError("Busy fixture realm identity changed before admission")
            store = RealmStore(realm, _admission_fd=descriptor)
            if realm_identity(realm) != request["realm_identity"] or store.realm["id"] != request["realm_identity"]["realm_id"]:
                raise ValueError("Busy fixture realm identity changed during admission")
            service = SimpleNamespace(store=store)
            if args.action == "busy":
                result = busy(service, request["operation"])
            elif "operation" in request:
                result = reconcile(service, request["operation"])
            else:
                result = release(service, request["keys"])
            print(json.dumps(result, sort_keys=True))
        finally:
            try:
                if store is not None:
                    store.close()
            finally:
                try:
                    if locked:
                        platform.unlock_file(descriptor)
                finally:
                    os.close(descriptor)
        return 0
    from runtime_protocol.service import RuntimeService
    service = RuntimeService(realm)
    try:
        if args.action == "seed":
            result = seed(service, **request)
        elif args.action == "snapshot":
            result = snapshot(service, request)
        print(json.dumps(result, sort_keys=True))
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
