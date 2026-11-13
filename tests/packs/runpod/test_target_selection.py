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
        "budget_cap_usd": 10.0,
        "volume": {"id": "volume-a", "name": "storage", "size_gb": 100, "mount_path": "/workspace"},
        "idle_timeout_seconds": 60, "credential_ref": "env:RUNPOD_RUNPOD_FAKE_CREDENTIAL",
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
    monkeypatch.setattr("astrid.packs.runpod.worker_preparation._get_provider_pod", get_pod)
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
    save_runpod_preferences(case.raw)
    assert read_runpod_preferences() == case.prefs
    from astrid.core.generation.targets import read_target
    human = read_target(path=path)
    assert human.owner == "human"
    assert human.endpoint == "http://127.0.0.1:8188"
    assert "current" not in path.read_text()


def test_canonical_account_path_and_dry_run_make_no_writes(case):
    path = case.owner.path
    assert path == canonical_claim_path("account-a", data_root=path.parents[2])
    assert path != canonical_claim_path("account-b", data_root=path.parents[2])
    result = asyncio.run(case.owner._select(case.grant, dry_run=True))
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
        asyncio.run(case.owner._select(case.grant))
    assert not case.owner.path.exists() and case.creates == 0


@pytest.mark.parametrize("change", [
    {"consent": ()}, {"max_usd": None}, {"max_usd": 0}, {"max_usd": float("nan")},
    {"max_usd": 0.001}, {"deadline": "2026-11-01T00:00:01Z"},
    {"deadline": "2026-11-01T03:00:00+03:00"}, {"deadline": "2026-11-01T01:00:00"},
    {"idle_seconds": -1}, {"offer_digest": "stale-offer"},
])
def test_denial_cap_deadline_and_changed_offer_block_before_claim_or_journal(case, change):
    with pytest.raises(PaidSelectionError):
        asyncio.run(case.owner._select(replace(case.grant, **change)))
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
    first = asyncio.run(case.owner._select(case.grant))
    assert first["pod_id"] == "pod-1"
    assert first["provider_account_ref"] == "account-a"
    second = asyncio.run(case.owner._select(replace(case.grant, invocation_id="invocation-2")))
    assert second["pod_id"] == "pod-1"
    assert case.creates == 1


def test_denied_reuse_does_not_change_custody_or_extend_hold(case):
    asyncio.run(case.owner._select(case.grant))
    before = case.owner.path.read_bytes()
    with pytest.raises(PaidSelectionError):
        asyncio.run(case.owner._select(replace(case.grant, invocation_id="denied", consent=())))
    assert case.owner.path.read_bytes() == before
    with pytest.raises(PaidSelectionError, match="another claim or reuse"):
        asyncio.run(case.owner._select(case.grant))
    assert case.owner.path.read_bytes() == before
    case.now += timedelta(hours=2)
    with pytest.raises(PaidSelectionError):
        asyncio.run(case.owner._select(replace(case.grant, deadline=(case.now + timedelta(hours=1)).isoformat())))
    assert case.owner.path.read_bytes() == before


def test_fallback_offer_requires_its_own_disclosed_price_and_consent(case):
    case.offer = RunPodOffer("account-a", "fallback-gpu", 2.0, "fallback-offer")
    case.rate = 2.0
    with pytest.raises(PaidSelectionError, match="offer or rate changed"):
        asyncio.run(case.owner._select(case.grant))
    assert case.creates == 0
    selected = asyncio.run(case.owner._select(replace(case.grant, offer_digest=case.offer.digest(case.prefs))))
    assert selected["gpu_type"] == "fallback-gpu"
    assert selected["paid_limits"]["hourly_usd"] == 2.0


@pytest.mark.parametrize("field,value", [
    ("provider_account_ref", "other-account"), ("worker_image", "other-image"),
    ("volume_mount_path", "/other"), ("network_volume_id", "other-volume"),
    ("request_name", "other-broker"), ("acquisition_mode", "attach"),
])
def test_other_brokers_or_changed_journal_identity_are_never_adopted(case, field, value):
    record = dict(asyncio.run(case.owner._select(case.grant)))
    record[field] = value
    case.owner.path.write_text(json.dumps(record))
    before = case.owner.path.read_bytes()
    with pytest.raises(AcquisitionUnresolvedError, match="exact account/pod/image/volume custody"):
        asyncio.run(case.owner._select(replace(case.grant, invocation_id="another")))
    assert case.creates == 1
    assert case.owner.path.read_bytes() == before


@pytest.mark.parametrize("rate,mount", [(None, "/workspace"), (2.0, "/workspace"), (1.0, "/wrong")])
def test_unconfirmed_actual_price_or_mount_retains_cleanup_pending(case, rate, mount):
    case.rate, case.mounts = rate, mount
    with pytest.raises(PaidSelectionError, match="requires cleanup"):
        asyncio.run(case.owner._select(case.grant))
    record = json.loads(case.owner.path.read_text())
    assert record["state"] == "cleanup_pending"
    assert record["pod_id"] == "pod-1"
    with pytest.raises(AcquisitionUnresolvedError, match="prevents admission"):
        asyncio.run(case.owner._select(replace(case.grant, invocation_id="again")))
    assert case.creates == 1


def test_restart_reconciles_lost_create_by_journal_name_without_another_claim(case):
    case.create_error = RuntimeError("lost create response")
    with pytest.raises(AcquisitionUnresolvedError, match="remains unresolved"):
        asyncio.run(case.owner._select(case.grant))
    assert json.loads(case.owner.path.read_text())["state"] == "allocation_pending"
    case.create_error = None
    restored = asyncio.run(case.owner._select(replace(case.grant, invocation_id="restart")))
    assert restored["pod_id"] == "pod-1"
    assert case.creates == 1


def test_ambiguous_orphan_names_and_legacy_journals_block_replacement(case):
    case.create_error = RuntimeError("lost")
    with pytest.raises(AcquisitionUnresolvedError):
        asyncio.run(case.owner._select(case.grant))
    case.create_error = None
    case.pods.append({**case.pods[0], "id": "other-broker-copy"})
    with pytest.raises(AcquisitionUnresolvedError):
        asyncio.run(case.owner._select(replace(case.grant, invocation_id="restart")))
    assert case.creates == 1
    legacy = case.owner.path.parent / "claim-legacy.json"
    legacy.write_text("{}")
    with pytest.raises(AcquisitionUnresolvedError, match="legacy"):
        asyncio.run(case.owner._select(replace(case.grant, invocation_id="legacy")))


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
        asyncio.run(case.owner._select(case.grant))
    assert "rpa_fake_secret" not in str(error.value)
    assert "rpa_fake_secret" not in case.owner.path.read_text()
    assert json.loads(case.owner.path.read_text())["state"] == "allocation_pending"
    assert case.creates == 1


