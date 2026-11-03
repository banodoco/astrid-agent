"""Offline source controls for authenticated same-alias inverse observations."""
from __future__ import annotations

import copy
import json
import sys

import pytest
import runtime_protocol
from runtime_protocol.timeline_commit_auth import location, validate_sequential_capture

from astrid.sdk.timeline._command_state import CommandError, State, present
from astrid.sdk.timeline.commit import (
    _digest,
    _manifest,
    bundle_from_document,
    create_runtime_commit_service,
)
from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_swap_media
from tests.editing_conformance.test_authoring_undo_projection import retained_copy as retained_copy
from tests.editing_conformance.test_doc_patch_v2 import submit
from tests.editing_conformance.test_provider_step_undo_v2 import undo
from tests.editing_conformance.test_recipe_occurrence_media import OWNER, TARGET, MediaRealm, inputs
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedPort
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


@pytest.fixture
def source_control():
    return make_source_control(shared=False)


def make_source_control(*, shared, native_items=True):
    baseline, old, new, verified = inputs()
    parent = baseline['parent_revision']
    if not shared:
        parent['payload']['occurrences'] = parent['payload']['occurrences'][:1]
    parent['content_digest'] = _digest(parent['payload'])
    if not native_items:
        for image in baseline['shot_revisions']:
            image['payload']['items'] = []
            image['content_digest'] = _digest(image['payload'])
        baseline['dependency_manifest'] = _manifest(baseline['shot_revisions'],
            baseline['internal_timeline_revisions'], baseline['dependency_manifest']['media'], [])
    new['asset'] = old['asset']
    verified[1]['selection']['asset'] = old['asset']
    realm = MediaRealm(baseline, verified)
    command = capture_occurrence_swap_media(bundle_from_document(baseline), TARGET, old, new,
                                            verified_media=verified)
    context = realm.context([command])
    context['request']['reads'] = []
    context['invocation']['body'] = copy.deepcopy(context['request'])
    context['request_hash'] = _digest(context['invocation'])

    class MediaPort(DetachedPort):
        def selected_media(self, pin):
            selected, = [value for value in verified if value['selection'] == pin]
            return copy.deepcopy(selected)

    created = create_runtime_commit_service().commit(context, MediaPort(realm, context))
    return realm, baseline, created


@pytest.fixture(params=[(True, False), (True, True), (False, False), (False, True)],
                ids=['native-item-accept', 'native-item-preview', 'ordinary-media-accept', 'ordinary-media-preview'])
def shared_inverse_packet(request, monkeypatch):
    """Generated offline packets, not an original11466 or Runtime acceptance."""
    native_items, dry_run = request.param
    realm, baseline, created = make_source_control(shared=True, native_items=native_items)
    assert len(baseline['parent_revision']['payload']['occurrences']) == 2
    assert len({row['shot_revision_id'] for row in
                baseline['parent_revision']['payload']['occurrences']}) == 1
    captures, prepared = [], []
    complete = DetachedPort.preview if dry_run else DetachedPort.accept

    def capture(port, packet):
        captures.append(copy.deepcopy(packet))

    def finish(port, packet):
        prepared.append((copy.deepcopy(port.context['request']), copy.deepcopy(packet)))
        return complete(port, packet)

    monkeypatch.setattr(DetachedPort, 'capture_sequential_execution', capture, raising=False)
    monkeypatch.setattr(DetachedPort, 'preview' if dry_run else 'accept', finish)
    submit(realm, undo(created['receipt']['receipt_id']), reads=[], dry_run=dry_run)
    request_body, packet = prepared[0]
    source = created['data']['canonical_document']
    history = {row['publication_id']: row for row in realm.entries
               if row['publication_id'] == created['receipt']['receipt_id']}
    return request_body, packet, captures[0], source, history, baseline


def test_two_shared_occurrences_inverse_accept_and_preview(shared_inverse_packet, monkeypatch):
    # R1/R2 must preserve this positive expectation across native identity
    # turnover and shared-source reachability; current refusal is not success.
    request, prepared, capture, source, history, baseline = shared_inverse_packet
    monkeypatch.setitem(sys.modules, 'runtime_protocol', runtime_protocol)
    validate_sequential_capture(request, prepared, capture, source, source, history=history)
    assert prepared['canonical_document'] == baseline


