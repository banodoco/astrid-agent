"""Capture proof from the real provider/port; Runtime verification is separate."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from astrid.sdk.timeline import _commit_preview
from astrid.sdk.timeline._command_state import present
from astrid.sdk.timeline._doc_patch import Closure
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from tests.editing_conformance import test_commit_runtime_source_copy
from tests.editing_conformance.test_doc_patch_sequential_rebase import (
    dump,
    peer_request,
    setup,
)
from tests.editing_conformance.test_doc_patch_v2 import change, patch

pytestmark = pytest.mark.integration
saved_source = test_commit_runtime_source_copy.saved_source


@pytest.fixture(autouse=True)
def origins(request):
    from tests.helpers.runtime import use_runtime

    use_runtime()
    import runtime_protocol.timeline_commit_auth as auth
    import runtime_protocol.timeline_commit_port as port

    from astrid.sdk.timeline import _commit_patch, commands, commit

    files = [module.__file__ for module in (_commit_patch, commands, commit, _commit_preview, port, auth)]
    print(json.dumps({'phase': 'before', 'case': request.node.nodeid, 'origins': files}))
    yield
    print(json.dumps({'phase': 'after', 'case': request.node.nodeid, 'origins': files}))


@pytest.mark.parametrize('declared', [False, True])
@pytest.mark.parametrize('dry_run', [False, True])
def test_real_provider_delivers_original_and_replay_before_acceptance(
        saved_source, tmp_path, monkeypatch, declared, dry_run):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit import CommitRejected
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    client, project, document, send, request, ref = setup(saved_source, declared=declared)
    original = copy.deepcopy(document('t'))
    peer = patch(change(['clips', 'id:late', 'params', 'text'], present('hi'), 'peer', ref=ref))
    send(project, 't', peer_request(client, project, peer), idempotency_key='peer')
    landing = copy.deepcopy(document('t'))
    history = copy.deepcopy(client.timelines.history(project, 't', limit=100).data)
    request['dry_run'] = dry_run
    packet = {'request': copy.deepcopy(request), 'source': original,
              'head_document': landing, 'history': history}
    deliver = _commit_preview.capture_sequential_execution

    def provider_capture(port, context, source_plan, landing_plan, bindings, prepared):
        packet['prepared'] = copy.deepcopy(prepared)
        deliver(port, context, source_plan, landing_plan, bindings, prepared)

    def receive(port, capture):
        assert capture['context'] == port._provider_context()
        packet['capture'] = copy.deepcopy(capture)
        raise CommitRejected({'http_status': 422, 'code': 'invalid',
                              'message': 'Stopped at private capture; no verifier installed',
                              'details': {'status': 'invalid', 'reason': 'capture_complete'}})

    monkeypatch.setattr(_commit_preview, 'capture_sequential_execution', provider_capture)
    monkeypatch.setattr(RequestCommitPort, 'capture_sequential_execution', receive, raising=False)
    before = dump(tmp_path)
    with pytest.raises(RuntimeErrorBase) as error:
        send(project, 't', request, idempotency_key='capture')
    assert error.value.details['reason'] == 'capture_complete'
    assert document('t') == landing
    assert dump(tmp_path) == before
    assert client.timelines.history(project, 't', limit=100).data == history
    capture = packet['capture']
    assert capture['prepared_digest'] == _digest(packet['prepared'])
    assert capture['context']['request'] == request
    assert capture['landing']['records'] == packet['prepared']['records']
    for name, image in (('original', original), ('landing', landing)):
        execution = capture[name]
        first, second = execution['commands']
        assert first['input'] == image
        assert first['output'] == second['input']
        assert second['boundary']['command_ordinal'] == 1
        assert second['boundary']['step_id'] == 'second'
        assert second['boundary']['evaluated_head'] == execution['evaluated_head']
        assert execution['records']['steps'][1]['dependencies']
        for item in execution['commands']:
            bundle_from_document(item['input'])
            bundle_from_document(item['output'])
    assert capture['original']['evaluated_head'] != capture['landing']['evaluated_head']
    submitted = request['commands'][1]['args']['changes'][0]['ref']
    original_input = Closure(bundle_from_document(capture['original']['commands'][1]['input']))
    landing_input = Closure(bundle_from_document(capture['landing']['commands'][1]['input']))
    assert submitted in original_input.images and submitted not in landing_input.images
    assert capture['replay_bindings']
    if declared:
        assert any(item['kind'] == 'read' and item['command_ordinal'] == 1
                   and item['ref'] == submitted for item in capture['replay_bindings'])
    if directory := os.environ.get('R1_EVIDENCE_DIR'):
        content = json.dumps(packet, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                             allow_nan=False)
        target = Path(directory) / f'capture-{int(declared)}-{int(dry_run)}.json'
        target.write_text(content + '\n', encoding='utf-8')


def test_real_unsupported_port_refuses_required_proof_without_writes(saved_source, tmp_path, monkeypatch):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    monkeypatch.setattr(RequestCommitPort, 'capture_sequential_execution', None, raising=False)
    client, project, document, send, request, ref = setup(saved_source)
    peer = patch(change(['clips', 'id:late', 'params', 'text'], present('hi'), 'peer', ref=ref))
    send(project, 't', peer_request(client, project, peer), idempotency_key='peer')
    landing = copy.deepcopy(document('t'))
    before = dump(tmp_path)
    with pytest.raises(RuntimeErrorBase) as error:
        send(project, 't', request, idempotency_key='unsupported-proof')
    assert error.value.details['reason'] == 'sequential_proof_unavailable'
    assert document('t') == landing
    assert dump(tmp_path) == before
