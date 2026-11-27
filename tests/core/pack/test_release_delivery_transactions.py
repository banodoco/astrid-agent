"""Installer receipts bind real group publication and interrupted rollback."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.core.pack.identity import read_pin
from astrid.core.pack.install import PackInstallError
from astrid.core.pack.release_manifest import install_release_manifest
from astrid.core.pack.transaction import read_group, rollback_group
from tests.core.pack.test_defaults import Repository


def test_receipt_is_written_before_publication_and_exception_preserves_live(tmp_path):
    repo = Repository(tmp_path / "repo")
    root = tmp_path / "home/Astrid/packs"
    first = install_release_manifest(manifest=repo.defaults_manifest(), packs_root=root)
    old = (root / ".group.json").read_bytes()
    repo.write_pack("default_one", "new\n")
    repo.publish()
    seen = []

    def refuse(receipt):
        seen.append(receipt)
        assert read_group(root).transaction_id == first.transaction_id
        assert receipt["previous_transaction_id"] == first.transaction_id
        assert receipt["previous_revision"] == first.revision
        assert receipt["transaction_id"] != first.transaction_id
        assert receipt["pins"]["default_one"]["commit"] == repo.commit
        raise OSError("receipt could not be made durable")

    with pytest.raises(OSError, match="durable"):
        install_release_manifest(manifest=repo.defaults_manifest(), packs_root=root, on_prepared=refuse)
    assert len(seen) == 1
    assert (root / ".group.json").read_bytes() == old
    assert (root / "default_one/payload.txt").read_text() == "default v1\n"


def test_initial_rollback_requires_exact_prepared_previous_and_current_pins(tmp_path):
    repo = Repository(tmp_path / "repo")
    root = tmp_path / "home/Astrid/packs"
    receipt = []
    result = install_release_manifest(manifest=repo.defaults_manifest(), packs_root=root,
                                      on_prepared=receipt.append)
    record, = receipt
    before = (root / ".group.json").read_bytes()
    with pytest.raises(PackInstallError) as ordinary:
        rollback_group(packs_root=root, transaction_id=result.transaction_id)
    assert ordinary.value.code == "uncommitted_generation"
    with pytest.raises(PackInstallError) as wrong_previous:
        rollback_group(packs_root=root, transaction_id=result.transaction_id,
                       pack_ids=record["pack_ids"], expected_pins=record["pins"],
                       expected_previous=("0" * 32, 1))
    assert wrong_previous.value.code == "rollback_conflict"
    assert (root / ".group.json").read_bytes() == before
    rollback_group(packs_root=root, transaction_id=result.transaction_id,
                   pack_ids=record["pack_ids"], expected_pins=record["pins"], expected_previous=(None, 0))
    assert not read_group(root).paths


@pytest.mark.parametrize("phase", ["before_swap", "swapped"])
def test_real_process_exit_leaves_an_exact_recoverable_publication(tmp_path, phase):
    repo = Repository(tmp_path / "repo")
    root = tmp_path / "home/Astrid/packs"
    first = install_release_manifest(manifest=repo.defaults_manifest(), packs_root=root)
    original = {name: (path / "payload.txt").read_bytes() for name, path in first.paths.items()}
    repo.write_pack("default_one", "new\n")
    repo.publish()
    manifest = tmp_path / "release.json"
    manifest.write_text(json.dumps(repo.defaults_manifest()))
    receipt = tmp_path / "receipt.json"
    script = """
import json, os, sys
from pathlib import Path
from astrid.core.pack.release_manifest import install_release_manifest
from astrid.installer.core import write_json
def boundary(phase):
    if phase == sys.argv[4]:
        os._exit(73)
install_release_manifest(manifest=Path(sys.argv[1]), packs_root=Path(sys.argv[2]),
                         on_prepared=lambda value: write_json(Path(sys.argv[3]), value), boundary=boundary)
