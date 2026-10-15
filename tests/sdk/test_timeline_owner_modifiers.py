"""D-owned command hooks; F compilation and I rendering join separately through A."""
import copy
import json
from pathlib import Path

import pytest

from astrid.sdk.timeline import commands, modifiers, music_beds
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.rebase import rebase
from astrid.sdk.timeline.transaction import edit_transaction

SID = 'occurrence:occ-A/clip:a-rocket'
FIXTURE = Path(__file__).parents[1] / 'fixtures/timeline_editing/tiny_publishable.json'


@pytest.fixture
def bundle():
    return Checkout(json.loads(FIXTURE.read_text())).document()


@pytest.fixture
def declared(monkeypatch):
    # Only the declaration boundary is supplied here. The actual setter,
    # JSON-schema validator, transaction, compiler, provenance and rebase run.
    from astrid.core.element import catalog
    schema = dict(type='object', properties={'strength': dict(type='number', minimum=0, maximum=1),
                   'nested': dict(type='object'), 'keyframes': dict(type='array')}, additionalProperties=False)
    monkeypatch.setattr(catalog, 'read_element_schema', lambda element, **kw: schema if element == 'test.vignette' else (_ for _ in ()).throw(KeyError(element)))
    monkeypatch.setattr(catalog, 'read_element_meta', lambda *a, **kw: dict(kind='modifier', element='test.vignette', bounds='preserve', alpha='preserve', support='preserve', hosts=['composition', 'app']))


def call(path, value):
    return dict(op='clip.set-param', select=SID, args=dict(path=path, value=value))


def row(mid='shade', **params):
    return dict(id=mid, element='test.vignette', params=params or dict(strength=.25))


def apply(bundle, *calls):
    tl = Checkout(copy.deepcopy(bundle))
    with edit_transaction(tl, 'owner test', quiet=True, persist=False) as tx:
        tx.canonical = True
        for value in calls:
            commands.execute(tl, value)
    return tl.document(), tx.plan


def test_modifier_add_edit_order_remove_one_transaction(bundle, declared):
    result, plan = apply(bundle, call('modifiers["shade"]', row()),
        call('modifiers["other"]', row('other', strength=.5)),
        call('modifiers["shade"].params.strength', .75),
        call('modifiers.order', ['other', 'shade']),
        dict(op='clip.clear', select=SID, args={'keys': ['modifiers["other"]']}))
    assert Checkout(result).clip('a-rocket').data['modifiers'] == [row(strength=.75)]
    assert len(plan.calls) == 5
    assert all(c.resolved == (SID,) for c in plan.calls)
    assert plan.calls[2].expect == {'modifiers["shade"].params.strength': {'state': 'present', 'value': .25}}
    assert plan.calls[2].authorship['modifiers["shade"].params.strength'] == 'direct'
    assert all('modifiers[0]' not in w['path'] for w in plan.direct_writes)
    again, replay = apply(bundle, *[c.to_json() for c in plan.calls])
    assert again == result
    assert replay.direct_writes == plan.direct_writes


def test_modifier_rebase_is_by_id_after_order_changes(bundle, declared):
    initial, _ = apply(bundle, call('modifiers["shade"]', row()), call('modifiers["other"]', row('other')))
    _, plan = apply(initial, call('modifiers["shade"].params.strength', .75))
    head, _ = apply(initial, call('modifiers.order', ['other', 'shade']), call('modifiers["other"].params.strength', .5))
    result = rebase(plan.calls, head)
    assert not result.conflicts
    mods = Checkout(result.document).clip('a-rocket').data['modifiers']
    assert mods == [row('other', strength=.5), row(strength=.75)]
    overlap, _ = apply(initial, call('modifiers["shade"].params.strength', .9))
    assert rebase(plan.calls, overlap).conflicts[0]['path'] == 'modifiers["shade"].params.strength'
    removed, _ = apply(initial, dict(op='clip.clear', select=SID, args={'keys': ['modifiers["shade"]']}))
    assert rebase(plan.calls, removed).conflicts


