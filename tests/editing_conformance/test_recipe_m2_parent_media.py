"""clip.add-media v1: parent-owned media birth, replacement, guarded inverse and replay."""
from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path

import pytest

from astrid.core.timeline.shot_composition_projection import (
    project_runtime_parent_composition,
)
from astrid.sdk.timeline._collections import order, restoration_orders
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.capture import capture_clip_delete
from astrid.sdk.timeline.commands import execute, validate_command
from astrid.sdk.timeline.commit import (
    _digest,
    _manifest,
    bundle_from_document,
    document_from_bundle,
)
from astrid.sdk.timeline.native_auto_placement import auto_place_completion
from astrid.sdk.timeline.native_parent_media_commands import (
    capture_clip_add_media,
    capture_clip_replace_media,
    capture_parent_completion,
)
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import (
    accepted_boundaries,
    inverse,
    later,
)
from tests.editing_conformance.test_recipe_occurrence_media import MediaRealm, inputs
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    runtime_abi,  # noqa: F401 -- autouse detached ABI fixture
)
from tests.sdk.test_timeline_commit_provider import (
    inverse as inverse_call,
)

FIXTURE = Path(__file__).parent / 'fixtures' / 'recipe_m2_parent_media.json'
CHILD = 'parent/clip:born'
ASSET = 'parent/asset:reserved-new'


def parent_inputs(kind='image'):
    """The occurrence fixture plus a free parent lane 'overlay' holding one media clip."""
    document, old, new, verified = inputs(kind)
    payload = document['parent_revision']['payload']
    payload['config']['tracks'].append({'id': 'overlay', 'kind': 'visual', 'role': 'output'})
    for identity, at in (('ov-a', 0), ('ov-b', 5)):
        clip = {'id': identity, 'track': 'overlay', 'asset': 'picture', 'clipType': 'media', 'at': at, 'hold': 2}
        payload['config']['clips'].append(copy.deepcopy(clip))
        payload['clips'].append(copy.deepcopy(clip))
    document['parent_revision']['content_digest'] = _digest(payload)
    document['dependency_manifest'] = _manifest(document['shot_revisions'],
        document['internal_timeline_revisions'], document['dependency_manifest']['media'], [])
    return document, old, new, verified


def args(new, kind, **changes):
    # Completion scalars are a thin builder; only the complete row crosses the wire.
    row = {'id': 'born', 'clipType': kind, 'asset': new['asset'], 'track': 'overlay',
           'at': 3, 'hold': 4 if kind == 'video' else 1.25}
    if kind == 'video':
        row.update({'from': 2, 'to': 6, 'speed': 2})
    row.update(changes)
    fps = Fraction(30000, 1001)
    row['at'] = float(Fraction(round(row['at'] * fps), 1) / fps)
    return {'row': row, 'media': new}


def capture(bundle, value, verified):
    return capture_clip_add_media(bundle, value['row'], value['media'], verified_media=verified)


def calls(command):
    return command if isinstance(command, list) else [command]


def birth(command):
    return command[1] if isinstance(command, list) else command


def flat_document(document):
    document = copy.deepcopy(document)
    document['parent_revision']['payload']['occurrences'] = []
    document['shot_revisions'] = []
    document['internal_timeline_revisions'] = []
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    document['dependency_manifest'] = _manifest([], [], document['dependency_manifest']['media'], [])
    return document


def captured(kind='image', **changes):
    document, old, new, verified = parent_inputs(kind)
    bundle = bundle_from_document(document)
    command = capture(bundle, args(new, kind, **changes), verified)
    return document, bundle, command, verified


def plain(command):
    return {key: command[key] for key in ('schema_version', 'op', 'version', 'select', 'args', 'intent')}


def edit(bundle, ref, **values):
    """Write both parent clip mirrors, as every accepted edit does."""
    State(bundle).entity(ref).update(values)
    other = bundle['parent']['clips'] if State(bundle).parent_clip_path == ('config', 'clips') else (
        State(bundle).config('parent')['clips'])
    other[:] = copy.deepcopy(State(bundle).collection('parent', 'clip'))


