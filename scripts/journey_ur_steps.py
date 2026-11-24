"""Update/rollback leg: the four steps that drive the installed stack and write one receipt each.

Step 1 installs Release 14 and saves an edit; step 2 runs the real update, forced to fail its health step after a
Release 15 save; step 3 checks the automatic rollback; step 4 saves on Release 14 again and re-updates.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

if __package__:
    from . import ci_journey as journey
    from .check_journey_evidence import digest as sha256_file
    from .journey_ur_evidence import (HOOK_MARKER, R14, check_manifest, compare_reads, compare_stamps,
                                      fingerprint_summary, guard_state, hook_present, parse_candidate)
    from .journey_ur_receipts import receipt
    from .journey_ur_stack import (find_key, guard_triggers, observe, read_agent_file, realm_database, runtime_read,
                                   save_edit, service_action, stopped_fingerprint, take_snapshot,
                                   update_with_door_port, wait_for_candidate)
else:
    import ci_journey as journey
    from check_journey_evidence import digest as sha256_file
    from journey_ur_evidence import (HOOK_MARKER, R14, check_manifest, compare_reads, compare_stamps,
                                     fingerprint_summary, guard_state, hook_present, parse_candidate)
    from journey_ur_receipts import receipt
    from journey_ur_stack import (find_key, guard_triggers, observe, read_agent_file, realm_database, runtime_read,
                                  save_edit, service_action, stopped_fingerprint, take_snapshot,
                                  update_with_door_port, wait_for_candidate)

WINDOW_SECONDS = 85  # Backend.verify's deadline is 90 s; the Release 15 save must finish inside it


@dataclass
class Leg:
    """What the four steps share: validated inputs, the run's roots and the readings carried between steps."""
    args: object
    root: Path
    data: Path
    evidence: Path
    result: dict
    candidate: dict = field(default_factory=dict)
    app: Path = None
    r14_app: Path = None
    agent: Path = None
    r15_path: Path = None
    r14_path: Path = None
    r15_manifest: dict = field(default_factory=dict)
    r14_manifest: dict = field(default_factory=dict)
    media: Path = None
    project: str = "ur-project"
    timeline: str = "ur-timeline"
    ids: dict = field(default_factory=dict)
    stamps14: dict = field(default_factory=dict)
    read14: dict = field(default_factory=dict)
    fp14: dict = field(default_factory=dict)
    read15: dict = field(default_factory=dict)
    live_candidate: dict = None
    discovery: dict = None


def prepare(args, root: Path, data: Path, evidence: Path, result: dict) -> Leg:
    """Validate the candidate tuple, both App checkouts, both manifests and the Agent rollback hook."""
    leg = Leg(args, root, data, evidence, result)
    leg.candidate = parse_candidate(journey.read_json(args.candidate))
    candidate = leg.candidate
    leg.app, leg.r14_app = args.app_checkout.resolve(strict=True), args.r14_app_checkout.resolve(strict=True)
    leg.agent = args.candidate_agent_checkout.resolve(strict=True)
    if journey.git_source(leg.app)["sha"] != candidate["app"] or journey.git_source(leg.r14_app)["sha"] != R14["app"]:
        raise RuntimeError("App checkouts are not the candidate and Release 14 commits")
    if journey.git_source(leg.agent)["sha"] != candidate["agent"]:
        raise RuntimeError("Agent checkout is not the candidate Agent commit")
    leg.r15_path = leg.app / "public" / "install-manifest.json"
    if sha256_file(leg.r15_path) != candidate["manifest_sha256"]:
        raise RuntimeError("candidate App manifest bytes differ from candidate.manifest_sha256")
    leg.r15_manifest = check_manifest(journey.read_json(leg.r15_path), candidate)
    leg.r14_path = leg.r14_app / "public" / "install-manifest.json"
    leg.r14_manifest = check_manifest(journey.read_json(leg.r14_path), R14, release=R14["release"])
    result["hook_present"] = hook_present(read_agent_file(leg.agent, candidate["agent"], "astrid/installer/core.py"),
                                          read_agent_file(leg.agent, candidate["agent"], "astrid/installer/backend.py"))
    result.update(candidate=candidate, release14=R14, candidate_release=leg.r15_manifest["release"])
    leg.media = args.media.resolve(strict=True)
    return leg


