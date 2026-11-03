"""Native provider root compilation, local reservations, inverse and redo."""
from __future__ import annotations

import copy
import tempfile

import pytest
from jsonschema import Draft202012Validator

from astrid.core.timeline.authoring_bundle import compile_authoring_candidate
from astrid.sdk.timeline import commit
from astrid.sdk.timeline._command_state import CommandError, State, present
from astrid.sdk.timeline.commands import _SCHEMA, execute
from tests.editing_conformance.test_recorded_graph_roots import (
    child_command,
    create_command,
    root_values,
)
from tests.editing_conformance.test_recorded_graph_roots import (
    child_ops as child_ops,
)
from tests.editing_conformance.test_recorded_graph_roots import (
    root_ops as root_ops,
)
from tests.helpers.runtime import use_runtime
from tests.helpers.trusted_seed import seed_trusted_parent
from tests.sdk.test_timeline_commit_provider_document import document_fixture

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures('root_ops')]


def _replace_media(document, media_id):
    document = copy.deepcopy(document)

    def replace(value):
        if isinstance(value, dict):
            return {key: replace(child) for key, child in value.items()}
        if isinstance(value, list):
            return [replace(child) for child in value]
        return media_id if value == 'sha256:' + '1' * 64 else value

    document = replace(document)
    parent = document['parent_revision']
    shots = document['shot_revisions']
    internals = document['internal_timeline_revisions']
    for image in (parent, *shots, *internals):
        image['content_digest'] = commit._digest(image['payload'])
    document['dependency_manifest'] = commit._manifest(
        shots, internals, [{'media_id': media_id, 'content_digest': media_id}], [])
    return document


class RuntimeRealm:
    """Small facade over the genuine Runtime commit route used by these tests."""

    def __init__(self, document):
        use_runtime()
        from runtime_protocol.service import RuntimeService
        from runtime_protocol.store import RealmStore
        from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider

        self._temporary = tempfile.TemporaryDirectory(prefix='timeline-graph-')
        RealmStore.initialize(self._temporary.name).close()
        self.service = RuntimeService(self._temporary.name)
        self.service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
        self.project = self.service.create_project(
            {'slug': 'graph-roots', 'name': 'Graph roots'}, idempotency_key='project')['id']
        self.timeline = 'film'
        self.service.create_timeline(self.project, self.timeline, idempotency_key='timeline')
        self.service.create_timeline(self.project, 'chapter-1', idempotency_key='internal-timeline')
        media = self.service.ingest(
            self.project, b'graph-root-media', media_type='image/png',
            idempotency_key='graph-media')['data']['object_id']
        self.document = _replace_media(document, media)
        self.document['project_id'] = self.project
        self.document['timeline_id'] = self.timeline
        seed_trusted_parent(self.service, {
            'project_id': self.project,
            'timeline_id': self.timeline,
            'expected_head': None,
            'parent_revision_id': self.document['parent_revision']['revision_id'],
            'parent_composition': self.document['parent_revision']['payload'],
            'shot_revisions': self.document['shot_revisions'],
            'internal_timeline_revisions': self.document['internal_timeline_revisions'],
        }, idempotency_key='seed-parent')
        self._commits, _ = self.service._timeline_commits()
        self._request_count = 0
        self.accepted = []
        self.prepared = []

    @property
    def head(self):
        return self._commits.head(self.project, self.timeline)

    @property
    def entries(self):
        return self.service.list_timeline_commits(
            self.project, self.timeline, authorize=lambda: None)['items']

    @property
    def documents(self):
        return {
            entry['revision_id']: self._commits.revision_closure(
                self.project, self.timeline, entry['revision_id'])
            for entry in self.entries
        }

    def call(self, calls, *, base=None, dry_run=False):
        from runtime_protocol.errors import RuntimeErrorBase

        from tests.sdk.test_timeline_commit_provider import CommitRejected

        base = base or self.head
        self._request_count += 1
        body = {
            'api_version': 1, 'project_id': self.project, 'timeline_id': self.timeline,
            'base_revision_id': base['revision_id'],
            'base_publication_id': base['publication_id'],
            'commands': copy.deepcopy(calls), 'dry_run': dry_run,
            'label': 'Graph roots',
            'steps': [{
                'id': 'graph-roots', 'label': 'Graph roots', 'intent': 'test intent',
                'tags': [], 'command_ordinals': list(range(len(calls))), 'patch': [],
            }],
            'merge': 'fields', 'reads': [],
            'source': {'via': 'python'}, 'rationale': 'test intent',
        }
        try:
            result = self.service.commit_timeline(
                self.project, self.timeline, body,
                idempotency_key='request-' + str(self._request_count), publisher='actor-1',
                invocation_path=f'/v1/projects/{self.project}/timelines/{self.timeline}/commit',
                authorize=lambda: None)
        except RuntimeErrorBase as error:
            raise CommitRejected({
                'http_status': error.status, 'code': getattr(error, 'code', 'conflict'),
                'message': str(error), 'details': copy.deepcopy(error.details),
            }) from error
        if result['receipt'] is not None:
            self.prepared.append(copy.deepcopy(body))
            self.accepted.append(copy.deepcopy(result))
        return result