def test_wrong_provider_volume_blocks_before_create(case, monkeypatch):
    from runpod_lifecycle import api
    monkeypatch.setattr(api, "get_network_volumes", lambda key: [])
    with pytest.raises(AcquisitionUnresolvedError, match="volume cannot be confirmed"):
        asyncio.run(case.owner._select(case.grant))
    assert case.creates == 0
    assert not case.owner.path.exists()


@pytest.mark.parametrize("capacity_failures", [3, 20])
def test_bounded_capacity_retries_use_real_lifecycle_and_fake_provider(case, monkeypatch, capacity_failures):
    import runpod_lifecycle
    from runpod_lifecycle import RunPodConfig, lifecycle
    from runpod_lifecycle.errors import ProviderCapacityUnavailable

    elapsed = [0.0]
    starts = []
    sleeps = []

    def create(**kwargs):
        starts.append(elapsed[0])
        assert json.loads(case.owner.path.read_text())["create_attempts"] == len(starts)
        if len(starts) <= capacity_failures:
            raise ProviderCapacityUnavailable(200, "no instances")
        case.pods.append({"id": "pod-1", "name": kwargs["name"], "desiredStatus": "RUNNING",
                          "actualStatus": "RUNNING", "machineType": case.offer.gpu_type,
                          "imageName": case.prefs.image, "networkVolumeId": case.prefs.volume_id})
        return {"id": "pod-1"}

    async def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds
        case.now += timedelta(seconds=seconds)

    async def ready(self, **kwargs):
        pass

    case.owner.config_factory = lambda offer: RunPodConfig(
        api_key="fake-only", gpu_type=offer.gpu_type, worker_image=case.prefs.image,
        volume_mount_path=case.prefs.mount_path, network_volume_id=case.prefs.volume_id,
        data_center_id=offer.datacenter_id or case.prefs.datacenter_id, gpu_type_id=offer.gpu_type_id,
        ram_tiers_enabled=True, attach_only=True,
    )
    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", lifecycle.launch_when_available)
    monkeypatch.setattr(lifecycle, "find_gpu_type", lambda *args: {"id": "gpu-1"})
    monkeypatch.setattr(lifecycle, "get_storage_volume_id", lambda key, name: case.prefs.volume_id)
    monkeypatch.setattr(lifecycle, "create_pod", create)
    monkeypatch.setattr(lifecycle.Pod, "wait_ready", ready)
    case.owner.sleep = sleep
    case.owner.monotonic = lambda: elapsed[0]
    monkeypatch.setattr(selection.random, "uniform", lambda *args: 0)
    case.grant = replace(case.grant, capacity_timeout_seconds=20)
    if capacity_failures < 10:
        result = asyncio.run(case.owner._select(case.grant))
        assert result["pod_id"] == "pod-1"
        assert len(starts) == 4
    else:
        with pytest.raises(selection.CapacityWaitTimeout, match="capacity timeout"):
            asyncio.run(case.owner._select(case.grant))
        assert len(starts) == 4
        record = json.loads(case.owner.path.read_text())
        assert record["state"] == "capacity_retry_ready"
        assert record["create_attempts"] == 4
        assert record["capacity_rejection"] == "ProviderCapacityUnavailable"
    assert sleeps == [5] * (3 if capacity_failures < 10 else 4)
    assert [b - a for a, b in zip(starts, starts[1:])] == [5, 5, 5]


def test_bounded_capacity_retries_never_repeat_ambiguous_post(case, monkeypatch):
    import runpod_lifecycle
    from runpod_lifecycle import RunPodConfig, lifecycle

    starts = []
    def create(**kwargs):
        starts.append(kwargs["name"])
        raise TimeoutError("lost response")

    case.owner.config_factory = lambda offer: RunPodConfig(
        api_key="fake-only", gpu_type=offer.gpu_type, worker_image=case.prefs.image,
        volume_mount_path=case.prefs.mount_path, network_volume_id=case.prefs.volume_id,
        data_center_id=offer.datacenter_id or case.prefs.datacenter_id, gpu_type_id=offer.gpu_type_id,
        attach_only=True,
    )
    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", lifecycle.launch_when_available)
    monkeypatch.setattr(lifecycle, "find_gpu_type", lambda *args: {"id": "gpu-1"})
    monkeypatch.setattr(lifecycle, "get_storage_volume_id", lambda key, name: case.prefs.volume_id)
    monkeypatch.setattr(lifecycle, "create_pod", create)
    with pytest.raises(AcquisitionUnresolvedError):
        asyncio.run(case.owner._select(case.grant))
    assert len(starts) == 1
    record = json.loads(case.owner.path.read_text())
    assert record["state"] == "allocation_unknown"
    assert record["create_attempts"] == 1


def test_preferences_default_idle_uses_shared_warm_hold_default(case):
    from astrid.core.execution.warm_hold import DEFAULT_IDLE_SECONDS

    raw = {key: value for key, value in case.raw.items() if key != "idle_timeout_seconds"}
    parsed = RunPodPreferences.from_mapping(raw)
    direct = RunPodPreferences(case.prefs.provider_account_ref, case.prefs.gpu_types, case.prefs.image,
                               case.prefs.volume_id, case.prefs.volume_name, case.prefs.volume_size_gb,
                               case.prefs.mount_path)
    assert DEFAULT_IDLE_SECONDS == 1800
    assert parsed.idle_timeout_seconds == direct.idle_timeout_seconds == DEFAULT_IDLE_SECONDS
    assert RunPodPreferences.from_mapping(parsed.as_mapping()) == parsed
def test_default_idle_and_explicit_optional_volume(generation_settings_store):
    from astrid.core.execution.warm_hold import DEFAULT_IDLE_SECONDS
    raw = {"provider_account_ref": "account-a", "gpu_types": ["gpu"],
           "image": "worker@sha256:" + "a" * 64, "volume": None, "budget_cap_usd": 5.0}
    preferences = RunPodPreferences.from_mapping(raw)
    assert preferences.idle_timeout_seconds == DEFAULT_IDLE_SECONDS == 1800
    assert RunPodPreferences.from_mapping(preferences.as_mapping()) == preferences
    save_runpod_preferences(raw)
    assert read_runpod_preferences() == preferences
    stored, _ = generation_settings_store.documents["generation"]
    assert "volume" not in stored["machines"]["runpod"]


def test_initial_intent_directory_is_flushed_before_create(case, monkeypatch):
    from astrid.packs.runpod import worker_preparation
    flushed = []
    real = worker_preparation.platform.fsync_directory

    def fsync(path):
        flushed.append(path)
        return real(path)

    monkeypatch.setattr(worker_preparation.platform, "fsync_directory", fsync)
    case.before_create = lambda: pytest.fail("intent directory was not flushed") if case.owner.path.parent not in flushed else None
    asyncio.run(case.owner._select(case.grant))
    assert case.creates == 1