def clip_ids(bundle):
    return [row['id'] for row in State(bundle).collection('parent', 'clip')]


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_exact_capture_replay_scoped_birth_and_inverse(kind):
    document, bundle, command, verified = captured(kind)
    before = copy.deepcopy(bundle)
    validate_command(command)
    plan = execute(bundle, calls(command), verified_media=verified)
    assert plan.commands == [command]
    assert bundle == before
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == before
    redone = inverse(undone.bundle, accepted_boundaries(undone.records, plan.bundle, undone.bundle))
    assert redone.bundle == plan.bundle
    assert birth(command)['created'] == [ASSET, CHILD]
    assert plan.bundle['placements'] == before['placements'] and plan.bundle['shots'] == before['shots']
    row = State(plan.bundle).entity(CHILD)
    assert (row['asset'], row['track'], row['clipType']) == ('reserved-new', 'overlay', kind)
    assert row['at'] == pytest.approx(round(3 * 30000 / 1001) * 1001 / 30000)
    if kind == 'video':
        assert (row['from'], row['to'], row['hold'], row['speed']) == (2, 6, 4, 2)
    assert State(plan.bundle).entity(ASSET)['media_id'] == command['args']['media']['object_id']
    assert clip_ids(plan.bundle)[-1] == 'born'
    output = document_from_bundle(plan.bundle)
    assert command['args']['media']['object_id'] in {pin['media_id'] for pin in output['dependency_manifest']['media']}


def test_portable_golden():
    golden = json.loads(FIXTURE.read_text())
    document, bundle, command, verified = captured('video')
    plan = execute(bundle, calls(command), verified_media=verified)
    assert golden == {'input': document, 'verified_media': verified, 'commands': [command],
                      'records': plan.records, 'output': document_from_bundle(plan.bundle)}


@pytest.mark.parametrize('existing', ['absent', 'without-duration', 'with-duration'])
def test_video_birth_retains_verified_intrinsic_duration_and_exact_inverse(existing):
    _, bundle, command, verified = captured('video')
    if existing != 'absent':
        pin = command['args']['media']
        entry = {'media_id': pin['object_id'], 'type': 'video',
                 'generationId': pin['generation_id'], 'variantId': pin['variant_id']}
        if existing == 'with-duration':
            entry['duration'] = 6
        State(bundle).registry('parent')['assets'][pin['asset']] = entry
        command = capture(bundle, command['args'], verified)
    before = copy.deepcopy(bundle)
    plan = execute(bundle, [command], verified_media=verified)
    row = State(plan.bundle).entity(CHILD)
    assert row['hold'] == 4
    assert State(plan.bundle).registry('parent')['assets'][row['asset']]['duration'] == 6
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    assert inverse(plan.bundle, plan.records).bundle == before


