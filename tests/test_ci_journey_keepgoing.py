import json
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey
from scripts import ci_journey_keepgoing as keepgoing
from scripts import journey_version_jumps as jumps


def setup_diagnostic(tmp_path, monkeypatch, failure):
    evidence = tmp_path / 'run' / 'evidence' / 'release0-to-release1'
    app = tmp_path / 'app'
    app.mkdir()
    candidate = tmp_path / 'candidate.json'
    candidate.write_text('{}')
    manifest0 = tmp_path / 'manifest0.json'
    manifest0.write_text('{}')
    args = SimpleNamespace(job_id='direct-linux', mode='direct', leg='release0-to-release1',
                           shell='sh', app_checkout=app, candidate=candidate)
    calls = []
    state = {'release': 0}
    manifest = {'release': 1}
    monkeypatch.setattr(jumps, 'release0_admission', lambda _: (tmp_path, manifest0, {}))
    monkeypatch.setattr(journey, 'selected_manifest', lambda *a, **k: (candidate, manifest))
    monkeypatch.setattr(keepgoing.gate, 'validate_tuple', lambda *a: manifest)
    monkeypatch.setattr(journey, 'sentinel_observation', lambda *a: {'alive': True})
    monkeypatch.setattr(jumps, 'bootstrap_release0', lambda *a: calls.append('bootstrap0'))
    monkeypatch.setattr(jumps, 'start_direct_runtime', lambda *a: calls.append('start'))
    monkeypatch.setattr(journey, 'wait_ready', lambda *a: None)
    monkeypatch.setattr(journey, 'installation', lambda *a: (tmp_path / ('v' + str(state['release'])), tmp_path / 'data', {'release': state['release']}))
    monkeypatch.setattr(jumps, 'installed_release', lambda v: v['release'])
    monkeypatch.setattr(journey, 'selected_tuple', lambda *a: {})
    monkeypatch.setattr(journey, 'assert_direct_service_ownership', lambda *a: calls.append('ownership'))
    monkeypatch.setattr(journey, 'assert_one_runtime', lambda *a: calls.append('one'))
    def verify(mode, *args, **kwargs):
        calls.append('verify-' + mode)
        if failure == 'verify' and mode == 'observe':
            raise RuntimeError('verification exact failure')
        receipt = args[2] / f'verify-{mode}.json'
        receipt.parent.mkdir(parents=True, exist_ok=True)
        passed = not (failure == 'observe-failed' and mode == 'observe')
        receipt.write_text(json.dumps({'passed': passed,
                                       'failures': [] if passed else ['capabilities.approval', 'capabilities.required']}))
        return receipt
    monkeypatch.setattr(journey, 'verify', verify)
    monkeypatch.setattr(jumps, 'direct_workspace', lambda *a: {'project': 'p'} if a[4] == 'seed' else {'snapshot': True})
    monkeypatch.setattr(jumps, 'task_after_update', lambda *a: 'task')
    monkeypatch.setattr(journey, 'healthy', lambda *a: {'pid': 1 if state['release'] == 0 else 2, 'runtime_instance_id': str(state['release'])})
    monkeypatch.setattr(journey, 'process_birth', lambda pid: 'birth' if state['release'] == 0 else None)
    def bootstrap(*args):
        calls.append('update')
        if failure == 'update':
            raise RuntimeError('update exact failure')
        state['release'] = 1
    monkeypatch.setattr(journey, 'bootstrap', bootstrap)
    monkeypatch.setattr(jumps, 'selected_candidate', lambda *a: {})
    monkeypatch.setattr(jumps, 'assert_preserved', lambda *a: None)
    return args, evidence, calls


@pytest.mark.parametrize('runner', ['linux', 'macos', 'windows'])
def test_failed_verification_is_labelled_and_independent_update_runs(tmp_path, monkeypatch, runner):
    args, evidence, calls = setup_diagnostic(tmp_path, monkeypatch, 'verify')
    args.job_id = 'direct-' + runner
    report = keepgoing.execute(args, tmp_path / 'program', tmp_path / 'data', evidence, SimpleNamespace(pid=1), 'birth')
    row = next(row for row in report['stages'] if row['stage'] == 'release0-verify')
    assert row['result'] == 'fail' and row['first_error'] == 'RuntimeError: verification exact failure'
    assert row['forced_continuation_used'] is True and 'FORCED' in row['continuation']
    assert 'update' in calls
    assert next(row for row in report['stages'] if row['stage'] == 'release1-task')['result'] == 'pass'
    receipt = json.loads((evidence / 'journey-result.json').read_text())
    assert receipt['proof_mode'] == keepgoing.PROOF_MODE and receipt['status'] == 'fail'
    assert receipt['qualification']['eligible'] is False and receipt['classification'] == 'NON-PROOF'
    assert '| stage | result | observation | forced-continuation-used | first error |' in (evidence / 'keepgoing-report.md').read_text()