def test_shared_stage_diagnostic_preserves_all_native_sites(shared_inverse_packet, monkeypatch):
    from runtime_protocol.timeline_commit_auth import _inverse_stage_ranges

    _, prepared, _, source, history, _ = shared_inverse_packet
    monkeypatch.setitem(sys.modules, 'runtime_protocol', runtime_protocol)
    record, = prepared['records']['commands']
    entry, = history.values()
    original, = entry['records']['commands']
    historical = [(write['ref'], write['path']) for write in reversed(original['writes'])]
    actual = [(write['ref'], write['path']) for write in record['writes']]
    differences = [(old, new) for old, new in zip(historical, actual) if old != new]
    assert len(actual) == len(historical)
    assert len(record['bindings']) > 0
    if differences:
        (old, new), = differences
        assert old == (OWNER + '/shot', ['payload', 'items', 'item_id:item-1', 'media_id'])
        state = State(bundle_from_document(source))
        assert new == (old[0], [*old[1][:2], 'item_id:' + next(iter(state.shot_item_map(old[0]))), 'media_id'])
        with pytest.raises(CommandError, match='unknown published item identity'):
            state.get(*old)
        assert state.get(*new)['state'] == 'present'
        assert _inverse_stage_ranges(prepared['records']['steps'][0], prepared['records'], history)
    else:
        assert _inverse_stage_ranges(prepared['records']['steps'][0], prepared['records'], history)


def test_historical_item_site_does_not_observe_current_published_item(shared_inverse_packet, monkeypatch):
    from runtime_protocol.timeline_commit_auth import (
        _inverse_stage_boundaries,
        _inverse_stage_ranges,
        images,
    )

    _, prepared, _, source, history, _ = copy.deepcopy(shared_inverse_packet)
    monkeypatch.setitem(sys.modules, 'runtime_protocol', runtime_protocol)
    record, = prepared['records']['commands']
    original, = next(iter(history.values()))['records']['commands']
    # A diagnostic alteration only: restoring source spelling passes membership
    # but cannot make that absent path a truthful sequential value observation.
    for write, historical in zip(record['writes'], reversed(original['writes'])):
        write['path'] = copy.deepcopy(historical['path'])
    if any(write['path'][:2] == ['payload', 'items'] for write in original['writes']):
        with pytest.raises(CommitRejected, match='shared inverse native captures differ from authenticated history'):
            _inverse_stage_ranges(prepared['records']['steps'][0], prepared['records'], history)
        return
    step, = prepared['records']['steps']
    assert _inverse_stage_ranges(step, prepared['records'], history)
    before, after = images(source), images(prepared['canonical_document'])
    assert _inverse_stage_boundaries(before, after, step, prepared['records'], after, before, history)


