"""Validate and import a streamed `.astrid` timeline archive."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import uuid
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from astrid.sdk.pagination import paged_rows
from astrid.sdk.timeline_import_prepare import check_import_manifest, prepare_import

_HEX = re.compile(r"^[0-9a-f]{64}$")
_TRANSCRIPT = re.compile(r"^transcripts/([0-9a-f]{64})\.([A-Za-z0-9_.%~-]+)\.json$")
_SAFE_SOURCE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
_MAX_METADATA_BYTES = 64 * 1024 * 1024
_INLINE_COMMIT_LIMIT = 768 * 1024
_CHUNK_SIZE = 1024 * 1024


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict
        return _plain(asdict(value))
    return value


def _result_data(result: Any, what: str) -> Mapping[str, Any]:
    if hasattr(result, "ok") and not result.ok:
        error = getattr(result, "error", None)
        raise ValueError(f"{what} failed: {getattr(error, 'message', error)}")
    value = _plain(getattr(result, "data", result))
    if isinstance(value, Mapping) and isinstance(value.get("data"), Mapping):
        value = value["data"]
    if not isinstance(value, Mapping):
        raise ValueError(f"Runtime returned no {what} data")
    return value


def _read_json(archive: zipfile.ZipFile, name: str, max_bytes: int) -> Any:
    info = archive.getinfo(name)
    if info.file_size > max_bytes:
        raise ValueError(f"timeline archive metadata entry {name!r} exceeds the {max_bytes}-byte limit")
    with archive.open(info, "r") as source:
        raw = source.read(max_bytes + 1)
    if len(raw) != info.file_size or len(raw) > max_bytes:
        raise ValueError(f"timeline archive metadata entry {name!r} has an invalid size")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"timeline archive entry {name!r} is not valid UTF-8 JSON") from exc


def _validate_media_archive(archive: zipfile.ZipFile, manifest: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise ValueError("timeline archive contains duplicate entry names")
    for info in infos:
        name = info.filename
        parts = name.split("/")
        mode = (info.external_attr >> 16) & 0xFFFF
        if (not name or name.startswith(("/", "\\")) or "\\" in name or
                any(part in {"", ".", ".."} for part in parts) or info.is_dir() or
                (mode and (mode & 0o170000) == 0o120000) or info.flag_bits & 0x1):
            raise ValueError(f"timeline archive contains an unsafe entry name {name!r}")
        if info.compress_type != zipfile.ZIP_STORED:
            raise ValueError(f"timeline archive entry {name!r} is compressed; stored entries are required")
    if "manifest.json" not in names or "timeline.json" not in names:
        raise ValueError("timeline archive must contain manifest.json and timeline.json")
    media_rows = manifest.get("media")
    transcript_rows = manifest.get("transcripts")
    if not isinstance(media_rows, list) or not isinstance(transcript_rows, list):
        raise ValueError("timeline manifest media and transcripts must be arrays")
    media: list[dict[str, Any]] = []
    expected_media: set[str] = set()
    seen_digests: set[str] = set()
    for index, raw in enumerate(media_rows):
        if not isinstance(raw, Mapping):
            raise ValueError(f"timeline manifest media entry {index} must be an object")
        digest, size = raw.get("sha256"), raw.get("size")
        if not isinstance(digest, str) or not _HEX.fullmatch(digest):
            raise ValueError(f"timeline manifest media entry {index} has an invalid SHA-256 digest")
        if digest in seen_digests:
            raise ValueError(f"timeline manifest repeats media digest {digest}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"timeline manifest media {digest} has an invalid size")
        media_type = raw.get("media_type") or "application/octet-stream"
        if not isinstance(media_type, str) or not media_type:
            raise ValueError(f"timeline manifest media {digest} has an invalid media_type")
        entry_name = f"media/{digest}"
        try:
            info = archive.getinfo(entry_name)
        except KeyError as exc:
            raise ValueError(f"timeline archive is missing media/{digest}") from exc
        if info.file_size != size:
            raise ValueError(f"timeline archive media/{digest} size does not match the manifest")
        hasher = hashlib.sha256()
        total = 0
        with archive.open(info, "r") as source:
            while True:
                chunk = source.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                hasher.update(chunk)
        if total != size or hasher.hexdigest() != digest:
            raise ValueError(f"timeline archive media/{digest} failed size or SHA-256 validation")
        media.append({**dict(raw), "sha256": digest, "media_type": media_type})
        expected_media.add(entry_name)
        seen_digests.add(digest)

    transcripts: list[str] = []
    for index, name in enumerate(transcript_rows):
        if not isinstance(name, str) or not _TRANSCRIPT.fullmatch(name):
            raise ValueError(f"timeline manifest transcript entry {index} has an unsafe archive name")
        if name in transcripts:
            raise ValueError(f"timeline manifest repeats transcript entry {name!r}")
        if name not in names:
            raise ValueError(f"timeline archive is missing transcript entry {name!r}")
        transcripts.append(name)

    expected = {"manifest.json", "timeline.json", *expected_media, *transcripts}
    extras = sorted(set(names) - expected)
    missing = sorted(expected - set(names))
    if extras or missing:
        raise ValueError(f"timeline archive entries do not match its manifest (extra={extras}, missing={missing})")
    return media, transcripts


def _transport(client: Any) -> Any:
    value = getattr(client, "_transport", None)
    if value is None:
        value = getattr(getattr(client, "_remote", None), "_transport", None)
    if value is None and callable(getattr(client, "list_project_objects", None)):
        value = client
    if value is None:
        raise ValueError("timeline import requires an Astrid Runtime client")
    return value


def _project_id(client: Any, project: str | None) -> str:
    ref = project
    resolver = getattr(getattr(client, "timelines", None), "_resolve_project_ref", None)
    if callable(resolver):
        resolved = resolver(project)
        if hasattr(resolved, "ok") and not resolved.ok:
            error = getattr(resolved, "error", None)
            raise ValueError(f"cannot resolve target project: {getattr(error, 'message', error)}")
        ref = getattr(resolved, "data", resolved)
    elif project in (None, ""):
        transport = _transport(client)
        current = _plain(transport.current_project())
        selected = current.get("project") if isinstance(current, Mapping) else None
        ref = (selected or {}).get("project_id") or (selected or {}).get("id") or (selected or {}).get("slug")
        if not isinstance(ref, str) or not ref:
            raise ValueError("no current project is selected")
    result = client.projects.show(ref)
    data = _result_data(result, "target project")
    project_id = data.get("project_id") or data.get("id")
    if not isinstance(project_id, str) or not project_id:
        raise ValueError(f"target project {project!r} has no Runtime identity")
    return project_id


def _existing_objects(transport: Any, project_id: str) -> set[str]:
    rows = paged_rows(transport.list_project_objects, project_id, limit=100)
    if rows is None:
        raise ValueError("Runtime could not list target project objects")
    existing: set[str] = set()
    for raw in rows:
        row = _plain(raw)
        # A project may reference an object for other reasons (for example as
        # a promoted output); only a managed relation satisfies import's
        # commit dependency check. Missing relation metadata is not sufficient.
        if not isinstance(row, Mapping) or row.get("relation") != "managed":
            continue
        digest = row.get("digest") or row.get("object_id")
        if isinstance(digest, str) and digest:
            existing.add(digest.removeprefix("sha256:"))
    return existing


def _derived_key(key: str, operation: str, identity: str) -> str:
    return hashlib.sha256(f"{key}\0{operation}\0{identity}".encode("utf-8")).hexdigest()


def _upload(client: Any, project_id: str, stream: Any, *, size: int, media_type: str,
            filename: str, digest: str, key: str) -> Mapping[str, Any]:
    result = client.media.import_stream(
        project=project_id, stream=stream, size=size, filename=filename,
        media_type=media_type, expected_digest="sha256:" + digest, idempotency_key=key,
    )
    row = _result_data(result, f"upload of {filename}")
    object_id = row.get("object_id") or row.get("digest")
    if not isinstance(object_id, str) or not object_id:
        raise ValueError(f"Runtime upload of {filename} returned no object identity")
    if object_id.removeprefix("sha256:") != digest:
        raise ValueError(f"Runtime uploaded {filename} with a different object digest")
    return row


def _source_document_rows(archive: zipfile.ZipFile, names: list[str]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    used = 0
    for name in names:
        info = archive.getinfo(name)
        used += info.file_size
        if used > _MAX_METADATA_BYTES:
            raise ValueError("timeline transcripts exceed the 64 MiB metadata limit")
        content = _read_json(archive, name, _MAX_METADATA_BYTES)
        if not isinstance(content, Mapping):
            raise ValueError(f"transcript archive entry {name!r} must contain a JSON object")
        matched = _TRANSCRIPT.fullmatch(name)
        media_id, source_id = content.get("media_id"), content.get("source_id")
        if (matched is None or not isinstance(media_id, str) or media_id != "sha256:" + matched.group(1)
                or not isinstance(source_id, str)
                or quote(source_id, safe=_SAFE_SOURCE) != matched.group(2)):
            raise ValueError(f"transcript archive entry {name!r} does not match its transcript identity")
        output.append({"kind": "media.transcript", "content": dict(content)})
    return output


def import_timeline(client: Any, path: str | Path, project: str | None = None, name: str | None = None) -> dict[str, Any]:
    """Import an archive after full preflight, streaming each missing object once."""
    archive_path = Path(path)
    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"cannot read timeline archive {archive_path}: {exc}") from exc
    with archive:
        names = [info.filename for info in archive.infolist()]
        if len(names) != len(set(names)):
            raise ValueError("timeline archive contains duplicate entry names")
        if "manifest.json" not in names:
            raise ValueError("timeline archive is missing manifest.json")
        manifest = _read_json(archive, "manifest.json", _MAX_METADATA_BYTES)
        if not isinstance(manifest, Mapping):
            raise ValueError("timeline archive manifest must be a JSON object")
        # Unknown formats fail immediately with the supported recovery message.
        check_import_manifest(manifest)
        media_rows, transcript_names = _validate_media_archive(archive, manifest)
        metadata_names = ("manifest.json", "timeline.json", *transcript_names)
        if "timeline.json" not in names:
            raise ValueError("timeline archive is missing timeline.json")
        metadata_bytes = sum(archive.getinfo(entry).file_size for entry in metadata_names)
        if metadata_bytes > _MAX_METADATA_BYTES:
            raise ValueError("timeline archive metadata exceeds the 64 MiB limit")
        timeline = _read_json(archive, "timeline.json", _MAX_METADATA_BYTES)
        if not isinstance(timeline, Mapping):
            raise ValueError("timeline archive timeline.json must contain an object")
        transcripts = _source_document_rows(archive, transcript_names)

        # Compatibility and all pure copy/compiler checks happen before uploads.
        differences = check_import_manifest(manifest)
        project_id = _project_id(client, project)
        prepared = prepare_import(timeline, manifest, transcripts, project_id, name=name)
        required = {str(row.get("media_id") or "").removeprefix("sha256:")
                    for row in prepared["publication"].get("dependency_manifest", {}).get("media", [])
                    if isinstance(row, Mapping)}
        declared = {row["sha256"] for row in media_rows}
        if required != declared:
            missing = sorted(required - declared)
            extra = sorted(declared - required)
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if extra:
                details.append("unreferenced " + ", ".join(extra))
            raise ValueError("timeline archive media does not match the prepared publication closure: " + "; ".join(details))
        for document in transcripts:
            transcript_media = str(document.get("content", {}).get("media_id") or "").removeprefix("sha256:")
            if transcript_media not in declared:
                raise ValueError(f"timeline archive is missing transcript media {transcript_media}")
        for binding in prepared.get("text_bindings", []):
            binding_digest = str(binding.get("media_id") or "").removeprefix("sha256:")
            if binding_digest not in declared:
                raise ValueError(f"timeline archive is missing UTF-8 text binding object {binding_digest}")

        prepared_bytes = json.dumps(prepared, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(prepared_bytes) > _MAX_METADATA_BYTES:
            raise ValueError("prepared timeline import exceeds the 64 MiB Runtime metadata limit")

        transport = _transport(client)
        existing = _existing_objects(transport, project_id)
        key = uuid.uuid4().hex
        uploaded_count = uploaded_bytes = 0
        for item in media_rows:
            digest = item["sha256"]
            if digest in existing:
                continue
            entry = f"media/{digest}"
            filename = str(item.get("original_name") or digest).replace("\\", "/").rsplit("/", 1)[-1] or digest
            media_type = str(item.get("media_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream")
            with archive.open(entry, "r") as stream:
                _upload(client, project_id, stream, size=item["size"], media_type=media_type,
                        filename=filename, digest=digest, key=_derived_key(key, "media", digest))
            uploaded_count += 1
            uploaded_bytes += item["size"]

        body: Mapping[str, Any] = {**prepared, "source": {"via": "cli"}}
        if len(prepared_bytes) > _INLINE_COMMIT_LIMIT:
            digest = hashlib.sha256(prepared_bytes).hexdigest()
            import io
            body = {"prepared_object_id": "sha256:" + digest, "source": {"via": "cli"}}
            _upload(client, project_id, io.BytesIO(prepared_bytes), size=len(prepared_bytes),
                    media_type="application/json", filename="prepared-timeline-import.json",
                    digest=digest, key=_derived_key(key, "prepared", digest))

        commit = client.timelines.import_timeline(project_id, body, idempotency_key=key)
        result = _result_data(commit, "timeline import commit")
        result = dict(result)
        receipt = getattr(commit, "receipt", None)
        if receipt is not None:
            result["receipt"] = receipt.as_dict() if hasattr(receipt, "as_dict") else _plain(receipt)
        media_receipt = result.get("media")
        if not isinstance(media_receipt, Mapping):
            media_receipt = {"count": len(media_rows), "bytes": sum(item["size"] for item in media_rows)}
        result["media"] = {**dict(media_receipt), "uploaded": uploaded_count, "uploaded_bytes": uploaded_bytes}
        result.setdefault("missing_effects", differences["missing_effects"])
        result.setdefault("different_effects", differences["different_effects"])
        return result


__all__ = ["import_timeline"]