def test_existing_binding_is_required_exactly_without_birth():
    _, bundle, command, verified = captured()
    assets = State(bundle).registry('parent')['assets']
    pin = command['args']['media']
    assets['reserved-new'] = {'media_id': pin['object_id'], 'type': 'image',
                              'generationId': pin['generation_id'], 'variantId': pin['variant_id']}
    plan = execute(bundle, [plain(command)], verified_media=verified)
    assert plan.commands[0]['created'] == [CHILD]
    assert inverse(plan.bundle, plan.records).bundle == bundle
    assets['reserved-new']['opaque'] = None
    with pytest.raises(CommandError, match='existing registry binding'):
        execute(bundle, [plain(command)], verified_media=verified)


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('storage', ['absent', 'direct', 'config', 'mirrored'])
def test_missing_registry_containers_are_guarded_writes_with_exact_inverse_redo(kind, storage):
    document, _, new, verified = parent_inputs(kind)
    bundle = bundle_from_document(document)
    parent = bundle['parent']
    parent['clips'] = parent['config']['clips'] = []
    parent.pop('registry')
    paths = [] if storage == 'absent' else [['registry']] if storage == 'direct' else (
        [['config', 'registry']] if storage == 'config' else [['registry'], ['config', 'registry']])
    for path in paths:
        target = parent if len(path) == 1 else parent['config']
        target['registry'] = {'opaque': {'values': [None, False, {}, []]}}
    immutable = copy.deepcopy(bundle)
    command = capture(bundle, args(new, kind), verified)
    plan = execute(bundle, [command], verified_media=verified)
    writes = plan.records[0]['writes']
    expected = ([{'ref': 'parent', 'path': ['registry'], 'before': {'state': 'absent'},
                  'after': {'state': 'present', 'value': {}}}] if storage == 'absent' else [])
    containers = ([('parent', ['registry', 'assets'])] if storage == 'mirrored' else
                  [('parent', [*path, 'assets']) for path in paths or [['registry']]])
    expected.extend({'ref': ref, 'path': path, 'before': {'state': 'absent'},
                     'after': {'state': 'present', 'value': {}}} for ref, path in containers)
    assert writes[:-2] == expected
    assert [write['ref'] for write in writes[-2:]] == [ASSET, CHILD]
    assert bundle == immutable
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    if storage == 'mirrored':
        assert plan.bundle['parent']['registry'] == plan.bundle['parent']['config']['registry']
    for path in paths:
        target = plan.bundle['parent'] if len(path) == 1 else plan.bundle['parent']['config']
        assert target['registry']['opaque'] == {'values': [None, False, {}, []]}
    reloaded = bundle_from_document(document_from_bundle(plan.bundle))
    undone = inverse(reloaded, plan.records)
    assert undone.bundle['parent'] == parent
    redone = inverse(undone.bundle, accepted_boundaries(undone.records, reloaded, undone.bundle))
    assert document_from_bundle(redone.bundle) == document_from_bundle(plan.bundle)
    # Even an equal empty container authored since capture changes its presence.
    head = copy.deepcopy(bundle)
    target = head['parent'] if storage != 'config' else head['parent']['config']
    if storage == 'absent':
        target['registry'] = {}
    else:
        target['registry']['assets'] = {}
    immutable = copy.deepcopy(head)
    with pytest.raises(CommandError):
        execute(head, [command], verified_media=verified)
    assert head == immutable


@pytest.mark.parametrize('path', [['registry'], ['config', 'registry']])
@pytest.mark.parametrize('value', [None, False, [], 'opaque'])
def test_explicit_malformed_asset_containers_refuse_untouched(path, value):
    document, _, new, verified = parent_inputs()
    bundle = bundle_from_document(document)
    parent = bundle['parent']
    registry = parent.pop('registry')
    target = parent if len(path) == 1 else parent['config']
    target['registry'] = {**registry, 'assets': value}
    immutable = copy.deepcopy(bundle)
    with pytest.raises(CommandError):
        capture(bundle, args(new, 'image'), verified)
    assert bundle == immutable


@pytest.mark.parametrize('damage', ['missing-input', 'wrong-object', 'wrong-catalog', 'intent', 'occurrence',
    'no-track', 'occurrence-lane', 'hidden-track', 'audio-track', 'occupied-id', 'image-window', 'image-speed',
    'window', 'past-media', 'zero-hold', 'negative-at', 'boolean-speed', 'extra-arg', 'two-registries',
    'occupied-alias', 'birth', 'membership'])
