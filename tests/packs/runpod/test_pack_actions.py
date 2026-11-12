"""Owner-backed setup and lease listing stay local to retained custody."""
from __future__ import annotations

import socket
import threading
import json
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from astrid.core.execution.warm_hold import WarmHolds
from astrid.packs.runpod import target_selection
from astrid.packs.runpod.adapter import RunPodAdapter, TargetProviderBindings
from astrid.packs.runpod.target_selection import (
    PaidRunConsent, PaidSelectionError, RunPodOffer, RunPodPreferences,
)
from astrid.packs.runpod.worker_owner import RunPodLeaseOwner, RunPodWorkerOwnerError


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))


def raw_preferences(**changes):
    raw = {
        "provider_account_ref": "account-a",
        "gpu_types": ["gpu-a", "gpu-b"],
        "image": "worker@sha256:" + "a" * 64,
        "volume": {"id": "volume-a", "name": "storage", "size_gb": 100,
                    "mount_path": "/workspace"},
        "idle_timeout_seconds": 120,
        "budget_cap_usd": 2.5,
    }
    raw.update(changes)
    return raw


def make_owner(tmp_path, raw=None):
    preferences = RunPodPreferences.from_mapping(raw or raw_preferences())
    selection = SimpleNamespace(
        preferences=preferences,
        path=target_selection.canonical_claim_path("account-a", data_root=tmp_path / "machine"),
        compatibility={"runtime_commit": "0" * 40},
        _read=lambda: None,
    )
    owner = RunPodLeaseOwner(selection, holds=WarmHolds(), quiesce=lambda record: None,
                             environ={}, service_environ={})
    return owner


def test_setup_persists_validated_preferences_without_authorizing_a_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    owner = make_owner(tmp_path)
    try:
        result = owner.setup()
        saved = target_selection.read_runpod_preferences()
    finally:
        owner.close()

    assert result["state"] == "saved"
    assert saved.budget_cap_usd == 2.5
    assert saved.gpu_types == ("gpu-a", "gpu-b")
    assert saved.volume_id == "volume-a" and saved.idle_timeout_seconds == 120
    assert not owner.started


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "2"])
def test_setup_rejects_invalid_budget_preference_before_acquisition(value):
    with pytest.raises(PaidSelectionError, match="budget cap"):
        RunPodPreferences.from_mapping(raw_preferences(budget_cap_usd=value))


def test_saved_budget_ceiling_still_requires_fresh_per_invocation_consent():
    preferences = RunPodPreferences.from_mapping(raw_preferences())
    offer = RunPodOffer("account-a", "gpu-a", 1.0, "offer-1")
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    grant = PaidRunConsent("inv-1", offer.digest(preferences), 2.0,
                           "2026-11-02T01:00:00Z", (), 60, 60, 60)
    with pytest.raises(PaidSelectionError, match="fresh explicit paid consent"):
        grant.validate(offer, preferences, now)
    too_large = PaidRunConsent("inv-2", offer.digest(preferences), 3.0,
                               "2026-11-02T01:00:00Z", ("paid",), 60, 60, 60)
    with pytest.raises(PaidSelectionError, match="saved RunPod budget ceiling"):
        too_large.validate(offer, preferences, now)


def test_setup_refuses_unresolved_lease_before_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    owner = make_owner(tmp_path)
    owner._read = lambda: {"state": "cleanup_pending", "operation_id": "op"}
    try:
        with pytest.raises(RunPodWorkerOwnerError, match="lease is unresolved"):
            owner.setup()
        assert not (tmp_path / "home" / ".vibecomfy" / "config.toml").exists()
    finally:
        owner.close()


def test_ls_returns_zero_or_one_lease_from_this_owner(tmp_path):
    owner = make_owner(tmp_path)
    try:
        assert owner.ls() == []
        current = {
            "operation_id": "0123456789abcdef0123456789abcdef",
            "provider_account_ref": "account-a",
            "pod_id": "pod-owned",
            "state": "claimed",
            "lease": {"state": "idle", "generation": 2, "invocation_id": "inv-2",
                      "paid_expires_at": 200, "idle_expires_at": 150, "idle_seconds": 30,
                      "provider_secret": "must-not-leak"},
            "other_account_pod": "hidden",
        }
        owner._read = lambda: current
        assert owner.ls() == [{
            "operation_id": current["operation_id"], "pod_id": "pod-owned",
            "provider_account_ref": "account-a", "state": "claimed",
            "lease": {"state": "idle", "generation": 2, "invocation_id": "inv-2",
                      "paid_expires_at": 200, "idle_expires_at": 150, "idle_timeout_s": 30},
        }]
        current["state"] = "cleanup_complete"
        assert owner.ls() == []
    finally:
        owner.close()


