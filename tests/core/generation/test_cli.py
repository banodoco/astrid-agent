"""The public generate command stays a thin adapter over the SDK facade."""

from __future__ import annotations

import json
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli


def _facade(monkeypatch):
    import astrid

    calls = []

    def invoke(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, to_dict=lambda: {"image_paths": ["dog.png"]})

    facade = SimpleNamespace(image=invoke, video=invoke)
    monkeypatch.setitem(astrid.__dict__, "generate", facade)
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext("runtime-client"))
    return calls


def test_bare_prompt_uses_auto_target_and_sdk_default_model(monkeypatch, capsys):
    calls = _facade(monkeypatch)

    assert cli.main(["image", "a dog eating an ice cream", "--project", "demo"]) == 0

    assert calls == [{
        "model": None, "target": "auto", "dry_run": False,
        "prompt": "a dog eating an ice cream", "project": "demo",
        "client": "runtime-client",
    }]
    assert "dog.png" in capsys.readouterr().out


@pytest.mark.parametrize(("modality", "dimension_input"), [("image", "size"), ("video", "resolution")])
def test_entry_edit_and_settings_reach_the_existing_facade_once(monkeypatch, capsys, modality, dimension_input):
    calls = _facade(monkeypatch)
    target = "http://127.0.0.1:8188"
    assert cli.main([
        modality, "a dog", "--entry", "flux-schnell", "--mode", "edit",
        "--target", target, "--project", "demo", "--out", "export",
        "--edit", "reference.png", "--width", "768", "--height", "512",
        "--steps", "8", "--seed", "41", "--cfg", "1.5", "--json",
    ]) == 0
    assert calls == [{
        "model": "flux-schnell", "mode": "edit", "target": target,
        "dry_run": False, "prompt": "a dog", "project": "demo", "out": "export",
        "image_ref": "reference.png", dimension_input: "768x512",
        "steps": 8, "seed": 41, "guidance_scale": 1.5,
        "client": "runtime-client",
    }]
    assert json.loads(capsys.readouterr().out)["image_paths"] == ["dog.png"]


def test_dry_run_is_forwarded_to_sdk_without_a_second_invocation(monkeypatch, capsys):
    calls = _facade(monkeypatch)
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("dry-run connected to Runtime"))
    assert cli.main(["image", "a dog", "--model", "flux-schnell", "--target", "managed", "--dry-run"]) == 0
    assert len(calls) == 1
    assert calls[0]["model"] == "flux-schnell"
    assert calls[0]["target"] == "managed"
    assert calls[0]["dry_run"] is True
    assert "client" not in calls[0]
    assert json.loads(capsys.readouterr().out)["image_paths"] == ["dog.png"]


