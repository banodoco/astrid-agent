"""Portable native picker captures, derived caches and closed parameter schema."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.core.timeline.authoring_bundle import _digest
from astrid.sdk.media_transcript import build_transcript, transcript_document_id
from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline.commands import execute, validate_command
from astrid.sdk.timeline.commit import _manifest, bundle_from_document
from astrid.sdk.timeline.native_word_anchor import capture_anchor
from tests.sdk.test_timeline_commit_provider_document import MEDIA, document_fixture

FIXTURE = Path(__file__).parent / 'fixtures' / 'native_word_anchor.json'


def fixture():
    document = document_fixture(shared=False)
    parent = document['parent_revision']['payload']
    parent['clips'] = [parent['clips'][0]]
    parent['clips'][0]['app']['astrid'] = {'for': 0.5, 'opaque': [None, False]}
    parent['config']['clips'] = copy.deepcopy(parent['clips'])
    parent['config']['theme_overrides'] = {'visual': {'canvas': {
        'fps': {'numerator': 30000, 'denominator': 1001}}}}
    occurrence = parent['occurrences'][0]
    occurrence.update(source_offset=0, speed=1)
    occurrence['placement']['start_ms'] = 2000
    internal = document['internal_timeline_revisions'][0]['payload']
    internal.pop('source_window')
    internal['clips'] = [{'id': 'voice', 'clipType': 'media', 'track': 'vo', 'asset': 'picture',
                           'at': 1, 'from': 2, 'to': 6, 'speed': 2}]
    internal['tracks'] = [{'id': 'vo', 'kind': 'audio'}]
    internal['registry']['assets']['picture']['type'] = 'audio'
    for image in [document['parent_revision'], *document['internal_timeline_revisions']]:
        image['content_digest'] = _digest(image['payload'])
    document['dependency_manifest'] = _manifest(document['shot_revisions'],
        document['internal_timeline_revisions'], document['dependency_manifest']['media'], [])
    content = build_transcript(MEDIA, 'line', 'again again stop',
                               [[2, 2.5, 'again'], [3, 3.5, 'again'], [5.5, 6, 'stop']])
    identity = transcript_document_id(document['project_id'], MEDIA, content['source_id'])
    pin = {'document_id': identity, 'version': 1, 'content_digest': _digest(content)}
    bundle = bundle_from_document(document)
    bundle['source_mapping']['pinned_documents'] = [{'pin': pin, 'document': {
        'document_id': identity, 'project_id': document['project_id'], 'version': 1,
        'kind': 'media.transcript', 'content': content}}]
    return bundle, pin, content


def vector(name):
    bundle, pin, content = fixture()
    choice = {'source': 'occurrence:occ-a/clip:voice', 'document': pin,
              'word_ids': [content['segments'][0]['words'][name == 'b']['id']]}
    ref = 'parent/clip:clip-a'
    if name == 'clear':
        selected = capture_anchor(bundle, ref, 'on', 'again', choice)
        bundle = execute(bundle, [selected]).bundle
        choice = None
    command = capture_anchor(bundle, ref, 'on', None if name == 'clear' else 'again', choice)
    plan = execute(bundle, [command])
    return {'command': command, 'records': plan.records, 'clip': plan.bundle['parent']['clips'][0]}


@pytest.mark.parametrize('name', ['a', 'b', 'clear'])
def test_portable_native_captures(name):
    assert vector(name) == json.loads(FIXTURE.read_text())[name]


def test_closed_native_schema_refuses_inline_transcript_authority():
    # Full frozen-S2 containment is guarded separately; these captures retain
    # the closed word-choice envelope in that additive successor.
    from astrid.sdk.timeline.commands import _SCHEMA, operations

    assert operations()['clip.anchor'].parameters == _SCHEMA['$defs']['ClipAnchorArgs']
    for name in ('a', 'b', 'clear'):
        validate_command(vector(name)['command'])
    invalid = vector('a')['command']
    invalid['args']['choice']['inline_transcript'] = {'segments': []}
    with pytest.raises(CommandError):
        validate_command(invalid)


def test_capture_refuses_missing_verified_pin_and_unsupported_target_without_mutation():
    bundle, _, _ = fixture()
    command = vector('a')['command']
    bundle['source_mapping']['pinned_documents'] = []
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError, match='exact transcript pin is missing'):
        execute(bundle, [command])
    assert bundle == before
    command['select'] = 'shot:chapter-shot/clip:clip-a'
    with pytest.raises(CommandError):
        execute(bundle, [command])
    assert bundle == before


def test_clear_removes_legacy_echo_without_revealing_a_previous_text_binding():
    bundle, _, _ = fixture()
    bundle['parent']['clips'][0]['app']['on'] = '"again" #2'
    bundle['parent']['config']['clips'] = copy.deepcopy(bundle['parent']['clips'])
    selected = vector('a')['command']
    anchored = execute(bundle, [selected]).bundle
    clear = capture_anchor(anchored, 'parent/clip:clip-a', 'on', None, None)
    cleared = execute(anchored, [clear]).bundle['parent']['clips'][0]['app']
    assert 'on' not in cleared
    assert 'on' not in cleared['astrid']
    assert 'on' not in cleared['astrid']['word_anchors']
    assert cleared['astrid']['opaque'] == [None, False]


def test_null_default_speed_and_unrelated_retained_config_keep_native_word_semantics():
    bundle, _, _ = fixture()
    row = bundle['parent']['clips'][0]
    row['speed'] = None
    row['app']['astrid']['until'] = 'after "stop"'
    bundle['parent']['config']['clips'] = copy.deepcopy(bundle['parent']['clips'])
    bundle['source_mapping']['pinned_documents'].append({'pin': {
        'document_id': 'settings', 'version': 1, 'content_digest': _digest({'opaque': None})},
        'document': {'document_id': 'settings', 'version': 1, 'project_id': bundle['project_id'],
                     'kind': 'fixture.config', 'content': {'opaque': None}}})
    plan = execute(bundle, [vector('a')['command']])
    projected = plan.bundle['parent']['clips'][0]
    assert projected['speed'] is None
    assert projected['hold'] > 1
    assert projected['app']['astrid']['until'] == 'after "stop"'


@pytest.mark.parametrize('damage', ['hidden-clip', 'hidden-track', 'hidden-owner', 'source-window', 'mapped-asset'])
def test_word_choice_refuses_inactive_or_unsupported_source_ownership_before_writes(damage):
    from astrid.sdk.timeline._recording import Recording
    from astrid.sdk.timeline.native_word_anchor import declare

    bundle, _, _ = fixture()
    internal = bundle['shots']['chapter-shot']['internal_timeline']
    if damage == 'hidden-clip':
        internal['clips'][0]['hidden'] = True
    elif damage == 'hidden-track':
        internal['tracks'][0]['enabled'] = False
    elif damage == 'hidden-owner':
        bundle['placements'][0]['active'] = False
    elif damage == 'source-window':
        internal['source_window'] = {'start': 1, 'end': 4}
    else:
        internal['registry']['assets']['picture']['timeMap'] = {'unimplemented': True}
    command = vector('a')['command']
    ctx = Recording(copy.deepcopy(bundle), command, 0)
    with pytest.raises(CommandError, match='active source selection|supported source map'):
        declare(ctx, command)
    assert ctx.record['writes'] == []


@pytest.mark.parametrize('duration', [None, float('inf')])
def test_unresolvable_occurrence_window_refuses_before_declaration_writes(duration):
    from astrid.sdk.timeline._recording import Recording
    from astrid.sdk.timeline.native_word_anchor import declare

    bundle, _, _ = fixture()
    bundle['placements'][0]['duration_ms'] = duration
    ctx = Recording(copy.deepcopy(bundle), vector('a')['command'], 0)
    with pytest.raises(CommandError, match='resolvable occurrence interval'):
        declare(ctx, ctx.record['command'])
    assert ctx.record['writes'] == []
    assert ctx.state.bundle == bundle
