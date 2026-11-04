"""Execute the provisional Release-1 -> stand-in Release-2 journeys.

``journey_release2`` owns the accepted scenario and receipt contract.  This
module owns the missing executable half: it invokes a supplied platform
boundary once per declared action, validates every observation, and writes the
ordered action log into the lifecycle receipt.

Disposable boundaries are intentionally useful for focused tests, but their
receipts are labelled ``disposable-executable``/``exercised``.  Only a boundary
running on the matching hosted runner and attesting installed-executable
observations can produce ``hosted-lifecycle``/``pass``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path

if __package__:
    from . import journey_release2 as release2
else:
    import journey_release2 as release2

BOUNDARY_PROTOCOL = "astrid-release2-boundary/v1"
DISPOSABLE_EVIDENCE = "disposable-executable"
HOSTED_EVIDENCE = "hosted-lifecycle"
EVIDENCE_KINDS = (DISPOSABLE_EVIDENCE, HOSTED_EVIDENCE)


class ExecutionError(RuntimeError):
    """The executable boundary or one of its observations was invalid."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_receipt(path: Path, receipt: Mapping[str, object]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _mapping(value: object, label: str) -> dict:
    if not isinstance(value, Mapping):
        raise ExecutionError(f"{label} must be an object")
    return dict(value)


def _expect(value: object, expected: object, label: str) -> None:
    if value != expected:
        raise ExecutionError(f"{label} was {value!r}; expected {expected!r}")


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or release2.DIGEST.fullmatch(value) is None:
        raise ExecutionError(f"{label} must be a SHA-256 digest")
    return value


def _absolute_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    resolved = path.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    if not resolved.is_dir():
        raise ValueError(f"{label} must be a directory")
    return resolved


def _command_identity(command: Sequence[str]) -> dict:
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("boundary command must contain non-empty strings")
    selected = command[0]
    executable = shutil.which(selected) if os.sep not in selected else selected
    if not executable:
        raise ValueError(f"boundary executable was not found: {selected}")
    path = Path(executable)
    if path.is_symlink():
        path = path.resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"boundary executable is not an executable regular file: {path}")
    executable_sha256 = _sha256_bytes(path.read_bytes())
    command_sha256 = _sha256_bytes(_canonical_bytes(list(command)))
    return {
        "argv": list(command),
        "executable": str(path.resolve()),
        "executable_sha256": executable_sha256,
        "command_sha256": command_sha256,
    }


def _runner_provenance(
    *, scenario: Mapping[str, object], evidence_kind: str, environment: Mapping[str, str]
) -> dict:
    runner = release2.RUNNERS[str(scenario["os"])]
    if evidence_kind == DISPOSABLE_EVIDENCE:
        return {
            "RUNNER_OS": runner["runner_os"],
            "ImageOS": runner["image_os"],
            "ImageVersion": "disposable-executable",
        }
    if evidence_kind != HOSTED_EVIDENCE:
        raise ValueError(f"unsupported evidence kind: {evidence_kind}")
    if (
        environment.get("CI", "").lower() != "true"
        or environment.get("GITHUB_ACTIONS", "").lower() != "true"
    ):
        raise ExecutionError("hosted lifecycle evidence requires the GitHub Actions CI boundary")
    provenance = {
        "RUNNER_OS": environment.get("RUNNER_OS"),
        "ImageOS": environment.get("ImageOS"),
        "ImageVersion": environment.get("ImageVersion"),
    }
    _expect(provenance["RUNNER_OS"], runner["runner_os"], "hosted RUNNER_OS")
    _expect(provenance["ImageOS"], runner["image_os"], "hosted ImageOS")
    image_version = provenance["ImageVersion"]
    if not isinstance(image_version, str) or not image_version:
        raise ExecutionError("hosted ImageVersion is required")
    if any(word in image_version.lower() for word in ("synthetic", "source", "shape-only")):
        raise ExecutionError("hosted ImageVersion cannot be synthetic or source-only")
    return provenance


