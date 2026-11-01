"""Raw parent swaps share the occurrence recipe and preserve authored state."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
from astrid.sdk.timeline._command_state import CommandError, State, overlaps
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.native_occurrence_media_commands import (
    capture_parent_swap_media,
)
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import inverse, later
from tests.editing_conformance.test_recipe_occurrence_media import MediaRealm, inputs
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    runtime_abi,  # noqa: F401 -- detached ABI fixture
)
from tests.sdk.test_timeline_commit_provider import (
    inverse as inverse_call,
)

TARGET = 'parent/clip:clip-a'


def parent_input(kind='video', location='direct'):
    document, old, new, verified = inputs(kind)
    bundle = bundle_from_document(document)
    state = State(bundle)
    row = state.entity(TARGET)
    row.update(opaque=[None, False, {}, []], params=None)
    row.update(copy.deepcopy(state.entity('occurrence:occ-a/clip:clip-a')))
    registry = bundle['parent']['registry']
    registry['assets']['picture']['type'] = kind
    registry['opaque'] = [None, False, {}]
    if location in ('nested', 'both'):
        bundle['parent']['config']['registry'] = copy.deepcopy(registry)
    if location == 'nested':
        del bundle['parent']['registry']
    bundle['parent']['config']['clips'] = copy.deepcopy(bundle['parent']['clips'])
    return bundle, old, new, verified


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('location', ['direct', 'nested', 'both'])
def test_scoped_capture_reload_inverse_and_exact_existing_alias(kind, location):
    bundle, old, new, verified = parent_input(kind, location)
    before = copy.deepcopy(bundle)
    captured = capture_parent_swap_media(bundle, TARGET, old, new, verified_media=verified)
    plan = execute(bundle, [captured], verified_media=verified)
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    assert inverse(plan.bundle, plan.records).bundle == before
    assert bundle == before
    expected = copy.deepcopy(State(before).entity(TARGET))
    expected['asset'] = new['asset']
    assert State(plan.bundle).entity(TARGET) == expected
    assert plan.bundle['shots'] == before['shots']
    assert plan.bundle['placements'] == before['placements']
    assert State(plan.bundle).entity('parent/clip:clip-b') == State(before).entity('parent/clip:clip-b')
    assert captured['created'] == ['parent/asset:' + new['asset']]
    assert [write['ref'] for write in plan.records[0]['writes']] == [
        'parent/asset:' + new['asset'], TARGET]
    output = document_from_bundle(plan.bundle)
    reloaded = bundle_from_document(output)
    # Pure reversal preserves retained pins; accepted ancestry authorizes pin
    # removal, covered by the provider test below. Authored content is exact.
    restored = document_from_bundle(inverse(reloaded, plan.records).bundle, source_bundles=(before,))
    baseline = document_from_bundle(before)
    assert restored['parent_revision']['payload'] == baseline['parent_revision']['payload']
    assert restored['shot_revisions'] == baseline['shot_revisions']
    assert restored['internal_timeline_revisions'] == baseline['internal_timeline_revisions']
    # Existing bindings are required exactly; they never acquire extra fields.
    binding = copy.deepcopy(State(plan.bundle).entity('parent/asset:' + new['asset']))
    for node in (bundle['parent'], bundle['parent']['config']):
        if isinstance(node.get('registry'), dict):
            node['registry']['assets'][new['asset']] = copy.deepcopy(binding)
    captured = capture_parent_swap_media(bundle, TARGET, old, new, verified_media=verified)
    assert captured['created'] == []
    plan = execute(bundle, [captured], verified_media=verified)
    assert inverse(plan.bundle, plan.records).bundle == bundle


def test_disjoint_rebase_aba_identity_and_created_alias_dependencies():
    bundle, old, new, verified = parent_input()
    command = capture_parent_swap_media(bundle, TARGET, old, new, verified_media=verified)
    plan = execute(bundle, [command], verified_media=verified)
    head = copy.deepcopy(bundle)
    State(head).entity('parent/clip:clip-b')['params'] = {'opacity': .5}
    head['parent']['config']['clips'] = copy.deepcopy(head['parent']['clips'])
    change = later('parent/clip:clip-b', ['params'], {'opacity': .5})
    rebased = replay(plan, head, verified_media=verified, intervening_records=[change])
    assert State(inverse(rebased.bundle, rebased.records).bundle).entity('parent/clip:clip-b')['params'] == {'opacity': .5}
    for ref, path, value in ((TARGET, ['asset'], old['asset']),
                             ('parent', ['registry', 'assets', new['asset'], 'type'], 'video')):
        with pytest.raises(CommandError, match='intervening'):
            inverse(plan.bundle, plan.records, [later(ref, path, value)])
    # Equal-state ABA rebases by settled policy; undo above still refuses it.
    assert replay(plan, bundle, verified_media=verified,
                  intervening_records=[later(TARGET, ['asset'], old['asset'])]).records == plan.records
    with pytest.raises(CommandError, match='intervening'):
        replay(plan, bundle, verified_media=verified,
               intervening_records=[later(TARGET, [], State(bundle).entity(TARGET))])
    State(plan.bundle).entity('parent/clip:clip-b')['asset'] = new['asset']
    with pytest.raises(CommandError, match='surviving dependents'):
        inverse(plan.bundle, plan.records)
    assert overlaps(('parent/asset:' + new['asset'], ()),
                    ('parent', ('registry', 'assets', new['asset'], 'type')))


@pytest.mark.parametrize('damage', ['missing-old', 'missing-new', 'kind', 'duration', 'selector',
    'null-selector', 'clear', 'wrong-owner', 'opaque-alias', 'null-registry'])
def test_closed_contract_refuses_atomically(damage):
    bundle, old, new, verified = parent_input()
    command = {'schema_version': 1, 'op': 'clip.swap-media', 'version': '1', 'select': TARGET,
               'args': {'old_media': old, 'media': new}, 'intent': {'ownership': 'parent'}}
    if damage == 'missing-old':
        verified.pop(0)
    elif damage == 'missing-new':
        verified.pop()
    elif damage == 'kind':
        verified[-1]['media_type'] = 'image/png'
    elif damage == 'duration':
        verified[-1]['duration_seconds'] = 5.999
    elif damage in ('selector', 'null-selector'):
        State(bundle).entity(TARGET)['media_id'] = None if damage == 'null-selector' else new['object_id']
    elif damage == 'clear':
        command['args']['media'] = None
    elif damage == 'wrong-owner':
        command['select'] = 'occurrence:occ-a/clip:clip-a'
    elif damage == 'opaque-alias':
        bundle['parent']['registry']['assets'][new['asset']] = {'opaque': None}
    else:
        bundle['parent']['registry'] = None
    before = copy.deepcopy(bundle)
    with pytest.raises((CommandError, ValueError)):
        execute(bundle, [command], verified_media=verified)
    assert bundle == before


@pytest.mark.parametrize('value', [None, '', False])
def test_clear_removes_presence_and_swap_does_not_treat_null_empty_or_false_as_clear(value):
    bundle, old, new, verified = parent_input()
    row = State(bundle).entity(TARGET)
    row['asset'] = value
    with pytest.raises(CommandError):
        capture_parent_swap_media(bundle, TARGET, old, new, verified_media=verified)
    row['asset'] = old['asset']
    command = capture_clip_set(bundle, TARGET, clear=['asset'])
    plan = execute(bundle, [command])
    assert 'asset' not in State(plan.bundle).entity(TARGET)
    assert inverse(plan.bundle, plan.records).bundle == bundle


def test_provider_manifest_reload_undo_and_redo():
    bundle, old, new, verified = parent_input()
    document = document_from_bundle(bundle)
    bundle = bundle_from_document(document)
    command = capture_parent_swap_media(bundle, TARGET, old, new, verified_media=verified)
    realm = MediaRealm(document, verified)
    created = realm.call([command])
    output = created['data']['canonical_document']
    assert output['shot_revisions'] == document['shot_revisions']
    assert output['internal_timeline_revisions'] == document['internal_timeline_revisions']
    undone = realm.call([inverse_call([created['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == document
    redone = realm.call([inverse_call([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == output
    with pytest.raises(CommitRejected):
        realm.call([command])


@pytest.mark.parametrize('window', ['hold', 'from-to', 'null-hold', 'null-window'])
def test_parent_raw_window_and_incoming_transition_survive_without_normalization(window):
    bundle, old, new, verified = parent_input()
    row = State(bundle).entity(TARGET)
    if window == 'hold':
        row.pop('from')
        row.pop('to')
    elif window == 'from-to':
        row.pop('hold')
    elif window == 'null-hold':
        row['hold'] = None
    else:
        row.update({'from': None, 'to': None, 'speed': None})
    row['transition'] = {'type': 'crossfade', 'duration': .5, 'opaque': [False, None]}
    bundle['parent']['config']['clips'] = copy.deepcopy(bundle['parent']['clips'])
    before = copy.deepcopy(row)
    command = capture_parent_swap_media(bundle, TARGET, old, new, verified_media=verified)
    plan = execute(bundle, [command], verified_media=verified)
    assert State(plan.bundle).entity(TARGET) == {**before, 'asset': new['asset']}
    assert inverse(plan.bundle, plan.records).bundle == bundle
