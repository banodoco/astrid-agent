from __future__ import annotations

import json
import subprocess

import pytest

from astrid.installer.backend import _last_logged_error


def test_last_logged_runtime_error_is_found_in_pretty_json(tmp_path):
    log = tmp_path / "runtime-service.log"
    first = {"error": "older failure", "ok": False}
    last = {"error": "[Errno 13] Permission denied: '/'", "log": "/x/runtime.log", "ok": False}
    log.write_text("starting\n" + json.dumps(first, indent=2) + "\nnoise {not json}\n" + json.dumps(last, indent=2) + "\n")
    assert _last_logged_error(log) == "[Errno 13] Permission denied: '/'"


def test_no_logged_error_means_no_cause(tmp_path):
    log = tmp_path / "runtime-service.log"
    log.write_text(json.dumps({"ok": True, "status": "started"}) + "\n")
    assert _last_logged_error(log) is None
    assert _last_logged_error(tmp_path / "missing.log") is None


def test_source_ownership_evidence_error_is_preserved(tmp_path):
    from astrid.installer.backend import Backend

    backend = Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv")
    discovery = tmp_path / "data" / "runtime" / "discovery.json"
    discovery.parent.mkdir(parents=True)
    discovery.write_text("{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Runtime discovery owner identity cannot be read"):
        backend.source_checkout_ownership(tmp_path / "data")


@pytest.mark.parametrize('failure', [
    subprocess.TimeoutExpired(['runtime'], 1), OSError('owner unavailable'),
])
def test_runtime_transport_failure_never_proves_stop(tmp_path, monkeypatch, failure):
    from astrid.installer.backend import Backend
    from astrid.installer.core import RuntimeOperationError
    value = Backend(tmp_path, tmp_path / 'log', tmp_path / 'uv')
    def fail(*_args, **_kwargs):
        raise failure
    monkeypatch.setattr(value, 'run', fail)
    with pytest.raises(RuntimeOperationError) as raised:
        value.runtime(None, tmp_path / 'data', 'status')
    assert not raised.value.stop_completed
    assert raised.value.details is None


@pytest.mark.parametrize('output', ['', 'not-json', '[]', '{"ok":true} trailing'])
def test_runtime_invalid_result_never_proves_idle(tmp_path, monkeypatch, output):
    from astrid.installer.backend import Backend
    from astrid.installer.core import RuntimeOperationError
    value = Backend(tmp_path, tmp_path / 'log', tmp_path / 'uv')
    monkeypatch.setattr(value, 'run', lambda *_args, **_kwargs: output)
    with pytest.raises(RuntimeOperationError, match='invalid result') as raised:
        value.runtime(None, tmp_path / 'data', 'down')
    assert not raised.value.stop_completed


@pytest.mark.parametrize('nonzero_exit', [False, True])
def test_upgrade_cli_lifecycle_refusal_ends_installer_transaction(tmp_path, monkeypatch, capsys, nonzero_exit):
    """The real serializer/result mapper must forbid rollback after upgrade refuses."""
    from astrid.installer.backend import Backend
    from astrid.installer.core import Installer, RuntimeOperationError, current, read_json
    from astrid_runtime import cli, operator_upgrade
    from tests.test_installer_lifecycle import _installed_lifecycle_backend

    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    headline = 'Astrid is busy (1 tasks running). Try again when they finish.'
    details = {'safe': False, 'task_count': 1, 'headline': headline, 'message': headline,
               'blockers': [{'task_id': 'busy', 'kind': 'render', 'state': 'cancel_requested',
                             'created_at': None, 'age_seconds': None, 'blocker_kind': 'attempt',
                             'attempt_id': 'attempt-1', 'lease_id': 'lease-1'}]}
    monkeypatch.setattr(operator_upgrade, 'inspect_interruption_state', lambda _: details)
    monkeypatch.setattr(cli, '_config', lambda *_: object())
    monkeypatch.setattr(operator_upgrade, 'upgrade_workspace',
                        lambda *_args, **_kwargs: operator_upgrade._assert_idle(data))
    adapter = Backend(root, tmp_path / 'log', tmp_path / 'uv')

    def run(argv, **_kwargs):
        assert 'upgrade' in argv
        assert cli.main(['upgrade', '--data-root', str(data), '--json']) == 1
        output = capsys.readouterr().out
        assert json.loads(output)['error']['details'] == details
        if nonzero_exit:
            raise subprocess.CalledProcessError(1, ['runtime'], output=output)
        return output

    monkeypatch.setattr(adapter, 'run', run)
    backend.upgrade = adapter.upgrade
    backend.restore = lambda *_: backend.calls.append(('restore',))
    with pytest.raises(RuntimeOperationError) as raised:
        Installer(root, data, backend, say=lambda _: None).run('update', {})

    assert raised.value.details == details
    assert str(raised.value) == headline
    assert raised.value.stop_refused
    assert not raised.value.stop_completed
    assert [call[0] for call in backend.calls] == ['stop', 'backup', 'stage']
    assert current(root) == old
    assert read_json(root / 'update-transaction.json')['step'] == 'staged'


def test_upgrade_migration_error_does_not_claim_a_lifecycle_refusal():
    from astrid.installer.backend import Backend
    from astrid.installer.core import RuntimeOperationError
    details = {'phase': 'migration', 'reason': 'invalid schema'}
    with pytest.raises(RuntimeOperationError) as raised:
        Backend._raise_runtime_error({'ok': False, 'error': {'message': 'migration failed', 'details': details}})
    assert raised.value.details == details
    assert not raised.value.stop_refused
    assert not raised.value.stop_completed
