"""Fail-closed R-24 live-provider hook for a run-owned Agent journey.

The hook binds runner-local invocation and export bytes.  It deliberately does
not authenticate hosted artifact custody or make a qualification decision.
"""
from __future__ import annotations

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
    "app": "933c53a6fb1a11e5f8a01335d6252016d703beec",
    "agent": "ab43bad818ef65ca1e929b973a2f61cd9fc4bf61",
    "runtime": "2776ba973b6c8f3555b5629b0840d9691a98d3c2",
    "pinokio": "3a6a98ae251ca126505245286e2d3e760f4348e9",
    "manifest_sha256": "b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f",
}
EXPORT_NAMES = frozenset({
    "junit.xml", "manifest.json", "observation-result.json",
    "observation-busy.json", "observation-unknown.json",
    "observation-restoration.json",
})
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


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


def _runner_identity() -> dict[str, Any]:
    """Bind the candidate-runner source separately from installed Agent bytes."""
    source = Path(__file__)
    if source.is_symlink() or not source.is_file():
        raise JourneyProviderError("runner hook source is unavailable")
    root = source.resolve().parent.parent
    try:
        top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").strip())).resolve()
        head = _git(root, "rev-parse", "HEAD").strip().decode()
        dirty = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    except (UnicodeError, OSError) as exc:
        raise JourneyProviderError("runner identity is unreadable") from exc
    if top != root or not _HEX40.fullmatch(head) or dirty:
        raise JourneyProviderError("runner checkout identity or cleanliness mismatch")
    if head == PUBLISHED_CANDIDATE["agent"]:
        raise JourneyProviderError("candidate runner must differ from installed Agent")
    sources: dict[str, str] = {}
    for relative in ("scripts/ci_journey.py", "scripts/ci_journey_provider.py",
                     "scripts/ci_journey_r24.py"):
        working = _regular(root / relative)
        committed = _git(root, "show", f"{head}:{relative}")
        if _sha(working) != _sha(committed):
            raise JourneyProviderError("runner source differs from its committed identity")
        sources[relative] = _sha(working)
    return {"head": head, "sources": sources,
            "candidate_agent_distinct": True}


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
            or not _HEX32.fullmatch(owner["run_id"])):
        raise JourneyProviderError("run owner identity mismatch")
    paths = []
    for name in ("root", "data_root"):
        value = owner[name]
        if not isinstance(value, str) or not os.path.isabs(value):
            raise JourneyProviderError("run owner path mismatch")
        selected = Path(value)
        try:
            resolved = selected.resolve(strict=True)
        except OSError as exc:
            raise JourneyProviderError("run owner path is unavailable") from exc
        if selected.is_symlink() or not resolved.is_dir() or selected != resolved:
            raise JourneyProviderError("run owner path mismatch")
        paths.append(resolved)
    if paths[0] == paths[1]:
        raise JourneyProviderError("run owner paths must be distinct")
    return owner


