"""Repeated consumers retain their observations and one exact track binding."""
from __future__ import annotations

import copy

import pytest

from astrid.packs.rendering.docs.scripts.timeline_document import emit_document_publish_commands
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import (
    _digest,
    _manifest,
    bundle_from_document,
    create_runtime_commit_service,
)
from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_swap_media
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import inverse, later
from tests.editing_conformance.test_recipe_occurrence_media import NEW, OWNER, TARGET, inputs
from tests.helpers.runtime import runtime_checkout
from tests.sdk.test_timeline_commit_provider import DetachedPort, DetachedRealm


def same_alias():
    document, old, new, verified = inputs()
    new['asset'] = old['asset']
    verified[1]['selection']['asset'] = old['asset']
    return document, old, new, verified


def test_publish_caller_preserves_both_consumers_payload_effects_and_inverse():
    document, old, new, verified = same_alias()
    bundle = bundle_from_document(document)
    bundle['base_parent']['publication_id'] = 'bootstrap'
    before = copy.deepcopy(bundle)
    command = capture_occurrence_swap_media(bundle, TARGET, old, new, verified_media=verified)
    plan = execute(bundle, [command], verified_media=verified)
    emitted = emit_document_publish_commands(
        bundle, plan.bundle, {(OWNER, old['asset']): {'old': old, 'new': new}}, verified)
    assert emitted == [command]
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    record = plan.records[0]
    assert [b for b in record['bindings'] if b['key'] == 'media.track'] == [
        {'key': 'media.track', 'ref': OWNER + '/track:picture',
         'policy': 'fixed', 'dependencies': []}]
    for identity in ('clip-a', 'clip-b'):
        ref = OWNER + '/clip:' + identity
        assert State(plan.bundle).entity(ref) == State(bundle).entity(ref)
        assert any(read['ref'] == ref and read['path'] == [] for read in record['reads'])
    assert record['command']['bindings']['media.target'] == TARGET
    shot = plan.bundle['shots'][State(plan.bundle).occurrence(OWNER)['shot_id']]
    assert shot['payload']['items'][0]['media_id'] == NEW
    assert State(plan.bundle).registry(OWNER)['assets'][old['asset']]['media_id'] == NEW
    assert plan.bundle['parent'] == bundle['parent']
    assert plan.bundle['placements'] == bundle['placements']
    assert inverse(plan.bundle, plan.records).bundle == before
    assert bundle == before


def test_second_consumer_is_still_validated_before_any_rebind():
    document, old, new, verified = same_alias()
    bundle = bundle_from_document(document)
    State(bundle).entity(OWNER + '/clip:clip-b')['hold'] = 100
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError, match='exceeds occurrence'):
        capture_occurrence_swap_media(bundle, TARGET, old, new, verified_media=verified)
    assert bundle == before


def test_order_and_aba_observations_survive_coalescing():
    document, old, new, verified = same_alias()
    bundle = bundle_from_document(document)
    command = capture_occurrence_swap_media(bundle, TARGET, old, new, verified_media=verified)
    plan = execute(bundle, [command], verified_media=verified)
    reordered = copy.deepcopy(bundle)
    State(reordered).config(OWNER)['clips'].reverse()
    with pytest.raises(CommandError):
        replay(plan, reordered, verified_media=verified)
    changed = State(bundle).entity(OWNER + '/track:picture')
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, [
            later(OWNER + '/track:picture', [], {**changed, 'label': 'away'}),
            later(OWNER + '/track:picture', [], changed),
        ])


@pytest.fixture
def runtime_auth(monkeypatch):
    # Import the real guard without a daemon, sockets, or dependency changes.
    monkeypatch.syspath_prepend(str(runtime_checkout()))
    from runtime_protocol import timeline_commit_auth
    from runtime_protocol.timeline_commit import CommitRejected

    return timeline_commit_auth, CommitRejected


@pytest.fixture
def packet(runtime_auth):
    document, old, new, verified = same_alias()
    # Bound this source control to track authentication. Payload mirror effects
    # and the shared sibling are covered above by the unchanged native fixture.
    document['parent_revision']['payload']['occurrences'] = (
        document['parent_revision']['payload']['occurrences'][:1])
    document['shot_revisions'][0]['payload']['items'] = []
    for image in (document['parent_revision'], document['shot_revisions'][0]):
        image['content_digest'] = _digest(image['payload'])
    document['dependency_manifest'] = _manifest(
        document['shot_revisions'], document['internal_timeline_revisions'],
        document['dependency_manifest']['media'], [])
    bundle = bundle_from_document(document)
    swap = capture_occurrence_swap_media(bundle, TARGET, old, new, verified_media=verified)
    patch = {'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
             'args': {'changes': [{'op': 'set', 'ref': 'parent', 'path': ['name'],
                                   'before': {'state': 'present', 'value': 'Renamed Film'},
                                   'value': 'Published'}]}}
    realm = DetachedRealm(document)
    context = realm.context([patch, swap])
    context['request'].update(label='Publish document', reads=[], merge='fields')

    class Port(DetachedPort):
        def selected_media(self, pin):
            return copy.deepcopy(next(row for row in verified if row['selection'] == pin))

        def capture_sequential_execution(self, capture):
            self.capture = copy.deepcopy(capture)

        def accept(self, prepared):
            self.prepared = copy.deepcopy(prepared)
            return prepared

    port = Port(realm, context)
    create_runtime_commit_service().commit(context, port)
    return document, port


