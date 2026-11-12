"""Jobs use the claim's authority and cannot allocate a replacement machine."""
import json
from dataclasses import replace

import pytest

from astrid.core.execution.machine_run import run_on_machine
from astrid.core.execution.warm_hold import WarmHolds
from tests.packs.runpod.test_machine import machine_case as machine_case
from tests.packs.runpod.test_generation_preparation import retained as retained
from tests.packs.runpod.test_remote_placement import sources as sources
from tests.packs.runpod.test_target_selection import case as case
from tests.packs.runpod.test_warm_session import lease_selection_case as lease_selection_case, small_lease as small_lease


def claim(c):
    c.host.start_fresh_claim()
    c.host.claim(replace(c.grant, invocation_id="manual-claim", preparation_seconds=0,
                         execution_seconds=0), requested_idle_seconds=c.grant.idle_seconds)


def job(c, n=1, **bounds):
    original = c.job(n)
    policy = {key: original.policy[key] for key in ("kind", "engine", "preferences")}
    return replace(original, policy={**policy, **bounds})


def run(c, request):
    return run_on_machine(c.machine, request, destination=request.scratch / "outputs",
                          cancelled=lambda: False, sleep=lambda _: None)


def restart_bindings(c, monkeypatch):
    """Build a fresh real host adapter over this fixture's fake lease edges."""
    from astrid.packs.runpod import adapter
    from astrid.packs.runpod import worker_owner

    real_owner = worker_owner.RunPodLeaseOwner

    def timer(seconds, callback):
        result = c.clock.timer(seconds, callback)
        result.interval = seconds
        return result

    def owner_factory(*args, **kwargs):
        return real_owner(*args, clock=c.clock, timer=timer, **kwargs)

    monkeypatch.setattr(adapter, "broker_selection", lambda *args, **kwargs: c.owner)
    monkeypatch.setattr(worker_owner, "RunPodLeaseOwner", owner_factory)
    holds = WarmHolds(clock=c.clock, timer=timer)
    bindings = adapter.TargetProviderBindings(pack_roots=(), holds=holds,
                                               credentials=lambda name: "fake-only", environ={})
    return bindings, holds


def forbid_allocation(c, monkeypatch):
    from astrid.packs.runpod import target_selection

    def forbidden(*args, **kwargs):
        pytest.fail("job reached allocation or market observation")

    monkeypatch.setattr(target_selection, "acquire_target", forbidden)
    monkeypatch.setattr(target_selection, "reconcile_target", forbidden)
    monkeypatch.setattr(c.owner, "observe_offer", forbidden)
    monkeypatch.setattr(c.owner, "observe_offers", forbidden)


def test_job_without_consent_runs_on_claim_and_never_allocates(machine_case, monkeypatch):
    c = machine_case
    claim(c)
    paid = c.read()["paid_limits"]
    forbid_allocation(c, monkeypatch)
    request = job(c)
    c.module.prevalidate_task(request.policy, request.inputs, scratch=request.scratch)
    run(c, request)
    assert c.creates == 1 and c.read()["pod_id"] == "pod-1"
    assert c.read()["paid_limits"] == paid


def test_no_machine_refuses_one_line_without_provider_calls(machine_case, monkeypatch):
    c = machine_case
    forbid_allocation(c, monkeypatch)
    with pytest.raises(Exception) as caught:
        run(c, job(c))
    assert str(caught.value) == "no machine: run `astrid machines claim`"
    assert c.creates == c.configs == 0 and not c.owner.path.exists()


def test_second_sequential_job_after_expiry_never_creates_replacement(machine_case, monkeypatch):
    c = machine_case
    claim(c)
    forbid_allocation(c, monkeypatch)
    queued = [job(c, n) for n in (1, 2)]
    run(c, queued[0])
    c.clock.advance(c.read()["lease"]["paid_expires_at"] - c.clock())
    with pytest.raises(Exception, match="no machine: run `astrid machines claim`"):
        run(c, queued[1])
    assert c.creates == 1


