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
    assert '| stage | result | forced-continuation-used | first error |' in (evidence / 'keepgoing-report.md').read_text()


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


@pytest.mark.parametrize('exit_code,output', [
    (0, b'{"ok":true,"data":{"slug":"tst-m4-3b-release0","path":"/home/private","token":"secret"}}'),
    (3, b'{"ok":false,"error":{"code":"not_found","message":"secret /home/private"}}'),
    (1, b'not json secret /home/private'),
])
def test_project_observation_is_reduced_and_retained(tmp_path, monkeypatch, exit_code, output):
    called = []
    def run(argv, **kwargs):
        called.append((argv, kwargs))
        return SimpleNamespace(returncode=exit_code, stdout=output)
    monkeypatch.setattr(keepgoing.subprocess, 'run', run)
    task_dir = tmp_path / 'run/evidence/fresh-install/release1-working-task'
    keepgoing.observe_project_create({'python': 'python', 'env': {}}, task_dir, 'tst-m4-3b-release0')
    observation = json.loads((task_dir / 'project-create-observation.json').read_text())
    assert observation['exit_code'] == exit_code and observation['phase'] == 'after-failed-task'
    assert called[0][0][-4:] == ['tst-m4-3b-release0', '--name', 'tst-m4-3b-release0', '--json']
    assert 'secret' not in json.dumps(observation) and '/home/private' not in json.dumps(observation)
    journey.retain_named_evidence(task_dir.parent, 'before-cleanup')
    assert json.loads((tmp_path / 'run/evidence-upload/before-cleanup/project-create-observation.json').read_text()) == observation


def test_fresh_probe_follows_failed_task_without_changing_result(tmp_path, monkeypatch):
    args, evidence, calls = setup_diagnostic(tmp_path, monkeypatch, 'none')
    args.leg = 'fresh-install'
    monkeypatch.setattr(journey, 'installation', lambda *a: (tmp_path / 'v1', tmp_path / 'data', {'release': 1}))
    monkeypatch.setattr(journey, 'wait_ready', lambda *a, **kw: None)
    def fail_task(*a):
        calls.append('task-failed')
        raise RuntimeError('Installed SDK did not admit exactly one task')
    monkeypatch.setattr(jumps, 'task_after_update', fail_task)
    monkeypatch.setattr(keepgoing, 'observe_project_create', lambda *a: calls.append('probe'))
    report = keepgoing.execute(args, tmp_path / 'program', tmp_path / 'data', evidence, SimpleNamespace(pid=1), 'birth')
    row = next(r for r in report['stages'] if r['stage'] == 'release1-task')
    assert row['result'] == 'fail' and row['first_error'] == 'RuntimeError: Installed SDK did not admit exactly one task'
    assert calls.index('task-failed') < calls.index('probe') < calls.index('one')
    assert report['status'] == 'fail' and report['qualification']['eligible'] is False


def test_project_observation_failure_is_best_effort(tmp_path, monkeypatch):
    def fail(*a, **kw):
        raise OSError('secret /home/private')
    monkeypatch.setattr(keepgoing.subprocess, 'run', fail)
    keepgoing.observe_project_create({'python': 'python', 'env': {}}, tmp_path, 'tst-m4-3b-release0')
    assert json.loads((tmp_path / 'project-create-observation.json').read_text()) == {
        'phase': 'after-failed-task', 'project': 'tst-m4-3b-release0', 'status': 'unavailable'}
