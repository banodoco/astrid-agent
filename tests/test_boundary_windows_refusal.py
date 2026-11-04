"""Windows boundary refusals retain data and intent without manager mutations.

These host-independent tests use disposable files and a manager that rejects
every command. They do not qualify native Windows registration or adoption.
Replace the refusal expectations only with the complete Windows boundary.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from astrid.installer import boundary_handoff, boundary_registration, r0_stop
from astrid.installer.core import Version
from astrid.installer.service import Service


def snapshot(root):
    return {
        str(path.relative_to(root)): (
            path.stat().st_mode, path.stat().st_size, path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in (root, *root.rglob("*"))
    }


@pytest.fixture
def case(tmp_path, monkeypatch):
    # Service's Windows selection/intent defaults must never resolve to the
    # developer's real LOCALAPPDATA, even when this suite runs on Windows.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("No manager or runtime command is authorized")

    root = tmp_path / "program"
    folder = root / "versions/r0"
    folder.mkdir(parents=True)
    version = Version(folder, folder / "python.exe", folder / "profile.json", {})
    (folder / "version.json").write_text(json.dumps(version.document()), encoding="utf-8")
    (root / "current").write_text("r0\n", encoding="utf-8")
    data = tmp_path / "data"
    realm = data / "runtime/realms/r0"
    realm.mkdir(parents=True)
    for name, content in {
        "owner.lock": b"untouched lock", "realm.sqlite3": b"opaque R0 DB",
        "realm.sqlite3-wal": b"opaque R0 WAL", "discovery.json": b"do not parse",
    }.items():
        (realm / name).write_bytes(content)
    service = Service(root, tmp_path / "log", forbidden, home=tmp_path, system="win32")
    service.path.write_text("unverified task definition", encoding="utf-8")
    intent = r0_stop._marker_path(service)
    intent.parent.mkdir(parents=True)
    backend = SimpleNamespace(service=service, stage=forbidden, runtime=forbidden,
                              verify=forbidden, recover_r1_registration=forbidden)
    return SimpleNamespace(root=root, data=data, realm=realm, service=service,
                           intent=intent, backend=backend, calls=calls, forbidden=forbidden)


def test_windows_registration_refuses_before_publishing_or_querying(case, tmp_path):
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="not qualified"):
        boundary_registration.BoundaryRegistration(case.service)
    assert snapshot(tmp_path) == before
    assert case.calls == []


def test_windows_public_handoff_refuses_before_staging_or_stopping(case, tmp_path):
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="not qualified"):
        boundary_handoff.handle(case.root, case.data, case.backend,
                                manifest_loader=case.forbidden, say=case.forbidden)
    assert snapshot(tmp_path) == before
    assert case.calls == []


@pytest.mark.parametrize("rollback", [False, True])
def test_windows_interrupted_handoff_retains_both_roots_and_journal(case, tmp_path, rollback):
    destination = tmp_path / "data-r1"
    destination.mkdir()
    (destination / "accepted-work").write_bytes(b"never remove an uncertain copy")
    journal = boundary_handoff.journal_path(case.service)
    journal.write_text('{"phase":"starting","system":"win32"}\n', encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="not qualified"):
        boundary_handoff.handle(case.root, case.data, case.backend, rollback=rollback)
    assert snapshot(tmp_path) == before
    assert case.calls == []


def test_windows_stop_does_not_create_intent_or_change_unverified_task(case, tmp_path):
    before = snapshot(tmp_path)
    with pytest.raises(r0_stop.R0StopError, match="cannot be verified"):
        r0_stop.stop_r0_and_wait(case.service, case.realm, data_root=case.data, timeout=0)
    assert snapshot(tmp_path) == before
    assert case.calls == []


@pytest.mark.parametrize("changed_definition", [False, True])
def test_windows_recovery_never_reenables_unverified_task(case, tmp_path, changed_definition):
    intent = {"version": 1, "system": "win32", "program_root": str(case.root),
              "data_root": str(case.data), "unit": str(case.service.path),
              "unit_sha256": r0_stop._unit_digest(case.service)}
    case.intent.write_text(json.dumps(intent), encoding="utf-8")
    if changed_definition:
        case.service.path.write_text("replacement task", encoding="utf-8")
    before = snapshot(tmp_path)
    # Repeated install/recovery attempts must retain the same evidence. An
    # unchanged local XML alone also cannot authorize enabling a manager job.
    for _ in range(2):
        with pytest.raises(r0_stop.R0StopError, match="recovery marker was retained"):
            r0_stop.recover_r0(case.service)
        assert snapshot(tmp_path) == before
        assert case.calls == []