def step1_install_r14(leg: Leg) -> None:
    """Release 14 from its real tuple, one saved edit, and the stopped-root fingerprint."""
    ev1 = leg.evidence / "step1"
    journey.bootstrap(leg.r14_app, leg.r14_path, leg.args.shell, "install", ev1, leg.root, leg.data, leg.agent)
    journey.wait_ready(leg.data)
    _, _, r14 = journey.installation(leg.root, leg.data)
    journey.selected_tuple(leg.r14_manifest, r14)
    before = observe(leg.root, leg.data)
    installed_cli = lambda argv, name: journey.installed_cli(r14, argv, ev1, name)  # noqa: E731
    installed_cli(["projects", "create", leg.project, "--name", leg.project, "--json"], "project.json")
    created = installed_cli(["timelines", "create", "--project", leg.project, "--slug", leg.timeline,
                             "--name", "Update rollback", "--json"], "timeline.json")
    leg.ids["timeline"] = find_key(json.loads(created), ("timeline_id", "id"))
    save_edit(r14, ev1, leg.project, leg.timeline, leg.media, "r14-edit", 0)
    leg.read14 = read14 = runtime_read(leg.data, leg.project, leg.timeline, leg.ids)
    leg.fp14 = fp14 = stopped_fingerprint(leg.root, leg.data, ev1, "r14")
    leg.stamps14 = observe(leg.root, leg.data)
    leg.result["step1"] = receipt(
        leg.evidence, "step1-r14-install.json",
        {"r14_tuple_exact": "pass", "project_timeline_edit_saved": "pass" if read14["parent_clips"] else "fail",
         "digest_recorded": "pass" if read14["head_content_digest"] else "fail",
         "realm_ids_recorded": "pass" if fp14["realms"] and before["active_realm"] in fp14["realms"] else "fail",
         "fingerprint_recorded": "pass", "stamps_recorded": "pass" if before["manifest_sha256"] else "fail"},
        stamps=before, read=read14, realm_ids=fp14["realms"], ids=leg.ids,
        fingerprint={"file": "step1/fingerprint-r14.json", "files": len(fp14["files"])})


def step2_forced_failing_update(leg: Leg) -> None:
    """The real update, forced to fail its health step after the Release 15 save inside the health window."""
    ev2 = leg.evidence / "step2"
    ev2.mkdir(parents=True, exist_ok=True)
    failure: list[BaseException] = []

    def update() -> None:
        try:
            update_with_door_port(leg.app, leg.r15_path, leg.args.shell, ev2, leg.root, leg.data)
        except BaseException as exc:  # noqa: BLE001 - the expected installer failure is evidence
            failure.append(exc)

    worker = threading.Thread(target=update, name="forced-failing-update")
    started = time.monotonic()
    worker.start()
    leg.live_candidate, leg.discovery = wait_for_candidate(leg.root, leg.data, leg.stamps14["runtime_instance_id"],
                                                           worker)
    checks2, step2 = {}, {}
    if leg.live_candidate is not None:
        seen = time.monotonic()
        step2["staging_seconds"] = round(seen - started, 1)
        try:
            save_edit(leg.live_candidate, ev2, leg.project, leg.timeline, leg.media, "r15-edit", 4)
            leg.read15 = read15 = runtime_read(leg.data, leg.project, leg.timeline, leg.ids)
            checks2["r15_edit_saved"] = ("pass" if len(read15["parent_clips"]) > len(leg.read14["parent_clips"])
                                         else "fail")
            checks2["digest_recorded"] = ("pass" if read15["head_content_digest"] and
                                          read15["head_content_digest"] != leg.read14["head_content_digest"]
                                          else "fail")
            step2["read"] = read15
            names = guard_triggers(realm_database(leg.discovery["realm_root"]))
            step2["guard_triggers_while_live"] = names
            checks2["guard_layer_present_while_candidate_live"] = "pass" if guard_state(names) == "installed" else "fail"
            step2["window_seconds"] = round(time.monotonic() - seen, 1)
            checks2["candidate_live_in_window"] = "pass" if step2["window_seconds"] <= WINDOW_SECONDS else "fail"
        except Exception as exc:  # noqa: BLE001
            step2["window_error"] = f"{type(exc).__name__}: {exc}"
    worker.join(timeout=1800)
    checks2["installer_failed_health"] = "pass" if failure and not worker.is_alive() else "fail"
    step2["installer_error"] = str(failure[0])[:400] if failure else None
    leg.result["step2"] = receipt(leg.evidence, "step2-update-r15.json", checks2,
                                  candidate_version=(leg.live_candidate or {}).get("root"), **step2)
    if leg.result["step2"]["status"] != "pass":
        raise RuntimeError("step 2 failed: " + json.dumps(leg.result["step2"]["checks"]))


