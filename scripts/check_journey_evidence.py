#!/usr/bin/env python3
"""Fail closed on missing or mismatched installed-product journey evidence."""
import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

if __package__:
    from .journey_matrix import (
        GPU_MATRIX,
        HOSTED_BUTTON_JOB_IDS,
        END_STATE_INVENTORY,
        M4_4A_BACKEND_MATRIX,
        M4_4A_BACKEND_SIDECARS,
        M4_4A_OLD_LAUNCHER_REF,
        M4_4A_OLD_UPDATE_SHA256,
        M4_4A_PROVISIONAL_AGENT_REF,
        M4_4A_REQUIRED_AGENT_ANCESTORS,
        M4_4A_REQUIRED_EVENTS,
        M4_4B_LOCAL_SMOKE,
        M4_6_MATRIX,
        M4_6_SLOT_IDS,
        RELEASE1_JOB_IDS,
        RELEASE1_MATRIX,
        RELEASE1_AUTOMATIC_MATRIX,
        RELEASE1_1_DEFERRED_PINOKIO_MATRIX,
        RELEASE1_1_PINOKIO_MATRIX,
    )
else:
    try:
        from journey_matrix import (
            GPU_MATRIX,
            HOSTED_BUTTON_JOB_IDS,
            END_STATE_INVENTORY,
            M4_4A_BACKEND_MATRIX,
            M4_4A_BACKEND_SIDECARS,
            M4_4A_OLD_LAUNCHER_REF,
            M4_4A_OLD_UPDATE_SHA256,
            M4_4A_PROVISIONAL_AGENT_REF,
            M4_4A_REQUIRED_AGENT_ANCESTORS,
            M4_4A_REQUIRED_EVENTS,
            M4_4B_LOCAL_SMOKE,
            M4_6_MATRIX,
            M4_6_SLOT_IDS,
            RELEASE1_JOB_IDS,
            RELEASE1_MATRIX,
            RELEASE1_AUTOMATIC_MATRIX,
            RELEASE1_1_DEFERRED_PINOKIO_MATRIX,
            RELEASE1_1_PINOKIO_MATRIX,
        )
    except ModuleNotFoundError:  # importlib-loaded unit tests run from the repository root
        from scripts.journey_matrix import (
            GPU_MATRIX,
            HOSTED_BUTTON_JOB_IDS,
            END_STATE_INVENTORY,
            M4_4A_BACKEND_MATRIX,
            M4_4A_BACKEND_SIDECARS,
            M4_4A_OLD_LAUNCHER_REF,
            M4_4A_OLD_UPDATE_SHA256,
            M4_4A_PROVISIONAL_AGENT_REF,
            M4_4A_REQUIRED_AGENT_ANCESTORS,
            M4_4A_REQUIRED_EVENTS,
            M4_4B_LOCAL_SMOKE,
            M4_6_MATRIX,
            M4_6_SLOT_IDS,
            RELEASE1_JOB_IDS,
            RELEASE1_MATRIX,
            RELEASE1_AUTOMATIC_MATRIX,
            RELEASE1_1_DEFERRED_PINOKIO_MATRIX,
            RELEASE1_1_PINOKIO_MATRIX,
        )

SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
ARTIFACTS = ("public/install", "public/install.sh", "public/install.ps1")
RELEASE0_SUMS_SHA256 = "58221a50aa7940e3121d6239ddeb92d6409df3711096aac1161c7ec130b755c0"
RELEASE9_APP_COMMIT = "546c91b7aaf90835445136eb6e93459385ffe21c"
RELEASE9_IDENTITY = {
    "agent": "709492fa5f4850b9caf0cea4a4c543023b73fa0a",
    "runtime": "f4c7a4623c84da077daac3e3a9366fc2e6a55051",
    "schema_digest": "sha256:9628bb5fd6e1dbbec392dd39f2e01031622dcf52d7ccff7cd1f0ad10743dcb1c",
}
# The served Release 11 starts the release11-then-update leg; the candidate's
# install --update must take it to Release 12 (the pins live in journey_version_jumps).
RELEASE9_LEG = "release9-then-install"
RELEASE9_CHECKS = ("release", "service", "health", "semantic_data", "task_settled", "unrelated_sentinel")
RELEASE11_LEG = "release11-then-update"
RELEASE11_APP_COMMIT = "57e200b45615a59181920192697a932400d985fc"
RELEASE11_RELEASE = 11
RELEASE11_TARGET_RELEASE = 12
RELEASE11_IDENTITY = {
    "agent": "a105de3a4852cb61b9647dfe2c920118bd29f272",
    "runtime": "4f5477cb0f5b03d28bec991a1fe34edcd8fc309f",
    "schema_digest": "sha256:9628bb5fd6e1dbbec392dd39f2e01031622dcf52d7ccff7cd1f0ad10743dcb1c",
}
RELEASE11_CHECKS = ("release11_install", "update_exit", "run_receipt", "release", "one_runtime", "service",
                    "health", "semantic_data", "task_settled", "unrelated_sentinel")
AUTOSTART_LEG = "fresh-install-autostart-off"
AUTOSTART_CHECKS = ("install_exit", "selection_autostart_false", "update_status_autostart_off",
                    "post_update_refused_unchanged", "autostart_card_capture", "service", "health",
                    "unrelated_sentinel")
RELEASE0_BOOTSTRAP_SHA256 = {
    "install": "100bab1deb15d620abd910c8fb0c9a6d77f4945428a3229dac6b506705e3fd0d",
    "install.ps1": "bba3f2edc41c047cb51475ceeac4489a04610af73a3af44bdc05287964d28258",
}
RUNNER_PROVENANCE = {
    "runpod": ("Linux", "runpod"),
    "ubuntu-24.04": ("Linux", "ubuntu24"),
    "ubuntu-22.04": ("Linux", "ubuntu22"),
    "windows-2022": ("Windows", "win22"),
    "macos-15": ("macOS", "macos15"),
}


def _migrate_macos_receipt_key(contract):
    """Compatibility for existing receipt fixtures; the matrix now owns this key."""
    return contract


REQUIRED = {
    # Release candidates are no longer a loose list of legs.  They are the
    # exact five direct jobs and must use --aggregate-release1.
    "release-candidate": RELEASE1_JOB_IDS,
    "weekly": ("fresh-install", "update-from-release:pre-updater"),
}


def read(path):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError(f"duplicate JSON field {key}")
            obj[key] = value
        return obj

    obj = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(obj, dict):
        raise ValueError(f"{path}: expected object")
    return obj


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_tuple(candidate, app, manifest):
    for key in ("app", "agent", "runtime", "pinokio"):
        if not isinstance(candidate.get(key), str) or not SHA.fullmatch(candidate[key]):
            raise ValueError(f"candidate {key} must be a full commit SHA")
    if not DIGEST.fullmatch(str(candidate.get("manifest_sha256", ""))):
        raise ValueError("candidate manifest_sha256 must be a SHA-256")
    if digest(manifest) != candidate["manifest_sha256"]:
        raise ValueError("manifest hash differs from frozen candidate")
    actual = subprocess.check_output(["git", "-C", str(app), "rev-parse", "HEAD"], text=True).strip()
    if actual != candidate["app"]:
        raise ValueError("App checkout differs from frozen candidate")
    artifacts = candidate.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(ARTIFACTS):
        raise ValueError("candidate must pin all three bootstrap artifact hashes")
    for path in ARTIFACTS:
        if not DIGEST.fullmatch(str(artifacts[path])) or digest(Path(app) / path) != artifacts[path]:
            raise ValueError(f"artifact hash mismatch: {path}")
    manifest_data = read(manifest)
    for key in ("agent", "runtime"):
        if (manifest_data.get(key) or {}).get("sha") != candidate[key]:
            raise ValueError(f"manifest {key} differs from frozen candidate")
    return manifest_data


def evaluate(candidate, manifest, results, required, expected_runner, expected_shell, allow_override=False):
    errors = []
    by_name = {}
    for path in Path(results).rglob("journey-result.json"):
        try:
            row = read(path)
            name = row.get("scenario")
            if not isinstance(name, str) or name in by_name:
                errors.append(f"{path}: missing or duplicate scenario")
            else:
                by_name[name] = row
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
    for name in required:
        row = by_name.get(name)
        if row is None:
            errors.append(f"{name}: missing")
            continue
        if row.get("status") != "pass":
            errors.append(f"{name}: status {row.get('status')!r} is not pass")
        override = row.get("source_override_test_mode")
        if not isinstance(override, bool):
            errors.append(f"{name}: override mode is unlabelled")
        elif override and not allow_override:
            errors.append(f"{name}: source override is not admissible")
        if row.get("runner_image") != expected_runner:
            errors.append(f"{name}: runner image label does not match {expected_runner}")
        provenance = row.get("runner_provenance")
        expected_os = (RUNNER_PROVENANCE[expected_runner][0]
                       if expected_runner != "local"
                       else {"Darwin": "macOS"}.get(platform.system(), platform.system()))
        if expected_runner != "local":
            if not isinstance(provenance, dict):
                errors.append(f"{name}: runner provenance is missing")
            else:
                if provenance.get("RUNNER_OS") != expected_os:
                    errors.append(f"{name}: runner OS provenance does not match {expected_os}")
                expected_image_os = RUNNER_PROVENANCE[expected_runner][1]
                if provenance.get("ImageOS") != expected_image_os:
                    errors.append(f"{name}: ImageOS provenance does not match {expected_image_os}")
                for field in ("ImageOS", "ImageVersion"):
                    if not isinstance(provenance.get(field), str) or not provenance[field].strip():
                        errors.append(f"{name}: runner {field} provenance is missing")
        os_build = row.get("os_build")
        prefix = expected_os + "-"
        if not isinstance(os_build, str) or not os_build.startswith(prefix):
            errors.append(f"{name}: OS build does not match {expected_runner}")
        if row.get("shell") != expected_shell:
            errors.append(f"{name}: shell does not match {expected_shell}")
        if row.get("final_cleanup") != "pass":
            errors.append(f"{name}: final cleanup did not pass")
        installed = row.get("installed_tuple") or {}
        sources = row.get("sources") or {}
        for key in ("app", "agent", "runtime"):
            source = sources.get(key) or {}
            if installed.get(key) != candidate[key] or source.get("sha") != candidate[key]:
                errors.append(f"{name}: {key} differs from frozen candidate")
        for key in ("agent", "runtime"):
            if not override and sources.get(key) != manifest.get(key):
                errors.append(f"{name}: {key} source differs from manifest")
        if installed.get("schema_digest") != (manifest.get("runtime") or {}).get("schema_digest"):
            errors.append(f"{name}: runtime schema digest differs from manifest")
        expected_tools = {k: v.get("version") for k, v in manifest.get("tools", {}).items()
                          if isinstance(v, dict) and v.get("version")}
        if installed.get("tools") != expected_tools:
            errors.append(f"{name}: tool versions differ from manifest")
        if not row.get("import_origins"):
            errors.append(f"{name}: import origins absent")
        for check in ("task_settled", "workspace_content", "unrelated_sentinel"):
            if (row.get("checks") or {}).get(check) != "pass":
                errors.append(f"{name}: {check} did not pass")
    return errors


