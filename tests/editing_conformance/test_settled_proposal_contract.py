"""Design goldens only; no registered producer guard or Runtime acceptance yet."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from astrid.core.timeline.authoring_bundle import AuthoringBundleError
from astrid.sdk.timeline._command_state import equal
from astrid.sdk.timeline._recording import Recording
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, _json
from tests.editing_conformance.test_recipe_m1_commands import inverse

FIXTURES = Path(__file__).parent / 'fixtures'
SCHEMA = json.loads((Path(__file__).parents[2]
                     / 'astrid/core/timeline/schema/edit-command.schema.json').read_text())
BATCH_SCHEMA = {**SCHEMA, '$ref': '#/$defs/CommandBatch'}


def golden():
    return json.loads((FIXTURES / 'settled_proposal_move.json').read_text())


def settled_proposal():
    return json.loads((FIXTURES / golden()['artifact']['fixture']).read_bytes())


def payload(proposal):
    return {key: value for key, value in proposal.items() if key != 'proposal_digest'}


def test_settled_move_golden_uses_existing_capture_execution_and_inverse():
    fixture = golden()
    source = json.loads((FIXTURES / fixture['source_fixture']).read_text())
    guard, *commands = fixture['commands']
    proposal, pin = settled_proposal(), guard['args']['pin']
    command = capture_move(source['bundle'], source['select'], source['on'])
    assert commands == proposal['commands'] == [command]
    assert proposal['admitted_target'] == {
        'project_id': source['bundle']['project_id'],
        'timeline_id': source['bundle']['timeline_id'],
        'base_revision_id': source['bundle']['base_parent']['revision_id'],
        'base_publication_id': 'bootstrap-publication',
    }
    assert set(proposal) == {'schema_version', 'producer', 'admitted_target',
                             'commands', 'rationale', 'proposal_digest'}
    assert pin == {**proposal['producer'], 'admitted_target': proposal['admitted_target'],
                   'proposal_digest': _digest(payload(proposal))}
    assert pin['proposal_digest'] == proposal['proposal_digest'] == (
        'sha256:a4939d8873bd3bdc4f8eb79fc973267b0b1e37bb468d89f9ea9ca3d635de0a1b')
    assert fixture['rationale'] == proposal['rationale']
    assert check_forwarding(fixture, fixture['artifact']) == pin
    plan = execute(source['bundle'], commands)
    assert plan.bundle == source['after_bundle']
    assert inverse(plan.bundle, plan.records).bundle == source['bundle']
    assert execute(source['bundle'], commands).records == plan.records


def test_guard_fits_closed_envelope_and_authority_is_only_recorder_output():
    fixture = golden()
    guard = fixture['commands'][0]
    Draft202012Validator(BATCH_SCHEMA).validate(fixture['commands'])
    assert {key: value for key, value in guard.items() if key != 'args'} == {
        'schema_version': 1, 'op': 'producer.guard', 'version': '1', 'select': None,
    }
    assert set(guard['args']) == {'pin'}
    assert set(guard['args']['pin']) == {'task_id', 'attempt_id', 'executor_id', 'fence',
                                         'runtime_epoch', 'admitted_target', 'proposal_digest'}
    assert fixture['dependency'] == {
        'key': 'producer_attempt:assembler-task', 'kind': 'authority', 'policy': 'exact',
        'pin': guard['args']['pin'],
    }
    Draft202012Validator({**SCHEMA, '$ref': '#/$defs/AgentDependency'}).validate(
        fixture['dependency'])
    # This checks the existing recorder carrier, not task authority verification.
    source = json.loads((FIXTURES / fixture['source_fixture']).read_text())
    before = copy.deepcopy(source['bundle'])
    recorder = Recording(source['bundle'], guard, 0)
    recorder.dependency(fixture['dependency']['key'], 'authority', guard['args']['pin'])
    assert recorder.finish()['dependencies'] == [fixture['dependency']]
    assert recorder.finish()['writes'] == []
    assert source['bundle'] == before
    invalid = copy.deepcopy(fixture['commands'])
    invalid[0]['dependencies'] = [fixture['dependency']]
    assert not Draft202012Validator(BATCH_SCHEMA).is_valid(invalid)


@pytest.mark.parametrize('field,replacement', [
    ('args', {'on': 5}), ('select', 'parent/clip:substitute'), ('version', '2'),
    ('expect', {}), ('reads', []), ('selections', []), ('bindings', {}),
    ('created', ['parent/clip:reserved']), ('authorship', {}), ('intent', {}),
    ('rationale', 'requester changed reason'), ('group_id', 'another-group'),
    ('resolved', []), ('op', 'track.create'),
])
def test_digest_binds_all_settled_command_fields(field, replacement):
    proposal = settled_proposal()
    changed = copy.deepcopy(proposal)
    changed['commands'][0][field] = replacement
    assert _digest(payload(changed)) != proposal['proposal_digest']


@pytest.mark.parametrize('scope,field,replacement', [
    ('producer', 'task_id', 'other-task'), ('producer', 'attempt_id', 'new-attempt'),
    ('producer', 'executor_id', 'other-executor'), ('producer', 'fence', 8),
    ('producer', 'runtime_epoch', 4), ('admitted_target', 'project_id', 'elsewhere'),
    ('admitted_target', 'timeline_id', 'other-film'),
    ('admitted_target', 'base_revision_id', 'new-revision'),
    ('admitted_target', 'base_publication_id', 'new-publication'),
])
def test_digest_binds_attempt_and_original_target(scope, field, replacement):
    proposal = settled_proposal()
    changed = copy.deepcopy(proposal)
    changed[scope][field] = replacement
    assert _digest(payload(changed)) != proposal['proposal_digest']


def test_digest_binds_rationale_presence_order_and_extra_commands():
    proposal = settled_proposal()
    for rationale in ('different', None):
        assert _digest({**payload(proposal), 'rationale': rationale}) != proposal['proposal_digest']
    without_reason = payload(proposal)
    del without_reason['rationale']
    assert _digest(without_reason) != proposal['proposal_digest']
    changed = copy.deepcopy(proposal)
    changed['commands'][0]['reads'].reverse()
    assert _digest(payload(changed)) != proposal['proposal_digest']
    changed = copy.deepcopy(proposal)
    changed['commands'].append(copy.deepcopy(changed['commands'][0]))
    assert _digest(payload(changed)) != proposal['proposal_digest']
    a = proposal['commands'][0]
    b = {**a, 'args': {'on': 5}}
    assert _digest([a, b]) != _digest([b, a])
    assert _digest({**payload(proposal), 'extra': None}) != proposal['proposal_digest']


def test_canonicalization_is_existing_finite_utf8_json_without_normalization():
    value = {'z': [None, True, 1, 1.0, -0.0, 0.000001], 'é': 'e\u0301', 'a': 'café'}
    expected = '{"a":"café","z":[null,true,1,1.0,-0.0,1e-06],"é":"é"}'
    assert _json(value).encode('utf-8') == expected.encode('utf-8')
    assert _json(dict(reversed(list(value.items())))) == expected
    assert _digest({'x': 'é'}) != _digest({'x': 'e\u0301'})
    assert _digest({'x': 1}) != _digest({'x': 1.0})


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_proposal_cannot_have_a_digest(value):
    proposal = settled_proposal()
    proposal['commands'][0]['args']['on'] = value
    with pytest.raises(AuthoringBundleError, match='finite JSON'):
        _digest(payload(proposal))


def check_forwarding(request, descriptor, *, raw=None):
    """Bounded value/custody spike, not a Runtime port or admission simulator."""
    if raw is None:
        raw = (FIXTURES / descriptor['fixture']).read_bytes()
    if (len(raw) != descriptor['size']
            or 'sha256:' + hashlib.sha256(raw).hexdigest() != descriptor['digest']):
        raise ValueError('artifact byte custody')
    proposal = json.loads(raw)
    digest = _digest(payload(proposal))
    if digest != proposal['proposal_digest']:
        raise ValueError('original proposal digest')
    pin = {**proposal['producer'], 'admitted_target': proposal['admitted_target'],
           'proposal_digest': digest}
    guard, *commands = request['commands']
    if (_json({key: value for key, value in guard.items() if key != 'args'}) != _json({
            'schema_version': 1, 'op': 'producer.guard', 'version': '1', 'select': None})
            or set(guard['args']) != {'pin'}):
        raise ValueError('closed pin-only guard')
    if _json(guard['args']['pin']) != _json(pin):
        raise ValueError('exact original pin')
    if (not equal(commands, proposal['commands']) or 'rationale' not in request
            or not equal(request['rationale'], proposal['rationale'])):
        raise ValueError('forwarded commands or rationale')
    return pin


@pytest.fixture(scope='module')
def node_forwarded_numeric():
    descriptor = golden()['numeric_forwarding']['artifact']
    raw = (FIXTURES / descriptor['fixture']).read_bytes()
    # Use the installed toolchain, not a Python approximation of JS number spelling.
    result = subprocess.run(['node', '-e', """
