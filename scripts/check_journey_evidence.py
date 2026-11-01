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
        APP_UPDATE_GUARD_MATRIX,
        DIRECT_LINUX_FAULT_SCENARIOS,
        DIRECT_MACOS_FAULT_SCENARIOS,
        DIRECT_WINDOWS_FAULT_SCENARIOS,
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
    )
else:
    try:
        from journey_matrix import (
            APP_UPDATE_GUARD_MATRIX,
            DIRECT_LINUX_FAULT_SCENARIOS,
            DIRECT_MACOS_FAULT_SCENARIOS,
            DIRECT_WINDOWS_FAULT_SCENARIOS,
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
        )
    except ModuleNotFoundError:  # importlib-loaded unit tests run from the repository root
        from scripts.journey_matrix import (
            APP_UPDATE_GUARD_MATRIX,
            DIRECT_LINUX_FAULT_SCENARIOS,
            DIRECT_MACOS_FAULT_SCENARIOS,
            DIRECT_WINDOWS_FAULT_SCENARIOS,
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
        )

SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
ARTIFACTS = ("public/install", "public/install.sh", "public/install.ps1")
RELEASE0_SUMS_SHA256 = "58221a50aa7940e3121d6239ddeb92d6409df3711096aac1161c7ec130b755c0"
RELEASE0_BOOTSTRAP_SHA256 = {
    "install": "100bab1deb15d620abd910c8fb0c9a6d77f4945428a3229dac6b506705e3fd0d",
    "install.ps1": "bba3f2edc41c047cb51475ceeac4489a04610af73a3af44bdc05287964d28258",
}
RUNNER_PROVENANCE = {
    "ubuntu-24.04": ("Linux", "ubuntu24"),
    "ubuntu-22.04": ("Linux", "ubuntu22"),
    "windows-2022": ("Windows", "win22"),
    "macos-14": ("macOS", "macos14"),
}
REQUIRED = {
    # Release candidates are no longer a loose list of legs.  They are the
    # exact nine ownership/OS jobs and must use --aggregate-release1.
    "release-candidate": RELEASE1_JOB_IDS,
    "weekly": ("fresh-install", "update-from-release:pre-updater"),
}


def read(path):
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
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
        expected_os = {"ubuntu-24.04": "Linux", "ubuntu-22.04": "Linux",
                       "windows-2022": "Windows", "macos-14": "macOS",
                       "local": {"Darwin": "macOS"}.get(platform.system(), platform.system())}[expected_runner]
        if expected_runner != "local":
            if not isinstance(provenance, dict):
                errors.append(f"{name}: runner provenance is missing")
            else:
                if provenance.get("RUNNER_OS") != expected_os:
                    errors.append(f"{name}: runner OS provenance does not match {expected_os}")
                expected_image_os = {"ubuntu-24.04": "ubuntu24", "ubuntu-22.04": "ubuntu22",
                                     "windows-2022": "win22", "macos-14": "macos14"}[expected_runner]
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
        for check in ("task_settled", "workspace_content", "one_runtime", "unrelated_sentinel"):
            if (row.get("checks") or {}).get(check) != "pass":
                errors.append(f"{name}: {check} did not pass")
    return errors