def test_failed_release0_observation_is_reported_without_gating_continuation(tmp_path, monkeypatch):
    args, evidence, calls = setup_diagnostic(tmp_path, monkeypatch, 'observe-failed')
    report = keepgoing.execute(args, tmp_path / 'program', tmp_path / 'data', evidence,
                               SimpleNamespace(pid=1), 'birth')
    rows = {row['stage']: row for row in report['stages']}
    observation = rows['release0-verify']
    assert observation['result'] == 'observed-failed'
    assert observation['observation'] == {
        'passed': False,
        'failures': ['capabilities.approval', 'capabilities.required'],
    }
    assert observation['failure_class'] == 'real'
    assert observation['forced_continuation_used'] is False
    assert rows['release1-update']['result'] == 'pass' and 'update' in calls
    assert rows['release1-verify-update']['result'] == 'pass'
    assert any(item['name'] == 'verified0' and item['source'] == 'genuine'
               for item in rows['release1-verify-update']['preconditions'])
    rendered = (evidence / 'keepgoing-report.md').read_text()
    assert '| release0-verify | observed-failed | {"failures": ["capabilities.approval", "capabilities.required"], "passed": false} |' in rendered
    assert 'REAL failures: release0-verify' in rendered
    stage_index = [row['stage'] for row in report['stages']].index('release0-verify')
    assert f'style s{stage_index} fill:#f99' in rendered


def test_failed_update_does_not_force_service_or_marker(tmp_path, monkeypatch):
    args, evidence, calls = setup_diagnostic(tmp_path, monkeypatch, 'update')
    marker = tmp_path / 'instance.lock'
    marker.write_text('untouched')
    report = keepgoing.execute(args, tmp_path / 'program', tmp_path / 'data', evidence, SimpleNamespace(pid=1), 'birth')
    rows = {row['stage']: row for row in report['stages']}
    assert rows['release1-update']['first_error'] == 'RuntimeError: update exact failure'
    assert rows['release1-start']['result'] == 'not-run'
    assert rows['release1-task']['result'] == 'not-run'
    assert not any(row['forced_continuation_used'] for row in report['stages'])
    assert calls.count('start') == 1 and marker.read_text() == 'untouched'
    assert rows['sentinel']['result'] == 'pass'


def test_default_and_diagnostic_proof_guards_are_wired():
    workflow = (journey.ROOT / '.github/workflows/journey.yml').read_text()
    assert workflow.count('diagnostic_keep_going:\n') == 2
    assert workflow.count('description: NON-PROOF disposable direct keep-going diagnostic\n        required: false\n        default: false') == 2
    assert 'diagnostic_keep_going requires direct_only' in workflow
    assert 'test "$DIAGNOSTIC_KEEP_GOING" != true' in workflow


