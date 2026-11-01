"""Joint successor controls through a normally generated, in-process Runtime.

The missing-source control stays explicit; the separate S3 journey creates its
first occurrence natively. These controls also retain historical word sources.
"""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from astrid.sdk.media_transcript import build_transcript, transcript_document_id
from astrid.sdk.timeline import commands
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from tests.editing_conformance import test_commit_runtime_parent_media as parent
from tests.editing_conformance import test_native_word_anchor as word
from tests.editing_conformance.test_pristine_initialize import GOLDEN, batch
from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration
retained = word.retained


def reopen(editing):
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider

    editing.service.close()
    editing.service = RuntimeService(editing.root)
    editing.service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))


@pytest.fixture(autouse=True)
def _route_retained_cases_through_runtime(request, monkeypatch):
    if 'retained' not in request.fixturenames:
        return

    case = request.getfixturevalue('retained')
    from runtime_protocol.timeline_commit_port import route_path

    def commit_timeline(body, key, authorize=lambda: None):
        result = case.service.commit_timeline(
            case.project, 'film', body, idempotency_key=key, publisher='timing-editor',
            invocation_path=route_path(case.project, 'film'), authorize=authorize)
        document = result['data']['canonical_document']
        case.documents[document['parent_revision']['revision_id']] = document
        return result

    monkeypatch.setattr(case, 'submit', commit_timeline)


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_pristine_init_first_birth_reload_undo_and_missing_word_source(tmp_path, monkeypatch, kind):
    use_runtime()
    from runtime_protocol.errors import RuntimeErrorBase

    realm = parent.media_realm(tmp_path, monkeypatch, kind, historical=False, pristine=True)
    editing = next(realm)
    try:
        original = parent.current(editing)
        init_request = parent.body(editing, batch(bundle_from_document(original)))
        initialized = parent.commit(editing, init_request, 'joint-init')
        assert parent.current(editing)['parent_revision']['payload'] == GOLDEN['app_parent']
        born_request = parent.body(editing, [parent.birth(editing, parent.row(editing, track='V1'))])
        born = parent.commit(editing, born_request, 'joint-birth')
        born_document = born['data']['canonical_document']
        reopen(editing)
        assert parent.current(editing) == born_document
        assert parent.commit(editing, init_request, 'joint-init') == initialized
        assert parent.commit(editing, born_request, 'joint-birth') == born

        content = build_transcript(editing.pins['new']['object_id'], 'line', 'again again',
                                   [[0, .25, 'again'], [.5, .75, 'again']])
        document_id = transcript_document_id(editing.project, content['media_id'], content['source_id'])
        editing.service.create_document(editing.project, {
            'document_id': document_id, 'kind': 'media.transcript', 'content': content},
            idempotency_key='joint-transcript')
        anchor = parent.call('clip.anchor', {'field': 'on', 'declaration': 'again', 'choice': {
            'source': 'occurrence:missing/clip:voice',
            'document': {'document_id': document_id, 'version': 1, 'content_digest': _digest(content)},
            'word_ids': [content['segments'][0]['words'][1]['id']]}}, 'parent/clip:reserved-row')
        before = parent.rows(editing)
        with pytest.raises(RuntimeErrorBase):
            parent.commit(editing, parent.body(editing, [anchor]), 'joint-missing-source')
        assert parent.rows(editing) == before
        assert parent.current(editing) == born_document
        undone_birth = parent.undo(editing, born, 'joint-undo-birth')
        assert undone_birth['data']['canonical_document'] == initialized['data']['canonical_document']
        # Guarded undo includes every later write, including its reversal.
        parent.commit(editing, parent.body(editing, [parent.call('timeline.inverse', {
            'publications': [result['receipt']['receipt_id']
                             for result in (initialized, born, undone_birth)]}, None)]), 'joint-undo-all')
        assert parent.current(editing) == original
        reopen(editing)
        before = parent.rows(editing)
        for request in (init_request, parent.body(editing, init_request['commands'])):
            with pytest.raises(RuntimeErrorBase) as error:
                parent.commit(editing, request, 'joint-reinit-' + request['base_publication_id'])
            assert error.value.details['reason'] == 'not_pristine'
        with monkeypatch.context() as retry:
            retry.setattr(commands, 'execute', lambda *a, **kw: pytest.fail('retry executed semantics'))
            assert parent.commit(editing, init_request, 'joint-init') == initialized
            assert parent.commit(editing, born_request, 'joint-birth') == born
        assert parent.rows(editing) == before
    finally:
        realm.close()


