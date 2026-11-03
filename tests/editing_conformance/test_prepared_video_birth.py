"""Normalized UI video duplication is separate from literal parent media birth."""
from __future__ import annotations

import copy
import json
import subprocess
import types
from functools import cache

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from astrid.sdk.timeline import native_parent_media_commands as parent_media
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commands import execute, validate_command
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance import test_commit_runtime_parent_media as runtime_media
from tests.editing_conformance.test_recipe_m1_commands import inverse, later
from tests.editing_conformance.test_recipe_m2_parent_media import ordered_parent
from tests.helpers.runtime import runtime_checkout

BASE = 'c36ca8c94b695b0e25b7d42c89f194661f187423'
RUNTIME = '8ea0766f5e9fb23cea26c7398770a8aaac06b258'
PRODUCER = '34eac0041b0891e9011966b39bcbd26053fde5bc'
SOURCE = 'parent/clip:target'


def call(old, new, identity='born'):
    return {'schema_version': 1, 'op': 'clip.duplicate-media', 'version': '1',
            'select': SOURCE, 'args': {'id': identity, 'old_media': old, 'media': new},
            'intent': {'ownership': 'parent'}}


def case(hold=4):
    bundle, new, verified = ordered_parent('video')
    old = verified[0]['selection']
    for rows in (bundle['parent']['clips'], bundle['parent']['config']['clips']):
        source = next(row for row in rows if row['id'] == 'target')
        source.update(clipType='video', at=1, hold=hold, **{'from': 2, 'to': 6, 'speed': 2},
                      transition={'type': 'crossfade', 'duration': .5},
                      params={'opaque': [None, False, [], {}]})
        if hold == 'absent':
            source.pop('hold')
        elif isinstance(hold, (int, float)) and hold < .05:
            source.update({'from': 0, 'to': hold})
        next(row for row in rows if row['id'] == 'C')['at'] = 5
        rows.append({'id': 'other-row', 'clipType': 'video', 'track': 'other', 'at': 6,
                     'hold': 2, 'asset': 'picture', 'opaque': [None, False]})
    return bundle, old, new, verified


def capture(bundle, old, new, verified):
    return parent_media.capture_clip_duplicate_media(
        bundle, SOURCE, 'born', old, new, verified_media=verified)


def full_request(commands):
    return {'api_version': 1, 'project_id': 'project', 'timeline_id': 'film',
            'base_revision_id': 'revision', 'base_publication_id': 'publication',
            'commands': commands, 'label': 'Add prepared video', 'merge': 'fields', 'reads': [],
            'steps': [{'id': 'prepared-video', 'label': 'Add prepared video',
                       'intent': 'Duplicate after source', 'tags': ['media'],
                       'command_ordinals': list(range(len(commands))), 'patch': [], 'reads': []}],
            'dry_run': False, 'source': {'via': 'python'}, 'rationale': 'Prepared UI video',
            'group_id': 'prepared-video'}


@cache
def frozen_validator(commit, definition):
    bundle = json.loads(subprocess.check_output(['git', '-C', str(runtime_checkout()), 'show',
        commit + ':runtime_protocol/schemas/timeline-commit-bundle.json']))
    registry = Registry()
    for uri, schema in bundle['resources'].items():
        registry = registry.with_resource(uri, Resource.from_contents(schema, default_specification=DRAFT202012))
    return Draft202012Validator({'$ref': bundle['root'] + '#/$defs/' + definition}, registry=registry)


