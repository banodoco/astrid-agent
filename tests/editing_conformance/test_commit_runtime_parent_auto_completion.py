"""Parent automatic completion through the normally generated whole private S3 pair.

In-process Runtime only: native creation, real managed outputs, current provider,
normal validation, reload and guarded history. No legacy baseline publication.
"""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline import commands
from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params, capture_track_set
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.native_auto_placement import auto_place_completion
from tests.editing_conformance import test_commit_runtime_parent_media as parent
from tests.editing_conformance.test_next_command_cohort_runtime import reopen

pytestmark = pytest.mark.integration


@pytest.fixture(params=['image', 'video'])
def editing(request, tmp_path, monkeypatch):
    realm = parent.media_realm(tmp_path, monkeypatch, request.param, historical=False, pristine=True)
    try:
        editing = next(realm)
        from astrid.sdk.timeline.emitter import emit_document_commands

        before = bundle_from_document(parent.current(editing))
        after = copy.deepcopy(before)
        after['parent']['config']['tracks'].append({
            'id': 'visual', 'kind': 'visual', 'role': 'output', 'opaque': [None, False],
        })
        commands_to_apply = emit_document_commands(before, after)
        request_body = parent.body(editing, commands_to_apply)
        request_body.update(
            label='parent auto fixture', merge='atomic', reads=[],
            rationale='Parent auto fixture bootstrap', group_id='parent-auto-fixture',
            steps=[{
                'id': 'parent-auto-fixture', 'label': 'parent auto fixture',
                'intent': 'Prepare parent auto fixture', 'tags': ['integration'],
                'command_ordinals': list(range(len(commands_to_apply))),
                'patch': [change for command in commands_to_apply
                          for change in command['args']['changes']],
                'reads': [],
            }],
        )
        parent.commit(editing, request_body, 'parent-auto-fixture')
        yield editing
    finally:
        realm.close()


def completion(editing, policy):
    # A normal dry-run obtains the exact detached VerifiedMedia facts from Runtime.
    parent.commit(editing, parent.body(editing, [parent.birth(editing)], dry_run=True), 'media-preview')
    original = bundle_from_document(parent.current(editing))
    result = auto_place_completion(original, 'parent', 'completed', editing.pins['new'],
                                   ownership='parent', track='visual', verified_media=editing.selected,
                                   **policy)
    return parent.body(editing, result['commands']), result['capture']


def anchor(editing):
    value = parent.row(editing, 'anchor', asset='anchor-source')
    value.update(at=8, hold=2.5)
    if editing.kind == 'video':
        value.update({'from': 0, 'to': 2.5, 'speed': 1})
    parent.commit(editing, parent.body(editing, [parent.birth(editing, value, editing.pins['old'])]), 'anchor')


def guards(monkeypatch):
    from runtime_protocol.service import RuntimeService

    def unavailable(*args, **kwargs):
        pytest.fail('completion used legacy publication or renderer')

    for name in ('publish_parent_composition', 'publish_timeline_render', 'update_timeline',
                 'replace_timeline_clip', 'replace_parent_composition_media'):
        if hasattr(RuntimeService, name):
            monkeypatch.setattr(RuntimeService, name, unavailable)
    import runtime_protocol.service as service_module
    import runtime_protocol.visual_seam as visual_seam
    monkeypatch.setattr(service_module, 'evaluate_closure', unavailable)
    monkeypatch.setattr(visual_seam, 'evaluate_closure', unavailable)