def _release_step_errors(job_id, label, release_from, release_to):
    """The row lands on exactly published + 1; the published release is whatever the row installed."""
    if (any(type(value) is not int or value < 0 for value in (release_from, release_to))
            or release_to != release_from + 1):
        return [f"{job_id}: {label} release transition is invalid"]
    return []


def _hosted_update_errors(job_id, update, release_to):
    """The update was the hosted page's request, served from the intercepted stand-in release."""
    hosted = update.get("hosted")
    if not isinstance(hosted, dict):
        return [f"{job_id}: hosted Update request receipt is absent"]
    request = hosted.get("request") if isinstance(hosted.get("request"), dict) else {}
    served = hosted.get("served") if isinstance(hosted.get("served"), dict) else {}
    final = hosted.get("final") if isinstance(hosted.get("final"), dict) else {}
    errors = []
    if (hosted.get("route") != "hosted-button" or request.get("method") != "POST"
            or request.get("path") != "/api/runtime/v1/update"
            or request.get("origin") != "https://astrid.haus" or request.get("content_length") != 0
            or request.get("authorization") is not False or request.get("cookie") is not False
            or request.get("status") != 202):
        errors.append(f"{job_id}: hosted Update request was not the page's credential-free POST")
    if (served.get("/install-manifest.json", 0) < 1
            or served.get("/install", 0) + served.get("/install.ps1", 0) < 1):
        errors.append(f"{job_id}: hosted Update did not fetch the stand-in release")
    if final.get("outcome") != "updated" or final.get("target_release") != release_to:
        errors.append(f"{job_id}: hosted Update did not end as updated at the stand-in release")
    if hosted.get("intercept_reverted") is not True:
        errors.append(f"{job_id}: hosted Update intercept was not reverted")
    if job_id in HOSTED_BUTTON_JOB_IDS:
        errors.extend(_hosted_button_errors(job_id, hosted))
    return errors


def _hosted_button_errors(job_id, hosted):
    """The request came from a browser clicking the hosted App build's own Update now."""
    page = hosted.get("page") if isinstance(hosted.get("page"), dict) else {}
    request = hosted.get("request") if isinstance(hosted.get("request"), dict) else {}
    headers = request.get("headers")
    errors = []
    if hosted.get("driver") != "app-button" or page.get("offered") is not True or page.get("clicked") is not True:
        errors.append(f"{job_id}: hosted Update was not the page's Update now clicked in a browser")
    if type(page.get("served_manifest")) is not int or page["served_manifest"] < 1:
        errors.append(f"{job_id}: the page did not read the stand-in release it offered")
    if not DIGEST.fullmatch(str(page.get("build_sha256", ""))):
        errors.append(f"{job_id}: the hosted App build identity is absent")
    if (not isinstance(headers, list) or "origin" not in headers
            or {"authorization", "cookie"} & set(headers)):
        errors.append(f"{job_id}: the page's Update request headers are absent or carry a credential")
    return errors


def _windows_fresh_update_errors(update, job_id):
    errors = []
    if not isinstance(update, dict):
        return [f"{job_id}: Windows fresh update receipt is absent"]
    errors.extend(_release_step_errors(job_id, "Windows update", update.get("from_release"), update.get("to_release")))
    errors.extend(_hosted_update_errors(job_id, update, update.get("to_release")))
    if (update.get("marker") != "M-RT UPDATE TEST STAND-IN"
            or not isinstance(update.get("manifest_sha256"), str)
            or not DIGEST.fullmatch(update["manifest_sha256"])):
        errors.append(f"{job_id}: Windows update stand-in identity is invalid")
    old, current = update.get("old_version"), update.get("current_version")
    if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]+", value)
               and value not in {".", ".."} for value in (old, current)):
        errors.append(f"{job_id}: Windows update version identities are invalid")
    labels = update.get("task_labels")
    if not isinstance(labels, list) or {"name": "Astrid Runtime", "path": "\\"} not in labels:
        errors.append(f"{job_id}: Windows update owned task label is absent")
    services = update.get("service_names")
    if not isinstance(services, list) or any(not isinstance(name, str) for name in services):
        errors.append(f"{job_id}: Windows update service labels are absent")
    checks = update.get("checks") if isinstance(update.get("checks"), dict) else {}
    for check in ("running_before", "owned_task", "healthy", "same_labels", "no_r0_handoff",
                  "workspace", "sentinel", "prior_version"):
        if checks.get(check) != "pass":
            errors.append(f"{job_id}: Windows update {check} did not pass")
    if update.get("outcome") != "updated":
        errors.append(f"{job_id}: Windows update outcome is invalid")
    code = update.get("exit_code")
    if (type(code) is not int or code != 0 or not old or not current or old == current
            or update.get("refusal") is not None or not isinstance(update.get("task_id"), str)
            or not update["task_id"].strip()):
        errors.append(f"{job_id}: Windows update success identity is invalid")
    for check in ("verify_update", "task_settled"):
        if checks.get(check) != "pass":
            errors.append(f"{job_id}: Windows update {check} did not pass")
    return errors


# The receipt kind each Release-1 matrix leg writes. The R0 handoff leg and the
# release11-then-update leg write their own kinds; every other leg writes the shared lifecycle receipt.
RELEASE1_RECEIPT_KINDS = {"r0-handoff-then-update": "r0-handoff-then-update",
                          RELEASE11_LEG: RELEASE11_LEG, AUTOSTART_LEG: AUTOSTART_LEG}
RELEASE1_DEFAULT_RECEIPT_KIND = "release1-installed-lifecycle"


def release1_receipt_kind(contract):
    """The receipt kind a Release-1 matrix row must carry for its leg."""
    return RELEASE1_RECEIPT_KINDS.get(contract.get("leg", "fresh-install"), RELEASE1_DEFAULT_RECEIPT_KIND)


UPGRADE_LEGS = frozenset({"r0-handoff-then-update", RELEASE9_LEG, RELEASE11_LEG})


def _upgrade_release_errors(job_id, contract, row, release):
    """An upgrade row's installed release after the update must be the candidate's release.

    The candidate release is read from the candidate manifest in main(), never from the row. Each
    upgrade leg records the installed release as release_transition.to, which it asserts equals the
    installed release before writing. The r0-handoff and release11 legs also record the live Runtime's
    identity release in health_transition.candidate_live_release.
    """
    if contract.get("leg") not in UPGRADE_LEGS:
        return []
    if type(release) is not int or release < 1:
        return [f"{job_id}: candidate release is absent from the candidate manifest"]
    transition = row.get("release_transition") if isinstance(row.get("release_transition"), dict) else {}
    installed = transition.get("to")
    errors = []
    if installed != release:
        errors.append(f"{job_id}: installed release {installed} is not the candidate release {release}")
    if contract.get("leg") != RELEASE9_LEG:
        health = row.get("health_transition") if isinstance(row.get("health_transition"), dict) else {}
        live = health.get("candidate_live_release")
        if live != release:
            errors.append(f"{job_id}: live Runtime release {live} is not the candidate release {release}")
    return errors


def _autostart_row_errors(job_id, row, os_name):
    """The fresh-install-autostart-off row's named assertions, each read from the leg's own receipt fields."""
    errors = []
    selection = row.get("selection") if isinstance(row.get("selection"), dict) else {}
    if selection.get("choices_autostart") is not False:
        errors.append(f"{job_id}: selection record does not have choices.autostart false")
    status = row.get("update_status") if isinstance(row.get("update_status"), dict) else {}
    if status.get("reason") != "autostart-off":
        errors.append(f"{job_id}: GET /update/status does not report reason autostart-off")
    post = row.get("post_update") if isinstance(row.get("post_update"), dict) else {}
    if not (post.get("refused") is True and post.get("unchanged") is True
            and post.get("reason") == "autostart-off" and type(post.get("status")) is int and post["status"] >= 400):
        errors.append(f"{job_id}: POST update was not refused with autostart-off, "
                      "or it changed the selection, the data or the live Runtime")
    capture = row.get("capture") if isinstance(row.get("capture"), dict) else {}
    checks = row.get("checks") if isinstance(row.get("checks"), dict) else {}
    # The macOS capture is attempted but not gated: it may record not-gated. Linux always gates it.
    not_gated = (os_name == "macos" and checks.get("autostart_card_capture") == "not-gated"
                 and capture.get("status") == "not-gated")
    if not not_gated and (capture.get("device_scale_factor") != 2 or not isinstance(capture.get("path"), str)
                          or not DIGEST.fullmatch(str(capture.get("sha256", "")))):
        errors.append(f"{job_id}: autostart card capture is absent or not at deviceScaleFactor 2")
    for check in AUTOSTART_CHECKS:
        if check == "autostart_card_capture" and not_gated:
            continue
        if checks.get(check) != "pass":
            errors.append(f"{job_id}: {check} assertion did not pass")
    return errors


