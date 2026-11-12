"""Jobs use the claim's authority and cannot allocate a replacement machine."""
import json
from dataclasses import replace

import pytest

from astrid.core.execution.machine_run import run_on_machine
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


def test_expiry_between_queued_jobs_never_creates_replacement(machine_case, monkeypatch):
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


def test_three_queued_jobs_share_claim_without_renewal(machine_case, monkeypatch):
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


def test_restart_recovers_claim_from_journal(machine_case, monkeypatch):
    from astrid.core.generation import settings
    from astrid.packs.runpod import adapter, target_selection

    c = machine_case
    claim(c)
    original = c.prefs.as_mapping()
    saved = {**original, "gpu_types": ["changed-saved-gpu"], "image": "changed@sha256:" + "2" * 64}
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: saved)
    monkeypatch.setattr(target_selection, "canonical_claim_path", lambda *args, **kwargs: c.owner.path)
    assert adapter._claimed_preferences() == original

    # Older journals did not persist the full preference mapping. Verify that
    # the adapter reconstructs all custody-relevant fields from the claim, then
    # restore the complete record before exercising restart recovery below.
    from astrid.packs.runpod.worker_preparation import write_target_record

    record = c.read()
    legacy = dict(record)
    legacy.pop("claim_preferences", None)
    expected = {
        "provider_account_ref": record["provider_account_ref"],
        "gpu_types": [record["gpu_type"]],
        "image": record["worker_image"],
        "datacenter_id": record.get("network_volume_datacenter_id"),
        "volume": {
            "id": record["network_volume_id"],
            "name": record.get("network_volume_name"),
            "size_gb": record.get("network_volume_size_gb"),
            "mount_path": record.get("volume_mount_path"),
        },
    }
    try:
        write_target_record(c.owner.path, legacy)
        assert adapter._claimed_preferences() == expected
    finally:
        write_target_record(c.owner.path, record)

    c.host.close()
    replacement = c.fresh()
    replacement.recover_job()
    c.machine.adapter.lease_owner = replacement
    forbid_allocation(c, monkeypatch)
    run(c, job(c, preferences=saved))
    assert c.creates == 1 and c.read()["pod_id"] == "pod-1"
    replacement.close()
