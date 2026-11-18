"""Bounded opening of Runtime-owned timeline sources and artifacts.

This module is deliberately a resolver, not a second media service.  It takes
the structured reference returned by a canonical timeline/view/run response,
resolves it through the existing Runtime read methods, and returns one small
JSON-safe result.  Opening never renders, edits, imports, or starts a task.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import DomainResult, ErrorObject
from .pagination import paged_rows
from .workspace_client import WorkspaceClientError

DEFAULT_PREVIEW_BYTES = 16 * 1024
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_MATERIALIZE_BYTES = 100 * 1024 * 1024


def _failure(code: str, message: str, **details: Any) -> DomainResult[Any]:
    return DomainResult.failure(ErrorObject(code, message, details))


def _kind(ref: Mapping[str, Any]) -> str:
    value = ref.get("kind") or ref.get("type") or ref.get("ref_kind") or "media"
    return str(value).strip().lower().replace("-", "_")


def _ref_id(ref: Mapping[str, Any]) -> str | None:
    for key in (
        "object_id",
        "source_object_id",
        "media_id",
        "managed_object_reference",
        "digest",
        "id",
        "ref",
    ):
        value = ref.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalized_digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    digest = value.removeprefix("sha256:").lower()
    return digest if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest) else None


def _safe_filename(value: Any, *, fallback: str) -> str:
    candidate = Path(str(value or fallback)).name
    return candidate if candidate not in {"", ".", ".."} else fallback


def _read_bytes(client: Any, object_id: str) -> tuple[bytes | None, DomainResult[Any] | None]:
    try:
        response = client.get_object(object_id)
    except WorkspaceClientError as exc:
        return None, _failure(exc.code, exc.message, **dict(exc.details))
    except Exception as exc:  # transport adapters may expose typed errors differently
        return None, _failure("unavailable", "managed object is unavailable", reason=str(exc))
    if isinstance(response, (bytes, bytearray)):
        return bytes(response), None
    data = response.get("data") if isinstance(response, Mapping) else getattr(response, "data", None)
    if not isinstance(data, (bytes, bytearray)):
        return None, _failure("protocol_error", "Runtime object read returned no bytes", object_id=object_id)
    return bytes(data), None


def _project_objects(client: Any, project: str) -> list[Mapping[str, Any]] | None:
    try:
        rows = paged_rows(client.list_project_objects, str(project), limit=50)
    except WorkspaceClientError:
        return None
    return [row for row in (rows or []) if isinstance(row, Mapping)] if rows is not None else None


def _find_project_object(client: Any, project: str, object_id: str) -> tuple[Mapping[str, Any] | None, DomainResult[Any] | None]:
    rows = _project_objects(client, project)
    if rows is None:
        return None, _failure("unavailable", "Runtime project media listing is unavailable", project=str(project))
    normalized = object_id.removeprefix("sha256:")
    match = next(
        (
            row for row in rows
            if str(row.get("object_id") or "").removeprefix("sha256:") == normalized
            or str(row.get("digest") or "").removeprefix("sha256:") == normalized
        ),
        None,
    )
    if match is None:
        return None, _failure("not_found", "media object is not in the selected project", project=str(project), object_id=object_id)
    return match, None


def _preview(data: bytes, *, media_type: str, limit: int) -> dict[str, Any] | None:
    if limit <= 0:
        return None
    bounded = data[:limit]
    truncated = len(data) > len(bounded)
    if media_type.startswith("text/") or media_type in {"application/json", "application/markdown", "application/x-yaml"}:
        try:
            return {
                "encoding": "utf-8",
                "text": bounded.decode("utf-8"),
                "truncated": truncated,
                "bytes": len(bounded),
            }
        except UnicodeDecodeError:
            pass
    return {
        "encoding": "base64",
        "data": base64.b64encode(bounded).decode("ascii"),
        "truncated": truncated,
        "bytes": len(bounded),
    }


def _materialize(data: bytes, *, filename: str, media_type: str, cache_root: str | Path | None,
                 source: Path | None = None) -> tuple[str, str]:
    default_cache = cache_root is None
    if cache_root is None:
        try:
            from .storage_root import resolve_runtime_data_root

            root = resolve_runtime_data_root() / "astrid-open"
        except (OSError, ValueError):
            root = Path(tempfile.gettempdir()) / "astrid-open"
    else:
        root = Path(cache_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    clean_name = _safe_filename(filename, fallback="opened-object")
    if not Path(clean_name).suffix:
        extension = mimetypes.guess_extension(media_type, strict=False)
        if extension:
            clean_name += extension
    digest = hashlib.sha256(data).hexdigest()
    target = root / (f"{digest[:12]}-{clean_name}" if default_cache else clean_name)
    # Never overwrite a caller's existing path.  The digest suffix makes the
    # disposable delivery deterministic enough to identify while preserving
    # ordinary filename extensions for host readers.
    strategy = "verified-copy"
    if target.exists():
        if (not target.is_symlink() and target.is_file() and target.stat().st_nlink == 1
                and hashlib.sha256(target.read_bytes()).hexdigest() == digest):
            if source is not None:
                try:
                    if os.path.samefile(target, source):
                        from .media_views import clone_or_copy

                        strategy = clone_or_copy(source, target, digest=digest)
                    else:
                        strategy = "verified-reuse"
                except OSError:
                    strategy = "verified-reuse"
            else:
                strategy = "verified-reuse"
            return str(target), strategy
        if default_cache:
            target = target.with_name(f"{target.stem}-{digest[:12]}{target.suffix}")
            if target.exists():
                raise FileExistsError(f"media-open view path collision: {target}")
    if source is not None:
        from .media_views import clone_or_copy

        strategy = clone_or_copy(source, target, digest=digest)
    else:
        # A predictable temporary name or a reused hardlink can expose the
        # store's inode for writing. Create an owned, independent file instead.
        fd, raw = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(raw)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return str(target), strategy


def _definition(client: Any, ref: Mapping[str, Any], *, project: str) -> DomainResult[Any]:
    del client  # definitions are installed Astrid registry data, not Runtime bytes
    from astrid.core.element.catalog import list_element_descriptors

    element_id = str(ref.get("id") or ref.get("element_id") or "").strip()
    if not element_id:
        return _failure("validation_error", "element definition reference requires an id", field="id")
    requested_kind = str(ref.get("element_kind") or ref.get("kind_name") or ref.get("definition_kind") or ref.get("type_name") or "").strip().lower()
    if not requested_kind and str(ref.get("kind") or "").strip().lower() in {"effect", "animation", "transition"}:
        requested_kind = str(ref.get("kind")).strip().lower()
    if not requested_kind:
        # Timeline element refs use kind=element and put the registry kind in
        # element_kind.  A bare id may only resolve against the installed
        # catalog if it is unambiguous.
        requested_kind = ""
    try:
        candidates = list_element_descriptors(project_slug=str(project))
    except Exception as exc:  # installed definition access is optional for output opening
        return _failure("unavailable", "element definition catalog is unavailable", id=element_id, reason=str(exc))
    matches = [row for row in candidates if str(row.get("id")) == element_id and (not requested_kind or str(row.get("kind", "")).lower() in {requested_kind, requested_kind.rstrip("s")})]
    if len(matches) != 1:
        return _failure("unavailable", "element definition is not available in the current installed catalog", id=element_id, kind=requested_kind or None, project=str(project))
    descriptor = dict(matches[0])
    resolved_revision = descriptor.get("revision")
    requested_revision = ref.get("revision") or ref.get("requested_revision")
    if requested_revision is not None and requested_revision != resolved_revision:
        return _failure("unavailable", "requested element definition revision is unavailable", id=element_id, kind=descriptor.get("kind"), requested_revision=requested_revision, resolved_revision=resolved_revision)
    provenance = {
        "source": descriptor.get("source"),
        "pack_id": descriptor.get("packId"),
        "component_path": descriptor.get("componentPath"),
    }
    return DomainResult.success({
        "kind": "element_definition",
        "id": element_id,
        "element_kind": descriptor.get("kind"),
        "requested_revision": requested_revision,
        "resolved_revision": resolved_revision,
        "provenance": provenance,
        # The descriptor is schema/metadata only.  It never includes loaded
        # component bytes and opening it never executes the component.
        "definition": descriptor,
        "scope": {"project": str(project), "authority": "installed_catalog", "read_only": True},
        "availability": "available",
        "preview": None,
        "local_path": None,
        "next_actions": [],
    })


def _resolve_timeline_asset(client: Any, project: str, ref: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, DomainResult[Any] | None]:
    timeline_ref = ref.get("timeline") or ref.get("timeline_ref") or ref.get("timeline_id")
    if not isinstance(timeline_ref, str) or not timeline_ref.strip():
        return None, _failure("validation_error", "timeline asset references require a timeline scope", field="timeline")
    rows = paged_rows(client.list_timelines, str(project), limit=50)
    match = next((row for row in (rows or []) if isinstance(row, Mapping) and str(timeline_ref) in {str(row.get("timeline_id", "")), str(row.get("slug", ""))}), None)
    if match is None:
        return None, _failure("not_found", "timeline is not in the selected project", project=str(project), timeline=str(timeline_ref))
    timeline_id = str(match.get("timeline_id") or match.get("id") or "")
    revision_id = ref.get("revision_id") or match.get("head_revision_id")
    payload = client.inspect_timeline(str(project), timeline_id, options={"revision_id": revision_id, "limit": 500, "detail": True})
    selected = payload.get("selected", []) if isinstance(payload, Mapping) else []
    clip_id = str(ref.get("clip_id") or "")
    asset_id = str(ref.get("asset_id") or "")
    for item in selected if isinstance(selected, list) else []:
        if not isinstance(item, Mapping):
            continue
        for clip in item.get("clips", []) if isinstance(item.get("clips"), list) else []:
            if not isinstance(clip, Mapping):
                continue
            if clip_id and str(clip.get("clip_id")) != clip_id:
                continue
            if asset_id and str(clip.get("asset_id")) != asset_id:
                continue
            if clip_id or asset_id:
                return clip, None
    return None, _failure("unavailable", "timeline asset has no resolvable source object", timeline_id=timeline_id, clip_id=clip_id or None, asset_id=asset_id or None)


def open_reference(
    client: Any,
    project: str,
    ref: Mapping[str, Any] | str,
    *,
    materialize: bool = False,
    cache_root: str | Path | None = None,
    preview_bytes: int = DEFAULT_PREVIEW_BYTES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> DomainResult[Any]:
    """Resolve and boundedly open one returned reference.

    ``ref`` is intentionally a returned structured action.  A plain string
    is accepted only as an exact project-scoped Runtime media id; timeline
    aliases and host-specific artifact URLs require their structured scope.
    """
    if not isinstance(project, str) or not project.strip():
        return _failure("validation_error", "opening requires an explicit project", field="project")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        return _failure("validation_error", "max_bytes must be a positive integer", field="max_bytes")
    if (not isinstance(preview_bytes, int) or isinstance(preview_bytes, bool)
            or not 0 <= preview_bytes <= max_bytes):
        return _failure(
            "validation_error",
            f"preview_bytes must be between 0 (no inline preview) and max_bytes ({max_bytes})",
            field="preview_bytes", allowed=[0, max_bytes],
        )
    if isinstance(ref, str):
        ref = {"kind": "media", "object_id": ref}
    if not isinstance(ref, Mapping):
        return _failure("validation_error", "open requires a structured returned reference", field="ref")
    reference = dict(ref)
    kind = _kind(reference)
    if kind in {"element", "element_definition", "definition", "effect", "animation", "transition"}:
        return _definition(client, reference, project=str(project))

    if kind in {"text", "text_binding", "binding", "voice", "voiceover"}:
        binding_id = reference.get("binding_id") or reference.get("text_binding_id")
        if not isinstance(binding_id, str) or not binding_id:
            return _failure("validation_error", "text opening requires a project-scoped binding_id", field="binding_id")
        try:
            binding = client.get_project_shot_text_binding(str(project), binding_id)
        except WorkspaceClientError as exc:
            return _failure(exc.code, exc.message, **dict(exc.details))
        if not isinstance(binding, Mapping):
            return _failure("protocol_error", "text binding response is malformed", binding_id=binding_id)
        object_id = str(binding.get("media_id") or "")
        if not object_id:
            return _failure("unavailable", "text binding has no managed source object", binding_id=binding_id)
        metadata, failure = _find_project_object(client, str(project), object_id)
        if failure:
            return failure
        data, failure = _read_bytes(client, object_id)
        if failure:
            return failure
        assert data is not None
        expected_digest = _normalized_digest(binding.get("content_hash"))
        actual_digest = hashlib.sha256(data).hexdigest()
        if expected_digest and expected_digest != actual_digest:
            return _failure("integrity_error", "text binding bytes do not match its content identity", binding_id=binding_id)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return _failure("integrity_error", "text binding is not valid UTF-8", binding_id=binding_id)
        bounded = text.encode("utf-8")[:preview_bytes].decode("utf-8", errors="replace")
        return DomainResult.success({
            "kind": "text",
            "ref": {"kind": "text_binding", "binding_id": binding_id},
            "mime_type": "text/plain",
            "name": binding.get("slot") or binding_id,
            "size_bytes": len(data),
            "scope": {"project": str(project), "binding_id": binding_id, "read_only": True},
            "content_identity": "sha256:" + actual_digest,
            "availability": "available",
            "preview": {"encoding": "utf-8", "text": bounded, "truncated": len(data) > preview_bytes, "bytes": min(len(data), preview_bytes)},
            "local_path": None,
            "next_actions": [],
            "binding": dict(binding),
        })

    if kind in {"timeline_asset", "timeline_source", "source"} and not _ref_id(reference):
        clip, failure = _resolve_timeline_asset(client, str(project), reference)
        if failure:
            return failure
        assert clip is not None
        reference = {**reference, **clip}

    if kind in {"managed_output", "output", "artifact", "view", "input_view", "render_output"}:
        association_id = reference.get("association_id") or reference.get("managed_output_id") or reference.get("artifact_id")
        has_direct_object = any(reference.get(key) for key in ("object_id", "source_object_id", "media_id", "digest", "managed_object_reference"))
        if not association_id and not has_direct_object and isinstance(reference.get("id"), str):
            association_id = reference.get("id")
        if association_id and not has_direct_object:
            try:
                association = client.get_managed_output(str(association_id))
            except WorkspaceClientError as exc:
                return _failure(exc.code, exc.message, **dict(exc.details))
            if not isinstance(association, Mapping):
                return _failure("protocol_error", "managed output response is malformed", association_id=association_id)
            reference = {**reference, **dict(association)}

    object_id = _ref_id(reference)
    if not object_id:
        return _failure("validation_error", "open reference has no resolvable media or artifact identity", kind=kind)
    metadata, failure = _find_project_object(client, str(project), object_id)
    if failure:
        return failure
    assert metadata is not None
    expected_size = metadata.get("size")
    if not isinstance(expected_size, int):
        expected_size = metadata.get("size_bytes")
    if isinstance(expected_size, int) and expected_size > max_bytes and not materialize:
        return DomainResult.success({
            "kind": kind,
            "ref": reference,
            "mime_type": metadata.get("media_type") or metadata.get("mime_type") or "application/octet-stream",
            "name": _safe_filename(metadata.get("filename"), fallback=object_id.replace(":", "-")),
            "size_bytes": expected_size,
            "scope": {"project": str(project), "read_only": True},
            "content_identity": metadata.get("digest") or metadata.get("object_id") or object_id,
            "availability": "oversize",
            "preview": None,
            "local_path": None,
            "next_actions": [{"action": "materialize", "max_bytes": DEFAULT_MATERIALIZE_BYTES}],
        })
    if isinstance(expected_size, int) and expected_size > max_bytes and materialize:
        return _failure("size_limit", "requested materialization exceeds its byte limit", size_bytes=expected_size, max_bytes=max_bytes)
    data, failure = _read_bytes(client, str(metadata.get("object_id") or object_id))
    if failure:
        return failure
    assert data is not None
    actual_digest = hashlib.sha256(data).hexdigest()
    declared_digest = _normalized_digest(metadata.get("digest") or metadata.get("object_id"))
    if declared_digest and declared_digest != actual_digest:
        return _failure("integrity_error", "opened bytes do not match Runtime media identity", object_id=object_id)
    media_type = str(metadata.get("media_type") or metadata.get("mime_type") or mimetypes.guess_type(str(metadata.get("filename") or ""))[0] or "application/octet-stream")
    filename = _safe_filename(metadata.get("filename"), fallback=actual_digest[:16])
    local_path = None
    local_path_storage = None
    if materialize:
        if len(data) > max_bytes:
            return _failure("size_limit", "requested materialization exceeds its byte limit", size_bytes=len(data), max_bytes=max_bytes)
        from .media_views import verified_cas_path

        source = verified_cas_path(client, str(project), str(metadata.get("object_id") or object_id), size=len(data))
        local_path, local_path_storage = _materialize(data, filename=filename, media_type=media_type,
                                                      cache_root=cache_root, source=source)
    return DomainResult.success({
        "kind": kind,
        "ref": reference,
        "mime_type": media_type,
        "name": filename,
        "size_bytes": len(data),
        "scope": {"project": str(project), "read_only": True},
        "content_identity": "sha256:" + actual_digest,
        "availability": "available",
        "preview": _preview(data, media_type=media_type, limit=preview_bytes),
        "local_path": local_path,
        "local_path_storage": local_path_storage,
        "next_actions": [],
    })


__all__ = ["DEFAULT_MAX_BYTES", "DEFAULT_MATERIALIZE_BYTES", "DEFAULT_PREVIEW_BYTES", "open_reference"]