def evaluate_release1_matrix(candidate, results):
    """Reject an incomplete, skipped, or ownership-ambiguous nine-job receipt set.

    This runs after artifacts have been downloaded into one directory.  It does
    not infer a missing row from a successful matrix dependency: each named job
    must leave exactly one machine-readable ``journey-result.json`` receipt.
    """
    errors = []
    expected = {row["job_id"]: row for row in RELEASE1_MATRIX}
    found = {}
    for path in Path(results).rglob("journey-result.json"):
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
        if row.get("receipt_kind") != "release1-installed-lifecycle":
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
        if row.get("final_cleanup") != "pass":
            errors.append(f"{job_id}: final cleanup did not pass")
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
            if row.get("scenario") != "release0-to-release1":
                errors.append(f"{job_id}: direct receipt did not run the authentic release-0 to release-1 leg")
            transition = row.get("release_transition")
            if (not isinstance(transition, dict) or transition.get("from") != 0 or transition.get("to") != 1
                    or transition.get("harness_override") != "frozen-release0-direct-to-release1"):
                errors.append(f"{job_id}: release-0 to release-1 transition is absent or wrong")
            fixture = row.get("release0_fixture")
            if (not isinstance(fixture, dict) or fixture.get("fixture_sums_sha256") != RELEASE0_SUMS_SHA256
                    or fixture.get("bootstrap_sha256") != RELEASE0_BOOTSTRAP_SHA256 or not all(
                    isinstance(fixture.get(name), str) and DIGEST.fullmatch(fixture[name])
                    for name in ("fixture_sums_sha256", "manifest_sha256", "provenance_sha256"))
                    or fixture.get("sources") != {
                        "agent": "3204bb00135bec8ee2d2d3fb21eed5abfceca649",
                        "runtime": "6d53a19070faf63fc15ae1f81417722edc2c8a5f",
                    }):
                errors.append(f"{job_id}: frozen release-0 fixture provenance is absent or wrong")
            semantic = row.get("semantic_data")
            if (not isinstance(semantic, dict) or semantic.get("identical") is not True
                    or not all(isinstance(semantic.get(name), str) and DIGEST.fullmatch(semantic[name])
                               for name in ("before_sha256", "after_sha256"))
                    or semantic.get("before_sha256") != semantic.get("after_sha256")):
                errors.append(f"{job_id}: semantic data preservation is absent or unequal")
            for check in ("release", "service", "health", "semantic_data", "task_settled",
                          "one_runtime", "unrelated_sentinel"):
                if (row.get("checks") or {}).get(check) != "pass":
                    errors.append(f"{job_id}: {check} assertion did not pass")
        elif (prerequisite.get("outcome") != "explicit-owner"
              or prerequisite.get("managed_by") != "pinokio"
              or prerequisite.get("foreground_fallback") is not False):
            errors.append(f"{job_id}: Pinokio ownership prerequisite is not explicit")
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


