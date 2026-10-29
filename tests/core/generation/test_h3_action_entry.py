from types import SimpleNamespace

import pytest

from astrid.core.generation import entries, listing
from astrid.sdk.exceptions import CapabilityPreconditionError, CapabilityValidationError
from astrid.sdk.generation import GenerationFacade


def test_h3_entry_comes_from_action_declaration_and_file_ports():
    entry = entries.find_entry("h3")
    assert entry is not None
    assert entry["action"] == "h3_av.transform"
    assert entry["kind"] == "video" and entry["modes"] == ["transform"]
    assert [(port["name"], port["type"], port["required"]) for port in entry["inputs"]] == [
        ("request", "file", True), ("asset_map", "file", True),
    ]
    assert entry["requirements"] == ["profiles/native-v1.yaml", "profiles/native-v2.yaml"]
    assert "vibecomfy" in entry["isolation_requirements"]


def test_h3_list_never_claims_unverified_requirements_ready(monkeypatch):
    monkeypatch.setattr(listing, "_targets", lambda: [])
    row = next(row for row in listing.generation_rows("video") if row["id"] == "h3")
    assert row["source"] == "action-entry" and row["template"] == "h3_av.transform"
    assert row["requirements"] == ["profiles/native-v1.yaml", "profiles/native-v2.yaml"]
    assert not row["ready"] and row["readiness_scope"] == "requirements_unverified"
    assert not row["models_present"]


def test_action_entry_requires_managed_files_and_stops_before_local_orchestrator(monkeypatch):
    entry = entries.find_entry("h3")
    monkeypatch.setattr(entries, "find_entry", lambda name: entry if name == "h3" else None)
    calls = []
    monkeypatch.setattr(GenerationFacade, "_invoke", lambda *args, **kwargs: calls.append((args, kwargs)))
    facade = GenerationFacade()
    digest = "sha256:" + "1" * 64
    with pytest.raises(CapabilityValidationError, match="missing required.*asset_map"):
        facade.video(model="h3", request=digest)
    with pytest.raises(CapabilityValidationError, match="managed Runtime object"):
        facade.video(model="h3", request="/tmp/request.json", asset_map=digest)
    with pytest.raises(CapabilityValidationError, match="unsupported entry inputs.*prompt"):
        facade.video(model="h3", request=digest, asset_map=digest, prompt="fake")
    with pytest.raises(CapabilityPreconditionError, match="Runtime-admitted parent"):
        facade.video(model="h3", request={"digest": digest}, asset_map=digest)
    with pytest.raises(CapabilityPreconditionError, match="Runtime-admitted parent"):
        facade.video(entry="h3", request={"digest": digest}, asset_map=digest)
    with pytest.raises(CapabilityValidationError, match="model and entry"):
        facade.video(model="other", entry="h3", request=digest, asset_map=digest)
    assert calls == []


def test_invalid_action_entry_port_declaration_fails_closed():
    capability = SimpleNamespace(
        id="example.action",
        definition={"metadata": {"generation_entry": {"id": "bad", "kind": "video",
                    "modes": ["transform"], "inputs": ["missing"]}}},
        inputs=(),
    )
    with pytest.raises(ValueError, match="invalid generation entry"):
        entries.action_entries(SimpleNamespace(actions=[capability]))


def test_action_entry_collision_is_not_silently_shadowed(monkeypatch):
    action = {"id": "h3", "action": "h3_av.transform"}
    monkeypatch.setattr(entries, "action_entries", lambda: [action])
    monkeypatch.setattr(entries, "user_entries", lambda: [{"id": "h3"}])
    monkeypatch.setattr(entries, "ready_entries", lambda: [])
    with pytest.raises(ValueError, match="ambiguous generation entry"):
        entries.find_entry("h3")
