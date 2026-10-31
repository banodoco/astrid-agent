"""Fail-closed invocation boundary for Pinokio's immutable Stop action.

This module owns provenance and orchestration. It does not emulate Pinokio's
action engine. A real run requires an independently supplied, pinned owner
wrapper and condition-controller authority. The wrapper must attest the
source-backed pterm start route and terminal task settlement. The clean
candidate checkout alone does not contain either.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

PINOKIO_REF = "c5b42701641889ecb916c60cde09a717f83d62ff"
SUPPLIED_PINOKIO_REF = "c5b42701641889ec916c60cde09a717f83d62ff"
STOP_PATH = "pinokio/stop.js"
STOP_SHA256 = "f9d62e4e6a826fccd9791ee211dae933f46c8f99019e145abb3db3bf9595fd48"
PINOKIO_JSON_SHA256 = "c1a274033a1912d7f97069e37f84bf5662616a8eb4f619cdae05180577d94ddb"
PINOKIO_JS_SHA256 = "43b19549666e1e264e6d8cd2a4705234961038106471ba28a073b2a9599a2951"
ENGINE_PROTOCOL = "pinokio-owner-route/v1"
CONDITION_PROTOCOL = "pinokio-stop-condition/v1"
ENGINE_NAME = "pinokiod"
ENGINE_VERSION = "8.1.1"
ENGINE_REF = "add4a674ad1ba95bb62ec427fce519fe5bf0e9bf"
CLIENT_NAME = "pterm"
CLIENT_REF = "5be43718701c448b43daf0ce51d8935f170065bb"
PINOKIO_APP_VERSION = "8.0.0"
TIMEOUT_MAX = 45
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_RE = re.compile(r"^[0-9a-f]{40}$")


class BoundaryError(RuntimeError):
    """A missing or mismatched authority prevented safe execution."""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _need_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BoundaryError(f"{label} must be a non-empty string")
    return value


def _absolute(value: Any, label: str) -> Path:
    path = Path(_need_text(value, label))
    if not path.is_absolute():
        raise BoundaryError(f"{label} must be absolute")
    for component in reversed((path, *path.parents)):
        if component.exists() and component.is_symlink():
            raise BoundaryError(f"{label} path must not contain a symlink: {component}")
    return path


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_MAX,
        shell=False,
        check=False,
    )
    if result.returncode:
        raise BoundaryError(
            f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip()


def authenticate_candidate(root_value: str | Path, *, ref: str | None = None) -> dict[str, Any]:
    """Authenticate the exact clean source, Stop bytes, and app identity."""
    ref = PINOKIO_REF if ref is None else ref
    if ref != PINOKIO_REF:
        if ref == SUPPLIED_PINOKIO_REF:
            raise BoundaryError(
                f"supplied Pinokio ref {ref} is malformed (39 hex characters); "
                f"the authenticated candidate is {PINOKIO_REF}"
            )
        raise BoundaryError("Pinokio ref does not match the immutable approved candidate")
    if not GIT_RE.fullmatch(ref):
        raise BoundaryError("Pinokio ref must be a full 40-character Git SHA")
    root = _absolute(str(root_value), "Pinokio checkout")
    if not root.is_dir():
        raise BoundaryError("Pinokio checkout must be an existing directory")
    if _git(root, "rev-parse", "HEAD") != ref:
        raise BoundaryError("Pinokio checkout HEAD does not match the immutable candidate ref")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise BoundaryError("Pinokio candidate checkout is mutable")
    stop = root / STOP_PATH
    if not stop.is_file() or stop.is_symlink():
        raise BoundaryError("immutable pinokio/stop.js is absent or symlinked")
    observed_stop = _digest(stop.read_bytes())
    if observed_stop != STOP_SHA256:
        raise BoundaryError("pinokio/stop.js bytes differ from the immutable approved descriptor")
    identities = {}
    for name, expected in (
        ("pinokio.json", PINOKIO_JSON_SHA256),
        ("pinokio.js", PINOKIO_JS_SHA256),
    ):
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise BoundaryError(f"Pinokio app identity file {name} is absent or symlinked")
        observed = _digest(path.read_bytes())
        if observed != expected:
            raise BoundaryError(f"Pinokio app identity {name} differs from the immutable candidate")
        identities[name] = observed
    try:
        metadata = json.loads((root / "pinokio.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BoundaryError(f"cannot read Pinokio app identity: {exc}") from exc
    if metadata.get("title") != "Astrid":
        raise BoundaryError("Pinokio app title is not the authenticated Astrid identity")
    app_source = (root / "pinokio.js").read_text(encoding="utf-8")
    if re.search(r"\bversion\s*:\s*['\"]8\.0\.0['\"]", app_source) is None:
        raise BoundaryError("Pinokio app semantic version is not exactly 8.0.0")
    return {
        "checkout": str(root.resolve()),
        "ref": ref,
        "stop": {"path": STOP_PATH, "sha256": observed_stop},
        "identity": {"title": "Astrid", "version": PINOKIO_APP_VERSION, **identities},
    }


def _authority(raw: Any, label: str, protocol: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise BoundaryError(f"{label} authority absent: supply a pinned {protocol} provider")
    if raw.get("protocol") != protocol:
        raise BoundaryError(f"{label}.protocol must be {protocol}")
    checkout = _absolute(raw.get("checkout"), f"{label}.checkout")
    ref = _need_text(raw.get("ref"), f"{label}.ref")
    if not GIT_RE.fullmatch(ref):
        raise BoundaryError(f"{label}.ref must be a full Git SHA")
    if not checkout.is_dir() or _git(checkout, "rev-parse", "HEAD") != ref:
        raise BoundaryError(f"{label} checkout HEAD does not match its immutable ref")
    if _git(checkout, "status", "--porcelain", "--untracked-files=all"):
        raise BoundaryError(f"{label} checkout is mutable")
    file = _absolute(raw.get("file"), f"{label}.file")
    expected = _need_text(raw.get("sha256"), f"{label}.sha256").lower()
    if not SHA256_RE.fullmatch(expected):
        raise BoundaryError(f"{label}.sha256 must be a SHA-256 digest")
    if (
        not file.is_file()
        or file.is_symlink()
        or not file.resolve().is_relative_to(checkout.resolve())
    ):
        raise BoundaryError(f"{label}.file must be a regular file inside its pinned checkout")
    if not os.access(file, os.X_OK):
        raise BoundaryError(f"{label}.file must be an executable authority")
    if _digest(file.read_bytes()) != expected:
        raise BoundaryError(f"{label}.file digest does not match its immutable authority")
    argv = raw.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise BoundaryError(f"{label}.argv must be explicit non-empty argv")
    if argv[0] != str(file):
        raise BoundaryError(f"{label}.argv[0] must be the pinned authority file")
    if protocol == ENGINE_PROTOCOL:
        if raw.get("role") != "pinned-owner-wrapper":
            raise BoundaryError("action engine must be a pinned owner wrapper for the pterm route")
        expected_engine = {"name": ENGINE_NAME, "version": ENGINE_VERSION, "ref": ENGINE_REF}
        expected_client = {"name": CLIENT_NAME, "ref": CLIENT_REF}
        if raw.get("engine_identity") != expected_engine:
            raise BoundaryError(
                "action engine identity/version/ref does not match pinned pinokiod source"
            )
        if raw.get("client_identity") != expected_client:
            raise BoundaryError(
                "action engine client identity/ref does not match pinned pterm source"
            )
    timeout = raw.get("timeout_seconds", 30)
    if type(timeout) not in (int, float) or timeout <= 0 or timeout > TIMEOUT_MAX:
        raise BoundaryError(f"{label}.timeout_seconds must be in (0, {TIMEOUT_MAX}]")
    result = {
        "protocol": protocol,
        "checkout": str(checkout.resolve()),
        "ref": ref,
        "file": str(file.resolve()),
        "sha256": expected,
        "argv": list(argv),
        "timeout_seconds": float(timeout),
    }
    if protocol == ENGINE_PROTOCOL:
        result.update({"engine_identity": expected_engine, "client_identity": expected_client})
    return result


def _run(
    authority: Mapping[str, Any],
    *,
    action: Path | None = None,
    candidate: Mapping[str, Any] | None = None,
    condition: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    argv = [*authority["argv"], "--protocol", authority["protocol"]]
    if action is not None:
        if candidate is None:
            raise BoundaryError(
                "owner route invocation requires authenticated candidate provenance"
            )
        downstream = ["pterm", "start", "stop.js", "--ref", candidate["ref"]]
        argv.extend(
            [
                "--app-root",
                candidate["checkout"],
                "--action",
                str(action),
                "--action-sha256",
                candidate["stop"]["sha256"],
                "--candidate-ref",
                candidate["ref"],
                "--engine-ref",
                authority["engine_identity"]["ref"],
                "--engine-version",
                authority["engine_identity"]["version"],
                "--client-ref",
                authority["client_identity"]["ref"],
                "--downstream-argv-json",
                json.dumps(downstream, separators=(",", ":")),
            ]
        )
    if condition is not None:
        argv.extend(["--condition", condition])
    try:
        result = runner(
            argv,
            cwd=authority["checkout"],
            capture_output=True,
            text=True,
            timeout=authority["timeout_seconds"],
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BoundaryError(f"{authority['protocol']} invocation failed: {exc}") from exc
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise BoundaryError(
            f"{authority['protocol']} must return one JSON object on stdout"
        ) from exc
    if not isinstance(payload, dict):
        raise BoundaryError(f"{authority['protocol']} response must be a JSON object")
    if payload.get("protocol") != authority["protocol"]:
        raise BoundaryError(f"{authority['protocol']} response protocol is absent or mismatched")
    payload["exit_code"] = result.returncode
    return payload


def _state(condition_authority: Mapping[str, Any], runner: Callable[..., Any]) -> dict[str, Any]:
    result = _run(condition_authority, condition="observe", runner=runner)
    state = result.get("state")
    if result["exit_code"] != 0 or not isinstance(state, dict):
        raise BoundaryError("condition observer failed to return an authoritative state object")
    for name in ("processes", "listeners", "identity"):
        if name not in state:
            raise BoundaryError(f"condition observer state lacks {name}")
    return state


def _protected_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Project the process/listener/identity facts that Stop must preserve."""
    return {name: state[name] for name in ("processes", "listeners", "identity")}


