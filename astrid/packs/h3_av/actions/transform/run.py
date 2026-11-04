"""SDK-connected H3 audiovisual transform orchestration."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import zipfile
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from os import environ
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from runtime_protocol import platform
from runtime_protocol.dirfd import filesystem as os

from astrid.core._shared.result_manifest import write_json_atomic
from astrid.core.contracts.errors import AstridError, coerce_astrid_error
from astrid.core.execution.terminal_result import serialize_terminal_result
from astrid.core.foundation.hash import sha256_file
from astrid.core.env_vars import ASTRID_TASK_RUN_ID
from astrid.core.execution.worker_qualification import (
    WorkerQualificationError,
    ensure_runpod_worker,
    load_worker_qualification,
    qualification_digest,
)
from astrid.core.pack.entrypoint import guard_canonical_entrypoint, run_pack_main
from astrid.packs.h3_av.actions.compose.run import _video_media_type
from astrid.packs.h3_av.actions.transform.operation import OperationJournal, OperationJournalError
from astrid.packs.h3_av.actions.transform.receipt import (
    attest_runtime_managed_composition,
    attest_runtime_managed_publication,
    build_final_receipt,
    write_final_receipt,
)
from astrid.packs.h3_av.shared.input_bundle import build_input_bundle, materialize_input_bundle
from astrid.packs.h3_av.shared.request import load_request_document, normalize_request
from astrid.packs.h3_av.shared.request_v2 import CHARACTER_SWAP_ID, branch_for
from astrid.sdk import AstridClient, generate, observe_task_invocation
from astrid.sdk.results import InvocationResult


UNSUPPORTED_TARGET_MESSAGE = (
    "H3 execution-request target must be default or runpod; choose where H3 runs with --placement local|runpod"
)
LOCAL_ENGINE_INSTALL_ACTION = "astrid engines install vibecomfy-h3"


def _run_remote_placement(args: argparse.Namespace) -> dict[str, Any]:
    """``--placement runpod``: claim a pod, run the local command there, pull back, release."""

    from astrid.packs.runpod.remote_placement import run_remote_placement

    return run_remote_placement(args)


def _resolve_local_engine() -> Any:
    """Resolve the installed local engine endpoint or fail fast with the next action."""

    from astrid.core.generation.engine_contracts import EngineError
    from astrid.core.generation.engine_local_endpoint import EngineNotInstalled, local_engine_endpoint

    try:
        return local_engine_endpoint("vibecomfy-h3")
    except EngineError as exc:
        if exc.code == "endpoint_ownership":
            raise AstridError(f"local H3 engine endpoint ownership error ({exc.code}): {exc}",
                              code=exc.code) from exc
        reason = f"readiness reason: {exc.reason_code}; " if isinstance(exc, EngineNotInstalled) else ""
        raise AstridError(
            f"local H3 engine is not ready ({exc.code}): "
            f"{reason}run `{LOCAL_ENGINE_INSTALL_ACTION}` first",
            recovery_command=LOCAL_ENGINE_INSTALL_ACTION,
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a bounded H3 audiovisual transform.")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--asset-map", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--project")
    parser.add_argument("--execution-request", type=Path)
    worker_group = parser.add_mutually_exclusive_group()
    worker_group.add_argument(
        "--worker-qualification",
        type=Path,
        help="deployment-owned qualified-worker receipt required before targeted admission",
    )
    worker_group.add_argument(
        "--prepared-worker",
        type=Path,
        help="select a prepared RunPod deployment for the canonical VibeComfy task",
    )
    parser.add_argument(
        "--require-worker-qualification",
        action="store_true",
        help="fail before admission unless a qualified or prepared worker is selected",
    )
    parser.add_argument(
        "--editorial-approved",
        action="store_true",
        help="record explicit editorial approval in the final receipt",
    )
    parser.add_argument(
        "--cleanup-receipt",
        type=Path,
        help="JSON receipt listing exact owned-resource cleanup postconditions",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume from SDK recovery receipts or verified legacy locators without resampling",
    )
    parser.add_argument(
        "--placement",
        choices=("local", "runpod"),
        default=None,
        help=(
            "where the H3 engine runs (default: local, or runpod when a legacy "
            "--worker-qualification/--prepared-worker/runpod target is given)"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _json_mapping(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AstridError(f"{path} must contain a JSON object")
    return value


def _direct_output(result: Any, name: str) -> Path | None:
    outputs = getattr(result, "outputs", {})
    value = outputs.get(name) if isinstance(outputs, Mapping) else None
    candidates: list[Any] = [value]
    if isinstance(outputs, Mapping):
        candidates.extend(outputs.get(key, []) for key in ("artifacts", "managed_outputs"))
    raw = getattr(result, "raw_result", {})
    if isinstance(raw, Mapping):
        raw_outputs = raw.get("outputs")
        if isinstance(raw_outputs, Mapping):
            candidates.append(raw_outputs.get(name))
            candidates.append(raw_outputs.get("artifacts"))
        candidates.append(raw.get("managed_outputs"))
    flattened: list[Any] = []
    for candidate in candidates:
        if isinstance(candidate, list):
            flattened.extend(candidate)
        elif candidate is not None:
            flattened.append(candidate)
    for candidate in flattened:
        if isinstance(candidate, str):
            path = Path(candidate).expanduser()
            if path.is_file():
                return path.resolve()
        if isinstance(candidate, Mapping):
            candidate_name = candidate.get("name") or candidate.get("output_port")
            if candidate_name not in (None, name):
                continue
            raw_path = candidate.get("path") or candidate.get("local_path") or candidate.get("file")
            if isinstance(raw_path, str) and Path(raw_path).expanduser().is_file():
                return Path(raw_path).expanduser().resolve()
    return None


def _managed_rows(result: Any, name: str) -> list[Mapping[str, Any]]:
    outputs = getattr(result, "outputs", {})
    raw = getattr(result, "raw_result", {})
    values: list[Any] = []
    containers: list[Mapping[str, Any]] = []
    for container in (outputs, raw):
        if not isinstance(container, Mapping):
            continue
        containers.append(container)
        nested_outputs = container.get("outputs")
        if isinstance(nested_outputs, Mapping):
            containers.append(nested_outputs)
    for container in containers:
        for key in ("managed_outputs", "artifacts"):
            value = container.get(key)
            if isinstance(value, list):
                values.extend(value)
            elif isinstance(value, Mapping):
                values.extend(value.values())
    rows: list[Mapping[str, Any]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        row_name = value.get("name") or value.get("output_port")
        producer = value.get("producer")
        producer_port = producer.get("output_port") if isinstance(producer, Mapping) else None
        if row_name == name or (
            name == "vibecomfy_run"
            and row_name in {"vibecomfy_run", "generated_videos", "video", "audio"}
        ) or (
            name == "vibecomfy_run"
            and producer_port in {"video", "audio", "generated_video", "generated_audio"}
        ) or (
            name == "vibecomfy_run"
            and _output_role(value) in {"video", "audio"}
        ):
            rows.append(value)
    unique: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        producer = row.get("producer")
        producer_id = (
            producer.get("producer_output_id")
            if isinstance(producer, Mapping)
            else row.get("producer_output_id")
        )
        identity = row.get("association_id") or producer_id or row.get("object_id") or row.get("digest") or row.get("content_hash")
        key = (str(identity), str(_output_role(row) or row.get("name") or row.get("output_port") or ""))
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def _output_role(row: Mapping[str, Any]) -> str | None:
    """Return the producer-declared audiovisual role for one settled row."""

    producer = row.get("producer")
    producer_port = producer.get("output_port") if isinstance(producer, Mapping) else None
    port = producer_port or row.get("output_port") or row.get("role")
    if isinstance(port, str):
        if port in {"video", "generated_video", "generated_videos", "SaveVideo"}:
            return "video"
        if port in {"audio", "generated_audio", "generated_audios", "SaveAudio"}:
            return "audio"
    media_type = row.get("media_type")
    if not isinstance(media_type, str) and isinstance(producer, Mapping):
        media_type = producer.get("media_type")
    if isinstance(media_type, str):
        if media_type.startswith("video/"):
            return "video"
        if media_type.startswith("audio/"):
            return "audio"
    return None


def _digest(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    raw = value.get("object_id") or value.get("digest") or value.get("content_hash")
    if not isinstance(raw, str):
        return None
    normalized = raw.removeprefix("sha256:")
    return "sha256:" + normalized if re.fullmatch(r"[0-9a-f]{64}", normalized) else None


def _descriptor(row: Mapping[str, Any], *, filename: str) -> dict[str, Any]:
    object_id = _digest(row)
    if object_id is None:
        raise AstridError(f"managed artifact {filename!r} has no canonical object digest")
    return {
        "object_id": object_id,
        "digest": object_id,
        "filename": Path(filename).name,
        "required": True,
    }


def _import_runtime_file(client: Any, *, project: str | None, path: Path, filename: str) -> dict[str, Any]:
    """Upload an immutable local input through the connected Runtime client."""

    importer = getattr(getattr(client, "media", None), "import_file", None)
    if not callable(importer):
        raise AstridError("connected Runtime client cannot import a managed input")
    if not isinstance(project, str) or not project:
        current = getattr(getattr(client, "projects", None), "current", None)
        observed = current() if callable(current) else None
        data = getattr(observed, "data", observed)
        if isinstance(data, Mapping):
            project = str(data.get("project_id") or data.get("id") or "") or None
    if not project:
        raise AstridError("H3 transform requires a selected project to stage managed inputs")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = importer(
        project=project,
        path=path,
        idempotency_key=f"h3-av-input-{digest}",
    )
    if not getattr(result, "ok", False):
        raise AstridError(f"could not import managed H3 input {filename!r}: {getattr(result, 'error', result)}")
    data = getattr(result, "data", None)
    if not isinstance(data, Mapping):
        raise AstridError(f"managed H3 input {filename!r} returned no object descriptor")
    descriptor = _descriptor(data, filename=filename)
    if descriptor["digest"] != "sha256:" + digest:
        raise AstridError(f"managed H3 input {filename!r} import returned a different digest")
    return descriptor


def _materialize_output(
    client: Any,
    result: Any,
    name: str,
    out_dir: Path,
    *,
    managed_only: bool = False,
    expected_filename: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    if not managed_only:
        local = _direct_output(result, name)
        if local is not None:
            return local, {"path": str(local), "sha256": hashlib.sha256(local.read_bytes()).hexdigest()}
    rows = _managed_rows(result, name)
    if not rows:
        raise AstridError(f"child {getattr(result, 'capability_id', '?')} did not return settled output {name!r}")
    # A primary result is authoritative; do not accidentally retrieve a JSON
    # receipt or an auxiliary output with the same task.
    row = next((item for item in rows if item.get("is_primary") or item.get("role") == "result"), rows[0])
    object_id = _digest(row)
    if object_id is None:
        raise AstridError(f"managed output {name!r} has no canonical object digest")
    data = client.media.read_bytes(object_id)
    if not isinstance(data, bytes):
        raise AstridError(f"managed output {name!r} did not return bytes")
    actual = "sha256:" + hashlib.sha256(data).hexdigest()
    if actual != object_id:
        raise AstridError(f"managed output {name!r} failed digest verification")
    declared_size = row.get("size")
    if declared_size is not None and declared_size != len(data):
        raise AstridError(f"managed output {name!r} failed size verification")
    filename = row.get("filename") or row.get("name") or name
    filename = Path(str(filename)).name
    if not filename or filename in {".", ".."}:
        filename = name
    if expected_filename is not None and filename != expected_filename:
        raise AstridError(
            f"managed output {name!r} returned filename {filename!r}; expected {expected_filename!r}"
        )
    destination = out_dir / "retrieved" / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return destination, {**dict(row), "object_id": object_id, "digest": object_id, "path": str(destination), "sha256": object_id.removeprefix("sha256:")}


def _retrieve_generation_outputs(
    client: Any,
    result: Any,
    out_dir: Path,
) -> dict[str, tuple[Path, dict[str, Any]]]:
    """Retrieve LanPaint's required producer outputs without flattening roles."""

    rows = _managed_rows(result, "vibecomfy_run")
    by_role: dict[str, list[Mapping[str, Any]]] = {"video": [], "audio": []}
    if isinstance(client, ManagedParentClient):
        by_role["video"] = client.selected_raw(rows)
        for row in rows:
            producer = row.get("producer", {})
            if isinstance(producer, Mapping) and producer.get("output_port") in {"audio", "generated_audio", "generated_audios", "SaveAudio"}:
                by_role["audio"].append(row)
    else:
        for row in rows:
            role = _output_role(row)
            if role is not None:
                by_role[role].append(row)
    missing = [role for role in ("video", "audio") if not by_role[role]]
    if missing:
        raise AstridError(
            "managed LanPaint generation is missing required output role(s): "
            + ", ".join(missing)
        )
    duplicate = [role for role in ("video", "audio") if len(by_role[role]) != 1]
    if duplicate:
        raise AstridError(
            "managed LanPaint generation has ambiguous output role(s): "
            + ", ".join(duplicate)
        )
    return {
        role: (
            _materialize_output(
                client,
                SimpleNamespace(  # type: ignore[name-defined]
                    capability_id=getattr(result, "capability_id", "vibecomfy.run"),
                    outputs={"managed_outputs": [row]},
                    raw_result={},
                ),
                "vibecomfy_run",
                out_dir,
                managed_only=True,
            )
        )
        for role, row_list in by_role.items()
        for row in row_list
    }


def _retrieve_muxed_generation(
    client: Any,
    result: Any,
    out_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    """Retrieve the native graph's one full-timeline muxed AV result."""

    rows = _managed_rows(result, "vibecomfy_run")
    if isinstance(client, ManagedParentClient):
        rows = client.selected_raw(rows)
    if len(rows) != 1:
        raise AstridError(
            "managed native H3 generation must return exactly one muxed AV output; "
            f"observed {len(rows)}"
        )
    row = rows[0]
    return _materialize_output(
        client,
        SimpleNamespace(
            capability_id=getattr(result, "capability_id", "vibecomfy.run"),
            outputs={"managed_outputs": [row]},
            raw_result={},
        ),
        "vibecomfy_run",
        out_dir,
        managed_only=True,
    )


def _write_generated_bundle(
    outputs: Mapping[str, tuple[Path, Mapping[str, Any]]],
    destination: Path,
) -> Path:
    """Create a deterministic, role-bearing bundle for compose/verify."""

    if set(outputs) != {"video", "audio"}:
        raise AstridError("LanPaint generated bundle requires exactly video and audio outputs")
    records: list[dict[str, Any]] = []
    payloads: list[tuple[str, bytes]] = []
    for role in ("video", "audio"):
        path, row = outputs[role]
        if not path.is_file():
            raise AstridError(f"retrieved LanPaint {role} output is missing: {path}")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        declared = row.get("sha256")
        if declared != digest:
            raise AstridError(f"retrieved LanPaint {role} output failed digest verification")
        member = f"outputs/{role}-{path.name}"
        records.append(
            {
                "role": role,
                "member": member,
                "filename": path.name,
                "sha256": digest,
                "size": len(data),
                "object_id": row.get("object_id"),
                "producer_output_id": row.get("producer_output_id"),
                "output_port": row.get("producer", {}).get("output_port", role)
                if isinstance(row.get("producer"), Mapping)
                else row.get("output_port", role),
            }
        )
        payloads.append((member, data))
    manifest = {"schema_version": 1, "kind": "h3_av_generated_av", "outputs": records}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as archive:
        for member, data in [("manifest.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")), *payloads]:
            info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, data)
    return destination


