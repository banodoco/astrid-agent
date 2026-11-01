"""Deliver settled generation media to the CLI caller, independent of target."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


def _sha256(value: Any) -> str:
    digest = str(value or '').removeprefix('sha256:').lower()
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('generation output has no valid sha256 digest')
    return digest


def deliver_generation(result: Any, *, client: Any, out: str | Path | None, project: str | None = None) -> Any:
    """Return JSON with verified caller-local paths; never expose worker paths.

    SDK results retain durable Runtime references. Only this synchronous CLI
    boundary adds a local export, while its Runtime connection is still open.
    """
    if getattr(result, 'ok', True) is False:
        return result
    payload = result.to_dict() if hasattr(result, 'to_dict') else result
    if not isinstance(payload, dict):
        return payload
    # The tracked H3 parent returns an InvocationResult, whose Runtime-owned
    # associations include its universal manifest and final media separately.
    # Project those references into the same delivery path as image results.
    if payload.get('capability_id') == 'h3_av.transform':
        associations = (payload.get('outputs') or {}).get('managed_outputs') or []
        associations = [row for row in associations if isinstance(row, Mapping)]
        manifests = [row for row in associations if row.get('output_port', row.get('name')) == 'manifest']
        media = [row for row in associations if row.get('role', 'result') == 'result'
                 and str(row.get('media_type') or '').startswith(('video/', 'audio/'))]
        media.sort(key=lambda row: not str(row.get('media_type')).startswith('video/'))
        payload = {**payload, 'task_id': payload.get('kernel_task_id'),
                   'manifest_ref': (manifests[0].get('object_id') or manifests[0].get('digest'))
                   if len(manifests) == 1 else None,
                   'manifest': {'outputs': media}}
    manifest = payload.get('manifest') or {}
    rows = manifest.get('outputs', []) if isinstance(manifest, Mapping) else []
    rows = [row for row in rows if isinstance(row, Mapping)
            and row.get('role', 'result') == 'result']
    managed = bool(payload.get('manifest_ref') or any(row.get('object_id') for row in rows))
    is_h3 = payload.get('capability_id') == 'h3_av.transform'
    if not managed and not is_h3:
        # Older in-process providers already return caller-local media. Route
        # their files through the same export path when an export is requested.
        from .backends.base import GenerationResult
        if not isinstance(result, GenerationResult):
            return payload
        if not rows:
            rows = [{'path': path} for path in payload.get('image_paths', [])]
    try:
        if is_h3 and (len(manifests) != 1 or not payload['manifest_ref']):
            raise ValueError('completed H3 generation has no unique Runtime manifest')
        if is_h3 and not rows:
            raise ValueError('completed H3 generation has no deliverable media')
        return _deliver(payload, rows, client=client, out=out, managed=managed, project=project)
    except Exception as exc:
        from .preflight import generation_error
        raise generation_error(
            ValueError(f'Generation completed, but local output delivery failed: {exc}'),
            code='output_delivery_failed', task_id=payload.get('task_id'),
            details={'manifest_ref': payload.get('manifest_ref')},
        ) from exc


def _deliver(payload, rows, *, client, out, managed, project):
    from astrid.sdk.media_open import DEFAULT_MATERIALIZE_BYTES, _materialize

    destination = Path(out).expanduser().absolute() if out is not None else None
    file_destination = destination is not None and not destination.is_dir() and bool(destination.suffix)
    root = (destination.parent if file_destination else destination) if destination else (
        Path.cwd() / 'outputs' / str(payload.get('task_id') or uuid.uuid4().hex))
    root.mkdir(parents=True, exist_ok=True)
    project = next((row['project_id'] for row in rows if row.get('project_id')), project)
    if managed and project is None:
        from astrid.sdk.generation import _resolve_invoke_destination
        _, project = _resolve_invoke_destination(client=client, out=out, project=None, project_root=None)
    paths = []
    with TemporaryDirectory(prefix='.astrid-delivery-', dir=root) as scratch:
        def open_verified(reference, digest, *, size=None, slot):
            # Reuse the product's scoped resolver, integrity checks and CAS
            # clone/copy path, including for the immutable producer manifest.
            opened = client.media.open(project, reference, materialize=True,
                cache_root=Path(scratch) / slot, preview_bytes=0,
                max_bytes=size if isinstance(size, int) and size > 0 else DEFAULT_MATERIALIZE_BYTES)
            if not opened.ok:
                raise ValueError(opened.error.message)
            path = Path(opened.data['local_path'])
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError(f'sha256 mismatch for Runtime object {reference}')
            if size is not None and path.stat().st_size != size:
                raise ValueError(f'byte count mismatch for Runtime object {reference}')
            return path

        sealed_outputs = None
        if payload.get('manifest_ref'):
            reference = payload['manifest_ref']
            manifest_path = open_verified(reference, _sha256(reference), slot='manifest')
            sealed = json.loads(manifest_path.read_bytes())
            sealed_outputs = sealed.get('outputs')
            if not isinstance(sealed_outputs, list):
                raise ValueError('generation manifest has no output inventory')
            if not rows:
                rows = [row for row in sealed_outputs if isinstance(row, Mapping)]
        if not rows:
            raise ValueError('completed generation has no deliverable media')

        staged = []
        used = set()
        for index, row in enumerate(rows):
            reference = row.get('object_id') or row.get('digest')
            digest_value = row.get('digest') or row.get('sha256') or row.get('content_hash') or reference
            digest = _sha256(digest_value) if digest_value else None
            size = row.get('size', row.get('bytes'))
            if managed:
                digest = _sha256(digest_value)
                if sealed_outputs is not None and not any(
                    isinstance(item, Mapping)
                    and _sha256(item.get('sha256') or item.get('digest') or item.get('content_hash') or item.get('object_id')) == digest
                    and item.get('bytes', item.get('size')) == size
                    and (payload.get('capability_id') != 'h3_av.transform'
                         or item.get('name') == row.get('output_port', row.get('name')))
                    for item in sealed_outputs
                ):
                    raise ValueError('Runtime output is absent from the generation manifest')
                temporary = open_verified(reference or 'sha256:' + digest, digest,
                                          size=size, slot=str(index))
            else:
                source = Path(row['path'])
                data = source.read_bytes()
                if digest and hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError('sha256 mismatch for local generation output')
                local_path, _ = _materialize(data, filename=source.name,
                    media_type=row.get('media_type', 'application/octet-stream'),
                    cache_root=Path(scratch) / str(index), source=source)
                temporary = Path(local_path)
            name = Path(str(row.get('filename') or row.get('path') or temporary.name)).name
            if name in {'', '.', '..'}:
                name = f'output-{index}'
            if file_destination:
                suffix = Path(name).suffix or destination.suffix
                path = destination if index == 0 else destination.with_name(
                    f'{destination.stem}-{index + 1}{suffix}')
            else:
                path = root / name
                while path in used:
                    path = path.with_name(f'{index + 1}-{path.name}')
            used.add(path)
            staged.append((temporary, path))
            paths.append(str(path))
        # No requested file is replaced until every object passes verification.
        for temporary, path in staged:
            os.replace(temporary, path)
    return {**payload, 'image_paths': paths, 'run_dir': str(root)}