def _bind_owner_to_install(owner: dict[str, Any], config: dict[str, Any]) -> None:
    """Join the run owner to the genuine Pinokio-selected install."""
    home = Path(config["engine"]["pinokio_home"])
    backend = (home / "api/astrid/app/astrid").resolve(strict=True)
    raw = _regular(backend / "install.json")
    install = _decode(raw)
    if (not isinstance(install, dict)
            or install.get("owner") != "haus.astrid.runtime"
            or install.get("managed_by") != "pinokio"
            or not isinstance(install.get("data_root"), str)
            or not os.path.isabs(install["data_root"])):
        raise JourneyProviderError("installed owner record mismatch")
    selected_data = Path(install["data_root"])
    try:
        data = selected_data.resolve(strict=True)
    except OSError as exc:
        raise JourneyProviderError("installed data root is unavailable") from exc
    if (selected_data.is_symlink() or selected_data != data or not data.is_dir()
            or owner["root"] != str(backend)
            or owner["data_root"] != str(data)):
        raise JourneyProviderError("run owner differs from installed selection")


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
    stop = _regular(roots["pinokio"] / "pinokio/stop.js")
    committed_stop = _git(roots["pinokio"], "show", f"{selected['pinokio']}:pinokio/stop.js")
    if (_sha(stop) != binding.get("stop_sha256")
            or _sha(committed_stop) != binding.get("stop_sha256")):
        raise JourneyProviderError("candidate Stop differs from producer binding")

    config_raw, config = _read_json_file(config_file, "CONFIG")
    config = _validate_config(config, roots)
    _bind_owner_to_install(owner, config)
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
                    output: provider.LinuxProviderOutput,
                    runtime_execution_sources: dict[str, dict[str, str]]) -> tuple[
                        str, list[dict[str, Any]]]:
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
    common = {"schema", "request_sha256", "run_id", "session", "engine", "provider_ref", "kind",
              "runtime", "runtime_execution_sources", "action_invocation_sha256"}
    additions = {
        "busy": {"engine_binding", "busy", "action_invocation",
                 "protected_before", "protected_after"},
        "unknown": {"unknown", "protected_before", "protected_after"},
        "restoration": {"restoration", "protected_before", "protected_after", "control_removed"},
    }
    shared_runtime = None
    shared_sources = None
    shared_invocation_sha = None
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
                or not isinstance(observation["action_invocation_sha256"], str)
                or not _HEX64.fullmatch(observation["action_invocation_sha256"])
                or rows[kind].source not in request_data["provider"]["sources"]
                or rows[kind].evidence_sha256 != _sha(raw)):
            raise JourneyProviderError("raw observation binding mismatch")
        runtime = _object(observation["runtime"], {
            "pid", "process_birth_id", "runtime_instance_id", "runtime_session_id",
            "runtime_epoch", "endpoint", "realm_id",
        }, "observation runtime")
        if (type(runtime["pid"]) is not int or runtime["pid"] < 1
                or type(runtime["runtime_epoch"]) is not int or runtime["runtime_epoch"] < 1
                or any(not isinstance(runtime[name], str) or not runtime[name]
                       for name in ("process_birth_id", "runtime_instance_id", "runtime_session_id",
                                    "endpoint", "realm_id"))):
            raise JourneyProviderError("raw observation Runtime binding mismatch")
        sources = observation["runtime_execution_sources"]
        if (not isinstance(sources, dict) or set(sources) != {"agent", "runtime"}
                or any(not isinstance(rows_by_owner, dict) or not rows_by_owner
                       or any(not isinstance(path, str) or not path
                              or not isinstance(digest, str) or not _HEX64.fullmatch(digest)
                              for path, digest in rows_by_owner.items())
                       for rows_by_owner in sources.values())):
            raise JourneyProviderError("raw observation execution sources mismatch")
        if sources != runtime_execution_sources:
            raise JourneyProviderError("raw observation sources differ from producer binding")
        invocation_sha = observation["action_invocation_sha256"]
        if shared_runtime is None:
            shared_runtime = runtime
            shared_sources = sources
            shared_invocation_sha = invocation_sha
        elif (runtime != shared_runtime or sources != shared_sources
              or invocation_sha != shared_invocation_sha):
            raise JourneyProviderError("raw observations span multiple Runtime bindings")
        if kind == "busy":
            invocation = observation["action_invocation"]
            if (not isinstance(invocation, dict)
                    or _sha(_canonical(invocation)) != invocation_sha
                    or invocation.get("schema") != "astrid.r24.action-invocation.v1"
                    or invocation.get("request_sha256") != request.sha256
                    or invocation.get("run_id") != request_data["run_id"]
                    or invocation.get("session") != current_session
                    or invocation.get("engine") != observation["engine"]
                    or invocation.get("runtime_execution_sources") != sources
                    or invocation.get("source") not in request_data["provider"]["sources"]
                    or invocation.get("source_sha256") != request_data["provider"]["sources"].get(
                        invocation.get("source"))
                    or invocation.get("returncode") != 0
                    or invocation.get("exit_code") != 0
                    or invocation.get("signal") is not None
                    or invocation.get("raw_result_validated") is not True):
                raise JourneyProviderError("raw action invocation binding mismatch")
        if session is None:
            session = current_session
        elif session != current_session:
            raise JourneyProviderError("raw observations span multiple sessions")
    assert session is not None
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
    runner = _runner_identity()
    evidence = run_root.resolve() / "evidence" / "r24-live"
    evidence.mkdir(parents=True, exist_ok=False)
    receipt_path = evidence / "r24-invocation.json"
    receipt: dict[str, Any] = {
        "schema": "astrid.r24.journey-provider-invocation.v1",
        "run_id": owner["run_id"],
        "status": "not-run",
        "runner": runner,
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
        session, inventory = validate_export(
            export, request, output, binding["runtime_execution_sources"])
    except Exception as exc:
        receipt.update(status="fail", error=f"{type(exc).__name__}: {exc}",
                       invocation={"status": "invalid", "exit": output.returncode,
                                   "stdout": stdout, "stderr": stderr})
        _write_receipt(receipt_path, receipt)
        raise
    receipt.update(
        status="captured-untrusted",
        session=session,
        runtime_execution_sources=binding["runtime_execution_sources"],
        invocation={"status": "completed", "exit": output.returncode,
                    "stdout": stdout, "stderr": stderr},
        export={"path": "provider-export", "files": inventory},
    )
    _write_receipt(receipt_path, receipt)
    return receipt
