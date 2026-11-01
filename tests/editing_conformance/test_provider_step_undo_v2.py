"""Step inverse dispatch, physical history and retry guards through the provider."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, present
from astrid.sdk.timeline.commit import _digest, _manifest, bundle_from_document
from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def undo(publication, *steps):
    call = inverse([publication])
    if steps:
        call['args']['steps'] = {publication: list(steps)}
    return call


def parent(result):
    return result['data']['canonical_document']['parent_revision']['payload']


def test_two_patch_sequential_reads_select_last_step_and_redo():
    realm = DetachedRealm()
    first = patch(change(['name'], present('Renamed Film'), 'B'))
    second = patch(change(['name'], present('B'), 'C'))
    steps = [step('first', 0, first), step('second', 1, second, reads=[
        {'kind': 'field', 'ref': 'parent', 'path': ['name'], 'state': present('B')}])]
    landed = submit(realm, first, second, reads=[], steps=steps)
    publication = landed['receipt']['receipt_id']
    original = copy.deepcopy(realm.entries[-1])
    with pytest.raises(CommitRejected) as error:
        submit(realm, undo(publication, 'first'), reads=[])
    assert error.value.failure['details']['reason'] in ('intervening_write', 'inverse_conflict')
    undone = submit(realm, undo(publication, 'second'), reads=[])
    assert parent(undone)['name'] == 'B'
    assert realm.prepared[-1]['records']['undo_steps'] == {publication: ['second']}
    assert realm.entries[-2] == original
    redone = submit(realm, undo(undone['receipt']['receipt_id']), reads=[])
    assert parent(redone)['name'] == 'C'
    assert realm.prepared[-1]['records']['undo_steps'] is None


def test_whole_inverse_projects_each_stage_even_when_final_bytes_equal():
    realm = DetachedRealm()
    first = patch(change(['name'], present('Renamed Film'), 'B'))
    second = patch(change(['name'], present('B'), 'Renamed Film'))
    landed = submit(realm, first, second, reads=[], steps=[step('a', 0, first), step('b', 1, second)])
    undone = submit(realm, undo(landed['receipt']['receipt_id']), reads=[])
    effects = realm.prepared[-1]['records']['steps'][0]['patch']
    assert [(item['before']['value'], item['value']) for item in effects if item['path'] == ['name']] == [
        ('Renamed Film', 'B'), ('B', 'Renamed Film')]
    submit(realm, undo(undone['receipt']['receipt_id']), reads=[])
    assert parent(undone)['name'] == 'Renamed Film'


@pytest.mark.parametrize('dependent', [False, True])
def test_step_selection_and_guards_are_rechecked_after_cas_race(dependent):
    realm = DetachedRealm()
    first = patch(change(['unknown', 'one'], ABSENT, 'B'))
    second = patch(change(['unknown', 'two'], ABSENT, 'B'))
    landed = submit(realm, first, second, reads=[], steps=[step('one', 0, first), step('two', 1, second)])
    publication = landed['receipt']['receipt_id']

    def concurrent():
        if dependent:
            submit(realm, patch(change(['unknown', 'one'], present('B'), 'C')), reads=[])
            submit(realm, patch(change(['unknown', 'one'], present('C'), 'B')), reads=[])
        else:
            submit(realm, patch(change(['unknown', 'other'], ABSENT, 'kept')), reads=[])

    realm.before_accept = concurrent
    if dependent:
        with pytest.raises(CommitRejected) as error:
            submit(realm, undo(publication, 'one'), reads=[])
        assert error.value.failure['details']['reason'] == 'intervening_write'
        assert realm.documents[realm.head['revision_id']]['parent_revision']['payload']['unknown']['one'] == 'B'
    else:
        result = submit(realm, undo(publication, 'one'), reads=[])
        assert parent(result)['unknown'] == {'opaque': ['parent-1', None], 'two': 'B', 'other': 'kept'}
        assert realm.prepared[-1]['records']['undo_steps'] == {publication: ['one']}


def two_images():
    document = document_fixture()
    internal = copy.deepcopy(document['internal_timeline_revisions'][0])
    internal['revision_id'] = 'internal-2'
    shot = copy.deepcopy(document['shot_revisions'][0])
    shot.update(revision_id='shot-2', internal_timeline_revision_id='internal-2')
    shot['payload']['internal_timeline_revision_id'] = 'internal-2'
    shot['content_digest'] = _digest(shot['payload'])
    document['internal_timeline_revisions'].append(internal)
    document['shot_revisions'].append(shot)
    document['parent_revision']['payload']['occurrences'][1]['shot_revision_id'] = 'shot-2'
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    document['dependency_manifest'] = _manifest(document['shot_revisions'], document['internal_timeline_revisions'],
                                               document['dependency_manifest']['media'], [])
    return document


def test_multiple_child_revisions_undo_exact_bound_image_and_keep_disjoint_successor():
    realm = DetachedRealm(two_images())
    landed = submit(realm, patch(change(['unknown'], present([1, 2]), [3],
                                      ref='internal-revision:internal-1')), reads=[])
    submit(realm, patch(change(['unknown'], present([1, 2]), [4], ref='internal-revision:internal-2')), reads=[])
    undone = submit(realm, undo(landed['receipt']['receipt_id']), reads=[])
    assert sorted(image['payload']['unknown'] for image in undone['data']['canonical_document'][
        'internal_timeline_revisions']) == [[1, 2], [4]]
    redone = submit(realm, undo(undone['receipt']['receipt_id']), reads=[])
    assert sorted(image['payload']['unknown'] for image in redone['data']['canonical_document'][
        'internal_timeline_revisions']) == [[3], [4]]


def test_create_edit_whole_undo_redo_retains_transient_roots():
    realm = DetachedRealm()
    bundle = bundle_from_document(realm.documents[realm.head['revision_id']])
    create = capture_occurrence_create(bundle, name='New', track='picture', on=10, duration=2,
                                       occurrence_id='new-occ', shot_id='new-shot')
    edit = patch(change(['name'], present('New'), 'Edited', ref='shot:new-shot'))
    landed = submit(realm, create, edit, reads=[], steps=[step('create', 0, create), step('edit', 1, edit)])
    publication = landed['receipt']['receipt_id']
    undone = submit(realm, undo(publication), reads=[])
    assert all(row['occurrence_id'] != 'new-occ' for row in parent(undone)['occurrences'])
    effects = realm.prepared[-1]['records']['steps'][0]['patch']
    assert any(item['ref'].startswith('shot-revision:') and not item['path']
               and item['op'] == 'set' for item in effects)
    redone = submit(realm, undo(undone['receipt']['receipt_id']), reads=[])
    assert parent(redone) == parent(landed)


@pytest.mark.parametrize('selection', [None, [], {}, {'wrong-publication': ['edit']}])
def test_invalid_step_selection_is_refused_without_acceptance(selection):
    realm = DetachedRealm()
    landed = submit(realm, patch(change(['name'], present('Renamed Film'), 'B')), reads=[])
    call = undo(landed['receipt']['receipt_id'])
    call['args']['steps'] = selection
    before = copy.deepcopy((realm.head, realm.entries))
    with pytest.raises(CommitRejected) as error:
        submit(realm, call, reads=[])
    assert error.value.failure['details']['reason'] == 'invalid_steps'
    assert (realm.head, realm.entries) == before


def test_disjoint_same_child_successor_is_normalized_for_inverse_and_redo():
    realm = DetachedRealm()
    landed = submit(realm, patch(change(['unknown'], present([1, 2]), [3], ref='internal:chapter-1')), reads=[])
    submit(realm, patch(change(['source_window'], present({'start': 4, 'end': 9}),
                              {'start': 4, 'end': 8}, ref='internal:chapter-1')), reads=[])
    undone = submit(realm, undo(landed['receipt']['receipt_id']), reads=[])
    child = undone['data']['canonical_document']['internal_timeline_revisions'][0]['payload']
    assert child['unknown'] == [1, 2] and child['source_window'] == {'start': 4, 'end': 8}
    redone = submit(realm, undo(undone['receipt']['receipt_id']), reads=[])
    child = redone['data']['canonical_document']['internal_timeline_revisions'][0]['payload']
    assert child['unknown'] == [3] and child['source_window'] == {'start': 4, 'end': 8}


def test_later_declared_consumer_blocks_selected_output_undo():
    realm = DetachedRealm()
    landed = submit(realm, patch(change(['name'], present('Renamed Film'), 'B')), reads=[])
    submit(realm, patch(change(['unknown', 'note'], ABSENT, 'consumer')), reads=[
        {'kind': 'field', 'ref': 'parent', 'path': ['name'], 'state': present('B')}])
    before = copy.deepcopy((realm.head, realm.entries))
    with pytest.raises(CommitRejected) as error:
        submit(realm, undo(landed['receipt']['receipt_id']), reads=[])
    assert error.value.failure['details']['reason'] == 'inverse_conflict'
    assert (realm.head, realm.entries) == before


@pytest.mark.parametrize('selected', ['media', 'note'])
def test_selected_media_removal_keeps_complete_unselected_boundary(monkeypatch, selected):
    from tests.editing_conformance.test_recipe_m2_parent_media import calls, captured
    from tests.editing_conformance.test_recipe_occurrence_media import NEW, MediaRealm

    document, _, command, verified = captured()
    realm = MediaRealm(document, verified)
    commands = calls(command)
    note = patch(change(['unknown', 'note'], ABSENT, 'kept'))
    media_start = 1 if selected == 'media' else 0
    media_step = {'id': 'media', 'label': 'media', 'intent': '', 'tags': [],
                  'command_ordinals': list(range(media_start, media_start + len(commands))), 'patch': []}
    controls = {'reads': [], 'steps': ([step('note', 0, note), media_step] if selected == 'media'
        else [media_step, step('note', len(commands), note)])}
    original_context = realm.context

    def context(calls, **kwargs):
        result = original_context(calls, **kwargs)
        result['request'].update(copy.deepcopy(controls))
        result['invocation']['body'] = copy.deepcopy(result['request'])
        result['request_hash'] = _digest(result['invocation'])
        return result

    monkeypatch.setattr(realm, 'context', context)
    landed = realm.call([note, *commands] if selected == 'media' else [*commands, note])
    controls.clear()
    controls['reads'] = []
    result = realm.call([undo(landed['receipt']['receipt_id'], selected)])
    media = {pin['media_id'] for pin in result['data']['canonical_document']['dependency_manifest']['media']}
    assert (NEW in media) == (selected == 'note')
    assert ('note' in parent(result)['unknown']) == (selected == 'media')
    assert {item['id'] for item in realm.entries[-2]['records']['steps']} == {'media', 'note'}


@pytest.mark.parametrize('selected', ['anchored', 'note'])
def test_selected_created_clip_controls_first_use_transcript_removal(selected):
    from tests.editing_conformance.test_native_word_anchor_vectors import fixture

    bundle, pin, content = fixture()
    realm = DetachedRealm(bundle['source_mapping']['_runtime_document'])
    realm.live_sources[_digest(pin)] = copy.deepcopy(bundle['source_mapping']['pinned_documents'][0])
    create = {'schema_version': 1, 'op': 'clip.add', 'version': '1', 'select': 'parent',
              'args': {'row': {'id': 'born', 'clipType': 'am-type', 'track': 'picture', 'at': 0, 'hold': .5}}}
    anchor = {'schema_version': 1, 'op': 'clip.anchor', 'version': '1', 'select': 'parent/clip:born',
              'args': {'field': 'on', 'declaration': 'again', 'choice': {
                  'source': 'occurrence:occ-a/clip:voice', 'document': pin,
                  'word_ids': [content['segments'][0]['words'][0]['id']]}}}
    note = patch(change(['unknown', 'note'], ABSENT, 'kept'))
    steps = [{'id': 'anchored', 'label': 'anchored', 'intent': '', 'tags': [],
              'command_ordinals': [0, 1], 'patch': []}, step('note', 2, note)]
    landed = submit(realm, create, anchor, note, reads=[], steps=steps)
    assert landed['data']['canonical_document']['dependency_manifest']['documents'] == [pin]
    result = submit(realm, undo(landed['receipt']['receipt_id'], selected), reads=[])
    assert (pin in result['data']['canonical_document']['dependency_manifest']['documents']) == (selected == 'note')
    assert any(row['id'] == 'born' for row in parent(result)['clips']) == (selected == 'note')


@pytest.mark.parametrize('damage', ['missing', 'digest'])
def test_inverse_refuses_unproven_physical_successor(damage):
    realm = DetachedRealm()
    landed = submit(realm, patch(change(['unknown'], present([1, 2]), [3], ref='internal:chapter-1')), reads=[])
    record = realm.entries[-1]['records']['commands'][0]
    if damage == 'missing':
        record['bindings'][:] = [binding for binding in record['bindings']
                                 if binding['key'] != 'commit.materialization']
    else:
        next(binding for binding in record['bindings'] if binding['key'] == 'commit.materialization')[
            'content_digest'] = 'sha256:' + '0' * 64
    before = copy.deepcopy((realm.head, realm.entries))
    with pytest.raises(CommitRejected) as error:
        submit(realm, undo(landed['receipt']['receipt_id']), reads=[])
    assert error.value.failure['details']['reason'] == 'history_unavailable'
    assert (realm.head, realm.entries) == before