"""
    ran = subprocess.run([sys.executable, "-c", script, str(manifest), str(root), str(receipt), phase],
                         capture_output=True, text=True, timeout=90)
    assert ran.returncode == 73, ran.stdout + ran.stderr
    record = json.loads(receipt.read_text())
    observed = read_group(root)
    if phase == "before_swap":
        assert observed.transaction_id == first.transaction_id
    else:
        assert observed.transaction_id == record["transaction_id"]
        rollback_receipts = []
        rollback_group(packs_root=root, transaction_id=record["transaction_id"],
                       pack_ids=record["pack_ids"], expected_pins=record["pins"],
                       expected_previous=(record["previous_transaction_id"], record["previous_revision"]),
                       on_prepared=rollback_receipts.append)
        assert len(rollback_receipts) == 1
        assert read_group(root).transaction_id == rollback_receipts[0]["transaction_id"]
    assert {name: (path / "payload.txt").read_bytes() for name, path in read_group(root).paths.items()} == original


def test_removed_default_has_explicit_absence_pin_in_receipt(tmp_path):
    repo = Repository(tmp_path / "repo")
    root = tmp_path / "home/Astrid/packs"
    initial = install_release_manifest(manifest=repo.defaults_manifest(), packs_root=root)
    receipt = []
    changed = install_release_manifest(manifest=repo.defaults_manifest(("default_one",)), packs_root=root,
                                       on_prepared=receipt.append)
    record, = receipt
    assert record["pins"]["default_two"] is None
    rollback_group(packs_root=root, transaction_id=changed.transaction_id, pack_ids=record["pack_ids"],
                   expected_pins=record["pins"], expected_previous=(initial.transaction_id, initial.revision))
    assert set(read_group(root).paths) == {"default_one", "default_two"}
    assert read_pin(root / "default_two/PIN")["origin"] == "default"


def _all_user_bytes(root):
    return {p.relative_to(root).as_posix():
            ("link", os.readlink(p)) if p.is_symlink() else
            ("directory", None) if p.is_dir() else ("file", p.read_bytes())
            for p in root.rglob("*")}


def _removed_default(tmp_path):
    repo = Repository(tmp_path / "repo")
    root = tmp_path / "home/Astrid/packs"
    initial = install_release_manifest(manifest=repo.defaults_manifest(), packs_root=root)
    receipts = []
    changed = install_release_manifest(manifest=repo.defaults_manifest(("default_one",)), packs_root=root,
                                       on_prepared=receipts.append)
    record, = receipts
    assert record["pins"]["default_two"] is None
    assert not (root / "default_two").exists()
    return root, dict(packs_root=root, transaction_id=changed.transaction_id,
                     pack_ids=record["pack_ids"], expected_pins=record["pins"],
                     expected_previous=(initial.transaction_id, initial.revision))


def _recreate_user_path(root, kind):
    from astrid.core.pack.transaction import collection_write_custody
    target = root / "default_two"
    if kind == "public-new":
        ran = subprocess.run([sys.executable, "-B", "-m", "astrid.core.pack.cli", "new", "default_two",
                              "--destination", str(target), "--role", "action"],
                             capture_output=True, text=True, timeout=60)
        assert ran.returncode == 0, ran.stdout + ran.stderr
    with collection_write_custody(root):
        if kind == "file":
            target.write_bytes(b"user file at the removed pack's name\x00\xff")
        elif kind == "dangling-link":
            target.symlink_to(root.parent / "user-selected-missing-target")
        elif kind == "empty-directory":
            target.mkdir()
        else:
            target.mkdir(exist_ok=True)
            if kind != "public-new":
                (target / "pack.yaml").write_text(
                    "schema_version: 3\nid: default_two\nname: User\nversion: 1.0.0\n"
                    'pack_api: "2.0"\ndocumentation: {kind: none, reason: User pack.}\n')
            (target / "user-sentinel.bin").write_bytes(b"new user work\x00\xff\n")
    assert not (target / "PIN").exists()


@pytest.mark.parametrize("kind", ["public-new", "unpinned-pack", "file", "empty-directory", "dangling-link"])
def test_absence_receipt_refuses_every_present_named_object_and_preserves_user_bytes(tmp_path, kind):
    root, request = _removed_default(tmp_path)
    state = (root / ".group.json").read_bytes()
    _recreate_user_path(root, kind)
    before = _all_user_bytes(root)
    with pytest.raises(PackInstallError) as refused:
        rollback_group(**request)
    assert refused.value.code == "rollback_conflict"
    assert (root / ".group.json").read_bytes() == state
    assert read_group(root).transaction_id == request["transaction_id"]
    assert _all_user_bytes(root) == before


def test_absence_receipt_rechecks_after_prepared_callback(tmp_path):
    root, request = _removed_default(tmp_path)
    state = (root / ".group.json").read_bytes()
    written = []

    def on_prepared(_receipt):
        _recreate_user_path(root, "unpinned-pack")
        written.append(_all_user_bytes(root))

    with pytest.raises(PackInstallError) as refused:
        rollback_group(**request, on_prepared=on_prepared)
    assert refused.value.code == "rollback_conflict"
    assert len(written) == 1
    assert _all_user_bytes(root) == written[0]
    assert (root / ".group.json").read_bytes() == state


@pytest.mark.parametrize("also_write_published", [False, True], ids=["retired-write", "both-generations-written"])
def test_absence_receipt_rechecks_the_exchanged_generation_and_keeps_late_user_bytes(
        tmp_path, monkeypatch, also_write_published):
    from astrid.core.pack import transaction
    root, request = _removed_default(tmp_path)
    state = (root / ".group.json").read_bytes()
    publish = transaction._publish_collection
    authored = []

    def race(candidate, live, *, restore=False):
        if not restore:
            # This is after the last prepublication check, immediately before
            # the actual exchange. Both exchanges still use the real primitive.
            _recreate_user_path(live, "unpinned-pack")
            authored.append(_all_user_bytes(live))
        publish(candidate, live, restore=restore)
        if not restore and also_write_published:
            (live / "default_two/new-generation-user.txt").write_bytes(b"user write after exchange\n")

    monkeypatch.setattr(transaction, "_publish_collection", race)
    with pytest.raises(PackInstallError) as refused:
        rollback_group(**request)
    assert refused.value.code == "rollback_conflict"
    assert len(authored) == 1
    assert _all_user_bytes(root) == authored[0]
    assert (root / ".group.json").read_bytes() == state
    if also_write_published:
        retained = list((root.parent / ".astrid-retired").glob("refused-*/default_two/new-generation-user.txt"))
        assert len(retained) == 1
        assert retained[0].read_bytes() == b"user write after exchange\n"
        assert str(retained[0].parent.parent) in str(refused.value)


def test_absent_tombstoned_target_stays_removed_on_receipt_rollback(tmp_path):
    from astrid.core.pack.transaction import install_group
    repo = Repository(tmp_path / "repo")
    root = tmp_path / "home/Astrid/packs"
    first = install_release_manifest(manifest=repo.defaults_manifest(), packs_root=root)
    receipts = []
    removed = install_group(source="", packs={}, packs_root=root, python_packages={},
                            remove=("default_two",), on_prepared=receipts.append)
    record, = receipts
    rollback_group(packs_root=root, transaction_id=removed.transaction_id, pack_ids=record["pack_ids"],
                   expected_pins=record["pins"], expected_previous=(first.transaction_id, first.revision))
    assert not (root / "default_two").exists()
    assert read_group(root).removals == ("default_two",)
    assert read_pin(root / "default_one/PIN")["origin"] == "default"