@pytest.mark.parametrize("args", [
    ["image", "a dog", "--width", "512"],
    ["image", "a dog", "--width", "0", "--height", "512"],
    ["video", "   "],
])
def test_invalid_request_does_not_invoke(monkeypatch, capsys, args):
    calls = _facade(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code == 2
    assert not calls
    assert capsys.readouterr().err


def test_gateway_preserves_coded_actionable_target_error(monkeypatch, capsys):
    from astrid.core import gateway
    from astrid.sdk.exceptions import CapabilityPreconditionError
    import astrid

    def fail(**_kwargs):
        error = CapabilityPreconditionError("No Comfy target is ready.")
        error.code = "COMFY_TARGET_UNAVAILABLE"
        error.field = "target"
        error.fix = "Use --target http://127.0.0.1:8188 after starting ComfyUI."
        raise error

    monkeypatch.setitem(astrid.__dict__, "generate", SimpleNamespace(image=fail))
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext("runtime-client"))
    assert gateway.main(["generate", "image", "a dog"]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "code: COMFY_TARGET_UNAVAILABLE" in captured.err
    assert "fix: Use --target http://127.0.0.1:8188" in captured.err
    assert "this is a bug" not in captured.err


def test_runtime_connection_stays_open_until_generation_finishes(monkeypatch, capsys):
    import astrid

    events = []
    client = object()

    @contextmanager
    def open_client():
        events.append("connect")
        try:
            yield client
        finally:
            events.append("close")

    def generate(**kwargs):
        assert kwargs["client"] is client
        assert events == ["connect"]
        events.append("complete")
        return SimpleNamespace(ok=True, to_dict=lambda: {"image_paths": ["dog.png"]})

    monkeypatch.setattr(cli, "_open_client", open_client)
    monkeypatch.setitem(astrid.__dict__, "generate", SimpleNamespace(image=generate))
    assert cli.main(["image", "a dog", "--project", "demo"]) == 0
    assert events == ["connect", "complete", "close"]


@pytest.mark.parametrize("modality", ["image", "video"])
def test_installed_console_dispatches_generate_to_the_gateway(monkeypatch, modality):
    from astrid import console
    from astrid.core import gateway

    calls = []
    monkeypatch.setattr(gateway, "main", lambda argv: calls.append(argv) or 0)
    assert console.main(["generate", modality, "a dog", "--target", "runpod"]) == 0
    assert calls == [["generate", modality, "a dog", "--target", "runpod"]]


def _public_main(entrypoint):
    from astrid import console
    from astrid.core import gateway

    return {"console": console.main, "gateway": gateway.main}[entrypoint]


@pytest.mark.parametrize("entrypoint", ["console", "gateway"])
def test_public_listing_preserves_kind_and_json(monkeypatch, capsys, entrypoint):
    from astrid.core.generation import listing

    calls = []
    rows = [{"id": "example-video", "kind": "video"}]
    monkeypatch.setattr(listing, "generation_rows", lambda kind: calls.append(kind) or rows)
    monkeypatch.setattr(cli, "main", lambda args: pytest.fail("list reached generation parser"))

    assert _public_main(entrypoint)(["generate", "list", "--kind", "video", "--json"]) == 0
    assert calls == ["video"]
    assert json.loads(capsys.readouterr().out) == rows


@pytest.mark.parametrize("entrypoint", ["console", "gateway"])
@pytest.mark.parametrize("modality", ["image", "video"])
def test_public_generation_forms_reach_the_existing_facade(monkeypatch, capsys, entrypoint, modality):
    from astrid.core.generation import listing

    calls = _facade(monkeypatch)
    monkeypatch.setattr(listing, "main", lambda args: pytest.fail("generation reached listing parser"))

    assert _public_main(entrypoint)([
        "generate", modality, "a dog", "--target", "http://127.0.0.1:8188", "--json",
    ]) == 0
    assert calls == [{
        "model": None, "target": "http://127.0.0.1:8188", "dry_run": False,
        "prompt": "a dog", "client": "runtime-client",
    }]
    assert json.loads(capsys.readouterr().out)["image_paths"] == ["dog.png"]


@pytest.mark.parametrize(("args", "handler"), [
    (["list", "--json"], "listing"),
    (["image", "a dog", "--target", "managed"], "cli"),
    (["video", "a dog", "--target", "auto"], "cli"),
])
def test_console_uses_process_arguments_for_each_generate_form(monkeypatch, args, handler):
    import sys
    from astrid import console
    from astrid.core.generation import listing

    calls = []
    modules = {"listing": listing, "cli": cli}
    monkeypatch.setattr(modules[handler], "main", lambda argv: calls.append(argv) or 7)
    other = "cli" if handler == "listing" else "listing"
    monkeypatch.setattr(modules[other], "main", lambda argv: pytest.fail("wrong generate parser"))
    monkeypatch.setattr(sys, "argv", ["astrid", "generate", *args])

    assert console.main() == 7
    assert calls == [args]


@pytest.mark.parametrize("entrypoint", ["console", "gateway"])
@pytest.mark.parametrize(("args", "diagnostic"), [
    (["list", "--target", "managed"], "unrecognized arguments: --target managed"),
    (["list", "--kind", "model"], "invalid choice: 'model'"),
    (["image", "a dog", "--kind", "image"], "unrecognized arguments: --kind image"),
    (["video", "a dog", "--steps", "many"], "argument --steps: invalid int value"),
    ([], "the following arguments are required: command"),
    (["unknown"], "invalid choice: 'unknown'"),
])
def test_public_generate_rejects_invalid_options_in_the_intended_parser(
    monkeypatch, capsys, entrypoint, args, diagnostic,
):
    from astrid.core.generation import listing

    calls = _facade(monkeypatch)
    monkeypatch.setattr(listing, "generation_rows", lambda kind: pytest.fail("invalid list probed readiness"))

    assert _public_main(entrypoint)(["generate", *args]) == 2
    error = capsys.readouterr().err
    assert diagnostic in error
    if not args or args[0] == "unknown":
        assert "{list}" in error
    assert not calls


def test_generation_help_does_not_load_sdk_or_start_runtime(monkeypatch, capsys):
    import builtins

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name not in {"astrid.sdk.exceptions", "astrid.sdk.client"}
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(SystemExit) as exc:
        cli.main(["image", "--help"])
    assert exc.value.code == 0
    assert "--target" in capsys.readouterr().out