@pytest.mark.parametrize('path,value', [
    ('modifiers[0]', row()), ('modifiers["shade"]', row('wrong')),
    ('modifiers["shade"]', row(strength=2)), ('modifiers["shade"]', row(strength=True)),
    ('modifiers["shade"]', dict(id='shade', element='missing.modifier', params={})),
    ('modifiers["shade"]', dict(id='shade', element='test.vignette', params={}, extra=1)),
    ('modifiers.order', ['shade', 'shade']), ('modifiers["missing"].params.strength', .5),
    ('modifiers["shade"].id', 'new')])
def test_invalid_modifier_rolls_back_whole_transaction(bundle, declared, path, value):
    tl = Checkout(copy.deepcopy(bundle)); before = tl.document(); view = tl.clip('a-rocket')
    with pytest.raises(commands.TimelineEditError):
        with edit_transaction(tl, 'invalid', quiet=True, persist=False):
            commands.execute(tl, call('modifiers["shade"]', row()))
            commands.execute(tl, call(path, value))
    assert tl.document() == before
    assert 'modifiers' not in view.data


def test_nested_values_and_explicit_easing_survive_json_and_replay(bundle, declared):
    params = dict(strength=.5, nested={'nullable': None, 'easing': 'linear', 'absent': {}},
                  keyframes=[{'at': 0, 'value': .25, 'easing': 'linear'}, {'at': 12, 'value': .8}])
    after, plan = apply(bundle, call('modifiers["shade"]', row(**params)),
        dict(op='clip.set-look', select=SID, args={'entrance': {'type': 'fade', 'duration': .5, 'params': {'easing': 'linear'}}}))
    restored = json.loads(json.dumps(after)); loaded = Checkout(restored)
    assert loaded.clip('a-rocket').data['modifiers'][0]['params'] == params
    assert loaded.clip('a-rocket').data['entrance']['params']['easing'] == 'linear'
    assert 'easing' not in params['keyframes'][1]
    replay, _ = apply(bundle, *json.loads(json.dumps([c.to_json() for c in plan.calls])))
    assert replay == after


def carrier_bundle(bundle, outputs=(), *, owner='occurrence:occ-A/clip:bed'):
    from astrid.sdk.timeline import music_beds
    source = copy.deepcopy(Checkout(bundle).clip('a-rocket').data)
    source['id'] = 'bed'
    # These ID-map fixtures change ownership, not the owner's film clock.
    for placement in bundle['placements']:
        placement['placement']['start_ms'] = 0
    # Numeric authored timing: do not inherit the sprite's word anchor.
    source['app'].pop('on', None)
    source['at'], source['hold'] = (0, 4) if outputs else (1, .5)
    basis = [dict(at_s=1, delta_s=-.5)]
    tl = Checkout(bundle)
    sid, clips = music_beds.locate(tl, owner, source)
    projection, _, _ = music_beds.materialize(source, basis, owner, start=tl._shot_start(sid), fps=tl.fps)
    assert [clip['id'] for clip in projection] == list(outputs)
    clips.extend(projection)
    ns = bundle['parent']['config'].setdefault('app', {}).setdefault('astrid', {})
    # This fixture exercises authored v2 carriers in the joined F/D source.
    ns['v'] = 2
    ns['music_beds'] = {owner: dict(owner=owner, source=source, ripple_basis=basis, outputs=list(outputs))}
    music_beds.validate(tl)
    return owner


@pytest.mark.parametrize('outputs', [[], ['bed', 'bed:b']])
def test_carrier_fixture_projects_positive_duration_source(bundle, outputs):
    from astrid.core.timeline.cuts import clip_duration
    from astrid.sdk.timeline import music_beds
    owner = carrier_bundle(bundle, outputs)
    tl = Checkout(bundle)
    bed = commands.music_carriers(tl)[owner]
    assert clip_duration(bed['source']) > 0
    sid, clips = music_beds.locate(tl, owner, bed['source'])
    projected, _, _ = music_beds.materialize(bed['source'], bed['ripple_basis'], owner,
                                            start=tl._shot_start(sid), fps=tl.fps)
    assert [clip['id'] for clip in projected] == outputs
    assert [clip for clip in clips if clip['id'] in outputs] == projected
    assert tl.clip(owner).data is bed['source']
    music_beds.validate(tl)


