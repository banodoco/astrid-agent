"""Journal/consent proofs using only fake lifecycle and provider observations."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import target_selection as selection
from astrid.packs.runpod.target_selection import (
    PaidRunConsent, PaidSelectionError, RunPodOffer, RunPodPreferences,
    RunPodSelectionOwner, canonical_claim_path, read_runpod_preferences, save_runpod_preferences,
)
from astrid.packs.runpod.worker_preparation import AcquisitionUnresolvedError, target_owner


@pytest.fixture
def case(tmp_path, monkeypatch):
    import runpod_lifecycle
    from runpod_lifecycle import api

    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    raw = {
        "provider_account_ref": "account-a", "gpu_types": ["preferred-gpu", "fallback-gpu"],
        "image": "worker@sha256:" + "a" * 64,
        "volume": {"id": "volume-a", "name": "storage", "size_gb": 100, "mount_path": "/workspace"},
        "idle_timeout_seconds": 60, "credential_ref": "env:FAKE_RUNPOD_CREDENTIAL",
    }
    prefs = RunPodPreferences.from_mapping(raw)
    offer = RunPodOffer("account-a", "preferred-gpu", 1.0, "offer-1")
    state = SimpleNamespace(
        now=now, raw=raw, prefs=prefs, offer=offer, creates=0, mounts="/workspace", rate=1.0,
        pods=[], lookup_error=None, create_error=None, configs=0, before_create=None,
    )

    async def launch(config, *, name, **kwargs):
        if state.before_create:
            state.before_create()
        state.creates += 1
        record = {
            "id": f"pod-{state.creates}", "name": name, "desiredStatus": "RUNNING",
            "actualStatus": "RUNNING", "machineType": state.offer.gpu_type,
            "imageName": prefs.image, "networkVolumeId": prefs.volume_id,
        }
        state.pods.append(record)
        if state.create_error:
            raise state.create_error
        async def ready(**kwargs):
            pass
        return SimpleNamespace(id=record["id"], name=name, wait_ready=ready)

    async def get_pod(pod_id, config, *, name):
        async def ready(**kwargs):
            pass
        return SimpleNamespace(id=pod_id, name=name, wait_ready=ready)

    def pods(key):
        if state.lookup_error:
            raise state.lookup_error
        return list(state.pods)

    def volumes(key):
        return [{"id": prefs.volume_id, "name": prefs.volume_name, "size": prefs.volume_size_gb,
                 "dataCenterId": None}]

    def config(offer):
        state.configs += 1
        selected = state.owner.preferences
        return SimpleNamespace(api_key="fake-only", gpu_type_candidates=(offer.gpu_type,),
                               worker_image=prefs.image, volume_mount_path=prefs.mount_path,
                               network_volume_id=selected.volume_id,
                               data_center_id=offer.datacenter_id or selected.datacenter_id,
                               gpu_type_id=offer.gpu_type_id)

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", launch)
    monkeypatch.setattr(runpod_lifecycle, "get_pod", get_pod)
    monkeypatch.setattr(api, "list_pods", pods)
    monkeypatch.setattr(api, "get_network_volumes", volumes)
    monkeypatch.setattr(selection, "read_exact_pod", lambda key, pod: (
        200, {"id": pod, "volumeMountPath": state.mounts, "networkVolumeId": prefs.volume_id},
    ))
    state.owner = RunPodSelectionOwner(
        prefs, account_ref="account-a", config_factory=config,
        observe_offer=lambda: state.offer, observed_hourly_usd=lambda pod: state.rate,
        data_root=tmp_path / "machine", clock=lambda: state.now,
    )
    state.grant = PaidRunConsent(
        "invocation-1", offer.digest(prefs), 1.0, (now + timedelta(hours=1)).isoformat(),
        ("paid",), 30, 30, 60,
    )
    return state


def test_preferences_use_independent_f02_writer_and_preserve_human_default(case, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[comfyui]\nurl="http://127.0.0.1:8188"\nowner="human"\n')
    save_runpod_preferences(case.raw, path=path)
    assert read_runpod_preferences(path=path) == case.prefs
    from astrid.core.generation.targets import read_target
    human = read_target(path=path)
    assert human.owner == "human"
    assert human.endpoint == "http://127.0.0.1:8188"
    assert "current" not in path.read_text()


def test_canonical_account_path_and_dry_run_make_no_writes(case):
    path = case.owner.path
    assert path == canonical_claim_path("account-a", data_root=path.parents[2])
    assert path != canonical_claim_path("account-b", data_root=path.parents[2])
    result = asyncio.run(case.owner.select(case.grant, dry_run=True))
    assert result["offer"]["hourly_usd"] == 1.0
    assert not path.parent.exists()
    assert case.creates == case.configs == 0


@pytest.mark.parametrize("change", [
    {"network_volume_id": "foreign"}, {"data_center_id": "other"},
    {"gpu_type_id": "other-gpu"}, {"storage_name": "storage"},
    {"storage_volumes": ("storage",)},
])
def test_config_cannot_substitute_accepted_placement_before_create(case, change):
    config = case.owner.config_factory(case.offer)
    case.owner.config_factory = lambda offer: SimpleNamespace(**{**vars(config), **change})
    with pytest.raises(PaidSelectionError, match="consented exact placement"):
        asyncio.run(case.owner.select(case.grant))
    assert not case.owner.path.exists() and case.creates == 0


@pytest.mark.parametrize("change", [
    {"consent": ()}, {"max_usd": None}, {"max_usd": 0}, {"max_usd": float("nan")},
    {"max_usd": 0.001}, {"deadline": "2026-11-01T00:00:01Z"},
    {"deadline": "2026-11-01T03:00:00+03:00"}, {"deadline": "2026-11-01T01:00:00"},
    {"idle_seconds": -1}, {"offer_digest": "stale-offer"},
])
def test_denial_cap_deadline_and_changed_offer_block_before_claim_or_journal(case, change):
    with pytest.raises(PaidSelectionError):
        asyncio.run(case.owner.select(replace(case.grant, **change)))
    assert not case.owner.path.exists()
    assert case.creates == case.configs == 0


def test_claim_namespaces_intent_before_provider_and_reuses_only_exact_journal(case):
    def before_create():
        journal = json.loads(case.owner.path.read_text())
        assert journal["state"] == "allocation_pending"
        assert journal["request_name"] == f'astrid-product-{journal["operation_id"]}'
        assert journal["paid_limits"]["max_usd"] == 1.0
        with pytest.raises(AcquisitionUnresolvedError, match="another process"):
            with target_owner(case.owner.path):
                pytest.fail("concurrent claimant acquired the owner lock")
    case.before_create = before_create
    first = asyncio.run(case.owner.select(case.grant))
    assert first["pod_id"] == "pod-1"
    assert first["provider_account_ref"] == "account-a"
    second = asyncio.run(case.owner.select(replace(case.grant, invocation_id="invocation-2")))
    assert second["pod_id"] == "pod-1"
    assert case.creates == 1


def test_denied_reuse_does_not_change_custody_or_extend_hold(case):
    asyncio.run(case.owner.select(case.grant))
    before = case.owner.path.read_bytes()
    with pytest.raises(PaidSelectionError):
        asyncio.run(case.owner.select(replace(case.grant, invocation_id="denied", consent=())))
    assert case.owner.path.read_bytes() == before
    with pytest.raises(PaidSelectionError, match="another claim or reuse"):
        asyncio.run(case.owner.select(case.grant))
    assert case.owner.path.read_bytes() == before
    case.now += timedelta(hours=2)
    with pytest.raises(PaidSelectionError):
        asyncio.run(case.owner.select(replace(case.grant, deadline=(case.now + timedelta(hours=1)).isoformat())))
    assert case.owner.path.read_bytes() == before


def test_fallback_offer_requires_its_own_disclosed_price_and_consent(case):
    case.offer = RunPodOffer("account-a", "fallback-gpu", 2.0, "fallback-offer")
    case.rate = 2.0
    with pytest.raises(PaidSelectionError, match="offer or rate changed"):
        asyncio.run(case.owner.select(case.grant))
    assert case.creates == 0
    selected = asyncio.run(case.owner.select(replace(case.grant, offer_digest=case.offer.digest(case.prefs))))
    assert selected["gpu_type"] == "fallback-gpu"
    assert selected["paid_limits"]["hourly_usd"] == 2.0


@pytest.mark.parametrize("field,value", [
    ("provider_account_ref", "other-account"), ("worker_image", "other-image"),
    ("volume_mount_path", "/other"), ("network_volume_id", "other-volume"),
    ("request_name", "other-broker"), ("acquisition_mode", "attach"),
])
def test_other_brokers_or_changed_journal_identity_are_never_adopted(case, field, value):
    record = dict(asyncio.run(case.owner.select(case.grant)))
    record[field] = value
    case.owner.path.write_text(json.dumps(record))
    before = case.owner.path.read_bytes()
    with pytest.raises(AcquisitionUnresolvedError, match="exact account/pod/image/volume custody"):
        asyncio.run(case.owner.select(replace(case.grant, invocation_id="another")))
    assert case.creates == 1
    assert case.owner.path.read_bytes() == before


@pytest.mark.parametrize("rate,mount", [(None, "/workspace"), (2.0, "/workspace"), (1.0, "/wrong")])
def test_unconfirmed_actual_price_or_mount_retains_cleanup_pending(case, rate, mount):
    case.rate, case.mounts = rate, mount
    with pytest.raises(PaidSelectionError, match="requires cleanup"):
        asyncio.run(case.owner.select(case.grant))
    record = json.loads(case.owner.path.read_text())
    assert record["state"] == "cleanup_pending"
    assert record["pod_id"] == "pod-1"
    with pytest.raises(AcquisitionUnresolvedError, match="prevents admission"):
        asyncio.run(case.owner.select(replace(case.grant, invocation_id="again")))
    assert case.creates == 1


def test_restart_reconciles_lost_create_by_journal_name_without_another_claim(case):
    case.create_error = RuntimeError("lost create response")
    with pytest.raises(AcquisitionUnresolvedError, match="remains unresolved"):
        asyncio.run(case.owner.select(case.grant))
    assert json.loads(case.owner.path.read_text())["state"] == "allocation_pending"
    case.create_error = None
    restored = asyncio.run(case.owner.select(replace(case.grant, invocation_id="restart")))
    assert restored["pod_id"] == "pod-1"
    assert case.creates == 1


def test_ambiguous_orphan_names_and_legacy_journals_block_replacement(case):
    case.create_error = RuntimeError("lost")
    with pytest.raises(AcquisitionUnresolvedError):
        asyncio.run(case.owner.select(case.grant))
    case.create_error = None
    case.pods.append({**case.pods[0], "id": "other-broker-copy"})
    with pytest.raises(AcquisitionUnresolvedError):
        asyncio.run(case.owner.select(replace(case.grant, invocation_id="restart")))
    assert case.creates == 1
    legacy = case.owner.path.parent / "claim-legacy.json"
    legacy.write_text("{}")
    with pytest.raises(AcquisitionUnresolvedError, match="legacy"):
        asyncio.run(case.owner.select(replace(case.grant, invocation_id="legacy")))


def test_preferences_reject_secret_bytes_without_copying_them_to_errors(case):
    for change in ({"provider_account_ref": "rpa_fake_secret"},
                   {"credential_ref": "sk-fake-secret"}):
        with pytest.raises(PaidSelectionError) as error:
            RunPodPreferences.from_mapping({**case.raw, **change})
        assert "fake_secret" not in str(error.value)
        assert "sk-fake-secret" not in str(error.value)
    assert not case.owner.path.exists()


def test_provider_exception_bytes_are_redacted_and_do_not_enable_replacement(case):
    case.create_error = RuntimeError("rpa_fake_secret")
    with pytest.raises(AcquisitionUnresolvedError) as error:
        asyncio.run(case.owner.select(case.grant))
    assert "rpa_fake_secret" not in str(error.value)
    assert "rpa_fake_secret" not in case.owner.path.read_text()
    assert json.loads(case.owner.path.read_text())["state"] == "allocation_pending"
    assert case.creates == 1


def test_wrong_provider_volume_blocks_before_create(case, monkeypatch):
    from runpod_lifecycle import api
    monkeypatch.setattr(api, "get_network_volumes", lambda key: [])
    with pytest.raises(AcquisitionUnresolvedError, match="volume cannot be confirmed"):
        asyncio.run(case.owner.select(case.grant))
    assert case.creates == 0
    assert not case.owner.path.exists()


def test_default_idle_and_explicit_optional_volume(tmp_path):
    from astrid.core.execution.warm_hold import DEFAULT_IDLE_SECONDS
    raw = {"provider_account_ref": "account-a", "gpu_types": ["gpu"],
           "image": "worker@sha256:" + "a" * 64, "volume": None}
    preferences = RunPodPreferences.from_mapping(raw)
    assert preferences.idle_timeout_seconds == DEFAULT_IDLE_SECONDS == 1800
    assert RunPodPreferences.from_mapping(preferences.as_mapping()) == preferences
    path = tmp_path / "config.toml"
    save_runpod_preferences(raw, path=path)
    assert read_runpod_preferences(path=path) == preferences
    assert "volume" not in path.read_text()


def test_initial_intent_directory_is_flushed_before_create(case, monkeypatch):
    from astrid.packs.runpod import worker_preparation
    flushed = []
    real = worker_preparation.platform.fsync_directory

    def fsync(path):
        flushed.append(path)
        return real(path)

    monkeypatch.setattr(worker_preparation.platform, "fsync_directory", fsync)
    case.before_create = lambda: pytest.fail("intent directory was not flushed") if case.owner.path.parent not in flushed else None
    asyncio.run(case.owner.select(case.grant))
    assert case.creates == 1


def test_symlink_account_lock_refused_before_create(case):
    case.owner.path.parent.mkdir(parents=True)
    other = case.owner.path.parent / "foreign-lock"
    other.write_text("foreign")
    case.owner.path.with_name(".claim.json.owner.lock").symlink_to(other)
    with pytest.raises(AcquisitionUnresolvedError, match="opened safely"):
        asyncio.run(case.owner.select(case.grant))
    assert case.creates == 0 and not case.owner.path.exists()
    assert other.read_text() == "foreign"


def test_failed_intent_flush_keeps_custody_and_never_rents(case, monkeypatch):
    from astrid.packs.runpod import worker_preparation

    def failed_flush(path):
        raise OSError("simulated directory flush failure")

    monkeypatch.setattr(worker_preparation.platform, "fsync_directory", failed_flush)
    with pytest.raises(OSError):
        asyncio.run(case.owner.select(case.grant))
    assert case.creates == 0
    assert json.loads(case.owner.path.read_text())["state"] == "allocation_ready"


def test_no_volume_legacy_worker_route_refuses_before_renting(case):
    preferences = RunPodPreferences.from_mapping({**case.raw, "volume": None})
    case.owner.preferences = preferences
    with pytest.raises(PaidSelectionError, match="retained lease owner"):
        asyncio.run(case.owner.select(replace(case.grant, offer_digest=case.offer.digest(preferences))))
    assert case.creates == case.configs == 0 and not case.owner.path.exists()
