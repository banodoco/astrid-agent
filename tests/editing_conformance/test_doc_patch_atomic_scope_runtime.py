"""Complete JSON commits retain atomic scopes through child materialization."""
from __future__ import annotations

import copy
import json
import sys
from contextlib import nullcontext

import pytest

from astrid.packs.rendering.docs.scripts import timeline_document as helper
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from astrid.sdk.timeline_checkout import Checkout
from tests.editing_conformance import test_commit_runtime_source_copy
from tests.sdk.test_timeline_restructure import flat_film

pytestmark = pytest.mark.integration
saved_source = test_commit_runtime_source_copy.saved_source


def test_complete_json_script_commit_authenticates_materialized_atomic_scopes(
    saved_source, tmp_path, monkeypatch, capsys,
):
    client, project, document, bodies = saved_source(flat_film())
    original = document('t')
    history = client.timelines.history(project, 't', limit=100).data
    base = history['head']
    open_bundle = client.timelines.open_bundle

    def pinned_bundle(p, t, *, revision_id=None, publication_id=None):
        result = open_bundle(p, t, revision_id=revision_id)
        assert (revision_id, publication_id) == (base['revision_id'], base['publication_id'])
        result.data['bundle']['base_parent']['publication_id'] = publication_id
        return result

    client.timelines.open_bundle = pinned_bundle
    monkeypatch.setattr(helper.AstridClient, 'open_from_launcher', lambda **kw: nullcontext(client))
    monkeypatch.setattr(helper, 'workspace', lambda: client._remote._transport)
    checkout = Checkout(bundle_from_document(original))
    source = tmp_path / 'complete.json'
    checkout.save(source, quiet=True)
    candidate = Checkout.load(source).document()
    candidate['parent']['config'].setdefault('app', {}).setdefault('astrid', {})['slots'] = {}
    next(row for row in candidate['shots']['film']['internal_timeline']['clips']
         if row['id'] == 'inside')['params']['x'] = 15
    candidate['base_parent']['publication_id'] = base['publication_id']
    path = tmp_path / 'complete.commit.json'
    path.write_text(json.dumps(candidate))
    capsys.readouterr()

    def run(command):
        monkeypatch.setattr(sys, 'argv', ['timeline_document', command, '--file', str(path),
            *(['--idempotency-key', 'complete-json-atomic'] if command == 'publish' else [])])
        helper.main()
        return json.loads(capsys.readouterr().out)

    assert run('check')['validation']['valid']
    result = run('publish')['publication']
    assert result['data']['status'] == 'accepted' and result['receipt']
    request = bodies[-1]
    assert request['base_revision_id'] == base['revision_id']
    assert request['base_publication_id'] == base['publication_id']
    assert request['merge'] == 'atomic' and request['label'] == 'Publish document'
    assert 'reads' not in request  # Keep the complete-transform conservative fallback.
    assert request['source'] == {'via': 'script'}
    assert request['steps'][0]['command_ordinals'] == [0]
    assert request['steps'][0]['patch'] == request['commands'][0]['args']['changes']
    landed = document('t')
    assert landed == result['data']['canonical_document']
    clip = next(row for row in landed['internal_timeline_revisions'][0]['payload']['clips']
                if row['id'] == 'inside')
    assert clip['params']['x'] == 15
    assert landed['parent_revision']['payload']['config']['app']['astrid']['slots'] == {}
    accepted_history = copy.deepcopy(client.timelines.history(project, 't', limit=100).data)
    assert len(accepted_history['items']) == len(history['items']) + 1
    records = next(row['records'] for row in accepted_history['items']
                   if row['publication_id'] == result['receipt']['receipt_id'])
    assert records['merge_mode'] == 'atomic'
    dependencies = records['steps'][0]['dependencies']
    assert {'kind': 'scope', 'ref': 'timeline', 'path': []} in dependencies
    captures = [binding for binding in records['commands'][0]['bindings']
                if binding['key'] == 'commit.atomic']
    assert {binding['physical_ref'] for binding in captures} == {
        'parent', 'shot-revision:shot-film', 'internal-revision:internal-film'}
    assert all(binding['boundary']['phase'] == 'input' for binding in captures)
    assert run('publish')['publication'] == result
    send = client._remote._transport._generated.commit_timeline
    replay = send(project, 't', request, idempotency_key=result['receipt']['idempotency_key'])
    assert replay['receipt'] == result['receipt']
    assert replay['data']['canonical_document'] == landed
    assert send(project, 't', request, idempotency_key=result['receipt']['idempotency_key']) == replay
    assert client.timelines.history(project, 't', limit=100).data == accepted_history
    assert document('t') == landed


