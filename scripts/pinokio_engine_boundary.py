"""Fail-closed boundary for the registered Pinokio Stop action.

The Git revision identifies the immutable Astrid checkout.  It is *not* the
value passed to PTerm ``--ref``: that value is the registered Pinokio resource
selector (``pinokio://.../api/<id>``).  The selector is resolved and bound to
the checkout before a condition provider is allowed to mutate anything.

This module invokes PTerm directly through its pinned, versioned client and
requires the matching pinokiod source identity.  It never loads a descriptor
outside the action engine, accepts an untrusted caller wrapper, performs
process termination itself, or uses a fallback route as success.
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
from urllib.error import URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

PINOKIO_REF = "c5b42701641889ecb916c60cde09a717f83d62ff"
SUPPLIED_PINOKIO_REF = "c5b42701641889ec916c60cde09a717f83d62ff"
STOP_PATH = "pinokio/stop.js"
STOP_SHA256 = "f9d62e4e6a826fccd9791ee211dae933f46c8f99019e145abb3db3bf9595fd48"
PINOKIO_JSON_SHA256 = "c1a274033a1912d7f97069e37f84bf5662616a8eb4f619cdae05180577d94ddb"
PINOKIO_JS_SHA256 = "43b19549666e1e264e6d8cd2a4705234961038106471ba28a073b2a9599a2951"
ENGINE_PROTOCOL = "pinokio-action-engine/v2"
RESOURCE_PROTOCOL = "pinokio-registered-resource/v1"
CONDITION_PROTOCOL = "pinokio-stop-condition/v1"
ENGINE_NAME = "pinokiod"
ENGINE_VERSION = "8.1.1"
ENGINE_REF = "add4a674ad1ba95bb62ec427fce519fe5bf0e9bf"
CLIENT_NAME = "pterm"
CLIENT_REF = "5be43718701c448b43daf0ce51d8935f170065bb"
CLIENT_ENTRYPOINT = "index.js"
PINOKIO_APP_VERSION = "8.0.0"
PINOKIO_APP_ID = "astrid"
PINOKIO_RESOURCE_REF = "pinokio://127.0.0.1:42000/api/astrid"
TIMEOUT_MAX = 45
HTTP_RESPONSE_MAX = 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_RE = re.compile(r"^[0-9a-f]{40}$")
SELECTOR_RE = re.compile(r"^pinokio://[^/?#]+/api/(?:[^/?#]+)(?:/[^/?#]+)*$")
REFUSAL_MARKERS = {
    "busy": ("Astrid is busy (", "Runtime stop preflight"),
    "unknown": ("Astrid is still running. Stop it in Pinokio, then try again.",),
}

# Pinned source files are not a substitute for the installed daemon.  They
# prevent a caller from relabelling an arbitrary executable as pinokiod/pterm.
PINOKIOD_SOURCE_DIGESTS = {
    "server/socket.js": "d40871a7e50ae9dae1f920d860de5b64b5e9b5d064d88556b79d27c29a07bbc6",
    "kernel/api/index.js": "acdd39ce8c058d42e7555b029c8798aa3173062e33c3249773a25b239dfc58a7",
}
PTERM_SOURCE_DIGESTS = {
    "endpoint.js": "7f9020f0bfcb07ae52d541f1c1ef074f17b8f56b1f9922efe99d9306ffb85db5",
    "rpc.js": "27e1eda8e6755248deb9ce5816bac082029b7e3dd793632633b0db5dcb5a5a3e",
    "script.js": "0fc5f343aebb7419241974857cf63a15871bb26b08cca9d1817c99508b373b32",
    "target.js": "2afa27f8bd26ea39d9ffecabca00c34e98c8bf1aae125db4640372984720bb0e",
}
APPROVED_OLD_LAUNCHER_REF = "bbba620e3d89eeff8021417f365e6ea584203105"
APPROVED_OLD_UPDATE_SHA256 = "a0f998c03bbba57966c8863a3bd38411d7859d970ddc4c93af1e28b04a66291b"


class BoundaryError(RuntimeError):
    """A missing, ambiguous, or mismatched authority stopped the route."""


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
        detail = (result.stderr or result.stdout).strip()
        raise BoundaryError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _clean_checkout(root: Path, ref: str, label: str) -> None:
    if not root.is_dir() or _git(root, "rev-parse", "HEAD") != ref:
        raise BoundaryError(f"{label} HEAD does not match its immutable ref")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise BoundaryError(f"{label} checkout is mutable")


def _regular_digest(path: Path, expected: str, label: str) -> None:
    if (
        not path.is_file()
        or path.is_symlink()
        or any(parent.is_symlink() for parent in path.parents if parent.exists())
    ):
        raise BoundaryError(f"{label} is absent or symlinked")
    if _digest(path.read_bytes()) != expected:
        raise BoundaryError(f"{label} digest does not match its immutable authority")


def _selector(value: Any, label: str = "PTerm --ref") -> tuple[str, str]:
    selector = _need_text(value, label)
    parsed = urlsplit(selector)
    if (
        not SELECTOR_RE.fullmatch(selector)
        or parsed.scheme != "pinokio"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise BoundaryError(f"{label} must be a registered pinokio://.../api/... selector")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] != "api" or any(part in {".", ".."} for part in parts):
        raise BoundaryError(f"{label} must select one registered Pinokio app")
    return selector, "/".join(parts[1:])


def authenticate_candidate(root_value: str | Path, *, ref: str | None = None) -> dict[str, Any]:
    """Authenticate the clean candidate checkout and immutable Stop identity."""
    ref = PINOKIO_REF if ref is None else ref
    if ref != PINOKIO_REF:
        if ref == SUPPLIED_PINOKIO_REF:
            raise BoundaryError(
                f"supplied Pinokio ref {ref} is malformed (39 hex characters); "
                f"the authenticated candidate is {PINOKIO_REF}"
            )
        raise BoundaryError("Pinokio checkout ref does not match the immutable approved candidate")
    if not GIT_RE.fullmatch(ref):
        raise BoundaryError("Pinokio checkout ref must be a full 40-character Git SHA")
    root = _absolute(str(root_value), "Pinokio checkout")
    _clean_checkout(root, ref, "Pinokio candidate")
    stop = root / STOP_PATH
    _regular_digest(stop, STOP_SHA256, "immutable pinokio/stop.js")
    identities: dict[str, str] = {}
    for name, expected in (
        ("pinokio.json", PINOKIO_JSON_SHA256),
        ("pinokio.js", PINOKIO_JS_SHA256),
    ):
        path = root / name
        _regular_digest(path, expected, f"Pinokio app identity {name}")
        identities[name] = expected
    try:
        metadata = json.loads((root / "pinokio.json").read_text(encoding="utf-8"))
        app_source = (root / "pinokio.js").read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BoundaryError(f"cannot read Pinokio app identity: {exc}") from exc
    if metadata.get("title") != "Astrid":
        raise BoundaryError("Pinokio app title is not the authenticated Astrid identity")
    if re.search(r"\bversion\s*:\s*['\"]8\.0\.0['\"]", app_source) is None:
        raise BoundaryError("Pinokio app semantic version is not exactly 8.0.0")
    return {
        "checkout": str(root.resolve()),
        "ref": ref,
        "stop": {"path": STOP_PATH, "sha256": STOP_SHA256},
        "identity": {
            "app_id": PINOKIO_APP_ID,
            "title": "Astrid",
            "version": PINOKIO_APP_VERSION,
            **identities,
        },
    }


def _authority(raw: Any, label: str, protocol: str) -> dict[str, Any]:
    """Authenticate one immutable executable authority without invoking it."""
    if not isinstance(raw, Mapping):
        raise BoundaryError(f"{label} authority absent: supply a pinned {protocol} provider")
    if raw.get("protocol") != protocol:
        raise BoundaryError(f"{label}.protocol must be {protocol}")
    if protocol == ENGINE_PROTOCOL and raw.get("role") in {
        "pinned-owner-wrapper",
        "caller-wrapper",
        "fake",
        "fixture",
    }:
        raise BoundaryError("action engine is not an authenticated PTerm client")
    checkout = _absolute(raw.get("checkout"), f"{label}.checkout")
    ref = _need_text(raw.get("ref"), f"{label}.ref")
    if not GIT_RE.fullmatch(ref):
        raise BoundaryError(f"{label}.ref must be a full Git SHA")
    _clean_checkout(checkout, ref, label)
    file = _absolute(raw.get("file"), f"{label}.file")
    expected = _need_text(raw.get("sha256"), f"{label}.sha256").lower()
    if not SHA256_RE.fullmatch(expected):
        raise BoundaryError(f"{label}.sha256 must be a SHA-256 digest")
    if not file.resolve().is_relative_to(checkout.resolve()):
        raise BoundaryError(f"{label}.file must be inside its pinned checkout")
    _regular_digest(file, expected, f"{label}.file")
    if not os.access(file, os.X_OK):
        raise BoundaryError(f"{label}.file must be executable")
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v for v in argv):
        raise BoundaryError(f"{label}.argv must be explicit non-empty argv")
    if argv[0] != str(file):
        raise BoundaryError(f"{label}.argv[0] must be the pinned authority file")
    timeout = raw.get("timeout_seconds", 30)
    if type(timeout) not in (int, float) or timeout <= 0 or timeout > TIMEOUT_MAX:
        raise BoundaryError(f"{label}.timeout_seconds must be in (0, {TIMEOUT_MAX}]")
    return {
        "protocol": protocol,
        "checkout": str(checkout.resolve()),
        "ref": ref,
        "file": str(file.resolve()),
        "sha256": expected,
        "argv": list(argv),
        "timeout_seconds": float(timeout),
    }


def _source_bundle(
    raw: Any, label: str, expected_ref: str, expected_files: Mapping[str, str]
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise BoundaryError(f"{label} source authority absent")
    checkout = _absolute(raw.get("checkout"), f"{label}.checkout")
    ref = _need_text(raw.get("ref"), f"{label}.ref")
    if ref != expected_ref:
        raise BoundaryError(f"{label} source ref is not the pinned version")
    _clean_checkout(checkout, ref, label)
    files = raw.get("files")
    if not isinstance(files, Mapping):
        raise BoundaryError(f"{label}.files must bind the pinned source files")
    observed: dict[str, str] = {}
    for relative, expected in expected_files.items():
        supplied = files.get(relative, expected)
        if supplied != expected:
            raise BoundaryError(f"{label} source digest for {relative} is not pinned")
        path = checkout / relative
        _regular_digest(path, expected, f"{label} source {relative}")
        observed[relative] = expected
    version = raw.get("version")
    if label == "pinokiod" and version != ENGINE_VERSION:
        raise BoundaryError("pinokiod version is not the pinned 8.1.1 engine")
    return {"checkout": str(checkout.resolve()), "ref": ref, "version": version, "files": observed}


def _http_json(url: str, timeout: float) -> Any:
    request = Request(
        url,
        headers={"Accept": "application/json", "x-pinokio-client": CLIENT_NAME},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed loopback authority
            data = response.read(HTTP_RESPONSE_MAX + 1)
    except (OSError, URLError) as exc:
        raise BoundaryError(f"Pinokio control-plane read failed: {exc}") from exc
    if len(data) > HTTP_RESPONSE_MAX:
        raise BoundaryError("Pinokio control-plane response exceeds the bounded size")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BoundaryError("Pinokio control-plane response is not JSON") from exc


def _read_registered_resource(selector: str, timeout: float) -> dict[str, Any]:
    """Read the same version and resource endpoints used by pinned PTerm source."""
    parsed = urlsplit(selector)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise BoundaryError("registered Pinokio authority must be loopback-local")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    base = f"http://{host}:{parsed.port}"
    version_payload = _http_json(f"{base}/pinokio/version", timeout)
    resource_payload = _http_json(
        f"{base}/pinokio/resource/status?{urlencode({'ref': selector})}", timeout
    )
    if isinstance(version_payload, str):
        version = version_payload
    elif isinstance(version_payload, Mapping):
        version = version_payload.get("version")
    else:
        version = None
    return {"engine_version": version, "resource": resource_payload}


def authenticate_registered_resource(
    raw: Any,
    candidate: Mapping[str, Any],
    *,
    engine: Mapping[str, Any],
    reader: Callable[[str, float], Mapping[str, Any]] = _read_registered_resource,
) -> dict[str, Any]:
    """Resolve PTerm's URI selector and bind it to the exact candidate bytes."""
    if not isinstance(raw, Mapping):
        raise BoundaryError("registered Pinokio resource authority absent")
    selector, app_id = _selector(raw.get("selector", raw.get("ref")))
    if selector != PINOKIO_RESOURCE_REF:
        raise BoundaryError("PTerm --ref does not match the admitted Astrid resource")
    expected_app_id = _need_text(raw.get("app_id", app_id), "registered_resource.app_id")
    if expected_app_id != app_id or expected_app_id != PINOKIO_APP_ID:
        raise BoundaryError("PTerm selector/resource app identity mismatch")
    checkout = _absolute(raw.get("checkout"), "registered_resource.checkout")
    checkout_ref = raw.get("checkout_ref", raw.get("ref_sha", candidate["ref"]))
    checkout_ref = _need_text(checkout_ref, "registered_resource.checkout_ref")
    if checkout_ref != candidate["ref"]:
        raise BoundaryError("registered resource checkout ref differs from candidate ref")
    _clean_checkout(checkout, checkout_ref, "registered resource")
    # The registered resource must contain the same immutable action and app
    # identity.  A matching Git ref alone is not enough for an installed app.
    for relative, expected in (
        (STOP_PATH, candidate["stop"]["sha256"]),
        ("pinokio.json", candidate["identity"]["pinokio.json"]),
        ("pinokio.js", candidate["identity"]["pinokio.js"]),
    ):
        _regular_digest(checkout / relative, expected, f"registered resource {relative}")
    # The pinned PTerm target source resolves --ref through these control-plane
    # endpoints before it builds the action URI. Read them independently before
    # condition preparation so a mismatched registration cannot mutate state.
    try:
        observed = reader(selector, engine["pterm"]["timeout_seconds"])
    except BoundaryError:
        raise
    except Exception as exc:
        raise BoundaryError(f"registered resource authority failed: {exc}") from exc
    if not isinstance(observed, Mapping):
        raise BoundaryError("registered resource authority returned no observation")
    if observed.get("engine_version") != ENGINE_VERSION:
        raise BoundaryError("live Pinokio action engine version is absent or mismatched")
    status = observed.get("resource")
    if not isinstance(status, Mapping):
        raise BoundaryError("Pinokio resource status is absent")
    for key in ("ref", "selector"):
        if key in status and status[key] != selector:
            raise BoundaryError("Pinokio resource status selector mismatch")
    for key in ("id", "app_id"):
        if key in status and status[key] != app_id:
            raise BoundaryError("Pinokio resource status app identity mismatch")
    observed_path = status.get("path")
    if not isinstance(observed_path, str) or not observed_path:
        raise BoundaryError("Pinokio resource status path is absent")
    try:
        status_path = Path(observed_path).resolve(strict=True)
    except OSError as exc:
        raise BoundaryError("PTerm status checkout is unavailable") from exc
    if status_path != checkout.resolve():
        raise BoundaryError("PTerm status path differs from authenticated resource checkout")
    return {
        "protocol": RESOURCE_PROTOCOL,
        "selector": selector,
        "app_id": app_id,
        "checkout": str(checkout.resolve()),
        "checkout_ref": checkout_ref,
        "identity": candidate["identity"],
        "stop_sha256": candidate["stop"]["sha256"],
        "status": {
            "engine_version": ENGINE_VERSION,
            "path": str(status_path),
            "sha256": _digest(
                json.dumps(status, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ),
        },
    }


def _invoke_json(
    authority: Mapping[str, Any],
    extra: list[str],
    *,
    runner: Callable[..., Any],
    expected_protocol: str | None = None,
) -> dict[str, Any]:
    argv = [*authority["argv"], *extra]
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
    if expected_protocol is None:
        expected_protocol = authority["protocol"]
    if expected_protocol and payload.get("protocol") != expected_protocol:
        raise BoundaryError(f"{authority['protocol']} response protocol is absent or mismatched")
    payload["exit_code"] = result.returncode
    return payload


def _authenticate_engine(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise BoundaryError("action engine authority absent: supply the pinned PTerm action engine")
    if raw.get("protocol") != ENGINE_PROTOCOL:
        raise BoundaryError(f"action engine.protocol must be {ENGINE_PROTOCOL}")
    if raw.get("role") != "pterm-action-engine":
        raise BoundaryError("caller-supplied wrapper/fake engine is not an action-engine authority")
    pterm_raw = raw.get("pterm", raw.get("client", raw))
    pterm = _authority(pterm_raw, "pterm client", ENGINE_PROTOCOL)
    if raw.get("client_identity") != {"name": CLIENT_NAME, "ref": CLIENT_REF}:
        raise BoundaryError("pterm client identity/ref does not match the pinned source")
    if raw.get("engine_identity") != {
        "name": ENGINE_NAME,
        "version": ENGINE_VERSION,
        "ref": ENGINE_REF,
    }:
        raise BoundaryError(
            "action engine identity/version/ref does not match pinned pinokiod source"
        )
    pinokiod = _source_bundle(raw.get("pinokiod"), "pinokiod", ENGINE_REF, PINOKIOD_SOURCE_DIGESTS)
    pterm_source = _source_bundle(
        raw.get("pterm_source"), "pterm", CLIENT_REF, PTERM_SOURCE_DIGESTS
    )
    entrypoint = Path(pterm_source["checkout"]) / CLIENT_ENTRYPOINT
    if (
        pterm["checkout"] != pterm_source["checkout"]
        or pterm["ref"] != CLIENT_REF
        or pterm["file"] != str(entrypoint.resolve())
        or pterm["argv"] != [str(entrypoint)]
    ):
        raise BoundaryError(
            "PTerm executable is not the pinned source checkout index.js entry point"
        )
    return {
        "protocol": ENGINE_PROTOCOL,
        "pterm": pterm,
        "engine_identity": dict(raw["engine_identity"]),
        "client_identity": dict(raw["client_identity"]),
        "pinokiod": pinokiod,
        "pterm_source": pterm_source,
    }


def _authenticate_old_launcher(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {
            "ref": APPROVED_OLD_LAUNCHER_REF,
            "sha256": APPROVED_OLD_UPDATE_SHA256,
            "source": "frozen-boundary-tuple",
        }
    ref = _need_text(raw.get("ref"), "old_launcher.ref")
    digest = _need_text(raw.get("sha256"), "old_launcher.sha256").lower()
    if ref != APPROVED_OLD_LAUNCHER_REF or digest != APPROVED_OLD_UPDATE_SHA256:
        raise BoundaryError("old-launcher provenance does not match the approved immutable update")
    if not SHA256_RE.fullmatch(digest):
        raise BoundaryError("old_launcher.sha256 must be a SHA-256 digest")
    return {"ref": ref, "sha256": digest}


def _state(authority: Mapping[str, Any], runner: Callable[..., Any]) -> dict[str, Any]:
    result = _invoke_json(authority, ["--condition", "observe"], runner=runner)
    state = result.get("state")
    if result["exit_code"] != 0 or not isinstance(state, dict):
        raise BoundaryError("condition observer failed to return an authoritative state object")
    required = (
        "condition",
        "processes",
        "listeners",
        "identity",
        "candidate",
        "resource",
        "checkout",
        "instance",
        "health",
    )
    missing = [name for name in required if name not in state]
    if missing:
        raise BoundaryError("condition observer state lacks " + ", ".join(missing))
    return state


def _protected_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: state[name]
        for name in (
            "condition",
            "processes",
            "listeners",
            "identity",
            "candidate",
            "resource",
            "checkout",
            "instance",
            "health",
        )
    }


def _custody_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in _protected_state(state).items() if key != "condition"}


def _validate_observation_binding(
    state: Mapping[str, Any],
    candidate: Mapping[str, Any],
    resource: Mapping[str, Any],
) -> None:
    expected = {
        "candidate": candidate["ref"],
        "resource": resource["selector"],
        "checkout": resource["checkout"],
    }
    for key, value in expected.items():
        if state.get(key) != value:
            raise BoundaryError(
                f"condition observer {key} does not bind the authenticated resource"
            )
    for key in ("identity", "instance", "health"):
        if state.get(key) in (None, "", [], {}):
            raise BoundaryError(f"condition observer {key} is empty")


def _run_engine(
    engine: Mapping[str, Any],
    resource: Mapping[str, Any],
    candidate: Mapping[str, Any],
    condition: str,
    *,
    runner: Callable[..., Any],
) -> dict[str, Any]:
    # This is the only success-capable action invocation in the module.  The
    # registered URI, not the Git SHA, is the PTerm resource selector.
    argv = [
        *engine["pterm"]["argv"],
        "start",
        STOP_PATH,
        "--ref",
        resource["selector"],
    ]
    try:
        result = runner(
            argv,
            cwd=engine["pterm"]["checkout"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=engine["pterm"]["timeout_seconds"],
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BoundaryError(f"real PTerm action invocation failed: {exc}") from exc
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    stderr = result.stderr if isinstance(result.stderr, str) else ""
    transcript = f"{stdout}\n{stderr}"
    marker = next((value for value in REFUSAL_MARKERS[condition] if value in transcript), None)
    if result.returncode != 0:
        raise BoundaryError("real PTerm action process did not complete its engine route")
    if marker is None:
        # PTerm exits zero on a bare engine disconnect. A raw zero exit is
        # therefore never refusal evidence without Stop's own emitted text.
        raise BoundaryError("PTerm ended without the immutable Stop action's refusal observation")
    return {
        "route_kind": "pterm-start-script",
        "engine_entrypoint": "pinokiod.kernel.api.process",
        "candidate_ref": candidate["ref"],
        "resource_ref": resource["selector"],
        "app_root": resource["checkout"],
        "app_identity": candidate["identity"],
        "action_path": str(Path(resource["checkout"]) / STOP_PATH),
        "action_sha256": candidate["stop"]["sha256"],
        "engine_name": ENGINE_NAME,
        "engine_ref": ENGINE_REF,
        "engine_version": ENGINE_VERSION,
        "client_name": CLIENT_NAME,
        "client_ref": CLIENT_REF,
        "downstream_argv": argv,
        "exit_code": result.returncode,
        "stdout_sha256": _digest(stdout.encode("utf-8")),
        "stderr_sha256": _digest(stderr.encode("utf-8")),
        "refusal_marker": marker,
        "refusal_reason": condition,
    }


def run_stop_condition(
    config: Mapping[str, Any],
    condition: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    resource_reader: Callable[[str, float], Mapping[str, Any]] = _read_registered_resource,
) -> dict[str, Any]:
    """Run a busy/transport-unknown refusal and prove full restoration."""
    if condition not in {"busy", "unknown"}:
        raise BoundaryError("condition must be exactly 'busy' or 'unknown'")
    candidate = authenticate_candidate(
        config.get("candidate_checkout"), ref=config.get("candidate_ref", PINOKIO_REF)
    )
    old_launcher = _authenticate_old_launcher(config.get("old_launcher"))
    engine = _authenticate_engine(config.get("engine"))
    resource = authenticate_registered_resource(
        config.get("registered_resource", config.get("resource")),
        candidate,
        engine=engine,
        reader=resource_reader,
    )
    controls = config.get("conditions")
    if not isinstance(controls, Mapping) or not isinstance(controls.get(condition), Mapping):
        raise BoundaryError(f"{condition} condition authority absent")
    controller = _authority(controls[condition], f"{condition} condition", CONDITION_PROTOCOL)

    before = _state(controller, runner)
    _validate_observation_binding(before, candidate, resource)
    if before.get("condition") == condition:
        raise BoundaryError(f"{condition} condition is already active before preparation")
    prepared = False
    active: dict[str, Any] | None = None
    after_stop: dict[str, Any] | None = None
    restored: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None
    try:
        prepared = True
        setup = _invoke_json(
            controller, ["--condition", "prepare", "--expected", condition], runner=runner
        )
        if (
            setup["exit_code"] != 0
            or setup.get("condition") != condition
            or setup.get("prepared") is not True
        ):
            raise BoundaryError(f"{condition} preparation did not establish the genuine condition")
        active = _state(controller, runner)
        _validate_observation_binding(active, candidate, resource)
        if active.get("condition") != condition or active == before:
            raise BoundaryError(
                f"{condition} preparation did not establish an observable condition"
            )
        if _custody_state(active) != _custody_state(before):
            raise BoundaryError(f"{condition} preparation changed protected identity/state")
        stop_error: BoundaryError | None = None
        try:
            outcome = _run_engine(engine, resource, candidate, condition, runner=runner)
        except BoundaryError as exc:
            # A disconnect or malformed result can follow a destructive
            # action. Observe before restore so restoration cannot hide it.
            stop_error = exc
        after_stop = _state(controller, runner)
        _validate_observation_binding(after_stop, candidate, resource)
        if _custody_state(after_stop) != _custody_state(before):
            raise BoundaryError("protected custody state changed after Stop attempt")
        if after_stop.get("condition") != condition:
            raise BoundaryError("Stop refusal did not preserve the genuine refusal condition")
        if stop_error is not None:
            raise stop_error
    finally:
        if prepared:
            restored_response = _invoke_json(
                controller, ["--condition", "restore", "--expected", condition], runner=runner
            )
            if restored_response["exit_code"] != 0 or restored_response.get("restored") is not True:
                raise BoundaryError(f"{condition} provider failed to restore its Runtime condition")
            restored = _state(controller, runner)
            _validate_observation_binding(restored, candidate, resource)
            if _protected_state(restored) != _protected_state(before):
                raise BoundaryError(
                    f"{condition} restoration did not reproduce the complete baseline state"
                )
    if active is None or after_stop is None or restored is None or outcome is None:
        raise BoundaryError("Stop refusal scenario completed without full observation evidence")
    return {
        "evidence_class": "boundary-observation",
        "candidate": candidate,
        "registered_resource": resource,
        "old_launcher": old_launcher,
        "engine": {
            "protocol": engine["protocol"],
            "engine_identity": engine["engine_identity"],
            "client_identity": engine["client_identity"],
            "pinokiod": engine["pinokiod"],
            "pterm_source": engine["pterm_source"],
            "pterm": {key: engine["pterm"][key] for key in ("checkout", "ref", "file", "sha256")},
        },
        "condition_authority": {
            key: controller[key] for key in ("protocol", "checkout", "ref", "file", "sha256")
        },
        "condition": condition,
        "invocation": {
            key: outcome[key]
            for key in (
                "route_kind",
                "engine_entrypoint",
                "downstream_argv",
                "exit_code",
                "stdout_sha256",
                "stderr_sha256",
                "refusal_marker",
                "refusal_reason",
            )
        },
        "observations": {
            "before": before,
            "active": active,
            "after_stop": after_stop,
            "restored": restored,
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
                {"error": str(exc), "evidence_class": "none", "status": "refused"},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
