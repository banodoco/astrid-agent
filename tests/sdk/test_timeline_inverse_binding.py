from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, present
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from tests.editing_conformance.test_doc_patch_v2 import change, patch, submit
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedPort, DetachedRealm, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import MEDIA, document_fixture


@pytest.mark.parametrize('mismatched', [False, True])
def test_raw_physical_scope_records_cannot_authorize_inverse(mismatched):
    realm = DetachedRealm()
    landed = submit(realm, patch(change(['name'], present('Renamed Film'), 'B')),
                    reads=[{'kind': 'scope', 'ref': 'timeline', 'path': []}])
    records = copy.deepcopy(realm.entries[-1]['records']['commands'])
    if mismatched:
        records[0]['bindings'][0]['revision_id'] = 'different-revision'
    bundle = bundle_from_document(realm.documents[realm.head['revision_id']])
    original = copy.deepcopy(bundle)
    with pytest.raises(CommandError, match='authenticated physical binding normalization') as error:
        execute(bundle, [inverse([landed['receipt']['receipt_id']])], inverse_records=records)
    assert error.value.kind == 'history_unavailable'
    assert bundle == original


@pytest.mark.parametrize('later_write', [False, True])
def test_provider_scope_inverse_uses_authenticated_closure_and_keeps_later_guard(monkeypatch, later_write):
    realm = DetachedRealm()
    landed = submit(realm, patch(change(['name'], present('Renamed Film'), 'B')),
                    reads=[{'kind': 'scope', 'ref': 'timeline', 'path': []}])
    record = realm.entries[-1]['records']['commands'][0]
    scopes = [binding for binding in record['bindings'] if binding['ref'] == 'timeline']
    assert {binding['physical_ref'] for binding in scopes} == {
        'parent', 'shot-revision:shot-1', 'internal-revision:internal-1'}
    assert realm.head['revision_id'] != scopes[0]['revision_id']
    if later_write:
        submit(realm, patch(change(['name'], present('B'), 'B')), reads=[])
    before = copy.deepcopy((realm.head, realm.entries))
    original_entity = State.entity

    def entity(state, ref, *, optional=False):
        assert ref != 'timeline', 'timeline scope must not dispatch through State.entity'
        return original_entity(state, ref, optional=optional)

    monkeypatch.setattr(State, 'entity', entity)
    call = inverse([landed['receipt']['receipt_id']])
    if later_write:
        with pytest.raises(CommitRejected) as error:
            submit(realm, call, reads=[])
        assert error.value.failure['details']['reason'] == 'intervening_write'
        assert (realm.head, realm.entries) == before
    else:
        undone = submit(realm, call, reads=[])
        assert undone['data']['canonical_document']['parent_revision']['payload']['name'] == 'Renamed Film'
        redone = submit(realm, inverse([undone['receipt']['receipt_id']]), reads=[])
        assert redone['data']['canonical_document']['parent_revision']['payload']['name'] == 'B'


def test_picture_audio_tracks_and_overlay_wav_undo_restore_exact_parent_revision(monkeypatch):
    wav = 'sha256:' + '4' * 64
    original = document_fixture()
    realm = DetachedRealm(original)
    original = copy.deepcopy(realm.documents[realm.head['revision_id']])
    parent = original['parent_revision']['payload']
    old_clips = parent['clips']
    added_clips = [
        {'id': 'overlay', 'track': 'V2', 'asset': 'overlay', 'clipType': 'media',
         'at': 8, 'hold': 2},
        {'id': 'voice', 'track': 'A1', 'asset': 'voice', 'clipType': 'media',
         'at': 0, 'hold': 4},
    ]

    def track(identity, kind):
        return {'schema_version': 1, 'op': 'track.create', 'version': '1', 'select': 'parent',
                'args': {'row': {'id': identity, 'kind': kind}}}

    def raw_media_info(self, pin):
        assert pin in ({'media_id': wav, 'content_digest': wav},
                       {'media_id': MEDIA, 'content_digest': MEDIA})
        return {'object_id': pin['media_id']}

    monkeypatch.setattr(DetachedPort, 'raw_media_info', raw_media_info, raising=False)

    def media_admit(media_id):
        return {'schema_version': 1, 'op': 'media.admit', 'version': '1', 'select': None,
                'args': {'inputs': [{'pin': {'media_id': media_id, 'content_digest': media_id}}]}}

    edit_patch = patch(
        change(['clips'], present(old_clips), [*old_clips, *added_clips]),
        change(['registry', 'assets', 'overlay'], ABSENT,
               {'media_id': MEDIA, 'type': 'image'}),
        change(['registry', 'assets', 'voice'], ABSENT,
               {'media_id': wav, 'type': 'audio/wav'}),
        created=['parent/clip:overlay', 'parent/clip:voice', 'parent/asset:overlay',
                 'parent/asset:voice'],
    )

    edited = submit(
        realm,
        media_admit(wav),
        media_admit(MEDIA),
        track('V2', 'visual'),
        track('A1', 'audio'),
        edit_patch,
        steps=[
            {'id': 'admit', 'label': 'Admit WAV', 'intent': 'Verify audio media', 'tags': [],
             'command_ordinals': [0], 'patch': [], 'reads': []},
            {'id': 'admit-picture', 'label': 'Admit picture', 'intent': 'Verify picture media', 'tags': [],
             'command_ordinals': [1], 'patch': [], 'reads': []},
            {'id': 'edit', 'label': 'Add picture and audio', 'intent': 'Place media on tracks',
             'tags': [], 'command_ordinals': [2, 3, 4], 'patch': copy.deepcopy(edit_patch['args']['changes']),
             'reads': []},
        ],
    )
    assert edited['data']['status'] == 'accepted'
    assert edited['data']['canonical_document']['parent_revision']['revision_id'] != \
        original['parent_revision']['revision_id']

    undone = submit(realm, inverse([edited['receipt']['receipt_id']]), reads=[])
    assert undone['data']['canonical_document'] == original
    assert len(realm.accepted) == 2  # One authored edit followed by one undo.

    inverse_record = realm.entries[-1]['records']['commands'][0]
    inverse_stages = [binding for binding in inverse_record['bindings']
                      if binding.get('key') == 'inverse.stage']
    assert [binding['boundary']['native_write_range'][0] == binding['boundary']['native_write_range'][1]
            for binding in inverse_stages] == [False, True, True]
    parent_effects = [binding for binding in inverse_record['bindings']
                      if binding.get('key') == 'commit.effect' and binding.get('physical_ref') == 'parent'
                      and binding.get('boundary', {}).get('side') == 'after']
    assert parent_effects
    final_parent_effect = max(parent_effects, key=lambda binding: binding['effect_ordinal'])
    restored_parent = undone['data']['canonical_document']['parent_revision']
    assert (final_parent_effect['revision_id'], final_parent_effect['content_digest']) == (
        restored_parent['revision_id'], restored_parent['content_digest'])
