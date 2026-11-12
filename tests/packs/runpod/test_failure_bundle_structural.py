"""Regressions for structural masking and incomplete workflow context."""
import json
from pathlib import Path

import pytest

from astrid.packs.runpod import failure_bundle as fb
from tests.packs.runpod.test_failure_bundle import _pod


@pytest.mark.parametrize('text', [
    'cm9zZQ', 'YX4_Yg==', '%72%6f%73%65', '%2572%256f%2573%2565',
    '&#114;&#111;&#115;&#101;', r'\x72\x6f\x73\x65',
    'https%3A%2F%2Falice%3Aviolet%40host.internal%2Fprivate',
    'https&#58;&#47;&#47;alice:violet@host.internal/private', r'\x3a\x3a',
])
def test_encoded_span(text):
    for _ in range(2):
        result = fb.Redactor(prompts=['rose']).text(text)
        assert result != text
        assert all(part not in result for part in ('alice', 'violet', 'host.internal'))
        text = result + ' ' + text  # exercise the second pass on original and masked spans


def test_whole_quoted_password():
    result = fb.Redactor().text('password="violet meadow password"')
    assert 'violet' not in result and 'meadow' not in result


def test_shared_json_credentials():
    result = fb.Redactor().value({'pwd': 'violet', 'session_id': 'tulip'})
    assert list(result.values()) == ['<secret>', '<secret>']


@pytest.mark.parametrize('kind', ['incomplete', 'over_cap'])
def test_incomplete_workflow_context(tmp_path, kind):
    _, request = _pod(tmp_path, bindings={'prompt': 'rose'})
    run = Path(request['run_dir'])
    graph = run / 'in' / 'workflow.json'
    graph.parent.mkdir(exist_ok=True)
    graph.write_text(json.dumps({'a': {'class_type': 'rose', 'inputs': {'violet': 'secret'}}}))
    job_path = run / 'job.json'
    job = json.loads(job_path.read_text())
    job['execution'] = {'source': str(graph)}
    if kind == 'incomplete':
        job = {'execution': job['execution']}
    else:
        request['redaction_context'] = {'complete': True, 'prompts': ['x' * 262145], 'secrets': []}
    job_path.write_text(json.dumps(job))
    folder, _, facts = fb.collect(request, environ={})
    assert facts['context_complete'] is False
    text = (Path(folder) / 'workflow.redacted.json').read_text()
    assert 'rose' not in text and 'violet' not in text
    assert 'node_1' in text


def test_readable_diagnostics():
    text = '\n'.join([
        'Traceback (most recent call last):',
        '  File "/opt/app/main.py", line 12, in execute',
        'CUDA out of memory. Tried to allocate 2.00 GiB',
        'ERROR: pip could not find a version that satisfies the requirement torch',
        'ComfyUI node error: KSampler failed',
    ])
    assert fb.Redactor().text(text) == text


@pytest.mark.parametrize('text', ['abcdefghijklmnoq', 'YXJv/c2U', 'abc_def8', 'abCD12xy', 'foo-bar8'])
def test_structural_base64_rule_without_known_context(text):
    assert fb.Redactor().text(text) == '<encoded>'


@pytest.mark.parametrize('text', ['diagnostic', '/opt/app/main', 'ComfyUI', 'Traceback'])
def test_plain_words_and_paths(text):
    assert fb.Redactor().text(text) == text


@pytest.mark.parametrize('swap', [False, True])
@pytest.mark.parametrize('cross_device', [False, True])
def test_success_fetch_retains_directory_through_transport_rename(tmp_path, monkeypatch, swap, cross_device):
    import hashlib
    from types import SimpleNamespace

    from astrid.packs.runpod.machine import RunPodMachine

    destination = tmp_path / 'out'
    destination.mkdir()
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    moved = tmp_path / 'moved'
    outside = tmp_path / 'outside'
    outside.mkdir()
    if cross_device:
        import errno
        import os

        original_replace = os.replace

        def replace(source, destination, **kwargs):
            if kwargs.get('dst_dir_fd') is not None and Path(source).is_absolute():
                raise OSError(errno.EXDEV, 'different filesystems')
            return original_replace(source, destination, **kwargs)

        monkeypatch.setattr(os, 'replace', replace)
    payload = b'normal output'
    manifest = json.dumps({'files': {'result.bin': {'size': len(payload),
                          'sha256': hashlib.sha256(payload).hexdigest()}}}).encode()

    def get(remote, local):
        if remote.endswith(fb.MANIFEST_NAME):
            local.write_bytes(manifest)
            return
        if swap:
            destination.rename(moved)
            destination.symlink_to(outside, target_is_directory=True)
        partial = local.with_name(local.name + '.https-part')
        partial.write_bytes(payload)
        partial.replace(local)

    RunPodMachine._fetch_verified(SimpleNamespace(get=get), '/output', destination, scratch,
                                  hashlib.sha256(manifest).hexdigest())
    assert not list(outside.iterdir())
    assert ((moved if swap else destination) / 'result.bin').read_bytes() == payload


