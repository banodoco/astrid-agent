"""Offline private Scene dispatch through existing SDK and Runtime custody."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from astrid.sdk import invocation, timeline_filmstrip
from astrid.sdk.contracts import DomainResult
from astrid.sdk.remote import RemoteTasks
from tests.sdk.test_capture_source_runtime import open_source_runtime


@pytest.fixture
def runtime(tmp_path):
    yield from open_source_runtime(tmp_path)


@pytest.mark.parametrize('working_copy', [False, True])
def test_scene_request_and_sdk_admission_keep_certified_source(runtime, tmp_path, monkeypatch, working_copy):
    # Real projection and certification; no render history, sockets or Node.
    snapshot = runtime.snapshot(working_copy)
    inputs = {'timeline_ref': runtime.timeline_id, 'revision_id': runtime.parent_revision_id}
    if working_copy:
        inputs['authoring_preview'] = deepcopy(runtime.authoring_preview)
    request, context = timeline_filmstrip._scene_only_request(
        inputs, project=runtime.project_id, client=runtime,
    )
    assert request['include_media'] is False
    assert context['capture_snapshot'] == snapshot
    assert timeline_filmstrip._scene_only_authority(context) is not None
    admitted = []

    def create(**kwargs):
        body = {'project': kwargs['project_id'], 'capability_id': kwargs['capability'],
                'input_object_ids': kwargs['input_manifest'], 'spec': kwargs['spec']}
        task = runtime.service.create_task(body)['task']
        admitted.append(task)
        return DomainResult.success({**task, 'task_id': task['id']})

    runtime.tasks = SimpleNamespace(create=create)
    result = invocation._invoke_internal_result(
        'rendering.timeline_visualize', kind='executor', project=runtime.project_id,
        inputs=request, client=runtime, wait=False,
    )
    assert result.ok, result.error
    assert len(admitted) == 1
    task = admitted[0]
    spec = task['spec']['spec']
    authority = spec['authority_context']
    assert authority['mode'] == 'scene_only'
    assert json.loads(spec['inputs']['filmstrip_authority'])['mode'] == 'scene_only'
    assert 'capture_snapshot' not in authority
    ref = authority['capture_snapshot_ref']
    assert ref['source_kind'] == ('working_copy' if working_copy else 'revision')
    assert 'rendered_video' not in spec['inputs']
    if working_copy:
        assert spec['inputs']['authoring_preview'] == inputs['authoring_preview']
    # Use the same host materialization seam as the existing custody suite.
    values = runtime.materialize(task, tmp_path / 'attempt')
    bound = values.timeline_source.read()
    assert bound['snapshot']['config'] == snapshot['config']
    assert bound['ref'] == ref
    assert bound['snapshot']['metadata']['scene_clips'] == snapshot['metadata']['scene_clips']

    # Run executor23db's actual pack writer with the real host-minted source;
    # only Scene evaluation is doubled, so no Node or rendering occurs.
    from astrid.core.rendering import scene_answers
    from astrid.packs.rendering.actions.timeline_visualize import filmstrip_execution

    monkeypatch.setattr('astrid.core.rendering.verified_source.inherited_source',
                        lambda *_: values.timeline_source)
    monkeypatch.setattr(scene_answers, '_prepared_host_frames_at', lambda _t, _a, frames, **kw: [
        {'frame': frame, 'coverage': 'COVERED', 'content': 'CONTENT', 'layers': [], 'findings': []}
        for frame in frames
    ])
    args = SimpleNamespace(out=tmp_path / 'output', timeline=values['timeline'],
                           assets_registry=values['assets_registry'],
                           project_slug=snapshot['project_slug'])
    executed = filmstrip_execution._execute_scene_only(args, {
        'mode': 'scene_only', 'capture_snapshot': bound['snapshot'],
    })
    assert executed['returncode'] == 0
    host_manifest = json.loads((args.out / 'manifest.json').read_text())
    artifact = next(row for row in host_manifest['outputs'] if row['name'] == 'filmstrip_bundle')
    managed = {**artifact, 'output_port': 'filmstrip_bundle',
               'digest': artifact['content_hash'], 'size': artifact['bytes']}
    bundle = (args.out / artifact['path']).read_bytes()
    from astrid.sdk.results import InvocationResult
    result = InvocationResult(
        capability_id='rendering.timeline_visualize', capability_type='executor',
        native_kind='external', ok=True,
        outputs={'managed_outputs': [managed], 'artifacts': [artifact]},
        raw_result={'managed_outputs': [deepcopy(managed)],
                    'outputs': {'artifacts': [deepcopy(artifact)]}},
    )
    reader = SimpleNamespace(media=SimpleNamespace(read_bytes=lambda _: bundle))
    spans = timeline_filmstrip._scene_only_spans(result, context, client=reader)
    assert spans[0].cls == 'COVERED'
    assert spans[-1].end_frame == snapshot['duration_frames']
    assert artifact['provenance']['video_digest'] == context['capture_identity']
    assert artifact['provenance']['timeline_id'] == snapshot['timeline_id']


def test_private_scene_dispatch_does_not_use_public_client_selector(monkeypatch):
    snapshot = {'duration_frames': 1, 'video_digest': 'capture', 'metadata': {
        'capture_identity': 'capture', 'capture_authority': {'parent_revision_id': 'r1'},
        'scene_clips': {'clip': {'address': 'c1.picture'}},
    }}
    context = {'capture_identity': 'capture', 'capture_snapshot': snapshot,
               'video_digest': 'capture', 'video_object_id': 'legacy', 'managed_object_reference': 'legacy'}
    calls = []
    client = SimpleNamespace(invoke_result=lambda *a, **k: pytest.fail('public selector used'))
    monkeypatch.setattr(timeline_filmstrip, 'prepare_filmstrip', lambda *a, **k: context)

    def invoke(capability_id, **kwargs):
        calls.append((capability_id, kwargs))
        return SimpleNamespace(ok=False)

    monkeypatch.setattr(invocation, '_invoke_internal_result', invoke)
    spans, prepared = timeline_filmstrip._invoke_scene_only(
        {'timeline_ref': 'main'}, project='p', client=client,
    )
    assert prepared is context
    assert calls[0][0] == 'rendering.timeline_visualize'
    assert calls[0][1]['client'] is client
    assert calls[0][1]['wait'] and calls[0][1]['read_managed_outputs']
    assert calls[0][1]['inputs']['mode'] == 'scene_only'
    assert spans[0].cls == 'UNAVAILABLE'


def test_normal_admission_finds_private_backend_after_first_catalog_page():
    calls = []
    digest = 'sha256:' + 'a' * 64

    def list_capabilities(*, cursor=None, limit=50):
        calls.append(cursor)
        if cursor is None:
            return [[{'capability_id': f'a{i:02}', 'definition_digest': digest,
                      'status': 'ready'} for i in range(50)], 'next']
        assert cursor == 'next'
        return [[{'capability_id': 'rendering.timeline_visualize',
                  'definition_digest': digest, 'status': 'ready'}], None]

    result = RemoteTasks(SimpleNamespace(list_capabilities=list_capabilities)).prepare_admission(
        project_id='p', capability='rendering.timeline_visualize', spec={'inputs': {}},
    )
    assert result.ok, result.error
    assert result.data['capability_digest'] == digest
    assert calls == [None, 'next']