def _retrieve_compiled_workflow(
    client: Any,
    compiled: Any,
    compilation: Mapping[str, Any],
    out_dir: Path,
    *,
    materialized_paths: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Retrieve and attest the exact canonical bundle selected by compilation."""

    workflow = compilation.get("workflow")
    if not isinstance(workflow, Mapping):
        raise AstridError("compilation.json is missing its selected workflow manifest")
    inputs: dict[str, Any] = {}
    for port, filename in (
        ("python", "workflow.py"),
        ("companion", "workflow.vibe.json"),
        ("source", "source.json"),
    ):
        identity = workflow.get(filename)
        expected = identity.get("sha256") if isinstance(identity, Mapping) else None
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise AstridError(f"compilation.json has no valid hash for workflow member {filename!r}")
        path, row = _materialize_output(
            client,
            compiled,
            port,
            out_dir,
            managed_only=True,
            expected_filename=filename,
        )
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise AstridError(
                f"managed workflow member {filename!r} failed compilation hash/provenance verification"
            )
        inputs[port] = _descriptor(row, filename=filename)
        if materialized_paths is not None:
            materialized_paths[port] = path
    return inputs


def _materialize_prepared_worker_workflow_bundle(
    paths: Mapping[str, Path],
    compilation: Mapping[str, Any],
    destination: Path,
) -> Path:
    """Freeze the verified H3 bundle under this operation for P2 staging."""

    expected_names = {
        "python": "workflow.py",
        "companion": "workflow.vibe.json",
        "source": "source.json",
    }
    if set(paths) != set(expected_names):
        raise AstridError("prepared RunPod execution requires the complete canonical workflow bundle")
    workflow = compilation.get("workflow")
    if not isinstance(workflow, Mapping):
        raise AstridError("compilation.json is missing its selected workflow manifest")
    expected_hashes: dict[str, str] = {}
    for port, filename in expected_names.items():
        row = workflow.get(filename)
        digest = row.get("sha256") if isinstance(row, Mapping) else None
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AstridError(f"compilation.json has no valid hash for {filename!r}")
        source = Path(paths[port])
        if source.is_symlink() or not source.is_file():
            raise AstridError(f"materialized workflow member {filename!r} is not a regular file")
        if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise AstridError(f"materialized workflow member {filename!r} changed after verification")
        expected_hashes[filename] = digest

    if destination.is_symlink():
        raise AstridError("prepared workflow bundle path must not be a symlink")
    if destination.exists():
        if not destination.is_dir() or {item.name for item in destination.iterdir()} != set(expected_names.values()):
            raise AstridError("saved prepared workflow bundle has unexpected content")
        for port, filename in expected_names.items():
            target = destination / filename
            if (target.is_symlink() or not target.is_file()
                    or hashlib.sha256(target.read_bytes()).hexdigest() != expected_hashes[filename]):
                raise AstridError("saved prepared workflow bundle differs from the frozen compilation")
    else:
        destination.mkdir(parents=True, mode=0o700)
        try:
            for port, filename in expected_names.items():
                (destination / filename).write_bytes(Path(paths[port]).read_bytes())
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    return destination / "workflow.py"


def _provenance(preparation: Mapping[str, Any], compilation: Mapping[str, Any]) -> dict[str, Any]:
    request_digest = preparation.get("request_digest")
    if request_digest != compilation.get("request_digest"):
        raise AstridError("H3 request provenance does not match the selected compilation")
    assets = preparation.get("assets")
    workflow = compilation.get("workflow")
    managed_assets = compilation.get("managed_assets")
    if not isinstance(workflow, Mapping) or not isinstance(managed_assets, Mapping):
        raise AstridError("H3 compilation is missing managed workflow or asset provenance")
    return {
        "schema_version": 1,
        "request_digest": request_digest,
        "schedule_digest": preparation.get("mask_schedule", {}).get("digest"),
        "assets": [dict(item) for item in assets] if isinstance(assets, list) else [],
        "graph": {
            "profile": compilation.get("profile"),
            "compilation_digest": compilation.get("compilation_digest"),
            "workflow": dict(workflow),
            "managed_assets": dict(managed_assets),
        },
    }


def _authoritative_source_asset_id(request: Any) -> str | None:
    """Return the source baseline asset without projecting v1 keys onto v2."""

    value = request.value
    if value.get("version") != 2:
        source = value["source"]
        return None if source is None else str(source["asset"])
    branch = branch_for(request)
    if branch in {"source_free", "audio_only"}:
        return None
    videos = [
        item
        for item in value["media"]
        if item["role"] == "timeline" and item.get("modality") == "video"
    ]
    return str(videos[0]["asset"]) if len(videos) == 1 else None


def _write_provenance_preparation(root: Path, preparation: Mapping[str, Any], provenance: Mapping[str, Any]) -> Path:
    enriched = dict(preparation)
    enriched["provenance"] = dict(provenance)
    path = root / "provenance-preparation.json"
    path.write_text(json.dumps(enriched, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


_LEASE_CATEGORIES = frozenset({
    "lease_loss_unclassified", "task_read_exception", "lease_metadata_invalid",
    "lease_expired", "task_state_changed", "attempt_changed", "fence_changed",
    "heartbeat_exception",
})


def _bounded_lease_diagnostic(value: Any) -> dict[str, Any] | None:
    """Copy only the host's bounded lease evidence, never its message or payload."""
    if not isinstance(value, Mapping) or type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        return None
    category = value.get("category")
    if not isinstance(category, str) or category not in _LEASE_CATEGORIES:
        return None
    result: dict[str, Any] = {"schema_version": 1, "category": category}

    def timestamp(item: Any, *, utc: bool = False) -> str | None:
        if not isinstance(item, str) or len(item) > 96:
            return None
        try:
            parsed = datetime.fromisoformat(item)
            offset = parsed.utcoffset()
            if offset is None or (utc and offset != timedelta(0)):
                return None
        except ValueError:
            return None
        return item

    for key in ("observed_at_utc", "last_successful_heartbeat_at_utc", "last_successful_heartbeat_expiry"):
        if key in value and value[key] is None and key.startswith("last_successful_"):
            result[key] = None
        elif (safe := timestamp(value.get(key), utc=key.endswith("_at_utc"))) is not None:
            result[key] = safe
    for key, limit, pattern in (
        ("exception_type", 64, r"[A-Za-z_][A-Za-z0-9_]*"),
        ("runtime_code", 64, r"[A-Za-z0-9._-]+"),
        ("request_id", 96, r"[A-Za-z0-9._:-]+"),
    ):
        item = value.get(key)
        if isinstance(item, str) and len(item) <= limit and re.fullmatch(pattern, item):
            result[key] = item
    observed = value.get("observed")
    if isinstance(observed, Mapping):
        safe_observed: dict[str, str] = {}
        for key, limit, pattern in (
            ("state", 48, r"[A-Za-z_]+"),
            ("attempt_id", 96, r"[A-Za-z0-9._:-]+"),
            ("fence", 96, r"[0-9]+"),
        ):
            item = observed.get(key)
            if isinstance(item, str) and len(item) <= limit and re.fullmatch(pattern, item):
                safe_observed[key] = item
        if (safe := timestamp(observed.get("lease_expires_at"))) is not None:
            safe_observed["lease_expires_at"] = safe
        if safe_observed:
            result["observed"] = safe_observed
    return result


def _validation_failure(result: InvocationResult, *, capability_id: str = "vibecomfy.validate") -> AstridError:
    # Runtime's failed task carries the host diagnostic in result.error.
    # The SDK also embeds that object in its formatted error details: remove
    # this untrusted subtree before formatting, even when it is malformed.
    raw = result.raw_result
    task = raw.get("task") if isinstance(raw, Mapping) else None
    settled = task.get("result") if isinstance(task, Mapping) else None
    failure = settled.get("error") if isinstance(settled, Mapping) else None
    diagnostic = _bounded_lease_diagnostic(failure.get("diagnostic")) if isinstance(failure, Mapping) else None
    error = result.error
    has_diagnostic = isinstance(failure, Mapping) and "diagnostic" in failure
    if isinstance(error, Mapping):
        details = error.get("details")
        error_result = details.get("result") if isinstance(details, Mapping) else None
        inner = error_result.get("error") if isinstance(error_result, Mapping) else None
        has_diagnostic = has_diagnostic or (isinstance(inner, Mapping) and "diagnostic" in inner)
        if diagnostic is None and isinstance(inner, Mapping):
            diagnostic = _bounded_lease_diagnostic(inner.get("diagnostic"))
    if has_diagnostic:
        # A settled failure can contain arbitrary input and exception data in
        # other fields too. Keep its failure state without formatting that data.
        code = error.get("code") if isinstance(error, Mapping) else None
        if not isinstance(code, str) or code not in {"task_failed", "task_cancelled", "task_cancel_requested"}:
            code = "task_failed"
        error = {"code": code, "message": "validation task failed"}
    exc = AstridError(f"{capability_id} failed: {error}")
    if diagnostic is not None:
        exc.lease_diagnostic = diagnostic
    return exc


def _invoke(
    client: Any,
    capability_id: str,
    *,
    inputs: Mapping[str, Any],
    out: Path,
    project: str | None,
    execution_request: Mapping[str, Any] | None = None,
    idempotency_context: Mapping[str, Any] | None = None,
    recovery_path: Path | None = None,
    resume: bool = False,
) -> Any:
    result = client.invoke_result(
        capability_id,
        kind="executor",
        inputs=inputs,
        out=out,
        project=project,
        execution_request=execution_request,
        idempotency_context=idempotency_context,
        wait=True,
        recovery_path=recovery_path,
        resume=resume,
        read_managed_outputs=True,
    )
    if not result.ok:
        exc = _validation_failure(result, capability_id=capability_id)
        exc.h3_child_correlation = {
            key: _safe_correlation(getattr(result, attribute, None))
            for key, attribute in (("run_id", "kernel_run_id"), ("task_id", "kernel_task_id"),
                                   ("attempt_id", "kernel_attempt_id"))
        }
        raise exc
    return result


def _invoke_workflow(
    client: Any,
    *,
    placement: str,
    inputs: Mapping[str, Any],
    out: Path,
    project: str | None,
    execution_request: Mapping[str, Any] | None,
    idempotency_context: Mapping[str, Any] | None,
    recovery_path: Path,
    resume: bool = False,
) -> InvocationResult:
    if isinstance(client, ManagedParentClient):
        return _invoke(client, "vibecomfy.run", inputs=inputs, out=out, project=project)
    result = generate.workflow(
        client=client,
        placement=placement,
        **inputs,
        execution_request=execution_request,
        out=out,
        project=project,
        idempotency_context=idempotency_context,
        recovery_path=recovery_path,
        resume=resume,
        wait=True,
        read_managed_outputs=True,
        timeout_seconds=3600.0,
        poll_seconds=1.0,
    )
    if not result.ok:
        exc = _validation_failure(result, capability_id="vibecomfy.run")
        exc.h3_child_correlation = {
            key: _safe_correlation(getattr(result, attribute, None))
            for key, attribute in (("run_id", "kernel_run_id"), ("task_id", "kernel_task_id"),
                                   ("attempt_id", "kernel_attempt_id"))
        }
        raise exc
    return result


def _execution_request_for_child(
    execution_request: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Carry scheduling intent without inheriting the parent's input set.

    The parent H3 task owns ``request.json`` and ``h3-input-bundle.zip``.  The
    nested GPU task owns the sealed workflow and managed-assets objects.  By
    omitting ``inputs`` here, the SDK derives the child's canonical manifest
    from its actual file inputs and Runtime remains the sole input authority.
    """

    if execution_request is None:
        return None
    return {key: value for key, value in execution_request.items() if key != "inputs"}


def _qualified_execution_request(
    qualification: Mapping[str, Any],
    execution_request: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_target = (
        execution_request.get("target")
        if isinstance(execution_request, Mapping)
        else None
    )
    qualified_target = ensure_runpod_worker(
        qualification, expected_target=expected_target
    )
    target = {
        key: qualified_target[key]
        for key in (
            "kind",
            "pod_id",
            "provider_account_ref",
            "backup_volume_id",
            "network_volume_id",
            "volume_id",
            "storage",
        )
        if key in qualified_target
    }
    if execution_request is None:
        request = {
            "target": target,
            "lifecycle": {"mode": "leave_running"},
            "limits": {"max_queue_seconds": 1800, "max_runtime_seconds": 3600},
        }
    else:
        request = dict(execution_request)
        request["target"] = target
    return request, qualified_target


def _receipt_target(target: Mapping[str, Any]) -> dict[str, Any]:
    """Keep admitted placement and nested storage identity in task evidence."""

    return {
        key: target[key]
        for key in (
            "kind",
            "pod_id",
            "provider_account_ref",
            "backup_volume_id",
            "network_volume_id",
            "volume_id",
            "storage",
        )
        if key in target
    }


def _invocation_payload(result: InvocationResult) -> dict[str, Any]:
    to_dict = getattr(result, "to_dict", None)
    payload = (
        to_dict()
        if callable(to_dict)
        else {
            field: getattr(result, field, None)
            for field in (
                "capability_id",
                "capability_type",
                "native_kind",
                "ok",
                "error",
                "manifest_path",
                "raw_result",
                "run_id",
                "run_root",
                "outputs",
                "executor_version",
                "kernel_run_id",
                "kernel_task_id",
                "kernel_attempt_id",
            )
        }
    )
    if not isinstance(payload, Mapping):
        raise AstridError("canonical invocation result did not serialize to an object")
    return dict(payload)


def _save_invocation_result(path: Path, result: InvocationResult, *, input_digest: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    result_payload = _invocation_payload(result)
    envelope = {
        "schema_version": 1,
        "input_digest": input_digest,
        "result_digest": _stable_digest(result_payload),
        "result": result_payload,
    }
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(envelope, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _load_invocation_result(path: Path, *, expected_input_digest: str) -> InvocationResult:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AstridError(f"cannot load saved canonical run result: {exc}") from exc
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        raise AstridError("saved canonical run result envelope is unsupported")
    if value.get("input_digest") != expected_input_digest:
        raise AstridError("saved canonical run result belongs to different stage inputs")
    result_payload = value.get("result")
    if not isinstance(result_payload, Mapping):
        raise AstridError("saved canonical run result must contain an object")
    if value.get("result_digest") != _stable_digest(result_payload):
        raise AstridError("saved canonical run result failed its integrity check")
    value = result_payload
    required = ("capability_id", "capability_type", "native_kind", "ok")
    if any(key not in value for key in required):
        raise AstridError("saved canonical run result is incomplete")
    raw_result = value.get("raw_result")
    outputs = value.get("outputs")
    return InvocationResult(
        capability_id=str(value["capability_id"]),
        capability_type=str(value["capability_type"]),  # type: ignore[arg-type]
        native_kind=str(value["native_kind"]),
        ok=bool(value["ok"]),
        error=value.get("error") if isinstance(value.get("error"), Mapping) else None,
        manifest_path=value.get("manifest_path") if isinstance(value.get("manifest_path"), str) else None,
        raw_result=raw_result if isinstance(raw_result, Mapping) else {},
        run_id=value.get("run_id") if isinstance(value.get("run_id"), str) else None,
        run_root=value.get("run_root") if isinstance(value.get("run_root"), str) else None,
        outputs=outputs if isinstance(outputs, Mapping) else {},
        executor_version=value.get("executor_version") if isinstance(value.get("executor_version"), str) else None,
        kernel_run_id=value.get("kernel_run_id") if isinstance(value.get("kernel_run_id"), str) else None,
        kernel_task_id=value.get("kernel_task_id") if isinstance(value.get("kernel_task_id"), str) else None,
        kernel_attempt_id=value.get("kernel_attempt_id") if isinstance(value.get("kernel_attempt_id"), str) else None,
    )


def _reobserve_saved_result(
    client: Any,
    saved: InvocationResult,
    *,
    phase: str,
) -> InvocationResult:
    """Re-read one saved child identity from Runtime before resuming.

    The saved DTO is only a locator for the prior task.  Runtime remains the
    authority for terminal state, settlement bytes, and managed lineage; a
    missing or changed identity is explicit unknown and must not resample.
    """

    task_id = saved.kernel_task_id
    run_id = saved.kernel_run_id
    attempt_id = saved.kernel_attempt_id
    if not all(isinstance(value, str) and value for value in (task_id, run_id, attempt_id)):
        raise AstridError(
            f"cannot resume {phase}: saved result is missing task/run/attempt identity"
        )

    tasks = getattr(client, "tasks", None)
    show = getattr(tasks, "show", None)
    read_outputs = getattr(tasks, "list_managed_outputs", None)
    if not callable(show) or not callable(read_outputs):
        raise AstridError(
            f"cannot resume {phase}: Runtime identity and managed-output readback are unavailable"
        )

    observed = show(task_id)
    observed_ok = bool(getattr(observed, "ok", isinstance(observed, Mapping)))
    task = getattr(observed, "data", observed if isinstance(observed, Mapping) else None)
    if not observed_ok or not isinstance(task, Mapping):
        raise AstridError(
            f"cannot resume {phase}: Runtime settlement for task {task_id!r} is unknown"
        )
    task = dict(task)
    if task.get("task_id", task.get("id")) != task_id:
        raise AstridError(f"cannot resume {phase}: Runtime returned a different task identity")
    if task.get("run_id") != run_id or task.get("attempt_id") != attempt_id:
        raise AstridError(f"cannot resume {phase}: Runtime task identity disagrees with saved identity")
    state = str(task.get("state") or task.get("status") or "").lower()
    if state not in {"succeeded", "completed"}:
        raise AstridError(
            f"cannot resume {phase}: Runtime task {task_id!r} is not settled ({state or 'unknown'}); refusing replay"
        )
    settled = task.get("result")
    if not isinstance(settled, Mapping):
        raise AstridError(f"cannot resume {phase}: Runtime settlement has no result")

    managed_response = read_outputs(task_id)
    managed_ok = bool(getattr(managed_response, "ok", isinstance(managed_response, Mapping)))
    managed = getattr(
        managed_response,
        "data",
        managed_response if isinstance(managed_response, (list, tuple)) else None,
    )
    if not managed_ok:
        raise AstridError(
            f"cannot resume {phase}: Runtime managed-output readback for task {task_id!r} is unknown"
        )
    if isinstance(managed, tuple) and len(managed) == 2 and isinstance(managed[0], list):
        managed = managed[0]
    if not isinstance(managed, list):
        raise AstridError(f"cannot resume {phase}: Runtime managed-output readback is invalid")

    output_rows = settled.get("outputs")
    raw_result = {
        "ok": True,
        "state": "completed",
        "kernel_run_id": run_id,
        "kernel_task_id": task_id,
        "kernel_attempt_id": attempt_id,
        "task": task,
        "result": dict(settled),
        "outputs": {"artifacts": list(output_rows) if isinstance(output_rows, list) else []},
        "managed_outputs": managed,
    }
    return InvocationResult(
        capability_id=saved.capability_id,
        capability_type=saved.capability_type,
        native_kind=saved.native_kind,
        ok=True,
        error=None,
        manifest_path=saved.manifest_path,
        raw_result=raw_result,
        run_id=saved.run_id,
        run_root=saved.run_root,
        outputs={"artifacts": raw_result["outputs"]["artifacts"], "managed_outputs": managed},
        executor_version=saved.executor_version,
        kernel_run_id=run_id,
        kernel_task_id=task_id,
        kernel_attempt_id=attempt_id,
    )


def _assert_recorded_identity(
    result: InvocationResult,
    previous: Mapping[str, Any] | None,
    *,
    phase: str,
) -> None:
    if previous is None:
        return
    recorded_capability = previous.get("capability_id")
    if (
        isinstance(recorded_capability, str)
        and recorded_capability
        and recorded_capability != result.capability_id
    ):
        raise AstridError(f"cannot resume {phase}: capability identity disagrees with journal admission")
    for journal_key, result_value in (
        ("task_id", result.kernel_task_id),
        ("run_id", result.kernel_run_id),
        ("attempt_id", result.kernel_attempt_id),
    ):
        recorded = previous.get(journal_key)
        if isinstance(recorded, str) and recorded and recorded != result_value:
            raise AstridError(f"cannot resume {phase}: observed identity disagrees with journal admission")


def _recovery_receipt_path(saved_result: Path, journal: OperationJournal) -> Path:
    """Return the deterministic SDK receipt slot for this submission and stage."""

    submission_id = journal.submission_id
    if not isinstance(submission_id, str) or not submission_id:
        raise AstridError("H3 operation journal is missing its submission identity")
    return saved_result.with_name(f".{saved_result.name}.{submission_id}.recovery.json")


def _observe_legacy_saved_result(
    client: Any,
    saved: InvocationResult,
    *,
    phase: str,
    attempt_pin: str | None = None,
) -> InvocationResult:
    """Adapt a schema-1 result locator to the SDK's shared task observer.

    This is only for operations created before per-stage SDK receipts existed.
    Runtime remains the authority for task state and managed lineage.
    """

    task_id = saved.kernel_task_id
    run_id = saved.kernel_run_id
    saved_attempt_id = saved.kernel_attempt_id
    if not all(isinstance(value, str) and value for value in (task_id, run_id)):
        raise AstridError(
            f"cannot resume {phase}: saved result is missing task/run identity"
        )
    expected_attempt_id = attempt_pin or saved_attempt_id
    if attempt_pin and saved_attempt_id and attempt_pin != saved_attempt_id:
        raise AstridError(f"cannot resume {phase}: saved attempt disagrees with journal admission")
    observed = observe_task_invocation(
        client,
        capability_id=saved.capability_id,
        capability_type=saved.capability_type,
        native_kind=saved.native_kind,
        task_id=task_id,
        run_id=run_id,
        attempt_id=expected_attempt_id,
        wait=False,
        read_managed_outputs=True,
        executor_version=saved.executor_version,
        manifest_path=saved.manifest_path,
    )
    if not observed.ok:
        raise AstridError(
            f"cannot resume {phase}: Runtime settlement for task {task_id!r} is unknown or unsuccessful"
        )
    task = observed.raw_result.get("task") if isinstance(observed.raw_result, Mapping) else None
    if not isinstance(task, Mapping):
        raise AstridError(f"cannot resume {phase}: Runtime settlement has no task resource")
    if task.get("task_id", task.get("id")) != task_id or task.get("run_id") != run_id:
        raise AstridError(f"cannot resume {phase}: Runtime task identity disagrees with saved identity")
    observed_attempt_id = str(task.get("attempt_id") or "")
    if expected_attempt_id and observed_attempt_id != expected_attempt_id:
        raise AstridError(f"cannot resume {phase}: Runtime attempt identity disagrees with saved identity")
    state = str(task.get("state") or task.get("status") or "").lower()
    if state not in {"succeeded", "completed"}:
        raise AstridError(
            f"cannot resume {phase}: Runtime task {task_id!r} is not settled ({state or 'unknown'}); refusing replay"
        )
    if not isinstance(task.get("result"), Mapping):
        raise AstridError(f"cannot resume {phase}: Runtime settlement has no result")
    return observed


def _invoke_stage_impl(
    client: Any,
    capability_id: str,
    *,
    inputs: Mapping[str, Any],
    out: Path,
    project: str | None,
    saved_result: Path,
    resume: bool,
    journal: OperationJournal,
    phase: str,
    execution_request: Mapping[str, Any] | None = None,
    idempotency_context: Mapping[str, Any] | None = None,
) -> InvocationResult:
    """Invoke one canonical stage through the SDK's durable recovery receipt."""

    input_digest = _stable_digest({
        "capability_id": capability_id,
        "inputs": inputs,
        "project": project,
        "execution_request": execution_request,
    })
    previous = journal.latest(phase)
    receipt_path = _recovery_receipt_path(saved_result, journal)
    if resume and previous is not None and isinstance(client, ManagedParentClient):
        if previous.get("input_digest") != input_digest or previous.get("capability_id") != capability_id:
            raise AstridError(f"cannot resume {phase}: journaled child inputs changed")
        # The same stable child key is rebound by the authenticated host and
        # Runtime within this exact parent attempt, including unknown replies.
        result = _invoke(client, capability_id, inputs=inputs, out=out, project=project)
        _assert_recorded_identity(result, previous, phase=phase)
        _save_invocation_result(saved_result, result, input_digest=input_digest)
        journal.record(phase, "reused", capability_id=capability_id, input_digest=input_digest,
                       task_id=result.kernel_task_id, run_id=result.kernel_run_id,
                       attempt_id=result.kernel_attempt_id)
        return result
    if resume and receipt_path.is_file():
        if previous is not None:
            recorded_digest = previous.get("input_digest")
            if isinstance(recorded_digest, str) and recorded_digest != input_digest:
                raise AstridError(f"cannot resume {phase}: different stage inputs from the journal")
            recorded_capability = previous.get("capability_id")
            if isinstance(recorded_capability, str) and recorded_capability != capability_id:
                raise AstridError(f"cannot resume {phase}: journaled capability identity changed")
        result = _invoke(
            client,
            capability_id,
            inputs=inputs,
            out=out,
            project=project,
            execution_request=execution_request,
            idempotency_context=idempotency_context,
            recovery_path=receipt_path,
            resume=True,
        )
        _assert_recorded_identity(result, previous, phase=phase)
        journal.record(
            phase,
            "reused",
            capability_id=capability_id,
            input_digest=input_digest,
            task_id=result.kernel_task_id,
            run_id=result.kernel_run_id,
            attempt_id=result.kernel_attempt_id,
        )
        return result
    if resume and saved_result.is_file():
        if previous is None:
            raise AstridError(
                f"cannot resume {phase}: saved result has no matching journal admission"
            )
        recorded_digest = previous.get("input_digest")
        if isinstance(recorded_digest, str) and recorded_digest != input_digest:
            raise AstridError(f"cannot resume {phase}: different stage inputs from the journal")
        result = _load_invocation_result(saved_result, expected_input_digest=input_digest)
        if not result.ok:
            raise AstridError(f"saved {phase} result is not successful")
        if result.capability_id != capability_id:
            raise AstridError(f"saved {phase} result belongs to {result.capability_id!r}")
        result = _observe_legacy_saved_result(
            client,
            result,
            phase=phase,
            attempt_pin=(
                previous.get("attempt_id")
                if isinstance(previous.get("attempt_id"), str)
                else None
            ),
        )
        _assert_recorded_identity(result, previous, phase=phase)
        journal.record(
            phase,
            "reused",
            capability_id=capability_id,
            input_digest=input_digest,
            task_id=result.kernel_task_id,
            run_id=result.kernel_run_id,
            attempt_id=result.kernel_attempt_id,
        )
        return result
    if resume and previous is not None and previous.get("status") in {"started", "uncertain", "completed"}:
        raise AstridError(
            f"cannot resume {phase}: SDK recovery receipt and legacy result are missing after {previous.get('status')}"
        )
    journal.record(phase, "started", capability_id=capability_id, input_digest=input_digest)
    try:
        result = _invoke(
            client,
            capability_id,
            inputs=inputs,
            out=out,
            project=project,
            execution_request=execution_request,
            idempotency_context=idempotency_context,
            recovery_path=receipt_path,
        )
    except Exception as exc:
        try:
            journal.record(
                phase, "uncertain", capability_id=capability_id,
                input_digest=input_digest, error=type(exc).__name__, message=str(exc)[:1000],
            )
            exc.h3_journal_status = "succeeded"
        except Exception:
            exc.h3_journal_status = "failed"
        raise
    _save_invocation_result(saved_result, result, input_digest=input_digest)
    journal.record(
        phase,
        "completed",
        capability_id=capability_id,
        input_digest=input_digest,
        task_id=result.kernel_task_id,
        run_id=result.kernel_run_id,
        attempt_id=result.kernel_attempt_id,
    )
    return result


_H3_STAGES = frozenset({"prepare", "compile", "validate", "compose", "verify",
                        "cumulative-compose", "cumulative-verify", "finalizer", "run"})


def _invoke_stage(*args: Any, **kwargs: Any) -> InvocationResult:
    # Also cover errors in the resume path and failures before invocation.
    try:
        return _invoke_stage_impl(*args, **kwargs)
    except Exception as exc:
        phase = kwargs.get("phase")
        exc.h3_stage = phase if phase in _H3_STAGES else "unavailable"
        raise


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _invoke_canonical_run_impl(
    client: Any,
    *,
    inputs: Mapping[str, Any],
    execution_request: Mapping[str, Any] | None,
    out: Path,
    project: str | None,
    saved_result: Path,
    journal: OperationJournal,
    resume: bool,
    idempotency_context: Mapping[str, Any] | None = None,
    placement: str | None = None,
) -> InvocationResult:
    """Admit once; an unsettled or identity-free response cannot be replayed."""

    from astrid.core.generation.backends.registry import (
        GenerationBackendRegistry, resolve_workflow_descriptor,
    )

    # An unplaced call carrying a RunPod target is the legacy prepared route.
    if placement is None:
        target = execution_request.get("target") if isinstance(execution_request, Mapping) else None
        if isinstance(target, Mapping) and target.get("kind") == "runpod":
            placement = "runpod"
    # Resolve only inert placement metadata before touching admission or the
    # prepared client. The facade validates this same original target again.
    descriptor = resolve_workflow_descriptor(
        GenerationBackendRegistry(), placement=placement,
        execution_request=execution_request,
    )
    workflow_placement = descriptor.placement.resolve(placement)
    workflow_route = f"generation.workflow.v1/{workflow_placement}"

    admission_payload = {
        "capability_id": "vibecomfy.run",
        "inputs": inputs,
        "execution_request": execution_request,
    }
    admission_digest = _stable_digest(admission_payload)
    run_input_digest = _stable_digest({
        **admission_payload,
        "project": project,
    })
    prior_run = journal.latest("run")
    # The creative/admission digest stays unchanged. Route compatibility is
    # evidence in the existing operation journal, never a second recovery log.
    if resume and prior_run is not None and prior_run.get("workflow_route") != workflow_route:
        raise AstridError("cannot resume canonical run: incompatible workflow route; refusing replay")
    if resume and prior_run is not None and journal.admission_digest is None:
        raise AstridError("cannot resume canonical run: journal has no frozen admission identity")
    try:
        journal.freeze_admission(admission_digest)
    except OperationJournalError as exc:
        raise AstridError(str(exc)) from exc

    receipt_path = _recovery_receipt_path(saved_result, journal)
    if resume and prior_run is not None and isinstance(client, ManagedParentClient):
        if prior_run.get("admission_digest") != admission_digest:
            raise AstridError("cannot resume canonical run: journaled admission identity changed")
        result = _invoke_workflow(client, placement=workflow_placement, inputs=inputs, out=out,
            project=project, execution_request=execution_request, idempotency_context=idempotency_context,
            recovery_path=receipt_path, resume=True)
        _assert_recorded_identity(result, prior_run, phase="canonical run")
        _save_invocation_result(saved_result, result, input_digest=run_input_digest)
        journal.set_admission_phase("settled")
        journal.record("run", "reused", task_id=result.kernel_task_id, run_id=result.kernel_run_id,
                       attempt_id=result.kernel_attempt_id, admission_digest=admission_digest,
                       workflow_route=workflow_route)
        return result
    if resume and receipt_path.is_file():
        if prior_run is None:
            raise AstridError("cannot resume canonical run: SDK receipt has no matching journal route")
        recorded_admission = prior_run.get("admission_digest")
        if isinstance(recorded_admission, str) and recorded_admission != admission_digest:
            raise AstridError("cannot resume canonical run: journaled admission identity changed")
        result = _invoke_workflow(
            client,
            placement=workflow_placement,
            inputs=inputs,
            out=out,
            project=project,
            execution_request=execution_request,
            idempotency_context=idempotency_context,
            recovery_path=receipt_path,
            resume=True,
        )
        identity = (result.kernel_task_id, result.kernel_run_id, result.kernel_attempt_id)
        if not all(isinstance(value, str) and value for value in identity):
            raise AstridError("canonical run receipt returned incomplete task/run/attempt identity")
        _assert_recorded_identity(result, prior_run, phase="canonical run")
        journal.set_admission_phase("settled")
        journal.record(
            "run",
            "reused",
            task_id=identity[0],
            run_id=identity[1],
            attempt_id=identity[2],
            admission_digest=admission_digest, workflow_route=workflow_route,
        )
        return result

    if resume and saved_result.is_file():
        if prior_run is None:
            raise AstridError(
                "cannot resume canonical run: saved result has no matching journal admission"
            )
        result = _load_invocation_result(saved_result, expected_input_digest=run_input_digest)
        if not result.ok:
            raise AstridError("saved canonical run result is not successful")
        if result.capability_id != "vibecomfy.run":
            raise AstridError("saved canonical run result has the wrong capability identity")
        identity = (result.kernel_task_id, result.kernel_run_id, result.kernel_attempt_id)
        if not all(isinstance(value, str) and value for value in identity):
            raise AstridError("saved canonical run result is missing task/run/attempt identity")
        recorded_admission = prior_run.get("admission_digest")
        if isinstance(recorded_admission, str) and recorded_admission != admission_digest:
            raise AstridError("cannot resume canonical run: journaled admission identity changed")
        result = _observe_legacy_saved_result(
            client,
            result,
            phase="canonical run",
            attempt_pin=(
                prior_run.get("attempt_id")
                if isinstance(prior_run.get("attempt_id"), str)
                else None
            ),
        )
        _assert_recorded_identity(result, prior_run, phase="canonical run")
        journal.set_admission_phase("settled")
        journal.record(
            "run", "reused", task_id=identity[0], run_id=identity[1],
            attempt_id=identity[2], admission_digest=admission_digest, workflow_route=workflow_route,
        )
        return result

    if resume and journal.admission_phase in {"admission_started", "uncertain", "settled"}:
        raise AstridError(
            "cannot resume canonical run: admission may have occurred but its settled result is unavailable"
        )
    if resume and prior_run is not None:
        raise AstridError("cannot resume canonical run: journal phase and saved result disagree")

    journal.set_admission_phase("admission_started")
    journal.record(
        "run", "admission_started", admission_digest=admission_digest,
        workflow_route=workflow_route,
    )
    try:
        result = _invoke_workflow(
            client, placement=workflow_placement, inputs=inputs, out=out, project=project,
            execution_request=execution_request,
            idempotency_context=idempotency_context,
            recovery_path=receipt_path,
        )
        identity = (result.kernel_task_id, result.kernel_run_id, result.kernel_attempt_id)
        if not all(isinstance(value, str) and value for value in identity):
            raise AstridError("canonical run result is missing task/run/attempt identity")
        _save_invocation_result(saved_result, result, input_digest=run_input_digest)
    except Exception as exc:
        try:
            journal.set_admission_phase("uncertain")
            journal.record(
                "run", "uncertain", admission_digest=admission_digest, workflow_route=workflow_route,
                error=type(exc).__name__, message=str(exc)[:1000],
            )
            exc.h3_journal_status = "succeeded"
        except Exception:
            exc.h3_journal_status = "failed"
        raise
    journal.set_admission_phase("settled")
    journal.record(
        "run", "completed", task_id=identity[0], run_id=identity[1],
        attempt_id=identity[2], admission_digest=admission_digest, workflow_route=workflow_route,
    )
    return result


def _invoke_canonical_run(*args: Any, **kwargs: Any) -> InvocationResult:
    try:
        return _invoke_canonical_run_impl(*args, **kwargs)
    except Exception as exc:
        exc.h3_stage = "run"
        raise


def _generation_intent(compilation: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the child publication declaration from the sealed compilation."""

    capabilities = compilation.get("capabilities")
    public = capabilities.get("public_generation") if isinstance(capabilities, Mapping) else None
    if not isinstance(public, Mapping):
        raise AstridError("sealed H3 compilation is missing public_generation")
    selectors = public.get("selectors")
    modality = public.get("modality", "video")
    if not isinstance(selectors, list) or not selectors:
        raise AstridError("sealed H3 compilation declares no public generation selectors")
    if not all(isinstance(selector, Mapping) for selector in selectors):
        raise AstridError("sealed H3 generation selectors must be objects")
    return {
        "version": 1,
        "modality": modality,
        "partial_success_policy": "reject",
        "groups": [{"group_key": "main", "selectors": [dict(selector) for selector in selectors]}],
        "metadata": {"compiled_generation_contract": True},
    }


def _finalizer_generation_intent(
    *,
    generation_intent: Mapping[str, Any],
    request_digest: str,
    verification: Mapping[str, Any],
    raw_managed_publication: Any,
) -> dict[str, Any]:
    """Declare one Runtime-owned final publication with sealed raw lineage."""

    raw_evidence = getattr(raw_managed_publication, "evidence", None)
    raw_publication = raw_evidence.get("publication") if isinstance(raw_evidence, Mapping) else None
    if not isinstance(raw_publication, Mapping):
        raise AstridError("cannot admit finalizer without verified raw Runtime lineage")
    candidate_sha256 = verification.get("candidate_sha256")
    if not isinstance(candidate_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", candidate_sha256):
        raise AstridError("cannot admit finalizer without a verified candidate digest")
    if verification.get("request_digest") != request_digest:
        raise AstridError("cannot admit finalizer with verification for a different H3 request")
    raw_object_id = _digest(raw_publication)
    if raw_object_id is None or raw_object_id.removeprefix("sha256:") == candidate_sha256:
        raise AstridError("finalizer requires distinct raw and composed hashes")
    required = {
        "generation_id": raw_publication.get("generation_id"),
        "variant_id": raw_publication.get("variant_id"),
        "association_id": raw_publication.get("association_id"),
        "group_key": raw_publication.get("group_key"),
        "variant_key": raw_publication.get("variant_key"),
        "output_port": raw_publication.get("output_port"),
    }
    if not all(isinstance(value, str) and value for value in required.values()):
        raise AstridError("raw Runtime lineage lacks finalizer identity")
    return {
        "version": 1,
        "modality": generation_intent.get("modality", "video"),
        "partial_success_policy": "reject",
        "groups": [{
            "group_key": "main",
            "selectors": [{
                "selector": "final-composition",
                "ordinal": 0,
                "variant_key": "final-composition",
                "required": True,
            }],
        }],
        "metadata": {
            "compiled_generation_contract": True,
            "h3_av": {
                "request_digest": request_digest,
                "publication_scope": "final_composition",
                "candidate_sha256": candidate_sha256,
                "raw_sha256": raw_object_id.removeprefix("sha256:"),
                "raw_generation_id": required["generation_id"],
                "raw_variant_id": required["variant_id"],
                "raw_association_id": required["association_id"],
                "raw_group_key": required["group_key"],
                "raw_variant_key": required["variant_key"],
                "raw_output_port": required["output_port"],
            },
        },
    }


def _prepare_transform_input(
    client: Any, *, args: argparse.Namespace, root: Path, request: Any,
    raw_request: Mapping[str, Any], asset_map: Mapping[str, str],
    journal: OperationJournal, idempotency_context: Mapping[str, Any],
) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    managed_client = getattr(args, "_managed_client", None)
    staging = root / "staged-inputs"
    staging.mkdir(parents=True, exist_ok=True)
    if managed_client is not None:
        bundle_path = args._managed_bundle
        request_descriptor = args._managed_roots["request"]
        bundle_descriptor = args._managed_roots["input_bundle"]
    else:
        bundle_path = build_input_bundle(
            request, asset_map, staging / "h3-inputs.zip"
        )
        request_path = staging / "request.json"
        request_path.write_text(json.dumps(raw_request, sort_keys=True), encoding="utf-8")
        request_descriptor = _import_runtime_file(
            client, project=args.project, path=request_path, filename=request_path.name
        )
        bundle_descriptor = _import_runtime_file(
            client, project=args.project, path=bundle_path, filename=bundle_path.name
        )
    # Freeze the authoritative source from the same verified bytes as the
    # child inputs. Never open a prepare worker's absolute source path.
    frozen_assets, _ = materialize_input_bundle(request, bundle_path, staging / "assets")
    source_inputs = {}
    source_asset_id = _authoritative_source_asset_id(request)
    if source_asset_id is not None:
        source_path = Path(frozen_assets[source_asset_id])
        source_inputs["source"] = (args._managed_roots["source"] if managed_client is not None else
            _import_runtime_file(client, project=args.project, path=source_path, filename=source_path.name))
    prepared = _invoke_stage(
        client, "h3_av.prepare",
        inputs={"request": request_descriptor, "input_bundle": bundle_descriptor},
        out=root / "01-prepare", project=args.project,
        saved_result=root / "01-prepare" / "invocation-result.json",
        resume=bool(getattr(args, "resume", False)),
        journal=journal,
        phase="prepare",
        idempotency_context=idempotency_context,
    )
    preparation_path, preparation_row = _materialize_output(client, prepared, "preparation", root / "01-prepare", managed_only=True)
    preparation = _json_mapping(preparation_path)
    return {name: value for name, value in (
        ("bundle_descriptor", bundle_descriptor),
        ("source_inputs", source_inputs),
        ("preparation_path", preparation_path),
        ("preparation_row", preparation_row),
        ("preparation", preparation),
        ("frozen_assets", frozen_assets),
    )}


def _execute_verified_pass(
    client: Any, *, args: argparse.Namespace, root: Path, request: Any,
    raw_request: Mapping[str, Any], asset_map: Mapping[str, str],
    journal: OperationJournal, idempotency_context: Mapping[str, Any],
    local_engine: Any, prepared_selection: Any,
    execution_request: Mapping[str, Any] | None, placement: str,
    prepared_input: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The existing canonical stages; return only after composed-byte verification."""
    managed_client = getattr(args, "_managed_client", None)
    if prepared_input is None:
        prepared_input = _prepare_transform_input(
            client, args=args, root=root, request=request, raw_request=raw_request,
            asset_map=asset_map, journal=journal, idempotency_context=idempotency_context,
        )
    bundle_descriptor, source_inputs, preparation_path, preparation_row, preparation = (
        prepared_input[name] for name in
        ("bundle_descriptor", "source_inputs", "preparation_path", "preparation_row", "preparation")
    )
    compiled = _invoke_stage(
        client, "h3_av.compile", inputs={
            "preparation": _descriptor(preparation_row, filename="preparation.json"),
            "input_bundle": bundle_descriptor,
        },
        out=root / "02-compile", project=args.project,
        saved_result=root / "02-compile" / "invocation-result.json",
        resume=bool(getattr(args, "resume", False)),
        journal=journal,
        phase="compile",
        idempotency_context=idempotency_context,
    )
    compilation_path, compilation_row = _materialize_output(client, compiled, "compilation", root / "02-compile", managed_only=True)
    managed_assets_path, managed_assets_row = _materialize_output(client, compiled, "managed_assets", root / "02-compile", managed_only=True)
    compilation = _json_mapping(compilation_path)
    if managed_client is not None:
        managed_client.verify_compilation(compilation)
    expected_assets_digest = compilation.get("managed_assets", {}).get("sha256")
    actual_assets_digest = hashlib.sha256(managed_assets_path.read_bytes()).hexdigest()
    if expected_assets_digest != actual_assets_digest:
        raise AstridError("managed H3 assets failed compilation hash/provenance verification")
    provenance = _provenance(preparation, compilation)
    provenance_path = _write_provenance_preparation(root, preparation, provenance)
    materialized_workflow: dict[str, Path] = {}
    bundle_inputs = _retrieve_compiled_workflow(
        client,
        compiled,
        compilation,
        root / "02-compile",
        materialized_paths=materialized_workflow,
    )
    endpoint = (managed_client.plan.get("controls", {}).get("h3_endpoint")
                if managed_client is not None else None)
    if endpoint is not None:
        expected = endpoint["preflight"]["graph"]
        # The CPU parent has no engine network authority. Bind the admission
        # readiness to the SHA-verified staged graph and exact bindings here;
        # the local run executor repeats live readiness before any prompt.
        if ({name: row["sha256"] for name, row in compilation["workflow"].items()} != expected["members"]
                or _stable_digest(compilation["workflow_inputs"]) != expected["workflow_inputs_sha256"]):
            raise AstridError("H3 endpoint graph differs from pre-acquisition compilation")
        provenance["endpoint"] = {**endpoint["target"], "admission_readiness": expected}
        provenance_path = _write_provenance_preparation(root, preparation, provenance)
    # Canonical workflow Python is executable input.  Carry the explicit
    # consent scalar required by VibeComfy's audited validation gate;
    # execution-request targeting is not itself Python consent.
    _invoke_stage(
        client,
        "vibecomfy.validate",
        inputs={
            **bundle_inputs,
            "python_execution_consent": "confirmed",
            **({} if endpoint is not None or (managed_client is not None and
                managed_client.plan.get("controls", {}).get("target_policy") is not None) else {"workflow_inputs": json.dumps(
                compilation["workflow_inputs"],
                sort_keys=True,
                separators=(",", ":"),
            )}),
        },
        out=root / "03-validate",
        project=args.project,
        saved_result=root / "03-validate" / "invocation-result.json",
        resume=bool(getattr(args, "resume", False)),
        journal=journal,
        phase="validate",
        idempotency_context=idempotency_context,
    )
    generation_metadata = {"h3_av": provenance}
    generation_intent = _generation_intent(compilation)
    generation_intent["metadata"].update(generation_metadata)
    run_inputs = {
        **bundle_inputs,
        "managed_assets": _descriptor(managed_assets_row, filename="managed-assets.zip"),
        "workflow_inputs": json.dumps(compilation["workflow_inputs"], sort_keys=True, separators=(",", ":")),
        "generation_intent": generation_intent,
    }
    if local_engine is not None:
        run_inputs["comfy_url"] = local_engine.endpoint.url
        run_inputs["comfy_output_dir"] = local_engine.comfy_output_dir
    if endpoint is not None:
        # The offline validator retains its structural/security gate. Concrete
        # model checks above and the run adapter's fresh pre-prompt check use
        # the selected endpoint's choices, without a local weight-root demand.
        run_inputs["comfy_url"] = endpoint["target"]["comfy_url"]
    saved_run_path = root / "04-run" / "run-result.json"
    child_execution_request = _execution_request_for_child(execution_request)
    run_client = client
    if prepared_selection is not None:
        prepared_workflow_path = _materialize_prepared_worker_workflow_bundle(
            materialized_workflow,
            compilation,
            root / "04-run" / "prepared-workflow-bundle",
        )
        run_client = prepared_selection.bind_client(
            client,
            workflow_path=prepared_workflow_path,
            workflow_bundle_path=prepared_workflow_path.parent,
            workflow_inputs=compilation["workflow_inputs"],
        )
    run = _invoke_canonical_run(
        run_client,
        inputs=run_inputs,
        execution_request=child_execution_request,
        out=root / "04-run",
        project=args.project,
        saved_result=saved_run_path,
        journal=journal,
        resume=bool(getattr(args, "resume", False)),
        idempotency_context=idempotency_context,
        placement=placement,
    )
    output_contract = compilation.get("capabilities", {}).get("output_contract")
    generated_audio_path: Path | None = None
    generated_bundle_path: Path | None = None
    if output_contract == "muxed_av_full_timeline":
        generated_path, muxed_row = _retrieve_muxed_generation(
            client, run, root / "04-run"
        )
        generated_outputs: dict[str, tuple[Path, Mapping[str, Any]]] = {
            "muxed_av": (generated_path, muxed_row)
        }
    elif output_contract == "separate_av_full_timeline":
        generated_outputs = _retrieve_generation_outputs(client, run, root / "04-run")
        generated_path = generated_outputs["video"][0]
        generated_audio_path = generated_outputs["audio"][0]
        if managed_client is None:
            generated_bundle_path = _write_generated_bundle(
                generated_outputs,
                root / "04-run" / "retrieved" / "lanpaint-generated-av.zip",
            )
    else:
        raise AstridError(f"unsupported H3 output contract: {output_contract!r}")
    journal.record("pullback", "completed")
    raw_managed_publication = attest_runtime_managed_publication(
        runtime_result=run,
        request_digest=str(preparation["request_digest"]),
        generation_intent=generation_intent,
        retrieved_outputs=[row for _path, row in generated_outputs.values()],
    )
    runtime_provenance = {
        "run_id": getattr(run, "kernel_run_id", None),
        "task_id": getattr(run, "kernel_task_id", None),
        "attempt_id": getattr(run, "kernel_attempt_id", None),
        "generated_outputs": {
            role: row for role, (_path, row) in generated_outputs.items()
        },
        "output_contract": output_contract,
    }
    if generated_bundle_path is not None:
        runtime_provenance["generated_bundle"] = {
            "filename": generated_bundle_path.name,
            "sha256": hashlib.sha256(generated_bundle_path.read_bytes()).hexdigest(),
        }
    enriched = dict(preparation)
    enriched["provenance"] = {**provenance, "runtime": runtime_provenance}
    provenance_path.write_text(json.dumps(enriched, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    generated_input_path = generated_bundle_path or generated_path
    if managed_client is not None:
        preparation_descriptor = _descriptor(preparation_row, filename="preparation.json")
        raw_row = (muxed_row if output_contract == "muxed_av_full_timeline"
                   else generated_outputs["video"][1])
        generated_descriptor = _descriptor(raw_row, filename=generated_path.name)
    else:
        preparation_descriptor = _import_runtime_file(
            client, project=args.project, path=provenance_path, filename="provenance-preparation.json")
        generated_descriptor = _import_runtime_file(
            client, project=args.project, path=generated_input_path, filename=generated_input_path.name)
    composed = _invoke_stage(
        client, "h3_av.compose",
        inputs={
            "preparation": preparation_descriptor,
            "input_bundle": bundle_descriptor,
            "generated": generated_descriptor,
            **({"compilation": _descriptor(compilation_row, filename="compilation.json"),
                "generation_provenance": json.dumps({
                    "run_id": run.kernel_run_id, "task_id": run.kernel_task_id,
                    "attempt_id": run.kernel_attempt_id, "output_contract": output_contract,
                    "generated_outputs": {role: {key: row[key] for key in (
                        "association_id", "run_id", "task_id", "attempt_id", "object_id", "digest", "size",
                        "filename", "media_type", "output_port", "ordinal", "group_key", "variant_key", "producer")
                        if key in row} for role, (_path, row) in generated_outputs.items()},
                }, sort_keys=True, separators=(",", ":")),
                **({"generated_audio": _descriptor(generated_outputs["audio"][1], filename="generated-audio.media")}
                   if output_contract == "separate_av_full_timeline" else {})} if managed_client is not None else {}),
            **source_inputs,
        },
        out=root / "05-compose", project=args.project,
        saved_result=root / "05-compose" / "invocation-result.json",
        resume=bool(getattr(args, "resume", False)),
        journal=journal,
        phase="compose",
        idempotency_context=idempotency_context,
    )
    composition_path, composition_row = _materialize_output(client, composed, "composition", root / "05-compose", managed_only=True)
    candidate_path, candidate_row = _materialize_output(client, composed, "candidate", root / "05-compose", managed_only=True)
    if managed_client is not None:
        _, enriched_row = _materialize_output(client, composed, "preparation", root / "05-compose", managed_only=True)
        preparation_descriptor = _descriptor(enriched_row, filename="provenance-preparation.json")
    verified = _invoke_stage(
        client, "h3_av.verify",
        inputs={
            "preparation": preparation_descriptor,
            "input_bundle": bundle_descriptor,
            "composition": _descriptor(composition_row, filename="composition-manifest.json"),
            "candidate": _descriptor(candidate_row, filename="candidate.media"),
            **source_inputs,
        },
        out=root / "06-verify", project=args.project,
        saved_result=root / "06-verify" / "invocation-result.json",
        resume=bool(getattr(args, "resume", False)),
        journal=journal,
        phase="verify",
        idempotency_context=idempotency_context,
    )
    verification_path, verification_row = _materialize_output(client, verified, "verification", root / "06-verify", managed_only=True)
    verification = _json_mapping(verification_path)
    if (verification.get("status") != "verified"
            or verification.get("request_digest") != request.digest
            or "sha256:" + str(verification.get("candidate_sha256")) != candidate_row["digest"]):
        raise AstridError("verification does not certify the composed candidate digest")
    return {name: value for name, value in (
        ("preparation_path", preparation_path),
        ("preparation", preparation),
        ("compilation_path", compilation_path),
        ("compilation", compilation),
        ("generated_path", generated_path),
        ("generated_audio_path", generated_audio_path),
        ("composition_path", composition_path),
        ("composition_row", composition_row),
        ("candidate_path", candidate_path),
        ("candidate_row", candidate_row),
        ("verification_path", verification_path),
        ("verification_row", verification_row),
        ("verification", verification),
        ("run", run),
        ("generation_intent", generation_intent),
        ("raw_managed_publication", raw_managed_publication),
    )}


def _verify_cumulative_passes(
    client: Any, *, args: argparse.Namespace, root: Path, request: Any,
    original: Mapping[str, Any], state: dict[str, Any],
    journal: OperationJournal, idempotency_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Reuse the candidate contract against original-source permissions."""
    preparation = original["preparation"]
    provenance = {
        "schema_version": 1, "request_digest": request.digest,
        "schedule_digest": preparation["mask_schedule"]["digest"],
        "assets": preparation["assets"],
        "ordered_passes": str(root / "ordered-passes.json"),
    }
    preparation_path = _write_provenance_preparation(root, preparation, provenance)
    preparation_descriptor = _import_runtime_file(
        client, project=args.project, path=preparation_path, filename="provenance-preparation.json",
    )
    common_inputs = {
        "preparation": preparation_descriptor,
        "input_bundle": original["bundle_descriptor"], **original["source_inputs"],
    }
    composed = _invoke_stage(
        client, "h3_av.compose", inputs={
            **common_inputs,
            "generated": _descriptor(state["candidate_row"], filename="verified-pass.media"),
        }, out=root / "05-compose", project=args.project,
        saved_result=root / "05-compose" / "invocation-result.json",
        resume=bool(getattr(args, "resume", False)), journal=journal,
        phase="cumulative-compose", idempotency_context=idempotency_context,
    )
    composition_path, composition_row = _materialize_output(
        client, composed, "composition", root / "05-compose", managed_only=True,
    )
    candidate_path, candidate_row = _materialize_output(
        client, composed, "candidate", root / "05-compose", managed_only=True,
    )
    verified = _invoke_stage(
        client, "h3_av.verify", inputs={
            **common_inputs,
            "composition": _descriptor(composition_row, filename="composition-manifest.json"),
            "candidate": _descriptor(candidate_row, filename="candidate.media"),
        }, out=root / "06-verify", project=args.project,
        saved_result=root / "06-verify" / "invocation-result.json",
        resume=bool(getattr(args, "resume", False)), journal=journal,
        phase="cumulative-verify", idempotency_context=idempotency_context,
    )
    verification_path, verification_row = _materialize_output(
        client, verified, "verification", root / "06-verify", managed_only=True,
    )
    verification = _json_mapping(verification_path)
    if (verification.get("status") != "verified"
            or verification.get("request_digest") != request.digest
            or "sha256:" + str(verification.get("candidate_sha256")) != candidate_row["digest"]):
        raise AstridError("cumulative verification does not certify the composed candidate digest")
    return {**state, "preparation_path": original["preparation_path"],
            "preparation": preparation, "composition_path": composition_path,
            "composition_row": composition_row, "candidate_path": candidate_path,
            "candidate_row": candidate_row, "verification_path": verification_path,
            "verification_row": verification_row, "verification": verification}


def _run_transform_unlocked(
    args: argparse.Namespace, *, terminal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    terminal = terminal if terminal is not None else {}
    terminal["execution_not_started"] = True
    root = args.out.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    managed_client = getattr(args, "_managed_client", None)
    execution_request = (getattr(args, "_admitted_execution_request", None) if managed_client is not None
                         else _json_mapping(args.execution_request) if args.execution_request else None)
    requested_target = execution_request.get("target") if execution_request else None
    if isinstance(requested_target, Mapping) and requested_target.get("kind") not in (None, "default", "runpod"):
        raise AstridError(UNSUPPORTED_TARGET_MESSAGE)
    has_runpod_worker = (
        isinstance(requested_target, Mapping)
        and requested_target.get("kind") == "runpod"
        and isinstance(requested_target.get("pod_id"), str)
        and bool(requested_target["pod_id"].strip())
    )
    legacy_prepared = bool(
        has_runpod_worker
        or args.worker_qualification is not None
        or args.prepared_worker is not None
    )
    placement = getattr(args, "placement", None) or ("runpod" if legacy_prepared else "local")
    if placement == "local" and (legacy_prepared or (
            isinstance(requested_target, Mapping) and requested_target.get("kind") == "runpod")):
        raise AstridError(
            "a RunPod worker selection or target requires --placement runpod",
            valid_options=("--placement runpod",),
        )
    if args.dry_run:
        from astrid.packs.h3_av.shared.planner import plan_request

        plan = plan_request(
            normalize_request(load_request_document(args.request)),
            asset_map=_json_mapping(args.asset_map), out_dir=root,
        )
        return {
            **plan,
            "placement": placement,
            "request": str(args.request.resolve()),
            "asset_map": str(args.asset_map.resolve()),
            "stages": ["h3_av.prepare", "h3_av.compile", "vibecomfy.validate", "vibecomfy.run", "h3_av.compose", "h3_av.verify", "h3_av.publication_finalizer"],
        }
    local_engine = None
    # The remote placement command owns execution on its local run root.
    terminal["enabled"] = placement == "local" or legacy_prepared
    if placement == "local":
        if managed_client is None:
            local_engine = _resolve_local_engine()
    elif not legacy_prepared:
        return _run_remote_placement(args)
    prepared_selection = None
    qualification: dict[str, Any] | None = None
    if args.worker_qualification is not None:
        try:
            qualification = load_worker_qualification(args.worker_qualification)
            execution_request, qualified_target = _qualified_execution_request(
                qualification, execution_request
            )
        except WorkerQualificationError as exc:
            raise AstridError(
                f"worker qualification failed before admission: {exc}"
            ) from exc
        root.mkdir(parents=True, exist_ok=True)
        (root / "00-worker-qualification.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "accepted",
                    "qualification_digest": qualification_digest(qualification),
                    "target": qualified_target,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    elif args.require_worker_qualification:
        if args.prepared_worker is None:
            raise AstridError(
                "worker qualification is required before targeted H3 admission"
            )
    if args.prepared_worker is not None:
        from astrid.packs.runpod.prepared_task import load_prepared_worker_selection

        prepared_selection = load_prepared_worker_selection(args.prepared_worker)
        execution_request = prepared_selection.execution_request(execution_request)
    admitted_qualification = getattr(args, "_admitted_qualification_digest", None)
    if managed_client is not None and admitted_qualification is not None:
        (root / "00-worker-qualification.json").write_text(json.dumps({"schema_version": 1,
            "status": "accepted", "qualification_digest": admitted_qualification,
            "target": requested_target}, sort_keys=True) + "\n", encoding="utf-8")
    cleanup_receipt = (getattr(args, "_admitted_cleanup_receipt", None) if managed_client is not None
                       else _json_mapping(args.cleanup_receipt) if args.cleanup_receipt else None)
    # The parent orchestrator is launched locally, while its child executor
    # tasks target the already-supervised Runtime/GenericPackHost. Starting a
    # second local pack host here would compete with that existing authority.
    connection = _client_with_failure_evidence(args, root, terminal)
    with connection as client:
        raw_request = load_request_document(args.request)
        request = normalize_request(raw_request)
        terminal["request_digest"] = request.digest
        try:
            journal = OperationJournal(
                root / "operation-state.json", request_digest=request.digest
            )
        except OperationJournalError as exc:
            terminal["enabled"] = False
            raise AstridError(f"cannot open H3 operation journal: {exc}") from exc
        if journal.has_history and not getattr(args, "resume", False):
            terminal["enabled"] = False
            raise AstridError("operation journal already has history; use --resume to continue it")
        journal.record("operation", "started", resume=bool(getattr(args, "resume", False)))
        terminal["journal"] = journal
        idempotency_context = {"h3_submission_id": journal.submission_id}
        from astrid.packs.h3_av.shared.spec import canonical_spec, sampling_request_document

        asset_map = {} if managed_client is not None else _json_mapping(args.asset_map)
        original = _prepare_transform_input(
            client, args=args, root=root, request=request, raw_request=raw_request,
            asset_map=asset_map, journal=journal, idempotency_context=idempotency_context,
        )
        adapter = request.value.get("settings", {}).get("adapter", {})
        if isinstance(adapter, Mapping) and adapter.get("id") == CHARACTER_SWAP_ID:
            # The existing adapter compiler supports reference semantics outside
            # SPEC-MIN. Keep that single compilation route and its validation.
            sampling_passes = (None,)
        else:
            sampling_passes = canonical_spec(request).sampling_passes()
        if not sampling_passes:
            raise AstridError("H3 execution requires a modifying operation")
        if len(sampling_passes) == 1:
            state = _execute_verified_pass(
                client, args=args, root=root, request=request, raw_request=raw_request,
                asset_map=asset_map, journal=journal, idempotency_context=idempotency_context,
                local_engine=local_engine, prepared_selection=prepared_selection,
                execution_request=execution_request, placement=placement,
                prepared_input=original,
            )
        else:
            current_assets = dict(original["frozen_assets"])
            pass_rows: list[dict[str, Any]] = []
            ledger_path = root / "ordered-passes.json"
            for index, sampling_pass in enumerate(sampling_passes):
                pass_number = index + 1
                pass_root = root / "passes" / f"{pass_number:02d}"
                pass_document = sampling_request_document(
                    request, index, previous_verified_output=index > 0,
                )
                pass_request = normalize_request(pass_document)
                pass_journal = OperationJournal(
                    pass_root / "operation-state.json", request_digest=pass_request.digest,
                )
                source_asset = _authoritative_source_asset_id(pass_request)
                input_path = Path(current_assets[source_asset]) if source_asset else None
                row = {
                    "index": pass_number, "operation": asdict(sampling_pass.operation),
                    "request_digest": pass_request.digest, "reads": sampling_pass.reads,
                    "preserves": [asdict(op) for op in sampling_pass.preserves],
                    "input": None if input_path is None else {
                        "path": str(input_path), "sha256": sha256_file(input_path),
                    },
                    "journal": str(pass_journal.path), "status": "started",
                }
                pass_rows.append(row)
                write_json_atomic(ledger_path, {
                    "schema_version": 1, "request_digest": request.digest,
                    "pass_count": len(sampling_passes), "passes": pass_rows,
                })
                journal.record(f"pass-{pass_number}", "started", pass_record=row)
                try:
                    state = _execute_verified_pass(
                        client, args=args, root=pass_root, request=pass_request,
                        raw_request=pass_document, asset_map=current_assets,
                        journal=pass_journal,
                        idempotency_context={**idempotency_context, "h3_pass": pass_number},
                        local_engine=local_engine, prepared_selection=prepared_selection,
                        execution_request=execution_request, placement=placement,
                    )
                except Exception as exc:
                    row.update(status="failed", error=type(exc).__name__, message=str(exc)[:1000])
                    pass_journal.record("operation", "failed", error=type(exc).__name__, message=str(exc)[:1000])
                    journal.record(f"pass-{pass_number}", "failed", error=type(exc).__name__, message=str(exc)[:1000])
                    write_json_atomic(ledger_path, {
                        "schema_version": 1, "request_digest": request.digest,
                        "pass_count": len(sampling_passes), "passes": pass_rows,
                    })
                    raise
                # The stage helper has independently checked both verification
                # identity and the managed candidate digest before this handoff.
                candidate = state["candidate_path"]
                if "sha256:" + sha256_file(candidate) != state["candidate_row"]["digest"]:
                    raise AstridError("verified pass candidate changed before handoff")
                row.update(
                    status="verified", output={"path": str(candidate), "sha256": sha256_file(candidate)},
                    preparation=str(state["preparation_path"]),
                    composition=str(state["composition_path"]),
                    verification=str(state["verification_path"]),
                    verification_result=state["verification"],
                )
                pass_journal.record("operation", "verified", output=row["output"], verification=row["verification"])
                journal.record(f"pass-{pass_number}", "verified", output=row["output"], verification=row["verification"])
                write_json_atomic(ledger_path, {
                    "schema_version": 1, "request_digest": request.digest,
                    "pass_count": len(sampling_passes), "passes": pass_rows,
                })
                if source_asset is not None:
                    current_assets[source_asset] = str(candidate)
            # Compose and independently verify source→final preservation using
            # the original permissions and authoritative source bytes.
            state = _verify_cumulative_passes(
                client, args=args, root=root, request=request, original=original,
                state=state, journal=journal, idempotency_context=idempotency_context,
            )
            write_json_atomic(ledger_path, {
                "schema_version": 1, "request_digest": request.digest,
                "pass_count": len(sampling_passes), "passes": pass_rows,
                "cumulative": {"composition": str(state["composition_path"]),
                               "verification": str(state["verification_path"]),
                               "verification_result": state["verification"]},
            })
        preparation_path, preparation, compilation_path, compilation, generated_path, generated_audio_path, composition_path, composition_row, candidate_path, candidate_row, verification_path, verification_row, verification, run, generation_intent, raw_managed_publication = (
            state[name] for name in ('preparation_path', 'preparation', 'compilation_path', 'compilation', 'generated_path', 'generated_audio_path', 'composition_path', 'composition_row', 'candidate_path', 'candidate_row', 'verification_path', 'verification_row', 'verification', 'run', 'generation_intent', 'raw_managed_publication')
        )
        task_evidence = {
            key: value
            for key, value in {
                "run_id": getattr(run, "kernel_run_id", None),
                "task_id": getattr(run, "kernel_task_id", None),
                "attempt_id": getattr(run, "kernel_attempt_id", None),
                "capability_id": getattr(run, "capability_id", None),
            }.items()
            if value is not None
        }
        admitted_target = (
            execution_request.get("target")
            if isinstance(execution_request, Mapping)
            else None
        )
        if isinstance(admitted_target, Mapping):
            task_evidence["target"] = _receipt_target(admitted_target)
        final_managed_publication = None
        if getattr(raw_managed_publication, "status", None) == "passed":
            try:
                finalizer_intent = _finalizer_generation_intent(
                    generation_intent=generation_intent,
                    request_digest=str(preparation["request_digest"]),
                    verification=verification,
                    raw_managed_publication=raw_managed_publication,
                )
                finalizer = _invoke_stage(
                    client,
                    "h3_av.publication_finalizer",
                    inputs={
                        "candidate": _descriptor(candidate_row, filename="candidate.media"),
                        "verification": _descriptor(verification_row, filename="verification.json"),
                        "expected_request_digest": str(preparation["request_digest"]),
                        "generation_intent": finalizer_intent,
                    },
                    out=root / "07-finalizer",
                    project=args.project,
                    saved_result=root / "07-finalizer" / "invocation-result.json",
                    resume=bool(getattr(args, "resume", False)),
                    journal=journal,
                    phase="finalizer",
                    idempotency_context=idempotency_context,
                )
                final_path, final_row = _materialize_output(
                    client,
                    finalizer,
                    "verified_candidate",
                    root / "07-finalizer",
                    managed_only=True,
                )
                final_retrieved = {
                    **final_row,
                    "verified": True,
                    "size": final_path.stat().st_size,
                    "sha256": hashlib.sha256(final_path.read_bytes()).hexdigest(),
                }
                final_managed_publication = attest_runtime_managed_composition(
                    runtime_result=finalizer,
                    request_digest=str(preparation["request_digest"]),
                    candidate_verified={
                        "verification_path": str(verification_path),
                        "verification": verification,
                    },
                    retrieved_outputs=[final_retrieved],
                    raw_managed_publication=raw_managed_publication,
                )
            except (RuntimeError, OSError, ValueError) as exc:
                # Raw lineage and local verification remain useful, but an
                # unproven final association must never be upgraded to final.
                journal.record(
                    "finalizer",
                    "unproven",
                    capability_id="h3_av.publication_finalizer",
                    error=type(exc).__name__,
                    message=str(exc)[:1000],
                )
        else:
            journal.record(
                "finalizer", "skipped",
                capability_id="h3_av.publication_finalizer",
                reason="raw managed publication was not proven",
            )
        receipt = build_final_receipt(
            request_digest=str(preparation["request_digest"]),
            task_succeeded=task_evidence,
            candidate_verified={
                "verification_path": str(verification_path),
                "verification": verification,
            },
            editorially_approved=(
                {"source": "--editorial-approved"}
                if args.editorial_approved
                else None
            ),
            cleanup=cleanup_receipt,
            placement=placement,
            local_attestation=(
                asdict(local_engine.endpoint.attestation) if local_engine is not None else None
            ),
            raw_managed_publication=raw_managed_publication,
            final_managed_publication=final_managed_publication,
        )
        # A child generation receipt cannot certify the whole composition.
        # Revalidate the local bytes after the finalizer before describing them
        # as a verified result of this operation.
        candidate_digest = "sha256:" + sha256_file(candidate_path)
        candidate_size = candidate_path.stat().st_size
        if (candidate_digest != candidate_row["digest"]
                or (candidate_row.get("size") is not None and candidate_size != candidate_row["size"])):
            raise AstridError("verified candidate digest or size changed before result publication")
        if receipt["states"]["candidate_verified"]["status"] != "passed":
            raise AstridError("operation has no verified composition")
        final_receipt_path = write_final_receipt(root / "08-final-receipt.json", receipt)
        journal.record(
            "operation",
            receipt["overall_status"],
            final_receipt=str(final_receipt_path),
        )
        terminal["artifacts"] = [{
            "id": "candidate#0", "name": "candidate", "ordinal": 0,
            "digest": candidate_digest, "media_type": _video_media_type(candidate_path),
            "size": candidate_size, "path": str(candidate_path), "role": "result",
        }]
    return {
        "status": receipt["overall_status"],
        "preparation": str(preparation_path),
        "compilation": str(compilation_path),
        "generated": str(generated_path),
        "generated_audio": None if generated_audio_path is None else str(generated_audio_path),
        "composition": str(composition_path),
        "candidate": str(candidate_path),
        "verification": str(verification_path),
        "final_receipt": str(final_receipt_path),
        "final_receipt_status": receipt["overall_status"],
        "pass_count": len(sampling_passes),
        "ordered_passes": str(root / "ordered-passes.json") if len(sampling_passes) > 1 else None,
        "verified_candidate": str(final_path) if final_managed_publication is not None else None,
    }


# These are node classes used by the bundled H3 workflows. Unknown classes are
# deliberately omitted: a syntactically valid identifier can still be a secret.


@contextmanager
def _operation_directory_writer_lock(root: Path):
    """Allow one H3 writer per operation directory at a time."""

    lock_path = root / ".operation.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        try:
            platform.lock_file(descriptor, blocking=False)
        except BlockingIOError as exc:
            raise AstridError("H3 operation directory is already in use") from exc
        yield
    finally:
        os.close(descriptor)


def _safe_correlation(value: Any) -> str:
    if isinstance(value, str) and len(value) <= 96 and re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        return value
    return "unavailable"


_COMFY_NODE_CLASSES = frozenset({
    "BasicGuider", "BasicScheduler", "CLIPLoader", "ComfyMathExpression",
    "KSamplerSelect", "LoadAudio", "LoadImage", "LoraLoaderModelOnly",
    "MiniMaxH3AVExtensionController", "MiniMaxH3AVSourceAudioModeParam",
    "MiniMaxH3AVStartModeParam", "MiniMaxH3AudioVAECompatibility",
    "MiniMaxH3CropTo32", "MiniMaxH3CustomKeyframes", "MiniMaxH3FinalizeVHSOutput",
    "MiniMaxH3GeneratedAVMaskedContext", "MiniMaxH3LastActiveVHSPreviewBarrier",
    "MiniMaxH3ReferenceToVideo", "MiniMaxH3SigmaShift", "MiniMaxH3SourceAudioPolicy",
    "MiniMaxH3SourceAudioRegenLength", "MiniMaxH3SourceAudioRegenMask",
    "MiniMaxH3StartCanvasSelector", "MiniMaxH3StartMaskedContext",
    "MiniMaxH3StreamLiveExtensionAVToVHS", "MiniMaxH3Validate24FPSVideo",
    "ModelAttentionBackend", "PreviewAny", "PrimitiveFloat", "PrimitiveInt",
    "RandomNoise", "ResolutionSelector", "SamplerCustomAdvanced", "UNETLoader",
    "VAEDecode", "VAEDecodeAudio", "VAELoader", "VHS_LoadVideoFFmpeg", "VHS_VideoCombine",
})
_COMFY_EXCEPTION_TYPES = frozenset({
    "AssertionError", "IndexError", "KeyError", "OSError", "RuntimeError", "TypeError", "ValueError",
})


def _bounded_execution_diagnostic(error: Exception) -> dict[str, Any] | str:
    """Copy only recognized Comfy facts; never copy free-form exception text.

    A producer-provided source identity has no independent attestation on this
    route. A broken property or mapping must not affect execution or cleanup.
    """
    try:
        value = getattr(error, "comfy_diagnostic", None)
        if not isinstance(value, Mapping) or type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
            return "unavailable"

        def choice(key: str, allowed: frozenset[str]) -> str:
            item = value.get(key)
            return item if type(item) is str and item in allowed else "unavailable"

        node_id = value.get("node_id")
        if type(node_id) is not str or not re.fullmatch(r"[0-9]{1,32}", node_id):
            node_id = "unavailable"
        correlation = value.get("correlation")
        if type(correlation) is not str or not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", correlation
        ):
            correlation = "unavailable"

        def number(key: str) -> int | float | str:
            item = value.get(key)
            if type(item) not in (int, float) or abs(item) > 2**53 - 1:
                return "unavailable"
            return item if math.isfinite(item) else "unavailable"

        return {
            "schema_version": 1,
            "code": choice("code", frozenset({"comfy_execution_error"})),
            "stage": choice("stage", frozenset({"execution"})),
            "node_id": node_id,
            "node_class": choice("node_class", _COMFY_NODE_CLASSES),
            "exception_type": choice("exception_type", _COMFY_EXCEPTION_TYPES),
            "message": "unavailable",
            "expected": number("expected"),
            "observed": number("observed"),
            "correlation": correlation,
            "source_identity": "unavailable",
        }
    except Exception:
        return "unavailable"


def _failure_envelope(error: Exception, terminal: Mapping[str, Any]) -> dict[str, Any]:
    child = getattr(error, "h3_child_correlation", None)
    child = child if isinstance(child, Mapping) else {}
    correlation = {key: _safe_correlation(child.get(key)) for key in ("run_id", "task_id", "attempt_id")}
    diagnostic = _bounded_lease_diagnostic(getattr(error, "lease_diagnostic", None))
    stage = getattr(error, "h3_stage", "unavailable")
    stage = stage if stage in _H3_STAGES else "unavailable"
    return {
        "schema_version": 1,
        "category": ("child_failure" if any(v != "unavailable" for v in correlation.values()) or diagnostic
                     else "pre_execution_failure" if terminal.get("execution_not_started") else "unclassified"),
        "stage": stage,
        "observed_at_utc": terminal.get("failure_observed_at_utc") or datetime.now(timezone.utc).isoformat(),
        "child": correlation,
        # No trusted import attestation is exposed by this invocation route.
        "source": {"identity": "unavailable", "sha256": "unavailable"},
        "cause": {"category": diagnostic["category"] if diagnostic else "unclassified",
                  "lease_diagnostic": diagnostic if diagnostic else "unavailable",
                  "execution_diagnostic": _bounded_execution_diagnostic(error)},
        "persistence": {"before_cleanup": terminal.get("evidence_persistence", "unavailable"),
                        "journal": getattr(error, "h3_journal_status", "unavailable")},
        # Client exit and actual ephemeral-host cleanup are different operations.
        "cleanup": {"status": "unavailable",
                    "client_context_exit": terminal.get("cleanup_status", "unavailable")},
    }


@contextmanager
def _client_with_failure_evidence(args: argparse.Namespace, root: Path, terminal: dict[str, Any]):
    """Persist failure before the client context can clean up child evidence."""
    managed_client = getattr(args, "_managed_client", None)
    manager = (nullcontext(managed_client) if managed_client is not None else
               AstridClient.open_from_launcher(start_pack_host=False))
    client = manager.__enter__()
    terminal["execution_not_started"] = False
    try:
        yield client
    except Exception as exc:
        terminal["failure_observed_at_utc"] = datetime.now(timezone.utc).isoformat()
        terminal["cleanup_status"] = "pending"
        terminal["evidence_persistence"] = "succeeded"
        if terminal.get("enabled"):
            try:
                _write_transform_result(args, root, terminal, error=exc)
            except Exception:
                terminal["evidence_persistence"] = "failed"
        else:
            terminal["evidence_persistence"] = "unavailable"
        try:
            manager.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            terminal["cleanup_status"] = "failed"
        else:
            terminal["cleanup_status"] = "succeeded"
        raise
    else:
        manager.__exit__(None, None, None)


def _write_transform_result(
    args: argparse.Namespace,
    root: Path,
    terminal: Mapping[str, Any],
    *,
    result: Mapping[str, Any] | None = None,
    error: Exception | None = None,
) -> None:
    normalized = coerce_astrid_error(error) if error is not None else None
    managed_parent = isinstance(getattr(args, "_managed_client", None), ManagedParentClient)
    native_kind = "executor" if managed_parent else "orchestrator"
    options = {key: str(value) if isinstance(value, Path) else value
               for key, value in vars(args).items() if not key.startswith("_")}
    receipt = serialize_terminal_result(
        InvocationResult(
            capability_id="h3_av.transform", capability_type=native_kind, native_kind=native_kind,
            ok=error is None, run_root=str(root), raw_result=dict(result or {}),
            error=None if normalized is None else {
                "code": normalized.code or "executor_failed", "message": normalized.message,
                "field": normalized.field, "fix": normalized.fix,
            },
            outputs={"artifacts": terminal.get("artifacts", []) if error is None else []},
        ),
        status="succeeded" if error is None else "failed",
        # This diagnostic sidecar precedes host settlement. The host owns the
        # authenticated parent identity and final settlement; this route has
        # no parent identity readback. Environment markers are not attestation.
        task_id=None, attempt_id=None, parent_task_id=None, parent_attempt_id=None,
        request_digest=terminal.get("request_digest") or _stable_digest(options),
        resolved_options=options, cost=None, warnings=[],
        settled=False, settlement=None, effect=None,
    )
    diagnostic = _bounded_lease_diagnostic(getattr(error, "lease_diagnostic", None))
    if diagnostic is not None:
        receipt["lease_diagnostic"] = diagnostic
    if error is not None:
        receipt["failure_envelope"] = _failure_envelope(error, terminal)
    write_json_atomic(root / "result.json", receipt)


def run_transform(args: argparse.Namespace) -> dict[str, Any]:
    """Run one serialized H3 operation in its existing output directory."""

    if getattr(args, "dry_run", False):
        return _run_transform_unlocked(args)
    root = args.out.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with _operation_directory_writer_lock(root):
        terminal: dict[str, Any] = {}
        try:
            result = _run_transform_unlocked(args, terminal=terminal)
        except Exception as exc:
            journal = terminal.get("journal")
            if journal is not None:
                try:
                    journal.record("operation", "failed", error=type(exc).__name__, message=str(exc)[:1000])
                    if getattr(exc, "h3_journal_status", None) != "failed":
                        exc.h3_journal_status = "succeeded"
                except Exception:
                    exc.h3_journal_status = "failed"
            if terminal.get("enabled"):
                try:
                    _write_transform_result(args, root, terminal, error=exc)
                except Exception:
                    # Expose the persistence failure to the caller without replacing exc.
                    exc.h3_evidence_persistence = "failed"
            raise
        if terminal.get("enabled"):
            _write_transform_result(args, root, terminal, result=result)
        return result


def main(argv: list[str] | None = None) -> int:
    from astrid.core.execution._child_import_receipt import observe_action_entry

    observe_action_entry()
    # A task marker permits entry; the child bridge still owns admission and
    # publication authority for the managed parent route.
    if not environ.get(ASTRID_TASK_RUN_ID, "").strip():
        raise AstridError(
            "H3 transform requires a Runtime-admitted parent task "
            f"({ASTRID_TASK_RUN_ID}); invoke through the public Astrid action with a project",
            code="tracked_parent_required",
            field=ASTRID_TASK_RUN_ID,
        )
    result = run_transform(build_parser().parse_args(argv))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


STAGES = (
    ("prepare", "h3_av.prepare"), ("compile", "h3_av.compile"),
    ("validate", "vibecomfy.validate"), ("run", "vibecomfy.run"),
    ("compose", "h3_av.compose"), ("verify", "h3_av.verify"),
    ("finalizer", "h3_av.publication_finalizer"),
)


def _bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _parent_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _compile_plan(request, bundle: Path, destination: Path, *, endpoint_client=None) -> dict[str, Any]:
    # Reuse the installed H3 implementation, including all branch/eligibility
    # decisions. This is CPU preparation, never a provider/worker admission.
    from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
    from astrid.packs.h3_av.actions.compile.compile import compile_preparation
    from astrid.packs.h3_av.shared.input_bundle import bundle_digest, resolve_preparation_assets
    from astrid.packs.vibecomfy.invocation_preflight import _assert_no_unresolved_placeholders

    paths, identities = materialize_input_bundle(request, bundle, destination / "assets")
    preparation = prepare_request(request, asset_map=paths)
    preparation["assets"] = identities
    preparation["input_bundle_sha256"] = bundle_digest(bundle)
    preparation = resolve_preparation_assets(preparation, bundle, destination / "compile-assets")
    compilation = compile_preparation(preparation, out_dir=destination / "compile")
    _assert_no_unresolved_placeholders(compilation["workflow_inputs"])
    sealed = _sealed_plan(compilation)
    if endpoint_client is not None:
        from astrid.core.generation.preflight import require_h3_graph_readiness
        sealed = {**sealed, "graph": require_h3_graph_readiness(compilation, endpoint_client, destination),
                  "request_digest": request.digest, "input_bundle_sha256": bundle_digest(bundle)}
        if "source_video" in compilation["workflow_inputs"]:
            from astrid.packs.vibecomfy.invocation_preflight import measure_h3_source_av
            source_id = _authoritative_source_asset_id(request)
            if source_id is None:
                raise AstridError("H3 endpoint source video has no authoritative bundle identity")
            sealed["h3_source_av"] = measure_h3_source_av(paths[source_id]).to_dict()
    return sealed


def _validate_endpoint_inputs(inputs: Mapping[str, Any]):
    """Check endpoint-only H3 controls before any paid acquisition."""
    from astrid.packs.h3_av.shared.spec import canonical_spec

    if "ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE" in environ:
        raise AstridError("H3 RunPod endpoint route cannot use the T9 substitute")
    if set(inputs) - {"request", "asset_map", "editorial_approved"}:
        raise AstridError("unsupported H3 endpoint controls; all executors must stay local")
    if type(inputs.get("editorial_approved", False)) is not bool:
        raise AstridError("H3 editorial approval must be an explicit boolean")
    request = normalize_request(load_request_document(inputs["request"]))
    if len(canonical_spec(request).sampling_passes()) != 1:
        raise AstridError("H3 endpoint route requires exactly one sampling pass")
    return request


def preflight_endpoint(inputs: Mapping[str, Any], endpoint_client) -> dict[str, Any]:
    """Compose or refuse locally before the endpoint owner may acquire/retain."""
    import tempfile

    request = _validate_endpoint_inputs(inputs)
    with tempfile.TemporaryDirectory(prefix="astrid-h3-endpoint-") as temporary:
        root = Path(temporary)
        bundle = build_input_bundle(request, _json_mapping(Path(inputs["asset_map"])), root / "inputs.zip")
        return _compile_plan(request, bundle, root / "plan", endpoint_client=endpoint_client)


def _sealed_plan(compilation: Mapping[str, Any]) -> dict[str, Any]:
    capabilities = compilation.get("capabilities", {})
    contract = capabilities.get("output_contract")
    if contract not in {"muxed_av_full_timeline", "separate_av_full_timeline"}:
        raise AstridError("unsupported H3 parent output mapping")
    intent = _generation_intent(compilation)
    if (intent["modality"] != "video" or len(intent["groups"]) != 1
            or len(intent["groups"][0]["selectors"]) != 1):
        raise AstridError("tracked H3 requires the sealed single video selector")
    digest = compilation.get("compilation_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise AstridError("H3 compilation has no sealed identity")
    return {"compilation_digest": digest, "output_contract": contract, "intent": intent,
            "workflow_members": {name: row["sha256"] for name, row in compilation["workflow"].items()},
            "workflow_inputs_sha256": _stable_digest(compilation["workflow_inputs"])}


def _import(client, project: str, data: bytes, filename: str) -> dict[str, Any]:
    import mimetypes

    importer = getattr(getattr(client, "media", None), "import_bytes", None)
    if not callable(importer):
        raise AstridError("tracked H3 requires Runtime managed-byte import")
    oid = _parent_digest(data)
    response = importer(project=project, data=data, filename=filename,
                        media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
                        idempotency_key="h3-parent-input-" + oid[7:])
    value = getattr(response, "data", None)
    if not getattr(response, "ok", False) or not isinstance(value, Mapping):
        raise AstridError("H3 managed input import did not return a confirmed object")
    if (value.get("digest") or value.get("object_id")) != oid:
        raise AstridError("H3 managed input import changed its bytes")
    return {"object_id": oid, "digest": oid, "filename": filename}


PUBLIC_CONTROLS = frozenset({"execution_request", "worker_qualification", "prepared_worker",
                              "cleanup_receipt", "require_worker_qualification", "resume", "dry_run"})


def public_controls(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve producer-local controls; no owner path crosses the worker seam."""
    from astrid.sdk.execution_request import normalize_execution_request
    def document(name):
        value = inputs.get(name)
        return _json_mapping(Path(value)) if isinstance(value, (str, Path)) else value
    for name in ("require_worker_qualification", "resume", "dry_run"):
        if type(inputs.get(name, False)) is not bool:
            raise AstridError(f"H3 {name} must be an explicit boolean")
    request = normalize_execution_request(document("execution_request"))
    if request and request.get("inputs"):
        raise AstridError("H3 run inputs are derived from admitted stages")
    qualification = None
    if inputs.get("worker_qualification") is not None:
        qualification = load_worker_qualification(inputs["worker_qualification"])
        request, _ = _qualified_execution_request(qualification, request)
    prepared_digest = None
    if inputs.get("prepared_worker") is not None:
        from astrid.packs.runpod.prepared_task import load_prepared_worker_selection, prepared_selection_digest
        selection = load_prepared_worker_selection(inputs["prepared_worker"])
        request = selection.execution_request(request)
        prepared_digest = prepared_selection_digest(selection)
    elif inputs.get("require_worker_qualification") and qualification is None:
        raise AstridError("worker qualification is required before targeted H3 admission")
    if request and request["target"]["kind"] not in {"default", "runpod"}:
        raise AstridError(UNSUPPORTED_TARGET_MESSAGE)
    request = normalize_execution_request(request)
    cleanup = document("cleanup_receipt")
    if cleanup is not None and not isinstance(cleanup, Mapping):
        raise AstridError("cleanup_receipt must contain a JSON object")
    return {"run_execution_request": request, "cleanup_receipt": cleanup,
            "qualification_digest": qualification_digest(qualification) if qualification else None,
            "prepared_selection_digest": prepared_digest,
            "require_worker_qualification": bool(inputs.get("require_worker_qualification", False))}


def project_parent(inputs: Mapping[str, Any], *, project: str, client, registry, h3_endpoint=None):
    """Freeze and import exact request/bundle/source bytes before one admission."""
    import tempfile
    from astrid.core.foundation.hash import executor_definition_digest
    from astrid.sdk.invocation import _generation_publish_effect

    if not project or inputs.get("project", project) != project:
        raise AstridError("H3 parent project must match its Runtime project")
    show = getattr(getattr(client, "projects", None), "show", None)
    observed = show(project) if callable(show) else None
    project_data = getattr(observed, "data", None)
    if not getattr(observed, "ok", False) or not isinstance(project_data, Mapping):
        raise AstridError("H3 parent requires confirmed Runtime project identity")
    project = project_data.get("project_id") or project_data.get("id")
    if not isinstance(project, str) or not project:
        raise AstridError("H3 Runtime project has no canonical identity")
    controls = public_controls(inputs)
    target_policy = inputs.get("target_policy")
    if target_policy is not None:
        if (not isinstance(target_policy, Mapping) or h3_endpoint is not None
                or controls["run_execution_request"] is not None or inputs.get("prepared_worker") is not None):
            raise AstridError("host target policy requires ordinary local child placement")
        _validate_endpoint_inputs({key: value for key, value in inputs.items()
                                   if key not in {"target_policy", "project"}})
        controls["target_policy"] = json.loads(json.dumps(target_policy, allow_nan=False))
    if h3_endpoint is not None:
        if controls["run_execution_request"] is not None or inputs.get("prepared_worker") is not None:
            raise AstridError("H3 endpoint executors require the default local target")
        controls["h3_endpoint"] = json.loads(json.dumps(h3_endpoint))
    if set(inputs) - ({"request", "asset_map", "project", "editorial_approved", "target_policy"} | PUBLIC_CONTROLS):
        raise AstridError("unsupported tracked H3 parent controls or caller-supplied roots")
    if type(inputs.get("editorial_approved", False)) is not bool:
        raise AstridError("H3 editorial approval must be an explicit boolean")
    # Only the trusted producer opens asset_map paths. No such path is retained
    # in the admitted spec. The existing bundle validates exact membership.
    request_path, map_path = inputs.get("request"), inputs.get("asset_map")
    if not isinstance(request_path, (str, Path)) or not isinstance(map_path, (str, Path)):
        raise AstridError("tracked H3 requires request and asset_map files before admission")
    document = load_request_document(request_path)
    request = normalize_request(document)
    asset_map = _json_mapping(Path(map_path))
    with tempfile.TemporaryDirectory(prefix="astrid-h3-parent-") as temporary:
        root = Path(temporary)
        bundle = build_input_bundle(request, asset_map, root / "h3-inputs.zip")
        paths, _ = materialize_input_bundle(request, bundle, root / "validated-assets")
        sealed = _compile_plan(request, bundle, root / "plan")
        if h3_endpoint is not None:
            from astrid.packs.h3_av.shared.input_bundle import bundle_digest
            expected = h3_endpoint["preflight"]
            if (sealed != {key: expected[key] for key in sealed}
                    or request.digest != expected["request_digest"]
                    or bundle_digest(bundle) != expected["input_bundle_sha256"]):
                raise AstridError("H3 endpoint inputs changed after pre-acquisition compilation")
        roots = {
            "request": _import(client, project, _bytes(document), "request.json"),
            "input_bundle": _import(client, project, bundle.read_bytes(), "h3-inputs.zip"),
        }
        source_id = _authoritative_source_asset_id(request)
        if source_id is not None:
            source = Path(paths[source_id])
            roots["source"] = _import(client, project, source.read_bytes(), source.name)

    identities = {name: {"capability_id": capability,
                         "capability_digest": "sha256:" + executor_definition_digest(registry.get(capability))}
                  for name, capability in STAGES}
    root_ref = lambda name: {"name": name, "root_object_id": roots[name]["object_id"]}
    producer = lambda name, stage, port: {"name": name, "producer_stage": stage, "output_port": port}
    workflow = [producer(name, "compile", name) for name in ("python", "companion", "source")]
    source = [root_ref("source")] if "source" in roots else []
    bindings = {
        "prepare": [root_ref("request"), root_ref("input_bundle")],
        "compile": [producer("preparation", "prepare", "preparation"), root_ref("input_bundle")],
        "validate": workflow,
        "run": [*workflow, producer("managed_assets", "compile", "managed_assets")],
        "compose": [producer("preparation", "prepare", "preparation"), root_ref("input_bundle"),
                    producer("compilation", "compile", "compilation"),
                    producer("generated", "run", "vibecomfy_run"),
                    *([producer("generated_audio", "run", "vibecomfy_run")]
                      if sealed["output_contract"] == "separate_av_full_timeline" else []), *source],
        "verify": [producer("preparation", "compose", "preparation"), root_ref("input_bundle"),
                   producer("composition", "compose", "composition"), producer("candidate", "compose", "candidate"), *source],
        "finalizer": [producer("candidate", "compose", "candidate"), producer("verification", "verify", "verification")],
    }
    def capability(capability_id):
        definition = registry.get(capability_id)
        return SimpleNamespace(id=capability_id, inputs=definition.inputs, outputs=definition.outputs,
                               definition=definition.to_dict())
    intent = {**sealed["intent"], "metadata": {"h3_av": {"request_digest": request.digest}}}
    raw = _generation_publish_effect(capability("vibecomfy.run"), project=project, generation_intent=intent)
    final_intent = {**intent, "groups": [{"group_key": "main", "selectors": [{
        "selector": "final-composition", "ordinal": 0, "variant_key": "final-composition", "required": True}]}]}
    final_intent["metadata"] = {"h3_av": {"request_digest": request.digest, "publication_scope": "final_composition"}}
    final = _generation_publish_effect(capability("h3_av.publication_finalizer"), project=project,
                                       generation_intent=final_intent)
    run_request = controls["run_execution_request"]
    run_target = run_request["target"] if run_request else {"kind": "default"}
    policy = {
        "capabilities": list(identities.values()),
        "targets": [{"kind": "default"}] + ([run_target] if run_target != {"kind": "default"} else []),
        "input_object_ids": list(dict.fromkeys(row["object_id"] for row in roots.values())),
        "stages": [{"name": name, **identities[name], "target": run_target if name == "run" else {"kind": "default"},
                    **({"execution_request": run_request} if name == "run" and run_request else {}),
                    "inputs": [ref for port in registry.get(capability_id).inputs
                               for ref in bindings[name] if ref["name"] == port.name]}
                   for name, capability_id in STAGES],
        "publications": {
            "raw": {"stage": "run", "output_port": "vibecomfy_run", "effect": raw},
            "final": {"stage": "finalizer", "output_port": "verified_candidate", "candidate_stage": "compose",
                      "candidate_output_port": "candidate", "verify_stage": "verify",
                      "verify_output_port": "verification", "effect": final},
        },
    }
    plan = {"version": 1, "project": project, "request_digest": request.digest, "roots": roots,
            "stages": policy["stages"], "publications": policy["publications"], "controls": controls, **sealed}
    return {**roots, "plan": plan, "editorial_approved": bool(inputs.get("editorial_approved", False))}, policy


class ManagedParentClient:
    """Adapt the existing transform to the attempt's host-owned child bridge."""
    def __init__(self, plan: Mapping[str, Any], output_root: Path):
        self.plan = plan
        self.output_root = output_root
        self.results: dict[str, Any] = {}
        self.media = SimpleNamespace(read_bytes=self.read_bytes)

    def selected_raw(self, rows):
        group = self.plan["publications"]["raw"]["effect"]["payload"]["groups"][0]
        selector = group["selectors"][0]
        selected = [row for row in rows if row.get("output_port") == selector["output_port"]
                    and row.get("group_key") == group["group_key"]
                    and row.get("variant_key") == selector["variant_key"]
                    and row.get("ordinal") == selector["ordinal"]]
        if len(selected) != 1:
            raise AstridError("raw selector must resolve exactly one child association")
        return selected

    def invoke_result(self, capability_id, *, inputs, **kwargs):
        from astrid.sdk.invocation import invoke_result
        stage = next((name for name, cid in STAGES if cid == capability_id), None)
        if stage is None:
            raise AstridError("H3 parent attempted an undeclared stage")
        values = dict(inputs)
        target_policy = self.plan.get("controls", {}).get("target_policy")
        if target_policy is not None and stage == "run":
            values["target_policy"] = json.dumps(target_policy, sort_keys=True, separators=(",", ":"), allow_nan=False)
        endpoint = self.plan.get("controls", {}).get("h3_endpoint")
        if endpoint is not None and stage == "run":
            if (values.get("comfy_url") != endpoint["target"]["comfy_url"]
                    or values.get("comfy_output_dir") or kwargs.get("execution_request")):
                raise AstridError("H3 run endpoint differs from the admitted local endpoint")
        intent = values.pop("generation_intent", None)
        if intent is not None:
            values["generation_intent"] = intent
        declaration = next(row for row in self.plan["stages"] if row["name"] == stage)
        refs = []
        for declared in declaration["inputs"]:
            name = declared["name"]
            value = values.get(name)
            if not isinstance(value, Mapping):
                raise AstridError("staged input descriptor is missing")
            oid = value.get("object_id", value.get("digest"))
            values[name] = {key: value[key] for key in ("object_id", "digest", "filename") if key in value}
            if "root_object_id" in declared:
                if oid != declared["root_object_id"]:
                    raise AstridError("staged root identity changed")
                refs.append({"name": name, "root_object_id": oid})
                continue
            producer = self.results.get(declared["producer_stage"])
            if producer is None:
                raise AstridError("staged input producer is unavailable")
            rows = [row for row in producer.outputs.get("managed_outputs", [])
                    if row["object_id"] == oid and row["output_port"] == declared["output_port"]]
            if declared["producer_stage"] == "run" and name == "generated":
                selector = self.plan["publications"]["raw"]["effect"]["payload"]["groups"][0]
                selected = selector["selectors"][0]
                rows = [row for row in rows if row.get("group_key") == selector["group_key"]
                        and row.get("variant_key") == selected["variant_key"]
                        and row.get("ordinal") == selected["ordinal"]]
            if name == "generated_audio":
                rows = [row for row in rows if row.get("producer", {}).get("output_port") in {"audio", "generated_audio", "generated_audios", "SaveAudio"}]
            if len(rows) != 1:
                raise AstridError("staged input association is absent or ambiguous")
            row = rows[0]
            refs.append({"name": name, "producer_task_id": row["task_id"],
                         "association_id": row["association_id"], "output_port": row["output_port"]})
        result = invoke_result(capability_id, kind="executor", inputs=values, child_key=stage,
                               stage=stage, input_refs=refs,
                               wait=True, read_managed_outputs=True,
                               timeout_seconds=3600.0, poll_seconds=1.0)
        if not result.ok:
            exc = _validation_failure(result, capability_id=capability_id)
            exc.h3_stage = stage
            exc.h3_child_correlation = {
                key: _safe_correlation(getattr(result, attribute, None))
                for key, attribute in (("run_id", "kernel_run_id"), ("task_id", "kernel_task_id"),
                                       ("attempt_id", "kernel_attempt_id"))
            }
            raise exc
        self.results[stage] = result
        return result

    def verify_compilation(self, compilation):
        observed = _sealed_plan(compilation)
        if observed != {key: self.plan[key] for key in observed}:
            raise AstridError("H3 staged compilation changed the admitted sealed plan")

    def read_bytes(self, object_id):
        for result in self.results.values():
            for row in result.outputs.get("managed_outputs", []):
                if row.get("object_id") == object_id:
                    materialized = result.materialize_output(row["association_id"])
                    return (self.output_root / materialized.filename).read_bytes()
        raise AstridError("H3 parent cannot read an unbound child object")


def execute(*, request, input_bundle, plan, source=None, editorial_approved=False,
            host_output_root=None, **unused):
    """Installed Python action; the host owns output root, lease and settlement."""
    from os import environ
    from astrid.sdk import _child_bridge
    if not environ.get(ASTRID_TASK_RUN_ID, "").strip() or _child_bridge._bridge is None:
        raise AstridError("H3 parent requires a live Runtime-admitted attempt and child bridge")
    if unused or not isinstance(plan, Mapping) or plan.get("version") != 1 or host_output_root is None:
        raise AstridError("unsupported H3 parent projection")
    root = Path(host_output_root)
    paths = {"request": Path(request), "input_bundle": Path(input_bundle)}
    if source is not None:
        paths["source"] = Path(source)
    roots = plan.get("roots", {})
    if set(paths) != set(roots):
        raise AstridError("H3 parent root input set changed")
    for name, path in paths.items():
        if _parent_digest(path.read_bytes()) != roots[name].get("object_id"):
            raise AstridError("H3 parent managed root bytes changed")
    normalized = normalize_request(load_request_document(paths["request"]))
    if normalized.digest != plan.get("request_digest"):
        raise AstridError("H3 parent request identity changed")
    members, _ = materialize_input_bundle(normalized, paths["input_bundle"], root / "root-assets")
    source_id = _authoritative_source_asset_id(normalized)
    if (source_id is None) != (source is None):
        raise AstridError("H3 parent source declaration changed")
    if source_id is not None and paths["source"].read_bytes() != Path(members[source_id]).read_bytes():
        raise AstridError("H3 parent source must be the exact validated bundle member")
    # Python actions load under a verified private module prefix. Use the
    # installed adapter class shared with transform.run's bounded dispatch.
    client = ManagedParentClient(plan, root)
    controls = plan.get("controls", {})
    admitted_request = controls.get("run_execution_request")
    placement = "runpod" if (admitted_request or {}).get("target", {}).get("kind") == "runpod" else "local"
    args = argparse.Namespace(request=paths["request"], asset_map=None, out=root / "stages",
        project=plan["project"], execution_request=None, worker_qualification=None, prepared_worker=None,
        cleanup_receipt=None, require_worker_qualification=False, editorial_approved=editorial_approved,
        resume=(root / "stages" / "operation-state.json").is_file(),
        dry_run=False, placement=placement, _managed_client=client,
        _managed_bundle=paths["input_bundle"], _managed_roots=roots,
        _admitted_cleanup_receipt=controls.get("cleanup_receipt"),
        _admitted_execution_request=admitted_request,
        _admitted_qualification_digest=controls.get("qualification_digest"))
    result = run_transform(args)
    receipt = _json_mapping(Path(result["final_receipt"]))
    if (receipt.get("overall_status") not in {"candidate_verified", "complete"}
            or any(receipt.get("states", {}).get(key, {}).get("status") != "passed"
                   for key in ("raw_managed_publication", "candidate_verified", "final_composition_publication"))):
        raise AstridError("H3 parent final managed publication is unproven")
    candidate = root / "verified-candidate.media"
    final_receipt = root / "final-receipt.json"
    shutil.copyfile(result["verified_candidate"], candidate)
    shutil.copyfile(result["final_receipt"], final_receipt)
    final_rows = [row for row in client.results["finalizer"].outputs["managed_outputs"]
                  if row["output_port"] == "verified_candidate"]
    if len(final_rows) != 1 or _parent_digest(candidate.read_bytes()) != final_rows[0]["object_id"]:
        raise AstridError("H3 parent must retain the actual final publication bytes")
    _write_parent_manifests(root, final_rows[0]["media_type"])
    return {"status": receipt["overall_status"], "overall_status": receipt["overall_status"],
            "states": receipt["states"]}


def _write_parent_manifests(root: Path, media_type: str) -> None:
    """Settle a retrievable media inventory alongside the host harvest receipt."""
    from astrid.core._shared.result_manifest import build_manifest, write_manifest
    outputs = [
        {"name": "verified_candidate", "path": "verified-candidate.media", "ordinal": 0,
         "group_key": "main", "variant_key": "final-composition",
         "media_type": media_type},
        {"name": "final_receipt", "path": "final-receipt.json", "ordinal": 1, "media_type": "application/json"},
    ]
    created = datetime.now(timezone.utc).isoformat()
    write_manifest(root / "parent-manifest.json", build_manifest(
        kind="h3_av.transform", inputs={}, outputs=outputs, created=created))
    write_manifest(root / "manifest.json", build_manifest(kind="h3_av.transform", inputs={},
        outputs=[*outputs, {"name": "manifest", "path": "parent-manifest.json", "ordinal": 2,
                            "media_type": "application/json"}], created=created))


if __name__ == "__main__":
    guard_canonical_entrypoint("h3_av.transform")
    raise SystemExit(run_pack_main("h3_av.transform", lambda: main()))