def _release9_row_errors(job_id, row):
    """The strict release9-then-install row: the plain install of the pinned Release 9, then the candidate.

    The row must show the pinned Release 9 source, a transition from 9 to a later candidate release
    with the candidate's Runtime instance replacing the release-9 one, and semantically identical
    seeded data. Every release9 leg check must pass.
    """
    errors = []
    source = row.get("release9_source")
    if (not isinstance(source, dict) or source.get("app_commit") != RELEASE9_APP_COMMIT
            or source.get("identity") != RELEASE9_IDENTITY):
        errors.append(f"{job_id}: pinned release-9 source identity is absent or wrong")
    transition = row.get("release_transition")
    if (not isinstance(transition, dict) or transition.get("from") != 9
            or type(transition.get("to")) is not int or transition["to"] <= 9
            or transition.get("source") != "pinned-release9-plain-install"):
        errors.append(f"{job_id}: release 9 to candidate transition is absent or wrong")
    health = row.get("health_transition") if isinstance(row.get("health_transition"), dict) else {}
    old_id, new_id = health.get("release9_runtime_instance_id"), health.get("candidate_runtime_instance_id")
    if not (isinstance(old_id, str) and isinstance(new_id, str) and old_id and new_id != old_id):
        errors.append(f"{job_id}: the candidate Runtime instance did not replace the release-9 one")
    semantic = row.get("semantic_data")
    if (not isinstance(semantic, dict) or semantic.get("identical") is not True
            or not all(isinstance(semantic.get(name), str) and DIGEST.fullmatch(semantic[name])
                       for name in ("before_sha256", "after_sha256"))
            or semantic.get("before_sha256") != semantic.get("after_sha256")):
        errors.append(f"{job_id}: semantic data preservation is absent or unequal")
    checks = row.get("checks") if isinstance(row.get("checks"), dict) else {}
    for check in RELEASE9_CHECKS:
        if checks.get(check) != "pass":
            errors.append(f"{job_id}: {check} assertion did not pass")
    return errors


def _release11_row_errors(job_id, row):
    """The release11-then-update row's four assertions, each read from the leg's own receipt fields.

    1. the installed release is 12 (from 11, the pinned Release 11 source, live identity 12);
    2. the candidate's run receipt says updated, exit 0, still and target 12;
    3. exactly one Runtime is live afterwards;
    4. the seeded user data survives (identical semantic digests).
    Every other leg assertion must also pass. Each failure names the missing assertion.
    """
    errors = []
    source = row.get("release11_source")
    if (not isinstance(source, dict) or source.get("app_commit") != RELEASE11_APP_COMMIT
            or source.get("identity") != RELEASE11_IDENTITY):
        errors.append(f"{job_id}: pinned release-11 source identity is absent or wrong")
    transition = row.get("release_transition")
    health = row.get("health_transition") if isinstance(row.get("health_transition"), dict) else {}
    if (not isinstance(transition, dict) or transition.get("from") != RELEASE11_RELEASE
            or transition.get("to") != RELEASE11_TARGET_RELEASE
            or transition.get("source") != "candidate-install-update"
            or health.get("candidate_live_release") != RELEASE11_TARGET_RELEASE):
        errors.append(f"{job_id}: installed release is not {RELEASE11_TARGET_RELEASE} "
                      f"(release {RELEASE11_RELEASE} to {RELEASE11_TARGET_RELEASE}, live identity)")
    update = row.get("update")
    receipt = update.get("receipt") if isinstance(update, dict) else None
    if (not isinstance(update, dict) or update.get("command") != "install --update"
            or update.get("exit_code") != 0 or not isinstance(receipt, dict)
            or receipt.get("kind") != "updated" or receipt.get("exit_code") != 0
            or receipt.get("still") != RELEASE11_TARGET_RELEASE
            or receipt.get("target") != RELEASE11_TARGET_RELEASE):
        errors.append(f"{job_id}: candidate install --update receipt does not say updated to release "
                      f"{RELEASE11_TARGET_RELEASE}")
    live = row.get("live_runtimes")
    roots = row.get("run_roots") if isinstance(row.get("run_roots"), dict) else {}
    data = roots.get("data")
    if (not isinstance(live, list) or len(live) != 1 or not isinstance(live[0], dict)
            or not isinstance(data, str) or live[0].get("data_root") not in {data, data + "-r1"}):
        errors.append(f"{job_id}: exactly one live Runtime is not recorded after the update")
    semantic = row.get("semantic_data")
    if (not isinstance(semantic, dict) or semantic.get("identical") is not True
            or not all(isinstance(semantic.get(name), str) and DIGEST.fullmatch(semantic[name])
                       for name in ("before_sha256", "after_sha256"))
            or semantic.get("before_sha256") != semantic.get("after_sha256")):
        errors.append(f"{job_id}: seeded user data preservation is absent or unequal")
    checks = row.get("checks") if isinstance(row.get("checks"), dict) else {}
    for check in RELEASE11_CHECKS:
        if checks.get(check) != "pass":
            errors.append(f"{job_id}: {check} assertion did not pass")
    return errors


