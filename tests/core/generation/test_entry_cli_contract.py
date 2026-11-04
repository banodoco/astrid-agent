"""Public entry metadata and structured input boundaries stay metadata-only."""
from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli, entries


def _description(source="catalog", kind="image", mode="t2i"):
    fields = [{"name": "prompt", "type": "string", "required": True}]
    if mode == "edit":
        fields.append({"name": "image_ref", "type": "string", "required": True})
    else:
        fields.append({"name": "seed", "type": "integer", "required": False, "default": 42})
    if source == "workflow":
        fields.append({"name": "paint_strength", "type": "number", "required": True})
    if source == "action":
        fields = [{"name": "request", "type": "file", "required": True},
                  {"name": "asset_map", "type": "file", "required": True}]
    modes = [mode] if source != "catalog" else ["t2i", "edit"]
    row = {"id": source, "kind": kind, "source": source, "modes": modes,
           "mode": mode, "inputs": fields}
    if source == "action":
        row["action"] = "h3_av.transform"
    return row


@pytest.fixture
def declared(monkeypatch):
    calls = []

    def describe(entry_id, mode=None, **kwargs):
        calls.append((entry_id, mode))
        source = entry_id
        return _description(source, "video" if source == "action" else "image",
                            mode or ("transform" if source == "action" else "t2i"))

    monkeypatch.setattr(entries, "describe_entry", describe)
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("opened Runtime before validation"))
    return calls


@pytest.mark.parametrize("entrypoint", ["console", "gateway"])
def test_public_describe_uses_only_metadata(monkeypatch, capsys, declared, entrypoint):
    from astrid import console
    from astrid.core import gateway
    monkeypatch.setattr(entries, "resolve_entry_inputs", lambda *a, **k: pytest.fail("describe resolved inputs"))
    main = console.main if entrypoint == "console" else gateway.main
    assert main(["generate", "describe", "--entry", "catalog", "--mode", "edit", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == _description(mode="edit")
    assert declared == [("catalog", "edit")]


@pytest.mark.parametrize(("source", "kind", "mode"), [
    ("catalog", "image", "edit"), ("workflow", "image", "t2i"), ("action", "video", "transform"),
])
def test_context_help_agrees_with_describe_without_admission(monkeypatch, capsys, declared, source, kind, mode):
    monkeypatch.setattr(entries, "resolve_entry_inputs", lambda *a, **k: pytest.fail("help validated inputs"))
    with pytest.raises(SystemExit) as exc:
        cli.main([kind, "--entry", source, "--mode", mode, "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    projection = _description(source, kind, mode)
    assert f"Entry: {projection['id']} ({source}, {kind})" in help_text
    assert "Modes: " + ", ".join(projection["modes"]) in help_text
    for field in projection["inputs"]:
        assert f"{field['name']} ({field['type']}; " in help_text
    assert declared == [(source, mode)]


def test_json_and_convenience_values_use_shared_resolver(monkeypatch, capsys, tmp_path, declared):
    import astrid
    captured = []
    values = {"prompt": "paint a star", "paint_strength": 0.4}
    file = tmp_path / "inputs.json"
    file.write_text(json.dumps(values))
    expected_description, expected = entries.resolve_entry_inputs("workflow", {**values, "seed": 19},
                                                                 mode="t2i", kind="image")
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext("client"))
    monkeypatch.setattr(cli, '_generate_facade', lambda: SimpleNamespace(
        image=lambda **kwargs: captured.append(kwargs) or {"ok": True}))
    assert cli.main(["image", "--entry", "workflow", "--inputs-json", str(file), "--seed", "19", "--json"]) == 0
    assert captured == [{"entry": "workflow", "mode": expected_description["mode"],
                         "target": "auto", "dry_run": False, "client": "client", **expected}]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


@pytest.mark.parametrize(("body", "flags", "reason"), [
    ('{"prompt":"x","prompt":"y"}', [], "duplicate JSON input"),
    ('[]', [], "must contain a JSON object"),
    ('{"prompt":"x","seed":"bad"}', [], "requires an integer"),
    ('{"prompt":"x","seed":true}', [], "requires an integer"),
    ('{"prompt":"x","unknown":1}', [], "unsupported entry inputs"),
    ('{"prompt":"x","seed":NaN}', [], "invalid JSON constant"),
    ('{"prompt":"x"}', ["another prompt"], "duplicate input values"),
    ('{"prompt":"x","seed":1}', ["--seed", "1"], "duplicate input values"),
    ('{"prompt":"x"}', ["--seed", "1", "--seed", "2"], "specified more than once"),
    ('{}', [], "missing required entry inputs"),
])
def test_malformed_unknown_conflicting_inputs_reject_before_runtime(tmp_path, capsys, declared, body, flags, reason):
    file = tmp_path / "inputs.json"
    file.write_text(body)
    with pytest.raises(SystemExit) as exc:
        cli.main(["image", *flags, "--entry", "catalog", "--inputs-json", str(file)])
    assert exc.value.code == 2
    assert reason in capsys.readouterr().err


def test_action_json_reaches_fail_closed_sdk_without_opening_runtime(monkeypatch, capsys, tmp_path, declared):
    import astrid
    from astrid.sdk.exceptions import CapabilityPreconditionError
    from astrid.core.contracts.errors import AstridError
    request, assets = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    file = tmp_path / "inputs.json"
    file.write_text(json.dumps({"request": request, "asset_map": assets}))
    captured = []

    def reject(**kwargs):
        captured.append(kwargs)
        raise CapabilityPreconditionError("tracked parent interface is not yet available")

    monkeypatch.setattr(cli, '_generate_facade', lambda: SimpleNamespace(video=reject))
    assert cli.main(["video", "--entry", "action", "--inputs-json", str(file), '--json']) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['issues'][0]['code'] == 'missing_dependency'
    assert 'tracked parent interface' in payload['issues'][0]['message']
    assert len(captured) == 1
    assert captured[0]["request"] == request
    assert "client" not in captured[0]


def test_unspecialized_context_help_shows_each_mode_without_probes(monkeypatch, capsys):
    import socket
    import subprocess

    modes = {mode: _description(mode=mode) for mode in ("t2i", "edit")}
    projection = {**modes["t2i"], "mode": None, "inputs": [], "mode_descriptions": modes}
    monkeypatch.setattr(entries, "describe_entry", lambda *a, **k: projection)
    monkeypatch.setattr(entries, "target_schema", lambda *a, **k: pytest.fail("help probed target schema"))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("help opened Runtime"))
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("help opened a socket"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("help started a process"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["image", "--entry", "catalog", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "Inputs for mode t2i:" in help_text
    assert "Inputs for mode edit:" in help_text
    assert "seed (integer; optional, default=42)" in help_text
    assert "image_ref (string; required)" in help_text


def test_conflicting_selectors_reject_before_metadata_or_runtime(capsys, declared):
    with pytest.raises(SystemExit) as exc:
        cli.main(["image", "hello", "--entry", "catalog", "--model", "different"])
    assert exc.value.code == 2
    assert "select different generation entries" in capsys.readouterr().err
    assert not declared


def test_context_help_exposes_declared_aliases_and_constraints(monkeypatch, capsys):
    projection = _description(source="workflow")
    field = projection["inputs"][-1]
    field.update(aliases=["texture"], min=0, max=1)
    monkeypatch.setattr(entries, "describe_entry", lambda *a, **k: projection)
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("help opened Runtime"))
    with pytest.raises(SystemExit) as result:
        cli.main(["image", "--entry", "workflow", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert "Aliases: texture" in help_text
    assert "min: 0" in help_text and "max: 1" in help_text