@pytest.mark.parametrize('hold', [4, None, 'absent', .02])
def test_normalized_production_video_birth_and_guarded_inverse(hold):
    bundle, old, new, verified = case(hold)
    before = copy.deepcopy(bundle)
    command = capture(bundle, old, new, verified)
    validate_command(command)
    # Exact command/request enter both frozen public schemas without substitutes.
    for root in (PRODUCER, RUNTIME):
        frozen_validator(root, 'EditCommand').validate(command)
        frozen_validator(root, 'CommitRequest').validate(full_request([command]))
    plan = execute(bundle, [command], verified_media=verified)
    source_duration = (4 if hold in (None, 'absent') else hold) / 2
    duration = max(.05, source_duration)
    row = State(plan.bundle).entity('parent/clip:born')
    assert row['at'] == pytest.approx(1 + source_duration)
    if hold in (None, 'absent'):
        assert row['from'] == 2 and row['to'] == 6 and row['speed'] == 2
        assert ('hold' in row) == (hold is None)
    else:
        assert row['hold'] == duration
        assert all(key not in row for key in ('from', 'to', 'speed', 'at_ms'))
    assert row['transition'] == {'type': 'crossfade', 'duration': .5}
    assert row['params'] == {'opaque': [None, False, [], {}]}
    assert row['asset'] == new['asset']
    assert 'opaque' not in row and 'nullable' not in row  # outside TimelineData metadata
    assert State(plan.bundle).entity('parent/clip:C')['at'] == pytest.approx(5 + duration)
    assert State(plan.bundle).entity('parent/clip:A') == State(before).entity('parent/clip:A')
    assert State(plan.bundle).entity('parent/clip:other-row') == State(before).entity('parent/clip:other-row')
    assert [row['id'] for row in plan.bundle['parent']['clips']] == ['A', 'target', 'born', 'C', 'other-row']
    assert plan.bundle['parent']['clips'] == plan.bundle['parent']['config']['clips']
    assert command['created'] == ['parent/asset:' + new['asset'], 'parent/clip:born']
    assert command['bindings']['parent.after-clip'] == SOURCE
    assert {dep['key'] for dep in plan.records[0]['dependencies']} == {'media.old', 'media.new'}
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    assert inverse(plan.bundle, plan.records).bundle == before
    assert bundle == before


def test_literal_after_insert_request_is_identical_on_c36_and_new(monkeypatch):
    bundle, old, new, verified = case()
    row = {'id': 'literal', 'clipType': 'video', 'track': 'overlay', 'asset': new['asset'],
           'at': 11, 'hold': 1, 'from': 0, 'to': 1, 'speed': 1, 'nullable': False,
           'opaque': {'caller': [None, False, [], {}]}}
    original = types.ModuleType('astrid.sdk.timeline._c36_parent_media')
    source = subprocess.check_output(['git', 'show', BASE + ':astrid/sdk/timeline/native_parent_media_commands.py'], text=True)
    exec(compile(source, '<c36 parent media>', 'exec'), original.__dict__)
    new_command = parent_media.capture_clip_add_media(bundle, row, new, after=SOURCE, verified_media=verified)
    with monkeypatch.context() as old_code:
        old_code.setattr(parent_media, 'add_media', original.add_media)
        old_command = original.capture_clip_add_media(bundle, row, new, after=SOURCE, verified_media=verified)
        old_plan = execute(bundle, [new_command], verified_media=verified)
    new_plan = execute(bundle, [old_command], verified_media=verified)
    assert new_command == old_command
    assert new_plan.records == old_plan.records and new_plan.bundle == old_plan.bundle
    assert State(new_plan.bundle).entity('parent/clip:literal') == row
    assert State(new_plan.bundle).entity('parent/clip:C')['at'] == 5
    frozen_validator(PRODUCER, 'CommitRequest').validate(full_request([new_command]))


@pytest.mark.parametrize('field', ['unexpected', 'idempotency_key'])
def test_frozen_request_envelope_rejects_unknown_fields(field):
    bundle, old, new, verified = case()
    request = full_request([capture(bundle, old, new, verified)])
    request[field] = 'invalid'
    for root in (PRODUCER, RUNTIME):
        assert not frozen_validator(root, 'CommitRequest').is_valid(request)


@pytest.mark.parametrize('damage', ['unknown-op', 'extra-arg', 'row', 'hold', 'extra-media',
                                  'missing-media', 'bad-id', 'extra-envelope', 'bad-intent'])
