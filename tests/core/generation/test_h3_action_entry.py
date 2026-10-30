from contextlib import nullcontext
import json
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
        ("execution_request", "file", False), ("worker_qualification", "file", False),
        ("prepared_worker", "file", False), ("cleanup_receipt", "file", False),
        ("require_worker_qualification", "boolean", False), ("resume", "boolean", False),
        ("dry_run", "boolean", False),
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
             "inputs": [{"name": "request", "type": "file", "required": True, "file_custody": "producer_local"},
                        {"name": "asset_map", "type": "file", "required": True, "file_custody": "producer_local"}]}
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


@pytest.mark.parametrize("local_files", [["missing"], ["flag"], ["file", "file"], "file"])
def test_invalid_producer_local_declaration_fails_closed(local_files):
    capability = SimpleNamespace(
        id="example.action",
        definition={"metadata": {"generation_entry": {"id": "bad", "kind": "video",
                    "modes": ["transform"], "inputs": ["file", "flag"],
                    "producer_local_inputs": local_files}}},
        inputs=(SimpleNamespace(name="file", type="file"), SimpleNamespace(name="flag", type="boolean")),
    )
    with pytest.raises(ValueError, match="invalid generation entry"):
        entries.action_entries(SimpleNamespace(actions=[capability]))


@pytest.fixture
def public_h3_contract_boundary(monkeypatch):
    """Observe public validation at invoke_result, before Runtime admission."""
    import astrid.sdk as sdk

    calls = []
    client = SimpleNamespace(
        invoke_result=lambda *args, **kwargs: calls.append((args, kwargs)) or {"contract_valid": True},
    )
    monkeypatch.setattr(sdk.AstridClient, "open_from_launcher", lambda **_kwargs: nullcontext(client))
    return client, calls


def test_public_h3_cli_and_sdk_preserve_the_same_producer_local_inputs(
    tmp_path, capsys, public_h3_contract_boundary,
):
    from astrid import console, generate

    client, calls = public_h3_contract_boundary
    values = {"request": str(tmp_path / "request.json"), "asset_map": str(tmp_path / "assets.json")}
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps(values), encoding="utf-8")
    description, resolved = entries.resolve_entry_inputs("h3", values, kind="video")
    assert description["action"] == "h3_av.transform" and resolved == values

    assert generate.video(entry="h3", project="project", client=client, **values) == {"contract_valid": True}
    assert console.main([
        "generate", "video", "--entry", "h3", "--inputs-json", str(inputs),
        "--project", "project", "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {"contract_valid": True}
    assert len(calls) == 2
    for args, kwargs in calls:
        assert args == ("h3_av.transform",)
        assert kwargs["inputs"] == resolved
        assert kwargs["kind"] == "action" and kwargs["project"] == "project"
        assert kwargs["wait"] is True and kwargs["read_managed_outputs"] is True


@pytest.mark.parametrize("invalid,diagnostic", [
    ({"request": ""}, "missing required entry inputs: request"),
    ({"request": None}, "request"),
    ({"request": True}, "requires a string"),
    ({"request": {"digest": "sha256:malformed"}}, "requires a string"),
    ({"request": {"digest": "sha256:" + "a" * 64}}, "requires a string"),
    ({"asset_map": {"object_id": "sha256:" + "a" * 64}}, "requires a string"),
    ({"undeclared_input": "unexpected"}, "unsupported entry inputs: undeclared_input"),
])
def test_public_h3_cli_and_sdk_reject_the_same_invalid_entry_inputs(
    tmp_path, capsys, public_h3_contract_boundary, invalid, diagnostic,
):
    from astrid import console, generate

    client, calls = public_h3_contract_boundary
    values = {"request": str(tmp_path / "request.json"), "asset_map": str(tmp_path / "assets.json"), **invalid}
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps(values), encoding="utf-8")

    with pytest.raises(CapabilityValidationError, match=diagnostic):
        generate.video(entry="h3", project="project", client=client, **values)
    assert console.main([
        "generate", "video", "--entry", "h3", "--inputs-json", str(inputs),
        "--project", "project", "--json",
    ]) == 2
    captured = capsys.readouterr()
    assert not captured.out and diagnostic in captured.err
    assert calls == []