def test_symlink_account_lock_refused_before_create(case):
    case.owner.path.parent.mkdir(parents=True)
    other = case.owner.path.parent / "foreign-lock"
    other.write_text("foreign")
    case.owner.path.with_name(".claim.json.owner.lock").symlink_to(other)
    with pytest.raises(AcquisitionUnresolvedError, match="opened safely"):
        asyncio.run(case.owner._select(case.grant))
    assert case.creates == 0 and not case.owner.path.exists()
    assert other.read_text() == "foreign"


def test_failed_intent_flush_keeps_custody_and_never_rents(case, monkeypatch):
    from astrid.packs.runpod import worker_preparation

    def failed_flush(path):
        raise OSError("simulated directory flush failure")

    monkeypatch.setattr(worker_preparation.platform, "fsync_directory", failed_flush)
    with pytest.raises(OSError):
        asyncio.run(case.owner._select(case.grant))
    assert case.creates == 0
    assert json.loads(case.owner.path.read_text())["state"] == "allocation_ready"


def test_no_volume_legacy_worker_route_refuses_before_renting(case):
    preferences = RunPodPreferences.from_mapping({**case.raw, "volume": None})
    case.owner.preferences = preferences
    with pytest.raises(PaidSelectionError, match="retained lease owner"):
        asyncio.run(case.owner._select(replace(case.grant, offer_digest=case.offer.digest(preferences))))
    assert case.creates == case.configs == 0 and not case.owner.path.exists()


def test_mismatched_digest_refuses_before_any_claim_or_pod_create(case, monkeypatch):
    def unreachable(*args, **kwargs):
        raise AssertionError("claim or pod create reached before consent validation")

    monkeypatch.setattr(selection, "create_target_record_exclusive", unreachable)
    monkeypatch.setattr(selection, "target_owner", unreachable)
    monkeypatch.setattr(selection.acquire_target, "__wrapped__", unreachable)
    case.owner.config_factory = unreachable
    with pytest.raises(PaidSelectionError, match="offer or rate changed"):
        asyncio.run(case.owner._select(replace(case.grant, offer_digest="sha256:" + "9" * 64)))
    assert not case.owner.path.exists() and case.creates == case.configs == 0


def test_per_job_gpu_override_refuses_a_digest_from_the_saved_gpu_list(case):
    per_job = RunPodPreferences.from_mapping({**case.raw, "gpu_types": ["RTX 4090"]})
    assert per_job.gpu_types == ("RTX 4090",)
    case.offer = RunPodOffer("account-a", "RTX 4090", 1.0, "offer-1")
    assert case.grant.offer_digest != case.offer.digest(per_job)
    case.owner.preferences = per_job

    with pytest.raises(PaidSelectionError, match="offer or rate changed"):
        asyncio.run(case.owner._select(case.grant))

    assert case.creates == case.configs == 0 and not case.owner.path.exists()


def waiting_grant(case, **changes):
    return replace(case.grant, **{"offer_digest": None, "deadline": None,
                   "policy_digest": selection.policy_consent_digest(case.owner.preferences, 1.5),
                   "max_hourly_usd": 1.5, "capacity_timeout_seconds": 30, **changes})


def fake_capacity_clock(case, monkeypatch):
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        case.now += timedelta(seconds=seconds)

    case.owner.sleep = sleep
    case.owner.monotonic = lambda: case.now.timestamp()
    monkeypatch.setattr(selection.random, "uniform", lambda *args: 0)
    return sleeps


def capacity_offer_provider(case, monkeypatch, request, *, rejections=0, ambiguous=False):
    """Real journaled acquisition with only POST and provider reads faked."""
    import runpod_lifecycle
    from runpod_lifecycle.errors import ProviderCapacityUnavailable
    from astrid.packs.runpod import worker_preparation
    from astrid.packs.runpod.worker_owner import RunPodLeaseOwner
    from astrid.core.execution.warm_hold import WarmHolds

    case.owner.preferences = RunPodPreferences.from_mapping({**case.raw, "volume": None})
    factory = case.owner.config_factory
    attempts = []

    def config(offer):
        case.rate = offer.hourly_usd
        return factory(offer)

    async def launch(config, *, name, **kwargs):
        attempts.append((config.gpu_type_candidates[0], config.data_center_id))
        case.creates += 1
        journal = json.loads(case.owner.path.read_text())
        assert journal["state"] == "allocation_pending"
        assert journal["consented_offer"]["datacenter_id"] == config.data_center_id
        if len(attempts) <= rejections:
            raise runpod_lifecycle.LaunchFailure("fake definite capacity rejection") from ProviderCapacityUnavailable(503, "fake capacity")
        pod = {"id": "pod-1", "name": name, "desiredStatus": "RUNNING", "actualStatus": "RUNNING",
               "machineType": config.gpu_type_candidates[0], "imageName": config.worker_image,
               "networkVolumeId": config.network_volume_id, "volumeMountPath": config.volume_mount_path}
        case.pods.append(pod)
        if ambiguous:
            raise TimeoutError("fake lost POST response")

        async def ready(**kwargs):
            pass

        return SimpleNamespace(id=pod["id"], name=name, wait_ready=ready)

    def exact(key, pod_id):
        pod = next((pod for pod in case.pods if pod["id"] == pod_id), None)
        return (200, dict(pod)) if pod is not None else (404, None)

    case.owner.config_factory = config
    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", launch)
    monkeypatch.setattr(selection, "read_exact_pod", exact)
    monkeypatch.setattr(worker_preparation, "read_exact_pod", exact)
    case.owner.compatibility = selection.RunPodLeaseCompatibility(
        "sha256:" + "a" * 64, "sha256:" + "b" * 64, "a" * 40, "b" * 40).as_mapping()

    def timer(seconds, callback):
        handle = SimpleNamespace(cancelled=False)
        handle.cancel = lambda: setattr(handle, "cancelled", True)
        return handle

    clock = lambda: case.now.timestamp()
    owner = RunPodLeaseOwner(case.owner, holds=WarmHolds(clock=clock, timer=timer),
                            quiesce=lambda record: None, clock=clock, timer=timer, environ={})
    request.addfinalizer(owner.close)
    return attempts, owner


def test_definite_capacity_rejection_rotates_datacenters_without_reobserving(case, monkeypatch, request):
    attempts, owner = capacity_offer_provider(case, monkeypatch, request, rejections=1)
    sleeps = fake_capacity_clock(case, monkeypatch)
    offers = (replace(case.offer, datacenter_id="DC-A", hourly_usd=1.0),
              replace(case.offer, datacenter_id="DC-B", hourly_usd=1.1))
    observations = []
    case.owner.observe_offers = lambda: (observations.append(1) or offers)
    result = owner.acquire(waiting_grant(case), requested_idle_seconds=0).claim
    assert attempts == [("preferred-gpu", "DC-A"), ("preferred-gpu", "DC-B")]
    assert len(observations) == 1 and sleeps == [5]
    assert result["network_volume_datacenter_id"] == "DC-B" and case.creates == 2


