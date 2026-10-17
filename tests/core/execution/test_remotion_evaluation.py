"""Python cache/protocol checks only; injected replies are not renderer proof."""
import json
import hashlib
import sys
from pathlib import Path

import pytest

from astrid.core.execution import remotion_evaluation as evaluation
from astrid.core.execution.remotion_frame_owner import FrameOwnerError


@pytest.fixture(autouse=True)
def trusted_local_context(monkeypatch):
    # The suite runs in an internal-invocation context. These injected local
    # transports explicitly model the trusted host, not a confined executor.
    monkeypatch.delenv('ASTRID_INTERNAL_INVOCATION', raising=False)


def query_client(tmp_path, monkeypatch, *, response=None):
    calls = []
    current = ['closure-component-package-font-a']
    class Transport:
        def request(self, identity, request, parser):
            props = json.loads(Path(request['propsPath']).read_bytes())
            assert request['type'] == 'evaluate'
            assert request['compositionId'] == 'AstridTrackEvaluation'
            assert props['frames'] == request['frames']
            calls.append((identity, props))
            if response is not None:
                return response
            evaluation = [
                ({'frame': frame, 'layers': [], 'coverage': 'GAP', 'content': None, 'findings': []}
                 if 'scene' in props else {'frame': frame, 'layers': []})
                for frame in props['frames']
            ]
            return {'ok': True, 'evaluation': evaluation}
    transport = Transport()
    monkeypatch.setattr(evaluation, 'host_remotion_client', lambda: transport)
    client = evaluation.RemotionEvaluationClient(project_dir=tmp_path, attempt_root=tmp_path,
        node_executable=sys.executable, renderer_closure=lambda: current[0])
    return client, calls, current


def ask(client, **overrides):
    return client.query_track(**{'revision_digest': 'resolved-relevant-revision',
        'spans': [{'start': 0, 'duration': 4}], 'transitions': [], 'frames': [0, 3, 4], **overrides})


def test_repeat_returns_copied_answer_and_new_identity_or_inputs_recompute(tmp_path, monkeypatch):
    client, calls, current = query_client(tmp_path, monkeypatch)
    first = ask(client)
    first[0]['layers'].append({'caller-mutation': True})
    assert ask(client)[0]['layers'] == []
    assert len(calls) == 1
    for label in ('component-change', 'package-change', 'font-change'):
        current[0] = label
        ask(client)
    assert len(calls) == 4
    ask(client, revision_digest='changed-relevant-revision')
    ask(client, spans=[{'start': 0, 'duration': 5}])
    assert len(calls) == 6
    assert not list(tmp_path.glob('evaluation-*'))


def test_owner_unavailable_fails_and_never_creates_legacy_owner(tmp_path, monkeypatch):
    client, _, _ = query_client(tmp_path, monkeypatch)
    monkeypatch.setattr(evaluation, 'host_remotion_client', lambda: None)
    with pytest.raises(FrameOwnerError, match='frame_owner_unavailable'):
        ask(client)
    assert not list(tmp_path.glob('evaluation-*'))


@pytest.mark.parametrize('response', [
    {'ok': False, 'error': 'owned-renderer-failure'},
    {'ok': True, 'evaluation': []},
    {'ok': True, 'evaluation': [{'frame': True, 'layers': []}]},
])
def test_failure_or_wrong_frames_are_never_cached(tmp_path, monkeypatch, response):
    client, calls, _ = query_client(tmp_path, monkeypatch, response=response)
    for _ in range(2):
        with pytest.raises(FrameOwnerError):
            ask(client)
    assert len(calls) == 2 and not client._cache
    assert not list(tmp_path.glob('evaluation-*'))


def test_closure_change_during_delivery_refuses_result(tmp_path, monkeypatch):
    client, _, _ = query_client(tmp_path, monkeypatch)
    identities = iter(['before', 'after'])
    client.closure = lambda: next(identities)
    with pytest.raises(FrameOwnerError, match='renderer_closure_changed_during_query'):
        ask(client)
    assert not client._cache


def test_query_count_budget_fails_before_transport(tmp_path, monkeypatch):
    client, calls, _ = query_client(tmp_path, monkeypatch)
    for frames in ([], list(range(121)), [True], [2**53]):
        with pytest.raises(FrameOwnerError, match='evaluation_inputs_invalid'):
            ask(client, frames=frames)
    assert calls == []


@pytest.mark.parametrize('field', ['project_dir', 'node_executable', 'attempt_root'])
def test_trusted_local_construction_keeps_strict_resolution(tmp_path, field):
    arguments = dict(project_dir=tmp_path, node_executable=sys.executable,
                     attempt_root=tmp_path, renderer_closure=lambda: 'local')
    arguments[field] = tmp_path / 'absent'
    with pytest.raises(FileNotFoundError):
        evaluation.RemotionEvaluationClient(**arguments)


@pytest.mark.parametrize('change', ['remove', 'replace'])
def test_local_cache_cannot_outlive_installed_client(tmp_path, monkeypatch, change):
    client, calls, _ = query_client(tmp_path, monkeypatch)
    ask(client)
    monkeypatch.setattr(evaluation, 'host_remotion_client', lambda: None if change == 'remove' else object())
    with pytest.raises(FrameOwnerError):
        ask(client)
    assert len(calls) == 1


