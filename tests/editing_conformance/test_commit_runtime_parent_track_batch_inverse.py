"""Real Runtime coverage for a grouped public track birth and parent move."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.capture import capture_move, capture_track_create
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.sync import TimelineSession
from tests.editing_conformance import test_commit_runtime_m1 as m1

film, renderer_off, wire = m1.film, m1.renderer_off, m1.wire

pytestmark = pytest.mark.integration


def test_grouped_parent_track_create_move_reload_undo_redo(
    tmp_path, film, renderer_off, real_runtime,
):
    fixture, _, _ = film
    daemon = real_runtime(fixture.duplicate_root, support_root=tmp_path / 'support',
                          authoring_profile='astrid')
    client = m1.open_client(daemon)
    opened = client.timelines.open_bundle(fixture.project_id, fixture.timeline_id)
    assert opened.ok, opened.as_dict()
    baseline = opened.data['bundle']
    baseline_document = daemon.service._timeline_commits()[0].revision_closure(
        fixture.project_id, fixture.timeline_id, opened.data['revision_id'])

    create = capture_track_create(baseline, 'parent', {
        'id': 'V3', 'kind': 'visual', 'role': 'output', 'active': True,
        'opaque': [None, False],
    })
    staged = execute(baseline, [create])
    move = capture_move(staged.bundle, 'parent/clip:music-bed', 1)

    session = TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                   client=client, outbox_path=tmp_path / 'batch.json')
    accepted = session.commit_many([create, move], idempotency_key='parent-track-batch')
    assert accepted['data']['commands'] == [create, move]
    m1.assert_readback(client, fixture, accepted)
    accepted_document = copy.deepcopy(accepted['data']['canonical_document'])
    accepted_state = State(bundle_from_document(accepted_document))
    assert accepted_state.entity('parent/track:V3')['opaque'] == [None, False]
    assert accepted_state.entity('parent/clip:music-bed')['at'] == 1

    reloaded = TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                    client=client, outbox_path=tmp_path / 'reload.json')
    undone = reloaded.undo([accepted['receipt']['receipt_id']])
    m1.assert_readback(client, fixture, undone)
    assert undone['data']['canonical_document'] == baseline_document

    redone = reloaded.undo([undone['receipt']['receipt_id']])
    m1.assert_readback(client, fixture, redone)
    assert redone['data']['canonical_document'] == accepted_document
