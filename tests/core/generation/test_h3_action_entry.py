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
        ("request", "file", True), ("asset_map", "file", False),
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


def test_action_entry_routes_producer_files_once_and_keeps_validation(monkeypatch):
    entry = {"id": "h3", "action": "h3_av.transform", "kind": "video", "modes": ["transform"],
             "inputs": [{"name": "request", "type": "file", "required": True},
                        {"name": "asset_map", "type": "file", "required": False}]}
    monkeypatch.setattr(entries, "find_entry", lambda name: entry if name == "h3" else None)
    monkeypatch.setattr(entries, "describe_entry", lambda *args, **kwargs: {
        **entry, "source": "action", "mode": "transform"})
    import astrid.sdk as sdk
    monkeypatch.setattr(sdk, "_load_model_registry", lambda **kwargs: object())
    calls = []
    client = SimpleNamespace(invoke_result=lambda *args, **kwargs: calls.append((args, kwargs)) or "admitted")
    facade = GenerationFacade()
    with pytest.raises(CapabilityValidationError, match="asset_map"):
        facade.video(model="h3", request="request.json", client=client)
    with pytest.raises(CapabilityValidationError, match="unsupported entry inputs.*prompt"):
        facade.video(model="h3", request="request.json", asset_map="assets.json", prompt="fake", client=client)
    with pytest.raises(CapabilityValidationError, match="model and entry"):
        facade.video(model="other", entry="h3", request="request.json", asset_map="assets.json", client=client)
    assert calls == []
    assert facade.video(entry="h3", request="request.json", asset_map="assets.json", project="p", client=client) == "admitted"
    assert len(calls) == 1 and calls[0][0] == ("h3_av.transform",)
    assert calls[0][1]["kind"] == "action" and calls[0][1]["project"] == "p"


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
