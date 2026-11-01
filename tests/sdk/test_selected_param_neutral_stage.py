"""Selected authority survives neutral native stages, never a different binding."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline._selected_param_authority import resolve_selected_param_source
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import create_runtime_commit_service
from tests.sdk import test_timeline_selected_param_authority as selected
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedPort
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_selected_schema_runtime import retained_pack as retained_pack
from tests.sdk.test_timeline_selected_schema_runtime import (
    selected_runtime_env as selected_runtime_env,
)

configured_project_pack = selected.configured_project_pack
two_pins = selected.two_pins
REF = selected.REF


def _sequence(element, options):
    return [
        {'schema_version': 1, 'op': 'clip.set-element', 'version': '1', 'select': REF,
         'args': {'elementRef': element, 'schema_source': options['schema_source']}},
        {'schema_version': 1, 'op': 'clip.set', 'version': '1', 'select': REF,
         'args': {'values': {'hold': 3}, 'clear': []}},
        selected._params(options, {'seed': 8}),
    ]


def _context(realm, calls, v2):
    context = realm.context(calls)
    context['realm_id'] = 'realm'
    if v2:
        context['request'].update(label='Select, hold and PARAM', merge='fields', reads=[],
            steps=[{'id': 'element', 'label': 'Update element', 'intent': '', 'tags': [],
                    'command_ordinals': list(range(len(calls))), 'patch': [], 'reads': []}])
    return context


@pytest.mark.parametrize('v2', [False, True])
def test_reselection_hold_param_preserves_exact_new_source(two_pins, v2):
    element, p1, p2 = two_pins
    realm = selected._realm(two_pins)
    selected._birth(realm, element)
    selected._reselect(realm, element, p1)
    before = copy.deepcopy(State(selected.current(realm)).entity(REF))
    calls = _sequence(element, p2)
    captured = execute(selected.current(realm), calls,
                       schema_inputs=p2['schema_inputs'], realm_id='realm').commands
    context = _context(realm, captured, v2)
    accepted = create_runtime_commit_service().commit(context, DetachedPort(realm, context))
    assert accepted['data']['commands'] == captured
    assert State(selected.current(realm)).entity(REF) == {
        **before, 'hold': 3, 'params': {'seed': 8}}
    authority = resolve_selected_param_source(DetachedPort(realm, context), context=context,
                                             through=realm.head, select=REF)
    assert authority == next(dep['pin'] for dep in realm.entries[-1]['records']['commands'][-1][
        'dependencies'] if dep['key'] == 'params.snapshot')
    assert authority['source'] == p2['schema_source']
    assert authority['element'] == element
    with pytest.raises(CommitRejected, match='source changed'):
        selected._commit(realm, [selected._params(p1, {'seed': 9})])


@pytest.mark.parametrize('damage', ['selection', 'source', 'owner', 'binding', 'history'])
def test_neutral_stage_cannot_repair_invalid_authority(two_pins, damage):
    element, p1, p2 = two_pins
    realm = selected._realm(two_pins)
    selected._birth(realm, element)
    selected._reselect(realm, element, p1)
    calls = _sequence(element, p2)[:2]
    if damage == 'selection':
        calls[0]['select'] = 'parent/clip:other'
    elif damage == 'source':
        calls[0]['args']['schema_source'] = p1['schema_source']
    elif damage == 'owner':
        calls[0]['args']['elementRef'] = {**element, 'packId': 'foreign'}
    elif damage == 'binding':
        calls[1]['bindings'] = {'scalar.target': 'parent/clip:other'}
    else:
        realm.entries[-1]['previous'] = copy.deepcopy(realm.entries[0]['previous'])
    calls.append(selected._params(p2, {'seed': 8}))
    before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted))
    with pytest.raises(CommitRejected):
        selected._commit(realm, calls)
    assert (realm.head, realm.entries, realm.documents, realm.accepted) == before


def test_hold_without_selection_does_not_create_authority(two_pins):
    element, p1, _ = two_pins
    realm = selected._realm(two_pins)
    selected._birth(realm, element)
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommitRejected, match='authority is unavailable'):
        selected._commit(realm, _sequence(element, p1)[1:])
    assert (realm.head, realm.entries, realm.documents) == before


def test_neutral_hold_keeps_accepted_authority(two_pins):
    element, p1, _ = two_pins
    realm = selected._realm(two_pins)
    selected._birth(realm, element)
    selected._reselect(realm, element, p1)
    hold = capture_clip_set(selected.current(realm), REF, values={'hold': 3})
    selected._commit(realm, [hold, selected._params(p1, {'seed': 8})])
    assert State(selected.current(realm)).entity(REF)['hold'] == 3


@pytest.mark.parametrize('path', [
    ['elementRef'], ['params'], ['id'], ['clipType'], ['asset'], ['source'], [],
])
def test_set_record_with_non_neutral_effect_invalidates_authority(two_pins, path):
    element, p1, _ = two_pins
    realm = selected._realm(two_pins)
    selected._birth(realm, element)
    selected._reselect(realm, element, p1)
    plan = execute(selected.current(realm), [capture_clip_set(
        selected.current(realm), REF, values={'hold': 3})])
    record = copy.deepcopy(plan.records[0])
    record['writes'][0]['path'] = path
    context = realm.context([])
    with pytest.raises(CommandError, match='authority is unavailable'):
        resolve_selected_param_source(DetachedPort(realm, context), context=context,
                                      through=realm.head, select=REF, staged=[record])


@pytest.mark.parametrize('damage', ['ref', 'policy', 'dependencies', 'duplicate', 'unknown'])
def test_v2_staged_association_keeps_strict_semantic_binding(two_pins, damage):
    element, _, p2 = two_pins
    realm = selected._realm(two_pins)
    selected._birth(realm, element)
    calls = _sequence(element, p2)[:2]
    context = _context(realm, calls, True)
    plan = execute(selected.current(realm), calls, schema_inputs=p2['schema_inputs'],
                   realm_id='realm', commit_request=context['request'], evaluated_head=realm.head)
    binding = next(item for item in plan.records[0]['bindings'] if item['key'] == 'params.element')
    assert binding['physical_ref'] == 'parent'
    assert binding['boundary']['command_ordinal'] == 0
    assert binding['path'] == ['config', 'clips', 'id:effect']
    port = DetachedPort(realm, context)
    authority = resolve_selected_param_source(port, context=context, through=realm.head,
                                             select=REF, staged=plan.records)
    assert authority['source'] == p2['schema_source']
    if damage == 'duplicate':
        plan.records[0]['bindings'].append(copy.deepcopy(binding))
    elif damage == 'unknown':
        binding['unauthenticated'] = True
    else:
        binding[damage] = {'ref': 'parent/clip:other', 'policy': 'floating',
                           'dependencies': ['params.schema']}[damage]
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommandError, match='authority is unavailable'):
        resolve_selected_param_source(port, context=context, through=realm.head,
                                      select=REF, staged=plan.records)
    assert (realm.head, realm.entries, realm.documents) == before


PHYSICAL_DAMAGE = [
    'physical_ref', 'owners', 'revision_id', 'content_digest', 'path',
    'base_revision_id', 'base_publication_id', 'evaluated_head', 'step_id',
    'command_ordinal', 'phase', 'boundary_extra', 'boundary_missing',
    'missing_image', 'extra_image', 'stripped_image',
]


def _damage_binding(record, damage):
    binding = next(item for item in record['bindings'] if item['key'] == 'params.element')
    if damage == 'physical_ref':
        binding.update(physical_ref='internal-revision:unrelated', owners=['occurrence:other/shot'])
    elif damage == 'owners':
        binding['owners'] = ['occurrence:other/shot']
    elif damage in ('revision_id', 'content_digest'):
        binding[damage] = 'f' * 64
    elif damage == 'path':
        binding['path'] = ['config', 'clips', 'id:other']
    elif damage == 'boundary_extra':
        binding['boundary']['publication_id'] = 'unrelated'
    elif damage == 'boundary_missing':
        binding['boundary'].pop('base_publication_id')
    elif damage == 'missing_image':
        binding.pop('content_digest')
    elif damage == 'extra_image':
        binding['image'] = 'unrelated'
    elif damage == 'stripped_image':
        for key in ('physical_ref', 'revision_id', 'content_digest', 'boundary', 'path'):
            binding.pop(key)
    else:
        binding['boundary'][damage] = {
            'base_revision_id': 'f' * 64, 'base_publication_id': 'unrelated',
            'evaluated_head': {'revision_id': 'f' * 64, 'publication_id': 'unrelated'},
            'step_id': 'unrelated', 'command_ordinal': 1, 'phase': 'output',
        }[damage]


@pytest.mark.parametrize('damage', PHYSICAL_DAMAGE)
@pytest.mark.parametrize('accepted', [False, True])
def test_physical_binding_requires_authenticated_source_context(two_pins, damage, accepted):
    element, _, p2 = two_pins
    realm = selected._realm(two_pins)
    selected._birth(realm, element)
    calls = _sequence(element, p2)[:2]
    context = _context(realm, calls, True)
    port = DetachedPort(realm, context)
    if accepted:
        create_runtime_commit_service().commit(context, port)
        staged = ()
        record = realm.entries[-1]['records']['commands'][0]
    else:
        plan = execute(selected.current(realm), calls, schema_inputs=p2['schema_inputs'],
                       realm_id='realm', commit_request=context['request'], evaluated_head=realm.head)
        staged = plan.records
        record = staged[0]
    assert resolve_selected_param_source(port, context=context, through=realm.head,
                                         select=REF, staged=staged)['source'] == p2['schema_source']
    _damage_binding(record, damage)
    before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted))
    with pytest.raises(CommandError, match='authority is unavailable'):
        resolve_selected_param_source(port, context=context, through=realm.head,
                                      select=REF, staged=staged)
    assert (realm.head, realm.entries, realm.documents, realm.accepted) == before


@pytest.mark.parametrize('damage', PHYSICAL_DAMAGE)
def test_provider_param_checks_physical_custody_at_real_stage(two_pins, monkeypatch, damage):
    from astrid.sdk.timeline import _selected_param_authority as authority

    element, _, p2 = two_pins
    realm = selected._realm(two_pins)
    selected._birth(realm, element)
    calls = _sequence(element, p2)
    context = _context(realm, calls, True)
    resolve = authority.resolve_selected_param_source
    reached = []

    def corrupt(port, **kwargs):
        assert [record['command']['op'] for record in kwargs['staged']] == [
            'clip.set-element', 'clip.set']
        reached.append(True)
        kwargs['staged'] = copy.deepcopy(kwargs['staged'])
        _damage_binding(kwargs['staged'][0], damage)
        return resolve(port, **kwargs)

    monkeypatch.setattr(authority, 'resolve_selected_param_source', corrupt)
    before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted))
    with pytest.raises(CommitRejected, match='authority is unavailable'):
        create_runtime_commit_service().commit(context, DetachedPort(realm, context))
    assert reached == [True]
    assert (realm.head, realm.entries, realm.documents, realm.accepted) == before


def test_changed_staged_revision_is_verified_at_its_own_input(two_pins):
    element, _, p2 = two_pins
    realm = selected._realm(two_pins)
    selected._birth(realm, element)
    sequence = _sequence(element, p2)
    calls = [sequence[1], sequence[0], sequence[2]]
    context = _context(realm, calls, True)
    plan = execute(selected.current(realm), calls, schema_inputs=p2['schema_inputs'],
                   realm_id='realm', commit_request=context['request'], evaluated_head=realm.head)
    binding = next(item for item in plan.records[1]['bindings'] if item['key'] == 'params.element')
    assert binding['revision_id'] != realm.head['revision_id']
    assert binding['boundary']['command_ordinal'] == 1
    accepted = create_runtime_commit_service().commit(context, DetachedPort(realm, context))
    assert accepted['data']['status'] == 'accepted'
    assert resolve_selected_param_source(DetachedPort(realm, context), context=context,
        through=realm.head, select=REF)['source'] == p2['schema_source']


@pytest.mark.parametrize('damage', [None, 'owners', 'physical_ref', 'revision_id', 'path'])
def test_occurrence_physical_owners_come_from_sealed_closure(two_pins, damage):
    element, p1, _ = two_pins
    document = selected.document_fixture()
    for occurrence in document['parent_revision']['payload']['occurrences']:
        occurrence.update(source_offset=0, speed=1)
    document['parent_revision']['content_digest'] = selected._digest(document['parent_revision']['payload'])
    internal = document['internal_timeline_revisions'][0]
    internal['payload']['clips'][0].update(elementRef=element, params={'seed': 4})
    internal['content_digest'] = selected._digest(internal['payload'])
    document['dependency_manifest']['internal_timelines'][0]['content_digest'] = internal['content_digest']
    realm = selected._realm(two_pins, document)
    ref = 'occurrence:occ-a/clip:clip-a'
    calls = _sequence(element, p1)
    for call in calls:
        call['select'] = ref
    context = _context(realm, calls, True)
    port = DetachedPort(realm, context)
    plan = execute(selected.current(realm), calls, schema_inputs=p1['schema_inputs'],
                   realm_id='realm', commit_request=context['request'], evaluated_head=realm.head)
    binding = next(item for item in plan.records[0]['bindings'] if item['key'] == 'params.element')
    assert binding['owners'] == ['occurrence:occ-a/shot', 'occurrence:occ-b/shot']
    if damage is None:
        create_runtime_commit_service().commit(context, port)
        assert resolve_selected_param_source(port, context=context, through=realm.head,
                                             select=ref)['source'] == p1['schema_source']
    else:
        if damage == 'owners':
            binding['owners'] = ['occurrence:occ-a/shot']
        else:
            _damage_binding(plan.records[0], damage)
        with pytest.raises(CommandError, match='authority is unavailable'):
            resolve_selected_param_source(port, context=context, through=realm.head,
                                          select=ref, staged=plan.records[:2])


@pytest.fixture
def mounted_pack(monkeypatch, request):
    import sys

    from tests.helpers.runtime import use_runtime

    # This lane uses the real mounted provider, rather than the detached ABI.
    use_runtime()
    for name in tuple(sys.modules):
        if name == 'runtime_protocol' or name.startswith('runtime_protocol.'):
            monkeypatch.delitem(sys.modules, name)
    return request.getfixturevalue('retained_pack')


@pytest.mark.integration
@pytest.mark.parametrize('damage', [None, *PHYSICAL_DAMAGE, 'source', 'element', 'object'])
def test_mounted_param_custody_refuses_corruption_atomically(mounted_pack, monkeypatch, damage):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService

    from astrid.sdk.timeline import _selected_param_authority as authority

    case = mounted_pack
    body = case.request(_sequence(case.selected, case.options))
    body.update(label='Select, hold and PARAM', merge='fields', reads=[], steps=[{
        'id': 'element', 'label': 'Update element', 'intent': '', 'tags': [],
        'command_ordinals': [0, 1, 2], 'patch': [], 'reads': []}])
    original = copy.deepcopy(body)
    resolve, reached = authority.resolve_selected_param_source, []

    def inspect(port, **kwargs):
        assert [record['command']['op'] for record in kwargs['staged']] == [
            'clip.set-element', 'clip.set']
        reached.append(True)
        kwargs['staged'] = copy.deepcopy(kwargs['staged'])
        record = kwargs['staged'][0]
        if damage in ('source', 'element', 'object'):
            snapshot = next(dep['pin'] for dep in record['dependencies']
                            if dep['key'] == 'params.snapshot')
            if damage == 'object':
                snapshot['object']['object_id'] = 'sha256:' + 'f' * 64
            elif damage == 'element':
                snapshot['element']['revision'] = 'sha256:' + 'f' * 64
                record['command']['args']['elementRef'] = copy.deepcopy(snapshot['element'])
            else:
                snapshot['source']['content_digest'] = 'sha256:' + 'f' * 64
                schema = next(dep for dep in record['dependencies'] if dep['key'] == 'params.schema')
                schema['pin'] = copy.deepcopy(snapshot['source'])
                record['command']['args']['schema_source'] = copy.deepcopy(snapshot['source'])
        elif damage is not None:
            _damage_binding(record, damage)
        return resolve(port, **kwargs)

    before = case.head()
    with monkeypatch.context() as injected:
        injected.setattr(authority, 'resolve_selected_param_source', inspect)
        if damage is not None:
            with pytest.raises(RuntimeErrorBase, match=(
                    'authority is unavailable|snapshot association is unavailable|source changed')):
                case.submit(body, 'corrupt-stage')
            assert case.head() == before
        else:
            accepted = case.submit(body, 'valid-stage')
            assert accepted['data']['status'] == 'accepted'
            assert accepted['data']['label'] == body['label']
            assert accepted['data']['steps'][0]['patch']
            row = State(case.bundle()).entity(REF)
            assert row['hold'] == 3 and row['params']['seed'] == 8
    assert reached == [True]
    assert body == original
    if damage is None:
        case.service.close()
        case.service = RuntimeService(case.root)
        case.service.register_timeline_commit_provider(create_runtime_commit_service())
        assert case.submit(body, 'valid-stage') == accepted
        followup = case.request([selected._params(case.options, {'seed': 9})])
        followup.update(label='PARAM after restart', merge='fields', reads=[])
        assert case.submit(followup, 'after-restart')['data']['status'] == 'accepted'
