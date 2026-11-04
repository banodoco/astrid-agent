"""Connected-project plumbing; fake SDK results are not H3 semantic acceptance."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrid.core import action_cli
from astrid.sdk.client import AstridClient
from astrid.sdk.results import InvocationResult


def _argv(tmp_path, *extra):
    request, assets = tmp_path / "request.json", tmp_path / "assets.json"
    request.write_text('{}')
    # This must never be opened as a nested path by CLI plumbing.
    assets.write_text('{"source": "/untrusted/nested/path"}')
    return ['run', 'h3_av.transform', '--request', str(request), '--asset-map', str(assets),
            '--out', str(tmp_path / 'out'), '--placement', 'local', '--project', 'user-project', *extra]


def _result(*, ok=True, state='completed', attempt='attempt-1', task='task-1', domain='candidate_verified'):
    return InvocationResult(capability_id='h3_av.transform', capability_type='orchestrator',
        native_kind='external', ok=ok, error=None if ok else {'code': 'runtime_failure'},
        run_id='run-1', kernel_run_id='run-1', kernel_task_id=task, kernel_attempt_id=attempt,
        raw_result={'state': state, 'result': {'overall_status': domain}},
        outputs={'managed_outputs': [{'association_id': 'final-association'}]})


def _install(monkeypatch, result):
    calls = []
    monkeypatch.setattr(action_cli, 'ephemeral_runtime',
                        lambda *_: pytest.fail('connected project started an ephemeral Runtime'))
    monkeypatch.setattr(action_cli.tempfile, 'mkdtemp',
                        lambda **_: pytest.fail('connected project allocated a throw-away workspace'))
    def invoke(capability, **kwargs):
        calls.append((capability, kwargs))
        return result
    client = SimpleNamespace(invoke_result=invoke,
        projects=SimpleNamespace(create=lambda **_: pytest.fail('created another project')))
    monkeypatch.setattr(AstridClient, 'open_from_launcher', lambda **_: client)
    return calls


@pytest.mark.parametrize('domain', ['candidate_verified', 'complete'])
def test_project_invokes_once_and_preserves_runtime_result(tmp_path, monkeypatch, capsys, domain):
    calls = _install(monkeypatch, _result(domain=domain))
    argv = _argv(tmp_path, '--idempotency-key', 'stable-submission')
    stale_receipt = tmp_path / 'out' / action_cli.RECEIPT_NAME
    stale_receipt.parent.mkdir()
    stale_receipt.write_text('{"overall_status": "complete"}')
    assert action_cli.main(argv) == 0
    summary = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    capability, kwargs = calls[0]
    assert capability == 'h3_av.transform' and kwargs['kind'] == 'action'
    assert kwargs['project'] == kwargs['inputs']['project'] == 'user-project'
    assert kwargs['idempotency_key'] == 'stable-submission' and kwargs['wait'] is True
    assert summary['kernel_task_id'] == 'task-1' and summary['kernel_attempt_id'] == 'attempt-1'
    assert summary['state'] == 'completed' and summary['result']['overall_status'] == domain
    assert summary['outputs']['managed_outputs'] == [{'association_id': 'final-association'}]
    assert summary['receipt'] is None  # Local receipt is not publication or settlement authority.
    assert json.loads((tmp_path / 'assets.json').read_text())['source'] == '/untrusted/nested/path'


@pytest.mark.parametrize('state', ['failed', 'cancelled', 'cancel_requested', 'lost', 'unknown'])
def test_project_preserves_failed_or_unresolved_result(tmp_path, monkeypatch, capsys, state):
    calls = _install(monkeypatch, _result(ok=False, state=state, domain='task_failed'))
    assert action_cli.main(_argv(tmp_path)) == 1
    summary = json.loads(capsys.readouterr().out)
    assert len(calls) == 1 and summary['state'] == state
    assert summary['error'] == {'code': 'runtime_failure'}
    assert 'idempotency_key' not in calls[0][1]


@pytest.mark.parametrize('change,code', [
    ({'task': None}, 'tracked_parent_unavailable'),
    ({'attempt': None}, 'tracked_parent_unavailable'),
    ({'state': 'running'}, 'task_settlement_unproven'),
    ({'state': 'unknown'}, 'task_settlement_unproven'),
])
def test_project_refuses_untracked_or_unsettled_success(tmp_path, monkeypatch, capsys, change, code):
    calls = _install(monkeypatch, _result(**change))
    assert action_cli.main(_argv(tmp_path)) == 1
    summary = json.loads(capsys.readouterr().out)
    assert len(calls) == 1 and summary['ok'] is False
    assert summary['error']['code'] == code


def test_unknown_sdk_outcome_reports_failure_without_retry(tmp_path, monkeypatch, capsys):
    from astrid.sdk.exceptions import CapabilityInvocationError

    calls = _install(monkeypatch, _result())
    def unknown(capability, **kwargs):
        calls.append((capability, kwargs))
        raise CapabilityInvocationError('Runtime settlement status unavailable')
    client = SimpleNamespace(invoke_result=unknown)
    monkeypatch.setattr(AstridClient, 'open_from_launcher', lambda **_: client)
    assert action_cli.main(_argv(tmp_path)) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary['ok'] is False and 'unavailable' in summary['error']
    assert len(calls) == 1
