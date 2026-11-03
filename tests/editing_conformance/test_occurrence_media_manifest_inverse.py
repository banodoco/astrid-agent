"""Popup foundation probe; test-only handlers are not a published media contract."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline import commands
from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline._commit_patch import inverse_address_resolver
from astrid.sdk.timeline._step_undo import select_inverse_records
from astrid.sdk.timeline.commit import (
    _digest,
    _manifest,
    bundle_from_document,
    document_from_bundle,
)
from tests.editing_conformance.test_recipe_m1_commands import inverse
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedRealm,
    runtime_abi,  # noqa: F401 -- provide the detached provider ABI fixture
)
from tests.sdk.test_timeline_commit_provider import (
    inverse as inverse_call,
)
from tests.sdk.test_timeline_commit_provider_document import document_fixture

OWNER = 'occurrence:occ-a'
NEW = 'sha256:' + '2' * 64


def popup_document():
    document = document_fixture()
    internal = document['internal_timeline_revisions'][0]
    internal['payload'].pop('source_window')
    internal['payload']['registry'] = copy.deepcopy(internal['payload']['registry'])
    internal['payload']['registry']['assets']['picture'].update(
        generationId='generation-old', variantId='variant-old', opaque=[None, False])
    internal['content_digest'] = _digest(internal['payload'])
    parent = document['parent_revision']
    parent['payload']['occurrences'][0]['placement']['start_ms'] = 8000
    parent['content_digest'] = _digest(parent['payload'])
    document['dependency_manifest'] = _manifest(
        document['shot_revisions'], document['internal_timeline_revisions'],
        document['dependency_manifest']['media'], [])
    return document


@pytest.fixture(params=['add', 'swap'])
def popup_edit(request, monkeypatch):
    """Use actual recorder writes to isolate compilation from media admission.

    The detached IDs are unit-fixture values. This deliberately makes no claim
    about Runtime selection, provider acceptance or an installed popup opcode.
    """
    operation = request.param

    def handler(ctx, command):
        ctx.read(OWNER + '/source')
        ctx.write(OWNER + '/source', ['registry', 'assets', 'new'],
                  {'media_id': NEW, 'type': 'image', 'generationId': 'generation-new',
                   'variantId': 'variant-new', 'opaque': [None, False, {}]})
        if operation == 'add':
            ctx.create(OWNER, 'clip', {
                'id': 'reserved-child', 'track': 'picture', 'asset': 'new',
                'clipType': 'image', 'at': .125, 'hold': 1.25, 'params': None})
        else:
            ctx.read(OWNER + '/clip:clip-a')
            ctx.write(OWNER + '/clip:clip-a', ['asset'], 'new')

    installed = commands.operations()
    installed['test.popup-media'] = commands.Operation(
        {'type': 'object', 'additionalProperties': False}, handler,
        {'ownership': 'occurrence'})

    def properties(ctx, command):
        for write in command['args']['writes']:
            ctx.write(write['ref'], write['path'], write['value'])

    installed['test.properties'] = commands.Operation({'type': 'object'}, properties, {})
    monkeypatch.setattr(commands, 'operations', lambda: installed)
    document = popup_document()
    original = bundle_from_document(document)
    call = {'schema_version': 1, 'op': 'test.popup-media', 'version': '1',
            'select': OWNER, 'args': {}}
    plan = commands.execute(original, [call])
    return document, original, plan


def test_recorded_registry_creation_and_child_inverse_preserve_authored_state(popup_edit):
    document, original, plan = popup_edit
    assert commands.execute(original, plan.commands).records == plan.records
    assert original == bundle_from_document(document)
    assert plan.bundle['parent'] == original['parent']
    sibling = original['placements'][1]['shot_id']
    assert plan.bundle['shots'][sibling] == original['shots'][sibling]
    assert inverse(plan.bundle, plan.records).bundle == original
    assert document_from_bundle(inverse(plan.bundle, plan.records).bundle) == document
    reloaded = bundle_from_document(document_from_bundle(plan.bundle))
    undone = inverse(reloaded, plan.records)
    restored = document_from_bundle(undone.bundle, source_bundles=(original,))
    assert State(undone.bundle).config(OWNER)['registry'] == State(original).config(OWNER)['registry']
    assert restored['parent_revision']['payload'] == document['parent_revision']['payload']
    assert restored['shot_revisions'] == document['shot_revisions']
    assert restored['internal_timeline_revisions'] == document['internal_timeline_revisions']


def accepted_records(realm, entry):
    def load_boundary(edge):
        return (
            bundle_from_document(realm.documents[edge['previous']['revision_id']]),
            bundle_from_document(realm.documents[edge['revision_id']]),
        )

    def load_history(publication):
        return next(item for item in realm.entries
                    if item['publication_id'] == publication)

    accepted = AcceptedRecords([entry], entry['records']['commands'], load_boundary)
    resolver = inverse_address_resolver(
        [entry], bundle_from_document(realm.documents[entry['revision_id']]),
        load_boundary=load_boundary, load_history=load_history)
    return select_inverse_records(accepted, address_resolver=resolver)


def accepted_popup(popup_edit):
    document, original, plan = popup_edit
    realm = DetachedRealm(document)
    created = realm.call(plan.commands)
    return realm, created, accepted_records(realm, realm.entries[-1])


def properties(realm, *writes):
    return realm.call([{'schema_version': 1, 'op': 'test.properties', 'version': '1',
                       'select': None, 'args': {'writes': [
                           {'ref': ref, 'path': path, 'value': value} for ref, path, value in writes]}}])


def test_compiled_popup_inverse_restores_exact_original_document(popup_edit):
    # Restore this expectation through the shared recipe; do not preseed NEW in
    # the baseline, drop the manifest comparison or accept a different revision.
    document, original, plan = popup_edit
    reloaded = bundle_from_document(document_from_bundle(plan.bundle))
    undone = inverse(reloaded, plan.records)
    realm, created, records = accepted_popup(popup_edit)
    assert document_from_bundle(undone.bundle, source_bundles=(original,)) != document
    assert document_from_bundle(undone.bundle, source_bundles=(original,), inverse_records=records) == document
    accepted = copy.deepcopy(realm.entries)
    undo = realm.call([inverse_call([created['receipt']['receipt_id']])])
    assert undo['data']['canonical_document'] == document
    assert realm.entries[:-1] == accepted
    # A reloaded, guarded inverse of undo restores the exact introduced pin and revision.
    redo = realm.call([inverse_call([undo['receipt']['receipt_id']])])
    assert redo['data']['canonical_document'] == created['data']['canonical_document']
    again = realm.call([inverse_call([redo['receipt']['receipt_id']])])
    assert again['data']['canonical_document'] == document


def test_unselected_retained_media_pins_cannot_be_globally_pruned():
    document = popup_document()
    # Retained media can belong to immutable sources rather than selected clips.
    # The current converter explicitly preserves such pins; fixing popup undo by
    # pruning all unselected media would silently lose this existing behavior.
    document['dependency_manifest']['media'].append({'media_id': NEW, 'content_digest': NEW})
    assert document_from_bundle(bundle_from_document(document)) == document


def test_later_disjoint_media_and_original_unselected_pins_survive(popup_edit):
    document, original, plan = popup_edit
    retained = 'sha256:' + '3' * 64
    document['dependency_manifest']['media'].append({'media_id': retained, 'content_digest': retained})
    realm = DetachedRealm(document)
    created = realm.call(plan.commands)
    other = 'sha256:' + '4' * 64
    later = properties(realm, ('parent', ['registry', 'assets', 'other'],
                               {'media_id': other, 'type': 'image', 'opaque': [None, False]}),
                       ('parent/clip:clip-b', ['asset'], 'other'))
    result = realm.call([inverse_call([created['receipt']['receipt_id']])])['data']['canonical_document']
    assert {pin['media_id'] for pin in result['dependency_manifest']['media']} == {
        document['dependency_manifest']['media'][0]['media_id'], retained, other}
    expected = copy.deepcopy(later['data']['canonical_document']['parent_revision']['payload'])
    expected['occurrences'] = document['parent_revision']['payload']['occurrences']
    assert result['parent_revision']['payload'] == expected
    assert result['shot_revisions'] == document['shot_revisions']
    assert result['internal_timeline_revisions'] == document['internal_timeline_revisions']


@pytest.mark.parametrize('dependent', ['registry', 'direct'])
def test_later_reference_refuses_atomically(popup_edit, dependent):
    realm, created, _ = accepted_popup(popup_edit)
    if dependent == 'registry':
        properties(realm, ('parent', ['registry', 'assets', 'later'], {'media_id': NEW, 'type': 'image'}))
    else:
        properties(realm, ('parent/clip:clip-b', ['media_id'], NEW))
    head, entries = copy.deepcopy(realm.head), copy.deepcopy(realm.entries)
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse_call([created['receipt']['receipt_id']])])
    assert error.value.failure['details']['reason'] == 'surviving_dependents'
    assert realm.head == head and realm.entries == entries


def test_guarded_redo_refuses_same_value_registry_aba(popup_edit):
    realm, created, _ = accepted_popup(popup_edit)
    undo = realm.call([inverse_call([created['receipt']['receipt_id']])])
    path = ['registry', 'assets', 'picture', 'opaque']
    properties(realm, (OWNER + '/source', path, 'changed'))
    properties(realm, (OWNER + '/source', path, [None, False]))
    head, entries = copy.deepcopy(realm.head), copy.deepcopy(realm.entries)
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse_call([undo['receipt']['receipt_id']])])
    assert error.value.failure['details']['reason'] == 'intervening_write'
    assert realm.head == head and realm.entries == entries


@pytest.mark.parametrize('damage', ['missing', 'digest', 'foreign', 'revision', 'extra-pin', 'document-pin', 'payload', 'writes'])
def test_corrupt_media_boundary_refuses(popup_edit, damage):
    document, original, plan = popup_edit
    realm, created, records = accepted_popup(popup_edit)
    reloaded = bundle_from_document(created['data']['canonical_document'])
    undone = inverse(reloaded, records)
    before, after = records.load_boundary(records.entries[0])
    if damage == 'missing':
        with pytest.raises(CommandError, match='accepted boundaries'):
            document_from_bundle(undone.bundle, source_bundles=(original,), inverse_records=list(records))
        return
    if damage == 'writes':
        records = copy.deepcopy(records)
        records[0]['writes'] = []
    else:
        image = after['source_mapping']['_runtime_document']
        if damage == 'digest':
            image['parent_revision']['content_digest'] = NEW
        elif damage == 'foreign':
            image['project_id'] = 'other-project'
        elif damage == 'revision':
            image['parent_revision']['revision_id'] = 'wrong-boundary'
        elif damage == 'document-pin':
            image['dependency_manifest']['documents'] = [{
                'document_id': 'other.config', 'version': 1, 'content_digest': NEW}]
        elif damage == 'extra-pin':
            pin = 'sha256:' + '9' * 64
            image['dependency_manifest']['media'].append({'media_id': pin, 'content_digest': pin})
        else:
            image['parent_revision']['payload']['unknown']['corrupt'] = True
            image['parent_revision']['content_digest'] = _digest(image['parent_revision']['payload'])
        records.load_boundary = lambda entry: (before, after)
    from astrid.core.timeline.authoring_bundle import AuthoringBundleError
    with pytest.raises((CommandError, AuthoringBundleError)):
        document_from_bundle(undone.bundle, source_bundles=(original,), inverse_records=records)


@pytest.mark.parametrize('pair', ['create-undo', 'undo-redo'])
def test_multiple_target_pin_changes_use_reverse_publication_order(popup_edit, pair):
    document, _, _ = popup_edit
    realm, created, _ = accepted_popup(popup_edit)
    undo = realm.call([inverse_call([created['receipt']['receipt_id']])])
    targets = [created, undo]
    expected = document
    if pair == 'undo-redo':
        redo = realm.call([inverse_call([undo['receipt']['receipt_id']])])
        targets, expected = [undo, redo], created['data']['canonical_document']
    restored = realm.call([inverse_call([target['receipt']['receipt_id'] for target in targets])])
    assert restored['data']['canonical_document'] == expected


@pytest.mark.parametrize('source', ['missing', 'dependent', 'disjoint'])
def test_retained_document_scope_is_required_before_removing_media(popup_edit, source):
    _, original, _ = popup_edit
    realm, created, records = accepted_popup(popup_edit)
    undone = inverse(bundle_from_document(created['data']['canonical_document']), records)
    content = {'media_id': NEW if source == 'dependent' else 'sha256:' + '8' * 64}
    pin = {'document_id': 'example.config', 'version': 1, 'content_digest': _digest(content)}
    if source != 'missing':
        undone.bundle['source_mapping']['pinned_documents'] = [{
            'pin': pin, 'document': {'content': content}}]
    if source == 'disjoint':
        result = document_from_bundle(undone.bundle, document_pins=[pin], source_bundles=(original,),
                                      inverse_records=records)
        assert result['dependency_manifest']['documents'] == [pin]
        assert NEW not in {row['media_id'] for row in result['dependency_manifest']['media']}
    else:
        with pytest.raises(CommandError) as error:
            document_from_bundle(undone.bundle, document_pins=[pin], source_bundles=(original,),
                                 inverse_records=records)
        assert error.value.kind == ('history_unavailable' if source == 'missing' else 'surviving_dependents')


@pytest.mark.parametrize('damage', ['missing-before', 'foreign-before', 'broken-link'])
def test_provider_refuses_corrupt_boundary_without_partial_acceptance(popup_edit, damage):
    realm, created, _ = accepted_popup(popup_edit)
    previous = realm.entries[-1]['previous']
    if damage == 'missing-before':
        del realm.documents[previous['revision_id']]
    elif damage == 'foreign-before':
        realm.documents[previous['revision_id']]['project_id'] = 'other-project'
    else:
        previous['publication_id'] = 'missing-publication'
    head, entries, count = copy.deepcopy(realm.head), copy.deepcopy(realm.entries), len(realm.accepted)
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse_call([created['receipt']['receipt_id']])])
    assert error.value.failure['details']['reason'] in {'history_unavailable', 'invalid_document'}
    assert realm.head == head and realm.entries == entries and len(realm.accepted) == count



def test_parent_media_introduction_uses_same_framework(popup_edit):
    document, _, _ = popup_edit
    realm = DetachedRealm(document)
    introduced = properties(realm, ('parent', ['registry', 'assets', 'new'],
                                    {'media_id': NEW, 'type': 'image', 'opaque': [None, False]}),
                            ('parent/clip:clip-a', ['asset'], 'new'))
    undo = realm.call([inverse_call([introduced['receipt']['receipt_id']])])
    assert undo['data']['canonical_document'] == document
    redo = realm.call([inverse_call([undo['receipt']['receipt_id']])])
    assert redo['data']['canonical_document'] == introduced['data']['canonical_document']
