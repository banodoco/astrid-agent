"""Jobs use the claim's authority and cannot allocate a replacement machine."""
import json
from dataclasses import replace
from datetime import datetime, timezone

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


def job(c, n=1):
    original = c.job(n)
    policy = {key: original.policy[key] for key in ("kind", "engine")}
    return replace(original, policy=policy)


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


@pytest.mark.parametrize("source", ["omitted", "request", "configured", "typed"])
def test_sdk_default_length_explicit_hold_shortens_a_longer_claim(machine_case, monkeypatch, source):
    from astrid.core.generation import placement, settings
    from astrid.sdk.generation import GenerationFacade
    from astrid.sdk.results import InvocationResult
    from tests.sdk.test_generation_target import storage_client

    c = machine_case
    c.grant = replace(c.grant, idle_seconds=3600)
    claim(c)
    paid = c.read()["paid_limits"]
    monkeypatch.setattr(placement, "_runpod_leases", lambda kind: [
        {"machine": "pod-1", "id": "pod-1", "live": True}])
    monkeypatch.setattr(settings, "placement_default", lambda *args, **kwargs: (
        {"kind": "runpod", "policy": {"idle_timeout_s": 1800}} if source == "configured" else {}))
    selector = {"kind": "runpod"}
    if source == "request":
        selector["policy"] = {"idle_timeout_s": 1800}
    elif source == "typed":
        selector = placement.Placement(kind="runpod", policy=placement.PlacementPolicy(idle_timeout_s=1800))
    client, _ = storage_client()
    submitted = []

    def invoke(capability, **kwargs):
        submitted.append(json.loads(kwargs["inputs"]["placement_policy"]))
        return InvocationResult(capability, "executor", "command", True,
                                kernel_task_id="hold-task", raw_result={"state": "queued"})

    client.invoke_result = invoke
    result = GenerationFacade().image(entry="cpu-image", placement=selector, client=client,
                                      project="demo", wait=False)
    assert result.ok, result.to_dict()
    request = replace(job(c), policy=submitted[0])
    run(c, request)
    expected = 3600 if source == "omitted" else 1800
    assert c.read()["lease"]["idle_seconds"] == expected
    assert c.read()["lease"]["idle_expires_at"] == min(c.clock() + expected,
                                                      c.read()["lease"]["paid_expires_at"])
    assert c.read()["paid_limits"] == paid
    assert c.creates == 1


@pytest.mark.parametrize("field", [
    "preferences", "offer_digest", "max_usd", "deadline", "preparation_seconds",
    "execution_seconds", "idle_seconds", "placement_idle_seconds", "policy_digest",
    "max_hourly_usd", "capacity_timeout_seconds", "wait_for_capacity",
])
def test_job_policy_rejects_retired_fields(field):
    from astrid.packs.runpod.adapter import prevalidate_task
    from astrid.packs.runpod.target_selection import PaidSelectionError

    policy = {"kind": "runpod", "engine": "comfyui", field: None}
    with pytest.raises(PaidSelectionError, match="target policy requires a machine placement"):
        prevalidate_task(policy, {}, scratch=None)


def test_job_policy_allows_user_env_consent_but_rejects_paid_consent():
    from astrid.packs.runpod.adapter import prevalidate_task
    from astrid.packs.runpod.target_selection import PaidSelectionError

    with pytest.raises(PaidSelectionError, match="retained generation requires materialized canonical workflow"):
        prevalidate_task({"kind": "runpod", "engine": "comfyui", "consent": ["user_env"]},
                         {}, scratch=None)
    with pytest.raises(PaidSelectionError, match="invalid job consent scope"):
        prevalidate_task({"kind": "runpod", "engine": "comfyui", "consent": ["paid"]},
                         {}, scratch=None)


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
        record["paid_limits"]["expires_at"] = datetime.fromtimestamp(
            c.clock() - 1, timezone.utc).isoformat()
    else:
        record["paid_limits"]["max_usd"] = 0
    write_target_record(c.owner.path, record)
    forbid_allocation(c, monkeypatch)
    reason = "remaining paid lifetime" if limit == "time" else "remaining budget"
    with pytest.raises(Exception, match=reason):
        run(c, job(c))
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
        request = job(c, n)
        run(c, request)
        assert c.read()["pod_id"] == "pod-1"
    assert c.creates == 1