def test_adapter_dispatches_setup_ls_and_preserves_status_release():
    calls = []

    class Owner:
        lock = threading.RLock()

        def setup(self):
            calls.append("setup")
            return {"state": "saved"}

        def ls(self):
            calls.append("ls")
            return []

        def release(self, *, reason):
            calls.append(("release", reason))
            return {"state": "cleanup_complete"}

        def _read(self):
            calls.append("status")
            return None

    adapter = RunPodAdapter(lease_owner=Owner(), placement=None, installation=None, validate=None)
    assert adapter.operate("setup")["state"] == "saved"
    assert adapter.operate("ls") == {"leases": []}
    assert adapter.operate("status")["state"] == "absent"
    assert adapter.operate("release")["state"] == "cleanup_complete"
    assert calls == ["setup", "ls", "status", ("release", "explicit")]


def test_unsupported_operation_refuses_before_provider_construction():
    bindings = TargetProviderBindings.__new__(TargetProviderBindings)
    bindings.lock = __import__("threading").RLock()
    bindings.provider = None
    bindings.signature = None
    bindings.get = lambda **kwargs: pytest.fail("unsupported action constructed an owner")
    with pytest.raises(ValueError, match="unsupported retained target operation"):
        bindings.operate({"kind": "runpod", "engine": "comfyui", "preferences": {}}, "reboot")


def test_manifest_routes_setup_and_ls_to_the_existing_host_owner():
    from pathlib import Path

    manifest = Path(__file__).parents[3] / "astrid" / "packs" / "runpod" / "pack.yaml"
    text = manifest.read_text()
    assert "metadata: {host_target_operation: setup, project_scope: optional}" in text
    assert "metadata: {host_target_operation: ls}" in text
    assert "host_target_operation: status" in text
    assert "host_target_operation: release" in text
    assert "host_target_operation: keep" in text


def keep_policy():
    preferences = RunPodPreferences.from_mapping(raw_preferences())
    offer = RunPodOffer("account-a", "gpu-a", 1.0, "offer-1")
    grant = PaidRunConsent("keep-1", offer.digest(preferences), 2.0,
                           "2026-11-02T02:00:00Z", ("paid",), 0, 0, 120)
    return {"kind": "runpod", "engine": "comfyui", "preferences": preferences.as_mapping(),
            "machine": "pod-owned", "paid_consent": asdict(grant), "until_release": True}


def test_keep_without_a_live_owner_refuses_without_constructing_or_recovering():
    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    bindings.get = lambda **kwargs: pytest.fail("keep constructed an owner and could recover its pod")
    policy = keep_policy()
    before = json.dumps(policy, sort_keys=True)
    with pytest.raises(ValueError, match="running host that owns this machine"):
        bindings.operate(policy, "keep")
    assert bindings.provider is None and json.dumps(policy, sort_keys=True) == before


def test_keep_operation_dispatches_exact_machine_and_fresh_consent_to_retained_owner():
    calls = []

    class Owner:
        def keep(self, machine, consent, until_release):
            calls.append((machine, consent, until_release))
            return {"state": "claimed", "pod_id": machine, "lease": {"state": "idle", "paid_expires_at": 200},
                    "paid_limits": {"hourly_usd": 1, "max_usd": consent.max_usd, "expires_at": "later"}}

    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    policy = keep_policy()
    bindings.provider = RunPodAdapter(lease_owner=Owner(), placement=None, installation=None, validate=None)
    bindings.signature = json.dumps([policy["kind"], policy["engine"], policy["preferences"]])
    bindings.get = lambda **kwargs: pytest.fail("keep replaced the retained owner")
    result = bindings.operate(policy, "keep")
    assert len(calls) == 1
    machine, grant, until_release = calls[0]
    assert machine == "pod-owned" and grant.invocation_id == "keep-1" and until_release is True
    assert result["paid_limits"]["max_usd"] == 2.0 and result["lease"]["paid_expires_at"] == 200
    assert bindings.operations == 0