def _release1_row_errors(candidate, contract, row):
    """Validate one Release-1 row for both the aggregate and inventory view."""
    job_id = contract["job_id"]
    errors = []
    if row.get("receipt_kind") != release1_receipt_kind(contract):
        errors.append(f"{job_id}: not a Release-1 lifecycle receipt")
    if row.get("status") != "pass":
        errors.append(f"{job_id}: status {row.get('status')!r} is not pass")
    if row.get("mode") != contract["mode"]:
        errors.append(f"{job_id}: mode does not match the required matrix")
    if row.get("scenario") != contract.get("leg", "fresh-install"):
        errors.append(f"{job_id}: scenario does not match the required matrix leg")
    if row.get("runner_image") != contract["runner"]:
        errors.append(f"{job_id}: runner does not match the required matrix")
    if row.get("shell") != contract["shell"]:
        errors.append(f"{job_id}: shell does not match the required matrix")
    expected_os, expected_image = RUNNER_PROVENANCE[contract["runner"]]
    provenance = row.get("runner_provenance")
    if (not isinstance(provenance, dict) or provenance.get("RUNNER_OS") != expected_os
            or provenance.get("ImageOS") != expected_image
            or not isinstance(provenance.get("ImageVersion"), str)
            or not provenance["ImageVersion"].strip()):
        errors.append(f"{job_id}: hosted runner provenance does not match the required matrix")
    os_build = row.get("os_build")
    if not isinstance(os_build, str) or not os_build.startswith(expected_os + "-"):
        errors.append(f"{job_id}: OS build does not match the required matrix")
    if row.get("source_override_test_mode") is not False:
        errors.append(f"{job_id}: source override is not admissible")
    # Every row ends in an ordinary uninstall, except that a handed-off install may
    # end in the documented uninstall refusal that keeps both data roots.
    handoff_leg = contract.get("leg") == "r0-handoff-then-update"
    cleanup = row.get("final_cleanup")
    if handoff_leg and cleanup == "retained-after-upgrade-refusal":
        refusal = row.get("upgrade_uninstall_refusal")
        if (not isinstance(refusal, dict) or type(refusal.get("exit_code")) is not int
                or refusal.get("exit_code") == 0
                or refusal.get("message") != (
                    "Astrid was upgraded from an earlier version; automatic uninstall after an upgrade "
                    "isn't supported yet. Both data folders were kept.")
                or refusal.get("r0_data_root") != (row.get("run_roots") or {}).get("data")
                or not isinstance(refusal.get("r1_data_root"), str)
                or refusal.get("r1_data_root") == refusal.get("r0_data_root")):
            errors.append(f"{job_id}: required preserved-state uninstall refusal is absent or invalid")
    elif cleanup != "pass":
        errors.append(f"{job_id}: final cleanup is not pass")
    observed = row.get("candidate_checkouts")
    if not isinstance(observed, dict):
        errors.append(f"{job_id}: exact candidate checkout receipt is absent")
    else:
        for name in ("app", "agent", "runtime", "pinokio"):
            if (observed.get(name) or {}).get("sha") != candidate[name]:
                errors.append(f"{job_id}: {name} checkout differs from frozen candidate")
    sources = row.get("sources") or {}
    installed = row.get("installed_tuple") or {}
    for name in ("app", "agent", "runtime"):
        source = sources.get(name) or {}
        if installed.get(name) != candidate[name] or source.get("sha") != candidate[name]:
            errors.append(f"{job_id}: {name} installed/source identity differs from frozen candidate")
    if not row.get("import_origins"):
        errors.append(f"{job_id}: installed import origins are absent")
    prerequisite = row.get("service_manager_prerequisite")
    if not isinstance(prerequisite, dict):
        errors.append(f"{job_id}: service-manager prerequisite receipt is absent")
    elif contract["mode"] == "direct":
        if (prerequisite.get("required") is not True or prerequisite.get("outcome") != "pass"
                or not isinstance(prerequisite.get("version"), str)
                or not prerequisite["version"].strip()
                or prerequisite.get("foreground_fallback") is not False):
            errors.append(f"{job_id}: native service-manager prerequisite did not pass")
        manager = row.get("service_manager")
        if not isinstance(manager, dict) or manager.get("foreground_fallback") is not False:
            errors.append(f"{job_id}: direct service ownership/foreground assertion is absent")
        fresh = contract.get("leg") == "fresh-install"
        if fresh:
            for check in ("task_settled", "workspace_content", "unrelated_sentinel"):
                if (row.get("checks") or {}).get(check) != "pass":
                    errors.append(f"{job_id}: fresh {check} assertion did not pass")
        if job_id in {"direct-linux-fresh", "direct-macos-fresh"}:
            errors.extend(fresh_update_errors(job_id, row.get("update")))
        if job_id == "direct-windows":
            errors.extend(_windows_fresh_update_errors(row.get("update"), job_id))
            refusal = row.get("windows_upgrade_refusal")
            if not isinstance(refusal, dict):
                errors.append(f"{job_id}: separate Windows upgrade refusal receipt is absent")
            else:
                if (refusal.get("receipt_kind") != "windows-upgrade-refusal" or
                        refusal.get("job_id") != job_id or refusal.get("scenario") != "windows-upgrade-refusal" or
                        refusal.get("mode") != "direct" or refusal.get("status") != "pass" or
                        refusal.get("final_cleanup") != "pass" or refusal.get("source_override_test_mode") is not False or
                        refusal.get("runner_image") != row.get("runner_image") or refusal.get("shell") != row.get("shell") or
                        refusal.get("runner_provenance") != row.get("runner_provenance") or refusal.get("os_build") != row.get("os_build") or
                        refusal.get("candidate_checkouts") != row.get("candidate_checkouts")):
                    errors.append(f"{job_id}: refusal receipt identity, cleanup or candidate is invalid")
                observation = refusal.get("refusal") or {}
                if (type(observation.get("exit_code")) is not int or observation["exit_code"] == 0 or
                        observation.get("message") != "Upgrading from an earlier Windows version isn't supported yet. Your data was kept; reinstall to get the new version."):
                    errors.append(f"{job_id}: required refusal and nonzero exit are absent")
                fixture = refusal.get("release0_fixture") or {}
                if (fixture.get("fixture_sums_sha256") != RELEASE0_SUMS_SHA256 or
                        fixture.get("bootstrap_sha256") != RELEASE0_BOOTSTRAP_SHA256 or
                        fixture.get("sources") != {"agent": "3204bb00135bec8ee2d2d3fb21eed5abfceca649", "runtime": "6d53a19070faf63fc15ae1f81417722edc2c8a5f"} or
                        not all(isinstance(fixture.get(name), str) and DIGEST.fullmatch(fixture[name]) for name in ("manifest_sha256", "provenance_sha256"))):
                    errors.append(f"{job_id}: frozen refusal fixture provenance is absent or wrong")
                marker = refusal.get("data_marker") or {}
                if not isinstance(marker.get("sha256"), str) or not DIGEST.fullmatch(marker["sha256"]):
                    errors.append(f"{job_id}: refusal data marker evidence is absent")
                task = refusal.get("scheduled_task") or {}
                if (not all(isinstance(task.get(name), str) and DIGEST.fullmatch(task[name])
                            for name in ("before_sha256", "after_sha256"))
                        or task.get("before_sha256") != task.get("after_sha256")):
                    errors.append(f"{job_id}: refusal scheduled task definition is absent or changed")
                for check in ("refusal", "data_marker", "r0_selected", "scheduled_task"):
                    if (refusal.get("checks") or {}).get(check) != "pass":
                        errors.append(f"{job_id}: refusal {check} assertion did not pass")
        elif contract.get("leg") == AUTOSTART_LEG:
            errors.extend(_autostart_row_errors(job_id, row, contract["os"]))
        elif contract.get("leg") == RELEASE9_LEG:
            errors.extend(_release9_row_errors(job_id, row))
        elif contract.get("leg") == RELEASE11_LEG:
            errors.extend(_release11_row_errors(job_id, row))
        elif not fresh:
            if row.get("scenario") != "r0-handoff-then-update":
                errors.append(f"{job_id}: direct receipt did not run the r0-handoff-then-update leg")
            fixture = row.get("release0_fixture")
            if (not isinstance(fixture, dict) or fixture.get("fixture_sums_sha256") != RELEASE0_SUMS_SHA256
                    or fixture.get("bootstrap_sha256") != RELEASE0_BOOTSTRAP_SHA256
                    or not all(isinstance(fixture.get(name), str) and DIGEST.fullmatch(fixture[name])
                               for name in ("manifest_sha256", "provenance_sha256"))
                    or fixture.get("sources") != {
                        "agent": "3204bb00135bec8ee2d2d3fb21eed5abfceca649",
                        "runtime": "6d53a19070faf63fc15ae1f81417722edc2c8a5f",
                    }):
                errors.append(f"{job_id}: frozen release-0 fixture provenance is absent or wrong")
            source = row.get("handoff_source")
            bootstrap = source.get("bootstrap_sha256") if isinstance(source, dict) else None
            if (not isinstance(source, dict) or source.get("app_commit") != RELEASE9_APP_COMMIT
                    or source.get("identity") != RELEASE9_IDENTITY
                    or not DIGEST.fullmatch(str(source.get("manifest_sha256", "")))
                    or not isinstance(bootstrap, dict) or set(bootstrap) != set(ARTIFACTS)
                    or not all(isinstance(value, str) and DIGEST.fullmatch(value) for value in bootstrap.values())):
                errors.append(f"{job_id}: pinned release-9 handoff installer provenance is absent or wrong")
            handoff = row.get("handoff")
            data_root = (row.get("run_roots") or {}).get("data")
            if (not isinstance(handoff, dict) or handoff.get("phase") != "complete"
                    or handoff.get("release") != 9 or handoff.get("r0_data_root") != data_root
                    or not isinstance(data_root, str) or handoff.get("r1_data_root") != data_root + "-r1"):
                errors.append(f"{job_id}: completed R0 handoff start state is absent or wrong")
            update = row.get("update")
            if (not isinstance(update, dict) or update.get("command") != "install --update"
                    or update.get("exit_code") != 0):
                errors.append(f"{job_id}: candidate install --update did not exit 0")
            transition = row.get("release_transition")
            if (not isinstance(transition, dict) or transition.get("from") != 9
                    or type(transition.get("to")) is not int or transition["to"] <= 9
                    or transition.get("start_state") != "r0-handoff-complete"
                    or transition.get("source") != "candidate-install-update"):
                errors.append(f"{job_id}: handed-off release-9 to candidate transition is absent or wrong")
            health = row.get("health_transition")
            if (not isinstance(health, dict) or not isinstance(transition, dict)
                    or health.get("candidate_live_release") != transition.get("to")):
                errors.append(f"{job_id}: the live Runtime does not report the candidate release")
            semantic = row.get("semantic_data")
            if (not isinstance(semantic, dict) or semantic.get("identical") is not True
                    or not all(isinstance(semantic.get(name), str) and DIGEST.fullmatch(semantic[name])
                               for name in ("before_sha256", "after_sha256"))
                    or semantic.get("before_sha256") != semantic.get("after_sha256")):
                errors.append(f"{job_id}: semantic data preservation is absent or unequal")
            for check in ("handed_off_start", "update_exit", "release", "service", "health",
                          "semantic_data", "task_settled", "unrelated_sentinel"):
                if (row.get("checks") or {}).get(check) != "pass":
                    errors.append(f"{job_id}: {check} assertion did not pass")
    elif (prerequisite.get("outcome") != "explicit-owner"
          or prerequisite.get("managed_by") != "pinokio"
          or prerequisite.get("foreground_fallback") is not False):
        errors.append(f"{job_id}: Pinokio ownership prerequisite is not explicit")
    return errors


def fresh_update_errors(job_id, update):
    """Require a completed, same-label R1 update for the two fresh rows."""
    errors = []
    if not isinstance(update, dict) or update.get("status") != "pass":
        return [f"{job_id}: fresh update receipt is absent or did not pass"]
    errors.extend(_release_step_errors(job_id, "fresh update", update.get("release_from"), update.get("release_to")))
    errors.extend(_hosted_update_errors(job_id, update, update.get("release_to")))
    old, new = update.get("old_version"), update.get("new_version")
    if (not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]+", value)
                for value in (old, new)) or old == new):
        errors.append(f"{job_id}: fresh update version transition is invalid")
    if not DIGEST.fullmatch(str(update.get("manifest_sha256", ""))):
        errors.append(f"{job_id}: fresh update manifest digest is invalid")
    service = update.get("service")
    names = update.get("service_names")
    if (not isinstance(service, dict) or service.get("foreground_fallback") is not False
            or not isinstance(service.get("service_path"), str)
            or not isinstance(names, list) or not names
            or Path(service["service_path"]).name not in names):
        errors.append(f"{job_id}: fresh update service evidence is invalid")
    checks = update.get("checks")
    if not isinstance(checks, dict):
        checks = {}
    for check in ("new_version", "old_version_retained", "same_service", "no_boundary_handoff",
                  "health", "workspace_content", "unrelated_sentinel", "verify_update", "task_settled"):
        if checks.get(check) != "pass":
            errors.append(f"{job_id}: fresh update {check} assertion did not pass")
    return errors


