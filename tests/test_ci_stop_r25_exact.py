import hashlib
import json
from types import SimpleNamespace

import pytest
from scripts import ci_stop_r25_exact as stop


def setup_fake(monkeypatch):
    monkeypatch.setattr(stop, 'silence_cancel', lambda: None)
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


def test_cancellation_while_freezing_resumes_every_persisted_intent(monkeypatch):
    rows, sent = setup_fake(monkeypatch)
    def signal(pid, sig):
        sent.append((pid, sig))
        if sig == stop.signal.SIGSTOP and pid == 11:
            raise stop.Cancelled('fixture cancellation')
        if sig == stop.signal.SIGSTOP: rows[pid]['state'] = 'T'
        if sig == stop.signal.SIGCONT: rows[pid]['state'] = 'S'
    monkeypatch.setattr(stop.os, 'kill', signal)
    audit = {}; checkpoints = []
    assert not stop.freeze_and_stop(audit, lambda value: checkpoints.append(json.loads(json.dumps(value))))
    assert audit['stage'] == 'refused-pre-kill'
    assert sent == [(10, stop.signal.SIGSTOP), (11, stop.signal.SIGSTOP),
                    (11, stop.signal.SIGCONT), (10, stop.signal.SIGCONT)]
    assert any(item.get('stop_intents') == [10, 11] and item['stage'] == 'freezing' for item in checkpoints)


def test_partial_kill_recovery_only_kills_still_frozen_matching_survivors(monkeypatch):
    rows, sent = setup_fake(monkeypatch)
    original = json.loads(json.dumps(rows))
    def partial(pid, sig):
        sent.append((pid, sig))
        if sig == stop.signal.SIGSTOP: rows[pid]['state'] = 'T'
        elif sig == stop.signal.SIGKILL and pid == 12: rows.pop(pid)
        elif sig == stop.signal.SIGKILL and pid == 11: raise OSError('fixture kill failure')
    monkeypatch.setattr(stop.os, 'kill', partial)
    audit = {'schema': 'r25-exact-stop/v2', 'runner': 'hannah-mac', 'uid': 503,
             'guard_sha256': stop.guard_fingerprint(), 'deletion': 'none'}
    assert not stop.freeze_and_stop(audit, lambda _: None)
    assert audit['stage'] == 'partial-kill-review-required'
    assert rows[10]['state'] == rows[11]['state'] == 'T'
    monkeypatch.setattr(stop, 'descendants', lambda roots: set(roots))
    def finish(pid, sig):
        sent.append((pid, sig))
        assert sig == stop.signal.SIGKILL
        rows.pop(pid)
    monkeypatch.setattr(stop.os, 'kill', finish)
    assert stop.recovery(audit, lambda _: None)
    assert audit['stage'] == 'recovered-stop' and audit['recovery_killed'] == [11, 10]
    assert not rows


def test_partial_recovery_refuses_identity_mismatch_without_signal(monkeypatch):
    rows, sent = setup_fake(monkeypatch)
    audit = {'schema': 'r25-exact-stop/v2', 'runner': 'hannah-mac', 'uid': 503,
             'guard_sha256': stop.guard_fingerprint(), 'deletion': 'none', 'stage': 'kill-started',
             'before': {str(pid): dict(row) for pid, row in rows.items()}, 'stop_intents': [10, 11, 12], 'frozen_pids': [10, 11, 12]}
    for row in rows.values(): row['state'] = 'T'
    rows[12]['start'] = 'different process'
    with pytest.raises(RuntimeError, match='changed'):
        stop.recovery(audit, lambda _: None)
    assert sent == []


def test_ps_error_is_not_treated_as_absence(monkeypatch):
    monkeypatch.setattr(stop.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr='permission denied'))
    with pytest.raises(RuntimeError, match='absence'):
        stop.observe(10)


def test_ps_empty_requires_esrch_for_absence(monkeypatch):
    monkeypatch.setattr(stop.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr=''))
    monkeypatch.setattr(stop.os, 'kill', lambda *a: (_ for _ in ()).throw(ProcessLookupError()))
    assert stop.observe(10) is None
    monkeypatch.setattr(stop.os, 'kill', lambda *a: None)
    with pytest.raises(RuntimeError, match='exists'):
        stop.observe(10)


