"""Logical inverse representation proofs on the existing in-memory history port."""
import copy
import json

import pytest
import test_timeline_publication_inverse as publication_fixture
from test_timeline_identity_inverse import seed
from test_timeline_narration_reflow import line, op, pinned, take
from test_timeline_publication_inverse import publish
from test_timeline_saved_publish import synthetic_scene_answers  # noqa: F401

from astrid.sdk.timeline import commands, inverse, narration
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commit import RuntimeCommitService, _Refusal
from astrid.sdk.timeline.saved_intent import read_saved_calls

setup = publication_fixture.setup
pytestmark = pytest.mark.usefixtures("synthetic_scene_answers")


def version(document):
    return document['parent'].get('config', {}).get('app', {}).get('astrid', {}).get('v', 0)


def seven_command_draft(setup, monkeypatch):
    """D211's seven operations, with existing exact synthetic takes and fake acceptance."""
    from test_timeline_compatibility import candidate

    _, runtime, client = setup
    old = take('It went viral.', voice='original', duration=1.872)
    fresh = take('Then it faded.', voice='replacement', rate='+20%', duration=1.872)
    takes = {narration.take_key(t['refs']): t for t in (old, fresh)}
    bundle = candidate(0)
    bundle.update(project_id='p', timeline_id='t')
    for key in ('parent', 'base_parent_payload'):
        bundle[key]['config'] = dict(tracks=[dict(id='voice', kind='audio')])
    before, _, _ = RuntimeCommitService._apply(bundle, [op(line(None, 'speech-original', old,
        segment='original-speech-line', gap=.3))], narration_takes=takes)
    before = pinned(before)
    seed(runtime, before)
    original_port = runtime.port
    def port(body):
        value = original_port(body)
        value.narration_take = lambda refs: copy.deepcopy(takes[narration.take_key(refs)])
        return value
    runtime.port = port
    monkeypatch.setattr(narration, 'read_managed_take',
                        lambda _c, _p, refs: copy.deepcopy(takes[narration.take_key(refs)]))
    monkeypatch.setattr('astrid.sdk.invocation.invoke', lambda *_a, **_k: pytest.fail('inverse generated speech'))
    draft = Checkout.draft('p', 't', name='versioned-inverse')
    draft._narration_takes = takes
    calls = [
        dict(op='timeline.add', select='parent/track:music', expect={'parent/track:music::': dict(state='absent')}, args=dict(
            track=dict(id='music', kind='audio', label='Existing imported D music bed'))),
        dict(op='timeline.register-asset', select=None, args=dict(asset_id='d211-music',
            entry=dict(duration=2, object_id='sha256:' + '1' * 64, type='audio'))),
        dict(op='timeline.add-media', select=None, created=['parent/clip:d211-music-bed'], args=dict(asset='d211-music', at=0.,
            clip_id='d211-music-bed', for_seconds=2, track='music', trim_in=0, trim_out=None, until=None)),
        dict(op='clip.set-look', select='d211-music-bed', args=dict(speed=.5)),
        dict(op='clip.set-beats', select='d211-music-bed', args=dict(source=dict(
            bpm=132., beats=[0., .454545, .909091, 1.363636, 1.818182], downbeats=[0., 1.818182],
            bars=[0.], source='chiptune.compose'))),
        dict(op='timeline.adopt', select=None, args=dict(sources=['parent/clip:d211-music-bed'],
            alignment={'parent/clip:d211-music-bed': dict(confidence=1, source_offset_s=0)})),
        op(line(None, 'd211-new-line', fresh, segment='d211-new-line', gap=.3),
           line(None, 'speech-original', old, segment='original-speech-line', gap=.3)),
    ]
    with draft.edit('D211 seven-command reflow', quiet=True):
        for call in calls:
            commands.execute(draft, call)
    return draft, runtime, client, before, takes


