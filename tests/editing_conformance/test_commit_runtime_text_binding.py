"""Native text recipes on a real main-API film backup and managed project CAS.

No sockets, authored fake closure, SQL writes, or replacement semantic provider.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline.commit import bundle_from_document, create_runtime_commit_service
from astrid.sdk.timeline.native_text_binding import capture_text_binding_set
from tests.editing_conformance.test_recipe_text_binding_native import descriptor
from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration
OWNER = 'occurrence:occurrence-1'
VOICE = {'kind': 'voiceover_script', 'slot': None}
PROMPT = {'kind': 'prompt', 'slot': 'hero'}


def body(editing, calls, *, dry_run=False):
    head = editing.service.get_project_timeline(editing.project, editing.timeline)
    return {'api_version': 1, 'project_id': editing.project, 'timeline_id': editing.timeline,
            'base_revision_id': head['head_revision_id'], 'base_publication_id': head['head_publication_id'],
            'commands': calls, 'dry_run': dry_run}


def submit(editing, request, key):
    from runtime_protocol.timeline_commit_port import route_path

    return editing.service.commit_timeline(
        editing.project, editing.timeline, request, idempotency_key=key, publisher='text-editor',
        invocation_path=route_path(editing.project, editing.timeline), authorize=lambda: None)


def call(binding=VOICE, text=None, *, clear=False):
    return {'schema_version': 1, 'op': 'text-binding.set', 'version': '1', 'select': OWNER,
            'args': {'binding': binding, **({'clear': True} if clear else {'text': text})},
            'intent': {'ownership': 'occurrence'}}


def inverse(result):
    return {'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
            'args': {'publications': [result['receipt']['receipt_id']]}}


def put(editing, data, *, media_type=None, project=None):
    pin = descriptor(data)
    editing.service.ingest(project or editing.project, data, media_type=media_type or pin['media_type'],
                           idempotency_key='text-' + pin['digest'][7:])
    return pin


@pytest.fixture
def editing(tmp_path, monkeypatch):
    runtime = use_runtime()
    monkeypatch.syspath_prepend(str(runtime / 'tests'))
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_port import RequestCommitPort
    from timeline_commit_film_fixture import build_film_fixture

    fixture = build_film_fixture(tmp_path)
    service = RuntimeService(fixture.duplicate_root)
    service.register_timeline_commit_provider(create_runtime_commit_service())
    state = SimpleNamespace(service=service, project=fixture.project_id, timeline=fixture.timeline_id,
                            root=fixture.duplicate_root, selected=[], closures=[])
    real_selection, real_closure = RequestCommitPort.text_selection, RequestCommitPort.revision_closure

    def text_selection(port, selection):
        verified = real_selection(port, selection)
        state.selected.append(copy.deepcopy(verified))
        return verified

    def revision_closure(port, revision):
        closure = real_closure(port, revision)
        state.closures.append(copy.deepcopy(closure))
        return closure

    monkeypatch.setattr(RequestCommitPort, 'text_selection', text_selection)
    monkeypatch.setattr(RequestCommitPort, 'revision_closure', revision_closure)
    try:
        yield state
    finally:
        state.service.close()


def live(editing, binding=VOICE):
    rows = editing.service.list_project_shot_text_bindings(editing.project, kind=binding['kind'],
                                                         slot=binding['slot'])['items']
    return next(row for row in rows if row['shot_id'] == 'shot-1')


def restart(editing):
    from runtime_protocol.service import RuntimeService

    editing.service.close()
    editing.service = RuntimeService(editing.root)
    editing.service.register_timeline_commit_provider(create_runtime_commit_service())


@pytest.mark.parametrize('data', [b'first use\r\n', b'', 'héllo\r\n '.encode()])
def test_first_use_capture_projects_exact_text_and_retry(editing, data):
    pin = put(editing, data)
    initial = editing.service.list_timeline_commits(editing.project, editing.timeline, authorize=lambda: None)
    preview = submit(editing, body(editing, [call(text=pin)], dry_run=True), 'preview')
    assert preview['receipt'] is None
    assert editing.service.list_timeline_commits(editing.project, editing.timeline, authorize=lambda: None) == initial
    baseline = editing.closures[0]
    assert pin['object_id'] not in {row['media_id'] for row in baseline['dependency_manifest']['media']}
    captured = capture_text_binding_set(bundle_from_document(baseline), OWNER, VOICE, text=pin,
                                        verified_text=editing.selected)
    assert preview['data']['commands'] == [captured]
    request = body(editing, [captured])
    accepted = submit(editing, request, 'first-text')
    document = accepted['data']['canonical_document']
    row = live(editing)
    assert (row['media_id'], row['byte_size'], row['mime_type'], row['head']) == (
        pin['object_id'], pin['size'], pin['media_type'], 1)
    authored = next(image for image in document['shot_revisions'] if image['shot_id'] == 'shot-1')['payload']['text_bindings']
    authored_row = next(row for row in authored if row['kind'] == 'voiceover_script')
    assert set(authored_row) == {'kind', 'slot', 'media_id', 'content_hash', 'byte_size', 'mime_type'}
    assert editing.service.object(pin['object_id'])[1] == data
    assert pin['object_id'] in {row['media_id'] for row in document['dependency_manifest']['media']}
    restart(editing)
    assert submit(editing, request, 'first-text') == accepted
    assert live(editing) == row
    assert editing.service.store.integrity_report()['ok']


def test_rebind_retains_v1_after_v2_restart_exact_inverse_and_redo(editing):
    first, second = put(editing, b'V1 exact\r\n'), put(editing, b'V2')
    one = submit(editing, body(editing, [call(text=first)]), 'v1')
    two = submit(editing, body(editing, [call(text=second)]), 'v2')
    document = two['data']['canonical_document']
    assert {first['object_id'], second['object_id']} <= {row['media_id'] for row in document['dependency_manifest']['media']}
    # Retained V1 bytes remain available after its live row is rebound.
    restart(editing)
    undone = submit(editing, body(editing, [inverse(two)]), 'undo-v2')
    assert undone['data']['canonical_document'] == one['data']['canonical_document']
    assert live(editing)['media_id'] == first['object_id']
    assert live(editing)['head'] == 3
    assert editing.service.object(first['object_id'])[1] == b'V1 exact\r\n'
    redone = submit(editing, body(editing, [inverse(undone)]), 'redo-v2')
    assert redone['data']['canonical_document'] == two['data']['canonical_document']
    assert live(editing)['head'] == 4


# Qualified against Runtime's clear projection: first-birth undo removes the row;
# EMPTY remains present until an explicit clear or inverse.
def test_first_birth_undo_restores_absence(editing):
    pin = put(editing, b'')
    accepted = submit(editing, body(editing, [call(text=pin)]), 'empty-birth')
    baseline = editing.closures[0]
    undone = submit(editing, body(editing, [inverse(accepted)]), 'undo-birth')
    assert undone['data']['canonical_document'] == baseline
    assert not editing.service.list_project_shot_text_bindings(editing.project, kind='voiceover_script')['items']


def test_clear_removes_row_and_undo_restores_empty_exactly(editing):
    from runtime_protocol.errors import NotFoundError

    pin = put(editing, b'')
    submit(editing, body(editing, [call(text=pin)]), 'empty')
    accepted = submit(editing, body(editing, [call(PROMPT, put(editing, b'prompt'))]), 'prompt')
    prior = live(editing)
    shot = next(row for row in accepted['data']['canonical_document']['shot_revisions'] if row['shot_id'] == 'shot-1')
    assert [row['kind'] for row in shot['payload']['text_bindings']] == ['transcript', 'voiceover_script', 'prompt']
    request = body(editing, [call(clear=True)])
    cleared = submit(editing, request, 'clear')
    assert not editing.service.list_project_shot_text_bindings(editing.project, kind='voiceover_script')['items']
    with pytest.raises(NotFoundError):
        editing.service.get_project_shot_text_binding(editing.project, prior['binding_id'])
    restart(editing)
    assert submit(editing, request, 'clear') == cleared
    historical = editing.service.get_project_shot_revision(editing.project, 'shot-1', shot['revision_id'])
    assert historical['payload'] == shot['payload']
    restored = submit(editing, body(editing, [inverse(cleared)]), 'undo-clear')
    assert restored['data']['canonical_document'] == accepted['data']['canonical_document']
    assert live(editing)['byte_size'] == 0
    assert live(editing)['head'] == 3
    events = [row for row in editing.service.export_structured()['shot_text_binding_events']
              if row['binding_id'] == prior['binding_id']]
    assert [row['payload']['state'] for row in events] == ['present', 'absent', 'present']
    assert editing.service.store.integrity_report()['ok']


@pytest.mark.parametrize('damage', ['foreign', 'missing', 'size', 'digest', 'mime', 'stored-mime', 'utf8', 'limit'])
def test_exact_project_text_selection_refuses_before_writes(editing, damage):
    from runtime_protocol.errors import RuntimeErrorBase

    data = b'invalid text selection'
    project = None
    if damage == 'foreign':
        project = editing.service.create_project({'slug': 'foreign', 'name': 'Foreign'})['id']
    elif damage == 'utf8':
        data = b'\xff\xfe'
    elif damage == 'limit':
        data = b'x' * (1048576 + 1)
    pin = put(editing, data, project=project, media_type='text/plain' if damage == 'stored-mime' else None)
    if damage == 'missing':
        pin = descriptor(b'not stored')
    elif damage == 'size':
        pin['size'] += 1
    elif damage == 'digest':
        pin['digest'] = descriptor(b'other')['digest']
    elif damage == 'mime':
        pin['media_type'] = 'text/plain'
    before = editing.service.list_timeline_commits(editing.project, editing.timeline, authorize=lambda: None)
    live_before = editing.service.list_project_shot_text_bindings(editing.project)
    with pytest.raises(RuntimeErrorBase):
        submit(editing, body(editing, [call(text=pin)]), 'invalid-text')
    assert editing.service.list_timeline_commits(editing.project, editing.timeline, authorize=lambda: None) == before
    assert editing.service.list_project_shot_text_bindings(editing.project) == live_before
    assert editing.closures == []


def test_stale_disjoint_key_rebases_but_changed_guard_and_aba_refuse(editing):
    from runtime_protocol.errors import RuntimeErrorBase

    first, second = put(editing, b'first'), put(editing, b'second')
    submit(editing, body(editing, [call(text=first)]), 'voice-first')
    submit(editing, body(editing, [call(PROMPT, first)]), 'prompt-first')
    voice_request = body(editing, [call(text=second)], dry_run=True)
    voice = submit(editing, voice_request, 'capture-voice')['data']['commands'][0]
    voice_request.update(commands=[voice], dry_run=False)
    prompt = submit(editing, body(editing, [call(PROMPT, second)]), 'disjoint-prompt')
    accepted = submit(editing, voice_request, 'rebase-voice')
    assert accepted['data']['commands'] == [voice]
    assert live(editing, PROMPT)['media_id'] == second['object_id']
    assert live(editing)['media_id'] == second['object_id']
    assert accepted['data']['canonical_document'] != prompt['data']['canonical_document']
    with pytest.raises(RuntimeErrorBase) as error:
        submit(editing, voice_request, 'stale-original-guard')
    assert error.value.details['reason'] == 'dependency_changed'
    # Same-value writes advance accepted authorship even if live head is unchanged.
    submit(editing, body(editing, [call(text=second)]), 'same-value')
    assert live(editing)['head'] == 2
    with pytest.raises(RuntimeErrorBase) as error:
        submit(editing, body(editing, [inverse(accepted)]), 'undo-across-same-value')
    assert error.value.details['reason'] == 'intervening_write'


@pytest.mark.parametrize('action', ['rebind', 'clear'])
def test_bound_main_descriptor_invalidating_set_and_clear_refuse_without_writes(editing, action):
    from runtime_protocol.errors import RuntimeErrorBase

    binding = {'kind': 'transcript', 'slot': None}
    replacement = b'legitimate replacement transcript'
    pin = put(editing, replacement)
    submit(editing, body(editing, [call(text=pin)], dry_run=True), 'bound-main-baseline')
    canonical_before = copy.deepcopy(editing.closures[-1])
    head_before = editing.service.get_project_timeline(editing.project, editing.timeline)
    history_before = editing.service.list_timeline_commits(editing.project, editing.timeline,
                                                           authorize=lambda: None)
    binding_before = copy.deepcopy(live(editing, binding))
    source_bytes = editing.service.object(binding_before['media_id'])[1]
    request = body(editing, [call(binding, pin) if action == 'rebind' else call(binding, clear=True)])
    with pytest.raises(RuntimeErrorBase) as error:
        submit(editing, request, 'bound-main-' + action)

    assert error.value.code == 'validation_error'
    details = error.value.details
    assert details['reason'] == details['code'] == 'invalid_document'
    assert details['path'] == []
    assert error.value.message.endswith(
        'elementRef: historical text binding requires an unchanged declaration and source')
    assert editing.service.get_project_timeline(editing.project, editing.timeline) == head_before
    assert editing.service.list_timeline_commits(editing.project, editing.timeline,
                                                 authorize=lambda: None) == history_before
    assert editing.closures and all(closure == canonical_before for closure in editing.closures)
    assert live(editing, binding) == binding_before
    assert editing.service.object(binding_before['media_id'])[1] == source_bytes
    assert editing.service.object(pin['object_id'])[1] == replacement


def test_retained_v1_after_v2_clear_restart_group_inverse_is_exact(editing):
    first, second = put(editing, b'V1 retained\r\n'), put(editing, b'V2')
    one = submit(editing, body(editing, [call(text=first)]), 'retained-v1')
    two = submit(editing, body(editing, [call(text=second)]), 'retained-v2')
    prior = live(editing)
    cleared = submit(editing, body(editing, [call(clear=True)]), 'retained-clear')
    restart(editing)
    assert not editing.service.list_project_shot_text_bindings(editing.project, kind='voiceover_script')['items']
    # Current Runtime CAS is append-only; doctor checks retained reachability.
    # A destructive media collector is not exposed by this Runtime version.
    reachable = editing.service.store.integrity_report(deep=True)['checks']['reachable_cas']
    assert reachable['ok']
    assert first['object_id'][7:] not in reachable['orphaned']
    assert editing.service.object(first['object_id'])[1] == b'V1 retained\r\n'
    undo = inverse(two)
    undo['args']['publications'].append(cleared['receipt']['receipt_id'])
    restored = submit(editing, body(editing, [undo]), 'undo-v2-and-clear')
    assert restored['data']['canonical_document'] == one['data']['canonical_document']
    assert editing.service.object(first['object_id'])[1] == b'V1 retained\r\n'
    assert live(editing)['media_id'] == first['object_id']
    assert live(editing)['head'] == 4
    shot = next(row for row in one['data']['canonical_document']['shot_revisions'] if row['shot_id'] == 'shot-1')
    assert editing.service.get_project_shot_revision(editing.project, 'shot-1', shot['revision_id'])['payload'] == shot['payload']
    events = [row for row in editing.service.export_structured()['shot_text_binding_events']
              if row['binding_id'] == prior['binding_id']]
    assert [row['payload']['state'] for row in events] == ['present', 'present', 'absent', 'present']
    assert events[0]['payload']['media_id'] == first['object_id']
    assert editing.service.store.integrity_report()['ok']


def test_runtime_declares_and_captures_exact_text_port(editing):
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    from astrid.sdk.timeline.commands import validate_command

    pin = put(editing, b'')
    assert callable(RequestCommitPort.text_selection)
    # Validate the new command with Agent, independently of Runtime's frozen copy.
    validate_command(call(text=pin))


def test_text_input_is_reverified_at_atomic_acceptance(editing, monkeypatch):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    pin = put(editing, b'original')
    before = editing.service.list_timeline_commits(editing.project, editing.timeline, authorize=lambda: None)
    real_accept = RequestCommitPort.accept

    def accept(port, prepared):
        assert editing.selected[-1] == {**pin, 'utf8': True}
        path = editing.service.cas.path_for(pin['digest'][7:])
        path.chmod(0o644)
        path.write_bytes(b'CORRUPT!')
        return real_accept(port, prepared)

    monkeypatch.setattr(RequestCommitPort, 'accept', accept)
    with pytest.raises(RuntimeErrorBase) as error:
        submit(editing, body(editing, [call(text=pin)]), 'text-race')
    assert error.value.message == 'CAS object failed hash verification'
    assert editing.service.list_timeline_commits(editing.project, editing.timeline, authorize=lambda: None) == before
    assert not editing.service.list_project_shot_text_bindings(editing.project, kind='voiceover_script')['items']


def test_disjoint_first_birth_rebase_keeps_captured_intent_and_actual_order(editing):
    first, second = put(editing, b'voice'), put(editing, b'prompt')
    preview_body = body(editing, [call(text=first)], dry_run=True)
    captured = submit(editing, preview_body, 'birth-capture')['data']['commands'][0]
    prompt = submit(editing, body(editing, [call(PROMPT, second)]), 'birth-prompt')
    preview_body.update(commands=[captured], dry_run=False)
    accepted = submit(editing, preview_body, 'birth-rebase')
    assert accepted['data']['commands'] == [captured]
    shot = next(row for row in accepted['data']['canonical_document']['shot_revisions'] if row['shot_id'] == 'shot-1')
    assert [row['kind'] for row in shot['payload']['text_bindings']] == ['transcript', 'prompt', 'voiceover_script']
    entries = editing.service.list_timeline_commits(editing.project, editing.timeline, authorize=lambda: None)['items']
    record = entries[-1]['records']['commands'][0]
    selected = next(row for row in record['selections'] if row['key'].endswith('.append'))
    assert selected['order'] == [OWNER + '/text-binding:transcript', OWNER + '/text-binding:prompt:hero']
    assert prompt['data']['canonical_document'] != accepted['data']['canonical_document']
