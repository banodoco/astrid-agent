"""Index failures retain precedence over later ready-entry discovery work."""

import pytest
from vibecomfy import resources

from astrid.core.generation import entries
from astrid.core.model_catalog.registry import ModelRegistry


def _fail(message):
    def fail(*args, **kwargs):
        raise ValueError(message)
    return fail


def test_index_failure_precedes_discovery_failure(monkeypatch):
    monkeypatch.setattr(resources, "template_index", _fail("index"))
    monkeypatch.setattr(entries, "_discover_ready_workflows", _fail("discovery"))

    with pytest.raises(ValueError, match="^index$"):
        entries._ready_entries_for("missing")


def test_index_failure_precedes_registry_failure(monkeypatch):
    monkeypatch.setattr(resources, "template_index", _fail("index"))
    monkeypatch.setattr(entries, "_discover_ready_workflows", lambda: object())
    monkeypatch.setattr(ModelRegistry, "load_default", _fail("registry"))

    with pytest.raises(ValueError, match="^index$"):
        entries._EntryDiscovery().ready_for("missing")
