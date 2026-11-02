import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import ci_journey_provider as typed
from scripts import ci_journey_r24_hook as hook


def request():
    return typed.ProviderRequest.parse(typed._canonical({'schema': typed.REQUEST_SCHEMA,
        'candidate': hook.CANDIDATE, 'provider': {'ref': hook.REF, 'sources': hook.SOURCES},
        'stop': hook.STOP, 'config_sha256': 'a' * 64, 'run_id': 'b' * 32}))


def config():
    return {'engine': {'pid': 123, 'boot_id': 'boot', 'start_ticks': '10', 'port': 42000}, 'node_sha256': 'e' * 64}


def export(tmp_path, req):
    directory = tmp_path / 'export'
    directory.mkdir()
    rows = []
    for kind in ('busy', 'unknown', 'restoration'):
        session = 'c' * 32
        live = '~/api/astrid/pinokio/providers/r24-live-action.js?r24-session=' + session
        stop = '~/api/astrid/pinokio/stop.js?r24-session=' + session
        detail = {'busy': {'engine_binding': {'request_id': live + '-probe'},
                           'busy': {'stop_refused': True, 'fence': {'request_id': stop + '-busy-stop'}}},
                  'unknown': {'unknown': {'fence': {'request_id': live + '-unknown'}}},
                  'restoration': {'restoration': {'request_id': live + '-restoration'}}}[kind]
        data = typed._canonical({'schema': 'astrid.r24.live-observation.v1', 'kind': kind,
            'request_sha256': req.sha256, 'run_id': 'b' * 32, 'session': session,
            'engine': {**config()['engine'], 'node_sha256': config()['node_sha256']},
            'provider_ref': hook.REF, **detail})
        (directory / f'observation-{kind}.json').write_bytes(data)
        rows.append({'kind': kind, 'source': 'pinokio/providers/r24-action.cjs',
                     'evidence_sha256': hook.sha(data), 'safe': True})
    result = {'schema': typed.RESULT_SCHEMA, 'request_sha256': req.sha256, 'observations': rows}
    stdout = typed._canonical(result)
    (directory / 'observation-result.json').write_bytes(stdout + b'\n')
    (directory / 'junit.xml').write_bytes(b'<skipped/>')
    files = [{'path': p.name, 'bytes': len(p.read_bytes()), 'sha256': hook.sha(p.read_bytes())}
             for p in sorted(directory.iterdir())]
    (directory / 'manifest.json').write_bytes(typed._canonical({'schema': 'astrid.r24.producer-export.v1',
        'qualification': 'not-evaluated', 'files': files}))
    return directory, stdout


def env():
    return {'GITHUB_ACTIONS': 'true', 'RUNNER_OS': 'Linux', 'GITHUB_REPOSITORY': 'banodoco/astrid-agent',
            'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_JOB': 'job',
            'GITHUB_WORKFLOW_REF': 'banodoco/astrid-agent/.github/workflows/journey.yml@refs/heads/test',
            'GITHUB_WORKFLOW_SHA': 'a' * 40, 'GITHUB_SHA': 'b' * 40, 'RUNNER_NAME': 'runner'}


def test_typed_request_binds_candidate_provider_and_stop():
    req = request()
    value = json.loads(req.canonical)
    assert value['candidate'] == hook.CANDIDATE
    assert value['provider'] == {'ref': hook.REF, 'sources': hook.SOURCES}
    assert value['stop'] == hook.STOP
    value['stop']['ref'] = 'f' * 40
    with pytest.raises(typed.ProviderContractError):
        typed.ProviderRequest.parse(typed._canonical(value))


def test_export_hashes_result_and_session(tmp_path):
    req = request()
    directory, stdout = export(tmp_path, req)
    inventory, session = hook.verify_export(directory, req, stdout, config())
    assert set(inventory) == hook.MEMBERS
    assert session == 'c' * 32
    assert inventory['observation-busy.json']['sha256'] == hook.sha((directory / 'observation-busy.json').read_bytes())


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'changed', 'foreign_result', 'manifest', 'session', 'engine', 'action'])
def test_reject_invalid_export(tmp_path, mutation):
    req = request()
    directory, stdout = export(tmp_path, req)
    if mutation == 'missing':
        (directory / 'junit.xml').unlink()
    elif mutation == 'extra':
        (directory / 'extra').write_text('x')
    elif mutation == 'changed':
        (directory / 'observation-busy.json').write_bytes(b'x')
    elif mutation == 'foreign_result':
        stdout = stdout.replace(req.sha256.encode(), b'f' * 64)
    elif mutation == 'manifest':
        (directory / 'manifest.json').write_bytes(b'{}')
    else:
        data = json.loads((directory / 'observation-busy.json').read_bytes())
        if mutation == 'session':
            data['session'] = 'd' * 32
        elif mutation == 'engine':
            data['engine']['pid'] = 124
        else:
            data['busy']['fence']['request_id'] = 'foreign'
        raw = typed._canonical(data)
        (directory / 'observation-busy.json').write_bytes(raw)
        # Change the corresponding result and manifest too, so the session check is reached.
        result = json.loads(stdout)
        result['observations'][0]['evidence_sha256'] = hook.sha(raw)
        stdout = typed._canonical(result)
        (directory / 'observation-result.json').write_bytes(stdout)
        files = [{'path': p.name, 'bytes': len(p.read_bytes()), 'sha256': hook.sha(p.read_bytes())}
                 for p in sorted(directory.iterdir()) if p.name != 'manifest.json']
        (directory / 'manifest.json').write_bytes(typed._canonical({'schema': 'astrid.r24.producer-export.v1',
            'qualification': 'not-evaluated', 'files': files}))
    with pytest.raises(ValueError):
        hook.verify_export(directory, req, stdout, config())


def test_dispatch_requires_complete_linux_environment():
    assert hook.identity(env())['GITHUB_RUN_ID'] == '123'
    for key in ('GITHUB_RUN_ID', 'GITHUB_SHA', 'GITHUB_JOB', 'GITHUB_ACTIONS'):
        bad = env()
        bad.pop(key)
        with pytest.raises(ValueError):
            hook.identity(bad)


def test_real_pinned_producer_failure_capture(tmp_path):
    root = Path(os.environ['R24_PRODUCER_ROOT'])
    config = {'checkouts': {'app': '/missing/app', 'agent': '/missing/agent', 'runtime': '/missing/runtime',
                            'pinokio': '/missing/pinokio'}, 'pterm': '/missing/pterm', 'pinokiod': '/missing/daemon',
              'node': '/missing/node', 'node_sha256': 'a' * 64, 'pterm_entry_sha256': 'b' * 64,
              'unws_sha256': 'c' * 64, 'engine': {'pid': 1, 'boot_id': 'x', 'start_ticks': '1',
              'pinokio_home': '/missing/home', 'port': 42000}, 'scratch_root': '/missing/scratch'}
    output = tmp_path / 'bundle'
    result = hook.invoke(typed._canonical(config), root, output, env())
    assert result['status'] == 'AWAITING_EVIDENCE'
    assert result['exit_code'] == 1
    assert result['inner_action_exit_code'] is None
    assert result['stdout_sha256'] == hook.sha((output / 'stdout.bin').read_bytes())
    assert result['stderr_sha256'] == hook.sha((output / 'stderr.bin').read_bytes())
    assert b'producer-error' in (output / 'stderr.bin').read_bytes()
    assert not (output / 'export').exists()
    with pytest.raises(ValueError):
        hook.invoke(typed._canonical(config), root, output, env())
