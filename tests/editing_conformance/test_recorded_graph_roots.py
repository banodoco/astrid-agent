"""Root groundwork through the native executor; fixture ops are never public."""
from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError
from astrid.sdk.timeline import commands
from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, present
from astrid.sdk.timeline._recording import Recording
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.inverse import coalesce
from astrid.sdk.timeline.rebase import replay
from tests.sdk.test_timeline_commit_provider_document import document_fixture


@pytest.fixture
def root_ops(monkeypatch):
    def create(ctx, command):
        for root in command['args']['roots']:
            ctx.create_entity(**root)

    original = commands.operations
    monkeypatch.setattr(commands, 'operations', lambda: {
        **original(), 'test.create-roots': commands.Operation({
            'type': 'object', 'required': ['roots'], 'additionalProperties': False,
            'properties': {'roots': {'type': 'array'}},
        }, create, {}),
    })


def root_values():
    return [
        {'ref': 'shot:new-shot', 'kind': 'shot', 'owner': None, 'value': {
            'payload': {'name': 'New shot', 'items': [], 'unknown': {
                'pinnedShotGroups': None, 'clipType': 'shot', 'empty': {}, 'ordered': [None, False, 0]},
                'metadata': {}},
            'internal_timeline': {'tracks': [{'id': 'picture', 'kind': 'visual', 'unknown': None}],
                                  'clips': [{'id': 'title', 'track': 'picture', 'at': 0,
                                             'hold': 2, 'text': 'Hello', 'unknown': [None]}],
                                  'output': {'fps': {'numerator': 30000, 'denominator': 1001}},
                                  'unknown': {'null': None, 'empty': []}},
        }},
        {'ref': 'occurrence:new-occ', 'kind': 'occurrence', 'owner': 'parent', 'value': {
            'occurrence_id': 'new-occ', 'shot_id': 'new-shot', 'track': 'picture',
            'placement': {'start_ms': 20000, 'track': 'picture'}, 'duration_ms': 2000,
            'source_offset': 0, 'speed': 1, 'gain': 1,
            'provenance': {'group_migration': {'opaque': [None, {'clipType': 'shot'}]}},
        }},
    ]


def create_command(roots=None):
    roots = root_values() if roots is None else roots
    return {'schema_version': 1, 'op': 'test.create-roots', 'version': '1',
            'select': 'parent', 'args': {'roots': roots},
            'created': [root['ref'] for root in roots]}


def test_missing_roots_are_tagged_absent():
    state = State(bundle_from_document(document_fixture()))
    for ref in ('shot:missing', 'occurrence:missing'):
        assert state.entity(ref, optional=True) is None
        assert state.get(ref, []) == ABSENT
        assert state.get(ref, ['unknown']) == ABSENT


@pytest.mark.parametrize('scope', ['clip:title', 'track:picture', 'shot', 'source', 'registry'])
def test_missing_occurrence_scopes_have_absent_input_guards(scope):
    original = bundle_from_document(document_fixture())
    state = State(original)
    ref = 'occurrence:new-occ/' + scope
    assert state.entity(ref, optional=True) is None
    assert state.get(ref, []) == ABSENT
    assert state.get(ref, ['unknown']) == ABSENT
    with pytest.raises(CommandError):
        state.entity(ref)
    original['placements'].append(copy.deepcopy(original['placements'][0]))
    with pytest.raises(CommandError):
        state.get('occurrence:occ-a/' + scope, [])


@pytest.mark.parametrize('mode', ['read', 'selection', 'edit'])
def test_one_recipe_creates_roots_then_reads_and_edits_their_children(monkeypatch, mode):
    from tests.editing_conformance.test_recipe_m1_commands import inverse

    def create(ctx, command):
        for root in root_values():
            ctx.create_entity(**root)
        if mode == 'selection':
            ctx.select('collection.clips', 'occurrence:new-occ')
        ctx.read('occurrence:new-occ/clip:title', ['hold'])
        if mode == 'edit':
            ctx.write('occurrence:new-occ/clip:title', ['hold'], 3)

    registered = commands.operations()
    registered['test.create-roots'] = commands.Operation({'type': 'object'}, create, {})
    monkeypatch.setattr(commands, 'operations', lambda: registered)
    original = bundle_from_document(document_fixture())
    plan = commands.execute(original, [create_command()])
    for read in plan.commands[0]['reads']:
        assert State(original).matches(read['ref'], read['path'], read['state'])
    if mode == 'selection':
        assert plan.commands[0]['selections'][0]['members'] == []
    assert State(plan.bundle).get('occurrence:new-occ/clip:title', ['hold']) == present(3 if mode == 'edit' else 2)
    assert replay(plan, original).records == plan.records
    assert inverse(plan.bundle, plan.records).bundle == original