@pytest.mark.parametrize('state', ['collected', 'over_cap'])
def test_disposable_cleanup_cannot_hold_collection(tmp_path, monkeypatch, state):
    import threading
    import time
    from types import SimpleNamespace

    from astrid.packs.runpod import machine as rm

    entered, finish = threading.Event(), threading.Event()

    def blocked_cleanup(*args, **kwargs):
        entered.set()
        finish.wait(5)

    monkeypatch.setattr('shutil.rmtree', blocked_cleanup)
    owner = SimpleNamespace(clock=time.time, closed=False, _read=lambda: {})
    ops = SimpleNamespace(token='', bounded=lambda *args, **kwargs: SimpleNamespace(
        exec=lambda *args: (0, fb.MANIFEST_MARK + 'a' * 64, '')))
    machine = rm.RunPodMachine.__new__(rm.RunPodMachine)
    machine._require = lambda: SimpleNamespace(installation=None)
    def fetch(*args, **kwargs):
        if state == 'over_cap':
            raise rm.BundleOverCap('test cap')
        return {}

    machine._fetch_verified = fetch
    machine._redact_locally = lambda *args, **kwargs: {'files': [], 'bytes': 0}
    started = time.monotonic()
    try:
        result = machine._collect_failure(owner=owner, ops=ops, abort_event=threading.Event(),
            paid_expiry=time.time()+60, limit=None, scratch=tmp_path, name='cleanup', run_dir=None,
            job_dir=None, phase='preparation', exit_code=1, include_install=False, prompts=[], timing=None)
        assert entered.is_set()
        assert time.monotonic() - started < 1
        assert result['state'] == state
    finally:
        finish.set()


def test_controller_incomplete_workflow_keeps_opaque_edges_with_many_inputs():
    graph = {'a': {'class_type': 'rose', 'inputs': {f'private_field_{i}': ['a', 0] for i in range(12)}}}
    shaped, _ = fb.structure(graph, fb.Redactor(), context_complete=False)
    cleaned = json.loads(fb.redact_file('workflow.redacted.json', fb.dump(shaped).encode(),
                                      fb.Redactor(), context_complete=False))
    assert 'rose' not in json.dumps(cleaned) and 'private_field' not in json.dumps(cleaned)
    node = cleaned['nodes']['node_1']
    assert len(node['inputs']) == 12
    assert all(edge == ['node_1', 0] for edge in node['inputs'].values())


def test_digest_label_cannot_exempt_encoded_secret():
    value = 'violet'.encode().hex().ljust(64, '0')
    assert value not in fb.Redactor().text('sha256:' + value)


@pytest.mark.parametrize('text', ['{"private label":"violet"}', r'{"pr\u0069vate":"violet"}'])
def test_quoted_credential_key_with_spaces_in_text(text):
    assert 'violet' not in fb.Redactor().text(text)


def test_shipped_collector_and_controller_mask_exact_encoded_spans(tmp_path):
    import base64
    import subprocess
    import sys

    from tests.packs.runpod import pod_collector

    values = ['cm9zZQ', 'YX4_Yg==', '%72%6f%73%65', '%2572%256f%2573%2565',
              '&#114;&#111;&#115;&#101;', r'\x72\x6f\x73\x65',
              'https%3A%2F%2Falice%3Aviolet%40host.internal%2Fprivate',
              'https&#58;&#47;&#47;alice:violet@host.internal/private', r'\x3a\x3a',
              'password="violet meadow password"', '{"pwd":"violet","session_id":"tulip"}']
    _, request = _pod(tmp_path, bindings={'prompt': 'rose'}, stderr='\n'.join(values), result={}, graph={})
    source, _ = pod_collector.decode(fb.command(request))
    payload = base64.b64encode(json.dumps(request).encode()).decode()
    done = subprocess.run([sys.executable, '-I', '-S', '-', payload], input=source,
                          capture_output=True, env={'PATH': '/usr/bin:/bin'}, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    pod = (Path(request['failures_root']) / request['name'] / 'error-tail.txt').read_bytes()
    controller = fb.redact_file('error-tail.txt', pod, fb.Redactor(prompts=['rose']))
    for body in (pod.decode(), controller.decode()):
        assert all(value not in body for value in values)
        assert all(value not in body for value in ('alice', 'violet', 'tulip', 'host.internal'))
