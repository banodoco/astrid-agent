"""Same-occurrence native fork, immutable sibling preservation and guarded inverse."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commands import execute as _execute
from astrid.sdk.timeline.commands import validate_command
from astrid.sdk.timeline.commit import (
    _digest,
    _manifest,
    bundle_from_document,
    document_from_bundle,
)
from astrid.sdk.timeline.native_occurrence_fork import capture_occurrence_fork
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.m2_authenticated_redo import AuthenticatedRedo
from tests.editing_conformance.test_recipe_m1_commands import inverse as _inverse
from tests.editing_conformance.test_recipe_m1_commands import later
from tests.editing_conformance.test_recipe_occurrence_duplicate import identity_document
from tests.sdk.test_timeline_commit_provider_document import document_fixture

_AUTHENTICATED_REDO = AuthenticatedRedo(_execute, _inverse)
execute = _AUTHENTICATED_REDO.execute
inverse = _AUTHENTICATED_REDO.inverse
accepted = _AUTHENTICATED_REDO.accepted


def fork_document(*, distinct=False, count=2):
    document = identity_document(count, ('stable_deep_link', 'output_identity'), None)
    if distinct:
        shot = copy.deepcopy(document['shot_revisions'][0])
        shot.update(shot_id='other-shot', revision_id='other-shot-revision')
        shot['payload']['items'][0]['item_id'] = 'other-item'
        shot['content_digest'] = _digest(shot['payload'])
        document['shot_revisions'].append(shot)
        document['parent_revision']['payload']['occurrences'][1].update(
            shot_id=shot['shot_id'], shot_revision_id=shot['revision_id'])
        document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
        document['dependency_manifest'] = _manifest(document['shot_revisions'],
            document['internal_timeline_revisions'], document['dependency_manifest']['media'], [])
    return document


def fork(bundle):
    return capture_occurrence_fork(bundle, 'occurrence:occ-0', 'shot-fork')


@pytest.mark.parametrize('distinct', [False, True], ids=['shared-shot', 'shared-internal'])
def test_fork_then_edit_keeps_selected_identity_order_timing_and_exact_sibling(distinct):
    document = fork_document(distinct=distinct)
    original = bundle_from_document(document)
    before = copy.deepcopy(original)
    call = fork(original)
    plan = execute(original, [call])
    assert original == before
    assert call['created'] == ['shot:shot-fork']
    output = document_from_bundle(plan.bundle)
    rows = output['parent_revision']['payload']['occurrences']
    old = document['parent_revision']['payload']['occurrences']
    assert rows[1:] == old[1:]
    assert rows[0] == {**old[0], 'shot_id': 'shot-fork', 'shot_revision_id': rows[0]['shot_revision_id']}
    assert document['shot_revisions'][-1] in output['shot_revisions']
    assert document['internal_timeline_revisions'][0] in output['internal_timeline_revisions']
    copied = next(row for row in output['shot_revisions'] if row['shot_id'] == 'shot-fork')
    child = next(row for row in output['internal_timeline_revisions']
                 if row['revision_id'] == copied['internal_timeline_revision_id'])
    assert child['payload'] == document['internal_timeline_revisions'][0]['payload']
    assert child['revision_id'] != document['internal_timeline_revisions'][0]['revision_id']
    assert copied['payload']['items'][0]['item_id'] != 'item-1'
    assert copied['payload']['items'][0]['metadata'] == {'opaque_literal': 'item-1'}
    reloaded = bundle_from_document(output)
    edit = {'schema_version': 1, 'op': 'clip.set', 'version': '1',
            'select': 'occurrence:occ-0/clip:clip-a', 'args': {'values': {'hold': 3}, 'clear': []}}
    edited = document_from_bundle(execute(reloaded, [edit]).bundle)
    assert edited['parent_revision']['payload']['occurrences'][1:] == old[1:]
    assert document['internal_timeline_revisions'][0] in edited['internal_timeline_revisions']
    assert document['shot_revisions'][-1] in edited['shot_revisions']
    assert State(bundle_from_document(edited)).entity('occurrence:occ-0/clip:clip-a')['hold'] == 3
    assert State(bundle_from_document(edited)).entity('occurrence:occ-1/clip:clip-a')['hold'] == 2


@pytest.mark.parametrize('count', [1, 2])
def test_reload_undo_redo_exact_and_replay(count):
    document = fork_document(count=count)
    original = bundle_from_document(document)
    plan = execute(original, [fork(original)])
    output = document_from_bundle(plan.bundle)
    reloaded = bundle_from_document(output)
    undone = inverse(reloaded, accepted(plan.records, original, reloaded))
    restored_document = document_from_bundle(undone.bundle, source_bundles=(original, reloaded))
    assert restored_document == document
    restored = bundle_from_document(restored_document)
    redone = inverse(restored, accepted(undone.records, reloaded, restored))
    assert document_from_bundle(redone.bundle, source_bundles=(original, reloaded)) == output
    assert replay(plan, original).records == plan.records


@pytest.mark.parametrize('mutation', ['source', 'row', 'order', 'occupied'])
def test_stale_capture_refused_atomically(mutation):
    original = bundle_from_document(fork_document())
    call = fork(original)
    head = copy.deepcopy(original)
    if mutation == 'source':
        State(head).config('occurrence:occ-0')['clips'][0]['hold'] = 3
    elif mutation == 'row':
        State(head).entity('occurrence:occ-0')['unknown'] = {'changed': True}
    elif mutation == 'order':
        State(head).config('occurrence:occ-0')['clips'].reverse()
    else:
        head['shots']['shot-fork'] = copy.deepcopy(next(iter(head['shots'].values())))
    before = copy.deepcopy(head)
    with pytest.raises(CommandError):
        execute(head, [call])
    assert head == before


@pytest.mark.parametrize('field', ['created', 'reads', 'bindings', 'expect'])
def test_forged_capture_refused(field):
    original = bundle_from_document(fork_document())
    call = fork(original)
    call[field] = [] if field in ('created', 'reads') else {}
    with pytest.raises(CommandError, match='capture'):
        execute(original, [call])


@pytest.mark.parametrize('args', [{}, {'shot_id': 'shot-fork', 'extra': True},
                                   {'shot_id': 'bad/id'}, {'shot_id': ''}])
def test_closed_registered_arguments(args):
    with pytest.raises(CommandError):
        validate_command({'schema_version': 1, 'op': 'occurrence.fork', 'version': '1',
                          'select': 'occurrence:occ-0', 'args': args})


def test_undo_refuses_aba_and_surviving_owner():
    original = bundle_from_document(fork_document())
    plan = execute(original, [fork(original)])
    output = bundle_from_document(document_from_bundle(plan.bundle))
    records = accepted(plan.records, original, output)
    with pytest.raises(CommandError, match='intervening'):
        inverse(output, records, [later('occurrence:occ-0', ['shot_id'], 'shot-fork')])
    head = copy.deepcopy(output)
    head['placements'].append({**copy.deepcopy(head['placements'][0]), 'occurrence_id': 'consumer'})
    with pytest.raises(CommandError) as error:
        inverse(head, records)
    assert error.value.kind == 'surviving_dependents'


def test_fork_keeps_nontrivial_source_map_and_raw_windows_verbatim():
    document = document_fixture()
    original = bundle_from_document(document)
    call = capture_occurrence_fork(original, 'occurrence:occ-a', 'shot-fork')
    output = document_from_bundle(execute(original, [call]).bundle)
    old = document['parent_revision']['payload']['occurrences']
    rows = output['parent_revision']['payload']['occurrences']
    assert rows[1] == old[1]
    assert rows[0] == {**old[0], 'shot_id': 'shot-fork', 'shot_revision_id': rows[0]['shot_revision_id']}
    assert all(row['payload'] == document['internal_timeline_revisions'][0]['payload']
               for row in output['internal_timeline_revisions'])


def test_generic_envelope_still_requires_registered_provider_handler(monkeypatch):
    from astrid.sdk.timeline import commands

    call = {'schema_version': 1, 'op': 'occurrence.fork', 'version': '1',
            'select': 'occurrence:occ-0', 'args': {'shot_id': 'shot-fork'}}
    assert commands._VALIDATOR.is_valid(call)
    monkeypatch.setattr(commands, 'operations', lambda: {})
    with pytest.raises(CommandError) as error:
        commands.validate_command(call)
    assert error.value.kind == 'unsupported_operation'
