"""Ready-entry reuse preserves lookup guards before projecting registry rows."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from astrid.core.generation import entries
from astrid.core.model_catalog.registry import ModelRegistry


def _install_template_index(monkeypatch, templates):
    resources = types.ModuleType("vibecomfy.resources")
    resources.ready_templates_root = lambda: Path("/templates/ready")
    resources.template_index = lambda: {"templates": templates}
    resources.coverage_manifest = lambda: {"workflows": []}
    package = types.ModuleType("vibecomfy")
    package.__path__ = []
    package.resources = resources
    monkeypatch.setitem(sys.modules, "vibecomfy", package)
    monkeypatch.setitem(sys.modules, "vibecomfy.resources", resources)
    monkeypatch.setattr(entries, "user_entries", lambda: [])
    monkeypatch.setattr(entries, "action_entries", lambda: [])


def _install_empty_registry(monkeypatch, calls):
    class EmptyRegistry:
        def list_all(self, *, include_closed=False):
            return []

    monkeypatch.setattr(
        ModelRegistry, "load_default", classmethod(lambda cls: calls.append("registry") or EmptyRegistry())
    )


def test_absent_explicit_id_ignores_unrelated_index_row_without_path(monkeypatch):
    _install_template_index(monkeypatch, [{"id": "unrelated"}])
    registry_calls = []
    _install_empty_registry(monkeypatch, registry_calls)
    monkeypatch.setattr(entries, "_discover_ready_workflows", lambda: object())

    assert entries._EntryDiscovery().find_entry("absent") is None
    assert registry_calls == ["registry"]


def test_ready_discovery_error_precedes_registry_projection(monkeypatch):
    _install_template_index(monkeypatch, [{"id": "requested", "path": "requested.json"}])
    registry_calls = []
    _install_empty_registry(monkeypatch, registry_calls)
    failure = ValueError("unreadable template directory")

    def fail_discovery():
        raise failure

    monkeypatch.setattr(entries, "_discover_ready_workflows", fail_discovery)
    discovery = entries._EntryDiscovery()

    with pytest.raises(ValueError, match="unreadable template directory") as caught:
        discovery.ready_for("requested")

    assert caught.value is failure
    assert registry_calls == []
