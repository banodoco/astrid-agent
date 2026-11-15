"""Plan 32 T6c: one generate(entry, inputs, placement) front door; the kind comes from the entry's data."""
from __future__ import annotations

import pytest

from astrid.sdk.generation import CapabilityValidationError, GenerationFacade


class Recorder(GenerationFacade):
    def __init__(self):
        self.calls = []

    def image(self, **kw): self.calls.append(("image", kw)); return "image"
    def video(self, **kw): self.calls.append(("video", kw)); return "video"
    def audio(self, **kw): self.calls.append(("audio", kw)); return "audio"


@pytest.fixture
def facade(monkeypatch):
    kinds = {"cat-image": "image", "cat-video": "video", "cat-audio": "audio"}

    def describe(entry, mode=None, registry=None, _native=False):
        if entry not in kinds:
            raise ValueError(f"unknown generation entry {entry!r}")
        return {"kind": kinds[entry]}

    monkeypatch.setattr("astrid.core.generation.entries.describe_entry", describe)
    monkeypatch.setattr("astrid.sdk._load_model_registry", lambda **kw: object())
    return Recorder()


@pytest.mark.parametrize("entry,kind", [("cat-image", "image"), ("cat-video", "video"), ("cat-audio", "audio")])
def test_generate_takes_the_kind_from_the_entry(facade, entry, kind):
    assert facade.generate(entry, {"prompt": "x"}, "local") == kind
    assert facade.calls == [(kind, {"entry": entry, "placement": "local", "prompt": "x"})]


def test_generate_refuses_in_one_place_from_entry_data(facade):
    with pytest.raises(CapabilityValidationError, match="unknown generation entry"):
        facade.generate("nope", {})
    with pytest.raises(CapabilityValidationError, match="entry"):
        facade.generate()
    with pytest.raises(CapabilityValidationError, match="inputs"):
        facade.generate("cat-image", ["not", "a", "map"])
    assert facade.calls == []