def test_shared_source_restore_is_retained_reachability_not_exclusive_turnover(shared_inverse_packet):
    from runtime_protocol.timeline_commit_auth import images

    _, prepared, capture, source, history, baseline = shared_inverse_packet
    entry, = history.values()
    original, = entry['records']['commands']
    record, = prepared['records']['commands']
    stage, = [binding['boundary'] for binding in record['bindings'] if binding['key'] == 'inverse.stage']
    assert stage == {'publication_id': entry['publication_id'], 'step_id': 'edit',
                     'command_ordinals': [0], 'native_write_range': [0, len(original['writes'])],
                     'effect_range': [0, 4]}
    assert len(record['writes']) == len(original['writes'])
    assert record['reads'] and original['reads']
    assert prepared['canonical_document'] == baseline
    for execution in (capture['original'], capture['landing']):
        command, = execution['commands']
        assert command['input'] == source and command['output'] == baseline
        assert execution['records'] == prepared['records']
    before, after = images(source), images(baseline)
    source_rows = {row['occurrence_id']: row for row in source['parent_revision']['payload']['occurrences']}
    target_rows = {row['occurrence_id']: row for row in baseline['parent_revision']['payload']['occurrences']}
    assert source_rows['occ-b'] == target_rows['occ-b']
    assert source_rows['occ-a']['shot_id'] != target_rows['occ-a']['shot_id']
    assert target_rows['occ-a']['shot_revision_id'] == target_rows['occ-b']['shot_revision_id']
    materializations = [binding for binding in original['bindings'] if binding['key'] == 'commit.materialization']
    assert len(materializations) == 2
    for binding in materializations:
        fork, retained = binding['successor'], binding['physical_ref']
        assert binding['owner'] == OWNER + '/shot'
        assert binding['owners'] == [OWNER + '/shot', 'occurrence:occ-b/shot']
        assert fork in before and fork not in after
        assert retained in before and retained in after
        assert before[retained] == after[retained]
        assert binding['revision_id'] == before[retained]['revision_id']
        assert binding['content_digest'] == _digest(before[retained]['payload'])
        assert binding['content_digest'] == before[retained]['content_digest']
        assert binding['boundary'] == {'base_revision_id': entry['receipt']['result']['base_revision_id'],
            'base_publication_id': entry['receipt']['result']['base_publication_id'],
            'evaluated_head': entry['previous'], 'step_id': 'edit', 'command_ordinal': 0, 'phase': 'input'}
        sibling, = [item for item in materializations if item is not binding]
        assert binding['edge_before'] == sibling['physical_ref']
        assert binding['edge_after'] == sibling['successor']
    old_state, current_state = State(bundle_from_document(baseline)), State(bundle_from_document(source))
    mapping = old_state.shot_item_map(OWNER + '/shot')
    current = current_state.shot_item_map(OWNER + '/shot')
    assert set(mapping.values()) == set(current.values())
    assert all(published == local for published, local in current.items())
    # Exact placement-local correspondence, never a match on media bytes.
    for published, local in mapping.items():
        assert published != local
        path = ['payload', 'items', 'item_id:' + published, 'media_id']
        historical, = [write for write in original['writes'] if write['path'] == path]
        inverse, = [write for write in record['writes'] if write['path'] == [*path[:2], 'item_id:' + local, *path[3:]]]
        assert inverse['ref'] == historical['ref'] == OWNER + '/shot'
        assert inverse['before'] == historical['after'] and inverse['after'] == historical['before']


@pytest.mark.parametrize('dry_run', [False, True])
def test_retained_source_copy_inverse_passes_the_same_runtime_validator(request, monkeypatch, dry_run):
    realm, baseline, created = request.getfixturevalue('retained_copy')
    captures = []
    complete = DetachedPort.preview if dry_run else DetachedPort.accept
    monkeypatch.setattr(DetachedPort, 'capture_sequential_execution',
                        lambda port, packet: captures.append(copy.deepcopy(packet)), raising=False)

    def finish(port, prepared):
        source = created['data']['canonical_document']
        history = {row['publication_id']: row for row in realm.entries
                   if row['publication_id'] == created['receipt']['receipt_id']}
        original, = history.values()
        assert any(not write['path'] for command in original['records']['commands'] for write in command['writes'])
        assert not any(binding['key'] == 'commit.materialization'
                       for command in original['records']['commands'] for binding in command['bindings'])
        with monkeypatch.context() as modules:
            modules.setitem(sys.modules, 'runtime_protocol', runtime_protocol)
            validate_sequential_capture(port.context['request'], prepared, captures[-1], source, source, history=history)
        return complete(port, prepared)

    monkeypatch.setattr(DetachedPort, 'preview' if dry_run else 'accept', finish)
    result = submit(realm, undo(created['receipt']['receipt_id']), reads=[], dry_run=dry_run)
    assert result['data']['canonical_document'] == baseline


@pytest.mark.parametrize('shared_inverse_packet', [(False, False), (False, True)], indirect=True)
@pytest.mark.parametrize('damage', ['native-site', 'foreign-owner', 'native-order', 'native-range',
                                  'effect-range', 'source-publication', 'source-step', 'source-command'])