def test_exhausted_offer_list_reobserves_and_restarts_the_user_gpu_order(case, monkeypatch, request):
    attempts, owner = capacity_offer_provider(case, monkeypatch, request, rejections=3)
    sleeps = fake_capacity_clock(case, monkeypatch)
    preferred_a = replace(case.offer, datacenter_id="DC-A", hourly_usd=1.0)
    preferred_b = replace(case.offer, datacenter_id="DC-B", hourly_usd=1.1)
    cheaper_fallback = replace(case.offer, gpu_type="fallback-gpu", datacenter_id="DC-C", hourly_usd=0.5)
    observations = []

    def observe():
        observations.append(1)
        return (cheaper_fallback, preferred_b, preferred_a)

    case.owner.observe_offers = observe
    owner.acquire(waiting_grant(case), requested_idle_seconds=0)
    assert attempts == [("preferred-gpu", "DC-A"), ("preferred-gpu", "DC-B"),
                        ("fallback-gpu", "DC-C"), ("preferred-gpu", "DC-A")]
    assert len(observations) == 2 and sleeps == [5, 5, 5] and case.creates == 4


def test_ambiguous_create_never_advances_to_another_observed_offer(case, monkeypatch, request):
    attempts, owner = capacity_offer_provider(case, monkeypatch, request, ambiguous=True)
    case.owner.observe_offers = lambda: (replace(case.offer, datacenter_id="DC-A"),
                                         replace(case.offer, datacenter_id="DC-B"))
    with pytest.raises(AcquisitionUnresolvedError, match="remains unresolved"):
        owner.acquire(waiting_grant(case), requested_idle_seconds=0)
    assert attempts == [("preferred-gpu", "DC-A")] and case.creates == 1
    assert json.loads(case.owner.path.read_text())["state"] == "allocation_pending"


def test_rotated_offer_still_requires_the_same_policy_hourly_ceiling(case, monkeypatch, request):
    attempts, owner = capacity_offer_provider(case, monkeypatch, request, rejections=1)
    fake_capacity_clock(case, monkeypatch)
    case.owner.observe_offers = lambda: (replace(case.offer, datacenter_id="DC-A", hourly_usd=1.0),
                                         replace(case.offer, datacenter_id="DC-B", hourly_usd=2.0))
    with pytest.raises(PaidSelectionError, match="hourly ceiling"):
        owner.acquire(waiting_grant(case), requested_idle_seconds=0)
    assert attempts == [("preferred-gpu", "DC-A")] and case.creates == 1


def test_policy_wait_observes_stock_again_and_starts_paid_clock_at_post(case, monkeypatch):
    sleeps = fake_capacity_clock(case, monkeypatch)
    attempts = []
    initial = case.now

    def observe():
        attempts.append(case.now)
        assert not case.owner.path.exists()
        if len(attempts) < 3:
            raise selection.OfferObservationError("stock_unavailable")
        return case.offer

    case.owner.observe_offer = observe
    selected = asyncio.run(case.owner._select(waiting_grant(case)))
    assert len(attempts) == 3 and sleeps == [5, 5] and case.creates == 1
    assert selected["paid_limits"]["authorized_at"] == (initial + timedelta(seconds=10)).isoformat()
    assert selection._utc(selected["paid_limits"]["expires_at"]) == initial + timedelta(seconds=3610)
    assert selected["consented_offer"]["offer_id"] == case.offer.offer_id


def test_wait_never_refreshes_an_exact_offer_digest(case, monkeypatch):
    fake_capacity_clock(case, monkeypatch)
    calls = []

    def observe():
        calls.append(1)
        if len(calls) == 1:
            raise selection.OfferObservationError("stock_unavailable")
        return replace(case.offer, hourly_usd=1.25, offer_id="changed")

    case.owner.observe_offer = observe
    with pytest.raises(PaidSelectionError, match="offer or rate changed"):
        asyncio.run(case.owner._select(case.grant))
    assert len(calls) == 2 and case.creates == 0 and not case.owner.path.exists()


@pytest.mark.parametrize("change", [{"hourly_usd": 2.0}, {"gpu_type": "not-approved"},
                                     {"datacenter_id": "other-datacenter"}])
def test_waiting_policy_refuses_an_offer_outside_its_approved_policy(case, change):
    case.owner.preferences = replace(case.prefs, datacenter_id="approved-datacenter")
    case.offer = replace(case.offer, **{"datacenter_id": "approved-datacenter", **change})
    with pytest.raises(PaidSelectionError):
        asyncio.run(case.owner._select(waiting_grant(case)))
    assert case.creates == 0 and not case.owner.path.exists()


def test_hourly_ceiling_refusal_names_gpu_cloud_rate_and_next_action(case):
    case.owner.preferences = replace(case.prefs, gpu_types=("RTX 5090",))
    case.offer = replace(case.offer, gpu_type="RTX 5090", hourly_usd=1.39)
    grant = waiting_grant(
        case, max_hourly_usd=1.25,
        policy_digest=selection.policy_consent_digest(case.owner.preferences, 1.25),
    )
    with pytest.raises(PaidSelectionError) as caught:
        asyncio.run(case.owner._select(grant))
    assert str(caught.value) == (
        "RTX 5090 secure is $1.39/h, above your hourly ceiling (--max-hourly-usd 1.25). "
        "Raise --max-hourly-usd to at least 1.39, or choose another GPU."
    )
    assert case.creates == case.configs == 0 and not case.owner.path.exists()


def test_capacity_timeout_and_no_wait_make_no_pod_when_stock_is_absent(case, monkeypatch):
    sleeps = fake_capacity_clock(case, monkeypatch)
    calls = []

    def absent():
        calls.append(1)
        raise selection.OfferObservationError("stock_unavailable")

    case.owner.observe_offer = absent
    with pytest.raises(selection.OfferObservationError):
        asyncio.run(case.owner._select(waiting_grant(case, wait_for_capacity=False)))
    assert len(calls) == 1 and not sleeps
    with pytest.raises(selection.CapacityWaitTimeout, match="capacity timeout"):
        asyncio.run(case.owner._select(waiting_grant(case, capacity_timeout_seconds=12)))
    assert sleeps == [5, 5, 2] and case.creates == 0 and not case.owner.path.exists()


def test_absolute_policy_deadline_stops_capacity_wait_without_a_pod(case, monkeypatch):
    sleeps = fake_capacity_clock(case, monkeypatch)
    case.owner.observe_offer = lambda: (_ for _ in ()).throw(selection.OfferObservationError("stock_unavailable"))
    grant = waiting_grant(case, deadline=(case.now + timedelta(seconds=7)).isoformat(),
                          preparation_seconds=0, execution_seconds=0, idle_seconds=1)
    with pytest.raises(selection.CapacityWaitTimeout, match="paid deadline"):
        asyncio.run(case.owner._select(grant))
    assert sleeps == [5, 2] and case.creates == 0 and not case.owner.path.exists()


