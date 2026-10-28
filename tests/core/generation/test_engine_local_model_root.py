"""Model-root binding derived from the installed local engine (CPU only, engine_status stubbed)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrid.core.generation.engine_contracts import DownloadResult, HealthEvidence
from astrid.core.generation.engine_local_endpoint import (
    ENGINE_ROOT_ENV,
    LOCAL_ENGINE_HOST_ENV,
    EngineNotInstalled,
    local_model_root_binding,
    model_root_binding_from_receipt,
    record_engine_root,
)
from astrid.core.generation.model_root import (
    MODEL_ROOT_BINDING_ENV,
    MODEL_ROOT_ENV,
    ModelRootBindingError,
    canonical_model_inventory_digest,
    validate_model_root_binding,
)

PLATFORM = "linux-x86_64-cu130"
INSTALL = "astrid engines install vibecomfy-h3"
FILES = {
    "diffusion_models/unet.safetensors": b"unet bytes",
    "vae/video.safetensors": b"video vae bytes",
    "loras/turbo.safetensors": b"lora bytes",
}


def _install(tmp_path):
    """An engine root with its model files on disk and the receipt records the installer would have written."""
    root = tmp_path / "engine"
    models = []
    for destination, data in FILES.items():
        path = root / "models" / destination
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        models.append(DownloadResult(destination.rsplit("/", 1)[1], str(path), "downloaded", len(data),
                                     hashlib.sha256(data).hexdigest(), True, 0))
    return root, tuple(models)


def _receipt(models, **overrides):
    health = HealthEvidence("healthy", "2026-10-28T00:00:00Z", "ab" * 32, True)
    value = dict(status="installed", health=health, models=models)
    value.update(overrides)
    return SimpleNamespace(**value)


def _status(receipt):
    calls = []

    def stub(engine, **kwargs):
        calls.append((engine, kwargs))
        return receipt

    stub.calls = calls
    return stub


def _derive(root, receipt):
    return local_model_root_binding("vibecomfy-h3", platform=PLATFORM, root=str(root), context=object(),
                                    status=_status(receipt))


def test_valid_receipt_yields_the_binding_of_the_installed_model_lock(tmp_path):
    root, models = _install(tmp_path)
    stub = _status(_receipt(models))
    binding = local_model_root_binding("vibecomfy-h3", platform=PLATFORM, root=str(root), context=object(),
                                       status=stub)
    # The root comes from the engine status call; the inventory from the receipt's verified model files.
    assert stub.calls[0][1]["placement"] == "local" and stub.calls[0][1]["root"] == str(root)
    assert str(binding.path) == str(root / "models")
    assert binding.qualification_identity is None
    assert [(e["subdir"], e["name"], e["size"], e["sha256"]) for e in binding.inventory] == [
        ("diffusion_models", "unet.safetensors", len(FILES["diffusion_models/unet.safetensors"]),
         "sha256:" + hashlib.sha256(FILES["diffusion_models/unet.safetensors"]).hexdigest()),
        ("loras", "turbo.safetensors", len(FILES["loras/turbo.safetensors"]),
         "sha256:" + hashlib.sha256(FILES["loras/turbo.safetensors"]).hexdigest()),
        ("vae", "video.safetensors", len(FILES["vae/video.safetensors"]),
         "sha256:" + hashlib.sha256(FILES["vae/video.safetensors"]).hexdigest()),
    ]
    assert binding.inventory_digest == canonical_model_inventory_digest(list(binding.inventory))
    # It is a binding the consumer accepts, byte for byte, exactly as the readiness-profile one is.
    document = json.loads(json.dumps(binding.as_dict()))
    assert validate_model_root_binding(document, verify_files=True).inventory_digest == binding.inventory_digest


def test_binding_follows_the_recorded_install_root_without_an_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv(ENGINE_ROOT_ENV, raising=False)
    root, models = _install(tmp_path)
    record_engine_root("vibecomfy-h3", str(root))
    stub = _status(_receipt(models))
    binding = local_model_root_binding("vibecomfy-h3", platform=PLATFORM, context=object(), status=stub)
    assert stub.calls[0][1]["root"] == str(root)
    assert str(binding.path) == str(root / "models")


def test_derived_binding_rejects_a_model_file_that_changed_after_the_install(tmp_path):
    root, models = _install(tmp_path)
    binding = _derive(root, _receipt(models))
    (root / "models" / "vae" / "video.safetensors").write_bytes(b"video vae bytez")  # same size, other bytes
    with pytest.raises(ModelRootBindingError, match="hash mismatch"):
        validate_model_root_binding(binding.as_dict(), verify_files=True)


@pytest.mark.parametrize("overrides", [
    None,                                                       # no install receipt at all
    {"status": "failed"},
    {"status": "installing"},
    {"health": HealthEvidence("unhealthy", "2026-10-28T00:00:00Z", None, False)},
    {"health": None},
])
def test_missing_or_failed_install_is_refused_naming_the_install_action(tmp_path, overrides):
    root, models = _install(tmp_path)
    receipt = None if overrides is None else _receipt(models, **overrides)
    with pytest.raises(EngineNotInstalled) as caught:
        _derive(root, receipt)
    assert caught.value.next_action == INSTALL and INSTALL in str(caught.value)


@pytest.mark.parametrize("damage", [
    lambda m: replace(m, verified=False),                      # the installer did not verify this file
    lambda m: replace(m, sha256="not-a-digest"),               # no usable sha256
    lambda m: replace(m, sha256=m.sha256.upper()),             # digests are lowercase hex
    lambda m: replace(m, sha256=m.sha256[:-1]),                # short digest
    lambda m: replace(m, bytes=-1),                            # no usable size
    lambda m: replace(m, path="/elsewhere/" + m.model),        # outside <root>/models
])
def test_unverified_model_hash_is_refused_naming_the_install_action(tmp_path, damage):
    root, models = _install(tmp_path)
    damaged = (damage(models[0]),) + models[1:]
    with pytest.raises(EngineNotInstalled) as caught:
        _derive(root, _receipt(damaged))
    assert INSTALL in str(caught.value)


def test_a_receipt_with_no_verified_models_is_refused(tmp_path):
    root, _models = _install(tmp_path)
    with pytest.raises(EngineNotInstalled, match="no verified models"):
        _derive(root, _receipt(()))


def test_binding_from_receipt_needs_no_engine_status(tmp_path):
    root, models = _install(tmp_path)
    binding = model_root_binding_from_receipt(_receipt(models), str(root))
    assert len(binding.inventory) == len(FILES)


# --- the pack host: where the binding enters the VibeComfy child environment -------------------------------------

def _host_method():
    from astrid.core.execution.generic_host import GenericPackHost

    return GenericPackHost._local_engine_model_root_env


def test_host_derives_the_child_binding_from_the_installed_engine_once(tmp_path, monkeypatch):
    root, models = _install(tmp_path)
    calls = []

    def derive(engine):
        calls.append(engine)
        return _derive(root, _receipt(models))

    monkeypatch.setattr("astrid.core.generation.engine_local_endpoint.local_model_root_binding", derive)
    host = SimpleNamespace()
    first = _host_method()(host, "vibecomfy-h3")
    again = _host_method()(host, "vibecomfy-h3")
    assert first == again and calls == ["vibecomfy-h3"]
    assert first[MODEL_ROOT_ENV] == str(root / "models")
    binding = validate_model_root_binding(json.loads(first[MODEL_ROOT_BINDING_ENV]), verify_files=True)
    assert str(binding.path) == first[MODEL_ROOT_ENV]


def test_host_refuses_when_the_install_cannot_attest_a_binding(tmp_path, monkeypatch):
    from astrid.core.execution.generic_host import HostError

    def refuse(engine):
        raise EngineNotInstalled(engine, "no install receipt")

    monkeypatch.setattr("astrid.core.generation.engine_local_endpoint.local_model_root_binding", refuse)
    with pytest.raises(HostError, match="astrid engines install vibecomfy-h3"):
        _host_method()(SimpleNamespace(), "vibecomfy-h3")


def _child_environment(monkeypatch, tmp_path, *, profile=None, local_engine=None):
    """The VibeComfy child's environment as the real host builds it."""
    from astrid.core.execution import generic_host as module

    monkeypatch.delenv(LOCAL_ENGINE_HOST_ENV, raising=False)
    if local_engine:
        monkeypatch.setenv(LOCAL_ENGINE_HOST_ENV, local_engine)
    monkeypatch.setattr(module, "_read_readiness_profile_document", lambda: profile)
    host = object.__new__(module.GenericPackHost)
    host.credential_source = None
    host._credential_source_is_explicit = False
    host._vibecomfy_requested_warmth_hint = None
    host.client = None
    record = SimpleNamespace(id="vibecomfy.validate", definition=SimpleNamespace(
        isolation=SimpleNamespace(env_passthrough=()), inputs=(), metadata={}), matrix={})
    monkeypatch.setattr(module, "_required_secret_names", lambda _record: ())
    monkeypatch.setattr(module, "_fixed_worker_environment", lambda _record, explicit, _source: dict(explicit))
    monkeypatch.setattr(module, "process_control_support", lambda: None)
    env, _secrets = host._child_environment(record, tmp_path)
    return env