def test_native_stage_membership_and_partition_stay_strict(shared_inverse_packet, monkeypatch, damage):
    from runtime_protocol.timeline_commit_auth import _inverse_stage_ranges

    _, prepared, _, _, history, _ = copy.deepcopy(shared_inverse_packet)
    monkeypatch.setitem(sys.modules, 'runtime_protocol', runtime_protocol)
    records = prepared['records']
    step, = records['steps']
    record, = records['commands']
    assert _inverse_stage_ranges(step, records, history)
    binding, = [item for item in record['bindings'] if item['key'] == 'inverse.stage']
    boundary = binding['boundary']
    if damage == 'native-site':
        record['writes'][0]['path'].append('forged')
    elif damage == 'foreign-owner':
        record['writes'][0]['ref'] = record['writes'][0]['ref'].replace('occ-a', 'occ-b')
    elif damage == 'native-order':
        record['writes'].reverse()
    elif damage == 'native-range':
        boundary['native_write_range'][1] -= 1
    elif damage == 'effect-range':
        boundary['effect_range'][0] = 1
    elif damage == 'source-publication':
        boundary['publication_id'] = 'foreign-publication'
    elif damage == 'source-step':
        boundary['step_id'] = 'foreign-step'
    else:
        boundary['command_ordinals'] = [1]
    with pytest.raises(CommitRejected):
        _inverse_stage_ranges(step, records, history)


@pytest.mark.parametrize('dry_run', [False, True])
def test_same_alias_inverse_observations_validate(source_control, monkeypatch, tmp_path, dry_run):
    realm, baseline, created = source_control
    captures = []
    original = DetachedPort.preview if dry_run else DetachedPort.accept

    def capture(port, packet):
        # Newly generated offline values; no custody of original11466 is claimed.
        (tmp_path / 'capture.json').write_text(json.dumps(packet, allow_nan=False))
        captures.append(copy.deepcopy(packet))

    def finish(port, prepared):
        (tmp_path / 'prepared.json').write_text(json.dumps(prepared, allow_nan=False))
        (tmp_path / 'history.json').write_text(json.dumps(realm.entries, allow_nan=False))
        with monkeypatch.context() as modules:
            modules.setitem(sys.modules, 'runtime_protocol', runtime_protocol)
            source = created['data']['canonical_document']
            try:
                validate_sequential_capture(port.context['request'], prepared, captures[-1], source, source,
                                            history={row['publication_id']: row for row in realm.entries
                                                     if row['intent'] == 'recorded'})
            except CommitRejected as error:
                assert error.failure['details']['reason'] == 'invalid_records'
                assert error.failure['message'] == 'provider observation has an ambiguous item path'
                raise
        return original(port, prepared)

    monkeypatch.setattr(DetachedPort, 'capture_sequential_execution', capture, raising=False)
    monkeypatch.setattr(DetachedPort, 'preview' if dry_run else 'accept', finish)
    result = submit(realm, undo(created['receipt']['receipt_id']), reads=[], dry_run=dry_run)
    assert result['data']['canonical_document'] == baseline


def test_source_control_has_one_item_and_a_changed_native_observation(source_control):
    realm, _, _ = source_control
    record, = realm.entries[-1]['records']['commands']
    write, = [write for write in record['writes']
              if write['ref'] == OWNER + '/shot' and write['path'][:2] == ['payload', 'items']]
    assert write['path'] == ['payload', 'items', 'item_id:item-1', 'media_id']
    assert write['before'] != write['after']
    output = realm.entries[-1]['receipt']['result']['canonical_document']
    occurrence = next(row for row in output['parent_revision']['payload']['occurrences']
                      if row['occurrence_id'] == 'occ-a')
    shot, = [image for image in output['shot_revisions']
             if image['revision_id'] == occurrence['shot_revision_id']]
    assert len(shot['payload']['items']) == 1
    original = bundle_from_document(source_control[1])
    local = State(original).occurrence(OWNER)['shot_id']
    assert shot['payload']['items'][0]['item_id'] == original['shots'][local]['payload']['items'][0]['item_id']