def test_refusals_preserve_input(damage):
    kind = 'image' if damage.startswith('image') else 'video'
    _, bundle, command, verified = captured(kind)
    command = [plain(item) for item in command] if isinstance(command, list) else plain(command)
    if damage == 'missing-input':
        verified.pop()
    elif damage in ('wrong-object', 'wrong-catalog'):
        command['args']['media']['object_id' if damage == 'wrong-object' else 'catalog_digest'] = 'sha256:' + '9' * 64
    elif damage == 'intent':
        command['intent'] = {'ownership': 'occurrence'}
    elif damage == 'occurrence':
        command['select'] = 'occurrence:occ-a'
    elif damage == 'no-track':
        command['args']['row']['track'] = 'nowhere'
    elif damage == 'occurrence-lane':
        command['args']['row']['track'] = 'picture'
    elif damage == 'hidden-track':
        State(bundle).entity('parent/track:overlay')['hidden'] = True
    elif damage == 'audio-track':
        State(bundle).entity('parent/track:overlay')['kind'] = 'audio'
    elif damage == 'occupied-id':
        command['args']['row']['id'] = 'ov-a'
    elif damage == 'image-window':
        command['args']['row'].update({'from': 0, 'to': 1.25})
    elif damage == 'image-speed':
        command['args']['row']['speed'] = 2
    elif damage == 'window':
        command['args']['row']['to'] = 7
    elif damage == 'past-media':
        command['args']['row'].update({'from': 4, 'to': 8})
    elif damage == 'zero-hold':
        command['args']['row']['hold'] = 0
    elif damage == 'negative-at':
        command['args']['row']['at'] = -1
    elif damage == 'boolean-speed':
        command['args']['row']['speed'] = True
    elif damage == 'extra-arg':
        command['args']['extra'] = None
    elif damage == 'two-registries':
        State(bundle).config('parent')['registry'] = {'assets': {}, 'different': True}
    elif damage == 'occupied-alias':
        State(bundle).registry('parent')['assets']['reserved-new'] = None
    elif damage == 'birth':
        command['created'] = []
    else:
        State(bundle).config('parent')['clips'].insert(0, {'id': 'other', 'track': 'overlay'})
        bundle['parent']['clips'].insert(0, {'id': 'other', 'track': 'overlay'})
        command['selections'] = captured('video')[2]['selections']
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError):
        execute(bundle, calls(command), verified_media=verified)
    assert bundle == before


def replaced(**changes):
    document, _, new, verified = parent_inputs('video')
    bundle = bundle_from_document(document)
    value = args(new, 'video', **changes)
    command = capture_clip_replace_media(bundle, 'parent/clip:ov-a', value['row'], new,
                                         verified_media=verified)
    return document, bundle, command, verified


def test_replace_removes_the_populated_clip_and_inverse_restores_its_slot():
    _, bundle, command, verified = replaced()
    plan = execute(bundle, calls(command), verified_media=verified)
    assert clip_ids(bundle) == ['clip-a', 'clip-b', 'ov-a', 'ov-b']
    assert clip_ids(plan.bundle) == ['clip-a', 'clip-b', 'born', 'ov-b']
    assert birth(command)['created'] == [ASSET, CHILD]
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == bundle
    redone = inverse(undone.bundle, accepted_boundaries(undone.records, plan.bundle, undone.bundle))
    assert redone.bundle == plan.bundle


def ordered_parent(kind='image'):
    document, _, new, verified = parent_inputs(kind)
    bundle = bundle_from_document(flat_document(document))
    # Simultaneous clips on the same track have equal z priority; array order
    # must reach the composition draw list unchanged.
    rows = [{'id': identity, 'track': 'overlay', 'asset': 'picture', 'clipType': 'media',
             'at': 0, 'hold': 4, 'nullable': None, 'opaque': {'values': [None, False, {}, []]}}
            for identity in ('A', 'target', 'C')]
    bundle['parent']['clips'] = rows
    bundle['parent']['config']['clips'] = copy.deepcopy(rows)
    return bundle, new, verified


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('target', ['A', 'target', 'C'])
def test_replace_first_middle_last_exact_reload_inverse_redo_and_replay(kind, target):
    bundle, new, verified = ordered_parent(kind)
    command = capture_clip_replace_media(bundle, 'parent/clip:' + target, args(new, kind, at=0)['row'], new, verified_media=verified)
    plan = execute(bundle, calls(command), verified_media=verified)
    expected = ['born' if identity == target else identity for identity in ['A', 'target', 'C']]
    assert clip_ids(plan.bundle) == expected
    assert restoration_orders(plan.records) == {('parent', 'clip'): (
        order(State(bundle), 'parent', 'clip'), order(State(plan.bundle), 'parent', 'clip'))}
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    reloaded = bundle_from_document(document_from_bundle(plan.bundle))
    undone = inverse(reloaded, plan.records)
    assert undone.bundle['parent'] == bundle['parent']
    assert all(s['members'] == s['order'] == s['boundaries'] for s in undone.commands[0]['selections']
               if s['key'] == 'collection.clips')
    assert replay(undone, reloaded, inverse_records=plan.records).records == undone.records
    accepted = accepted_boundaries(undone.records, reloaded, undone.bundle)
    redone = inverse(undone.bundle, accepted)
    assert document_from_bundle(redone.bundle) == document_from_bundle(plan.bundle)
    accepted_redo = accepted_boundaries(redone.records, undone.bundle, redone.bundle)
    assert inverse(redone.bundle, accepted_redo).bundle == undone.bundle


