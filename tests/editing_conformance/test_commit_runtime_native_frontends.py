"""Composed native frontends on the existing renderer-off HTTP/Runtime fixture.

Run only through the central test service with a deliberately selected Runtime.
"""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.sync import CommitConflict, TimelineSession
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline_sheet import NativeSheet, SheetError
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.editing_conformance import test_commit_runtime_m1 as m1
from tests.editing_conformance import test_commit_runtime_native_checkout as checkout
from tests.sdk.test_timeline_sheet_native import edit_cell

base_film, film = checkout.base_film, checkout.film
renderer_off, wire = checkout.renderer_off, checkout.wire
pytestmark = pytest.mark.integration
REF = 'parent/clip:music-bed'


@pytest.mark.parametrize('relevant', [False, True])
def test_sheet_lost_ack_attached_suffix_restart_exact_retry_and_peer_guards(
        tmp_path, film, wire, real_runtime, monkeypatch, relevant):
    fixture, _, _ = film
    observed, lose = wire
    path = tmp_path / 'composed-outbox.json'
    support = tmp_path / 'support'

    def start(port=0):
        return real_runtime(fixture.duplicate_root, support_root=support, port=port, authoring_profile='astrid')

    def session(client):
        return TimelineSession.open(fixture.project_id, fixture.timeline_id, client=client, outbox_path=path)

    daemon = start()
    client = m1.open_client(daemon)
    original = session(client)
    stale = Checkout.attach(original)
    sheet = NativeSheet.export(original)
    draft = tmp_path / 'mixed.sheet'
    draft.write_text(edit_cell(sheet.text, REF, '1'))
    draft_bytes = draft.read_bytes()
    lose.add('sheet-A')
    with pytest.raises(WorkspaceClientError):
        sheet.apply(draft.read_text(), session=original, dry_run=False, idempotency_key='sheet-A')
    accepted_a = copy.deepcopy(observed[-1]['response'])
    flight = json.loads(path.read_bytes())['flight']
    before = path.read_bytes()
    with pytest.raises(TimelineEditError, match='basis changed'):
        stale.flush()
    assert path.read_bytes() == before
    tl = Checkout.attach(original)
    tl.clip(REF).trim(1, 4)
    suffix = json.loads(path.read_bytes())['pending'][1]
    assert suffix['op'] == 'clip.trim' and suffix['reads'] and suffix['bindings']
    assert tl.clip(REF).start == 1 and tl.clip(REF).data['hold'] == 3
    peer = Checkout.attach(TimelineSession.open(fixture.project_id, fixture.timeline_id,
        client=client, outbox_path=tmp_path / 'peer.json'))
    peer.clip('music-other').on(2)
    peer.flush()
    if relevant:
        peer.clip(REF).trim(0, 5)
        peer.flush()
    peer_head = copy.deepcopy(m1.history(client, fixture)['head'])
    port = daemon.httpd.server_port
    daemon.stop()
    daemon = start(port)
    client = m1.open_client(daemon)
    restarted = session(client)
    assert json.loads(path.read_bytes())['flight'] == flight
    reads = len(observed)

    def forbidden(*args, **kwargs):
        pytest.fail('sheet retry fetched a new bundle or recaptured intent')

    with monkeypatch.context() as patch:
        patch.setattr(TimelineSession, '_bundle', forbidden)
        retried = NativeSheet.load(restarted).apply(draft.read_text(), session=restarted, dry_run=False)
    assert len(observed) == reads + 1
    assert observed[-1]['response'] == accepted_a
    assert retried['receipt'] == accepted_a['receipt']
    assert observed[-1]['body'] == flight['body'].encode() and observed[-1]['key'] == flight['key']
    assert m1.history(client, fixture)['head'] == peer_head
    state = json.loads(path.read_bytes())
    assert state['pending'] == [suffix] and state['flight'] is None
    assert state['base'] == {'revision_id': accepted_a['data']['accepted_revision_id'],
                             'publication_id': accepted_a['receipt']['receipt_id']}
    preview = copy.deepcopy(tl.bundle)
    for action in (lambda: tl.clip(REF).on(3), lambda: tl.flush()):
        with pytest.raises(TimelineEditError, match='basis changed'):
            action()
        assert json.loads(path.read_bytes()) == state and tl.bundle == preview
    tl = Checkout.attach(restarted)
    from runtime_protocol.server import RuntimeHandler

    requests = []
    original_commit = RuntimeHandler._timeline_commit

    def observe_request(handler, *args, **kwargs):
        # The existing lost-ACK recorder observes successful deferred responses;
        # record entry too, since guard conflicts leave that deferred path.
        requests.append((handler._prefetched_body, handler.headers['Idempotency-Key']))
        return original_commit(handler, *args, **kwargs)

    monkeypatch.setattr(RuntimeHandler, '_timeline_commit', observe_request)
    if relevant:
        with pytest.raises(CommitConflict):
            tl.flush()
        assert json.loads(path.read_bytes())['pending'] == [suffix]
        assert json.loads(path.read_bytes())['conflict']
        assert m1.history(client, fixture)['head'] == peer_head
        assert tl.clip(REF).data['from'] == 1 and tl.clip(REF).data['hold'] == 3
    else:
        result = tl.flush()
        assert result['data']['status'] == 'rebased' and result['data']['commands'] == [suffix]
        assert tl.clip(REF).data['from'] == 1 and tl.clip(REF).data['hold'] == 3
        assert tl.clip('music-other').start == 2
    assert len(requests) == 1
    sent = json.loads(requests[0][0])
    assert sent['commands'] == [suffix] and requests[0][1] != flight['key']
    assert sent['base_revision_id'] == accepted_a['data']['accepted_revision_id']
    assert sent['base_publication_id'] == accepted_a['receipt']['receipt_id']
    assert draft.read_bytes() == draft_bytes


