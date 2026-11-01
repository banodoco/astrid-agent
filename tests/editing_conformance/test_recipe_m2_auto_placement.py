"""Completion auto-placement: anchor-after and lane-end fallback over existing birth ops."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commands import execute, validate_command
from astrid.sdk.timeline.commit import _digest, bundle_from_document, document_from_bundle
from astrid.sdk.timeline.native_auto_placement import auto_place_completion
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import inverse
from tests.editing_conformance.test_recipe_occurrence_media import inputs
from tests.sdk.test_timeline_commit_provider import (
    inverse as inverse_call,
)

OWNER = 'occurrence:occ-a'
CHILD = OWNER + '/clip:placed'
FIXTURE = Path(__file__).parent / 'fixtures' / 'recipe_m2_auto_placement.json'
# occ-a starts at 8 s at 30000/1001 fps. clip-a ends at local 3 s (global frame 330);
# clip-b ends at local 7 s (global frame 450), which only fits a 5 s item in a wide occurrence.
ANCHOR_FRAME, LANE_END_FRAME = 330, 450
CASES = {'image-anchor': ('image', 'clip-a'), 'image-absent': ('image', 'gone'),
         'video-anchor': ('video', 'clip-a'), 'video-absent': ('video', None)}


class GenuineTimeline:
    """A current Runtime commitTimeline caller for the placement recipes."""

    def __init__(self, service, project, authorize, document, old, new, verified):
        self.service = service
        self.project = project
        self.authorize = authorize
        self.document = document
        self.old = old
        self.new = new
        self.verified = verified

    @classmethod
    def create(cls, tmp_path, monkeypatch, kind):
        from tests.helpers.runtime import use_runtime

        runtime = use_runtime()
        monkeypatch.syspath_prepend(str(runtime / 'tests'))
        from runtime_protocol.auth import CredentialStore
        from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
        from test_generation_publish_v1_settlement import _effect, _fixture, _output, _settle

        from tests.editing_conformance.test_commit_runtime_occurrence_media import _create_fixture
        from tests.sdk.test_timeline_commit_provider_document import MEDIA

        def effect(project):
            result = _effect(project, groups=[{'group_key': 'main', 'selectors': [
                {'selector': name, 'ordinal': index, 'variant_key': name, 'output_port': kind}
                for index, name in enumerate(('old', 'new'))]}])
            result['payload']['metadata']['params'] = {'duration_seconds': 6} if kind == 'video' else {}
            return result

        service, project, task, attempt, admitted = _fixture(tmp_path, effect_factory=effect)
        project = project['id']
        credentials = CredentialStore(tmp_path / 'credentials')
        token, _ = credentials.provision('placement-editor', ['projects:read', 'projects:write'])

        def authorize():
            return credentials.require(token, 'projects:write')

        try:
            outputs = [_output(name.encode(), output_port=kind, group_key='main', variant_key=name,
                               ordinal=index, filename=name + ('.mp4' if kind == 'video' else '.png'),
                               media_type=kind + ('/mp4' if kind == 'video' else '/png'))
                       for index, name in enumerate(('old', 'new'))]
            _settle(service, attempt, outputs, effect=admitted)
            associations = service.managed_outputs(task['task']['id'])
            selections = [service.get_project_media_selection(project, {
                'association_id': association['association_id'], 'object_id': association['object_id'],
                'version': association['lifecycle']['version']}, authorize=authorize)
                          for association in associations]
            old, new = selections
            verified = [service.get_project_verified_media(project, selection, authorize=authorize)
                        for selection in selections]
            document, _, _, _ = inputs(kind)

            def replace(value):
                if value == MEDIA:
                    return old['object_id']
                if isinstance(value, dict):
                    return {key: replace(child) for key, child in value.items()}
                if isinstance(value, list):
                    return [replace(child) for child in value]
                return value

            document = replace(document)
            document.update(project_id=project, timeline_id='film')
            document['internal_timeline_revisions'][0]['timeline_id'] = 'film'
            document['dependency_manifest']['media'] = [
                {'media_id': old['object_id'], 'content_digest': old['object_id']}]
            for payload in (document['parent_revision']['payload'],
                            document['internal_timeline_revisions'][0]['payload']):
                assets = payload['registry']['assets']
                binding = assets.pop('picture')
                binding.update(media_id=old['object_id'], generationId=old['generation_id'],
                               variantId=old['variant_id'])
                assets[old['asset']] = binding
                for rows in (payload.get('clips', []), payload.get('config', {}).get('clips', [])):
                    for row in rows:
                        if row.get('asset') == 'picture':
                            row['asset'] = old['asset']
            for image in [document['parent_revision'], *document['shot_revisions'],
                          *document['internal_timeline_revisions']]:
                image['content_digest'] = _digest(image['payload'])
            service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
            service.create_timeline(project, 'film', idempotency_key='placement-bootstrap')
            _create_fixture(service, project, document, old, authorize=authorize)
            document = cls._document(service, project, 'film')
            return cls(service, project, authorize, document, old, new, verified)
        except Exception:
            service.close()
            raise

    @staticmethod
    def _document(service, project, timeline):
        head = service.get_project_timeline(project, timeline)
        parent = service.get_project_parent_composition_revision(project, timeline, head['head_revision_id'])
        occurrences = parent['payload'].get('occurrences', [])
        shots = {}
        internals = {}
        for occurrence in occurrences:
            shot = service.get_project_shot_revision(project, occurrence['shot_id'],
                                                     occurrence['shot_revision_id'])
            shots[(shot['shot_id'], shot['revision_id'])] = shot
            internal = service.get_project_timeline_revision(
                project, timeline, shot['internal_timeline_revision_id'])
            internals[(internal['timeline_id'], internal['revision_id'])] = internal
        return {'schema_version': 1, 'project_id': project, 'timeline_id': timeline,
                'parent_revision': parent, 'shot_revisions': list(shots.values()),
                'internal_timeline_revisions': list(internals.values()),
                'dependency_manifest': parent['dependency_manifest']}

    def body(self, commands, label):
        head = self.service.get_project_timeline(self.project, 'film')
        steps = [{'id': str(index), 'label': label, 'intent': 'M2 placement', 'tags': ['placement'],
                  'command_ordinals': [index],
                  'patch': copy.deepcopy(command.get('args', {}).get('changes', [])), 'reads': []}
                 for index, command in enumerate(commands)]
        return {'api_version': 1, 'project_id': self.project, 'timeline_id': 'film',
                'base_revision_id': head['head_revision_id'],
                'base_publication_id': head['head_publication_id'], 'commands': commands,
                'label': label, 'steps': steps, 'merge': 'atomic', 'reads': [], 'dry_run': False,
                'source': {'via': 'python'}, 'rationale': 'M2 placement recipe', 'group_id': label}

    def call(self, commands, label='placement'):
        return self.service.commit_timeline(
            self.project, 'film', self.body(commands, label), idempotency_key=label,
            publisher='placement-editor',
            invocation_path=f'/v1/projects/{self.project}/timelines/film/commit',
            authorize=self.authorize)

    def prepare_fixture(self, *, wide=False, overlay=False, shared=False):
        from astrid.sdk.timeline.emitter import emit_document_commands

        before = bundle_from_document(self.document)
        after = copy.deepcopy(before)
        state = State(after)
        if wide:
            state.occurrence(OWNER)['duration_ms'] = 20000
        if overlay or shared:
            tracks = state.config(OWNER)['tracks']
            if not any(track['id'] == 'overlay' for track in tracks):
                tracks.append({'id': 'overlay', 'kind': 'visual'})
        if shared and not any(row['id'] == 'clip-c' for row in state.config(OWNER)['clips']):
            state.config(OWNER)['clips'].append(
                {'id': 'clip-c', 'track': 'overlay', 'asset': 'picture', 'clipType': 'media',
                 'at': 1, 'hold': 8})
        commands = emit_document_commands(before, after)
        if commands:
            self.call(commands, label='placement-input')
            self.document = self._document(self.service, self.project, 'film')

    def accepted_boundaries(self, records, before, after, forward_commands):
        from astrid.sdk.timeline._collections import AcceptedRecords
        from astrid.sdk.timeline._commit_patch import inverse_address_resolver

        accepted = self.call(copy.deepcopy(forward_commands), label='placement-boundary-forward')
        undone = self.call(
            [inverse_call([accepted['receipt']['receipt_id']])],
            label='placement-boundary-undo')
        items = self.service.list_timeline_commits(
            self.project, 'film', limit=100, authorize=self.authorize)['items']
        history = {item['publication_id']: item for item in items}
        item = next(row for row in items
                    if row['receipt']['receipt_id'] == undone['receipt']['receipt_id'])

        def boundary(entry):
            previous = history[entry['previous']['publication_id']]
            return (bundle_from_document(previous['receipt']['result']['canonical_document']),
                    bundle_from_document(entry['receipt']['result']['canonical_document']))

        result = AcceptedRecords([item], item['records']['commands'], boundary)
        result.address_resolver = inverse_address_resolver(
            [item], after, load_boundary=boundary, load_history=history.__getitem__)
        return result

    def close(self):
        self.service.close()


@pytest.fixture
def genuine_runtime(request, tmp_path, monkeypatch):
    parameter = getattr(request.node, 'callspec', None)
    values = parameter.params if parameter is not None else {}
    name = values.get('name')
    kind = values.get('kind') or (CASES[name][0] if name in CASES else 'image')
    context = GenuineTimeline.create(tmp_path, monkeypatch, kind)
    try:
        yield context
    finally:
        context.close()


def build(kind, anchor, *, duration=None, wide=False, overlay=False, context=None, **saved):
    if context is None:
        document, _, new, verified = inputs(kind)
    else:
        context.prepare_fixture(wide=wide, overlay=overlay)
        document, new, verified = context.document, context.new, context.verified
    bundle = bundle_from_document(document)
    if wide:
        State(bundle).occurrence(OWNER)['duration_ms'] = 20000
    if overlay:
        tracks = State(bundle).config(OWNER)['tracks']
        if not any(track['id'] == 'overlay' for track in tracks):
            tracks.append({'id': 'overlay', 'kind': 'visual'})
    result = auto_place_completion(bundle, OWNER, 'placed', new, ownership='occurrence',
                                   verified_media=verified, anchor=anchor, duration=duration, **saved)
    return document, bundle, result, verified


def case(name, context=None, **kwargs):
    kind, anchor = CASES[name]
    document, bundle, result, verified = build(kind, anchor, wide=anchor in ('gone', None),
                                               context=context, **kwargs)
    return document, bundle, result, verified


@pytest.mark.parametrize('name', CASES)
def test_portable_goldens(name):
    _, bundle, result, verified = case(name)
    plan = execute(bundle, result['commands'], verified_media=verified)
    child = State(plan.bundle).entity(CHILD)
    golden = json.loads(FIXTURE.read_text())[name]
    assert golden == {'commands': result['commands'], 'capture': result['capture'], 'child': child}


@pytest.mark.parametrize('name', CASES)
@pytest.mark.integration
def test_exact_capture_replay_inverse_and_redo(name, genuine_runtime):
    _, bundle, result, verified = case(name, genuine_runtime)
    before = copy.deepcopy(bundle)
    [command] = result['commands']
    validate_command(command)
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    plan = execute(bundle, [command], verified_media=verified)
    assert bundle == before
    assert plan.commands == [command]
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == before
    redone = inverse(undone.bundle, genuine_runtime.accepted_boundaries(
        undone.records, plan.bundle, undone.bundle, plan.commands))
    assert redone.bundle == plan.bundle


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_anchor_after_starts_at_the_anchor_end_with_the_completion_default(kind):
    _, bundle, result, verified = build(kind, 'clip-a')
    capture = result['capture']
    assert (capture['resolution'], capture['start_frame']) == ('anchor-after', ANCHOR_FRAME)
    assert capture['anchor'] == {'id': 'clip-a', 'state': 'present'}
    child = State(execute(bundle, result['commands'], verified_media=verified).bundle).entity(CHILD)
    assert child['at'] == pytest.approx(ANCHOR_FRAME * 1001 / 30000 - 8)
    assert (child['clipType'], child['hold'], child['track']) == (kind, 5, 'picture')
    if kind == 'video':
        assert (child['from'], child['to'], child['speed']) == (0, 5, 1)


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('anchor', ['gone', None])
def test_absent_anchor_appends_at_the_lane_end_and_is_recorded(kind, anchor):
    _, bundle, result, verified = build(kind, anchor, wide=True)
    capture = result['capture']
    assert (capture['resolution'], capture['start_frame']) == ('lane-end', LANE_END_FRAME)
    assert capture['anchor']['state'] == 'absent'
    child = State(execute(bundle, result['commands'], verified_media=verified).bundle).entity(CHILD)
    assert child['at'] == pytest.approx(LANE_END_FRAME * 1001 / 30000 - 8)


def test_absent_anchor_on_an_empty_lane_starts_at_the_occurrence_start():
    _, bundle, _, verified = build('image', None, wide=True)
    State(bundle).config(OWNER)['clips'].clear()
    pin = verified[1]['selection']
    result = auto_place_completion(bundle, OWNER, 'placed', pin, ownership='occurrence',
                                   verified_media=verified)
    assert result['capture']['start_frame'] == 240  # round(8 s * 30000/1001)
    assert State(execute(bundle, result['commands'], verified_media=verified).bundle).entity(CHILD)['at'] == (
        pytest.approx(240 * 1001 / 30000 - 8))


def test_video_duration_is_supplied_or_defaults_and_never_exceeds_the_verified_media():
    _, bundle, result, verified = build('video', 'clip-a', duration=3)
    child = State(execute(bundle, result['commands'], verified_media=verified).bundle).entity(CHILD)
    assert (child['hold'], child['from'], child['to']) == (3, 0, 3)
    with pytest.raises(CommandError):
        build('video', 'clip-a', duration=7)
    # The image hold is the completion default whatever the caller says.
    assert build('image', 'clip-a', duration=3)[2]['capture']['seconds'] == 5


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_the_exact_completed_variant_is_bound_and_siblings_are_untouched(kind):
    _, bundle, result, verified = build(kind, 'clip-a')
    plan = execute(bundle, result['commands'], verified_media=verified)
    binding = State(plan.bundle).entity(OWNER + '/registry')['assets']['reserved-new']
    assert (binding['generationId'], binding['variantId']) == ('generation-new', 'variant-new')
    assert State(plan.bundle).entity(OWNER + '/registry')['assets']['picture'] == (
        State(bundle).entity(OWNER + '/registry')['assets']['picture'])
    assert plan.bundle['parent'] == bundle['parent']
    assert State(plan.bundle).collection('occurrence:occ-b', 'clip') == State(bundle).collection(
        'occurrence:occ-b', 'clip')


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_shared_parent_and_unbound_owners_are_refused_without_touching_input(kind):
    _, bundle, _, verified = build(kind, 'clip-a')
    pin = verified[1]['selection']
    shared = copy.deepcopy(bundle)
    shared['placements'][1]['shot_id'] = shared['placements'][0]['shot_id']
    for owner, ownership, head in [(OWNER, 'occurrence', shared), ('parent', 'occurrence', bundle),
                                   (OWNER, 'shared', bundle), (OWNER, 'parent', bundle)]:
        before = copy.deepcopy(head)
        with pytest.raises(CommandError) as error:
            auto_place_completion(head, owner, 'placed', pin, ownership=ownership, verified_media=verified)
        assert error.value.kind == 'unsupported_ownership'
        assert head == before


def test_unverified_or_unsupported_media_is_refused():
    _, bundle, _, verified = build('image', 'clip-a')
    pin = verified[1]['selection']
    with pytest.raises(CommandError):
        auto_place_completion(bundle, OWNER, 'placed', pin, ownership='occurrence', verified_media=[])
    audio = copy.deepcopy(verified)
    audio[1]['media_type'] = 'audio/fixture'
    with pytest.raises(CommandError) as error:
        auto_place_completion(bundle, OWNER, 'placed', pin, ownership='occurrence', verified_media=audio)
    assert error.value.kind == 'unsupported_media'


def test_an_anchor_with_unusable_timing_refuses_instead_of_falling_back():
    _, bundle, _, verified = build('image', 'clip-a')
    del State(bundle).entity(OWNER + '/clip:clip-a')['at']
    with pytest.raises(CommandError):
        auto_place_completion(bundle, OWNER, 'placed', verified[1]['selection'], ownership='occurrence',
                              verified_media=verified, anchor='clip-a')


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_replay_conflicts_when_the_anchor_moved_meanwhile_and_a_rebuild_follows_it(kind):
    # Was: replay kept the recorded start after the anchor moved (330 vs a fresh 360).
    _, bundle, result, verified = build(kind, 'clip-a')
    assert result['capture']['consumed'] == ['clip-a']
    plan = execute(bundle, result['commands'], verified_media=verified)
    head = copy.deepcopy(bundle)
    State(head).entity(OWNER + '/clip:clip-a')['at'] = 2
    with pytest.raises(CommandError) as error:
        replay(plan, head, verified_media=verified)
    assert error.value.kind == 'dependency_changed'
    moved = auto_place_completion(head, OWNER, 'again', verified[1]['selection'], ownership='occurrence',
                                  verified_media=verified, anchor='clip-a')
    assert moved['capture']['start_frame'] == 360
    # An exact retry on the unchanged head is the same request and the same result.
    assert replay(plan, bundle, verified_media=verified).records == plan.records


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_replay_conflicts_when_the_lane_end_moved_meanwhile_and_a_rebuild_follows_it(kind):
    # Was: replay landed at the stale lane end (450 vs a fresh 480).
    _, bundle, result, verified = build(kind, None, wide=True)
    assert result['capture']['start_frame'] == LANE_END_FRAME
    plan = execute(bundle, result['commands'], verified_media=verified)
    head = copy.deepcopy(bundle)
    State(head).entity(OWNER + '/clip:clip-b')['hold'] += 1
    with pytest.raises(CommandError) as error:
        replay(plan, head, verified_media=verified)
    assert error.value.kind == 'dependency_changed'
    rebuilt = auto_place_completion(head, OWNER, 'placed', verified[1]['selection'], ownership='occurrence',
                                    verified_media=verified)
    assert rebuilt['capture']['start_frame'] > LANE_END_FRAME


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_a_clip_moving_onto_the_lane_conflicts_with_the_recorded_lane_end(kind):
    _, bundle, result, verified = build(kind, None, wide=True, overlay=True)
    plan = execute(bundle, result['commands'], verified_media=verified)
    head = copy.deepcopy(bundle)
    State(head).entity(OWNER + '/clip:clip-a')['track'] = 'overlay'
    with pytest.raises(CommandError):
        replay(plan, head, verified_media=verified)


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_the_saved_fallback_time_and_track_win_and_stay_fixed_when_siblings_move(kind):
    _, bundle, result, verified = build(kind, 'gone', wide=True, overlay=True, fallback_at=22.25,
                                        fallback_track='overlay')
    capture = result['capture']
    assert (capture['resolution'], capture['lane'], capture['consumed']) == ('fallback-at', 'overlay', [])
    assert capture['start_frame'] == round(22.25 * 30000 / 1001)
    plan = execute(bundle, result['commands'], verified_media=verified)
    child = State(plan.bundle).entity(CHILD)
    assert (child['track'], child['hold']) == ('overlay', 5)
    assert child['at'] == pytest.approx(capture['start_frame'] * 1001 / 30000 - 8)
    # Nothing was consumed from the lane, so a sibling moving does not change the approved intent.
    head = copy.deepcopy(bundle)
    State(head).entity(OWNER + '/clip:clip-b')['hold'] += 1
    assert State(replay(plan, head, verified_media=verified).bundle).entity(CHILD)['at'] == child['at']


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_the_saved_preferred_track_wins_with_a_present_anchor(kind):
    _, bundle, result, verified = build(kind, 'clip-a', overlay=True, preferred_track='overlay',
                                        fallback_at=22.25, fallback_track='picture')
    assert (result['capture']['resolution'], result['capture']['start_frame']) == ('anchor-after', ANCHOR_FRAME)
    child = State(execute(bundle, result['commands'], verified_media=verified).bundle).entity(CHILD)
    assert child['track'] == 'overlay'


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_a_deleted_saved_track_refuses_instead_of_recomputing_another_lane(kind):
    for anchor, saved in [('clip-a', {'preferred_track': 'gone-lane'}),
                          ('gone', {'fallback_track': 'gone-lane', 'fallback_at': 22.25})]:
        with pytest.raises(CommandError) as error:
            build(kind, anchor, wide=True, **saved)
        assert error.value.kind == 'dependency_changed'


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_a_birth_cannot_appear_through_both_occurrences_after_a_retarget(kind):
    # Was: add-media (every video, and an image off the home lane) had no replay-time owner check.
    for saved in ({}, {'preferred_track': 'overlay'}):
        _, bundle, result, verified = build(kind, 'clip-a', overlay=True, **saved)
        plan = execute(bundle, result['commands'], verified_media=verified)
        head = copy.deepcopy(bundle)
        head['placements'][1]['shot_id'] = head['placements'][0]['shot_id']
        with pytest.raises(CommandError) as error:
            replay(plan, head, verified_media=verified)
        # The recorded placement.owners selection catches the retarget before the handler re-runs.
        assert error.value.kind == 'selection_changed'


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_replay_refuses_when_the_anchor_was_deleted_meanwhile_and_a_rebuild_falls_back(kind):
    _, bundle, result, verified = build(kind, 'clip-a')
    plan = execute(bundle, result['commands'], verified_media=verified)
    head = copy.deepcopy(bundle)
    rows = State(head).config(OWNER)['clips']
    rows[:] = [row for row in rows if row['id'] != 'clip-a']
    with pytest.raises(CommandError):
        replay(plan, head, verified_media=verified)
    State(head).occurrence(OWNER)['duration_ms'] = 20000
    rebuilt = auto_place_completion(head, OWNER, 'placed', verified[1]['selection'], ownership='occurrence',
                                    verified_media=verified, anchor='clip-a')
    assert (rebuilt['capture']['resolution'], rebuilt['capture']['anchor']['state']) == ('lane-end', 'absent')


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.integration
def test_provider_accepts_completion_placement_with_exact_undo_and_redo(kind, genuine_runtime):
    document, _, result, verified = build(kind, 'clip-a', context=genuine_runtime)
    realm = genuine_runtime
    created = realm.call(result['commands'], 'completion-placement')
    undo = realm.call([inverse_call([created['receipt']['receipt_id']])], 'completion-placement-undo')
    assert undo['data']['canonical_document'] == document
    redo = realm.call([inverse_call([undo['receipt']['receipt_id']])], 'completion-placement-redo')
    assert redo['data']['canonical_document'] == created['data']['canonical_document']
    assert document_from_bundle(bundle_from_document(created['data']['canonical_document'])) == (
        created['data']['canonical_document'])
