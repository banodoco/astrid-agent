"""The Popen guard must stay a subclassable type (astrid-dev#651).

On Windows, importing asyncio runs ``class Popen(subprocess.Popen)`` in
asyncio.windows_utils. When the guard replaced subprocess.Popen with a function,
that import raised TypeError inside every generation child process.
"""
import subprocess
import sys

import pytest

from astrid.core.execution import network_policy


@pytest.fixture
def guarded(monkeypatch):
    originals = {}
    events = []
    monkeypatch.setattr(network_policy, "_record", lambda kind, **fields: events.append((kind, fields)))
    network_policy._patch_native_descendants(originals)
    try:
        yield originals, events
    finally:
        for (owner, name), value in originals.items():
            setattr(owner, name, value)


def test_guard_is_a_subclassable_popen_type(guarded):
    originals, _ = guarded
    assert isinstance(subprocess.Popen, type)
    assert issubclass(subprocess.Popen, originals[(subprocess, "Popen")])

    class Popen(subprocess.Popen):  # what asyncio.windows_utils does at import
        pass

    assert issubclass(Popen, originals[(subprocess, "Popen")])


def test_guard_still_denies_and_allows(guarded, monkeypatch):
    _, events = guarded
    monkeypatch.setattr(network_policy, "_validated_descendant_owner", lambda: False)
    with pytest.raises(network_policy.NetworkPolicyError):
        subprocess.Popen([sys.executable, "-c", "pass"])
    assert events[-1][1]["allowed"] is False

    monkeypatch.setattr(network_policy, "_validated_descendant_owner", lambda: True)
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    assert isinstance(process, subprocess.Popen)
    assert process.wait(timeout=30) == 0
    assert events[-1][1]["allowed"] is True


def test_subclass_of_the_guard_is_also_checked(guarded, monkeypatch):
    monkeypatch.setattr(network_policy, "_validated_descendant_owner", lambda: False)

    class Popen(subprocess.Popen):
        pass

    with pytest.raises(network_policy.NetworkPolicyError):
        Popen([sys.executable, "-c", "pass"])
