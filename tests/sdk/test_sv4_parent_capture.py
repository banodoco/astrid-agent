"""Borrowed parent captures retain projected-value and ownership semantics."""
import copy
import json
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline._commit_patch import CommitPatch
from astrid.sdk.timeline._doc_patch import Closure, value_at
from astrid.sdk.timeline.commit import bundle_from_document
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def capture(closure, read, *, legacy=False):
    calls = []
    ctx = SimpleNamespace(record={'ordinal': 0, 'bindings': []},
                          read=lambda ref, path: calls.append((ref, path)))
    if legacy:
        closure.read = lambda physical, path: value_at(closure.payload(physical), physical, path)
    try:
        result = CommitPatch.capture(object(), closure, read, ctx, {'phase': 'input'})
        return {'result': result, 'record': ctx.record, 'calls': calls}
    except CommandError as error:
        return {'error': error.to_json(), 'message': str(error), 'record': ctx.record, 'calls': calls}


@pytest.mark.parametrize('path,state', [
    (['name'], {'state': 'present', 'value': 'wrong'}),
    (['missing'], {'state': 'absent'}),
    (['config', 'missing'], {'state': 'absent'}),
    (['clips'], None),
    (['config', 'clips'], None),
    (['occurrences'], None),
    ([], None),
])
def test_parent_capture_matches_materialized_projection(path, state):
    bundle = bundle_from_document(document_fixture(shared=False))
    old, new = Closure(copy.deepcopy(bundle)), Closure(copy.deepcopy(bundle))
    if state is None:
        state = value_at(old.payload('parent'), 'parent', path)
    read = {'kind': 'field', 'ref': 'parent', 'path': path, 'state': state}
    before = copy.deepcopy(bundle)
    actual, baseline = capture(new, read), capture(old, read, legacy=True)
    assert json.dumps(actual, sort_keys=True) == json.dumps(baseline, sort_keys=True)
    assert bundle == before
    if 'result' in actual:
        actual['result']['path'].append('changed')
        assert new.bundle == before


def test_scalar_parent_capture_does_not_materialize_payload(monkeypatch):
    closure = Closure(bundle_from_document(document_fixture(shared=False)))
    state = value_at(closure.payload('parent'), 'parent', ['name'])
    monkeypatch.setattr(closure, 'payload', lambda _: pytest.fail('unrelated payload materialized'))
    result = capture(closure, {'kind': 'field', 'ref': 'parent', 'path': ['name'], 'state': state})
    assert result['result']['state'] == state


def test_container_capture_does_not_alias_parent():
    closure = Closure(bundle_from_document(document_fixture(shared=False)))
    state = value_at(closure.payload('parent'), 'parent', ['config'])
    before = copy.deepcopy(closure.bundle)
    result = capture(closure, {'kind': 'field', 'ref': 'parent', 'path': ['config'], 'state': state})
    result['result']['state']['value']['new'] = ['changed']
    result['record']['bindings'][0]['dependency']['state']['value']['other'] = True
    assert closure.bundle == before
