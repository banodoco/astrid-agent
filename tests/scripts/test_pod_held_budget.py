"""Measured held recovery records; all paid/provider/model boundaries are CPU fixtures."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

import pytest
import yaml

from tests.scripts.test_pod_jobs import (
    _held_joblist_manifest, failure_diagnostics, held_control, ps, run_jobs,
)


_STAGE = r'''
import hashlib,json,os,pathlib,runpy,subprocess,sys,tarfile
def unique(pairs):
    value = {}
    for key,item in pairs:
        if key in value: raise ValueError("duplicate source key")
        value[key] = item
    return value
root = pathlib.Path(sys.argv[1])
expected = json.loads(pathlib.Path('expected-source.json').read_bytes(), object_pairs_hook=unique)
selected = json.loads(pathlib.Path('selection.json').read_bytes(), object_pairs_hook=unique)
assert set(expected) == {'heads','runtime_lock_sha256'}
assert set(expected['heads']) == {'astrid-agent','astrid-runtime','runpod-python','runpod-lifecycle','VibeComfy'}
for key in list(os.environ):
    if key.upper().startswith('GIT_'): del os.environ[key]
os.environ.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull)
bundle = pathlib.Path('candidate.tgz')
assert hashlib.sha256(bundle.read_bytes()).hexdigest() == sys.argv[2]
assert root.resolve() == root and not any(p.is_symlink() for p in (root,*root.parents))
root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
assert root.parent.is_dir() and not any(p.is_symlink() for p in (root,*root.parents))
root.mkdir(mode=0o700, parents=False, exist_ok=False)
with tarfile.open(bundle, 'r:gz') as archive:
    archive.extractall(root, filter='data')
assert json.loads((root/'.pod-source.json').read_bytes()) == expected
for name, head in expected['heads'].items():
    checkout = root/name
    assert subprocess.check_output(['git','-C',str(checkout),'rev-parse','HEAD']).decode().strip() == head
    assert not subprocess.check_output(['git','-C',str(checkout),'status','--porcelain','--untracked-files=all'])
lock = root/'astrid-agent/runtime.lock.json'
assert hashlib.sha256(lock.read_bytes()).hexdigest() == expected['runtime_lock_sha256']
assert json.loads(lock.read_bytes())['source_commit'] == expected['heads']['astrid-runtime']
selection = dict(agent_root=str(root/'astrid-agent'),runtime_root=str(root/'astrid-runtime'),
                agent_head=expected['heads']['astrid-agent'],runtime_head=expected['heads']['astrid-runtime'],
                runtime_lock_sha256=expected['runtime_lock_sha256'])
assert selected == selection
_,identity = runpy.run_path('pod_action_source.py')['verify_selection'](selected)
assert identity.get('managed_by') == 'source' and identity.get('dirty') is False
assert identity.get('agent_head') == selected['agent_head']
assert identity.get('runtime_head') == selected['runtime_head']
pathlib.Path('attestation.json').write_text(json.dumps(dict(schema='astrid.pod-candidate-stage.v1',
    source=expected,selection=selection,identity=identity,bundle_sha256=sys.argv[2],
    bundle_bytes=bundle.stat().st_size),sort_keys=True)+'\n')
'''


def _evidence(name, value):
    directory = os.environ.get('HELD_BUDGET_EVIDENCE_DIR')
    if directory:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        (root / (name + '.json')).write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    print(json.dumps({'held_budget_case': name, **value}, sort_keys=True))


def _staging(tmp_path, session, pod):
    local = tmp_path / 'stage-input'
    local.mkdir()
    archive = local / 'candidate.tgz'
    _size, digest = ps.build_bundle(pod.agent_root, pod.sources, archive)
    source = json.loads((session.workdir / 'source-identity.json').read_bytes())
    (local / 'expected-source.json').write_text(json.dumps(source))
    (local / 'stage.py').write_text(_STAGE)
    candidate = pod.root / 'candidate-fresh-diagnostic'
    selection = {'agent_root': str(candidate / 'astrid-agent'), 'runtime_root': str(candidate / 'astrid-runtime'),
                 'agent_head': source['heads']['astrid-agent'], 'runtime_head': source['heads']['astrid-runtime'],
                 'runtime_lock_sha256': source['runtime_lock_sha256']}
    (local / 'selection.json').write_text(json.dumps(selection))
    # Tiny trees have no importable Runtime. Canonical verifier is a CPU boundary;
    # actual source/lock/import validation has separate focused source tests.
    (local / 'pod_action_source.py').write_text(
        "def verify_selection(value):\n"
        "    return None, dict(managed_by='source',dirty=False,agent_head=value['agent_head'],runtime_head=value['runtime_head'])\n")
    row = {'id': 'stage279', 'type': 'cmd', 'timeout': 40,
           'command': [sys.executable, 'stage.py', str(candidate), digest],
           'input-files': {'candidate.tgz': str(archive), 'stage.py': str(local / 'stage.py'),
                           'expected-source.json': str(local / 'expected-source.json'),
                           'selection.json': str(local / 'selection.json'),
                           'pod_action_source.py': str(local / 'pod_action_source.py')},
           'expected-outputs': ['attestation.json']}
    manifest = local / 'stage.yaml'
    manifest.write_text(yaml.safe_dump({'version': 1, 'jobs': [row]}))
    return manifest, row, candidate, source


def _instrument(control, monkeypatch, labels, progress):
    reserve = control._reserve_held_job
    def counted(label):
        labels.append(label)
        return reserve(label)
    monkeypatch.setattr(control, '_reserve_held_job', counted)
    factory = control.transport_factory
    def observed_factory(*args):
        transport = factory(*args)
        put = getattr(transport, '_budget_original_put', transport.put)
        transport._budget_original_put = put
        def observed_put(local, remote, **kwargs):
            if remote == ps.PROGRESS_REMOTE:
                progress.append(hashlib.sha256(Path(local).read_bytes()).hexdigest())
            return put(local, remote, **kwargs)
        transport.put = observed_put
        return transport
    monkeypatch.setattr(control, 'transport_factory', observed_factory)


def test_staged_candidate_plus_installer_h3_measured_budget(tmp_path, monkeypatch):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        initial = json.loads((session.workdir / 'held-session.json').read_bytes())
        before = pod.record_count
        labels, progress = [], []
        staging, _row, candidate, source = _staging(tmp_path, session, pod)
        stage = held_control(session, pod, cli, transport_factory, jobs=staging)
        _instrument(stage, monkeypatch, labels, progress)
        assert stage.run_held_jobs() == 0, failure_diagnostics(stage)
        stage_labels = list(labels)
        assert stage_labels == ['storage mount verification', 'Runtime readiness', 'job:stage279', 'remote sha256']
        assert stage.budget.count - initial.get('charged_jobs', initial['jobs']) == pod.record_count - before == 4
        attestation = json.loads((session.workdir / 'packets/stage279/outputs/attestation.json').read_bytes())
        assert attestation['source'] == source
        selected = attestation['selection']
        assert selected['agent_root'] == str(candidate / 'astrid-agent')
        assert selected['runtime_root'] == str(candidate / 'astrid-runtime')
        selection = tmp_path / 'selected.json'
        selection.write_text(json.dumps(selected))
        manifest = _held_joblist_manifest(tmp_path / 'recovery-list')
        document = yaml.safe_load(manifest.read_text())
        document['jobs'] = [{'id': 'recover279', 'type': 'h3-install', 'timeout': 40}, document['jobs'][0]]
        manifest.write_text(yaml.safe_dump(document))
        ready = pod.root / 'selected-ready'
        invocations = []
        evidence = {'schema': 'astrid.pod-action-source.v1', **{k: selected[k] for k in
                    ('agent_head', 'runtime_head', 'runtime_lock_sha256')}, 'identity': {
                    'release': 0, 'managed_by': 'source', 'dirty': False, 'dirty_hash': 'd' * 64,
                    **{k: selected[k] for k in ('agent_head', 'runtime_head')}}}
        def selected_cli(value, evidence_path, argv):
            assert value == selected and attestation['source'] == source
            assert (candidate / '.pod-source.json').is_file()
            invocations.append(argv)
            script = 'import json,pathlib,subprocess,sys\n'
            script += f'pathlib.Path({evidence_path!r}).write_text({json.dumps(evidence)!r})\n'
            if argv[:2] == ['engines', 'install']:
                result = {'schema': 'astrid.pod-h3-install.v1', 'engine': ps.ENGINE, 'root': ps.ENGINE_ROOT,
                          'status': 'ready', 'receipt_sha256': 'e' * 64, 'receipt_id': 'synthetic-canonical-receipt'}
                script += f'pathlib.Path({str(ready)!r}).touch()\nprint({json.dumps(result)!r})\n'
            else:
                script += f'assert pathlib.Path({str(ready)!r}).is_file()\n'
                script += f'sys.exit(subprocess.call({["astrid", *argv]!r}))\n'
            return shlex.join([sys.executable, '-c', script])
        monkeypatch.setattr(ps, 'source_invocation', selected_cli)
        parser = ps.build_parser
        def with_selection():
            result = parser()
            result.set_defaults(h3_source_selection=str(selection))
            return result
        monkeypatch.setattr(ps, 'build_parser', with_selection)
        recovery_before = pod.record_count
        recovery = held_control(session, pod, cli, transport_factory, jobs=manifest)
        _instrument(recovery, monkeypatch, labels, progress)
        assert recovery.run_held_jobs() == 0, failure_diagnostics(recovery)
        recovery_labels = labels[len(stage_labels):]
        assert recovery_labels == ['storage mount verification', 'Runtime readiness', 'job:recover279',
                                   'h3:clip:s0', 'job:clip', 'remote sha256']
        assert pod.record_count - recovery_before == 6
        held = json.loads((session.workdir / 'held-session.json').read_bytes())
        assert held['charged_jobs'] - initial.get('charged_jobs', initial['jobs']) == pod.record_count - before == len(labels) == 10
        assert len(progress) == 11 and 'progress' not in labels
        assert held['status'] == 'held'
        for key in ('source', 'runtime_identity', 'pod_id', 'session', 'expires_at'):
            assert held[key] == initial[key]
        assert [args[:2] for args in invocations] == [['engines', 'install'], ['action', 'run'], ['action', 'run']]
        assert 86 + len(labels) == 96 and ps.JOB_LIMIT - 1 - 96 == 3
        _evidence('success', {'staging_labels': stage_labels, 'recovery_labels': recovery_labels,
                            'actual_record_delta': pod.record_count - before, 'held_charge_delta': held['charged_jobs'] - initial.get('charged_jobs', initial['jobs']),
                            'progress_puts': len(progress), 'charged_baseline_projection': 86,
                            'charged_success_projection': 96, 'remaining_admissible_records': 3,
                            'source_boundary': 'tiny committed synthetic source trees; selected CLI/readiness and GPU/model are fixtures'})
    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure='command', after_run=after_run)


@pytest.mark.parametrize('uncertain', [False, True], ids=['terminal-stage-failure', 'lost-stage-response'])
def test_staging_failure_and_exact_reconciliation_measured_allowance(tmp_path, monkeypatch, uncertain):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        staging, row, _candidate, _source = _staging(tmp_path, session, pod)
        # A bounded failure before publishing candidate attestation. The uncertain
        # case starts the actual finite job and loses only its launch response.
        row['command'] = [sys.executable, '-c', 'raise SystemExit(9)']
        staging.write_text(yaml.safe_dump({'version': 1, 'jobs': [row]}))
        before = pod.record_count
        held_before = json.loads((session.workdir / 'held-session.json').read_bytes())
        labels, progress, identities, originals = [], [], [], []
        control = held_control(session, pod, cli, transport_factory, jobs=staging)
        _instrument(control, monkeypatch, labels, progress)
        if uncertain:
            factory = control.transport_factory
            def failing_factory(*args):
                transport = factory(*args)
                start, poll = transport.detached_start, transport.detached_poll
                originals.append((transport, start, poll))
                def lost(command, **kwargs):
                    handle = start(command, **kwargs)
                    if 'raise SystemExit(9)' in command:
                        identities.append(handle['id'])
                        raise OSError('synthetic lost stage response')
                    return handle
                def unavailable(job_id, **kwargs):
                    if job_id in identities:
                        raise OSError('synthetic terminal proof unavailable')
                    return poll(job_id, **kwargs)
                transport.detached_start, transport.detached_poll = lost, unavailable
                return transport
            monkeypatch.setattr(control, 'transport_factory', failing_factory)
        assert control.run_held_jobs() == 1
        held = json.loads((session.workdir / 'held-session.json').read_bytes())
        assert labels == ['storage mount verification', 'Runtime readiness', 'job:stage279']
        assert held['charged_jobs'] - held_before.get('charged_jobs', held_before['jobs']) == pod.record_count - before == 3
        assert held['status'] == ('in_use' if uncertain else 'held')
        if uncertain:
            for transport, start, poll in originals:
                transport.detached_start, transport.detached_poll = start, poll
            assert held['operation']['id'] == identities[0] and held['operation']['state'] == 'starting'
            reconcile = held_control(session, pod, cli, transport_factory, jobs=staging)
            reconcile_labels, reconcile_progress = [], []
            _instrument(reconcile, monkeypatch, reconcile_labels, reconcile_progress)
            assert reconcile.run_held_jobs() == 1
            recovered = json.loads((session.workdir / 'held-session.json').read_bytes())
            assert recovered['status'] == 'held' and recovered['operation']['id'] == identities[0]
            assert recovered['operation']['state'] == 'exited' and recovered['operation']['exit_code'] == 9
            assert recovered['charged_jobs'] == held['charged_jobs'] and not reconcile_labels
            assert pod.record_count - before == 3  # exact poll/GET/PUT proof; no replay
        _evidence('uncertain' if uncertain else 'terminal-failure', {
            'labels': labels, 'actual_record_delta': pod.record_count - before, 'charge_delta': 3,
            'reconciliation_records': 0 if uncertain else None,
            'charged_success_plus_one_early_failed_attempt_projection': 86 + 10 + 3,
            'remaining_after_that_projection': 0,
            'late_four_record_failure_then_full_ten_record_path_projection': 100,
            'late_failure_shortfall_to_strict_cap': 1})
    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure='command', after_run=after_run)


def test_late_staging_failure_measures_four_record_shortfall(tmp_path, monkeypatch):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        staging, _row, candidate, _source = _staging(tmp_path, session, pod)
        pod.corrupt_download = 'attestation.json'
        labels, progress = [], []
        before = pod.record_count
        held_before = json.loads((session.workdir / 'held-session.json').read_bytes())
        control = held_control(session, pod, cli, transport_factory, jobs=staging)
        _instrument(control, monkeypatch, labels, progress)
        assert control.run_held_jobs() == 1
        held = json.loads((session.workdir / 'held-session.json').read_bytes())
        assert candidate.is_dir()  # Root creation is known; do not replay staging into it.
        assert labels == ['storage mount verification', 'Runtime readiness', 'job:stage279', 'remote sha256']
        assert held['charged_jobs'] - held_before.get('charged_jobs', held_before['jobs']) == pod.record_count - before == 4
        assert held['status'] == 'held' and held['operation']['state'] == 'exited'
        # This is an independent accounting projection, not a held-ledger edit.
        budget = ps.JobBudget(count=86 + 4)
        for number in range(9):
            budget.charge(f'projected recovery record {number + 1}')
        with pytest.raises(ps.BudgetExceeded, match='would reach the limit of 100'):
            budget.charge('projected recovery record 10')
        assert budget.count == 99
        _evidence('late-failure', {'labels': labels, 'actual_record_delta': pod.record_count - before,
                                 'charge_delta': 4, 'full_path_after_failure_projection': 100,
                                 'shortfall_to_strict_cap': 1, 'candidate_root_exists_no_replay': True})
    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure='command', after_run=after_run)