class ExecutableBoundary:
    """JSON-over-stdin boundary with an ordered, hash-tied action log."""

    def __init__(
        self,
        *,
        command: Sequence[str],
        scenario: Mapping[str, object],
        inputs: Mapping[str, object],
        evidence_kind: str,
        environment: Mapping[str, str],
        timeout: float,
    ) -> None:
        self.command = tuple(command)
        self.identity = _command_identity(self.command)
        self.scenario = dict(scenario)
        self.inputs = dict(inputs)
        self.evidence_kind = evidence_kind
        self.environment = dict(environment)
        self.timeout = timeout
        self.actions: list[dict] = []

    def invoke(self, action: str, payload: Mapping[str, object] | None = None) -> dict:
        sequence = len(self.actions) + 1
        request = {
            "protocol": BOUNDARY_PROTOCOL,
            "sequence": sequence,
            "action": action,
            "scenario": {
                name: self.scenario[name]
                for name in ("scenario_id", "channel", "mode", "os", "job_id")
            },
            "inputs": deepcopy(self.inputs),
            "payload": deepcopy(dict(payload or {})),
        }
        encoded_request = _canonical_bytes(request)
        process = subprocess.run(
            self.command,
            input=encoded_request.decode("utf-8") + "\n",
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
            env=dict(self.environment),
        )
        if process.returncode != 0:
            stderr = process.stderr.strip()
            raise ExecutionError(
                f"boundary action {action} exited {process.returncode}"
                + (f": {stderr}" if stderr else "")
            )
        try:
            response = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise ExecutionError(f"boundary action {action} returned invalid JSON") from exc
        response = _mapping(response, f"boundary action {action} response")
        _expect(response.get("protocol"), BOUNDARY_PROTOCOL, f"{action} response protocol")
        _expect(response.get("sequence"), sequence, f"{action} response sequence")
        _expect(response.get("action"), action, f"{action} response action")
        _expect(response.get("status"), "pass", f"{action} response status")
        evidence = _mapping(response.get("evidence"), f"{action} executable evidence")
        expected_boundary_kind = (
            "installed-executable"
            if self.evidence_kind == HOSTED_EVIDENCE
            else "disposable-executable"
        )
        _expect(evidence.get("kind"), expected_boundary_kind, f"{action} executable evidence kind")
        _expect(evidence.get("synthetic"), False, f"{action} synthetic evidence flag")
        _expect(evidence.get("source_only"), False, f"{action} source-only evidence flag")
        observation = _mapping(response.get("observation"), f"{action} observation")
        self.actions.append(
            {
                "sequence": sequence,
                "action": action,
                "scenario": deepcopy(request["scenario"]),
                "payload": deepcopy(request["payload"]),
                "observation": deepcopy(observation),
                "request_sha256": _sha256_bytes(encoded_request),
                "response_sha256": _sha256_bytes(_canonical_bytes(response)),
                "boundary_command_sha256": self.identity["command_sha256"],
                "evidence": evidence,
            }
        )
        return observation