@pytest.mark.parametrize('mode', ['anchor', 'fallback', 'lane-end'])
def test_disjoint_head_accept_reload_exact_retry_undo_redo_and_reserved_id(editing, monkeypatch, mode):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_validation import validate_commit_value

    guards(monkeypatch)
    anchor(editing)
    policy = {'anchor': 'anchor', 'preferred_track': 'visual'} if mode == 'anchor' else (
        {'anchor': 'absent', 'fallback_at': 22.25, 'fallback_track': 'visual'} if mode == 'fallback' else {})
    request, capture = completion(editing, policy)
    validate_commit_value(request, 'CommitRequest')
    assert capture['media'] == editing.pins['new']
    assert capture['seconds'] == 5
    assert capture['resolution'] == {'anchor': 'anchor-after', 'fallback': 'fallback-at',
                                     'lane-end': 'lane-end'}[mode]
    [command] = request['commands']
    assert set(command['args']) == {'row', 'media'} and command['op'] == 'clip.add-media'
    changed = capture_clip_set_params(bundle_from_document(parent.current(editing)),
                                     'parent/clip:anchor', values={'later': [None, False, {'custom': 1}]})
    parent.commit(editing, parent.body(editing, [changed]), 'disjoint')
    baseline = parent.current(editing)
    before = parent.rows(editing)
    preview = parent.commit(editing, dict(request, dry_run=True), 'completion-preview')
    assert parent.rows(editing) == before
    result = parent.commit(editing, request, 'complete')
    document = parent.current(editing)
    assert result['data']['canonical_document'] == document == preview['data']['canonical_document']
    placed = State(bundle_from_document(document)).entity('parent/clip:completed')
    assert placed == command['args']['row']
    assert placed['at'] == capture['on']
    assert State(bundle_from_document(document)).entity('parent/clip:anchor')['params']['later'] == [
        None, False, {'custom': 1}]
    binding = document['parent_revision']['payload']['registry']['assets'][editing.pins['new']['asset']]
    assert binding['variantId'] == editing.pins['new']['variant_id']
    assert binding['media_id'] == editing.pins['new']['object_id']
    history = editing.service.list_timeline_commits(editing.project, 'film', authorize=editing.authorize)
    reopen(editing)
    assert parent.current(editing) == document
    with monkeypatch.context() as retry:
        retry.setattr(commands, 'execute', lambda *a, **kw: pytest.fail('retry executed semantics'))
        assert parent.commit(editing, request, 'complete') == result
    assert editing.service.list_timeline_commits(editing.project, 'film', authorize=editing.authorize) == history
    undone = parent.undo(editing, result, 'undo-complete')
    assert parent.current(editing) == undone['data']['canonical_document'] == baseline
    before = parent.rows(editing)
    with pytest.raises(RuntimeErrorBase) as error:
        parent.commit(editing, parent.body(editing, [command]), 'retired-reservation')
    assert error.value.details['reason'] == 'identity_collision'
    assert parent.rows(editing) == before
    redone = parent.undo(editing, undone, 'redo-complete')
    assert parent.current(editing) == redone['data']['canonical_document'] == document
    assert editing.service.store.integrity_report()['ok']


@pytest.mark.parametrize('change', ['at', 'hold', 'track-label'])
def test_relevant_stale_reads_refuse_before_accept_and_any_persistence(editing, monkeypatch, change):
    from runtime_protocol.errors import RuntimeErrorBase

    guards(monkeypatch)
    anchor(editing)
    request, _ = completion(editing, {'anchor': 'anchor'})
    bundle = bundle_from_document(parent.current(editing))
    if change == 'at':
        changed = capture_move(bundle, 'parent/clip:anchor', 9)
    elif change == 'hold':
        changed = capture_clip_set(bundle, 'parent/clip:anchor', values={'hold': 2})
    else:
        changed = capture_track_set(bundle, 'parent/track:visual', values={'label': 'changed saved lane'})
    parent.commit(editing, parent.body(editing, [changed]), 'relevant')
    editing.candidates.clear()
    before, document = parent.rows(editing), copy.deepcopy(parent.current(editing))
    objects = {str(path.relative_to(editing.root)): path.read_bytes()
               for path in editing.root.rglob('*') if path.is_file() and
               any(part in ('cas', 'staging', 'snapshots') for part in path.parts)}
    assert objects
    traced = []
    editing.service.store.conn.set_trace_callback(traced.append)
    try:
        with pytest.raises(RuntimeErrorBase) as error:
            parent.commit(editing, request, 'stale-complete')
    finally:
        editing.service.store.conn.set_trace_callback(None)
    assert not [sql for sql in traced if sql.lstrip().split(' ', 1)[0].upper() in
                ('INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'DROP', 'ALTER')]
    expected_reason = 'value_conflict'
    assert error.value.details['reason'] == expected_reason
    assert editing.candidates == [] and parent.rows(editing) == before
    assert parent.current(editing) == document
    assert {str(path.relative_to(editing.root)): path.read_bytes()
            for path in editing.root.rglob('*') if path.is_file() and
            any(part in ('cas', 'staging', 'snapshots') for part in path.parts)} == objects


def test_completed_variant_is_exact_even_after_a_new_primary_is_promoted(editing, monkeypatch):
    guards(monkeypatch)
    completed = copy.deepcopy(editing.pins['old'])
    latest = parent.promote_latest_variant(editing)
    assert latest['new_primary_variant_id'] != completed['variant_id']
    editing.pins['new'] = completed
    request, capture = completion(editing, {'anchor': 'absent', 'fallback_at': 22.25})
    assert capture['media'] == completed
    result = parent.commit(editing, request, 'complete-old-variant')
    binding = result['data']['canonical_document']['parent_revision']['payload']['registry']['assets'][
        completed['asset']]
    assert (binding['variantId'], binding['media_id']) == (completed['variant_id'], completed['object_id'])
    assert binding['variantId'] != latest['new_primary_variant_id']
