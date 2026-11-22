"""Sharing a persisted source preserves its typed clock through canonical reopening."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline.commit import _digest, _manifest, bundle_from_document
from astrid.sdk.timeline.native_occurrence_shared import capture_occurrence_place_shared
from astrid.sdk.timeline.validation import FinalCandidateError, _validate_fps
from tests.sdk import test_timeline_commit_provider as provider
from tests.sdk.test_d136_fps_upstream_compat import _controls
from tests.sdk.test_p1_doc_patch_provider import _service_call
from tests.sdk.test_timeline_commit_provider_document import document_fixture

runtime_abi = provider.runtime_abi


def _document(*, shared=False, fps=24):
    document = document_fixture(shared=shared)
    parent = document['parent_revision']
    internal, = document['internal_timeline_revisions']
    shot, = document['shot_revisions']
    parent['payload']['config']['output']['fps'] = copy.deepcopy(fps)
    internal['payload']['output']['fps'] = copy.deepcopy(fps)
    internal['payload'].pop('source_window')
    shot['payload']['text_bindings'] = []
    for occurrence in parent['payload']['occurrences']:
        occurrence.update(source_offset=0, speed=1)
    for image in (parent, internal, shot):
        image['content_digest'] = _digest(image['payload'])
    document['dependency_manifest'] = _manifest(
        [shot], [internal], document['dependency_manifest']['media'], [])
    return document


@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.parametrize('fps', [24, {'numerator': 30000, 'denominator': 1001}])
def test_provider_shares_the_same_persisted_typed_clock(fps, dry_run):
    original = _document(fps=fps)
    realm = provider.DetachedRealm(original)
    before_head = copy.deepcopy(realm.head)
    command = capture_occurrence_place_shared(
        bundle_from_document(original), 'occurrence:occ-a', 'occ-b', on=10)

    result = _service_call(realm, [command], controls=_controls(dry_run=dry_run))

    data = result['data']
    assert data['status'] == ('preview' if dry_run else 'accepted')
    actual = data['canonical_document']
    assert actual['shot_revisions'] == original['shot_revisions']
    assert actual['internal_timeline_revisions'] == original['internal_timeline_revisions']
    assert actual['dependency_manifest'] == original['dependency_manifest']
    occurrences = actual['parent_revision']['payload']['occurrences']
    assert {row['occurrence_id'] for row in occurrences} == {'occ-a', 'occ-b'}
    assert {row['shot_revision_id'] for row in occurrences} == {'shot-1'}
    reopened = bundle_from_document(actual)
    assert len(reopened['shots']) == 2
    for shot in reopened['shots'].values():
        assert shot['internal_timeline']['output']['fps'] == fps
    if dry_run:
        assert data['would_status'] == 'accepted'
        assert result['receipt'] is None
        assert realm.head == before_head
        assert realm.prepared == realm.accepted == []
    else:
        assert realm.documents[realm.head['revision_id']] == actual
        assert result['receipt']['result'] == data
        restored = _service_call(
            realm, [provider.inverse([realm.head['publication_id']])],
            controls=_controls(dry_run=False))
        assert restored['data']['status'] == 'accepted'
        assert restored['data']['canonical_document'] == original


@pytest.mark.parametrize('damage', [
    'parent-remove', 'child-remove', 'move-to-open', 'changed-rate',
    'equivalent-spelling', 'shot-owner', 'shot-revision', 'child-owner',
    'child-revision', 'shot-digest', 'child-digest', 'occurrence-owner',
    'arbitrary-alias', 'invalid-fps',
])
def test_shared_aliases_do_not_hide_changed_fps_declarations_or_owners(damage):
    baseline = bundle_from_document(_document())
    candidate = bundle_from_document(_document(shared=True))
    parent = candidate['parent']['config']
    if damage == 'parent-remove':
        parent['output'].pop('fps')
    for local_id, shot in list(candidate['shots'].items()):
        output = shot['internal_timeline']['output']
        origin = candidate['source_mapping']['shots'][local_id]
        if damage == 'child-remove':
            output.pop('fps')
        elif damage == 'move-to-open':
            parent['theme_overrides'] = {'visual': {'canvas': {'fps': output.pop('fps')}}}
        elif damage == 'changed-rate':
            output['fps'] = 30
        elif damage == 'equivalent-spelling':
            output['fps'] = {'numerator': 48, 'denominator': 2}
        elif damage == 'invalid-fps':
            output['fps'] = {'numerator': 24, 'denominator': 0}
        elif damage in {'shot-owner', 'shot-revision', 'child-owner', 'child-revision',
                        'shot-digest', 'child-digest'}:
            key = {'shot-owner': 'shot_id', 'shot-revision': 'revision_id',
                   'child-owner': 'internal_timeline_id',
                   'child-revision': 'internal_timeline_revision_id',
                   'shot-digest': 'content_digest',
                   'child-digest': 'internal_timeline_content_digest'}[damage]
            origin[key] = 'replacement'
        elif damage == 'arbitrary-alias':
            replacement = 'replacement-' + local_id
            candidate['shots'][replacement] = candidate['shots'].pop(local_id)
            candidate['source_mapping']['shots'][replacement] = (
                candidate['source_mapping']['shots'].pop(local_id))
            for row in candidate['placements']:
                if row['shot_id'] == local_id:
                    row['shot_id'] = replacement
        elif damage == 'occurrence-owner':
            for row in candidate['placements']:
                row['occurrence_id'] = 'replacement-' + row['occurrence_id']
    before = copy.deepcopy(candidate)
    with pytest.raises(FinalCandidateError):
        _validate_fps(candidate, baseline)
    assert candidate == before