@pytest.mark.parametrize("saved_cap,saved_idle", [(0.5, 10), (20, 3600)])
@pytest.mark.parametrize("exhausted", ["budget", "expiry"])
@pytest.mark.usefixtures("machine_case")
def test_jobs_obey_recorded_bounds_and_report_stricter_saved_policy(request, monkeypatch,
                                                                 saved_cap, saved_idle, exhausted):
    from astrid.packs.runpod import adapter, target_selection
    from astrid.packs.runpod.worker_preparation import write_target_record

    c = request.getfixturevalue("machine_case")
    c.grant = replace(c.grant, idle_seconds=3600)
    claim(c)
    recorded = c.read()
    paid = dict(recorded["paid_limits"])
    expiry = recorded["lease"]["paid_expires_at"]
    # Run near the paid boundary so a renewed idle hold must be clamped.
    c.clock.advance(expiry - c.clock() - 60)
    target_selection.save_runpod_preferences({**c.prefs.as_mapping(), "budget_cap_usd": saved_cap,
                                              "idle_timeout_seconds": saved_idle})
    forbid_allocation(c, monkeypatch)
    bindings = adapter.TargetProviderBindings(pack_roots=(), holds=None, credentials=None, environ={})
    bindings.provider = c.machine.adapter
    bindings.signature = json.dumps(["runpod", "comfyui", recorded["claim_preferences"]])
    assert bindings.get(kind="runpod", engine="comfyui", job=True) is c.machine.adapter
    request = job(c)
    descriptions = []
    receipt = run_on_machine(c.machine, request, destination=request.scratch / "outputs",
                             cancelled=lambda: False, sleep=lambda _: None,
                             on_claim=lambda lease: descriptions.append(c.machine.describe(lease)))
    status = bindings.operate({"kind": "runpod", "engine": "comfyui",
                               "preferences": None}, "status")
    if saved_cap < paid["max_usd"]:
        expected = {"budget_cap_usd": {"recorded": paid["max_usd"], "saved": saved_cap},
                    "idle_timeout_seconds": {"recorded": paid["idle_seconds"], "saved": saved_idle}}
        assert receipt["policy_notice"]["limits"] == expected
        for field, values in expected.items():
            message = receipt["policy_notice"]["message"]
            assert field in message and str(values["recorded"]) in message and str(values["saved"]) in message
        assert status["policy_notice"] == receipt["policy_notice"]
        assert descriptions[0]["policy_notice"] == receipt["policy_notice"]
    else:
        assert "policy_notice" not in receipt and "policy_notice" not in status
    assert c.read()["paid_limits"] == paid
    assert c.read()["lease"]["paid_expires_at"] == expiry
    assert c.read()["lease"]["idle_expires_at"] <= expiry
    if exhausted == "budget":
        record = c.read()
        record["paid_limits"]["max_usd"] = 0
        write_target_record(c.owner.path, record)
    else:
        c.clock.advance(expiry - c.clock() + 1)
    with pytest.raises(Exception, match="remaining budget|no machine"):
        run(c, job(c, 2))
    assert c.creates == 1


def test_sdk_admission_and_job_need_only_saved_account(machine_case, monkeypatch):
    """A live claim needs no saved budget, GPU or datacenter choices for a job."""
    import time

    from astrid.core.generation import placement, settings
    from astrid.packs.runpod import target_selection
    from astrid.sdk.generation import _require_claimed_machine

    c = machine_case
    claim(c)
    paid = c.read()["paid_limits"]
    forbid_allocation(c, monkeypatch)
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: {
        "provider_account_ref": c.prefs.provider_account_ref})
    monkeypatch.setattr(target_selection, "canonical_claim_path", lambda *args, **kwargs: c.owner.path)

    requested = placement.Placement(kind="runpod", machine="pod-1")
    assert placement.validate_placement(requested, machine_kinds=("runpod",)) is requested
    with monkeypatch.context() as sdk_clock:
        sdk_clock.setattr(time, "time", c.clock)
        _require_claimed_machine(requested.kind, requested.machine)
    run(c, job(c))
    assert c.creates == 1 and c.read()["pod_id"] == "pod-1"
    assert c.read()["paid_limits"] == paid


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
        request = job(c, n)
        provider = bindings.get(kind="runpod", engine="comfyui", preferences=None, job=True)
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
    request = job(c)
    old_epoch = c.read()["lease"]["owner_epoch"]
    provider = bindings.get(kind="runpod", engine="comfyui", preferences=None, job=True)
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