def test_capacity_rate_limit_honours_retry_after(case, monkeypatch):
    sleeps = fake_capacity_clock(case, monkeypatch)
    calls = []

    def observe():
        calls.append(1)
        if len(calls) == 1:
            raise selection.OfferObservationError("provider_rate_limited", retry_after=12)
        return case.offer

    case.owner.observe_offer = observe
    asyncio.run(case.owner._select(waiting_grant(case)))
    assert sleeps == [12] and case.creates == 1


def test_capacity_rate_limit_without_metadata_backs_off_and_resets_after_observation(case, monkeypatch):
    sleeps = fake_capacity_clock(case, monkeypatch)
    outcomes = iter(["provider_rate_limited"] * 6 + ["stock_unavailable", "provider_rate_limited", None])

    def observe():
        reason = next(outcomes)
        if reason is not None:
            raise selection.OfferObservationError(reason)
        return case.offer

    case.owner.observe_offer = observe
    asyncio.run(case.owner._select(waiting_grant(case, capacity_timeout_seconds=300)))
    assert sleeps == [5, 10, 20, 40, 60, 60, 5, 5] and case.creates == 1


def test_capacity_wait_retries_unclear_observations_and_resets_after_stock(case, monkeypatch):
    sleeps = fake_capacity_clock(case, monkeypatch)
    outcomes = iter(["stock_unavailable"] * 3 + ["provider_observation_unavailable"] * 2 + [None])
    rows = []
    case.owner.progress = rows.append

    def observe():
        reason = next(outcomes)
        if reason is not None:
            raise selection.OfferObservationError(reason)
        return case.offer

    case.owner.observe_offer = observe
    # 5+5+5+5+10 s of waiting needs more than the helper's 30 s capacity window.
    asyncio.run(case.owner._select(waiting_grant(case, capacity_timeout_seconds=120)))
    assert sleeps == [5, 5, 5, 5, 10]
    assert case.creates == 1
    assert sum("retrying in" in str(row) for row in rows) == 2


def test_capacity_wait_stops_after_nine_consecutive_unclear_observations(case, monkeypatch):
    sleeps = fake_capacity_clock(case, monkeypatch)
    case.owner.observe_offer = lambda: (_ for _ in ()).throw(
        selection.OfferObservationError("provider_observation_unavailable"))
    with pytest.raises(selection.OfferObservationError, match="provider_observation_unavailable"):
        asyncio.run(case.owner._select(waiting_grant(case, capacity_timeout_seconds=600)))
    assert len(sleeps) == 8 and case.creates == 0


def test_capacity_wait_unclear_retries_remain_bounded_by_capacity_timeout(case, monkeypatch):
    sleeps = fake_capacity_clock(case, monkeypatch)
    case.owner.observe_offer = lambda: (_ for _ in ()).throw(
        selection.OfferObservationError("observation_failed"))
    with pytest.raises(selection.CapacityWaitTimeout, match="capacity timeout"):
        asyncio.run(case.owner._select(waiting_grant(case, capacity_timeout_seconds=7)))
    assert sleeps == [5, 2] and case.creates == 0


@pytest.mark.parametrize("wait_for_capacity", [True, False])
def test_capacity_wait_refuses_authentication_failure_and_no_wait_unclear(case, monkeypatch, wait_for_capacity):
    sleeps = fake_capacity_clock(case, monkeypatch)
    reason = "provider_authentication_failed" if wait_for_capacity else "provider_observation_unavailable"
    case.owner.observe_offer = lambda: (_ for _ in ()).throw(selection.OfferObservationError(reason))
    with pytest.raises(selection.OfferObservationError, match=reason):
        asyncio.run(case.owner._select(waiting_grant(case, wait_for_capacity=wait_for_capacity)))
    assert sleeps == [] and case.creates == 0


def test_waiting_policy_cannot_create_on_different_storage(case):
    config = case.owner.config_factory(case.offer)
    case.owner.config_factory = lambda offer: SimpleNamespace(**{**vars(config), "network_volume_id": "other-volume"})
    with pytest.raises(PaidSelectionError, match="exact placement"):
        asyncio.run(case.owner._select(waiting_grant(case)))
    assert case.creates == 0 and not case.owner.path.exists()


@pytest.mark.parametrize("when", ["sleep", "observation"])
def test_capacity_wait_cancellation_before_post_creates_no_pod(case, monkeypatch, when):
    fake_capacity_clock(case, monkeypatch)
    cancelled = [False]

    def observe():
        if when == "observation":
            cancelled[0] = True
            return case.offer
        raise selection.OfferObservationError("stock_unavailable")

    async def sleep(seconds):
        cancelled[0] = True

    case.owner.observe_offer, case.owner.sleep = observe, sleep
    with pytest.raises(selection.CapacityWaitCancelled):
        asyncio.run(case.owner._select(waiting_grant(case), cancelled=lambda: cancelled[0]))
    assert case.creates == 0 and not case.owner.path.exists()


def test_capacity_cancellation_during_post_keeps_exact_journal_for_reconciliation(case, monkeypatch):
    import runpod_lifecycle
    cancelled = [False]

    async def launch(config, *, name, **kwargs):
        case.creates += 1
        case.pods.append({"id": "pod-1", "name": name, "desiredStatus": "RUNNING", "actualStatus": "RUNNING",
                          "machineType": case.offer.gpu_type, "imageName": case.prefs.image,
                          "networkVolumeId": case.prefs.volume_id})
        cancelled[0] = True
        await asyncio.sleep(100)

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", launch)
    with pytest.raises(selection.CapacityWaitCancelled):
        asyncio.run(case.owner._select(waiting_grant(case), cancelled=lambda: cancelled[0]))
    record = json.loads(case.owner.path.read_text())
    assert record["state"] == "allocation_unknown" and record["safe_to_retry"] is False
    restored = asyncio.run(case.owner._select(replace(waiting_grant(case), invocation_id="reconcile")))
    assert restored["pod_id"] == "pod-1" and case.creates == 1


def test_retained_machine_reuse_does_not_require_current_market_stock(case):
    first = asyncio.run(case.owner._select(case.grant))
    case.owner.observe_offer = lambda: (_ for _ in ()).throw(selection.OfferObservationError("stock_unavailable"))
    second = asyncio.run(case.owner._select(replace(case.grant, invocation_id="reuse-with-no-stock")))
    assert second["pod_id"] == first["pod_id"] and case.creates == 1


def test_delayed_post_does_not_restart_the_paid_clock_at_readiness(case):
    initial = case.now
    case.before_create = lambda: setattr(case, "now", case.now + timedelta(seconds=30))
    result = asyncio.run(case.owner._select(waiting_grant(case)))
    assert result["paid_limits"]["authorized_at"] == initial.isoformat()
    assert selection._utc(result["paid_limits"]["expires_at"]) == initial + timedelta(hours=1)