@pytest.mark.parametrize('outputs', [[], ['bed', 'bed:b']])
def test_authored_music_entity_and_guards_never_require_outputs(bundle, outputs):
    owner = carrier_bundle(bundle, outputs)
    tl = Checkout(bundle); states = commands.entity_states(tl)
    assert states[owner]['id'] == 'bed'
    assert states[owner]['music_bed']['outputs'] == outputs
    assert all('bed:b' not in key for key in states)
    assert 'music_beds' not in states['parent']['config']['app']['astrid']
    after, plan = apply(bundle, dict(op='clip.set', select=owner, args={'x': 240}))
    assert plan.calls[0].resolved == (owner,)
    assert plan.calls[0].expect['params.x'] == {'state': 'present', 'value': 10}
    saved = json.loads(json.dumps(after))
    again, _ = apply(bundle, plan.calls[0])
    assert again == saved
    # Recorded relative intent cannot silently replay against the changed source.
    # Runtime authentication/deduplication is intentionally outside this D runner.
    with pytest.raises(commands.SemanticConflict):
        apply(after, plan.calls[0])
    assert commands.music_carriers(Checkout(after))[owner]['ripple_basis'] == [dict(at_s=1, delta_s=-.5)]


def test_shared_local_source_ids_keep_scoped_replay_and_compaction(bundle):
    owner = carrier_bundle(bundle)
    other = 'occurrence:occ-B/clip:bed'
    beds = music_beds.collection(Checkout(bundle))
    beds[other] = copy.deepcopy(beds[owner]); beds[other]['owner'] = other
    after, plan = apply(bundle, dict(op='clip.set', select=owner, args={'x': 240}))
    assert commands.compact_calls(plan.calls, Checkout(after)) == plan.calls
    assert commands.music_carriers(Checkout(after))[other]['source']['params']['x'] == 10
    again, _ = apply(bundle, *plan.calls)
    assert again == after


def test_modifier_save_load_undo_redo_keep_instances_and_values(bundle, declared, tmp_path, monkeypatch):
    monkeypatch.setenv('BANODOCO_LOCAL_DATA_ROOT', str(tmp_path / 'data'))
    path = tmp_path / 'modifiers.json'
    tl = Checkout(bundle, path=path)
    tl.save(quiet=True)
    with tl.edit('modifiers', quiet=True):
        commands.execute(tl, call('modifiers["shade"]', row(nested={'easing': 'linear', 'opaque': [None, {'x': True}]})))
        commands.execute(tl, call('modifiers["other"]', row('other')))
        commands.execute(tl, call('modifiers.order', ['other', 'shade']))
    final = copy.deepcopy(tl.clip('a-rocket').data['modifiers'])
    fresh = Checkout.load(path)
    assert fresh.clip('a-rocket').data['modifiers'] == final
    assert fresh.undo(1) == ['modifiers']
    assert 'modifiers' not in fresh.clip('a-rocket').data
    assert fresh.redo(1) == ['modifiers']
    assert Checkout.load(path).clip('a-rocket').data['modifiers'] == final


def test_music_rebase_maps_owner_without_changing_opaque_values(bundle):
    owner = carrier_bundle(bundle)
    _, plan = apply(bundle, dict(op='clip.set', select=owner, args={'x': 240}))
    head = copy.deepcopy(bundle)
    beds = music_beds.collection(Checkout(head))
    new = 'occurrence:occ-B/clip:forked-bed'
    clone = beds.pop(owner); clone['owner'] = new; clone['source']['id'] = 'forked-bed'
    clone['source']['params']['opaque'] = owner
    beds[new] = clone
    result = rebase(plan.calls, head, id_map={owner: new})
    assert not result.conflicts
    assert result.id_map == {owner: new}
    source = commands.music_carriers(Checkout(result.document))[new]['source']
    assert source['params']['x'] == 40
    assert source['params']['opaque'] == owner
    assert result.plan.calls[0].resolved == (new,)