def test_replace_preserves_equal_z_order_in_composition_input():
    bundle, new, verified = ordered_parent()
    command = capture_clip_replace_media(bundle, 'parent/clip:target', args(new, 'image', at=0)['row'], new, verified_media=verified)
    plan = execute(bundle, calls(command), verified_media=verified)
    document = document_from_bundle(plan.bundle)
    parent = dict(document['parent_revision'], project_id=document['project_id'], timeline_id=document['timeline_id'])
    projection = project_runtime_parent_composition(parent, shot_revisions=document['shot_revisions'],
        internal_timeline_revisions=document['internal_timeline_revisions'])
    same_z = [row for row in projection.config['clips'] if row['track'] == 'overlay']
    assert [row['id'] for row in same_z] == ['A', 'born', 'C']
    assert all(row['at'] == 0 and row['hold'] > 0 for row in same_z)


@pytest.mark.parametrize('damage', ['order', 'membership'])
@pytest.mark.parametrize('undo', [False, True])
def test_replace_keeps_order_and_membership_guards(damage, undo):
    _, bundle, command, verified = replaced()
    plan = execute(bundle, calls(command), verified_media=verified)
    head = copy.deepcopy(plan.bundle if undo else bundle)
    if damage == 'order':
        head['parent']['clips'].reverse()
        head['parent']['config']['clips'].reverse()
    else:
        for rows in (head['parent']['clips'], head['parent']['config']['clips']):
            rows.append({'id': 'other', 'track': 'overlay', 'at': 9, 'hold': 1})
    before = copy.deepcopy(head)
    with pytest.raises(CommandError) as error:
        if undo:
            inverse(head, plan.records)
        else:
            replay(plan, head, verified_media=verified)
    assert error.value.kind == 'selection_changed'
    assert head == before


@pytest.mark.parametrize('field', ['members', 'boundaries'])
def test_replace_refuses_corrupt_insertion_proof(field):
    _, bundle, command, verified = replaced()
    plan = execute(bundle, calls(command), verified_media=verified)
    records = copy.deepcopy(plan.records)
    selected = next(s for s in records[1]['selections'] if s['key'].endswith('.after'))
    selected[field] = ['parent/clip:ov-b']
    before = copy.deepcopy(plan.bundle)
    with pytest.raises(CommandError):
        inverse(plan.bundle, records)
    assert plan.bundle == before


@pytest.mark.parametrize('create_first', [False, True])
def test_replacement_composes_with_create_remove_neighbors(create_first):
    bundle, new, verified = ordered_parent()
    add = capture_clip_replace_media(bundle, 'parent/clip:target', args(new, 'image')['row'], new, verified_media=verified)
    delete = capture_clip_delete(bundle, 'parent/clip:A')
    first = add if create_first else delete
    middle = execute(bundle, calls(first), verified_media=verified).bundle
    second = (capture_clip_delete(middle, 'parent/clip:A') if create_first else
              capture_clip_replace_media(middle, 'parent/clip:target', args(new, 'image')['row'], new, verified_media=verified))
    plan = execute(bundle, [*calls(first), *calls(second)], verified_media=verified)
    assert clip_ids(plan.bundle) == ['born', 'C']
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == bundle
    accepted = accepted_boundaries(undone.records, plan.bundle, undone.bundle)
    assert inverse(undone.bundle, accepted).bundle == plan.bundle