def authenticate(auth, document, port, capture=None):
    return auth.validate_sequential_capture(
        port.context['request'], port.prepared,
        port.capture if capture is None else capture, document, document)


def track_bindings(capture, side):
    return [b for b in capture[side]['records']['commands'][1]['bindings']
            if b['key'] == 'media.track']


def test_actual_original_and_landing_packets_authenticate(packet, runtime_auth):
    document, port = packet
    auth, _ = runtime_auth
    authenticate(auth, document, port)
    for side in ('original', 'landing'):
        bindings = track_bindings(port.capture, side)
        assert len(bindings) == 1
        binding = bindings[0]
        assert binding['physical_ref'] == 'internal-revision:internal-1'
        assert binding['revision_id'] == 'internal-1'
        assert binding['content_digest'] == document['internal_timeline_revisions'][0]['content_digest']
        assert binding['owners'] == [OWNER + '/shot']
        assert binding['path'] == ['tracks', 'id:picture']
        assert binding['boundary']['command_ordinal'] == 1
        assert binding['boundary']['phase'] == 'input'
        assert len(port.capture[side]['commands']) == 2


@pytest.mark.parametrize('side', ['original', 'landing'])
@pytest.mark.parametrize('damage', [
    'duplicate', 'conflicting-binding', 'foreign-owner', 'different-revision',
    'forged-read', 'order', 'aba',
])
def test_runtime_still_rejects_conflicting_or_forged_captures(packet, runtime_auth, side, damage):
    document, port = packet
    auth, rejected = runtime_auth
    capture = copy.deepcopy(port.capture)
    binding = track_bindings(capture, side)[0]
    record = capture[side]['records']['commands'][1]
    if damage in ('duplicate', 'conflicting-binding'):
        duplicate = copy.deepcopy(binding)
        if damage == 'conflicting-binding':
            duplicate['content_digest'] = 'sha256:' + 'f' * 64
        record['bindings'].append(duplicate)
    elif damage == 'foreign-owner':
        binding['owners'] = ['occurrence:foreign/shot']
    elif damage == 'different-revision':
        binding['revision_id'] = 'unobserved-revision'
    elif damage == 'forged-read':
        read = next(read for read in record['reads'] if read['ref'] == OWNER + '/track:picture')
        read['state']['value']['kind'] = 'audio'
    elif damage == 'order':
        capture[side]['commands'].reverse()
    else:
        capture[side]['evaluated_head']['publication_id'] = 'earlier-equal-bytes-publication'
    before = copy.deepcopy((document, port.prepared, port.capture))
    with pytest.raises(rejected) as error:
        authenticate(auth, document, port, capture)
    assert error.value.failure['details']['reason'] == 'invalid_records'
    assert (document, port.prepared, port.capture) == before


def test_equal_track_payloads_at_distinct_refs_remain_distinct():
    document, old, new, verified = same_alias()
    bundle = bundle_from_document(document)
    config = State(bundle).config(OWNER)
    config['tracks'].append({**copy.deepcopy(config['tracks'][0]), 'id': 'other-picture'})
    State(bundle).entity(OWNER + '/clip:clip-b')['track'] = 'other-picture'
    command = capture_occurrence_swap_media(bundle, TARGET, old, new, verified_media=verified)
    plan = execute(bundle, [command], verified_media=verified)
    assert [b['ref'] for b in plan.records[0]['bindings'] if b['key'] == 'media.track'] == [
        OWNER + '/track:picture', OWNER + '/track:other-picture']
    assert inverse(plan.bundle, plan.records).bundle == bundle


@pytest.mark.parametrize('other_owner', [False, True], ids=['next-command', 'other-owner'])
def test_bindings_remain_separate_across_commands_and_owners(other_owner):
    document, old, new, verified = same_alias()
    bundle = bundle_from_document(document)
    first = capture_occurrence_swap_media(bundle, TARGET, old, new, verified_media=verified)
    middle = execute(bundle, [first], verified_media=verified).bundle
    owner = 'occurrence:occ-b' if other_owner else OWNER
    previous, replacement = (old, new) if other_owner else (new, old)
    second = capture_occurrence_swap_media(
        middle, owner + '/clip:clip-a', previous, replacement, verified_media=verified)
    plan = execute(bundle, [first, second], verified_media=verified)
    assert [[b['ref'] for b in record['bindings'] if b['key'] == 'media.track']
            for record in plan.records] == [
                [OWNER + '/track:picture'], [owner + '/track:picture']]
    assert inverse(plan.bundle, plan.records).bundle == bundle