@pytest.mark.parametrize('outputs', [[], ['bed', 'bed:b']])
def test_music_basis_changes_are_guarded_owner_scoped_consequences(bundle, monkeypatch, outputs):
    owner = carrier_bundle(bundle, outputs)
    # Exercise an actual existing operation envelope with its producer-side
    # consequence seam supplied. This is D recording/replay proof, not F timing
    # math or Runtime authenticated-acceptance proof.
    original = commands.REGISTRY['clip.set']
    def source_and_basis(self, **params):
        result = original.handler(self, **params)
        bed = commands.music_carriers(self._tl)[owner]
        bed['ripple_basis'].append(dict(at_s=2, delta_s=.25))
        from astrid.sdk.timeline.music_beds import project
        project(self._tl)
        return result
    monkeypatch.setitem(commands.REGISTRY, 'clip.set', commands.Operation('clip.set', 'clip', 'set', source_and_basis))
    after, plan = apply(bundle, dict(op='clip.set', select=owner, args={'x': 240}))
    record = plan.calls[0]
    assert record.expect['music_bed.ripple_basis'] == {'state': 'present', 'value': [dict(at_s=1, delta_s=-.5)]}
    assert record.authorship['music_bed.ripple_basis'] == 'consequence'
    assert any(w['id'] == owner and w['path'] == 'music_bed.ripple_basis' for w in plan.consequences)
    if outputs:
        spans = [w for w in plan.consequences if 'projection' in w['path']]
        assert spans and all(w['id'] == owner and w['cause'] == 0 for w in spans)
        assert all(w['id'] == owner for w in plan.direct_writes)
    replay, _ = apply(bundle, record)
    assert replay == after
    # Isolate the basis guard: restore just the direct source property.
    commands.music_carriers(Checkout(after))[owner]['source']['params']['x'] = 10
    before = copy.deepcopy(after)
    with pytest.raises(commands.SemanticConflict, match='music_bed.ripple_basis'):
        apply(after, record)
    assert after == before


def test_scoped_compaction_does_not_lookup_reused_local_source_ids():
    calls = [commands.EditCommand('clip.remove', 'occurrence:one/clip:bed', {}, resolved=('occurrence:one/clip:bed',)),
             commands.EditCommand('timeline.add', None, {'clip_id': 'bed'}, resolved=('occurrence:one/clip:bed',))]
    class NoLocalLookup:
        def clip(self, *args):
            pytest.fail('compaction must not re-resolve a local alias')
    assert commands.compact_calls(calls, NoLocalLookup()) == tuple(calls)


def test_duplicate_modifier_ids_fail_before_source_mutation(bundle, declared):
    tl = Checkout(bundle)
    tl.clip('a-rocket').data['modifiers'] = [row(), row()]
    before = tl.document()
    with pytest.raises(commands.TimelineEditError, match='duplicate modifier'):
        commands.run(tl, call('modifiers["shade"].params.strength', .5))
    assert tl.document() == before


def test_unknown_modifier_declaration_refuses_without_catalog_mock(bundle):
    tl = Checkout(bundle); before = tl.document()
    with pytest.raises(commands.TimelineEditError, match='invalid modifier'):
        commands.run(tl, call('modifiers["shade"]', row()))
    assert tl.document() == before


