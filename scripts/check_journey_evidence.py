#!/usr/bin/env python3
"""Fail closed on missing or mismatched installed-product journey evidence."""
import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

if __package__:
    from .journey_matrix import (
        DEFERRED_R1_X_DIRECT_CONTAINMENT_CONTINUITY_OBLIGATIONS,
        DEFERRED_R1_X_PINOKIO_CONTINUITY_OBLIGATIONS,
        END_STATE_INVENTORY,
        GPU_MATRIX,
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
        PINOKIO_R1_SNAPSHOT_ACTIONS,
        PINOKIO_R1_SNAPSHOT_SCENARIO_ASSERTIONS,
        RELEASE1_JOB_IDS,
        RELEASE1_MATRIX,
    )
else:
    try:
        from journey_matrix import (
            DEFERRED_R1_X_DIRECT_CONTAINMENT_CONTINUITY_OBLIGATIONS,
            DEFERRED_R1_X_PINOKIO_CONTINUITY_OBLIGATIONS,
            END_STATE_INVENTORY,
            GPU_MATRIX,
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
            PINOKIO_R1_SNAPSHOT_ACTIONS,
            PINOKIO_R1_SNAPSHOT_SCENARIO_ASSERTIONS,
            RELEASE1_JOB_IDS,
            RELEASE1_MATRIX,
        )
    except ModuleNotFoundError:  # importlib-loaded unit tests run from the repository root
        from scripts.journey_matrix import (
            DEFERRED_R1_X_DIRECT_CONTAINMENT_CONTINUITY_OBLIGATIONS,
            DEFERRED_R1_X_PINOKIO_CONTINUITY_OBLIGATIONS,
            END_STATE_INVENTORY,
            GPU_MATRIX,
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
            PINOKIO_R1_SNAPSHOT_ACTIONS,
            PINOKIO_R1_SNAPSHOT_SCENARIO_ASSERTIONS,
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
    "runpod": ("Linux", "runpod"),
    "ubuntu-24.04": ("Linux", "ubuntu24"),
    "ubuntu-22.04": ("Linux", "ubuntu22"),
    "windows-2022": ("Windows", "win22"),
    "macos-15": ("macOS", "macos15"),
}

# This is deliberately not an alias in RUNNER_PROVENANCE.  Hosted matrix
# validation remains closed over GitHub-hosted images; the self-hosted route is
# admitted only by the separate trusted-context predicate below.
SELF_HOSTED_MACOS_VENUE = "self-hosted"
SELF_HOSTED_MACOS_RUNNER_NAME = "hannah-mac"
SELF_HOSTED_MACOS_LABEL = "astrid-mac"
SELF_HOSTED_MACOS_CONTEXT_FIELDS = frozenset((
    "schema_version", "source", "repository", "workflow_path", "workflow_id", "workflow_sha", "ref", "head_sha",
    "run_id", "run_attempt", "job_id", "matrix_job_id", "runner_name",
    "runner_labels", "runner_os", "runner_arch", "conclusion", "candidate",
    "receipt_sha256", "collector_profile", "collector_version", "collector_sha256",
    "job_started_at", "job_completed_at",
))
SELF_HOSTED_MACOS_RECEIPT_IDENTITY_FIELDS = frozenset((
    "repository", "workflow_path", "workflow_id", "workflow_sha", "ref", "head_sha", "run_id", "run_attempt",
    "job_id", "matrix_job_id", "runner_name", "runner_labels", "runner_os",
    "runner_arch", "collector_profile", "collector_version", "collector_sha256",
))
SELF_HOSTED_MACOS_COLLECTION_FIELDS = frozenset((
    "schema_version", "prepared_only", "started_at", "ended_at", "initial_census",
    "final_census", "event_sequence", "loss_counters", "inner_exit", "freshness_seconds",
))
SELF_HOSTED_MACOS_COLLECTOR_PROFILE = "release1-installed-lifecycle"
SELF_HOSTED_MACOS_COLLECTOR_VERSION = 2
SELF_HOSTED_MACOS_MAX_FRESHNESS_SECONDS = 900


def _migrate_macos_receipt_key(contract):
    """Compatibility for existing receipt fixtures; the matrix now owns this key."""
    return contract


def _positive_integer(value):
    return type(value) is int and value > 0


