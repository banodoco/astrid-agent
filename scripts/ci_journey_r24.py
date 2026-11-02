"""Fail-closed R-24 live-provider hook for a run-owned Agent journey.

The hook binds runner-local invocation and export bytes.  It deliberately does
not authenticate hosted artifact custody or make a qualification decision.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

try:
    from . import ci_journey_provider as provider
except ImportError:
    import ci_journey_provider as provider


PUBLISHED_CANDIDATE = {
    "app": "ce1502e94b935fe6ef1e2f363fc6b55aade4a204",
    "agent": "2de0726d1eaea7f7bd141251e088aa9fdf224525",
    "runtime": "5453366458f7b4ee874c94d15ae7952e3e82d3c3",
    "pinokio": "3a6a98ae251ca126505245286e2d3e760f4348e9",
    "manifest_sha256": "920477210d01615a6698cda30f248552f13c712734e86ea56f283300cb1a9c87",
}
EXPORT_NAMES = frozenset({
    "junit.xml", "manifest.json", "observation-result.json",
    "observation-busy.json", "observation-unknown.json",
    "observation-restoration.json",
})
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_LOCAL_ENDPOINT = re.compile(r"http://127\\.0\\.0\\.1:[1-9][0-9]{0,4}\Z")


class JourneyProviderError(ValueError):
    """The journey cannot safely invoke or retain the live provider."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in items:
        if key in value:
            raise JourneyProviderError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              JourneyProviderError("invalid JSON constant")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise JourneyProviderError(f"invalid JSON: {exc}") from exc


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise JourneyProviderError(f"{label} fields differ")
    return value


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _engine(value: Any, label: str) -> dict[str, Any]:
    engine = _object(value, {"pid", "boot_id", "start_ticks", "port", "node_sha256"}, label)
    if (type(engine["pid"]) is not int or engine["pid"] < 1
            or not isinstance(engine["boot_id"], str) or not _BOOT_ID.fullmatch(engine["boot_id"])
            or not isinstance(engine["start_ticks"], str)
            or not re.fullmatch(r"[1-9][0-9]*", engine["start_ticks"])
            or type(engine["port"]) is not int or not 1 <= engine["port"] <= 65535
            or not isinstance(engine["node_sha256"], str)
            or not _HEX64.fullmatch(engine["node_sha256"])):
        raise JourneyProviderError(f"invalid {label}")
    return engine


def _runtime(value: Any, label: str) -> dict[str, Any]:
    runtime = _object(value, {"pid", "process_birth_id", "runtime_instance_id",
                              "runtime_session_id", "runtime_epoch", "endpoint", "realm_id"},
                      label)
    if (type(runtime["pid"]) is not int or runtime["pid"] < 1
            or type(runtime["runtime_epoch"]) is not int or runtime["runtime_epoch"] < 1
            or not isinstance(runtime["endpoint"], str)
            or not _LOCAL_ENDPOINT.fullmatch(runtime["endpoint"])
            or any(not isinstance(runtime[key], str) or not runtime[key]
                   for key in ("process_birth_id", "runtime_instance_id", "runtime_session_id", "realm_id"))):
        raise JourneyProviderError(f"invalid {label}")
    return runtime


def _captured_stream(value: Any, label: str, *, nonempty: bool) -> dict[str, Any]:
    stream = _object(value, {"bytes", "sha256", "base64"}, label)
    if (type(stream["bytes"]) is not int or not 0 <= stream["bytes"] <= 16384
            or not isinstance(stream["sha256"], str) or not _HEX64.fullmatch(stream["sha256"])
            or not isinstance(stream["base64"], str)):
        raise JourneyProviderError(f"invalid {label}")
    try:
        decoded = base64.b64decode(stream["base64"], validate=True)
    except (ValueError, UnicodeError) as exc:
        raise JourneyProviderError(f"invalid {label}") from exc
    if (len(decoded) != stream["bytes"] or _sha(decoded) != stream["sha256"]
            or (nonempty and not decoded)):
        raise JourneyProviderError(f"invalid {label}")
    return stream


