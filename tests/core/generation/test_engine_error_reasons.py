"""Public check text must survive install/start without publishing runtime text."""
from __future__ import annotations

import ast
from contextlib import redirect_stderr
import json
from pathlib import Path

import pytest

from astrid.core import engine_cli
from astrid.core.generation import engine_contracts as contracts
from astrid.core.generation.engine_contracts import engine_reason
from astrid.core.generation.engine_runner import _failure_diagnostics


ROOT = Path(__file__).resolve().parents[3]
INSTALL_START_SOURCES = (
    'astrid/core/engine_cli.py',
    *(f'astrid/core/generation/{name}.py' for name in (
        'engine_contracts', 'engine_recipe', 'engine_runner', 'engine_runner_target',
        'engine_custody', 'engine_execution', 'engine_platforms', '_engine_target_files',
        'engine_local_endpoint', 'engine_local_server',
    )),
)


def test_install_start_raise_sites_have_declared_fixed_reasons():
    errors = {name: cls for name, cls in vars(contracts).items()
              if isinstance(cls, type) and issubclass(cls, contracts.EngineError)}
    errors['CustodyNotOwnerOnly'] = engine_cli.CustodyNotOwnerOnly
    checked = 0
    for relative in INSTALL_START_SOURCES:
        tree = ast.parse((ROOT / relative).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in errors and node.args:
                argument = node.args[0]
                # This local conditional consists of the two fixed source checks below.
                if (relative.endswith('/engine_runner.py') and isinstance(argument, ast.Name)
                        and argument.id == 'reason'):
                    for text in ('source patch digest mismatch', 'source contains uncommitted content'):
                        assert errors[node.func.id](text).reason == text
                    continue
                assert isinstance(argument, ast.Constant), (relative, node.lineno)
                reason = errors[node.func.id](argument.value).reason
                assert reason == argument.value and reason, (relative, node.lineno)
                checked += 1
            elif isinstance(node.func, ast.Attribute) and node.func.attr == '_custody_refusal':
                argument = node.args[1]
                assert isinstance(argument, ast.Constant), (relative, node.lineno)
                assert engine_reason(argument.value) == argument.value, (relative, node.lineno)
                checked += 1
    assert checked > 250


@pytest.mark.parametrize('reason', [
    'service evidence does not match installation',
    'invalid service ownership state',
    'engine port is occupied by an unowned service',
    'service incarnation or launch binding differs',
    'server start did not establish ownership',
    'engine listener ownership changed',
    'service request differs from bound recipe or owner',
    'live engine custody is required',
    'supervisor installation identity differs',
])
def test_each_ownership_check_reaches_declared_install_log(tmp_path, monkeypatch, reason):
    def fail(**kwargs):
        error = contracts.EndpointOwnershipError(reason, step_id='start')
        error.port = 8188
        raise error

    monkeypatch.setattr(engine_cli, '_disk_anchor', lambda root: ('/', 1, 1, 10**15))
    log = tmp_path / 'engine-install.stderr'
    with log.open('w') as stream, redirect_stderr(stream):
        assert engine_cli.main(['install', 'comfyui', '--root', str(tmp_path / 'engine')],
                               backend_factory=fail) == 1
    error = json.loads(log.read_text())['error']
    assert error['code'] == 'endpoint_ownership' and error['step'] == 'start'
    assert error['reason'] == reason
    assert 'retry' in error['message'] or 'release the engine' in error['message']
    if 'port is occupied' in reason:
        assert '8188' in error['message'] and error['port'] == 8188
    else:
        assert reason in error['message']


@pytest.mark.parametrize('error_type', [contracts.TransportError, contracts.IntegrityError,
                                      contracts.CommandFailed, contracts.EndpointOwnershipError])
def test_untrusted_messages_and_diagnostic_reasons_are_never_public(monkeypatch, capsys, error_type):
    secret = 'PRIVATE_TRANSPORT_OWNER_TOKEN_ENV'
    error = error_type(secret, step_id='start')
    error.reason = secret
    error.diagnostics = {'reason': secret, 'porcelain': secret}
    error.command_output = {'argv': [secret], 'stdout': secret, 'stderr': secret}
    assert _failure_diagnostics(error) == {}
    def fail(**kwargs):
        raise error
    monkeypatch.setattr(engine_cli, '_disk_anchor', lambda root: ('/', 1, 1, 10**15))
    assert engine_cli.main(['install', 'comfyui', '--root', '/TARGET_ONLY/engine'],
                           backend_factory=fail) == 1
    assert secret not in capsys.readouterr().err


def test_error_reason_survives_failure_receipt_for_non_integrity_errors():
    error = contracts.EndpointOwnershipError('server start did not establish ownership', step_id='start')
    assert _failure_diagnostics(error) == {'reason': error.reason}


def test_unexpected_backend_exception_has_fixed_public_reason(monkeypatch, capsys):
    secret = 'PRIVATE_BACKEND_EXCEPTION_TOKEN'

    def fail(**kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(engine_cli, '_disk_anchor', lambda root: ('/', 1, 1, 10**15))
    assert engine_cli.main(['install', 'comfyui', '--root', '/TARGET_ONLY/engine'],
                           backend_factory=fail) == 1
    output = capsys.readouterr()
    assert not output.out and secret not in output.err
    error = json.loads(output.err)['error']
    assert error['code'] == 'engine_error'
    assert error['reason'] == 'unexpected engine operation failure'
    assert engine_reason(error['reason']) == error['reason']


@pytest.mark.parametrize(('running', 'pending', 'valid'), [
    (2, 3, True), (0, 0, True), (True, 1, False), (-1, 0, False),
    ('PRIVATE_QUEUE_TOKEN', 1, False), (1, None, False),
])
def test_busy_public_message_contains_only_validated_counts(monkeypatch, capsys, running, pending, valid):
    def fail(**kwargs):
        error = contracts.EngineBusy('PRIVATE_QUEUE_TOKEN')
        error.running_count = running
        error.pending_count = pending
        raise error

    monkeypatch.setattr(engine_cli, '_disk_anchor', lambda root: ('/', 1, 1, 10**15))
    assert engine_cli.main(['install', 'comfyui', '--root', '/TARGET_ONLY/engine'],
                           backend_factory=fail) == 1
    output = capsys.readouterr()
    assert 'PRIVATE_QUEUE_TOKEN' not in output.err
    error = json.loads(output.err)['error']
    assert error['code'] == 'engine_busy'
    if valid:
        assert f'running={running}, pending={pending}' in error['message']
    else:
        assert 'running=' not in error['message'] and 'pending=' not in error['message']


@pytest.mark.linux_native
def test_each_custody_check_has_a_fixed_public_reason(tmp_path):
    from astrid.core.generation.engine_custody import LinuxEngineServiceClient

    client = LinuxEngineServiceClient(root=str(tmp_path), engine='comfyui',
                                      platform='linux-x86_64-cu130',
                                      recipe_sha256='0' * 64, helper_sha256='0' * 64)
    for check, reason in contracts._CUSTODY_MESSAGES.items():
        error = client._refusal(contracts.CustodyUnresolved, None, reason=check)
        assert error.reason == reason


@pytest.mark.parametrize('readable', [False, True])
def test_busy_counts_require_a_successful_queue_read(tmp_path, monkeypatch, readable):
    from astrid.core.generation import engine_custody

    def queue(*args, **kwargs):
        if not readable:
            raise contracts.CustodyUnresolved('PRIVATE_QUEUE_FAILURE')
        return [[], []], [[]]

    monkeypatch.setattr(engine_custody, '_engine_queue', queue)
    with pytest.raises(contracts.EngineBusy) as err:
        engine_custody.refuse_busy_engine(root=str(tmp_path), engine='comfyui',
                                         platform='linux-x86_64-cu130', deadline=0)
    if readable:
        assert (err.value.running_count, err.value.pending_count) == (2, 1)
    else:
        assert not hasattr(err.value, 'running_count')
        assert not hasattr(err.value, 'pending_count')
