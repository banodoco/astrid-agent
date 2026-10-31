"""CPU-only coverage of the public H3 adapter."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid import console
from astrid.core import action_cli, gateway
from astrid.packs.h3_av import cli
from astrid.packs.h3_av.shared.request import load_request, normalize_request
from astrid.packs.h3_av.shared.spec import canonical_spec


def _document() -> dict:
    return {"version": 2, "prompt": "A quiet cafe.", "duration": 5,
            "media": [{"id": "reference", "asset": "look.png", "role": "reference", "modality": "image"}],
            "settings": {}}


def _write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_registration_and_public_help(capsys, monkeypatch) -> None:
    calls = []
    original = cli.main
    monkeypatch.setattr(cli, "main", lambda args: calls.append(args) or 7)
    assert gateway.main(["h3", "plan"]) == 7
    assert console.main(["h3", "edit"]) == 7
    assert calls == [["plan"], ["edit"]]
    monkeypatch.setattr(cli, "main", original)
    assert gateway.main(["h3", "--help"]) == 0
    help_text = capsys.readouterr().out.lower()
    for command in ("plan", "run", "compose", "edit"):
        assert command in help_text
    for forbidden in ("journal", "custody", "worker", "prepare", "compile", "verify"):
        assert forbidden not in help_text
    for command in ("plan", "run", "compose", "edit"):
        assert gateway.main(["h3", command, "--help"]) == 0
        text = capsys.readouterr().out.lower()
        assert not any(term in text for term in ("journal", "custody", "worker", "prepare", "compile", "verify"))


def test_edit_writes_valid_spec_and_updates_existing(tmp_path, capsys) -> None:
    path = tmp_path / "spec.json"
    assert gateway.main(["h3", "edit", "--prompt", "A quiet cafe.", "--duration", "5", "--reference", "look.png", "--out", str(path)]) == 0
    assert json.loads(path.read_text()) == _document()
    assert canonical_spec(load_request(path)) == canonical_spec(normalize_request(_document()))
    revised = tmp_path / "revised.json"
    assert gateway.main(["h3", "edit", "--spec", str(path), "--prompt", "A busy cafe.", "--out", str(revised)]) == 0
    assert load_request(revised).value["prompt"] == "A busy cafe."
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["spec_path"] == str(revised)


def test_edit_rejects_invalid_spec_before_writing(tmp_path) -> None:
    path = tmp_path / "invalid.json"
    assert gateway.main(["h3", "edit", "--prompt", "", "--duration", "5", "--reference", "look.png", "--out", str(path)]) != 0
    assert not path.exists()


def test_edit_updates_v1_without_changing_other_request_fields(tmp_path) -> None:
    document = {
        "version": 1, "operation": "generate", "source": None,
        "content": {"prompt": "A quiet cafe."}, "output": {"duration": 5},
        "changes": {"video": [], "audio": []},
        "references": [{"asset": "look.png", "purpose": "appearance"}],
        "overrides": {"seed": 17},
    }
    source = _write(tmp_path / "source.json", document)
    out = tmp_path / "edited.json"
    prompt = " \tA busy café.  \n"
    assert console.main(["h3", "edit", "--spec", str(source), "--prompt", prompt,
                         "--duration", "6", "--out", str(out)]) == 0
    document["content"]["prompt"] = prompt
    document["output"]["duration"] = 6
    assert json.loads(out.read_text()) == document
    assert canonical_spec(load_request(out)) == canonical_spec(normalize_request(document))


@pytest.mark.parametrize(("container", "flag"), [("content", "--prompt"), ("output", "--duration")])
@pytest.mark.parametrize("invalid", [None, "not an object", []])
def test_edit_rejects_invalid_v1_container_without_writing(tmp_path, capsys, container, flag, invalid) -> None:
    source = _write(tmp_path / "source.json", {"version": 1, container: invalid})
    out = tmp_path / "edited.json"
    assert console.main(["h3", "edit", "--spec", str(source), flag, "6", "--out", str(out)]) != 0
    assert "h3.request.invalid" in capsys.readouterr().err
    assert not out.exists()


def test_plan_verb_equals_handwritten_and_does_not_call_execution(tmp_path, monkeypatch, capsys) -> None:
    from astrid.packs.h3_av.actions.transform import run as transform

    def forbidden(*args, **kwargs):
        raise AssertionError("planning reached execution")

    monkeypatch.setattr(action_cli, "main", forbidden)
    for name in ("_resolve_local_engine", "_run_remote_placement", "_invoke_stage", "_invoke_canonical_run",
                 "ensure_runpod_worker", "load_worker_qualification", "generate"):
        monkeypatch.setattr(transform, name, forbidden)
    monkeypatch.setattr(transform.AstridClient, "open_from_launcher", forbidden)
    from PIL import Image

    image_path = tmp_path / "look.png"
    Image.new("RGB", (32, 32), color=(32, 64, 128)).save(image_path)
    assets = _write(tmp_path / "assets.json", {"look.png": str(image_path)})
    out = tmp_path / "plan"
    assert gateway.main(["h3", "plan", "--prompt", "A quiet cafe.", "--duration", "5", "--reference", "look.png", "--asset-map", str(assets), "--out", str(out)]) == 0
    verb_plan = json.loads(capsys.readouterr().out)
    spec_path = _write(tmp_path / "request.json", _document())
    assert gateway.main(["h3", "plan", "--spec", str(spec_path), "--asset-map", str(assets), "--out", str(out)]) == 0
    spec_plan = json.loads(capsys.readouterr().out)
    assert verb_plan == spec_plan
    assert verb_plan["spec"]["prompt"] == "A quiet cafe."
    assert verb_plan == json.loads((out / "plan.json").read_text())
    assert all(not item["available"] for item in verb_plan["capabilities"].values())
    assert not (out / "operation.json").exists()


@pytest.mark.parametrize("command", ["run", "compose"])
def test_unavailable_capability_never_invokes_action(tmp_path, monkeypatch, capsys, command) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("unavailable capability reached execution")

    monkeypatch.setattr(action_cli, "main", forbidden)
    assert gateway.main(["h3", command, "--prompt", "A quiet cafe.", "--duration", "5", "--reference", "look.png", "--out", str(tmp_path / "out")]) != 0
    assert "h3.capability.unavailable" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_ops_routes_to_existing_entrypoints(monkeypatch) -> None:
    from astrid.packs.h3_av.actions.transform import run as transform

    seen = []
    monkeypatch.setattr(transform, "main", lambda args: seen.append(args) or 9)
    assert gateway.main(["h3", "ops", "transform", "--help"]) == 9
    assert seen == [["--help"]]


@pytest.mark.parametrize("command", ["run", "compose"])
def test_available_capability_delegates_to_transform_action(tmp_path, monkeypatch, command) -> None:
    from astrid.packs.h3_av.shared.spec import H3Spec

    monkeypatch.setattr(H3Spec, "capabilities", property(lambda self: {
        op: {"available": True} for op in ("generate", "extend", "regenerate", "preserve")
    }))
    calls = []

    def action(args):
        calls.append(args)
        assert json.loads(Path(args[args.index("--request") + 1]).read_text()) == _document()
        assert json.loads(Path(args[args.index("--asset-map") + 1]).read_text()) == {}
        return 7

    monkeypatch.setattr(action_cli, "main", action)
    assert gateway.main(["h3", command, "--prompt", "A quiet cafe.", "--duration", "5", "--reference", "look.png", "--out", str(tmp_path / "out")]) == 7
    assert calls[0][:2] == ["run", "h3_av.transform"]
