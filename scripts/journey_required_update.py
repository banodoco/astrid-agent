#!/usr/bin/env python3
"""Run the R13 required-update browser rows on an installed Linux Runtime."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import os
from pathlib import Path
import platform
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time

if __package__:
    from . import check_journey_evidence as gate
    from . import ci_journey as journey
    from . import journey_hosted_update as hosted
    from . import journey_version_jumps as jumps
    from .journey_matrix import FIX17_PROMPT_LEG, FIX17_REALM_LEG, FIX17_RELOAD_INFLIGHT_LEG, validate_job
else:
    import check_journey_evidence as gate
    import ci_journey as journey
    import journey_hosted_update as hosted
    import journey_version_jumps as jumps
    from journey_matrix import FIX17_PROMPT_LEG, FIX17_REALM_LEG, FIX17_RELOAD_INFLIGHT_LEG, validate_job


@contextmanager
def stage(result: dict, result_path: Path, name: str):
    entry = {"stage": name, "state": "running"}
    result["steps"].append(entry)
    journey.write_json(result_path, result)
    try:
        yield entry
    except Exception as error:
        entry.update(state="fail", error=f"{type(error).__name__}: {error}")
        if isinstance(error, jumps._Fix17ChildFailure):
            entry.update(error.receipt())
        journey.write_json(result_path, result)
        raise
    if entry["state"] == "running":
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
    allowed_config = {"mode", "profileDir", "browserDriverApp", "projectId", "projectName", "timelineId",
                      "initialLabel", "savedLabel", "draftLabel", "expectedDraft", "requiredReason",
                      "initialManifestB64"}
    if set(config) - allowed_config:
        raise ValueError("Unexpected browser configuration fields")
    manifest_sha = gate.digest(manifest)
    with (
        TemporaryDirectory(prefix="fix17-browser-") as private,
        hosted.Intercept(routes, python=python, types=types) as server,
    ):
        config_path = Path(private) / "input.json"
        journey.write_json(config_path, {**config, "origin": hosted.ORIGIN, "result": str(result_path),
                                        "spki": server.leaf_spki, "manifestSha256": manifest_sha})
        argv = ["node", journey.ROOT / "scripts/journey_required_update_browser.mjs", config_path]
        try:
            jumps._fix17_subprocess(argv, evidence / "browser-stdout.log", evidence / "browser-stderr.log",
                                    timeout=420)
        except jumps._Fix17ChildFailure as error:
            if not result_path.is_file():
                raise
            observed = journey.read_json(result_path)
            if observed.get("status") != "fail" or observed.get("mode") != config["mode"]:
                raise
            observed.update(error.receipt())
        else:
            observed = journey.read_json(result_path)
        if observed.get("status") not in {"pass", "fail"} or observed.get("mode") != config["mode"]:
            raise RuntimeError("Hosted App did not return a result for " + config["mode"])
        hits = server.hits()
        if observed["status"] == "pass" and not any(
                hit["path"] == hosted.PAGE_ENTRY_ROUTE and hit["status"] == 200 for hit in hits):
            raise RuntimeError("No successful hosted editor page request")
        # Intercept.preflight binds the installed Python to these same manifest bytes.
        manifest_reads = sum(hit["path"] == hosted.MANIFEST_ROUTE and hit["status"] == 200
                             and hit["method"] == "GET" for hit in hits)
        if config["mode"] == "publication" and observed["status"] == "pass" and manifest_reads < 2:
            raise RuntimeError("No browser read of the published R14 manifest")
    if server.reverted.get("clean") is not True:
        raise RuntimeError("The hosted intercept did not restore hosts and system trust")
    return {
        **observed, "app": expected_app, "manifest_sha256": manifest_sha,
        "build_sha256": gate.digest(build), "manifest_reads": manifest_reads,
        "intercept_reverted": True,
    }


def _reload_inflight(config, *, app, old_app, expected_app, old_dist, new_dist, release13_source,
                     old_manifest, new_manifest, old_python, evidence, update_and_verify):
    """Keep the real R13 editor alive while the installed Runtime changes and routes flip to R14."""
    _git_source(old_app, jumps.RELEASE13_APP_COMMIT)
    _git_source(app, expected_app)
    old_dist, new_dist = _dist(old_app, old_dist), _dist(app, new_dist)
    if journey.read_json(old_dist / "build.json").get("app") != jumps.RELEASE13_APP_COMMIT:
        raise RuntimeError("R13 browser build identity mismatch")
    if journey.read_json(new_dist / "build.json").get("app") != expected_app:
        raise RuntimeError("R14 browser build identity mismatch")
    old_routes, old_types = hosted.page_routes(old_dist)
    old_routes.update(hosted.release_routes(release13_source, old_manifest.read_bytes()))
    new_routes, new_types = hosted.page_routes(new_dist)
    new_routes.update(hosted.release_routes(app, new_manifest.read_bytes()))
    types = {**old_types, **new_types}
    evidence.mkdir(parents=True, exist_ok=True)
    result_path = evidence / "browser-result.json"
    with TemporaryDirectory(prefix="fix17-reload-inflight-") as private:
        private = Path(private)
        held, published = private / "held", private / "published"
        config_path = private / "input.json"
        with hosted.Intercept(old_routes, python=old_python, types=types,
                              secondary_routes=new_routes, activate=published) as server:
            payload = {**config, "origin": hosted.ORIGIN, "result": str(result_path),
                       "spki": server.leaf_spki, "manifestSha256": gate.digest(old_manifest),
                       "expectedApp": expected_app, "initialApp": jumps.RELEASE13_APP_COMMIT,
                       "initialBuildSha256": gate.digest(old_dist / "build.json"),
                       "candidateBuildSha256": gate.digest(new_dist / "build.json"),
                       "candidateIndexSha256": gate.digest(new_dist / "index.html"),
                       "heldPath": str(held), "publishedPath": str(published)}
            journey.write_json(config_path, payload)
            argv = ["node", journey.ROOT / "scripts/journey_reload_inflight_browser.mjs", config_path]
            child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 300
                while not held.exists():
                    if child.poll() is not None:
                        out, err = child.communicate(timeout=5)
                        (evidence / "browser-stdout.log").write_text(out, encoding="utf-8")
                        (evidence / "browser-stderr.log").write_text(err, encoding="utf-8")
                        raise RuntimeError("Browser child exited before holding its R13 save")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Browser child did not signal held R13 save")
                    time.sleep(.2)
                held_save = journey.read_json(held)
                if (held_save.get("held") is not True or held_save.get("release_after_reload") is not False
                        or not all(gate.DIGEST.fullmatch(str(held_save.get(name, "")))
                                   for name in ("body_sha256", "key_sha256"))):
                    raise RuntimeError("Browser did not signal a held, scoped save")
                output = update_and_verify()
                temp_marker = published.with_name("published.tmp")
                temp_marker.write_text("published\n", encoding="utf-8")
                os.replace(temp_marker, published)
                try:
                    out, err = child.communicate(timeout=240)
                except subprocess.TimeoutExpired:
                    raise TimeoutError("Browser child did not finish after R14 publication")
                (evidence / "browser-stdout.log").write_text(out, encoding="utf-8")
                (evidence / "browser-stderr.log").write_text(err, encoding="utf-8")
                if child.returncode:
                    raise RuntimeError(f"Browser child exited {child.returncode}")
                observed = journey.read_json(result_path)
                if (observed.get("mode") != FIX17_RELOAD_INFLIGHT_LEG
                        or observed.get("status") not in {"pass", "fail"}):
                    raise RuntimeError("Browser child did not produce a reload-inflight result")
                if observed.get("held_save") != held_save:
                    raise RuntimeError("Final browser receipt changed the held R13 request")
                hits = server.hits()
                observed["https"] = {
                    "initial_build_sha256": payload["initialBuildSha256"],
                    "candidate_build_sha256": payload["candidateBuildSha256"],
                    "candidate_index_sha256": payload["candidateIndexSha256"],
                    "candidate_manifest_sha256": gate.digest(new_manifest),
                    "candidate_build_served": any(hit.get("phase") == "secondary" and hit["path"] == "/build.json"
                                                   and hit["status"] == 200 for hit in hits),
                    "candidate_page_served": any(hit.get("phase") == "secondary" and hit["path"] == hosted.PAGE_ENTRY_ROUTE
                                                  and hit["status"] == 200 for hit in hits),
                }
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=10)
        if server.reverted.get("clean") is not True:
            raise RuntimeError("The hosted intercept did not restore hosts and system trust")
        observed["intercept_reverted"] = True
        if isinstance(observed.get("author"), dict):
            observed["author"]["intercept_reverted"] = True
        return observed, output


def _installed(root, data, manifest, release):
    path, selected_data, version = journey.installation(root, data)
    selected = journey.selected_tuple(manifest, version)
    ownership = journey.assert_direct_service_ownership(root, selected_data)
    health = journey.healthy(selected_data)
    if (selected_data != data or jumps.installed_release(version) != release
            or (health.get("identity") or {}).get("release") != release):
        raise RuntimeError(f"Expected the native Runtime and install for Release {release}")
    return path, version, selected, ownership


def _reopen_catalog(root: Path, data: Path, evidence: Path) -> dict:
    catalog_path = data / "runtime" / "catalog.json"
    if catalog_path.is_symlink():
        raise RuntimeError("Run-owned Runtime catalog is a symlink")
    catalog = journey.read_json(catalog_path)
    entries = catalog.get("realms")
    if not isinstance(entries, list) or not entries or any(
            not isinstance(entry, dict) or not isinstance(entry.get("realm_id"), str)
            or not entry["realm_id"].strip() for entry in entries):
        raise RuntimeError("Run-owned Runtime catalog does not list valid realm IDs")
    ids = sorted(entry["realm_id"] for entry in entries)
    live = jumps.live_workspace(root, data, evidence, "realm_inventory", {})
    # R13 (and this R14 path) opens one selected realm. Require that the live
    # realm covers the entire catalog. This refuses any unopened catalog entry.
    if (ids != [live.get("realm_id")] or ids != [catalog.get("selected_realm_id")]
            or Path(entries[0].get("data_root", "")) != journey.workspace_root(data)):
        raise RuntimeError("Live timelines and history did not cover every ID in the Runtime catalog")
    return {**live, "realm_ids": ids}


def execute(args, root: Path, data: Path, evidence: Path, sentinel, birth: str) -> dict:
    mode = args.leg
    if (args.source_override or args.shell != "sh"
            or validate_job(args.job_id, args.mode)["leg"] != mode or args.mode != "direct"):
        raise RuntimeError("This row requires a frozen native Linux direct installation")
    evidence.mkdir(parents=True, exist_ok=True)
    result_path = evidence / "journey-result.json"
    profile = evidence / "_browser-profile"
    profile.mkdir(mode=0o700)
    result = {
        "schema_version": 1, "receipt_kind": mode, "job_id": args.job_id,
        "mode": args.mode, "scenario": mode, "status": "not-run",
        "os_build": platform.platform(), "shell": args.shell, **journey.runner_identity(),
        "source_override_test_mode": False,
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
            count = 2 if mode == FIX17_REALM_LEG else 1
            for number in range(1, count + 1):
                slug = f"fix17-project-{number}"
                seed = jumps.live_workspace(root, data, evidence / slug, "seed_authored_timeline",
                                            {"project": slug, "generation": slug})
                config = {
                    "profileDir": str(profile), "browserDriverApp": str(app),
                    "projectId": seed["project"], "projectName": seed["project_name"],
                    "timelineId": seed["timeline"], "initialLabel": seed["initial_label"],
                    "savedLabel": f"FIX17 saved edit {number}", "draftLabel": f"FIX17 draft {number}",
                }
                if mode == FIX17_PROMPT_LEG:
                    publication = _browser({
                        **config, "mode": "publication",
                        "requiredReason": manifest["required"]["reason"],
                        "initialManifestB64": base64.b64encode(old_path.read_bytes()).decode("ascii"),
                    }, app=old_app, expected_app=jumps.RELEASE13_APP_COMMIT,
                        dist=args.release13_browser_dist, release_app=app, manifest=path,
                        python=old_version["python"], evidence=evidence / "required-prompt")
                    result["prompt"] = {**publication, "mode": "prompt"}
                    author = {
                        **(publication.get("author") or {}), "app": jumps.RELEASE13_APP_COMMIT,
                        "manifest_sha256": result["release13_source"]["manifest_sha256"],
                        "build_sha256": publication["build_sha256"], "intercept_reverted": True,
                    }
                elif mode != FIX17_RELOAD_INFLIGHT_LEG:
                    author = _browser({**config, "mode": "author"},
                                      app=old_app, expected_app=jumps.RELEASE13_APP_COMMIT,
                                      dist=args.release13_browser_dist, release_app=source, manifest=old_path,
                                      python=old_version["python"], evidence=evidence / (slug + "-r13"))
                else:
                    author = {"status": "pending", "app": jumps.RELEASE13_APP_COMMIT,
                              "manifest_sha256": result["release13_source"]["manifest_sha256"]}
                result["timelines"].append({
                    "config": {key: config[key] for key in
                               ("projectId", "projectName", "timelineId", "savedLabel", "draftLabel")},
                    "author": author,
                })
                if mode != FIX17_RELOAD_INFLIGHT_LEG and author.get("status") != "pass":
                    raise RuntimeError("R13 App did not author the original scoped saved and unsaved edits: "
                                       + str(author.get("error") or (result.get("prompt") or {}).get("error")
                                             or "missing evidence"))
            if mode == FIX17_REALM_LEG:
                result["realm_before"] = _reopen_catalog(root, data, evidence / "before")
        if mode == FIX17_PROMPT_LEG:
            with stage(result, result_path, "required_prompt") as entry:
                if result["prompt"].get("status") != "pass":
                    entry.update(state="fail", error=result["prompt"].get("error", "Required prompt evidence absent"))

        with stage(result, result_path, "native_r14_update"):
            def update_and_verify():
                result["update"] = jumps.candidate_update(app, path, args.shell, evidence / "r14-update", root, data)
                receipt = jumps.run_receipt(data)
                result["update_receipt"] = receipt
                if result["update"]["exit_code"] != 0 or not isinstance(receipt, dict) or (
                        receipt.get("kind"), receipt.get("exit_code"), receipt.get("still"), receipt.get("target")
                ) != ("updated", 0, 14, 14):
                    raise RuntimeError("Candidate install --update did not report updated release 14")
                installed_now, version_now, selected_now, ownership_now = _installed(root, data, manifest, 14)
                if installed_now == original:
                    raise RuntimeError("Candidate did not activate a new installed version")
                result["release_transition"] = {"from": 13, "to": 14, "source": "candidate-install-update"}
                result["installed_tuple"] = {"app": candidate["app"], **selected_now}
                result["service_manager"] = ownership_now
                verify_now = evidence / "verify-r14"
                verify_now.mkdir()
                journey.verify("install", installed_now, data, verify_now)
                result["import_origins"] = (journey.read_json(verify_now / "verify-install.json").get("details") or {}).get("origin.imports")
                if not result["import_origins"]:
                    raise RuntimeError("Installed import origins are absent")
                return installed_now, version_now, selected_now, ownership_now

            if mode == FIX17_RELOAD_INFLIGHT_LEG:
                timeline = result["timelines"][0]
                browser_config = {**config, "mode": FIX17_RELOAD_INFLIGHT_LEG}
                observed, update_state = _reload_inflight(
                    browser_config, app=app, old_app=old_app, expected_app=candidate["app"],
                    old_dist=args.release13_browser_dist, new_dist=args.candidate_browser_dist,
                    release13_source=source, old_manifest=old_path, new_manifest=path,
                    old_python=old_version["python"], evidence=evidence / "r13-reload-inflight",
                    update_and_verify=update_and_verify)
                timeline["reload_inflight"] = observed
                timeline["author"] = observed.get("author", {})
                installed, version, selected, ownership = update_state
            else:
                installed, version, selected, ownership = update_and_verify()

        with stage(result, result_path, "r14_reopen") as entry:
            if mode == FIX17_REALM_LEG:
                result["realm_after"] = _reopen_catalog(root, data, evidence / "after")
                if result["realm_before"] != result["realm_after"]:
                    raise RuntimeError("An R13 project, timeline or published history changed during the update")
            if mode == FIX17_RELOAD_INFLIGHT_LEG and observed["status"] != "pass":
                entry.update(state="fail", error=observed.get("error", "Guarded Reload did not restore the draft"))
            timelines_to_retry = () if mode == FIX17_RELOAD_INFLIGHT_LEG else result["timelines"]
            for number, timeline in enumerate(timelines_to_retry, start=1):
                config = timeline["config"]
                timeline["reopen"] = _browser({
                    **config, "mode": "reopen", "expectedDraft": timeline["author"]["draft"],
                    "profileDir": str(profile), "browserDriverApp": str(app),
                }, app=app, expected_app=candidate["app"], dist=args.candidate_browser_dist,
                    release_app=app, manifest=path, python=version["python"], evidence=evidence / f"reopen-{number}")
                reopened = timeline["reopen"]
                if reopened["status"] != "pass":
                    entry.update(state="fail", error=reopened.get("error", "Draft Retry did not finish"))
                    continue
                reopened["accepted_readback"] = jumps.live_workspace(
                    root, data, evidence / f"readback-{number}", "reopen_browser_composition", {
                        "project_id": config["projectId"], "timeline_id": config["timelineId"],
                        "revision_id": reopened["retry"]["revision"],
                        "publication_id": reopened["retry"]["publication"], "label": config["draftLabel"],
                    })

        with stage(result, result_path, "native_end"):
            _installed(root, data, manifest, 14)
            result["live_runtimes"] = jumps.live_runtime_owners(data)
            if len(result["live_runtimes"]) != 1 or result["live_runtimes"][0]["data_root"] != str(data):
                raise RuntimeError("The update did not settle on exactly one owned Runtime")
            settled = journey.sentinel_observation(sentinel, birth)
            result["sentinel"]["at_settlement"] = settled
            if not settled["alive"]:
                raise RuntimeError("The update disturbed the unrelated sentinel")
        failed = [entry["stage"] for entry in result["steps"] if entry["state"] != "pass"]
        if failed:
            raise RuntimeError("Failed browser qualification stages: " + ", ".join(failed))
        result["status"] = "pass"
    except Exception as exc:
        partial = evidence / "r13-reload-inflight" / "browser-result.json"
        if (mode == FIX17_RELOAD_INFLIGHT_LEG and partial.is_file() and result.get("timelines")
                and "reload_inflight" not in result["timelines"][0]):
            try:
                result["timelines"][0]["reload_inflight"] = journey.read_json(partial)
                observed_author = result["timelines"][0]["reload_inflight"].get("author")
                if isinstance(observed_author, dict):
                    result["timelines"][0]["author"] = observed_author
            except Exception:
                result["timelines"][0]["reload_inflight"] = {"status": "fail", "error": "partial browser receipt unreadable"}
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
