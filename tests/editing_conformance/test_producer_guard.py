"""Native producer recipe records intent without manufacturing Runtime authority."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline import build_producer_batch
from astrid.sdk.timeline.commands import CommandError, execute, operations, validate_command
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import inverse
from tests.editing_conformance.test_settled_proposal_contract import (
    FIXTURES,
    golden,
    settled_proposal,
)


def source_bundle():
    return json.loads((FIXTURES / golden()['source_fixture']).read_text())['bundle']


def test_native_guard_records_only_exact_dependency_and_preserves_command_metadata():
    proposal = settled_proposal()
    proposal['commands'][0].update(rationale='Keep café', group_id='producer-group')
    batch = build_producer_batch(proposal)
    assert operations()['producer.guard'].intent == {}
    before = source_bundle()
    plan = execute(before, batch['commands'])
    guard = plan.records[0]
    assert guard['dependencies'] == [golden()['dependency']]
    assert all(guard[key] == [] for key in (
        'writes', 'reads', 'selections', 'bindings', 'created', 'derived_writes'))
    assert plan.commands[1:] == proposal['commands']
    assert plan.records[1]['ordinal'] == 1
    ordinary = execute(before, proposal['commands'])
    assert plan.bundle == ordinary.bundle
    assert {**plan.records[1], 'ordinal': 0} == ordinary.records[0]
    assert before == source_bundle()
    batch['commands'][1]['args']['on'] = 500
    assert proposal['commands'][0]['args']['on'] == 4


def test_inverse_redo_and_replay_do_not_need_old_producer_authority():
    before = source_bundle()
    plan = execute(before, build_producer_batch(settled_proposal())['commands'])
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == before
    assert not any(dep['kind'] == 'authority' for record in undone.records
                   for dep in record['dependencies'])
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle
    assert replay(plan, before).records == plan.records


@pytest.mark.parametrize('field,value', [
    ('fence', True), ('fence', 0), ('fence', -1), ('fence', 1.0),
    ('runtime_epoch', False), ('runtime_epoch', 0), ('runtime_epoch', 3.0),
    ('task_id', ''), ('attempt_id', ' '), ('executor_id', None),
    ('proposal_digest', 'sha256:' + 'A' * 64),
    ('proposal_digest', 'sha256:' + 'a' * 64 + '\n'), ('extra', None),
])
def test_closed_native_pin_refuses_invalid_identity(field, value):
    guard = golden()['commands'][0]
    guard['args']['pin'][field] = value
    with pytest.raises(CommandError):
        validate_command(guard)


@pytest.mark.parametrize('field', ['extra', 'project_id', 'timeline_id',
                                  'base_revision_id', 'base_publication_id'])
def test_closed_original_target(field):
    guard = golden()['commands'][0]
    guard['args']['pin']['admitted_target'][field] = ''
    with pytest.raises(CommandError):
        validate_command(guard)


@pytest.mark.parametrize('field,value', [('select', 'parent'), ('intent', {'ripple': 'none'}),
                                        ('version', '2'), ('dependencies', [])])
def test_native_guard_envelope(field, value):
    guard = golden()['commands'][0]
    guard[field] = value
    with pytest.raises(CommandError):
        validate_command(guard)


@pytest.mark.parametrize('field', ['proposal', 'raw-json', 'serialized-proposal'])
def test_guard_args_carry_pin_only(field):
    guard = golden()['commands'][0]
    guard['args'][field] = settled_proposal()
    with pytest.raises(CommandError):
        validate_command(guard)


@pytest.mark.parametrize('mutation', ['alone', 'late', 'duplicate', 'inverse', 'restore'])
def test_guard_must_be_first_unique_and_followed_by_ordinary_edits(mutation):
    guard, edit = golden()['commands']
    commands = {'alone': [guard], 'late': [edit, guard], 'duplicate': [guard, guard, edit],
                'inverse': [guard, {'op': 'timeline.inverse'}],
                'restore': [guard, {'op': 'timeline.restore'}]}[mutation]
    with pytest.raises(CommandError):
        execute(source_bundle(), commands)


def test_private_verified_pin_cannot_substitute_declared_dependency():
    batch = build_producer_batch(settled_proposal())
    changed = copy.deepcopy(batch['commands'][0]['args']['pin'])
    changed['fence'] += 1
    with pytest.raises(CommandError, match='verified producer pin changed'):
        execute(source_bundle(), batch['commands'], verified_producer=changed)


def test_guard_metadata_and_empty_claims_roundtrip_through_native_capture():
    commands = golden()['commands']
    commands[0].update(rationale='Settled result review', group_id='proposal', intent={},
                       resolved=[], expect={}, authorship={}, reads=[], selections=[],
                       bindings={}, created=[])
    plan = execute(source_bundle(), commands)
    assert plan.commands[0] == commands[0]
    assert plan.records[0]['writes'] == []


def test_guard_cannot_claim_authored_reads_that_it_did_not_make():
    commands = golden()['commands']
    commands[0]['reads'] = commands[1]['reads'][:1]
    with pytest.raises(CommandError, match='submitted reads'):
        execute(source_bundle(), commands)


def test_builder_preserves_node_forwarded_values_without_rehashing():
    # Integral float spelling may already be lost by the requesting JS session.
    proposal = settled_proposal()
    original_digest = proposal['proposal_digest']
    proposal['commands'][0]['args']['on'] = 4.0
    batch = build_producer_batch(proposal)
    assert batch['commands'][0]['args']['pin']['proposal_digest'] == original_digest
    assert type(batch['commands'][1]['args']['on']) is float
    assert set(batch) == {*proposal['admitted_target'], 'commands', 'rationale'}
