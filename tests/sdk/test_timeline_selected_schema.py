"""New parameter edits must respect the selected immutable pack declaration."""
from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from astrid.core.element import project_packs
from astrid.core.timeline.authoring_bundle import AuthoringBundleError, compile_authoring_candidate
from astrid.sdk.timeline._clip_commands import capture_clip_add
from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set_element
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import (
    _digest,
    bundle_from_document,
    create_runtime_commit_service,
    document_from_bundle,
)
from tests.editing_conformance import test_recipe_scalar_commands as scalar_recipes
from tests.editing_conformance.test_recipe_m2_clip_add import empty_document
from tests.helpers.selected_param_legacy import accepted_params
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedPort,
    DetachedRealm,
    inverse,
    track_create,
)
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_scalar import current

REF = 'parent/clip:effect'
admitted_param_revisions = scalar_recipes.admitted_param_revisions
configured_project_pack = scalar_recipes.configured_project_pack


def commit(realm, commands):
    context = realm.context(commands)
    # Use the same realm/project as the real admission fixture.
    context['realm_id'] = 'realm'
    return create_runtime_commit_service().commit(context, DetachedPort(realm, context))


@pytest.fixture
def selected_effect(admitted_param_revisions, configured_project_pack):
    _, selected = admitted_param_revisions
    record = project_packs.resolve_admitted_element('realm', 'project-1', selected)
    schema = record['descriptor']['schema']
    assert schema['properties']['seed']['maximum'] == 10
    Draft202012Validator(schema).validate({'seed': 4})
    Draft202012Validator(schema).validate({'seed': 8})
    errors = list(Draft202012Validator(schema).iter_errors({'seed': 16}))
    assert len(errors) == 1 and errors[0].validator == 'maximum'

    realm = DetachedRealm(empty_document())
    realm.schema_options = scalar_recipes.schema_options(configured_project_pack, selected)
    verified, realm.snapshot_objects = realm.schema_options['schema_inputs'][0]
    realm.live_sources[_digest(verified['pin'])] = verified
    commit(realm, [track_create('picture')])
    row = {'id': 'effect', 'clipType': selected['id'], 'elementRef': selected,
           'at': 0, 'hold': 2, 'track': 'picture',
           'params': {'seed': 4, 'future': {'keep': [None, False, {}]}}}
    commit(realm, [capture_clip_add(current(realm), 'parent', row)])
    assert State(current(realm)).entity(REF) == row
    commit(realm, [capture_clip_set_element(current(realm), REF, element=selected,
                                            **realm.schema_options)])
    commit(realm, [capture_clip_set_params(current(realm), REF, values={'seed': 4},
                                           **realm.schema_options)])
    return realm, selected, schema