def _invoke_stop(
    engine: Mapping[str, Any],
    candidate: Mapping[str, Any],
    action: Path,
    condition: str,
    runner: Callable[..., Any],
) -> dict[str, Any]:
    outcome = _run(engine, action=action, candidate=candidate, runner=runner)
    downstream = ["pterm", "start", "stop.js", "--ref", candidate["ref"]]
    required = {
        "route_kind": "pterm-start-script",
        "transport": "pterm-websocket-json-rpc",
        "engine_entrypoint": "pinokiod.kernel.api.process",
        "candidate_ref": candidate["ref"],
        "app_root": candidate["checkout"],
        "app_identity": candidate["identity"],
        "action_path": str(action.resolve()),
        "action_sha256": candidate["stop"]["sha256"],
        "engine_name": ENGINE_NAME,
        "engine_ref": ENGINE_REF,
        "engine_version": ENGINE_VERSION,
        "client_name": CLIENT_NAME,
        "client_ref": CLIENT_REF,
        "downstream_argv": downstream,
    }
    for key, expected in required.items():
        if outcome.get(key) != expected:
            raise BoundaryError(
                f"owner wrapper response {key} does not attest the exact Pinokio Stop route"
            )
    for key in ("request_id", "task_id"):
        if not isinstance(outcome.get(key), str) or not outcome[key].strip():
            raise BoundaryError(f"owner wrapper response lacks non-empty {key}")
    if (
        outcome.get("accepted") is not True
        or outcome.get("settled") is not True
        or outcome.get("terminal") is not True
        or outcome.get("settlement") != "terminal"
    ):
        raise BoundaryError(
            "Pinokio action request is queued, disconnected, or not terminally settled"
        )
    if outcome.get("result") != "refused" or outcome.get("refusal_reason") != condition:
        raise BoundaryError(
            f"immutable Stop did not refuse with the expected {condition} semantics"
        )
    return outcome