@pytest.fixture
def child_ops(root_ops, monkeypatch):
    def create(ctx, command):
        for root in root_values():
            ctx.create_entity(**root)
        ref = 'occurrence:new-occ/clip:title'
        ctx.select('collection.clips', 'occurrence:new-occ')
        ctx.read(ref, ['hold'])
        ctx.bind('child', ref)
        for value in (3, 3, 2, 3):
            ctx.write(ref, ['hold'], value)
            ctx.read(ref, ['hold'])

    def write(ctx, command):
        for value in command['args']['values']:
            ctx.write(command['select'], ['hold'], value)

    def write_root(ctx, command):
        owner = command['select'].split('/')[0]
        ref = 'shot:' + ctx.state.occurrence(owner)['shot_id']
        for value in command['args']['values']:
            shot = ctx.read(ref)
            shot['internal_timeline']['clips'][0]['hold'] = value
            ctx.write(ref, [], shot)

    registered = commands.operations()
    registered['test.create-roots'] = commands.Operation({'type': 'object'}, create, {})
    registered['test.child-write'] = commands.Operation({'type': 'object'}, write, {}, True)
    registered['test.root-write'] = commands.Operation({'type': 'object'}, write_root, {}, True)
    monkeypatch.setattr(commands, 'operations', lambda: registered)


def child_command(ref='occurrence:new-occ/clip:title', values=(3,)):
    return {'schema_version': 1, 'op': 'test.child-write', 'version': '1',
            'select': ref, 'args': {'values': list(values)}}


def test_created_root_child_stages_replay_inverse_redo_and_compiled_reload(child_ops):
    from tests.editing_conformance.test_recipe_m1_commands import inverse

    original = bundle_from_document(document_fixture())
    plan = commands.execute(original, [create_command()])
    records = copy.deepcopy(plan.records)
    assert replay(plan, original).records == records
    undone = inverse(plan.bundle, records)
    assert undone.bundle == original
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle
    assert replay(undone, plan.bundle, inverse_records=records).records == undone.records
    accepted = document_from_bundle(plan.bundle)
    reloaded = bundle_from_document(accepted)
    undone = inverse(reloaded, records)
    restored = document_from_bundle(undone.bundle, source_bundles=(original, reloaded))
    assert restored == document_fixture()
    redone = inverse(undone.bundle, undone.records)
    assert document_from_bundle(redone.bundle, source_bundles=(original, reloaded)) == accepted
    assert plan.records == records


@pytest.mark.parametrize('values', [(3,), (4, 3)])
def test_created_root_child_same_value_and_aba_publications_refuse(child_ops, values):
    from tests.editing_conformance.test_recipe_m1_commands import inverse

    original = bundle_from_document(document_fixture())
    plan = commands.execute(original, [create_command()])
    later = commands.execute(plan.bundle, [child_command(values=values)])
    assert later.bundle == plan.bundle
    before = copy.deepcopy(later.bundle)
    with pytest.raises(CommandError) as error:
        inverse(later.bundle, plan.records, later.records)
    assert error.value.kind == 'intervening_write'
    assert later.bundle == before


def test_created_shot_later_occurrence_dependency_refuses(root_ops):
    from tests.editing_conformance.test_recipe_m1_commands import inverse

    original = bundle_from_document(document_fixture())
    plan = commands.execute(original, [create_command()])
    root = copy.deepcopy(root_values()[1])
    root['ref'] = 'occurrence:dependent'
    root['value']['occurrence_id'] = 'dependent'
    later = commands.execute(plan.bundle, [create_command([root])])
    before = copy.deepcopy(later.bundle)
    with pytest.raises(CommandError) as error:
        inverse(later.bundle, plan.records, later.records)
    assert error.value.kind == 'surviving_dependents'
    assert later.bundle == before


