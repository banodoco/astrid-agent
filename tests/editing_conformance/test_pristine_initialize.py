"""Exact App bootstrap capture, authored nulls, order and guarded reversal."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commands import execute, validate_command
from astrid.sdk.timeline.commit import (
    _digest,
    bundle_from_document,
    document_from_bundle,
)
from astrid.sdk.timeline.native_initialize import capture_initialize
from astrid.sdk.timeline.rebase import replay

FIXTURE = Path(__file__).parent / 'fixtures/pristine_initialize.json'
GOLDEN = json.loads(FIXTURE.read_text())


def empty_document():
    payload = {'config': {'clips': [], 'tracks': []}, 'registry': {}, 'clips': [], 'occurrences': []}
    return {'schema_version': 1, 'project_id': 'project-1', 'timeline_id': 'film',
            'parent_revision': {'revision_id': 'bootstrap-revision', 'payload': payload,
                                'content_digest': _digest(payload)},
            'shot_revisions': [], 'internal_timeline_revisions': [],
            'dependency_manifest': {'shots': [], 'internal_timelines': [], 'media': [], 'documents': []}}


def batch(bundle):
    parent = GOLDEN['app_parent']
    calls = [capture_initialize(bundle, name=parent['name'], slug=parent['slug'],
                                output=parent['config']['output'])]
    for row in parent['config']['tracks']:
        current = execute(bundle, calls).bundle
        calls.append(capture_track_create(current, 'parent', row))
    return calls


def inverse(bundle, records, intervening=()):
    call = {'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
            'args': {'publications': ['accepted-init']}}
    return execute(bundle, [call], inverse_records=records, intervening_records=intervening)


def test_app_golden_exact_capture_replay_inverse_and_redo():
    original = bundle_from_document(empty_document())
    before = copy.deepcopy(original)
    calls = batch(original)
    assert calls == GOLDEN['commands']
    for call in calls:
        validate_command(call)
    plan = execute(original, calls)
    assert original == before
    assert plan.records == GOLDEN['records']
    assert document_from_bundle(plan.bundle)['parent_revision']['payload'] == GOLDEN['app_parent']
    assert replay(plan, original).records == plan.records
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == original
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle
    assert all(write['ref'] == 'parent' and write['path'] for write in plan.records[0]['writes'])
    assert plan.records[0]['writes'][-1]['path'] == ['registry', 'assets']
    assert 'registry' not in plan.bundle['parent']['config']
    assert all(key not in plan.bundle['parent']['config']
               for key in ('theme', 'theme_overrides', 'generation_defaults'))


def test_initialize_preserves_tracks_supplied_by_runtime_bootstrap():
    document = empty_document()
    tracks = [{'id': 'runtime-track', 'kind': 'visual', 'label': 'Runtime track'}]
    payload = document['parent_revision']['payload']
    payload['config']['tracks'] = tracks
    document['parent_revision']['content_digest'] = _digest(payload)
    original = bundle_from_document(document)
    call = capture_initialize(original, name='Main', slug='main',
                              output=GOLDEN['app_parent']['config']['output'])

    initialized = execute(original, [call]).bundle

    assert initialized['parent']['config']['tracks'] == tracks


@pytest.mark.parametrize('path,value', [
    (['config', 'output'], None), (['name'], None), (['slug'], ''),
    (['registry', 'assets'], {}), (['registry', 'opaque'], [None, False]),
    (['config', 'opaque'], {'enabled': False}), (['opaque'], {'literal': 'V1'}),
    (['config', 'tracks'], 'not-a-list'),
    (['config', 'clips'], [{'id': 'later'}]), (['clips'], [{'id': 'later'}]),
])
def test_nonpristine_and_opaque_content_is_untouched(path, value):
    original = bundle_from_document(empty_document())
    node = original['parent']
    for token in path[:-1]:
        node = node[token]
    node[path[-1]] = value
    before = copy.deepcopy(original)
    with pytest.raises(CommandError):
        execute(original, [GOLDEN['commands'][0]])
    assert original == before


@pytest.mark.parametrize('mutation', ['absent-null', 'extra-output', 'extra-arg', 'version',
                                     'select', 'intent', 'fps-bool', 'resolution'])
def test_closed_typed_versioned_arguments(mutation):
    command = copy.deepcopy(GOLDEN['commands'][0])
    if mutation == 'absent-null':
        del command['args']['output']['background']
    elif mutation == 'extra-output':
        command['args']['output']['opaque'] = None
    elif mutation == 'extra-arg':
        command['args']['pristine'] = True
    elif mutation in ('version', 'select'):
        command[mutation] = 'other'
    elif mutation == 'intent':
        command['intent'] = {'overwrite': True}
    elif mutation == 'fps-bool':
        command['args']['output']['fps'] = True
    else:
        command['args']['output']['resolution'] = '1920x1080'
    with pytest.raises(CommandError):
        validate_command(command)


def test_exact_order_claims_and_intervening_write_guard():
    original = bundle_from_document(empty_document())
    plan = execute(original, batch(original))
    changed = copy.deepcopy(plan.bundle)
    changed['parent']['config']['tracks'].reverse()
    with pytest.raises(CommandError):
        inverse(changed, plan.records)
    write = copy.deepcopy(plan.records[0]['writes'][0])
    write['before'] = write['after']
    with pytest.raises(CommandError) as error:
        inverse(plan.bundle, plan.records, [{'writes': [write]}])
    assert error.value.kind == 'intervening_write'
    forged = copy.deepcopy(plan.commands)
    forged[0]['reads'][0]['state']['value']['opaque'] = None
    with pytest.raises(CommandError):
        execute(original, forged)
    # A different raw parent cannot be treated as an equivalent empty basis.
    changed = copy.deepcopy(original)
    changed['parent']['registry']['opaque'] = [None]
    with pytest.raises(CommandError):
        replay(plan, changed)


def test_additional_timeline_names_and_app_empty_name_are_preserved():
    original = bundle_from_document(empty_document())
    for name, slug in (('Second Cut', 'second-cut'), ('', 'timeline')):
        call = capture_initialize(original, name=name, slug=slug,
                                  output=GOLDEN['app_parent']['config']['output'])
        plan = execute(original, [call])
        assert plan.bundle['parent']['name'] == name
        assert plan.bundle['parent']['slug'] == slug
        assert inverse(plan.bundle, plan.records).bundle == original


def test_complete_s2_schema_is_retained_additively():
    schema = json.loads((Path(__file__).parents[2] /
                         'astrid/core/timeline/schema/edit-command.schema.json').read_text())
    from tests.sdk.test_timeline_current_union_schema import (
        without_doc_patch,
        without_inverse_steps,
        without_text,
    )

    # The frozen S2 slice is retained inside the current schema, which also
    # carries the approved doc.patch and per-step inverse additions.
    assert len(schema['$defs']) == 58
    schema = without_inverse_steps(without_doc_patch(without_text(schema)))
    for name in ('AssembledTargetArgs', 'ClipAddMediaArgs', 'WordAnchor', 'ClipAnchorArgs',
                 'OccurrenceCreateArgs'):
        del schema['$defs'][name]
    del schema['$defs']['TimelineInitArgs']
    conditions = schema['$defs']['EditCommand']['allOf']
    added = {'timeline.init', 'timeline.assembled-target', 'clip.add-media', 'clip.anchor',
             'occurrence.create', 'clip.swap-media'}
    assert {item['if']['properties']['op']['const'] for item in conditions} >= added
    conditions[:] = [item for item in conditions
                     if item['if']['properties']['op']['const'] not in added]
    assert len(conditions) == 22
    frozen = json.loads((Path(__file__).parents[1] / 'fixtures/current-union-command-schemas' /
                         'agent-frozen-s2-26ef2bbb.json').read_text())
    assert schema['$defs']['Intent']['properties']['ownership']['enum'].pop() == 'parent'
    schema['$defs']['ClipSetArgs']['description'] = frozen['$defs']['ClipSetArgs']['description']
    canonical = json.dumps(schema, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    assert hashlib.sha256(canonical).hexdigest() == GOLDEN['s2_semantic_sha256']