@pytest.mark.parametrize('damage', ['missing', 'other-track', 'not-media', 'unrelated-dependency', 'self'])
def test_replace_refuses_without_a_populated_same_track_target(damage):
    _, bundle, command, verified = replaced()
    target = 'parent/clip:ov-a'
    if damage == 'missing':
        target = 'parent/clip:gone'
    elif damage == 'other-track':
        target = 'parent/clip:clip-a'
    elif damage == 'not-media':
        edit(bundle, target, asset=None)
    elif damage == 'unrelated-dependency':
        edit(bundle, 'parent/clip:ov-b', app={'astrid': {'on': 'word:x:0'}})
    else:
        target = CHILD
    before = copy.deepcopy(bundle)
    row, pin = birth(command)['args']['row'], birth(command)['args']['media']
    with pytest.raises(CommandError):
        capture_clip_replace_media(bundle, target, row, pin, verified_media=verified)
    assert bundle == before


@pytest.mark.parametrize('replace', [False, True])
def test_asset_alias_inverse_is_guarded_by_dependents_and_aba(replace):
    _, bundle, command, verified = replaced() if replace else captured()
    plan = execute(bundle, calls(command), verified_media=verified)
    edit(plan.bundle, 'parent/clip:ov-b', asset='reserved-new')
    with pytest.raises(CommandError, match='surviving dependents'):
        inverse(plan.bundle, plan.records)
    for ref, path in ((CHILD, ['hold']), (ASSET, ['type'])):
        value = State(plan.bundle).entity(ref)[path[0]]
        with pytest.raises(CommandError, match='intervening'):
            inverse(plan.bundle, plan.records, [later(ref, path, value)])


def test_disjoint_edit_rebases_and_a_taken_id_conflicts():
    _, bundle, command, verified = captured()
    plan = execute(bundle, calls(command), verified_media=verified)
    head = copy.deepcopy(bundle)
    edit(head, 'parent/clip:ov-b', at=6)
    rebased = replay(plan, head, verified_media=verified,
                     intervening_records=[later('parent/clip:ov-b', ['at'], 6)])
    assert State(rebased.bundle).entity('parent/clip:ov-b')['at'] == 6
    taken = copy.deepcopy(bundle)
    for rows in (State(taken).config('parent')['clips'], taken['parent']['clips']):
        rows.append({'id': 'born', 'track': 'overlay', 'clipType': 'text', 'at': 0, 'hold': 1})
    with pytest.raises(CommandError):
        replay(plan, taken, verified_media=verified)


@pytest.mark.parametrize('replace', [False, True])
def test_provider_exact_reload_inverse_redo_and_retired_ids(replace):
    document, _, command, verified = replaced() if replace else captured('video')
    realm = MediaRealm(document, verified)
    created = realm.call(calls(command))
    undo = realm.call([inverse_call([created['receipt']['receipt_id']])])
    assert undo['data']['canonical_document'] == document
    with pytest.raises(CommitRejected) as error:
        realm.call(calls(command))
    assert error.value.failure['details']['reason'] == 'identity_collision'
    redo = realm.call([inverse_call([undo['receipt']['receipt_id']])])
    assert redo['data']['canonical_document'] == created['data']['canonical_document']


@pytest.mark.parametrize('kind, anchor, resolution, frame',
                         [('image', 'ov-a', 'anchor-after', 60), ('video', None, 'lane-end', 210),
                          ('image', 'gone', 'lane-end', 210)])
def test_completion_builder_places_a_parent_clip_after_the_anchor_or_at_the_track_end(
        kind, anchor, resolution, frame):
    document, _, new, verified = parent_inputs(kind)
    bundle = bundle_from_document(document)
    result = auto_place_completion(bundle, 'parent', 'born', new, ownership='parent',
                                   verified_media=verified, anchor=anchor, track='overlay')
    capture = result['capture']
    assert (capture['resolution'], capture['start_frame'], capture['seconds']) == (resolution, frame, 5)
    [command] = result['commands']
    assert (command['op'], command['args']['row']['track']) == ('clip.add-media', 'overlay')
    plan = execute(bundle, result['commands'], verified_media=verified)
    assert inverse(plan.bundle, plan.records).bundle == bundle