def test_stockless_description_keeps_policy_disclosure_and_exact_quote_unchanged(case, monkeypatch):
    from astrid.packs.runpod.adapter import RunPodAdapter
    from astrid.sdk import generation
    from dataclasses import asdict

    adapter = RunPodAdapter(lease_owner=SimpleNamespace(selection=case.owner), placement=None,
                            installation=None, validate=None)
    assert adapter.quote() == {"offer": asdict(case.offer), "offer_digest": case.offer.digest(case.prefs)}
    case.owner.observe_offer = lambda: (_ for _ in ()).throw(selection.OfferObservationError("stock_unavailable"))
    stockless = adapter.quote()
    assert stockless["availability"]["reason"] == "stock_unavailable"
    monkeypatch.setattr(generation, "_runpod_preferences", lambda client: case.prefs.as_mapping())
    monkeypatch.setattr(generation, "_runpod_status", lambda **kwargs: dict(stockless))
    described = generation.GenerationFacade().describe(placement="runpod", max_hourly_usd=1.5)
    assert described["waiting_policy"]["max_hourly_usd"] == 1.5
    assert described["waiting_policy"]["policy_digest"] == selection.policy_consent_digest(case.prefs, 1.5)


def test_stockless_availability_adds_waiting_message_and_keeps_tokens(case):
    from astrid.packs.runpod.adapter import RunPodAdapter

    adapter = RunPodAdapter(lease_owner=SimpleNamespace(selection=case.owner), placement=None,
                            installation=None, validate=None)
    case.owner.observe_offer = lambda: (_ for _ in ()).throw(selection.OfferObservationError("stock_unavailable"))
    availability = adapter.quote()["availability"]
    assert set(availability) == {"status", "reason", "message"}
    assert availability["status"] == "unavailable" and availability["reason"] == "stock_unavailable"
    message = availability["message"]
    assert "no machine is in stock right now" in message.lower() and "this is normal" in message
    assert "astrid machines claim" in message and "--max-hourly-usd <ceiling>" in message
    assert "waits for one by default" in message
    assert "astrid generate" not in message


def test_offer_refusal_records_offer_and_both_digests_without_secrets(case):
    case.offer = RunPodOffer("account-a", "preferred-gpu", 1.5, "offer-2")
    observed = case.offer.digest(case.prefs)
    with pytest.raises(PaidSelectionError, match="offer or rate changed") as raised:
        asyncio.run(case.owner._select(case.grant))
    assert case.grant.offer_digest in str(raised.value) and observed in str(raised.value)
    record = json.loads(case.owner.path.with_name("offer-refusal.json").read_text())
    assert record["schema"] == "astrid.runpod.offer-refusal.v1"
    assert record["offer"] == {"provider_account_ref": "account-a", "gpu_type": "preferred-gpu",
                               "hourly_usd": 1.5, "offer_id": "offer-2", "datacenter_id": None,
                               "gpu_type_id": None}
    assert record["run_offer_digest"] == observed
    assert record["quoted_offer_digest"] == case.grant.offer_digest
    assert "fake-only" not in json.dumps(record)
    assert not case.owner.path.exists() and case.creates == 0


def test_generation_job_without_claim_refuses_before_staging_or_acquisition(case, monkeypatch, tmp_path):
    from astrid.core.execution.machine_run import MachineJob
    from astrid.core.execution.warm_hold import WarmHolds
    from astrid.packs.runpod import adapter, machine
    from astrid.packs.runpod.worker_owner import RunPodLeaseOwner

    monkeypatch.setattr(adapter, "prevalidate_task", lambda *args, **kwargs: None)
    case.owner.compatibility = selection.RunPodLeaseCompatibility(
        "sha256:" + "a" * 64, "sha256:" + "b" * 64, "a" * 40, "b" * 40).as_mapping()
    clock = lambda: case.now.timestamp()
    owner = RunPodLeaseOwner(case.owner, holds=WarmHolds(),
                             quiesce=lambda record: None, clock=clock, environ={})
    facade = adapter.RunPodAdapter(lease_owner=owner, placement=None,
                                   installation=SimpleNamespace(engine="comfyui"), validate=None)

    def unreachable(*args, **kwargs):
        raise AssertionError("preparation reached before consent validation")

    facade.prepare = unreachable
    monkeypatch.setattr("astrid.packs.runpod.generation.prepare_retained_generation", unreachable)
    monkeypatch.setattr("astrid.packs.vibecomfy.shared.bundle_inputs.staged_workflow_path", unreachable)
    policy = {"kind": "runpod", "engine": "comfyui", "preferences": case.prefs.as_mapping()}
    inputs = {"python": "workflow.py", "companion": "{}", "source": "{}"}
    job = MachineJob(entry="cpu-image", mode="t2i", policy=policy, inputs=inputs, invocation_id="invocation-1",
                     task_id="task", attempt_id="attempt", scratch=tmp_path,
                     progress_path=tmp_path / ".astrid-progress.json")
    runpod = machine.MACHINE.for_host(facade, begin=unreachable, done=unreachable)
    try:
        with pytest.raises(PaidSelectionError, match="no machine: run `astrid machines claim`"):
            runpod.claim(job)
    finally:
        owner.close()
    assert not case.owner.path.exists() and case.creates == case.configs == 0


def test_refused_reuse_never_reaches_create_or_reconcile(case, monkeypatch):
    """A reuse refused for lifetime must not start any create, launch or reconcile."""
    from astrid.packs.runpod import target_selection as selection_module

    asyncio.run(case.owner._select(case.grant))

    def must_not_run(*args, **kwargs):
        raise AssertionError("refused reuse reached a pod create, launch or reconcile")

    for name in ("acquire_target", "reconcile_target"):
        target = getattr(selection_module, name)
        monkeypatch.setattr(target, "__wrapped__", must_not_run, raising=False)
        monkeypatch.setattr(selection_module, name, must_not_run)
    case.now += timedelta(hours=2)
    with pytest.raises(PaidSelectionError, match="cannot be extended by reuse"):
        asyncio.run(case.owner._select(replace(case.grant, deadline=(case.now + timedelta(hours=1)).isoformat())))


@pytest.mark.parametrize('failure,reason', [
    ('transport', 'provider_transport_or_response_unavailable'),
    ('authentication', 'provider_authentication_failed'),
    ('rate_limit', 'provider_rate_limited'),
    ('invalid_response', 'provider_observation_unavailable'),
    ('stock', 'stock_unavailable'),
])
def test_offer_reobservation_preserves_fixed_provider_or_stock_reason_without_allocation(case, monkeypatch, failure, reason):
    import httpx
    import runpod_lifecycle
    from runpod_lifecycle import availability
    from astrid.packs.runpod.adapter import observe_priced_offer
    c = case
    secret = 'PRIVATE_PROVIDER_RESPONSE_AND_CREDENTIAL'
    c.owner.preferences = RunPodPreferences.from_mapping({**c.raw, 'volume': None})
    config = SimpleNamespace(api_key=secret, container_disk_gb=40, allowed_cuda_versions=('13.0',), min_cuda_version=None)
    if failure == 'stock':
        async def no_stock(**kwargs):
            return availability.GpuAvailability(kwargs['gpu'], 'gpu-id', 'GPU', 'unavailable', 'now', {}, [
                availability.DataCenterAvailability('DC', None, None, 'unavailable', None, 1.0, False)])
        monkeypatch.setattr(runpod_lifecycle, 'gpu_availability', no_stock)
    else:
        def post(*args, **kwargs):
            if failure == 'transport':
                raise httpx.ConnectError(secret)
            status = {'authentication': 403, 'rate_limit': 429, 'invalid_response': 200}[failure]
            return SimpleNamespace(status_code=status, json=lambda: {'errors': [{'message': secret}]})
        # Real pinned lifecycle query/unknown behavior, replaced only at HTTP I/O.
        monkeypatch.setattr(availability.httpx, 'post', post)
    c.owner.observe_offer = lambda: asyncio.run(observe_priced_offer(c.owner.preferences, config))
    with pytest.raises(PaidSelectionError) as raised:
        c.owner.consented_offer(c.grant, c.now)
    assert getattr(raised.value, 'reason', None) == reason
    assert 'reason=' + reason in str(raised.value)
    assert secret not in str(raised.value)
    assert c.creates == 0 and not c.owner.path.exists()


