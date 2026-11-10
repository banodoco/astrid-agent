"""CPU proof of the fixed failure protocol; no provider or pod is contacted."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import failure_diagnostics as fd
from astrid.packs.runpod import machine as rm

SENTINEL = 'SENTINEL_TOKEN https://private.example/secret private-output'


def emit(tmp_path, raw):
    path = tmp_path / 'result.json'
    path.write_bytes(raw)
    command = rm.generation_command('cpu-image', '/fake/run', mode='t2i')
    # Execute the exact producer projection shipped in the failed-job branch.
    branch = command.split('if [ $rc -ne 0 ]; then ', 1)[1].split('; exit $rc; fi', 1)[0]
    argv = shlex.split(branch)
    argv[0], argv[-1] = sys.executable, str(path)
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    assert not result.stderr and SENTINEL not in result.stdout
    return result.stdout


@pytest.mark.parametrize('payload,expected', [
    ({'issues': [{'severity': 'error', 'code': 'input_rejected', 'message': SENTINEL,
                  'details': {'engine_code': 'download_failed', 'url': SENTINEL}}]},
     {'code': 'download_failed', 'category': 'model'}),
    ({'error': {'code': 'deadline_exceeded', 'message': SENTINEL}},
     {'code': 'deadline_exceeded', 'category': 'deadline'}),
    ({'issues': [{'severity': 'warning', 'code': 'missing_model', 'message': SENTINEL}]}, fd.fallback()),
    ({'error': {'code': SENTINEL, 'message': SENTINEL}}, fd.fallback()),
    ({'error': {'code': ['download_failed'], 'message': SENTINEL}}, fd.fallback()),
])
def test_emitted_failure_is_bounded_allowlisted_and_redacted(tmp_path, payload, expected):
    stdout = emit(tmp_path, json.dumps(payload).encode())
    assert fd.parse_frame(stdout) == expected
    assert len(stdout) < 200


@pytest.mark.parametrize('raw', [b'not-json', b'x' * (fd.MAX_RESULT_BYTES + 1), b'[]', b'null'])
def test_unreadable_or_invalid_result_has_generic_fallback(tmp_path, raw):
    assert fd.parse_frame(emit(tmp_path, raw)) == fd.fallback()


def test_result_symlink_and_missing_file_are_not_read(tmp_path):
    secret = tmp_path / 'secret'
    secret.write_text(json.dumps({'code': 'download_failed', 'message': SENTINEL}))
    link = tmp_path / 'link'
    link.symlink_to(secret)
    assert fd.read_result(link) == fd.fallback()
    assert fd.read_result(tmp_path / 'missing') == fd.fallback()


@pytest.mark.parametrize('stdout', [
    SENTINEL, fd.MARK + '{broken', fd.MARK + json.dumps({'code': SENTINEL, 'category': 'model'}),
    fd.MARK + json.dumps({'code': 'download_failed', 'category': SENTINEL}),
    fd.MARK + json.dumps({'code': 'download_failed', 'category': 'model', 'message': SENTINEL}),
    (fd.MARK + '{"code":"download_failed","category":"model"}\n') * 2,
    'x' * (fd.MAX_FRAME_BYTES + 1),
])
def test_untrusted_frames_never_escape_or_select_unknown_categories(stdout):
    assert fd.parse_frame(stdout) == fd.fallback()


@pytest.mark.parametrize('exit_code', [2, SENTINEL])
def test_fetch_surfaces_only_fixed_diagnostic_and_safe_exit(tmp_path, exit_code):
    stdout = emit(tmp_path, json.dumps({'error': {'code': 'download_failed', 'message': SENTINEL}}).encode())
    calls = []
    class Ops:
        def job_poll(self, job):
            return {'state': 'exited', 'exit_code': exit_code}
        def exec(self, command, timeout):
            calls.append(command)
            return 0, stdout, SENTINEL
    run = SimpleNamespace(job_id='fake-job', directory='/fake/job',
                          lease=SimpleNamespace(state=None, claim=SimpleNamespace(ops=Ops())))
    machine = rm.RunPodMachine(SimpleNamespace(placement=None))
    machine._checkpoint = lambda *args: None
    with pytest.raises(rm.MachineError) as caught:
        machine.fetch(run, tmp_path / 'unused')
    assert str(caught.value) == ('remote generation failed (exit '
        + ('2' if exit_code == 2 else 'unknown') + '; category=model; code=download_failed)')
    assert SENTINEL not in str(caught.value)
    assert calls == [f'tail -c {fd.MAX_FRAME_BYTES + 1} /fake/job/stdout']


def test_real_job_cli_keeps_structured_failure_code(monkeypatch, tmp_path, capsys):
    from astrid.core.generation import cli, local_job
    from astrid.core.generation.preflight import generation_error
    error = generation_error({'code': 'download_failed', 'message': SENTINEL})
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(local_job, 'run_job', fail)
    assert cli.main(['cpu-image', '--placement', 'local', '--job', str(tmp_path / 'job.json'),
                     '--out', str(tmp_path / 'out'), '--json']) == 2
    envelope = json.loads(capsys.readouterr().out)
    stdout = emit(tmp_path, json.dumps(envelope).encode())
    assert fd.parse_frame(stdout) == {'code': 'download_failed', 'category': 'model'}
    assert SENTINEL not in stdout
