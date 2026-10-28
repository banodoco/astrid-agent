"""Local engine endpoint producer, with engine_status stubbed (CPU only)."""
import hashlib
from types import SimpleNamespace

import pytest

from astrid.core.generation.engine_contracts import HealthEvidence, LocalAttestation
from astrid.core.generation.engine_endpoint import endpoint_to_dict, parse_endpoint
from astrid.core.generation.engine_local_endpoint import EngineNotInstalled, local_engine_endpoint
from astrid.core.generation.engine_recipe import load_recipe

PLATFORM = "linux-x86_64-cu130"
OBJECT_INFO = "ab" * 32


def _root(tmp_path):
    root = tmp_path / "engine"
    (root / ".astrid-engine").mkdir(parents=True)
    receipt = root / ".astrid-engine" / "install.json"
    receipt.write_bytes(b'{"status":"installed"}')
    return root, hashlib.sha256(receipt.read_bytes()).hexdigest()


def _status(**overrides):
    health = HealthEvidence("healthy", "2026-10-28T00:00:00Z", OBJECT_INFO, True)
    value = dict(status="installed", health=health)
    value.update(overrides)
    calls = []
    def stub(engine, **kwargs):
        calls.append((engine, kwargs))
        return SimpleNamespace(**value) if value["status"] is not None else None
    stub.calls = calls
    return stub


def test_installed_engine_becomes_a_local_endpoint(tmp_path):
    root, receipt_sha = _root(tmp_path)
    stub = _status()
    local = local_engine_endpoint("vibecomfy-h3", platform=PLATFORM, root=str(root), context=object(), status=stub)
    endpoint = local.endpoint
    recipe = load_recipe("vibecomfy-h3", PLATFORM)
    assert stub.calls[0][1]["placement"] == "local" and stub.calls[0][1]["root"] == str(root)
    assert endpoint.placement == "local" and endpoint.state == "ready"
    assert endpoint.url == f"http://127.0.0.1:{recipe.start.port}"
    assert endpoint.auth.kind == "none" and endpoint.auth.token is None
    assert endpoint.lifecycle_owner.kind == "astrid-local" and endpoint.lifecycle_owner.owner_id == "astrid-local"
    assert isinstance(endpoint.attestation, LocalAttestation)
    assert endpoint.attestation.install_receipt_sha256 == receipt_sha
    assert endpoint.attestation.object_info_sha256 == OBJECT_INFO
    assert endpoint.attestation.install_receipt_path == str(root / ".astrid-engine" / "install.json")
    assert parse_endpoint(endpoint_to_dict(endpoint)) == endpoint
    comfy = next(s.relative_path for s in recipe.sources if s.name == "comfyui")
    assert local.comfy_output_dir == f"{root}/{comfy}/output"


@pytest.mark.parametrize("overrides", [
    {"status": None},
    {"status": "failed"},
    {"status": "installing"},
    {"health": HealthEvidence("unhealthy", "2026-10-28T00:00:00Z", None, False)},
    {"health": None},
])
def test_missing_or_unhealthy_engine_fails_fast_with_next_action(tmp_path, overrides):
    root, _ = _root(tmp_path)
    with pytest.raises(EngineNotInstalled) as caught:
        local_engine_endpoint("vibecomfy-h3", platform=PLATFORM, root=str(root), context=object(),
                              status=_status(**overrides))
    assert caught.value.next_action == "astrid engines install vibecomfy-h3"
    assert "astrid engines install vibecomfy-h3" in str(caught.value)
