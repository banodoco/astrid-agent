"""Command-local ownership resets the ContextVar but does not clear captured dicts.

Copied async contexts that outlive the command can therefore retain and observe
their original dictionary. Clearing the dictionary on exit is a possible
Plan 82/review follow-up, outside this test-only card.
"""
from __future__ import annotations

import pytest

from astrid.core.generation import cli
from astrid.core.generation.preflight import GenerationReadinessError
from astrid.core.pack import discovery
from astrid.sdk.generation import GenerationFacade


def test_dry_run_cli_owns_fresh_pack_discovery_cache_per_invocation(monkeypatch):
    owners = []

    def command(_argv):
        owner = discovery._discovery_cache.get()
        assert isinstance(owner, dict)
        assert owner == {}
        owners.append(owner)
        owner["invocation"] = len(owners)
        return 0

    monkeypatch.setattr(cli, "_main", command)

    assert cli.main(["image", "a dog", "--dry-run"]) == 0
    assert discovery._discovery_cache.get() is None
    assert cli.main(["image", "a cat", "--dry-run"]) == 0
    assert discovery._discovery_cache.get() is None

    assert len(owners) == 2
    assert owners[0] is not owners[1]
    assert owners == [{"invocation": 1}, {"invocation": 2}]


def test_dry_run_cli_resets_pack_discovery_owner_after_command_exception(monkeypatch):
    owners = []

    def command(_argv):
        owner = discovery._discovery_cache.get()
        assert owner == {}
        owner["failed"] = True
        owners.append(owner)
        raise RuntimeError("command failed")

    monkeypatch.setattr(cli, "_main", command)

    with pytest.raises(GenerationReadinessError, match="command failed"):
        cli.main(["image", "a dog", "--dry-run"])
    assert isinstance(owners[0], dict)
    assert discovery._discovery_cache.get() is None
    # The module accessor is reset; an already captured reference still sees
    # the object, as documented above.
    assert owners[0] == {"failed": True}


def test_sdk_image_owns_fresh_pack_discovery_cache_per_invocation(monkeypatch):
    owners = []
    initial_contents = []

    def media_request(_self, _modality, **_kwargs):
        owner = discovery._discovery_cache.get()
        assert isinstance(owner, dict)
        owners.append(owner)
        initial_contents.append(dict(owner))
        owner["seen"] = len(owners)
        return "admitted"

    monkeypatch.setattr(GenerationFacade, "_media_request", media_request)
    facade = GenerationFacade()

    assert facade.image(prompt="a dog") == "admitted"
    assert discovery._discovery_cache.get() is None
    assert facade.image(prompt="a cat") == "admitted"
    assert discovery._discovery_cache.get() is None

    assert len(owners) == 2
    assert owners[0] is not owners[1]
    assert initial_contents == [{}, {}]
    assert owners == [{"seen": 1}, {"seen": 2}]


def test_sdk_image_resets_pack_discovery_owner_after_inner_exception(monkeypatch):
    owners = []

    def media_request(_self, _modality, **_kwargs):
        owner = discovery._discovery_cache.get()
        assert isinstance(owner, dict)
        owners.append(owner)
        owner["seen"] = True
        raise RuntimeError("inner generation failed")

    monkeypatch.setattr(GenerationFacade, "_media_request", media_request)

    with pytest.raises(RuntimeError, match="inner generation failed"):
        GenerationFacade().image(prompt="a dog")
    assert isinstance(owners[0], dict)
    assert discovery._discovery_cache.get() is None
    assert owners[0] == {"seen": True}

