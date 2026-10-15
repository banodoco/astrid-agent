"""Semantic replay proofs using the real detached Checkout and transaction runner."""
import copy
import json
from pathlib import Path

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline import commands, store
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.rebase import rebase
from astrid.sdk.timeline.transaction import edit_transaction

FIXTURES = Path(__file__).parents[2] / 'schemas/change-contract/fixtures'
SID = 'occurrence:occ-A/clip:a-rocket'


@pytest.fixture
def bundle():
    return Checkout(json.loads((Path(__file__).parents[1] /
                    'fixtures/timeline_editing/tiny_publishable.json').read_text())).document()


def call(op='clip.set', select='a-rocket', **args):
    return dict(op=op, select=select, args=args)


def apply(bundle, *calls):
    tl = Checkout(copy.deepcopy(bundle))
    with edit_transaction(tl, 'test', quiet=True, persist=False) as tx:
        tx.canonical = True
        for value in calls:
            commands.execute(tl, value)
    return tl.document(), tx.plan


@pytest.mark.parametrize('path', sorted(FIXTURES.glob('*/opcall.*.json')), ids=lambda p: p.name)
def test_all_shared_opcall_fixtures_are_lossless_or_refused(path):
    value = json.loads(path.read_text())['instance']
    original = copy.deepcopy(value)
    if path.parent.name == 'invalid':
        with pytest.raises(commands.CommandError):
            commands.EditCommand.coerce(value)
    else:
        parsed = commands.EditCommand.coerce(value)
        for key, item in value.items():
            assert parsed.to_json()[key] == item
        assert commands.EditCommand.coerce(parsed).to_json() == parsed.to_json()
    assert value == original


@pytest.mark.parametrize('old', ['absent', None, {'$absent': True}, {'op': 'clear'}])
def test_exact_expectations_distinguish_absent_null_and_opaque_objects(bundle, old):
    tl = Checkout(copy.deepcopy(bundle))
    clip = tl.clip('a-rocket')
    if old != 'absent':
        clip.data['params']['opaque'] = copy.deepcopy(old)
    expected = dict(state='absent') if old == 'absent' else dict(state='present', value=old)
    value = call(**{'opaque': {'retained': None}, '_allow_new': True})
    value['expect'] = {'params.opaque': expected}
    before = tl.document()
    plan = commands.run(tl, value, dry_run=True)
    assert tl.document() == before
    assert plan.calls[0].expect['params.opaque'] == expected
    assert plan.calls[0].authorship['params.opaque'] == 'direct'
    other = copy.deepcopy(value)
    other['expect']['params.opaque'] = dict(state='present', value=None) if old == 'absent' else dict(state='absent')
    with pytest.raises(commands.SemanticConflict, match='same-property'):
        commands.run(tl, other)
    assert tl.document() == before


def test_clear_shape_is_an_explicit_setter_operation_and_null_is_not_clear(bundle):
    after, plan = apply(bundle, call(x={'op': 'clear'}))
    assert 'x' not in Checkout(after).clip('a-rocket').params
    assert plan.calls[0].args['x'] == {'op': 'clear'}
    assert plan.calls[0].expect['params.x'] == dict(state='present', value=10)
    assert plan.direct_writes[0]['after'] == dict(state='absent')
    untouched, _ = apply(bundle, call())
    assert Checkout(untouched).clip('a-rocket').params['x'] == 10
    with pytest.raises(commands.TimelineEditError):
        apply(bundle, call(x=None))


def test_adjacent_calls_keep_intermediate_expectations_and_order(bundle):
    _, plan = apply(bundle, call(x=120), call(x=240))
    assert len(plan.calls) == 2
    assert plan.calls[0].expect['params.x'] == dict(state='present', value=10)
    assert plan.calls[1].expect['params.x'] == dict(state='present', value=20)
    assert commands.compact_calls(plan.calls) == plan.calls
    bad = plan.calls[1].to_json()
    bad['expect']['params.x'] = dict(state='present', value=999)
    with pytest.raises(commands.SemanticConflict):
        apply(bundle, plan.calls[0], bad)


