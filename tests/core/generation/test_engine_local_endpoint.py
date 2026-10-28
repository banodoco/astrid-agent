"""Local engine endpoint producer, with engine_status stubbed (CPU only)."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation.engine_contracts import HealthEvidence, LocalAttestation, UnsafePathError
from astrid.core.generation.engine_endpoint import endpoint_to_dict, parse_endpoint
from astrid.core.generation.engine_local_endpoint import (
    ENGINE_ROOT_ENV,
    EngineNotInstalled,
    default_engine_root,
    local_engine_endpoint,
    record_engine_root,
    recorded_engine_root,
)
from astrid.core.generation.engine_recipe import load_recipe

PLATFORM = "linux-x86_64-cu130"
ENGINE = "vibecomfy-h3"
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


def _engine_at(path):
    """An installed engine root (receipt file present) at ``path``."""
    (path / ".astrid-engine").mkdir(parents=True)
    (path / ".astrid-engine" / "install.json").write_bytes(b'{"status":"installed"}')
    return path


def _status_for(*roots):
    """A status stub that answers as a healthy installed engine only for the given roots."""
    calls = []

    def stub(engine, **kwargs):
        calls.append((engine, kwargs))
        if kwargs["root"] not in roots:
            return None
        return SimpleNamespace(status="installed", health=HealthEvidence("healthy", "2026-10-28T00:00:00Z", OBJECT_INFO, True))

    stub.calls = calls
    return stub


@pytest.fixture
def no_env_override(monkeypatch):
    monkeypatch.delenv(ENGINE_ROOT_ENV, raising=False)


def test_run_path_uses_the_recorded_install_root_without_an_env_var(tmp_path, no_env_override):
    installed = _engine_at(tmp_path / "custom-engine")
    record_engine_root(ENGINE, str(installed))
    stub = _status_for(str(installed))
    local = local_engine_endpoint(ENGINE, platform=PLATFORM, context=object(), status=stub)
    assert stub.calls[0][1]["root"] == str(installed)
    assert local.endpoint.attestation.install_receipt_path == str(installed / ".astrid-engine" / "install.json")
    assert local.comfy_output_dir.startswith(f"{installed}/")
    assert default_engine_root(ENGINE) == str(installed)


def test_env_override_wins_over_the_recorded_root(tmp_path, monkeypatch):
    recorded = _engine_at(tmp_path / "recorded")
    override = _engine_at(tmp_path / "override")
    record_engine_root(ENGINE, str(recorded))
    monkeypatch.setenv(ENGINE_ROOT_ENV, str(override))
    stub = _status_for(str(override))
    local_engine_endpoint(ENGINE, platform=PLATFORM, context=object(), status=stub)
    assert stub.calls[0][1]["root"] == str(override)
    assert default_engine_root(ENGINE) == str(override)


def test_recorded_root_without_receipt_refuses_and_never_falls_back_to_the_default(tmp_path, no_env_override):
    moved = tmp_path / "moved-engine"
    moved.mkdir()  # the install was removed; only the directory remains
    record_engine_root(ENGINE, str(moved))
    default = _engine_at(Path.home() / ".astrid-data" / "engines" / ENGINE)  # a valid install elsewhere
    stub = _status_for(str(default))
    with pytest.raises(EngineNotInstalled, match="no install receipt"):
        local_engine_endpoint(ENGINE, platform=PLATFORM, context=object(), status=stub)
    assert [kwargs["root"] for _, kwargs in stub.calls] == [str(moved)]


def test_without_a_record_or_override_the_default_root_is_used(no_env_override):
    assert default_engine_root(ENGINE) == str(Path.home() / ".astrid-data" / "engines" / ENGINE)


def test_a_later_install_replaces_the_record_and_leaves_no_temporary_file(tmp_path, no_env_override):
    first, second = tmp_path / "first", tmp_path / "second"
    record_engine_root(ENGINE, str(first))
    record_engine_root(ENGINE, str(second))
    assert recorded_engine_root(ENGINE) == str(second)
    assert sorted(p.name for p in (Path.home() / ".astrid-data" / "engines").iterdir()) == [f"{ENGINE}.root.json"]


@pytest.mark.parametrize("content", [
    "{not json",
    json.dumps({"schema": "astrid.engine-root.v1", "engine": "other-engine", "root": "/x/engine"}),
    json.dumps({"schema": "wrong.v0", "engine": ENGINE, "root": "/x/engine"}),
    json.dumps({"schema": "astrid.engine-root.v1", "engine": ENGINE, "root": "relative/engine"}),
    json.dumps({"schema": "astrid.engine-root.v1", "engine": ENGINE, "root": "/"}),
    json.dumps({"schema": "astrid.engine-root.v1", "engine": ENGINE, "root": "/x/../engine"}),
    json.dumps({"schema": "astrid.engine-root.v1", "engine": ENGINE, "root": "/x/engine", "extra": 1}),
])
def test_malformed_record_refuses_instead_of_selecting_another_install(tmp_path, no_env_override, content):
    path = record_engine_root(ENGINE, str(tmp_path / "engine"))
    path.write_text(content, encoding="utf-8")
    with pytest.raises(EngineNotInstalled):
        default_engine_root(ENGINE)


def test_record_names_only_plain_engine_ids_and_absolute_roots(tmp_path, no_env_override):
    with pytest.raises(UnsafePathError):
        record_engine_root("../escape", str(tmp_path / "engine"))
    with pytest.raises(UnsafePathError):
        record_engine_root(ENGINE, "relative/engine")
    assert recorded_engine_root("../escape") is None
    assert not (Path.home() / ".astrid-data").exists()