@pytest.mark.parametrize('grouped_undo', [False, True])
def test_first_parent_media_then_stable_same_echo_word_choices_reload_inverse_and_retry(
        retained, monkeypatch, grouped_undo):
    runtime = use_runtime()
    monkeypatch.syspath_prepend(str(runtime / 'tests'))
    from test_generation_publish_v1_settlement import (
        CAPABILITY,
        _effect,
        _output,
        _settle,
    )

    case = retained
    service = case.service
    service.register_executor({'executor_id': 'joint-worker', 'capabilities': [CAPABILITY]},
                              idempotency_key='joint-worker')
    effect = _effect(case.project, groups=[{'group_key': 'main', 'selectors': [
        {'selector': 'image', 'ordinal': 0, 'variant_key': 'image', 'output_port': 'image'}]}])
    task = service.create_task({'capability_id': CAPABILITY,
        'capability_digest': 'sha256:' + hashlib.sha256(CAPABILITY.encode()).hexdigest(),
        'project': case.project, 'settlement_effect': effect, 'idempotency_key': 'joint-task'})
    attempt = service.claim_next({'executor_id': 'joint-worker', 'capability_ids': [CAPABILITY],
        'runtime_epoch': service.health()['runtime_epoch']}, idempotency_key='joint-claim')
    _settle(service, attempt, [_output(b'joint-image', output_port='image', group_key='main',
        variant_key='image', ordinal=0, filename='image.png', media_type='image/png')], effect=effect)
    association = service.managed_outputs(task['task']['id'])[0]
    pin = service.get_project_media_selection(case.project, {
        'association_id': association['association_id'], 'object_id': association['object_id'],
        'version': association['lifecycle']['version']}, authorize=lambda: None)
    track = capture_track_create(word.bundle(case), 'parent', {'id': 'overlay', 'kind': 'visual'})
    case.submit(case.request([track]), 'joint-overlay')
    baseline = copy.deepcopy(case.documents[case.head()['revision_id']])
    assert baseline['parent_revision']['payload'].get('registry', {}).get('assets', {}) == {}
    row = {'id': 'born', 'clipType': 'image', 'track': 'overlay', 'asset': pin['asset'],
           'at': 0, 'hold': .5, 'opaque': [None, False]}
    born_request = case.request([parent.call('clip.add-media', {'row': row, 'media': pin})])
    born = case.submit(born_request, 'joint-parent-birth')
    documents, requests, accepted = [], [], []
    for index in (0, 1):
        choice = {'source': 'occurrence:occ/clip:voice', 'document': case.pin,
                  'word_ids': [case.source['content']['segments'][0]['words'][index]['id']]}
        request = case.request([parent.call('clip.anchor', {
            'field': 'on', 'declaration': 'again', 'choice': choice}, 'parent/clip:born')])
        result = case.submit(request, 'joint-word-' + str(index))
        document = result['data']['canonical_document']
        assert document['dependency_manifest']['documents'] == [case.pin]
        target = next(clip for clip in document['parent_revision']['payload']['clips'] if clip['id'] == 'born')
        assert target['app']['astrid']['word_anchors']['on'] == choice
        assert target['opaque'] == row['opaque']
        documents.append(document)
        requests.append(request)
        accepted.append(result)
    assert documents[0]['parent_revision']['content_digest'] != documents[1]['parent_revision']['content_digest']
    starts = [next(clip['at'] for clip in document['parent_revision']['payload']['clips']
                   if clip['id'] == 'born') for document in documents]
    assert starts[0] < starts[1]
    case.restart()
    loaded = case.service.get_project_parent_composition_revision(
        case.project, 'film', case.head()['revision_id'])
    assert loaded['payload'] == documents[1]['parent_revision']['payload']
    assert word.bundle(case)['parent']['clips'][-1]['app']['astrid']['word_anchors']['on']['word_ids'] == [
        case.source['content']['segments'][0]['words'][1]['id']]
    with monkeypatch.context() as retry:
        retry.setattr(commands, 'execute', lambda *a, **kw: pytest.fail('retry executed semantics'))
        assert case.submit(requests[0], 'joint-word-0') == accepted[0]
        assert case.submit(requests[1], 'joint-word-1') == accepted[1]
        assert case.submit(born_request, 'joint-parent-birth') == born
    undone_b = word.undo(case, accepted[1], 'joint-undo-b')
    assert undone_b['data']['canonical_document'] == documents[0]
    from runtime_protocol.errors import RuntimeErrorBase

    before = case.head()
    with pytest.raises(RuntimeErrorBase) as error:
        word.undo(case, accepted[0], 'joint-guarded-earlier-undo')
    assert error.value.details['reason'] == 'intervening_write' and case.head() == before
    if grouped_undo:
        case.submit(case.request([parent.call('timeline.inverse', {
            'publications': [result['receipt']['receipt_id']
                             for result in (born, *accepted, undone_b)]}, None)]), 'joint-undo-all')
        assert case.documents[case.head()['revision_id']] == baseline
        assert json.dumps(case.documents[case.head()['revision_id']], sort_keys=True) == json.dumps(
            baseline, sort_keys=True)
        for document in documents:
            image = document['parent_revision']
            retained_revision = case.service.get_project_parent_composition_revision(
                case.project, 'film', image['revision_id'])
            assert json.dumps(retained_revision['payload'], sort_keys=True) == json.dumps(
                image['payload'], sort_keys=True)
    else:
        redone_b = word.undo(case, undone_b, 'joint-redo-b')
        assert redone_b['data']['canonical_document'] == documents[1]
        case.restart()
        assert case.submit(requests[1], 'joint-word-1') == accepted[1]
    assert case.service.store.integrity_report()['ok']