def test_rebase_accepts_disjoint_properties_and_rejects_exact_property(bundle, monkeypatch):
    _, plan = apply(bundle, call(x=240))
    head, _ = apply(bundle, call(y=48))
    original = copy.deepcopy(head), [item.to_json() for item in plan.calls]
    monkeypatch.setattr(store, 'three_way', lambda *a, **k: pytest.fail('document merge'))
    rebased = rebase(plan.calls, head)
    assert not rebased.conflicts and rebased.id_map == {}
    clip = Checkout(rebased.document).clip('a-rocket')
    assert (clip.get('x'), clip.get('y')) == (240, 48)
    assert rebased.plan.calls[0].expect == plan.calls[0].expect
    assert head == original[0] and [item.to_json() for item in plan.calls] == original[1]
    overlap, _ = apply(bundle, call(x=180))
    conflict = rebase(plan.calls, overlap).conflicts[0]
    assert conflict['kind'] == 'same-property'
    assert conflict['path'] == 'params.x' and conflict['id'] == SID
    assert conflict['base'] == dict(state='present', value=10)
    assert conflict['theirs'] == dict(state='present', value=30)


def test_absent_precondition_conflicts_with_present_null_and_clear_rebases(bundle):
    _, plan = apply(bundle, call(y=48))
    head = copy.deepcopy(bundle)
    Checkout(head).clip('a-rocket').params['y'] = None
    assert rebase(plan.calls, head).conflicts[0]['kind'] == 'same-property'
    head, _ = apply(bundle, call(y=48))
    _, clear = apply(bundle, call('clip.clear', keys=['x']))
    result = rebase(clear.calls, head)
    assert not result.conflicts and 'x' not in Checkout(result.document).clip('a-rocket').params
    assert Checkout(result.document).clip('a-rocket').get('y') == 48


def test_deleted_target_never_rebinds_to_a_similar_alias(bundle):
    _, plan = apply(bundle, call(x=240))
    tl = Checkout(copy.deepcopy(bundle))
    tl._internal('A')['clips'] = [c for c in tl._internal('A')['clips'] if c['id'] != 'a-rocket']
    replacement = copy.deepcopy(Checkout(copy.deepcopy(bundle)).clip('a-rocket').data)
    replacement['id'] = 'replacement'
    tl._internal('A')['clips'].append(replacement)
    result = rebase(plan.calls, tl.document())
    assert result.conflicts[0]['kind'] == 'delete-vs-edit'
    assert result.document is None


def test_word_binding_keeps_exact_text_and_refuses_rebinding(bundle):
    text = '  "viral" +9f  '
    _, plan = apply(bundle, call('clip.on', moment=text))
    record = plan.calls[0]
    assert record.bindings[text] == 'word:s1:2'
    assert record.resolved == (SID,)
    head = copy.deepcopy(bundle)
    words = intent.words(Checkout(head).clip('a-vo').data)
    words.insert(0, [0, 0.05, 'new'])
    intent.set_line(Checkout(head).clip('a-vo').data, 's1', words)
    result = rebase(plan.calls, head)
    assert result.conflicts[0]['kind'] == 'dependent'
    assert result.conflicts[0]['base'] == 'word:s1:2'
    assert result.conflicts[0]['theirs'] == 'word:s1:3'


def test_nudge_records_absolute_anchored_intent_and_survives_retime(bundle):
    _, plan = apply(bundle, call('clip.nudge', frames=9))
    assert plan.calls[0].op == 'clip.on'
    assert plan.calls[0].args['moment'] == '"viral" +9f'
    head = copy.deepcopy(bundle)
    Checkout(head).clip('a-vo').data['at'] += .2
    result = rebase(plan.calls, head)
    assert not result.conflicts
    tl = Checkout(result.document)
    assert tl.clip('a-rocket').start == pytest.approx(tl.time('"viral"') + .3)
    assert result.plan.calls[0].bindings == plan.calls[0].bindings