class RuntimeHistoryPort:
    """Authenticated history/closure reads for direct reservation verification."""

    def __init__(self, realm, *, replacement=None, target_publication=None):
        self.realm = realm
        self.replacement = replacement
        self.target_publication = target_publication

    def head(self):
        return copy.deepcopy(self.realm.head)

    def revision_closure(self, revision):
        return self.realm._commits.revision_closure(
            self.realm.project, self.realm.timeline, revision)

    def history_entry(self, publication):
        return self.realm._commits.history_entry(
            self.realm.project, self.realm.timeline, publication)

    def history(self, through, after_publication_id=None, cursor=None, limit=100):
        page = self.realm._commits.history(
            self.realm.project, self.realm.timeline, through,
            after_publication_id=after_publication_id, cursor=cursor, limit=limit)
        if self.replacement is not None:
            page = copy.deepcopy(page)
            for entry in page['items']:
                if entry['publication_id'] == self.target_publication:
                    entry['records']['commands'] = copy.deepcopy(self.replacement)
        return page


def test_new_roots_close_exact_content_once_and_retain_original_closure():
    source = document_fixture()
    bundle = commit.bundle_from_document(source)
    plan = execute(bundle, [create_command()])
    compilation = compile_authoring_candidate(plan.bundle)
    document = commit.document_from_bundle(plan.bundle)
    new_shot = next(row for row in document['shot_revisions'] if row['shot_id'] == 'new-shot')
    new_internal = next(row for row in document['internal_timeline_revisions']
                        if row['revision_id'] == new_shot['internal_timeline_revision_id'])
    assert new_internal['timeline_id'] == source['timeline_id']
    assert new_internal['payload'] == root_values()[0]['value']['internal_timeline']
    assert new_shot['payload'] == {**root_values()[0]['value']['payload'],
                                  'internal_timeline_revision_id': new_internal['revision_id']}
    assert new_internal['content_digest'] == commit._digest(new_internal['payload'])
    assert new_shot['content_digest'] == commit._digest(new_shot['payload'])
    assert new_shot in compilation.publication['shot_revisions']
    assert new_internal in compilation.publication['internal_timeline_revisions']
    assert document['parent_revision']['payload']['occurrences'][-1] == {
        **root_values()[1]['value'], 'shot_revision_id': new_shot['revision_id']}
    assert source['shot_revisions'][0] in document['shot_revisions']
    assert source['internal_timeline_revisions'][0] in document['internal_timeline_revisions']
    assert compilation.identity_mapping['shots']['new-shot']['source_shot_id'] is None
    assert compilation.identity_mapping['shots']['new-shot']['source_revision_id'] is None
    assert plan.bundle['source_mapping'] == bundle['source_mapping']
    reopened = commit.bundle_from_document(document)
    assert commit.document_from_bundle(reopened) == document
    assert State(reopened).get('occurrence:new-occ/shot', ['shot_id']) == present('new-shot')