def test_sdk_keep_sends_exact_machine_consent_and_waits_longer_than_release_timeout():
    from astrid.sdk.generation import _runpod_operation

    calls = []
    client = SimpleNamespace(invoke_result=lambda *args, **kwargs: calls.append((args, kwargs)))
    policy = keep_policy()
    _runpod_operation("keep", client=client, wait=True, preferences=policy["preferences"],
                       machine_id=policy["machine"], consent=policy["paid_consent"],
                       until_release=True, timeout_seconds=300)
    args, kwargs = calls[0]
    assert args == ("runpod.keep",)
    submitted = json.loads(kwargs["inputs"]["placement_policy"])
    assert submitted["machine"] == "pod-owned" and submitted["until_release"] is True
    assert submitted["paid_consent"]["invocation_id"] == "keep-1"
    assert kwargs["wait"] is True and kwargs["timeout_seconds"] == 300


def claim_policy():
    policy = keep_policy()
    policy.pop("machine")
    policy.pop("until_release")
    policy["requested_idle_seconds"] = 120
    policy["paid_consent"]["invocation_id"] = "claim-1"
    return policy


def test_claim_operation_dispatches_retention_and_cancellation_to_shared_owner():
    calls = []
    cancelled = lambda: False

    class Owner:
        def claim(self, consent, *, requested_idle_seconds, cancelled):
            calls.append((consent, requested_idle_seconds, cancelled))
            return {"state": "claimed", "pod_id": "pod-owned", "lease": {"state": "idle", "paid_expires_at": 200},
                    "paid_limits": {"hourly_usd": 1, "max_usd": consent.max_usd}}

    policy = claim_policy()
    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    bindings.provider = RunPodAdapter(lease_owner=Owner(), placement=None, installation=None, validate=None)
    bindings.signature = json.dumps([policy["kind"], policy["engine"], policy["preferences"]])
    bindings.get = lambda **kwargs: pytest.fail("claim replaced its live owner")
    result = bindings.operate(policy, "claim", cancelled=cancelled)
    assert result["pod_id"] == "pod-owned" and result["lease"]["state"] == "idle"
    assert calls[0][0].invocation_id == "claim-1" and calls[0][1:] == (120, cancelled)


def test_claim_and_disclosure_construct_only_a_fresh_guarded_owner(monkeypatch):
    from astrid.packs.runpod import adapter
    calls = []
    policy = claim_policy()
    provider = SimpleNamespace(operate=lambda *args, **kwargs: {"state": "claimed"})

    def build(**kwargs):
        calls.append(kwargs)
        assert kwargs["fresh_claim"] is True
        return provider

    monkeypatch.setattr(adapter, "build_host_adapter", build)
    for operation, request in (("claim", policy), ("status", {key: value for key, value in policy.items()
                                                          if key in {"kind", "engine", "preferences"}})):
        bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
        if operation == "status":
            request = {**request, "fresh_claim": True}
        assert bindings.operate(request, operation, include_quote=operation == "status")["state"] == "claimed"
    assert len(calls) == 2


@pytest.mark.parametrize("changed_preferences", [{"gpu_types": ["gpu-b"]}, {"datacenter_id": "DC-other"}])
def test_claim_different_gpu_or_datacenter_policy_refuses_while_held_without_replacement(changed_preferences):
    policy = claim_policy()
    owner = SimpleNamespace(lock=threading.RLock(), _read=lambda: {"state": "claimed", "pod_id": "pod-owned"})
    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    bindings.provider = RunPodAdapter(lease_owner=owner, placement=None, installation=None, validate=None)
    bindings.signature = json.dumps([policy["kind"], policy["engine"], policy["preferences"]])
    policy = {**policy, "preferences": {**policy["preferences"], **changed_preferences}}
    with pytest.raises(ValueError, match="policy differs"):
        bindings.operate(policy, "claim")
    assert bindings.provider.lease_owner is owner


def test_sdk_claim_requires_ready_host_before_submission_and_serializes_fresh_consent():
    from astrid.sdk.generation import _runpod_operation
    calls = []
    client = SimpleNamespace(invoke_result=lambda *args, **kwargs: calls.append((args, kwargs)),
                             list_capabilities=lambda **kwargs: [[{"capability_id": "runpod.claim", "status": "ready"}], None],
                             get_user_app_state=lambda: None)
    policy = claim_policy()
    _runpod_operation("claim", client=client, wait=True, preferences=policy["preferences"],
                       consent=policy["paid_consent"], requested_idle_seconds=120)
    args, kwargs = calls[0]
    assert args == ("runpod.claim",) and kwargs["wait"] is True
    submitted = json.loads(kwargs["inputs"]["placement_policy"])
    assert submitted["requested_idle_seconds"] == 120 and "machine" not in submitted
    assert submitted["paid_consent"]["invocation_id"] == "claim-1"
    client.list_capabilities = lambda **kwargs: [[], None]
    with pytest.raises(Exception, match="running Astrid host"):
        _runpod_operation("claim", client=client, wait=True, preferences=policy["preferences"],
                           consent=policy["paid_consent"], requested_idle_seconds=120)
    assert len(calls) == 1


