"""Adapter unit tests; installed-interpreter proof is a separate real venue."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.pack.identity import read_pin
from astrid.core.pack.install import PackInstallError
from astrid.core.pack.release_manifest import install_release_manifest, verify_release_default
from astrid.core.pack.transaction import install_group, read_group
from astrid.installer import default_packs as delivery
from tests.core.pack.test_defaults import Repository


@pytest.fixture
def venue(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "repository")
    versions = tmp_path / "program/versions"
    selected = []
    home = Path.home()
    root = home / "Astrid/packs"

    def candidate(name, manifest=None):
        folder = versions / name
        folder.mkdir(parents=True)
        manifest = manifest or repo.defaults_manifest()
        (folder / "manifest.json").write_text(json.dumps(manifest))
        selected[:] = [folder, manifest]
        return folder

    # This seam models only candidate selection for unit tests. No sys.prefix
    # or import-path spoofing: the separate installed proof uses a real venv.
    monkeypatch.setattr(delivery, "_version", lambda: tuple(selected))
    monkeypatch.setattr(delivery, "installed_release_manifest", lambda: tuple(selected))
    return repo, root, candidate


def test_private_prepare_clean_apply_idempotent_repair_and_initial_rollback(venue):
    repo, root, candidate = venue
    folder = candidate("first")
    delivery.prepare()
    assert not root.exists()
    applied = delivery.apply()
    assert applied["status"] == "applied"
    identity = read_group(root).transaction_id
    for pack_id, pin in repo.defaults_manifest()["packs"].items():
        assert verify_release_default(root / pack_id, pin)["origin"] == "default"
    assert delivery.needs_repair() == {"needed": False}
    assert delivery.apply(previous=folder)["status"] == "unchanged"
    assert read_group(root).transaction_id == identity
    delivery.rollback()
    assert not read_group(root).paths
    assert delivery.rollback()["status"] == "rolled_back"


def test_repair_reinstalls_missing_default_and_keeps_explicit_removal(venue):
    repo, root, candidate = venue
    folder = candidate("repair")
    delivery.prepare()
    delivery.apply()
    # An explicit removal is the only way to add a tombstone. Another missing
    # pack models an interrupted external cleanup and is repaired canonically.
    install_group(source="", packs={}, packs_root=root, python_packages={}, remove=("default_two",))
    import shutil
    shutil.rmtree(root / "default_one")
    assert delivery.needs_repair() == {"needed": True}
    record = delivery.apply(previous=folder)
    assert record["previous"] == str(folder)
    state = read_group(root)
    assert set(state.paths) == {"default_one"}
    assert state.removals == ("default_two",)
    assert delivery.needs_repair() == {"needed": False}


def test_update_rollback_restores_defaults_and_preserves_user_pack(venue):
    repo, root, candidate = venue
    old = candidate("old")
    delivery.prepare()
    delivery.apply()
    install_group(source=repo.bare.as_uri(), revision=repo.commit, packs={"user_owned": "user_owned"},
                  packs_root=root, python_packages={}, origin="user")
    user_before = {p.relative_to(root / "user_owned"): p.read_bytes()
                   for p in (root / "user_owned").rglob("*") if p.is_file()}
    repo.write_pack("default_one", "new release\n")
    repo.publish()
    candidate("new", repo.defaults_manifest(("default_one",)))
    delivery.prepare()
    assert (root / "default_one/payload.txt").read_text() == "default v1\n"
    delivery.apply(previous=old)
    assert set(read_group(root).paths) == {"default_one", "user_owned"}
    delivery.rollback(previous=old)
    assert (root / "default_one/payload.txt").read_text() == "default v1\n"
    assert (root / "default_two").is_dir()
    assert user_before == {p.relative_to(root / "user_owned"): p.read_bytes()
                           for p in (root / "user_owned").rglob("*") if p.is_file()}


@pytest.mark.parametrize("damage", ["missing", "bytes", "definitions", "admission", "epoch", "malformed-definitions", "origin", "commit", "source"])
def test_real_admission_is_required_for_default_inventory(venue, damage):
    repo, root, candidate = venue
    candidate("damaged")
    delivery.prepare()
    delivery.apply()
    pack = root / "default_one"
    if damage == "missing":
        (pack / "PIN").unlink()
    elif damage == "bytes":
        (pack / "payload.txt").write_text("changed")
    elif damage == "definitions":
        (pack / ".build/definitions.json").write_text("{}")
    elif damage == "admission":
        state = json.loads((root / ".group.json").read_text())
        state["admissions"].pop("default_one")
        (root / ".group.json").write_text(json.dumps(state))
    elif damage in {"epoch", "malformed-definitions"}:
        import hashlib
        state = json.loads((root / ".group.json").read_text())
        if damage == "epoch":
            state["admissions"]["default_one"]["transactionId"] = "0" * 32
        else:
            (pack / ".build/definitions.json").write_text("[]")
            state["admissions"]["default_one"]["definitionsDigest"] = hashlib.sha256(b"[]").hexdigest()
        (root / ".group.json").write_text(json.dumps(state))
    else:
        from astrid.core.pack.identity import write_pin
        pin = read_pin(pack / "PIN")
        pin[damage] = {"origin": "user", "commit": "0" * 40, "source": "https://example.invalid/other#default_one"}[damage]
        write_pin(pack / "PIN", pin)
    with pytest.raises((PackInstallError, OSError, ValueError)):
        verify_release_default(pack, repo.defaults_manifest()["packs"]["default_one"])
    assert delivery.needs_repair() == {"needed": True}


@pytest.mark.parametrize("damage", ["missing-prepared", "manifest", "scope", "epoch", "duplicate", "link"])
def test_invalid_receipt_or_manifest_refuses_without_collection_mutation(venue, damage):
    repo, root, candidate = venue
    folder = candidate("invalid")
    delivery.prepare()
    delivery.apply()
    before = (root / ".group.json").read_bytes()
    receipt = folder / "default-packs.json"
    record = json.loads(receipt.read_text())
    if damage == "missing-prepared":
        (folder / "default-packs-prepared.json").unlink()
    elif damage == "manifest":
        (folder / "manifest.json").write_text("{}")
    elif damage == "scope":
        record["publication"]["pack_ids"].append("personal")
        receipt.write_text(json.dumps(record))
    elif damage == "epoch":
        record["publication"]["previous_revision"] = 100
        receipt.write_text(json.dumps(record))
    elif damage == "duplicate":
        receipt.write_text('{"schema":"wrong",' + receipt.read_text()[1:])
    else:
        saved = folder / "kept.json"
        receipt.rename(saved)
        receipt.symlink_to(saved)
    with pytest.raises(PackInstallError):
        delivery.apply()
    assert (root / ".group.json").read_bytes() == before


def test_conflicting_user_pin_and_newer_group_are_preserved(venue):
    repo, root, candidate = venue
    candidate("conflict")
    delivery.prepare()
    install_release_manifest(manifest=repo.defaults_manifest(), packs_root=root, origin="user")
    before = (root / ".group.json").read_bytes()
    with pytest.raises(PackInstallError):
        delivery.apply()
    assert (root / ".group.json").read_bytes() == before
    assert read_pin(root / "default_one/PIN")["origin"] == "user"