def test_collection_candidate_requires_source_to_authoring_item_projection():
    baseline, _, _, _ = inputs()
    bundle = bundle_from_document(baseline)
    state = State(bundle)
    shot = state.occurrence(OWNER)['shot_id']
    source = bundle['source_mapping']['shots'][shot]
    observed = state.get(OWNER + '/shot', ['payload', 'items'])['value']
    authored = bundle['shots'][shot]['payload']['items']
    # A published item and its placement-local item have distinct identities.
    assert observed[0]['item_id'] == 'item-1'
    assert source['item_id_map'][observed[0]['item_id']] == authored[0]['item_id']
    assert authored[0]['item_id'] != observed[0]['item_id']
    # Published aggregate values must preserve placement-local stored IDs.
    state.set(OWNER + '/shot', ['payload', 'items'], {'state': 'present', 'value': observed})
    assert bundle['shots'][shot]['payload']['items'] == authored
    assert bundle['shots'][shot]['payload']['items'] != observed


@pytest.mark.parametrize('reversed_order', [False, True])
def test_runtime_item_identity_uses_published_binding_not_position(monkeypatch, reversed_order):
    baseline, old, _, _ = inputs()
    state = State(bundle_from_document(baseline))
    shot = state.occurrence(OWNER)['shot_id']
    source = state.bundle['source_mapping']['shots'][shot]
    observed = state.get(OWNER + '/shot', ['payload', 'items'])['value']
    identity = observed[0]['item_id']
    authored = state.bundle['shots'][shot]['payload']['items'][0]['item_id']
    assert source['item_id_map'][identity] == authored
    assert identity != authored
    other = copy.deepcopy(observed[0])
    other.update(item_id='other:item', media_id='sha256:' + '3' * 64)
    rows = [other, *observed] if reversed_order else [*observed, other]
    monkeypatch.setitem(sys.modules, 'runtime_protocol', runtime_protocol)
    assert location({'items': rows}, ['items', 'item_id:' + identity, 'media_id']) == {
        'state': 'present', 'value': old['object_id']}
    assert location({'items': rows}, ['items', 'item_id:other:item', 'media_id']) == {
        'state': 'present', 'value': other['media_id']}
    with pytest.raises(CommitRejected, match='provider observation has an ambiguous item path'):
        location({'items': rows}, ['items', 'item_id:' + authored, 'media_id'])


@pytest.mark.parametrize('rows, token', [
    ([{'item_id': 'a'}], '0'),
    ([{'item_id': 'a'}], 'item_id:missing'),
    ([{'item_id': 'a'}, {'item_id': 'a'}], 'item_id:a'),
])
def test_runtime_strict_item_path_rejects_numeric_missing_and_duplicate(rows, token, monkeypatch):
    monkeypatch.setitem(sys.modules, 'runtime_protocol', runtime_protocol)
    with pytest.raises(CommitRejected, match='provider observation has an ambiguous item path'):
        location({'items': rows}, ['items', token, 'media_id'])


def item_state():
    document, _, _, _ = inputs()
    shot = document['shot_revisions'][0]
    other = copy.deepcopy(shot['payload']['items'][0])
    other['item_id'] = 'other:item'
    shot['payload']['items'].append(other)
    shot['content_digest'] = _digest(shot['payload'])
    document['dependency_manifest'] = _manifest(document['shot_revisions'],
        document['internal_timeline_revisions'], document['dependency_manifest']['media'], [])
    return State(bundle_from_document(document))


