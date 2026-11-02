"""Freeze the pack API and retain every published version for compatibility checks."""

import inspect
import json
from pathlib import Path
import re
import subprocess
import sys
import typing

import pytest

from astrid import pack_api

SNAPSHOT = Path(__file__).with_name("pack_api_surface.json")
ROOT = str(Path(__file__).resolve().parents[2])


def _signature(value):
    try:
        signature = str(inspect.signature(value))
    except (TypeError, ValueError):
        # TypedDicts and some exception classes have no inspectable signature.
        try:
            signature = str(inspect.signature(value.__init__))
        except (AttributeError, TypeError, ValueError):
            signature = None
    if signature is not None:
        signature = re.sub(r" at 0x[0-9a-fA-F]+", "", signature)
        signature = signature.replace(ROOT, "<REPO_ROOT>")
    return signature


def _surface():
    surface = {}
    for name in pack_api.__all__:
        if name == "VERSION":
            continue
        value = getattr(pack_api, name)
        item = {"type": f"{type(value).__module__}.{type(value).__qualname__}"}
        if callable(value):
            item["signature"] = _signature(value)
        if typing.is_typeddict(value):
            item["annotations"] = {key: str(annotation) for key, annotation in sorted(value.__annotations__.items())}
            item["required_keys"] = sorted(value.__required_keys__)
            item["optional_keys"] = sorted(value.__optional_keys__)
        if isinstance(value, (str, int, float, bool, type(None))):
            item["value"] = value
        surface[name] = item
    return surface


def _compatible(previous_version, previous, current_version, current):
    """Caret policy: additions need a minor bump; breaking changes a major."""
    if current_version <= previous_version:
        return False
    breaking = any(name not in current or current[name] != item for name, item in previous.items())
    if breaking:
        return current_version[0] > previous_version[0]
    return current_version[0] > previous_version[0] or current_version[1] > previous_version[1]


def test_frozen_surface_and_version_history():
    history = json.loads(SNAPSHOT.read_text())
    assert history, "Keep all published snapshots; append a new version for changes"
    versions = [tuple(entry["version"]) for entry in history]
    assert all(len(version) == 2 and all(type(n) is int and n >= 0 for n in version) for version in versions)
    assert tuple(pack_api.VERSION) == versions[-1], "Append a snapshot for the new VERSION"
    for old, new in zip(history, history[1:]):
        assert _compatible(tuple(old["version"]), old["exports"], tuple(new["version"]), new["exports"]), "API change requires a compatible version bump"
    assert _surface() == history[-1]["exports"], "API changed: bump VERSION and append a snapshot; never rewrite published history"


@pytest.mark.parametrize(("version", "surface", "allowed"), [
    ((1, 0), {"a": "old"}, False),
    ((1, 0), {"a": "old", "b": "new"}, False),
    ((1, 1), {"a": "old", "b": "new"}, True),
    ((1, 1), {"a": "changed"}, False),
    ((1, 1), {}, False),
    ((2, 0), {"a": "changed"}, True),
    ((2, 0), {}, True),
])
def test_version_policy(version, surface, allowed):
    assert _compatible((1, 0), {"a": "old"}, version, surface) is allowed


def test_facade_is_lazy_and_imports_no_pack():
    script = """\
import sys
import astrid.pack_api as api
assert not any(name.startswith('astrid.core.') or name.startswith('astrid.packs') for name in sys.modules)
assert all(module.startswith('astrid.core.') for module in api._EXPORTS.values())
assert set(api.__all__) == set(api._EXPORTS) | {'VERSION'}
try:
    api.this_name_does_not_exist
except AttributeError:
    pass
else:
    raise AssertionError('unknown export accepted')
"""
    subprocess.run([sys.executable, "-c", script], check=True)
