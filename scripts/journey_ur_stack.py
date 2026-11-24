"""Update/rollback leg: observations of, and actions on, the REAL installed stack (hosted Linux runner only)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

if __package__:
    from . import ci_journey as journey
    from . import data_fingerprint
    from . import journey_live_workspace as live
    from .check_journey_evidence import digest as sha256_file
    from .journey_ur_evidence import GUARD_TRIGGERS
else:
    import ci_journey as journey
    import data_fingerprint
    import journey_live_workspace as live
    from check_journey_evidence import digest as sha256_file
    from journey_ur_evidence import GUARD_TRIGGERS

REALM_GLOB = "runtime/realms/*"
FAILING_DOOR_PORT = "0"  # the staged candidate records port 0; Backend.verify can never match its ephemeral door
STAGING_SECONDS = 2400  # the real download/stage of the candidate precedes its (90 s) health window
SDK_EDIT = r'''
import json, sys
from astrid.sdk.timeline.api import Timeline
project, timeline, media, clip, at, label, key = sys.argv[1:8]
result = Timeline.open(project, timeline).commit(
    lambda t: t.add(media, at=float(at), track="V1", id=clip), label=label, idempotency_key=key)
print(json.dumps({"committed": True, "summary": str(result.get("summary") if isinstance(result, dict) else result)[:200]}))
'''


def guard_triggers(database: Path) -> list[str]:
    """The history guard layer 1 triggers present in a realm database (read only)."""
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        marks = ",".join("?" * len(GUARD_TRIGGERS))
        rows = connection.execute(f"SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name="
                                  f"'timeline_commit_history' AND name IN ({marks})", GUARD_TRIGGERS).fetchall()
    finally:
        connection.close()
    return sorted(name for (name,) in rows)


def realm_database(discovery_realm: str) -> Path:
    # journey.workspace_root(data) resolves the same discovery realm_root but also requires it to exist and sit under
    # the run root; the step 3 reads happen on a stopped Runtime, so the plain recorded path is used unvalidated.
    return Path(discovery_realm) / "realm.sqlite3"


def health_body() -> dict:
    # journey.healthy(data) makes this same request but returns the discovery document, not the health body that
    # the stamps compare (identity, schema_digest, protocol).
    request = urllib.request.Request(journey.HEALTH_URL, headers={"Origin": "https://astrid.haus"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
        return json.load(response)


def observe(root: Path, data: Path) -> dict:
    """Version/build stamps of the selected install and live Runtime."""
    # journey.selected_tuple returns only agent/runtime/schema_digest/tools and raises on a mismatch, so it cannot
    # supply the release and vibecomfy pins this leg compares across the rollback.
    path, _, version = journey.installation(root, data)
    manifest_path = Path(version["root"]) / "manifest.json"
    manifest = journey.read_json(manifest_path)
    discovery = journey.healthy(data)
    return {"version_id": path.parent.name, "identity": version.get("identity"),
            "manifest_sha256": sha256_file(manifest_path),
            "tuple": {"release": manifest.get("release"), "agent": (manifest.get("agent") or {}).get("sha"),
                      "runtime": (manifest.get("runtime") or {}).get("sha"),
                      "runtime_schema_digest": (manifest.get("runtime") or {}).get("schema_digest"),
                      "vibecomfy": (manifest.get("vibecomfy") or {}).get("sha")},
            "health": health_body(), "runtime_instance_id": discovery.get("runtime_instance_id"),
            "active_realm": discovery.get("active_realm"), "realm_root": discovery.get("realm_root")}


def find_key(value, names):
    """First value found under any of ``names`` in nested JSON (CLI envelopes differ across releases)."""
    if isinstance(value, dict):
        for name in names:
            if isinstance(value.get(name), str) and value[name]:
                return value[name]
        value = list(value.values())
    for item in value if isinstance(value, list) else ():
        found = find_key(item, names)
        if found:
            return found
    return None


def runtime_read(data: Path, project: str, timeline: str, ids: dict) -> dict:
    """The Runtime's own inspection of the timeline head: content digests and the parent clips."""
    discovery = journey.read_json(data / "runtime" / "discovery.json")
    client = live.LiveWorkspace(discovery["endpoint"], live.credential_token(Path(discovery["credential_file"])))
    project_id = ids.get("project") or find_key(client.request("GET", live.path("projects", project)),
                                                ("project_id", "id"))
    if not project_id:
        raise RuntimeError("the Runtime served no project id for the update-rollback project")
    timeline_id = ids.get("timeline") or timeline
    inspected = client.request("POST", live.path("projects", project_id, "timelines", timeline_id, "inspect"), {})
    ids.update(project=project_id, timeline=timeline_id)
    return {"head_revision_id": inspected.get("head_revision_id"),
            "head_content_digest": inspected.get("head_content_digest"),
            "parent_content_digest": inspected.get("parent_content_digest"),
            "parent_clips": sorted(inspected.get("selected_parent_clips") or [], key=lambda c: str(c.get("clip_id")))}


