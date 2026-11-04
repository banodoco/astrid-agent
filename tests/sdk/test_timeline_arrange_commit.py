"""Occurrence arrangement through function capture, publication and exact inverse."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from astrid.core.receipts.canonical import canonical_json
from astrid.core.receipts.contract import CommandReceipt
from astrid.core.timeline.duration import clip_timeline_duration
from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline import store
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commit import _digest, bundle_from_document, create_runtime_commit_service
from astrid.sdk.timeline.commit_session import Timeline
from astrid.sdk.timeline.model import Clip
from astrid.sdk.timeline.outbox import Outbox
from astrid.sdk.timeline.sync import TimelineSession
from tests.sdk.test_timeline_commit_provider import DetachedPort, DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def twenty_document():
    document = document_fixture(shared=False)
    parent = document['parent_revision']['payload']
    template = parent['occurrences'][0]
    parent['occurrences'] = []
    for index in range(20):
        row = copy.deepcopy(template)
        row.update(occurrence_id=f'occ-{index:02d}', duration_ms=1000 + index % 3 * 100,
                   source_offset={'start': index * 100, 'end': index * 100 + 1000 + index % 3 * 100})
        row['placement']['start_ms'] = 100000 + index * 2000
        parent['occurrences'].append(row)
    spare = copy.deepcopy(template)
    spare.update(occurrence_id='spare', duration_ms=1000)
    spare['placement']['start_ms'] = 90000
    parent['occurrences'].insert(7, spare)
    for rows in (parent['clips'], parent['config']['clips']):
        for index, clip in enumerate(rows):
            clip['at'] = 80 + index * 3
    document['parent_revision']['content_digest'] = _digest(parent)
    return document


@pytest.fixture
def arranged_session(tmp_path, monkeypatch):
    """Fake the transport only; all capture, provider and inverse code is real."""
    realm = DetachedRealm(twenty_document())
    calls = []
    identity = {'realm_id': 'realm-1', 'actor_id': 'actor-1'}

    def send(project, timeline, body, *, idempotency_key):
        assert (project, timeline) == ('project-1', 'film')
        calls.append(copy.deepcopy(body))
        base = {'revision_id': body['base_revision_id'], 'publication_id': body['base_publication_id']}
        context = realm.context(body['commands'], base=base)
        # DetachedPort expects the optional diagnostic rationale in its request.
        context['request'] = {'rationale': 'Arrange exact occurrences', **copy.deepcopy(body)}
        context['idempotency_key'] = idempotency_key
        invocation = {'operation': 'commitTimeline', 'path': '/v1/projects/project-1/timelines/film/commit',
                      'publisher': identity['actor_id'], 'request': body}
        context['request_hash'] = hashlib.sha256(canonical_json(invocation).encode()).hexdigest()
        result = create_runtime_commit_service().commit(context, DetachedPort(realm, context))
        # The provider fixture records one sequence number; public receipts use
        # an inclusive first/last pair even when there is only one event.
        result['receipt']['project_seq'] *= 2
        realm.entries[-1]['receipt'] = copy.deepcopy(result['receipt'])
        return DomainResult.success(result['data'], receipt=CommandReceipt.from_dict(result['receipt']))

    def fetch(project, timeline, *, revision_id, publication_id=None, client=None):
        assert (project, timeline) == ('project-1', 'film')
        assert any(entry['revision_id'] == revision_id and entry['publication_id'] == publication_id
                   for entry in realm.entries)
        bundle = bundle_from_document(realm.documents[revision_id])
        bundle['base_parent']['publication_id'] = publication_id
        return bundle

    monkeypatch.setattr(store, 'fetch_bundle', fetch)
    client = SimpleNamespace(
        _remote=SimpleNamespace(_transport=SimpleNamespace(_last_handshake=identity)),
        timelines=SimpleNamespace(commit=send))
    outbox = Outbox(tmp_path / 'outbox.json')
    outbox.write({'scope': {**identity, 'project_id': 'project-1', 'timeline_id': 'film'},
                  'base': copy.deepcopy(realm.head), 'pending': [], 'flight': None})
    session = TimelineSession(client, outbox, identity, 'project-1', 'film')
    bundle = fetch('project-1', 'film', **realm.head)
    return Timeline(session, bundle, realm.head), realm, calls


@pytest.mark.parametrize('gap', [0, 0.125])
def test_arrange_twenty_in_one_function_commit_and_exact_undo(arranged_session, gap):
    tl, realm, calls = arranged_session
    original = copy.deepcopy(realm.documents[realm.head['revision_id']])
    before = original['parent_revision']['payload']
    order = [f'occ-{index:02d}' for index in reversed(range(20))]
    result = tl.commit(lambda t: t.arrange(order, at=0, gap=gap))
    assert len(calls) == len(realm.accepted) == 1
    assert calls[0]['merge'] == 'atomic' and 'reads' not in calls[0]
    assert all(command['op'] == 'doc.patch' for command in calls[0]['commands'])
    landed = result['data']['canonical_document']
    after = landed['parent_revision']['payload']
    assert [row['occurrence_id'] for row in after['occurrences'] if row['occurrence_id'] != 'spare'] == order
    old_rows = {row['occurrence_id']: row for row in before['occurrences']}
    cursor = 0
    for row in after['occurrences']:
        previous = old_rows[row['occurrence_id']]
        if row['occurrence_id'] == 'spare':
            assert row == previous
            continue
        assert row == {**previous, 'placement': {**previous['placement'], 'start_ms': cursor}}
        cursor += row['duration_ms'] + gap * 1000
    assert {key: value for key, value in after.items() if key != 'occurrences'} == {
        key: value for key, value in before.items() if key != 'occurrences'}
    for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
        assert landed[key] == original[key]
    undone = tl.undo(publications=[result['receipt']['receipt_id']])
    assert len(calls) == len(realm.accepted) == 2
    assert undone['data']['canonical_document'] == original


def test_sequential_twenty_occurrence_setup_then_function_arrange_and_exact_undo(arranged_session):
    tl, realm, calls = arranged_session
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create

    from_bundle = copy.deepcopy(tl.bundle)
    births = []
    for index in range(20):
        command = capture_occurrence_create(
            from_bundle, f'new-occ-{index:02d}', f'new-shot-{index:02d}',
            name=f'New shot {index:02d}', track='picture', on=index * 3, duration=2)
        births.append(command)
        from_bundle = execute(from_bundle, [command]).bundle

    setup = tl.commit(births, label='Create twenty occurrences')
    assert setup['receipt']['receipt_id']
    assert len(calls) == len(realm.accepted) == 1
    before_arrange = copy.deepcopy(realm.documents[realm.head['revision_id']])
    order = [f'new-occ-{index:02d}' for index in reversed(range(20))]

    arranged = tl.commit(lambda t: t.arrange(order, at=200, gap=0.25), label='Arrange occurrences')
    assert len(calls) == len(realm.accepted) == 2
    after = arranged['data']['canonical_document']['parent_revision']['payload']['occurrences']
    arranged_rows = [row for row in after if row['occurrence_id'] in set(order)]
    assert [row['occurrence_id'] for row in arranged_rows] == order
    assert [row['placement']['start_ms'] for row in arranged_rows] == [200000 + i * 2250 for i in range(20)]

    undone = tl.undo(publications=[arranged['receipt']['receipt_id']])
    assert len(calls) == len(realm.accepted) == 3
    assert undone['data']['canonical_document'] == before_arrange


def test_arrange_composes_with_free_edits_and_another_arrangement(arranged_session):
    tl, realm, calls = arranged_session
    original = copy.deepcopy(realm.documents[realm.head['revision_id']])

    def edit(t):
        t.bundle['parent']['name'] = 'Arranged film'
        t.arrange(['occ-00', 'occ-01'], at=0)
        t.arrange(['occ-01', 'occ-00'], at=4, gap=0.25)
        t.bundle['parent']['unknown']['arranged'] = True

    result = tl.commit(edit)
    assert len(calls) == len(realm.accepted) == 1
    payload = result['data']['canonical_document']['parent_revision']['payload']
    assert payload['name'] == 'Arranged film' and payload['unknown']['arranged'] is True
    rows = {row['occurrence_id']: row for row in payload['occurrences']}
    assert rows['occ-01']['placement']['start_ms'] == 4000
    assert rows['occ-00']['placement']['start_ms'] == 5350
    undone = tl.undo(publications=[result['receipt']['receipt_id']])
    assert undone['data']['canonical_document'] == original


def test_split_in_one_function_commit_and_immediate_undo(arranged_session):
    tl, realm, calls = arranged_session
    original = copy.deepcopy(realm.documents[realm.head['revision_id']])

    def split(t):
        row = next(c for c in t.bundle['parent']['clips'] if c['id'] == 'clip-a')
        clip = Clip(t, 'parent', row)
        clip.split(81, id='right')

    result = tl.commit(split)

    assert len(calls) == len(realm.accepted) == 1
    assert [command['op'] for command in calls[0]['commands']] == ['clip.split']
    payload = result['data']['canonical_document']['parent_revision']['payload']
    left, right = payload['clips'][:2]
    assert [left['id'], right['id']] == ['clip-a', 'right']
    assert (left['at'], left['hold']) == (80, 1)
    assert (right['at'], right['hold']) == (81, 1)
    assert clip_timeline_duration(left) == clip_timeline_duration(right) == 1
    undone = tl.undo(publications=[result['receipt']['receipt_id']])
    assert len(calls) == len(realm.accepted) == 2
    assert undone['data']['canonical_document'] == original


@pytest.mark.parametrize('damage', ['unknown-duration', 'unselected-collision'])
def test_arrange_function_refusal_has_no_partial_writes(arranged_session, damage):
    tl, realm, calls = arranged_session
    if damage == 'unknown-duration':
        document = realm.documents[realm.head['revision_id']]
        document['parent_revision']['payload']['occurrences'][-1].pop('duration_ms')
        document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    original = copy.deepcopy((tl.bundle, realm.head, realm.documents, realm.entries))
    outbox_before = json.loads(tl.session.outbox.path.read_text())
    with pytest.raises(TimelineEditError, match='duration|collid|overlap'):
        tl.commit(lambda t: t.arrange(['occ-00', 'occ-19'], at=90 if damage == 'unselected-collision' else 0))
    assert (tl.bundle, realm.head, realm.documents, realm.entries) == original
    assert calls == [] and realm.accepted == []
    assert json.loads(tl.session.outbox.path.read_text()) == outbox_before