@pytest.mark.parametrize('definition,value', [
    ('ScopedId', 'shot:new-shot'), ('EntityRef', 'shot:new-shot'),
    ('TargetKey', 'shot:new-shot::'), ('TargetKey', 'shot:new-shot::payload.metadata'),
    ('AnchorIdentity', 'shot:new-shot'), ('ScopedId', 'occurrence:new-occ'),
])
def test_root_schema_addresses(definition, value):
    Draft202012Validator({'$defs': _SCHEMA['$defs'], '$ref': '#/$defs/' + definition}).validate(value)


@pytest.mark.parametrize('value', ['shot:', 'shot:new/other', 'shot:a::b'])
def test_invalid_shot_root_schema_addresses(value):
    validator = Draft202012Validator({'$defs': _SCHEMA['$defs'], '$ref': '#/$defs/ScopedId'})
    assert not validator.is_valid(value)


class HistoryRejected(Exception):
    def __init__(self, failure):
        self.failure = failure


def reservation_check(monkeypatch, plan, history_records=()):
    realm = RuntimeRealm(document_fixture())
    plan.bundle['project_id'] = realm.project
    plan.bundle['timeline_id'] = realm.timeline
    target = None
    if history_records:
        accepted = realm.call([create_command()])
        target = accepted['receipt']['receipt_id']
    port = RuntimeHistoryPort(realm, replacement=history_records or None,
                              target_publication=target)
    commit._check_reservations(port, plan, realm.head, HistoryRejected, realm.head)



def test_typed_roots_are_valid_local_creations_and_retired_roots_are_reserved():
    for index in (0, 1):
        realm = RuntimeRealm(document_fixture())
        original = commit.bundle_from_document(realm.document)
        plan = execute(original, [create_command()])
        accepted = realm.call([create_command()])
        assert (accepted['data']['commands'][0]['created'][index] ==
                plan.records[0]['command']['created'][index])
        before = (copy.deepcopy(realm.head), copy.deepcopy(realm.entries),
                  copy.deepcopy(realm.documents), copy.deepcopy(realm.prepared))
        with pytest.raises(CommandError, match='already reserved') as error:
            commit._check_reservations(
                RuntimeHistoryPort(realm), plan, realm.head, HistoryRejected, realm.head)
        assert error.value.kind == 'identity_collision'
        assert (realm.head, realm.entries, realm.documents, realm.prepared) == before


@pytest.mark.parametrize('damage', ['shot-owner', 'occurrence-owner', 'kind', 'ref', 'claim'])
def test_bad_typed_creation_history_refuses(monkeypatch, damage):
    plan = execute(commit.bundle_from_document(document_fixture()), [create_command()])
    history = copy.deepcopy(plan.records)
    items = history[0]['created']
    if damage == 'shot-owner':
        items[0]['owner'] = 'parent'
    elif damage == 'occurrence-owner':
        items[1]['owner'] = None
    elif damage == 'kind':
        items[0]['kind'] = 'track'
    elif damage == 'ref':
        items[0]['ref'] = 'shot:a/b'
    else:
        history[0]['command']['created'] = []
    with pytest.raises(HistoryRejected) as error:
        reservation_check(monkeypatch, plan, history)
    assert error.value.failure['details']['reason'] == 'history_unavailable'


def test_duplicate_batch_and_invalid_new_facts_refuse(monkeypatch):
    plan = execute(commit.bundle_from_document(document_fixture()), [create_command()])
    duplicate = copy.deepcopy(plan)
    duplicate.records.append(copy.deepcopy(plan.records[0]))
    with pytest.raises(CommandError, match='already reserved'):
        reservation_check(monkeypatch, duplicate)
    invalid = copy.deepcopy(plan)
    invalid.records[0]['created'][0]['owner'] = 'parent'
    with pytest.raises(CommandError, match='invalid created identity scope'):
        reservation_check(monkeypatch, invalid)


def test_trusted_inverse_recreation_keeps_identity_reservation(monkeypatch):
    plan = execute(commit.bundle_from_document(document_fixture()), [create_command()])
    # Authenticated inverse dispatch is tested by the existing provider suites;
    # here only its exemption from ordinary fresh allocation is exercised.
    restored = copy.deepcopy(plan)
    restored.records[0]['command']['op'] = 'timeline.inverse'
    reservation_check(monkeypatch, restored, plan.records)