def test_root_and_child_aliases_use_exact_authored_owner_not_shared_source(child_ops, monkeypatch):
    from tests.editing_conformance.test_recipe_m1_commands import inverse

    def edit(ctx, command):
        ref = command['select']
        shot = 'shot:' + ctx.state.occurrence(ref.split('/')[0])['shot_id']
        value = ctx.read(shot)
        value['internal_timeline']['clips'][0]['hold'] = 3
        ctx.write(shot, [], value)
        ctx.write(ref, ['hold'], 4)

    registered = commands.operations()
    registered['test.root-child'] = commands.Operation({'type': 'object'}, edit, {}, True)
    monkeypatch.setattr(commands, 'operations', lambda: registered)
    original = bundle_from_document(document_fixture())
    ref = 'occurrence:occ-a/clip:clip-a'
    sibling = 'occurrence:occ-b/clip:clip-a'
    assert State(original).occurrence('occurrence:occ-a')['shot_id'] != State(original).occurrence('occurrence:occ-b')['shot_id']
    command = {**child_command(ref), 'op': 'test.root-child', 'args': {}}
    plan = commands.execute(original, [command])
    later = commands.execute(plan.bundle, [child_command(sibling, (5,))])
    expected = commands.execute(original, [child_command(sibling, (5,))])
    undone = inverse(later.bundle, plan.records, later.records)
    assert undone.bundle == expected.bundle
    assert inverse(undone.bundle, undone.records).bundle == later.bundle
    assert replay(plan, expected.bundle, intervening_records=later.records).bundle == later.bundle
    collision = commands.execute(plan.bundle, [child_command(ref, (4,))])
    with pytest.raises(CommandError) as error:
        inverse(collision.bundle, plan.records, collision.records)
    assert error.value.kind == 'intervening_write'


@pytest.mark.parametrize('damage', ['missing-created', 'wrong-kind', 'wrong-owner', 'missing-owner',
                                   'changed-owner', 'duplicate-owner', 'missing-source'])
def test_created_child_inverse_requires_complete_exact_root_context(child_ops, damage):
    from tests.editing_conformance.test_recipe_m1_commands import inverse

    plan = commands.execute(bundle_from_document(document_fixture()), [create_command()])
    records = copy.deepcopy(plan.records)
    head = copy.deepcopy(plan.bundle)
    if damage == 'missing-created':
        records[0]['created'] = []
    elif damage in ('wrong-kind', 'wrong-owner'):
        records[0]['created'][0]['kind' if damage == 'wrong-kind' else 'owner'] = 'parent'
    elif damage == 'missing-owner':
        records[0]['writes'].pop(1)
    elif damage == 'changed-owner':
        records[0]['writes'][1]['after']['value']['shot_id'] = 'other-shot'
    elif damage == 'duplicate-owner':
        head['placements'].append(copy.deepcopy(head['placements'][-1]))
    else:
        head['shots']['new-shot'].pop('_created')
    before = copy.deepcopy(head)
    with pytest.raises(CommandError):
        inverse(head, records)
    assert head == before


def test_executor_creates_full_roots_replays_and_compiles_without_source_pins(root_ops):
    original = bundle_from_document(document_fixture())
    before = copy.deepcopy(original)
    roots = root_values()
    plan = commands.execute(original, [create_command()])
    assert original == before
    state = State(plan.bundle)
    for root in roots:
        assert state.get(root['ref'], []) == present(root['value'])
    assert state.get('occurrence:new-occ/shot', ['shot_id']) == present('new-shot')
    assert plan.bundle['source_mapping'] == before['source_mapping']
    record = plan.records[0]
    assert record['created'] == [{key: root[key] for key in ('ref', 'kind', 'owner')} for root in roots]
    assert record['reads'] == [{'ref': root['ref'], 'path': [], 'state': ABSENT} for root in roots]
    assert record['writes'] == [{'ref': root['ref'], 'path': [], 'before': ABSENT,
                                 'after': present(root['value'])} for root in roots]
    assert record['dependencies'] == []
    recaptured = replay(plan, before)
    assert recaptured.records == plan.records
    assert recaptured.bundle == plan.bundle
    document = document_from_bundle(plan.bundle)
    assert document_from_bundle(plan.bundle) == document
    assert document_from_bundle(bundle_from_document(document)) == document