@pytest.mark.parametrize("limit", ["time", "budget"])
def test_insufficient_claim_refuses_before_job(machine_case, monkeypatch, limit):
    from astrid.packs.runpod.worker_preparation import write_target_record

    c = machine_case
    claim(c)
    record = c.read()
    if limit == "time":
        record["lease"]["paid_expires_at"] = c.clock() + 30
        record["lease"]["idle_expires_at"] = c.clock() + 30
    else:
        record["paid_limits"]["max_usd"] = 0.001
    write_target_record(c.owner.path, record)
    forbid_allocation(c, monkeypatch)
    with pytest.raises(Exception, match="remaining (paid lifetime|budget)"):
        run(c, job(c, preparation_seconds=30, execution_seconds=30))
    assert c.creates == 1 and not c.pod.job_log


def test_three_sequential_jobs_share_claim_without_renewal(machine_case, monkeypatch):
    c = machine_case
    claim(c)
    paid = c.read()["paid_limits"]
    forbid_allocation(c, monkeypatch)
    queued = [job(c, n) for n in (1, 2, 3)]
    for request in queued:
        run(c, request)
        assert c.read()["pod_id"] == "pod-1" and c.read()["lease"]["state"] == "idle"
        assert c.read()["paid_limits"] == paid
    assert c.creates == 1


def test_jobs_ignore_changed_preferences(machine_case, monkeypatch):
    from astrid.core.generation import settings

    c = machine_case
    claim(c)
    forbid_allocation(c, monkeypatch)
    saved = {**c.prefs.as_mapping(), "gpu_types": ["changed-saved-gpu"]}
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: saved)
    for n in (1, 2):
        request = job(c, n, preferences={**saved, "gpu_types": [f"job-gpu-{n}"]})
        run(c, request)
        assert c.read()["pod_id"] == "pod-1"
    assert c.creates == 1


def test_host_cache_jobs_ignore_changed_preferences(machine_case, monkeypatch):
    from astrid.core.generation import settings
    from astrid.packs.runpod.adapter import TargetProviderBindings

    c = machine_case
    claim(c)
    forbid_allocation(c, monkeypatch)
    bindings = TargetProviderBindings(pack_roots=(), holds=None, credentials=None, environ={})
    bindings.provider = c.machine.adapter
    bindings.signature = json.dumps(["runpod", "comfyui", c.prefs.as_mapping()], sort_keys=True, separators=(",", ":"))
    saved = {**c.prefs.as_mapping(), "gpu_types": ["changed-saved-gpu"]}
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: saved)
    for n in (1, 2):
        request = job(c, n, preferences={**saved, "gpu_types": [f"job-gpu-{n}"]})
        provider = bindings.get(kind="runpod", engine="comfyui", preferences=request.policy["preferences"], job=True)
        assert provider is c.machine.adapter
        run(c, request)
    assert c.creates == 1 and c.read()["pod_id"] == "pod-1"