@pytest.mark.parametrize(
    ("state", "lease_state", "missing_pod"),
    [
        ("claimed", "active", False),
        ("claimed", "preparing", False),
        ("release_pending", "releasing", False),
        ("cleanup_pending", "releasing", False),
        ("allocated_unverified", "preparing", False),
        ("allocation_pending", "preparing", False),
        ("allocation_unknown", "preparing", False),
        ("allocation_pending", "preparing", True),
        ("allocation_unknown", "preparing", True),
        ("allocation_ready", "preparing", True),
    ],
)
def test_restart_reconciles_interrupted_journal_without_adopting_job(
        machine_case, monkeypatch, state, lease_state, missing_pod):
    from astrid.core.generation import settings
    from astrid.packs.runpod import target_selection
    from astrid.packs.runpod.worker_preparation import write_target_record

    c = machine_case
    claim(c)
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: c.prefs.as_mapping())
    record = c.read()
    record["state"] = state
    record["lease"]["state"] = lease_state
    if missing_pod:
        # The original provider pod remains discoverable by exact reconciliation.
        record["pod_id"] = None
    write_target_record(c.owner.path, record)
    monkeypatch.setattr(target_selection, "canonical_claim_path", lambda *args, **kwargs: c.owner.path)
    c.host.close()
    bindings, _ = restart_bindings(c, monkeypatch)
    forbid_allocation(c, monkeypatch)
    provider = bindings.get(kind="runpod", engine="comfyui",
                            preferences=c.prefs.as_mapping(), job=True)
    assert provider.lease_owner.started
    assert c.deletes == ["pod-1"]
    assert c.read()["state"] == "cleanup_complete"
    assert c.read()["pod_id"] == "pod-1"
    assert c.creates == 1
    request = job(c)
    with pytest.raises(Exception) as caught:
        provider.lease_owner.job_limits(request.invocation_id)
    assert len(str(caught.value).splitlines()) == 1
    assert "cleanup_complete" in str(caught.value)
    assert "astrid machines claim" in str(caught.value)
    bindings.close()


@pytest.mark.parametrize("compatibility_field", [
    "recipe_digest", "model_lock_digest", "runtime_commit", "vibecomfy_commit",
])
def test_restart_reconciles_incompatible_idle_claim_without_adopting(
        machine_case, monkeypatch, compatibility_field):
    from astrid.core.generation import settings
    from astrid.packs.runpod import target_selection

    c = machine_case
    claim(c)
    original_record = c.read()
    paid = original_record["paid_limits"]
    compatibility = dict(c.owner.compatibility)
    value = compatibility[compatibility_field]
    compatibility[compatibility_field] = ("sha256:" + "f" * 64 if value.startswith("sha256:") else "f" * 40)
    c.owner.compatibility = compatibility
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: c.prefs.as_mapping())
    monkeypatch.setattr(target_selection, "canonical_claim_path", lambda *args, **kwargs: c.owner.path)
    c.host.close()
    bindings, _ = restart_bindings(c, monkeypatch)
    forbid_allocation(c, monkeypatch)

    provider = bindings.get(kind="runpod", engine="comfyui",
                            preferences=c.prefs.as_mapping(), job=True)
    request = job(c)
    with pytest.raises(Exception) as caught:
        provider.lease_owner.job_limits(request.invocation_id)
    assert len(str(caught.value).splitlines()) == 1
    assert "cleanup_complete" in str(caught.value)
    assert "astrid machines claim" in str(caught.value)
    assert c.read()["paid_limits"] == paid
    assert c.read()["state"] == "cleanup_complete"
    assert c.read()["pod_id"] == "pod-1"
    assert c.creates == 1 and c.deletes == ["pod-1"]
    bindings.close()


def test_restart_recovery_failure_keeps_retry_timer_and_exact_custody(machine_case, monkeypatch):
    from astrid.core.generation import settings
    from runpod_lifecycle import api
    from astrid.packs.runpod import target_selection
    from astrid.packs.runpod.worker_preparation import write_target_record

    c = machine_case
    claim(c)
    original_record = c.read()
    paid = original_record["paid_limits"]
    paid_expiry = original_record["lease"]["paid_expires_at"]
    record = dict(original_record)
    record["lease"] = {**record["lease"], "state": "active"}
    write_target_record(c.owner.path, record)
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: c.prefs.as_mapping())
    monkeypatch.setattr(target_selection, "canonical_claim_path", lambda *args, **kwargs: c.owner.path)
    c.host.close()
    terminate = api.terminate_pod

    def fail_before_delete(pod_id, key):
        raise TimeoutError("fake exact delete unavailable")

    monkeypatch.setattr(api, "terminate_pod", fail_before_delete)
    bindings, _ = restart_bindings(c, monkeypatch)
    forbid_allocation(c, monkeypatch)
    provider = bindings.get(kind="runpod", engine="comfyui",
                            preferences=c.prefs.as_mapping(), job=True)
    retry = provider.lease_owner.deadline_timer
    assert retry is not None
    assert c.read()["state"] == "cleanup_pending"
    assert c.deletes == [] and c.creates == 1
    request = job(c)
    with pytest.raises(Exception) as caught:
        provider.lease_owner.job_limits(request.invocation_id)
    assert len(str(caught.value).splitlines()) == 1
    assert str(caught.value) == (
        "machine pod-1 has an unresolved preparation: "
        "run `astrid machines release pod-1`, then claim again"
    )
    assert c.read()["paid_limits"] == paid
    assert c.read()["lease"]["paid_expires_at"] == paid_expiry

    monkeypatch.setattr(api, "terminate_pod", terminate)
    retry.callback()
    assert c.read()["state"] == "cleanup_complete"
    assert c.read()["pod_id"] == "pod-1"
    assert c.deletes == ["pod-1"] and c.creates == 1
    assert c.read()["paid_limits"] == paid
    assert c.read()["lease"]["paid_expires_at"] == paid_expiry
    bindings.close()