def test_concurrent_lock_is_refused(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(stop, 'UID', os.getuid())
    fd = stop.acquire_lock(tmp_path)
    try:
        with pytest.raises(RuntimeError, match='active'):
            stop.acquire_lock(tmp_path)
    finally:
        os.close(fd)


def test_checkpoint_writes_durable_json_outside_runner_temp(tmp_path):
    path = tmp_path / 'audit.json'
    stop.checkpoint(path, {'stage': 'freezing', 'stop_intents': [10]})
    assert json.loads(path.read_text())['stop_intents'] == [10]
    assert list(tmp_path.iterdir()) == [path]


def test_failed_pre_kill_resume_can_be_explicitly_recovered(monkeypatch):
    rows, sent = setup_fake(monkeypatch)
    def interrupted(pid, sig):
        sent.append((pid, sig))
        if sig == stop.signal.SIGSTOP and pid == 11: raise stop.Cancelled('fixture cancellation')
        if sig == stop.signal.SIGSTOP: rows[pid]['state'] = 'T'
        if sig == stop.signal.SIGCONT and pid == 10: raise OSError('fixture resume failure')
    monkeypatch.setattr(stop.os, 'kill', interrupted)
    audit = {'schema': 'r25-exact-stop/v2', 'runner': 'hannah-mac', 'uid': 503,
             'guard_sha256': stop.guard_fingerprint(), 'deletion': 'none'}
    assert not stop.freeze_and_stop(audit, lambda _: None)
    assert audit['stage'] == 'pre-kill-recovery-required'
    def resume(pid, sig):
        assert sig == stop.signal.SIGCONT
        rows[pid]['state'] = 'S'
    monkeypatch.setattr(stop.os, 'kill', resume)
    assert stop.recovery(audit, lambda _: None)
    assert audit['stage'] == 'recovered-pre-kill' and rows[10]['state'] == 'S'


def test_cancel_handler_produces_catchable_interruption():
    with pytest.raises(stop.Cancelled):
        stop.cancel_handler(stop.signal.SIGTERM, None)


def test_recovery_does_not_create_missing_audit_directory(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(stop, 'UID', os.getuid())
    path = tmp_path / 'missing'
    with pytest.raises(RuntimeError, match='absent'):
        stop.owned_private_directory(path, create=False)
    assert not path.exists()


def test_partial_kill_recovery_refuses_incomplete_freeze_record(monkeypatch):
    rows, sent = setup_fake(monkeypatch)
    audit = {'schema': 'r25-exact-stop/v2', 'runner': 'hannah-mac', 'uid': 503,
             'guard_sha256': stop.guard_fingerprint(), 'deletion': 'none', 'stage': 'kill-started',
             'before': {str(pid): dict(row) for pid, row in rows.items()},
             'stop_intents': [10, 11], 'frozen_pids': [10, 11]}
    with pytest.raises(RuntimeError, match='fully verified'):
        stop.recovery(audit, lambda _: None)
    assert sent == []


def test_interrupted_partial_recovery_keeps_durable_recovery_stage(monkeypatch):
    rows, sent = setup_fake(monkeypatch)
    before = {str(pid): dict(row) for pid, row in rows.items()}
    for row in rows.values(): row['state'] = 'T'
    audit = {'schema': 'r25-exact-stop/v2', 'runner': 'hannah-mac', 'uid': 503,
             'guard_sha256': stop.guard_fingerprint(), 'deletion': 'none', 'stage': 'kill-started',
             'before': before, 'stop_intents': [10, 11, 12], 'frozen_pids': [10, 11, 12]}
    monkeypatch.setattr(stop, 'descendants', lambda roots: set(roots))
    monkeypatch.setattr(stop.os, 'kill', lambda pid, sig: (_ for _ in ()).throw(stop.Cancelled('cancelled')))
    saved = []
    with pytest.raises(stop.Cancelled):
        stop.recovery(audit, lambda value: saved.append(json.loads(json.dumps(value))))
    assert saved[-1]['stage'] == 'recovery-killing'
    assert saved[-1]['recovery_killed'] == []
    assert all(row['state'] == 'T' for row in rows.values())


def test_cancel_silencing_restores_original_signal_mask(monkeypatch):
    calls = []
    previous = {stop.signal.SIGUSR1}
    def mask(how, values):
        calls.append((how, values))
        return previous
    monkeypatch.setattr(stop.signal, 'pthread_sigmask', mask)
    monkeypatch.setattr(stop.signal, 'signal', lambda sig, handler: None)
    stop.silence_cancel()
    assert calls[0][0] == stop.signal.SIG_BLOCK
    assert calls[-1] == (stop.signal.SIG_SETMASK, previous)
