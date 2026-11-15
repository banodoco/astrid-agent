import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from astrid.core.generation import engine_custody as custody


def child(script):
    return subprocess.Popen([sys.executable, '-u', '-c', script], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_attach_after_output(tmp_path):
    p = child("import sys; print('before'); sys.stdin.readline(); print('after')")
    out = custody._Output(p.stdout, 1024)
    wait_for(lambda: 'before' in out.text())
    path = tmp_path / 'engine.log'
    out.attach_sink(path)
    p.stdin.write(b'go\n'); p.stdin.flush()
    p.wait(timeout=5); out.thread.join(5)
    assert path.read_bytes() == b'after\n'


def test_two_streams_and_truncation(tmp_path):
    p = child("import sys; sys.stdin.readline(); sys.stdout.write('one\\n'); sys.stderr.write('two\\n'); sys.stdin.readline(); sys.stdout.write('three\\n')")
    outputs = [custody._Output(p.stdout, 1024), custody._Output(p.stderr, 1024)]
    path = tmp_path / 'engine.log'
    for out in outputs:
        out.attach_sink(path)
    p.stdin.write(b'go\n'); p.stdin.flush()
    wait_for(lambda: path.exists() and len(path.read_bytes().splitlines()) == 2)
    assert sorted(path.read_bytes().splitlines()) == [b'one', b'two']
    subprocess.run([sys.executable, '-c', 'import sys; open(sys.argv[1], "wb").close()', str(path)], check=True)
    p.stdin.write(b'go\n'); p.stdin.flush()
    p.wait(timeout=5)
    for out in outputs:
        out.thread.join(5)
    assert path.read_bytes() == b'three\n'


def test_unwritable_sink_preserves_ring(tmp_path):
    p = child("print('still drained')")
    out = custody._Output(p.stdout, 1024)
    out.attach_sink(tmp_path / 'missing' / 'log')
    p.wait(timeout=5); out.thread.join(5)
    assert 'still drained' in out.text()


def test_sink_write_failure_preserves_ring(tmp_path, monkeypatch):
    p = child("import sys; sys.stdin.readline(); print('still drained')")
    out = custody._Output(p.stdout, 1024)
    out.attach_sink(tmp_path / 'engine.log')
    real_write = custody.os.write

    def fail_sink(fd, data):
        if fd == out.sink:
            raise OSError('write failed')
        return real_write(fd, data)

    monkeypatch.setattr(custody.os, 'write', fail_sink)
    p.stdin.write(b'go\n'); p.stdin.flush()
    p.wait(timeout=5); out.thread.join(5)
    assert not out.thread.is_alive()
    assert 'still drained' in out.text()


def test_engine_log_path():
    assert custody.engine_log_path('/e', 'comfyui') == '/e/runtime/out/sessions/comfyui-logs/engine.log'


def test_launch_request_log_captures_early_exit(tmp_path):
    supervisor = custody._Supervisor.__new__(custody._Supervisor)
    supervisor.root = str(tmp_path)
    supervisor.identity = {'engine': 'comfyui'}
    supervisor.recipe = SimpleNamespace(start=SimpleNamespace(port=0),
                                        health=SimpleNamespace(timeout_seconds=1, poll_interval_seconds=0.01))
    supervisor.unresolved = False
    supervisor.engine = None
    supervisor.token = 'owner'
    supervisor.valid_launch = lambda launch: True
    supervisor.source_identity = lambda deadline, channel: ('revision', 'digest')
    # 13b4bf569 binds the launch at admission through the platform adapter.
    supervisor.adapter = SimpleNamespace(start_request=lambda recipe, root, *, deadline, models_root: SimpleNamespace(
        argv=(sys.executable,), cwd='/', env={}))
    def spawn(request, *, output_log=None):
        p = child("import sys; sys.stdout.write('startup failure\\n'); sys.stderr.write('details\\n'); sys.exit(1)")
        # As _Owned does: the launch names the complete log its output is appended to.
        return SimpleNamespace(process=p,
                               stdout=custody._Output(p.stdout, 1024, output_log),
                               stderr=custody._Output(p.stderr, 1024, output_log))

    def health(channel, deadline):
        assert supervisor.engine.process.wait(timeout=5) == 1
        raise custody.EndpointHealthError('exited before readiness')

    def stop():
        supervisor.engine.stdout.thread.join(5)
        supervisor.engine.stderr.thread.join(5)

    supervisor.spawn = spawn
    supervisor.health = health
    supervisor.stop_engine = stop
    payload = {'schema': custody._SCHEMA, 'engine': 'comfyui', 'launch': {'argv': [sys.executable], 'cwd': '/', 'env': {}},
               'owner_id': None, 'timeout_seconds': 1, 'poll_interval_seconds': 0.01}
    with pytest.raises(custody.EndpointHealthError):
        supervisor.service('start', payload, time.monotonic() + 5, None)
    path = tmp_path / 'runtime/out/sessions/comfyui-logs/engine.log'
    assert sorted(path.read_bytes().splitlines()) == [b'details', b'startup failure']