def evaluate_release1_matrix(candidate, results, scope="nine"):
    """Reject an incomplete, skipped, or ownership-ambiguous nine-job receipt set.

    The nine rows are the Release-1 matrix: every one is required, and each is validated
    against its own leg, receipt kind and assertions. A missing row is named.
    """
    if scope not in {"five", "nine"}:
        raise ValueError("Release-1 scope must be five or nine")
    contracts = RELEASE1_AUTOMATIC_MATRIX if scope == "five" else RELEASE1_MATRIX
    errors = []
    expected = {row["job_id"]: row for row in contracts}
    found = {}
    for path in Path(results).rglob("journey-result.json"):
        if path.parent.name == "before-cleanup":
            continue  # Diagnostic snapshot; final receipt is the after-cleanup observation.
        try:
            row = read(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        job_id = row.get("job_id")
        if not isinstance(job_id, str) or job_id not in expected:
            errors.append(f"{path}: unknown or absent Release-1 job id")
        elif job_id in found:
            errors.append(f"{path}: duplicate receipt for {job_id}")
        else:
            found[job_id] = row
    for job_id, contract in expected.items():
        row = found.get(job_id)
        if row is None:
            errors.append(f"{job_id}: missing receipt")
            continue
        errors.extend(_release1_row_errors(candidate, contract, row))
        errors.extend(_upgrade_release_errors(job_id, contract, row, candidate.get("release")))
    return errors


def _sidecar_path(receipt: Path, item: object) -> tuple[Path | None, str | None]:
    if not isinstance(item, dict):
        return None, None
    relative = item.get("path")
    sha = item.get("sha256")
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        return None, None
    if not isinstance(sha, str) or not DIGEST.fullmatch(sha):
        return None, None
    path = (receipt / relative).resolve()
    if not path.is_relative_to(receipt.resolve()):
        return None, None
    return path, sha


def _event_errors(path: Path, expected_os: str, mode: str) -> list[str]:
    errors = []
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            rows.append(json.loads(line))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"events.jsonl is not valid JSONL: {exc}"]
    sequences = [row.get("seq") for row in rows]
    if sequences != list(range(1, len(rows) + 1)):
        errors.append("events.jsonl sequence is not monotonic")
    for row in rows:
        if row.get("schema_version") != 1 or row.get("os") != expected_os or row.get("mode") != mode:
            errors.append("events.jsonl event lacks the stable OS/mode schema")
            break
    names = {row.get("event") for row in rows}
    missing = set(M4_4A_REQUIRED_EVENTS) - names
    if missing:
        errors.append("events.jsonl is missing required events: " + ", ".join(sorted(missing)))
    positions = {name: next((i for i, row in enumerate(rows) if row.get("event") == name), None)
                 for name in names}
    for left, right in (("action_built", "launcher_ref_advanced"),
                        ("launcher_ref_advanced", "first_click_begin"),
                        ("first_click_begin", "launcher_pull_begin"),
                        ("launcher_pull_begin", "launcher_pull_end"),
                        ("ready_sentinel", "installer_end"),
                        ("installer_end", "action_end"),
                        ("action_end", "health_probe_begin"),
                        ("health_probe_begin", "health_probe_pass")):
        if positions.get(left) is not None and positions.get(right) is not None and positions[left] >= positions[right]:
            errors.append(f"events.jsonl order is invalid: {left} must precede {right}")
    return errors


def _sidecar_json(path: Path, *, expected_os: str, mode: str) -> tuple[dict | None, list[str]]:
    try:
        value = read(path)
    except (OSError, ValueError) as exc:
        return None, [f"{path.name} is not a JSON object: {exc}"]
    errors = []
    if value.get("schema_version") != 1 or value.get("os") != expected_os or value.get("mode") != mode:
        errors.append(f"{path.name} lacks the stable OS/mode receipt schema")
    return value, errors


