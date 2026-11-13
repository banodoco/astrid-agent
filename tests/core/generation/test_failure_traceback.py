"""Pod job failures retain safe, useful diagnostics alongside readiness JSON."""
import json

from astrid.core.generation import cli, local_job
from astrid.core.generation.preflight import generation_error
from astrid.packs.runpod.machine import generation_argv


def _run_failure(monkeypatch, capsys, failure):
    def failing_stage(*args, **kwargs):
        raise failure

    monkeypatch.setattr(local_job, 'run_job', failing_stage)
    argv = generation_argv('example', '/job', mode='sfx')[2:]
    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.err


def test_unexpected_stage_failure_keeps_issue_and_traceback(monkeypatch, capsys):
    payload, stderr = _run_failure(monkeypatch, capsys, ValueError('boom'))
    assert payload['issues'] == [{'code': 'generation_failed', 'message': 'boom',
                                  'severity': 'error', 'details': {}}]
    assert 'Traceback (most recent call last)' in stderr
    assert 'failing_stage' in stderr
    assert 'ValueError: boom' in stderr


def test_typed_refusal_has_no_traceback(monkeypatch, capsys):
    _, unexpected = _run_failure(monkeypatch, capsys, ValueError('boom'))
    assert 'ValueError: boom' in unexpected
    refusal = generation_error(ValueError('target unavailable'), code='target_unreachable')
    payload, stderr = _run_failure(monkeypatch, capsys, refusal)
    assert payload['issues'][0]['code'] == 'target_unreachable'
    assert stderr == ''


def test_traceback_redacts_credentials_and_keeps_cause(monkeypatch, capsys):
    try:
        raise RuntimeError('token=synthetic-secret')
    except RuntimeError as cause:
        failure = ValueError('api_key=synthetic-key')
        failure.__cause__ = cause
    payload, stderr = _run_failure(monkeypatch, capsys, failure)
    assert 'RuntimeError: token=<redacted>' in stderr
    assert 'ValueError: api_key=<redacted>' in stderr
    assert 'synthetic-secret' not in stderr and 'synthetic-key' not in stderr
    assert payload['issues'][0]['message'] == 'api_key=<redacted>'
