"""Typed dispatch must honor facade overrides on every command."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from astrid.console import main
from astrid.core.generation import cli
from astrid.sdk.generation import GenerationFacade, generate


def test_cli_explicit_entry_calls_class_override(capsys):
    calls = []

    def custom_image(self, **kwargs):
        calls.append(kwargs["entry"])
        return SimpleNamespace(ok=True, to_dict=lambda: {"entry": kwargs["entry"]})

    with patch.object(type(generate), "image", custom_image), patch.object(
        cli, "_open_client", lambda: nullcontext(object())
    ):
        code = main(["generate", "image", "--entry", "z-image", "--mode", "t2i",
                     "--placement", "managed", "--dry-run", "--json", "a studio test image"])
    assert code == 0
    assert calls == ["z-image"]
    assert '"entry": "z-image"' in capsys.readouterr().out


@pytest.mark.parametrize("modality", ["image", "video", "audio"])
@pytest.mark.parametrize("override", ["class", "subclass", "instance"])
def test_typed_command_respects_alias_overrides(monkeypatch, modality, override):
    calls = []
    def custom(*args, **kwargs):
        calls.append(kwargs)
        return "override"

    if override == "subclass":
        facade_type = type("CustomFacade", (GenerationFacade,), {modality: custom})
        facade = facade_type()
    else:
        facade = GenerationFacade()
        if override == "class":
            monkeypatch.setattr(GenerationFacade, modality, custom)
        else:
            object.__setattr__(facade, modality, custom)
    assert facade._typed_command(modality, object())(entry="chosen") == "override"
    assert calls == [{"entry": "chosen"}]


def test_typed_command_respects_class_media_request_override(monkeypatch):
    calls = []
    def custom(self, modality, **kwargs):
        calls.append((modality, kwargs["entry"]))
        return "override"
    monkeypatch.setattr(GenerationFacade, "_media_request", custom)
    assert GenerationFacade()._typed_command("image", object())(entry="chosen") == "override"
    assert calls == [("image", "chosen")]
