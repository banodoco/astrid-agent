"""Nested structural slices restore their own authenticated collection order."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._collections import AcceptedRecords, arrange, restoration_orders
from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, present
from astrid.sdk.timeline._step_undo import inverse_stages, validate_nested_history
from astrid.sdk.timeline.capture import capture_clip_delete
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.inverse import _stage_restoration_orders
from astrid.sdk.timeline.native_parent_media_commands import capture_clip_replace_media
from tests.editing_conformance.test_recipe_m2_parent_media import args, clip_ids, ordered_parent
from tests.editing_conformance.test_recipe_occurrence_media import MediaRealm
from tests.sdk.test_timeline_commit_provider import inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


def _track_create_record(identity, order, *, kind=None):
    ref = f'parent/track:{identity}'
    kind = kind or ('audio' if identity == 'A2' else 'visual')
    return {
        'command': {'op': 'track.create'},
        'ordinal': len(order),
        'created': [{'ref': ref, 'owner': 'parent', 'kind': 'track'}],
        'writes': [{'ref': ref, 'path': [], 'before': ABSENT,
                    'after': present({'id': identity, 'kind': kind})}],
        'selections': [{'key': 'collection.tracks', 'owner': 'parent', 'members': list(order),
                        'order': list(order), 'boundaries': list(order), 'query_version': 1,
                        'policy': 'guard-membership'}],
    }


def _track_order_patch_record(before, after):
    return {
        'command': {'op': 'doc.patch'},
        'ordinal': 2,
        'writes': [{'ref': 'parent', 'path': ['config', 'tracks'],
                    'before': present([{'id': item.rsplit(':', 1)[1]} for item in before]),
                    'after': present([{'id': item.rsplit(':', 1)[1]} for item in after])}],
        'selections': [],
    }


@pytest.fixture
def chain():
    bundle, media, verified = ordered_parent('image')
    document = document_from_bundle(bundle)
    bundle = bundle_from_document(document)
    realm = MediaRealm(document, verified)
    calls = capture_clip_replace_media(bundle, 'parent/clip:A',
        args(media, 'image', at=0)['row'], media, verified_media=verified)
    first = realm.call(calls)
    landed = bundle_from_document(first['data']['canonical_document'])
    second = realm.call([capture_clip_delete(landed, 'parent/clip:C')])
    undone = realm.call([inverse([first['receipt']['receipt_id'], second['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == document
    return realm, document, second['data']['canonical_document'], undone


@pytest.mark.parametrize('selected', [False, True], ids=['whole', 'selected'])
def test_structural_partial_stages_restore_each_order_across_repeated_inverses(chain, selected):
    realm, document, landed, undone = chain
    entry = realm.entries[-1]
    original = copy.deepcopy(entry)
    records = AcceptedRecords([entry], entry['records']['commands'], None)
    groups = list(inverse_stages(records))
    assert len(groups) == 2
    orders = [_stage_restoration_orders(records, commands[0])['parent', 'clip']
              for _, _, commands in groups]
    assert orders == [(['parent/clip:born', 'parent/clip:target', 'parent/clip:C'],
                       ['parent/clip:A', 'parent/clip:target', 'parent/clip:C']),
                      (['parent/clip:born', 'parent/clip:target'],
                       ['parent/clip:born', 'parent/clip:target', 'parent/clip:C'])]
    assert all(commands[0]['ordinal'] == 0 and publication == entry['publication_id']
               for publication, _, commands in groups)
    command = inverse([undone['receipt']['receipt_id']])
    if selected:
        command['args']['steps'] = {entry['publication_id']: [entry['records']['steps'][0]['id']]}
    result = realm.call([command])
    assert result['data']['canonical_document'] == landed
    assert clip_ids(bundle_from_document(landed)) == ['born', 'target']
    assert entry == original
    for expected in (document, landed, document):
        result = realm.call([inverse([result['receipt']['receipt_id']])])
        assert result['data']['canonical_document'] == expected
    assert entry == original


@pytest.mark.parametrize('damage', [
    'source', 'step', 'boundary', 'native', 'effect', 'mirror', 'ordinal', 'command', 'owner',
])
def test_stage_order_requires_exact_accepted_source_partition(chain, damage):
    realm, _, _, _ = chain
    entry = realm.entries[-1]
    records = AcceptedRecords([entry], entry['records']['commands'], None)
    record = list(inverse_stages(records))[0][2][0]
    source, step, boundary = record['_inverse_stage']
    record = {**record}
    if damage == 'source':
        record['_inverse_stage'] = (copy.deepcopy(source), step, boundary)
    elif damage == 'step':
        record['_inverse_stage'] = (source, copy.deepcopy(step), boundary)
    elif damage == 'boundary':
        record['_inverse_stage'] = (source, step, copy.deepcopy(boundary))
    elif damage == 'native':
        record['writes'] = record['writes'][1:]
    elif damage == 'effect':
        record['_inverse_effects'] = []
    elif damage == 'ordinal':
        record['ordinal'] = 1
    elif damage == 'command':
        record['command'] = {**record['command'], 'select': 'foreign'}
    elif damage == 'owner':
        record['selections'] = []
    else:
        record['derived_writes'] = []
    original = copy.deepcopy(realm.entries)
    with pytest.raises(CommandError) as error:
        _stage_restoration_orders(records, record)
    assert error.value.kind == 'history_unavailable'
    assert realm.entries == original


@pytest.mark.parametrize('damage', ['missing', 'foreign', 'duplicate'])
def test_stage_order_never_recreates_members_from_mirror_images(chain, damage):
    realm, document, _, _ = chain
    entry = realm.entries[-1]
    records = AcceptedRecords([entry], entry['records']['commands'], None)
    stage = list(inverse_stages(records))[0][2][0]
    expected, _ = _stage_restoration_orders(records, stage)['parent', 'clip']
    bundle = bundle_from_document(document)
    rows = bundle['parent']['clips']
    rows[0]['id'] = 'born'
    if damage == 'missing':
        rows.pop()
    elif damage == 'foreign':
        rows[1]['id'] = 'foreign'
    else:
        rows.append(copy.deepcopy(rows[0]))
    bundle['parent']['config']['clips'] = copy.deepcopy(rows)
    original = copy.deepcopy(bundle)
    with pytest.raises(CommandError):
        arrange(State(bundle), 'parent', 'clip', expected)
    assert bundle == original


def test_partial_stage_mirror_chain_rejects_a_broken_previous_image(chain):
    realm, _, _, _ = chain
    history = copy.deepcopy(realm.entries)
    entry = history[-1]
    record = entry['records']['commands'][0]
    record['derived_writes'][1]['before']['value'].reverse()
    original = copy.deepcopy(history)
    with pytest.raises(CommandError) as error:
        validate_nested_history(entry, {item['publication_id']: item for item in history}.__getitem__)
    assert error.value.kind == 'history_unavailable'
    assert history == original


def test_structural_order_accepts_continuous_track_creation_stages():
    initial = ['parent/track:V1', 'parent/track:A1']
    after_picture_track = [*initial, 'parent/track:V2']
    records = [_track_create_record('V2', initial),
               _track_create_record('A2', after_picture_track)]

    assert restoration_orders(records) == {
        ('parent', 'track'): (initial, [*after_picture_track, 'parent/track:A2'])
    }


def test_provider_undo_restores_create_reorder_create_track_batch():
    from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
    from tests.sdk.test_timeline_commit_provider import DetachedRealm, inverse, track_create
    realm = DetachedRealm()
    before = copy.deepcopy(realm.documents[realm.head['revision_id']])
    after_v2 = ['picture', 'V2']
    reordered = ['V2', 'picture']
    calls = [
        track_create('V2'),
        patch(change(['config', 'tracks'], present({'id': 'V2', 'kind': 'visual',
                                                    'opaque': {'clear': None}}),
                     op='list.move', id='V2', order_before=after_v2, after_id=None)),
        {'schema_version': 1, 'op': 'track.create', 'version': '1', 'select': 'parent',
         'args': {'row': {'id': 'A2', 'kind': 'audio'}}},
    ]
    landed = submit(realm, *calls, reads=[],
                    steps=[step('create-v2', 0, calls[0]), step('reorder-v2', 1, calls[1]),
                           step('create-a2', 2, calls[2])])
    assert [row['id'] for row in landed['data']['canonical_document']['parent_revision']['payload'][
        'config']['tracks']] == [*reordered, 'A2']
    entry = realm.entries[-1]
    accepted = AcceptedRecords([entry], entry['records']['commands'], None)
    create_records = {record['command']['args']['row']['id']: record
                      for record in accepted if record['command']['op'] == 'track.create'}
    assert _stage_restoration_orders(accepted, create_records['V2'])['parent', 'track'] == (
        ['parent/track:picture'], ['parent/track:picture', 'parent/track:V2'])
    assert _stage_restoration_orders(accepted, create_records['A2'])['parent', 'track'] == (
        ['parent/track:V2', 'parent/track:picture'],
        ['parent/track:V2', 'parent/track:picture', 'parent/track:A2'])

    undone = realm.call([inverse([landed['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == before


@pytest.mark.parametrize('damage', ['membership', 'duplicate', 'wrong-before', 'foreign-id', 'missing-image'])
def test_structural_order_rejects_unproven_track_list_patch(damage):
    initial = ['parent/track:V1', 'parent/track:A1']
    after_v2_create = [*initial, 'parent/track:V2']
    reordered = ['parent/track:V1', 'parent/track:V2', 'parent/track:A1']
    before, after = after_v2_create, reordered
    if damage == 'membership':
        after = ['parent/track:V1', 'parent/track:V2', 'parent/track:alien']
    elif damage == 'duplicate':
        after = ['parent/track:V1', 'parent/track:V2', 'parent/track:V2']
    elif damage == 'wrong-before':
        before = reordered
    elif damage == 'foreign-id':
        after = ['parent/track:V1', 'parent/track:V2/bad', 'parent/track:A1']
    elif damage == 'missing-image':
        bad_patch = _track_order_patch_record(after_v2_create, reordered)
        bad_patch['writes'][0]['after'] = {'state': 'absent'}
        records = [_track_create_record('V2', initial), bad_patch,
                   _track_create_record('A2', reordered)]
        with pytest.raises(CommandError):
            restoration_orders(records)
        return
    records = [_track_create_record('V2', initial),
               _track_order_patch_record(before, after),
               _track_create_record('A2', reordered)]

    with pytest.raises(CommandError):
        restoration_orders(records)


def test_standalone_track_list_patch_is_not_projected_as_structural_membership():
    before = ['parent/track:V1']
    after = [*before, 'parent/track:declared']
    assert restoration_orders([_track_order_patch_record(before, after)]) == {}


def test_structural_order_rejects_disconnected_track_creation_stages():
    initial = ['parent/track:V1', 'parent/track:A1']
    records = [_track_create_record('V2', initial),
               _track_create_record('A2', ['parent/track:V1', 'parent/track:A1', 'parent/track:other'])]

    with pytest.raises(CommandError, match='dependent structural stages require staged reversal'):
        restoration_orders(records)
