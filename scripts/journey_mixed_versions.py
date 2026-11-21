#!/usr/bin/env python3
"""R14-FIX-17: genuine hosted mixed-version browser on one installed Runtime.

Both rows are Linux native-installed journeys with the original frozen
candidate/source/ownership gates. No source Runtime, fake server, schema
override, foreground fallback, or guessed App SHA is admitted. The candidate
tuple comes only from the Lead's eventual FIX-15 App commit.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import platform
import sys

if __package__:
    from . import check_journey_evidence as gate
    from . import ci_journey as journey
    from . import journey_hosted_update as hosted
    from . import journey_version_jumps as jumps
else:
    import check_journey_evidence as gate
    import ci_journey as journey
    import journey_hosted_update as hosted
    import journey_version_jumps as jumps

NEW_APP_OLD_RUNTIME = "mixed-r14-app-r13-installed"
OLD_APP_NEW_RUNTIME = "mixed-r13-app-r14-installed"
JOB_NEW_APP_OLD = "direct-linux-mixed-r14app-r13"
JOB_OLD_APP_NEW = "direct-linux-mixed-r13app-r14"
CASES = {
    NEW_APP_OLD_RUNTIME: JOB_NEW_APP_OLD,
    OLD_APP_NEW_RUNTIME: JOB_OLD_APP_NEW,
}
DOOR_BASE = "http://127.0.0.1:17340"
AUTHOR_LABEL = "FIX17 R14 authored creative timeline"
SAVE_LABEL = "FIX17 R14-on-R13 creative edit"

REQUIRED_CHECKS = {
    NEW_APP_OLD_RUNTIME: (
        "frozen_candidate", "pinned_r13", "r13_install", "canonical_seed",
        "native_owner", "real_browser_save", "save_reopen", "fix16_rejected_card",
        "rejected_outbox_retained", "native_end", "unrelated_sentinel"),
    OLD_APP_NEW_RUNTIME: (
        "frozen_candidate", "pinned_r13", "r13_install", "canonical_seed",
        "r14_update", "r14_installed", "native_owner", "r14_authored",
        "r14_save_reopen", "r13_legacy_open", "r13_legacy_queue_read",
        "r13_legacy_reopen", "native_end", "unrelated_sentinel"),
}


@contextmanager
def stage(result: dict, result_path: Path, name: str):
    entry = {"stage": name, "state": "running"}
    result["steps"].append(entry)
    journey.write_json(result_path, result)
    try:
        yield
    except Exception as error:
        entry.update(state="fail", error=f"{type(error).__name__}: {error}")
        journey.write_json(result_path, result)
        raise
    entry["state"] = "pass"
    journey.write_json(result_path, result)


def _git_source(checkout: Path, expected: str) -> dict:
    if checkout.is_symlink() or not checkout.is_dir():
        raise RuntimeError("App source checkout is missing or symlinked")
    resolved = journey.git_source(checkout.resolve(strict=True))
    if resolved.get("sha") != expected:
        raise RuntimeError("Hosted App checkout is not the exact pinned source")
    return resolved


def _dist(checkout: Path, supplied: Path) -> Path:
    actual = supplied.resolve(strict=True)
    if supplied.is_symlink() or actual != checkout.resolve(strict=True) / "dist":
        raise RuntimeError("Mixed App build does not belong to the checked immutable source")
    if any(not (actual / name).is_file() for name in ("index.html", "build.json")):
        raise RuntimeError("Mixed App did not produce its own hosted index and build receipt")
    return actual


def _browser(config: dict, *, app: Path, dist: Path, version: dict,
             evidence: Path, step_name: str, expected_sha: str) -> dict:
    """A new Chromium instance reads the real App build from a pinned HTTPS host."""
    evidence.mkdir(parents=True, exist_ok=True)
    manifest = (app / "public" / "install-manifest.json").read_bytes()
    page, types = hosted.page_routes(dist)
    page.update(hosted.release_routes(app, manifest))
    if "/tools/video-editor" not in page:
        raise RuntimeError("The published editor page was not built")
    app_identity = _git_source(app, expected_sha)
    config_path = evidence / "browser-input.json"
    result_path = evidence / "browser-result.json"
    # No tokens, private credential paths or owner secrets are in the browser config.
    config = {**config, "app": str(app), "result": str(result_path),
              "origin": hosted.ORIGIN, "door": DOOR_BASE,
              "browserAppSha": app_identity["sha"]}
    with hosted.Intercept(page, python=version["python"], types=types) as intercept:
        config["spki"] = intercept.leaf_spki
        journey.write_json(config_path, config)
        journey.run(["node", journey.ROOT / "scripts" / "journey_mixed_version_browser.mjs",
                     config_path], evidence / "browser.log", timeout=900)
        result = journey.read_json(result_path)
        ok = result.get("status") == "pass" and result.get("mode") == config["mode"]
        visited = [hit for hit in intercept.hits()
                   if hit.get("path") == hosted.PAGE_ENTRY_ROUTE and hit.get("status") == 200]
        if not ok or not visited:
            raise RuntimeError("Hosted mixed App did not finish an authentic browser step "
                               + step_name + ": " + str(result.get("error", "missing hosted page")))
        served = {"editor_page_requests": len(visited),
                  "browser_app_sha": app_identity["sha"],
                  "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                  "build_sha256": hashlib.sha256((dist / "build.json").read_bytes()).hexdigest()}
    if intercept.reverted.get("clean") is not True:
        raise RuntimeError("Mixed-version intercept did not restore hosts and system trust")
    return {"step": step_name, "result": result, "hosted": served,
            "intercept_reverted": True}


def _candidate(args, root, data, evidence):
    if args.mode != "direct" or args.source_override or args.diagnostic_keep_going:
        raise RuntimeError("Mixed-version release proof requires frozen direct installed sources")
    if sys.platform != "linux" or args.shell != "sh":
        raise RuntimeError("Mixed-version hosted browser rows require native Linux")
    if args.job_id != CASES.get(args.leg):
        raise RuntimeError("Mixed-version job ID does not match its isolated case")
    app = args.app_checkout.resolve(strict=True)
    candidate_path, manifest = journey.selected_manifest(
        app, source_override=False, agent=None, runtime=None, evidence=evidence)
    candidate = journey.read_json(args.candidate)
    if gate.validate_tuple(candidate, app, candidate_path) != manifest:
        raise RuntimeError("Mixed-version candidate App is not the frozen manifest")
    if manifest.get("release") != 14:
        raise RuntimeError("FIX-17 requires the eventual Release 14 App manifest, not an inferred pin")
    source, previous_manifest, admission = jumps.release13_admission(
        args, root.parent / "release13-source")
    if (admission.get("app_commit") != jumps.RELEASE13_APP_COMMIT
            or admission.get("identity") != jumps.RELEASE13_IDENTITY):
        raise RuntimeError("Historical R13 admission did not match the published source")
    return app, candidate_path, manifest, candidate, source, previous_manifest, admission


def _installed(root: Path, data: Path, manifest: dict, *, release: int) -> tuple[Path, Path, dict, dict]:
    version_path, selected_data, version = journey.installation(root, data)
    if jumps.installed_release(version) != release:
        raise RuntimeError("Selected installed version is not release " + str(release))
    selected = journey.selected_tuple(manifest, version)
    service = journey.assert_direct_service_ownership(root, selected_data)
    live = journey.healthy(selected_data)
    if (live.get("identity") or {}).get("release") != release:
        raise RuntimeError("Installed native Runtime health reports the wrong release")
    return version_path, selected_data, version, {"selected": selected, "service": service, "health": live}


def execute(args, root: Path, data: Path, evidence: Path, sentinel, birth: str) -> dict:
    evidence.mkdir(parents=True, exist_ok=True)
    mode = args.leg
    result = {
        "schema_version": 1, "receipt_kind": mode, "job_id": args.job_id,
        "mode": args.mode, "scenario": mode, "status": "not-run",
        "os_build": platform.platform(), "shell": args.shell, **journey.runner_identity(),
        "source_override_test_mode": False,
        "candidate_checkouts": getattr(args, "candidate_checkouts", None),
        "service_manager_prerequisite": getattr(args, "service_manager_prerequisite", None),
        "run_roots": {"program": str(root), "data": str(data)},
        "sources": None, "installed_tuple": None, "import_origins": None,
        "release13_source": None, "browser_app_sha": None, "browser_steps": [],
        "release_transition": None, "live_runtimes": None,
        "service_manager": None,
        "sentinel": {"pid": sentinel.pid, "birth": birth},
        "steps": [], "checks": {key: "not-run" for key in REQUIRED_CHECKS[mode]},
    }
    path = evidence / "journey-result.json"
    journey.write_json(path, result)
    try:
        with stage(result, path, "candidate_and_pinned_source"):
            app, candidate_path, candidate_manifest, candidate, historical_app, historical_manifest, admitted = (
                _candidate(args, root, data, evidence))
            result["sources"] = {"app": journey.git_source(app),
                                 "agent": candidate_manifest["agent"], "runtime": candidate_manifest["runtime"]}
            result["release13_source"] = admitted
            result["checks"]["frozen_candidate"] = "pass"
            result["checks"]["pinned_r13"] = "pass"
            old_checkout = args.release13_app_checkout.resolve(strict=True)
            new_dist = _dist(app, args.mixed_candidate_dist)
            old_dist = _dist(old_checkout, args.mixed_release13_dist)
            old_manifest = journey.read_json(historical_manifest)

        with stage(result, path, "native_r13_install"):
            jumps.bootstrap_release13(historical_app, historical_manifest, args.shell,
                                      evidence / "r13-bootstrap", root, data,
                                      agent=jumps.release9_consent_agent(args))
            jumps.start_direct_runtime(evidence / "r13-bootstrap")
            journey.wait_ready(data)
            old_path, old_data, old, old_info = _installed(
                root, data, old_manifest, release=13)
            result["checks"]["r13_install"] = "pass"

        with stage(result, path, "authored_workspace_seed"):
            seed = jumps.live_workspace(root, data, evidence / "r13-authoring",
                                        "seed_authored_timeline", {
                                            "project": "fix17-mixed-" + mode,
                                            "generation": mode,
                                        })
            if seed.get("authored") is not True or seed.get("initial_label") != "V1":
                raise RuntimeError("Published R13 did not persist a canonical editor document")
            result["seed"] = seed
            result["checks"]["canonical_seed"] = "pass"

        if mode == OLD_APP_NEW_RUNTIME:
            with stage(result, path, "r14_native_update"):
                update = jumps.candidate_update(app, candidate_path, args.shell,
                                                evidence / "r14-update", root, data)
                result["update"] = update
                receipt = jumps.run_receipt(data)
                if update.get("exit_code") != 0 or not isinstance(receipt, dict) or (
                        receipt.get("kind"), receipt.get("exit_code"), receipt.get("still"),
                        receipt.get("target")) != ("updated", 0, 14, 14):
                    raise RuntimeError("Candidate install --update did not report updated release 14")
                result["checks"]["r14_update"] = "pass"
                version_path, selected_data, version, installed = _installed(
                    root, data, candidate_manifest, release=14)
                if version_path == old_path:
                    raise RuntimeError("Candidate did not activate a new installed Runtime version")
                result["release_transition"] = {
                    "from": 13, "to": 14, "source": "candidate-install-update"}
                result["checks"]["r14_installed"] = "pass"
        else:
            version_path, selected_data, version, installed = old_path, old_data, old, old_info
            result["release_transition"] = {
                "from": 13, "to": 13, "source": "live-website-with-no-update"}

        with stage(result, path, "installed_source_and_owner"):
            selected = installed["selected"]
            result["installed_tuple"] = {
                "app": candidate["app"] if mode == OLD_APP_NEW_RUNTIME else jumps.RELEASE13_APP_COMMIT,
                **selected}
            result["service_manager"] = installed["service"]
            result["live_runtimes"] = jumps.live_runtime_owners(data)
            if len(result["live_runtimes"]) != 1 or (
                    result["live_runtimes"][0]["data_root"] != str(selected_data)):
                raise RuntimeError("Cross-version row has zero, multiple or foreign Runtime owners")
            verify_dir = evidence / "installed-verify"
            verify_dir.mkdir(parents=True, exist_ok=True)
            journey.verify("install", version_path, selected_data, verify_dir)
            result["import_origins"] = (
                journey.read_json(verify_dir / "verify-install.json").get("details") or {}).get("origin.imports")
            if not result["import_origins"]:
                raise RuntimeError("Installed import origins are absent")
            result["checks"]["native_owner"] = "pass"

        if mode == NEW_APP_OLD_RUNTIME:
            with stage(result, path, "r14_app_over_live_r13"):
                run = _browser({
                    "mode": "r14-app-r13-installed",
                    "projectId": seed["project"], "projectName": seed["project_name"],
                    "timelineId": seed["timeline"], "initialLabel": seed["initial_label"],
                    "savedLabel": SAVE_LABEL,
                }, app=app, dist=new_dist, version=version,
                   evidence=evidence / "r14-app-on-r13", step_name="r14-save-refuse", expected_sha=candidate["app"])
                result["browser_steps"].append(run)
                observed = run["result"]
                if (not observed.get("realRuntime") or not observed.get("realInstalledEdit")
                        or observed.get("reopened") is not True):
                    raise RuntimeError("R14 App could not save/reopen using the un-updated R13 Runtime")
                result["checks"]["real_browser_save"] = "pass"
                result["checks"]["save_reopen"] = "pass"
                refused = observed.get("rejectedHttp") or {}
                if (refused.get("status") != 413 or refused.get("injected") is not True
                        or refused.get("count") != 1 or observed.get("fix16Card") is not True):
                    raise RuntimeError("FIX-16 refusal card did not appear after the single controlled 413")
                result["checks"]["fix16_rejected_card"] = "pass"
                if (observed.get("refusedLabelPreserved") is not True
                        or not any(item.get("intents", 0) > 0 or item.get("pending")
                                   for item in observed.get("outboxAfter", []))):
                    raise RuntimeError("FIX-16 silently lost the rejected edit or durable outbox")
                result["checks"]["rejected_outbox_retained"] = "pass"
                result["browser_app_sha"] = candidate["app"]
        else:
            with stage(result, path, "r14_creative_authoring"):
                authored = _browser({
                    "mode": "r14-author-r14-installed",
                    "projectId": seed["project"], "projectName": seed["project_name"],
                    "timelineId": seed["timeline"], "initialLabel": seed["initial_label"],
                    "savedLabel": AUTHOR_LABEL,
                }, app=app, dist=new_dist, version=version,
                   evidence=evidence / "r14-author", step_name="r14-author", expected_sha=candidate["app"])
                result["browser_steps"].append(authored)
                if (not authored["result"].get("realRuntime")
                        or not authored["result"].get("realInstalledEdit")):
                    raise RuntimeError("Candidate R14 App did not create an accepted R14-authored edit")
                if authored["result"].get("reopened") is not True:
                    raise RuntimeError("Candidate R14 creative edit was not reopened")
                result["checks"]["r14_authored"] = "pass"
                result["checks"]["r14_save_reopen"] = "pass"
            with stage(result, path, "r13_app_reads_r14_realm"):
                older = _browser({
                    "mode": "r13-app-r14-installed",
                    "projectId": seed["project"], "projectName": seed["project_name"],
                    "timelineId": seed["timeline"], "initialLabel": seed["initial_label"],
                    "savedLabel": AUTHOR_LABEL,
                }, app=old_checkout, dist=old_dist, version=version,
                   evidence=evidence / "r13-app-on-r14", step_name="r13-legacy-read",
                   expected_sha=jumps.RELEASE13_APP_COMMIT)
                result["browser_steps"].append(older)
                observed = older["result"]
                if not (observed.get("legacyAppOpened") is True
                        and observed.get("legacyQueueRead") is True
                        and observed.get("reopened") is True
                        and observed.get("editorPosts") == 0):
                    raise RuntimeError("Published R13 App could not read/reopen the R14-authored realm")
                result["browser_app_sha"] = jumps.RELEASE13_APP_COMMIT
                result["checks"]["r13_legacy_open"] = "pass"
                result["checks"]["r13_legacy_queue_read"] = "pass"
                result["checks"]["r13_legacy_reopen"] = "pass"

        with stage(result, path, "native_postcheck_and_sentinel"):
            journey.assert_direct_service_ownership(root, selected_data)
            health = journey.healthy(selected_data)
            expected_release = 13 if mode == NEW_APP_OLD_RUNTIME else 14
            if (health.get("identity") or {}).get("release") != expected_release:
                raise RuntimeError("Mixed browser changed the installed Runtime release")
            result["checks"]["native_end"] = "pass"
            settled = journey.sentinel_observation(sentinel, birth)
            result["sentinel"]["at_settlement"] = settled
            if not settled["alive"]:
                raise RuntimeError("Mixed browser disturbed the unrelated sentinel")
            result["checks"]["unrelated_sentinel"] = "pass"
        result["status"] = "pass"
    except Exception as error:
        result.update(status="fail", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        journey.write_json(path, result)
    return result