def run_stop_condition(
    config: Mapping[str, Any], condition: str, *, runner: Callable[..., Any] = subprocess.run
) -> dict[str, Any]:
    """Run one authentic refusal scenario and prove state restoration.

    Provider JSON protocol: the condition provider supports `observe`,
    `prepare`, and `restore`; each response contains `state` and `condition`
    where applicable. The action engine response contains `result` and
    `refusal_reason`. Providers must be independently pinned by checkout/ref/
    file digest. Exit status and unchanged state are enforced here.
    """
    if condition not in {"busy", "unknown"}:
        raise BoundaryError("condition must be exactly 'busy' or 'unknown'")
    candidate = authenticate_candidate(
        config.get("candidate_checkout"), ref=config.get("candidate_ref", PINOKIO_REF)
    )
    engine = _authority(config.get("engine"), "action engine", ENGINE_PROTOCOL)
    controls = config.get("conditions")
    if not isinstance(controls, Mapping) or not isinstance(controls.get(condition), Mapping):
        raise BoundaryError(
            f"{condition} condition authority absent: no genuine preparation/restoration provider"
        )
    controller = _authority(controls[condition], f"{condition} condition", CONDITION_PROTOCOL)
    before = _state(controller, runner)
    prepared = False
    active: dict[str, Any] | None = None
    after_stop: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None
    try:
        # Treat even a failed/partial preparation response as a mutation risk.
        prepared = True
        setup = _run(controller, condition="prepare", runner=runner)
        if (
            setup["exit_code"] != 0
            or setup.get("condition") != condition
            or setup.get("prepared") is not True
        ):
            raise BoundaryError(f"{condition} preparation did not establish the genuine condition")
        active = _state(controller, runner)
        if (
            active == before
            or active.get("condition") != condition
            or _protected_state(active) != _protected_state(before)
        ):
            raise BoundaryError(f"{condition} preparation did not change observed Runtime state")
        stop = Path(candidate["checkout"]) / STOP_PATH
        stop_error: BoundaryError | None = None
        try:
            outcome = _invoke_stop(engine, candidate, stop, condition, runner)
        except BoundaryError as exc:
            # Even a disconnect or bad settlement response may follow a
            # destructive operation; observe before any restoration can hide it.
            stop_error = exc
        # Observe immediately after the Stop attempt and before provider restore.
        after_stop = _state(controller, runner)
        if _protected_state(after_stop) != _protected_state(before):
            raise BoundaryError(
                "process/listener/identity protected state changed after Stop attempt"
            )
        if stop_error is not None:
            raise stop_error
    finally:
        if prepared:
            restored = _run(controller, condition="restore", runner=runner)
            if restored["exit_code"] != 0 or restored.get("restored") is not True:
                raise BoundaryError(f"{condition} provider failed to restore its Runtime condition")
            # Verify restoration even when preparation or Stop validation
            # raised. A successful restore response alone is not evidence that
            # custody returned to baseline.
            after = _state(controller, runner)
            if _protected_state(after) != _protected_state(before):
                raise BoundaryError(
                    f"{condition} restoration changed process/listener/identity state"
                )
    if active is None or after_stop is None or after is None or outcome is None:
        raise BoundaryError("Stop refusal scenario completed without full observation evidence")
    return {
        "evidence_class": "executable-boundary",
        "candidate": candidate,
        "engine": {
            key: engine[key]
            for key in (
                "protocol",
                "checkout",
                "ref",
                "file",
                "sha256",
                "engine_identity",
                "client_identity",
            )
        },
        "condition_authority": {
            key: controller[key] for key in ("protocol", "checkout", "ref", "file", "sha256")
        },
        "condition": condition,
        "invocation": {
            key: outcome[key]
            for key in (
                "request_id",
                "task_id",
                "route_kind",
                "transport",
                "engine_entrypoint",
                "downstream_argv",
                "settlement",
                "result",
                "refusal_reason",
            )
        },
        "observations": {
            "before": before,
            "active": active,
            "after_stop": after_stop,
            "restored": after,
        },
        "result": "refused-and-restored",
        "state_unchanged": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--condition", required=True, choices=("busy", "unknown"))
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise BoundaryError("config must be a JSON object")
        result = run_stop_condition(config, args.condition)
    except (OSError, UnicodeError, json.JSONDecodeError, BoundaryError) as exc:
        print(
            json.dumps(
                {"error": str(exc), "evidence_class": "none", "status": "refused"}, sort_keys=True
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