def test_authenticated_root_creation_reload_inverse_redo_exact_closure():
    from tests.sdk.test_timeline_commit_provider import inverse

    realm = RuntimeRealm(document_fixture())
    original = copy.deepcopy(realm.documents[realm.head['revision_id']])
    accepted = realm.call([create_command()])
    document = accepted['data']['canonical_document']
    assert commit.document_from_bundle(commit.bundle_from_document(document)) == document
    undone = realm.call([inverse([accepted['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == original
    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == document
    assert redone['data']['commands'][0]['created'] == ['shot:new-shot', 'occurrence:new-occ']


@pytest.mark.usefixtures('child_ops')
def test_authenticated_created_child_stages_reload_inverse_redo_exact_closure(monkeypatch):
    from tests.editing_conformance import test_recorded_graph_roots as graph_fixtures
    from tests.sdk.test_timeline_commit_provider import inverse

    original_root_values = graph_fixtures.root_values

    def valid_root_values():
        roots = original_root_values()
        # C0 final timing validation requires the retained hold to fit the new
        # occurrence's half-open duration; preserve the child hold=3 assertion.
        roots[1]['value']['duration_ms'] = 3000
        return roots

    monkeypatch.setattr(graph_fixtures, 'root_values', valid_root_values)

    realm = RuntimeRealm(document_fixture())
    original = copy.deepcopy(realm.documents[realm.head['revision_id']])
    accepted = realm.call([create_command()])
    document = accepted['data']['canonical_document']
    assert State(commit.bundle_from_document(document)).get(
        'occurrence:new-occ/clip:title', ['hold']) == present(3)
    undone = realm.call([inverse([accepted['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == original
    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == document
    assert redone['data']['commands'][0]['created'] == ['shot:new-shot', 'occurrence:new-occ']


@pytest.mark.usefixtures('child_ops')
@pytest.mark.parametrize('first_scope', ['root', 'child'])
@pytest.mark.parametrize('aba', [False, True])
@pytest.mark.parametrize('noncontiguous', [False, True])
def test_provider_root_child_aliases_guard_equal_state_publications(first_scope, aba, noncontiguous):
    from tests.sdk.test_timeline_commit_provider import CommitRejected, inverse

    realm = RuntimeRealm(document_fixture())
    command = child_command('occurrence:occ-a/clip:clip-a')
    root = {**command, 'op': 'test.root-write'}
    first = realm.call([root if first_scope == 'root' else command])
    gap = command if first_scope == 'root' else root
    realm.call([{**gap, 'args': {'values': [4, 3] if aba else [3]}}])
    targets = [first['receipt']['receipt_id']]
    if noncontiguous:
        second = realm.call([{**command, 'args': {'values': [5]}}])
        targets.append(second['receipt']['receipt_id'])
    head, accepted = copy.deepcopy(realm.head), copy.deepcopy(realm.accepted)
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse(targets)])
    assert error.value.failure['details']['reason'] == 'intervening_write'
    assert realm.head == head
    assert realm.accepted == accepted


@pytest.mark.parametrize('damage', ['opaque', 'internal', 'compiler-link'])
def test_compiled_root_guard_still_authenticates_authored_data(damage):
    original = commit.bundle_from_document(document_fixture())
    plan = execute(original, [create_command()])
    reloaded = commit.bundle_from_document(commit.document_from_bundle(plan.bundle))
    shot = reloaded['shots']['new-shot']
    if damage == 'opaque':
        shot['payload']['unknown']['ordered'][1] = 0
    elif damage == 'internal':
        shot['internal_timeline']['unknown']['empty'] = None
    else:
        shot['payload']['internal_timeline_revision_id'] = 'unverified-link'
    before = copy.deepcopy(reloaded)
    from tests.editing_conformance.test_recorded_graph_roots import inverse_command
    with pytest.raises(CommandError) as error:
        execute(reloaded, [inverse_command()], inverse_records=plan.records)
    assert error.value.kind == 'inverse_conflict'
    assert reloaded == before