# Three full saved-publication journeys (including replays) exceed the per-edit default.
@pytest.mark.timeout(300)
def test_seven_command_accepted_adoption_undo_redo_retains_representation(setup, monkeypatch):
    draft, runtime, client, before, takes = seven_command_draft(setup, monkeypatch)
    original = publish(draft, client, 'D211 seven-command reflow')
    assert [c['op'] for c in original['change']['commands']] == [
        'timeline.add', 'timeline.register-asset', 'timeline.add-media', 'clip.set-look',
        'clip.set-beats', 'timeline.adopt', 'timeline.apply-script']
    assert original['change']['commands'][5]['expect']['parent::config.app'] == dict(state='absent')
    accepted = Checkout.load(draft.path)
    assert version(accepted.document()) == 2
    assert accepted.bundle['base_parent_payload']['config']['app']['astrid']['v'] == 2
    accepted.undo(client=client, publication=original['accepted_publication_id'])
    staged = read_saved_calls(Checkout.load(draft.path))
    parent = next(w for w in staged[0]['args']['writes'] if w['id'] == 'parent' and w['path'] == 'config.app')
    assert parent['after'] == dict(state='present', value=dict(astrid=dict(v=2)))
    undone = publish(Checkout.load(draft.path), client, 'Undo D211')
    assert version(undone['canonical_document']) == 2
    assert 'bed' not in undone['canonical_document']['parent']['config']['app']['astrid']
    assert undone['canonical_document']['parent']['clips'] == before['parent']['clips']
    assert undone['canonical_document']['parent']['config']['tracks'] == before['parent']['config']['tracks']
    replay_body, headers = runtime.commits[-1]
    count = len(runtime.publications)
    replay = client._transport.commit_timeline(json.loads(replay_body))
    assert replay == runtime.saved[json.loads(replay_body)['request_id']]['result']
    assert len(runtime.publications) == count
    redo = Checkout.load(draft.path)
    redo.undo(client=client, publication=undone['accepted_publication_id'])
    redone = publish(Checkout.load(draft.path), client, 'Redo D211')
    assert version(redone['canonical_document']) == 2
    assert redone['canonical_document']['parent'] == original['canonical_document']['parent']
    redo_body = runtime.commits[-1][0]
    assert client._transport.commit_timeline(json.loads(redo_body)) == runtime.saved[json.loads(redo_body)['request_id']]['result']
    assert len(runtime.publications) == 3
    assert {narration.take_key(c.data['app']['astrid']['take']) for c in narration.line_clips(
        Checkout(redone['canonical_document'])).values()} == set(takes)


def container_transition(path, old_version=None):
    """One recorded historical enclosing-property replacement on an empty film."""
    from test_timeline_compatibility import candidate

    before = candidate(old_version or 0)
    if old_version is None:
        if path == 'config':
            before['parent'].pop('config')
        elif path == 'config.app':
            before['parent']['config'].pop('app')
        elif path == 'config.app.astrid':
            before['parent']['config']['app'].pop('astrid')
        else:
            before['parent']['config']['app']['astrid'].pop('v')
    else:
        ns = before['parent']['config']['app']['astrid']
        ns.update(authored='old', opaque={'null': None, 'empty': {}, 'array': [False, 0]},
                  _legacy={'opaque': 'retained collision'})
        before['parent']['config']['app']['other-app'] = dict(opaque=True)
    before['base_parent_payload'] = copy.deepcopy(before['parent'])
    after = copy.deepcopy(before)
    namespace = after['parent'].setdefault('config', {}).setdefault('app', {}).setdefault('astrid', {})
    namespace['v'] = 2
    if not path.endswith('.v'):
        namespace['authored'] = 'new'
    after['base_parent_payload'] = copy.deepcopy(after['parent'])
    old = commands.state_at(commands._recording_states(Checkout(copy.deepcopy(before))), 'parent', path)
    key = 'parent::' + path
    recorded = dict(op='timeline.inverse', select=None, args=dict(writes=[], publication='prior'),
                    expect={key: old}, authorship={key: 'direct'}, bindings={}, created=[], resolved=[])
    return before, after, recorded


@pytest.mark.parametrize('path', ['config', 'config.app', 'config.app.astrid', 'config.app.astrid.v'])
@pytest.mark.parametrize('old_version', [None, 1, 2], ids=['absent', 'v1', 'v2'])
def test_container_inverse_records_current_representation_and_exact_guard(path, old_version):
    before, after, recorded = container_transition(path, old_version)
    untouched = copy.deepcopy((before, after, recorded))
    undo = inverse.inverse_command([recorded], before, after, 'accepted-transition')
    write, = undo['args']['writes']
    assert write['before'] == commands.state_at(commands._recording_states(Checkout(copy.deepcopy(after))), 'parent', path)
    if path.endswith('.v'):
        assert write['after'] == dict(state='present', value=2)
    restored, plan, _ = RuntimeCommitService._apply(after, [undo])
    assert version(restored) == 2
    expected = copy.deepcopy(before['parent'])
    expected.setdefault('config', {}).setdefault('app', {}).setdefault('astrid', {})['v'] = 2
    assert restored['parent'] == expected
    assert (before, after, recorded) == untouched
    if plan.changed:
        replayed, _, _ = RuntimeCommitService._apply(after, plan.calls)
        assert replayed == restored
        from astrid.sdk.timeline.rebase import rebase
        assert rebase(plan.calls, after).document == restored
        redo = inverse.inverse_command(plan.calls, after, restored, 'undo-transition')
        assert RuntimeCommitService._apply(restored, [redo])[0]['parent'] == after['parent']


