"""Owner-backed setup and lease listing stay local to retained custody."""
from __future__ import annotations

import json
import socket
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from astrid.core.execution.warm_hold import WarmHolds
from astrid.packs.runpod import target_selection
from astrid.packs.runpod.adapter import RunPodAdapter, TargetProviderBindings
from astrid.packs.runpod.target_selection import (
    PaidRunConsent,
    PaidSelectionError,
    RunPodOffer,
    RunPodPreferences,
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


@pytest.mark.parametrize("state", [None, "claimed", "cleanup_complete"])
@pytest.mark.parametrize("change", [
    {"budget_cap_usd": 1.5}, {"gpu_types": ["gpu-b"]}, {"datacenter_id": "DC-other"},
    {"volume": {"id": "volume-b", "name": "other", "size_gb": 200, "mount_path": "/workspace"}},
])
def test_status_refuses_changed_preferences_without_touching_retained_owner(state, change):
    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    preferences = RunPodPreferences.from_mapping(raw_preferences()).as_mapping()
    provider = SimpleNamespace(operate=lambda *args, **kwargs: pytest.fail("status reached mismatched owner"),
        lease_owner=SimpleNamespace(_read=lambda: None if state is None else {"state": state}))
    bindings.provider = provider
    bindings.signature = json.dumps(["runpod", "comfyui", preferences])
    bindings.get = lambda **kwargs: pytest.fail("status replaced the retained owner")
    policy = {"kind": "runpod", "engine": "comfyui", "preferences": {**preferences, **change}}
    before = json.dumps(policy, sort_keys=True)
    with pytest.raises(ValueError, match="preferences differ.*runpod.setup"):
        bindings.operate(policy, "status")
    assert bindings.provider is provider and bindings.operations == 0
    assert json.dumps(policy, sort_keys=True) == before


@pytest.mark.parametrize("operation", ["status", "ls", "release"])
def test_retained_operations_accept_equivalent_optional_volume_preferences(operation):
    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    preferences = RunPodPreferences.from_mapping(raw_preferences(volume=None)).as_mapping()
    calls = []
    bindings.provider = SimpleNamespace(operate=lambda name, **kwargs: calls.append(name) or {"state": "absent"})
    bindings.signature = json.dumps(["runpod", "comfyui", preferences])
    policy = {"kind": "runpod", "engine": "comfyui", "preferences": {**preferences, "volume": None}}
    assert bindings.operate(policy, operation) == {"state": "absent"}
    assert calls == [operation]


@pytest.mark.parametrize("operation", ["ls", "release"])
def test_custody_listing_and_release_use_retained_owner_after_preferences_change(operation):
    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    preferences = RunPodPreferences.from_mapping(raw_preferences()).as_mapping()
    calls = []
    provider = SimpleNamespace(operate=lambda name, **kwargs: calls.append(name) or {"state": "cleanup_complete"})
    bindings.provider = provider
    bindings.signature = json.dumps(["runpod", "comfyui", preferences])
    bindings.get = lambda **kwargs: pytest.fail("custody operation replaced retained owner")
    policy = {"kind": "runpod", "engine": "comfyui", "preferences": {**preferences, "budget_cap_usd": 1.5}}
    assert bindings.operate(policy, operation) == {"state": "cleanup_complete"}
    assert calls == [operation] and bindings.provider is provider


@pytest.mark.parametrize("key", ["max_usd", "deadline", "budget_cap_uds"])
def test_preferences_reject_unknown_or_claim_only_keys(key):
    with pytest.raises(PaidSelectionError, match="unsupported RunPod preferences"):
        RunPodPreferences.from_mapping(raw_preferences(**{key: 1.5}))


@pytest.mark.parametrize("operation", ["ls", "release"])
@pytest.mark.parametrize("change,engine", [
    ({"unknown_key": True}, "comfyui"), ({"gpu_types": []}, "comfyui"),
    ({"image": "invalid"}, "comfyui"), ({"volume": "invalid"}, "comfyui"),
    ({"budget_cap_usd": -1}, "comfyui"), ({}, "different-engine"),
])
def test_cleanup_ignores_invalid_saved_and_requested_policy(operation, change, engine, monkeypatch):
    from astrid.core.generation import settings

    preferences = raw_preferences()
    settings.set_machine_policy("runpod", {**preferences, **change})
    calls = []

    class Owner:
        def ls(self):
            calls.append("ls")
            return [{"pod_id": "pod-owned"}]

        def release(self, **kwargs):
            calls.append("release")
            return {"state": "cleanup_complete"}

        def claim(self, *args, **kwargs):
            pytest.fail("cleanup spent through claim")

        def keep(self, *args, **kwargs):
            pytest.fail("cleanup spent through keep")

    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    bindings.provider = RunPodAdapter(lease_owner=Owner(), placement=None, installation=None, validate=None)
    bindings.signature = json.dumps(["runpod", "comfyui", preferences])
    bindings.get = lambda **kwargs: pytest.fail("cleanup constructed or created a provider")
    monkeypatch.setattr(settings, "machine_policy", lambda *args, **kwargs: pytest.fail("cleanup read settings"))
    result = bindings.operate({"kind": "runpod", "engine": engine,
                               "preferences": {**preferences, **change}}, operation)
    assert calls == [operation]
    if operation == "ls":
        assert result["leases"] == [{"pod_id": "pod-owned"}]
    else:
        assert result["state"] == "cleanup_complete"


@pytest.mark.parametrize("operation", ["keep", "claim"])
@pytest.mark.parametrize("state,reason", [("missing", "no saved policy"),
    ("discovery", "store unreadable"), ("disk", "store unreadable"), ("invalid", "saved policy invalid")])
def test_paid_operation_refuses_unknown_current_policy(operation, state, reason, monkeypatch, tmp_path):
    from astrid.core.generation import settings

    policy = keep_policy() if operation == "keep" else claim_policy()
    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    bindings.provider = SimpleNamespace(operate=lambda *args, **kwargs: pytest.fail("unknown policy reached paid owner"))
    bindings.signature = json.dumps([policy["kind"], policy["engine"], policy["preferences"]])
    if state == "discovery":
        def unavailable():
            raise OSError("test store discovery failed")
        settings.set_store_factory(unavailable)
    elif state == "disk":
        path = tmp_path / "corrupt-settings.json"
        path.write_text("{broken")
        settings.set_store_factory(lambda: SimpleNamespace(get_user_app_state=lambda key: json.loads(path.read_text())))
    else:
        from tests.conftest import InMemoryAppState
        store = InMemoryAppState()
        settings.set_store_factory(lambda: store)
        if state == "invalid":
            settings.set_machine_policy("runpod", raw_preferences(gpu_types=[]))
    with pytest.raises(ValueError, match=reason + ".*runpod.setup"):
        bindings.operate(policy, operation)
    assert bindings.operations == 0


@pytest.mark.parametrize("operation", ["keep", "claim"])
def test_raising_saved_ceiling_conservatively_refuses_retained_owner(operation):
    policy = keep_policy() if operation == "keep" else claim_policy()
    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    bindings.provider = SimpleNamespace(operate=lambda *a, **k: pytest.fail("raised ceiling reused old authority"))
    bindings.signature = json.dumps([policy["kind"], policy["engine"], policy["preferences"]])
    target_selection.save_runpod_preferences(raw_preferences(budget_cap_usd=3))
    with pytest.raises(ValueError, match="preferences differ.*runpod.setup"):
        bindings.operate(policy, operation)


@pytest.mark.parametrize("operation,engine", [("ls", "comfyui"), ("release", "comfyui"),
                                               ("release", "different-engine")])
def test_cleanup_sdk_ignores_corrupt_disk_settings(operation, engine, monkeypatch, tmp_path):
    from astrid.core.generation import settings
    from astrid.sdk.generation import GenerationFacade, _runpod_operation

    path = tmp_path / "corrupt-settings.json"
    path.write_text("{broken")
    broken = SimpleNamespace(get_user_app_state=lambda key: json.loads(path.read_text()))
    settings.set_store_factory(lambda: broken)
    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    bindings.signature = json.dumps(["runpod", "comfyui", raw_preferences()])
    calls = []

    def cleanup(name, **kwargs):
        assert name in {"ls", "release"}, "cleanup reached claim, keep or create"
        assert "consent" not in kwargs
        calls.append(name)
        return {"state": "cleanup_complete"}

    bindings.provider = SimpleNamespace(operate=cleanup)
    bindings.get = lambda **kwargs: pytest.fail("cleanup built or created a provider")

    def invoke(capability, **kwargs):
        assert capability == "runpod." + operation
        return bindings.operate(json.loads(kwargs["inputs"]["placement_policy"]), operation)

    broken.invoke_result = invoke
    result = (_runpod_operation("ls", client=broken, wait=True) if operation == "ls" else
              GenerationFacade().release(engine=engine, placement="runpod", client=broken, wait=True))
    assert result["state"] == "cleanup_complete" and calls == [operation]


def test_cold_listing_uses_journal_without_settings_or_provider(monkeypatch):
    from astrid.packs.runpod import adapter

    record = {"operation_id": "operation", "pod_id": "pod-owned", "provider_account_ref": "account-a",
              "state": "claimed", "lease": {"state": "idle", "paid_expires_at": 200, "idle_seconds": 60}}
    monkeypatch.setattr(adapter, "_cleanup_records", lambda machine_id=None: [record])
    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    bindings.get = lambda **kwargs: pytest.fail("cold list constructed or created a provider")
    result = bindings.operate({"kind": "runpod", "engine": "different", "preferences": None}, "ls")
    assert result["leases"][0]["pod_id"] == "pod-owned"
    assert result["leases"][0]["lease"]["paid_expires_at"] == 200
    assert bindings.provider is None


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
    target_selection.save_runpod_preferences(preferences.as_mapping())
    offer = RunPodOffer("account-a", "gpu-a", 1.0, "offer-1")
    grant = PaidRunConsent("keep-1", offer.digest(preferences), 2.0,
                           "2026-11-02T02:00:00Z", ("paid",), 0, 0, 120)
    return {"kind": "runpod", "engine": "comfyui", "preferences": preferences.as_mapping(),
            "machine": "pod-owned", "paid_consent": asdict(grant), "until_release": True}


@pytest.mark.parametrize("operation", ["status", "keep"])
@pytest.mark.parametrize("field,value", [
    pytest.param("budget_cap_usd", 1.5, id="budget"),
    pytest.param("gpu_types", ["gpu-b"], id="gpu"),
    pytest.param("datacenter_id", "DC-other", id="datacenter"),
    pytest.param("volume.id", "other-volume", id="volume-id"),
    pytest.param("volume.name", "other-name", id="volume-name"),
    pytest.param("volume.size_gb", 200, id="volume-size"),
    pytest.param("volume.mount_path", "/other", id="volume-mount"),
    pytest.param("provider_account_ref", "account-b", id="account"),
    pytest.param("image", "worker@sha256:" + "b" * 64, id="image"),
    pytest.param("idle_timeout_seconds", 60, id="idle"),
    pytest.param("credential_ref", "env:RUNPOD_OTHER_CREDENTIAL", id="credential"),
    pytest.param("engine", "other-engine", id="engine"),
])
def test_retained_status_and_keep_refuse_full_policy_mismatch(operation, field, value):
    policy = keep_policy()
    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    provider = SimpleNamespace(operate=lambda *args, **kwargs: pytest.fail("mismatched policy reached owner"))
    bindings.provider = provider
    bindings.signature = json.dumps([policy["kind"], policy["engine"], policy["preferences"]])
    bindings.get = lambda **kwargs: pytest.fail("mismatched policy replaced owner")
    if operation == "status":
        policy = {key: policy[key] for key in ("kind", "engine", "preferences")}
    if field == "engine":
        policy["engine"] = value
    elif field.startswith("volume."):
        policy["preferences"]["volume"][field.split(".")[1]] = value
    else:
        policy["preferences"][field] = value
    before = json.dumps(policy, sort_keys=True)
    # cause daed72ed7; ruling: machines 12:35 Sat
    with pytest.raises(ValueError, match="preferences differ.*runpod.setup"):
        bindings.operate(policy, operation)
    assert bindings.provider is provider and bindings.operations == 0
    assert json.dumps(policy, sort_keys=True) == before


def test_keep_accepts_equivalent_normalized_volume_without_replacing_owner():
    policy = keep_policy()
    policy["preferences"].pop("volume")
    target_selection.save_runpod_preferences(policy["preferences"])
    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    bindings.signature = json.dumps([policy["kind"], policy["engine"], policy["preferences"]])
    calls = []
    bindings.provider = SimpleNamespace(operate=lambda name, **kwargs: calls.append(name) or {"state": "claimed"})
    bindings.get = lambda **kwargs: pytest.fail("equivalent policy replaced owner")
    policy["preferences"]["volume"] = {"id": None, "name": None, "size_gb": 0, "mount_path": "/workspace"}
    assert bindings.operate(policy, "keep") == {"state": "claimed"}
    assert calls == ["keep"]


def test_every_retained_entry_point_checks_policy_or_is_explicitly_nonspending(monkeypatch):
    import ast
    import inspect
    import textwrap
    from pathlib import Path

    import yaml

    from astrid.packs.runpod import adapter

    routes = adapter.RETAINED_OPERATION_POLICY
    manifest = yaml.safe_load((Path(__file__).parents[3] / "astrid/packs/runpod/pack.yaml").read_text())
    declared = {action["metadata"]["host_target_operation"]
                for action in manifest["actions"].values()
                if action.get("metadata", {}).get("host_target_operation")}
    dispatched = set()
    for method in (adapter.RunPodAdapter.operate, adapter.TargetProviderBindings.operate):
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "operation":
                dispatched.update(child.value for operand in node.comparators for child in ast.walk(operand)
                                  if isinstance(child, ast.Constant) and isinstance(child.value, str))
    assert declared | dispatched <= routes.keys(), "an action has no retained-policy classification"
    assert set(routes.values()) <= {"checked", "nonspending"}, "unclassified retained operation"
    assert {name for name, route in routes.items() if route == "nonspending"} == {"ls", "release"}

    class PolicyChecked(Exception):
        pass

    def checked(**kwargs):
        raise PolicyChecked

    def bindings_for(policy):
        bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
        bindings.signature = json.dumps([policy["kind"], policy["engine"], policy["preferences"]])
        selection = SimpleNamespace(_read=lambda: {"claim_preferences": policy["preferences"]})
        bindings.provider = SimpleNamespace(
            operate=lambda name, **kwargs: {"operation": name},
            lease_owner=SimpleNamespace(selection=selection))
        monkeypatch.setattr(bindings, "_check_reuse_policy", checked)
        return bindings

    for operation, route in routes.items():
        policy = (keep_policy() if operation == "keep" else claim_policy() if operation == "claim" else
                  {key: value for key, value in keep_policy().items() if key in {"kind", "engine", "preferences"}})
        variants = ({}, {"include_quote": True}, {"fresh_claim": True},
                    {"include_quote": True, "fresh_claim": True}) if operation == "status" else ({},)
        for variant in variants:
            request = {**policy, **({"fresh_claim": True} if variant.get("fresh_claim") else {})}
            bindings = bindings_for(policy)
            if route == "checked":
                with pytest.raises(PolicyChecked):
                    bindings.operate(request, operation, include_quote=variant.get("include_quote", False))
            else:
                assert bindings.operate(request, operation) == {"operation": operation}

    # GenericPackHost takes jobs through get(), not the target-action dispatcher.
    for job in (False, True):
        policy = keep_policy()
        bindings = bindings_for(policy)
        with pytest.raises(PolicyChecked):
            bindings.get(kind=policy["kind"], engine=policy["engine"],
                         preferences=None if job else policy["preferences"], job=job)

        # A restarted host may reconstruct the owner from the claim journal.
        cold = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
        monkeypatch.setattr(cold, "_check_reuse_policy", checked)
        monkeypatch.setattr(adapter, "build_host_adapter", lambda **kwargs: bindings.provider)
        monkeypatch.setattr(adapter, "_claimed_preferences", lambda *args: policy["preferences"])
        with pytest.raises(PolicyChecked):
            cold.get(kind=policy["kind"], engine=policy["engine"],
                     preferences=None if job else policy["preferences"], job=job)


@pytest.mark.parametrize("change", [{"budget_cap_usd": 1.5}, {"gpu_types": ["gpu-b"]},
                                     {"credential_ref": "env:RUNPOD_OTHER_CREDENTIAL"}])
def test_cached_job_checks_full_recorded_claim_policy(change):
    preferences = RunPodPreferences.from_mapping(raw_preferences()).as_mapping()
    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    bindings.signature = json.dumps(["runpod", "comfyui", preferences])
    selection = SimpleNamespace(_read=lambda: {"claim_preferences": {**preferences, **change}})
    provider = SimpleNamespace(lease_owner=SimpleNamespace(selection=selection))
    bindings.provider = provider
    with pytest.raises(ValueError, match="preferences differ.*runpod.setup"):
        bindings.get(kind="runpod", engine="comfyui", job=True)
    assert bindings.provider is provider


def test_claim_override_keeps_gpu_and_datacenter_but_obeys_current_saved_ceiling():
    target_selection.save_runpod_preferences(raw_preferences())
    policy = claim_policy()
    policy["preferences"] = {**policy["preferences"], "gpu_types": ["gpu-b"], "datacenter_id": "DC-other"}
    preferences = RunPodPreferences.from_mapping(policy["preferences"])
    policy["paid_consent"]["offer_digest"] = RunPodOffer("account-a", "gpu-b", 1, "offer-1", "DC-other").digest(preferences)
    bindings = TargetProviderBindings(pack_roots=[], holds=WarmHolds(), credentials=None, environ={})
    bindings.signature = json.dumps(["runpod", "comfyui", preferences.as_mapping()])
    calls = []
    bindings.provider = SimpleNamespace(operate=lambda name, **kwargs: calls.append(name) or {"state": "claimed"})
    assert bindings.operate(policy, "claim") == {"state": "claimed"}
    assert calls == ["claim"]
    target_selection.save_runpod_preferences(raw_preferences(budget_cap_usd=1.5))
    with pytest.raises(ValueError, match="preferences differ.*runpod.setup"):
        bindings.operate(policy, "claim")
    assert calls == ["claim"], "a stale explicit claim reached the old ceiling"


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
