"""SDK-connected H3 audiovisual transform orchestration."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from runtime_protocol import platform
from runtime_protocol.dirfd import filesystem as os

from astrid.core.contracts.errors import AstridError
from astrid.core.execution.worker_qualification import (
    WorkerQualificationError,
    ensure_runpod_worker,
    load_worker_qualification,
    qualification_digest,
)
from astrid.core.pack.entrypoint import guard_canonical_entrypoint, run_pack_main
from astrid.packs.h3_av.actions.transform.operation import OperationJournal, OperationJournalError
from astrid.packs.h3_av.actions.transform.receipt import (
    attest_runtime_managed_composition,
    attest_runtime_managed_publication,
    build_final_receipt,
    write_final_receipt,
)
from astrid.packs.h3_av.shared.input_bundle import build_input_bundle, materialize_input_bundle
from astrid.packs.h3_av.shared.request import load_request_document, normalize_request
from astrid.packs.h3_av.shared.request_v2 import branch_for
from astrid.sdk import AstridClient, observe_task_invocation
from astrid.sdk.results import InvocationResult


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
        identity = producer_id or row.get("object_id") or row.get("digest") or row.get("content_hash")
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
        raise AstridError(f"{capability_id} failed: {result.error}")
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


def _invoke_stage(
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
        journal.record(
            phase, "uncertain", capability_id=capability_id,
            input_digest=input_digest, error=type(exc).__name__, message=str(exc)[:1000],
        )
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


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _invoke_canonical_run(
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
) -> InvocationResult:
    """Admit once; an unsettled or identity-free response cannot be replayed."""

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
    if resume and prior_run is not None and journal.admission_digest is None:
        raise AstridError("cannot resume canonical run: journal has no frozen admission identity")
    try:
        journal.freeze_admission(admission_digest)
    except OperationJournalError as exc:
        raise AstridError(str(exc)) from exc

    receipt_path = _recovery_receipt_path(saved_result, journal)
    if resume and receipt_path.is_file():
        if prior_run is not None:
            recorded_admission = prior_run.get("admission_digest")
            if isinstance(recorded_admission, str) and recorded_admission != admission_digest:
                raise AstridError("cannot resume canonical run: journaled admission identity changed")
        result = _invoke(
            client,
            "vibecomfy.run",
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
            admission_digest=admission_digest,
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
            attempt_id=identity[2], admission_digest=admission_digest,
        )
        return result

    if resume and journal.admission_phase in {"admission_started", "uncertain", "settled"}:
        raise AstridError(
            "cannot resume canonical run: admission may have occurred but its settled result is unavailable"
        )
    if resume and prior_run is not None:
        raise AstridError("cannot resume canonical run: journal phase and saved result disagree")

    journal.set_admission_phase("admission_started")
    journal.record("run", "admission_started", admission_digest=admission_digest)
    try:
        result = _invoke(
            client, "vibecomfy.run", inputs=inputs, out=out, project=project,
            execution_request=execution_request,
            idempotency_context=idempotency_context,
            recovery_path=receipt_path,
        )
        identity = (result.kernel_task_id, result.kernel_run_id, result.kernel_attempt_id)
        if not all(isinstance(value, str) and value for value in identity):
            raise AstridError("canonical run result is missing task/run/attempt identity")
        _save_invocation_result(saved_result, result, input_digest=run_input_digest)
    except Exception as exc:
        journal.set_admission_phase("uncertain")
        journal.record(
            "run", "uncertain", admission_digest=admission_digest,
            error=type(exc).__name__, message=str(exc)[:1000],
        )
        raise
    journal.set_admission_phase("settled")
    journal.record(
        "run", "completed", task_id=identity[0], run_id=identity[1],
        attempt_id=identity[2], admission_digest=admission_digest,
    )
    return result


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


def _run_transform_unlocked(args: argparse.Namespace) -> dict[str, Any]:
    root = args.out.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        return {
            "status": "planned",
            "request": str(args.request.resolve()),
            "asset_map": str(args.asset_map.resolve()),
            "stages": ["h3_av.prepare", "h3_av.compile", "vibecomfy.validate", "vibecomfy.run", "h3_av.compose", "h3_av.verify", "h3_av.publication_finalizer"],
        }
    execution_request = _json_mapping(args.execution_request) if args.execution_request else None
    prepared_selection = None
    qualification: dict[str, Any] | None = None
    if args.worker_qualification is not None:
        try:
            qualification = load_worker_qualification(args.worker_qualification)
            execution_request, qualified_target = _qualified_execution_request(
                qualification, execution_request
            )
        except WorkerQualificationError as exc:
            raise AstridError(f"worker qualification failed before admission: {exc}") from exc
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
    cleanup_receipt = _json_mapping(args.cleanup_receipt) if args.cleanup_receipt else None
    # The parent orchestrator is launched locally, while its child executor
    # tasks target the already-supervised Runtime/GenericPackHost. Starting a
    # second local pack host here would compete with that existing authority.
    with AstridClient.open_from_launcher(start_pack_host=False) as client:
        raw_request = load_request_document(args.request)
        request = normalize_request(raw_request)
        try:
            journal = OperationJournal(
                root / "operation-state.json", request_digest=request.digest
            )
        except OperationJournalError as exc:
            raise AstridError(f"cannot open H3 operation journal: {exc}") from exc
        if journal.has_history and not getattr(args, "resume", False):
            raise AstridError("operation journal already has history; use --resume to continue it")
        journal.record("operation", "started", resume=bool(getattr(args, "resume", False)))
        idempotency_context = {"h3_submission_id": journal.submission_id}
        staging = root / "staged-inputs"
        staging.mkdir(parents=True, exist_ok=True)
        bundle_path = build_input_bundle(
            request, _json_mapping(args.asset_map), staging / "h3-inputs.zip"
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
            source_inputs["source"] = _import_runtime_file(
                client, project=args.project, path=source_path, filename=source_path.name
            )
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
        # Canonical workflow Python is executable input.  Carry the explicit
        # consent scalar required by VibeComfy's audited validation gate;
        # execution-request targeting is not itself Python consent.
        _invoke_stage(
            client,
            "vibecomfy.validate",
            inputs={
                **bundle_inputs,
                "python_execution_consent": "confirmed",
                "workflow_inputs": json.dumps(
                    compilation["workflow_inputs"],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
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
        preparation_descriptor = _import_runtime_file(
            client, project=args.project, path=provenance_path, filename="provenance-preparation.json"
        )
        generated_input_path = generated_bundle_path or generated_path
        generated_descriptor = _import_runtime_file(
            client,
            project=args.project,
            path=generated_input_path,
            filename=generated_input_path.name,
        )
        composed = _invoke_stage(
            client, "h3_av.compose",
            inputs={
                "preparation": preparation_descriptor,
                "input_bundle": bundle_descriptor,
                "generated": generated_descriptor,
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
            raw_managed_publication=raw_managed_publication,
            final_managed_publication=final_managed_publication,
        )
        final_receipt_path = write_final_receipt(root / "08-final-receipt.json", receipt)
        journal.record(
            "operation",
            receipt["overall_status"],
            final_receipt=str(final_receipt_path),
        )
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
    }


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


def run_transform(args: argparse.Namespace) -> dict[str, Any]:
    """Run one serialized H3 operation in its existing output directory."""

    if getattr(args, "dry_run", False):
        return _run_transform_unlocked(args)
    root = args.out.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with _operation_directory_writer_lock(root):
        return _run_transform_unlocked(args)


def main(argv: list[str] | None = None) -> int:
    result = run_transform(build_parser().parse_args(argv))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    guard_canonical_entrypoint("h3_av.transform")
    raise SystemExit(run_pack_main("h3_av.transform", lambda: main()))
