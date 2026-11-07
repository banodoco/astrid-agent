"""Folder update identity and empty release-default transaction contracts."""

from pathlib import Path

import pytest

from astrid.core.pack.identity import pack_digest, read_pin
from astrid.core.pack.install import PackInstallError
from astrid.core.pack.transaction import install_group, read_group, read_retired_group


def write_folder(root: Path, payload: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pack.yaml").write_text(
        "schema_version: 3\nid: local_ext\nname: Local\nversion: 1.0.0\n"
        'pack_api: "2.0"\n'
        "documentation: {kind: none, reason: Folder transaction fixture.}\n",
        encoding="utf-8",
    )
    (root / "payload.txt").write_text(payload, encoding="utf-8")
    return root


@pytest.mark.parametrize("subpath", [".", "packs/local # folder"])
def test_folder_update_matches_installed_string_source_identity(tmp_path: Path, subpath: str) -> None:
    source = tmp_path / "source"
    folder = write_folder(source / subpath, "before\n")
    root = tmp_path / "collection"
    first = install_group(source=source, revision=None, packs={"local_ext": subpath},
                          packs_root=root, python_packages={})
    old_pin = read_pin(root / "local_ext/PIN")
    assert isinstance(old_pin["source"], str)
    assert old_pin["source"] == folder.as_uri()
    assert old_pin["origin"] == "folder"
    assert old_pin["commit"] is old_pin["digest"] is None
    old_digest = pack_digest(root / "local_ext")
    write_folder(folder, "after\n")

    updated = install_group(source=source, revision=None, packs={"local_ext": subpath},
                            packs_root=root, python_packages={}, intent="update")

    current = read_group(root)
    assert updated.revision == current.revision == first.revision + 1
    assert current.transaction_id == updated.transaction_id != first.transaction_id
    assert updated.changed_ids == ("local_ext",)
    assert (current.paths["local_ext"] / "payload.txt").read_text() == "after\n"
    assert pack_digest(current.paths["local_ext"]) != old_digest
    assert read_pin(current.paths["local_ext"] / "PIN") == old_pin
    retired = read_retired_group(packs_root=root, transaction_id=updated.transaction_id)
    assert retired.transaction_id == first.transaction_id
    assert (retired.paths["local_ext"] / "payload.txt").read_text() == "before\n"


@pytest.mark.parametrize("other_source", ["root", "subpath"])
def test_folder_update_refuses_different_folder_identity(tmp_path: Path, other_source: str) -> None:
    source = tmp_path / "source"
    write_folder(source / "original", "before\n")
    root = tmp_path / "collection"
    install_group(source=source, revision=None, packs={"local_ext": "original"},
                  packs_root=root, python_packages={})
    before = read_group(root)
    before_files = {p.name: p.read_bytes() for p in before.paths["local_ext"].iterdir()}
    update_source = tmp_path / "other" if other_source == "root" else source
    subpath = "original" if other_source == "root" else "different"
    write_folder(update_source / subpath, "after\n")

    with pytest.raises(PackInstallError, match="conflicting source PIN") as failure:
        install_group(source=update_source, revision=None, packs={"local_ext": subpath},
                      packs_root=root, python_packages={}, intent="update")

    assert failure.value.code == "pin_mismatch"
    assert read_group(root) == before
    assert {p.name: p.read_bytes() for p in before.paths["local_ext"].iterdir()} == before_files


@pytest.mark.parametrize("revision", [None, "main", "a" * 40], ids=["missing-sha", "ref", "full-sha"])
@pytest.mark.parametrize("digests", [None, {}], ids=["omitted-digests", "empty-digests"])
def test_empty_default_group_requires_full_sha_but_no_pack_digests(
        tmp_path: Path, revision: str | None, digests: dict[str, str] | None) -> None:
    root = tmp_path / "collection"
    request = dict(source=tmp_path / "unused-source", revision=revision, packs={},
                   packs_root=root, python_packages={}, origin="default", intent="defaults",
                   expected_digests=digests)
    if revision is None or revision == "main":
        with pytest.raises(PackInstallError, match="full commit SHA") as failure:
            install_group(**request)
        assert failure.value.code == "invalid_pin"
        assert read_group(root).revision == 0
        assert not root.exists()
    else:
        result = install_group(**request)
        snapshot = read_group(root)
        assert result.revision == snapshot.revision == 1
        assert result.transaction_id == snapshot.transaction_id
        assert result.changed_ids == ()
        assert snapshot.paths == snapshot.admissions == {}
        assert snapshot.removals == ()