def test_main_bundle_new_roots_compile_without_runtime_bookkeeping(root_ops):
    from astrid.core.timeline.authoring_bundle import compile_authoring_candidate
    from tests.editing_conformance.test_recipe_m1_commands import bundle

    original = bundle()
    before = copy.deepcopy(original)
    plan = commands.execute(original, [create_command()])
    publication = compile_authoring_candidate(plan.bundle).publication
    assert any(row['shot_id'] == 'new-shot' for row in publication['shot_revisions'])
    assert publication['parent_composition']['occurrences'][-1]['occurrence_id'] == 'new-occ'
    assert original == before
    roots = [dict(root_values()[0], ref='shot:chapter-shot')]
    source_id = next(iter(original['source_mapping']['shots'].values()))['shot_id']
    roots[0]['ref'] = 'shot:' + source_id
    with pytest.raises(CommandError, match='occupied'):
        commands.execute(original, [create_command(roots)])
    assert original == before


def test_root_replace_remove_and_existing_baseline_recovery_are_lossless(root_ops):
    original = bundle_from_document(document_fixture(shared=False))
    plan = commands.execute(original, [create_command()])
    state = State(plan.bundle)
    root = root_values()[0]
    changed = copy.deepcopy(root['value'])
    changed['payload']['unknown']['empty'] = None
    state.set(root['ref'], [], present(changed))
    assert state.get(root['ref'], []) == present(changed)
    state.set(root['ref'], [], present(root['value']))
    with pytest.raises(CommandError, match='surviving'):
        state.set(root['ref'], [], ABSENT)
    for ref in ('occurrence:new-occ', 'shot:new-shot'):
        state.set(ref, [], ABSENT)
        assert state.get(ref, []) == ABSENT
    assert plan.bundle == original
    occurrence = state.get('occurrence:occ-a', [])
    shot = state.get('shot:chapter-shot', [])
    state.set('occurrence:occ-a', [], ABSENT)
    state.set('shot:chapter-shot', [], ABSENT)
    state.set('shot:chapter-shot', [], shot)
    state.set('occurrence:occ-a', [], occurrence)
    assert plan.bundle == original
    assert document_from_bundle(plan.bundle) == document_fixture(shared=False)


@pytest.mark.parametrize('ref,kind,owner', [
    ('shot:bad/name', 'shot', None), ('shot:x::y', 'shot', None), ('shot:', 'shot', None),
    ('shot: ', 'shot', None), ('shot:new-shot', 'occurrence', 'parent'),
    ('shot:new-shot', 'shot', 'parent'), ('occurrence:new-occ', 'occurrence', None),
    ('parent/track:new', 'track', 'parent'), ('parent', 'shot', None),
])
def test_invalid_root_scope_refuses_before_mutation(ref, kind, owner):
    bundle = bundle_from_document(document_fixture())
    before = copy.deepcopy(bundle)
    ctx = Recording(bundle, {}, 0)
    with pytest.raises(CommandError):
        ctx.create_entity(ref, kind=kind, owner=owner, value=root_values()[0]['value'])
    assert bundle == before
    assert ctx.record['created'] == ctx.record['reads'] == ctx.record['writes'] == []


@pytest.mark.parametrize('damage', ['duplicate', 'bad-occurrence', 'occupied', 'virtual-slot', 'source-shot'])
def test_batch_refusal_is_atomic(root_ops, damage):
    bundle = bundle_from_document(document_fixture())
    before = copy.deepcopy(bundle)
    roots = root_values()
    if damage == 'duplicate':
        roots.append(copy.deepcopy(roots[0]))
    elif damage == 'bad-occurrence':
        roots[1]['value']['shot_id'] = 'missing'
    elif damage == 'occupied':
        roots[1]['ref'] = 'occurrence:occ-a'
        roots[1]['value']['occurrence_id'] = 'occ-a'
    elif damage == 'virtual-slot':
        roots[0]['ref'] = 'shot:' + bundle['placements'][0]['shot_id']
    else:
        roots[0]['ref'] = 'shot:chapter-shot'
        assert roots[0]['ref'][5:] not in bundle['shots']  # Opening shared shots only made slots.
    with pytest.raises(CommandError):
        commands.execute(bundle, [create_command(roots)])
    assert bundle == before


