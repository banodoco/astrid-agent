"""Import a transcription only from its admitted workflow and source bytes."""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from .whisper_transcript import import_whisper_transcript, whisper_source_id


def _data(result: Any, label: str) -> Any:
    if hasattr(result, 'ok'):
        if not result.ok:
            raise ValueError(f'{label}: {result.error}')
        return result.data
    return result


def _bytes(client: Any, digest: str) -> bytes:
    if not isinstance(digest, str) or len(digest) != 71 or not digest.startswith('sha256:'):
        raise ValueError('invalid managed object digest')
    raw = client.media.read_bytes(digest)
    if 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('managed object bytes do not match their digest')
    return raw


def _expected_workflow_digest(source: Path) -> str:
    """Recreate exactly the graph the named entry admits for these source bytes."""
    from astrid.core.generation import entries
    from astrid.packs.vibecomfy.production_engine import _workflow_content_digest
    from vibecomfy.workflow_bundle import emit_bundle

    entry = entries.find_entry('transcription')
    if not entry or entry.get('kind') != 'audio' or 'transcribe' not in entry.get('modes', []):
        raise ValueError('the installed transcription entry is unavailable')
    workflow, plan, assets = entries.prepare(entries.bundle_path(entry), target_client=None,
        mode='transcribe', binds=entry.get('binds'), defaults=entry.get('defaults'),
        values={'audio_ref': str(source)})
    if plan['unbound'] or set(assets) != {'audio_ref'}:
        raise ValueError('the transcription entry has unexpected input bindings')
    with tempfile.TemporaryDirectory(prefix='astrid-transcript-verify-') as tmp:
        path = Path(tmp) / 'workflow.py'
        emit_bundle(workflow, path, {'operation': 'authored'})
        return _workflow_content_digest(path)


def import_transcription_run(client: Any, project: str, media_id: str, run_id: str, *,
                             replace: bool = False, expected_version: int | None = None) -> str:
    from astrid.packs.vibecomfy.asset_manifest import read_archive_bytes
    from astrid.sdk.media_handles import resolve_media_handle
    from astrid.core.media import ffprobe_duration_seconds

    descriptor, _ = resolve_media_handle(client, project, media_id)
    digest = descriptor['digest']
    project_row = _data(client.projects.show(project), 'project')
    project_id = project_row.get('project_id') or project_row.get('id')
    run = _data(client.runs.show(run_id), 'run')
    task_ids = run.get('task_ids') if isinstance(run, Mapping) else None
    if not isinstance(task_ids, list) or len(task_ids) != 1:
        raise ValueError('transcription run must contain exactly one task')
    task_id = task_ids[0]
    task = _data(client.tasks.show(task_id), 'task')
    if (not isinstance(task, Mapping) or task.get('run_id') != run_id
            or task.get('project_id') != project_id or task.get('capability_id') != 'vibecomfy.run'
            or task.get('state') not in {'completed', 'succeeded'}):
        raise ValueError('run task is not a completed project transcription task')
    envelope = task.get('spec')
    spec = envelope.get('spec') if isinstance(envelope, Mapping) else None
    if not isinstance(spec, Mapping):
        raise ValueError('run task has no admitted execution spec')
    input_digests = spec.get('input_digests')
    matches = [r.get('digest') for r in input_digests or []
               if isinstance(r, Mapping) and r.get('name') == 'managed_assets']
    if len(matches) != 1 or matches[0] not in (task.get('input_object_ids') or []):
        raise ValueError('run task has no unambiguous admitted asset archive')
    archive = read_archive_bytes(_bytes(client, matches[0]))
    assets = archive.manifest['assets']
    if len(assets) != 1 or assets[0]['binding'] != 'audio_ref':
        raise ValueError('run task has ambiguous source media')
    asset = assets[0]
    if 'sha256:' + asset['sha256'] != digest:
        raise ValueError('run source media digest does not match requested media')
    payload = archive.members[asset['member']]
    with tempfile.TemporaryDirectory(prefix='astrid-transcript-source-') as tmp:
        # The member is generated from the filename; it has already passed archive path validation.
        path = Path(tmp) / Path(asset['member']).name
        path.write_bytes(payload)
        receipt = spec.get('invocation_preflight')
        if not isinstance(receipt, Mapping) or receipt.get('workflow_content_digest') != _expected_workflow_digest(path):
            raise ValueError('run workflow does not match the installed transcription workflow')
        duration = ffprobe_duration_seconds(path)
    output_result = _data(client.tasks.list_managed_outputs(task_id), 'task outputs')
    if isinstance(output_result, tuple):
        output_result = output_result[0]
    outputs = [row for row in output_result if isinstance(row, Mapping)
               and row.get('output_port') == 'vibecomfy_run' and row.get('role') == 'result']
    if len(outputs) != 1:
        raise ValueError('transcription run must have exactly one result output')
    output_id = outputs[0].get('digest') or outputs[0].get('object_id')
    result = json.loads(_bytes(client, output_id))
    return import_whisper_transcript(client, project, digest, whisper_source_id('large-v3-turbo.pt'),
                                     result, media_duration=duration, replace=replace,
                                     expected_version=expected_version)