def test_completion_builder_needs_a_parent_track_and_matching_ownership():
    document, _, new, verified = parent_inputs()
    bundle = bundle_from_document(document)
    with pytest.raises(CommandError):
        auto_place_completion(bundle, 'parent', 'born', new, ownership='parent', verified_media=verified)
    with pytest.raises(CommandError):
        auto_place_completion(bundle, 'parent', 'born', new, ownership='occurrence',
                              verified_media=verified, track='overlay')


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('target', ['A', 'target', 'C'])
def test_ordered_provider_reload_inverse_redo_and_retired_ids(kind, target):
    bundle, new, verified = ordered_parent(kind)
    document = document_from_bundle(bundle)
    bundle = bundle_from_document(document)
    command = capture_clip_replace_media(bundle, 'parent/clip:' + target,
        args(new, kind, at=0)['row'], new, verified_media=verified)
    assert [item['op'] for item in command] == ['timeline.assembled-target', 'clip.add-media', 'clip.delete']
    assert set(command[1]['args']) == {'row', 'media'}
    realm = MediaRealm(document, verified)
    created = realm.call(command)
    expected = ['born' if identity == target else identity for identity in ['A', 'target', 'C']]
    assert clip_ids(bundle_from_document(created['data']['canonical_document'])) == expected
    undo = realm.call([inverse_call([created['receipt']['receipt_id']])])
    assert undo['data']['canonical_document'] == document
    with pytest.raises(CommitRejected) as error:
        realm.call(command)
    assert error.value.failure['details']['reason'] == 'identity_collision'
    redo = realm.call([inverse_call([undo['receipt']['receipt_id']])])
    assert redo['data']['canonical_document'] == created['data']['canonical_document']
    again = realm.call([inverse_call([redo['receipt']['receipt_id']])])
    assert again['data']['canonical_document'] == document


@pytest.mark.parametrize('kind, speed', [('image', None), ('image', 1), ('video', None),
                                        ('video', 1), ('video', 1.25)])
def test_literal_completion_uses_only_the_row_media_birth_and_preserves_source_units(kind, speed):
    document, _, new, verified = parent_inputs(kind)
    bundle = bundle_from_document(document)
    value = {'id': 'born', 'track': 'overlay', 'at': .123456789, 'hold': 1.5,
             'media': new, 'speed': speed}
    if kind == 'video':
        value.update({'from': .25, 'to': 1.75})
    before = copy.deepcopy(value)
    command = capture_parent_completion(bundle, value, verified_media=verified)
    assert set(command['args']) == {'row', 'media'}
    assert command['args']['row'] == {key: val for key, val in value.items() if key != 'media'} | {
        'clipType': 'media', 'asset': new['asset']}
    assert value == before
    plan = execute(bundle, [command], verified_media=verified)
    assert inverse(plan.bundle, plan.records).bundle == bundle
    value['discarded'] = None
    with pytest.raises(CommandError):
        capture_parent_completion(bundle, value, verified_media=verified)


def test_birth_after_a_boundary_keeps_it_until_an_explicit_delete():
    bundle, new, verified = ordered_parent()
    row = args(new, 'image')['row']
    command = capture_clip_add_media(bundle, row, new, after='parent/clip:target', verified_media=verified)
    plan = execute(bundle, [command], verified_media=verified)
    assert clip_ids(plan.bundle) == ['A', 'target', 'born', 'C']
    assert inverse(plan.bundle, plan.records).bundle == bundle
    assert set(command['args']) == {'row', 'media'}



def test_scoped_replacement_rebases_over_disjoint_sibling_values_without_losing_opaque_data():
    bundle, new, verified = ordered_parent()
    commands = capture_clip_replace_media(bundle, 'parent/clip:target',
        args(new, 'image')['row'], new, verified_media=verified)
    plan = execute(bundle, commands, verified_media=verified)
    head = copy.deepcopy(bundle)
    edit(head, 'parent/clip:A', at=6, opaque={'later': [None, False, {'asset': 'reserved-new'}]})
    rebased = replay(plan, head, verified_media=verified,
                     intervening_records=[later('parent/clip:A', ['at'], 6)])
    assert State(rebased.bundle).entity('parent/clip:A') == State(head).entity('parent/clip:A')
    assert clip_ids(rebased.bundle) == ['A', 'born', 'C']
    assert inverse(rebased.bundle, rebased.records).bundle == head
