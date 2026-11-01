"""History-backed summaries for saved timeline conflicts."""
from __future__ import annotations

import copy
from types import SimpleNamespace

from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline._doc_patch import Closure
from astrid.sdk.timeline.clash import _history, _receipt_sites, explain
from astrid.sdk.timeline.commit import bundle_from_document
from tests.sdk.test_timeline_commit_provider import DetachedRealm


class PacketSession:
    project_id, timeline_id = 'project-1', 'film'

    def __init__(self, realm):
        self.realm = realm
        self.page_size = 100
        self.client = SimpleNamespace(timelines=self)

    def history(self, project, timeline, *, cursor=None, limit=100):
        offset = int(cursor or 0)
        end = min(len(self.realm.entries), offset + min(limit, self.page_size))
        return DomainResult.success({'head': copy.deepcopy(self.realm.head),
                                     'items': copy.deepcopy(self.realm.entries[offset:end]),
                                     'next_cursor': str(end) if end < len(self.realm.entries) else None})

    def _capture_bundle(self, state, extra_commands=()):
        return bundle_from_document(self.realm.documents[state['base']['revision_id']])


def test_history_verifies_pages_and_returns_only_entries_after_base():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    realm.call([realm.move(on=3.003)])
    after_first = copy.deepcopy(realm.head)
    realm.call([realm.move(on=4.004)])
    session = PacketSession(realm)
    session.page_size = 1

    head, entries = _history(session, after_first)

    assert head == realm.head
    assert [entry['publication_id'] for entry in entries] == [realm.head['publication_id']]
    assert _history(PacketSession(DetachedRealm()), base)[1] == []


def test_receipts_keep_only_touched_refs_and_paths():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    realm.call([realm.move(on=3.003)])
    bundle = bundle_from_document(realm.documents[base['revision_id']])
    session = PacketSession(realm)
    _, entries = _history(session, base)

    receipts, sites = _receipt_sites(entries, Closure(bundle))
    raw_writes = [write for record in entries[0]['records']['commands']
                  for write in [*record['writes'], *record['derived_writes']]]

    assert receipts[0]['publication_id'] == realm.head['publication_id']
    assert receipts[0]['touched']
    assert receipts[0]['touched'] == [
        {'ref': write['ref'], 'path': write['path']} for write in raw_writes
    ]
    assert all(set(site) == {'ref', 'path'} for site in receipts[0]['touched'])
    assert all(isinstance(ref, str) and isinstance(path, list)
               for ref, path in sites[0])
    assert 'writes' not in receipts[0]


def _pending(base, head, conflict):
    return {'id': 'pc_saved', 'state': 'pending', 'label': 'Recut', 'key': 'recut-ep01',
            'origin': 'list', 'part': 0,
            'parts': [{'base': copy.deepcopy(base), 'commands': [],
                       'commit_options': {'merge': 'fields'}}],
            'failure': {'details': {'status': 'conflict', 'current': copy.deepcopy(head),
                                    'conflicts': [conflict]}}}


def test_packet_names_touching_receipt():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    receipt = realm.call([realm.move('parent/clip:clip-a', 6.006)])
    realm.entries[-1]['records']['label'] = 'Move clip-a'
    realm.entries[-1]['publisher'] = 'astrid-0123456789abcdef'
    conflict = {'kind': 'value_conflict', 'ref': 'parent/clip:clip-a', 'path': ['at'],
                'base': {'state': 'absent'}, 'theirs': {'state': 'absent'},
                'ours': {'state': 'absent'}}

    packet = explain(PacketSession(realm), _pending(base, realm.head, conflict))

    assert packet['schema'] == 'astrid.clash/2'
    assert packet['conflicts'] == [conflict]
    assert packet['receipts'][0]['publication_id'] == receipt['receipt']['receipt_id']
    assert packet['receipts'][0]['publisher'] == 'astrid-0123456789abcdef'
    assert packet['receipts'][0]['touches_conflict'] is True
    assert packet['receipts'][0]['touched']
    assert 'Move' in packet['summary']
    assert 'another writer' in packet['summary']
    assert 'astrid-0123456789abcdef' not in packet['summary']
    assert 'pc_saved' in packet['summary']
    assert packet['next'] == "tl.retry('pc_saved')"


def test_packet_without_history_still_has_summary():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    conflict = {'kind': 'value_conflict', 'ref': 'parent/clip:clip-a', 'path': ['at'],
                'base': {'state': 'absent'}, 'theirs': {'state': 'absent'},
                'ours': {'state': 'absent'}}
    session = PacketSession(realm)

    def unavailable(*args, **kwargs):
        raise ConnectionError('history unavailable')

    session.history = unavailable
    packet = explain(session, _pending(base, realm.head, conflict))

    assert packet['receipts'] is None
    assert 'Recut' in packet['summary']
    assert 'pc_saved' in packet['summary']
    assert 'Move' not in packet['summary']


def test_conflict_summary_uses_clip_facing_target():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    conflict = {'kind': 'value_conflict', 'ref': 'parent/clip:clip-a', 'path': ['at'],
                'base': {'state': 'absent'}, 'theirs': {'state': 'absent'},
                'ours': {'state': 'absent'}}
    packet = explain(PacketSession(realm), _pending(base, realm.head, conflict))

    assert 'clip clip-a start time' in packet['summary']
    assert 'parent/config/clips/id:' not in packet['summary']


def test_packet_does_not_attribute_nonconflicting_receipt():
    receipt = {'label': 'Peer move', 'publisher': 'peer-1',
               'publication_id': 'pub-peer', 'touches_conflict': False}
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    realm.call([realm.move('parent/clip:clip-a', 6.006)])
    realm.entries[-1]['records']['label'] = receipt['label']
    realm.entries[-1]['publisher'] = receipt['publisher']
    conflict = {'kind': 'value_conflict', 'ref': 'parent/clip:clip-b', 'path': ['at'],
                'base': {'state': 'absent'}, 'theirs': {'state': 'absent'},
                'ours': {'state': 'absent'}}

    packet = explain(PacketSession(realm), _pending(base, realm.head, conflict))

    assert packet['receipts'][0]['touches_conflict'] is False
    assert 'Peer move' not in packet['summary']
    assert 'peer-1' not in packet['summary']


def test_conflict_target_describes_verified_removal_and_equal_values():
    from astrid.sdk.timeline.clash import _target

    base = SimpleNamespace(document={'parent_revision': {'payload': {
        'config': {'clips': [{'id': 'clip-a', 'cut_id': 'c1', 'layer_name': 'v1'}]}}}})
    removed = SimpleNamespace(document={'parent_revision': {'payload': {'config': {'clips': []}}}})
    conflict = {'ref': 'parent/clip:clip-a', 'path': ['at'],
                'ours': {'state': 'absent'}, 'theirs': {'state': 'absent'}}

    assert _target(conflict, base, removed) == 'clip c1.v1 was removed'
    assert _target({**conflict, 'ours': {'state': 'present', 'value': 4},
                    'theirs': {'state': 'present', 'value': 4}}, base) == (
        'clip c1.v1 start time already has 4')
