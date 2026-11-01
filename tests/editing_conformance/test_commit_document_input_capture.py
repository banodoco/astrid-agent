"""Hydrated sources enter sequential closure manifests at consumption."""
from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _hydrate, document_from_bundle
from astrid.sdk.timeline.native_word_anchor import capture_anchor
from tests.editing_conformance.test_native_word_anchor_vectors import fixture
from tests.sdk import test_selected_param_neutral_stage as param_stage

configured_project_pack = param_stage.configured_project_pack
selected_runtime_env = param_stage.selected_runtime_env
retained_pack = param_stage.retained_pack
mounted_pack = param_stage.mounted_pack


def inputs():
    bundle, pin, content = fixture()
    verified = bundle['source_mapping'].pop('pinned_documents')[0]
    choice = {'source': 'occurrence:occ-a/clip:voice', 'document': pin,
              'word_ids': [content['segments'][0]['words'][0]['id']]}
    captured = copy.deepcopy(bundle)
    captured['source_mapping']['pinned_documents'] = [verified]
    track = capture_track_create(captured, 'parent', {'id': 'extra', 'kind': 'audio'})
    staged = execute(captured, [track]).bundle
    anchor = capture_anchor(staged, 'parent/clip:clip-a', 'on', 'again', choice)
    return bundle, verified, [track, anchor]


class Sources:
    def __init__(self, verified):
        self.verified = verified

    def transcript_source(self, pin):
        return copy.deepcopy(self.verified)


@pytest.mark.parametrize('separate_steps', [False, True])
def test_verified_transcript_is_added_only_at_its_consuming_command(separate_steps):
    bundle, verified, calls = inputs()
    immutable = copy.deepcopy(bundle['source_mapping']['_runtime_document'])
    _hydrate(Sources(verified), bundle, calls)
    assert document_from_bundle(bundle) == immutable
    request = {'reads': [], 'label': 'Causal transcript'}
    if separate_steps:
        request['steps'] = [{'id': str(i), 'label': 'Step', 'intent': '', 'tags': [],
                             'command_ordinals': [i], 'patch': []} for i in range(2)]
    plan = execute(bundle, calls, commit_request=request)
    first, consuming = plan._sequential_execution
    assert first['input'] == immutable
    assert first['output'] == consuming['input']
    assert first['output']['dependency_manifest']['documents'] == []
    assert consuming['output']['dependency_manifest']['documents'] == [verified['pin']]
    assert document_from_bundle(plan.bundle) == consuming['output']
    assert any(dep['key'] == 'anchor.transcript' and dep['pin'] == verified['pin']
               for dep in plan.records[1]['dependencies'])
    assert all(dep['kind'] != 'transcript' for dep in plan.records[0]['dependencies'])
    assert bundle['source_mapping']['_runtime_document'] == immutable


@pytest.mark.parametrize('damage', ['digest', 'project', 'version'])
def test_hydration_still_rejects_forged_or_foreign_transcript_before_staging(damage):
    bundle, verified, calls = inputs()
    original = copy.deepcopy(bundle)
    if damage == 'digest':
        verified['document']['content']['segments'][0]['text'] = 'forged'
    elif damage == 'project':
        verified['document']['project_id'] = 'foreign'
    else:
        verified['document']['version'] += 1
    with pytest.raises(AuthoringBundleError, match='exact scoped pin'):
        _hydrate(Sources(verified), bundle, calls)
    assert bundle == original


@pytest.mark.parametrize('source', ['requested', 'retained', 'unconsumed'])
def test_hydration_replaces_caller_manifest_with_authenticated_closure(source):
    bundle, verified, calls = inputs()
    document = bundle['source_mapping']['_runtime_document']
    if source == 'retained':
        document['dependency_manifest']['documents'] = [copy.deepcopy(verified['pin'])]
    elif source == 'unconsumed':
        calls = calls[:1]
    immutable = copy.deepcopy(document)
    forged = {'document_id': 'transcript:foreign-project:media:source',
              'version': 99, 'content_digest': 'sha256:' + 'f' * 64}
    bundle['source_mapping']['_copy_manifest'] = {
        **copy.deepcopy(document['dependency_manifest']), 'documents': [forged],
        'media': [{'media_id': 'sha256:' + 'f' * 64,
                   'content_digest': 'sha256:' + 'f' * 64}]}
    _hydrate(Sources(verified), bundle, calls)
    assert document_from_bundle(bundle) == immutable
    plan = execute(bundle, calls, commit_request={'reads': [], 'label': 'Manifest boundary'})
    result = document_from_bundle(plan.bundle)
    expected = [] if source == 'unconsumed' else [verified['pin']]
    assert result['dependency_manifest']['documents'] == expected
    assert result['dependency_manifest']['media'] == immutable['dependency_manifest']['media']
    assert bundle['source_mapping']['_runtime_document'] == immutable


