"""R93-A: real retained status reads effective settings without importing them."""
from __future__ import annotations

import copy
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution.warm_hold import WarmHolds
from astrid.core.generation import settings, targets
from astrid.packs.runpod.adapter import TargetProviderBindings
from astrid.packs.runpod.reuse_policy import RunPodPolicyMismatch, saved_policy_notice
from astrid.packs.runpod.target_selection import RunPodPreferences, canonical_claim_path
from astrid.sdk.generation import GenerationFacade


def _legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "machine"))
    raw = {
        "provider_account_ref": "legacy-account",
        "gpu_types": ["GPU fixture"],
        "image": "fixture/cuda130@sha256:" + "1" * 64,
        "idle_timeout_seconds": 1800,
        "credential_ref": "env:RUNPOD_FAKE_CREDENTIAL",
        "budget_cap_usd": 2,
    }
    config = targets.config_path()
    return raw, config


def _write_legacy(config, raw):
    config.parent.mkdir(parents=True)
    data = ('[placement]\nkind = "attached"\nendpoint = "http://127.0.0.1:8199"\n'
            '[runpod]\n' + ''.join(f"{key} = {json.dumps(value)}\n" for key, value in raw.items())).encode()
    config.write_bytes(data)
    return data


def _bindings():
    holds = WarmHolds()
    source = Path(__file__).resolve().parents[3]
    bindings = TargetProviderBindings(
        pack_roots=[source / "astrid/packs/runpod"], holds=holds,
        credentials={"RUNPOD_FAKE_CREDENTIAL": "synthetic-only"}.__getitem__, environ={},
    )
    return bindings, holds


@pytest.fixture
def no_provider_io(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append("connect")
        raise AssertionError("plain retained status attempted provider network I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    yield
    # Also fail if a product layer caught the forbidden provider exception.
    assert not attempts


@pytest.mark.parametrize("runtime_exists", [False, True])
def test_no_save_targets_executes_actual_retained_status(
        tmp_path, monkeypatch, generation_settings_store, no_provider_io, runtime_exists):
    legacy, config = _legacy(tmp_path, monkeypatch)
    effective = legacy
    if runtime_exists:
        effective = {**legacy, "provider_account_ref": "runtime-account",
                     "gpu_types": ["Runtime GPU"], "budget_cap_usd": 3}
        # Seed the real versioned app-state fixture before creating legacy TOML.
        settings.set_machine_policy("runpod", effective)
        settings.set_placement("http://127.0.0.1:8200")
    data = _write_legacy(config, legacy)
    before = copy.deepcopy((generation_settings_store.documents, generation_settings_store.receipts))
    bindings, holds = _bindings()
    invoked = []

    class Client:
        def get_user_app_state(self, key):
            return generation_settings_store.get_user_app_state(key)

        def update_user_app_state(self, key, content, **kwargs):
            return generation_settings_store.update_user_app_state(key, content, **kwargs)

        def invoke_result(self, capability, *, inputs, **kwargs):
            invoked.append(capability)
            assert capability == "runpod.status" and inputs["include_quote"] is False
            policy = json.loads(inputs["placement_policy"])
            assert policy == {"kind": "runpod", "engine": "comfyui", "preferences": None}
            # The same retained operation GenericPackHost dispatches to; no
            # replacement owner, settings reader or provider adapter.
            status = bindings.operate(policy, "status", include_quote=False)
            return SimpleNamespace(ok=True, raw_result={"result": {"payload": {"target_operation": status}}})

    try:
        facade = GenerationFacade()
        payload = facade.targets(placement="runpod", save=False, client=Client())
        assert payload["preferences"] == effective
        assert payload["target"] == {"state": "absent"}
        owner = bindings.provider
        assert owner is not None
        assert owner.lease_owner.selection.preferences == RunPodPreferences.from_mapping(effective)
        assert facade.targets(placement="runpod", save=False, client=Client()) == payload
        assert bindings.provider is owner
        assert invoked == ["runpod.status", "runpod.status"]
        assert not canonical_claim_path(effective["provider_account_ref"]).exists()
        assert not canonical_claim_path(legacy["provider_account_ref"]).exists()
        assert (generation_settings_store.documents, generation_settings_store.receipts) == before
        assert config.read_bytes() == data
        if runtime_exists:
            # An explicit writer changes saved preferences, not the retained
            # owner's signature. A plain status must not rebind that owner.
            revised = {**effective, "gpu_types": ["Other GPU"]}
            settings.set_machine_policy("runpod", revised)
            before = copy.deepcopy((generation_settings_store.documents, generation_settings_store.receipts))
            observed = facade.targets(placement="runpod", save=False, client=Client())
            assert observed["preferences"] == revised and observed["target"] == {"state": "absent"}
            assert bindings.provider is owner
            assert owner.lease_owner.selection.preferences == RunPodPreferences.from_mapping(effective)
            assert invoked == ["runpod.status"] * 3
            assert (generation_settings_store.documents, generation_settings_store.receipts) == before
            assert config.read_bytes() == data
    finally:
        bindings.close()
        holds.close()
    assert (generation_settings_store.documents, generation_settings_store.receipts) == before
    assert config.read_bytes() == data


def test_retained_status_refuses_unavailable_runtime_without_import(
        tmp_path, monkeypatch, generation_settings_store, no_provider_io):
    legacy, config = _legacy(tmp_path, monkeypatch)
    data = _write_legacy(config, legacy)
    monkeypatch.setattr(settings, "_store_factory", lambda: None)
    bindings, holds = _bindings()
    try:
        with pytest.raises(RunPodPolicyMismatch, match="settings store unreadable"):
            bindings.operate({"kind": "runpod", "engine": "comfyui", "preferences": None}, "status")
        assert bindings.provider is None
        assert generation_settings_store.documents == generation_settings_store.receipts == {}
        assert not canonical_claim_path(legacy["provider_account_ref"]).exists()
        assert config.read_bytes() == data
    finally:
        bindings.close()
        holds.close()


def test_claim_status_notice_projects_legacy_without_import(
        tmp_path, monkeypatch, generation_settings_store):
    legacy, config = _legacy(tmp_path, monkeypatch)
    data = _write_legacy(config, legacy)
    notice = saved_policy_notice({"state": "claimed", "paid_limits": {
        "max_usd": 4, "idle_seconds": 3600, "expires_at": "2026-11-27T17:00:00Z",
    }})
    assert notice["code"] == "saved_policy_stricter_than_claim"
    assert notice["limits"] == {
        "budget_cap_usd": {"recorded": 4, "saved": 2},
        "idle_timeout_seconds": {"recorded": 3600, "saved": 1800},
    }
    assert generation_settings_store.documents == generation_settings_store.receipts == {}
    assert config.read_bytes() == data