def test_child_environment_without_profile_or_local_flag_has_no_binding(monkeypatch, tmp_path):
    env = _child_environment(monkeypatch, tmp_path)
    assert MODEL_ROOT_BINDING_ENV not in env and MODEL_ROOT_ENV not in env


def test_child_environment_for_local_placement_carries_the_installed_engine_binding(monkeypatch, tmp_path):
    root, models = _install(tmp_path)
    monkeypatch.setattr("astrid.core.generation.engine_local_endpoint.local_model_root_binding",
                        lambda engine: _derive(root, _receipt(models)))
    env = _child_environment(monkeypatch, tmp_path, local_engine="vibecomfy-h3")
    assert env[MODEL_ROOT_ENV] == str(root / "models")
    assert json.loads(env[MODEL_ROOT_BINDING_ENV])["path"] == str(root / "models")


def test_readiness_profile_still_wins_over_the_local_flag(monkeypatch, tmp_path):
    """The readiness-profile source is unchanged: with a profile the host never consults the installed engine."""
    profile_root = tmp_path / "profile-models"
    profile_root.mkdir()
    data = b"profile model"
    (profile_root / "m.bin").write_bytes(data)
    inventory = [{"name": "m.bin", "sha256": "sha256:" + hashlib.sha256(data).hexdigest(), "size": len(data),
                  "subdir": ""}]
    profile = {"launch": {"model_root": {
        "schema_version": 1, "path": str(profile_root), "inventory": inventory,
        "inventory_digest": canonical_model_inventory_digest(inventory), "qualification_identity": None}}}

    def never(engine):
        raise AssertionError("the installed engine must not be consulted when a readiness profile is present")

    monkeypatch.setattr("astrid.core.generation.engine_local_endpoint.local_model_root_binding", never)
    env = _child_environment(monkeypatch, tmp_path, profile=profile, local_engine="vibecomfy-h3")
    assert env[MODEL_ROOT_ENV] == str(profile_root)
    assert json.loads(env[MODEL_ROOT_BINDING_ENV])["inventory"] == inventory
