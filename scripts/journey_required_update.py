#!/usr/bin/env python3
"""Run the R13 required-update and R13 realm-reopen rows on an installed Linux Runtime."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import platform
import shutil

if __package__:
    from . import check_journey_evidence as gate
    from . import ci_journey as journey
    from . import journey_hosted_update as hosted
    from . import journey_version_jumps as jumps
    from .journey_matrix import FIX17_PROMPT_LEG, FIX17_REALM_LEG, validate_job
else:
    import check_journey_evidence as gate
    import ci_journey as journey
    import journey_hosted_update as hosted
    import journey_version_jumps as jumps
    from journey_matrix import FIX17_PROMPT_LEG, FIX17_REALM_LEG, validate_job


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
    try:
        actual = supplied.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("Hosted App build is absent from its exact pinned checkout") from exc
    if supplied.is_symlink() or actual != checkout.resolve(strict=True) / "dist":
        raise RuntimeError("Hosted App build does not belong to the checked immutable source")
    if any(not (actual / name).is_file() for name in ("index.html", "build.json")):
        raise RuntimeError("Hosted App did not produce its own hosted index and build receipt")
    return actual


def _browser(config, *, app, expected_app, dist, release_app, manifest, python, evidence):
    """Serve the selected editor build and publication independently over hosted TLS."""
    _git_source(app, expected_app)
    evidence.mkdir(parents=True)
    dist = _dist(app, dist)
    build = dist / "build.json"
    if journey.read_json(build).get("app") != expected_app:
        raise RuntimeError("Hosted build.json does not identify the expected App commit")
    routes, types = hosted.page_routes(dist)
    routes.update(hosted.release_routes(release_app, manifest.read_bytes()))
    result_path = evidence / "browser-result.json"
    config_path = evidence / "browser-input.json"
    with hosted.Intercept(routes, python=python, types=types) as server:
        saved_config = {
            **config, "diagnostic": __import__("os").environ.get("ASTRID_FIX17_DIAGNOSTIC") == "1",
            "origin": hosted.ORIGIN, "result": str(result_path), "spki": server.leaf_spki,
        }
        private_probe = config.get("mode") in {"publicationProbe", "skewProbe"}
        if private_probe:
            saved_config = {key: value for key, value in saved_config.items()
                            if key not in {"profileDir", "publicationManifestPath", "r13ManifestPath",
                                           "runtimeHealthUrl"}}
        journey.write_json(config_path, saved_config)
        runtime_config_path = config_path
        if private_probe:
            # Keep local profile and staged manifest paths out of the saved
            # diagnostic record; the transient config is removed after execution.
            saved_config["profileDir"] = config["profileDir"]
            saved_config["publicationManifestPath"] = config["publicationManifestPath"]
            if config.get("r13ManifestPath"):
                saved_config["r13ManifestPath"] = config["r13ManifestPath"]
            runtime_config_path = evidence / "browser-input-runtime.json"
            journey.write_json(runtime_config_path, saved_config)
        try:
            journey.run(["node", journey.ROOT / "scripts/journey_required_update_browser.mjs", runtime_config_path],
                        evidence / "browser.log", timeout=420)
        except RuntimeError:
            if result_path.is_file():
                failure = journey.read_json(result_path)
                raise RuntimeError(f"{config['mode']} browser: {failure.get('error', 'failed')}") from None
            raise
        finally:
            if runtime_config_path != config_path:
                runtime_config_path.unlink(missing_ok=True)
        observed = journey.read_json(result_path)
        expected_status = "non-proof" if config.get("mode") in {
            "publicationProbe", "recoveryProbe", "skewProbe",
        } else "pass"
        if observed.get("status") != expected_status or observed.get("mode") != config["mode"]:
            raise RuntimeError("Hosted App did not finish " + config["mode"])
        hits = server.hits()
        if not any(hit["path"] == hosted.PAGE_ENTRY_ROUTE and hit["status"] == 200 for hit in hits):
            raise RuntimeError("No successful hosted editor page request")
        # Intercept.preflight binds the installed Python to these same manifest bytes.
        manifest_reads = sum(hit["path"] == hosted.MANIFEST_ROUTE and hit["status"] == 200
                             and hit["method"] == "GET" for hit in hits)
        if config["mode"] == "prompt" and manifest_reads < 2:
            raise RuntimeError("No browser read of the published R14 manifest")
    if server.reverted.get("clean") is not True:
        raise RuntimeError("The hosted intercept did not restore hosts and system trust")
    return {
        **observed, "app": expected_app, "manifest_sha256": gate.digest(manifest),
        "build_sha256": gate.digest(build), "manifest_reads": manifest_reads,
        "intercept_reverted": True,
    }


def _installed(root, data, manifest, release):
    path, selected_data, version = journey.installation(root, data)
    selected = journey.selected_tuple(manifest, version)
    ownership = journey.assert_direct_service_ownership(root, selected_data)
    health = journey.healthy(selected_data)
    if (selected_data != data or jumps.installed_release(version) != release
            or (health.get("identity") or {}).get("release") != release):
        raise RuntimeError(f"Expected the native Runtime and install for Release {release}")
    return path, version, selected, ownership


def execute(args, root: Path, data: Path, evidence: Path, sentinel, birth: str) -> dict:
    mode = args.leg
    if (args.source_override or args.shell != "sh"
            or validate_job(args.job_id, args.mode)["leg"] != mode or args.mode != "direct"):
        raise RuntimeError("This row requires a frozen native Linux direct installation")
    evidence.mkdir(parents=True, exist_ok=True)
    result_path = evidence / "journey-result.json"
    profile = evidence / "_browser-profile"
    profile.mkdir(mode=0o700)
    diagnostic = __import__("os").environ.get("ASTRID_FIX17_DIAGNOSTIC") == "1"
    diagnostic_path = evidence / "browser-diagnostic.json"
    diagnostic_legs = []

    def record_leg(project: str, leg: str, state: str, *, error: str | None = None,
                   skip_reason: str | None = None, evidence_path: str | None = None):
        item = {"project": project, "leg": leg, "state": state}
        if error:
            item["failure"] = error
        if skip_reason:
            item["skip_reason"] = skip_reason
        if evidence_path:
            item["evidence"] = evidence_path
        diagnostic_legs.append(item)
        journey.write_json(diagnostic_path, {
            "schema_version": 1, "evidence_class": "NON-PROOF",
            "source_commit": jumps.RELEASE13_APP_COMMIT, "legs": diagnostic_legs,
        })

    result = {
        "schema_version": 1, "receipt_kind": mode, "job_id": args.job_id,
        "mode": args.mode, "scenario": mode, "status": "not-run",
        "os_build": platform.platform(), "shell": args.shell, **journey.runner_identity(),
        "source_override_test_mode": False,
        **({"evidence_class": "NON-PROOF"} if diagnostic else {}),
        **({"diagnostic_legs": diagnostic_legs} if diagnostic else {}),
        "candidate_checkouts": args.candidate_checkouts,
        "service_manager_prerequisite": args.service_manager_prerequisite,
        "run_roots": {"program": str(root), "data": str(data)},
        "sentinel": {"pid": sentinel.pid, "birth": birth}, "steps": [], "timelines": [],
    }
    try:
        with stage(result, result_path, "pinned_sources"):
            app = args.app_checkout.resolve(strict=True)
            old_app = args.release13_app_checkout.resolve(strict=True)
            path, manifest = journey.selected_manifest(
                app, source_override=False, agent=None, runtime=None, evidence=evidence)
            candidate = journey.read_json(args.candidate)
            if gate.validate_tuple(candidate, app, path) != manifest or not gate.required_release14(manifest):
                raise RuntimeError("The candidate manifest must declare Release 14 and required 14 with a reason")
            source, old_path, result["release13_source"] = jumps.release13_admission(
                args, root.parent / "release13-source")
            result["sources"] = {
                "app": journey.git_source(app), "agent": manifest["agent"], "runtime": manifest["runtime"],
            }

        with stage(result, result_path, "native_r13_install"):
            jumps.bootstrap_release13(source, old_path, args.shell, evidence / "r13-bootstrap", root, data,
                                      agent=jumps.release9_consent_agent(args))
            jumps.start_direct_runtime(evidence / "r13-bootstrap")
            journey.wait_ready(data)
            original, old_version, result["installed_r13"], _ = _installed(
                root, data, journey.read_json(old_path), 13)

        with stage(result, result_path, "r13_authoring"):
            # The native direct owner has one realm. Enumerate its entire API scope
            # in the realm row and author independent projects in that same realm.
            count = 2 if mode == FIX17_REALM_LEG else 1
            for number in range(1, count + 1):
                slug = f"fix17-project-{number}"
                try:
                    seed = jumps.live_workspace(root, data, evidence / slug, "seed_authored_timeline",
                                                {"project": slug, "generation": slug})
                except Exception as exc:
                    record_leg(slug, "seed", "fail", error=f"{type(exc).__name__}: {exc}")
                    if not diagnostic:
                        raise
                    if mode == FIX17_PROMPT_LEG:
                        record_leg(slug, "required_prompt", "unavailable",
                                   skip_reason="No real seeded R13 project was available to open")
                        record_leg(slug, "queued_draft_preservation", "unavailable",
                                   skip_reason="No real seeded project or authentic queued draft exists")
                    record_leg(slug, "r14_reopen", "unavailable",
                               skip_reason="No real seeded project exists to reopen")
                    continue
                config = {
                    "profileDir": str(profile), "browserDriverApp": str(app),
                    "projectId": seed["project"], "projectName": seed["project_name"],
                    "timelineId": seed["timeline"], "initialLabel": seed["initial_label"],
                    "savedLabel": f"FIX17 saved edit {number}", "draftLabel": f"FIX17 draft {number}",
                }
                author = None
                try:
                    author = _browser({**config, "mode": "author"},
                                      app=old_app, expected_app=jumps.RELEASE13_APP_COMMIT,
                                      dist=args.release13_browser_dist, release_app=source, manifest=old_path,
                                      python=old_version["python"], evidence=evidence / (slug + "-r13"))
                    record_leg(slug, "r13_authoring", "pass", evidence_path=slug + "-r13/browser-result.json")
                except Exception as exc:
                    record_leg(slug, "r13_authoring", "fail", error=f"{type(exc).__name__}: {exc}",
                               evidence_path=slug + "-r13/browser-result.json")
                    if not diagnostic:
                        raise
                timeline = {"config": {key: config[key] for key in
                            ("projectId", "projectName", "timelineId", "savedLabel", "draftLabel")}}
                if author is not None:
                    timeline["author"] = author
                else:
                    timeline["preservation_unavailable"] = "R13 authoring did not produce a real queued draft"
                result["timelines"].append(timeline)
                if diagnostic and mode == FIX17_PROMPT_LEG:
                    # Keep publication observation in a disposable profile so
                    # its empty outbox cannot replace or consume the R13 draft.
                    probe_evidence = evidence / (slug + "-publication-probe")
                    probe_profile = evidence / ("_" + slug + "-publication-profile")
                    shutil.rmtree(probe_profile, ignore_errors=True)
                    probe_profile.mkdir(mode=0o700)
                    try:
                        observed = _browser({
                            **config, "mode": "publicationProbe",
                            "profileDir": str(probe_profile), "diagnostic": True,
                            "publicationManifestPath": str(path.resolve(strict=True)),
                            "r13ManifestPath": str(old_path.resolve(strict=True)),
                            "expectedAppCommit": jumps.RELEASE13_APP_COMMIT,
                        }, app=old_app, expected_app=jumps.RELEASE13_APP_COMMIT,
                            dist=args.release13_browser_dist, release_app=source, manifest=old_path,
                            python=old_version["python"], evidence=probe_evidence)
                        result.setdefault("publication_probes", []).append(observed)
                        record_leg(slug, "publication_probe", "observed",
                                   evidence_path=slug + "-publication-probe/browser-result.json")
                    except Exception as exc:
                        record_leg(slug, "publication_probe", "unavailable",
                                   error=f"{type(exc).__name__}: sanitized diagnostic failure",
                                   evidence_path=slug + "-publication-probe/browser-result.json")
                    finally:
                        shutil.rmtree(probe_profile, ignore_errors=True)
                        result.setdefault("publication_probe_profiles_removed", []).append(
                            not probe_profile.exists())
                if mode == FIX17_PROMPT_LEG:
                    try:
                        with stage(result, result_path, "required_prompt"):
                            result["prompt"] = _browser({
                                **config, "mode": "prompt", "diagnostic": diagnostic,
                                **({"expectedDraft": author["draft"]} if author else {}),
                                "requiredReason": manifest["required"]["reason"],
                            }, app=old_app, expected_app=jumps.RELEASE13_APP_COMMIT,
                                dist=args.release13_browser_dist, release_app=app, manifest=path,
                                python=old_version["python"], evidence=evidence / (
                                    (slug + "-required-prompt") if diagnostic else "required-prompt"))
                        prompt_evidence = (slug + "-required-prompt") if diagnostic else "required-prompt"
                        record_leg(slug, "required_prompt", "pass", evidence_path=prompt_evidence + "/browser-result.json")
                        if author is None:
                            record_leg(slug, "queued_draft_preservation", "unavailable",
                                       skip_reason="R13 authoring failed; no authentic queued draft exists")
                    except Exception as exc:
                        record_leg(slug, "required_prompt", "fail", error=f"{type(exc).__name__}: {exc}")
                        if not diagnostic:
                            raise
            if mode == FIX17_REALM_LEG:
                result["realm_before"] = jumps.live_workspace(
                    root, data, evidence / "before", "realm_inventory", {})

        with stage(result, result_path, "native_r14_update"):
            result["update"] = jumps.candidate_update(app, path, args.shell, evidence / "r14-update", root, data)
            receipt = jumps.run_receipt(data)
            result["update_receipt"] = receipt
            if result["update"]["exit_code"] != 0 or not isinstance(receipt, dict) or (
                    receipt.get("kind"), receipt.get("exit_code"), receipt.get("still"), receipt.get("target")
            ) != ("updated", 0, 14, 14):
                raise RuntimeError("Candidate install --update did not report updated release 14")
            installed, version, selected, ownership = _installed(root, data, manifest, 14)
            if installed == original:
                raise RuntimeError("Candidate did not activate a new installed version")
            result["release_transition"] = {"from": 13, "to": 14, "source": "candidate-install-update"}
            result["installed_tuple"] = {"app": candidate["app"], **selected}
            result["service_manager"] = ownership
            verify = evidence / "verify-r14"
            verify.mkdir()
            journey.verify("install", installed, data, verify)
            result["import_origins"] = (
                journey.read_json(verify / "verify-install.json").get("details") or {}).get("origin.imports")
            if not result["import_origins"]:
                raise RuntimeError("Installed import origins are absent")

            # Observe the pinned R13 App against the live candidate Runtime in
            # its own profile, independently of project authoring or seeding.
            if diagnostic and mode == FIX17_PROMPT_LEG:
                probe_evidence = evidence / "r13-app-r14-runtime-skew"
                skew_profile = evidence / "_r13-app-r14-runtime-profile"
                shutil.rmtree(skew_profile, ignore_errors=True)
                skew_profile.mkdir(mode=0o700)
                try:
                    runtime_health = journey.healthy(data)
                    health_identity = runtime_health.get("identity") or {}
                    observed = _browser({
                        "mode": "skewProbe", "profileDir": str(skew_profile),
                        "browserDriverApp": str(old_app),
                        "publicationManifestPath": str(path.resolve(strict=True)),
                        "runtimeHealthUrl": journey.HEALTH_URL,
                        "expectedAppCommit": jumps.RELEASE13_APP_COMMIT,
                        "servedAppCommit": jumps.RELEASE13_APP_COMMIT,
                        "expectedRuntimeRelease": health_identity.get("release"),
                        "expectedSchemaDigest": runtime_health.get("schema_digest"),
                    }, app=old_app, expected_app=jumps.RELEASE13_APP_COMMIT,
                        dist=args.release13_browser_dist, release_app=app, manifest=path,
                        python=version["python"], evidence=probe_evidence)
                    result["skew_probe"] = observed
                    record_leg("runtime-skew", "r13_app_r14_runtime_skew", "observed",
                               evidence_path="r13-app-r14-runtime-skew/browser-result.json")
                except Exception as exc:
                    record_leg("runtime-skew", "r13_app_r14_runtime_skew", "unavailable",
                               error=f"{type(exc).__name__}: sanitized diagnostic failure",
                               evidence_path="r13-app-r14-runtime-skew/browser-result.json")
                finally:
                    shutil.rmtree(skew_profile, ignore_errors=True)
                    result["skew_browser_profile_removed"] = not skew_profile.exists()

        with stage(result, result_path, "r14_reopen"):
            if mode == FIX17_REALM_LEG:
                result["realm_after"] = jumps.live_workspace(
                    root, data, evidence / "after", "realm_inventory", {})
                if result["realm_before"] != result["realm_after"]:
                    raise RuntimeError("An R13 project, timeline or published history changed during the update")
            for number, timeline in enumerate(result["timelines"], start=1):
                if "author" not in timeline:
                    record_leg(timeline["config"]["projectId"], "r14_reopen", "unavailable",
                               skip_reason="No authentic R13 saved edit and queued draft to reopen")
                    continue
                config = timeline["config"]
                try:
                    timeline["reopen"] = _browser({
                        **config, "mode": "reopen", "expectedDraft": timeline["author"]["draft"],
                        "requireDraftUI": mode == FIX17_PROMPT_LEG,
                        "profileDir": str(profile), "browserDriverApp": str(app),
                    }, app=app, expected_app=candidate["app"], dist=args.candidate_browser_dist,
                        release_app=app, manifest=path, python=version["python"], evidence=evidence / f"reopen-{number}")
                    record_leg(config["projectId"], "r14_reopen", "pass", evidence_path=f"reopen-{number}/browser-result.json")
                except Exception as exc:
                    record_leg(config["projectId"], "r14_reopen", "fail", error=f"{type(exc).__name__}: {exc}")
                    if not diagnostic:
                        raise

        if diagnostic and mode == FIX17_PROMPT_LEG:
            # Run only after the ordinary fresh-document R14 reopen observation.
            # The browser probe permits the selected exact pending intent only.
            for number, timeline in enumerate(result["timelines"], start=1):
                config = timeline["config"]
                reopen = timeline.get("reopen")
                if not timeline.get("author") or not reopen:
                    record_leg(config["projectId"], "exact_draft_recovery", "unavailable",
                               skip_reason="No completed authentic R14 reopen evidence and scoped R13 intent")
                    continue
                probe_evidence = evidence / f"recovery-probe-{number}"
                realm_before = None
                realm_before_error = None
                try:
                    realm_before = jumps.live_workspace(
                        root, data, evidence / f"recovery-probe-{number}-realm-before", "realm_inventory", {})
                except Exception as exc:
                    realm_before_error = type(exc).__name__
                try:
                    observed = _browser({
                        **config, "mode": "recoveryProbe", "profileDir": str(profile),
                        "browserDriverApp": str(app), "expectedDraft": timeline["author"]["draft"],
                    }, app=app, expected_app=candidate["app"], dist=args.candidate_browser_dist,
                        release_app=app, manifest=path, python=version["python"], evidence=probe_evidence)
                    realm_after = None
                    realm_after_error = None
                    try:
                        realm_after = jumps.live_workspace(
                            root, data, evidence / f"recovery-probe-{number}-realm-after", "realm_inventory", {})
                    except Exception as exc:
                        realm_after_error = type(exc).__name__
                    realm_stable = False
                    if realm_before is not None and realm_after is not None:
                        before_other = deepcopy(realm_before)
                        after_other = deepcopy(realm_after)
                        target_present = True
                        for inventory in (before_other, after_other):
                            target_project = inventory.get("projects", {}).get(config["projectId"])
                            if target_project is None or config["timelineId"] not in target_project.get("timelines", {}):
                                target_present = False
                                continue
                            target_project["timelines"].pop(config["timelineId"])
                        before_digest = hashlib.sha256(json.dumps(
                            before_other, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                        after_digest = hashlib.sha256(json.dumps(
                            after_other, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                        realm_stable = target_present and before_digest == after_digest
                    else:
                        before_digest = after_digest = None
                    observed["realm_inventory"] = {
                        "otherProjectsAndTimelinesStable": realm_stable,
                        "beforeSha256": before_digest, "afterSha256": after_digest,
                        "failure": realm_before_error or realm_after_error,
                    }
                    if not realm_stable and observed.get("step", {}).get("state") == "pass":
                        observed["step"].update(state="fail", failure={
                            "kind": "scope_ambiguity", "code": "realm_inventory_changed_or_unavailable"})
                    result.setdefault("recovery_probes", []).append(observed)
                    state = "pass" if observed.get("step", {}).get("state") == "pass" else "fail"
                    failure = observed.get("step", {}).get("failure") or {}
                    failure_label = str(failure.get("kind", "recovery_not_proven"))
                    if failure.get("code"):
                        failure_label += ":" + str(failure["code"])
                    record_leg(config["projectId"], "exact_draft_recovery", state,
                               error=None if state == "pass" else failure_label,
                               evidence_path=f"recovery-probe-{number}/browser-result.json")
                except Exception as exc:
                    record_leg(config["projectId"], "exact_draft_recovery", "fail",
                               error=f"{type(exc).__name__}: diagnostic probe unavailable",
                               evidence_path=f"recovery-probe-{number}/browser-result.json")

        with stage(result, result_path, "native_end"):
            _installed(root, data, manifest, 14)
            result["live_runtimes"] = jumps.live_runtime_owners(data)
            if len(result["live_runtimes"]) != 1 or result["live_runtimes"][0]["data_root"] != str(data):
                raise RuntimeError("The update did not settle on exactly one owned Runtime")
            settled = journey.sentinel_observation(sentinel, birth)
            result["sentinel"]["at_settlement"] = settled
            if not settled["alive"]:
                raise RuntimeError("The update disturbed the unrelated sentinel")
        result["status"] = "non-proof" if diagnostic else "pass"
    except Exception as exc:
        result.update(status="fail", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        try:
            shutil.rmtree(profile)
            result["browser_profile_removed"] = True
        except OSError:
            result.update(status="fail", browser_profile_removed=False)
            raise
        finally:
            journey.write_json(result_path, result)
    return result