def step3_rollback(leg: Leg) -> None:
    """Release 14 must be back exactly: stamps, guard layer removed, read-back and no data lost."""
    root, data = leg.root, leg.data
    ev3 = leg.evidence / "step3"
    ev3.mkdir(parents=True, exist_ok=True)
    journal = journey.read_json(root / "update-transaction.json")
    journey.wait_ready(data)
    after = observe(root, data)
    problems = compare_stamps(leg.stamps14, after)
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
    survival = fingerprint_summary(leg.fp14, fp_rb)
    hook = leg.result["hook_present"]
    checks3["c_guard_layer_removed"] = ("skip_as_fail" if not hook else
                                        "pass" if guard_state(triggers_after) == "absent" else "fail")
    read_back = runtime_read(data, leg.project, leg.timeline, leg.ids)
    comparison = compare_reads(leg.read15, read_back)
    checks3.update(d_timeline_readback="pass" if comparison["ok"] else "fail",
                   fingerprint_recorded="pass", no_data_lost="pass" if survival["ok"] else "fail")
    leg.result["step3"] = receipt(
        leg.evidence, "step3-rollback.json", checks3, stamps_after=after, stamp_problems=problems,
        journal={key: journal.get(key) for key in ("step", "backup", "history_guard_removal")},
        guard_triggers_after=triggers_after, readback=comparison, survival=survival,
        hook_marker=None if hook else HOOK_MARKER)


def step4_resave_reupdate(leg: Leg) -> None:
    """Step 3(e): one Release 14 save, then the clean update to the candidate. run() removes the final install."""
    ev4 = leg.evidence / "step4"
    _, _, r14_again = journey.installation(leg.root, leg.data)
    save_edit(r14_again, ev4, leg.project, leg.timeline, leg.media, "r14-edit-2", 8)
    read14b = runtime_read(leg.data, leg.project, leg.timeline, leg.ids)
    journey.bootstrap(leg.app, leg.r15_path, leg.args.shell, "update", ev4, leg.root, leg.data)
    journey.wait_ready(leg.data)
    _, _, r15 = journey.installation(leg.root, leg.data)
    journey.selected_tuple(leg.r15_manifest, r15)
    final = observe(leg.root, leg.data)
    read_final = runtime_read(leg.data, leg.project, leg.timeline, leg.ids)
    names_final = guard_triggers(realm_database(final["realm_root"]))
    candidate = leg.candidate
    leg.result["step4"] = receipt(
        leg.evidence, "step4-resave-reupdate.json",
        {"e_r14_edit_saved": "pass" if len(read14b["parent_clips"]) > len(leg.read15["parent_clips"]) else "fail",
         "e_reupdate_succeeded": "pass" if final["tuple"]["release"] == leg.r15_manifest["release"]
         and final["tuple"]["agent"] == candidate["agent"] and final["tuple"]["runtime"] == candidate["runtime"]
         else "fail",
         "e_history_preserved": "pass" if read_final["head_content_digest"] == read14b["head_content_digest"]
         and len(read_final["parent_clips"]) == len(read14b["parent_clips"]) >= 3 else "fail"},
        stamps=final, read_before=read14b, read_after=read_final, guard_triggers=names_final)


def execute(args, root: Path, data: Path, evidence: Path, result: dict) -> None:
    leg = prepare(args, root, data, evidence, result)
    step1_install_r14(leg)
    step2_forced_failing_update(leg)
    step3_rollback(leg)
    step4_resave_reupdate(leg)