def test_new_recipe_rejects_unknown_or_disallowed_fields_without_write(damage):
    bundle, old, new, verified = case()
    command = copy.deepcopy(call(old, new))
    if damage == 'unknown-op':
        command['op'] = 'clip.unknown-media'
    elif damage in ('extra-arg', 'row', 'hold'):
        command['args'][damage] = 1
    elif damage == 'extra-media':
        command['args']['media']['duration_seconds'] = 6
    elif damage == 'missing-media':
        command['args']['media'].pop('catalog_digest')
    elif damage == 'bad-id':
        command['args']['id'] = 'bad/id'
    elif damage == 'extra-envelope':
        command['unexpected'] = True
    else:
        command['intent']['ownership'] = 'occurrence'
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError):
        execute(bundle, [command], verified_media=verified)
    assert bundle == before


@pytest.mark.parametrize('damage', ['missing-input', 'catalog', 'old-catalog', 'object', 'old-object',
    'intrinsic', 'source-bounds', 'source-asset', 'source-track', 'source-window', 'transition-map',
    'ownership', 'occupied-id', 'occupied-alias', 'previous', 'reads', 'created', 'order-claim'])
def test_media_source_previous_read_and_owner_controls_are_atomic(damage):
    bundle, old, new, verified = case()
    command = capture(bundle, old, new, verified)
    if damage == 'missing-input':
        verified = []
    elif damage in ('catalog', 'old-catalog', 'object', 'old-object'):
        pin = command['args']['old_media' if damage.startswith('old') else 'media']
        pin['catalog_digest' if 'catalog' in damage else 'object_id'] = 'sha256:' + 'f' * 64
    elif damage == 'intrinsic':
        verified = copy.deepcopy(verified)
        verified[1]['duration_seconds'] = 1
    elif damage in ('source-bounds', 'source-asset', 'source-track', 'transition-map'):
        for rows in (bundle['parent']['clips'], bundle['parent']['config']['clips']):
            source = next(row for row in rows if row['id'] == 'target')
            source.update({'to': 7} if damage == 'source-bounds' else
                          {'asset': 'foreign'} if damage == 'source-asset' else
                          {'track': 'foreign'} if damage == 'source-track' else {'time_map': {}})
    elif damage == 'source-window':
        bundle['parent']['config']['source_window'] = {'from': 0, 'to': 1}
    elif damage == 'ownership':
        command['select'] = 'occurrence:foreign/clip:target'
    elif damage == 'occupied-id':
        command['args']['id'] = 'C'
    elif damage == 'occupied-alias':
        State(bundle).registry('parent')['assets'][new['asset']] = {'type': 'video', 'media_id': 'foreign'}
    elif damage == 'previous':
        command['expect']['parent/clip:C::at'] = {'state': 'present', 'value': 6}
    elif damage == 'reads':
        command['reads'] = command['reads'][1:]
    elif damage == 'created':
        command['created'] = []
    else:
        command['selections'][0]['order'].reverse()
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError):
        execute(bundle, [command], verified_media=verified)
    assert bundle == before


@pytest.mark.parametrize('damage', ['anchor', 'sibling', 'membership', 'order', 'aba'])
def test_replay_and_inverse_retain_read_order_and_aba_guards(damage):
    bundle, old, new, verified = case()
    plan = execute(bundle, [capture(bundle, old, new, verified)], verified_media=verified)
    head = copy.deepcopy(bundle)
    if damage in ('anchor', 'sibling'):
        ref = SOURCE if damage == 'anchor' else 'parent/clip:C'
        for rows in (head['parent']['clips'], head['parent']['config']['clips']):
            next(row for row in rows if row['id'] == ref.split(':')[-1])['at'] = 9
        changed = later(ref, ['at'], 9)
    else:
        for rows in (head['parent']['clips'], head['parent']['config']['clips']):
            if damage == 'membership':
                rows.append({'id': 'foreign', 'track': 'overlay', 'at': 8, 'hold': 1})
            else:
                rows.reverse()
        changed = later('parent', ['clips'], copy.deepcopy(head['parent']['clips']))
        if damage == 'aba':
            for rows in (head['parent']['clips'], head['parent']['config']['clips']):
                rows.reverse()
    before = copy.deepcopy(head)
    with pytest.raises(CommandError):
        replay(plan, head, verified_media=verified, intervening_records=[changed])
    assert head == before
    with pytest.raises(CommandError):
        inverse(plan.bundle, plan.records, [changed])


