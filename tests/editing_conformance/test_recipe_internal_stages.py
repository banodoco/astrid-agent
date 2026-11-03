"""One deterministic recipe may observe its own intermediate authored state."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline import commands
from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, present
from astrid.sdk.timeline._recording import selection
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import bundle, inverse


@pytest.fixture
def recipe(monkeypatch):
    def apply(ctx, command):
        ref = command['select']
        owner = ref.rsplit('/', 1)[0]
        ctx.read(ref, ['opaque'])
        ctx.bind('consumer', ref)
        for value in (True, 1, None):
            ctx.write(ref, ['probe'], value)
            ctx.read(ref, ['probe'])
        ctx.clear(ref, ['probe'])
        ctx.read(ref, ['probe'])
        for identity in command['args']['tracks']:
            created = ctx.create(owner, 'track', {'id': identity, 'kind': 'visual'})
            ctx.read(created, ['kind'])
            ctx.bind(identity, created)
            ctx.select('collection.tracks', owner)
            ctx.write(ref, ['track'], identity)
            ctx.read(ref, ['track'])

    registered = commands.operations()
    registered['fixture.internal-stages'] = commands.Operation({'type': 'object'}, apply, {}, True)
    monkeypatch.setattr(commands, 'operations', lambda: registered)
    return lambda ref, tracks: {'schema_version': 1, 'op': 'fixture.internal-stages',
                               'version': '1', 'select': ref, 'args': {'tracks': tracks}}


@pytest.mark.parametrize('ref', ['parent/clip:direct', 'occurrence:occ-1/clip:picture-1'])
@pytest.mark.parametrize('tracks', [[], ['c', 'd']])
def test_internal_stages_whole_authored_roundtrip_redo_and_current_head_claims(recipe, ref, tracks):
    original = bundle()
    plan = commands.execute(original, [recipe(ref, tracks)])
    records = copy.deepcopy(plan.records)
    assert len(records) == 1
    assert [r['state'] for r in records[0]['reads'] if r['path'] == ['probe']] == [
        present(True), present(1), present(None), ABSENT]
    undone = inverse(plan.bundle, plan.records)
    assert json.dumps(undone.bundle) == json.dumps(original)
    assert plan.records == records
    for read in undone.commands[0]['reads']:
        assert State(plan.bundle).matches(read['ref'], read['path'], read['state'])
    for selected in undone.commands[0]['selections']:
        assert selection(State(plan.bundle), selected['key'], selected['owner'],
                         selected['members']) == selected
    assert replay(undone, plan.bundle, inverse_records=plan.records).records == undone.records
    assert json.dumps(inverse(undone.bundle, undone.records).bundle) == json.dumps(plan.bundle)


@pytest.mark.parametrize('ref', ['parent/clip:direct', 'occurrence:occ-1/clip:picture-1'])
def test_internal_recipe_captured_intent_replays(recipe, ref):
    original = bundle()
    plan = commands.execute(original, [recipe(ref, ['c', 'd'])])
    for read in plan.commands[0]['reads']:
        assert State(original).matches(read['ref'], read['path'], read['state'])
    assert replay(plan, original).records == plan.records


def test_internal_stages_preserve_source_disjoint_c_and_reject_aba_dependents(recipe):
    ref = 'occurrence:occ-1/clip:picture-1'
    original = bundle()
    plan = commands.execute(original, [recipe(ref, ['c', 'd'])])

    def edit_c(state):
        command = capture_clip_set_params(state, ref, values={'future': [None, False, 1]})
        return commands.execute(state, [command])

    concurrent = edit_c(plan.bundle)
    head, history = concurrent.bundle, concurrent.records
    assert replay(plan, edit_c(original).bundle, intervening_records=history).bundle == head
    undone = inverse(head, plan.records, history)
    assert undone.bundle == edit_c(original).bundle
    assert inverse(undone.bundle, undone.records).bundle == head
    aba = commands.execute(head, [recipe(ref, [])])
    assert aba.bundle == head
    with pytest.raises(CommandError) as error:
        inverse(aba.bundle, plan.records, [*history, *aba.records])
    assert error.value.kind == 'intervening_write'
    dependent = copy.deepcopy(head)
    State(dependent).config('occurrence:occ-1').setdefault('effects', []).append({'track': 'c'})
    before = copy.deepcopy(dependent)
    with pytest.raises(CommandError) as error:
        inverse(dependent, plan.records, history)
    assert error.value.kind == 'surviving_dependents'
    assert dependent == before


@pytest.mark.parametrize('failure', ['unrelated-read', 'bool-number', 'missing-binding', 'selection'])
def test_internal_stages_require_exact_proof_and_refuse_atomically(recipe, failure):
    ref = 'parent/clip:direct'
    plan = commands.execute(bundle(), [recipe(ref, ['c', 'd'])])
    broken = copy.deepcopy(plan.records)
    if failure == 'unrelated-read':
        broken[0]['reads'][0]['state'] = present('never observed')
    elif failure == 'bool-number':
        broken[0]['writes'][1]['after'] = present(True)
    elif failure == 'missing-binding':
        broken[0]['bindings'].append({'key': 'missing', 'ref': 'parent/track:missing'})
    else:
        broken[0]['selections'][-1]['members'].append('parent/track:missing')
    before = copy.deepcopy(plan.bundle)
    with pytest.raises(CommandError):
        inverse(plan.bundle, broken)
    assert plan.bundle == before