def test_atomic_fallback_union_covers_surviving_roots_at_every_step(saved_source):
    from astrid.sdk.timeline.native_occurrence_delete import capture_occurrence_delete
    from tests.editing_conformance.test_doc_patch_v2 import change, patch, step

    source = flat_film()
    source['shots']['untouched'] = copy.deepcopy(source['shots']['film'])
    source['placements'].append({**copy.deepcopy(source['placements'][0]),
        'shot_id': 'untouched', 'occurrence_id': 'occ-untouched', 'placement': {'start_ms': 12000}})
    client, project, document, bodies = saved_source(source)
    original = document('t')
    base = client.timelines.history(project, 't', limit=100).data['head']
    first = capture_occurrence_delete(bundle_from_document(original), 'occurrence:occ-film')
    second = patch(change(['name'], {'state': 'absent'}, 'Remaining film'))
    request = {'api_version': 1, 'project_id': project, 'timeline_id': 't',
        'base_revision_id': base['revision_id'], 'base_publication_id': base['publication_id'],
        'commands': [first, second], 'label': 'Remove and annotate', 'merge': 'atomic',
        'steps': [step('remove', 0, first), step('name', 1, second)]}
    result = client._remote._transport._generated.commit_timeline(
        project, 't', request, idempotency_key='atomic-removed-root')
    assert result['data']['status'] == 'accepted'
    landed = document('t')
    assert landed == result['data']['canonical_document']
    assert landed['parent_revision']['payload']['name'] == 'Remaining film'
    assert [row['occurrence_id'] for row in landed['parent_revision']['payload']['occurrences']] == ['occ-untouched']
    assert landed['shot_revisions'] == [row for row in original['shot_revisions']
                                       if row['revision_id'] == 'shot-untouched']
    assert landed['internal_timeline_revisions'] == [row for row in original['internal_timeline_revisions']
                                                    if row['revision_id'] == 'internal-untouched']
    history = client.timelines.history(project, 't', limit=100).data
    records = next(row['records'] for row in history['items']
                   if row['publication_id'] == result['receipt']['receipt_id'])
    for ordinal, recorded_step in enumerate(records['steps']):
        assert {'kind': 'scope', 'ref': 'timeline', 'path': []} in recorded_step['dependencies']
        captures = {binding['physical_ref'] for binding in records['commands'][ordinal]['bindings']
                    if binding['key'] == 'commit.atomic'}
        expected = {'parent', 'shot-revision:shot-untouched', 'internal-revision:internal-untouched'}
        if ordinal == 0:
            expected.update({'shot-revision:shot-film', 'internal-revision:internal-film'})
        assert captures == expected
    assert document('t') == landed


def test_atomic_batch_union_captures_each_sequential_step_input(saved_source):
    from tests.editing_conformance.test_doc_patch_v2 import change, patch, step

    client, project, document, bodies = saved_source(flat_film())
    original = document('t')
    history = client.timelines.history(project, 't', limit=100).data
    base = history['head']
    first = patch(change(['name'], {'state': 'absent'}, 'Sequential film'))
    second = patch(change(['clips', 'id:inside', 'params', 'x'], {'state': 'absent'},
                          15, ref='internal-revision:internal-film'))
    read = {'kind': 'field', 'ref': 'parent', 'path': ['name'],
            'state': {'state': 'present', 'value': 'Sequential film'}}
    request = {'api_version': 1, 'project_id': project, 'timeline_id': 't',
        'base_revision_id': base['revision_id'], 'base_publication_id': base['publication_id'],
        'commands': [first, second], 'label': 'Sequential atomic JSON', 'merge': 'atomic',
        'steps': [step('name', 0, first), step('child', 1, second, reads=[read])],
        'dry_run': False, 'source': {'via': 'script'}, 'rationale': 'Complete atomic union',
        'group_id': 'atomic-boundaries'}
    send = client._remote._transport._generated.commit_timeline
    preview = send(project, 't', {**request, 'dry_run': True}, idempotency_key='preview')
    assert preview['receipt'] is None
    assert preview['data']['would_status'] == 'accepted'
    assert document('t') == original
    assert client.timelines.history(project, 't', limit=100).data == history
    result = send(project, 't', request, idempotency_key='sequential-atomic')
    assert result['data']['status'] == 'accepted'
    assert document('t') == result['data']['canonical_document']
    accepted_history = client.timelines.history(project, 't', limit=100).data
    entry = next(row for row in accepted_history['items']
                 if row['publication_id'] == result['receipt']['receipt_id'])
    assert entry['source'] == request['source']
    assert entry['rationale'] == request['rationale'] and entry['group_id'] == request['group_id']
    records = entry['records']
    parent = copy.deepcopy(original['parent_revision']['payload'])
    parent['name'] = 'Sequential film'
    for ordinal, recorded_step in enumerate(records['steps']):
        assert {'kind': 'scope', 'ref': 'timeline', 'path': []} in recorded_step['dependencies']
        captures = [binding for binding in records['commands'][ordinal]['bindings']
                    if binding['key'] == 'commit.atomic']
        assert {binding['physical_ref'] for binding in captures} == {
            'parent', 'shot-revision:shot-film', 'internal-revision:internal-film'}
        assert all(binding['boundary']['command_ordinal'] == ordinal
                   and binding['boundary']['step_id'] == recorded_step['id']
                   and binding['boundary']['phase'] == 'input' for binding in captures)
        captured_parent = next(binding for binding in captures if binding['physical_ref'] == 'parent')
        assert captured_parent['content_digest'] == (
            original['parent_revision']['content_digest'] if ordinal == 0 else _digest(parent))
    assert read in records['steps'][1]['dependencies']
    assert send(project, 't', request, idempotency_key='sequential-atomic') == result
    assert client.timelines.history(project, 't', limit=100).data == accepted_history
