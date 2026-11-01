"""A01 selection, source custody, and dropped-in workflow boundaries."""
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli, targets
from astrid.sdk.generation import GenerationFacade, GenerationInputError


def test_target_discovery_and_transient_override_do_not_write(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('VIBECOMFY_COMFYUI_URL', raising=False)
    monkeypatch.setattr(cli, '_open_client', lambda: pytest.fail('discovery opened Runtime'))
    assert cli.main(['targets', '--target', 'http://localhost:8191', '--json']) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed == GenerationFacade().targets(target='http://localhost:8191')
    assert observed['target']['endpoint'] == 'http://127.0.0.1:8191'
    assert not targets.config_path().parent.exists()
    with pytest.raises(GenerationInputError, match='explicit target'):
        GenerationFacade().targets(save=True)
    assert not targets.config_path().parent.exists()
    saved = GenerationFacade().targets(target='http://localhost:8192', save=True)
    config = targets.config_path()
    before = config.read_bytes(), config.stat().st_mtime_ns
    targets.config_lock_path().unlink()
    assert GenerationFacade().targets() == saved
    override = GenerationFacade().targets(target='http://localhost:8193')
    assert override['target']['endpoint'].endswith(':8193')
    assert (config.read_bytes(), config.stat().st_mtime_ns) == before
    assert not targets.config_lock_path().exists()


def test_conflicting_target_flags_never_save(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    assert cli.main(['targets', '--target', 'http://localhost:8191', '--target',
                     'http://localhost:8192', '--save', '--json']) == 2
    error = json.loads(capsys.readouterr().out)
    assert error['issues'][0]['code'] == 'input_rejected'
    assert 'specified more than once' in error['issues'][0]['message']
    assert not targets.config_path().parent.exists()


def test_workflow_cli_calls_public_sdk_once_without_private_probe(monkeypatch, capsys):
    calls = []
    facade = SimpleNamespace(workflow=lambda **kwargs: calls.append(kwargs) or {'ok': True})
    monkeypatch.setattr(cli, '_generate_facade', lambda: facade)
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext('runtime'))
    assert cli.main(['workflow', 'submitted.json', '--target', 'http://localhost:8192',
        '--input', 'texture=0.7', '--input', 'node:12.strength=0.8', '--no-wait', '--json']) == 0
    assert calls == [{'workflow': Path('submitted.json'), 'target': 'http://localhost:8192',
                     'mode': None, 'inputs': {'texture': 0.7, 'node:12.strength': 0.8},
                     'dry_run': False, 'client': 'runtime', 'project': None, 'out': None,
                     'wait': False}]
    assert json.loads(capsys.readouterr().out) == {'ok': True}


@pytest.mark.parametrize('flags', [
    ['--replace'], ['--as-entry', 'mine', '--dry-run'],
    ['--input', 'texture=0.7', '--input', 'texture=0.8'],
])
def test_invalid_save_or_duplicate_input_rejects_before_runtime(monkeypatch, capsys, flags):
    monkeypatch.setattr(cli, '_open_client', lambda: pytest.fail('invalid request opened Runtime'))
    assert cli.main(['workflow', 'absent.json', *flags, '--json']) == 2
    assert json.loads(capsys.readouterr().out)['issues'][0]['code'] == 'input_rejected'


@pytest.mark.integration
@pytest.mark.parametrize('save_entry', [False, True])
def test_workflow_inputs_cross_real_runtime_and_preserve_exact_source(
    loopback_comfy, real_runtime, monkeypatch, tmp_path, capsys, save_entry,
):
    from concurrent.futures import ThreadPoolExecutor
    from astrid import console
    from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
    from astrid.sdk.client import AstridClient
    from tests.core.generation.test_generate_target_journey import PACK
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import load_bundle

    source = tmp_path / 'submitted.json'
    data = b' \r\n' + json.dumps(load_bundle(workflow_from_ready(
        'image/z_image_turbo')).materialize_ui(), indent=3).encode() + b'\r\n '
    source.write_bytes(data)
    digest = 'sha256:' + hashlib.sha256(data).hexdigest()
    monkeypatch.setenv('VIBECOMFY_ENTRIES_DIR', str(tmp_path / 'entries'))
    daemon = real_runtime()
    client = AstridClient.open(endpoint=daemon.endpoint, credential=daemon.credential_path,
        realm_id=daemon.service.realm['id'], actor_id='owner', client_name='a01-workflow',
        client_version='test', protocol_version='workspace.v1')
    project = client.projects.create(slug='a01', name='A01', idempotency_key='a01-project')
    assert project.ok
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(client))
    invoke_result = client.invoke_result
    admitted = []
    def bounded(*args, **kwargs):
        admitted.append(kwargs)
        kwargs.update(timeout_seconds=45.0, poll_seconds=0.02)
        return invoke_result(*args, **kwargs)
    monkeypatch.setattr(client, 'invoke_result', bounded)
    host = GenericPackHost(pack_roots=[PACK], attempt_root=tmp_path / 'attempt',
        client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token))
    host.discover()
    host.register()
    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            settled = worker.submit(host.run, poll_seconds=0.02, max_tasks=1)
            try:
                code = console.main(['generate', 'workflow', str(source),
                    '--input', 'prompt=A01 input prompt', '--input', 'seed=19',
                    '--target', loopback_comfy.url, '--project', project.data['project_id'],
                    *(['--as-entry', 'a01-saved'] if save_entry else []), '--json'])
            finally:
                host.shutdown()
            results = settled.result(timeout=15)
        captured = capsys.readouterr()
        assert code == 0, captured.out + captured.err
        assert len(results) == 1 and results[0].state == 'succeeded'
        assert client.media.read_bytes(digest) == data
        receipt = json.loads((tmp_path / 'attempt' / 'outputs' / 'managed-generation' /
                              'managed-generation-result.json').read_text())
        assert receipt['inputs']['source']['object_id'] == digest
        assert receipt['inputs']['values']['prompt'] == 'A01 input prompt'
        assert receipt['inputs']['values']['seed'] == 19
        assert len(admitted) == 1
        assert admitted[0]['inputs']['source']['digest'] == digest
        assert any(node['class_type'] == 'CLIPTextEncode' and
                   node['inputs'].get('text') == 'A01 input prompt'
                   for node in loopback_comfy.prompts[0].values())
        assert any(node['class_type'] == 'KSampler' and node['inputs']['seed'] == 19
                   for node in loopback_comfy.prompts[0].values())
        # The CLI checks availability before admission; the separate worker
        # makes one provenance observation during the run.
        assert len([call for call in loopback_comfy.calls if call[1] == '/system_stats']) == 2
        assert source.read_bytes() == data
        assert (tmp_path / 'entries' / 'a01-saved' / 'entry.json').exists() == save_entry
    finally:
        host.shutdown()


from tests.core.generation.test_generate_target_journey import loopback_comfy
