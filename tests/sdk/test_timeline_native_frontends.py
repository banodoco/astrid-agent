"""Checkout and sheet share one original capture and durable Session outbox."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.sync import CommitConflict, TimelineSession
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline_sheet import NativeSheet, SheetError
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.sdk import test_timeline_sync as sync
from tests.sdk.test_timeline_sheet_native import edit_cell

connected = sync.connected


@pytest.mark.parametrize('relevant', [False, True])
def test_sheet_lost_ack_checkout_suffix_exact_retry_and_original_guards(connected, relevant, monkeypatch):
    client, generated, path = connected
    session = sync.open_session(connected)
    sheet = NativeSheet.export(session)
    edited = edit_cell(sheet.text, 'parent/clip:a', '5')
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        sheet.apply(edited, session=session, dry_run=False, idempotency_key='sheet-A')
    flight = json.loads(path.read_bytes())['flight']
    accepted_a = copy.deepcopy(generated.accepted['sheet-A'][1])
    tl = Checkout.attach(session)
    tl.clip('a').on(7)
    suffix = json.loads(path.read_bytes())['pending'][1]
    assert suffix['expect']['parent/clip:a::at'] == {'state': 'present', 'value': 5}
    head = generated.bundles['r1']
    generated.bundles['peer'] = execute(head, [capture_move(
        head, 'parent/clip:a' if relevant else 'parent/clip:b', 9)]).bundle
    generated.head = {'revision_id': 'peer', 'publication_id': 'peer-publication'}
    session = sync.open_session(connected)
    reads = list(client.reads)
    with monkeypatch.context() as patch:
        patch.setattr(TimelineSession, '_bundle', lambda *a: pytest.fail('retry recaptured a bundle'))
        retried = NativeSheet.load(session).apply(edited, session=session, dry_run=False)
    assert retried['receipt'] == accepted_a['receipt']
    assert client.reads == reads
    assert generated.calls[0] == generated.calls[1] == (flight['body'].encode(), 'sheet-A')
    state = json.loads(path.read_bytes())
    assert state['pending'] == [suffix] and state['flight'] is None
    assert state['base'] == {'revision_id': 'r1', 'publication_id': 'pub1'}
    draft = copy.deepcopy(tl.bundle)
    with pytest.raises(TimelineEditError, match='basis changed'):
        tl.clip('a').on(8)
    assert tl.bundle == draft and json.loads(path.read_bytes()) == state
    tl = Checkout.attach(session)
    if relevant:
        with pytest.raises(CommitConflict):
            tl.flush()
        state = json.loads(path.read_bytes())
        assert state['pending'] == [] and state['flight'] is None
        record = next(iter(state['pending_changes'].values()))
        assert record['parts'][0]['commands'] == [suffix]
        assert record['failure']['details']['status'] == 'conflict'
        assert tl.clip('a').start == 7
    else:
        accepted = tl.flush()
        assert accepted['data']['commands'] == [suffix]
        assert accepted['data']['status'] == 'rebased'
        assert tl.clip('a').start == 7 and tl.clip('b').start == 9
    body, key = generated.calls[-1]
    assert key != flight['key']
    assert json.loads(body)['commands'] == [suffix]
    assert json.loads(body)['base_revision_id'] == 'r1'
    assert json.loads(body)['base_publication_id'] == 'pub1'


def test_checkout_first_sheet_refusal_keeps_draft_and_exact_pending_intent(connected):
    _, generated, path = connected
    session = sync.open_session(connected)
    sheet = NativeSheet.export(session)
    draft = edit_cell(sheet.text, 'parent/clip:b', '8')
    tl = Checkout.attach(session)
    tl.clip('a').on(5)
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        tl.flush(idempotency_key='checkout-A')
    before, preview = path.read_bytes(), copy.deepcopy(tl.bundle)
    for attempt in (lambda: NativeSheet.export(session),
                    lambda: sheet.apply(draft, session=session, dry_run=False)):
        with pytest.raises(SheetError, match='pending intent'):
            attempt()
        assert path.read_bytes() == before and tl.bundle == preview
    assert sheet.preview(draft).commands == [capture_move(sync.initial_bundle(), 'parent/clip:b', 8)]
    assert len(generated.calls) == 1
    tl.flush()
    assert generated.calls[0] == generated.calls[1]
    acknowledged = path.read_bytes()
    with pytest.raises(SheetError, match='original project, timeline and basis'):
        sheet.apply(draft, session=session, dry_run=False)
    assert path.read_bytes() == acknowledged


@pytest.mark.parametrize('acknowledged', [False, True])
@pytest.mark.parametrize('action', ['edit', 'flush', 'undo'])
def test_external_sheet_stage_or_ack_requires_explicit_reattach(connected, acknowledged, action):
    _, generated, path = connected
    session = sync.open_session(connected)
    tl = Checkout.attach(session)
    clip = tl.clip('a')
    sheet = NativeSheet.export(session)
    draft = edit_cell(sheet.text, 'parent/clip:a', '5')
    if acknowledged:
        sheet.apply(draft, session=session, dry_run=False)
    else:
        generated.lose_ack = True
        with pytest.raises(WorkspaceClientError):
            sheet.apply(draft, session=session, dry_run=False)
    before, preview = path.read_bytes(), copy.deepcopy(tl.bundle)
    with pytest.raises(TimelineEditError, match='basis changed'):
        if action == 'edit':
            clip.on(7)
        elif action == 'flush':
            tl.flush()
        else:
            tl.undo(['pub1'])
    assert path.read_bytes() == before and tl.bundle == preview
    reattached = Checkout.attach(session)
    reattached.clip('a').on(7)
    assert reattached.clip('a').start == 7
    assert json.loads(path.read_bytes())['pending'][-1]['expect']['parent/clip:a::at']['value'] == 5


def test_equal_preview_with_changed_publication_still_requires_reattach(connected):
    session = sync.open_session(connected)
    tl = Checkout.attach(session)
    session.commit({'schema_version': 1, 'op': 'clip.set-params', 'version': '1',
                    'select': 'parent/clip:a', 'args': {'values': {'text': 'café'}, 'clear': []}})
    before = connected[2].read_bytes()
    assert State(tl.bundle).entity('parent/clip:a') == State(session._bundle(session._state())).entity('parent/clip:a')
    with pytest.raises(TimelineEditError, match='basis changed'):
        tl.clip('a').on(1)
    assert connected[2].read_bytes() == before
