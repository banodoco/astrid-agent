"""Occurrence popup operations through real Runtime selection and acceptance.

Run with ASTRID_RUNTIME_CHECKOUT set and pytest -m integration. No sockets,
synthetic accepted records, direct SQL writes or generated schema overrides.
"""
from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace

import pytest

from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration
OWNER = 'occurrence:occ-a'
TARGET = OWNER + '/clip:clip-a'


def _create_fixture(service, project, document, selection, *, authorize):
    """Author the exact payloads from an empty bootstrap through commitTimeline."""
    from astrid.sdk.timeline._doc_patch import Closure, allocations
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create

    head = service.get_project_timeline(project, 'film')
    parent = service.get_project_parent_composition_revision(
        project, 'film', head['head_revision_id'])
    empty = {'schema_version': 1, 'project_id': project, 'timeline_id': 'film',
             'parent_revision': {key: parent[key] for key in
                                 ('revision_id', 'content_digest', 'payload')},
             'shot_revisions': [], 'internal_timeline_revisions': [],
             'dependency_manifest': parent['dependency_manifest']}
    bundle = bundle_from_document(empty)
    verified = service.get_project_verified_media(project, selection, authorize=authorize)
    calls = []

    def append(call):
        nonlocal bundle
        calls.append(call)
        bundle = execute(bundle, [call], verified_media=[verified]).bundle

    def patch(ref, before, value, path=()):
        closure = Closure(bundle)
        created = ([] if path else [row['ref'] for row in allocations(
            closure, closure.resolve(ref), before, value)])
        append({'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
                'created': created,
                'args': {'changes': [{'op': 'set', 'ref': ref, 'path': list(path),
                                      'before': {'state': 'present', 'value': before},
                                      'value': value}]}})

    def submit(commands, key):
        head = service.get_project_timeline(project, 'film')
        request = {'api_version': 1, 'project_id': project, 'timeline_id': 'film',
                   'base_revision_id': head['head_revision_id'],
                   'base_publication_id': head['head_publication_id'], 'commands': commands,
                   'label': key, 'steps': [{'id': str(index), 'label': key, 'intent': 'Fixture setup',
                       'tags': [], 'command_ordinals': [index],
                       'patch': call['args']['changes'] if call['op'] == 'doc.patch' else []}
                       for index, call in enumerate(commands)],
                   'merge': 'atomic', 'dry_run': False, 'source': {'via': 'python'}}
        return service.commit_timeline(project, 'film', request, idempotency_key=key,
            publisher='media-editor', invocation_path=f'/v1/projects/{project}/timelines/film/commit',
            authorize=authorize)

    authored_parent = copy.deepcopy(document['parent_revision']['payload'])
    occurrences = authored_parent.pop('occurrences')
    authored_parent['occurrences'] = []
    patch('parent', parent['payload'], authored_parent)
    # Admission has no local document effect; Runtime captures its authority.
    calls.append({'schema_version': 1, 'op': 'media.admit', 'version': '1', 'select': None,
            'args': {'inputs': [{'pin': {'media_id': selection['object_id'],
                                        'content_digest': selection['object_id']},
                                 'selection': selection}]}})
    append(capture_occurrence_create(
        bundle, 'occ-a', 'chapter-shot', name='Chapter One', track='picture', on=8, duration=10))
    # Runtime must accept the semantic birth before guarded patches can use its
    # registered source mapping for the new occurrence.
    birth = submit(calls, 'popup-birth')
    calls = []
    bundle = bundle_from_document(birth['data']['canonical_document'])
    current = document_from_bundle(bundle)
    patch('internal:film', current['internal_timeline_revisions'][0]['payload'],
          copy.deepcopy(document['internal_timeline_revisions'][0]['payload']))
    closure = Closure(bundle)
    before = closure.payload(closure.resolve('shot:chapter-shot'))
    shot = copy.deepcopy(document['shot_revisions'][0]['payload'])
    # The compiler supplies the new shot's managed link; it is not authored state.
    shot.pop('internal_timeline_revision_id')
    patch('shot:chapter-shot', before, shot)
    current = document_from_bundle(bundle)
    before = current['parent_revision']['payload']['occurrences'][0]
    occurrence = {**copy.deepcopy(occurrences[0]),
                  'shot_revision_id': before['shot_revision_id']}
    patch('parent', before, occurrence, ('occurrences', 'occurrence_id:occ-a'))
    submit(calls, 'popup-baseline')
    result = submit([{'schema_version': 1, 'op': 'occurrence.place-shared', 'version': '1',
                      'select': OWNER, 'args': {'occurrence_id': 'occ-b', 'on': 10}}], 'popup-sibling')
    actual = result['data']['canonical_document']
    expected_parent = copy.deepcopy(document['parent_revision']['payload'])
    actual_shot, = actual['shot_revisions']
    actual_internal, = actual['internal_timeline_revisions']
    for row in expected_parent['occurrences']:
        row['shot_revision_id'] = actual_shot['revision_id']
    expected_shot = copy.deepcopy(document['shot_revisions'][0]['payload'])
    expected_shot['internal_timeline_revision_id'] = actual_internal['revision_id']
    if (actual['parent_revision']['payload'] != expected_parent
            or actual_shot['shot_id'] != 'chapter-shot' or actual_shot['payload'] != expected_shot
            or actual_internal['timeline_id'] != 'film'
            or actual_internal['payload'] != document['internal_timeline_revisions'][0]['payload']):
        pytest.fail('supported fixture commits changed authored payloads or shared ownership')