@pytest.mark.parametrize('damage', ['digest', 'project', 'version'])
def test_forged_manifest_cannot_bypass_source_validation_or_mutate_input(damage):
    bundle, verified, calls = inputs()
    bundle['source_mapping']['_copy_manifest'] = {
        **copy.deepcopy(bundle['source_mapping']['_runtime_document']['dependency_manifest']),
        'documents': [copy.deepcopy(verified['pin'])]}
    original = copy.deepcopy(bundle)
    if damage == 'digest':
        verified['document']['content']['segments'][0]['text'] = 'forged'
    elif damage == 'project':
        verified['document']['project_id'] = 'foreign'
    else:
        verified['document']['version'] += 1
    with pytest.raises(AuthoringBundleError, match='exact scoped pin'):
        _hydrate(Sources(verified), bundle, calls)
    assert bundle == original


@pytest.mark.integration
@pytest.mark.parametrize('newly_admitted', [False, True])
@pytest.mark.parametrize('damage', [None, 'input', 'binding'])
def test_mounted_source_enters_only_its_consuming_output(mounted_pack, monkeypatch,
                                                       newly_admitted, damage):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    from astrid.core.element import project_packs
    from astrid.sdk.timeline._command_state import State
    from astrid.sdk.timeline.commit import _digest, create_runtime_commit_service

    case = mounted_pack
    element, pin = case.selected, case.pin
    if newly_admitted:
        component = case.path.parent / 'component.tsx'
        component.write_text(component.read_text() + '\n// New selected revision\n')
        admitted = project_packs.admit_project_pack('realm', case.project)
        descriptor = admitted['elements'][0]['descriptor']
        element = {key: descriptor[key] for key in ('id', 'kind', 'packId', 'revision')}
        document = case.service.get_document(case.project, case.pin['document_id'])
        pin = {'document_id': document['document_id'], 'version': document['version'],
               'content_digest': _digest(document['content'])}
        assert element != case.selected and pin != case.pin
    calls = [capture_track_create(case.bundle(), 'parent', {'id': 'extra', 'kind': 'audio'}),
             *param_stage._sequence(element, {'schema_source': pin})]
    body = case.request(calls)
    body.update(label='Consume selected source', merge='fields', reads=[{
        'kind': 'field', 'ref': 'parent', 'path': ['config', 'clips', 'id:effect', 'hold'],
        'state': {'state': 'present', 'value': 2}}], steps=[{
            'id': 'source', 'label': 'Consume source', 'intent': '', 'tags': [],
            'command_ordinals': list(range(len(calls))), 'patch': [], 'reads': []}])
    original = copy.deepcopy(body)
    before = case.head()
    sealed = case.bundle()['source_mapping']['_runtime_document']
    stored = tuple(case.service.store.conn.iterdump())
    deliver = RequestCommitPort.capture_sequential_execution
    packets = []

    def receive(port, packet):
        packets.append(copy.deepcopy(packet))
        packet = copy.deepcopy(packet)
        if damage == 'input':
            packet['original']['commands'][0]['input']['parent_revision']['revision_id'] = 'foreign'
        elif damage == 'binding':
            binding = next(item for item in packet['original']['records']['commands'][1]['bindings']
                           if item['key'] == 'params.element')
            binding['revision_id'] = 'foreign'
        return deliver(port, packet)

    with monkeypatch.context() as injected:
        injected.setattr(RequestCommitPort, 'capture_sequential_execution', receive)
        if damage is not None:
            with pytest.raises(RuntimeErrorBase):
                case.submit(body, 'source-boundary')
            assert case.head() == before
            assert tuple(case.service.store.conn.iterdump()) == stored
        else:
            accepted = case.submit(body, 'source-boundary')
            assert accepted['data']['status'] == 'accepted'
    assert body == original and len(packets) == 1
    for side in ('original', 'landing'):
        execution = packets[0][side]
        first, consuming, *later = execution['commands']
        assert first['input'] == sealed
        assert first['output'] == consuming['input']
        prior_pins = sealed['dependency_manifest']['documents']
        assert first['output']['dependency_manifest']['documents'] == prior_pins
        assert (pin in prior_pins) is not newly_admitted
        assert pin in consuming['output']['dependency_manifest']['documents']
        assert consuming['output'] == later[0]['input']
        for left, right in zip(execution['commands'], execution['commands'][1:]):
            assert left['output'] == right['input']
        record = execution['records']['commands'][1]
        assert any(dep['key'] == 'params.schema' and dep['pin'] == pin
                   for dep in record['dependencies'])
        for key in ('scalar.target', 'params.element'):
            binding, = [item for item in record['bindings'] if item['key'] == key]
            assert binding['revision_id'] == consuming['input']['parent_revision']['revision_id']
            assert binding['content_digest'] == consuming['input']['parent_revision']['content_digest']
    if damage is None:
        assert case.bundle()['source_mapping']['_runtime_document'] == later[-1]['output']
        case.service.close()
        case.service = RuntimeService(case.root)
        case.service.register_timeline_commit_provider(create_runtime_commit_service())
        followup = case.request([param_stage.selected._params({'schema_source': pin}, {'seed': 9})])
        followup.update(label='PARAM after restart', merge='fields', reads=[])
        assert case.submit(followup, 'source-after-restart')['data']['status'] == 'accepted'
        assert State(case.bundle()).entity(param_stage.REF)['params']['seed'] == 9