def _action_invocation(value: Any, *, request_data: dict[str, Any], request_sha256: str,
                       session: str, engine: dict[str, Any], digest: str) -> dict[str, Any]:
    action = _object(value, {"schema", "request_sha256", "run_id", "session", "source",
                             "source_sha256", "engine", "runtime_execution_sources",
                             "returncode", "exit_code", "signal", "raw_result", "stderr",
                             "raw_result_validated"}, "action invocation")
    source = "pinokio/providers/r24-action.cjs"
    if (action["schema"] != "astrid.r24.action-invocation.v1"
            or action["request_sha256"] != request_sha256
            or action["run_id"] != request_data["run_id"] or action["session"] != session
            or action["source"] != source
            or action["source_sha256"] != request_data["provider"]["sources"].get(source)
            or action["runtime_execution_sources"] != provider.LIVE_RUNTIME_EXECUTION_SOURCES
            or _engine(action["engine"], "action engine") != engine
            or action["returncode"] != 0 or action["exit_code"] != 0
            or action["signal"] is not None or action["raw_result_validated"] is not True
            or _sha(_canonical(action)) != digest):
        raise JourneyProviderError("action invocation binding mismatch")
    _captured_stream(action["raw_result"], "action stdout", nonempty=True)
    _captured_stream(action["stderr"], "action stderr", nonempty=False)
    return action