def _evaluate_pinokio_backend_matrix(candidate, results, matrix, gate_name):
    """Check one exact hosted Pinokio backend receipt matrix."""
    errors = []
    expected = {row["job_id"]: row for row in matrix}
    found = {}
    paths = {}
    for path in Path(results).rglob("journey-result.json"):
        if path.parent.name == "before-cleanup":
            continue  # Diagnostic snapshot; final receipt is the after-cleanup observation.
        try:
            row = read(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        job_id = row.get("job_id")
        if job_id not in expected:
            continue
        if job_id in found:
            errors.append(f"{job_id}: duplicate {gate_name} receipt")
        else:
            found[job_id], paths[job_id] = row, path.parent
    for job_id, contract in expected.items():
        row = found.get(job_id)
        if row is None:
            errors.append(f"{job_id}: missing {gate_name} receipt")
            continue
        if row.get("status") != "pass":
            errors.append(f"{job_id}: status {row.get('status')!r} is not pass")
        for key, expected_value in (("schema_version", 2),
                                    ("receipt_kind", "release1-installed-lifecycle"),
                                    ("mode", "pinokio-backend"), ("scenario", "fresh-install"),
                                    ("runner_image", contract["runner"]), ("shell", contract["shell"]),
                                    ("evidence_class", "hosted-lifecycle")):
            if row.get(key) != expected_value:
                errors.append(f"{job_id}: {key} is not {expected_value!r}")
        override = row.get("source_override_test_mode")
        if not isinstance(override, bool):
            errors.append(f"{job_id}: source override is unlabelled")
        elif override:
            errors.append(f"{job_id}: source override is not admitted by this gate")
        if row.get("final_cleanup") != "pass":
            errors.append(f"{job_id}: final cleanup did not pass")
        provenance = row.get("runner_provenance") or {}
        expected_os, expected_image = RUNNER_PROVENANCE[contract["runner"]]
        if (provenance.get("RUNNER_OS") != expected_os or provenance.get("ImageOS") != expected_image
                or not isinstance(provenance.get("ImageVersion"), str) or not provenance["ImageVersion"].strip()):
            errors.append(f"{job_id}: hosted runner provenance is absent or mismatched")
        observed = row.get("candidate_checkouts") or {}
        for name in ("app", "agent", "runtime", "pinokio"):
            if (observed.get(name) or {}).get("sha") != candidate.get(name):
                errors.append(f"{job_id}: candidate {name} checkout differs from frozen tuple")
        owner = row.get("service_manager_prerequisite") or {}
        if (owner.get("outcome") != "explicit-owner" or owner.get("managed_by") != "pinokio"
                or owner.get("foreground_fallback") is not False):
            errors.append(f"{job_id}: Pinokio ownership prerequisite did not pass")
        source_unit = row.get("source_unit_evidence")
        if (not isinstance(source_unit, dict) or source_unit.get("status") != "pass"
                or source_unit.get("kind") != "pinokio-action-array-source-check"
                or source_unit.get("substitute_for_hosted") is not False):
            errors.append(f"{job_id}: source/unit evidence is not visibly separate from hosted evidence")
        backend = row.get("pinokio_backend")
        if not isinstance(backend, dict) or backend.get("schema_version") != 1:
            errors.append(f"{job_id}: M4-4a backend receipt is missing or has the wrong schema")
            continue
        if backend.get("os") != contract["os"] or backend.get("mode") != contract["mode"]:
            errors.append(f"{job_id}: backend phase receipt lacks the required OS/mode")
        if (backend.get("hosted_lifecycle") or {}).get("status") != "pass":
            errors.append(f"{job_id}: hosted lifecycle evidence is missing or not pass")
        old_launcher = backend.get("old_launcher") or {}
        if (old_launcher.get("ref") != M4_4A_OLD_LAUNCHER_REF
                or old_launcher.get("sha256") != M4_4A_OLD_UPDATE_SHA256
                or old_launcher.get("validated") is not True):
            errors.append(f"{job_id}: old-launcher action provenance is absent or unapproved")
        stopped_path = backend.get("stopped_path_agent") or {}
        if (stopped_path.get("ref") != candidate.get("agent")
                or stopped_path.get("ref") == M4_4A_PROVISIONAL_AGENT_REF
                or set(stopped_path.get("required_ancestors") or []) != set(M4_4A_REQUIRED_AGENT_ANCESTORS)
                or stopped_path.get("validated") is not True):
            errors.append(f"{job_id}: integrated stopped-path Agent provenance did not pass")
        sidecars = backend.get("sidecars")
        receipt_dir = paths[job_id]
        sidecar_paths = {}
        if not isinstance(sidecars, dict) or set(sidecars) != set(M4_4A_BACKEND_SIDECARS):
            errors.append(f"{job_id}: required M4-4a sidecars are missing")
        else:
            for name in M4_4A_BACKEND_SIDECARS:
                sidecar, expected_sha = _sidecar_path(receipt_dir, sidecars[name])
                if sidecar is None or not sidecar.is_file():
                    errors.append(f"{job_id}: sidecar {name} is missing or unsafe")
                elif digest(sidecar) != expected_sha:
                    errors.append(f"{job_id}: sidecar {name} hash differs from its receipt")
                else:
                    sidecar_paths[name] = sidecar
            event_ref = sidecars.get("events.jsonl")
            event_path, _ = _sidecar_path(receipt_dir, event_ref)
            if event_path and event_path.is_file():
                errors.extend(f"{job_id}: {error}" for error in _event_errors(event_path, contract["os"], contract["mode"]))
        for name in M4_4A_BACKEND_SIDECARS:
            path = sidecar_paths.get(name)
            if path is None or path.suffix not in {".json", ".jsonl"} or name == "events.jsonl":
                continue
            _, schema_errors = _sidecar_json(path, expected_os=contract["os"], mode=contract["mode"])
            errors.extend(f"{job_id}: {error}" for error in schema_errors)
        first_action = sidecar_paths.get("action-array-first.json")
        if first_action:
            action, action_errors = _sidecar_json(
                first_action, expected_os=contract["os"], mode=contract["mode"])
            errors.extend(f"{job_id}: {error}" for error in action_errors)
            ordered = (action or {}).get("ordered_steps") or []
            shape = [(step.get("kind"), step.get("bootstrap_action"))
                     for step in ordered if isinstance(step, dict)]
            if ((action or {}).get("built_before_ref_advance") is not True
                    or ((action or {}).get("launcher") or {}).get("ref") != M4_4A_OLD_LAUNCHER_REF
                    or shape != [("git-shell", None), ("shell", "update-app")]):
                errors.append(f"{job_id}: first action is not the approved pre-self-pull H1 array")
        retry_action = sidecar_paths.get("action-array-retry.json")
        if retry_action:
            action, action_errors = _sidecar_json(
                retry_action, expected_os=contract["os"], mode=contract["mode"])
            errors.extend(f"{job_id}: {error}" for error in action_errors)
            if ((action or {}).get("launcher") or {}).get("sha") != candidate.get("pinokio"):
                errors.append(f"{job_id}: retry action was not built from the frozen Pinokio candidate")
        first = backend.get("first_click") or {}
        if first.get("outcome") not in {"refused", "succeeded"}:
            errors.append(f"{job_id}: first click has no contract-defined outcome")
        elif first.get("outcome") == "refused":
            if (backend.get("stop_after_refusal") or {}).get("result") != "stopped":
                errors.append(f"{job_id}: refused first click lacks a successful Stop phase")
            if (backend.get("retry_after_stop") or {}).get("result") != "succeeded":
                errors.append(f"{job_id}: refused first click lacks a successful retry after Stop")
        elif "A3" not in set(backend.get("not_applicable") or []):
            errors.append(f"{job_id}: successful first click must mark A3 not-applicable")
        readiness = backend.get("readiness_after_update") or {}
        if (readiness.get("status") != "pass" or readiness.get("manual_start") is not False
                or readiness.get("first_probe_http_status") != 200):
            errors.append(f"{job_id}: immediate post-update readiness did not pass")
        stop_start = backend.get("stop_start") or {}
        if ((stop_start.get("stop") or {}).get("result") != "stopped"
                or (stop_start.get("stop") or {}).get("next_action") != "Start"
                or (stop_start.get("start") or {}).get("result") != "ready"
                or (stop_start.get("start") or {}).get("next_action") != "Stop"):
            errors.append(f"{job_id}: Stop -> Start lifecycle did not return to ready")
        negatives = backend.get("negative_controls")
        if not isinstance(negatives, dict):
            errors.append(f"{job_id}: negative controls are missing")
        else:
            negative_path = sidecar_paths.get("negative-controls.json")
            if negative_path:
                negative_receipt, _ = _sidecar_json(
                    negative_path, expected_os=contract["os"], mode=contract["mode"])
                if (negative_receipt or {}).get("controls") != negatives:
                    errors.append(f"{job_id}: negative-control summary differs from its raw sidecar")
            for name in ("busy_stop", "unknown_stop"):
                control = negatives.get(name) or {}
                if (control.get("status") != "pass" or control.get("outcome") != "refused"
                        or control.get("same_session_retry") != "stopped"):
                    errors.append(f"{job_id}: negative control {name} is missing or skipped")
            for name in ("identity_mismatch", "instance_mismatch"):
                control = negatives.get(name) or {}
                if (control.get("status") != "pass" or control.get("mismatch_observed") is not True
                        or control.get("readiness_suppressed") is not True):
                    errors.append(f"{job_id}: negative control {name} is missing or skipped")
        historical = backend.get("historical_compatibility") or {}
        if (historical.get("status") != "pass"
                or set(historical.get("vectors") or []) != {"H1", "H2", "H3"}):
            errors.append(f"{job_id}: historical action compatibility is not pass")
    return errors


def evaluate_m4_4a_backend_matrix(candidate, results):
    """Check the provisional M4-4a backend contract, never source evidence alone."""
    return _evaluate_pinokio_backend_matrix(
        candidate, results, M4_4A_BACKEND_MATRIX, "M4-4a backend")


PINOKIO_ROW_STEPS = ("install", "start_task", "stop_start", "busy_stop_data",
                     "update_ready_task", "uninstall_data")
PINOKIO_ROW_OS = {row["job_id"]: row["os"] for row in RELEASE1_1_PINOKIO_MATRIX}


def _pinokio_row_errors(job_id, row, candidate_sha):
    """Return the errors in one pinokio-row-v1 receipt, if any."""
    errors = []
    if set(row) != {"schema", "job_id", "os", "pinokio_sha", "steps"}:
        errors.append(f"{job_id}: receipt fields do not match pinokio-row-v1")
    if row.get("schema") != "pinokio-row-v1":
        errors.append(f"{job_id}: schema is not 'pinokio-row-v1'")
    if row.get("job_id") != job_id or row.get("os") != PINOKIO_ROW_OS[job_id]:
        errors.append(f"{job_id}: job id and OS do not match")
    pinokio_sha = row.get("pinokio_sha")
    if not isinstance(pinokio_sha, str) or not SHA.fullmatch(pinokio_sha):
        errors.append(f"{job_id}: pinokio_sha must be a full commit SHA")
    elif candidate_sha is not None and pinokio_sha != candidate_sha:
        errors.append(f"{job_id}: pinokio SHA differs from candidate")
    steps = row.get("steps")
    if not isinstance(steps, list):
        return errors + [f"{job_id}: steps must be a list"]
    names = [step.get("name") if isinstance(step, dict) else None for step in steps]
    if sorted(n for n in names if n is not None) != sorted(PINOKIO_ROW_STEPS) or len(names) != len(PINOKIO_ROW_STEPS):
        errors.append(f"{job_id}: step names are missing, extra or duplicated")
    for step in steps:
        if not isinstance(step, dict) or set(step) != {"name", "status", "detail"} or not isinstance(step.get("detail"), str):
            errors.append(f"{job_id}: step is not a name/status/detail object")
            continue
        if step["status"] != "pass":
            errors.append(f"{job_id}: {step['name']} status is not pass")
    return errors


def evaluate_release1_1_pinokio_matrix(candidate, results, job_ids=None):
    """Check the exact selected Pinokio row receipts (all three when job_ids is None)."""
    expected_ids = set(PINOKIO_ROW_OS) if job_ids is None else set(job_ids)
    if not expected_ids or not expected_ids <= set(PINOKIO_ROW_OS):
        return ["pinokio job ids must be a non-empty subset of the three Pinokio rows"]
    candidate_sha = None
    if candidate is not None:
        if not isinstance(candidate, dict):
            return ["candidate: expected an object"]
        candidate_sha = candidate.get("pinokio")
        if not isinstance(candidate_sha, str) or not SHA.fullmatch(candidate_sha):
            return ["candidate: pinokio SHA must be a full commit SHA"]
    errors = []
    found = {}
    for path in Path(results).rglob("journey-result.json"):
        try:
            row = read(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        job_id = row.get("job_id")
        if job_id not in PINOKIO_ROW_OS:
            errors.append(f"{path}: unknown or absent Pinokio job id")
            continue
        if job_id not in expected_ids:
            errors.append(f"{path}: Pinokio job {job_id} was not selected for this run")
            continue
        if job_id in found:
            errors.append(f"{job_id}: duplicate Pinokio receipt")
            continue
        found[job_id] = row
    for job_id in sorted(expected_ids):
        if job_id not in found:
            errors.append(f"{job_id}: missing Pinokio receipt")
            continue
        errors.extend(_pinokio_row_errors(job_id, found[job_id], candidate_sha))
    return errors


def _m4_5_row_errors(row):
    """Validate one M4-5 receipt without creating another evaluator."""
    if __package__:
        from . import journey_release2
    else:
        try:
            import journey_release2
        except ModuleNotFoundError:
            from scripts import journey_release2

    errors = []
    try:
        journey_release2.validate_receipt(row)
    except ValueError as exc:
        errors.append(str(exc))
    if row.get("status") != "pass" or row.get("evidence_kind") != "hosted-lifecycle":
        errors.append("only a hosted-lifecycle pass is admissible")
    if row.get("source_override_test_mode") is not False:
        errors.append("source override evidence is not admissible")
    return errors


def evaluate_m4_5_matrix(results):
    """Require one authentic hosted receipt for each distinct Release-2 scenario."""
    if __package__:
        from . import journey_release2
    else:
        try:
            import journey_release2
        except ModuleNotFoundError:
            from scripts import journey_release2

    expected = set(journey_release2.RELEASE2_SCENARIO_IDS)
    found = {}
    errors = []
    for path in Path(results).rglob("journey-result.json"):
        try:
            row = read(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        if row.get("receipt_kind") != "release2-installed-lifecycle":
            continue
        scenario_id = row.get("scenario_id")
        if scenario_id not in expected:
            errors.append(f"{path}: unknown M4-5 scenario_id {scenario_id!r}")
        elif scenario_id in found:
            errors.append(f"{scenario_id}: duplicate M4-5 receipt")
        else:
            found[scenario_id] = row
    for scenario_id in journey_release2.RELEASE2_SCENARIO_IDS:
        row = found.get(scenario_id)
        if row is None:
            errors.append(f"{scenario_id}: missing M4-5 receipt")
            continue
        errors.extend(f"{scenario_id}: {error}" for error in _m4_5_row_errors(row))
    return errors


def evaluate_m4_4b_local_smoke(results):
    """Validate the one local-process M4-4b smoke without hosted promotion."""
    if __package__:
        from .fully_local_executor import validate_execution_receipt
    else:
        try:
            from fully_local_executor import validate_execution_receipt
        except ModuleNotFoundError:
            from scripts.fully_local_executor import validate_execution_receipt

    expected_name = "m4-4b-local-result.json"
    files = sorted(Path(results).rglob(expected_name))
    errors = []
    if len(files) != 1:
        errors.append(f"M4-4b local smoke requires exactly one receipt, found {len(files)}")
    for path in files:
        try:
            row = read(path)
            validate_execution_receipt(row)
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"{path}: invalid M4-4b local receipt: {exc}")
            continue
        if row.get("receipt_kind") != "m4-4b-fully-local-editor-journey":
            errors.append(f"{path}: unexpected M4-4b receipt kind")
        if row.get("evidence_class") != M4_4B_LOCAL_SMOKE["evidence_class"]:
            errors.append(f"{path}: M4-4b receipt is not local_process evidence")
        if row.get("hosted_linux_receipt") is not False or row.get("hosted_linux_receipt_required") is not True:
            errors.append(f"{path}: local M4-4b receipt cannot satisfy hosted Linux evidence")
        if (row.get("platform") or {}).get("linux_receipt") is not False:
            errors.append(f"{path}: local M4-4b receipt cannot claim linux_receipt")
    return errors


def _m4_6_local_row_errors(candidate, contract, row, path):
    """Validate one local M4-6 receipt; it is never hosted end-state proof."""
    try:
        if __package__:
            from . import failure_mode_executor
            from .check_failure_receipts import verify_evidence_files
            from .journey_failure_modes import candidate_errors
        else:
            try:
                import failure_mode_executor
                from check_failure_receipts import verify_evidence_files
                from journey_failure_modes import candidate_errors
            except ModuleNotFoundError:
                from scripts import failure_mode_executor
                from scripts.check_failure_receipts import verify_evidence_files
                from scripts.journey_failure_modes import candidate_errors
    except ModuleNotFoundError as exc:
        return [f"local M4-6 validator is unavailable: {exc}"]

    slot_id = contract["job_id"]
    errors = []
    errors.extend(f"candidate: {error}" for error in candidate_errors(candidate))
    for key, value in (("receipt_kind", "m4-6-local-disposable-execution"),
                       ("status", "exercised"), ("slot_id", slot_id),
                       ("execution_scope", "local-disposable"),
                       ("source_override_test_mode", False)):
        if row.get(key) != value:
            errors.append(f"local receipt {key} is not {value!r}")
    observation = row.get("observation")
    if not isinstance(observation, dict):
        return errors + ["local executor observation is missing or malformed"]
    if row.get("candidate") != candidate:
        errors.append("local receipt candidate tuple differs from the frozen tuple")
    observed_candidate = observation.get("candidate_checkouts") or {}
    if (observation.get("manifest_sha256") != candidate.get("manifest_sha256")
            or any((observed_candidate.get(name) or {}).get("sha") != candidate.get(name)
                   for name in ("app", "agent", "runtime", "pinokio"))):
        errors.append("local executor observed a wrong candidate tuple")
    if (observation.get("slot_id") != slot_id or observation.get("runner_image") != contract["runner"]
            or observation.get("shell") != contract["shell"]
            or observation.get("execution_scope") != "local-disposable"
            or (observation.get("runner_provenance") or {}).get("ImageVersion") != "local-disposable"):
        errors.append("local executor observation has a wrong tuple or scope")
    errors.extend(failure_mode_executor.execution_observation_errors(observation))
    errors.extend(verify_evidence_files(row, path))
    return errors


def evaluate_m4_6_local_matrix(results, candidate):
    """Validate all nine local-disposable M4-6 executions, never hosted receipts."""
    if __package__:
        from .journey_failure_modes import candidate_errors
    else:
        try:
            from journey_failure_modes import candidate_errors
        except ModuleNotFoundError:
            from scripts.journey_failure_modes import candidate_errors

    errors = [f"candidate: {error}" for error in candidate_errors(candidate)]
    expected = {row["job_id"]: row for row in M4_6_MATRIX}
    found = {}
    for path in Path(results).rglob("m4-6-local-result.json"):
        try:
            row = read(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        slot_id = row.get("slot_id")
        if slot_id not in expected:
            errors.append(f"{path}: unknown M4-6 local slot {slot_id!r}")
        elif slot_id in found:
            errors.append(f"{slot_id}: duplicate M4-6 local receipt")
        else:
            found[slot_id] = (row, path)
    for slot_id in M4_6_SLOT_IDS:
        entry = found.get(slot_id)
        if entry is None:
            errors.append(f"{slot_id}: missing M4-6 local execution")
            continue
        row, path = entry
        errors.extend(f"{slot_id}: {error}" for error in _m4_6_local_row_errors(
            candidate, expected[slot_id], row, path))
    return errors


def _visibility_receipts(results):
    """Collect only the receipt kinds admitted by the end-state inventory."""
    release1, m4_5, m4_6 = {}, {}, {}
    errors = []

    def add(target, key, entry, path):
        if not isinstance(key, str):
            errors.append(f"{path}: required receipt identity is absent")
        else:
            target.setdefault(key, []).append(entry)

    for path in sorted(Path(results).rglob("journey-result.json")):
        if path.parent.name == "before-cleanup":
            continue  # Diagnostic snapshot; final receipt is the after-cleanup observation.
        try:
            row = read(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: malformed journey receipt: {exc}")
            continue
        kind = row.get("receipt_kind")
        if kind in {RELEASE1_DEFAULT_RECEIPT_KIND, *RELEASE1_RECEIPT_KINDS.values()}:
            add(release1, row.get("job_id"), (row, path), path)
        elif kind == "release2-installed-lifecycle":
            add(m4_5, row.get("scenario_id"), (row, path), path)
    for path in sorted(Path(results).rglob("m4-6-local-result.json")):
        try:
            row = read(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: malformed M4-6 receipt: {exc}")
            continue
        add(m4_6, row.get("slot_id"), (row, path), path)
    return release1, m4_5, m4_6, errors


def evaluate_end_state_obligations(candidate, results):
    """Return the closed END-STATE inventory with fail-closed row statuses.

    This is a presentation of the existing receipt predicates, not another
    aggregate/verdict system.  A valid local-disposable M4-6 record remains
    ``not-run`` because the inventory requires hosted native-service evidence.
    """
    release1, m4_5, m4_6, errors = _visibility_receipts(results)
    release1_contracts = {row["job_id"]: row for row in (*RELEASE1_MATRIX, *RELEASE1_1_DEFERRED_PINOKIO_MATRIX)}
    m4_6_contracts = {row["job_id"]: row for row in M4_6_MATRIX}
    statuses = []

    for obligation in END_STATE_INVENTORY:
        identity = obligation["id"]
        kind = obligation["evidence_kind"]
        row_errors = []
        if kind == "unsupported":
            status = "not-run"
            row_errors.append("no supported authenticated evidence route")
        elif kind == "release1":
            job_id = obligation["job_id"]
            entries = release1.get(job_id, [])
            if not entries:
                status = "not-run"
                row_errors.append("missing Release-1 lifecycle receipt")
            elif len(entries) != 1:
                status = "failed"
                row_errors.append("duplicate Release-1 lifecycle receipts")
            else:
                row_errors.extend(_release1_row_errors(candidate, release1_contracts[job_id], entries[0][0]))
                status = "passed" if not row_errors else "failed"
        elif kind == "m4-5":
            entries = m4_5.get(identity, [])
            if not entries:
                status = "not-run"
                row_errors.append("missing M4-5 hosted lifecycle receipt")
            elif len(entries) != 1:
                status = "failed"
                row_errors.append("duplicate M4-5 lifecycle receipts")
            else:
                row_errors.extend(_m4_5_row_errors(entries[0][0]))
                observed = entries[0][0].get("candidate")
                if (not isinstance(observed, dict) or any(
                        observed.get(name) != candidate.get(name)
                        for name in ("app", "agent", "runtime", "pinokio"))):
                    row_errors.append("M4-5 receipt candidate differs from the frozen checker candidate")
                status = "passed" if not row_errors else "failed"
        elif kind == "m4-6":
            job_id = obligation["job_id"]
            entries = m4_6.get(job_id, [])
            if not entries:
                status = "not-run"
                row_errors.append("hosted M4-6 route is unimplemented; no receipt")
            elif len(entries) != 1:
                status = "failed"
                row_errors.append("duplicate M4-6 local receipts")
            else:
                row_errors.extend(_m4_6_local_row_errors(
                    candidate, m4_6_contracts[job_id], entries[0][0], entries[0][1]))
                if row_errors:
                    status = "failed"
                else:
                    status = "not-run"
                    row_errors.append("local-disposable M4-6 evidence cannot satisfy hosted end-state evidence")
        else:  # Matrix changes must be explicit before they can be admitted.
            status = "failed"
            row_errors.append(f"unknown inventory evidence kind {kind!r}")
        statuses.append({
            "id": identity,
            "phase": obligation["phase"],
            "status": status,
            "reasons": tuple(row_errors),
        })
    return statuses, errors


def format_end_state_obligations(statuses):
    lines = ["## Release-obligation visibility", ""]
    for row in statuses:
        reason = "; ".join(row["reasons"]) if row["reasons"] else "validated by the existing receipt predicate"
        lines.append(f"- [{row['status']}] {row['id']} ({row['phase']}): {reason}")
    return "\n".join(lines) + "\n"


def evaluate_gpu_matrix(candidate, manifest, results, required=None):
    """Check RunPod receipts with the ordinary journey gate and extra row assertions."""
    expected = {row["scenario_id"]: row for row in GPU_MATRIX}
    required = tuple(expected) if required is None else tuple(required)
    errors = []
    if not required or len(set(required)) != len(required) or any(name not in expected for name in required):
        return ["GPU required scenarios are empty, duplicated or unknown"]
    # Duplicate JSON members are ambiguous, including nested members. Do not let
    # the JSON parser silently retain only the last value.
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field {key}")
            result[key] = value
        return result
    found = {}
    for path in Path(results).rglob("journey-result.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
            if not isinstance(row, dict):
                raise ValueError("expected object")
            name = row.get("scenario")
            if name not in expected:
                errors.append(f"{path}: unknown or missing GPU scenario")
            elif name in found:
                errors.append(f"{name}: duplicate GPU receipt")
            else:
                found[name] = row
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"{path}: {exc}")
    try:
        errors.extend(evaluate(candidate, manifest, results, required, "runpod", "sh"))
    except (TypeError, AttributeError, KeyError, ValueError) as exc:
        errors.append(f"malformed journey receipt: {exc}")
    for name in required:
        row = found.get(name)
        if row is None:
            continue
        contract = expected[name]
        for key, value in (("schema_version", 2), ("receipt_kind", "gpu-installed-journey"),
                           ("mode", contract["mode"])):
            if type(row.get(key)) is not type(value) or row.get(key) != value:
                errors.append(f"{name}: {key} is not {value!r}")
        if not DIGEST.fullmatch(str(row.get("manifest_sha256", ""))) or row.get("manifest_sha256") != candidate.get("manifest_sha256"):
            errors.append(f"{name}: manifest digest is absent or differs from frozen candidate")
        checkouts = row.get("candidate_checkouts")
        for key in ("app", "agent", "runtime", "pinokio"):
            entry = checkouts.get(key) if isinstance(checkouts, dict) else None
            if not isinstance(entry, dict) or entry.get("sha") != candidate.get(key) or not SHA.fullmatch(str(entry.get("sha", ""))):
                errors.append(f"{name}: {key} checkout is absent or differs from frozen candidate")
        schema = (manifest.get("runtime") or {}).get("schema_digest")
        if not isinstance(schema, str) or not schema.strip():
            errors.append(f"{name}: manifest runtime schema digest is absent")
        checks = row.get("checks")
        for key in contract["checks"]:
            if not isinstance(checks, dict) or checks.get(key) != "pass":
                errors.append(f"{name}: {key} assertion did not pass")
        exits = row.get("inner_exits")
        for key in contract["inner_exits"]:
            if not isinstance(exits, dict) or type(exits.get(key)) is not int or exits[key] != 0:
                errors.append(f"{name}: {key} inner exit is missing or nonzero")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--app-checkout", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--expected-runner", choices=(*RUNNER_PROVENANCE, "local"))
    parser.add_argument("--expected-shell", choices=("sh", "powershell.exe", "pwsh.exe"))
    parser.add_argument("--cadence", choices=tuple(REQUIRED), default="release-candidate")
    parser.add_argument("--required", action="append", help="Override scenario list for focused local checks")
    parser.add_argument("--allow-source-override", action="store_true", help="Explicit test-mode evidence only")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--release1-scope", choices=("five", "nine"), default="nine",
                        help="five for an automatic run of the previous five rows; nine for row=direct")
    parser.add_argument("--aggregate-release1", action="store_true",
                        help="require the exact five direct Release-1 job receipts under --results")
    parser.add_argument("--aggregate-release1-1-pinokio", action="store_true",
                        help="require the exact selected pinokio-row-v1 receipts under --results")
    parser.add_argument("--pinokio-job-ids", default=None,
                        help="comma-separated pinokio job ids selected for this run (default: all three)")
    parser.add_argument("--aggregate-m4-4a", action="store_true",
                        help="require all three hosted Pinokio backend M4-4a receipts")
    parser.add_argument("--aggregate-m4-5", action="store_true",
                        help="require all twelve hosted Release-2 scenario receipts")
    parser.add_argument("--aggregate-m4-4b-local", action="store_true",
                        help="require one local-process M4-4b smoke receipt; never hosted evidence")
    parser.add_argument("--aggregate-m4-6-local", action="store_true",
                        help="require all nine separately retained local-disposable M4-6 outputs")
    parser.add_argument("--aggregate-gpu", action="store_true",
                        help="require all RunPod GPU journey receipts")
    parser.add_argument("--report-end-state-obligations", action="store_true",
                        help="list every END-STATE obligation using the existing receipt predicates")
    args = parser.parse_args()
    errors = []
    try:
        aggregate_modes = sum((args.aggregate_release1, args.aggregate_release1_1_pinokio,
                               args.aggregate_m4_4a, args.aggregate_m4_5,
                               args.aggregate_m4_4b_local, args.aggregate_m4_6_local, args.aggregate_gpu))
        if aggregate_modes > 1:
            raise ValueError("choose one aggregate mode")
        if args.report_end_state_obligations and aggregate_modes:
            raise ValueError("end-state obligation reporting cannot be combined with an aggregate mode")
        if args.report_end_state_obligations:
            if args.results is None or args.candidate is None or args.app_checkout is None:
                raise ValueError("--results, --candidate and --app-checkout are required with --report-end-state-obligations")
            candidate = read(args.candidate)
            validate_tuple(candidate, args.app_checkout, args.app_checkout / "public/install-manifest.json")
            obligation_statuses, visibility_errors = evaluate_end_state_obligations(candidate, args.results)
            errors.extend(visibility_errors)
            errors.extend(
                f"{row['id']}: obligation status {row['status']}"
                for row in obligation_statuses if row["status"] != "passed"
            )
        elif args.aggregate_m4_4b_local:
            if args.results is None:
                raise ValueError("--results is required with --aggregate-m4-4b-local")
            errors.extend(evaluate_m4_4b_local_smoke(args.results))
        elif args.aggregate_m4_6_local:
            if args.results is None or args.candidate is None:
                raise ValueError("--results and --candidate are required with --aggregate-m4-6-local")
            errors.extend(evaluate_m4_6_local_matrix(args.results, read(args.candidate)))
        else:
            if args.candidate is None or args.app_checkout is None:
                raise ValueError("--candidate and --app-checkout are required for hosted Release-1/M4 aggregates")
            candidate = read(args.candidate)
            manifest = validate_tuple(candidate, args.app_checkout, args.app_checkout / "public/install-manifest.json")
            candidate = {**candidate, "release": manifest.get("release")}
        if args.aggregate_gpu:
            if args.results is None or args.allow_source_override or args.required or args.prepare_only:
                raise ValueError("GPU aggregate requires --results and never admits overrides or subsets")
            errors.extend(evaluate_gpu_matrix(candidate, manifest, args.results))
        elif not args.aggregate_m4_4b_local and not args.aggregate_m4_6_local and args.aggregate_release1_1_pinokio:
            if args.results is None:
                raise ValueError("--results is required with --aggregate-release1-1-pinokio")
            if args.allow_source_override:
                raise ValueError("R1.1 Pinokio aggregate never admits a source override")
            selected = None if args.pinokio_job_ids is None else args.pinokio_job_ids.split(",")
            errors.extend(evaluate_release1_1_pinokio_matrix(candidate, args.results, selected))
        elif not args.aggregate_m4_4b_local and not args.aggregate_m4_6_local and args.aggregate_release1:
            if args.results is None:
                raise ValueError("--results is required with --aggregate-release1")
            errors.extend(evaluate_release1_matrix(candidate, args.results, args.release1_scope))
        elif not args.aggregate_m4_4b_local and not args.aggregate_m4_6_local and args.aggregate_m4_4a:
            if args.results is None:
                raise ValueError("--results is required with --aggregate-m4-4a")
            if args.allow_source_override:
                raise ValueError("M4-4a aggregate never admits a source override")
            errors.extend(evaluate_m4_4a_backend_matrix(candidate, args.results))
        elif not args.aggregate_m4_4b_local and not args.aggregate_m4_6_local and args.aggregate_m4_5:
            if args.results is None:
                raise ValueError("--results is required with --aggregate-m4-5")
            if args.allow_source_override:
                raise ValueError("M4-5 aggregate never admits a source override")
            errors.extend(evaluate_m4_5_matrix(args.results))
        elif not args.aggregate_m4_4b_local and not args.aggregate_m4_6_local and not args.prepare_only:
            if args.cadence == "release-candidate":
                raise ValueError("release-candidate evidence requires --aggregate-release1")
            if args.results is None or args.expected_runner is None or args.expected_shell is None:
                raise ValueError("--results, --expected-runner and --expected-shell are required")
            if args.expected_runner != "local" and (args.expected_runner == "windows-2022") != (args.expected_shell in ("powershell.exe", "pwsh.exe")):
                raise ValueError("expected runner and shell are inconsistent")
            errors.extend(evaluate(candidate, manifest, args.results, args.required or REQUIRED[args.cadence], args.expected_runner, args.expected_shell, args.allow_source_override))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        errors.append(str(exc))
    heading = "## Journey evidence: " + ("FAIL" if errors else "PASS")
    report = heading + "\n\n" + ("\n".join(f"- {error}" for error in errors) if errors else "All required scenarios and candidate identity verified.") + "\n"
    if args.report_end_state_obligations and "obligation_statuses" in locals():
        report += "\n" + format_end_state_obligations(obligation_statuses)
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(report)
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
