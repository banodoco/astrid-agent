"""Ready entry discovery preserves resource failure precedence."""

from __future__ import annotations

import itertools
import sys
import types
from pathlib import Path

import pytest

from astrid.core.generation import entries
from astrid.core.model_catalog.registry import ModelRegistry


_STAGES = ("index", "coverage", "discovery", "registry")


@pytest.mark.parametrize("failing_stages", list(itertools.combinations(_STAGES, 2)))
def test_ready_entries_reports_first_resource_failure(monkeypatch, failing_stages):
    calls = []
    failures = {stage: ValueError(stage) for stage in failing_stages}

    def stage(name, result):
        calls.append(name)
        if name in failures:
            raise failures[name]
        return result

    resources = types.ModuleType("vibecomfy.resources")
    resources.ready_templates_root = lambda: Path("/templates/ready")
    resources.template_index = lambda: stage("index", {"templates": []})
    resources.coverage_manifest = lambda: stage("coverage", {"workflows": []})
    package = types.ModuleType("vibecomfy")
    package.__path__ = []
    package.resources = resources
    monkeypatch.setitem(sys.modules, "vibecomfy", package)
    monkeypatch.setitem(sys.modules, "vibecomfy.resources", resources)
    monkeypatch.setattr(entries, "_discover_ready_workflows", lambda: stage("discovery", object()))
    monkeypatch.setattr(
        ModelRegistry, "load_default", classmethod(lambda cls: stage("registry", object()))
    )

    with pytest.raises(ValueError) as caught:
        entries.ready_entries()

    first = failing_stages[0]
    assert caught.value is failures[first]
    assert calls == list(_STAGES[: _STAGES.index(first) + 1])
