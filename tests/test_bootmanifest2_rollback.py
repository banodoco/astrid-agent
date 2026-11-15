"""A same-schema failed update restores boot provenance before activating R12."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.gateway.dispatch import compose_profile_handoff
from astrid.installer.core import Version, rollback_transaction
from tests.helpers.bootmanifest2_r12 import LEGACY_FIXTURE, r12_export, r12_probe


def _state(tmp_path):
    # The installer passes the DATA root; the host support root is data/runtime.
    support = tmp_path / "data" / "runtime"
    path = support / "astrid-host/boot-manifest.json"
    path.parent.mkdir(parents=True)
    # Preserve formatting too: the rollback promise is byte-for-byte.
    original = b"\n" + LEGACY_FIXTURE.read_bytes()
    path.write_bytes(original)
    return support, path, original


def _rollback(tmp_path, support, activate, *, backup=None, kind="migration", restore_workspace=True):
    events = []
    versions = [Version(tmp_path / str(r), tmp_path / str(r) / "python",
                        tmp_path / str(r) / "profile", {}, r) for r in (12, 13)]
    transaction = dict(old=versions[0].document(), candidate=versions[1].document(),
                       realm={}, backup=backup, transaction_kind=kind, step="switched")
    backend = SimpleNamespace(restore=lambda *a: events.append("restore"))

    def activate_old(*_args):
        result = activate()
        events.append("activate_old:" + result)
        assert result == "PASS", events

    rollback_transaction(
        tmp_path, support.parent, backend, transaction,
        stop=lambda *a: events.append("stop"),
        switch_old=lambda *a: events.append("switch_old"),
        save=lambda step: events.append(step), activate_old=activate_old,
        restore_workspace=restore_workspace,
    )
    return events


@pytest.mark.parametrize("kind", ["migration", "app"])
@pytest.mark.parametrize("restore_workspace", [True, False])
def test_rollback_without_workspace_backup_restores_original_bytes(tmp_path, kind, restore_workspace):
    support, path, original = _state(tmp_path)
    compose_profile_handoff(path, support_root=support)
    assert path.read_bytes() != original

    def activate():
        return "PASS" if path.read_bytes() == original else "BootManifestDrift"

    events = _rollback(tmp_path, support, activate, kind=kind, restore_workspace=restore_workspace)
    assert events == ["stop", "recovering", "switch_old", "rollback_selected", "activate_old:PASS", "rolled_back"]
    assert path.read_bytes() == original
    # Replaying rollback after interruption must preserve the same old bytes.
    _rollback(tmp_path, support, activate, kind=kind, restore_workspace=restore_workspace)
    assert path.read_bytes() == original


def test_restamp_backup_is_atomic_and_never_overwritten(tmp_path, monkeypatch):
    from astrid.core._shared import boot_manifest as bm

    support, path, original = _state(tmp_path)
    backup = path.with_name(path.name + ".pre-restamp")
    replace = bm.platform.replace_path

    def checked_replace(source, destination):
        assert backup.read_bytes() == original  # durable before the new stamp is visible
        return replace(source, destination)

    monkeypatch.setattr(bm.platform, "replace_path", checked_replace)
    compose_profile_handoff(path, support_root=support)
    assert backup.read_bytes() == original
    identity = backup.stat()
    path.write_bytes(LEGACY_FIXTURE.read_bytes())
    compose_profile_handoff(path, support_root=support)
    assert backup.read_bytes() == original
    assert backup.stat() == identity
    assert not list(path.parent.glob(".boot-manifest-*.tmp"))


def test_no_sibling_backup_removes_new_stamp_before_old_writer(tmp_path):
    support, path, _original = _state(tmp_path)
    path.unlink()
    compose_profile_handoff(path, support_root=support)
    assert path.exists()
    legacy = json.loads(LEGACY_FIXTURE.read_text())

    def old_writer():
        # R12 stamp_boot_manifest's existing-file comparison and absent-file path;
        # the fixture was captured from its own registry/fixtures (see provenance).
        if path.exists() and json.loads(path.read_text()) != legacy:
            return "BootManifestDrift"
        if not path.exists():
            path.write_bytes(LEGACY_FIXTURE.read_bytes())
        return "PASS"

    _rollback(tmp_path, support, old_writer)
    assert path.read_bytes() == LEGACY_FIXTURE.read_bytes()


@pytest.mark.integration
@pytest.mark.linux_native
@pytest.mark.parametrize("retain_backup", [True, False])
def test_real_rollback_activates_r12_own_composition(tmp_path, r12_export, retain_backup):
    support, path, original = _state(tmp_path)
    assert r12_probe(r12_export, support) == {
        "outcome": "PASS", "equals_live": True, "unchanged": True, "created": False,
    }
    compose_profile_handoff(path, support_root=support)
    assert path.read_bytes() != original
    assert r12_probe(r12_export, support)["outcome"] == "BootManifestDrift"
    if not retain_backup:
        path.with_name(path.name + ".pre-restamp").unlink(missing_ok=True)
    result = {}

    def activate():
        result.update(r12_probe(r12_export, support))
        return result["outcome"]

    events = _rollback(tmp_path, support, activate)
    assert "activate_old:PASS" in events
    assert result["equals_live"] is True
    assert result["unchanged"] is retain_backup
    assert result["created"] is not retain_backup
    assert path.read_bytes() == (original if retain_backup else LEGACY_FIXTURE.read_bytes())
