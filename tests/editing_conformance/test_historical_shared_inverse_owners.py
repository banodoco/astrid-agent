"""Historical sharing must retain existing destination owners during inversion."""
from __future__ import annotations

import copy

import pytest

from tests.editing_conformance import test_commit_runtime_source_copy as source_fixture
from tests.editing_conformance.test_occurrence_shared_historical import prepare, with_media

pytestmark = pytest.mark.integration
source_copy = source_fixture.source_copy


def inverse(result):
    return {'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
            'args': {'publications': [result['receipt']['receipt_id']]}}


@pytest.mark.parametrize('media', [False, True])
def test_inverse_retains_existing_destination_owner(source_copy, media):
    service, document, request, commit, call, *_ = (
        with_media(source_copy) if media else prepare(source_copy))
    commit('destination', request('destination', [call]), 'first-owner')
    before, source = document('destination'), document('source')
    counts = tuple(service.store.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                   for table in ('shot_revisions', 'internal_timeline_revisions'))
    heads = list(service.store.conn.execute('SELECT * FROM shot_revision_heads'))
    other = copy.deepcopy(call)
    other['args']['occurrence_id'] = 'second-owner'
    other['created'] = ['occurrence:second-owner']
    placed = commit('destination', request('destination', [other]), 'second-owner')
    body = request('destination', [inverse(placed)])
    undone = commit('destination', body, 'undo-second-owner')
    assert undone['data']['canonical_document'] == before
    assert document('destination') == before
    assert document('source') == source
    assert tuple(service.store.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                 for table in ('shot_revisions', 'internal_timeline_revisions')) == counts
    assert list(service.store.conn.execute('SELECT * FROM shot_revision_heads')) == heads
    assert commit('destination', body, 'undo-second-owner') == undone


@pytest.mark.parametrize('media', [False, True])
def test_inverse_guards_later_destination_consumer(source_copy, media):
    from runtime_protocol.errors import RuntimeErrorBase

    service, document, request, commit, call, *_ = (
        with_media(source_copy) if media else prepare(source_copy))
    first = commit('destination', request('destination', [call]), 'first-owner')
    other = copy.deepcopy(call)
    other['args']['occurrence_id'] = 'later-consumer'
    other['created'] = ['occurrence:later-consumer']
    commit('destination', request('destination', [other]), 'later-consumer')
    before, source = document('destination'), document('source')
    snapshot = tuple(service.store.conn.iterdump())
    with pytest.raises(RuntimeErrorBase) as error:
        commit('destination', request('destination', [inverse(first)]), 'undo-first-owner')
    assert error.value.details['reason'] == 'intervening_write'
    assert document('destination') == before
    assert document('source') == source
    assert tuple(service.store.conn.iterdump()) == snapshot
