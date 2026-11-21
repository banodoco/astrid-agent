"""LS-15: selected built-in resident action is the only parent cache bridge."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution.resident_sessions import (
    ResidentProtocolError,
    host_preparation_for_record,
)


def _record(*, selected=True, resident=True, path="actions/run/resident.py", action="vibecomfy.run"):
    built_in = Path(__file__).resolve().parents[3] / "astrid" / "packs" / "vibecomfy"
    metadata = {
        "source_pack": "vibecomfy",
        "pack_root": str(built_in if selected else built_in.parent / "external"),
        "action_declaration": {"session": {"kind": "resident", "protocol": "astrid.pack-session.v1"}}
        if resident else {},
        "resident_invocation": {"kind": "python", "path": path, "function": "run"},
    }
    definition = SimpleNamespace(metadata=metadata)
    return SimpleNamespace(id=action, definition=definition, captured_from=None)


def test_host_preparation_uses_the_admitted_resident_action(monkeypatch):
    from astrid.core.pack import resolver

    host, task, inputs = object(), {"id": "task-1"}, {"workflow": "captured.json"}
    record = _record()
    calls = []
    sentinel = object()

    def resolve(module, function):
        assert module == "astrid.packs.vibecomfy.actions.run.resident"
        assert function == "host_preparation"

        def prepare(*args, **kwargs):
            calls.append((args, kwargs))
            return sentinel

        return prepare

    monkeypatch.setattr(resolver, "importlib_resolve", resolve)
    assert host_preparation_for_record(
        host, record, task, inputs, align="align", entry="workflow"
    ) is sentinel
    assert calls == [((host, record, task, inputs), {"align": "align", "entry": "workflow"})]


@pytest.mark.parametrize("option", ["not-resident", "other-action", "external-source"])
def test_no_undeclared_or_external_host_execution(monkeypatch, option):
    from astrid.core.pack import resolver

    def forbidden(*_args, **_kwargs):
        raise AssertionError("untrusted host callback was imported")

    monkeypatch.setattr(resolver, "importlib_resolve", forbidden)
    record = _record(
        selected=option != "external-source",
        resident=option != "not-resident",
        action="other.run" if option == "other-action" else "vibecomfy.run",
    )
    assert host_preparation_for_record(object(), record, {}, {}, align="check", entry=None) is None


def test_invalid_declared_resident_route_fails_closed(monkeypatch):
    from astrid.core.pack import resolver

    monkeypatch.setattr(resolver, "importlib_resolve", lambda *_: (_ for _ in ()).throw(
        AssertionError("an invalid resident route reached the resolver")))
    with pytest.raises(ResidentProtocolError, match="requires its declared resident entry"):
        host_preparation_for_record(
            object(), _record(path="actions/other.py"), {}, {}, align="check", entry=None
        )


def test_pack_owned_action_entry_delegates_cache_without_changing_its_context(monkeypatch):
    from astrid.packs.vibecomfy import resident
    from astrid.packs.vibecomfy.actions.run.resident import host_preparation

    host, record, task, inputs = object(), object(), {"task": 1}, {"workflow": "input.json"}
    seen, sentinel = [], object()

    def select(*args, **kwargs):
        seen.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(resident, "host_cache", select)
    assert host_preparation(host, record, task, inputs, align="check", entry=None) is sentinel
    assert seen == [((host, record, task, inputs), {"align": "check", "entry": None})]
