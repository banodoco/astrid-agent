"""Freeze the pack-facing contract and its version history."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
from pathlib import Path
import subprocess
import sys

import pytest

import astrid.pack_api as api

SNAPSHOT = Path(__file__).with_name("pack_api_surface.json")
# Published history is append-only, even when a newer snapshot is added.
FROZEN_1_0_SHA256 = "190007d72228c3bb4faa8da7afc1338425d10379fea7f1060ce2501975132a3f"


class _ProjectRootDefault:
    def __repr__(self):
        return "<project root>"


def _signature(value):
    signature = inspect.signature(value)
    root = Path(api.__file__).resolve().parents[1]
    parameters = [
        param.replace(default=_ProjectRootDefault())
        if isinstance(param.default, Path) and param.default == root else param
        for param in signature.parameters.values()
    ]
    return re.sub(r" at 0x[0-9a-fA-F]+(?=>)", "", str(signature.replace(parameters=parameters)))


def _surface():
    result = {}
    for name in api.__all__:
        if name == "VERSION":
            continue
        value = getattr(api, name)
        kind = "class" if inspect.isclass(value) else "callable" if callable(value) else "value"
        signature = None
        if callable(value):
            try:
                signature = _signature(value)
            except (TypeError, ValueError):
                # Some builtin-derived exception classes have no Python signature.
                signature = "<unavailable>"
        result[name] = {"kind": kind, "signature": signature}
    return result


def _digest(surface):
    return hashlib.sha256(json.dumps(surface, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _check_evolution(old_version, old, new_version, new):
    assert new_version >= old_version, "pack API version cannot decrease"
    removed = old.keys() - new.keys()
    changed = {name for name in old.keys() & new.keys() if old[name] != new[name]}
    added = new.keys() - old.keys()
    if removed or changed:
        assert new_version[0] > old_version[0], "removal or signature change requires a major bump"
    elif added:
        assert new_version[0] > old_version[0] or (new_version[0] == old_version[0] and new_version[1] > old_version[1]), "addition requires a minor bump"


def test_pack_api_snapshot_and_version_history():
    history = json.loads(SNAPSHOT.read_text())
    assert _digest(history["1.0"]) == FROZEN_1_0_SHA256, "published 1.0 snapshot must not be rewritten"
    versions = sorted((tuple(map(int, key.split("."))), key) for key in history)
    assert all(len(version) == 2 and all(part >= 0 for part in version) for version, _ in versions)
    assert isinstance(api.VERSION, tuple) and len(api.VERSION) == 2
    assert all(type(part) is int and part >= 0 for part in api.VERSION)
    assert versions[-1][0] == api.VERSION, "add a snapshot for the current VERSION"
    for (old_version, old_key), (new_version, new_key) in zip(versions, versions[1:]):
        _check_evolution(old_version, history[old_key], new_version, history[new_key])
    assert len(api.__all__) == len(set(api.__all__))
    assert set(api.__all__) == set(api._EXPORTS) | {"VERSION"}
    assert _surface() == history[versions[-1][1]], "public surface changed: bump VERSION and add a snapshot"


def test_version_policy_rejects_unversioned_and_breaking_changes():
    old = {"f": {"kind": "callable", "signature": "(x)"}}
    added = {**old, "g": {"kind": "value", "signature": None}}
    changed = {"f": {"kind": "callable", "signature": "(x, y)"}}
    for version, new in [((1, 0), added), ((1, 1), changed), ((1, 1), {})]:
        with pytest.raises(AssertionError):
            _check_evolution((1, 0), old, version, new)
    _check_evolution((1, 0), old, (1, 1), added)
    _check_evolution((1, 0), old, (2, 0), changed)
    _check_evolution((1, 0), old, (2, 0), {})


def test_added_public_name_without_version_bump_fails_snapshot(monkeypatch):
    monkeypatch.setitem(api._EXPORTS, "new_public_name", ("astrid.core.util.time", "utc_now_iso"))
    monkeypatch.setattr(api, "__all__", [*api.__all__, "new_public_name"])
    with pytest.raises(AssertionError, match="public surface changed"):
        test_pack_api_snapshot_and_version_history()


def test_facade_import_is_lazy_and_pack_free():
    root = str(Path(__file__).resolve().parents[2])
    code = "import sys, astrid.pack_api; assert not any(n == 'astrid.packs' or n.startswith('astrid.packs.') for n in sys.modules)"
    subprocess.run([sys.executable, "-c", code], check=True, env={**os.environ, "PYTHONPATH": root})
    assert all(not module.startswith("astrid.packs") for module, _ in api._EXPORTS.values())
    with pytest.raises(AttributeError):
        getattr(api, "not_a_pack_api_name")