const proposal = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const pin = {...proposal.producer, admitted_target: proposal.admitted_target,
             proposal_digest: proposal.proposal_digest};
process.stdout.write(JSON.stringify({commands: [
  {schema_version: 1, op: 'producer.guard', version: '1', select: null, args: {pin}},
  ...proposal.commands
], rationale: proposal.rationale}));
"""], input=raw, capture_output=True, check=True, timeout=15)
    return json.loads(result.stdout)


def test_node_numeric_forwarding_accepts_original_digest_and_executes(node_forwarded_numeric):
    fixture = golden()
    vector = fixture['numeric_forwarding']
    original = json.loads((FIXTURES / vector['artifact']['fixture']).read_bytes())
    request = node_forwarded_numeric
    assert type(original['commands'][0]['args']['on']) is float
    assert type(request['commands'][1]['args']['on']) is int
    assert original['commands'][0]['args']['on'] == 1.0
    assert request['commands'][1]['args']['on'] == 1
    assert _digest(payload(original)) == vector['pin']['proposal_digest'] == (
        'sha256:a2669f27bcf2cabb6705215a1d694e08efbfb051303ce5cc2c847463865d4052')
    assert _digest({**payload(original), 'commands': request['commands'][1:]}) != (
        vector['pin']['proposal_digest'])
    assert check_forwarding(request, vector['artifact']) == vector['pin']
    Draft202012Validator(BATCH_SCHEMA).validate(request['commands'])
    source = json.loads((FIXTURES / fixture['source_fixture']).read_text())
    plan = execute(source['bundle'], request['commands'][1:])
    assert plan.bundle == source['after_bundle']
    assert plan.records == execute(source['bundle'], original['commands']).records
    assert inverse(plan.bundle, plan.records).bundle == source['bundle']


@pytest.mark.parametrize('field,replacement', [
    ('args', {'on': 1.5}), ('args', {'on': True}),
    ('select', 'parent/clip:substitute'), ('version', '2'),
    ('expect', {}), ('reads', []), ('selections', []), ('bindings', {}),
    ('created', ['parent/clip:reserved']), ('authorship', {}), ('intent', {}),
    ('rationale', 'requester changed reason'), ('group_id', 'another-group'),
    ('resolved', []), ('op', 'track.create'),
])
def test_node_forwarding_refuses_value_and_capture_mutations(
        node_forwarded_numeric, field, replacement):
    request = copy.deepcopy(node_forwarded_numeric)
    request['commands'][1][field] = replacement
    with pytest.raises(ValueError, match='forwarded commands or rationale'):
        check_forwarding(request, golden()['numeric_forwarding']['artifact'])


@pytest.mark.parametrize('mutation', [
    'command-order', 'read-order', 'extra-command', 'missing-command',
    'reason', 'missing-reason', 'null-reason', 'extra-null', 'missing-field',
    'pin-id', 'pin-extra', 'target', 'digest', 'proposal-copy', 'serialized-proposal',
    'raw-json',
])
def test_node_forwarding_refuses_order_presence_pin_and_copy_mutations(
        node_forwarded_numeric, mutation):
    request = copy.deepcopy(node_forwarded_numeric)
    guard, first = request['commands'][:2]
    if mutation == 'command-order':
        request['commands'][1:] = reversed(request['commands'][1:])
    elif mutation == 'read-order':
        first['reads'].reverse()
    elif mutation == 'extra-command':
        request['commands'].append(copy.deepcopy(first))
    elif mutation == 'missing-command':
        request['commands'].pop()
    elif mutation == 'reason':
        request['rationale'] += ' Changed.'
    elif mutation == 'missing-reason':
        del request['rationale']
    elif mutation == 'null-reason':
        request['rationale'] = None
    elif mutation == 'extra-null':
        first['extra'] = None
    elif mutation == 'missing-field':
        del first['created']
    elif mutation == 'pin-id':
        guard['args']['pin']['attempt_id'] = 'replacement-attempt'
    elif mutation == 'pin-extra':
        guard['args']['pin']['extra'] = None
    elif mutation == 'target':
        guard['args']['pin']['admitted_target']['base_publication_id'] = 'new-head'
    elif mutation == 'digest':
        guard['args']['pin']['proposal_digest'] = _digest(request['commands'][1:])
    else:
        guard['args']['proposal' if mutation == 'proposal-copy' else mutation] = (
            settled_proposal() if mutation == 'proposal-copy'
                                   else _json(settled_proposal()))
    with pytest.raises(ValueError):
        check_forwarding(request, golden()['numeric_forwarding']['artifact'])


@pytest.mark.parametrize('rebind_descriptor,recompute_proposal_digest', [
    (False, False), (True, False), (True, True),
])
def test_tampered_artifact_refuses_original_pin(
        node_forwarded_numeric, rebind_descriptor, recompute_proposal_digest):
    descriptor = copy.deepcopy(golden()['numeric_forwarding']['artifact'])
    raw = (FIXTURES / descriptor['fixture']).read_bytes()
    # Same byte size, changed numeric value: raw custody must still catch this.
    tampered = raw.replace(b'"on":1.0', b'"on":1.5', 1)
    assert tampered != raw and len(tampered) == len(raw)
    if recompute_proposal_digest:
        proposal = json.loads(tampered)
        proposal['proposal_digest'] = _digest(payload(proposal))
        tampered = _json(proposal).encode('utf-8')
    if rebind_descriptor:
        descriptor.update(size=len(tampered),
                          digest='sha256:' + hashlib.sha256(tampered).hexdigest())
    reason = ('artifact byte custody' if not rebind_descriptor else
              'exact original pin' if recompute_proposal_digest else 'original proposal digest')
    with pytest.raises(ValueError, match=reason):
        check_forwarding(node_forwarded_numeric, descriptor, raw=tampered)