def test_manifest_claim_routes_to_existing_host_without_media_output():
    from pathlib import Path
    import yaml
    manifest = yaml.safe_load((Path(__file__).parents[3] / "astrid/packs/runpod/pack.yaml").read_text())
    assert manifest["actions"]["claim"]["metadata"]["host_target_operation"] == "claim"
    assert manifest["extensions"]["astrid"]["output_result_exemptions"]["actions"]["claim"]["reasons"] == ["no-artifact"]


def test_claim_wait_policy_is_recognized_and_ctrl_c_cancels_known_task(monkeypatch):
    from astrid.sdk import invocation
    policy = claim_policy()
    policy["paid_consent"].update(capacity_timeout_seconds=600, wait_for_capacity=True)
    task = {"spec": {"spec": {"capability_id": "runpod.claim", "inputs": {"placement_policy": json.dumps(policy)}}}}
    assert invocation._runpod_generation_policy(task) == json.loads(json.dumps(policy["paid_consent"]))
    cancelled = []
    client = SimpleNamespace(tasks=SimpleNamespace(cancel=lambda *args, **kwargs: cancelled.append((args, kwargs))))

    def interrupt(*args, **kwargs):
        kwargs["owner_tick"](task)
        raise KeyboardInterrupt

    monkeypatch.setattr(invocation, "_wait_for_kernel_task_loop", interrupt)
    with pytest.raises(KeyboardInterrupt):
        invocation._wait_for_kernel_task(client, task_id="claim-task", run_id="claim-run",
                                         timeout_seconds=3600, poll_seconds=0.01)
    assert len(cancelled) == 1 and cancelled[0][0] == ("claim-task",)


def test_claim_policy_without_volume_or_datacenter_observes_all_returned_datacenters(monkeypatch):
    import asyncio
    import runpod_lifecycle
    from astrid.packs.runpod.adapter import observe_priced_offer
    preferences = RunPodPreferences.from_mapping(raw_preferences(volume=None, datacenter_id=None))
    seen = []

    async def availability(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(status="available", gpu_type_id="gpu-id", datacenters=[
            SimpleNamespace(status="unavailable", price_per_hour=None, datacenter_id="CA-ONLY-EMPTY"),
            SimpleNamespace(status="available", price_per_hour=1.0, datacenter_id="UNRESTRICTED-AVAILABLE"),
        ])

    monkeypatch.setattr(runpod_lifecycle, "gpu_availability", availability)
    offer = asyncio.run(observe_priced_offer(preferences, SimpleNamespace(api_key="synthetic-only",
        container_disk_gb=40, allowed_cuda_versions=("13.0",), min_cuda_version=None)))
    assert seen[0]["datacenter_ids"] is None and seen[0]["require_network_volume"] is False
    assert offer.datacenter_id == "UNRESTRICTED-AVAILABLE"


@pytest.mark.parametrize("capacity_timeout,complete,limit", [(None, True, None), (10, False, 3610)])
def test_claim_follow_uses_capacity_timeout_and_heartbeats_without_one_hour_ceiling(
        monkeypatch, capsys, capacity_timeout, complete, limit):
    from astrid.sdk import invocation
    now = [0.0]
    policy = claim_policy()
    policy["paid_consent"].update(capacity_timeout_seconds=capacity_timeout, wait_for_capacity=True)

    def show(task_id):
        state = "succeeded" if complete and now[0] >= 3700 else "running"
        return SimpleNamespace(ok=True, data={"state": state, "result": {"outputs": []},
            "spec": {"spec": {"capability_id": "runpod.claim", "inputs": {"placement_policy": json.dumps(policy)}}}})

    monkeypatch.setattr(invocation.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(invocation.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    raw, ok, _ = invocation._wait_for_kernel_task(SimpleNamespace(tasks=SimpleNamespace(show=show)),
        task_id="claim-task", run_id="claim-run", timeout_seconds=3600, poll_seconds=60)
    if complete:
        assert ok and now[0] > 3600
    else:
        assert not ok and raw["error"]["code"] == "task_wait_timeout"
        assert raw["error"]["details"]["timeout_seconds"] == limit and now[0] == limit
    assert "Still following RunPod task claim-task" in capsys.readouterr().err
