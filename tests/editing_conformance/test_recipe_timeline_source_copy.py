"""Lossless source-copy semantics, distinct from Runtime input authentication."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, document_from_bundle
from astrid.sdk.timeline.native_timeline_duplicate import copy_command, resolve_source
from tests.editing_conformance.test_recipe_occurrence_create import empty_bundle
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def inputs(*, shared=True, retained=False):
    document = document_fixture(shared=shared)
    for row in document['parent_revision']['payload']['occurrences']:
        row['source_offset'] = 0
    # Opaque identity-looking strings must remain opaque in all payloads.
    document['parent_revision']['payload']['meta'] = {'org.example': {'literal': 'chapter-shot'}}
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    configuration = {'document_id': 'copy-config', 'project_id': document['project_id'],
                     'kind': 'copy.config', 'version': 1, 'content': {'opaque': [None, False, 2]}}
    pin = {'document_id': configuration['document_id'], 'version': 1,
           'content_digest': _digest(configuration['content'])}
    if retained:
        document['dependency_manifest']['documents'] = [pin]
        unused = 'sha256:' + '2' * 64
        document['dependency_manifest']['media'].append({'media_id': unused, 'content_digest': unused})
    target = empty_bundle()
    target['project_id'] = document['project_id']
    target['timeline_id'] = 'destination'
    target['source_mapping']['_runtime_document']['project_id'] = document['project_id']
    target['source_mapping']['_runtime_document']['timeline_id'] = 'destination'
    ref = {'project_id': document['project_id'], 'timeline_id': document['timeline_id'],
           'revision_id': document['parent_revision']['revision_id'], 'publication_id': 'accepted-source',
           'content_digest': document['parent_revision']['content_digest']}
    command = copy_command(ref, {'chapter-shot': 'fresh-shot'},
        {row['occurrence_id']: 'fresh-' + row['occurrence_id']
         for row in document['parent_revision']['payload']['occurrences']})
    captured = []
    class Port:
        def revision_closure(self, pin):
            assert pin == ref
            return copy.deepcopy(document)
        def config_source(self, requested):
            assert requested == pin and retained
            return {'pin': copy.deepcopy(pin), 'document': copy.deepcopy(configuration)}
        def raw_media_info(self, pin):
            assert pin in document['dependency_manifest']['media']
            captured.append(copy.deepcopy(pin))
    return target, document, command, resolve_source(Port(), lambda ctx: []), captured


@pytest.mark.parametrize('shared', [False, True])
def test_source_copy_keeps_raw_parent_closure_order_sharing_and_opaque_data(shared):
    target, source, command, resolver, captured = inputs(shared=shared)
    before, original = copy.deepcopy(target), copy.deepcopy(source)
    plan = execute(target, [command], resolve_inputs=resolver)
    result = document_from_bundle(plan.bundle)
    parent = copy.deepcopy(result['parent_revision']['payload'])
    rows = parent.pop('occurrences')
    want = copy.deepcopy(source['parent_revision']['payload'])
    source_rows = want.pop('occurrences')
    assert parent == want
    assert [row['occurrence_id'] for row in rows] == ['fresh-' + row['occurrence_id'] for row in source_rows]
    assert {row['shot_id'] for row in rows} == {'fresh-shot'}
    assert len({row['shot_revision_id'] for row in rows}) == 1
    assert len(result['shot_revisions']) == len(result['internal_timeline_revisions']) == 1
    assert result['internal_timeline_revisions'][0]['payload'] == source['internal_timeline_revisions'][0]['payload']
    payload = copy.deepcopy(result['shot_revisions'][0]['payload'])
    expected = copy.deepcopy(source['shot_revisions'][0]['payload'])
    payload.pop('internal_timeline_revision_id')
    expected.pop('internal_timeline_revision_id')
    assert payload['items'][0]['item_id'] != expected['items'][0]['item_id']
    payload['items'][0]['item_id'] = expected['items'][0]['item_id']
    assert payload == expected
    assert result['dependency_manifest']['media'] == source['dependency_manifest']['media'] == captured
    assert result['parent_revision']['content_digest'] == _digest(result['parent_revision']['payload'])
    assert source == original and target == before
    assert plan.records[0]['dependencies'][0]['pin'] == command['args']['source_ref']


def test_unverified_source_bytes_are_never_an_input_authority():
    target, source, command, _, _ = inputs()
    before = copy.deepcopy(target)
    with pytest.raises(CommandError, match='Runtime-authenticated'):
        execute(target, [command])
    assert target == before
    forged = copy.deepcopy(command)
    forged['args']['document'] = source
    with pytest.raises(CommandError):
        execute(target, [forged])
    assert target == before


@pytest.mark.parametrize('claim', ['created', 'edge', 'scope', 'shot', 'occurrence', 'order'])
def test_source_copy_rejects_forged_claims_and_incomplete_maps_atomically(claim):
    target, source, command, resolver, _ = inputs()
    if claim == 'created':
        command['created'].append('shot:forged')
    elif claim == 'edge':
        command['args']['shot_revision_id'] = source['shot_revisions'][0]['revision_id']
    elif claim == 'scope':
        command['select'] = 'occurrence:foreign'
    elif claim == 'shot':
        command['args']['shots']['chapter-shot'] = 'chapter-shot'
    elif claim == 'occurrence':
        command['args']['occurrences']['occ-b'] = 'fresh-occ-a'
    else:
        command['created'] = list(reversed(command['created']))
    before = copy.deepcopy(target)
    with pytest.raises(CommandError):
        execute(target, [command], resolve_inputs=resolver)
    assert target == before


def test_source_copy_preserves_retained_media_and_documents_and_records_inputs():
    target, source, command, resolver, captured = inputs(retained=True)
    plan = execute(target, [command], resolve_inputs=resolver)
    document = document_from_bundle(plan.bundle)
    manifest = source['dependency_manifest']
    assert document['dependency_manifest']['media'] == manifest['media'] == captured
    assert document['dependency_manifest']['documents'] == manifest['documents']
    dependencies = plan.records[0]['dependencies']
    assert all(any(row['kind'] == 'media' and row['pin'] == pin for row in dependencies)
               for pin in manifest['media'])
    assert any(row['kind'] == 'source' and row['pin'] == manifest['documents'][0] for row in dependencies)