def evaluate_m4_4a_backend_matrix(candidate, results):
    """Check the provisional M4-4a backend contract, never source evidence alone."""
    errors = []
    expected = {row["job_id"]: row for row in M4_4A_BACKEND_MATRIX}
    found = {}
    paths = {}
    for path in Path(results).rglob("journey-result.json"):
        try:
            row = read(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        job_id = row.get("job_id")
        if job_id not in expected:
            continue
        if job_id in found:
            errors.append(f"{job_id}: duplicate M4-4a backend receipt")
        else:
            found[job_id], paths[job_id] = row, path.parent
    for job_id, contract in expected.items():
        row = found.get(job_id)
        if row is None:
            errors.append(f"{job_id}: missing M4-4a backend receipt")
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
        expected_os = {"macos-14": "macOS", "ubuntu-24.04": "Linux", "windows-2022": "Windows"}[contract["runner"]]
        expected_image = {"macos-14": "macos14", "ubuntu-24.04": "ubuntu24", "windows-2022": "win22"}[contract["runner"]]
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
        try:
            journey_release2.validate_receipt(row)
        except ValueError as exc:
            errors.append(f"{scenario_id}: {exc}")
        if row.get("status") != "pass" or row.get("evidence_kind") != "hosted-lifecycle":
            errors.append(f"{scenario_id}: only a hosted-lifecycle pass is admissible")
        if row.get("source_override_test_mode") is not False:
            errors.append(f"{scenario_id}: source override evidence is not admissible")
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


def evaluate_m4_6_local_matrix(results, candidate):
    """Validate all nine local-disposable M4-6 executions, never hosted receipts."""
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
        contract = expected[slot_id]
        for key, value in (("receipt_kind", "m4-6-local-disposable-execution"),
                           ("status", "exercised"), ("slot_id", slot_id),
                           ("execution_scope", "local-disposable"),
                           ("source_override_test_mode", False)):
            if row.get(key) != value:
                errors.append(f"{slot_id}: local receipt {key} is not {value!r}")
        observation = row.get("observation")
        if not isinstance(observation, dict):
            errors.append(f"{slot_id}: local executor observation is missing or malformed")
            continue
        if row.get("candidate") != candidate:
            errors.append(f"{slot_id}: local receipt candidate tuple differs from the frozen tuple")
        observed_candidate = observation.get("candidate_checkouts") or {}
        if (observation.get("manifest_sha256") != candidate.get("manifest_sha256")
                or any((observed_candidate.get(name) or {}).get("sha") != candidate.get(name)
                       for name in ("app", "agent", "runtime", "pinokio"))):
            errors.append(f"{slot_id}: local executor observed a wrong candidate tuple")
        if (observation.get("slot_id") != slot_id or observation.get("runner_image") != contract["runner"]
                or observation.get("shell") != contract["shell"]
                or observation.get("execution_scope") != "local-disposable"
                or (observation.get("runner_provenance") or {}).get("ImageVersion") != "local-disposable"):
            errors.append(f"{slot_id}: local executor observation has a wrong tuple or scope")
        observation_errors = failure_mode_executor.execution_observation_errors(observation)
        errors.extend(f"{slot_id}: {error}" for error in observation_errors)
        errors.extend(f"{slot_id}: {error}" for error in verify_evidence_files(row, path))
    return errors


def evaluate_installed_faults(candidate, manifest, results, trusted_producers=None):
    """Validate independently attributed installed-fault and App-guard receipts.

    Step evidence paths are relative to each receipt directory. Fault steps use
    the shared installed-fault helper's observation contract; replay only checks
    recorded observations and does not execute an installed journey. The caller
    must obtain trusted_producers independently from the authenticated artifact
    retrieval system, never from the downloaded results being evaluated. Its
    entries bind each scenario's producer identity to the exact receipt bytes.
    """
    if __package__:
        from .failure_mode_executor import execute_installed_fault
    else:
        try:
            from failure_mode_executor import execute_installed_fault
        except ModuleNotFoundError:
            from scripts.failure_mode_executor import execute_installed_fault

    contracts = (*DIRECT_LINUX_FAULT_SCENARIOS, *DIRECT_MACOS_FAULT_SCENARIOS,
                 *DIRECT_WINDOWS_FAULT_SCENARIOS, *APP_UPDATE_GUARD_MATRIX)
    expected = {contract["scenario_id"]: contract for contract in contracts}
    errors = []
    if len(expected) != len(contracts):
        errors.append("installed-fault matrix contains duplicate scenario identities")
    keys = ("app", "agent", "runtime", "pinokio")
    if (not isinstance(candidate, dict) or any(not isinstance(candidate.get(k), str)
            or not SHA.fullmatch(candidate[k]) for k in keys)
            or not isinstance(candidate.get("manifest_sha256"), str)
            or not DIGEST.fullmatch(candidate["manifest_sha256"])):
        return errors + ["candidate tuple or manifest digest is invalid"]
    if (not isinstance(manifest, dict) or any(not isinstance(manifest.get(k), dict)
            or manifest[k].get("sha") != candidate[k] for k in ("agent", "runtime"))):
        return errors + ["manifest sources differ from candidate"]
    if (not isinstance(trusted_producers, dict)
            or trusted_producers.get("candidate_tuple") != {k: candidate[k] for k in keys}
            or trusted_producers.get("manifest_sha256") != candidate["manifest_sha256"]
            or not isinstance(trusted_producers.get("scenarios"), dict)
            or set(trusted_producers["scenarios"]) != set(expected)):
        return errors + ["trusted producer/run index missing or mismatched"]
    found = {}
    for path in Path(results).rglob("journey-result.json"):
        try:
            receipt_bytes = path.read_bytes()
            row = json.loads(receipt_bytes)
            if not isinstance(row, dict):
                raise ValueError("expected object")
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        name = row.get("scenario_id")
        if not isinstance(name, str) or name not in expected:
            errors.append(f"{path}: unknown or absent installed scenario id")
        elif name in found:
            errors.append(f"{name}: duplicate receipt")
        else:
            found[name] = (row, path, hashlib.sha256(receipt_bytes).hexdigest())

    for name, contract in expected.items():
        if name not in found:
            errors.append(f"{name}: missing receipt")
            continue
        row, receipt_path, receipt_digest = found[name]
        root = receipt_path.parent
        def fail(message):
            errors.append(f"{name}: {message}")

        guard = "phases" in contract
        trusted = trusted_producers["scenarios"].get(name)
        identity = row.get("producer_run")
        if (not isinstance(trusted, dict) or not isinstance(identity, dict)
                or any(not isinstance(identity.get(k), str) or not identity[k].strip()
                       for k in ("producer", "run_id"))
                or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip()
                       for k, v in identity.items())
                or trusted.get("producer_run") != identity
                or not isinstance(trusted.get("receipt_sha256"), str)
                or not DIGEST.fullmatch(trusted["receipt_sha256"])
                or receipt_digest != trusted["receipt_sha256"]):
            fail("authenticated producer/run identity or receipt binding is missing or mismatched")
        if (row.get("receipt_kind") != "installed-lifecycle-scenario"
                or row.get("execution_scope") != "hosted-installed"
                or row.get("evidence_class") != (contract["evidence_class"] if guard else "installed_native")
                or row.get("status") != "pass" or row.get("source_override_test_mode") is not False):
            fail("receipt kind, status or source override is inadmissible")
        if row.get("candidate_tuple") != {k: candidate[k] for k in keys}:
            fail("candidate tuple differs from frozen candidate")
        if row.get("manifest_sha256") != candidate["manifest_sha256"]:
            fail("manifest digest differs from frozen candidate")
        steps_for_selection = row.get("steps")
        rolled_back = (not guard and isinstance(steps_for_selection, dict)
                       and ("fail_candidate" in steps_for_selection
                            or (isinstance(steps_for_selection.get("recover"), dict)
                                and steps_for_selection["recover"].get("journal_step") == "rolled_back")))
        if rolled_back:
            attempted = row.get("attempted_tuple")
            selected = row.get("selected_tuple")
            baseline = steps_for_selection.get("baseline")
            verify = steps_for_selection.get("verify")
            fault = steps_for_selection.get("fail_candidate", steps_for_selection.get("interrupt"))
            if (attempted != {k: candidate[k] for k in keys}
                    or not isinstance(selected, dict) or set(selected) != set(keys)
                    or any(not isinstance(selected.get(k), str) or not SHA.fullmatch(selected[k]) for k in keys)
                    or row.get("installed_tuple") != selected
                    or not isinstance(baseline, dict) or baseline.get("selected_tuple") != selected
                    or not isinstance(verify, dict) or verify.get("selected_tuple") != selected
                    or not isinstance(fault, dict) or fault.get("attempted_tuple") != attempted):
                fail("attempted candidate and selected predecessor tuples are missing or inconsistent")
        elif row.get("installed_tuple") != {k: candidate[k] for k in keys}:
            fail("installed tuple differs from frozen candidate")
        sources = row.get("sources")
        if (not isinstance(sources, dict) or sources.get("agent") != manifest["agent"]
                or sources.get("runtime") != manifest["runtime"]
                or sources.get("app") != {"sha": candidate["app"]}):
            fail("installed sources differ from manifest or candidate")
        expected_os, image = RUNNER_PROVENANCE[contract["runner"]]
        provenance = row.get("runner_provenance")
        if (row.get("runner_image") != contract["runner"] or row.get("shell") != contract["shell"]
                or not isinstance(provenance, dict) or provenance.get("RUNNER_OS") != expected_os
                or provenance.get("ImageOS") != image
                or not isinstance(provenance.get("ImageVersion"), str)
                or not provenance["ImageVersion"].strip()
                or not isinstance(row.get("os_build"), str)
                or not row["os_build"].startswith(expected_os + "-")):
            fail("runner, shell or hosted provenance does not match matrix")
        if row.get("final_cleanup") != "pass":
            fail("final cleanup did not pass")

        if guard:
            identity = row.get("installed_app_identity")
            if (not isinstance(identity, dict) or identity.get("app_sha") != candidate["app"]
                    or not isinstance(identity.get("installation_id"), str)
                    or not identity["installation_id"].strip()):
                fail("installed App identity is absent or wrong")
            phases = row.get("phases")
            if not isinstance(phases, list) or len(phases) != len(contract["phases"]):
                fail("guard phase observations missing or duplicated")
                continue
            used = set()
            for observed, phase in zip(phases, contract["phases"]):
                if not isinstance(observed, dict):
                    fail("guard phase is malformed")
                    continue
                count = observed.get("update_post_count")
                if (observed.get("phase_id") != phase["phase_id"]
                        or observed.get("save_state") != phase["save_state"]
                        or observed.get("status") != "pass"
                        or type(count) is not int or count != phase["expected_update_post_count"]
                        or observed.get("installation_id") != (identity.get("installation_id") if isinstance(identity, dict) else None)):
                    fail(f"guard phase {phase['phase_id']} has wrong save or POST observation")
                if (not isinstance(observed.get("command"), list) or not observed["command"]
                        or any(not isinstance(v, str) or not v.strip() for v in observed["command"])
                        or type(observed.get("command_exit")) is not int or observed["command_exit"] != 0
                        or type(observed.get("inner_exit")) is not int or observed["inner_exit"] != 0):
                    fail(f"guard phase {phase['phase_id']} lacks successful command exits")
                evidence = observed.get("evidence")
                if not isinstance(evidence, list) or not evidence:
                    fail(f"guard phase {phase['phase_id']} has no evidence")
                    continue
                for item in evidence:
                    path, sha = _sidecar_path(root, item)
                    if path is None or not path.is_file() or path in used or digest(path) != sha:
                        fail(f"guard phase {phase['phase_id']} has invalid or reused evidence")
                    else:
                        used.add(path)
            continue

        if row.get("mode") != contract["mode"]:
            fail("ownership mode differs from matrix")
        predicates = row.get("predicates")
        if not isinstance(predicates, dict) or any(predicates.get(key) is not True
                                                   for key in contract["required_evidence"]):
            fail("matrix-required evidence predicates are missing or false")
        manager = row.get("service_manager")
        manager_name = contract.get("manager", "systemd")
        if (not isinstance(manager, dict) or manager.get("name") != manager_name
                or manager.get("native") is not True or manager.get("healthy") is not True
                or manager.get("foreground_fallback") is not False or manager.get("one_runtime") is not True):
            fail("native service manager ownership and health are absent")
        cleanup = row.get("cleanup")
        if (not isinstance(cleanup, dict) or cleanup.get("keep_data") is not True
                or cleanup.get("owned_processes_stopped") is not True):
            fail("keep-data and owned-process cleanup are absent")
        steps = row.get("steps")
        if not isinstance(steps, dict) or set(steps) != set(contract["steps"]):
            fail("fault step observations are missing or unknown")
            continue
        baseline = steps.get("baseline")
        if not isinstance(baseline, dict) or baseline.get("authentic_predecessor") is not True:
            fail("authentic Release-0 predecessor is absent")
        fixture = row.get("release0_fixture")
        baseline_evidence = baseline.get("evidence") if isinstance(baseline, dict) else None
        evidence_hashes = {item.get("sha256") for item in baseline_evidence
                           if isinstance(item, dict) and isinstance(item.get("sha256"), str)} if isinstance(baseline_evidence, list) else set()
        if (not isinstance(fixture, dict) or fixture.get("authenticated") is not True
                or any(not isinstance(fixture.get(key), str) or not DIGEST.fullmatch(fixture[key])
                       or fixture[key] not in evidence_hashes for key in ("manifest_sha256", "provenance_sha256"))
                or fixture.get("manifest_sha256") == fixture.get("provenance_sha256")
                or not isinstance(baseline, dict)
                or baseline.get("selected_sha256") != baseline.get("predecessor_sha256")):
            fail("selected predecessor or hashed Release-0 fixture provenance is absent")

        class RecordedAdapter:
            def run_step(self, step, previous):
                return steps[step]
        try:
            execute_installed_fault(contract.get("scenario", contract.get("leg")), RecordedAdapter(), root)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            fail(f"fault observations invalid: {exc}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--app-checkout", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--expected-runner", choices=("ubuntu-24.04", "ubuntu-22.04", "windows-2022", "macos-14", "local"))
    parser.add_argument("--expected-shell", choices=("sh", "powershell.exe", "pwsh.exe"))
    parser.add_argument("--cadence", choices=tuple(REQUIRED), default="release-candidate")
    parser.add_argument("--required", action="append", help="Override scenario list for focused local checks")
    parser.add_argument("--allow-source-override", action="store_true", help="Explicit test-mode evidence only")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--aggregate-release1", action="store_true",
                        help="require the exact nine Release-1 job receipts under --results")
    parser.add_argument("--aggregate-m4-4a", action="store_true",
                        help="require all three hosted Pinokio backend M4-4a receipts")
    parser.add_argument("--aggregate-m4-5", action="store_true",
                        help="require all twelve hosted Release-2 scenario receipts")
    parser.add_argument("--aggregate-m4-4b-local", action="store_true",
                        help="require one local-process M4-4b smoke receipt; never hosted evidence")
    parser.add_argument("--aggregate-m4-6-local", action="store_true",
                        help="require all nine separately retained local-disposable M4-6 outputs")
    parser.add_argument("--installed-faults", action="store_true",
                        help="require all installed fault and App guard scenario receipts")
    parser.add_argument("--trusted-producer-index", type=Path,
                        help="independently authenticated producer/run index for installed faults")
    args = parser.parse_args()
    errors = []
    try:
        aggregate_modes = sum((args.aggregate_release1, args.aggregate_m4_4a, args.aggregate_m4_5,
                               args.aggregate_m4_4b_local, args.aggregate_m4_6_local, args.installed_faults))
        if aggregate_modes > 1:
            raise ValueError("choose one aggregate mode")
        if args.aggregate_m4_4b_local:
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
        if args.installed_faults:
            if args.results is None or args.trusted_producer_index is None or args.allow_source_override or args.prepare_only:
                raise ValueError("--installed-faults requires results and a trusted producer index and forbids override and prepare-only")
            if args.trusted_producer_index.resolve().is_relative_to(args.results.resolve()):
                raise ValueError("trusted producer index must be independent of downloaded results")
            errors.extend(evaluate_installed_faults(candidate, manifest, args.results, read(args.trusted_producer_index)))
        elif not args.aggregate_m4_4b_local and not args.aggregate_m4_6_local and args.aggregate_release1:
            if args.results is None:
                raise ValueError("--results is required with --aggregate-release1")
            errors.extend(evaluate_release1_matrix(candidate, args.results))
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
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(report)
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
