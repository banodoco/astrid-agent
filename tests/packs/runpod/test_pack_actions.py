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
    assert "metadata: {host_target_operation: setup}" in text
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
