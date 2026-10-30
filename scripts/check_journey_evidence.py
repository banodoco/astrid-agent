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
    from .journey_matrix import RELEASE1_JOB_IDS, RELEASE1_MATRIX
else:
    try:
        from journey_matrix import RELEASE1_JOB_IDS, RELEASE1_MATRIX
    except ModuleNotFoundError:  # importlib-loaded unit tests run from the repository root
        from scripts.journey_matrix import RELEASE1_JOB_IDS, RELEASE1_MATRIX

SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
ARTIFACTS = ("public/install", "public/install.sh", "public/install.ps1")
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
            if (not isinstance(fixture, dict) or not all(
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--app-checkout", required=True, type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--expected-runner", choices=("ubuntu-24.04", "ubuntu-22.04", "windows-2022", "macos-14", "local"))
    parser.add_argument("--expected-shell", choices=("sh", "powershell.exe", "pwsh.exe"))
    parser.add_argument("--cadence", choices=tuple(REQUIRED), default="release-candidate")
    parser.add_argument("--required", action="append", help="Override scenario list for focused local checks")
    parser.add_argument("--allow-source-override", action="store_true", help="Explicit test-mode evidence only")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--aggregate-release1", action="store_true",
                        help="require the exact nine Release-1 job receipts under --results")
    args = parser.parse_args()
    errors = []
    try:
        candidate = read(args.candidate)
        manifest = validate_tuple(candidate, args.app_checkout, args.app_checkout / "public/install-manifest.json")
        if args.aggregate_release1:
            if args.results is None:
                raise ValueError("--results is required with --aggregate-release1")
            errors.extend(evaluate_release1_matrix(candidate, args.results))
        elif not args.prepare_only:
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