def test_root_envelopes_and_leaf_writes_cannot_edit_compiler_bookkeeping():
    bundle = bundle_from_document(document_fixture())
    before = copy.deepcopy(bundle)
    ctx = Recording(bundle, {}, 0)
    for value in (None, {'payload': {}, 'internal_timeline': None},
                  {**root_values()[0]['value'], 'base_payload': {}},
                  {**root_values()[0]['value'], 'source_mapping': {}}):
        with pytest.raises(CommandError):
            ctx.create_entity('shot:new-shot', kind='shot', owner=None, value=value)
    with pytest.raises(CommandError):
        State(bundle).set('shot:' + bundle['placements'][0]['shot_id'], ['payload'], present({}))
    assert bundle == before
    assert ctx.record['writes'] == []


def test_unrecorded_or_orphan_shot_cannot_be_compiled(root_ops):
    original = bundle_from_document(document_fixture())
    ctx = Recording(original, {}, 0)
    ctx.write('shot:new-shot', [], root_values()[0]['value'])
    assert ctx.record['created'] == []
    with pytest.raises(AuthoringBundleError, match='source_mapping'):
        document_from_bundle(original)
    original = bundle_from_document(document_fixture())
    plan = commands.execute(original, [create_command(root_values()[:1])])
    with pytest.raises(AuthoringBundleError, match='require an occurrence'):
        document_from_bundle(plan.bundle)


def inverse_command():
    return {'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
            'args': {'publications': ['trusted-records-fixture']}}


def accepted_input_boundary(records, before, after):
    """Model the provider's accepted records plus its captured input boundary."""
    entry = {'records': {'commands': records,
                         'undo_of': inverse_command()['args']['publications']}}
    return AcceptedRecords([entry], records, lambda _: (before, after))


def test_creation_inverse_restores_original_root_absence(root_ops):
    original = bundle_from_document(document_fixture())
    plan = commands.execute(original, [create_command()])
    undone = commands.execute(plan.bundle, [inverse_command()], inverse_records=plan.records)
    assert undone.bundle == original
    assert document_from_bundle(undone.bundle) == document_fixture()


def test_creation_inverse_after_compiled_reload_restores_original_absence(root_ops):
    original = bundle_from_document(document_fixture())
    plan = commands.execute(original, [create_command()])
    accepted_document = document_from_bundle(plan.bundle)
    reloaded = bundle_from_document(accepted_document)
    accepted = accepted_input_boundary(plan.records, original, reloaded)
    undone = commands.execute(reloaded, [inverse_command()], inverse_records=accepted)
    restored = document_from_bundle(undone.bundle, source_bundles=(original, reloaded))
    assert restored == document_fixture()


def test_record_only_inverse_restores_authored_closure_but_not_parent_pin(root_ops):
    original = bundle_from_document(document_fixture())
    plan = commands.execute(original, [create_command()])
    reloaded = bundle_from_document(document_from_bundle(plan.bundle))
    undone = commands.execute(reloaded, [inverse_command()], inverse_records=plan.records)
    restored = document_from_bundle(undone.bundle)
    expected = document_fixture()

    assert restored['parent_revision']['payload'] == expected['parent_revision']['payload']
    assert restored['parent_revision']['content_digest'] == expected['parent_revision']['content_digest']
    assert restored['shot_revisions'] == expected['shot_revisions']
    assert restored['internal_timeline_revisions'] == expected['internal_timeline_revisions']
    assert restored['dependency_manifest'] == expected['dependency_manifest']
    assert restored['parent_revision']['revision_id'] != expected['parent_revision']['revision_id']


def test_record_only_inverse_cannot_distinguish_original_parent_revision_names(root_ops):
    first = document_fixture()
    second = copy.deepcopy(first)
    second['parent_revision']['revision_id'] = 'different-original-parent'
    plans = [commands.execute(bundle_from_document(source), [create_command()])
             for source in (first, second)]
    accepted_documents = [document_from_bundle(plan.bundle) for plan in plans]

    assert plans[0].records == plans[1].records
    assert accepted_documents[0] == accepted_documents[1]

    restored = []
    for document in accepted_documents:
        reloaded = bundle_from_document(document)
        undone = commands.execute(reloaded, [inverse_command()],
                                  inverse_records=plans[0].records)
        restored.append(document_from_bundle(undone.bundle))

    assert restored[0] == restored[1]
    assert restored[0]['parent_revision']['revision_id'] not in {
        first['parent_revision']['revision_id'], second['parent_revision']['revision_id']}