@pytest.fixture(params=['image', 'video'])
def editing(request, tmp_path, monkeypatch, operation, media_owner, source_bytes):
    runtime = use_runtime()
    monkeypatch.syspath_prepend(str(runtime / 'tests'))
    from runtime_protocol import timeline_commit_port
    from runtime_protocol.auth import CredentialStore
    from runtime_protocol.timeline_commit_port import RequestCommitPort
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
    from runtime_protocol.timeline_commit_validation import validate_commit_value
    from test_generation_publish_v1_settlement import (
        _effect,
        _fixture,
        _output,
        _settle,
    )

    kind = request.param

    def effect(project):
        value = _effect(project, groups=[{'group_key': 'main', 'selectors': [
            {'selector': name, 'ordinal': index, 'variant_key': name, 'output_port': kind}
            for index, name in enumerate(('old', 'new'))]}])
        value['payload']['metadata'] = {'prompt': 'bounded swap source',
                                       'params': {'duration_seconds': 6} if kind == 'video' else {}}
        return value

    # Observe the real public read's fingerprint input without replacing it.
    fingerprint_inputs = []
    real_digest = timeline_commit_port.digest

    def digest(value):
        result = real_digest(value)
        if isinstance(value, dict) and {'generation', 'variant', 'entry'} <= value.keys():
            fingerprint_inputs.append((copy.deepcopy(value), result))
        return result

    monkeypatch.setattr(timeline_commit_port, 'digest', digest)
    service, project, task, attempt, admitted = _fixture(tmp_path, effect_factory=effect)
    project = project['id']
    credentials = CredentialStore(tmp_path / 'credentials')
    token, _ = credentials.provision('media-editor', ['projects:read', 'projects:write'])

    def authorize():
        return credentials.require(token, 'projects:write')

    try:
        outputs = [
            _output(source_bytes(kind, name), output_port=kind, group_key='main', variant_key=name,
                    ordinal=index, filename=name + ('.mp4' if kind == 'video' else '.png'),
                    media_type=kind + ('/mp4' if kind == 'video' else '/png'))
            for index, name in enumerate(('old', 'new'))]
        _settle(service, attempt, outputs, effect=admitted)
        associations = service.managed_outputs(task['task']['id'])
        pins = []
        for association in associations:
            pin = service.get_project_media_selection(project, {
                'association_id': association['association_id'],
                'object_id': association['object_id'], 'version': association['lifecycle']['version'],
            }, authorize=authorize)
            validate_commit_value(pin, 'MediaSelection')
            pins.append(pin)
        by_name = {row['variant_key']: pin for row, pin in zip(associations, pins)}
        old, new = by_name['old'], by_name['new']
        from tests.sdk.test_timeline_commit_provider_document import (
            MEDIA,
            document_fixture,
        )

        def replace(value):
            if value == MEDIA:
                return old['object_id']
            if isinstance(value, dict):
                return {key: replace(child) for key, child in value.items()}
            if isinstance(value, list):
                return [replace(child) for child in value]
            return value

        document = replace(document_fixture())
        internal = document['internal_timeline_revisions'][0]
        internal['payload'].pop('source_window')
        internal['timeline_id'] = 'film'
        internal['payload']['output']['fps'] = 24
        internal['payload']['registry']['assets']['picture'].update(type=kind)
        if kind == 'video':
            internal['payload']['clips'][0].update({'from': 2, 'to': 6, 'hold': 4, 'speed': 2})
        parent = document['parent_revision']['payload']
        parent['config']['output']['fps'] = 24
        if media_owner == 'parent':
            parent['registry']['assets']['picture']['type'] = kind
            for rows in (parent['clips'], parent['config']['clips']):
                rows[0].update(copy.deepcopy(internal['payload']['clips'][0]))
        for row in parent['occurrences']:
            row.update(source_offset=0, speed=1)
        parent['occurrences'][0]['placement']['start_ms'] = 8000
        # The HTTP caller compares the authored clip identity with the exact
        # public selection it flushes.  Carry the genuine old selection's
        # alias and immutable generation/variant identity into the fixture's
        # authored registry and every raw clip mirror.
        for payload in (internal['payload'], parent):
            assets = payload['registry']['assets']
            binding = assets.pop('picture', assets.get(old['asset']))
            binding.update(generationId=old['generation_id'], variantId=old['variant_id'])
            assets[old['asset']] = binding
        for rows in (internal['payload']['clips'], parent['clips'], parent['config']['clips']):
            for row in rows:
                if row.get('asset') == 'picture':
                    row['asset'] = old['asset']
        document['shot_revisions'][0]['payload']['text_bindings'] = []
        service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
        service.create_timeline(project, 'film', idempotency_key='popup-timeline')
        _create_fixture(service, project, document, old, authorize=authorize)

        selected, closures, candidates = [], [], []
        real_selected = RequestCommitPort.selected_media
        real_closure = RequestCommitPort.revision_closure
        real_accept = RequestCommitPort.accept

        def selected_media(port, pin):
            value = real_selected(port, pin)
            validate_commit_value(value, 'VerifiedMedia')
            selected.append(copy.deepcopy(value))
            return value

        def revision_closure(port, revision):
            value = real_closure(port, revision)
            closures.append(copy.deepcopy(value))
            return value

        state = SimpleNamespace(service=service, project=project, old=old, new=new,
                                associations=associations, selected=selected, closures=closures,
                                candidates=candidates, before_accept=None, credentials=credentials,
                                authorize=authorize, effect=effect, outputs=outputs,
                                fingerprint_inputs=fingerprint_inputs, kind=kind, operation=operation, root=tmp_path / "publish")

        def accept(port, prepared):
            candidates.append(copy.deepcopy(prepared))
            print('DEBUG PREPARED COMMAND', prepared['records']['commands'][0]['command'])
            if state.before_accept:
                state.before_accept()
            return real_accept(port, prepared)

        real_preview = RequestCommitPort.preview

        def preview(port, prepared):
            print('DEBUG PREVIEW COMMAND', prepared['records']['commands'][0]['command'])
            return real_preview(port, prepared)

        monkeypatch.setattr(RequestCommitPort, 'selected_media', selected_media)
        monkeypatch.setattr(RequestCommitPort, 'revision_closure', revision_closure)
        monkeypatch.setattr(RequestCommitPort, 'accept', accept)
        monkeypatch.setattr(RequestCommitPort, 'preview', preview)
        yield state
    finally:
        if 'state' in locals():
            state.service.close()
        service.close()


