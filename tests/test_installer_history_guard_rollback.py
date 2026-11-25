"""The SV-L5 rollback hook is offline, candidate-owned and safe to retry."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from astrid.installer import core
from astrid.installer.backend import Backend
from astrid.installer.core import RuntimeOperationError, Version


def _versions(tmp_path):
    old = Version(tmp_path / "old", tmp_path / "old/python",
                  tmp_path / "old/profile.json", {}, release=14)
    candidate = Version(tmp_path / "candidate", tmp_path / "candidate/python",
                        tmp_path / "candidate/profile.json",
                        {"ASTRID_INSTALLER_OPERATION_ID": "stale"}, release=15)
    return old, candidate


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("backup", [False, True])
@pytest.mark.parametrize("refuse", [False, True])
def test_history_guard_rollback_order(tmp_path, monkeypatch, recover, backup, refuse):
    root, data = tmp_path / "program", tmp_path / "data"
    root.mkdir()
    data.mkdir()
    old, candidate = _versions(tmp_path)
    realm = {"realm_root": str(data / "realm"), "realm_id": "realm-1"}
    retained = {"root": str(root / "backups/retained"), "kind": "clone"} if backup else None
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "transaction_kind": "migration", "step": "switched", "data_root": str(data),
        "old": old.document(), "candidate": candidate.document(), "realm": realm,
        "backup": retained,
    }
    events = []

    def remove(version, realm_root, realm_id):
        events.append(("remove", version, realm_root, realm_id))
        if refuse:
            raise RuntimeOperationError("Could not remove history guards before activating the previous Runtime")

    backend = SimpleNamespace(
        restore=lambda *args: events.append(("restore", *args)),
        remove_history_guards=remove,
    )
    callbacks = {
        "stop": lambda *args: events.append(("stop", *args)),
        "switch_old": lambda *args: events.append(("switch", *args)),
        "activate_old": lambda *args: events.append(("activate", *args)),
        "save": lambda step, **_details: events.append((step,)),
    }
    monkeypatch.setattr(core, "current", lambda _root: candidate)
    monkeypatch.setattr(core, "install_runtime", lambda _root: None)
    monkeypatch.setattr(core, "install_launcher", lambda _root: None)

    def rollback():
        if recover:
            (root / "update-transaction.json").write_text(json.dumps(transaction), encoding="utf-8")
            core.recover_transaction(root, data, backend, **callbacks)
        else:
            core.rollback_transaction(root, data, backend, transaction, **callbacks)

    if refuse:
        with pytest.raises(RuntimeOperationError, match="Could not remove history guards"):
            rollback()
    else:
        rollback()
    expected = [("stop", candidate, data), ("recovering",)]
    if backup:
        expected.append(("restore", old, data, realm, retained))
    expected.append(("remove", candidate, realm["realm_root"], realm["realm_id"]))
    if not refuse:
        expected += [
            ("switch", old, candidate), ("rollback_selected",),
            ("activate", old, data, realm), ("rolled_back",),
        ]
    assert events == expected


@pytest.mark.parametrize("changed", [True, False])
def test_backend_history_guard_removal_uses_candidate(tmp_path, monkeypatch, changed):
    _, candidate = _versions(tmp_path)
    realm_root = tmp_path / "realm"
    backend = Backend(tmp_path, tmp_path / "install.log", tmp_path / "uv")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return json.dumps({"ok": True, "changed": changed, "realm_id": "realm-1",
                           "schema_version": 33, "history_guard_layer": 0})

    monkeypatch.setattr(backend, "run", run)
    assert backend.remove_history_guards(candidate, str(realm_root), "realm-1") is None
    assert calls == [(
        [candidate.python, "-X", "utf8", "-I", "-m", "runtime_protocol.cli",
         "remove-history-guards", "--root", str(realm_root),
         "--confirm", "REMOVE HISTORY GUARDS realm-1"],
        {"extra_env": {k: v for k, v in candidate.env.items() if k != "ASTRID_INSTALLER_OPERATION_ID"},
         "timeout": 120},
    )]


@pytest.mark.parametrize("exit_code, output", [
    (2, "usage: astrid-runtime-protocol ...\nastrid-runtime-protocol: error: argument command: invalid choice: 'remove-history-guards' (choose from 'init', 'doctor')"),
    (1, json.dumps({"ok": False, "error": {"code": "validation_error",
                                          "message": "history guard rollback requires exact canonical U33"}})),
    (1, json.dumps({"ok": False, "error": {"code": "validation_error",
                                          "message": "canonical runtime_schema table is missing"}})),
])
def test_backend_history_guard_compatibility_noop(tmp_path, monkeypatch, exit_code, output):
    _, candidate = _versions(tmp_path)
    backend = Backend(tmp_path, tmp_path / "install.log", tmp_path / "uv")

    def run(argv, **_kwargs):
        raise subprocess.CalledProcessError(exit_code, argv, output=output)

    monkeypatch.setattr(backend, "run", run)
    assert backend.remove_history_guards(candidate, str(tmp_path / "realm"), "realm-1") is None


@pytest.mark.parametrize("exit_code, output", [
    (1, json.dumps({"ok": False, "error": {"code": "owner_busy", "message": "realm owned"}})),
    (1, json.dumps({"ok": False, "error": {"code": "validation_error", "message": "partial layer"}})),
    (1, json.dumps({"ok": False, "error": {"code": "conflict", "message": "activation pending"}})),
    (1, json.dumps({"ok": False, "error": {"code": "conflict",
                                          "message": "canonical runtime_schema table is missing"}})),
    (0, json.dumps({"ok": False, "error": {"message": "refused"}})),
])
def test_backend_history_guard_refusal_is_not_a_compatibility_noop(tmp_path, monkeypatch, exit_code, output):
    _, candidate = _versions(tmp_path)
    backend = Backend(tmp_path, tmp_path / "install.log", tmp_path / "uv")

    def run(argv, **_kwargs):
        if exit_code:
            raise subprocess.CalledProcessError(exit_code, argv, output=output)
        return output

    monkeypatch.setattr(backend, "run", run)
    with pytest.raises(RuntimeOperationError, match="Could not remove history guards"):
        backend.remove_history_guards(candidate, str(tmp_path / "realm"), "realm-1")



@pytest.mark.parametrize("exit_code, output", [
    (1, "no machine-readable result"),
    (2, "astrid-runtime-protocol: error: unrecognized arguments: --confirm"),
    (1, json.dumps({"ok": True, "changed": False, "realm_id": "realm-1", "history_guard_layer": 0})),
    (0, json.dumps({"ok": True, "changed": True, "realm_id": "wrong", "history_guard_layer": 0})),
    (0, json.dumps({"ok": True, "changed": False, "realm_id": "realm-1", "history_guard_layer": 1})),
    (0, json.dumps({"ok": False, "error": "not an error object"})),
    (0, "not JSON"),
])
def test_backend_history_guard_unverified_result(tmp_path, monkeypatch, exit_code, output):
    _, candidate = _versions(tmp_path)
    backend = Backend(tmp_path, tmp_path / "install.log", tmp_path / "uv")

    def run(argv, **_kwargs):
        if exit_code:
            raise subprocess.CalledProcessError(exit_code, argv, output=output)
        return output

    monkeypatch.setattr(backend, "run", run)
    reason = backend.remove_history_guards(candidate, str(tmp_path / "realm"), "realm-1")
    assert reason and reason.startswith("candidate Runtime ")


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("response", ["missing", "broken", "garbled", "timeout", "refused", "v23", "removed"])
def test_history_guard_rollback_candidate_response(tmp_path, monkeypatch, recover, response):
    root, data = tmp_path / "program", tmp_path / "data"
    root.mkdir()
    data.mkdir()
    old, candidate = _versions(tmp_path)
    realm = {"realm_root": str(data / "realm"), "realm_id": "realm-1"}
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "transaction_kind": "migration", "step": "switched", "data_root": str(data),
        "old": old.document(), "candidate": candidate.document(), "realm": realm,
        "backup": None,
    }
    journal = root / "update-transaction.json"
    journal.write_text(json.dumps(transaction), encoding="utf-8")
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    events, warnings = [], []

    def run(argv, *, extra_env, timeout):
        assert argv[0] == candidate.python
        assert timeout == 120
        events.append("remove")
        if response == "missing":
            raise FileNotFoundError("candidate missing")
        if response == "broken":
            raise subprocess.CalledProcessError(1, argv, output="cannot import Runtime")
        if response == "garbled":
            return "garbled"
        if response == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if response in {"refused", "v23"}:
            error = ({"code": "owner_busy", "message": "realm owned"} if response == "refused"
                     else {"code": "validation_error", "message": "canonical runtime_schema table is missing"})
            raise subprocess.CalledProcessError(1, argv, output=json.dumps({"ok": False, "error": error}))
        return json.dumps({"ok": True, "changed": True, "realm_id": realm["realm_id"],
                           "history_guard_layer": 0})

    def save(step, **details):
        transaction.update(step=step, **details)
        journal.write_text(json.dumps(transaction), encoding="utf-8")
        events.append(step)

    def switch(*_args):
        if response in {"missing", "broken", "garbled", "timeout"}:
            assert json.loads(journal.read_text(encoding="utf-8"))["history_guard_removal"].startswith("unverified: ")
        events.append("switch")

    monkeypatch.setattr(backend, "run", run)
    monkeypatch.setattr(core, "current", lambda _root: candidate)
    monkeypatch.setattr(core, "install_runtime", lambda _root: None)
    monkeypatch.setattr(core, "install_launcher", lambda _root: None)
    callbacks = dict(stop=lambda *_args: events.append("stop"), switch_old=switch,
                     activate_old=lambda *_args: events.append("activate"), save=save,
                     say=warnings.append)

    def rollback():
        if recover:
            core.recover_transaction(root, data, backend, **callbacks)
        else:
            core.rollback_transaction(root, data, backend, transaction, **callbacks)

    if response == "refused":
        with pytest.raises(RuntimeOperationError, match="realm owned"):
            rollback()
        assert events == ["stop", "recovering", "remove"]
        assert json.loads(journal.read_text(encoding="utf-8"))["step"] == "recovering"
        return

    rollback()
    if response in {"missing", "broken", "garbled", "timeout"}:
        assert events == ["stop", "recovering", "remove", "recovering", "switch",
                          "rollback_selected", "activate", "rolled_back"]
        receipt = json.loads(journal.read_text(encoding="utf-8"))
        assert receipt["history_guard_removal"].startswith("unverified: ")
        warning, = [line for line in warnings if line.startswith("Warning:")]
        assert "guarded realm may be refused" in warning
        assert 'astrid-runtime-protocol remove-history-guards --root "<realm root>"' in warning
        assert '--confirm "REMOVE HISTORY GUARDS <realm id>"' in warning
        assert "working release-15 Runtime" in warning
    else:
        assert events == ["stop", "recovering", "remove", "switch",
                          "rollback_selected", "activate", "rolled_back"]
        assert not any(line.startswith("Warning:") for line in warnings)
        assert "history_guard_removal" not in json.loads(journal.read_text(encoding="utf-8"))
    assert json.loads(journal.read_text(encoding="utf-8"))["step"] == "rolled_back"


def test_history_guard_unverified_old_activation_failure_names_remedy(tmp_path):
    old, candidate = _versions(tmp_path)
    realm = {"realm_root": str(tmp_path / "realm"), "realm_id": "realm-1"}
    transaction = {"old": old.document(), "candidate": candidate.document(),
                   "realm": realm, "step": "switched", "backup": None}
    backend = SimpleNamespace(remove_history_guards=lambda *_args: "candidate unavailable")
    failure = RuntimeError("older Runtime cannot open guarded realm")

    def activate(*_args):
        raise failure

    with pytest.raises(RuntimeError, match="older Runtime cannot open guarded realm") as caught:
        core.rollback_transaction(
            tmp_path, tmp_path, backend, transaction,
            stop=lambda *_args: None, switch_old=lambda *_args: None, activate_old=activate,
            save=lambda step, **details: transaction.update(step=step, **details),
            say=lambda _line: None,
        )
    assert caught.value.__cause__ is failure
    assert "working release-15 Runtime" in str(caught.value)
    assert "remove-history-guards" in str(caught.value)
    assert "re-run the update" in str(caught.value)
    assert transaction["history_guard_removal"] == "unverified: candidate unavailable"
    assert transaction["step"] == "rollback_selected"