@pytest.mark.parametrize('reverse', [False, True])
def test_state_item_path_storage_and_aggregate_projection(reverse):
    state = item_state()
    ref = OWNER + '/shot'
    shot = state.occurrence(OWNER)['shot_id']
    stored = state.bundle['shots'][shot]['payload']['items']
    original_ids = [row['item_id'] for row in stored]
    published = state.get(ref, ['payload', 'items'])['value']
    if reverse:
        state.set(ref, ['payload', 'items'], present(list(reversed(published))))
    path = ['payload', 'items', 'item_id:other:item', 'media_id']
    state.set(ref, path, present('changed'))
    assert state.get(ref, path) == present('changed')
    assert state.get(ref, ['payload', 'items', 'item_id:item-1', 'media_id']) == present(published[0]['media_id'])
    assert [row['item_id'] for row in stored] == original_ids
    actual = state.bundle['shots'][shot]['payload']['items']
    assert [row['item_id'] for row in actual] == (list(reversed(original_ids)) if reverse else original_ids)
    assert next(row for row in actual if row['item_id'] == original_ids[1])['media_id'] == 'changed'
    with pytest.raises(CommandError):
        state.get(ref, ['payload', 'items', 'item_id:' + original_ids[1], 'media_id'])
    before = copy.deepcopy(state.bundle)
    with pytest.raises(CommandError):
        state.set(ref, [*path[:-1], 'missing', 'child'], present('no container'))
    assert state.bundle == before
    with pytest.raises(CommandError):
        state.set(ref, [*path[:-1], 'item_id'], present('another'))
    assert state.bundle == before


@pytest.mark.parametrize('damage', ['missing-map', 'empty-map', 'nonbijective', 'extra-map',
    'missing-item', 'duplicate-item', 'foreign-map', 'wrong-owner', 'revision', 'digest', 'internal-revision'])
@pytest.mark.parametrize('access', ['leaf', 'aggregate', 'set'])
def test_item_mapping_refuses_unverified_correspondence(damage, access):
    state = item_state()
    ref = OWNER + '/shot'
    shot = state.occurrence(OWNER)['shot_id']
    source = state.bundle['source_mapping']['shots'][shot]
    mapping = source['item_id_map']
    rows = state.bundle['shots'][shot]['payload']['items']
    if damage == 'missing-map':
        source.pop('item_id_map')
    elif damage == 'empty-map':
        source['item_id_map'] = {}
    elif damage == 'nonbijective':
        mapping['other:item'] = mapping['item-1']
    elif damage == 'extra-map':
        mapping['foreign'] = 'unbound'
    elif damage == 'missing-item':
        rows.pop()
    elif damage == 'duplicate-item':
        rows[1]['item_id'] = rows[0]['item_id']
    elif damage == 'wrong-owner':
        state.occurrence(OWNER)['shot_id'] = state.bundle['placements'][1]['shot_id']
    elif damage == 'foreign-map':
        source['item_id_map'] = copy.deepcopy(state.bundle['source_mapping']['shots'][
            state.bundle['placements'][1]['shot_id']]['item_id_map'])
    else:
        source[{'revision': 'revision_id', 'digest': 'content_digest',
                'internal-revision': 'internal_timeline_revision_id'}[damage]] = 'unverified'
    before = copy.deepcopy(state.bundle)
    path = ['payload', 'items', 'item_id:item-1', 'media_id']
    with pytest.raises(CommandError):
        if access == 'set':
            state.set(ref, path, present('changed'))
        else:
            state.get(ref, ['payload', 'items'] if access == 'aggregate' else path)
    assert state.bundle == before


@pytest.mark.parametrize('accepted_boundary', [False, True])
@pytest.mark.parametrize('aggregate_first', [False, True])
@pytest.mark.parametrize('aggregate_path', [[], ['payload'], ['payload', 'items']])
def test_inverse_folds_item_leaf_with_local_and_published_aggregates(accepted_boundary, aggregate_first, aggregate_path):
    from astrid.sdk.timeline._recording import Recording
    from astrid.sdk.timeline.inverse import coalesce

    state = item_state()
    shot = state.occurrence(OWNER)['shot_id']
    ref = 'shot:' + shot if not aggregate_path else OWNER + '/shot'
    before = state.get(ref, aggregate_path)
    original = copy.deepcopy(state.bundle)
    recording = Recording(state.bundle, {'op': 'clip.set_params'}, 0)
    path = ['payload', 'items', 'item_id:other:item', 'media_id']
    if aggregate_first:
        recording.assign(ref, aggregate_path, before)
    recording.write(OWNER + '/shot', path, 'changed')
    if not aggregate_first:
        recording.assign(ref, aggregate_path, recording.state.get(ref, aggregate_path))
    if accepted_boundary:
        from astrid.sdk.timeline._collections import AcceptedRecords

        entry = {'records': {'commands': [recording.record]}}
        records = AcceptedRecords([entry], [recording.record], lambda entry: (original, recording.state.bundle))
        folded, = coalesce(records)
    else:
        folded, = coalesce([recording.record], recording.state)
    assert folded['before'] == before
    assert folded['after'] == recording.state.get(ref, aggregate_path)
    assert folded['ref'] == ref and folded['path'] == aggregate_path


