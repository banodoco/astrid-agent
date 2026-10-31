"""Provisional Release-1 -> stand-in Release-2 journey contract.

This module deliberately contains no second lifecycle runner.  It freezes the
M4-5 scenario inputs and the receipt semantics that the shared TST driver and
the Pinokio adapters must eventually execute.  A source-level test can validate
the contract, but only a receipt labelled ``hosted-lifecycle`` can satisfy
``receipt_errors``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "release0"
STAND_IN_DESCRIPTOR = FIXTURE_ROOT / "release2" / "stand-in.json"
STAND_IN_MANIFEST = FIXTURE_ROOT / "release2" / "installable-stand-in.json"
FIXTURE_SUMS = FIXTURE_ROOT / "SHA256SUMS"

SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
CHECKSUM_LINE = re.compile(r"^([0-9a-f]{64})  ([^/].*)$")

RELEASE1_CANDIDATE = {
    "app": "33f1de3f33dfb1e9799ca745c5c3b8437173e06e",
    "agent": "3cc84be5d8c565f1a4a500dcf713478498e34885",
    "runtime": "a3e3f2ed95d6363e7f5b463b521da94221cf6f00",
    "pinokio": "c5b42701641889ecb916c60cde09a717f83d62ff",
}

STAND_IN_SUMS_SHA256 = "6f6dc795391577933e5f8451b814b77d58a58c41b01ad86b0fcf8d32bddbe93a"
STAND_IN_DESCRIPTOR_SHA256 = "4b30c4d1cf8703bbb6efa65ae7bfafc7b28f5e410ccb411084c88e4f79512668"
STAND_IN_MANIFEST_SHA256 = "332458a5dda4408f392ca3c6fca59647000afec875c9769cfd400dc929aaf9b9"
STAND_IN_MARKER = "M4-5 RELEASE 2 INSTALLABLE STAND-IN — UPDATE LANDED"
STAND_IN_DESCRIPTOR_MARKER = "M4-2 RELEASE 2 STAND-IN — UPDATE LANDED"
STAND_IN_AGENT = "2958bacea3e3c7961788fa9c625dd9db0be282de"
STAND_IN_RUNTIME = "ebd412199a9c1e53c3a984d27d335de8b40654a1"
STAND_IN_SCHEMA = "sha256:5d229bf66ffaeb0b4937844a3c2b08ddd21f97cbcc305101a5cc19a95c971194"
STAND_IN_VIBECOMFY = "ac0ee1b80991f2414dae27d264af4b5e2890a7a6"

RUNNERS = {
    "macos": {
        "runner": "macos-14",
        "shell": "sh",
        "runner_os": "macOS",
        "image_os": "macos14",
        "manager": "launchd",
        "native_system": "darwin",
    },
    "linux": {
        "runner": "ubuntu-24.04",
        "shell": "sh",
        "runner_os": "Linux",
        "image_os": "ubuntu24",
        "manager": "systemd-user",
        "native_system": "linux",
    },
    "windows": {
        "runner": "windows-2022",
        "shell": "powershell.exe",
        "runner_os": "Windows",
        "image_os": "win22",
        "manager": "task-scheduler",
        "native_system": "windows",
    },
}

CHANNEL_MODES = {
    "button": ("direct",),
    "install-line": ("direct",),
    "pinokio-update": ("pinokio-backend", "pinokio-fully-local"),
}


def _scenario_rows() -> tuple[dict, ...]:
    rows = []
    for os_name, runner in RUNNERS.items():
        for channel, mode in (
            ("button", "direct"),
            ("install-line", "direct"),
            ("pinokio-update", "pinokio-backend"),
            ("pinokio-update", "pinokio-fully-local"),
        ):
            controls = []
            if channel == "button":
                controls.append("lost-acknowledgement")
                if os_name == "linux":
                    controls.append("unsaved-draft")
            rows.append(
                {
                    "scenario_id": f"release2-{channel}-{mode}-{os_name}",
                    "scenario": "release1-to-release2",
                    "job_id": f"{mode}-{os_name}",
                    "channel": channel,
                    "mode": mode,
                    "os": os_name,
                    "runner": runner["runner"],
                    "shell": runner["shell"],
                    "controls": tuple(controls),
                }
            )
    return tuple(rows)


RELEASE2_SCENARIOS = _scenario_rows()
RELEASE2_SCENARIO_IDS = tuple(row["scenario_id"] for row in RELEASE2_SCENARIOS)


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scenario_definition(scenario_id: str) -> dict:
    """Return a copy of one fixed M4-5 scenario definition."""
    for row in RELEASE2_SCENARIOS:
        if row["scenario_id"] == scenario_id:
            return deepcopy(row)
    raise ValueError(f"Unknown Release-2 scenario: {scenario_id}")


def verified_stand_in(
    manifest_path: Path = STAND_IN_MANIFEST,
    descriptor_path: Path = STAND_IN_DESCRIPTOR,
    sums_path: Path = FIXTURE_SUMS,
) -> dict:
    """Authenticate the checked-in M4-2/M4-5 stand-in without fallback."""
    supplied = (manifest_path, descriptor_path, sums_path)
    expected = (STAND_IN_MANIFEST, STAND_IN_DESCRIPTOR, FIXTURE_SUMS)
    for path, canonical in zip(supplied, expected, strict=True):
        if path.is_symlink() or not path.is_file():
            raise ValueError("Release-2 stand-in inputs must be regular checked-in files")
        if path.resolve(strict=True) != canonical.resolve(strict=True):
            raise ValueError("Release-2 stand-in inputs must be the checked-in frozen fixture")
    if _digest(sums_path) != STAND_IN_SUMS_SHA256:
        raise ValueError("Release-2 parent checksum manifest differs from the approved fixture")

    entries = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        matched = CHECKSUM_LINE.fullmatch(line)
        if matched is None:
            raise ValueError("Release-2 parent checksum manifest contains an invalid entry")
        digest, relative = matched.groups()
        if relative in entries or ".." in Path(relative).parts:
            raise ValueError("Release-2 parent checksum manifest contains an unsafe entry")
        entries[relative] = digest
    expected_entries = {
        "release2/stand-in.json": STAND_IN_DESCRIPTOR_SHA256,
        "release2/installable-stand-in.json": STAND_IN_MANIFEST_SHA256,
    }
    if any(entries.get(name) != digest for name, digest in expected_entries.items()):
        raise ValueError("Release-2 fixture digests are absent or changed")
    if (
        _digest(descriptor_path) != STAND_IN_DESCRIPTOR_SHA256
        or _digest(manifest_path) != STAND_IN_MANIFEST_SHA256
    ):
        raise ValueError("Release-2 stand-in bytes differ from their frozen digests")

    descriptor = _read_json(descriptor_path)
    if descriptor != {
        "fixture_format": "astrid-release-stand-in/v1",
        "release": 2,
        "source": {"kind": "M4-2 qualification stand-in", "release_1_agent_base": STAND_IN_AGENT},
        "marker": {"path": "release2/stand-in.json", "value": STAND_IN_DESCRIPTOR_MARKER},
    }:
        raise ValueError("Release-2 descriptor identity or marker differs from M4-2")
    manifest = _read_json(manifest_path)
    if (
        manifest.get("schema_version") != 1
        or manifest.get("release") != 2
        or manifest.get("qualification_marker") != STAND_IN_MARKER
        or (manifest.get("agent") or {}).get("sha") != STAND_IN_AGENT
        or (manifest.get("runtime") or {}).get("sha") != STAND_IN_RUNTIME
        or (manifest.get("runtime") or {}).get("schema_digest") != STAND_IN_SCHEMA
        or manifest.get("vibecomfy") != {"sha": STAND_IN_VIBECOMFY}
        or not isinstance(manifest.get("tools"), dict)
        or not manifest["tools"]
    ):
        raise ValueError("Release-2 installable stand-in identity or marker differs from M4-2")
    canonical = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if manifest_path.read_bytes() != canonical:
        raise ValueError("Release-2 stand-in differs from the installer's canonical manifest")
    return {
        "fixture_sums_sha256": STAND_IN_SUMS_SHA256,
        "descriptor_sha256": STAND_IN_DESCRIPTOR_SHA256,
        "manifest_sha256": STAND_IN_MANIFEST_SHA256,
        "release": 2,
        "qualification_marker": STAND_IN_MARKER,
        "target": {
            "agent": STAND_IN_AGENT,
            "runtime": STAND_IN_RUNTIME,
            "schema_digest": STAND_IN_SCHEMA,
        },
    }


def _candidate(value: Mapping[str, object]) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("Release-1 candidate input must be an object")
    observed = {name: value.get(name) for name in RELEASE1_CANDIDATE}
    for name, expected in RELEASE1_CANDIDATE.items():
        if not isinstance(observed[name], str) or not SHA.fullmatch(observed[name]):
            raise ValueError(f"Release-1 candidate {name} must be a full commit SHA")
        if observed[name] != expected:
            raise ValueError(f"Release-1 candidate {name} differs from the provisional M4-5 tuple")
    return observed


def validate_admission(
    *,
    scenario_id: str,
    candidate: Mapping[str, object],
    stand_in_marker: str,
    channel: str,
    mode: str,
    os_name: str,
    job_id: str,
    runner: str,
    shell: str,
    source_override: bool = False,
    stand_in_manifest: Path = STAND_IN_MANIFEST,
    stand_in_descriptor: Path = STAND_IN_DESCRIPTOR,
    fixture_sums: Path = FIXTURE_SUMS,
) -> dict:
    """Fail closed unless every independently supplied scenario input agrees."""
    row = scenario_definition(scenario_id)
    supplied = {
        "channel": channel,
        "mode": mode,
        "os": os_name,
        "job_id": job_id,
        "runner": runner,
        "shell": shell,
    }
    if any(not isinstance(value, str) or not value for value in supplied.values()):
        raise ValueError("Release-2 channel, mode, OS, job, runner and shell inputs are required")
    for name, value in supplied.items():
        if value != row[name]:
            raise ValueError(f"Release-2 {name} input differs from scenario {scenario_id}")
    if channel not in CHANNEL_MODES or mode not in CHANNEL_MODES[channel]:
        raise ValueError("Release-2 channel/mode ownership is not admitted")
    if source_override is not False:
        raise ValueError("Release-2 candidate source override is not admissible")
    if stand_in_marker != STAND_IN_MARKER:
        raise ValueError("Release-2 qualification marker is missing or changed")
    return {
        "schema_version": 1,
        "receipt_kind": "release2-scenario-admission",
        **row,
        "candidate": _candidate(candidate),
        "stand_in": verified_stand_in(stand_in_manifest, stand_in_descriptor, fixture_sums),
        "source_override_test_mode": False,
    }


def _control_map(value: object, errors: list[str]) -> dict[tuple[str, str], dict]:
    if not isinstance(value, list):
        errors.append("controls must be a list")
        return {}
    controls = {}
    for item in value:
        if not isinstance(item, dict):
            errors.append("control receipt is not an object")
            continue
        key = (item.get("control"), item.get("polarity"))
        if not all(isinstance(part, str) for part in key) or key in controls:
            errors.append("control receipt has a missing or duplicate identity")
            continue
        controls[key] = item
    return controls


def _button_errors(row: Mapping[str, object], scenario: dict) -> list[str]:
    errors = []
    trigger = row.get("trigger")
    runner = RUNNERS[scenario["os"]]
    native = trigger.get("native_job") if isinstance(trigger, dict) else None
    if (
        not isinstance(trigger, dict)
        or trigger.get("channel") != "button"
        or trigger.get("entrypoint") != "post-v1-update"
        or trigger.get("request") != "explicit-update"
        or trigger.get("draft_flush") != "pass"
    ):
        errors.append("button trigger does not record the explicit guarded Update request")
    if (
        not isinstance(native, dict)
        or native.get("format") != "astrid-update-platform/v1"
        or native.get("system") != runner["native_system"]
        or native.get("one_shot") is not True
        or native.get("owned") is not True
        or native.get("acknowledged") is not True
        or not isinstance(native.get("operation_id"), str)
        or not native["operation_id"]
    ):
        errors.append("button trigger lacks the owned acknowledged native one-shot job")

    controls = _control_map(row.get("controls"), errors)
    negative = controls.get(("lost-acknowledgement", "negative"), {})
    positive = controls.get(("lost-acknowledgement", "positive"), {})
    if (
        negative.get("states") != ["active", "recovery-needed"]
        or negative.get("automatic_update_requests") != 0
        or negative.get("terminal_inferred") is not False
        or not isinstance(negative.get("operation_id"), str)
        or not negative.get("operation_id")
    ):
        errors.append("lost-ack negative control does not stop at recovery-needed without retry")
    if (
        positive.get("states") != ["recovery-needed", "active", "terminal"]
        or positive.get("action") != "explicit-update"
        or positive.get("reconciled_under_lock") is not True
        or positive.get("logical_acceptances") != 1
        or positive.get("new_operation_id") is not False
        or positive.get("result") != "updated"
        or positive.get("operation_id") != negative.get("operation_id")
    ):
        errors.append("lost-ack positive control is not one locked explicit-Update reconciliation")

    expected_keys = {("lost-acknowledgement", "negative"), ("lost-acknowledgement", "positive")}
    if scenario["os"] == "linux":
        draft_negative = controls.get(("unsaved-draft", "negative"), {})
        draft_positive = controls.get(("unsaved-draft", "positive"), {})
        if (
            draft_negative.get("surface") != "browser"
            or draft_negative.get("save") != "failed"
            or draft_negative.get("update_requests") != 0
            or draft_negative.get("native_jobs") != 0
            or draft_negative.get("installed_release") != 1
        ):
            errors.append("unsaved-draft negative control did not refuse before the update request")
        if (
            draft_positive.get("surface") != "browser"
            or draft_positive.get("save") != "pass"
            or draft_positive.get("update_requests") != 1
            or draft_positive.get("native_jobs") != 1
            or draft_positive.get("installed_release") != 2
        ):
            errors.append("unsaved-draft positive control did not continue after a guarded save")
        expected_keys.update({("unsaved-draft", "negative"), ("unsaved-draft", "positive")})
    if set(controls) != expected_keys:
        errors.append("button control set differs from the required negative/positive controls")
    return errors


def _trigger_errors(row: Mapping[str, object], scenario: dict) -> list[str]:
    if scenario["channel"] == "button":
        return _button_errors(row, scenario)
    errors = []
    trigger = row.get("trigger")
    if not isinstance(trigger, dict):
        return ["trigger receipt is absent"]
    if scenario["channel"] == "install-line":
        if (
            trigger.get("channel") != "install-line"
            or trigger.get("entrypoint") != "public-bootstrap"
            or trigger.get("update_flag") is not True
            or trigger.get("manifest_sha256") != STAND_IN_MANIFEST_SHA256
            or trigger.get("root_bound") is not True
            or trigger.get("data_root_bound") is not True
        ):
            errors.append(
                "install-line trigger lacks the explicit manifest/root/data --update inputs"
            )
    else:
        route = (
            "backend-adapter" if scenario["mode"] == "pinokio-backend" else "existing-fully-local"
        )
        provenance = trigger.get("action_provenance")
        if (
            trigger.get("channel") != "pinokio-update"
            or trigger.get("entrypoint") != "pinokio-update"
            or trigger.get("managed_by") != "pinokio"
            or trigger.get("mode") != scenario["mode"]
            or trigger.get("route") != route
            or trigger.get("foreground_fallback") is not False
            or not isinstance(provenance, dict)
            or provenance.get("pinokio_sha") != RELEASE1_CANDIDATE["pinokio"]
            or not DIGEST.fullmatch(str(provenance.get("action_sha256", "")))
        ):
            errors.append("Pinokio Update trigger lacks exact owner, route or action provenance")
    if row.get("controls") != []:
        errors.append("non-button scenarios must not claim button-only controls")
    return errors


def receipt_errors(row: Mapping[str, object]) -> list[str]:
    """Return every fail-closed M4-5 receipt-shape error."""
    if not isinstance(row, Mapping):
        return ["receipt is not an object"]
    errors = []
    try:
        scenario = scenario_definition(str(row.get("scenario_id", "")))
    except ValueError as exc:
        return [str(exc)]
    for field in ("scenario", "job_id", "channel", "mode", "os", "runner", "shell"):
        if row.get(field) != scenario[field]:
            errors.append(f"{field} does not match the admitted Release-2 scenario")
    if row.get("schema_version") != 1 or row.get("receipt_kind") != "release2-installed-lifecycle":
        errors.append("receipt schema/kind is not the Release-2 lifecycle contract")
    if row.get("status") != "pass":
        errors.append("receipt status is not pass")
    if row.get("evidence_kind") != "hosted-lifecycle":
        errors.append("source or synthetic evidence cannot satisfy a hosted lifecycle receipt")
    if row.get("source_override_test_mode") is not False:
        errors.append("source override is not admissible")
    try:
        if _candidate(row.get("candidate")) != RELEASE1_CANDIDATE:
            errors.append("Release-1 candidate is not exact")
    except ValueError as exc:
        errors.append(str(exc))
    checkouts = row.get("candidate_checkouts")
    if not isinstance(checkouts, dict) or any(
        (checkouts.get(name) or {}).get("sha") != sha for name, sha in RELEASE1_CANDIDATE.items()
    ):
        errors.append("exact Release-1 candidate checkout receipt is absent")
    try:
        if row.get("stand_in") != verified_stand_in():
            errors.append("stand-in receipt differs from the frozen fixture")
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    if row.get("release_transition") != {"from": 1, "to": 2, "stand_in": True}:
        errors.append("exact Release-1 to stand-in Release-2 transition is absent")
    installed = row.get("installed")
    if (
        not isinstance(installed, dict)
        or installed.get("release") != 2
        or installed.get("qualification_marker") != STAND_IN_MARKER
        or installed.get("manifest_sha256") != STAND_IN_MANIFEST_SHA256
        or installed.get("agent") != STAND_IN_AGENT
        or installed.get("runtime") != STAND_IN_RUNTIME
        or installed.get("schema_digest") != STAND_IN_SCHEMA
    ):
        errors.append("installed stand-in identity or visible marker is absent")

    runner = RUNNERS[scenario["os"]]
    provenance = row.get("runner_provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("RUNNER_OS") != runner["runner_os"]
        or provenance.get("ImageOS") != runner["image_os"]
        or not isinstance(provenance.get("ImageVersion"), str)
        or not provenance.get("ImageVersion")
    ):
        errors.append("hosted runner provenance does not match the scenario")
    os_build = row.get("os_build")
    if not isinstance(os_build, str) or not os_build.startswith(runner["runner_os"] + "-"):
        errors.append("OS build does not match the scenario")

    owner = row.get("service_manager_prerequisite")
    if scenario["mode"] == "direct":
        if (
            not isinstance(owner, dict)
            or owner.get("required") is not True
            or owner.get("outcome") != "pass"
            or owner.get("manager") != runner["manager"]
            or not isinstance(owner.get("version"), str)
            or not owner.get("version")
            or owner.get("foreground_fallback") is not False
        ):
            errors.append("direct native service-manager prerequisite did not pass")
    elif (
        not isinstance(owner, dict)
        or owner.get("required") is not False
        or owner.get("outcome") != "explicit-owner"
        or owner.get("managed_by") != "pinokio"
        or owner.get("foreground_fallback") is not False
    ):
        errors.append("Pinokio ownership prerequisite is not explicit")

    semantic = row.get("semantic_data")
    if (
        not isinstance(semantic, dict)
        or semantic.get("identical") is not True
        or not DIGEST.fullmatch(str(semantic.get("before_sha256", "")))
        or semantic.get("before_sha256") != semantic.get("after_sha256")
    ):
        errors.append("semantic data preservation is absent or unequal")
    required_checks = {
        "release",
        "marker",
        "service",
        "health",
        "semantic_data",
        "task_settled",
        "one_runtime",
        "unrelated_sentinel",
    }
    if scenario["channel"] == "button":
        required_checks.update({"native_one_shot", "lost_ack_controls"})
        if scenario["os"] == "linux":
            required_checks.add("draft_controls")
    elif scenario["channel"] == "install-line":
        required_checks.add("install_line")
    else:
        required_checks.add("pinokio_update")
    checks = row.get("checks")
    if not isinstance(checks, dict) or any(checks.get(name) != "pass" for name in required_checks):
        errors.append("one or more required Release-2 assertions did not pass")
    errors.extend(_trigger_errors(row, scenario))
    if row.get("final_cleanup") != "pass":
        errors.append("final cleanup did not pass")
    return errors


def validate_receipt(row: Mapping[str, object]) -> dict:
    """Return a receipt copy, or raise with all shape errors."""
    errors = receipt_errors(row)
    if errors:
        raise ValueError("; ".join(errors))
    return deepcopy(dict(row))