def test_creation_inverse_redo_reuses_exact_roots(root_ops):
    original = bundle_from_document(document_fixture())
    plan = commands.execute(original, [create_command()])
    undone = commands.execute(plan.bundle, [inverse_command()], inverse_records=plan.records)
    redone = commands.execute(undone.bundle, [inverse_command()], inverse_records=undone.records)
    assert redone.bundle == plan.bundle
    assert document_from_bundle(redone.bundle) == document_from_bundle(plan.bundle)


def test_fixture_root_ops_stay_private_and_public_create_args_stay_closed():
    for op in ('test.create-roots', 'shot.create'):
        with pytest.raises(CommandError, match='unsupported operation'):
            commands.validate_command({**create_command(), 'op': op})
    with pytest.raises(CommandError):
        commands.validate_command({**create_command(), 'op': 'occurrence.create'})
    with pytest.raises(CommandError):
        commands.validate_command({'schema_version': 1, 'op': 'track.create', 'version': '1',
                                   'select': 'parent', 'args': {'row': {'id': 'v', 'kind': 'visual'},
                                                             'payload': {}}})


def test_generic_net_zero_root_edges_inverse_redo_preserve_absence_and_created_facts(root_ops, monkeypatch):
    def remove(ctx, command):
        for root in reversed(root_values()):
            ctx.clear(root['ref'], [])

    registered = commands.operations
    monkeypatch.setattr(commands, 'operations', lambda: {
        **registered(), 'test.remove-roots': commands.Operation({'type': 'object'}, remove, {}),
    })
    original = bundle_from_document(document_fixture())
    remove_command = {'schema_version': 1, 'op': 'test.remove-roots', 'version': '1',
                      'select': 'parent', 'args': {}}
    plan = commands.execute(original, [create_command(), remove_command])
    records = copy.deepcopy(plan.records)
    assert document_from_bundle(plan.bundle) == document_fixture()
    unchanged = [{'ref': root['ref'], 'path': [], 'before': ABSENT, 'after': ABSENT}
                 for root in root_values()]
    sequential = [dict(write, after=present(root['value']))
                  for write, root in zip(unchanged, root_values())]
    sequential += [dict(write, before=write['after'], after=ABSENT)
                   for write in reversed(sequential)]
    created = [{key: root[key] for key in ('ref', 'kind', 'owner')} for root in root_values()]
    assert plan.records[0]['writes'] == sequential[:2]
    assert plan.records[1]['writes'] == sequential[2:]
    assert plan.records[1]['created'] == []
    undone = commands.execute(plan.bundle, [inverse_command()], inverse_records=plan.records)
    # C0 requires child-before-owner restoration, then owner-before-child
    # removal, even when the boundary projection has no reachable new roots.
    assert undone.records[0]['writes'] == sequential
    assert coalesce(undone.records) == unchanged
    assert undone.records[0]['created'] == created
    assert document_from_bundle(undone.bundle) == document_fixture()
    redone = commands.execute(undone.bundle, [inverse_command()], inverse_records=undone.records)
    assert redone.records[0]['writes'] == sequential
    assert coalesce(redone.records) == unchanged
    assert redone.records[0]['created'] == created
    assert document_from_bundle(redone.bundle) == document_fixture()
    for result in (plan, undone, redone):
        for root in root_values():
            assert State(result.bundle).get(root['ref'], []) == ABSENT
            assert {'ref': root['ref'], 'path': [], 'state': ABSENT} in result.records[0]['reads']
        for index in range(2):
            damaged = copy.deepcopy(result.records)
            value = damaged[0]['writes'][index]['after']['value']
            if index == 0:
                value['payload']['name'] = 'Changed intermediate shot'
            else:
                value['gain'] = 0
            head = copy.deepcopy(result.bundle)
            before = copy.deepcopy(head)
            with pytest.raises(CommandError) as error:
                commands.execute(head, [inverse_command()], inverse_records=damaged)
            assert error.value.kind == 'inverse_conflict'
            assert head == before
    assert plan.records == records
    assert plan.records[0]['created'] == created