@pytest.fixture
def source_bytes():
    return lambda kind, name: name.encode()


@pytest.fixture
def media_owner():
    return OWNER


@pytest.fixture(params=['add', 'swap'])
def operation(request):
    return request.param


def raw_command(editing):
    if editing.operation == 'swap':
        return {'schema_version': 1, 'op': 'occurrence.swap-media', 'version': '1',
                'select': TARGET, 'args': {'old_media': editing.old, 'media': editing.new},
                'intent': {'ownership': 'occurrence'}}
    return {'schema_version': 1, 'op': 'occurrence.add-media', 'version': '1',
            'select': OWNER, 'args': {'media': editing.new, 'row': {
                'id': 'reserved-child', 'clipType': editing.kind, 'track': 'picture',
                'asset': editing.new['asset'], 'at': .125, 'hold': 4 if editing.kind == 'video' else 1.25,
                'params': None, 'opaque': [None, False, {}],
                **({'from': 2, 'to': 6, 'speed': 2} if editing.kind == 'video' else {})}},
            'intent': {'ownership': 'occurrence'}}


def body(editing, commands=None, *, dry_run=False):
    head = editing.service.get_project_timeline(editing.project, 'film')
    return {'api_version': 1, 'project_id': editing.project, 'timeline_id': 'film',
            'base_revision_id': head['head_revision_id'], 'base_publication_id': head['head_publication_id'],
            'commands': commands or [raw_command(editing)],
            'dry_run': dry_run, 'source': {'via': 'python'}}


