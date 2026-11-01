"""Native numeric moves on API-built films, including the M1 command surface."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline import commit
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.emitter import emit_document_commands
from tests.helpers.runtime import use_runtime, vendored_runtime_commit

pytestmark = pytest.mark.integration


@pytest.fixture
def numeric_film(tmp_path, monkeypatch):
    runtime = use_runtime(commit=vendored_runtime_commit())
    monkeypatch.syspath_prepend(str(runtime / 'tests'))
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
    from timeline_commit_film_fixture import build_film_fixture

    film = build_film_fixture(tmp_path)
    service = RuntimeService(film.duplicate_root)
    service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
    case = SimpleNamespace(service=service, film=film)

    def head():
        row = case.service.get_project_timeline(film.project_id, film.timeline_id)
        return {'revision_id': row['head_revision_id'], 'publication_id': row['head_publication_id']}

    def document(revision=None):
        return case.service._timeline_commits()[0].revision_closure(
            film.project_id, film.timeline_id, revision or head()['revision_id'])

    def submit(calls, key):
        base = head()
        patch = [change for call in calls if call['op'] == 'doc.patch'
                 for change in call['args']['changes']]
        body = {'api_version': 1, 'project_id': film.project_id, 'timeline_id': film.timeline_id,
                'base_revision_id': base['revision_id'], 'base_publication_id': base['publication_id'],
                'commands': calls, 'label': key, 'merge': 'atomic', 'reads': [],
                'steps': [{'id': key, 'label': key, 'intent': 'Numeric conformance edit',
                           'tags': ['numeric'], 'command_ordinals': list(range(len(calls))),
                           'patch': patch, 'reads': []}],
                'dry_run': False, 'source': {'via': 'python'},
                'rationale': 'Preserve exact numeric authored values', 'group_id': key}
        return case.service.commit_timeline(
            film.project_id, film.timeline_id, body, idempotency_key=key, publisher='author',
            invocation_path=f'/v1/projects/{film.project_id}/timelines/{film.timeline_id}/commit',
            authorize=lambda: None)

    case.head, case.document, case.submit = head, document, submit
    try:
        yield case
    finally:
        case.service.close()


def exact(actual, expected):
    assert commit._json(actual) == commit._json(expected)


@pytest.mark.parametrize('scope', ['parent', 'occurrence'])
@pytest.mark.parametrize('start,on', [(0, 0), (0.0, 0), (0, 1)])
def test_native_move_keeps_exact_numeric_image_and_unchanged_history(numeric_film, scope, start, on):
    case = numeric_film
    if scope == 'parent':
        ref = 'parent/clip:music-bed'
    else:
        ref = 'occurrence:occurrence-1/clip:clip-1'

    bundle = commit.bundle_from_document(case.document())
    hold = 0.25 if scope == 'occurrence' else State(bundle).entity(ref)['hold']
    candidate = copy.deepcopy(bundle)
    target = State(candidate).entity(ref)
    target['at'] = start
    target['app'] = {'astrid': {'fixed_start': True, 'for': hold},
                     'org.example/opaque': [None, False, 1, 1.0]}
    if scope == 'occurrence':
        target['hold'] = 0.25
    case.submit(emit_document_commands(bundle, candidate), 'numeric-baseline')
    original, base = case.document(), case.head()
    bundle = commit.bundle_from_document(original)
    command = capture_move(bundle, ref, on)
    accepted = case.submit([command], 'numeric-move')
    document = accepted['data']['canonical_document']
    actual = State(commit.bundle_from_document(document)).entity(ref)
    assert type(actual['at']) is float and actual['at'] == on
    assert actual['app']['org.example/opaque'] == [None, False, 1, 1.0]
    changed = commit._json(start) != commit._json(float(on))
    assert (case.head()['revision_id'] != base['revision_id']) is changed
    assert case.head()['publication_id'] != base['publication_id']
    exact(case.document(), document)
    exact(case.document(base['revision_id']), original)
    exact(document['dependency_manifest']['media'], original['dependency_manifest']['media'])
    history = case.service.list_timeline_commits(
        case.film.project_id, case.film.timeline_id, authorize=lambda: None)['items']
    record = next(row for row in history if row['publication_id'] == case.head()['publication_id'])
    assert any(write['ref'] == ref and write['path'] == ['at']
               for item in record['records']['commands'] for write in item['writes'])
