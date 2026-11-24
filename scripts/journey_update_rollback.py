#!/usr/bin/env python3
"""Release 15 update/rollback leg: published Release 14 -> real candidate -> forced rollback -> update again.

``run`` drives a REAL isolated install on a hosted Linux runner (see docs/qualification/update-rollback-leg.md);
``check`` re-reads the receipts it wrote and fails closed on anything missing. The pure functions are unit tested.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

if __package__:
    from . import ci_journey as journey
    from . import data_fingerprint
    from . import journey_live_workspace as live
else:
    import ci_journey as journey
    import data_fingerprint
    import journey_live_workspace as live

R14 = {"release": 14, "app": "e0da794ac9d26a808ab4082b29f60408b66b3a4c",
       "agent": "c0ad6b54aadd21630d98bb5039613e37efe92a8f",
       "runtime": "5120462d67479dc4719406d0d34440edb616ace1",
       "vibecomfy": "48b5fa9e45630e1bc502651975ec0ef3a5e74c4c"}
EVIDENCE_DIR = "update-rollback"
GUARD_TRIGGERS = ("timeline_history_no_delete", "timeline_history_no_replace", "timeline_history_no_update")
REALM_GLOB = "runtime/realms/*"
EPHEMERAL_SUFFIXES = ("-wal", "-shm", "-journal")
HOOK_MARKER = "SKIP_AS_FAIL:rollback-hook-absent"
FAILING_DOOR_PORT = "0"  # the staged candidate records port 0; Backend.verify can never match its ephemeral door
STAGING_SECONDS = 2400  # the real download/stage of the candidate precedes its (90 s) health window
WINDOW_SECONDS = 85  # Backend.verify's deadline is 90 s; the Release 15 save must finish inside it
STEPS = {
    "step1-r14-install.json": ("r14_tuple_exact", "project_timeline_edit_saved", "digest_recorded",
                               "realm_ids_recorded", "fingerprint_recorded", "stamps_recorded"),
    "step2-update-r15.json": ("candidate_live_in_window", "r15_edit_saved", "digest_recorded",
                              "guard_layer_present_while_candidate_live", "installer_failed_health"),
    "step3-rollback.json": ("a_stamps_restored", "b_r14_runtime_starts", "c_guard_layer_removed",
                            "d_timeline_readback", "journal_rolled_back", "fingerprint_recorded", "no_data_lost"),
    "step4-resave-reupdate.json": ("e_r14_edit_saved", "e_reupdate_succeeded", "e_history_preserved"),
}
RESULT_NAME = "journey-result.json"
SDK_EDIT = r'''
import json, sys
from astrid.sdk.timeline.api import Timeline
project, timeline, media, clip, at, label, key = sys.argv[1:8]
result = Timeline.open(project, timeline).commit(
    lambda t: t.add(media, at=float(at), track="V1", id=clip), label=label, idempotency_key=key)
print(json.dumps({"committed": True, "summary": str(result.get("summary") if isinstance(result, dict) else result)[:200]}))
'''


# ---- pure logic (unit tested) -------------------------------------------------------------------------------

def sha_field(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError(f"candidate.{name}: full commit SHA required")
    return value


def parse_candidate(candidate: dict) -> dict:
    """The Release 15 tuple (the workflow's ``candidate`` input); it must not be the Release 14 tuple."""
    if not isinstance(candidate, dict):
        raise ValueError("candidate must be a JSON object")
    parsed = {name: sha_field(candidate.get(name), name) for name in ("app", "agent", "runtime")}
    digest = candidate.get("manifest_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("candidate.manifest_sha256: SHA-256 required")
    if parsed["agent"] == R14["agent"] or parsed["runtime"] == R14["runtime"] or parsed["app"] == R14["app"]:
        raise ValueError("candidate shares a source with the Release 14 tuple; this leg needs a real update")
    return {**parsed, "manifest_sha256": digest}


def check_manifest(manifest: dict, expected: dict, *, release=None) -> dict:
    """The manifest must pin exactly ``expected`` agent/runtime (and vibecomfy when given)."""
    if not isinstance(manifest, dict) or type(manifest.get("release")) is not int:
        raise ValueError("manifest has no integer release")
    for name in ("agent", "runtime", "vibecomfy"):
        if name in expected and (manifest.get(name) or {}).get("sha") != expected[name]:
            raise ValueError(f"manifest {name} pin differs from the expected tuple")
    if release is not None and manifest["release"] != release:
        raise ValueError(f"manifest release {manifest['release']} is not {release}")
    if release is None and manifest["release"] <= R14["release"]:
        raise ValueError("candidate manifest is not newer than Release 14")
    return manifest


def hook_present(core_source: str, backend_source: str) -> bool:
    """Whether the Agent source carries the rollback hook (astrid-agent #609)."""
    return "remove_history_guards" in core_source and "def remove_history_guards" in backend_source


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


def guard_state(names: list[str]) -> str:
    return ("installed" if sorted(names) == list(GUARD_TRIGGERS) else "absent" if not names else "partial")


def compare_stamps(before: dict, after: dict) -> list[str]:
    """Release 14 must be back exactly: same version, tuple, manifest bytes and health identity; new instance."""
    problems = []
    for key in ("version_id", "identity", "manifest_sha256", "tuple"):
        if before.get(key) != after.get(key) or before.get(key) in (None, {}, ""):
            problems.append(f"stamp {key} differs or is missing: {before.get(key)!r} -> {after.get(key)!r}")
    for key in ("identity", "schema_digest", "protocol"):
        if (before.get("health") or {}).get(key) != (after.get("health") or {}).get(key):
            problems.append(f"health {key} differs after rollback")
    if not (before.get("health") or {}).get("identity"):
        problems.append("health reported no identity stamp")
    if before.get("runtime_instance_id") == after.get("runtime_instance_id"):
        problems.append("Runtime instance did not change across the update and rollback")
    return problems


def compare_reads(candidate_read: dict, old_read: dict) -> dict:
    """Release 14's read-back of the Release 15 save: identical, or only by fields it cannot represent."""
    mismatched, unrepresentable = [], []
    old_clips = {clip.get("clip_id"): clip for clip in old_read.get("parent_clips", [])}
    for clip in candidate_read.get("parent_clips", []):
        other = old_clips.get(clip.get("clip_id"))
        if other is None:
            mismatched.append(f"clip {clip.get('clip_id')} missing")
            continue
        for field, value in clip.items():
            if field not in other:
                unrepresentable.append(f"{clip.get('clip_id')}.{field}")
            elif other[field] != value:
                mismatched.append(f"{clip.get('clip_id')}.{field}")
    if len(old_clips) != len(candidate_read.get("parent_clips", [])):
        mismatched.append("clip count differs")
    same = bool(candidate_read.get("head_content_digest")) and (
        candidate_read.get("head_content_digest") == old_read.get("head_content_digest"))
    if not same and not unrepresentable and not mismatched:
        mismatched.append("head content digest differs with every clip field identical")
    return {"identical": same, "unrepresentable": sorted(unrepresentable), "mismatched": sorted(mismatched),
            "ok": not mismatched and bool(candidate_read.get("head_content_digest"))}


def fingerprint_summary(before: dict, after: dict) -> dict:
    """Data survival between two stopped-root snapshots (see data_fingerprint.py); ephemeral SQLite files ignored."""
    lost = [path for path in before["files"] if path not in after["files"] and not path.endswith(EPHEMERAL_SUFFIXES)]
    changed = [path for path, meta in before["files"].items() if path in after["files"] and not meta.get("live")
               and after["files"][path]["sha256"] != meta["sha256"]]
    return {"realms_before": before["realms"], "realms_after": after["realms"],
            "realms_lost": sorted(set(before["realms"]) - set(after["realms"])),
            "files_lost": sorted(lost), "files_changed": sorted(changed),
            "files_new": sorted(set(after["files"]) - set(before["files"])),
            "ok": bool(before["realms"]) and not (set(before["realms"]) - set(after["realms"])) and not lost}


def evaluate(receipts: dict) -> list[str]:
    """Every problem in a set of receipts keyed by file name; empty only when every required check passed."""
    problems = []
    for name, required in STEPS.items():
        receipt = receipts.get(name)
        if not isinstance(receipt, dict):
            problems.append(f"{name}: receipt missing")
            continue
        if receipt.get("status") != "pass":
            problems.append(f"{name}: status {receipt.get('status')!r}")
        checks = receipt.get("checks") if isinstance(receipt.get("checks"), dict) else {}
        for check in required:
            if checks.get(check) != "pass":
                problems.append(f"{name}: check {check} is {checks.get(check, 'missing')!r}")
    result = receipts.get(RESULT_NAME)
    if not isinstance(result, dict):
        problems.append(f"{RESULT_NAME}: receipt missing")
        return problems
    if result.get("status") != "pass":
        problems.append(f"{RESULT_NAME}: status {result.get('status')!r}: {result.get('error')}")
    if result.get("hook_present") is not True:
        problems.append(f"{HOOK_MARKER}: hook_present={result.get('hook_present')!r}; the candidate Agent must carry "
                        "the rollback hook (astrid-agent #609) for step 3(c) to pass")
    if result.get("final_cleanup") != "pass":
        problems.append(f"{RESULT_NAME}: final_cleanup is {result.get('final_cleanup')!r}")
    return problems


# ---- observations on the live install ------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def health_body() -> dict:
    import urllib.request
    request = urllib.request.Request(journey.HEALTH_URL, headers={"Origin": "https://astrid.haus"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
        return json.load(response)


def observe(root: Path, data: Path) -> dict:
    """Version/build stamps of the selected install and live Runtime."""
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
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(version["env"])
    output = journey.run([version["python"], "-I", "-c", SDK_EDIT, project, timeline, str(media), clip, str(at),
                          f"Update rollback {clip}", f"ur-{clip}"], evidence / f"edit-{clip}.log", env=env, timeout=300)
    lines = [line for line in output.splitlines() if line.startswith("{")]
    if not lines or json.loads(lines[-1]).get("committed") is not True:
        raise RuntimeError(f"edit {clip} did not report a commit")
    return json.loads(lines[-1])


def realm_database(discovery_realm: str) -> Path:
    return Path(discovery_realm) / "realm.sqlite3"


def service_action(action: str, evidence: Path, data: Path) -> None:
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


def receipt(evidence: Path, name: str, checks: dict, **fields) -> dict:
    required = STEPS[name]
    body = {"schema_version": 1, "receipt_kind": "r15-update-rollback-step", "step": name,
            "checks": {key: checks.get(key, "not-run") for key in required}, **fields}
    body["status"] = "pass" if all(value == "pass" for value in body["checks"].values()) else "fail"
    journey.write_json(evidence / name, body)
    return body


# ---- the leg -----------------------------------------------------------------------------------------------------

def update_with_door_port(app: Path, manifest: Path, shell: str, evidence: Path, root: Path, data: Path) -> None:
    """The real ``public/install --update`` with the one environment difference that fails the candidate's health."""
    env = dict(os.environ, BROWSER=f'"{sys.executable}" -c "pass" %s', ASTRID_RUNTIME_DOOR_PORT=FAILING_DOOR_PORT)
    flags = journey.bootstrap_flags(manifest, "update", root, data)
    journey.run([shell, app / "public" / "install", *flags], evidence / "update-bootstrap.log", env=env)


def read_agent_file(checkout: Path, sha: str, path: str) -> str:
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


def execute(args, root: Path, data: Path, evidence: Path, result: dict) -> None:
    candidate = parse_candidate(journey.read_json(args.candidate))
    app, r14_app = args.app_checkout.resolve(strict=True), args.r14_app_checkout.resolve(strict=True)
    agent = args.candidate_agent_checkout.resolve(strict=True)
    if journey.git_source(app)["sha"] != candidate["app"] or journey.git_source(r14_app)["sha"] != R14["app"]:
        raise RuntimeError("App checkouts are not the candidate and Release 14 commits")
    if journey.git_source(agent)["sha"] != candidate["agent"]:
        raise RuntimeError("Agent checkout is not the candidate Agent commit")
    r15_path = app / "public" / "install-manifest.json"
    if sha256_file(r15_path) != candidate["manifest_sha256"]:
        raise RuntimeError("candidate App manifest bytes differ from candidate.manifest_sha256")
    r15_manifest = check_manifest(json.loads(r15_path.read_text("utf-8")), candidate)
    r14_path = r14_app / "public" / "install-manifest.json"
    r14_manifest = check_manifest(json.loads(r14_path.read_text("utf-8")), R14, release=R14["release"])
    result["hook_present"] = hook_present(read_agent_file(agent, candidate["agent"], "astrid/installer/core.py"),
                                          read_agent_file(agent, candidate["agent"], "astrid/installer/backend.py"))
    result.update(candidate=candidate, release14=R14, candidate_release=r15_manifest["release"])
    media = args.media.resolve(strict=True)
    project, timeline, ids = "ur-project", "ur-timeline", {}

    # Step 1: Release 14 from its real tuple.
    ev1 = evidence / "step1"
    journey.bootstrap(r14_app, r14_path, args.shell, "install", ev1, root, data, agent)
    journey.wait_ready(data)
    _, _, r14 = journey.installation(root, data)
    journey.selected_tuple(r14_manifest, r14)
    before = observe(root, data)
    installed_cli = lambda argv, name: journey.installed_cli(r14, argv, ev1, name)  # noqa: E731
    installed_cli(["projects", "create", project, "--name", project, "--json"], "project.json")
    created = installed_cli(["timelines", "create", "--project", project, "--slug", timeline,
                             "--name", "Update rollback", "--json"], "timeline.json")
    ids["timeline"] = find_key(json.loads(created), ("timeline_id", "id"))
    save_edit(r14, ev1, project, timeline, media, "r14-edit", 0)
    read14 = runtime_read(data, project, timeline, ids)
    fp14 = stopped_fingerprint(root, data, ev1, "r14")
    stamps14 = observe(root, data)
    result["step1"] = receipt(
        evidence, "step1-r14-install.json",
        {"r14_tuple_exact": "pass", "project_timeline_edit_saved": "pass" if read14["parent_clips"] else "fail",
         "digest_recorded": "pass" if read14["head_content_digest"] else "fail",
         "realm_ids_recorded": "pass" if fp14["realms"] and before["active_realm"] in fp14["realms"] else "fail",
         "fingerprint_recorded": "pass", "stamps_recorded": "pass" if before["manifest_sha256"] else "fail"},
        stamps=before, read=read14, realm_ids=fp14["realms"], ids=ids,
        fingerprint={"file": "step1/fingerprint-r14.json", "files": len(fp14["files"])})

    # Steps 2 and 3: the real update, forced to fail its health step after the Release 15 save.
    ev2 = evidence / "step2"
    ev2.mkdir(parents=True, exist_ok=True)
    failure: list[BaseException] = []

    def update() -> None:
        try:
            update_with_door_port(app, r15_path, args.shell, ev2, root, data)
        except BaseException as exc:  # noqa: BLE001 - the expected installer failure is evidence
            failure.append(exc)

    worker = threading.Thread(target=update, name="forced-failing-update")
    started = time.monotonic()
    worker.start()
    live_candidate, discovery = wait_for_candidate(root, data, stamps14["runtime_instance_id"], worker)
    checks2, step2 = {}, {}
    if live_candidate is not None:
        seen = time.monotonic()
        step2["staging_seconds"] = round(seen - started, 1)
        try:
            save_edit(live_candidate, ev2, project, timeline, media, "r15-edit", 4)
            read15 = runtime_read(data, project, timeline, ids)
            checks2["r15_edit_saved"] = "pass" if len(read15["parent_clips"]) > len(read14["parent_clips"]) else "fail"
            checks2["digest_recorded"] = ("pass" if read15["head_content_digest"] and
                                          read15["head_content_digest"] != read14["head_content_digest"] else "fail")
            step2["read"] = read15
            names = guard_triggers(realm_database(discovery["realm_root"]))
            step2["guard_triggers_while_live"] = names
            checks2["guard_layer_present_while_candidate_live"] = "pass" if guard_state(names) == "installed" else "fail"
            step2["window_seconds"] = round(time.monotonic() - seen, 1)
            checks2["candidate_live_in_window"] = "pass" if step2["window_seconds"] <= WINDOW_SECONDS else "fail"
        except Exception as exc:  # noqa: BLE001
            step2["window_error"] = f"{type(exc).__name__}: {exc}"
    worker.join(timeout=1800)
    checks2["installer_failed_health"] = "pass" if failure and not worker.is_alive() else "fail"
    step2["installer_error"] = str(failure[0])[:400] if failure else None
    result["step2"] = receipt(evidence, "step2-update-r15.json", checks2, candidate_version=(live_candidate or {}).get("root"),
                              **step2)
    if result["step2"]["status"] != "pass":
        raise RuntimeError("step 2 failed: " + json.dumps(result["step2"]["checks"]))

    ev3 = evidence / "step3"
    ev3.mkdir(parents=True, exist_ok=True)
    journal = journey.read_json(root / "update-transaction.json")
    journey.wait_ready(data)
    after = observe(root, data)
    problems = compare_stamps(stamps14, after)
    checks3 = {"a_stamps_restored": "fail" if problems else "pass",
               "journal_rolled_back": "pass" if journal.get("step") == "rolled_back" else "fail"}
    checks3["b_r14_runtime_starts"] = ("pass" if (after["health"].get("identity") or {}).get("release") == 14
                                       and after["tuple"]["release"] == 14 else "fail")
    journey.assert_direct_service_ownership(root, data)
    service_action("stop", ev3, data)  # the guard check and fingerprint read a stopped realm
    try:
        triggers_after = guard_triggers(realm_database(after["realm_root"]))
        fp_rb = take_snapshot(data)
    finally:
        service_action("start", ev3, data)
    journey.write_json(ev3 / "fingerprint-rollback.json", fp_rb)
    survival = fingerprint_summary(fp14, fp_rb)
    checks3["c_guard_layer_removed"] = ("skip_as_fail" if not result["hook_present"] else
                                        "pass" if guard_state(triggers_after) == "absent" else "fail")
    read_back = runtime_read(data, project, timeline, ids)
    comparison = compare_reads(read15, read_back)
    checks3.update(d_timeline_readback="pass" if comparison["ok"] else "fail",
                   fingerprint_recorded="pass", no_data_lost="pass" if survival["ok"] else "fail")
    result["step3"] = receipt(evidence, "step3-rollback.json", checks3, stamps_after=after, stamp_problems=problems,
                              journal={key: journal.get(key) for key in ("step", "backup", "history_guard_removal")},
                              guard_triggers_after=triggers_after, readback=comparison, survival=survival,
                              hook_marker=None if result["hook_present"] else HOOK_MARKER)

    # Step 3(e): one Release 14 save, then the clean update to the candidate.
    ev4 = evidence / "step4"
    _, _, r14_again = journey.installation(root, data)
    save_edit(r14_again, ev4, project, timeline, media, "r14-edit-2", 8)
    read14b = runtime_read(data, project, timeline, ids)
    journey.bootstrap(app, r15_path, args.shell, "update", ev4, root, data)
    journey.wait_ready(data)
    _, _, r15 = journey.installation(root, data)
    journey.selected_tuple(r15_manifest, r15)
    final = observe(root, data)
    read_final = runtime_read(data, project, timeline, ids)
    names_final = guard_triggers(realm_database(final["realm_root"]))
    result["step4"] = receipt(
        evidence, "step4-resave-reupdate.json",
        {"e_r14_edit_saved": "pass" if len(read14b["parent_clips"]) > len(read15["parent_clips"]) else "fail",
         "e_reupdate_succeeded": "pass" if final["tuple"]["release"] == r15_manifest["release"]
         and final["tuple"]["agent"] == candidate["agent"] and final["tuple"]["runtime"] == candidate["runtime"] else "fail",
         "e_history_preserved": "pass" if read_final["head_content_digest"] == read14b["head_content_digest"]
         and len(read_final["parent_clips"]) == len(read14b["parent_clips"]) >= 3 else "fail"},
        stamps=final, read_before=read14b, read_after=read_final, guard_triggers=names_final)
    # The final install is removed by run(); a not-passing step 4 is raised by it.


def run(args) -> int:
    run_root, root, data, _ = journey.run_scope(args.run_root, args.root, args.data_root, "fresh-install")
    evidence = run_root / "evidence" / EVIDENCE_DIR
    evidence.mkdir(parents=True, exist_ok=True)
    result = {"schema_version": 1, "receipt_kind": "r15-update-rollback", "status": "not-run",
              "final_cleanup": "not-run", "hook_present": None, **journey.runner_identity()}
    status = 1
    try:
        journey.assert_runner_identity(journey.runner_identity())
        with journey.isolated_environment(run_root, root, data):
            journey.assert_host_available("fresh-install", root, data)
            try:
                execute(args, root, data, evidence, result)
                result["status"] = "pass" if all(
                    (result.get(step) or {}).get("status") == "pass" for step in ("step1", "step2", "step3", "step4")
                ) else "fail"
            except Exception as exc:  # noqa: BLE001
                result.update(status="fail", error=f"{type(exc).__name__}: {exc}")
                print(f"Journey failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            finally:
                try:
                    if (root / "current").is_file():
                        _, _, installed = journey.installation(root, data)
                        journey.installed_cli(installed, ["install", "--uninstall", "--root", str(root),
                                                          "--data-root", str(data)], evidence, "final-uninstall.log",
                                              timeout=1800)
                    if journey.runtime_processes(data):
                        raise RuntimeError("Runtime processes remained after final uninstall")
                    journey.assert_host_available("cleanup", root, data)
                    result["final_cleanup"] = "pass"
                except Exception as exc:  # noqa: BLE001
                    result.update(final_cleanup="fail", cleanup_error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        result.update(status="fail", error=f"{type(exc).__name__}: {exc}")
    finally:
        for key in ("step1", "step2", "step3", "step4"):
            result[key] = (result.get(key) or {}).get("status", "not-run")
        journey.write_json(evidence / RESULT_NAME, result)
    problems = evaluate(load_receipts(evidence))
    for problem in problems:
        print("PROBLEM:", problem, file=sys.stderr)
    status = 1 if problems else 0
    return status


def load_receipts(directory: Path) -> dict:
    found = {}
    for name in (*STEPS, RESULT_NAME):
        path = Path(directory) / name
        try:
            found[name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("--candidate", type=Path, required=True)
    runner.add_argument("--app-checkout", type=Path, required=True)
    runner.add_argument("--r14-app-checkout", type=Path, required=True)
    runner.add_argument("--candidate-agent-checkout", type=Path, required=True)
    runner.add_argument("--media", type=Path, required=True)
    runner.add_argument("--run-root", type=Path, required=True)
    runner.add_argument("--root", type=Path, required=True)
    runner.add_argument("--data-root", type=Path, required=True)
    runner.add_argument("--shell", default="sh")
    checker = sub.add_parser("check")
    checker.add_argument("receipts", type=Path)
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args)
    problems = evaluate(load_receipts(args.receipts))
    for problem in problems:
        print("PROBLEM:", problem, file=sys.stderr)
    print(f"update-rollback receipts: {'FAIL' if problems else 'pass'} ({len(problems)} problems)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
