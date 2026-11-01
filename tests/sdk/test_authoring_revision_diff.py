from __future__ import annotations

import argparse
import copy
from types import SimpleNamespace

import pytest

from astrid.packs.timeline.cli import _cmd_diff, _configure_diff
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.remote import RemoteTimelines
from tests.timeline.test_authoring_bundle import _closure, _digest


class HistoricalReader(RemoteTimelines):
    def __init__(self):
        parent, shots, internals = _closure(shared=True)
        second_parent, second_shots, second_internals = copy.deepcopy((parent, shots, internals))
        second_parent['revision_id'] = 'parent-2'
        second_parent['payload']['occurrences'][0]['shot_revision_id'] = 'shot-rev-2'
        second_parent['payload']['occurrences'][1]['shot_revision_id'] = 'shot-rev-2'
        second_shots[0]['revision_id'] = 'shot-rev-2'
        second_shots[0]['internal_timeline_revision_id'] = 'internal-2'
        second_shots[0]['payload']['internal_timeline_revision_id'] = 'internal-2'
        second_shots[0]['payload']['metadata']['label'] = 'changed'
        second_internals[0]['revision_id'] = 'internal-2'
        second_internals[0]['payload']['clips'][0]['rect']['x'] = .4
        for row in (second_parent, *second_shots, *second_internals):
            row['content_digest'] = _digest(row['payload'])
        self.rows = {row['revision_id']: row for row in (parent, *shots, *internals, second_parent, *second_shots, *second_internals)}
        self.calls = []
        self.scope = {'timeline_id': 'main', 'project_ref': 'project-slug', 'project_id': 'project-1',
                      'head_revision_id': 'future-parent-99'}

    def _resolve_timeline(self, project, ref):
        self.calls.append(('resolve', project, ref))
        return DomainResult.success(self.scope)

    def open_composition(self, project, ref, *, revision_id=None, **kwargs):
        # The Runtime inspection read only names the timeline identity; the
        # pinned closure itself is read through _typed below.
        self.calls.append(('open_composition', project, ref, revision_id))
        return DomainResult.success({
            'native_inspection': {'project_id': 'project-1', 'timeline_id': 'main',
                                  'head_revision_id': self.scope['head_revision_id']},
            'summary': {},
        })

    def _typed(self, operation, *args, **kwargs):
        self.calls.append((operation, args, kwargs))
        if operation == 'get_project':
            return DomainResult.success({'id': 'project-1'})
        if operation == 'diff_timeline':
            return DomainResult.success({'legacy': kwargs})
        if args[-1] not in self.rows:
            return DomainResult.failure(ErrorObject('not_found', 'revision unavailable', {}))
        return DomainResult.success(self.rows[args[-1]])


def test_exact_old_and_new_closures_ignore_later_current_head_and_preserve_unknown_changes():
    reader = HistoricalReader()
    result = reader.diff('project-slug', 'main', from_revision='parent-1', to_revision='parent-2')
    assert result.ok, result.error
    assert result.data['complete'] is True
    assert result.data['from_revision'] == 'parent-1'
    assert result.data['to_revision'] == 'parent-2'
    assert result.data['summary']['other_properties_changed'] is True
    assert any('rect' in change['path'] for change in result.data['changes'])
    assert any('metadata' in change['path'] for change in result.data['changes'])
    assert all('future-parent-99' not in str(call) for call in reader.calls)
    for operation in ('get_project_shot_revision', 'get_project_timeline_revision'):
        # A shared child is fetched once per historical closure, rather than
        # once per occurrence or from mutable child heads.
        assert sum(call[0] == operation for call in reader.calls) == 2


@pytest.mark.parametrize('damage', ['missing_child', 'foreign_project', 'foreign_timeline', 'wrong_revision', 'corrupt_bytes'])
def test_missing_or_mismatched_pin_fails_without_partial_or_current_fallback(damage):
    reader = HistoricalReader()
    if damage == 'missing_child':
        del reader.rows['internal-2']
    elif damage == 'foreign_project':
        reader.rows['shot-rev-2']['project_id'] = 'foreign'
    elif damage == 'foreign_timeline':
        reader.rows['internal-2']['timeline_id'] = 'foreign'
    elif damage == 'wrong_revision':
        reader.rows['parent-2']['revision_id'] = 'different'
    else:
        reader.rows['internal-2']['payload']['clips'][0]['volume'] = .2
    result = reader.diff('project-slug', 'main', from_revision='parent-1', to_revision='parent-2')
    assert not result.ok
    assert result.data is None
    assert result.error.code in ('not_found', 'integrity_error')
    assert all('future-parent-99' not in str(call) for call in reader.calls)


@pytest.mark.parametrize('kwargs', [{}, {'from_revision': 'parent-1'}, {'to_revision': 'parent-2'},
                                   {'from_version': 1}, {'from_revision': 'parent-1', 'to_revision': 'parent-2', 'from_version': 1}])
def test_incomplete_or_mixed_diff_pairs_are_rejected_before_reads(kwargs):
    reader = HistoricalReader()
    result = reader.diff('project-slug', 'main', **kwargs)
    assert not result.ok and result.error.code == 'validation_error'
    assert reader.calls == []


def test_legacy_pair_remains_explicit_and_preserves_target_reference():
    reader = HistoricalReader()
    result = reader.diff('project-slug', 'main-slug', from_version=1, to_version=2)
    assert result.ok
    assert reader.calls == [('diff_timeline', ('main-slug',), {'from_version': 1, 'to_version': 2})]


def test_cli_revision_pair_parses_and_forwards_exact_identity_without_mutation(monkeypatch):
    parser = argparse.ArgumentParser()
    _configure_diff(parser)
    parsed = parser.parse_args(['main-slug', '--project', 'project-slug', '--from-revision', 'parent-1', '--to-revision', 'parent-2', '--json'])
    calls = []

    def open_bundle(*args, **kwargs):
        calls.append((args, kwargs))
        return DomainResult.failure(ErrorObject('not_found', 'stop after the first read', {}))

    parsed.client = SimpleNamespace(timelines=SimpleNamespace(
        open_bundle=open_bundle,
        diff=lambda *args, **kwargs: pytest.fail('a revision pair is read as two exact bundles, not a legacy diff'),
    ))
    printed = []
    monkeypatch.setattr('astrid.packs.timeline.cli.print_result', lambda result, **kwargs: printed.append((result, kwargs)) or 0)
    assert _cmd_diff(parsed) == 0
    assert calls == [(('project-slug', 'main-slug'), {'revision_id': 'parent-1'})]
    assert printed[0][0].error.code == 'not_found' and printed[0][1] == {'as_json': True}