def test_checkout_first_refuses_sheet_suffix_keeps_draft_and_pending_intent(
        tmp_path, film, wire, real_runtime):
    fixture, _, _ = film
    observed, lose = wire
    daemon = real_runtime(fixture.duplicate_root, support_root=tmp_path / 'support', authoring_profile='astrid')
    client = m1.open_client(daemon)
    session = TimelineSession.open(fixture.project_id, fixture.timeline_id, client=client,
                                   outbox_path=tmp_path / 'reverse-outbox.json')
    sheet = NativeSheet.export(session)
    draft = tmp_path / 'reverse.sheet'
    draft.write_text(edit_cell(sheet.text, 'parent/clip:music-other', '2'))
    draft_bytes = draft.read_bytes()
    tl = Checkout.attach(session)
    tl.clip(REF).on(1)
    lose.add('checkout-A')
    with pytest.raises(WorkspaceClientError):
        tl.flush(idempotency_key='checkout-A')
    before, preview = session.outbox.path.read_bytes(), copy.deepcopy(tl.bundle)
    sent = len(observed)
    for action in (lambda: NativeSheet.export(session),
                   lambda: sheet.apply(draft.read_text(), session=session, dry_run=False)):
        with pytest.raises(SheetError, match='pending intent'):
            action()
        assert session.outbox.path.read_bytes() == before and tl.bundle == preview
        assert draft.read_bytes() == draft_bytes and len(observed) == sent
    tl.flush()
    assert observed[-1]['body'] == observed[-2]['body']
    assert observed[-1]['key'] == observed[-2]['key'] == 'checkout-A'
    acknowledged = session.outbox.path.read_bytes()
    with pytest.raises(SheetError, match='original project, timeline and basis'):
        sheet.apply(draft.read_text(), session=session, dry_run=False)
    assert session.outbox.path.read_bytes() == acknowledged and draft.read_bytes() == draft_bytes
