"""Media handles: pass one capability's media to the next without files.

A *handle* names exactly one media object inside a project.  Any declared
``type: file`` input of any executor accepts one, and admission resolves it to
the managed descriptor ``{digest, filename, media_type, size_bytes}`` the host
already materializes.  Nothing is exported, re-imported or uploaded from a
local path, and every resolution is recorded on the task receipt
(``authority_context.media_handles``).

Handle forms::

    "sha256:<64 hex>"              a media digest owned by the project
    "ref:<name or id>[#n]"         a reference's primary image (or its media ordinal n)
    "run:<run_id>[/<port>][#n]"    output n (ordinal) of a previous run ("task:<id>/..." too)
    result.output("<port>", n)     the managed output row itself
    {"ref": <any of the above>}    the same, as a mapping

Generation adds role sugar on top: ``references=[{"ref": handle or reference
name, "role": "source" | "character" | "style" | "brand" | "mask", "depicts":
bool}]``.
Roles map onto the capability's existing ports in provider order (source
first, then character/style, then brand); one handle per port.
"""

from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .exceptions import CapabilityValidationError
from .pagination import page_pair

__all__ = [
    "REFERENCE_ROLES",
    "MediaHandleError",
    "apply_media_handles",
    "is_media_handle",
    "link_outputs_to_references",
    "resolve_media_handle",
]

# Generation input roles -> the existing port that carries them, in the order
# the provider receives attachments.  ``character`` and ``style`` share a slot.
REFERENCE_ROLES: dict[str, str] = {
    "source": "image_ref",
    "character": "style_ref",
    "style": "style_ref",
    "brand": "brand_ref",
    "mask": "mask_ref",
}

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_OUTPUT_HANDLE = re.compile(r"(run|task):([0-9A-Za-z_-]+)(?:/([A-Za-z0-9_.-]+))?(?:#(\d+))?")
_THUMBNAIL_ROLES = frozenset({"thumbnail"})


class MediaHandleError(CapabilityValidationError):
    """A handle could not be resolved to exactly one project media object."""


def _data(result: Any, what: str) -> Any:
    if hasattr(result, "ok") and hasattr(result, "data"):
        if not result.ok:
            error = getattr(result, "error", None)
            message = getattr(error, "message", None) or (error.get("message") if isinstance(error, Mapping) else None)
            raise MediaHandleError(f"{what}: {message or 'not found'}")
        return result.data
    return result


def _rows(value: Any) -> list[Mapping[str, Any]]:
    page = page_pair(value)
    items = page[0] if page is not None else value
    return [row for row in (items or []) if isinstance(row, Mapping)]


def is_media_handle(value: Any) -> bool:
    """True for handle strings, ``{"ref": ...}`` mappings and managed output rows."""
    if isinstance(value, str):
        return value.startswith(("ref:", "run:", "task:")) or bool(_DIGEST.fullmatch(value))
    if isinstance(value, Mapping):
        return "ref" in value or ("output_port" in value and ("digest" in value or "object_id" in value))
    return False


def _safe_filename(name: str, media_type: str, fallback: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "")).strip("-.") or fallback
    extension = {"image/jpeg": ".jpg"}.get(media_type) or mimetypes.guess_extension(media_type or "") or ""
    if extension and Path(stem).suffix.lower() not in {extension, ".jpeg" if extension == ".jpg" else extension}:
        stem = f"{stem}{extension}"
    return stem


def _project_media(client: Any, project: str, digest: str, handle: str) -> Mapping[str, Any]:
    result = client.media.show(project, digest)
    if hasattr(result, "ok") and not result.ok:
        raise MediaHandleError(
            f"{handle!r} resolves to {digest}, which is not media in project {project!r}; "
            "import it with `python3 -m astrid media import <file> --project <slug>` first"
        )
    row = _data(result, f"media {digest}")
    if not isinstance(row, Mapping):
        raise MediaHandleError(f"media {digest} has no readable metadata in project {project!r}")
    return row