def test_preflight_failure_is_diagnostic_and_leaves_all_stages_unrun(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(journey, "runner_identity", lambda: {})
    def refuse(_identity):
        raise RuntimeError("unknown service, refuse")
    monkeypatch.setattr(journey, "assert_runner_identity", refuse)
    monkeypatch.setattr(journey, "snapshot_failure_evidence", lambda *a: None)
    run_root = tmp_path / "fresh"
    args = SimpleNamespace(run_root=run_root, root=run_root / "program", data_root=run_root / "data",
                           job_id="direct-linux", mode="direct", leg="release0-to-release1", shell="sh",
                           diagnostic_start_at=None, diagnostic_keep_going=True)
    with pytest.raises(RuntimeError, match="unknown service"):
        journey.execute(args)
    evidence = run_root / "evidence" / "release0-to-release1"
    report = json.loads((evidence / "keepgoing-report.json").read_text())
    receipt = json.loads((evidence / "journey-result.json").read_text())
    assert report["stages"][0]["first_error"] == "RuntimeError: unknown service, refuse"
    assert all(row["result"] == "not-run" for row in report["stages"][1:])
    assert receipt["proof_mode"] == "keep-going-diagnostic" and receipt["status"] == "fail"
    assert not (run_root / "program").exists()


def test_keepgoing_cli_rejects_unscoped_modes(tmp_path):
    common = ["--leg", "fresh-install", "--mode", "direct", "--app-checkout", str(tmp_path),
              "--run-root", str(tmp_path / "run"), "--root", str(tmp_path / "run/program"),
              "--data-root", str(tmp_path / "run/data"), "--diagnostic-keep-going"]
    with pytest.raises(SystemExit) as exc:
        journey.main(common)
    assert exc.value.code == 2
    assert not (tmp_path / "run").exists()


def test_keepgoing_log_retention_preserves_error_path_and_bounded_tail(tmp_path, monkeypatch):
    args, evidence, calls = setup_diagnostic(tmp_path, monkeypatch, "none")
    def fail_with_log(*arguments):
        path = evidence / "release0" / "release0-bootstrap.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("specific underlying installer error")
        raise RuntimeError(f"release0-bootstrap.log: exit 1; see {path}")
    monkeypatch.setattr(jumps, "bootstrap_release0", fail_with_log)
    report = keepgoing.execute(args, tmp_path / "program", tmp_path / "data", evidence, SimpleNamespace(pid=1), "birth")
    row = next(row for row in report["stages"] if row["stage"] == "release0-bootstrap")
    assert "exit 1; see" in row["first_error"] and row["evidence_path"].endswith("release0-bootstrap.log")
    journey.retain_named_evidence(evidence, "before-cleanup")
    artifact = evidence.parent.parent / "evidence-upload" / row["retained_evidence"]
    assert artifact.read_text() == "specific underlying installer error"


def test_failed_downstream_verification_after_bypass_is_suspect(tmp_path, monkeypatch):
    args, evidence, calls = setup_diagnostic(tmp_path, monkeypatch, 'verify')
    def fail_both(mode, *args, **kwargs):
        if mode in ('observe', 'update'):
            raise RuntimeError(mode + ' failed')
    monkeypatch.setattr(journey, 'verify', fail_both)
    report = keepgoing.execute(args, tmp_path / 'program', tmp_path / 'data', evidence, SimpleNamespace(pid=1), 'birth')
    rows = {row['stage']: row for row in report['stages']}
    assert rows['release0-verify']['failure_class'] == 'real'
    assert rows['release1-verify-update']['failure_class'] == 'suspect:release0-verify'
    assert any(item['source'] == 'forced-by:release0-verify' for item in rows['release1-verify-update']['preconditions'])
    text = (evidence / 'keepgoing-report.md').read_text()
    assert 'REAL failures: release0-verify' in text
    assert 'SUSPECT failures: release1-verify-update (suspect:release0-verify)' in text
    assert '-.->' in text


def test_fresh_install_continues_independent_checks_after_verify_failure(tmp_path, monkeypatch):
    args, evidence, calls = setup_diagnostic(tmp_path, monkeypatch, 'none')
    args.leg = 'fresh-install'
    monkeypatch.setattr(journey, 'wait_ready', lambda *a, **kw: None)
    monkeypatch.setattr(journey, 'installation', lambda *a: (tmp_path / 'v1', tmp_path / 'data', {'release': 1}))
    def fail_verify(*a, **kw):
        raise RuntimeError('fresh verify failed')
    monkeypatch.setattr(journey, 'verify', fail_verify)
    monkeypatch.setattr(jumps, 'task_after_update', lambda *a: calls.append('task'))
    report = keepgoing.execute(args, tmp_path / 'program', tmp_path / 'data', evidence, SimpleNamespace(pid=1), 'birth')
    rows = {row['stage']: row for row in report['stages']}
    assert rows['release1-verify-install']['first_error'] == 'RuntimeError: fresh verify failed'
    assert rows['release1-task']['result'] == 'pass' and 'task' in calls
    assert rows['one-runtime']['result'] == 'pass' and rows['sentinel']['result'] == 'pass'
    assert report['qualification']['eligible'] is False and report['status'] == 'fail'


def test_fresh_install_failure_blocks_dependent_stages(tmp_path, monkeypatch):
    args, evidence, calls = setup_diagnostic(tmp_path, monkeypatch, 'update')
    args.leg = 'fresh-install'
    report = keepgoing.execute(args, tmp_path / 'program', tmp_path / 'data', evidence, SimpleNamespace(pid=1), 'birth')
    rows = {row['stage']: row for row in report['stages']}
    assert rows['release1-install']['result'] == 'fail'
    assert rows['release1-start']['result'] == 'not-run'
    assert rows['release1-task']['result'] == 'not-run'
    assert rows['sentinel']['result'] == 'pass'


def test_prestop_runs_before_update_and_retains_nonproof(tmp_path, monkeypatch):
    args, evidence, calls = setup_diagnostic(tmp_path, monkeypatch, 'verify')
    args.diagnostic_prestop_r0 = True
    monkeypatch.setattr(keepgoing, 'prestop_r0', lambda *a: calls.append('prestop') or {'owner_lock_free': True})
    report = keepgoing.execute(args, tmp_path / 'program', tmp_path / 'data', evidence, SimpleNamespace(pid=1), 'birth')
    rows = {row['stage']: row for row in report['stages']}
    assert calls.index('prestop') < calls.index('update')
    assert rows['release0-prestop']['result'] == 'pass'
    assert all(rows[name]['result'] == 'pass' for name in ('release1-release', 'release1-service', 'release1-health', 'semantic-preservation', 'release1-task', 'one-runtime', 'sentinel'))
    assert report['prestop_r0']['owner_lock_free'] and report['status'] == 'fail'
    assert report['proof_mode'] == 'keep-going-diagnostic' and not report['qualification']['eligible']


def test_failed_prestop_blocks_update_without_forcing(tmp_path, monkeypatch):
    args, evidence, calls = setup_diagnostic(tmp_path, monkeypatch, 'none')
    args.diagnostic_prestop_r0 = True
    def refuse(*a):
        raise RuntimeError('owner lock still held')
    monkeypatch.setattr(keepgoing, 'prestop_r0', refuse)
    report = keepgoing.execute(args, tmp_path / 'program', tmp_path / 'data', evidence, SimpleNamespace(pid=1), 'birth')
    rows = {row['stage']: row for row in report['stages']}
    assert rows['release0-prestop']['result'] == 'fail'
    assert rows['release1-update']['result'] == 'not-run' and 'update' not in calls
    assert not rows['release0-prestop']['forced_continuation_used']


@pytest.mark.parametrize('manager,commands', [
    ('systemd-user', [['systemctl', '--user', 'stop', 'astrid.service'], ['systemctl', '--user', 'disable', 'astrid.service']]),
    ('launchd', [['launchctl', 'bootout', 'gui/123/haus.astrid.runtime']]),
    ('task-scheduler', []),
])
def test_prestop_uses_only_selected_native_manager_and_probes_lock(tmp_path, monkeypatch, manager, commands):
    health = {'pid': 42, 'runtime_instance_id': 'old'}
    calls = []
    monkeypatch.setattr(journey, 'assert_direct_service_ownership', lambda *a: {'manager': manager})
    monkeypatch.setattr(journey, 'assert_one_runtime', lambda *a: None)
    monkeypatch.setattr(journey, 'healthy', lambda *a: health)
    births = iter(['old-birth', None])
    monkeypatch.setattr(journey, 'process_birth', lambda *a: next(births))
    monkeypatch.setattr(journey, 'workspace_root', lambda *a: tmp_path)
    monkeypatch.setattr(journey, 'runtime_processes', lambda *a: [])
    monkeypatch.setattr(keepgoing, 'owner_lock_free', lambda *a: calls.append('probe') or True)
    monkeypatch.setattr(keepgoing.os, 'getuid', lambda: 123, raising=False)
    monkeypatch.setattr(journey, 'run', lambda cmd, *a, **kw: calls.append(cmd))
    monkeypatch.setattr(journey.windows, 'powershell', lambda cmd, *a, **kw: calls.append(cmd))
    result = keepgoing.prestop_r0(SimpleNamespace(candidate_runtime_checkout=tmp_path), tmp_path, tmp_path, tmp_path / 'ev', health, 42, 'old-birth')
    assert result['owner_lock_free'] and calls[-1] == 'probe'
    if manager == 'task-scheduler':
        assert 'schtasks.exe /End' in calls[0] and 'Disable-ScheduledTask' in calls[0]
    else:
        assert calls[:-1] == commands


def test_owner_lock_probe_respects_real_lock_and_does_not_change_bytes(tmp_path):
    import subprocess
    import sys
    import os
    runtime = os.environ.get('JOURNEY_TEST_RUNTIME_CHECKOUT')
    if not runtime or sys.platform == 'win32':
        pytest.skip('set JOURNEY_TEST_RUNTIME_CHECKOUT to run POSIX native lock contention')
    from pathlib import Path
    runtime = Path(runtime)
    lock = tmp_path / 'owner.lock'
    lock.write_bytes(b'unchanged')
    child = subprocess.Popen([sys.executable, '-c', 'import fcntl,sys; f=open(sys.argv[1]); fcntl.flock(f,fcntl.LOCK_EX); print("locked",flush=True); sys.stdin.read()', str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        assert not keepgoing.owner_lock_free(tmp_path, runtime)
    finally:
        child.stdin.close()
        child.wait(timeout=10)
    assert keepgoing.owner_lock_free(tmp_path, runtime)
    assert lock.read_bytes() == b'unchanged'
    lock.unlink()
    with pytest.raises(RuntimeError, match='missing or unsafe'):
        keepgoing.owner_lock_free(tmp_path, runtime)


def test_prestop_waits_for_lock_and_refuses_held_owner(tmp_path, monkeypatch):
    health = {'pid': 42, 'runtime_instance_id': 'old'}
    monkeypatch.setattr(journey, 'assert_direct_service_ownership', lambda *a: {'manager': 'systemd-user'})
    monkeypatch.setattr(journey, 'assert_one_runtime', lambda *a: None)
    monkeypatch.setattr(journey, 'healthy', lambda *a: health)
    births = iter(['old-birth', None])
    monkeypatch.setattr(journey, 'process_birth', lambda *a: next(births))
    monkeypatch.setattr(journey, 'workspace_root', lambda *a: tmp_path)
    monkeypatch.setattr(journey, 'runtime_processes', lambda *a: [])
    monkeypatch.setattr(journey, 'run', lambda *a, **kw: None)
    monkeypatch.setattr(keepgoing, 'owner_lock_free', lambda *a: False)
    ticks = iter([0, 121])
    monkeypatch.setattr(keepgoing.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(RuntimeError, match='realm lock did not stop'):
        keepgoing.prestop_r0(SimpleNamespace(candidate_runtime_checkout=tmp_path), tmp_path, tmp_path, tmp_path / 'ev', health, 42, 'old-birth')


def test_diagnostic_mode_cannot_be_accepted_as_proof_even_if_forged():
    contract = {'job_id': 'direct-linux', 'mode': 'direct', 'runner': 'ubuntu-24.04', 'shell': 'sh'}
    row = {'proof_mode': 'keep-going-diagnostic', 'receipt_kind': 'release1-installed-lifecycle', 'status': 'pass'}
    candidate = {key: 'a' * 40 for key in ('app', 'agent', 'runtime', 'pinokio')}
    errors = keepgoing.gate._release1_row_errors(candidate, contract, row)
    assert any('diagnostic proof mode is not eligible' in error for error in errors)


@pytest.mark.parametrize('mode,leg', [('direct', 'fresh-install'), ('pinokio-backend', 'release0-to-release1')])
def test_prestop_cli_rejects_other_modes(tmp_path, mode, leg):
    with pytest.raises(SystemExit) as exc:
        journey.main(['--leg', leg, '--mode', mode, '--app-checkout', str(tmp_path),
                      '--run-root', str(tmp_path / 'run'), '--root', str(tmp_path / 'run/program'),
                      '--data-root', str(tmp_path / 'run/data'), '--diagnostic-prestop-r0'])
    assert exc.value.code == 2
    assert not (tmp_path / 'run').exists()


def test_prestop_seven_checks_continue_after_service_check_failure(tmp_path, monkeypatch):
    args, evidence, calls = setup_diagnostic(tmp_path, monkeypatch, 'none')
    args.diagnostic_prestop_r0 = True
    monkeypatch.setattr(keepgoing, 'prestop_r0', lambda *a: {'owner_lock_free': True})
    seen = []
    def service(*a):
        seen.append('service')
        if len(seen) == 2:
            raise RuntimeError('service assertion failed')
    monkeypatch.setattr(journey, 'assert_direct_service_ownership', service)
    report = keepgoing.execute(args, tmp_path / 'program', tmp_path / 'data', evidence, SimpleNamespace(pid=1), 'birth')
    rows = {row['stage']: row for row in report['stages']}
    assert rows['release1-service']['result'] == 'fail'
    assert all(rows[name]['result'] == 'pass' for name in ('release1-release', 'release1-health', 'semantic-preservation', 'release1-task', 'one-runtime', 'sentinel'))


def test_prestop_release_failure_does_not_suppress_other_checks(tmp_path, monkeypatch):
    args, evidence, calls = setup_diagnostic(tmp_path, monkeypatch, 'none')
    args.diagnostic_prestop_r0 = True
    monkeypatch.setattr(keepgoing, 'prestop_r0', lambda *a: {'owner_lock_free': True})
    monkeypatch.setattr(jumps, 'selected_candidate', lambda *a: (_ for _ in ()).throw(RuntimeError('wrong release tuple')))
    report = keepgoing.execute(args, tmp_path / 'program', tmp_path / 'data', evidence, SimpleNamespace(pid=1), 'birth')
    rows = {row['stage']: row for row in report['stages']}
    assert rows['release1-release']['result'] == 'fail'
    assert all(rows[name]['result'] == 'pass' for name in ('release1-service', 'release1-health', 'semantic-preservation', 'release1-task', 'one-runtime', 'sentinel'))