def test_new_valid_selected_param_compiles_commits_reloads_and_roundtrips(selected_effect):
    realm, selected, schema = selected_effect
    original = copy.deepcopy(realm.documents[realm.head['revision_id']])
    opaque = {'literal': [None, False, {}, 'ƒ(keep literal)']}
    command = capture_clip_set_params(current(realm), REF, values={'seed': 8, 'other': opaque},
                                      **realm.schema_options)
    plan = execute(current(realm), [command], schema_inputs=realm.schema_options['schema_inputs'],
                   realm_id='realm')
    compile_authoring_candidate(plan.bundle)
    accepted = commit(realm, [command])
    assert accepted['data']['commands'] == [command]
    document = accepted['data']['canonical_document']
    reloaded = bundle_from_document(json.loads(json.dumps(document)))
    row = State(reloaded).entity(REF)
    assert row['elementRef'] == selected
    assert row['params'] == {'seed': 8, 'other': opaque, 'future': {'keep': [None, False, {}]}}
    Draft202012Validator(schema).validate(row['params'])
    assert project_packs.resolve_admitted_element(
        'realm', 'project-1', row['elementRef'])['descriptor']['schema'] == schema
    undone = commit(realm, [inverse([accepted['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == original
    redone = commit(realm, [inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == document


def test_admitted_element_can_be_placed_on_occurrence_clip_without_prior_ref(selected_effect):
    realm, selected, _ = selected_effect
    original = current(realm)
    shot_id = 'shot-placement'
    occurrence_id = 'occ-placement'
    target = f'occurrence:{occurrence_id}/clip:discord'
    original['placements'] = [{
        'occurrence_id': occurrence_id, 'shot_id': shot_id,
        'placement': {'start_ms': 0}, 'duration_ms': 2000,
    }]
    original['source_mapping']['placements'] = {
        occurrence_id: copy.deepcopy(original['placements'][0]),
    }
    original['source_mapping']['shots'] = {
        shot_id: {'shot_id': shot_id, 'revision_id': 'shot-revision',
                  'content_digest': 'sha256:' + 'a' * 64},
    }
    original['shots'] = {
        shot_id: {
            'payload': {},
            'internal_timeline': {
                'tracks': [{'id': 'picture', 'kind': 'visual'}],
                'clips': [{'id': 'discord', 'clipType': 'am-discord',
                           'track': 'picture', 'at': 0, 'hold': 2, 'params': {}}],
            },
        },
    }

    command = capture_clip_set_element(
        original, target, element=selected, **realm.schema_options)
    assert command['select'] == target
    assert command['args']['schema_source'] == realm.schema_options['schema_source']
    plan = execute(original, [command],
                   schema_inputs=realm.schema_options['schema_inputs'],
                   realm_id=realm.schema_options['realm_id'])
    assert State(plan.bundle).entity(target)['elementRef'] == selected


@pytest.mark.parametrize('stage', ['capture', 'compile', 'provider'])
# Retained-input qualification: test_timeline_selected_schema_runtime.py uses
# actual admission + paired Runtime custody through restart/replay/inverse.
def test_new_invalid_selected_param_refuses_without_acceptance(selected_effect, stage):
    realm, selected, schema = selected_effect
    original = current(realm)
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    assert State(original).entity(REF)['elementRef'] == selected
    assert list(Draft202012Validator(schema).iter_errors({'seed': 16}))
    with pytest.raises((CommandError, AuthoringBundleError, CommitRejected), match='seed|16|10'):
        command = capture_clip_set_params(original, REF, values={'seed': 16}, **realm.schema_options)
        if stage != 'capture':
            plan = execute(original, [command], schema_inputs=realm.schema_options['schema_inputs'],
                           realm_id='realm')
            compile_authoring_candidate(plan.bundle)
        if stage == 'provider':
            commit(realm, [command])
    assert (realm.head, realm.entries, realm.documents) == before
    assert current(realm) == original


@pytest.mark.parametrize('damage', ['missing', 'pin', 'owner', 'id', 'kind', 'revision',
                                   'revoked', 'corrupt', 'legacy', 'foreign_object', 'missing_object'])
def test_selected_input_identity_and_portable_bytes_fail_closed(selected_effect, damage):
    realm, selected, _ = selected_effect
    original = current(realm)
    options = copy.deepcopy(realm.schema_options)
    verified, objects = options['schema_inputs'][0]
    if damage == 'missing':
        options['schema_inputs'] = []
    elif damage == 'pin':
        options['schema_source']['version'] += 1
    elif damage in {'owner', 'id', 'kind', 'revision'}:
        key = 'packId' if damage == 'owner' else damage
        State(original).entity(REF)['elementRef'][key] = 'wrong'
    elif damage == 'revoked':
        verified['document']['content']['admissions'] = []
    elif damage == 'legacy':
        for admission in verified['document']['content']['admissions']:
            del admission['snapshot_object']
    elif damage == 'corrupt':
        for snapshot in objects.values():
            snapshot['bytes_base64'] = 'e30='
    elif damage == 'missing_object':
        objects.clear()
    else:
        admission = next(item for item in verified['document']['content']['admissions']
                         if item['revision'] == selected['revision'])
        object_id = admission['snapshot_object']['object_id']
        other = next(snapshot for identity, snapshot in objects.items() if identity != object_id)
        objects[object_id] = other
    # The caller cannot turn even well-formed JSON into an admitted declaration.
    with pytest.raises(CommandError):
        capture_clip_set_params(original, REF, values={'seed': 8}, **options)


def test_raw_provider_rechecks_admission_instead_of_caller_catalog(selected_effect, monkeypatch):
    realm, _, _ = selected_effect
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    # Type-only/caller catalog information cannot authorize the value.
    monkeypatch.setattr('astrid.sdk.timeline_address.element_schema', lambda _: {
        'properties': {'seed': {'type': 'number', 'maximum': 100}}})
    raw = {'schema_version': 1, 'op': 'clip.set-params', 'version': '1', 'select': REF,
           'args': {'values': {'seed': 16}, 'clear': [],
                    'schema_source': realm.schema_options['schema_source']}}
    with pytest.raises(CommitRejected, match='10'):
        commit(realm, [raw])
    assert (realm.head, realm.entries, realm.documents) == before


def test_noop_declared_value_clear_and_opaque_values_preserve_literal_state(selected_effect):
    realm, _, _ = selected_effect
    original = current(realm)
    State(original).entity(REF)['params']['seed'] = 16
    original['parent']['config']['clips'] = copy.deepcopy(original['parent']['clips'])
    # Existing legacy values are not retroactively validated by a no-op write.
    command = capture_clip_set_params(original, REF, values={'seed': 16}, **realm.schema_options)
    plan = execute(original, [command], schema_inputs=realm.schema_options['schema_inputs'], realm_id='realm')
    assert document_from_bundle(plan.bundle) == document_from_bundle(original)
    assert any(write['before'] == write['after'] and write['path'] == ['params', 'seed']
               for write in plan.records[0]['writes'])
    opaque = {'unknown': [None, False, {}, 'ƒ(literal)']}
    command = capture_clip_set_params(original, REF, values={'opaque': opaque}, clear=['seed'],
                                      **realm.schema_options)
    plan = execute(original, [command], schema_inputs=realm.schema_options['schema_inputs'], realm_id='realm')
    assert State(plan.bundle).entity(REF)['params'] == {
        'opaque': opaque, 'future': {'keep': [None, False, {}]}}


@pytest.mark.parametrize('pin', [None, {}, {'document_id': 'x', 'version': True,
                                          'content_digest': 'sha256:' + '0' * 64},
                                {'document_id': 'x', 'version': 0,
                                 'content_digest': 'sha256:' + '0' * 64},
                                {'document_id': 'x', 'version': 1, 'content_digest': 'bad'},
                                {'document_id': 'x', 'version': 1,
                                 'content_digest': 'sha256:' + '0' * 64, 'schema': {}}])
def test_schema_source_is_only_a_closed_exact_document_pin(selected_effect, pin):
    realm, _, _ = selected_effect
    raw = {'schema_version': 1, 'op': 'clip.set-params', 'version': '1', 'select': REF,
           'args': {'values': {'seed': 8}, 'clear': [], 'schema_source': pin}}
    with pytest.raises(CommandError):
        execute(current(realm), [raw])


def test_selected_pack_refuses_a_port_without_document_object(selected_effect, monkeypatch):
    realm, _, _ = selected_effect
    command = capture_clip_set_params(current(realm), REF, values={'seed': 8}, **realm.schema_options)
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    monkeypatch.delattr(DetachedPort, 'document_object')
    with pytest.raises(CommitRejected, match='document_object support is required'):
        commit(realm, [command])
    assert (realm.head, realm.entries, realm.documents) == before


def test_inverse_of_older_param_record_uses_only_selected_identity(monkeypatch):
    historical = accepted_params()

    class HistoricalRealm(DetachedRealm):
        def context(self, *args, **kwargs):
            context = super().context(*args, **kwargs)
            context['request']['project_id'] = historical['project']
            context['invocation']['path'] = f"/v1/projects/{historical['project']}/timelines/film/commit"
            context['request_hash'] = _digest(context['invocation'])
            return context

    # Detached compatibility boundary: these are unmodified reads from an old
    # real Runtime acceptance, not a newly authorized first PARAM. The native
    # companion restores its authenticated backup and tests current authority.
    realm = HistoricalRealm()
    realm.head, realm.entries, realm.documents = (
        historical['head'], historical['entries'], historical['documents'])
    realm.schema_options = historical['options']
    verified, realm.snapshot_objects = realm.schema_options['schema_inputs'][0]
    realm.retained_sources[_digest(verified['pin'])] = verified
    selected, row, accepted = historical['selected'], historical['row'], historical['accepted']
    # The ignored alias names another real admitted revision in this same pin.
    admissions = realm.schema_options['schema_inputs'][0][0]['document']['content']['admissions']
    other = next(item for item in admissions if item['revision'] != selected['revision'])
    assert row['elementRef'] == selected
    assert row['element_ref'] == {**{key: other[key] for key in ('id', 'kind', 'revision')},
                                 'packId': selected['packId']}
    ref = 'parent/clip:legacy-alias'
    assert State(current(realm)).entity(ref) == {**row, 'params': {**row['params'], 'seed': 8}}
    # Represent the predecessor's accepted record shape, preserving all its
    # original source pin, authored writes, identity reads and expectations.
    record = realm.entries[-1]['records']['commands'][0]
    record['dependencies'] = [dep for dep in record['dependencies'] if dep['key'] != 'params.snapshot']
    for binding in record['bindings']:
        if binding['key'] == 'params.element':
            binding['dependencies'] = ['params.schema']
    reads = []
    original = DetachedPort.document_object

    def read(port, pin, object_id):
        reads.append((pin, object_id))
        return original(port, pin, object_id)

    monkeypatch.setattr(DetachedPort, 'document_object', read)
    commit(realm, [inverse([accepted['receipt']['receipt_id']])])
    assert State(current(realm)).entity(ref) == row
    selected_object = next(item['snapshot_object']['object_id'] for item in admissions
                           if item['revision'] == selected['revision'])
    assert reads == [(realm.schema_options['schema_source'], selected_object)]


@pytest.mark.parametrize('params', [None, {}])
def test_element_only_retains_target_declaration_for_rebase_reload_and_inverse(selected_effect, params):
    from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set_element
    from astrid.sdk.timeline._scalar_commands import capture_track_set

    realm, selected, _ = selected_effect
    row = copy.deepcopy(State(current(realm)).entity(REF))
    row['id'] = 'element-only'
    row.pop('params')
    if params is not None:
        row['params'] = params
    ref = 'parent/clip:element-only'
    commit(realm, [capture_clip_add(current(realm), 'parent', row)])
    original = copy.deepcopy(current(realm))
    pin = realm.schema_options['schema_source']
    admissions = realm.schema_options['schema_inputs'][0][0]['document']['content']['admissions']
    target = next(item for item in admissions if item['revision'] != selected['revision'])
    element = {key: target[key] for key in ('id', 'kind', 'revision')}
    element['packId'] = selected['packId']
    command = capture_clip_set_element(original, ref, element=element, **realm.schema_options)
    base = copy.deepcopy(realm.head)
    commit(realm, [capture_track_set(current(realm), 'parent/track:picture', values={'label': 'Newer'})])
    context = realm.context([command], base=base)
    context['realm_id'] = 'realm'
    accepted = create_runtime_commit_service().commit(context, DetachedPort(realm, context))
    assert accepted['data']['status'] == 'rebased'
    reloaded = bundle_from_document(json.loads(json.dumps(accepted['data']['canonical_document'])))
    assert State(reloaded).entity(ref) == {**row, 'elementRef': element}
    record = realm.entries[-1]['records']['commands'][0]
    assert [write['path'] for write in record['writes']] == [['elementRef']]
    assert next(dep['pin'] for dep in record['dependencies'] if dep['key'] == 'params.snapshot') == {
        'source': pin, 'element': element, 'object': target['snapshot_object']}
    assert accepted['data']['canonical_document']['dependency_manifest']['documents'] == [pin]
    # Only retained inputs remain available to a fresh provider instance.
    realm.live_sources.clear()
    realm.reads.clear()
    undone = commit(realm, [inverse([accepted['receipt']['receipt_id']])])
    assert State(current(realm)).entity(ref) == row
    assert [read for read in realm.reads if read[0] == 'config'] == [('config', pin)]
    redone = commit(realm, [inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == accepted['data']['canonical_document']


@pytest.mark.parametrize('damage', ['missing-pin', 'revision', 'owner', 'missing-object', 'corrupt-object'])
def test_element_only_refuses_before_provider_acceptance(selected_effect, damage):
    realm, selected, _ = selected_effect
    pin = realm.schema_options['schema_source']
    command = {'schema_version': 1, 'op': 'clip.set-element', 'version': '1', 'select': REF,
               'args': {'elementRef': copy.deepcopy(selected), 'schema_source': copy.deepcopy(pin)}}
    if damage == 'missing-pin':
        del command['args']['schema_source']
    elif damage in ('revision', 'owner'):
        command['args']['elementRef']['packId' if damage == 'owner' else 'revision'] = (
            'unadmitted' if damage == 'owner' else 'sha256:' + '0' * 64)
    elif damage == 'missing-object':
        realm.snapshot_objects.clear()
    else:
        for obj in realm.snapshot_objects.values():
            obj['bytes_base64'] = 'e30='
    before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.prepared))
    with pytest.raises(CommitRejected):
        commit(realm, [command])
    assert (realm.head, realm.entries, realm.documents, realm.prepared) == before