def _reference(client: Any, project: str, ref: str) -> Mapping[str, Any]:
    resource = _data(client.references.show(project, ref), f"reference {ref!r} in project {project!r}")
    if not isinstance(resource, Mapping):
        raise MediaHandleError(f"reference {ref!r} returned no read model")
    return resource


def _resolve_reference(client: Any, project: str, text: str) -> tuple[str, dict[str, Any], str]:
    name, _, ordinal = text.partition("#")
    resource = _reference(client, project, name.strip())
    associations = [row for row in resource.get("media_references") or [] if isinstance(row, Mapping)]
    if ordinal:
        if not ordinal.isdigit():
            raise MediaHandleError(f"reference media ordinal must be a number: {text!r}")
        chosen = next((row for row in associations if int(row.get("ordinal", -1)) == int(ordinal)), None)
    else:
        chosen = next((row for row in associations if row.get("is_primary")), None)
    if chosen is None:
        available = ", ".join(f"#{row.get('ordinal')} {row.get('role')}" for row in associations) or "none"
        raise MediaHandleError(f"reference {resource.get('name')!r} has no media {('#' + ordinal) if ordinal else 'primary'}; media: {available}")
    lineage = {
        "reference": {
            "id": resource.get("reference_id"),
            "name": resource.get("name"),
            "association_id": chosen.get("association_id"),
            "ordinal": chosen.get("ordinal"),
        }
    }
    return str(chosen.get("media_id")), lineage, str(resource.get("name") or name)


def _output_rows(client: Any, kind: str, ident: str) -> list[Mapping[str, Any]]:
    task_ids = [ident]
    if kind == "run":
        run = _data(client.runs.show(ident), f"run {ident}")
        task_ids = [str(value) for value in (run.get("task_ids") or [])] if isinstance(run, Mapping) else []
        if not task_ids:
            raise MediaHandleError(f"run {ident} has no tasks")
    rows: list[Mapping[str, Any]] = []
    for task_id in task_ids:
        rows.extend(_rows(_data(client.tasks.list_managed_outputs(task_id), f"outputs of task {task_id}")))
    return rows


def _select_output(rows: list[Mapping[str, Any]], port: str | None, ordinal: int, handle: str) -> Mapping[str, Any]:
    if port is None:
        candidates = [row for row in rows if row.get("role") == "result"]
        port = str(candidates[0].get("output_port")) if candidates else None
    matches = sorted(
        (
            row for row in rows
            if row.get("output_port") == port
            and (port == "thumbnail" or row.get("role") not in _THUMBNAIL_ROLES)
        ),
        key=lambda row: int(row.get("ordinal") or 0),
    )
    chosen = next((row for row in matches if int(row.get("ordinal") or 0) == ordinal), None)
    if chosen is None:
        available = ", ".join(
            sorted({f"{row.get('output_port')}#{row.get('ordinal')}" for row in rows})
        ) or "none"
        raise MediaHandleError(f"{handle!r} matches no output; available: {available}")
    return chosen


def _row_lineage(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "output": {
            key: row.get(key)
            for key in ("run_id", "task_id", "output_port", "ordinal")
            if row.get(key) is not None
        }
    }


