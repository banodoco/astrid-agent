"""Real Runtime door coverage for the parent-track label handoff.

Run centrally with the ``parent-label-proof`` suite (current Runtime source).
"""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._track_commands import capture_track_set
from astrid.sdk.timeline.commit import bundle_from_document
from tests.editing_conformance import test_commit_runtime_m1 as m1

film, renderer_off, wire = m1.film, m1.renderer_off, m1.wire

pytestmark = pytest.mark.integration


def test_move_then_parent_label_reload_inverse_preserves_newer_disjoint_move(
    tmp_path, monkeypatch, film, renderer_off, real_runtime,
):
    from astrid.sdk.timeline.sync import TimelineSession

    fixture, original_parent, _ = film
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(tmp_path / 'client-data'))
    support = tmp_path / 'support'
    daemon = real_runtime(fixture.duplicate_root, support_root=support, authoring_profile='astrid')
    client = m1.open_client(daemon)
    outbox = tmp_path / 'parent-label-outbox.json'
    session = TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                   client=client, outbox_path=outbox)

    moved = session.move('parent/clip:music-bed', 1)
    m1.assert_move(original_parent, m1.parent_of(moved), 'music-bed', 1)
    m1.assert_readback(client, fixture, moved)
    moved_bundle = bundle_from_document(moved['data']['canonical_document'])
    assert State(moved_bundle).entity('parent/clip:music-bed')['at'] == 1
    label = session.queue({'schema_version': 1, 'op': 'track.set', 'version': '1',
                           'select': 'parent/track:music',
                           'args': {'values': {'label': 'Music'}, 'clear': []}})
    assert label == capture_track_set(moved_bundle, 'parent/track:music', values={'label': 'Music'})
    accepted = session.flush()
    assert json.loads(outbox.read_bytes())['pending'] == []
    assert accepted['data']['commands'] == [label]
    m1.assert_readback(client, fixture, accepted)
    accepted_bundle = bundle_from_document(accepted['data']['canonical_document'])
    assert State(accepted_bundle).entity('parent/track:music')['label'] == 'Music'
    assert State(accepted_bundle).entity('parent/clip:music-bed')['at'] == 1
    expected = copy.deepcopy(m1.parent_of(moved))
    next(row for row in expected['config']['tracks'] if row['id'] == 'music')['label'] = 'Music'
    assert m1.parent_of(accepted) == expected

    reloaded = TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                    client=client, outbox_path=tmp_path / 'reload-outbox.json')
    other = TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                 client=client, outbox_path=tmp_path / 'other-outbox.json')
    disjoint = other.move('parent/clip:music-other', 2)
    m1.assert_move(m1.parent_of(accepted), m1.parent_of(disjoint), 'music-other', 2)
    m1.assert_readback(client, fixture, disjoint)
    undone = reloaded.undo([accepted['receipt']['receipt_id']])
    m1.assert_readback(client, fixture, undone)
    document = undone['data']['canonical_document']
    restored = bundle_from_document(document)
    assert State(restored).entity('parent/clip:music-bed')['at'] == 1
    assert State(restored).entity('parent/clip:music-other')['at'] == 2
    assert 'label' not in State(restored).entity('parent/track:music')
    assert State(bundle_from_document(disjoint['data']['canonical_document'])).entity(
        'parent/clip:music-other')['at'] == 2
    expected = copy.deepcopy(m1.parent_of(disjoint))
    next(row for row in expected['config']['tracks'] if row['id'] == 'music').pop('label')
    assert m1.parent_of(undone) == expected
    # Only the authored parent revision changes; all immutable closures and
    # document/media pins survive the label, concurrent move and guarded undo.
    moved_document = moved['data']['canonical_document']
    for result in (accepted, disjoint, undone):
        current = result['data']['canonical_document']
        assert {key: value for key, value in current.items() if key != 'parent_revision'} == {
            key: value for key, value in moved_document.items() if key != 'parent_revision'}


def test_native_label_cli_lost_ack_then_python_suffix_keeps_original_claims(
    tmp_path, monkeypatch, film, renderer_off, wire, real_runtime, capsys,
):
    from astrid.sdk.timeline.sync import TimelineSession

    fixture, _, _ = film
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(tmp_path / 'client-data'))
    support = tmp_path / 'support'
    daemon = real_runtime(fixture.duplicate_root, support_root=support, authoring_profile='astrid')
    client = m1.open_client(daemon)
    observed, lose = wire
    lose.add('native-label-A')
    from astrid.packs.timeline.cli import build_parser

    parsed = build_parser(client).parse_args([
        'edit', 'track-label', fixture.timeline_id, '--project', fixture.project_id,
        '--track', 'parent/track:music', '--set', '', '--idempotency-key', 'native-label-A', '--json',
    ])
    assert parsed.handler(parsed) == 1
    assert not json.loads(capsys.readouterr().out)['ok']
    original = copy.deepcopy(observed[-1])
    assert original['status'] == 200
    empty_bundle = bundle_from_document(original['response']['data']['canonical_document'])
    session = TimelineSession.open(fixture.project_id, fixture.timeline_id, client=client)
    suffix = session.queue({'schema_version': 1, 'op': 'track.set', 'version': '1',
                            'select': 'parent/track:music',
                            'args': {'values': {}, 'clear': ['label']}})
    assert suffix == capture_track_set(empty_bundle, 'parent/track:music', clear=['label'])
    # A second author advances the head while A's acknowledgement is missing.
    other = TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                 client=client, outbox_path=tmp_path / 'peer.json')
    disjoint = other.move('parent/clip:music-other', 2)
    daemon.stop()
    daemon = real_runtime(fixture.duplicate_root, support_root=support, authoring_profile='astrid')
    client = m1.open_client(daemon)
    session = TimelineSession.open(fixture.project_id, fixture.timeline_id, client=client)
    retried = session.flush()
    assert observed[-1]['key'] == original['key']
    assert observed[-1]['body'] == original['body']
    assert retried['receipt'] == original['response']['receipt']
    cleared = session.flush()
    assert observed[-1]['key'] != 'native-label-A'
    body = json.loads(observed[-1]['body'])
    assert body['commands'] == [suffix]
    assert body['base_revision_id'] == retried['data']['accepted_revision_id']
    assert body['base_publication_id'] == retried['receipt']['receipt_id']
    assert cleared['data']['commands'] == [suffix]
    result = bundle_from_document(cleared['data']['canonical_document'])
    assert 'label' not in State(result).entity('parent/track:music')
    assert State(result).entity('parent/clip:music-other')['at'] == 2
    m1.assert_readback(client, fixture, cleared)
    undone = session.undo([cleared['receipt']['receipt_id']])
    result = bundle_from_document(undone['data']['canonical_document'])
    assert State(result).entity('parent/track:music')['label'] == ''
    assert State(result).entity('parent/clip:music-other')['at'] == 2
    assert disjoint['receipt']['receipt_id'] != undone['receipt']['receipt_id']