def _rfc3339(value):
    """Parse a UTC RFC-3339 timestamp without accepting a local-time claim."""
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _labels(value, *, subject):
    """Return a duplicate-free API label set, or a precise closed-profile error."""
    if not isinstance(value, list) or not value:
        return None, f"{subject}: runner labels are absent"
    if any(not isinstance(label, str) or not label.strip() for label in value):
        return None, f"{subject}: runner labels must be non-empty strings"
    labels = frozenset(value)
    if len(labels) != len(value):
        return None, f"{subject}: runner labels contain a duplicate"
    required = {"self-hosted", "macOS", SELF_HOSTED_MACOS_LABEL}
    missing = sorted(required - labels)
    if missing:
        return None, f"{subject}: runner labels are missing {', '.join(missing)}"
    return labels, None


def _self_hosted_collection_errors(receipt, trusted_context):
    """Require the R-30 collector facts rather than treating a pass bit as proof.

    The API cannot observe the private lifecycle itself.  These fields therefore
    remain receipt claims, but they are closed, digest-bound, time-bounded by
    API job timestamps, and fail closed until an independent collector supplies
    them.  In particular, a prepared-only receipt cannot be relabelled as a
    completed journey.
    """
    errors = []
    collection = receipt.get("self_hosted_collection") if isinstance(receipt, dict) else None
    if not isinstance(collection, dict):
        return ["self-hosted macOS: collector evidence is absent"]
    missing = sorted(SELF_HOSTED_MACOS_COLLECTION_FIELDS - set(collection))
    unknown = sorted(set(collection) - SELF_HOSTED_MACOS_COLLECTION_FIELDS)
    if missing:
        errors.append("self-hosted macOS: collector evidence is missing " + ", ".join(missing))
    if unknown:
        errors.append("self-hosted macOS: collector evidence has unknown fields " + ", ".join(unknown))
    if collection.get("schema_version") != 1:
        errors.append("self-hosted macOS: collector evidence schema version is not 1")
    if collection.get("prepared_only") is not False or receipt.get("evidence_class") == "prepared-only":
        errors.append("self-hosted macOS: prepared-only evidence is not admissible")

    started = _rfc3339(collection.get("started_at"))
    ended = _rfc3339(collection.get("ended_at"))
    job_started = _rfc3339(trusted_context.get("job_started_at"))
    job_completed = _rfc3339(trusted_context.get("job_completed_at"))
    if started is None or ended is None or ended < started:
        errors.append("self-hosted macOS: collector start/end times are absent or malformed")
    if job_started is None or job_completed is None or job_completed < job_started:
        errors.append("self-hosted macOS: trusted job time bounds are absent or malformed")
    if None not in (started, ended, job_started, job_completed):
        if started < job_started or ended > job_completed:
            errors.append("self-hosted macOS: collector times fall outside API job bounds")
        freshness = collection.get("freshness_seconds")
        observed_freshness = int((job_completed - ended).total_seconds())
        if (type(freshness) is not int or freshness < 0
                or freshness != observed_freshness
                or freshness > SELF_HOSTED_MACOS_MAX_FRESHNESS_SECONDS):
            errors.append("self-hosted macOS: collector freshness is absent, stale, or inconsistent")

    for name in ("initial_census", "final_census"):
        census = collection.get(name)
        if (not isinstance(census, dict) or set(census) != {"source", "sha256", "count"}
                or census.get("source") != "independent"
                or not DIGEST.fullmatch(str(census.get("sha256", "")))
                or type(census.get("count")) is not int or census["count"] < 0):
            errors.append(f"self-hosted macOS: {name.replace('_', ' ')} is absent or not independent")

    sequence = collection.get("event_sequence")
    if not isinstance(sequence, dict) or set(sequence) != {"first", "last", "count", "sha256"}:
        errors.append("self-hosted macOS: complete event sequence is absent")
        event_count = None
    elif (type(sequence.get("first")) is not int or type(sequence.get("last")) is not int
          or type(sequence.get("count")) is not int or sequence["first"] < 1
          or sequence["last"] < sequence["first"]
          or sequence["count"] != sequence["last"] - sequence["first"] + 1
          or not DIGEST.fullmatch(str(sequence.get("sha256", "")))):
        errors.append("self-hosted macOS: event sequence is incomplete or malformed")
        event_count = None
    else:
        event_count = sequence["count"]

    losses = collection.get("loss_counters")
    if (not isinstance(losses, dict) or set(losses) != {"emitted", "persisted", "dropped"}
            or any(type(losses.get(key)) is not int or losses[key] < 0 for key in losses)
            or losses.get("dropped") != 0 or event_count is None
            or losses.get("emitted") != event_count or losses.get("persisted") != event_count):
        errors.append("self-hosted macOS: event loss counters are absent, nonzero, or incomplete")
    if collection.get("inner_exit") != 0:
        errors.append("self-hosted macOS: inner exit is missing or nonzero")
    return errors


