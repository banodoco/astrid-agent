"""Exact native JSON publication against the official Bundle2 Runtime artifact."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline import commands, commit
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params
from tests.helpers.runtime import use_runtime, vendored_runtime_commit
from tests.sdk.test_timeline_selected_schema_runtime import (
    REF,
    configured_project_pack,  # noqa: F401
    inverse,
    raw_params,
    selected_runtime_env,  # noqa: F401
)
from tests.sdk.test_timeline_selected_schema_runtime import (
    retained_pack as retained_pack,
)

pytestmark = pytest.mark.integration
RUNTIME = vendored_runtime_commit()


@pytest.fixture(autouse=True)
def official_runtime():
    use_runtime(RUNTIME)


def closure(case, revision=None):
    return case.service._timeline_commits()[0].revision_closure(
        case.project, 'film', revision or case.head()['revision_id'])


def exact(actual, expected):
    assert commit._json(actual) == commit._json(expected)


@pytest.mark.parametrize('before,after', [(2, 2.0), (1, True), (True, 1),
                                         (2, 2), (2.0, 2.0), (True, True)])
def test_native_params_exact_values_history_reload_retry_and_inverse(
        retained_pack, before, after, monkeypatch):  # noqa: F811 - imported pytest fixture
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService

    case = retained_pack
    case.submit(case.request([raw_params(case, {'x': before})]), 'seed-x')
    original = copy.deepcopy(closure(case))
    baseline = case.head()
    command = capture_clip_set_params(case.bundle(), REF, values={'x': after}, **case.options)
    body = case.request([command])
    accepted = case.submit(body, 'numeric-edit')
    document = accepted['data']['canonical_document']
    params = State(commit.bundle_from_document(json.loads(json.dumps(document)))).entity(REF)['params']
    assert type(params['x']) is type(after) and params['x'] == after
    assert params['future'] == {'keep': [None, False, {}]} and params['seed'] == 4
    assert accepted['data']['commands'] == [command]
    entry = next(entry for entry in case.service.list_timeline_commits(
        case.project, 'film', authorize=lambda: None)['items']
        if entry['publication_id'] == case.head()['publication_id'])
    writes = [write for record in entry['records']['commands'] for write in record['writes']]
    assert any(write['path'] == ['params', 'x'] for write in writes)
    assert document['dependency_manifest'] == original['dependency_manifest']
    exact(closure(case, baseline['revision_id']), original)

    changed_encoding = commit._json(before) != commit._json(after)
    assert (case.head()['revision_id'] != baseline['revision_id']) is changed_encoding
    assert case.head()['publication_id'] != baseline['publication_id']
    assert document['parent_revision']['content_digest'] == commit._digest(document['parent_revision']['payload'])
    exact(closure(case), document)
    case.service.close()
    case.service = RuntimeService(case.root)
    case.service.register_timeline_commit_provider(commit.create_runtime_commit_service())
    exact(closure(case), document)
    history = case.service.list_timeline_commits(case.project, 'film', authorize=lambda: None)

    def forbidden(*args, **kwargs):
        raise AssertionError('exact retry executed provider')

    with monkeypatch.context() as retry:
        retry.setattr(commands, 'execute', forbidden)
        exact(case.submit(body, 'numeric-edit'), accepted)
    exact(case.service.list_timeline_commits(case.project, 'film', authorize=lambda: None), history)
    undone = case.submit(case.request([inverse(accepted['receipt']['receipt_id'])]), 'numeric-undo')
    exact(undone['data']['canonical_document'], original)
    redone = case.submit(case.request([inverse(undone['receipt']['receipt_id'])]), 'numeric-redo')
    exact(redone['data']['canonical_document'], document)
    # Unchanged writes remain accepted history edges and protect prior undo.
    noop = case.submit(case.request([raw_params(case, {'x': after})]), 'unchanged-write')
    exact(noop['data']['canonical_document'], document)
    assert noop['receipt']['receipt_id'] != redone['receipt']['receipt_id']
    head = case.head()
    with pytest.raises(RuntimeErrorBase) as error:
        case.submit(case.request([inverse(redone['receipt']['receipt_id'])]), 'old-undo')
    assert error.value.details['reason'] == 'intervening_write'
    assert case.head() == head
    with pytest.raises(RuntimeErrorBase):
        case.submit(case.request([raw_params(case, {'seed': 16})]), 'invalid-source-value')
    damaged = copy.deepcopy(raw_params(case, {'x': before}))
    damaged['args']['schema_source']['content_digest'] = 'sha256:' + 'f' * 64
    with pytest.raises(RuntimeErrorBase):
        case.submit(case.request([damaged]), 'invalid-source-pin')
    assert case.head() == head
    exact(closure(case, baseline['revision_id']), original)


@pytest.mark.parametrize('before,peer,allowed', [(2, 2.0, True), (1, True, False), (True, 1, False)])
def test_native_original_value_guards_keep_numeric_equivalence_and_bool_distinction(
        retained_pack, before, peer, allowed):  # noqa: F811 - imported pytest fixture
    from runtime_protocol.errors import RuntimeErrorBase

    case = retained_pack
    case.submit(case.request([raw_params(case, {'x': before})]), 'guard-seed')
    command = capture_clip_set_params(case.bundle(), REF, values={'x': 3}, **case.options)
    original_claims = copy.deepcopy(command)
    case.submit(case.request([raw_params(case, {'x': peer})]), 'guard-peer')
    head = case.head()
    if allowed:
        accepted = case.submit(case.request([command]), 'original-guard')
        assert accepted['data']['commands'] == [original_claims]
        assert State(case.bundle()).entity(REF)['params']['x'] == 3
    else:
        with pytest.raises(RuntimeErrorBase) as error:
            case.submit(case.request([command]), 'original-guard')
        assert error.value.details['reason'] == 'dependency_changed'
        assert case.head() == head
        assert type(State(case.bundle()).entity(REF)['params']['x']) is type(peer)
    exact(command, original_claims)