@pytest.mark.parametrize('retained,scope', [
    ({'opaque_pin_scope': scope}, scope) for scope in ('parent', 'shot', 'occurrence')
], indirect=['retained'])
def test_grouped_inverse_retains_surviving_opaque_transcript_pin(retained, scope):
    case = retained
    baseline = copy.deepcopy(case.documents[case.head()['revision_id']])
    payload = baseline['parent_revision']['payload']
    owner = (payload if scope == 'parent' else payload['occurrences'][0] if scope == 'occurrence'
             else baseline['shot_revisions'][0]['payload'])
    opaque = owner['opaque_transcript']
    assert opaque == {'pin': case.pin, 'raw': [None, False, 0, 0.0, {}, [], '']}
    assert baseline['dependency_manifest']['documents'] == []
    born = case.submit(case.request([parent.call('clip.add', {'row': {
        'id': 'born', 'clipType': 'am-type', 'track': 'picture', 'at': 0, 'hold': .5}}, 'parent')]),
        'opaque-pin-birth')
    anchored = case.submit(case.request([parent.call('clip.anchor', {
        'field': 'on', 'declaration': 'again', 'choice': {
            'source': 'occurrence:occ/clip:voice', 'document': case.pin,
            'word_ids': [case.source['content']['segments'][0]['words'][0]['id']]}
        }, 'parent/clip:born')]), 'opaque-pin-anchor')
    assert anchored['data']['canonical_document']['dependency_manifest']['documents'] == [case.pin]
    request = case.request([parent.call('timeline.inverse', {'publications': [
        result['receipt']['receipt_id'] for result in (born, anchored)]}, None)])
    undone = case.submit(request, 'opaque-pin-undo')
    document = undone['data']['canonical_document']
    assert document['dependency_manifest']['documents'] == [case.pin]
    assert json.dumps(document['parent_revision']['payload'], sort_keys=True) == json.dumps(
        baseline['parent_revision']['payload'], sort_keys=True)
    for key in ('shot_revisions', 'internal_timeline_revisions'):
        assert json.dumps(document[key], sort_keys=True) == json.dumps(baseline[key], sort_keys=True)
    redone = word.undo(case, undone, 'opaque-pin-redo')
    assert json.dumps(redone['data']['canonical_document'], sort_keys=True) == json.dumps(
        anchored['data']['canonical_document'], sort_keys=True)
    case.restart()
    assert case.submit(request, 'opaque-pin-undo') == undone


@pytest.mark.parametrize('retention', ['introduced', 'preexisting', 'surviving', 'opaque'])
def test_grouped_clip_cache_inverse_keeps_only_owned_transcript_pins(retained, retention):
    case = retained
    if retention == 'preexisting':
        case.submit(case.request([word.pick(case)]), 'pin-preexisting')
    if retention == 'opaque':
        case.submit(case.request([parent.call('clip.add', {'row': {
            'id': 'opaque', 'clipType': 'am-type', 'track': 'picture', 'at': 0, 'hold': .5,
            'opaque': case.pin}}, 'parent')]), 'pin-opaque')
    baseline = copy.deepcopy(case.documents[case.head()['revision_id']])
    born = case.submit(case.request([parent.call('clip.add', {'row': {
        'id': 'born', 'clipType': 'am-type', 'track': 'picture', 'at': 0, 'hold': .5}}, 'parent')]), 'pin-birth')
    choice = {'source': 'occurrence:occ/clip:voice', 'document': case.pin,
              'word_ids': [case.source['content']['segments'][0]['words'][0]['id']]}
    anchor = case.submit(case.request([parent.call('clip.anchor', {
        'field': 'on', 'declaration': 'again', 'choice': choice}, 'parent/clip:born')]), 'pin-anchor')
    if retention == 'surviving':
        case.submit(case.request([word.pick(case)]), 'pin-surviving-anchor')
    request = case.request([parent.call('timeline.inverse', {'publications': [
        result['receipt']['receipt_id'] for result in (born, anchor)]}, None)])
    undone = case.submit(request, 'pin-grouped-undo')
    document = undone['data']['canonical_document']
    assert document['dependency_manifest']['documents'] == ([] if retention == 'introduced' else [case.pin])
    if retention in ('introduced', 'preexisting'):
        assert json.dumps(document, sort_keys=True) == json.dumps(baseline, sort_keys=True)
    redone = word.undo(case, undone, 'pin-grouped-redo')
    assert redone['data']['canonical_document']['dependency_manifest']['documents'] == [case.pin]
    if retention != 'surviving':
        assert redone['data']['canonical_document'] == anchor['data']['canonical_document']
    case.restart()
    assert case.submit(request, 'pin-grouped-undo') == undone
