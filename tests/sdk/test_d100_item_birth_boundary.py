"""Physical item birth keeps source custody and copied correspondence separate."""
from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError
from astrid.sdk.timeline._command_state import CommandError, State, present
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import (
    _digest,
    _manifest,
    bundle_from_document,
    document_from_bundle,
)
from tests.editing_conformance.test_doc_patch_v2 import change, patch
from tests.sdk.test_timeline_commit_provider_document import MEDIA, document_fixture

REF = 'occurrence:occ-a/shot'
ITEM = {'item_id': 'first-item', 'media_id': MEDIA,
        'metadata': {'opaque': [None, False, 'first-item']}}


def empty_source():
    document = document_fixture(shared=False)
    shot = document['shot_revisions'][0]
    shot['payload']['items'] = []
    shot['content_digest'] = _digest(shot['payload'])
    document['dependency_manifest'] = _manifest(
        [shot], document['internal_timeline_revisions'], document['dependency_manifest']['media'], [])
    return document


@pytest.mark.parametrize('aggregate', [False, True])
def test_first_physical_item_preserves_custody_and_opaque_fields(aggregate):
    document = empty_source()
    bundle = bundle_from_document(document)
    before = copy.deepcopy(bundle)
    payload = copy.deepcopy(document['shot_revisions'][0]['payload'])
    replacement = {**payload, 'items': [ITEM]}
    call = patch(change([] if aggregate else ['items'],
                        present(payload if aggregate else []),
                        replacement if aggregate else [ITEM], ref='shot-revision:shot-1'))
    plan = execute(bundle, [call], commit_request={'reads': []})
    state = State(plan.bundle)
    assert state.get(REF, ['payload', 'items']) == present([ITEM])
    assert state.get(REF, ['payload', 'items', 'item_id:first-item', 'media_id']) == present(MEDIA)
    source_before = before['source_mapping']
    source_after = plan.bundle['source_mapping']
    assert source_after['shots'] == source_before['shots']
    assert source_after['placements'] == source_before['placements']
    assert source_after['_runtime_document'] == source_before['_runtime_document']
    assert set(source_after) == set(source_before) | {'_doc_patch_images'}
    assert source_after['_doc_patch_images'] == {
        'occurrence:occ-a': {
            'shot_id': 'chapter-shot', 'internal_timeline_id': 'chapter-1',
            'source_shot_revision': 'shot-1', 'source_internal_revision': 'internal-1',
            'payload': replacement,
            'internal_timeline': before['shots']['chapter-shot']['internal_timeline'],
        },
    }
    assert source_after['shots']['chapter-shot']['item_id_map'] == {}
    result = document_from_bundle(plan.bundle)
    assert result['shot_revisions'][0]['payload'] == replacement
    assert result['internal_timeline_revisions'] == document['internal_timeline_revisions']
    assert bundle == before


@pytest.mark.parametrize('field', [
    'shot_id', 'revision_id', 'content_digest', 'internal_timeline_id',
    'internal_timeline_revision_id', 'internal_timeline_content_digest', 'item_id_map',
])
def test_wrong_physical_custody_refuses_without_writes(field):
    bundle = bundle_from_document(empty_source())
    source = bundle['source_mapping']['shots']['chapter-shot']
    source[field] = {'first-item': 'first-item'} if field == 'item_id_map' else 'foreign'
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError) as error:
        State(bundle).set(REF, ['payload', 'items'], present([ITEM]))
    assert error.value.kind == 'identity_conflict'
    assert bundle == before


@pytest.mark.parametrize('family', ['shot_revisions', 'internal_timeline_revisions'])
def test_changed_authenticated_image_refuses_without_writes(family):
    bundle = bundle_from_document(empty_source())
    bundle['source_mapping']['_runtime_document'][family][0]['payload']['forged'] = True
    before = copy.deepcopy(bundle)
    with pytest.raises(AuthoringBundleError):
        State(bundle).set(REF, ['payload', 'items'], present([ITEM]))
    assert bundle == before


@pytest.mark.parametrize('rows', [
    [{'item_id': ''}], [{'item_id': None}], [{}], [None], [ITEM, ITEM], {},
])
def test_invalid_new_membership_refuses_without_writes(rows):
    bundle = bundle_from_document(empty_source())
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError) as error:
        State(bundle).set(REF, ['payload', 'items'], present(rows))
    assert error.value.kind == 'identity_conflict'
    assert bundle == before


@pytest.mark.parametrize('damage', ['birth', 'missing', 'duplicate', 'owner', 'map'])
def test_copied_placement_correspondence_remains_exact(damage):
    bundle = bundle_from_document(document_fixture(shared=True))
    local = bundle['placements'][0]['shot_id']
    rows = copy.deepcopy(bundle['shots'][local]['payload']['items'])
    if damage == 'birth':
        rows.append(ITEM)
    elif damage == 'missing':
        rows = []
    elif damage == 'duplicate':
        rows.append(copy.deepcopy(rows[0]))
    elif damage == 'owner':
        bundle['placements'][0]['shot_id'] = bundle['placements'][1]['shot_id']
    else:
        bundle['source_mapping']['shots'][local]['item_id_map']['foreign'] = rows[0]['item_id']
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError) as error:
        State(bundle).set(REF, ['payload', 'items'], present(rows))
    assert error.value.kind == 'identity_conflict'
    assert bundle == before