@pytest.mark.parametrize('site', ['leaf', 'aggregate'])
def test_item_inverse_preserves_intervening_equal_value_write_guard(source_control, site):
    from tests.editing_conformance.test_occurrence_media_manifest_inverse import accepted_records
    from tests.editing_conformance.test_recipe_m1_commands import inverse, later

    realm, _, created = source_control
    records = accepted_records(realm, realm.entries[-1])
    bundle = bundle_from_document(created['data']['canonical_document'])
    state = State(bundle)
    ref = OWNER + '/shot'
    path = ['payload', 'items']
    if site == 'leaf':
        path += ['item_id:item-1', 'media_id']
    value = state.get(ref, path)['value']
    before = copy.deepcopy(bundle)
    # Even a value-equal tail is an intervening write; final bytes grant no undo authority.
    with pytest.raises(CommandError, match='intervening'):
        inverse(bundle, records, [later(ref, path, value)])
    assert bundle == before


@pytest.mark.parametrize('reverse_order', [False, True])
def test_native_item_inverse_preserves_sequential_observations(reverse_order):
    from astrid.sdk.timeline._recording import Recording
    from astrid.sdk.timeline.native_occurrence_media_commands import _replace_payload_media
    from tests.editing_conformance.test_recipe_m1_commands import inverse

    state = item_state()
    original = copy.deepcopy(state.bundle)
    old = state.get(OWNER + '/shot', ['payload', 'items', 'item_id:item-1', 'media_id'])['value']
    recording = Recording(state.bundle, {'op': 'clip.set_params'}, 0)
    _replace_payload_media(recording, OWNER, old, 'changed')
    _replace_payload_media(recording, OWNER, 'changed', old)
    assert recording.state.bundle == original
    writes = recording.record['writes']
    assert len(writes) == 4
    assert writes[0]['after'] == writes[2]['before'] == present('changed')
    assert writes[0]['before'] == writes[2]['after'] == present(old)
    if reverse_order:
        shot = state.occurrence(OWNER)['shot_id']
        recording.state.bundle['shots'][shot]['payload']['items'].reverse()
        with pytest.raises(CommandError):
            inverse(recording.state.bundle, [recording.record])
    else:
        result = inverse(recording.state.bundle, [recording.record])
        assert result.bundle == original
        assert len(result.records[0]['writes']) == 4


def test_item_inverse_rejects_equal_bytes_on_unproven_revision(source_control):
    from tests.editing_conformance.test_occurrence_media_manifest_inverse import accepted_records
    from tests.editing_conformance.test_recipe_m1_commands import inverse

    realm, _, created = source_control
    records = accepted_records(realm, realm.entries[-1])
    document = copy.deepcopy(created['data']['canonical_document'])
    shot, = document['shot_revisions']
    shot['revision_id'] += ':unproven'
    parent = document['parent_revision']
    parent['payload']['occurrences'][0]['shot_revision_id'] = shot['revision_id']
    parent['revision_id'] += ':unproven'
    parent['content_digest'] = _digest(parent['payload'])
    document['dependency_manifest'] = _manifest(document['shot_revisions'],
        document['internal_timeline_revisions'], document['dependency_manifest']['media'], [])
    bundle = bundle_from_document(document)
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError, match='binding changed'):
        inverse(bundle, records)
    assert bundle == before
