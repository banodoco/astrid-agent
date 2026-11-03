"""Authenticated inverse projection through the production commit/capture path."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, present
from astrid.sdk.timeline._doc_patch import Closure
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
from astrid.sdk.timeline.native_timeline_duplicate import copy_command
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.editing_conformance.test_provider_step_undo_v2 import undo
from tests.editing_conformance.test_recipe_occurrence_create import empty_bundle
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedPort, DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import document_fixture


@pytest.mark.parametrize('dry_run', [False, True])
def test_single_inverse_captures_transient_roots_and_exact_parent(monkeypatch, dry_run):
    realm = DetachedRealm()
    baseline = copy.deepcopy(realm.documents[realm.head['revision_id']])
    create = capture_occurrence_create(bundle_from_document(baseline), name='New', track='picture',
        on=10, duration=2, occurrence_id='new-occ', shot_id='new-shot')
    edit = patch(change(['unknown'], ABSENT, [3], ref='internal:film'))
    created = submit(realm, create, edit, reads=[],
                     steps=[step('create', 0, create), step('edit', 1, edit)])
    captures = []
    monkeypatch.setattr(DetachedPort, 'capture_sequential_execution',
                        lambda port, value: captures.append(copy.deepcopy(value)), raising=False)
    before = copy.deepcopy((realm.head, realm.documents, realm.entries))
    result = submit(realm, undo(created['receipt']['receipt_id']), reads=[], dry_run=dry_run)
    assert result['data']['canonical_document'] == baseline
    assert len(captures) == 1
    capture = captures[0]
    assert set(capture) == {'schema_version', 'context', 'original', 'landing',
                            'replay_bindings', 'prepared_digest'}
    assert capture['original'] == capture['landing']
    for execution in (capture['original'], capture['landing']):
        command, = execution['commands']
        assert command['input'] == created['data']['canonical_document']
        assert command['output'] == baseline
        record, = execution['records']['commands']
        stages = [binding['boundary'] for binding in record['bindings']
                  if binding['key'] == 'inverse.stage']
        assert len(stages) == 2
        assert stages[0]['effect_range'][0] == stages[0]['native_write_range'][0] == 0
        assert stages[0]['effect_range'][1] == stages[1]['effect_range'][0]
        assert stages[0]['native_write_range'][1] == stages[1]['native_write_range'][0]
        assert stages[-1]['native_write_range'][1] == len(record['writes'])
        effects = execution['records']['steps'][0]['patch']
        assert stages[-1]['effect_range'][1] == len(effects)
        inputs = Closure(bundle_from_document(command['input']))
        for binding in record['bindings']:
            if binding['key'] == 'commit.materialization':
                assert binding['physical_ref'] in inputs.images
                assert binding['successor'] in Closure(bundle_from_document(command['output'])).images
        final_parent = [binding for binding in record['bindings']
                        if binding['key'] == 'commit.effect' and binding['physical_ref'] == 'parent'
                        and binding['boundary']['side'] == 'after'
                        and binding['effect_ordinal'] >= stages[-1]['effect_range'][0]]
        assert final_parent
        assert all(binding['revision_id'] == baseline['parent_revision']['revision_id']
                   for binding in final_parent)
    if dry_run:
        assert result['receipt'] is None
        assert (realm.head, realm.documents, realm.entries) == before
    else:
        assert capture['prepared_digest'] == _digest(realm.prepared[-1])
        redone = submit(realm, undo(result['receipt']['receipt_id']), reads=[])
        assert redone['data']['canonical_document'] == created['data']['canonical_document']


def test_existing_child_multi_stage_undo_redo_preserves_lineage(monkeypatch):
    realm = DetachedRealm(document_fixture(shared=False))
    baseline = copy.deepcopy(realm.documents[realm.head['revision_id']])
    first = patch(change(['unknown'], present([1, 2]), [3], ref='internal-revision:internal-1'))
    plan = execute(bundle_from_document(baseline), [first], commit_request={'reads': []})
    staged = Closure(plan.bundle).resolve('internal:chapter-1')
    second = patch(change(['unknown'], present([3]), [4], ref=staged))
    created = submit(realm, first, second, reads=[], steps=[step('first', 0, first), step('second', 1, second)])
    captures = []
    monkeypatch.setattr(DetachedPort, 'capture_sequential_execution',
                        lambda port, value: captures.append(copy.deepcopy(value)), raising=False)
    undone = submit(realm, undo(created['receipt']['receipt_id']), reads=[])
    assert undone['data']['canonical_document'] == baseline
    capture, = captures
    for execution in (capture['original'], capture['landing']):
        command, = execution['commands']
        before, after = (Closure(bundle_from_document(command[side])) for side in ('input', 'output'))
        for binding in execution['records']['commands'][0]['bindings']:
            if binding['key'] == 'commit.materialization':
                assert binding['physical_ref'] in before.images
                assert binding['successor'] in after.images
    redone = submit(realm, undo(undone['receipt']['receipt_id']), reads=[])
    assert redone['data']['canonical_document'] == created['data']['canonical_document']


@pytest.fixture
def retained_copy(monkeypatch):
    baseline = copy.deepcopy(empty_bundle()['source_mapping']['_runtime_document'])
    baseline.update(project_id='project-1', timeline_id='film')
    baseline['parent_revision']['revision_id'] = 'authenticated-baseline'
    source = document_fixture(shared=False)
    source['timeline_id'] = 'source'
    for row in source['parent_revision']['payload']['occurrences']:
        row['source_offset'] = 0
    source['parent_revision']['content_digest'] = _digest(source['parent_revision']['payload'])
    baseline['dependency_manifest']['media'] = copy.deepcopy(source['dependency_manifest']['media'])
    realm = DetachedRealm(baseline)
    document = {'document_id': 'retained-config', 'project_id': 'project-1', 'kind': 'copy.config',
                'version': 1, 'content': {'opaque': [None, False, 2]}}
    pin = {'document_id': document['document_id'], 'version': 1, 'content_digest': _digest(document['content'])}
    source['dependency_manifest']['documents'] = [pin]
    realm.live_sources[_digest(pin)] = {'pin': pin, 'document': document}
    ref = {'project_id': source['project_id'], 'timeline_id': source['timeline_id'],
           'revision_id': source['parent_revision']['revision_id'], 'publication_id': 'source-publication',
           'content_digest': source['parent_revision']['content_digest']}
    original = DetachedPort.revision_closure

    def load(port, revision):
        if isinstance(revision, dict):
            assert revision == ref
            return copy.deepcopy(source)
        return original(port, revision)

    monkeypatch.setattr(DetachedPort, 'revision_closure', load)
    monkeypatch.setattr(DetachedPort, 'raw_media_info', lambda port, pin: copy.deepcopy(pin), raising=False)
    command = copy_command(ref, {'chapter-shot': 'copied'}, {'occ-a': 'copy-a'})
    created = submit(realm, command, reads=[],
                     steps=[step('copy', 0, command, reads=[{'kind': 'scope', 'ref': 'timeline', 'path': []}])])
    return realm, baseline, created


@pytest.mark.parametrize('dry_run', [False, True])
def test_final_stage_uses_authenticated_parent_after_retained_pin_removal(retained_copy, monkeypatch, dry_run):
    realm, baseline, created = retained_copy
    captures = []
    monkeypatch.setattr(DetachedPort, 'capture_sequential_execution',
                        lambda port, value: captures.append(copy.deepcopy(value)), raising=False)
    result = submit(realm, undo(created['receipt']['receipt_id']), reads=[], dry_run=dry_run)
    assert result['data']['canonical_document'] == baseline
    capture, = captures
    assert capture['landing']['commands'][0]['output'] == baseline
    records = capture['landing']['records']
    record, = records['commands']
    stages = [binding['boundary'] for binding in record['bindings'] if binding['key'] == 'inverse.stage']
    start, end = stages[-1]['effect_range']
    assert end == len(records['steps'][0]['patch'])
    parents = [binding for binding in record['bindings'] if binding['key'] == 'commit.effect'
               and binding['physical_ref'] == 'parent' and binding['boundary']['side'] == 'after'
               and start <= binding['effect_ordinal'] < end]
    assert parents
    assert all(binding['revision_id'] == 'authenticated-baseline' for binding in parents)
    assert all(binding['content_digest'] == baseline['parent_revision']['content_digest'] for binding in parents)


@pytest.mark.parametrize('damage', ['input', 'ref', 'history', 'boundary', 'owner', 'digest', 'range', 'aba'])
def test_inverse_projection_keeps_source_and_history_guards(damage):
    realm = DetachedRealm(document_fixture(shared=False))
    created = submit(realm, patch(change(['name'], present('Chapter One'), 'Edited',
                                       ref='shot:chapter-shot')), reads=[])
    entry = realm.entries[-1]
    record = entry['records']['commands'][0]
    if damage == 'input':
        record['writes'][0]['before'] = present('forged')
    elif damage == 'ref':
        record['writes'][0]['ref'] = 'occurrence:foreign/shot'
    elif damage == 'history':
        entry['previous']['publication_id'] = 'missing'
    elif damage == 'boundary':
        next(binding for binding in record['bindings'] if binding['key'] == 'commit.materialization')[
            'boundary']['evaluated_head']['publication_id'] = 'foreign'
    elif damage == 'owner':
        next(binding for binding in record['bindings'] if binding['key'] == 'commit.materialization')[
            'owner'] = 'occurrence:foreign/shot'
    elif damage == 'digest':
        next(binding for binding in record['bindings'] if binding['key'] == 'commit.materialization')[
            'content_digest'] = 'sha256:' + '0' * 64
    elif damage == 'range':
        created = submit(realm, undo(created['receipt']['receipt_id']), reads=[])
        record = realm.entries[-1]['records']['commands'][0]
        binding = next(binding for binding in record['bindings'] if binding['key'] == 'inverse.stage')
        binding['boundary']['native_write_range'] = [0, 100]
    else:
        submit(realm, patch(change(['name'], present('Edited'), 'Other', ref='shot:chapter-shot')), reads=[])
        submit(realm, patch(change(['name'], present('Other'), 'Edited', ref='shot:chapter-shot')), reads=[])
    before = copy.deepcopy((realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared))
    with pytest.raises(CommitRejected):
        submit(realm, undo(created['receipt']['receipt_id']), reads=[])
    assert (realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared) == before


@pytest.mark.parametrize('damage', ['missing', 'ref', 'owner', 'digest', 'boundary', 'range', 'policy', 'history'])
def test_nested_inverse_requires_exact_stage_image_authority(damage):
    realm = DetachedRealm(document_fixture(shared=False))
    created = submit(realm, patch(change(['unknown'], present([1, 2]), [3],
                                       ref='internal-revision:internal-1')), reads=[])
    undone = submit(realm, undo(created['receipt']['receipt_id']), reads=[])
    record = realm.entries[-1]['records']['commands'][0]
    binding = next(item for item in record['bindings']
                   if item['key'] == 'commit.effect' and item['physical_ref'].startswith('internal-revision:')
                   and item['boundary']['side'] == 'before')
    if damage == 'missing':
        record['bindings'].remove(binding)
    elif damage == 'ref':
        binding['ref'] = 'internal-revision:foreign'
    elif damage == 'owner':
        binding['owners'] = ['occurrence:foreign/shot']
    elif damage == 'digest':
        binding['content_digest'] = 'sha256:' + '0' * 64
    elif damage == 'boundary':
        binding['boundary']['evaluated_head']['publication_id'] = 'foreign'
    elif damage == 'policy':
        binding['policy'] = 'follow'
    elif damage == 'range':
        next(item for item in record['bindings'] if item['key'] == 'inverse.stage')[
            'boundary']['effect_range'] = [0, 999]
    else:
        record['command']['args']['publications'] = ['missing-source']
    before = copy.deepcopy((realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared))
    with pytest.raises(CommitRejected) as error:
        submit(realm, undo(undone['receipt']['receipt_id']), reads=[])
    assert error.value.failure['details']['reason'] == 'history_unavailable'
    assert (realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared) == before