def test_copied_payload_edit_preserves_exact_physical_item_identity():
    document = document_fixture(shared=True)
    bundle = bundle_from_document(document)
    local = bundle['placements'][0]['shot_id']
    custody = copy.deepcopy(bundle['source_mapping'])
    physical_id = bundle['shots'][local]['payload']['items'][0]['item_id']
    rows = copy.deepcopy(document['shot_revisions'][0]['payload']['items'])
    rows[0]['metadata']['note'] = [None, False, 'item-1']
    State(bundle).set(REF, ['payload', 'items'], present(rows))
    assert bundle['shots'][local]['payload']['items'] == [{**rows[0], 'item_id': physical_id}]
    assert State(bundle).get(REF, ['payload', 'items']) == present(rows)
    assert bundle['source_mapping'] == custody


def test_wrong_previous_value_refuses_entire_patch_without_writes():
    bundle = bundle_from_document(empty_source())
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError):
        execute(bundle, [patch(change(['name'], present('Renamed Film'), 'Changed'),
                               change(['items'], present([ITEM]), [ITEM],
                                      ref='shot-revision:shot-1'))])
    assert bundle == before


def test_existing_owner_cannot_switch_to_another_valid_physical_source():
    bundle = bundle_from_document(empty_source())
    created = execute(bundle, [{
        'schema_version': 1, 'op': 'occurrence.create', 'version': '1', 'select': 'parent',
        'args': {'occurrence_id': 'other', 'shot_id': 'other-shot', 'name': 'Other',
                 'track': 'picture', 'on': 20, 'duration': 1},
    }])
    bundle = bundle_from_document(document_from_bundle(created.bundle))
    bundle['placements'][0]['shot_id'] = 'other-shot'
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError) as error:
        State(bundle).set(REF, ['payload', 'items'], present([ITEM]))
    assert error.value.kind == 'identity_conflict'
    assert bundle == before


def test_registered_shared_owner_uses_the_same_physical_custody_without_alias_birth():
    document = empty_source()
    document['parent_revision']['payload']['occurrences'][0]['source_offset'] = 0
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    bundle = bundle_from_document(document)
    before = copy.deepcopy(bundle)
    shared = {'schema_version': 1, 'op': 'occurrence.place-shared', 'version': '1',
              'select': 'occurrence:occ-a', 'created': ['occurrence:shared'],
              'args': {'occurrence_id': 'shared', 'on': 20}}
    plan = execute(bundle, [shared, patch(change(
        ['items'], present([]), [ITEM], ref='shot-revision:shot-1'))])
    assert State(plan.bundle).get('occurrence:shared/shot', ['payload', 'items']) == present([ITEM])
    source_before = before['source_mapping']
    source_after = plan.bundle['source_mapping']
    assert source_after['shots'] == source_before['shots']
    assert source_after['placements'] == source_before['placements']
    assert source_after['_runtime_document'] == source_before['_runtime_document']
    assert set(source_after) == set(source_before) | {'_doc_patch_images'}
    authored_payload = {**before['shots']['chapter-shot']['payload'], 'items': [ITEM]}
    expected_cache = {
        owner: {
            'shot_id': 'chapter-shot', 'internal_timeline_id': 'chapter-1',
            'source_shot_revision': 'shot-1', 'source_internal_revision': 'internal-1',
            'payload': authored_payload,
            'internal_timeline': before['shots']['chapter-shot']['internal_timeline'],
        }
        for owner in ('occurrence:occ-a', 'occurrence:shared')
    }
    assert source_after['_doc_patch_images'] == expected_cache
    result = document_from_bundle(plan.bundle)
    assert len(result['shot_revisions']) == 1
    assert result['shot_revisions'][0]['payload']['items'] == [ITEM]
    assert {row['shot_id'] for row in result['parent_revision']['payload']['occurrences']} == {'chapter-shot'}
    assert bundle == before


@pytest.mark.integration
def test_registered_runtime_first_source_item_birth_and_no_write_refusal(tmp_path):
    from tests.sdk.test_capture_source_runtime import open_source_runtime

    for runtime in open_source_runtime(tmp_path):
        from runtime_protocol.errors import RuntimeErrorBase

        service, project, timeline = runtime.service, runtime.project_id, runtime.timeline_id
        shot = service.get_project_shot_revision(project, runtime.shot_id, runtime.shot_revision_id)
        assert shot['payload']['items'] == []
        internal = service.get_project_timeline_revision(project, timeline, runtime.internal_revision_id)
        clip = internal['payload']['clips'][0]
        media = internal['payload']['registry']['assets'][clip['asset']]['media_id']
        item = {**copy.deepcopy(ITEM), 'media_id': media}

        def submit(rows, previous, key, ref):
            head = service.get_project_timeline(project, timeline)
            return service.commit_timeline(
                project, timeline,
                {'api_version': 1, 'project_id': project, 'timeline_id': timeline,
                 'base_revision_id': head['head_revision_id'],
                 'base_publication_id': head['head_publication_id'],
                 'commands': [patch(change(['items'], present(previous), rows, ref=ref))],
                 'label': 'First physical item', 'merge': 'fields', 'reads': []},
                idempotency_key=key, publisher='item-birth-boundary',
                invocation_path=f'/v1/projects/{project}/timelines/{timeline}/commit',
                authorize=lambda: None,
            )

        result = submit([item], [], 'first-item', 'shot-revision:' + shot['revision_id'])
        assert result['data']['status'] == 'accepted'
        canonical = result['data']['canonical_document']
        saved = canonical['shot_revisions'][0]
        assert saved['shot_id'] == shot['shot_id']
        assert saved['payload'] == {**shot['payload'], 'items': [item]}
        assert service.get_project_shot_revision(
            project, saved['shot_id'], saved['revision_id'])['payload'] == saved['payload']
        head = service.get_project_timeline(project, timeline)
        with pytest.raises(RuntimeErrorBase) as error:
            submit([], [], 'wrong-previous', 'shot-revision:' + saved['revision_id'])
        assert error.value.status in (400, 409, 422)
        assert service.get_project_timeline(project, timeline) == head
