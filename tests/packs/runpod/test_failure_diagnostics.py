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


def test_host_summary_keeps_only_bounded_failure_facts():
    diagnostic = {'code': 'generation_failed', 'category': 'execution', 'node_id': '17',
                  'node_class': 'KSampler', 'field': SENTINEL, 'constraint': 'min_value'}
    summary = fd.host_summary(RuntimeError(SENTINEL), diagnostic, stage='fetch')
    assert summary == {'exception': 'RuntimeError', 'stage': 'fetch', 'code': 'generation_failed',
                       'category': 'execution', 'node_id': '17', 'node_class': 'KSampler',
                       'constraint': 'min_value'}
    assert SENTINEL not in json.dumps(summary)


def test_remote_issue_fields_are_allowlisted_and_redacted(tmp_path):
    payload = {'issues': [{'severity': 'error', 'code': 'generation_failed', 'message': SENTINEL,
                           'details': {'node_id': '17', 'node_class': 'KSampler',
                                       'field': SENTINEL, 'constraint': 'min_value'}}]}
    frame = emit(tmp_path, json.dumps(payload).encode())
    assert fd.parse_frame(frame) == {'code': 'generation_failed', 'category': 'execution',
                                     'node_id': '17', 'node_class': 'KSampler',
                                     'constraint': 'min_value'}
    assert SENTINEL not in frame


def test_journal_summary_is_revalidated_before_display():
    stored = {'exception': 'RuntimeError', 'stage': 'fetch', 'code': 'generation_failed',
              'category': 'execution', 'node_id': SENTINEL, 'prompt': SENTINEL}
    result = fd.safe_stored_summary(stored)
    assert result == {'exception': 'RuntimeError', 'stage': 'fetch',
                      'code': 'generation_failed', 'category': 'execution'}
    assert SENTINEL not in json.dumps(result)
    hostile_class = type('SENTINEL_TOKEN', (Exception,), {})
    assert fd.host_summary(hostile_class(SENTINEL))['exception'] == 'UnknownError'


def test_failure_observer_uses_one_scoped_transport_deadline(monkeypatch):
    from runpod_lifecycle import https
    from astrid.packs.runpod.remote_placement import _HttpsOps
    seen = []

    class Transport:
        def __init__(self, pod_id, token, *, deadline=None):
            seen.append((pod_id, deadline))
        def execute_command(self, command, timeout):
            return 0, "ok", ""
        def disconnect(self):
            pass

    monkeypatch.setattr(https, "HttpsPodTransport", Transport)
    original = _HttpsOps("pod-1", "private-token")
    observer = original.bounded(123.0)
    assert observer.exec("status", 12) == (0, "ok", "")
    assert seen == [("pod-1", 123.0)] and original.deadline is None


@pytest.mark.parametrize("mode", ["blocked_response", "route_retry"])
def test_pinned_https_receipt_read_stops_at_scoped_deadline(tmp_path, monkeypatch, mode):
    import httpx
    import time
    from runpod_lifecycle.https import HttpsPodTransport

    transport = HttpsPodTransport("pod1", "x" * 32, base_url="http://127.0.0.1:8000",
                                  deadline=time.monotonic() + 10,
                                  request_timeout=0.02, poll_interval=0.005, route_grace=1)
    transport.deadline = time.monotonic() + 0.5
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs.get("timeout"))
        if mode == "blocked_response":
            time.sleep(0.6)  # Simulate a transport that returns after its per-phase timeout.
            raise httpx.ReadTimeout("SENTINEL_TOKEN private response")
        return SimpleNamespace(status_code=404, content=b"", headers={})

    monkeypatch.setattr(transport.http, "request", request)
    try:
        with pytest.raises(Exception) as caught:
            transport.get("/fixed/install.json", tmp_path / "receipt")
        assert "SENTINEL_TOKEN" not in str(caught.value)
        assert calls and all(0 < timeout <= 0.5 for timeout in calls)
        assert not (tmp_path / "receipt").exists()
    finally:
        transport.disconnect()


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
