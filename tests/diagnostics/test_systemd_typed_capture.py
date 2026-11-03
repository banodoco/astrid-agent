import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('observe', Path(__file__).parents[2] / 'diagnostics/systemd-typed-capture/observe.py')
observe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observe)


def test_strict_classification_and_redaction():
    secret = '/private/secret path'
    raw = json.dumps({'type': 'a(sb)', 'data': [[secret, True], [secret, False]]}).encode()
    row = observe.classify(raw, 'property')
    assert row['classification'] == 'populated' and row['count'] == 2
    assert secret not in json.dumps(row)
    assert observe.classify(b'{"type":"a(sb)","data":[]}', 'property')['classification'] == 'empty'
    for bad in (b'', b'{"type":"a(sb)","data":[["/x",1]]}',
                b'{"type":"a(sb)","data":[],"data":[]}',
                b'{"type":"as","data":[]}', b'{"type":"a(sb)","data":[]}x'):
        assert observe.classify(bad, 'property')['classification'] == 'unknown'
    assert observe.classify(b'{"type":"o","data":["/wrong"]}', 'resolve')['classification'] == 'unknown'


def test_hooks_preserve_return_and_failure_and_actual_exits(monkeypatch):
    class Child:
        def __init__(self, code):
            self.returncode = code
    fake_subprocess = SimpleNamespace(Popen=lambda code: Child(code))
    text = 'Environment=PRIVATE=secret\n'
    def show():
        return text
    def bounded(args):
        fake_subprocess.Popen(23 if args == ['fail'] else 0)
        if args == ['fail']:
            raise ValueError('secret')
        return b'{"type":"a(sb)","data":[]}'
    service = SimpleNamespace(_transfer_systemctl_show=show, _bounded_busctl=bounded, subprocess=fake_subprocess, _TRANSFER_UNIT_PROPERTIES=('Environment', 'Id'))
    events = []
    with observe.capture(service, events):
        assert service._transfer_systemctl_show() == text
        assert service._bounded_busctl(['ok']).endswith(b'[]}')
        with pytest.raises(ValueError, match='secret'):
            service._bounded_busctl(['fail'])
    assert service._transfer_systemctl_show is show and service._bounded_busctl is bounded
    assert [row['exit'] for row in events] == [0, 0, 23]
    assert events[0]['present'] == ['Environment'] and events[0]['missing'] == ['Id']
    assert [row['endpoint'] for row in events] == [1, 1, 1]
    assert events[-1]['classification'] == 'unknown'
    assert 'secret' not in json.dumps(events) and 'PRIVATE' not in json.dumps(events)


def test_failed_show_records_exit_and_propagates():
    def fail():
        raise subprocess.CalledProcessError(7, ['systemctl'], output='secret')
    service = SimpleNamespace(_transfer_systemctl_show=fail, _bounded_busctl=lambda args: b'',
                              subprocess=SimpleNamespace(Popen=lambda: None))
    events = []
    with pytest.raises(subprocess.CalledProcessError), observe.capture(service, events):
        service._transfer_systemctl_show()
    assert events[0]['exit'] == 7 and 'secret' not in json.dumps(events)