def resolve_media_handle(client: Any, project: str, value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve one handle to ``(descriptor, lineage)`` inside *project*."""
    if client is None or not project:
        raise MediaHandleError("resolving a media handle needs client= and project=")
    if isinstance(value, Mapping) and "ref" in value:
        value = value["ref"]
    lineage: dict[str, Any]
    name_hint = ""
    if isinstance(value, Mapping):
        digest = str(value.get("digest") or value.get("object_id") or "")
        lineage = _row_lineage(value)
        name_hint = str(value.get("filename") or "")
        handle = f"run:{value.get('run_id')}/{value.get('output_port')}#{value.get('ordinal', 0)}"
    elif isinstance(value, str) and value.startswith("ref:"):
        handle = value
        digest, lineage, name = _resolve_reference(client, project, value[4:])
        name_hint = name
    elif isinstance(value, str) and (match := _OUTPUT_HANDLE.fullmatch(value)):
        handle = value
        kind, ident, port, ordinal = match.groups()
        row = _select_output(_output_rows(client, kind, ident), port, int(ordinal or 0), value)
        digest = str(row.get("digest") or row.get("object_id") or "")
        lineage = _row_lineage(row)
        name_hint = str(row.get("filename") or "")
    elif isinstance(value, str) and _DIGEST.fullmatch(value):
        handle, digest, lineage = value, value, {}
    else:
        raise MediaHandleError(
            f"not a media handle: {value!r}; use 'sha256:<digest>', 'ref:<reference name>', "
            "'run:<run_id>/<port>#n', or a row from result.output(...)"
        )
    if not digest.startswith("sha256:"):
        digest = "sha256:" + digest
    if not _DIGEST.fullmatch(digest):
        raise MediaHandleError(f"{handle!r} did not resolve to a sha256 media digest")
    media = _project_media(client, project, digest, handle)
    media_type = str(media.get("media_type") or "application/octet-stream")
    if not name_hint or name_hint == "generated_images":
        stored = str(media.get("filename") or "")
        name_hint = stored if Path(stored).suffix else f"media-{digest[7:19]}"
    descriptor = {
        "digest": digest,
        "filename": _safe_filename(name_hint, media_type, f"media-{digest[7:19]}"),
        "media_type": media_type,
        "size_bytes": int(media.get("size") or media.get("size_bytes") or 0),
    }
    return descriptor, {"handle": handle, "digest": digest, **lineage}


def _expand_references(capability: Any, inputs: dict[str, Any]) -> list[dict[str, Any]]:
    """Move ``references=[...]`` onto the capability's ports (pure, no I/O)."""
    entries = inputs.pop("references", None)
    if entries is None:
        return []
    if isinstance(entries, Mapping) or isinstance(entries, (str, bytes)) or not isinstance(entries, (list, tuple)):
        raise MediaHandleError('references must be a list like [{"ref": "Astrid presenter", "role": "character"}]')
    file_ports = {
        str(port.name)
        for port in (getattr(capability, "inputs", ()) or ())
        if str(getattr(port, "type", "")).lower() == "file"
    }
    supported = [role for role, port in REFERENCE_ROLES.items() if port in file_ports]
    planned: list[dict[str, Any]] = []
    taken: dict[str, str] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or "ref" not in entry or "role" not in entry:
            raise MediaHandleError(f'references[{index}] needs "ref" and "role" (got {entry!r})')
        unknown = sorted(set(entry) - {"ref", "role", "depicts"})
        if unknown:
            raise MediaHandleError(f"references[{index}] has unknown key(s): {', '.join(unknown)}")
        role = str(entry["role"])
        port = REFERENCE_ROLES.get(role)
        if port is None or port not in file_ports:
            raise MediaHandleError(
                f"role {role!r} is not available on {getattr(capability, 'id', 'this capability')}; "
                f"roles here: {', '.join(supported) or 'none'}"
                + ("" if supported else " (use execution='codex' for reference roles)")
            )
        if port in taken or inputs.get(port) is not None:
            other = taken.get(port, port)
            raise MediaHandleError(
                f"roles {other!r} and {role!r} both use the {port} slot; pass one of them"
            )
        taken[port] = role
        handle = entry["ref"]
        if isinstance(handle, str) and not is_media_handle(handle):
            handle = "ref:" + handle  # a bare name inside references= is a reference name
        inputs[port] = handle
        planned.append({"port": port, "role": role, "depicts": bool(entry.get("depicts", False))})
    return planned


def apply_media_handles(
    client: Any,
    project: str | None,
    capability: Any,
    inputs: dict[str, Any],
    *,
    resolve: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Expand ``references`` and resolve handles on every declared file port.

    Returns ``(inputs, receipt_lineage, reference_links)``.  Plain descriptors
    and non-handle values pass through unchanged (backwards compatible).
    """
    values = dict(inputs)
    planned = {item["port"]: item for item in _expand_references(capability, values)}
    file_ports = [
        str(port.name)
        for port in (getattr(capability, "inputs", ()) or ())
        if str(getattr(port, "type", "")).lower() == "file"
    ]
    lineage: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    if not resolve:
        return values, lineage, links
    seen: dict[str, str] = {}
    for port in file_ports:
        value = values.get(port)
        if isinstance(value, (list, tuple)):
            # Repeatable file inputs preserve order; resolve each managed handle.
            resolved_items = []
            for index, item in enumerate(value):
                if not is_media_handle(item):
                    resolved_items.append(item)
                    continue
                descriptor, record = resolve_media_handle(client, str(project or ""), item)
                resolved_items.append(descriptor)
                lineage.append({"port": port, "index": index, **record})
            values[port] = resolved_items
            continue
        if value is None or not is_media_handle(value):
            continue
        descriptor, record = resolve_media_handle(client, str(project or ""), value)
        role = planned.get(port, {}).get("role")
        label = f"{role} ({record['handle']})" if role else f"{port} ({record['handle']})"
        if descriptor["digest"] in seen:
            raise MediaHandleError(
                f"{seen[descriptor['digest']]} and {label} are the same image {descriptor['digest'][:19]}…; "
                "pass it once"
            )
        seen[descriptor["digest"]] = label
        values[port] = descriptor
        lineage.append({"port": port, **({"role": role} if role else {}), **record})
        reference = record.get("reference")
        if isinstance(reference, Mapping) and reference.get("id"):
            links.append({
                "reference_id": reference["id"],
                "name": reference.get("name"),
                "media_id": record["digest"],
                "port": port,
                "role": role or port,
                "depicts": bool(planned.get(port, {}).get("depicts")),
            })
    for port, item in planned.items():
        if item["depicts"] and not any(link["port"] == port for link in links):
            raise MediaHandleError(f'"depicts" needs a reference handle (ref:<name>) on role {item["role"]!r}')
    return values, lineage, links


# -- results: every output row carries its handle and a viewable local path --

VIEW_MAX_BYTES = 64 * 1024 * 1024


def output_handle(row: Mapping[str, Any]) -> str | None:
    """The ``run:<run_id>/<port>#<ordinal>`` handle of one managed output row."""
    run_id, port = row.get("run_id"), row.get("output_port")
    if not run_id or not port:
        return None
    return f"run:{run_id}/{port}#{int(row.get('ordinal') or 0)}"


def _view_root(project: str) -> Path:
    from .invocation import _runtime_data_root

    base = _runtime_data_root()
    base = (base / "media-view") if base is not None else Path.home() / ".cache" / "astrid" / "media-view"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(project or "unscoped")).strip("._") or "unscoped"
    return base / slug


def save_media(client: Any, digest: str, to: str | Path, *, overwrite: bool = False, project: str = "") -> Path:
    """Create an independently mutable, digest-verified view of one media object."""
    import hashlib
    import os
    from .media_views import clone_or_copy, verified_cas_path

    target = Path(to).expanduser()
    if target.exists() and not overwrite:
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest.removeprefix("sha256:"):
            source = verified_cas_path(client, project, digest) if project else None
            if source is None:
                return target  # verified bytes, but this client has no local CAS path
            clone_or_copy(source, target, digest=digest)
            return target
        raise MediaHandleError(f"{target} exists; choose another path or pass overwrite")
    data = client.media.read_bytes(digest)
    if hashlib.sha256(data).hexdigest() != digest.removeprefix("sha256:"):
        raise MediaHandleError(f"bytes read for {digest} do not match its digest")
    target.parent.mkdir(parents=True, exist_ok=True)
    source = verified_cas_path(client, project, digest, size=len(data)) if project else None
    if source is not None:
        clone_or_copy(source, target, digest=digest)
    else:
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)
    return target