def test_unexpected_offer_error_cannot_forward_provider_text(case):
    from astrid.packs.runpod.target_selection import OfferObservationError
    secret = 'PRIVATE_UNEXPECTED_PROVIDER_FAILURE'
    def failed():
        raise RuntimeError(secret)
    case.owner.observe_offer = failed
    with pytest.raises(OfferObservationError) as raised:
        case.owner.consented_offer(case.grant, case.now)
    assert raised.value.reason == 'observation_failed'
    assert secret not in str(raised.value)
    assert case.creates == 0 and not case.owner.path.exists()


@pytest.mark.parametrize('failure,reason', [
    ('authentication', 'provider_authentication_failed'),
    ('rate_limit', 'provider_rate_limited'),
    ('transport', 'provider_transport_or_response_unavailable'),
])
def test_pricing_http_failure_reports_specific_safe_reason(case, monkeypatch, failure, reason):
    import httpx
    from runpod_lifecycle import availability
    from astrid.packs.runpod.adapter import observe_priced_offer

    secret = 'PRIVATE_PRICE_RESPONSE_AND_CREDENTIAL'
    calls = []
    case.owner.preferences = RunPodPreferences.from_mapping({**case.raw, 'volume': None})
    config = SimpleNamespace(api_key=secret, container_disk_gb=40, allowed_cuda_versions=('13.0',), min_cuda_version=None)

    def post(*args, **kwargs):
        query = kwargs['json']['query']
        if 'GpuCatalogue' in query:
            calls.append('catalogue')
            data = {'gpuTypes': [{'id': 'gpu-id', 'displayName': 'preferred-gpu'}]}
        elif 'DataCenterStock' in query:
            calls.append('stock')
            data = {'dataCenters': [{'id': 'DC', 'storageSupport': False, 'gpuAvailability': [
                {'gpuTypeId': 'gpu-id', 'available': True, 'stockStatus': 'High'}]}]}
        else:
            assert 'DataCenterPrices' in query
            calls.append('prices')
            if failure == 'transport':
                raise httpx.ConnectError(secret)
            return SimpleNamespace(status_code={'authentication': 403, 'rate_limit': 429}[failure],
                                   json=lambda: {'errors': [{'message': secret}]})
        return SimpleNamespace(status_code=200, json=lambda: {'data': data})

    monkeypatch.setattr(availability.httpx, 'post', post)
    case.owner.observe_offer = lambda: asyncio.run(observe_priced_offer(case.owner.preferences, config))
    with pytest.raises(PaidSelectionError) as raised:
        case.owner.consented_offer(case.grant, case.now)
    assert calls[:3] == ['catalogue', 'stock', 'prices']
    assert raised.value.reason == reason
    assert secret not in str(raised.value)
    assert case.creates == case.configs == 0 and not case.owner.path.exists()


def test_pricing_mixed_datacenters_selects_healthy_offer_and_requires_consent(case, monkeypatch):
    from runpod_lifecycle import availability
    from astrid.packs.runpod.adapter import observe_priced_offer

    case.owner.preferences = RunPodPreferences.from_mapping({**case.raw, 'volume': None})
    config = SimpleNamespace(api_key='secret', container_disk_gb=40, allowed_cuda_versions=('13.0',), min_cuda_version=None)

    def post(*args, **kwargs):
        query = kwargs['json']['query']
        if 'GpuCatalogue' in query:
            data = {'gpuTypes': [{'id': 'gpu-id', 'displayName': 'preferred-gpu'}]}
        elif 'DataCenterStock' in query:
            data = {'dataCenters': [{'id': dc, 'gpuAvailability': [
                {'gpuTypeId': 'gpu-id', 'available': True, 'stockStatus': 'High'}]} for dc in ('A', 'B')]}
        else:
            assert 'DataCenterPrices' in query
            data = {'gpuTypes': [{'id': 'gpu-id', 'dc0': None,
                                  'dc1': {'stockStatus': 'High', 'uninterruptablePrice': 1.0}}]}
        return SimpleNamespace(status_code=200, json=lambda: {'data': data})

    monkeypatch.setattr(availability.httpx, 'post', post)
    case.owner.observe_offer = lambda: asyncio.run(observe_priced_offer(case.owner.preferences, config))
    offer = case.owner.observe_offer()
    assert offer.datacenter_id == 'B' and offer.hourly_usd == 1.0
    with pytest.raises(PaidSelectionError, match='offer or rate changed'):
        case.owner.consented_offer(case.grant, case.now)
    assert case.creates == case.configs == 0 and not case.owner.path.exists()


@pytest.mark.parametrize("selected_datacenter", [None, "DC-B"])
def test_offer_order_preserves_user_gpu_priority_then_price_within_gpu(case, monkeypatch, selected_datacenter):
    import runpod_lifecycle
    from astrid.packs.runpod.adapter import observe_priced_offer, observe_priced_offers

    preferences = RunPodPreferences.from_mapping({**case.raw, "volume": None, "datacenter_id": selected_datacenter})
    config = SimpleNamespace(api_key="fake-only", container_disk_gb=40,
                             allowed_cuda_versions=("13.0",), min_cuda_version=None)
    queried = []

    async def observe(**kwargs):
        queried.append((kwargs["gpu"], kwargs["datacenter_ids"]))
        rate = 1.0 if kwargs["gpu"] == "preferred-gpu" else 0.1
        # Return the more expensive DC first and include an outside DC even
        # when a provider response ignores the requested explicit restriction.
        return SimpleNamespace(status="available", gpu_type_id=kwargs["gpu"], datacenters=[
            SimpleNamespace(status="available", datacenter_id="DC-A", price_per_hour=rate + 0.2),
            SimpleNamespace(status="available", datacenter_id="DC-B", price_per_hour=rate)])

    monkeypatch.setattr(runpod_lifecycle, "gpu_availability", observe)
    offers = asyncio.run(observe_priced_offers(preferences, config))
    expected_dcs = ["DC-B", "DC-A"] if selected_datacenter is None else ["DC-B"]
    assert [(offer.gpu_type, offer.datacenter_id) for offer in offers] == [
        (gpu, dc) for gpu in preferences.gpu_types for dc in expected_dcs]
    assert offers[0].gpu_type == "preferred-gpu" and offers[-1].hourly_usd < offers[0].hourly_usd
    assert queried == [(gpu, None if selected_datacenter is None else ["DC-B"]) for gpu in preferences.gpu_types]
    assert asyncio.run(observe_priced_offer(preferences, config)) == offers[0]
    assert case.creates == case.configs == 0 and not case.owner.path.exists()