def commit(editing, request, key):
    return editing.service.commit_timeline(
        editing.project, 'film', request, idempotency_key=key, publisher='media-editor',
        invocation_path=f'/v1/projects/{editing.project}/timelines/film/commit', authorize=editing.authorize,
    )


def rows(editing):
    return tuple(editing.service.store.conn.iterdump())


def foreign_pin(editing):
    from test_generation_publish_v1_settlement import CAPABILITY, _output, _settle

    service = editing.service
    project = service.create_project({'slug': 'foreign', 'name': 'Foreign'})['id']
    effect = editing.effect(project)
    task = service.create_task({
        'capability_id': CAPABILITY, 'capability_digest': 'sha256:' + hashlib.sha256(CAPABILITY.encode()).hexdigest(),
        'project': project, 'settlement_effect': effect, 'idempotency_key': 'foreign-task',
    })
    attempt = service.claim_next({'executor_id': 'publish-worker', 'capability_ids': [CAPABILITY],
                                 'runtime_epoch': service.health()['runtime_epoch']}, idempotency_key='foreign-claim')
    outputs = [_output(b'foreign-' + row['variant_key'].encode(), **{
        key: row[key] for key in ('output_port', 'group_key', 'variant_key', 'ordinal', 'filename', 'media_type')
    }) for row in editing.outputs]
    _settle(service, attempt, outputs, effect=effect, key='foreign-settle')
    association = service.managed_outputs(task['task']['id'])[0]
    return service.get_project_media_selection(project, {
        'association_id': association['association_id'], 'object_id': association['object_id'],
        'version': association['lifecycle']['version'],
    }, authorize=editing.authorize)


def inverse(result):
    return {'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
            'args': {'publications': [result['receipt']['receipt_id']]}}


def test_exact_capture_acceptance_inverse_redo_restart_and_retry(editing, monkeypatch):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider

    from astrid.sdk.timeline import commands
    from astrid.sdk.timeline._command_state import State
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_occurrence_media_commands import (
        capture_occurrence_add_media,
        capture_occurrence_swap_media,
    )

    before = rows(editing)
    preview = commit(editing, body(editing, dry_run=True), 'preview')
    assert preview['receipt'] is None and rows(editing) == before
    baseline = editing.closures[0]
    assert editing.new['object_id'] not in {pin['media_id'] for pin in baseline['dependency_manifest']['media']}
    bundle = bundle_from_document(baseline)
    call = raw_command(editing)
    if editing.operation == 'add':
        captured = capture_occurrence_add_media(bundle, OWNER, call['args']['row'], editing.new,
                                                verified_media=editing.selected)
    else:
        captured = capture_occurrence_swap_media(bundle, TARGET, editing.old, editing.new,
                                                 verified_media=editing.selected)
    assert preview['data']['commands'] == [captured]
    request = body(editing, [captured])
    created = commit(editing, request, 'popup')
    output = created['data']['canonical_document']
    assert output == preview['data']['canonical_document']
    after = bundle_from_document(output)
    assert State(after).config('occurrence:occ-b') == State(bundle).config('occurrence:occ-b')
    record = editing.candidates[-1]['records']['commands'][0]
    assert record['command']['created'][0] == OWNER + '/asset:' + editing.new['asset']
    assert record['created'][0] == {'ref': OWNER + '/asset:' + editing.new['asset'],
                                    'kind': 'asset', 'owner': OWNER}
    undo_request = body(editing, [inverse(created)])
    undone = commit(editing, undo_request, 'undo')
    assert undone['data']['canonical_document'] == baseline
    history = editing.service.list_timeline_commits(editing.project, 'film', authorize=editing.authorize)
    editing.service.close()
    editing.service = RuntimeService(editing.root)
    editing.service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
    with monkeypatch.context() as retry:
        retry.setattr(commands, 'execute', lambda *a, **kw: pytest.fail('exact retry executed semantics'))
        assert commit(editing, request, 'popup') == created
        assert commit(editing, undo_request, 'undo') == undone
    changed = copy.deepcopy(request)
    changed['source'] = {'via': 'cli'}
    with pytest.raises(RuntimeErrorBase) as error:
        commit(editing, changed, 'popup')
    assert error.value.status == 409
    assert editing.service.list_timeline_commits(editing.project, 'film', authorize=editing.authorize) == history
    with pytest.raises(RuntimeErrorBase) as error:
        commit(editing, body(editing, [captured]), 'fresh-reuse')
    assert error.value.details['reason'] == 'identity_collision'
    redone = commit(editing, body(editing, [inverse(undone)]), 'redo')
    assert redone['data']['canonical_document'] == output
    assert editing.service.store.integrity_report()['ok']


