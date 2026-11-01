"""Managed selection uses installation custody; attached URLs retain human ownership."""
from types import SimpleNamespace

import pytest

from astrid.core.generation import engine_local_endpoint, preflight
from astrid.core.generation.backends.comfy_target import ComfyTargetClient
from astrid.core.generation.engine_contracts import EndpointOwnershipError
from astrid.core.model_catalog.registry import ModelRegistry


def test_installed_managed_engine_does_not_require_legacy_comfy_cli(monkeypatch):
    root = '/installed/comfyui'
    calls = []
    monkeypatch.setattr(engine_local_endpoint, 'recorded_engine_root', lambda engine: root)
    def verified(engine, **kwargs):
        calls.append((engine, kwargs))
        return SimpleNamespace(endpoint=SimpleNamespace(
            url='http://127.0.0.1:8188',
            health=SimpleNamespace(object_info_sha256='a' * 64, checked_at='2026-11-01T00:00:00Z')))
    monkeypatch.setattr(engine_local_endpoint, 'local_engine_endpoint', verified)
    monkeypatch.setattr(preflight, '_module_available', lambda name: name == 'vibecomfy')
    monkeypatch.setattr(preflight, '_comfyui_runtime_available',
                        lambda: pytest.fail('installed engine fell back to the legacy CLI'))
    client = preflight.select_generation_target('managed')
    assert isinstance(client, ComfyTargetClient)
    assert client.target.kind == 'managed'
    assert client.generation_target.owner == 'astrid'
    assert client.generation_target.root == root
    assert client.generation_target.endpoint == 'http://127.0.0.1:8188'
    assert calls == [('comfyui', {'root': root})]
    preflight.require_generation_target_readiness(
        ModelRegistry.load_default().get('qwen-image-2.1-turbo'), 't2i', client)


def test_uninstalled_managed_target_preserves_structural_preflight(monkeypatch):
    monkeypatch.setattr(engine_local_endpoint, 'recorded_engine_root', lambda engine: None)
    monkeypatch.setattr(engine_local_endpoint, 'local_engine_endpoint',
                        lambda *a, **k: pytest.fail('unrecorded engine was probed'))
    assert preflight.select_generation_target('managed') is None


def test_unowned_installed_engine_blocks_without_falling_back(monkeypatch):
    monkeypatch.setattr(engine_local_endpoint, 'recorded_engine_root', lambda engine: '/installed/comfyui')
    def unowned(*args, **kwargs):
        raise EndpointOwnershipError('synthetic unowned listener')
    monkeypatch.setattr(engine_local_endpoint, 'local_engine_endpoint', unowned)
    with pytest.raises(preflight.GenerationReadinessError) as caught:
        preflight.select_generation_target('managed')
    envelope = caught.value.to_envelope()
    assert envelope['status'] == 'blocked'
    assert envelope['issues'][0]['details']['engine_code'] == 'endpoint_ownership'
    assert envelope['fixes'][0]['argv'][:4] == ['astrid', 'engines', 'install', 'comfyui']


def test_explicit_attached_url_never_adopts_registered_engine(monkeypatch):
    monkeypatch.setattr(preflight, '_installed_managed_target',
                        lambda: pytest.fail('human URL adopted an installed engine'))
    monkeypatch.setattr(ComfyTargetClient, 'request_json',
                        lambda *a, **k: {'system': {'comfyui_version': '0.43.2'}})
    client = preflight.select_generation_target('http://127.0.0.1:8188')
    assert client.target.kind == 'attached'
    assert client.generation_target.owner == 'human'
