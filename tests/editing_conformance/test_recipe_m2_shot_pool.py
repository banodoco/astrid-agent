"""shot.pool: ordered pool membership on the shot, exact capture, inverse, rebase and refusals."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commands import execute as _execute
from astrid.sdk.timeline.commands import operations, validate_command
from astrid.sdk.timeline.commit import _digest, bundle_from_document, document_from_bundle
from astrid.sdk.timeline.native_auto_placement import auto_place_shared, unplace_shared
from astrid.sdk.timeline.native_shot_pool_commands import POOL, capture_shot_pool
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.fixture_loader import load_fixture
from tests.editing_conformance.test_recipe_m1_commands import inverse as _inverse
from tests.editing_conformance.test_recipe_m1_commands import later
from tests.editing_conformance.test_recipe_occurrence_media import inputs
from tests.sdk.test_timeline_commit_provider import (
    inverse as inverse_call,
)

execute = _execute
inverse = _inverse

OWNER = 'occurrence:occ-a'
SOURCE = OWNER + '/source'
FIXTURE = Path(__file__).parent / 'fixtures' / 'recipe_m2_shot_pool.json'
SCHEMA = Path(__file__).parents[2] / 'astrid/core/timeline/schema/edit-command.schema.json'


class GenuineTimeline:
    """A current Runtime commitTimeline caller for the pool recipes."""

    def __init__(self, service, project, authorize, document, pins, verified):
        self.service = service
        self.project = project
        self.authorize = authorize
        self.document = document
        self.pins = pins
        self.verified = verified
        self.calls = 0

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

        names = ('old', 'new', 'pool-b', 'pool-c')

        def effect(project):
            result = _effect(project, groups=[{'group_key': 'main', 'selectors': [
                {'selector': name, 'ordinal': index, 'variant_key': name, 'output_port': kind}
                for index, name in enumerate(names)]}])
            result['payload']['metadata']['params'] = {'duration_seconds': 6} if kind == 'video' else {}
            return result

        service, project, task, attempt, admitted = _fixture(tmp_path, effect_factory=effect)
        project = project['id']
        credentials = CredentialStore(tmp_path / 'credentials')
        token, _ = credentials.provision('pool-editor', ['projects:read', 'projects:write'])

        def authorize():
            return credentials.require(token, 'projects:write')

        try:
            outputs = [_output(name.encode(), output_port=kind, group_key='main', variant_key=name,
                               ordinal=index, filename=name + ('.mp4' if kind == 'video' else '.png'),
                               media_type=kind + ('/mp4' if kind == 'video' else '/png'))
                       for index, name in enumerate(names)]
            _settle(service, attempt, outputs, effect=admitted)
            associations = service.managed_outputs(task['task']['id'])
            selected = {}
            aliases = {'old': 'picture', 'new': 'reserved-new', 'pool-b': 'pool-b', 'pool-c': 'pool-c'}
            for association in associations:
                canonical = service.get_project_media_selection(project, {
                    'association_id': association['association_id'],
                    'object_id': association['object_id'],
                    'version': association['lifecycle']['version'],
                }, authorize=authorize)
                alias = aliases[association['variant_key']]
                selected[alias] = service.get_project_media_selection(project, {
                    'asset': alias, 'generation_id': canonical['generation_id'],
                    'variant_id': canonical['variant_id'],
                }, authorize=authorize)
            old = selected['picture']
            verified = [service.get_project_verified_media(project, selected[name], authorize=authorize)
                        for name in ('picture', 'reserved-new', 'pool-b', 'pool-c')]
            pins = [verified[index]['selection'] for index in range(1, 4)]
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
            for image in [document['parent_revision'], *document['shot_revisions'],
                          *document['internal_timeline_revisions']]:
                image['content_digest'] = _digest(image['payload'])
            document['dependency_manifest']['media'] = [
                {'media_id': old['object_id'], 'content_digest': old['object_id']}]
            for payload in (document['parent_revision']['payload'],
                            document['internal_timeline_revisions'][0]['payload']):
                binding = payload.get('registry', {}).get('assets', {}).get('picture')
                if binding is not None:
                    binding.update(media_id=old['object_id'], generationId=old['generation_id'],
                                   variantId=old['variant_id'])
            service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
            service.create_timeline(project, 'film', idempotency_key='pool-bootstrap')
            _create_fixture(service, project, document, old, authorize=authorize)
            document = cls._document(service, project, 'film')
            return cls(service, project, authorize, document, pins, verified)
        except Exception:
            service.close()
            raise

    @staticmethod
    def _document(service, project, timeline):
        head = service.get_project_timeline(project, timeline)
        parent = service.get_project_parent_composition_revision(project, timeline, head['head_revision_id'])
        occurrences = parent['payload'].get('occurrences', [])
        shots, internals = {}, {}
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
        steps = [{'id': str(index), 'label': label, 'intent': 'M2 shot pool', 'tags': ['shot.pool'],
                  'command_ordinals': [index],
                  'patch': copy.deepcopy(command.get('args', {}).get('changes', [])), 'reads': []}
                 for index, command in enumerate(commands)]
        return {'api_version': 1, 'project_id': self.project, 'timeline_id': 'film',
                'base_revision_id': head['head_revision_id'],
                'base_publication_id': head['head_publication_id'], 'commands': commands,
                'label': label, 'steps': steps, 'merge': 'atomic', 'reads': [], 'dry_run': False,
                'source': {'via': 'python'}, 'rationale': 'M2 shot pool recipe', 'group_id': label}

    def call(self, commands, label='shot-pool'):
        self.calls += 1
        result = self.service.commit_timeline(
            self.project, 'film', self.body(commands, label),
            idempotency_key=label + '-' + str(self.calls), publisher='pool-editor',
            invocation_path=f'/v1/projects/{self.project}/timelines/film/commit',
            authorize=self.authorize)
        self.document = result['data']['canonical_document']
        return result

    def seed_pooled(self, *, full=False, order=None, set_duration=True):
        from astrid.sdk.timeline._command_state import present

        bundle = bundle_from_document(self.document)
        if set_duration:
            occurrence = State(bundle).occurrence(OWNER)
            duration = occurrence['duration_ms']
            duration_command = {'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
                                'args': {'changes': [{'op': 'set', 'ref': OWNER,
                                                      'path': ['duration_ms'],
                                                      'before': present(duration), 'value': 20000}]}}
            self.call([duration_command], 'pool-duration')
        if order is None:
            order = (1, 0, 2) if full else ()
        for index in order:
            pin = self.pins[index]
            bundle = bundle_from_document(self.document)
            command = capture_shot_pool(bundle, OWNER, 'add', pin, ownership='occurrence',
                                        verified_media=self.verified)
            self.call([command], 'pool-seed-' + str(index))
        return bundle_from_document(self.document)

    def accepted_boundaries(self, records, before, after, commands):
        from astrid.sdk.timeline._collections import AcceptedRecords
        from astrid.sdk.timeline._commit_patch import inverse_address_resolver

        created = self.call(commands, 'pool-redo-source')
        if records and records[0]['command']['op'] == 'timeline.inverse':
            self.call([inverse_call([created['receipt']['receipt_id']])], 'pool-redo-source-undo')
        item = self.service.list_timeline_commits(
            self.project, 'film', limit=100, authorize=self.authorize)['items'][-1]
        history = {entry['publication_id']: entry for entry in self.service.list_timeline_commits(
            self.project, 'film', limit=100, authorize=self.authorize)['items']}

        def boundary(entry):
            previous = history[entry['previous']['publication_id']]
            return (bundle_from_document(previous['receipt']['result']['canonical_document']),
                    bundle_from_document(entry['receipt']['result']['canonical_document']))

        accepted = AcceptedRecords([item], item['records']['commands'], boundary)
        accepted.address_resolver = inverse_address_resolver(
            [item], after, load_boundary=boundary, load_history=history.__getitem__)
        return accepted

    def close(self):
        self.service.close()


@pytest.fixture
def genuine_runtime(request, tmp_path, monkeypatch):
    parameters = getattr(request.node, 'callspec', None)
    values = parameters.params if parameters is not None else {}
    kind = values.get('kind', 'image')
    context = GenuineTimeline.create(tmp_path, monkeypatch, kind)
    try:
        yield context
    finally:
        context.close()


def pins(kind='image'):
    document, old, new, verified = inputs(kind)
    others = [{'asset': 'pool-' + name, 'generation_id': 'generation-' + name, 'variant_id': 'variant-' + name,
               'object_id': 'sha256:' + digit * 64, 'catalog_digest': 'sha256:' + digit * 64}
              for name, digit in (('b', '3'), ('c', '4'))]
    verified += [{'selection': copy.deepcopy(pin), 'media_type': 'image/fixture', 'size': 123} for pin in others]
    return document, old, [new, *others], verified


def step(bundle, action, pin, verified):
    command = capture_shot_pool(bundle, OWNER, action, pin, ownership='occurrence', verified_media=verified)
    return command, execute(bundle, [command], verified_media=verified)


def pool(bundle):
    return State(bundle).config(OWNER)['app']['astrid']['pool']


def build(name, context=None):
    if context is None:
        document, _, [a, b, c], verified = pins()
    else:
        if name == 'add-second':
            context.seed_pooled(order=(0,), set_duration=False)
        elif name == 'remove-middle':
            context.seed_pooled(order=(0, 1, 2), set_duration=False)
        document = copy.deepcopy(context.document)
        a, b, c = copy.deepcopy(context.pins)
        verified = copy.deepcopy(context.verified)
    bundle = bundle_from_document(document)
    if name == 'add-first':
        return document, bundle, 'add', a, verified
    if context is None:
        for pin in (a, b, c)[:3 if name == 'remove-middle' else 1]:
            bundle = step(bundle, 'add', pin, verified)[1].bundle
    if name == 'add-second':
        return document, bundle, 'add', b, verified
    return document, bundle, 'remove', b, verified


CASES = ['add-first', 'add-second', 'remove-middle']


@pytest.mark.parametrize('name', CASES)
def test_portable_goldens(name):
    _, bundle, action, pin, verified = build(name)
    command, plan = step(bundle, action, pin, verified)
    assert load_fixture(FIXTURE)[name] == {
        'commands': [command], 'records': plan.records, 'pool': pool(plan.bundle)}


@pytest.mark.parametrize('name', CASES)
@pytest.mark.integration
def test_exact_capture_replay_inverse_and_redo(name, genuine_runtime):
    _, bundle, action, pin, verified = build(name, genuine_runtime)
    before = copy.deepcopy(bundle)
    command, plan = step(bundle, action, pin, verified)
    validate_command(command)
    assert json.loads(json.dumps(command, allow_nan=False)) == command
    assert bundle == before
    assert plan.commands == [command]
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == before
    redone = inverse(undone.bundle, genuine_runtime.accepted_boundaries(
        undone.records, plan.bundle, undone.bundle, [command]))
    assert redone.bundle == plan.bundle


def test_add_appends_in_order_and_binds_the_exact_media_without_touching_the_rest():
    document, bundle, _, pin, verified = build('add-second')
    plan = execute(bundle, [step(bundle, 'add', pin, verified)[0]], verified_media=verified)
    assert pool(plan.bundle) == ['reserved-new', 'pool-b']
    binding = State(plan.bundle).entity(OWNER + '/registry')['assets']['pool-b']
    assert (binding['generationId'], binding['variantId']) == ('generation-b', 'variant-b')
    assert plan.bundle['parent'] == bundle['parent']
    assert State(plan.bundle).collection(OWNER, 'clip') == State(bundle).collection(OWNER, 'clip')
    assert State(plan.bundle).config('occurrence:occ-b') == State(bundle).config('occurrence:occ-b')
    assert 'sha256:' + '3' * 64 in {row['media_id'] for row in
                                     document_from_bundle(plan.bundle)['dependency_manifest']['media']}


def test_remove_keeps_the_binding_and_undo_restores_the_original_position():
    _, bundle, _, pin, verified = build('remove-middle')
    assert pool(bundle) == ['reserved-new', 'pool-b', 'pool-c']
    _, plan = step(bundle, 'remove', pin, verified)
    assert pool(plan.bundle) == ['reserved-new', 'pool-c']
    assert 'pool-b' in State(plan.bundle).entity(OWNER + '/registry')['assets']
    assert pool(inverse(plan.bundle, plan.records).bundle) == ['reserved-new', 'pool-b', 'pool-c']


def test_absent_and_empty_pools_are_distinct_and_removing_the_last_member_keeps_an_empty_list():
    document, _, [a, *_], verified = pins()
    bundle = bundle_from_document(document)
    assert 'pool' not in (State(bundle).config(OWNER).get('app') or {}).get('astrid', {})
    added = step(bundle, 'add', a, verified)[1]
    emptied = step(added.bundle, 'remove', a, verified)[1]
    assert pool(emptied.bundle) == []
    assert inverse(emptied.bundle, emptied.records).bundle == added.bundle
    assert inverse(added.bundle, added.records).bundle == bundle


def test_the_first_add_keeps_existing_siblings_of_the_astrid_namespace():
    document, _, [a, *_], verified = pins()
    bundle = bundle_from_document(document)
    State(bundle).config(OWNER)['app'] = {'theme': 1, 'astrid': {'keep': [1]}}
    before = copy.deepcopy(bundle)
    plan = step(bundle, 'add', a, verified)[1]
    assert State(plan.bundle).config(OWNER)['app'] == {'theme': 1, 'astrid': {'keep': [1], 'pool': ['reserved-new']}}
    assert inverse(plan.bundle, plan.records).bundle == before


def test_legacy_group_pool_ids_in_provenance_are_never_rewritten():
    document, _, [a, *_], verified = pins()
    bundle = bundle_from_document(document)
    legacy = {'group_migration': {'legacy_group': {'shotId': 'g', 'poolGenerationIds': ['generation-new']}}}
    bundle['placements'][0]['provenance'] = copy.deepcopy(legacy)
    plan = step(bundle, 'add', a, verified)[1]
    assert plan.bundle['placements'][0]['provenance'] == legacy
    assert all(write['ref'] != OWNER for record in plan.records for write in record['writes'])


@pytest.mark.parametrize('damage', ['duplicate', 'not-a-member', 'placed', 'unverified', 'shared', 'bad-pool',
                                    'bad-list', 'bad-app'])
def test_refusals_preserve_input(damage):
    document, old, [a, b, _], verified = pins()
    bundle = bundle_from_document(document)
    action, pin, kind = 'add', a, None
    if damage == 'duplicate':
        bundle = step(bundle, 'add', a, verified)[1].bundle
        kind = 'identity_collision'
    elif damage == 'not-a-member':
        action, kind = 'remove', 'identity_conflict'
        bundle = step(bundle, 'add', b, verified)[1].bundle
    elif damage == 'placed':
        pin, kind = old, 'identity_conflict'
    elif damage == 'unverified':
        verified, kind = verified[:1], 'dependency_changed'
    elif damage == 'shared':
        bundle['placements'][1]['shot_id'] = bundle['placements'][0]['shot_id']
        kind = 'unsupported_ownership'
    elif damage == 'bad-pool':
        State(bundle).config(OWNER)['app'] = {'astrid': {'pool': 'reserved-new'}}
        kind = 'unsupported_pool'
    elif damage == 'bad-list':
        State(bundle).config(OWNER)['app'] = {'astrid': {'pool': ['pool-b', 3]}}
        kind = 'unsupported_pool'
    else:
        State(bundle).config(OWNER)['app'] = ['not', 'an', 'object']
    before = copy.deepcopy(bundle)
    command = {'schema_version': 1, 'op': 'shot.pool', 'version': '1', 'select': OWNER,
               'args': {'action': action, 'media': copy.deepcopy(pin)}, 'intent': {'ownership': 'occurrence'}}
    with pytest.raises(CommandError) as error:
        execute(bundle, [command], verified_media=verified)
    assert kind is None or error.value.kind == kind
    assert bundle == before


def test_args_are_closed_and_the_selector_must_be_an_explicit_occurrence():
    _, bundle, action, pin, verified = build('add-first')
    command = capture_shot_pool(bundle, OWNER, action, pin, ownership='occurrence', verified_media=verified)
    plain = {key: command[key] for key in ('schema_version', 'op', 'version', 'select', 'args', 'intent')}
    validator = Draft202012Validator(json.loads(SCHEMA.read_text()))
    assert not list(validator.iter_errors(plain))
    for changes in ({'args': {**plain['args'], 'extra': 1}}, {'args': {'action': 'add'}},
                    {'args': {**plain['args'], 'action': 'toggle'}}, {'select': 'parent'},
                    {'select': OWNER + '/clip:clip-a'}, {'version': '2'}, {'intent': {'ownership': 'shared'}}):
        assert list(validator.iter_errors({**plain, **changes}))
        with pytest.raises(CommandError):
            validate_command({**plain, **changes})
    for owner, ownership in [('parent', 'occurrence'), (OWNER + '::display', 'occurrence'), (OWNER, 'shared')]:
        with pytest.raises(CommandError) as error:
            capture_shot_pool(bundle, owner, action, pin, ownership=ownership, verified_media=verified)
        assert error.value.kind == 'unsupported_ownership'


def test_schema_registration_is_additive_and_complete():
    schema = json.loads(SCHEMA.read_text())
    constrained = {branch['if']['properties']['op']['const'] for branch in schema['$defs']['EditCommand']['allOf']}
    assert 'shot.pool' in constrained
    assert {'placement.create', 'placement.unplace', 'occurrence.add-media', 'clip.delete'} <= constrained
    assert 'shot.pool' in operations()
    assert operations()['shot.pool'].parameters == schema['$defs']['ShotPoolArgs']


def test_undo_refuses_after_any_intervening_write_to_the_pool_and_replay_refuses_a_changed_pool():
    _, bundle, action, pin, verified = build('add-second')
    _, plan = step(bundle, action, pin, verified)
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, [later(SOURCE, POOL, ['reserved-new', 'pool-b'])])
    head = copy.deepcopy(bundle)
    State(head).config(OWNER)['app']['astrid']['pool'].append('pool-c')
    before = copy.deepcopy(head)
    with pytest.raises(CommandError):
        replay(plan, head, verified_media=verified)
    assert head == before


def test_disjoint_edits_rebase_and_retain_the_original_capture():
    _, bundle, action, pin, verified = build('add-second')
    command, plan = step(bundle, action, pin, verified)
    head = copy.deepcopy(bundle)
    State(head).entity('occurrence:occ-b/clip:clip-a')['at'] = 3
    head['parent']['name'] = 'Someone else renamed this'
    State(head).config('occurrence:occ-b')['app'] = {'astrid': {'pool': ['pool-c']}}
    rebased = replay(plan, head, verified_media=verified)
    assert rebased.commands == [command]
    assert pool(rebased.bundle) == ['reserved-new', 'pool-b']
    assert State(rebased.bundle).config('occurrence:occ-b')['app']['astrid']['pool'] == ['pool-c']
    assert inverse(rebased.bundle, rebased.records).bundle == head


@pytest.mark.integration
def test_provider_accepts_add_and_remove_with_exact_undo_and_redo(genuine_runtime):
    document = copy.deepcopy(genuine_runtime.document)
    a = copy.deepcopy(genuine_runtime.pins[0])
    verified = copy.deepcopy(genuine_runtime.verified)
    realm = genuine_runtime
    added = realm.call([capture_shot_pool(bundle_from_document(document), OWNER, 'add', a,
                                          ownership='occurrence', verified_media=verified)], 'pool-add')
    after = added['data']['canonical_document']
    assert pool(bundle_from_document(after)) == ['reserved-new']
    removal = capture_shot_pool(bundle_from_document(after), OWNER, 'remove', a, ownership='occurrence',
                                verified_media=verified)
    removed = realm.call([removal], 'pool-remove')
    assert pool(bundle_from_document(removed['data']['canonical_document'])) == []
    undo = realm.call([inverse_call([removed['receipt']['receipt_id']])], 'pool-remove-undo')
    assert undo['data']['canonical_document'] == after
    redo = realm.call([inverse_call([undo['receipt']['receipt_id']])], 'pool-remove-redo')
    assert redo['data']['canonical_document'] == removed['data']['canonical_document']


def pooled_case(context=None, *, full=False):
    if context is None:
        document, _, [a, *_], verified = pins()
        bundle = bundle_from_document(document)
        State(bundle).occurrence(OWNER)['duration_ms'] = 20000
        result = auto_place_shared(bundle, OWNER, 'placed', a, ownership='occurrence',
                                   verified_media=verified, pooled=True)
        bundle = execute(bundle, result['commands'], verified_media=verified).bundle
    else:
        context.seed_pooled(full=full)
        document = copy.deepcopy(context.document)
        bundle = bundle_from_document(document)
        a = copy.deepcopy(context.pins[0])
        verified = copy.deepcopy(context.verified)
        if not full:
            result = auto_place_shared(bundle, OWNER, 'placed', a, ownership='occurrence',
                                       verified_media=verified, pooled=True)
            context.call(result['commands'], 'pool-primary')
            document = copy.deepcopy(context.document)
            bundle = bundle_from_document(document)
    return document, bundle, a, verified


def test_a_null_frame_pools_without_a_clip_and_refuses_a_frame_or_anchor():
    document, pooled, a, verified = pooled_case()
    assert pool(pooled) == ['reserved-new']
    assert State(pooled).collection(OWNER, 'clip') == State(bundle_from_document(document)).collection(OWNER, 'clip')
    for extra in ({'frame': 3}, {'anchor': 'clip-a'}):
        with pytest.raises(CommandError) as error:
            auto_place_shared(pooled, OWNER, 'placed', a, ownership='occurrence', verified_media=verified,
                              pooled=True, **extra)
        assert error.value.kind == 'unsupported_timing'


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.integration
def test_placing_pooled_media_promotes_it_in_one_batch_that_undoes_as_one(kind, genuine_runtime):
    document, pooled, a, verified = pooled_case(genuine_runtime, full=True)
    assert pool(pooled) == ['pool-b', 'reserved-new', 'pool-c']
    before = copy.deepcopy(pooled)
    result = auto_place_shared(pooled, 'occurrence:occ-a', 'placed', a, ownership='occurrence',
                               verified_media=verified, frame=None)
    assert [command['op'] for command in result['commands']] == [
        'placement.create' if kind == 'image' else 'occurrence.add-media', 'shot.pool']
    assert result['capture']['promoted'] is True
    plan = execute(pooled, result['commands'], verified_media=verified)
    assert pooled == before
    assert pool(plan.bundle) == ['pool-b', 'pool-c']
    assert 'placed' in {row['id'] for row in State(plan.bundle).collection(OWNER, 'clip')}
    assert replay(plan, pooled, verified_media=verified).records == plan.records
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == before
    redone = inverse(undone.bundle, genuine_runtime.accepted_boundaries(
        undone.records, plan.bundle, undone.bundle, result['commands']))
    assert redone.bundle == plan.bundle


@pytest.mark.integration
def test_promotion_and_keep_as_pooled_round_trip_through_the_provider(genuine_runtime):
    _, pooled, a, verified = pooled_case(genuine_runtime)
    realm = genuine_runtime
    promoted = realm.call(auto_place_shared(pooled, OWNER, 'placed', a, ownership='occurrence',
                                            verified_media=verified)['commands'], 'promotion')
    placed = bundle_from_document(promoted['data']['canonical_document'])
    assert pool(placed) == []
    kept = realm.call(unplace_shared(placed, OWNER, 'placed', ownership='occurrence', keep_as_pooled=True,
                                     media=a, verified_media=verified)['commands'], 'keep-pooled')
    assert pool(bundle_from_document(kept['data']['canonical_document'])) == ['reserved-new']
    undo = realm.call([inverse_call([kept['receipt']['receipt_id']])], 'keep-pooled-undo')
    assert undo['data']['canonical_document'] == promoted['data']['canonical_document']


def test_keep_as_pooled_needs_the_exact_media_of_the_placed_clip():
    _, pooled, a, verified = pooled_case()
    placed = execute(pooled, auto_place_shared(pooled, OWNER, 'placed', a, ownership='occurrence',
                                               verified_media=verified)['commands'], verified_media=verified).bundle
    for media in (None, verified[2]['selection']):
        with pytest.raises(CommandError) as error:
            unplace_shared(placed, OWNER, 'placed', ownership='occurrence', keep_as_pooled=True, media=media,
                           verified_media=verified)
        assert error.value.kind == 'identity_conflict'