@pytest.mark.integration
def test_prepared_video_public_preview_accept_retry_undo_and_no_write(tmp_path, monkeypatch):
    """One new managed-video body, using the exact reviewed Runtime tree."""
    from tests.helpers.runtime import use_runtime, vendored_runtime_commit

    expected_runtime_source = 'f103bb46866999c961a8865636f2e66d544e0c9b'
    expected_runtime_tree = '5d0cedf2e5134acf895e8d43191cc5fdc09f9883'
    runtime_pin = vendored_runtime_commit()
    assert runtime_pin == expected_runtime_source
    root = use_runtime(commit=runtime_pin)
    assert subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip() == (
        expected_runtime_source)
    assert subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD^{tree}'], text=True).strip() == (
        expected_runtime_tree)
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_validation import validate_commit_value

    realm = runtime_media.media_realm(tmp_path, monkeypatch, 'video', historical=False)
    editing = next(realm)
    try:
        old, new = editing.pins['old'], editing.pins['new']
        source = {'id': 'target', 'clipType': 'video', 'track': 'visual', 'asset': old['asset'],
                  'at': 1, 'hold': 4, 'from': 2, 'to': 6, 'speed': 2,
                  'params': {'opaque': [None, False, [], {}]}}
        sibling = dict(source, id='C', at=5)
        for row in (source, sibling):
            birth = runtime_media.birth(editing, row, old)
            birth['created'] = ([('parent/asset:' + old['asset'])] if row['id'] == 'target' else []) + [
                'parent/clip:' + row['id']]
            runtime_media.commit(editing, runtime_media.body(editing, [birth]), row['id'])
        transition = {'type': 'crossfade', 'duration': .5}
        patch = runtime_media.call('doc.patch', {'changes': [{'op': 'set', 'ref': 'parent',
            'path': ['config', 'clips', 'id:target', 'transition'],
            'before': {'state': 'absent'}, 'value': transition}]}, None)
        runtime_media.commit(editing, runtime_media.body(editing, [patch]), 'source-transition')
        source['transition'] = transition
        baseline = runtime_media.current(editing)
        # The exact public MediaSelection comes from Runtime's managed realm,
        # and both selections are resolved by the provider's selected_media port.
        command = call(old, new)
        command['created'] = ['parent/asset:' + new['asset'], 'parent/clip:born']
        request = {**full_request([command]), **runtime_media.body(editing, [command])}
        ids = ['target', 'C']
        request['reads'] = [{'kind': 'collection', 'ref': 'parent', 'path': ['config', 'clips'],
                             'members': sorted(ids), 'order': ids}]
        request['steps'][0]['reads'] = [{'kind': 'field', 'ref': 'parent',
            'path': ['config', 'clips', 'id:target'], 'state': {'state': 'present', 'value': source}}]
        for definition, value in (('EditCommand', command), ('CommitRequest', request)):
            validate_commit_value(value, definition)
            frozen_validator(PRODUCER, definition).validate(value)
        import runtime_protocol

        import astrid
        import astrid_runtime_client

        print('VIDEO_ORIGINS ' + json.dumps({'agent': astrid.__file__,
            'client': astrid_runtime_client.__file__, 'runtime': runtime_protocol.__file__,
            'runtime_tree': expected_runtime_tree, 'request': request}))
        before = runtime_media.rows(editing)
        preview = runtime_media.commit(editing, dict(request, dry_run=True), 'video-preview')
        validate_commit_value(preview, 'CommitPreviewResponse')
        assert preview['receipt'] is None and preview['data']['would_status'] == 'accepted'
        assert runtime_media.rows(editing) == before and runtime_media.current(editing) == baseline
        accepted = runtime_media.commit(editing, request, 'video-accept')
        print('VIDEO_ACCEPTED ' + json.dumps(accepted, sort_keys=True))
        validate_commit_value(accepted, 'CommitAcceptedMutation')
        assert accepted['receipt'] is not None
        assert runtime_media.current(editing) == accepted['data']['canonical_document']
        payload = accepted['data']['canonical_document']['parent_revision']['payload']
        assert [row['id'] for row in payload['clips']] == ['target', 'born', 'C']
        assert next(row for row in payload['clips'] if row['id'] == 'born') == {
            'id': 'born', 'clipType': 'video', 'asset': new['asset'], 'track': 'visual',
            'at': 3, 'hold': 2, 'params': source['params'], 'transition': transition}
        assert next(row for row in payload['clips'] if row['id'] == 'C')['at'] == 7
        assert {json.dumps(value['selection'], sort_keys=True) for value in editing.selected} >= {
            json.dumps(old, sort_keys=True), json.dumps(new, sort_keys=True)}
        history = editing.service.list_timeline_commits(editing.project, 'film', authorize=editing.authorize)
        with monkeypatch.context() as retry:
            from astrid.sdk.timeline import commands

            retry.setattr(commands, 'execute', lambda *a, **kw: pytest.fail('retry executed provider'))
            assert runtime_media.commit(editing, request, 'video-accept') == accepted
        assert editing.service.list_timeline_commits(editing.project, 'film', authorize=editing.authorize) == history
        record = next(record for record in editing.candidates[-1]['records']['commands']
                      if record['command']['op'] == command['op'])
        assert record['command']['created'] == ['parent/asset:' + new['asset'], 'parent/clip:born']
        assert {'reads', 'selections', 'bindings', 'dependencies', 'writes'} <= record.keys()
        assert accepted['data']['label'] == request['label'] and accepted['data']['merge_mode'] == 'fields'
        assert accepted['data']['steps'][0]['id'] == 'prepared-video'
        assert accepted['data']['steps'][0]['patch']
        before_invalid = runtime_media.rows(editing)
        bad = copy.deepcopy(request)
        bad['commands'][0]['args']['media']['catalog_digest'] = 'sha256:' + 'f' * 64
        with pytest.raises(RuntimeErrorBase) as error:
            runtime_media.commit(editing, bad, 'video-invalid')
        invalid = copy.deepcopy(error.value.details)
        assert {'code', 'path', 'current', 'hint', 'suggested_command'} <= invalid.keys()
        assert runtime_media.rows(editing) == before_invalid
        with pytest.raises(RuntimeErrorBase) as stale:
            runtime_media.commit(editing, request, 'video-stale')
        conflict = copy.deepcopy(stale.value.details)
        assert {'code', 'path', 'current', 'hint', 'suggested_command'} <= conflict.keys()
        assert runtime_media.rows(editing) == before_invalid
        undo_call = runtime_media.call('timeline.inverse', {
            'publications': [accepted['receipt']['receipt_id']]}, None)
        undo_request = {**full_request([undo_call]), **runtime_media.body(editing, [undo_call])}
        undo_request['label'] = 'Undo prepared video'
        undo_request['steps'][0].update(id='undo-prepared-video', label='Undo prepared video',
                                        intent='Guarded inverse')
        validate_commit_value(undo_request, 'CommitRequest')
        undone = runtime_media.commit(editing, undo_request, 'video-undo')
        validate_commit_value(undone, 'CommitAcceptedMutation')
        assert undone['data']['canonical_document'] == baseline == runtime_media.current(editing)
        assert editing.service.store.integrity_report()['ok']
        print('VIDEO_WITNESS ' + json.dumps({'runtime': str(root), 'request': request,
            'idempotency_key': 'video-accept', 'preview': preview, 'accepted': accepted,
            'invalid': invalid, 'conflict': conflict, 'undo_request': undo_request,
            'undo': undone, 'history': history,
            'records': editing.candidates[-1]['records']}, sort_keys=True))
    finally:
        realm.close()
