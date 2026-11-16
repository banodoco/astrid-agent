"""Run import binds the persisted transcript to the actual admitted source."""
import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.transcript_run import import_transcription_run


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


class Client:
    def __init__(self):
        source = b'source media'
        self.media_id = digest(source)
        self.output = json.dumps(['hello', [{'value': 'hello', 'start': 0, 'end': 1}],
                                  [{'value': 'hello', 'start': 0, 'end': 1}]]).encode()
        self.output_id = digest(self.output)
        asset = {'binding': 'audio_ref', 'member': 'assets/source.wav',
                 'sha256': self.media_id[7:], 'size': len(source)}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('manifest.json', json.dumps({'schema_version': 2, 'kind': 'vibecomfy_managed_assets',
                                                    'assets': [asset], 'workflow_inputs': {}}))
            z.writestr(asset['member'], source)
        self.archive = archive.getvalue()
        self.archive_id = digest(self.archive)
        self.objects = {self.media_id: source, self.archive_id: self.archive, self.output_id: self.output}
        self.project = {'project_id': 'project-1'}
        self.task = {'run_id': 'run-1', 'project_id': 'project-1', 'capability_id': 'vibecomfy.run',
                     'state': 'succeeded', 'input_object_ids': [self.archive_id],
                     'spec': {'spec': {'input_digests': [{'name': 'managed_assets', 'digest': self.archive_id}],
                                      'invocation_preflight': {'workflow_content_digest': 'expected'}}}}
        self.outputs = [{'output_port': 'vibecomfy_run', 'role': 'result', 'object_id': self.output_id}]
        self.document = None
        self.projects = SimpleNamespace(show=lambda _: DomainResult.success(self.project))
        self.runs = SimpleNamespace(show=lambda _: DomainResult.success({'task_ids': ['task-1']}))
        self.tasks = SimpleNamespace(show=lambda _: DomainResult.success(self.task),
                                     list_managed_outputs=lambda _: DomainResult.success((self.outputs, None)))
        self.media = SimpleNamespace(show=lambda p, d: DomainResult.success({'media_type': 'audio/wav'})
                                     if d == self.media_id else DomainResult.failure(ErrorObject('not_found', 'missing', {})),
                                     read_bytes=lambda d: self.objects[d])
        self.documents = SimpleNamespace(show=self.show, create=self.create, update=self.update)

    def show(self, *args, **kwargs):
        return DomainResult.success(self.document) if self.document else DomainResult.failure(ErrorObject('not_found', 'missing', {}))

    def create(self, *, kind, content, **kwargs):
        self.document = {'kind': kind, 'content': content, 'version': 1}
        return DomainResult.success(self.document)

    def update(self, *args, content, expected_version, **kwargs):
        assert expected_version == self.document['version']
        self.document = {'kind': 'media.transcript', 'content': content, 'version': expected_version + 1}
        return DomainResult.success(self.document)


def test_run_import_uses_admitted_source_and_cas(monkeypatch):
    import astrid.sdk.transcript_run as module
    import astrid.core.media as media
    monkeypatch.setattr(module, '_expected_workflow_digest', lambda _: 'expected')
    monkeypatch.setattr(media, 'ffprobe_duration_seconds', lambda _: 2)
    client = Client()
    assert import_transcription_run(client, 'project', client.media_id, 'run-1') == 'created'
    assert client.document['content']['segments'][0]['words'][0]['text'] == 'hello'
    assert import_transcription_run(client, 'project', client.media_id, 'run-1') == 'unchanged'
    assert import_transcription_run(client, 'project', client.media_id, 'run-1', replace=True, expected_version=1) == 'updated'


@pytest.mark.parametrize('change', ['source', 'workflow', 'output', 'duplicate', 'project'])
def test_run_import_refuses_unproven_identity(monkeypatch, change):
    import astrid.sdk.transcript_run as module
    import astrid.core.media as media
    monkeypatch.setattr(module, '_expected_workflow_digest', lambda _: 'expected')
    monkeypatch.setattr(media, 'ffprobe_duration_seconds', lambda _: 2)
    client = Client()
    if change == 'source':
        client.media_id = digest(b'different')
    elif change == 'workflow':
        client.task['spec']['spec']['invocation_preflight']['workflow_content_digest'] = 'wrong'
    elif change == 'output':
        client.objects[client.output_id] = b'tampered'
    elif change == 'duplicate':
        client.outputs.append(dict(client.outputs[0]))
    else:
        client.task['project_id'] = 'other'
    with pytest.raises(ValueError):
        import_transcription_run(client, 'project', client.media_id, 'run-1')
    assert client.document is None


def test_run_and_from_run_commands_parse():
    from astrid.core.cli.domain_timelines import build_parser
    parser = build_parser(None)
    run = parser.parse_args(['transcripts', 'run', 'sha256:' + 'a' * 64, '--project', 'p', '--placement', 'managed'])
    assert run.placement == 'managed'
    imp = parser.parse_args(['transcripts', 'import', 'sha256:' + 'a' * 64, '--project', 'p', '--from-run', 'run-1'])
    assert imp.file is None and imp.from_run == 'run-1'


def test_run_command_stores_stubbed_generation(monkeypatch):
    import astrid.core.media as media
    import astrid.sdk.transcript_run as module
    import astrid.sdk.generation as generation
    from astrid.core.cli.domain_timelines import build_parser
    import astrid.packs.timeline.cli  # install the product's execution handlers
    monkeypatch.setattr(module, '_expected_workflow_digest', lambda _: 'expected')
    monkeypatch.setattr(media, 'ffprobe_duration_seconds', lambda _: 2)
    client = Client()
    seen = {}
    def audio(**kwargs):
        seen.update(kwargs)
        assert Path(kwargs['audio_ref']).read_bytes() == b'source media'
        return {'kernel_run_id': 'run-1'}
    monkeypatch.setattr(generation, 'generate', SimpleNamespace(audio=audio))
    args = build_parser(client).parse_args(['transcripts', 'run', client.media_id, '--project', 'p',
                                            '--placement', 'managed'])
    assert args.handler(args) == 0
    assert seen['entry'] == 'transcription' and seen['placement'] == 'managed'
    assert client.document['content']['media_id'] == client.media_id


def test_real_entry_reconstructs_admitted_workflow(monkeypatch, tmp_path):
    """The verifier rebuilds the same bundle generation emits for this asset."""
    from astrid.core.generation import entries
    from astrid.packs.vibecomfy.production_engine import _workflow_content_digest
    from astrid.sdk.transcript_run import _expected_workflow_digest
    from vibecomfy.workflow_bundle import emit_bundle

    media = tmp_path / 'source.wav'
    media.write_bytes(b'source media')
    entry = entries.find_entry('transcription')
    workflow, plan, _ = entries.prepare(entries.bundle_path(entry), target_client=None,
        mode='transcribe', binds=entry.get('binds'), defaults=entry.get('defaults'),
        values={'audio_ref': str(media)})
    assert not plan['unbound']
    bundle = tmp_path / 'workflow.py'
    emit_bundle(workflow, bundle, {'operation': 'authored'})
    assert _expected_workflow_digest(media) == _workflow_content_digest(bundle)
