"""CPU-only coverage of the public H3 adapter."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


from astrid.core import action_cli
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


def _help_text(capsys, *args: str) -> str:
    """argparse prints help and exits 0; the pack CLI does not return it."""
    with pytest.raises(SystemExit) as exit_info:
        cli.main(list(args))
    assert exit_info.value.code == 0
    return capsys.readouterr().out.lower()


def test_registration_and_public_help(capsys, monkeypatch) -> None:
    calls = []
    original = cli.main
    monkeypatch.setattr(cli, "main", lambda args: calls.append(args) or 7)
    assert cli.main(["plan"]) == 7
    assert cli.main(["edit"]) == 7
    assert calls == [["plan"], ["edit"]]
    monkeypatch.setattr(cli, "main", original)
    help_text = _help_text(capsys, "--help")
    for command in ("plan", "run", "compose", "edit"):
        assert command in help_text
    for forbidden in ("journal", "custody", "worker", "prepare", "compile", "verify"):
        assert forbidden not in help_text
    for command in ("plan", "run", "compose", "edit"):
        text = _help_text(capsys, command, "--help")
        assert not any(term in text for term in ("journal", "custody", "worker", "prepare", "compile", "verify"))


def test_edit_writes_valid_spec_and_updates_existing(tmp_path, capsys) -> None:
    path = tmp_path / "spec.json"
    assert cli.main(["edit", "--prompt", "A quiet cafe.", "--duration", "5", "--reference", "look.png", "--out", str(path)]) == 0
    assert json.loads(path.read_text()) == _document()
    assert canonical_spec(load_request(path)) == canonical_spec(normalize_request(_document()))
    revised = tmp_path / "revised.json"
    assert cli.main(["edit", "--spec", str(path), "--prompt", "A busy cafe.", "--out", str(revised)]) == 0
    assert load_request(revised).value["prompt"] == "A busy cafe."
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["spec_path"] == str(revised)


def test_edit_rejects_invalid_spec_before_writing(tmp_path) -> None:
    path = tmp_path / "invalid.json"
    assert cli.main(["edit", "--prompt", "", "--duration", "5", "--reference", "look.png", "--out", str(path)]) != 0
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
    assert cli.main(["edit", "--spec", str(source), "--prompt", prompt,
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
    assert cli.main(["edit", "--spec", str(source), flag, "6", "--out", str(out)]) != 0
    assert "h3.request.invalid" in capsys.readouterr().err
    assert not out.exists()


def test_plan_verb_equals_handwritten_and_does_not_call_execution(tmp_path, monkeypatch, capsys) -> None:
    from astrid.core.generation import cli as generation_cli

    def forbidden(*args, **kwargs):
        raise AssertionError("planning reached execution")

    monkeypatch.setattr(action_cli, "main", forbidden)
    monkeypatch.setattr(generation_cli, "main", forbidden)
    from PIL import Image

    image_path = tmp_path / "look.png"
    Image.new("RGB", (32, 32), color=(32, 64, 128)).save(image_path)
    assets = _write(tmp_path / "assets.json", {"look.png": str(image_path)})
    out = tmp_path / "plan"
    assert cli.main(["plan", "--prompt", "A quiet cafe.", "--duration", "5", "--reference", "look.png", "--asset-map", str(assets), "--out", str(out)]) == 0
    verb_plan = json.loads(capsys.readouterr().out)
    spec_path = _write(tmp_path / "request.json", _document())
    assert cli.main(["plan", "--spec", str(spec_path), "--asset-map", str(assets), "--out", str(out)]) == 0
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
    assert cli.main([command, "--prompt", "A quiet cafe.", "--duration", "5", "--reference", "look.png", "--out", str(tmp_path / "out")]) != 0
    assert "h3.capability.unavailable" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()




@pytest.mark.parametrize("command", ["run", "compose"])
def test_available_capability_delegates_to_the_generate_path(tmp_path, monkeypatch, command) -> None:
    from astrid.core.generation import cli as generation_cli
    from astrid.packs.h3_av.shared.spec import H3Spec

    monkeypatch.setattr(H3Spec, "capabilities", property(lambda self: {
        op: {"available": True} for op in ("generate", "extend", "regenerate", "preserve")
    }))
    calls = []

    def generate(args):
        calls.append(args)
        inputs = json.loads(Path(args[args.index("--inputs-json") + 1]).read_text())
        assert json.loads(Path(inputs["request"]).read_text()) == _document()
        assert inputs["asset_map"] == {}
        return 7

    monkeypatch.setattr(generation_cli, "main", generate)
    assert cli.main([command, "--prompt", "A quiet cafe.", "--duration", "5", "--reference", "look.png", "--out", str(tmp_path / "out")]) == 7
    assert calls[0][:4] == ["video", "--entry", "h3", "--inputs-json"]
