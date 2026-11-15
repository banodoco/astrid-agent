"""Remaining warm waits sleep, and stop at their original deadline/fence."""
from types import SimpleNamespace

import pytest


def test_machine_backoff_cap_and_cancellation_budget(tmp_path):
    from astrid.core.execution.machine_run import MachineJob, MachineRunCancelled, run_on_machine
    now, sleeps, releases = [0.0], [], []
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    machine = SimpleNamespace(claim=lambda job: 'lease', start=lambda *a: 'job',
        poll=lambda *a: {'state': 'running'}, cancel=lambda *a: None,
        release=lambda *a: releases.append(a))
    job = MachineJob(entry='cpu', mode=None, policy={}, inputs={}, invocation_id='i',
                    task_id='t', attempt_id='a', scratch=tmp_path, progress_path=tmp_path / 'p')
    with pytest.raises(MachineRunCancelled):
        run_on_machine(machine, job, destination=tmp_path / 'out',
                       cancelled=lambda: now[0] >= 0.6, sleep=sleep)
    assert sleeps == pytest.approx([0.2, 0.2, 0.2])
    assert now[0] == pytest.approx(0.6)
    assert releases == [('lease', False)]


@pytest.mark.parametrize('paid', [False, True])
def test_reset_interval_honours_verification_and_paid_deadlines(monkeypatch, paid):
    from astrid.packs.runpod import machine as rm
    now, sleeps = [0.0], []
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    monkeypatch.delenv('ASTRID_MACHINE_POLL_SECONDS', raising=False)
    monkeypatch.setattr('time.monotonic', lambda: now[0])
    monkeypatch.setattr('time.sleep', sleep)
    ops = SimpleNamespace(job_start=lambda cmd, job_id: {'id': job_id},
                          job_poll=lambda job_id: {'id': job_id, 'state': 'running'})
    driver = SimpleNamespace(_checkpoint=lambda *a: None,
                             _require=lambda: SimpleNamespace(lease_owner=SimpleNamespace(clock=lambda: now[0])))
    lease = SimpleNamespace(claim=SimpleNamespace(ops=ops), run_dir='run', job_dir='job')
    deadline = {'paid_expiry': 0.45} if paid else {'verify_deadline': 0.45}
    with pytest.raises(rm.MachineError, match='original paid limit'):
        rm.RunPodMachine._reset(driver, lease, **deadline)
    assert sleeps == pytest.approx([0.2, 0.2, 0.05])
    assert now[0] == pytest.approx(0.45)


def test_reset_own_deadline_is_not_overslept(monkeypatch):
    from astrid.packs.runpod import machine as rm
    now, sleeps, cancelled = [0.0], [], []
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    monkeypatch.delenv('ASTRID_MACHINE_POLL_SECONDS', raising=False)
    monkeypatch.setattr('time.monotonic', lambda: now[0])
    monkeypatch.setattr('time.sleep', sleep)
    monkeypatch.setattr(rm, 'RESET_SECONDS', 0.45)
    def cancel(job_id):
        cancelled.append(job_id)
        return {'id': job_id, 'state': 'cancelled'}
    ops = SimpleNamespace(job_start=lambda cmd, job_id: {'id': job_id},
        job_poll=lambda job_id: {'id': job_id, 'state': 'running'}, job_cancel=cancel)
    driver = SimpleNamespace(_checkpoint=lambda *a: None)
    lease = SimpleNamespace(claim=SimpleNamespace(ops=ops), run_dir='run', job_dir='job')
    with pytest.raises(rm.MachineError, match='reset did not finish'):
        rm.RunPodMachine._reset(driver, lease)
    assert sleeps == pytest.approx([0.2, 0.2, 0.05])
    assert now[0] == pytest.approx(0.45)
    assert len(cancelled) == 1


def test_discovery_interval_and_original_deadline(monkeypatch, real_acquisition_confirmation):
    from runpod_lifecycle import graphql

    from astrid.packs.runpod import worker_preparation as wp
    from tests.packs.runpod.test_worker_preparation import _contract
    now, sleeps, calls = [0.0], [], []
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    monkeypatch.setattr(wp, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    monkeypatch.setattr(wp, 'provider_call', lambda key, fn: fn())
    def get_pod(*a):
        calls.append(now[0])
        return {'machine': {}}
    monkeypatch.setattr(graphql, 'get_pod', get_pod)
    with pytest.raises(wp.TargetObservationError, match='readiness timeout'):
        wp.confirm_acquired_machine('fake', _contract(), timeout_seconds=0.45)
    assert sleeps == pytest.approx([0.2, 0.2, 0.05])
    assert calls == pytest.approx([0, 0.2, 0.4, 0.45])


def test_host_forwards_the_short_default_to_real_machine_loop(monkeypatch, tmp_path):
    from astrid.core.execution import machine_run
    from astrid.core.execution.generic_host import GenericPackHost
    from tests.core.extensions.test_generation_machine_point import RentedDirMachine
    sleeps = []
    machine = RentedDirMachine()
    machine.describe = lambda lease: {'id': 'cpu-machine'}
    monkeypatch.delenv('ASTRID_MACHINE_POLL_SECONDS', raising=False)
    monkeypatch.setattr(machine_run, 'resolve_machine', lambda kind:
                        SimpleNamespace(for_host=lambda *a, **kw: machine))
    original = machine_run.run_on_machine
    def run(machine, job, **kwargs):
        return original(machine, job, sleep=sleeps.append, **kwargs)
    monkeypatch.setattr(machine_run, 'run_on_machine', run)
    context = SimpleNamespace(get=lambda **kw: None, begin=lambda *a: None, done=lambda *a: None)
    GenericPackHost._run_machine_job_in_scope(SimpleNamespace(), SimpleNamespace(id='vibecomfy.run'),
        {}, {'kind': 'cpu-machine', 'engine': 'comfyui'}, context, {}, root=tmp_path,
        output_root=tmp_path / 'out', cancelled=lambda: False, cancel_signal=None,
        task_id='t', attempt_id='a', fence=1, terminal={})
    assert sleeps == [0.2]
