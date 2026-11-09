"""Ready-template discovery degrades only when vibecomfy itself is absent."""

from __future__ import annotations

import sys
import types

import pytest

from astrid.core.generation import entries


def test_absent_vibecomfy_returns_no_entries(monkeypatch):
    monkeypatch.setitem(sys.modules, "vibecomfy", None)  # import vibecomfy -> ImportError(name="vibecomfy")
    for name in [n for n in sys.modules if n.startswith("vibecomfy.")]:
        monkeypatch.setitem(sys.modules, name, None)
    assert entries._ready_entries_for() == []


def test_missing_submodule_of_present_vibecomfy_raises(monkeypatch):
    package = types.ModuleType("vibecomfy")
    package.__path__ = []  # a package with no submodules: vibecomfy.resources is missing
    monkeypatch.setitem(sys.modules, "vibecomfy", package)
    for name in [n for n in sys.modules if n.startswith("vibecomfy.")]:
        monkeypatch.delitem(sys.modules, name)
    with pytest.raises(ImportError):
        entries._ready_entries_for()
