import hashlib
import json
from types import SimpleNamespace

import pytest
from scripts import ci_stop_r25_exact as stop


def setup_fake(monkeypatch):
    expected = {10: [1, 'Mon Nov 2 23:31:58 2026', 'a'],
                11: [10, 'Mon Nov 2 23:32:00 2026', 'b'],
                12: [11, 'Mon Nov 2 23:32:01 2026', 'c']}
    monkeypatch.setattr(stop, 'EXPECTED', expected)
    monkeypatch.setattr(stop, 'FREEZE_ORDER', [10, 11, 12])
    monkeypatch.setattr(stop, 'KILL_ORDER', [12, 11, 10])
    rows = {pid: {'uid': 503, 'ppid': value[0], 'state': 'S', 'start': value[1], 'command_sha256': value[2]}
            for pid, value in expected.items()}
    monkeypatch.setattr(stop, 'observe', lambda pid: rows.get(pid))
    monkeypatch.setattr(stop, 'tree', lambda: set(expected))
    sent = []
    def signal(pid, sig):
        sent.append((pid, sig))
        if sig == stop.signal.SIGSTOP: rows[pid]['state'] = 'T'
        elif sig == stop.signal.SIGCONT: rows[pid]['state'] = 'S'
        elif sig == stop.signal.SIGKILL: rows.pop(pid, None)
    monkeypatch.setattr(stop.os, 'kill', signal)
    return rows, sent


def test_success_freezes_and_kills_exact_pids_in_safe_order(monkeypatch):
    rows, sent = setup_fake(monkeypatch)
    audit = {}; saved = []
    assert stop.freeze_and_stop(audit, lambda value: saved.append(json.loads(json.dumps(value))))
    assert sent == [(10, stop.signal.SIGSTOP), (11, stop.signal.SIGSTOP), (12, stop.signal.SIGSTOP),
                    (12, stop.signal.SIGKILL), (11, stop.signal.SIGKILL), (10, stop.signal.SIGKILL)]
    assert audit['stage'] == 'stopped' and audit['after'] == {'10': 'absent', '11': 'absent', '12': 'absent'}
    assert not rows and saved[-1]['stage'] == 'stopped'


def test_extra_descendant_refuses_without_any_signal(monkeypatch):
    _, sent = setup_fake(monkeypatch)
    monkeypatch.setattr(stop, 'tree', lambda: {10, 11, 12, 99})
    audit = {}
    assert not stop.freeze_and_stop(audit, lambda _: None)
    assert audit['stage'] == 'refused-pre-kill' and sent == []


def test_changed_identity_before_freeze_refuses_without_signal(monkeypatch):
    rows, sent = setup_fake(monkeypatch)
    rows[11]['command_sha256'] = 'changed'
    audit = {}
    assert not stop.freeze_and_stop(audit, lambda _: None)
    assert audit['stage'] == 'refused-pre-kill' and sent == []


def test_new_descendant_after_freeze_resumes_exact_original_tree(monkeypatch):
    rows, sent = setup_fake(monkeypatch)
    calls = iter([{10, 11, 12}, {10, 11, 12, 99}])
    monkeypatch.setattr(stop, 'tree', lambda: next(calls))
    audit = {}
    assert not stop.freeze_and_stop(audit, lambda _: None)
    assert audit['stage'] == 'refused-pre-kill'
    assert sent == [(10, stop.signal.SIGSTOP), (11, stop.signal.SIGSTOP), (12, stop.signal.SIGSTOP),
                    (12, stop.signal.SIGCONT), (11, stop.signal.SIGCONT), (10, stop.signal.SIGCONT)]
    assert all(row['state'] == 'S' for row in rows.values())


def test_stop_error_cancels_pending_stop_before_return(monkeypatch):
    rows, sent = setup_fake(monkeypatch)
    def signal(pid, sig):
        sent.append((pid, sig))
        if pid == 11 and sig == stop.signal.SIGSTOP: raise OSError('fixture')
        # Simulate a pending SIGSTOP not yet visible in ps.
        if sig == stop.signal.SIGCONT: rows[pid]['state'] = 'S'
    monkeypatch.setattr(stop.os, 'kill', signal)
    audit = {}
    assert not stop.freeze_and_stop(audit, lambda _: None)
    assert sent == [(10, stop.signal.SIGSTOP), (11, stop.signal.SIGSTOP), (10, stop.signal.SIGCONT)]
    assert audit['stage'] == 'refused-pre-kill'


def test_observe_hashes_command_and_never_returns_it(monkeypatch):
    output = ' 503 1 Ss Mon Nov  2 23:31:58 2026 /private/task --secret=abc\n'
    monkeypatch.setattr(stop.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=output))
    observed = stop.observe(86213)
    assert observed['start'] == 'Mon Nov 2 23:31:58 2026'
    assert observed['command_sha256'] == hashlib.sha256(b'/private/task --secret=abc').hexdigest()
    assert 'secret' not in str(observed) and '/private' not in str(observed)


def test_manual_workflow_embeds_exact_guard_and_no_cleanup():
    import base64
    import re
    from pathlib import Path
    root = Path(__file__).parents[1]
    workflow = (root / '.github/workflows/journey.yml').read_text()
    match = re.search(r'base64\.b64decode\("([A-Za-z0-9+/=]+)", validate=True\)', workflow)
    assert match and base64.b64decode(match.group(1)) == (root / 'scripts/ci_stop_r25_exact.py').read_bytes()
    assert 'workflow_dispatch:' in workflow and 'actions/checkout' not in workflow
    source = (root / 'scripts/ci_stop_r25_exact.py').read_text()
    assert 'os.killpg' not in source and 'shutil.rmtree' not in source and '.unlink(' not in source