def _required_checks(scenario: Mapping[str, object]) -> set[str]:
    checks = {
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
        checks.update({"native_one_shot", "lost_ack_controls"})
        if scenario["os"] == "linux":
            checks.add("draft_controls")
    elif scenario["channel"] == "install-line":
        checks.add("install_line")
    else:
        checks.add("pinokio_update")
    return checks


def _initial_receipt(
    *,
    admission: Mapping[str, object],
    evidence_kind: str,
    provenance: Mapping[str, object],
    boundary: ExecutableBoundary,
) -> dict:
    scenario_fields = {
        name: deepcopy(admission[name])
        for name in (
            "scenario_id",
            "scenario",
            "job_id",
            "channel",
            "mode",
            "os",
            "runner",
            "shell",
        )
    }
    return {
        "schema_version": 1,
        "receipt_kind": "release2-installed-lifecycle",
        **scenario_fields,
        "status": "running",
        "evidence_kind": evidence_kind,
        "source_override_test_mode": False,
        "candidate": deepcopy(admission["candidate"]),
        "candidate_checkouts": {},
        "stand_in": deepcopy(admission["stand_in"]),
        "release_transition": {"from": 1, "to": 2, "stand_in": True},
        "installed": {},
        "runner_provenance": dict(provenance),
        "os_build": "not-run",
        "service_manager_prerequisite": {},
        "semantic_data": {},
        "checks": {name: "not-run" for name in sorted(_required_checks(admission))},
        "controls": [],
        "trigger": {},
        "actions": [],
        "execution_boundary": deepcopy(boundary.identity),
        "final_cleanup": "not-run",
    }


def _inspect_release1(
    *, receipt: dict, boundary: ExecutableBoundary, scenario: Mapping[str, object]
) -> str:
    observed = boundary.invoke("inspect-release1")
    _expect(observed.get("release"), 1, "initial installed release")
    candidate_checkouts = _mapping(observed.get("candidate_checkouts"), "candidate checkouts")
    for name, sha in release2.RELEASE1_CANDIDATE.items():
        checkout = _mapping(candidate_checkouts.get(name), f"{name} candidate checkout")
        _expect(checkout.get("sha"), sha, f"{name} candidate checkout SHA")
    semantic = _mapping(observed.get("semantic_data"), "initial semantic data")
    before_sha = _digest(semantic.get("sha256"), "initial semantic data digest")
    runner = release2.RUNNERS[str(scenario["os"])]
    os_build = observed.get("os_build")
    if not isinstance(os_build, str) or not os_build.startswith(runner["runner_os"] + "-"):
        raise ExecutionError("initial OS build does not match the admitted scenario")
    if receipt["evidence_kind"] == HOSTED_EVIDENCE and any(
        word in os_build.lower() for word in ("synthetic", "source", "disposable")
    ):
        raise ExecutionError("hosted OS build cannot be synthetic, source-only, or disposable")
    receipt["candidate_checkouts"] = candidate_checkouts
    receipt["os_build"] = os_build
    if scenario["mode"] == "direct":
        service = _mapping(observed.get("service_manager"), "native service manager")
        _expect(service.get("manager"), runner["manager"], "native service manager")
        if not isinstance(service.get("version"), str) or not service["version"]:
            raise ExecutionError("native service-manager version is required")
        _expect(service.get("foreground_fallback"), False, "foreground fallback")
        receipt["service_manager_prerequisite"] = {
            "required": True,
            "outcome": "pass",
            "manager": service["manager"],
            "version": service["version"],
            "foreground_fallback": False,
        }
    else:
        receipt["service_manager_prerequisite"] = {
            "required": False,
            "outcome": "explicit-owner",
            "managed_by": "pinokio",
            "foreground_fallback": False,
        }
    return before_sha


def _execute_button(
    *, receipt: dict, boundary: ExecutableBoundary, scenario: Mapping[str, object]
) -> None:
    controls = []
    if scenario["os"] == "linux":
        draft = boundary.invoke(
            "button-unsaved-draft",
            {"surface": "browser", "save": "failed", "request": "explicit-update"},
        )
        expected = {
            "surface": "browser",
            "save": "failed",
            "update_requests": 0,
            "native_jobs": 0,
            "installed_release": 1,
        }
        for name, value in expected.items():
            _expect(draft.get(name), value, f"unsaved-draft negative {name}")
        controls.append({"control": "unsaved-draft", "polarity": "negative", **expected})

    lost = boundary.invoke(
        "button-update-lost-ack",
        {"request": "explicit-update", "draft_flush": "pass", "simulate_lost_ack": True},
    )
    _expect(lost.get("states"), ["active", "recovery-needed"], "lost-ack states")
    _expect(lost.get("automatic_update_requests"), 0, "lost-ack automatic requests")
    _expect(lost.get("terminal_inferred"), False, "lost-ack terminal inference")
    operation_id = lost.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        raise ExecutionError("lost-ack operation ID is required")
    controls.append(
        {
            "control": "lost-acknowledgement",
            "polarity": "negative",
            "operation_id": operation_id,
            "states": lost["states"],
            "automatic_update_requests": 0,
            "terminal_inferred": False,
        }
    )

    recovered = boundary.invoke(
        "button-update-explicit",
        {"request": "explicit-update", "operation_id": operation_id},
    )
    expected_recovery = {
        "states": ["recovery-needed", "active", "terminal"],
        "action": "explicit-update",
        "reconciled_under_lock": True,
        "logical_acceptances": 1,
        "new_operation_id": False,
        "result": "updated",
        "operation_id": operation_id,
    }
    for name, value in expected_recovery.items():
        _expect(recovered.get(name), value, f"lost-ack explicit recovery {name}")
    controls.append(
        {
            "control": "lost-acknowledgement",
            "polarity": "positive",
            **expected_recovery,
        }
    )
    native_job = _mapping(recovered.get("native_job"), "button native one-shot job")
    runner = release2.RUNNERS[str(scenario["os"])]
    expected_native = {
        "format": "astrid-update-platform/v1",
        "system": runner["native_system"],
        "one_shot": True,
        "owned": True,
        "acknowledged": True,
        "operation_id": operation_id,
    }
    for name, value in expected_native.items():
        _expect(native_job.get(name), value, f"button native job {name}")
    if scenario["os"] == "linux":
        positive = _mapping(recovered.get("draft_control"), "unsaved-draft positive control")
        expected = {
            "surface": "browser",
            "save": "pass",
            "update_requests": 1,
            "native_jobs": 1,
            "installed_release": 2,
        }
        for name, value in expected.items():
            _expect(positive.get(name), value, f"unsaved-draft positive {name}")
        controls.append({"control": "unsaved-draft", "polarity": "positive", **expected})
    receipt["controls"] = controls
    receipt["trigger"] = {
        "channel": "button",
        "entrypoint": "post-v1-update",
        "request": "explicit-update",
        "draft_flush": "pass",
        "native_job": expected_native,
    }


def _execute_install_line(*, receipt: dict, boundary: ExecutableBoundary) -> None:
    observed = boundary.invoke(
        "install-line-update",
        {
            "update_flag": True,
            "manifest_sha256": release2.STAND_IN_MANIFEST_SHA256,
            "root_bound": True,
            "data_root_bound": True,
        },
    )
    expected = {
        "channel": "install-line",
        "entrypoint": "public-bootstrap",
        "update_flag": True,
        "manifest_sha256": release2.STAND_IN_MANIFEST_SHA256,
        "root_bound": True,
        "data_root_bound": True,
    }
    for name, value in expected.items():
        _expect(observed.get(name), value, f"install-line {name}")
    receipt["trigger"] = expected


def _execute_pinokio(
    *, receipt: dict, boundary: ExecutableBoundary, scenario: Mapping[str, object]
) -> None:
    route = "backend-adapter" if scenario["mode"] == "pinokio-backend" else "existing-fully-local"
    observed = boundary.invoke(
        "pinokio-update",
        {
            "mode": scenario["mode"],
            "route": route,
            "foreground_fallback": False,
        },
    )
    expected = {
        "channel": "pinokio-update",
        "entrypoint": "pinokio-update",
        "managed_by": "pinokio",
        "mode": scenario["mode"],
        "route": route,
        "foreground_fallback": False,
    }
    for name, value in expected.items():
        _expect(observed.get(name), value, f"Pinokio update {name}")
    action_sha256 = _digest(observed.get("action_sha256"), "Pinokio action provenance")
    receipt["trigger"] = {
        **expected,
        "action_provenance": {
            "pinokio_sha": release2.RELEASE1_CANDIDATE["pinokio"],
            "action_sha256": action_sha256,
        },
    }


def _inspect_release2(
    *,
    receipt: dict,
    boundary: ExecutableBoundary,
    before_sha: str,
) -> None:
    observed = boundary.invoke("inspect-release2")
    installed = _mapping(observed.get("installed"), "installed stand-in identity")
    expected = {
        "release": 2,
        "qualification_marker": release2.STAND_IN_MARKER,
        "manifest_sha256": release2.STAND_IN_MANIFEST_SHA256,
        "agent": release2.STAND_IN_AGENT,
        "runtime": release2.STAND_IN_RUNTIME,
        "schema_digest": release2.STAND_IN_SCHEMA,
    }
    for name, value in expected.items():
        _expect(installed.get(name), value, f"installed stand-in {name}")
    semantic = _mapping(observed.get("semantic_data"), "final semantic data")
    after_sha = _digest(semantic.get("sha256"), "final semantic data digest")
    _expect(after_sha, before_sha, "preserved semantic data digest")
    for name in ("service", "health", "task_settled", "one_runtime", "unrelated_sentinel"):
        _expect(observed.get(name), "pass", f"final {name} check")
    receipt["installed"] = expected
    receipt["semantic_data"] = {
        "identical": True,
        "before_sha256": before_sha,
        "after_sha256": after_sha,
    }


def execute_journey(
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
    stand_in_manifest: Path,
    stand_in_descriptor: Path,
    fixture_sums: Path,
    boundary_command: Sequence[str],
    root: Path,
    data_root: Path,
    receipt_path: Path,
    evidence_kind: str = DISPOSABLE_EVIDENCE,
    environment: Mapping[str, str] | None = None,
    timeout: float = 120.0,
) -> dict:
    """Execute one admitted route and persist its ordered lifecycle receipt."""
    if evidence_kind not in EVIDENCE_KINDS:
        raise ValueError(f"evidence_kind must be one of {EVIDENCE_KINDS}")
    root = _absolute_directory(root, "install root")
    data_root = _absolute_directory(data_root, "data root")
    if root == data_root:
        raise ValueError("install root and data root must be distinct")
    admission = release2.validate_admission(
        scenario_id=scenario_id,
        candidate=candidate,
        stand_in_marker=stand_in_marker,
        channel=channel,
        mode=mode,
        os_name=os_name,
        job_id=job_id,
        runner=runner,
        shell=shell,
        source_override=False,
        stand_in_manifest=stand_in_manifest,
        stand_in_descriptor=stand_in_descriptor,
        fixture_sums=fixture_sums,
    )
    process_environment = dict(os.environ)
    if environment is not None:
        process_environment.update(environment)
    provenance = _runner_provenance(
        scenario=admission, evidence_kind=evidence_kind, environment=process_environment
    )
    inputs = {
        "candidate": deepcopy(admission["candidate"]),
        "stand_in": deepcopy(admission["stand_in"]),
        "stand_in_marker": stand_in_marker,
        "root": str(root),
        "data_root": str(data_root),
    }
    boundary = ExecutableBoundary(
        command=boundary_command,
        scenario=admission,
        inputs=inputs,
        evidence_kind=evidence_kind,
        environment=process_environment,
        timeout=timeout,
    )
    receipt = _initial_receipt(
        admission=admission,
        evidence_kind=evidence_kind,
        provenance=provenance,
        boundary=boundary,
    )
    try:
        before_sha = _inspect_release1(receipt=receipt, boundary=boundary, scenario=admission)
        if channel == "button":
            _execute_button(receipt=receipt, boundary=boundary, scenario=admission)
        elif channel == "install-line":
            _execute_install_line(receipt=receipt, boundary=boundary)
        elif channel == "pinokio-update":
            _execute_pinokio(receipt=receipt, boundary=boundary, scenario=admission)
        else:  # validate_admission already rejects this, but keep dispatch fail-closed.
            raise ExecutionError(f"unsupported admitted channel: {channel}")
        _inspect_release2(receipt=receipt, boundary=boundary, before_sha=before_sha)
        cleaned = boundary.invoke("cleanup", {"owned_roots_only": True})
        _expect(cleaned.get("cleanup"), "pass", "final cleanup")
        receipt["final_cleanup"] = "pass"
        receipt["checks"] = {name: "pass" for name in receipt["checks"]}
        receipt["actions"] = deepcopy(boundary.actions)
        receipt["status"] = "pass" if evidence_kind == HOSTED_EVIDENCE else "exercised"
        if evidence_kind == HOSTED_EVIDENCE:
            release2.validate_receipt(receipt)
    except Exception as exc:
        receipt["status"] = "fail"
        receipt["actions"] = deepcopy(boundary.actions)
        receipt["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        _write_receipt(receipt_path, receipt)
        raise
    _write_receipt(receipt_path, receipt)
    return deepcopy(receipt)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--os", dest="os_name", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--runner", required=True)
    parser.add_argument("--shell", required=True)
    for name in release2.RELEASE1_CANDIDATE:
        parser.add_argument(f"--candidate-{name}", required=True)
    parser.add_argument("--stand-in-marker", required=True)
    parser.add_argument("--stand-in-manifest", required=True, type=Path)
    parser.add_argument("--stand-in-descriptor", required=True, type=Path)
    parser.add_argument("--fixture-sums", required=True, type=Path)
    parser.add_argument("--boundary", required=True)
    parser.add_argument("--boundary-arg", action="append", default=[])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--evidence-kind", choices=EVIDENCE_KINDS, default=DISPOSABLE_EVIDENCE)
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        execute_journey(
            scenario_id=args.scenario_id,
            candidate={
                name: getattr(args, f"candidate_{name}") for name in release2.RELEASE1_CANDIDATE
            },
            stand_in_marker=args.stand_in_marker,
            channel=args.channel,
            mode=args.mode,
            os_name=args.os_name,
            job_id=args.job_id,
            runner=args.runner,
            shell=args.shell,
            stand_in_manifest=args.stand_in_manifest,
            stand_in_descriptor=args.stand_in_descriptor,
            fixture_sums=args.fixture_sums,
            boundary_command=(args.boundary, *args.boundary_arg),
            root=args.root,
            data_root=args.data_root,
            receipt_path=args.receipt,
            evidence_kind=args.evidence_kind,
            timeout=args.timeout,
        )
    except (OSError, ValueError, ExecutionError, subprocess.SubprocessError) as exc:
        print(f"release2_executor: {exc}", file=sys.stderr)
        return 1
    print(args.receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