def test_modifier_nested_edit_and_duplicate_preserve_owner_local_instances(bundle, declared):
    initial, _ = apply(bundle, call('modifiers["shade"]', row(keyframes=[{'frame': 0, 'value': .2, 'easing': 'linear'}, {'frame': 12, 'value': .5}])))
    after, plan = apply(initial, call('modifiers["shade"].params.keyframes[1].value', .9),
        dict(op='cut.duplicate', select='c1', args={'at': 2.2}))
    tl = Checkout(after)
    original = tl.clip('a-rocket').data['modifiers']
    copied = [c for c in tl.cut(plan.calls[1].args['id']).clips if c.element == 'am-sprite'][0]
    assert copied.data['modifiers'] == original
    assert copied.data['modifiers'] is not original
    assert copied.data['modifiers'][0]['id'] == 'shade'
    assert commands.scoped_clip(tl, copied) != SID
    assert original[0]['params']['keyframes'][0]['easing'] == 'linear'
    assert original[0]['params']['keyframes'][1]['value'] == .9
    assert plan.calls[0].authorship['modifiers["shade"].params.keyframes[1].value'] == 'direct'
    replay, _ = apply(initial, *plan.calls)
    assert replay == after


def test_modifier_property_path_with_quoted_nested_key_refuses_atomically(bundle, declared):
    initial, _ = apply(bundle, call('modifiers["shade"]', row(nested={})))
    tl = Checkout(initial); before = tl.document()
    with pytest.raises(commands.TimelineEditError, match='property path'):
        commands.run(tl, call('modifiers["shade"].params.nested["key.with.dots"]', .5))
    assert tl.document() == before


def test_shared_authored_alias_and_duplicate_occurrence_refuse_atomically(bundle):
    owner = carrier_bundle(bundle)
    other = 'occurrence:occ-B/clip:bed'
    beds = music_beds.collection(Checkout(bundle))
    beds[other] = copy.deepcopy(beds[owner]); beds[other]['owner'] = other
    tl = Checkout(bundle); before = tl.document()
    with pytest.raises(commands.TimelineEditError):
        commands.run(tl, dict(op='clip.set', select='bed', args={'x': 240}))
    assert tl.document() == before
    duplicate = copy.deepcopy(bundle)
    duplicate['placements'][1]['occurrence_id'] = duplicate['placements'][0]['occurrence_id']
    with pytest.raises(commands.CommandError, match='Duplicate occurrence'):
        commands.run(Checkout(duplicate), dict(op='clip.set', select=owner, args={'x': 240}))


def test_existing_occurrence_selection_is_used_without_first_placement_fallback(bundle):
    tl = Checkout(bundle)
    tl.bundle['placements'].append({**copy.deepcopy(tl.bundle['placements'][0]), 'occurrence_id': 'other-A'})
    view = commands.authored_owner(tl, SID)
    with pytest.raises(commands.CommandError, match='unambiguous occurrence'):
        commands.selected_ids(tl, view, 'clip')
    view._occurrence_id = 'occ-A'
    assert commands.selected_ids(tl, view, 'clip') == (SID,)
    view._occurrence_id = 'missing'
    with pytest.raises(commands.CommandError, match='unambiguous occurrence'):
        commands.selected_ids(tl, view, 'clip')


def test_modifier_replacement_provenance_addresses_the_instance(bundle, declared):
    initial, _ = apply(bundle, call('modifiers["shade"]', row()))
    _, plan = apply(initial, call('modifiers["shade"]', row(strength=.75)))
    assert any(w['id'] == SID and w['path'] == 'modifiers["shade"]' for w in plan.direct_writes)
    assert not any(w['path'] == 'modifiers["shade"].params.strength' for w in plan.consequences)


def test_parent_bed_projection_is_an_owner_consequence_not_parent_authorship(bundle):
    owner = carrier_bundle(bundle, ['bed', 'bed:b'], owner='parent/clip:bed')
    states = commands.entity_states(Checkout(bundle))
    assert states['parent']['clips'] == []
    assert 'parent/clip:bed:b' not in states
    assert states[owner]['music_bed']['projection']['bed:b']['id'] == 'bed:b'
    view = commands.recorded_owner(Checkout(bundle), commands.EditCommand('clip.set', owner, {}, resolved=(owner,)), 'clip')
    assert view.id == 'bed' and view.shot_id is None