def test_representation_projection_preserves_legacy_namespace_collisions():
    # Historical restoration can retain both loose and namespaced legacy keys.
    state = dict(state='present', value=dict(slots={'source': 'config'}, app=dict(
        slots={'source': 'app'}, other={'opaque': None}, astrid=dict(
            v=1, slots={'source': 'namespace'}, _legacy={'slots': {'older': True}}))))
    original = copy.deepcopy(state)
    projected = inverse._representation_state('config', state, 2)['value']
    ns = projected['app']['astrid']
    assert ns['v'] == 2 and ns['slots'] == {'source': 'namespace'}
    assert ns['_legacy'] == {'slots': {'older': True}, 'slots#2': {'source': 'config'}, 'slots#3': {'source': 'app'}}
    assert projected['app']['other'] == {'opaque': None}
    assert state == original


@pytest.mark.parametrize('document', ['before', 'after'])
@pytest.mark.parametrize('field', ['parent', 'base_parent_payload'])
@pytest.mark.parametrize('bad', [True, '2', None, 3], ids=['bool', 'string', 'null', 'unknown'])
def test_inverse_producer_rejects_invalid_representation_before_projection(document, field, bad):
    from astrid.core.timeline.compatibility import TimelineCompatibilityError

    before, after, recorded = container_transition('config.app', 1)
    target = before if document == 'before' else after
    target[field]['config']['app']['astrid']['v'] = bad
    original = copy.deepcopy((before, after))
    with pytest.raises(TimelineCompatibilityError, match='must be an integer|unknown Astrid feature version'):
        inverse.inverse_command([recorded], before, after, 'invalid')
    assert (before, after) == original


def test_inverse_producer_refuses_missing_capability_and_existing_downgrade(monkeypatch):
    from astrid.core.timeline import compatibility

    before, after, recorded = container_transition('config.app', 1)
    after['parent']['config']['app']['astrid']['bed'] = {}
    monkeypatch.setattr(compatibility, 'WRITER_CAPABILITIES', frozenset())
    with pytest.raises(compatibility.TimelineCompatibilityError, match='writer lacks adopted-music-bed'):
        inverse.inverse_command([recorded], before, after, 'no-capability')
    after['parent']['config']['app']['astrid'].pop('bed')
    after['parent']['config']['app']['astrid']['v'] = 1
    with pytest.raises(compatibility.TimelineCompatibilityError, match='below pinned head'):
        inverse.inverse_command([recorded], before, after, 'downgrade')


@pytest.mark.parametrize('damage', ['explicit-downgrade', 'stale', 'tampered-history', 'parent-metadata'])
def test_representation_inverse_preserves_refusals_atomically(damage):
    from astrid.sdk.timeline._common import TimelineEditError

    before, after, recorded = container_transition('config.app', 1)
    if damage == 'tampered-history':
        recorded['expect']['parent::config.app'] = dict(state='absent')
        with pytest.raises(commands.CommandError, match='does not reconstruct'):
            inverse.inverse_command([recorded], before, after, 'tampered')
        return
    undo = inverse.inverse_command([recorded], before, after, 'accepted-transition')
    if damage == 'explicit-downgrade':
        undo['args']['writes'][0]['after']['value']['astrid']['v'] = 1
    elif damage == 'parent-metadata':
        undo['args']['writes'][0]['after']['value']['astrid']['bed'] = {'forged': True}
    else:
        after['parent']['config']['app']['other-app']['new-edit'] = True
    original = copy.deepcopy(after)
    error = _Refusal if damage == 'explicit-downgrade' else TimelineEditError
    with pytest.raises(error, match='below pinned head|same-property|cannot author owner-scoped bed'):
        RuntimeCommitService._apply(after, [undo])
    assert after == original


def test_representation_inverse_rebases_disjoint_sibling_and_conflicts_inside_guard():
    from astrid.sdk.timeline.rebase import rebase

    before, after, recorded = container_transition('config.app', 1)
    undo = inverse.inverse_command([recorded], before, after, 'accepted-transition')
    _, plan, _ = RuntimeCommitService._apply(after, [undo])
    disjoint = copy.deepcopy(after)
    disjoint['parent']['config']['unrelated'] = {'later': [None, False]}
    result = rebase(plan.calls, disjoint)
    assert not result.conflicts
    assert result.document['parent']['config']['unrelated'] == {'later': [None, False]}
    overlapping = copy.deepcopy(after)
    overlapping['parent']['config']['app']['other-app']['later'] = True
    assert rebase(plan.calls, overlapping).conflicts


@pytest.mark.parametrize('current_version', [1, 2])
def test_inverse_retains_the_validated_current_version_without_promoting_it(current_version):
    before, after, recorded = container_transition('config.app')
    for field in ('parent', 'base_parent_payload'):
        after[field]['config']['app']['astrid']['v'] = current_version
    undo = inverse.inverse_command([recorded], before, after, 'current-representation')
    assert undo['args']['writes'][0]['after'] == dict(state='present', value=dict(astrid=dict(v=current_version)))
    restored, _, _ = RuntimeCommitService._apply(after, [undo])
    assert version(restored) == current_version