def test_add_then_set_keeps_created_identity_and_dependency(bundle):
    tl = Checkout(copy.deepcopy(bundle))
    add = call('timeline.add', None, element='am-sprite', at='"now"', hold=1, layer='pin', params={'x': 600})
    preview = commands.run(tl, add, dry_run=True)
    assert tl.document() == bundle
    created = preview.calls[0].created
    assert created and created == preview.calls[0].resolved
    assert created[0].startswith('occurrence:occ-B/clip:')
    after, plan = apply(bundle, preview.calls[0], call(select=preview.calls[0].args['clip_id'], x=120))
    assert len(plan.calls) == 2
    assert plan.calls[0].created == created and plan.calls[1].resolved == created
    head, _ = apply(bundle, call(y=48))
    rebased = rebase(plan.calls, head)
    assert not rebased.conflicts
    assert rebased.plan.calls[0].created == created
    assert Checkout(rebased.document).clip(plan.calls[0].args['clip_id']).get('x') == 120
    assert Checkout(after).clip(plan.calls[0].args['clip_id']).get('x') == 120
    collision = rebase(plan.calls, after)
    assert collision.conflicts[0]['kind'] in {'dependent', 'same-property'}


def test_consequences_have_actual_causes_and_survive_no_compaction(bundle):
    _, plan = apply(bundle, call('clip.nudge', frames=9), call(select='b-type', text='new title'))
    assert len(plan.calls) == 2
    movement = [w for w in plan.consequences if w['id'] == SID and w['path'] == 'at']
    assert movement and movement[0]['cause'] == 0
    assert plan.calls[0].authorship['at'] == 'consequence'
    assert plan.calls[1].authorship['params.text'] == 'direct'
    assert all(w['cause'] < len(plan.calls) for w in plan.consequences)
    assert not any('actor' in c.to_json() for c in plan.calls)


def test_unrecorded_relative_call_and_false_resolved_identity_are_refused(bundle):
    assert rebase([call('clip.nudge', frames=9)], bundle).conflicts[0]['kind'] == 'dependent'
    value = call(x=240)
    value['resolved'] = ['occurrence:missing/clip:a-rocket']
    with pytest.raises(commands.SemanticConflict, match='delete-vs-edit'):
        apply(bundle, value)


def test_id_map_rewrites_only_recorded_scoped_ids_and_not_opaque_values(bundle):
    _, plan = apply(bundle, call(x=240))
    head = copy.deepcopy(bundle)
    Checkout(head).clip('a-rocket').data['id'] = 'renamed'
    new = 'occurrence:occ-A/clip:renamed'
    rebased = rebase(plan.calls, head, id_map={SID: new})
    assert not rebased.conflicts and rebased.id_map == {SID: new}
    assert rebased.plan.calls[0].resolved == (new,)
    assert Checkout(rebased.document).clip('renamed').get('x') == 240


def test_scoped_replay_survives_duplicate_local_ids_without_alias_reselection(bundle):
    duplicate = copy.deepcopy(bundle)
    tl = Checkout(duplicate)
    twin = copy.deepcopy(tl.clip('a-rocket').data)
    intent.set_cut(twin, 'c2')
    intent.set_layer(twin, 'twin')
    intent.set_on(twin, None)
    tl._internal('B')['clips'].append(twin)
    _, plan = apply(duplicate, call(select='c1.rocket', x=240))
    result = rebase(plan.calls, duplicate)
    assert not result.conflicts
    clips = {commands.scoped_clip(Checkout(result.document), c): c for c in Checkout(result.document).clips()}
    assert clips[SID].get('x') == 240
    assert clips['occurrence:occ-B/clip:a-rocket'].get('x') == 60


def test_noop_timing_call_keeps_its_direct_precondition(bundle):
    _, plan = apply(bundle, call('clip.on', moment='"viral"'))
    assert plan.calls[0].expect['intent.on'] == dict(state='present', value='"viral"')
    head, _ = apply(bundle, call('clip.on', moment='"went"'))
    assert rebase(plan.calls, head).conflicts[0]['kind'] == 'same-property'


def test_delete_against_edit_is_delete_vs_edit(bundle):
    _, plan = apply(bundle, call('clip.remove'))
    head, _ = apply(bundle, call(x=240))
    assert rebase(plan.calls, head).conflicts[0]['kind'] == 'delete-vs-edit'