def annotate_output_rows(client: Any, project: str | None, rows: list[Any]) -> list[Any]:
    """Add ``handle`` to every row and ``path`` (a viewable copy) to result media.

    The copy-on-write view lives in a deterministic project cache named by digest, so
    re-reading a result never duplicates bytes. Thumbnails, auxiliary files
    and media over 64 MiB get a handle only (``media open <handle> --to``).
    """
    annotated = []
    for row in rows:
        if not isinstance(row, Mapping):
            annotated.append(row)
            continue
        row = dict(row)
        handle = output_handle(row)
        if handle:
            row["handle"] = handle
        digest = str(row.get("digest") or row.get("object_id") or "")
        size = int(row.get("size") or row.get("size_bytes") or 0)
        if (client is not None and project and _DIGEST.fullmatch(digest) and row.get("role") == "result"
                and 0 < size <= VIEW_MAX_BYTES):
            name = _safe_filename(str(row.get("filename") or ""), str(row.get("media_type") or ""), "output")
            try:
                from .media_views import verified_cas_path

                target = _view_root(project) / f"{digest[7:19]}-{name}"
                project_scope = str(row.get("project_id") or project)
                source = verified_cas_path(client, project_scope, digest, size=size)
                row["path"] = str(save_media(client, digest, target, project=project_scope))
                row["path_storage"] = "apfs-clone" if source is not None and os.uname().sysname == "Darwin" else "verified-copy"
            except Exception:  # noqa: BLE001 - a view copy is a convenience, never a failure
                pass
        annotated.append(row)
    return annotated