def test_enrolled_source_collision_refuses_before_mutation(bundle):
    owner = carrier_bundle(bundle)
    source = commands.music_carriers(Checkout(bundle))[owner]['source']
    bundle['shots']['A']['internal_timeline']['clips'].append(copy.deepcopy(source))
    tl = Checkout(bundle); before = tl.document()
    with pytest.raises(commands.CommandError, match='collides'):
        commands.run(tl, dict(op='clip.set', select=owner, args={'x': 240}))
    assert tl.document() == before


def test_modifier_on_audio_and_nonmodifier_declarations_refuse(bundle, declared, monkeypatch):
    tl = Checkout(copy.deepcopy(bundle)); before = tl.document()
    request = call('modifiers["shade"]', row())
    request['select'] = 'occurrence:occ-A/clip:a-vo'
    with pytest.raises(commands.TimelineEditError, match='visual subtree'):
        commands.run(tl, request)
    assert tl.document() == before
    from astrid.core.element import catalog
    monkeypatch.setattr(catalog, 'read_element_meta', lambda *a, **kw: {'kind': 'ordinary'})
    with pytest.raises(commands.TimelineEditError, match='identity/subtype'):
        commands.run(tl, call('modifiers["shade"]', row()))
    assert tl.document() == before


def test_first_enrollment_records_absent_basis_guard(bundle, monkeypatch):
    original = commands.REGISTRY['clip.set']
    def enroll(self, **params):
        result = original.handler(self, **params)
        owner = commands.scoped_clip(self._tl, self)
        ns = self._tl.bundle['parent']['config'].setdefault('app', {}).setdefault('astrid', {})
        ns['v'] = 2
        ns.setdefault('music_beds', {})[owner] = dict(owner=owner, source=copy.deepcopy(self.data),
                                                    ripple_basis=[dict(at_s=1, delta_s=.5)], outputs=[self.id])
        return result
    monkeypatch.setitem(commands.REGISTRY, 'clip.set', commands.Operation('clip.set', 'clip', 'set', enroll))
    after, plan = apply(bundle, dict(op='clip.set', select=SID, args={'x': 240}))
    record = plan.calls[0]
    assert record.expect['music_bed.ripple_basis'] == {'state': 'absent'}
    commands.music_carriers(Checkout(after))[SID]['source']['params']['x'] = 10
    with pytest.raises(commands.SemanticConflict, match='music_bed.ripple_basis'):
        apply(after, record)


def test_rebase_rewrites_consequence_only_owner_and_schema_owned_source_id(bundle):
    from astrid.sdk.timeline.rebase import _rewrite, _identities
    old = 'occurrence:occ-A/clip:bed'; new = 'occurrence:occ-B/clip:forked'
    call = commands.EditCommand('timeline.insert-time', None, {'seconds': .5, 'at': 1}, resolved=(),
        expect={old + '::music_bed.ripple_basis': {'state': 'present', 'value': []}},
        authorship={old + '::music_bed.ripple_basis': 'consequence'}, created=(), bindings={})
    assert old in _identities(call)
    rewritten = _rewrite(call, {old: new})
    assert rewritten.expect == {new + '::music_bed.ripple_basis': {'state': 'present', 'value': []}}
    assert rewritten.authorship == {new + '::music_bed.ripple_basis': 'consequence'}
    assert rewritten.resolved == ()
    remove = commands.EditCommand('clip.remove', old, {}, resolved=(old,),
        expect={'': {'state': 'present', 'value': {'id': 'bed', 'params': {'opaque': 'bed'}}}},
        authorship={'': 'direct'}, created=(), bindings={})
    assert _rewrite(remove, {old: new}).expect['']['value'] == {'id': 'forked', 'params': {'opaque': 'bed'}}