def self_hosted_macos_identity_errors(candidate, receipt, trusted_context, receipt_sha256):
    """Bind a self-hosted receipt to context fetched outside that runner.

    ``trusted_context`` is intentionally a separate checker input.  It must be
    assembled by the trusted aggregate from the GitHub Actions run/job API and
    the downloaded artifact bytes, never by the self-hosted job or its upload.
    The receipt identity contains only claims which are compared against that
    independently reconstructed record.
    """
    errors = []
    if not isinstance(trusted_context, dict):
        return ["self-hosted macOS: trusted Actions context is absent"]
    missing = sorted(SELF_HOSTED_MACOS_CONTEXT_FIELDS - set(trusted_context))
    unknown = sorted(set(trusted_context) - SELF_HOSTED_MACOS_CONTEXT_FIELDS)
    if missing:
        errors.append("self-hosted macOS: trusted context is missing " + ", ".join(missing))
    if unknown:
        errors.append("self-hosted macOS: trusted context has unknown fields " + ", ".join(unknown))
    if trusted_context.get("schema_version") != 2:
        errors.append("self-hosted macOS: trusted context schema version is not 2")
    if trusted_context.get("source") != "github-actions-api":
        errors.append("self-hosted macOS: trusted context must be GitHub Actions API-originated")
    if trusted_context.get("repository") != "banodoco/astrid-agent":
        errors.append("self-hosted macOS: trusted repository is not banodoco/astrid-agent")
    if trusted_context.get("workflow_path") != ".github/workflows/journey.yml":
        errors.append("self-hosted macOS: trusted workflow path is not journey.yml")
    if not _positive_integer(trusted_context.get("workflow_id")):
        errors.append("self-hosted macOS: trusted workflow ID is absent or malformed")
    if not SHA.fullmatch(str(trusted_context.get("workflow_sha", ""))):
        errors.append("self-hosted macOS: trusted workflow SHA is absent or malformed")
    elif trusted_context.get("workflow_sha") != trusted_context.get("head_sha"):
        errors.append("self-hosted macOS: trusted workflow SHA differs from the API run head")
    ref = trusted_context.get("ref")
    if not isinstance(ref, str) or not ref.startswith("refs/") or "\n" in ref or "\r" in ref:
        errors.append("self-hosted macOS: trusted ref is absent or malformed")
    if not SHA.fullmatch(str(trusted_context.get("head_sha", ""))):
        errors.append("self-hosted macOS: trusted head SHA is absent or malformed")
    for field in ("run_id", "run_attempt", "job_id"):
        if not _positive_integer(trusted_context.get(field)):
            errors.append(f"self-hosted macOS: trusted {field} is absent or malformed")
    if trusted_context.get("matrix_job_id") != "direct-macos":
        errors.append("self-hosted macOS: trusted job is not direct-macos")
    if trusted_context.get("runner_name") != SELF_HOSTED_MACOS_RUNNER_NAME:
        errors.append("self-hosted macOS: trusted runner name is not hannah-mac")
    labels, label_error = _labels(
        trusted_context.get("runner_labels"), subject="self-hosted macOS trusted context"
    )
    if label_error:
        errors.append(label_error)
    if trusted_context.get("runner_os") != "macOS":
        errors.append("self-hosted macOS: trusted runner OS is not macOS")
    if trusted_context.get("runner_arch") not in {"ARM64", "X64"}:
        errors.append("self-hosted macOS: trusted runner architecture is absent or unsupported")
    elif labels is not None and trusted_context["runner_arch"] not in labels:
        errors.append("self-hosted macOS: trusted runner architecture is not an API runner label")
    if trusted_context.get("conclusion") != "success":
        errors.append("self-hosted macOS: trusted job conclusion is not success")
    if trusted_context.get("candidate") != candidate:
        errors.append("self-hosted macOS: trusted tuple differs from frozen candidate")
    if trusted_context.get("collector_profile") != SELF_HOSTED_MACOS_COLLECTOR_PROFILE:
        errors.append("self-hosted macOS: trusted collector profile is wrong")
    if trusted_context.get("collector_version") != SELF_HOSTED_MACOS_COLLECTOR_VERSION:
        errors.append("self-hosted macOS: trusted collector version is wrong")
    if not DIGEST.fullmatch(str(trusted_context.get("collector_sha256", ""))):
        errors.append("self-hosted macOS: trusted collector hash is absent or malformed")
    if _rfc3339(trusted_context.get("job_started_at")) is None or _rfc3339(trusted_context.get("job_completed_at")) is None:
        errors.append("self-hosted macOS: trusted API job times are absent or malformed")
    if not isinstance(receipt_sha256, str) or not DIGEST.fullmatch(receipt_sha256):
        errors.append("self-hosted macOS: checker did not hash the receipt bytes")
    elif trusted_context.get("receipt_sha256") != receipt_sha256:
        errors.append("self-hosted macOS: trusted receipt digest differs from parsed receipt bytes")

    if not isinstance(receipt, dict):
        return errors + ["self-hosted macOS: receipt is absent or malformed"]
    if receipt.get("venue") != SELF_HOSTED_MACOS_VENUE:
        errors.append("self-hosted macOS: receipt venue is not self-hosted")
    if receipt.get("runner_image") not in {"local", "self-hosted"}:
        errors.append("self-hosted macOS: receipt is relabelled hosted evidence or has an unknown runner image")
    identity = receipt.get("execution_identity")
    if not isinstance(identity, dict):
        return errors + ["self-hosted macOS: receipt execution identity is absent"]
    missing = sorted(SELF_HOSTED_MACOS_RECEIPT_IDENTITY_FIELDS - set(identity))
    unknown = sorted(set(identity) - SELF_HOSTED_MACOS_RECEIPT_IDENTITY_FIELDS)
    if missing:
        errors.append("self-hosted macOS: receipt identity is missing " + ", ".join(missing))
    if unknown:
        errors.append("self-hosted macOS: receipt identity has unknown fields " + ", ".join(unknown))
    for field in SELF_HOSTED_MACOS_RECEIPT_IDENTITY_FIELDS - {"runner_labels"}:
        if identity.get(field) != trusted_context.get(field):
            errors.append(f"self-hosted macOS: receipt {field} differs from trusted context")
    receipt_labels, receipt_label_error = _labels(
        identity.get("runner_labels"), subject="self-hosted macOS receipt"
    )
    if receipt_label_error:
        errors.append(receipt_label_error)
    if receipt_labels is not None and labels is not None and receipt_labels != labels:
        errors.append("self-hosted macOS: receipt runner labels differ from trusted context")
    errors.extend(_self_hosted_collection_errors(receipt, trusted_context))
    return errors