def test_trusted_local_request_fields_are_unchanged(tmp_path, monkeypatch):
    requests = []
    class Transport:
        def request(self, identity, request, parser):
            requests.append((identity, request))
            return {'ok': True, 'evaluation': [{'frame': 0, 'layers': []}]}
    transport = Transport()
    monkeypatch.setattr(evaluation, 'host_remotion_client', lambda: transport)
    alias = tmp_path / 'project-alias'
    alias.symlink_to(tmp_path, target_is_directory=True)
    client = evaluation.RemotionEvaluationClient(project_dir=alias, node_executable=sys.executable,
        attempt_root=tmp_path, renderer_closure=lambda: 'local-closure')
    ask(client, frames=[0])
    identity, request = requests[0]
    assert identity == 'local-closure'
    assert {key: value for key, value in request.items() if key not in {'propsPath', 'outputDir'}} == {
        'type': 'evaluate', 'projectDir': str(tmp_path.resolve()), 'compositionId': 'AstridTrackEvaluation',
        'nodeExecutable': str(Path(sys.executable).resolve()),
        'remotionCli': str(tmp_path.resolve() / 'node_modules/@remotion/cli/remotion-cli.js'),
        'frames': [0], 'resolution': None, 'port': 31500, 'environment': {}}


def test_scene_projection_overwrites_authored_identity_and_cache_binds_selected_font_bytes(tmp_path, monkeypatch):
    fonts = tmp_path / 'public' / 'fonts'
    fonts.mkdir(parents=True)
    gelasio = fonts / 'Gelasio-Regular.woff2'
    departure = fonts / 'DepartureMono-Regular.woff2'
    gelasio.write_bytes(b'host-selected Gelasio test bytes')
    departure.write_bytes(b'host-selected Departure Mono test bytes')

    client, calls, current = query_client(tmp_path, monkeypatch)
    scene = {
        'rendererIdentity': 'authored-renderer-identity',
        'fontDigests': {'Gelasio-Regular.woff2': 'authored-font-digest'},
    }
    client.query_scene(revision_digest='scene-r1', scene=scene, frames=[0])

    first_scene = calls[0][1]['scene']
    assert first_scene['rendererIdentity'] == current[0]
    assert first_scene['fontDigests'] == {
        'Gelasio-Regular.woff2': hashlib.sha256(gelasio.read_bytes()).hexdigest(),
        'DepartureMono-Regular.woff2': hashlib.sha256(departure.read_bytes()).hexdigest(),
    }
    assert scene['rendererIdentity'] == 'authored-renderer-identity'
    assert scene['fontDigests'] == {'Gelasio-Regular.woff2': 'authored-font-digest'}

    client.query_scene(revision_digest='scene-r1', scene=scene, frames=[0])
    assert len(calls) == 1
    departure.write_bytes(b'changed host-selected font bytes')
    client.query_scene(revision_digest='scene-r1', scene=scene, frames=[0])
    assert len(calls) == 2
    assert calls[1][1]['scene']['fontDigests']['DepartureMono-Regular.woff2'] == hashlib.sha256(
        departure.read_bytes()).hexdigest()


def test_missing_bootstrap_in_internal_executor_refuses_before_path_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv('ASTRID_INTERNAL_INVOCATION', '1')
    with pytest.raises(FrameOwnerError, match='bootstrap is required'):
        evaluation.RemotionEvaluationClient(project_dir='/forbidden/project', node_executable='/forbidden/node',
            attempt_root=tmp_path, renderer_closure=lambda: 'unused')


def test_scene_response_validation_and_cache_bounds_are_retained(tmp_path, monkeypatch):
    reply = {'ok': True, 'evaluation': [{'frame': 0, 'layers': [],
             'coverage': 'COVERED', 'content': 'CONTENT', 'findings': []}]}
    client, calls, _ = query_client(tmp_path, monkeypatch, response=reply)
    for number in range(130):
        client.query_scene(revision_digest=str(number), scene={}, frames=[0])
    assert len(calls) == 130 and len(client._cache) == 128
    assert client._cache_bytes == sum(map(len, client._cache.values())) <= 8 * 1024 * 1024
    reply['evaluation'][0]['layers'] = [{'clipId': 'c', 'visible': True, 'opacity': 1,
        'bounds': {'status': 'known', 'rect': {'x': 0, 'y': 0, 'width': -1, 'height': 1}}, 'findings': []}]
    with pytest.raises(FrameOwnerError, match='evaluation_response_invalid'):
        client.query_scene(revision_digest='invalid', scene={}, frames=[0])


def test_evaluation_cache_byte_budget_and_response_size_bound(tmp_path, monkeypatch):
    reply = {'ok': True, 'evaluation': [{'frame': 0, 'layers': [{'payload': 'x' * (4 * 1024 * 1024)}]}]}
    client, calls, _ = query_client(tmp_path, monkeypatch, response=reply)
    ask(client, revision_digest='one', frames=[0])
    ask(client, revision_digest='two', frames=[0])
    assert len(calls) == 2 and len(client._cache) == 1
    assert client._cache_bytes == sum(map(len, client._cache.values())) <= 8 * 1024 * 1024
    reply['evaluation'][0]['layers'][0]['payload'] *= 2
    with pytest.raises(FrameOwnerError, match='evaluation_response_too_large'):
        ask(client, revision_digest='oversize', frames=[0])
    assert len(client._cache) == 1
