#!/usr/bin/env python3
"""Fail closed on missing or mismatched installed-product journey evidence."""
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import subprocess
import sys

SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
ARTIFACTS = ("public/install", "public/install.sh", "public/install.ps1")
REQUIRED = {
    "release-candidate": ("fresh-install", "update-from-served", "reinstall", "pinokio", "fully-local", "source", "native-crash", "update-in-app"),
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
    args = parser.parse_args()
    errors = []
    try:
        candidate = read(args.candidate)
        manifest = validate_tuple(candidate, args.app_checkout, args.app_checkout / "public/install-manifest.json")
        if not args.prepare_only:
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
