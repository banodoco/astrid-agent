"""Native flat-parent assembly recipes over detached verified media.

No Runtime acceptance, registered-output custody or render recovery is simulated.
"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from astrid.packs.rendering.actions.assemble_timeline.run import (
    build_authoring_proposal,
    derive_asset_key,
    derive_clip_id,
)
from astrid.sdk.timeline import capture_assembled_media, commands
from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import (
    CommandError,
    State,
    equal,
    overlaps,
    present,
)
from astrid.sdk.timeline.commit import (
    _digest,
    _json,
    _reservation_refs,
    bundle_from_document,
    create_runtime_commit_service,
    document_from_bundle,
)
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_clip_delete import delete_document
from tests.editing_conformance.test_recipe_m1_commands import (
    accepted_boundaries,
    inverse,
    later,
)
from tests.packs.rendering.test_assemble_timeline import _payload
from tests.sdk import test_timeline_sync as session_fixtures
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedPort,
    DetachedRealm,
    refusal,
)
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import document_fixture
from tests.sdk.test_timeline_sync import open_session

MEDIA = 'sha256:' + '2' * 64
connected = session_fixtures.connected
FIXTURE = Path(__file__).parent / 'fixtures' / 'recipe_parent_media.json'


def call(op, args, select='parent'):
    return {'schema_version': 1, 'op': op, 'version': '1', 'select': select, 'args': args}


def assembled_inputs():
    payload = _payload()
    payload['spec']['runtime_dependencies']['aggregation']['order'] = [MEDIA, MEDIA]
    for child in payload['resolved_children']:
        child['outputs'][0].update(object_id=MEDIA, media_id=MEDIA, content_sha256=MEDIA)
    assembled = build_authoring_proposal(payload)
    # Fixture inputs stand in for detached selected_media results, never for CAS custody.
    pins = [{'asset': 'variant-' + str(i), 'generation_id': 'generation-' + str(i),
             'variant_id': 'variant-' + str(i), 'object_id': MEDIA,
             'catalog_digest': 'sha256:' + str(i + 3) * 64} for i in (0, 1)]
    verified = [{'selection': pin, 'media_type': 'video/mp4', 'size': 123,
                 'duration_seconds': 2.5} for pin in pins]
    return assembled, pins, verified


def target_bundle(populated=False):
    document = delete_document()
    parent = document['parent_revision']['payload']
    parent['occurrences'] = []
    parent['config']['tracks'][0]['id'] = 'visual'
    parent['config']['tracks'][0]['opaque'] = [None, False, 'keep track']
    rows = parent['clips'] if populated else []
    for row in rows:
        row['track'] = 'visual'
    parent['clips'] = rows
    parent['config']['clips'] = copy.deepcopy(rows)
    document['shot_revisions'] = []
    document['internal_timeline_revisions'] = []
    document['dependency_manifest']['shots'] = []
    document['dependency_manifest']['internal_timelines'] = []
    document['parent_revision']['content_digest'] = _digest(parent)
    return bundle_from_document(document)


def lower(bundle, *, replace_clips=(), guarded=True):
    """Normalize legacy assembly ONCE, before capturing/hashing the settled proposal."""
    assembled, pins, verified = assembled_inputs()
    if guarded:
        calls = capture_assembled_media(bundle, assembled, pins,
                                        replace_clips=iter(replace_clips), verified_media=verified)
    else:
        calls = [call('track.create', {'row': assembled['timeline']['tracks'][0]})]
        calls.extend(call('clip.add-media', {'row': {**row, 'hold': row['to'] - row['from']},
                                            'media': pin})
                     for row, pin in zip(assembled['timeline']['clips'], pins))
    return commands.execute(bundle, calls, verified_media=verified), verified


@pytest.mark.parametrize('populated', [False, True])
def test_assembled_insert_or_explicit_replace_exact_inverse_redo(populated):
    before = target_bundle(populated)
    old = copy.deepcopy(before)
    refs = ['parent/clip:' + row['id'] for row in before['parent']['clips']]
    plan, verified = lower(before, replace_clips=refs)
    assert before == old
    assert replay(plan, before, verified_media=verified).records == plan.records
    result = document_from_bundle(plan.bundle)
    reloaded = bundle_from_document(json.loads(json.dumps(result)))
    undone = inverse(reloaded, plan.records)
    # Pure immutable boundary fixture exercises existing manifest restoration;
    # it is not an acceptance receipt or a provider-acceptance simulation.
    entry = {'revision_id': result['parent_revision']['revision_id'],
             'previous': {'revision_id': before['source_mapping']['_runtime_document']['parent_revision']['revision_id']},
             'records': {'commands': plan.records}}
    history = AcceptedRecords([entry], plan.records, lambda _: (before, reloaded))
    assert document_from_bundle(undone.bundle, source_bundles=(before,), inverse_records=history) == document_from_bundle(before)
    accepted = accepted_boundaries(undone.records, reloaded, undone.bundle)
    assert document_from_bundle(inverse(undone.bundle, accepted).bundle) == result
    parent = result['parent_revision']['payload']
    assert parent['config']['tracks'] == old['parent']['config']['tracks']
    assert parent['config']['output'] == old['parent']['config']['output']
    assert parent['unknown'] == old['parent']['unknown']
    assert parent['registry']['assets']['picture'] == old['parent']['registry']['assets']['picture']
    assert document_from_bundle(before)['dependency_manifest']['media'][0] in result['dependency_manifest']['media']
    assert [row['id'] for row in parent['clips']] == [derive_clip_id('stitch-task-42', i) for i in (0, 1)]
    assert [row['asset'] for row in parent['clips']] == [derive_asset_key('stitch-task-42', i) for i in (0, 1)]
    assert [(row['at'], row['from'], row['to'], row['hold']) for row in parent['clips']] == [
        (0, 0, 1.5, 1.5), (1.5, 0, 2.5, 2.5)]
    media_records = plan.records[-2:]
    for i, record in enumerate(media_records):
        assert record['dependencies'] == [{'key': 'media.new', 'kind': 'media', 'policy': 'exact',
                                            'pin': verified[i]['selection']}]
        alias = derive_asset_key('stitch-task-42', i)
        assert alias != verified[i]['selection']['asset']
        assert {item['ref'] for item in record['created']} == {
            'parent/asset:' + alias, 'parent/clip:' + derive_clip_id('stitch-task-42', i)}
    assert plan.records[0]['writes'] == []
    assert all(write['ref'] != 'parent' for record in plan.records for write in record['writes'])


def test_parent_media_append_creates_visual_track_and_keeps_shared_closure_pins():
    document = document_fixture()
    before = bundle_from_document(document)
    # Generic parent add also composes without replacing existing occurrences.
    plan, _ = lower(before, guarded=False)
    after = document_from_bundle(plan.bundle)
    assert plan.records[0]['command']['op'] == 'track.create'
    for key in ('shot_revisions', 'internal_timeline_revisions'):
        assert after[key] == document[key]
    for key in ('shots', 'internal_timelines', 'documents'):
        assert after['dependency_manifest'][key] == document['dependency_manifest'][key]
    assert after['parent_revision']['payload']['occurrences'] == document['parent_revision']['payload']['occurrences']
    assert inverse(plan.bundle, plan.records).bundle == before


def test_empty_target_creates_first_track_without_overwriting_parent():
    before = target_bundle()
    before['parent']['config']['tracks'] = []
    plan, _ = lower(before)
    assert [record['command']['op'] for record in plan.records] == [
        'timeline.assembled-target', 'track.create', 'clip.add-media', 'clip.add-media']
    assert inverse(plan.bundle, plan.records).bundle == before


def test_ordinary_add_collision_never_becomes_replacement():
    plan, verified = lower(target_bundle())
    before = copy.deepcopy(plan.bundle)
    with pytest.raises(CommandError) as error:
        commands.execute(plan.bundle, plan.commands[-2:], verified_media=verified)
    assert error.value.kind == 'identity_collision'
    assert plan.bundle == before


def test_parent_source_window_and_disjoint_opaque_edit_survive_replay_and_inverse():
    before = target_bundle()
    assembled, pins, verified = assembled_inputs()
    row = {**assembled['timeline']['clips'][0], 'from': .5, 'to': 2, 'hold': 1.5, 'speed': 1.25}
    plan = commands.execute(before, [call('clip.add-media', {'row': row, 'media': pins[0]})],
                            verified_media=verified)
    head = copy.deepcopy(before)
    head['parent']['unknown']['later'] = {'asset': row['asset'], 'literal': None}
    rebased = replay(plan, head, verified_media=verified)
    assert rebased.bundle['parent']['clips'] == [row]
    # Opaque values on the parent are neither normalized nor treated as edges.
    assert inverse(rebased.bundle, rebased.records).bundle == head
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, [later('parent',
            ['registry', 'assets', row['asset'], 'type'], 'video')])


def test_explicit_replacement_refuses_occurrence_ownership():
    before = bundle_from_document(document_fixture())
    refs = ['parent/clip:' + row['id'] for row in before['parent']['clips']]
    with pytest.raises(CommandError, match='flat parent ownership'):
        lower(before, replace_clips=refs)


def test_populated_target_requires_explicit_observed_replacement():
    before = target_bundle(True)
    with pytest.raises(CommandError, match='explicit prior populated target'):
        lower(before)
    assert before == target_bundle(True)


@pytest.mark.parametrize('damage', ['clip-collision', 'asset-collision', 'missing-media',
    'wrong-variant', 'wrong-catalog', 'target-value', 'target-order', 'target-member', 'track'])
def test_original_capture_refuses_collisions_missing_media_and_target_change(damage):
    before = target_bundle(True)
    refs = ['parent/clip:' + row['id'] for row in before['parent']['clips']]
    plan, verified = lower(before, replace_clips=refs)
    head = copy.deepcopy(before)
    if damage == 'clip-collision':
        row = copy.deepcopy(plan.commands[-1]['args']['row'])
        head['parent']['clips'].append(row)
    elif damage == 'asset-collision':
        State(head).registry('parent')['assets'][derive_asset_key('stitch-task-42', 0)] = None
    elif damage == 'missing-media':
        verified.pop()
    elif damage in ('wrong-variant', 'wrong-catalog'):
        verified[0]['selection']['variant_id' if damage == 'wrong-variant' else 'catalog_digest'] = 'other'
    elif damage == 'target-value':
        head['parent']['clips'][0]['opaque']['values'].append('changed')
    elif damage == 'target-order':
        head['parent']['clips'].reverse()
    elif damage == 'target-member':
        head['parent']['clips'].pop()
    else:
        State(head).config('parent')['tracks'][0]['hidden'] = True
    head['parent']['config']['clips'] = copy.deepcopy(head['parent']['clips'])
    immutable = copy.deepcopy(head)
    with pytest.raises(CommandError):
        replay(plan, head, verified_media=verified)
    assert head == immutable


def test_registry_create_or_require_never_upserts_or_removes_reused_binding():
    before = target_bundle()
    alias = derive_asset_key('stitch-task-42', 0)
    pin = assembled_inputs()[1][0]
    binding = {'media_id': MEDIA, 'type': 'video',
               'generationId': pin['generation_id'], 'variantId': pin['variant_id']}
    State(before).registry('parent')['assets'][alias] = binding
    plan, _ = lower(before)
    assert 'parent/asset:' + alias not in plan.commands[-2]['created']
    assert inverse(plan.bundle, plan.records).bundle == before
    binding['opaque'] = None
    with pytest.raises(CommandError, match='existing registry binding'):
        lower(before)


def test_intervening_aba_and_later_created_asset_dependents_refuse_undo():
    before = target_bundle()
    plan, _ = lower(before)
    ref = 'parent/asset:' + derive_asset_key('stitch-task-42', 0)
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, [later(ref, ['type'], 'video')])
    head = copy.deepcopy(plan.bundle)
    row = {**head['parent']['clips'][0], 'id': 'later-clip'}
    head['parent']['clips'].append(row)
    head['parent']['config']['clips'] = copy.deepcopy(head['parent']['clips'])
    with pytest.raises(CommandError):
        inverse(head, plan.records)


@pytest.mark.parametrize('args', [{'replace_clips': [], 'extra': None}, {'replace_clips': ['parent/clip:x'] * 2},
    {'replace_clips': ['occurrence:x/clip:y']}, {}])
def test_target_guard_args_are_closed(args):
    with pytest.raises(CommandError):
        commands.execute(target_bundle(), [call('timeline.assembled-target', args)])


def test_settled_assembly_normalization_numeric_forwarding_and_normal_outbox(connected):
    from astrid.sdk.timeline import build_producer_batch
    from tests.editing_conformance.test_settled_proposal_contract import (
        check_forwarding,
    )
    plan, verified = lower(target_bundle())
    proposal = {'schema_version': 1, 'producer': {'task_id': 'stitch-task-42', 'attempt_id': 'attempt',
        'executor_id': 'worker', 'fence': 1, 'runtime_epoch': 2},
        'admitted_target': {'project_id': 'p', 'timeline_id': 't',
            'base_revision_id': 'r0', 'base_publication_id': 'pub0'},
        'commands': plan.commands, 'rationale': 'Review café assembly'}
    proposal['proposal_digest'] = _digest(proposal)
    raw = _json(proposal).encode()
    forwarded = subprocess.run(['node', '-e',
        "process.stdout.write(JSON.stringify(JSON.parse(require('fs').readFileSync(0,'utf8'))))"],
        input=raw, capture_output=True, check=True, timeout=15)
    forwarded = json.loads(forwarded.stdout)
    assert type(proposal['commands'][-2]['args']['row']['at']) is float
    assert type(forwarded['commands'][-2]['args']['row']['at']) is int
    batch = build_producer_batch(forwarded)
    descriptor = {'size': len(raw), 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}
    check_forwarding(batch, descriptor, raw=raw)
    assert equal(batch['commands'][1:], plan.commands)
    assert commands.execute(target_bundle(), batch['commands'], verified_media=verified).bundle == plan.bundle
    session = open_session(connected)
    assert session.queue_proposal(forwarded) == batch['commands']
    state = session.outbox.read()
    assert state['base'] == {'revision_id': 'r0', 'publication_id': 'pub0'}
    assert state['pending'] == batch['commands']
    assert state['rationale'] == proposal['rationale']
    changed = copy.deepcopy(batch)
    changed['commands'][-2]['args']['row']['at'] = True
    with pytest.raises(ValueError, match='forwarded commands'):
        check_forwarding(changed, descriptor, raw=raw)


@pytest.mark.parametrize('storage', ['direct', 'config', 'mirrored'])
def test_parent_media_registry_storage_shapes_exact_roundtrip(storage):
    before = target_bundle()
    parent = before['parent']
    parent['registry']['opaque'] = {'literal': [None, False, 'preserve registry']}
    if storage != 'direct':
        parent['config']['registry'] = copy.deepcopy(parent['registry'])
    if storage == 'config':
        del parent['registry']
    plan, verified = lower(before)
    after = plan.bundle['parent']
    assert ('registry' in after) == (storage != 'config')
    assert ('registry' in after['config']) == (storage != 'direct')
    if storage == 'mirrored':
        assert after['registry'] == after['config']['registry']
        assert after['registry'] is not after['config']['registry']
    assert State(plan.bundle).registry('parent')['opaque'] == State(before).registry('parent')['opaque']
    published = document_from_bundle(plan.bundle)
    assert published['parent_revision']['payload'] == {**after, 'occurrences': []}
    assert bundle_from_document(published)['parent'] == after
    assert 'parent/asset:picture' in _reservation_refs(State(before))
    assert replay(plan, before, verified_media=verified).records == plan.records
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == before
    assert inverse(undone.bundle, accepted_boundaries(undone.records, plan.bundle, before)).bundle == plan.bundle


@pytest.mark.parametrize('damage', ['conflicting', 'null-direct', 'null-config', 'opaque-direct',
                                   'opaque-config', 'null-assets'])
def test_parent_registry_unsupported_shapes_refuse_without_relocation(damage):
    before = target_bundle()
    parent = before['parent']
    if damage == 'conflicting':
        parent['config']['registry'] = {'assets': {}, 'different': True}
    elif damage in ('null-direct', 'opaque-direct'):
        parent['registry'] = None if damage == 'null-direct' else ['opaque']
    elif damage in ('null-config', 'opaque-config'):
        parent['config']['registry'] = None if damage == 'null-config' else False
    else:
        parent['registry']['assets'] = None
    immutable = copy.deepcopy(before)
    with pytest.raises(CommandError):
        lower(before)
    assert before == immutable


@pytest.mark.parametrize('damage', ['relocate', 'mirror', 'source-window', 'fps'])
def test_parent_storage_source_and_rate_observations_guard_original_capture(damage):
    before = target_bundle()
    plan, verified = lower(before)
    head = copy.deepcopy(before)
    if damage == 'relocate':
        head['parent']['config']['registry'] = head['parent'].pop('registry')
    elif damage == 'mirror':
        head['parent']['config']['registry'] = copy.deepcopy(head['parent']['registry'])
    elif damage == 'source-window':
        head['parent']['config']['source_window'] = {'from': 0, 'to': 10}
    else:
        head['parent']['config']['output']['fps'] = {'numerator': 24000, 'denominator': 1001}
    immutable = copy.deepcopy(head)
    with pytest.raises(CommandError):
        replay(plan, head, verified_media=verified)
    assert head == immutable


@pytest.mark.parametrize('path', [['registry', 'assets'], ['config', 'registry', 'assets']])
def test_registry_physical_aliases_share_intervening_write_guards(path):
    plan, _ = lower(target_bundle())
    alias = derive_asset_key('stitch-task-42', 0)
    assert overlaps(('parent/asset:' + alias, ['type']), ('parent', [*path, alias, 'type']))
    assert overlaps(('parent/registry', ['assets', alias]), ('parent/asset:' + alias, []))
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, [later('parent', [*path, alias, 'type'], 'video')])


def test_equal_registry_mirrors_receive_only_the_same_asset_leaf_write():
    before = target_bundle()
    parent = before['parent']
    parent['config']['registry'] = copy.deepcopy(parent['registry'])
    value = {'media_id': MEDIA, 'type': 'video'}
    state = State(before)
    state.set('parent/asset:reserved', [], present(value))
    state.set('parent/asset:reserved', ['type'], present('image'))
    assert parent['registry'] == parent['config']['registry']
    assert state.entity('parent/asset:reserved') == {**value, 'type': 'image'}
    assert parent['registry']['assets']['picture']['type'] == 'image'


def test_parent_media_undo_uses_canonical_clips_and_retains_unrelated_registry_edits():
    before = target_bundle()
    plan, verified = lower(before)
    head = copy.deepcopy(before)
    head['parent']['registry']['assets']['unrelated'] = {'opaque': [None, False]}
    head['parent']['registry']['extension'] = {'literal': None}
    head['parent']['config']['opaque'] = {'asset': derive_asset_key('stitch-task-42', 0)}
    rebased = replay(plan, head, verified_media=verified)
    # Both mirrors still contain added clips when staged reversal begins.
    assert rebased.bundle['parent']['config']['clips'] == rebased.bundle['parent']['clips']
    assert inverse(rebased.bundle, rebased.records).bundle == head


@pytest.mark.parametrize('selector', ['asset', 'asset_ref', 'owner_ref'])
def test_created_parent_asset_with_surviving_declared_dependent_refuses_undo(selector):
    plan, _ = lower(target_bundle())
    alias = derive_asset_key('stitch-task-42', 0)
    head = copy.deepcopy(plan.bundle)
    head['parent']['config']['effects'] = [{selector: alias if selector == 'asset' else 'parent/asset:' + alias}]
    original = copy.deepcopy(head)
    with pytest.raises(CommandError, match='surviving dependents'):
        inverse(head, plan.records)
    assert head == original


@pytest.mark.parametrize('dependency', ['transition', 'nested', 'shot', 'relation', 'config-sequence'])
def test_unsupported_replacement_retains_original_target_and_proposal(dependency):
    before = target_bundle(True)
    row = before['parent']['clips'][-1]
    if dependency == 'transition':
        row['transition'] = {'type': 'crossfade', 'duration': .5}
    elif dependency == 'nested':
        row['children'] = []
    elif dependency == 'shot':
        row['clipType'] = 'shot'
    elif dependency == 'relation':
        row['app'] = {'astrid': {'on': 'end'}}
    else:
        before['parent']['config']['sequences'] = {'seq': ['literal']}
    before['parent']['config']['clips'] = copy.deepcopy(before['parent']['clips'])
    immutable = copy.deepcopy(before)
    assembled, pins, verified = assembled_inputs()
    proposal = copy.deepcopy((assembled, pins))
    refs = ['parent/clip:' + row['id'] for row in before['parent']['clips']]
    with pytest.raises(CommandError):
        capture_assembled_media(before, assembled, pins, replace_clips=refs, verified_media=verified)
    assert before == immutable
    assert (assembled, pins) == proposal


@pytest.mark.parametrize('damage', ['extra', 'media-extra', 'missing-media-field',
                                   'select', 'intent', 'version', 'nonfinite'])
def test_parent_add_schema_is_closed_before_writing(damage):
    assembled, pins, verified = assembled_inputs()
    row = {**assembled['timeline']['clips'][0], 'hold': 1.5}
    command = call('clip.add-media', {'row': row, 'media': pins[0]})
    if damage == 'extra':
        command['args']['extra'] = None
    elif damage == 'media-extra':
        pins[0]['extra'] = None
    elif damage == 'missing-media-field':
        del pins[0]['catalog_digest']
    elif damage == 'select':
        command['select'] = 'occurrence:child'
    elif damage == 'intent':
        command['intent'] = {'ownership': 'occurrence'}
    elif damage == 'version':
        command['version'] = '2'
    else:
        row['at'] = float('nan')
    before = target_bundle()
    immutable = copy.deepcopy(before)
    with pytest.raises(CommandError):
        commands.execute(before, [command], verified_media=verified)
    assert before == immutable


def test_assembly_normalization_preserves_literals_and_explicit_hold():
    before = target_bundle()
    before['parent']['config']['output']['fps'] = {'numerator': 30000, 'denominator': 1001}
    assembled, pins, verified = assembled_inputs()
    row = assembled['timeline']['clips'][0]
    row.update(at=.123456789, **{'from': .25, 'to': 1.75, 'speed': 1.25, 'hold': 1.5},
               opaque={'values': [None, False, {'asset': row['asset']}]})
    original = copy.deepcopy((before, assembled, pins, verified))
    captured = capture_assembled_media(before, assembled, pins, replace_clips=[], verified_media=verified)
    assert captured[-2]['args']['row'] == row
    assert 'hold' not in assembled['timeline']['clips'][1]
    plan = commands.execute(before, captured, verified_media=verified)
    assert State(plan.bundle).collection('parent', 'clip')[0] == row
    assert inverse(plan.bundle, plan.records).bundle == before
    assert (before, assembled, pins, verified) == original
    # [from,to) permits exactly the intrinsic end, but not a longer source window.
    pins[0]['object_id'] = 'sha256:' + '9' * 64
    with pytest.raises(CommandError, match='ordered child output'):
        capture_assembled_media(before, assembled, pins, replace_clips=[], verified_media=verified)


@pytest.mark.parametrize('damage', ['source-window', 'source-overrun', 'speed', 'track-kind'])
def test_parent_media_literal_source_and_destination_guards(damage):
    before = target_bundle()
    assembled, pins, verified = assembled_inputs()
    if damage == 'source-window':
        before['parent']['config']['source_window'] = {'from': 1, 'to': 2}
    elif damage == 'source-overrun':
        row = assembled['timeline']['clips'][1]
        row['to'] = 2.500000001
    elif damage == 'speed':
        assembled['timeline']['clips'][0]['speed'] = 0
    else:
        before['parent']['config']['tracks'][0]['kind'] = 'audio'
    original = copy.deepcopy(before)
    with pytest.raises(CommandError):
        capture_assembled_media(before, assembled, pins, replace_clips=[], verified_media=verified)
    assert before == original


def test_native_parent_media_golden_is_the_exported_capture():
    golden = json.loads(FIXTURE.read_text())
    plan = commands.execute(golden['bundle'], golden['commands'], verified_media=golden['verified_media'])
    assert plan.records == golden['records']
    assert plan.bundle == golden['after_bundle']
    assert plan.commands == capture_assembled_media(golden['bundle'], assembled_inputs()[0],
        assembled_inputs()[1], replace_clips=[], verified_media=golden['verified_media'])


@pytest.mark.parametrize('missing_media', [False, True])
def test_provider_preview_reads_exact_parent_media_selections_without_accepting(missing_media):
    before = target_bundle()
    plan, verified = lower(before)
    realm = DetachedRealm(document_from_bundle(before))
    context = realm.context(plan.commands, dry_run=True)
    expected = commands.execute(before, plan.commands, verified_media=verified,
                                commit_request=context['request'], evaluated_head=realm.head)
    observed = []

    class ParentMediaPort(DetachedPort):
        def preview(self, prepared):
            return copy.deepcopy(prepared)

        def accept(self, prepared):
            raise AssertionError("preview must not accept")

        def selected_media(self, pin):
            observed.append(copy.deepcopy(pin))
            if missing_media:
                raise refusal('dependency_unavailable')
            return copy.deepcopy(next(row for row in verified if equal(row['selection'], pin)))

    if missing_media:
        with pytest.raises(CommitRejected, match='dependency_unavailable'):
            create_runtime_commit_service().commit(context, ParentMediaPort(realm, context))
    else:
        prepared = create_runtime_commit_service().commit(context, ParentMediaPort(realm, context))
        assert observed == [row['selection'] for row in verified]
        assert prepared['records']['commands'] == expected.records
        assert prepared['canonical_document'] == document_from_bundle(plan.bundle)
    assert not realm.accepted