REQUIRED = {
    # Release candidates are no longer a loose list of legs.  They are the
    # exact nine ownership/OS jobs and must use --aggregate-release1.
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
        for check in ("task_settled", "workspace_content", "one_runtime", "unrelated_sentinel"):
            if (row.get("checks") or {}).get(check) != "pass":
                errors.append(f"{name}: {check} did not pass")
    return errors


def _release1_row_errors(candidate, contract, row):
    """Validate one Release-1 row for both the aggregate and inventory view."""
    job_id = contract["job_id"]
    errors = []
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


def evaluate_release1_matrix(candidate, results):
    """Reject an incomplete, skipped, or ownership-ambiguous nine-job receipt set."""
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
        errors.extend(_release1_row_errors(candidate, contract, row))
    return errors


def evaluate_self_hosted_macos_receipt(candidate, receipt_path, trusted_context_path):
    """Admit only the separately authenticated hannah-mac direct-macOS receipt.

    This profile never relaxes the normal hosted aggregate: its API-built
    context is a separate input, and its lifecycle proof is still evaluated by
    the same Release-1 direct-macOS predicate after identity is authenticated.
    """
    receipt_path = Path(receipt_path)
    trusted_context_path = Path(trusted_context_path)
    if receipt_path.resolve() == trusted_context_path.resolve():
        return ["self-hosted macOS: trusted context cannot be supplied by the receipt artifact"]
    try:
        receipt = read(receipt_path)
    except (OSError, ValueError) as exc:
        return [f"self-hosted macOS: receipt is unreadable: {exc}"]
    try:
        trusted_context = read(trusted_context_path)
    except (OSError, ValueError) as exc:
        return [f"self-hosted macOS: trusted Actions context is unreadable: {exc}"]

    errors = self_hosted_macos_identity_errors(
        candidate, receipt, trusted_context, digest(receipt_path)
    )
    contract = next(row for row in RELEASE1_MATRIX if row["job_id"] == "direct-macos")
    if receipt.get("job_id") != contract["job_id"]:
        errors.append("self-hosted macOS: receipt job is not direct-macos")

    # Do not let a producer pretend to be a hosted image.  The local copy is
    # only for reusing every lifecycle predicate; API runner authentication is
    # exclusively the trusted context validated above.
    lifecycle = dict(receipt)
    lifecycle["runner_image"] = contract["runner"]
    lifecycle["runner_provenance"] = {
        "RUNNER_OS": "macOS", "ImageOS": "macos15", "ImageVersion": "trusted-self-hosted-profile",
    }
    errors.extend(_release1_row_errors(candidate, contract, lifecycle))
    return errors


def evaluate_release1_with_self_hosted_macos(candidate, results, receipt_path, trusted_context_path):
    """Require eight hosted rows and exactly one authenticated direct-macOS row."""
    errors = []
    expected = {row["job_id"]: row for row in RELEASE1_MATRIX if row["job_id"] != "direct-macos"}
    found = {}
    receipt_path = Path(receipt_path).resolve()
    for path in Path(results).rglob("journey-result.json"):
        try:
            resolved = path.resolve()
            row = read(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        if resolved == receipt_path:
            errors.append(f"{path}: self-hosted receipt must not be included in hosted results")
            continue
        # The collector intentionally retains the pre-cleanup copy as failure
        # diagnostics.  It can never be an admitted final receipt; each
        # artifact's post-cleanup copy is the sole canonical matrix input.
        if path.parent.name == "before-cleanup":
            continue
        if path.parent.name != "after-cleanup":
            errors.append(f"{path}: hosted receipt is not in the canonical after-cleanup location")
            continue
        job_id = row.get("job_id")
        if job_id == "direct-macos":
            errors.append(f"{path}: hosted results must not include a direct-macos receipt")
            continue
        if not isinstance(job_id, str) or job_id not in expected:
            errors.append(f"{path}: unknown or absent hosted Release-1 job id")
            continue
        if job_id in found:
            errors.append(f"{path}: duplicate receipt for {job_id}")
            continue
        found[job_id] = row
    for job_id, contract in expected.items():
        row = found.get(job_id)
        if row is None:
            errors.append(f"{job_id}: missing receipt")
            continue
        if row.get("venue") not in (None, "hosted"):
            errors.append(f"{job_id}: hosted row is relabelled as self-hosted")
        errors.extend(_release1_row_errors(candidate, contract, row))
    errors.extend(evaluate_self_hosted_macos_receipt(candidate, receipt_path, trusted_context_path))
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


# GitHub's artifact metadata identifies a run but not the producing job or
# attempt.  This index is diagnostic only until a platform origin attestation
# supplies that missing edge; it must never be passed off as an authenticated
# external_checkpoint_join to journey_predicates.check_m4_6.
M4_6_ORIGIN_NEEDS = (
    "M4-6 artifact-to-job/attempt/slot platform attestation is unavailable; "
    "API metadata and artifact names do not establish origin"
)


def m4_6_actions_index(candidate, slot_id, run_id, attempt, job_id, artifact_id,
                       harness_sha, run, jobs, artifact, archive):
    """Validate independent API observations and return a non-admitted index.

    Expected identities and candidate are selected by the checker operator, not
    from an artifact. The caller obtains the observations directly from GitHub.
    This function alone is a structural test seam, not an authority boundary.
    """
    if __package__:
        from .journey_failure_modes import candidate_errors
    else:
        try:
            from journey_failure_modes import candidate_errors
        except ModuleNotFoundError:
            from scripts.journey_failure_modes import candidate_errors
    if not isinstance(candidate, dict) or candidate_errors(candidate):
        raise ValueError("invalid frozen M4-6 candidate")
    contract = next((row for row in M4_6_MATRIX
                     if row["job_id"] == slot_id and row["mode"] == "direct"), None)
    if contract is None:
        raise ValueError("selected slot is not a direct checkpoint")
    if not all(_positive_integer(n) for n in (run_id, attempt, job_id, artifact_id)):
        raise ValueError("run, attempt, job and artifact IDs must be positive integers")
    if not isinstance(harness_sha, str) or not SHA.fullmatch(harness_sha):
        raise ValueError("a frozen harness commit SHA is required")
    repository = run.get("repository") if isinstance(run, dict) else None
    if not isinstance(run, dict) or (not _positive_integer(run.get("id"))
            or not _positive_integer(run.get("run_attempt")) or run.get("id") != run_id
            or run.get("run_attempt") != attempt or run.get("head_sha") != harness_sha
            or not isinstance(repository, dict) or repository.get("full_name") != "banodoco/astrid-agent"
            or str(run.get("path", "")).split("@", 1)[0] != ".github/workflows/journey.yml"
            or not _positive_integer(run.get("workflow_id"))):
        raise ValueError("API run, attempt, repository, workflow or harness differs")
    if not isinstance(jobs, list):
        raise ValueError("attempt-scoped API jobs are missing")
    selected = [job for job in jobs if isinstance(job, dict) and job.get("id") == job_id]
    if len(selected) != 1:
        raise ValueError("selected job is absent or duplicated in the API attempt")
    job = selected[0]
    labels = job.get("labels")
    if (not _positive_integer(job.get("id")) or not _positive_integer(job.get("run_id"))
            or job.get("run_id") != run_id
            or ("run_attempt" in job and (not _positive_integer(job["run_attempt"])
                                           or job["run_attempt"] != attempt))
            or job.get("status") != "completed" or job.get("conclusion") != "success"
            or not isinstance(job.get("name"), str) or not job["name"]
            or not isinstance(job.get("runner_name"), str) or not job["runner_name"]
            or not isinstance(labels, list) or contract["runner"] not in labels):
        raise ValueError("API job identity, conclusion or runner differs")
    artifact_run = artifact.get("workflow_run") if isinstance(artifact, dict) else None
    if not isinstance(artifact, dict) or (not _positive_integer(artifact.get("id"))
            or artifact.get("id") != artifact_id
            or artifact.get("name") != f"m4-6-{slot_id}" or artifact.get("expired") is not False
            or not isinstance(artifact_run, dict) or not _positive_integer(artifact_run.get("id"))
            or artifact_run.get("id") != run_id):
        raise ValueError("API artifact identity or run differs")
    archive_hash = hashlib.sha256(archive).hexdigest()
    if artifact.get("digest") != "sha256:" + archive_hash:
        raise ValueError("API artifact digest differs from downloaded archive")
    return {
        "slot_id": slot_id, "candidate": candidate,
        "checkpoint_obligation_class": "direct-r1-checkpoint",
        "repository": "banodoco/astrid-agent", "workflow": ".github/workflows/journey.yml",
        "workflow_id": run["workflow_id"], "run_id": run_id, "run_attempt": attempt,
        "job_id": job_id, "job_name": job["name"], "runner_name": job["runner_name"],
        "runner_labels": labels, "runner_os": RUNNER_PROVENANCE[contract["runner"]][0],
        "artifact_id": artifact_id, "artifact_name": artifact["name"],
        "artifact_sha256": archive_hash, "harness_sha": harness_sha,
        "admitted": False, "reason": M4_6_ORIGIN_NEEDS,
    }


def collect_m4_6_actions_index(candidate, slot_id, run_id, attempt, job_id, artifact_id,
                                harness_sha, token):
    """Fetch current API metadata and immutable artifact bytes, then veto admission."""
    if not token:
        raise ValueError("GH_TOKEN or GITHUB_TOKEN is required")
    root = "https://api.github.com/repos/banodoco/astrid-agent/actions"

    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not newurl.startswith("https://"):
                raise ValueError("artifact redirect must use HTTPS")
            redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
            if redirected is not None and not newurl.startswith("https://api.github.com/"):
                redirected.remove_header("Authorization")
            return redirected

    opener = urllib.request.build_opener(SafeRedirect())

    def fetch(path, *, binary=False):
        request = urllib.request.Request(root + path, headers={
            "Accept": "application/vnd.github+json", "Authorization": "Bearer " + token,
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with opener.open(request, timeout=30) as response:
            data = response.read(64 * 1024 * 1024 + 1)
        if len(data) > 64 * 1024 * 1024:
            raise ValueError("API response exceeds 64 MiB")
        return data if binary else json.loads(data)

    run = fetch(f"/runs/{run_id}")
    # The attempt-scoped jobs response may be paginated.
    jobs = []
    page = 1
    while True:
        payload = fetch(f"/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}")
        rows = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("malformed API jobs response")
        jobs.extend(rows)
        if len(rows) < 100:
            break
        page += 1
        if page > 100:
            raise ValueError("too many API job pages")
    artifact = fetch(f"/artifacts/{artifact_id}")
    archive = fetch(f"/artifacts/{artifact_id}/zip", binary=True)
    return m4_6_actions_index(candidate, slot_id, run_id, attempt, job_id, artifact_id,
                              harness_sha, run, jobs, artifact, archive)


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
        try:
            row = read(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: malformed journey receipt: {exc}")
            continue
        kind = row.get("receipt_kind")
        if kind == "release1-installed-lifecycle":
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


def _pinokio_snapshot_end_state_errors(row, obligation):
    """Require the Option-B R1 snapshot/end-state evidence for one Pinokio ID.

    These fields establish only the action-boundary/end-state assertion.  The
    separately inventoried gap-free custody and removal-authority-continuity
    claim is deferred and must not appear in this snapshot record.
    """
    errors = []
    snapshot = row.get("pinokio_snapshot_end_state")
    if not isinstance(snapshot, dict):
        return ["Pinokio action-boundary snapshot/end-state evidence is absent"]
    if snapshot.get("evidence_class") != "action-boundary-snapshots":
        errors.append("Pinokio evidence class must be action-boundary-snapshots")
    boundaries = snapshot.get("action_boundaries")
    if not isinstance(boundaries, dict):
        errors.append("Pinokio action-boundary snapshots are absent")
    else:
        for action in PINOKIO_R1_SNAPSHOT_ACTIONS:
            if boundaries.get(action) != "pass":
                errors.append(f"Pinokio {action} action-boundary snapshot did not pass")
    for assertion in (
        "bounded_polling",
        "independent_initial_census",
        "independent_final_census",
        "exactly_one_owner",
        "uninstall_end_state",
        "data_invariant",
    ):
        if snapshot.get(assertion) != "pass":
            errors.append(f"Pinokio {assertion} assertion did not pass")
    if obligation["id"].startswith("keep_data_uninstall:"):
        if snapshot.get("data_disposition") != "kept":
            errors.append("Pinokio keep-data uninstall did not retain data")
    scenario_id = obligation["id"].split(":", 1)[0]
    scenario_assertions = snapshot.get("scenario_assertions")
    if not isinstance(scenario_assertions, dict):
        errors.append(f"Pinokio {scenario_id} end-state assertions are absent")
    else:
        for assertion in PINOKIO_R1_SNAPSHOT_SCENARIO_ASSERTIONS[scenario_id]:
            if scenario_assertions.get(assertion) != "pass":
                errors.append(f"Pinokio {scenario_id} {assertion} assertion did not pass")
    for forbidden in ("custody_continuity", "gap_free_custody", "removal_authorization_continuity"):
        if forbidden in snapshot:
            errors.append(f"Pinokio snapshot evidence must not assert {forbidden}")
    return errors


def deferred_pinokio_continuity_statuses():
    """Expose, but never evaluate, the exact claims deferred from the R1 gate."""
    return tuple(
        {
            "id": row["id"],
            "assertion_id": row["assertion_id"],
            "status": "deferred-R1.x",
            "r1_gate": row["r1_gate"],
            "release_scope": row["release_scope"],
            "decision_pointer": row["decision_pointer"],
            "removal_authorization_relation": row["removal_authorization_relation"],
            "reasons": ("excluded from the R1 gate by Hannah's Option B decision",),
        }
        for row in DEFERRED_R1_X_PINOKIO_CONTINUITY_OBLIGATIONS
    )


def deferred_direct_containment_continuity_statuses():
    """Expose direct P13 exclusions; they are never evaluated as R1 passes."""
    return tuple(
        {
            "id": row["id"],
            "assertion_id": row["assertion_id"],
            "status": "deferred-R1.x",
            "r1_gate": row["r1_gate"],
            "release_scope": row["release_scope"],
            "decision_pointer": row["decision_pointer"],
            "removal_authorization_relation": row["removal_authorization_relation"],
            "reasons": (
                "excluded from the R1 gate by M-INT rule 36 P13; checkpoint evidence is not containment or continuous history",
            ),
        }
        for row in DEFERRED_R1_X_DIRECT_CONTAINMENT_CONTINUITY_OBLIGATIONS
    )


def evaluate_end_state_obligations(candidate, results):
    """Return the closed END-STATE inventory with fail-closed row statuses.

    This is a presentation of the existing receipt predicates, not another
    aggregate/verdict system.  A valid local-disposable M4-6 record remains
    ``not-run`` because the inventory requires hosted native-service evidence.
    """
    release1, m4_5, m4_6, errors = _visibility_receipts(results)
    release1_contracts = {row["job_id"]: row for row in RELEASE1_MATRIX}
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
        elif kind == "m4-6-snapshot-end-state":
            job_id = obligation["job_id"]
            entries = release1.get(job_id, [])
            if not entries:
                status = "not-run"
                row_errors.append("missing Pinokio action-boundary snapshot receipt")
            elif len(entries) != 1:
                status = "failed"
                row_errors.append("duplicate Pinokio action-boundary snapshot receipts")
            else:
                row_errors.extend(_release1_row_errors(candidate, release1_contracts[job_id], entries[0][0]))
                row_errors.extend(_pinokio_snapshot_end_state_errors(entries[0][0], obligation))
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
            "r1_gate": obligation.get("r1_gate", "required"),
            "status": status,
            "reasons": tuple(row_errors),
        })
    return statuses, errors


def format_end_state_obligations(statuses, deferred_statuses=()):
    lines = ["## Release-obligation visibility", ""]
    for row in statuses:
        reason = "; ".join(row["reasons"]) if row["reasons"] else "validated by the existing receipt predicate"
        lines.append(f"- [{row['status']}] {row['id']} ({row['phase']}): {reason}")
    if deferred_statuses:
        lines.extend(("", "## Deferred R1.x continuity obligations", ""))
        for row in deferred_statuses:
            lines.append(
                f"- [{row['status']}] {row['id']} ({row['removal_authorization_relation']}): "
                f"{row['release_scope']}; {row['decision_pointer']}; {'; '.join(row['reasons'])}"
            )
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
    parser.add_argument("--aggregate-release1", action="store_true",
                        help="require the exact nine Release-1 job receipts under --results")
    parser.add_argument("--aggregate-self-hosted-macos", action="store_true",
                        help="require one separately API-authenticated hannah-mac direct-macOS receipt")
    parser.add_argument("--aggregate-release1-with-self-hosted-macos", action="store_true",
                        help="require eight hosted Release-1 rows plus one authenticated direct-macOS receipt")
    parser.add_argument("--self-hosted-macos-receipt", type=Path,
                        help="downloaded direct-macOS journey-result.json, never the trusted context")
    parser.add_argument("--trusted-context", type=Path,
                        help="gate-built GitHub Actions API context for a self-hosted macOS receipt")
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
    parser.add_argument("--m4-6-api-index", action="store_true",
                        help="collect a diagnostic Actions index; always veto admission")
    parser.add_argument("--slot-id")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--run-attempt", type=int)
    parser.add_argument("--job-id", type=int)
    parser.add_argument("--artifact-id", type=int)
    parser.add_argument("--harness-sha")
    args = parser.parse_args()
    if args.m4_6_api_index:
        try:
            if not args.candidate:
                raise ValueError("--candidate is required")
            index = collect_m4_6_actions_index(
                read(args.candidate), args.slot_id, args.run_id, args.run_attempt,
                args.job_id, args.artifact_id, args.harness_sha,
                os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN"))
            print(json.dumps(index, indent=2, sort_keys=True))
            print("FAIL: " + M4_6_ORIGIN_NEEDS, file=sys.stderr)
        except (OSError, ValueError, TypeError) as exc:
            print("FAIL: " + str(exc), file=sys.stderr)
        return 1
    errors = []
    try:
        aggregate_modes = sum((args.aggregate_release1, args.aggregate_self_hosted_macos,
                               args.aggregate_release1_with_self_hosted_macos, args.aggregate_m4_4a,
                               args.aggregate_m4_5, args.aggregate_m4_4b_local,
                               args.aggregate_m4_6_local, args.aggregate_gpu))
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
            deferred_continuity_statuses = (
                *deferred_pinokio_continuity_statuses(),
                *deferred_direct_containment_continuity_statuses(),
            )
            errors.extend(visibility_errors)
            errors.extend(
                f"{row['id']}: obligation status {row['status']}"
                for row in obligation_statuses
                if row["r1_gate"] == "required" and row["status"] != "passed"
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
        if args.aggregate_gpu:
            if args.results is None or args.allow_source_override or args.required or args.prepare_only:
                raise ValueError("GPU aggregate requires --results and never admits overrides or subsets")
            errors.extend(evaluate_gpu_matrix(candidate, manifest, args.results))
        elif args.aggregate_self_hosted_macos:
            if (args.self_hosted_macos_receipt is None or args.trusted_context is None
                    or args.results is not None or args.expected_runner is not None
                    or args.expected_shell is not None or args.required or args.allow_source_override
                    or args.prepare_only):
                raise ValueError(
                    "self-hosted macOS aggregate requires only --candidate, --app-checkout, "
                    "--self-hosted-macos-receipt and --trusted-context"
                )
            errors.extend(evaluate_self_hosted_macos_receipt(
                candidate, args.self_hosted_macos_receipt, args.trusted_context
            ))
        elif args.aggregate_release1_with_self_hosted_macos:
            if (args.results is None or args.self_hosted_macos_receipt is None
                    or args.trusted_context is None or args.expected_runner is not None
                    or args.expected_shell is not None or args.required or args.allow_source_override
                    or args.prepare_only):
                raise ValueError(
                    "combined self-hosted macOS aggregate requires --candidate, --app-checkout, --results, "
                    "--self-hosted-macos-receipt and --trusted-context"
                )
            errors.extend(evaluate_release1_with_self_hosted_macos(
                candidate, args.results, args.self_hosted_macos_receipt, args.trusted_context
            ))
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
        elif (not args.aggregate_m4_4b_local and not args.aggregate_m4_6_local
              and not args.aggregate_self_hosted_macos
              and not args.aggregate_release1_with_self_hosted_macos and not args.prepare_only):
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
        report += "\n" + format_end_state_obligations(
            obligation_statuses, deferred_continuity_statuses
        )
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(report)
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
