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
