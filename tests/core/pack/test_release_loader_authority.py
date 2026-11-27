"""Real loader admission must not let a mutable PIN choose release authority."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.pack import release_manifest
from astrid.core.pack.identity import read_pin, write_pin
from astrid.core.pack.loader import scan_configured_packs
from astrid.core.pack.release_manifest import install_release_manifest
from astrid.core.pack.transaction import install_group, read_group
from tests.core.pack.test_defaults import Repository


@pytest.fixture
def authority(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "repo")
    home = Path.home()  # The unchanged autouse conftest owns this directory.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    manifest = repo.defaults_manifest(("default_one",))
    # Only version selection is a unit-test seam. Installation, admission,
    # PIN IO, source digests, and scan_configured_packs are the real functions.
    # The separate noneditable installed proof uses no selection substitute.
    monkeypatch.setattr(release_manifest, "installed_release_manifest",
                        lambda: (tmp_path / "selected-version", manifest))
    return repo, home / "Astrid/packs", manifest


def inventory(pack_id="default_one"):
    scan = scan_configured_packs()
    return (pack_id in {pack.id for pack in scan.packs},
            tuple(pack.error for pack in scan.quarantined if pack.pack_id == pack_id))


@pytest.mark.parametrize("changed_source", [False, True], ids=["matching-source", "changed-source"])
@pytest.mark.parametrize("damage", ["missing", "user", "folder"])
def test_loader_cannot_opt_admitted_default_out_via_pin(authority, changed_source, damage):
    _repo, root, manifest = authority
    install_release_manifest(manifest=manifest, packs_root=root)
    assert inventory() == (True, ())
    pack = root / "default_one"
    state = (root / ".group.json").read_bytes()
    if changed_source:
        (pack / "payload.txt").write_bytes(b"user-modified source\n")
    if damage == "missing":
        (pack / "PIN").unlink()
    else:
        pin = read_pin(pack / "PIN")
        pin["origin"] = damage
        if damage == "folder":
            pin.update(source=pack.as_uri(), commit=None, digest=None)
        write_pin(pack / "PIN", pin)
    user_bytes = {p.relative_to(pack): p.read_bytes() for p in pack.rglob("*") if p.is_file()}
    admitted, refused = inventory()
    assert not admitted
    assert refused
    assert (root / ".group.json").read_bytes() == state
    assert user_bytes == {p.relative_to(pack): p.read_bytes() for p in pack.rglob("*") if p.is_file()}


@pytest.mark.parametrize("origin", ["user", "folder"])
@pytest.mark.parametrize("changed_source", [False, True], ids=["matching-source", "edited-source"])
def test_same_id_independently_admitted_user_and_folder_stay_executable(authority, origin, changed_source):
    repo, root, manifest = authority
    if origin == "user":
        install_release_manifest(manifest=manifest, packs_root=root, origin="user")
    else:
        install_group(source=repo.work, packs={"default_one": "default_one"}, packs_root=root,
                      python_packages={}, origin="folder")
    pack = root / "default_one"
    if changed_source:
        (pack / "payload.txt").write_bytes(b"intentional user edit\n")
    state = (root / ".group.json").read_bytes()
    before = {p.relative_to(pack): p.read_bytes() for p in pack.rglob("*") if p.is_file()}
    assert inventory() == (True, ())
    assert read_pin(pack / "PIN")["origin"] == origin
    assert (root / ".group.json").read_bytes() == state
    assert before == {p.relative_to(pack): p.read_bytes() for p in pack.rglob("*") if p.is_file()}


def test_explicit_user_replacement_changes_canonical_provenance(authority):
    _repo, root, manifest = authority
    first = install_release_manifest(manifest=manifest, packs_root=root)
    user = install_release_manifest(manifest=manifest, packs_root=root, origin="user")
    assert user.transaction_id != first.transaction_id
    assert read_pin(root / "default_one/PIN")["origin"] == "user"
    assert inventory() == (True, ())


@pytest.mark.parametrize("origin", ["user", "folder"])
def test_legacy_user_admission_is_not_relabelled_as_selected_default(authority, origin):
    repo, root, manifest = authority
    if origin == "user":
        install_release_manifest(manifest=manifest, packs_root=root, origin="user")
    else:
        install_group(source=repo.work, packs={"default_one": "default_one"}, packs_root=root,
                      python_packages={}, origin="folder")
    # The original v1 canonical format had no collection PIN bindings. Keep
    # its actual admissions/definitions/source/PIN and all user edits intact.
    state = json.loads((root / ".group.json").read_bytes())
    state.pop("pins")
    (root / ".group.json").write_text(json.dumps(state))
    (root / "default_one/payload.txt").write_bytes(b"existing user edits\n")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert inventory() == (True, ())
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_explicit_removal_and_unadmitted_folder_do_not_become_defaults(authority):
    repo, root, manifest = authority
    install_release_manifest(manifest=manifest, packs_root=root)
    install_group(source="", packs={}, packs_root=root, python_packages={}, remove=("default_one",))
    install_release_manifest(manifest=manifest, packs_root=root)
    assert read_group(root).removals == ("default_one",)
    assert inventory() == (False, ())
    # A user-authored folder has no canonical default admission. Its same ID
    # and the selected manifest alone must never turn it into a default.
    pack = root / "default_one"
    pack.mkdir()
    (pack / "pack.yaml").write_bytes((repo.work / "default_one/pack.yaml").read_bytes())
    (pack / "user.txt").write_bytes(b"new folder after explicit removal\x00\xff")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert inventory() == (True, ())
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert read_group(root).removals == ("default_one",)


def test_bound_default_provenance_survives_unrelated_group_publication(authority):
    repo, root, manifest = authority
    first = install_release_manifest(manifest=manifest, packs_root=root)
    install_group(source=repo.bare, revision=repo.commit, packs={"user_owned": "user_owned"},
                  packs_root=root, python_packages={}, origin="user")
    snapshot = read_group(root)
    assert snapshot.admissions["default_one"]["transactionId"] == first.transaction_id
    assert snapshot.transaction_id != first.transaction_id
    (root / "default_one/PIN").unlink()
    admitted, refused = inventory()
    assert not admitted and refused
    assert inventory("user_owned") == (True, ())


def test_altering_bound_origin_bytes_without_canonical_admission_is_refused(authority):
    _repo, root, manifest = authority
    install_release_manifest(manifest=manifest, packs_root=root)
    pack = root / "default_one"
    definitions = pack / ".build/definitions.json"
    record = json.loads(definitions.read_bytes())
    assert read_group(root).pins["default_one"] == read_pin(pack / "PIN")
    # Pack-local metadata cannot replace the independent collection binding.
    record["pin"] = read_pin(pack / "PIN")
    record["pin"]["origin"] = "user"
    definitions.write_text(json.dumps(record))
    pin = read_pin(pack / "PIN")
    pin["origin"] = "user"
    write_pin(pack / "PIN", pin)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    admitted, refused = inventory()
    assert not admitted and refused
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