def save_edit(version: dict, evidence: Path, project: str, timeline: str, media: Path, clip: str, at: float) -> dict:
    """One saved edit through the installed SDK (the documented ``Timeline.open(...).commit`` route)."""
    # journey.installed_cli only runs ``python -I -m <module>``; the SDK commit needs ``-c``, so the same clean
    # environment is built here.
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(version["env"])
    output = journey.run([version["python"], "-I", "-c", SDK_EDIT, project, timeline, str(media), clip, str(at),
                          f"Update rollback {clip}", f"ur-{clip}"], evidence / f"edit-{clip}.log", env=env, timeout=300)
    lines = [line for line in output.splitlines() if line.startswith("{")]
    if not lines or json.loads(lines[-1]).get("committed") is not True:
        raise RuntimeError(f"edit {clip} did not report a commit")
    return json.loads(lines[-1])


def service_action(action: str, evidence: Path, data: Path) -> None:
    # journey_version_jumps.direct_workspace stops/starts the same unit inline with no reusable helper, and it
    # does not restart and wait for readiness.
    journey.run(["systemctl", "--user", action, "astrid.service"], evidence / f"service-{action}.log", timeout=120)
    if action == "stop":
        deadline = time.monotonic() + 120
        while journey.runtime_processes(data):
            if time.monotonic() >= deadline:
                raise RuntimeError("Runtime did not stop for the data snapshot")
            time.sleep(1)
    else:
        journey.wait_ready(data)


def take_snapshot(data: Path) -> dict:
    """data_fingerprint.snap on the STOPPED root; its SystemExit refusals become ordinary failures."""
    # journey_version_jumps._preseed_workspace_state fingerprints one realm into a single tree digest: it keeps
    # no per-file sha256 and no realm id list, which the survival comparison needs.
    try:
        return data_fingerprint.snap(data, REALM_GLOB, data_fingerprint.DEFAULT_EXCLUDES)
    except SystemExit as exc:
        raise RuntimeError(f"data fingerprint refused: {exc}") from exc


def stopped_fingerprint(root: Path, data: Path, evidence: Path, name: str) -> dict:
    """Stop the service, fingerprint the whole data root (data_fingerprint.py), restart it."""
    journey.assert_direct_service_ownership(root, data)
    service_action("stop", evidence, data)
    try:
        snapshot = take_snapshot(data)
    finally:
        service_action("start", evidence, data)
    if not snapshot["realms"]:
        raise RuntimeError(f"fingerprint {name}: no realm ids match {REALM_GLOB!r}")
    journey.write_json(evidence / f"fingerprint-{name}.json", snapshot)
    return snapshot


def update_with_door_port(app: Path, manifest: Path, shell: str, evidence: Path, root: Path, data: Path) -> None:
    """The real ``public/install --update`` with the one environment difference that fails the candidate's health."""
    env = dict(os.environ, BROWSER=f'"{sys.executable}" -c "pass" %s', ASTRID_RUNTIME_DOOR_PORT=FAILING_DOOR_PORT)
    flags = journey.bootstrap_flags(manifest, "update", root, data)
    journey.run([shell, app / "public" / "install", *flags], evidence / "update-bootstrap.log", env=env)


def read_agent_file(checkout: Path, sha: str, path: str) -> str:
    # ci_journey._git_show is private, returns bytes and raises when the file is absent; an absent file here means
    # "no rollback hook", which must be an empty string rather than an error.
    done = subprocess.run(["git", "-C", str(checkout), "show", f"{sha}:{path}"], capture_output=True, text=True,
                          timeout=120, check=False)
    return done.stdout if done.returncode == 0 else ""


def wait_for_candidate(root: Path, data: Path, old_instance: str, worker: threading.Thread):
    """The candidate Version document and discovery once the staged candidate's Runtime is live."""
    deadline = time.monotonic() + STAGING_SECONDS
    while worker.is_alive() and time.monotonic() < deadline:
        try:
            journal = journey.read_json(root / "update-transaction.json")
            discovery = journey.read_json(data / "runtime" / "discovery.json")
            candidate = journal.get("candidate")
            if (journal.get("step") == "switched" and isinstance(candidate, dict)
                    and discovery.get("runtime_instance_id") not in (None, old_instance)
                    and (discovery.get("identity") or {}).get("release") == candidate["identity"]["release"]):
                return candidate, discovery
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(0.5)
    return None, None