@pytest.mark.parametrize('side', ['old_media', 'media'])
@pytest.mark.parametrize('damage', ['stale', 'foreign', 'fingerprint'])
def test_invalid_exact_selection_refuses_without_any_writes(editing, side, damage):
    from runtime_protocol.errors import RuntimeErrorBase

    request = copy.deepcopy(body(editing))
    if side == 'old_media' and editing.operation == 'add':
        side = 'media'
    pin = request['commands'][0]['args'][side]
    if damage == 'stale':
        association = next(row for row in editing.associations if row['object_id'] == pin['object_id'])
        editing.service.update_managed_output_lifecycle(association['association_id'], {
            'operation': 'promote', 'expected_version': association['lifecycle']['version'],
        }, idempotency_key='promote-before-capture')
    elif damage == 'foreign':
        foreign = foreign_pin(editing)
        request['commands'][0]['args'][side] = foreign
        with pytest.raises(RuntimeErrorBase):
            editing.service.get_project_media_selection(editing.project, {
                key: foreign[key] for key in ('asset', 'generation_id', 'variant_id')
            }, authorize=editing.authorize)
    else:
        # Bytes alone are not the metadata/authority fingerprint, even for the
        # correct project, alias, generation, variant and immutable object.
        pin['catalog_digest'] = pin['object_id']
    before = rows(editing)
    with pytest.raises(RuntimeErrorBase) as error:
        commit(editing, request, 'invalid-selection')
    if damage != 'foreign':
        assert error.value.message == 'selected media catalog digest changed'
    else:
        assert error.value.message == 'selected media is not a managed project generation/variant/object'
    assert editing.candidates == []
    assert rows(editing) == before


def test_source_metadata_is_part_of_the_exact_selection_fingerprint(editing):
    from runtime_protocol.errors import ConflictError
    from runtime_protocol.timeline_commit_port import digest

    pin = editing.new
    source, fingerprint = next((source, fingerprint) for source, fingerprint in editing.fingerprint_inputs
                               if source['variant_id'] == pin['variant_id'])
    metadata = editing.service.get_generation(pin['generation_id'])['metadata']
    assert source['generation']['metadata'] == metadata
    assert fingerprint == pin['catalog_digest'] == digest(source)
    # Corrupt only the source metadata in an observed hash input. The resulting
    # bad request pin must never authorize a different snapshot or any write.
    changed = copy.deepcopy(source)
    if 'duration_seconds' in metadata['params']:
        changed['generation']['metadata']['params']['duration_seconds'] = 5.999
    else:
        changed['generation']['metadata']['prompt'] = 'different source'
    wrong_fingerprint = digest(changed)
    assert wrong_fingerprint != fingerprint
    request = copy.deepcopy(body(editing))
    request['commands'][0]['args']['media']['catalog_digest'] = wrong_fingerprint
    before = rows(editing)
    with pytest.raises(ConflictError, match='selected media catalog digest changed'):
        commit(editing, request, 'source-fingerprint')
    assert editing.candidates == []
    assert rows(editing) == before


@pytest.mark.parametrize('side', ['old_media', 'media'])
@pytest.mark.parametrize('race', ['lifecycle', 'revocation'])
def test_capture_to_accept_races_refuse_atomically(editing, side, race):
    from runtime_protocol.errors import RuntimeErrorBase

    request = body(editing)
    if side == 'old_media' and editing.operation == 'add':
        side = 'media'
    pin = request['commands'][0]['args'][side]
    before_accept = None
    def interfere():
        nonlocal before_accept
        if race == 'lifecycle':
            association = next(row for row in editing.associations if row['object_id'] == pin['object_id'])
            editing.service.update_managed_output_lifecycle(association['association_id'], {
                'operation': 'promote', 'expected_version': association['lifecycle']['version'],
            }, idempotency_key='promote-at-accept')
        else:
            editing.credentials.revoke('media-editor')
        before_accept = rows(editing)

    editing.before_accept = interfere
    with pytest.raises(RuntimeErrorBase) as error:
        commit(editing, request, 'race')
    assert error.value.message == ('selected media catalog digest changed' if race == 'lifecycle'
                                   else 'invalid bearer credential')
    assert [row['selection'] for row in editing.selected] == (
        [editing.new] if editing.operation == 'add' else [editing.old, editing.new])
    assert len(editing.candidates) == 1
    assert before_accept == rows(editing)
