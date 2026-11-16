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

    # Do not let a user entry with the same name define the verifier's authority.
    import yaml
    pack = Path(__file__).resolve().parents[1] / 'packs' / 'generation'
    manifest = pack / 'extensions' / 'vibecomfy.workflows' / 'transcription.yaml'
    declaration = yaml.safe_load(manifest.read_text(encoding='utf-8'))
    candidates = [row for row in declaration.get('entries', []) if row.get('id') == 'transcription']
    if len(candidates) != 1:
        raise ValueError('the installed transcription entry is unavailable')
    entry = dict(candidates[0])
    entry['bundle'] = str((manifest.parent / declaration['ready_root'] / entry['bundle']).resolve())
    if (entry.get('kind') != 'audio' or 'transcribe' not in entry.get('modes', [])
            or not Path(entry['bundle']).is_relative_to(pack.resolve())):
        raise ValueError('the installed transcription entry is invalid')
    workflow, plan, assets = entries.prepare(entries.bundle_path(entry), target_client=None,
        mode='transcribe', binds=entry.get('binds'), defaults=entry.get('defaults'),
        values={'audio_ref': str(source)})
    if plan['unbound'] or set(assets) != {'audio_ref'}:
        raise ValueError('the transcription entry has unexpected input bindings')
    with tempfile.TemporaryDirectory(prefix='astrid-transcript-verify-') as tmp:
        path = Path(tmp) / 'workflow.py'
        emit_bundle(workflow, path, {'operation': 'authored'})
        # Generation retains the original source evidence as the third member.
        original = entries.bundle_path(entry)
        original_source = original.with_name('source.json')
        (path.parent / 'source.json').write_bytes(
            (original_source if original_source.is_file() else original).read_bytes())
        return _workflow_content_digest(path)


def _admitted_input(task: Mapping[str, Any], spec: Mapping[str, Any], name: str, *, required: bool = True) -> str | None:
    from astrid.core.execution.managed_inputs import managed_file_digest
    inputs = spec.get('input_digests')
    if not isinstance(inputs, list):
        raise ValueError('run task has no admitted input witnesses')
    rows = [row.get('digest') for row in inputs if isinstance(row, Mapping) and row.get('name') == name]
    descriptors = spec.get('inputs')
    value = descriptors.get(name) if isinstance(descriptors, Mapping) else None
    if not rows and value is None and not required:
        return None
    if len(rows) != 1 or value is None:
        raise ValueError(f'run task has ambiguous {name} input')
    try:
        actual = managed_file_digest(value, name)
    except ValueError as exc:
        raise ValueError(f'run task has invalid {name} input') from exc
    if rows[0] != actual or actual not in (task.get('input_object_ids') or []):
        raise ValueError(f'run task {name} input witness does not match its admitted descriptor')
    return actual


def _admitted_workflow_digest(client: Any, task: Mapping[str, Any], spec: Mapping[str, Any]) -> str:
    from astrid.packs.vibecomfy.production_engine import _workflow_content_digest
    names = {'python': 'workflow.py', 'companion': 'workflow.vibe.json', 'source': 'source.json'}
    with tempfile.TemporaryDirectory(prefix='astrid-transcript-workflow-') as tmp:
        for name, filename in names.items():
            digest = _admitted_input(task, spec, name, required=name != 'source')
            if digest is not None:
                (Path(tmp) / filename).write_bytes(_bytes(client, digest))
        return _workflow_content_digest(Path(tmp) / 'workflow.py')


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
    archive_id = _admitted_input(task, spec, 'managed_assets')
    archive = read_archive_bytes(_bytes(client, archive_id))
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
        expected = _expected_workflow_digest(path)
        admitted = _admitted_workflow_digest(client, task, spec)
        if (not isinstance(receipt, Mapping) or receipt.get('workflow_content_digest') != expected
                or admitted != expected):
            raise ValueError('run workflow does not match the installed transcription workflow')
        duration = ffprobe_duration_seconds(path)
    output_result = _data(client.tasks.list_managed_outputs(task_id), 'task outputs')
    from .pagination import page_pair
    page = page_pair(output_result)
    if page is not None:
        if page[1] is not None:
            raise ValueError('transcription outputs span multiple pages')
        output_result = page[0]
    if not isinstance(output_result, list):
        raise ValueError('transcription output page is malformed')
    outputs = [row for row in output_result if isinstance(row, Mapping)
               and row.get('output_port') == 'vibecomfy_run' and row.get('role') == 'result']
    if len(outputs) != 1:
        raise ValueError('transcription run must have exactly one result output')
    output_id = outputs[0].get('digest') or outputs[0].get('object_id')
    result = json.loads(_bytes(client, output_id))
    return import_whisper_transcript(client, project, digest, whisper_source_id('large-v3-turbo.pt'),
                                     result, media_duration=duration, replace=replace,
                                     expected_version=expected_version)