def test_restart_recovers_claim_from_journal_through_provider_bindings(machine_case, monkeypatch):
    from astrid.core.generation import settings
    from astrid.packs.runpod import target_selection, machine as rm, adapter

    c = machine_case
    claim(c)
    original_record = c.read()
    old_timer = c.host.deadline_timer
    original = c.prefs.as_mapping()
    saved = {**original, "gpu_types": ["changed-saved-gpu"], "image": "changed@sha256:" + "2" * 64}
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: saved)
    monkeypatch.setattr(target_selection, "canonical_claim_path", lambda *args, **kwargs: c.owner.path)
    assert adapter._claimed_preferences() == original

    c.host.close()
    # New host bindings must reconstruct the provider from the journal's
    # recorded claim policy, even when current/job preferences have changed.
    monkeypatch.setattr(target_selection, "canonical_claim_path", lambda *args, **kwargs: c.owner.path)
    bindings, holds = restart_bindings(c, monkeypatch)
    assert bindings.provider is None and bindings.signature is None
    request = job(c, preferences=saved)
    old_epoch = c.read()["lease"]["owner_epoch"]
    provider = bindings.get(kind="runpod", engine="comfyui",
                            preferences=request.policy["preferences"], job=True)
    assert provider is not c.machine.adapter
    assert json.loads(bindings.signature) == ["runpod", "comfyui", original_record["claim_preferences"]]
    assert provider.lease_owner.epoch != old_epoch
    recovered = c.read()
    assert recovered["lease"]["paid_expires_at"] == original_record["lease"]["paid_expires_at"]
    assert recovered["paid_limits"] == original_record["paid_limits"]
    assert provider.lease_owner.deadline_timer.interval == pytest.approx(
        max(0, recovered["lease"]["paid_expires_at"] - c.clock()), abs=1
    )
    idle_hold = next(iter(holds._holds.values()))
    assert idle_hold.timer is not None
    assert idle_hold.expires_at == pytest.approx(recovered["lease"]["idle_expires_at"], abs=1)
    assert idle_hold.timer.interval == pytest.approx(
        max(0, recovered["lease"]["idle_expires_at"] - c.clock()), abs=1
    )
    # A timer left behind by the prior host carries its epoch and cannot
    # release or mutate the replacement owner's lease.
    old_timer.callback()
    assert c.read()["lease"]["owner_epoch"] == provider.lease_owner.epoch
    assert c.read()["state"] == "claimed"
    from astrid.packs.runpod.worker_owner import RunPodLease
    old_lease = RunPodLease(original_record["operation_id"], old_epoch,
                            original_record["lease"]["generation"],
                            original_record["lease"]["invocation_id"], {})
    with pytest.raises(Exception, match="invocation lease belongs to another holder"):
        provider.lease_owner.settle(old_lease)
    provider.placement = c.adapter.placement
    provider.validate = c.adapter.validate
    provider.observe_mounts = c.adapter.observe_mounts
    c.machine = rm.MACHINE.for_host(provider, begin=lambda lease: None, done=lambda lease: None)
    forbid_allocation(c, monkeypatch)
    run(c, request)
    assert c.creates == 1 and c.read()["pod_id"] == "pod-1"
    bindings.close()


def test_missing_claim_preferences_refuses_and_release_reconciles(machine_case, monkeypatch):
    from astrid.core.generation import settings
    from astrid.packs.runpod import target_selection
    from astrid.packs.runpod.worker_preparation import write_target_record

    c = machine_case
    claim(c)
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: c.prefs.as_mapping())
    record = c.read()
    record.pop("claim_preferences")
    write_target_record(c.owner.path, record)
    monkeypatch.setattr(target_selection, "canonical_claim_path", lambda *args, **kwargs: c.owner.path)
    c.host.close()
    bindings, _ = restart_bindings(c, monkeypatch)
    with pytest.raises(Exception, match=r"has no recorded claim settings: run `astrid machines release pod-1`, then claim again"):
        bindings.get(kind="runpod", engine="comfyui", preferences=c.prefs.as_mapping(), job=True)
    assert c.creates == 1 and c.deletes == []

    # The refusal leaves exact journal custody available to the normal release
    # recovery path, which reconciles and deletes the same pod.
    result = bindings.operate({"kind": "runpod", "engine": "comfyui",
                               "preferences": c.prefs.as_mapping(), "machine": "pod-1"}, "release")
    assert result["state"] == "cleanup_complete"
    assert c.deletes == ["pod-1"]
    assert c.read()["state"] == "cleanup_complete"
    bindings.close()


def test_restart_reconciles_active_journal_without_adopting_job(machine_case, monkeypatch):
    from astrid.core.generation import settings
    from astrid.packs.runpod import target_selection
    from astrid.packs.runpod.worker_preparation import write_target_record

    c = machine_case
    claim(c)
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: c.prefs.as_mapping())
    record = c.read()
    record["lease"]["state"] = "active"
    write_target_record(c.owner.path, record)
    monkeypatch.setattr(target_selection, "canonical_claim_path", lambda *args, **kwargs: c.owner.path)
    c.host.close()
    bindings, _ = restart_bindings(c, monkeypatch)
    provider = bindings.get(kind="runpod", engine="comfyui",
                            preferences=c.prefs.as_mapping(), job=True)
    assert provider.lease_owner.started
    assert c.deletes == ["pod-1"]
    assert c.read()["state"] == "cleanup_complete"
    assert c.read()["pod_id"] == "pod-1"
    assert c.creates == 1
    bindings.close()
