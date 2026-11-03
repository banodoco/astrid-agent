"""Offline producer custody checks; Runtime acceptance remains a separate gate."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import present
from astrid.sdk.timeline._recording import Recording
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _json, bundle_from_document, document_from_bundle
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.sdk.test_timeline_commit_provider import DetachedRealm, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def native(ref, field, value, *, op='track.set'):
    return {'schema_version': 1, 'op': op, 'version': '1', 'select': ref,
            'args': {'values': {field: value}, 'clear': []}}


@pytest.mark.parametrize('separate', [False, True])
def test_next_command_binds_complete_materialized_child_output(separate):
    bundle = bundle_from_document(document_fixture(shared=False))
    calls = [native('occurrence:occ-a/track:picture', 'label', 'Child'),
             native('parent/track:picture', 'label', 'Parent')]
    request = {'reads': []}
    if separate:
        request['steps'] = [step(str(index), index, call) for index, call in enumerate(calls)]
    original = copy.deepcopy(bundle)
    plan = execute(bundle, calls, commit_request=request)
    first, second = plan._sequential_execution
    before, after = first['input'], second['input']
    bindings = [binding for binding in plan.records[1]['bindings']
                if binding.get('boundary', {}).get('phase') == 'input']
    for family, prefix in (('shot_revisions', 'shot-revision:'),
                           ('internal_timeline_revisions', 'internal-revision:')):
        assert before[family][0]['revision_id'] != after[family][0]['revision_id']
        image = after[family][0]
        assert any(binding.get('physical_ref') == prefix + image['revision_id']
                   and binding.get('content_digest') == image['content_digest']
                   and binding.get('owners') == ['occurrence:occ-a/shot']
                   for binding in bindings)
    assert _json(bundle) == _json(original)


def test_legacy_materialization_has_source_effect_pins_for_inverse():
    realm = DetachedRealm(document_fixture(shared=False))
    realm.call([native('occurrence:occ-a/track:picture', 'label', 'Changed')])
    record = realm.entries[-1]['records']['commands'][0]
    materializations = [binding for binding in record['bindings']
                        if binding['key'] == 'commit.materialization']
    assert len(materializations) == 2
    for source in materializations:
        assert any(binding['key'] == 'commit.effect'
                   and binding['physical_ref'] == source['physical_ref']
                   and binding['revision_id'] == source['revision_id']
                   and binding['content_digest'] == source['content_digest']
                   and binding['boundary']['side'] == 'before'
                   for binding in record['bindings'])


@pytest.mark.parametrize('before,after', [(2, 2.0), (1, True), (True, 1)])
def test_single_inverse_keeps_endpoint_materializations_for_redo(before, after):
    document = document_fixture(shared=False)
    child = document['internal_timeline_revisions'][0]
    child['payload']['clips'][0]['params'] = {'x': before}
    from astrid.sdk.timeline.commit import _digest, _manifest

    child['content_digest'] = _digest(child['payload'])
    document['dependency_manifest'] = _manifest(
        document['shot_revisions'], [child], document['dependency_manifest']['media'], [])
    realm = DetachedRealm(document)
    changed = submit(realm, native('occurrence:occ-a/clip:clip-a', 'x', after,
                                  op='clip.set-params'), reads=[])
    undone = submit(realm, inverse([changed['receipt']['receipt_id']]), reads=[])
    assert _json(undone['data']['canonical_document']) == _json(document)
    record = realm.entries[-1]['records']['commands'][0]
    materializations = [binding for binding in record['bindings']
                        if binding['key'] == 'commit.materialization']
    assert len(materializations) == 2
    redone = submit(realm, inverse([undone['receipt']['receipt_id']]), reads=[])
    assert _json(redone['data']['canonical_document']) == _json(changed['data']['canonical_document'])


def test_patch_materialization_preserves_numeric_encoding():
    document = document_fixture(shared=False)
    child = document['internal_timeline_revisions'][0]
    child['payload']['unknown'] = {'x': 2}
    from astrid.sdk.timeline.commit import _digest, _manifest

    child['content_digest'] = _digest(child['payload'])
    document['dependency_manifest'] = _manifest(
        document['shot_revisions'], [child], document['dependency_manifest']['media'], [])
    bundle = bundle_from_document(document)
    plan = execute(bundle, [patch(change(['unknown', 'x'], present(2), 2.0,
                                        ref='internal-revision:internal-1'))], commit_request={'reads': []})
    image = document_from_bundle(plan.bundle)['internal_timeline_revisions'][0]
    assert type(image['payload']['unknown']['x']) is float
    assert image['revision_id'] != child['revision_id']


def test_compile_inputs_exposes_recorded_parent_mirrors_to_recipe_consumer(monkeypatch):
    from astrid.sdk.timeline.lookup import _LookupMixin

    original = Recording.compile_inputs

    def consumer(ctx):
        original(ctx)
        # Recipe validation uses the same production lookup before timing
        # compilation. It must see the native edit in both physical locations.
        clips = _LookupMixin._parent_clip_list(ctx.state)
        assert clips[0]['params']['note'] == 'Changed'

    monkeypatch.setattr(Recording, 'compile_inputs', consumer)
    bundle = bundle_from_document(document_fixture(shared=False))
    before = copy.deepcopy(bundle)
    plan = execute(bundle, [native('parent/clip:clip-a', 'note', 'Changed', op='clip.set-params')])
    assert _json(bundle) == _json(before)
    mirrors = plan.records[0]['derived_writes']
    assert len(mirrors) == 1
    assert mirrors[0]['before'] == present(before['parent']['config']['clips'])
    assert mirrors[0]['after']['value'][0]['params']['note'] == 'Changed'