def link_outputs_to_references(
    client: Any,
    project: str,
    links: list[dict[str, Any]],
    *,
    task_id: str,
    run_id: str,
    output_rows: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Record ``used_as_input`` (always) and ``depicts`` (opt-in) associations.

    Idempotency keys derive from the task, so a re-read never duplicates rows.
    Failures are reported, never raised: the generation already succeeded.
    """
    results: list[dict[str, Any]] = []
    images = [
        row for row in output_rows
        if row.get("role") == "result" and str(row.get("media_type") or "").startswith("image/")
    ]

    def associate(link: Mapping[str, Any], media_id: str, role: str, extra: Mapping[str, Any], key: str) -> None:
        metadata = {"context_task": task_id, "run_id": run_id, "port": link["port"], "as": link["role"], **extra}
        try:
            result = client.references.associate(
                project, link["reference_id"], media_id=media_id, role=role,
                metadata=metadata, idempotency_key=key,
            )
            ok = bool(getattr(result, "ok", True))
            error = None if ok else str(getattr(getattr(result, "error", None), "message", None) or getattr(result, "error", ""))
        except Exception as exc:  # noqa: BLE001 - reported on the result, never fatal
            ok, error = False, f"{type(exc).__name__}: {exc}"
        results.append({"reference": link.get("name"), "role": role, "media_id": media_id, "ok": ok,
                        **({"error": error} if error else {})})

    for link in links:
        # The Runtime keeps one row per (reference, media, role): the first
        # use is recorded; later tasks are on their receipts and depicts rows.
        try:
            current = client.references.show(project, link["reference_id"])
            rows = getattr(current, "data", None) or {}
            recorded = any(
                row.get("media_id") == link["media_id"] and row.get("role") == "used_as_input"
                for row in rows.get("media_references") or [] if isinstance(row, Mapping)
            )
        except Exception:  # noqa: BLE001 - fall through to the write, which reports its own error
            recorded = False
        if recorded:
            results.append({"reference": link.get("name"), "role": "used_as_input", "media_id": link["media_id"],
                            "ok": True, "existing": True})
        else:
            associate(link, str(link["media_id"]), "used_as_input", {}, f"used-{task_id}-{link['reference_id']}")
        if link.get("depicts"):
            for row in images:
                digest = str(row.get("digest") or row.get("object_id"))
                associate(
                    link, digest, "depicts",
                    {"output_port": row.get("output_port"), "ordinal": row.get("ordinal")},
                    f"depicts-{task_id}-{link['reference_id']}-{row.get('output_port')}-{row.get('ordinal')}",
                )
    return results


# -- timeline registry entries --

VIEW_MAX_BYTES = 64 * 1024 * 1024


def resolve_timeline_asset(client: Any, project: str, handle: Any) -> dict[str, Any]:
    """Resolve a media handle to a timeline registry entry.

    Returns ``{"key", "media_id", "content_sha256", "type", "resolution"?,
    "handle", "filename"}``; pass it to ``Clip.swap_asset`` (the ``key`` names
    the registry entry, the other fields are what the registry stores).
    """
    descriptor, lineage = resolve_media_handle(client, project, handle)
    digest = descriptor["digest"]
    reference = lineage.get("reference") if isinstance(lineage.get("reference"), Mapping) else None
    stem = Path(descriptor["filename"]).stem
    key = re.sub(r"[^A-Za-z0-9_-]+", "-", str(reference.get("name") if reference else stem)).strip("-") or digest[7:19]
    entry: dict[str, Any] = {
        "key": key,
        "media_id": digest,
        "content_sha256": digest,
        "type": descriptor["media_type"],
        "handle": lineage.get("handle"),
        "filename": descriptor["filename"],
    }
    pixel = _pixel_provenance(client, lineage, digest)
    if pixel is not None:
        entry["pixel_provenance"] = pixel
    if descriptor["media_type"].startswith("image/") and descriptor["size_bytes"] <= VIEW_MAX_BYTES:
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(client.media.read_bytes(digest))) as image:
                entry["resolution"] = f"{image.width}x{image.height}"
        except Exception:  # noqa: BLE001 - resolution is optional registry metadata
            pass
    return entry


def _pixel_provenance(client: Any, lineage: Mapping[str, Any], digest: str) -> dict[str, Any] | None:
    """Carry a pixel executor's report beside its timeline asset, without coupling checks to runtime I/O."""
    output = lineage.get("output")
    if not isinstance(output, Mapping) or not output.get("task_id"):
        return None
    port = str(output.get("output_port") or "")
    if port not in {"native", "sprites"}:
        return None
    try:
        rows = _rows(_data(client.tasks.list_managed_outputs(str(output["task_id"])), "pixel task outputs"))
        report_row = next((row for row in rows if row.get("output_port") == "report" and row.get("digest")), None)
        if report_row is None:
            return None
        import json

        report = json.loads(client.media.read_bytes(str(report_row["digest"])))
    except Exception:  # noqa: BLE001 - provenance is optional; the media handle itself remains usable
        return None
    run_id = output.get("run_id")
    if port == "sprites":
        ordinal = int(output.get("ordinal") or 0)
        piece = next((row for row in report.get("pieces", []) if int(row.get("index", -1)) == ordinal), None)
        if piece is None:
            return None
        return {
            "executor": "pixel.sprites", "run_id": run_id,
            "lattice": piece.get("lattice"), "drawn_block": piece.get("drawn_block"),
            "outline_integrity": piece.get("outline_integrity"),
        }
    integrity = report.get("outline_integrity")
    if integrity is None:
        import io

        import numpy as np
        from PIL import Image

        from astrid.core.media import load_rgba, outline_integrity  # core must work without the pixel pack

        try:
            image = Image.open(io.BytesIO(client.media.read_bytes(digest))).convert("RGBA")
        except Exception:  # noqa: BLE001 - reading the bytes (runtime or decode) is best-effort optional metadata
            image = None
        if image is not None:
            rgba = np.asarray(image)
            if (rgba[..., 3] < 255).any():
                integrity = outline_integrity(rgba)
    return {"executor": "pixel.snap", "run_id": run_id,
            "lattice": report.get("grid") or report.get("lattice"),
            "outline_integrity": integrity}