def test_removed_authored_source_has_separate_carrier_consequences(bundle, monkeypatch):
    owner = carrier_bundle(bundle, ['bed', 'bed:b'])
    def remove(self):
        bed = music_beds.collection(self._tl).pop(owner)
        clips = self._tl._internal(self.shot_id)['clips']
        clips[:] = [c for c in clips if c['id'] not in bed['outputs']]
    monkeypatch.setitem(commands.REGISTRY, 'clip.remove', commands.Operation('clip.remove', 'clip', 'remove', remove))
    after, plan = apply(bundle, dict(op='clip.remove', select=owner, args={}))
    assert owner not in commands.entity_states(Checkout(after))
    assert 'music_bed' not in plan.calls[0].expect['']['value']
    assert any(w['id'] == owner and w['path'] == 'music_bed' for w in plan.consequences)
    assert plan.calls[0].expect['music_bed.ripple_basis']['value'] == [dict(at_s=1, delta_s=-.5)]
    replay, _ = apply(bundle, plan.calls[0])
    assert replay == after


def test_rebase_consumes_id_map_for_consequence_only_owner(bundle, monkeypatch):
    old = carrier_bundle(bundle)
    def resolve(self):
        bed = next(iter(commands.music_carriers(self).values()))
        bed['ripple_basis'].append(dict(at_s=2, delta_s=.25))
        from astrid.sdk.timeline.music_beds import project
        project(self)
    monkeypatch.setitem(commands.REGISTRY, 'timeline.resolve', commands.Operation('timeline.resolve', 'timeline', 'resolve', resolve))
    _, plan = apply(bundle, dict(op='timeline.resolve', select=None, args={}))
    assert plan.calls[0].resolved == ()
    assert old + '::music_bed.ripple_basis' in plan.calls[0].expect
    head = copy.deepcopy(bundle); beds = music_beds.collection(Checkout(head))
    new = 'occurrence:occ-B/clip:bed'; bed = beds.pop(old); bed['owner'] = new; beds[new] = bed
    result = rebase(plan.calls, head, id_map={old: new})
    assert not result.conflicts and result.id_map == {old: new}
    assert commands.music_carriers(Checkout(result.document))[new]['ripple_basis'] == [dict(at_s=1, delta_s=-.5), dict(at_s=2, delta_s=.25)]


def test_instance_remove_and_add_rebase_over_independent_order(bundle, declared):
    initial, _ = apply(bundle, call('modifiers["shade"]', row()), call('modifiers["other"]', row('other')))
    _, removal = apply(initial, dict(op='clip.clear', select=SID, args={'keys': ['modifiers["shade"]']}))
    _, addition = apply(initial, call('modifiers["third"]', row('third')))
    head, _ = apply(initial, call('modifiers.order', ['other', 'shade']))
    removed = rebase(removal.calls, head)
    assert not removed.conflicts
    assert Checkout(removed.document).clip('a-rocket').data['modifiers'] == [row('other')]
    added = rebase(addition.calls, head)
    assert not added.conflicts
    assert [r['id'] for r in Checkout(added.document).clip('a-rocket').data['modifiers']] == ['other', 'shade', 'third']
    collision, _ = apply(head, call('modifiers["third"]', row('third')))
    assert rebase(addition.calls, collision).conflicts


def test_authored_music_v1_refuses_before_mutation(bundle):
    owner = carrier_bundle(bundle)
    bundle['parent']['config']['app']['astrid']['v'] = 1
    before = copy.deepcopy(bundle)
    with pytest.raises(commands.CommandError, match='music_beds requires app.astrid.v=2'):
        commands.run(Checkout(bundle), dict(op='clip.set', select=owner, args={'x': 240}))
    assert bundle == before


def test_explicit_missing_occurrence_refuses_on_single_placement(bundle):
    tl = Checkout(bundle)
    view = commands.authored_owner(tl, SID)
    view._occurrence_id = 'missing'
    before = tl.document()
    with pytest.raises(commands.CommandError, match='unambiguous occurrence'):
        commands.selected_ids(tl, view, 'clip')
    assert tl.document() == before