def _regular(path: Path, *, limit: int = 1024 * 1024) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise JourneyProviderError(f"missing regular evidence file: {path.name}")
    if path.stat().st_size > limit:
        raise JourneyProviderError(f"oversized evidence file: {path.name}")
    return path.read_bytes()


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(["git", "-C", str(root), *args],
                                       stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise JourneyProviderError("Git identity validation failed") from exc


def _checkout(path: Path, ref: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise JourneyProviderError("absolute nonsymlink checkout required")
    root = path.resolve(strict=True)
    try:
        top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").strip())).resolve()
        head = _git(root, "rev-parse", "HEAD").strip().decode()
        dirty = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    except (UnicodeError, OSError) as exc:
        raise JourneyProviderError("checkout identity is unreadable") from exc
    if top != root or head != ref or dirty:
        raise JourneyProviderError("checkout ref or cleanliness mismatch")
    return root


def _read_json_file(path: Path, label: str) -> tuple[bytes, Any]:
    if not path.is_absolute() or path.is_symlink():
        raise JourneyProviderError(f"absolute nonsymlink {label} required")
    raw = _regular(path.resolve(strict=True))
    return raw, _decode(raw)


def _owner(run_root: Path) -> dict[str, Any]:
    if not run_root.is_absolute() or run_root.is_symlink() or not run_root.is_dir():
        raise JourneyProviderError("absolute nonsymlink run root required")
    root = run_root.resolve(strict=True)
    raw, owner = _read_json_file(root / "tst-b1-owner.json", "run owner")
    del raw
    _object(owner, {"schema_version", "run_id", "root", "data_root"}, "run owner")
    if (owner["schema_version"] != 1 or not isinstance(owner["run_id"], str)
            or not _HEX32.fullmatch(owner["run_id"])
            or owner["root"] != str(root / "program")
            or owner["data_root"] != str(root / "data")):
        raise JourneyProviderError("run owner identity mismatch")
    return owner


def _validate_config(config: Any, checkouts: dict[str, Path]) -> dict[str, Any]:
    fields = {"checkouts", "pterm", "pinokiod", "node", "node_sha256",
              "pterm_entry_sha256", "unws_sha256", "engine", "scratch_root"}
    config = _object(config, fields, "CONFIG")
    paths = _object(config["checkouts"], set(checkouts), "CONFIG.checkouts")
    _object(config["engine"], {"pid", "boot_id", "start_ticks", "pinokio_home", "port"},
            "CONFIG.engine")
    for name, checkout in checkouts.items():
        value = paths[name]
        if not isinstance(value, str) or not os.path.isabs(value):
            raise JourneyProviderError(f"CONFIG checkout {name} is not absolute")
        try:
            selected = Path(value).resolve(strict=True)
        except OSError as exc:
            raise JourneyProviderError(f"CONFIG checkout {name} is unavailable") from exc
        if Path(value) != checkout or Path(value).is_symlink() or selected != checkout:
            raise JourneyProviderError(f"CONFIG checkout {name} differs from candidate")
    for key in ("node_sha256", "pterm_entry_sha256", "unws_sha256"):
        if not isinstance(config[key], str) or not _HEX64.fullmatch(config[key]):
            raise JourneyProviderError(f"invalid CONFIG {key}")
    return config


def prepare_request(*, run_root: Path, candidate_file: Path, config_file: Path,
                    producer_root: Path, checkouts: dict[str, Path]) -> tuple[
                        provider.ProviderRequest, bytes, dict[str, Any], dict[str, Any]]:
    """Validate exact candidates and sources before constructing the request."""
    owner = _owner(run_root)
    _, selected = _read_json_file(candidate_file, "candidate")
    selected = _object(selected, set(PUBLISHED_CANDIDATE), "candidate")
    if selected != PUBLISHED_CANDIDATE:
        raise JourneyProviderError("candidate differs from the published tuple")
    if selected != provider.LIVE_CANDIDATE or selected["pinokio"] != provider.LIVE_STOP["ref"]:
        raise JourneyProviderError("typed provider is not bound to the published tuple")
    if set(checkouts) != {"app", "agent", "runtime", "pinokio"}:
        raise JourneyProviderError("four candidate checkouts are required")
    roots = {name: _checkout(path, selected[name]) for name, path in checkouts.items()}
    producer_root = _checkout(producer_root, provider.LIVE_PRODUCER_REF)
    if len({*roots.values(), producer_root}) != 5:
        raise JourneyProviderError("candidate and producer checkouts must be distinct")

    manifest = _regular(roots["app"] / "public/install-manifest.json")
    committed_manifest = _git(
        roots["app"], "show", f"{selected['app']}:public/install-manifest.json")
    parsed_manifest = _decode(manifest)
    if (_sha(manifest) != selected["manifest_sha256"]
            or _sha(committed_manifest) != selected["manifest_sha256"]
            or not isinstance(parsed_manifest, dict)
            or parsed_manifest.get("release") != 1
            or any((parsed_manifest.get(name) or {}).get("sha") != selected[name]
                   for name in ("agent", "runtime"))):
        raise JourneyProviderError("App manifest does not select the candidate tuple")

    audited = {**provider.LIVE_PRODUCER_FILES, **provider.LIVE_INTERFACE_FILES}
    for relative, digest in audited.items():
        working = _regular(producer_root / relative)
        committed = _git(producer_root, "show", f"{provider.LIVE_PRODUCER_REF}:{relative}")
        if _sha(working) != digest or _sha(committed) != digest:
            raise JourneyProviderError("producer source digest mismatch")
    binding_raw = _regular(producer_root / "pinokio/providers/r24-linux-binding.json")
    binding = _decode(binding_raw)
    if binding.get("candidate") != selected:
        raise JourneyProviderError("producer binding candidate differs from published tuple")
    if binding.get("runtime_execution_sources") != provider.LIVE_RUNTIME_EXECUTION_SOURCES:
        raise JourneyProviderError("producer runtime execution source inventory differs")
    stop = _regular(roots["pinokio"] / "pinokio/stop.js")
    committed_stop = _git(roots["pinokio"], "show", f"{selected['pinokio']}:pinokio/stop.js")
    if (_sha(stop) != binding.get("stop_sha256")
            or _sha(committed_stop) != binding.get("stop_sha256")):
        raise JourneyProviderError("candidate Stop differs from producer binding")
    if binding["stop_sha256"] != provider.LIVE_STOP["sha256"]:
        raise JourneyProviderError("typed provider Stop binding differs")

    config_raw, config = _read_json_file(config_file, "CONFIG")
    config = _validate_config(config, roots)
    canonical_config = _canonical(config)
    if _decode(config_raw) != config:
        raise JourneyProviderError("CONFIG changed while validating")
    request_data = {
        "schema": provider.REQUEST_SCHEMA,
        "candidate": selected,
        "provider": {"ref": provider.LIVE_PRODUCER_REF,
                     "sources": provider.LIVE_PRODUCER_FILES},
        "stop": {"ref": selected["pinokio"], "path": "pinokio/stop.js",
                 "sha256": binding["stop_sha256"]},
        "config_sha256": _sha(canonical_config),
        "run_id": owner["run_id"],
    }
    return provider.ProviderRequest.parse(_canonical(request_data)), canonical_config, binding, owner


def validate_export(export: Path, request: provider.ProviderRequest,
                    output: provider.LinuxProviderOutput) -> tuple[str, list[dict[str, Any]]]:
    """Join every fixed export member to one request, run, session and result."""
    if export.is_symlink() or not export.is_dir():
        raise JourneyProviderError("provider export is unavailable")
    names = {item.name for item in export.iterdir()}
    if names != EXPORT_NAMES:
        raise JourneyProviderError("provider export inventory differs")
    payloads = {name: _regular(export / name) for name in sorted(names)}
    inventory = [{"path": name, "bytes": len(data), "sha256": _sha(data)}
                 for name, data in payloads.items()]
    manifest = _object(_decode(payloads["manifest.json"]),
                       {"schema", "qualification", "files"}, "export manifest")
    expected_files = [row for row in inventory if row["path"] != "manifest.json"]
    if (manifest["schema"] != "astrid.r24.producer-export.v1"
            or manifest["qualification"] != "not-evaluated"
            or manifest["files"] != expected_files):
        raise JourneyProviderError("provider export manifest mismatch")

    exported_result = provider.ProviderResult.parse(payloads["observation-result.json"], request)
    stdout_result = provider.ProviderResult.parse(output.stdout, request)
    if exported_result != output.result or stdout_result != output.result:
        raise JourneyProviderError("stdout and exported result differ")
    request_data = json.loads(request.canonical)
    rows = {row.kind: row for row in output.result.observations}
    session = None
    observed_engine = None
    observed_runtime = None
    action_digest = None
    common = {"schema", "request_sha256", "run_id", "session", "engine", "provider_ref",
              "runtime", "runtime_execution_sources", "action_invocation_sha256", "kind"}
    additions = {
        "busy": {"engine_binding", "busy", "action_invocation", "protected_before", "protected_after"},
        "unknown": {"unknown", "protected_before", "protected_after"},
        "restoration": {"restoration", "protected_before", "protected_after", "control_removed"},
    }
    for kind in ("busy", "unknown", "restoration"):
        raw = payloads[f"observation-{kind}.json"]
        observation = _object(_decode(raw), common | additions[kind], f"{kind} observation")
        current_session = observation["session"]
        if (observation["schema"] != "astrid.r24.live-observation.v1"
                or observation["request_sha256"] != request.sha256
                or observation["run_id"] != request_data["run_id"]
                or observation["provider_ref"] != request_data["provider"]["ref"]
                or observation["kind"] != kind
                or not isinstance(current_session, str) or not _HEX32.fullmatch(current_session)
                or rows[kind].source != "pinokio/providers/r24-action.cjs"
                or rows[kind].source not in request_data["provider"]["sources"]
                or rows[kind].evidence_sha256 != _sha(raw)):
            raise JourneyProviderError("raw observation binding mismatch")
        engine = _engine(observation["engine"], "observation engine")
        runtime = _runtime(observation["runtime"], "observation runtime")
        digest = observation["action_invocation_sha256"]
        if (not isinstance(digest, str) or not _HEX64.fullmatch(digest)
                or observation["runtime_execution_sources"] != provider.LIVE_RUNTIME_EXECUTION_SOURCES):
            raise JourneyProviderError("raw observation execution binding mismatch")
        if session is None:
            session = current_session
        elif session != current_session:
            raise JourneyProviderError("raw observations span multiple sessions")
        if observed_engine is None:
            observed_engine = engine
        elif observed_engine != engine:
            raise JourneyProviderError("raw observations span multiple engines")
        if observed_runtime is None:
            observed_runtime = runtime
        elif observed_runtime != runtime:
            raise JourneyProviderError("raw observations span multiple Runtime identities")
        if action_digest is None:
            action_digest = digest
        elif action_digest != digest:
            raise JourneyProviderError("raw observations span multiple action records")
        if kind == "busy":
            _action_invocation(observation["action_invocation"], request_data=request_data,
                               request_sha256=request.sha256, session=current_session,
                               engine=engine, digest=digest)
    assert session is not None
    assert observed_engine is not None and observed_runtime is not None and action_digest is not None
    return session, inventory


def _stream_record(path: Path, data: bytes) -> dict[str, Any]:
    path.write_bytes(data)
    return {"path": path.name, "bytes": len(data), "sha256": _sha(data)}


def _write_receipt(path: Path, value: dict[str, Any]) -> None:
    path.write_bytes(json.dumps(value, indent=2, sort_keys=True, allow_nan=False).encode("ascii") + b"\n")


def execute_live_hook(*, run_root: Path, candidate_file: Path, config_file: Path,
                      producer_root: Path, checkouts: dict[str, Path]) -> dict[str, Any]:
    """Invoke once after preflight, retaining a fail-closed run-owned receipt."""
    owner = _owner(run_root)
    evidence = run_root.resolve() / "evidence" / "r24-live"
    evidence.mkdir(parents=True, exist_ok=False)
    receipt_path = evidence / "r24-invocation.json"
    receipt: dict[str, Any] = {
        "schema": "astrid.r24.journey-provider-invocation.v1",
        "run_id": owner["run_id"],
        "status": "not-run",
        "candidate": PUBLISHED_CANDIDATE,
        "provider": {"ref": provider.LIVE_PRODUCER_REF,
                     "sources": provider.LIVE_PRODUCER_FILES,
                     "interface": provider.LIVE_INTERFACE_FILES},
        "invocation": {"status": "not-run"},
        "admission": {"status": "awaiting-evidence", "authenticated_custody": False,
                      "qualification": "not-evaluated"},
    }
    try:
        request, config, binding, _ = prepare_request(
            run_root=run_root, candidate_file=candidate_file, config_file=config_file,
            producer_root=producer_root, checkouts=checkouts)
        (evidence / "request.json").write_bytes(request.canonical + b"\n")
        (evidence / "config.json").write_bytes(config + b"\n")
        receipt.update(request_sha256=request.sha256, config_sha256=_sha(config),
                       binding_sha256=provider.LIVE_PRODUCER_FILES[
                           "pinokio/providers/r24-linux-binding.json"],
                       producer_binding_candidate=binding["candidate"])
    except Exception as exc:
        receipt.update(status="fail", error=f"{type(exc).__name__}: {exc}")
        _write_receipt(receipt_path, receipt)
        raise

    export = evidence / "provider-export"
    try:
        output = provider.invoke_linux_provider_export(
            request, config, str(producer_root.resolve()), str(export))
    except provider.ProviderInvocationError as exc:
        stdout = _stream_record(evidence / "provider-stdout.json", exc.stdout)
        stderr = _stream_record(evidence / "provider-stderr.log", exc.stderr)
        receipt.update(status="fail", error=f"{type(exc).__name__}: {exc}",
                       invocation={"status": "failed", "exit": exc.returncode,
                                   "stdout": stdout, "stderr": stderr})
        _write_receipt(receipt_path, receipt)
        raise
    except Exception as exc:
        receipt.update(status="fail", error=f"{type(exc).__name__}: {exc}",
                       invocation={"status": "rejected", "exit": None})
        _write_receipt(receipt_path, receipt)
        raise
    stdout = _stream_record(evidence / "provider-stdout.json", output.stdout)
    stderr = _stream_record(evidence / "provider-stderr.log", output.stderr)
    try:
        session, inventory = validate_export(export, request, output)
    except Exception as exc:
        receipt.update(status="fail", error=f"{type(exc).__name__}: {exc}",
                       invocation={"status": "invalid", "exit": output.returncode,
                                   "stdout": stdout, "stderr": stderr})
        _write_receipt(receipt_path, receipt)
        raise
    receipt.update(
        status="captured-untrusted",
        session=session,
        invocation={"status": "completed", "exit": output.returncode,
                    "stdout": stdout, "stderr": stderr},
        export={"path": "provider-export", "files": inventory},
    )
    _write_receipt(receipt_path, receipt)
    return receipt