@pytest.mark.parametrize("available_datacenter", ["EU-FAKE", "US-FAKE", "AP-FAKE", "CN-FAKE"])
def test_unrestricted_pricing_observes_every_returned_datacenter(case, monkeypatch, available_datacenter):
    import runpod_lifecycle
    from runpod_lifecycle import availability
    from astrid.packs.runpod.adapter import observe_priced_offer

    preferences = RunPodPreferences.from_mapping({**case.raw, "volume": None, "datacenter_id": None})
    config = SimpleNamespace(api_key="fake-only", container_disk_gb=40,
                             allowed_cuda_versions=("13.0",), min_cuda_version=None)
    datacenters = ["EU-FAKE", "US-FAKE", "AP-FAKE", "CN-FAKE"]
    filters, observed_datacenters = [], []
    real_observe = runpod_lifecycle.gpu_availability

    def post(*args, **kwargs):
        query = kwargs["json"]["query"]
        if "GpuCatalogue" in query:
            data = {"gpuTypes": [{"id": "gpu-id", "displayName": "preferred-gpu"}]}
        elif "DataCenterStock" in query:
            data = {"dataCenters": [{"id": dc, "gpuAvailability": [
                {"gpuTypeId": "gpu-id", "available": True, "stockStatus": "High"}]} for dc in datacenters]}
        else:
            assert "DataCenterPrices" in query
            data = {"gpuTypes": [{"id": "gpu-id", **{
                f"dc{index}": ({"stockStatus": "High", "uninterruptablePrice": 1.0}
                               if dc == available_datacenter else None)
                for index, dc in enumerate(datacenters)}}]}
        return SimpleNamespace(status_code=200, json=lambda: {"data": data})

    async def observe(**kwargs):
        filters.append(kwargs["datacenter_ids"])
        assert kwargs["require_network_volume"] is False
        result = await real_observe(**kwargs)
        observed_datacenters.append({dc.datacenter_id for dc in result.datacenters})
        return result

    monkeypatch.setattr(availability.httpx, "post", post)
    monkeypatch.setattr(runpod_lifecycle, "gpu_availability", observe)
    offer = asyncio.run(observe_priced_offer(preferences, config))
    assert filters == [None]
    assert observed_datacenters == [set(datacenters)]
    assert offer.datacenter_id == available_datacenter and offer.hourly_usd == 1.0
    assert case.creates == case.configs == 0 and not case.owner.path.exists()


def test_pricing_conflicting_failures_use_first_specific_reason(case, monkeypatch):
    from runpod_lifecycle import availability
    from astrid.packs.runpod.adapter import observe_priced_offer

    prefs = RunPodPreferences.from_mapping({**case.raw, 'volume': None})
    config = SimpleNamespace(api_key='secret', container_disk_gb=40, allowed_cuda_versions=('13.0',), min_cuda_version=None)
    def post(*args, **kwargs):
        query, variables = kwargs['json']['query'], kwargs['json']['variables']
        if 'GpuCatalogue' in query:
            data = {'gpuTypes': [{'id': gpu, 'displayName': gpu} for gpu in prefs.gpu_types]}
        elif 'DataCenterStock' in query:
            data = {'dataCenters': [{'id': 'DC', 'gpuAvailability': [
                {'gpuTypeId': gpu, 'available': True, 'stockStatus': 'High'} for gpu in prefs.gpu_types]}]}
        else:
            assert 'DataCenterPrices' in query
            status = 403 if variables['gpu']['id'] == prefs.gpu_types[0] else 429
            return SimpleNamespace(status_code=status)
        return SimpleNamespace(status_code=200, json=lambda: {'data': data})
    monkeypatch.setattr(availability.httpx, 'post', post)
    with pytest.raises(PaidSelectionError) as raised:
        asyncio.run(observe_priced_offer(prefs, config))
    assert raised.value.reason == 'provider_authentication_failed'


def test_capacity_attempt_line_reads_as_waiting_and_keeps_reason_token(case, monkeypatch, capsys):
    fake_capacity_clock(case, monkeypatch)
    case.owner.progress = None

    def absent():
        raise selection.OfferObservationError("stock_unavailable")

    case.owner.observe_offer = absent
    with pytest.raises(selection.CapacityWaitTimeout):
        asyncio.run(case.owner._select(waiting_grant(case, capacity_timeout_seconds=6)))
    first = capsys.readouterr().err.splitlines()[0]
    assert "RunPod capacity attempt 1:" in first
    assert first.endswith("no stock yet (stock_unavailable); still waiting, this is normal")
    with pytest.raises(selection.OfferObservationError):
        asyncio.run(case.owner._select(waiting_grant(case, wait_for_capacity=False)))
    assert "still waiting" not in capsys.readouterr().err


def test_rest_exact_pod_volume_object_passes_selection(case, monkeypatch):
    monkeypatch.setattr(selection, "read_exact_pod", lambda key, pod: (200, {
        "id": pod, "name": "worker", "image": case.prefs.image,
        "desiredStatus": "RUNNING", "volumeMountPath": "/workspace",
        "networkVolume": {"id": case.prefs.volume_id, "name": "storage", "size": 100},
        "machine": {}, "gpu": {"id": "preferred-gpu", "count": 1},
    }))
    result = asyncio.run(case.owner._select(case.grant))
    assert result["pod_id"] == "pod-1"


@pytest.mark.parametrize("retryable,custody,retries", [
    (True, "none", True), (False, "none", False),
    (True, "exact", False), (True, "unknown", False),
])
def test_capacity_wait_uses_refusal_policy_for_new_reason(case, monkeypatch, retryable, custody, retries):
    sleeps = fake_capacity_clock(case, monkeypatch)
    failure = selection.OfferObservationError("observation_failed")
    failure.reason = "new_provider_observation"
    failure.retryable = retryable
    failure.custody = custody
    outcomes = iter([failure, case.offer])

    def observe():
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    case.owner.observe_offer = observe
    if retries:
        asyncio.run(case.owner._select(waiting_grant(case)))
        assert sleeps == [5] and case.creates == 1
    else:
        with pytest.raises(selection.OfferObservationError) as caught:
            asyncio.run(case.owner._select(waiting_grant(case)))
        assert caught.value is failure
        assert sleeps == [] and case.creates == 0
