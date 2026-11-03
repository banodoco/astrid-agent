"""Ordinary CLI literal edits through the pinned Runtime's real HTTP handlers and disk.

Uses the existing in-memory HTTP transport: no sockets or daemon substitute.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.core.timeline.authoring_bundle import open_authoring_bundle
from astrid.packs.timeline.cli import build_parser
from astrid.sdk.timeline._clip_commands import capture_clip_add
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._trim_commands import capture_clip_trim
from astrid.sdk.timeline.capture import capture_move, capture_track_create
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.native_parent_media_commands import capture_clip_add_media
from astrid.sdk.timeline.sync import TimelineSession
from tests.editing_conformance.test_native_batch_ingress import runtime  # noqa: F401

pytestmark = pytest.mark.integration
REF = 'parent/clip:literal'
VIDEO_BYTES = (
    Path(__file__).resolve().parents[1] / 'fixtures' / 'reshape' / 'hype_regression' / 'main.mp4'
).read_bytes()

# D103 setup audit: the three live sequences keep their authored timing and
# inputs.  In exact rational seconds, the source window is [1, 6), the initial
# playback end is 0 + (6 - 1) / 2 = 5/2, trim-A reaches
# (5000001/1000000 - 1) / 2 = 4000001/2000000, the dependent trim reaches
# (4 - 1) / 2 = 3/2, the relevant peer trim reaches (3 - 1) / 2 = 1, and
# the move sequence reaches 3 + 5/2 = 11/2.  The retained text title ends at
# 5 + 1 = 6; app.astrid.for=2.5 is the same exact 5/2 duration.  The tracked
# authentic main.mp4 fixture is declared as 42 s in
# tests/fixtures/reshape/hype_regression/media_manifest.json, so its genuine
# duration contains every source-window and occurrence end without rounding,
# clamping, epsilon, or a changed authored bound.


def run(world, capsys, *args):
    parsed = build_parser(world.client()).parse_args([*args, '--project', world.project, '--json'])
    code = parsed.handler(parsed)
    output = capsys.readouterr()
    return code, json.loads(output.out)


@pytest.fixture
def film(runtime):  # noqa: F811
    client = runtime.client()
    service = runtime.service()
    imported = service.import_media(
        runtime.project, VIDEO_BYTES, media_type='video/mp4', original_name='picture.mp4',
        actor_id='cli-trim-fixture', width=1920, height=1080, duration_seconds=42.0,
        idempotency_key='picture',
    )['data']
    selection = service.get_project_media_selection(runtime.project, {
        'asset': imported['variant_id'], 'generation_id': imported['generation_id'],
        'variant_id': imported['variant_id'],
    }, authorize=lambda: None)
    verified = service.get_project_verified_media(runtime.project, selection, authorize=lambda: None)
    assert verified['selection'] == selection

    service.create_timeline(runtime.project, 'trim-film', idempotency_key='trim-timeline')
    session = TimelineSession.open(runtime.project, 'trim-film', client=client)
    bundle = session._bundle(session.outbox.read())
    output = {'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
              'args': {'changes': [{
                  'op': 'set', 'ref': 'parent', 'path': ['config', 'output'],
                  'before': {'state': 'absent'}, 'value': {'fps': 30},
              }]}}
    configured = session.commit(output, idempotency_key='trim-output')
    bundle = bundle_from_document(configured['data']['canonical_document'])
    tracked = session.commit(capture_track_create(bundle, 'parent', {
        'id': 'picture', 'kind': 'visual',
    }), idempotency_key='trim-track')
    bundle = bundle_from_document(tracked['data']['canonical_document'])
    row = {
        'id': 'literal', 'track': 'picture', 'clipType': 'media', 'asset': selection['asset'],
        'at': 0, 'from': 1, 'to': 6, 'hold': 5, 'speed': 2,
        'app': {'astrid': {'opaque': [None, False]}, 'extension': {'keep': True}},
    }
    added = session.commit(capture_clip_add_media(bundle, row, selection, verified_media=[verified]),
                           idempotency_key='trim-clip', verified_media=[verified])
    bundle = bundle_from_document(added['data']['canonical_document'])
    child_timing = {'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
                    'args': {'changes': [{
                        'op': 'set', 'ref': 'parent',
                        'path': ['clips', 'id:literal', 'app', 'astrid', 'for'],
                        'before': {'state': 'absent'}, 'value': 2.5,
                    }]}}
    timed = session.commit(child_timing, idempotency_key='trim-clip-timing')
    bundle = bundle_from_document(timed['data']['canonical_document'])
    assert State(bundle).registry('parent')['assets'][selection['asset']]['duration'] == 42
    title = {'id': 'title', 'track': 'picture', 'clipType': 'text', 'at': 5, 'hold': 1,
             'params': {'text': 'Keep me'}}
    session.commit(capture_clip_add(bundle, 'parent', title), idempotency_key='trim-title')
    runtime.restart()  # Reopen the current semantic bootstrap through the provider.
    runtime.history = lambda: client.timelines.history(runtime.project, 'trim-film', limit=100).data
    return runtime


@pytest.mark.parametrize('relevant_peer', [False, True], ids=['disjoint-rebase-undo', 'relevant-conflict'])
def test_cli_trim_exact_retry_restart_dependent_suffix_and_peer(film, tmp_path, capsys, relevant_peer):
    world = film
    client = world.client()
    session = TimelineSession.open(world.project, 'trim-film', client=client)
    original = session._bundle(session.outbox.read())
    expected_a = capture_clip_trim(original, REF, out_s=5.000001)
    args = ('edit', 'trim-film', '--clip', 'lit', '--trim-out', '5.000001', '--idempotency-key', 'cli-A')
    world.lost.add('cli-A')
    code, error = run(world, capsys, *args)
    assert code == 1 and not error['ok']
    first = copy.deepcopy(world.calls[-1])
    flight = json.loads(session.outbox.path.read_bytes())['flight']
    assert (flight['body'].encode(), flight['key']) == first[:2]
    assert json.loads(first[0])['commands'] == [expected_a]
    accepted_a = first[3]
    document_a = accepted_a['data']['canonical_document']
    actual_a = State(bundle_from_document(document_a)).entity(REF)
    assert actual_a == State(execute(original, [expected_a]).bundle).entity(REF)
    assert actual_a['to'] <= 5.000001
    b = session.queue({'schema_version': 1, 'op': 'clip.trim', 'version': '1',
                       'select': REF, 'args': {'in_s': None, 'out_s': 4}})
    assert b == capture_clip_trim(bundle_from_document(document_a), REF, out_s=4)
    assert json.loads(session.outbox.path.read_bytes())['pending'] == [expected_a, b]
    peer = TimelineSession.open(world.project, 'trim-film', client=client, outbox_path=tmp_path / 'peer.json')
    if relevant_peer:
        peer.trim(REF, out_s=3)
    else:
        peer.commit({'schema_version': 1, 'op': 'track.set', 'version': '1',
                     'select': 'parent/track:picture', 'args': {'values': {'label': 'Peer'}, 'clear': []}})
    history_before = copy.deepcopy(world.history())
    world.restart()
    code, retried = run(world, capsys, *args)
    assert code == 0 and retried['receipt'] == accepted_a['receipt']
    assert world.calls[-1][:2] == first[:2]
    assert world.calls[-1][3] == first[3]
    assert world.history() == history_before
    client = world.client()
    opened = client.timelines.open_bundle(world.project, 'trim-film', revision_id=document_a['parent_revision']['revision_id'])
    assert opened.ok and opened.data['bundle'] == open_authoring_bundle(
        {**document_a['parent_revision'], 'project_id': world.project, 'timeline_id': 'trim-film'},
        shot_revisions=document_a['shot_revisions'],
        internal_timeline_revisions=document_a['internal_timeline_revisions'])
    stored = client._remote._transport.get_project_parent_composition_revision(
        world.project, 'trim-film', document_a['parent_revision']['revision_id'])
    assert all(stored[key] == value for key, value in document_a['parent_revision'].items())
    assert stored['dependency_manifest'] == document_a['dependency_manifest']
    pending = json.loads(session.outbox.path.read_bytes())
    assert pending['pending'] == [b] and pending['flight'] is None
    assert pending['base'] == {'revision_id': accepted_a['data']['accepted_revision_id'],
                               'publication_id': accepted_a['receipt']['receipt_id']}
    code, result = run(world, capsys, 'edit', 'trim-film', '--clip', 'literal', '--trim-out', '4')
    assert json.loads(world.calls[-1][0])['commands'] == [b]
    assert world.calls[-1][1] != first[1]
    if relevant_peer:
        assert code == 1 and not result['ok']
        refused = json.loads(session.outbox.path.read_bytes())
        assert refused['pending'] == [b] and refused['conflict']['details']['status'] == 'conflict'
        assert world.history() == history_before
    else:
        assert code == 0 and result['data']['status'] == 'merged'
        bundle_b = bundle_from_document(result['data']['canonical_document'])
        assert State(bundle_b).entity(REF)['to'] == 4
        assert State(bundle_b).entity('parent/track:picture')['label'] == 'Peer'
        assert State(bundle_b).entity('parent/clip:title') == State(original).entity('parent/clip:title')
        code, undone = run(world, capsys, 'undo', 'trim-film', '--commit', result['receipt']['receipt_id'])
        assert code == 0
        undone_bundle = bundle_from_document(undone['data']['canonical_document'])
        assert State(undone_bundle).entity(REF) == actual_a
        assert State(undone_bundle).entity('parent/track:picture')['label'] == 'Peer'


def test_cli_move_exact_retry_restart_and_undo_over_real_http(film, capsys):
    world = film
    session = TimelineSession.open(world.project, 'trim-film', client=world.client())
    original = session._bundle(session.outbox.read())
    expected = capture_move(original, REF, 3)
    args = ('edit', 'trim-film', '--clip', 'lit', '--on', '3', '--idempotency-key', 'cli-move-A')

    world.lost.add('cli-move-A')
    code, failure = run(world, capsys, *args)
    assert code == 1 and not failure['ok']
    first = copy.deepcopy(world.calls[-1])
    flight = json.loads(session.outbox.path.read_bytes())['flight']
    assert (flight['body'].encode(), flight['key']) == first[:2]
    assert json.loads(first[0])['commands'] == [expected]
    accepted = first[3]
    history_after_accept = copy.deepcopy(world.history())

    world.restart()
    code, retry = run(world, capsys, *args)
    assert code == 0 and retry['receipt'] == accepted['receipt']
    assert world.calls[-1][:2] == first[:2]
    assert world.calls[-1][3] == accepted
    assert world.history() == history_after_accept

    code, undone = run(world, capsys, 'undo', 'trim-film',
                       '--commit', accepted['receipt']['receipt_id'])
    assert code == 0
    restored = bundle_from_document(undone['data']['canonical_document'])
    assert State(restored).entity(REF) == State(original).entity(REF)