def test_missing_and_ambiguous_bindings_are_typed_refusals(bundle):
    with pytest.raises(commands.TimelineEditError, match='not spoken'):
        apply(bundle, call('clip.on', moment='"missing"'))
    duplicated = copy.deepcopy(bundle)
    clip = Checkout(duplicated).clip('b-vo')
    intent.set_line(clip.data, 's2', [[0.1, 0.3, 'viral']])
    with pytest.raises(commands.TimelineEditError, match='spoken 2 times'):
        apply(duplicated, call('clip.on', moment='"viral"'))


def test_compact_calls_preserves_gesture_metadata_without_canonicalizing_opaque_args(bundle):
    _, plan = apply(bundle, call(x=120), call(y=48))
    records = [c.to_json() for c in plan.calls]
    assert [c.to_json() for c in commands.compact_calls(records)] == records
    assert records[0]['expect'] != records[1]['expect']


def test_id_map_does_not_rewrite_scoped_looking_opaque_text(bundle):
    _, plan = apply(bundle, call(select='b-type', text='occurrence:occ-B/clip:b-type'))
    head = copy.deepcopy(bundle)
    Checkout(head).clip('b-type').data['id'] = 'renamed-type'
    result = rebase(plan.calls, head, id_map={'occurrence:occ-B/clip:b-type': 'occurrence:occ-B/clip:renamed-type'})
    assert not result.conflicts
    assert Checkout(result.document).clip('renamed-type').get('text') == 'occurrence:occ-B/clip:b-type'


def test_compile_guard_covers_new_add_params(bundle, monkeypatch):
    resolve = Checkout.resolve
    def corrupt(self):
        result = resolve(self)
        for clip in self.clips():
            if clip.layer_name == 'pin':
                clip.data['params']['x'] = 999
        return result
    monkeypatch.setattr(Checkout, 'resolve', corrupt)
    with pytest.raises(commands.CommandError, match='timing replaced an explicit write'):
        apply(bundle, call('timeline.add', None, element='am-sprite', at='"now"', hold=1,
                           layer='pin', params={'x': 600}))


def test_expectations_use_json_boolean_identity_including_nested_values(bundle):
    tl = Checkout(copy.deepcopy(bundle))
    tl.clip('a-rocket').data['params']['opaque'] = {'nested': [False, None]}
    value = call(opaque='changed', _allow_new=True)
    value['expect'] = {'params.opaque': dict(state='present', value={'nested': [0, None]})}
    with pytest.raises(commands.SemanticConflict) as error:
        commands.run(tl, value)
    assert error.value.to_json()['code'] == 'EXPECT_MISMATCH'


def test_actual_plan_serialization_keeps_the_shared_plan_contract(bundle):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    root = FIXTURES.parent
    schemas = [json.loads(path.read_text()) for path in root.glob('*.schema.json')]
    registry = Registry().with_resources((s['$id'], Resource.from_contents(s)) for s in schemas)
    plan_schema = next(s for s in schemas if s['title'] == 'Plan')
    _, plan = apply(bundle, call('clip.nudge', frames=9), call(x=240))
    Draft202012Validator(plan_schema, registry=registry).validate(plan.to_json())


def test_split_records_created_ids_and_reuses_them_through_replay(bundle):
    after, plan = apply(bundle, call('cut.split', 'c1', on=2.0))
    assert plan.calls[0].args['on'] == 'c1 +2s'
    assert 'cut:c1a' in plan.calls[0].created
    assert any('/clip:' in sid for sid in plan.calls[0].created)
    result = rebase(plan.calls, bundle)
    assert not result.conflicts
    assert result.plan.calls[0].created == plan.calls[0].created
    assert Checkout(result.document).document() == after


def test_unused_id_map_entries_are_not_returned_as_identity_results(bundle):
    _, plan = apply(bundle, call(x=240))
    result = rebase(plan.calls, bundle, id_map={'occurrence:missing/clip:fake': 'occurrence:missing/clip:invented'})
    assert not result.conflicts and result.id_map == {}
