import json
from types import ModuleType

import pytest

from scripts import ci_journey as journey, ci_journey_keepgoing as keepgoing, journey_task_probe as probe


def test_probe_observes_actual_resolver_and_constructed_endpoint_without_token(tmp_path, monkeypatch):
    import astrid.sdk
    import sys
    storage = ModuleType('astrid.sdk.storage_root')
    workspace = ModuleType('astrid.sdk.workspace_client')
    root = tmp_path / 'data'
    storage.resolve_runtime_data_root = lambda: root
    class Client:
        def __init__(self, endpoint, token):
            self.endpoint = endpoint
            self.token = token
    workspace.WorkspaceClient = Client
    monkeypatch.setitem(sys.modules, 'astrid.sdk.storage_root', storage)
    monkeypatch.setitem(sys.modules, 'astrid.sdk.workspace_client', workspace)
    monkeypatch.setattr(astrid.sdk, 'storage_root', storage, raising=False)
    monkeypatch.setattr(astrid.sdk, 'workspace_client', workspace, raising=False)
    output = tmp_path / 'context.json'
    request = '{"project":"p"}'
    monkeypatch.setattr(sys, 'argv', ['probe', str(output), str(root), request])
    def invoke(name, **kwargs):
        assert name == 'astrid.omp_invoke' and sys.argv[1] == request
        assert storage.resolve_runtime_data_root() == root
        workspace.WorkspaceClient('http://127.0.0.1:43123', 'secret-token')
        raise SystemExit(7)
    monkeypatch.setattr(probe.runpy, 'run_module', invoke)
    with pytest.raises(SystemExit) as ex:
        probe.main()
    assert ex.value.code == 7
    text = output.read_text()
    assert 'secret-token' not in text
    assert json.loads(text) == {'root_observed': True, 'root_matches_run_data': True,
                                'data_root': str(root), 'endpoint_observed': True,
                                'endpoint': 'http://127.0.0.1:43123'}


def test_response_reduction_is_bounded_and_excludes_arbitrary_fields(tmp_path):
    src = tmp_path / 'sdk-invocation.json'
    inner = {'ok': False, 'error': {'code': 'not_found', 'message': 'project not found',
              'details': {'project': 'tst-m4-3b-release0', 'token': 'secret'}}}
    src.write_text(json.dumps({'content': [{'text': json.dumps(inner)}], 'details': {'astridTaskRefs': []}, 'secret': 'bad'}))
    result = journey.reduced_sdk_response(src)
    assert result == {'status': 'parsed', 'ok': False, 'error_code': 'not_found',
                      'project_not_found_message': True, 'project_matches_request': True,
                      'task_ref_count': 0}
    assert 'secret' not in json.dumps(result)
    src.write_bytes(b'x' * (journey.EVIDENCE_LOG_LIMIT + 1))
    assert journey.reduced_sdk_response(src) == {'status': 'oversized'}


def test_log_and_probe_failures_do_not_change_task_failure(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    log = tmp_path / 'home/.local/state/astrid-runtime/logs/runtime-service.log'
    log.parent.mkdir(parents=True)
    log.write_text('secret unrelated\nproject not found\n')
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    def fail(*args):
        assert args[-1] == data
        with log.open('a') as f:
            f.write('not_found secret after\nRuntime internal error (request 123)\n')
        raise RuntimeError('Installed SDK did not admit exactly one task')
    monkeypatch.setattr(keepgoing.jumps, 'task_after_update', fail)
    with pytest.raises(RuntimeError, match='Installed SDK did not admit exactly one task'):
        keepgoing.fresh_task({}, evidence, 'p', data)
    observation = json.loads((evidence / 'runtime-task-log-observation.json').read_text())
    assert observation['before']['project_not_found'] == 1
    assert observation['after']['not_found'] == 1
    assert observation['after']['runtime_internal_error'] == 1
    assert 'secret' not in json.dumps(observation)
    log.unlink()
    log.symlink_to(tmp_path / 'unrelated')
    assert keepgoing.task_log_observation(tmp_path) == {'available': False}


def test_task_uses_original_request_and_preserves_admission_failure(tmp_path, monkeypatch):
    calls = []
    def fake_run(argv, log, *, env, timeout):
        calls.append((argv, env, timeout))
        assert argv[1] == '-I' and str(argv[2]).endswith('journey_task_probe.py')
        assert json.loads(argv[-1]) == {'capability_id': 'chiptune.sfx', 'kind': 'executor',
                                      'project': 'tst-m4-3b-release0',
                                      'inputs': {'kind': 'blip', 'variant': 1, 'pitch': 0, 'duration_s': 0.1}}
        return json.dumps({'details': {'astridTaskRefs': []}})
    monkeypatch.setattr(journey, 'run', fake_run)
    data = tmp_path / 'data'
    evidence = tmp_path / 'evidence'
    with pytest.raises(RuntimeError, match='^Installed SDK did not admit exactly one task$'):
        journey.task({'python': '/installed/python', 'env': {}}, evidence,
                     'tst-m4-3b-release0', create_project=False, diagnostic_data=data)
    assert len(calls) == 1
    assert json.loads((evidence / 'sdk-request.json').read_text()) == json.loads(calls[0][0][-1])


def test_service_log_read_is_bounded_and_refuses_symlink(tmp_path):
    path = tmp_path / 'home/.local/state/astrid-runtime/logs/runtime-service.log'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'project not found' + b'x' * journey.EVIDENCE_LOG_LIMIT + b'not_found')
    result = keepgoing.task_log_observation(tmp_path)
    assert result['bytes_scanned'] == journey.EVIDENCE_LOG_LIMIT
    assert result['project_not_found'] == 0 and result['not_found'] == 1
    path.unlink()
    path.symlink_to(tmp_path / 'elsewhere')
    assert keepgoing.task_log_observation(tmp_path) == {'available': False}