@pytest.mark.parametrize('choice', ['absent', False, True, 'invalid', 'unreadable'])
def test_job_projects_current_model_verification_setting(machine_case, monkeypatch, generation_settings_store, choice):
    c = machine_case
    claim(c)
    if choice in (True, False):
        generation_settings_store.documents['generation'] = ({'models': {'trust_unchanged': choice}}, 1)
    elif choice == 'invalid':
        generation_settings_store.documents['generation'] = ({'models': {'trust_unchanged': 'true'}}, 1)
    elif choice == 'unreadable':
        def unreadable(key):
            raise OSError('settings store unreadable')
        monkeypatch.setattr(generation_settings_store, 'get_user_app_state', unreadable)
    request = job(c)
    run(c, request)
    body = json.loads((request.scratch / 'machine/job.json').read_text())
    assert body['generation_settings'] == {'models': {'trust_unchanged': choice is True or choice == 'absent'}}


def test_hostwedge_restart_live_job_fences_claim_until_exact_recovery(machine_case, monkeypatch):
    from runpod_lifecycle import api
    from astrid.packs.runpod.target_selection import PaidSelectionError

    c = machine_case
    claim(c)
    active, events = set(), []
    start, poll, cancel, delete = c.pod.job_start, c.pod.job_poll, c.pod.job_cancel, api.terminate_pod
    allow_cancel = False

    def detached_start(command, *args, **kwargs):
        assert not active, "a second detached job overlapped the dead host's live job"
        result = start(command, *args, **kwargs)
        if " generate cpu-image " in command:
            active.add(result["id"])
            events.append(("start", result["id"]))
        return result

    def detached_poll(job_id):
        return {"id": job_id, "state": "running"} if job_id in active else poll(job_id)

    def detached_cancel(job_id):
        events.append(("cancel", job_id))
        if not allow_cancel:
            return {"id": job_id, "state": "running"}
        result = cancel(job_id)
        active.discard(job_id)
        return result

    def terminate(pod_id, key):
        assert not active, "a live detached job lost its held resource"
        events.append(("delete", pod_id))
        return delete(pod_id, key)

    monkeypatch.setattr(c.pod, "job_start", detached_start)
    monkeypatch.setattr(c.pod, "job_poll", detached_poll)
    monkeypatch.setattr(c.pod, "job_cancel", detached_cancel)
    monkeypatch.setattr(api, "terminate_pod", terminate)
    request = job(c)
    lease = c.machine.claim(request)
    old = c.machine.start(lease, request)
    assert c.read()["lease"]["job"] == {"id": old.job_id, "state": "running"}
    assert active == {old.job_id}
    c.host.close()
    replacement = c.fresh()
    c.adapter.lease_owner = replacement
    replacement.recover_job()
    assert c.read()["state"] != "cleanup_complete"
    assert c.read()["lease"]["state"] != "idle" and active == {old.job_id}
    assert c.deletes == [] and c.creates == 1
    dispatched = len(c.pod.job_log)
    with pytest.raises(PaidSelectionError):
        run(c, job(c, 2))
    assert len(c.pod.job_log) == dispatched
    assert events == [("start", old.job_id), ("cancel", old.job_id)]
    allow_cancel = True
    replacement.recover_job()
    assert active == set()
    assert events == [("start", old.job_id), ("cancel", old.job_id),
                      ("cancel", old.job_id), ("delete", "pod-1")]
    assert c.pod.cancelled == [old.job_id]
    assert c.deletes == ["pod-1"] and c.creates == 1
    assert c.read()["state"] == "cleanup_complete"
