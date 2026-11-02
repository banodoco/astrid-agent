"""C6 release reconciliation is one atomic default-owned transition."""
from __future__ import annotations

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from astrid.core.pack.identity import pack_digest, read_pin, write_pin
from astrid.core.pack.install import PackInstallError
from astrid.core.pack.release_manifest import install_release_manifest, load_release_manifest
from astrid.core.pack.transaction import install_group, read_group, rollback_group

from tests.core.pack.test_transaction import Repository, accepted_refresh


def release(repo: Repository, *pack_ids: str) -> dict:
    # C6 maps each default id to its same-named pack directory in the source.
    # Mirror the general transaction fixture's packs/<id> layout at repo root.
    for pack_id in pack_ids:
        shutil.copytree(repo.work / "packs" / pack_id, repo.work / pack_id, dirs_exist_ok=True)
    if pack_ids:
        repo.publish()
    return {
        "release": {"channel": "fixture"},
        "packs": {
            pack_id: {
                "repo": str(repo.bare),
                "commit": repo.commit,
                "digest_rule": "pack-tree-v1",
                "digest": pack_digest(repo.work / pack_id),
            }
            for pack_id in pack_ids
        },
    }


def test_release_manifest_uses_shared_parser_and_frozen_c6_pin_shape(
        tmp_path: Path) -> None:
    repo = Repository(tmp_path / "repository")
    data = release(repo, "alpha", "shared")
    pin = data["packs"]["alpha"]
    path = tmp_path / "release.yaml"
    path.write_text(
        "release: {channel: fixture}\npacks:\n"
        f"  alpha: {{repo: '{pin['repo']}', commit: '{pin['commit']}', digest_rule: pack-tree-v1, digest: '{pin['digest']}'}}\n"
        f"  shared: {{repo: '{data['packs']['shared']['repo']}', commit: '{data['packs']['shared']['commit']}', digest_rule: pack-tree-v1, digest: '{data['packs']['shared']['digest']}'}}\n",
        encoding="utf-8",
    )
    assert load_release_manifest(path)["release"] == {"channel": "fixture"}
    root = tmp_path / "Astrid" / "packs"
    install_release_manifest(manifest=path, packs_root=root)
    assert set(read_group(root).paths) == {"alpha", "shared"}
    assert read_pin(read_group(root).paths["alpha"] / "PIN")["origin"] == "default"


def test_release_list_change_removes_only_owned_defaults_and_is_not_user_removal(
        tmp_path: Path) -> None:
    repo = Repository(tmp_path / "repository")
    root = tmp_path / "Astrid" / "packs"
    install_release_manifest(manifest=release(repo, "alpha", "shared"), packs_root=root)
    repo.write_pack("user_pack", "user-v1\n")
    repo.publish()
    shutil.copytree(repo.work / "packs" / "user_pack", repo.work / "user_pack", dirs_exist_ok=True)
    repo.publish()
    install_group(source=repo.bare, revision=repo.commit, packs={"user_pack": "user_pack"},
                  packs_root=root, python_packages={}, origin="user")
    # Folder and personal installations remain byte-for-byte intact.
    folder = root / "folder_pack"
    folder.mkdir()
    (folder / "pack.yaml").write_text(
        "schema_version: 3\nid: folder_pack\nname: Folder\nversion: 1.0.0\npack_api: '1.0'\nactions: {}\n",
        encoding="utf-8",
    )
    write_pin(folder / "PIN", {"version": 1, "source": folder.as_uri(), "commit": None,
                                "digest_rule": "pack-tree-v1", "digest": None, "origin": "folder"})
    (folder / "marker").write_bytes(b"folder")
    personal = root / "personal"
    personal.mkdir()
    (personal / "pack.yaml").write_text(
        "schema_version: 3\nid: personal\nname: Personal\nversion: 1.0.0\npack_api: '1.0'\nactions: {}\n",
        encoding="utf-8",
    )
    write_pin(personal / "PIN", {"version": 1, "source": personal.as_uri(), "commit": None,
                                  "digest_rule": "pack-tree-v1", "digest": None, "origin": "personal"})
    (personal / "marker").write_bytes(b"personal")
    repo.next_revision()
    install_release_manifest(manifest=release(repo, "shared"), packs_root=root)
    snapshot = read_group(root)
    assert "alpha" not in snapshot.paths
    assert snapshot.removals == ()
    assert set(snapshot.paths) == {"shared", "user_pack", "folder_pack", "personal"}
    assert (snapshot.paths["folder_pack"] / "marker").read_bytes() == b"folder"
    assert (snapshot.paths["personal"] / "marker").read_bytes() == b"personal"
    assert read_pin(snapshot.paths["user_pack"] / "PIN")["origin"] == "user"


def test_empty_release_list_removes_defaults_but_keeps_user_owned_packs(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "repository")
    root = tmp_path / "Astrid" / "packs"
    install_release_manifest(manifest=release(repo, "alpha", "shared"), packs_root=root)
    repo.write_pack("user_owned", "user-v1\n")
    repo.publish()
    install_group(source=repo.bare, revision=repo.commit, packs={"user_owned": "packs/user_owned"},
                  packs_root=root, python_packages={}, origin="user")

    install_release_manifest(manifest=release(repo), packs_root=root)

    snapshot = read_group(root)
    assert set(snapshot.paths) == {"user_owned"}
    assert snapshot.removals == ()
    assert read_pin(snapshot.paths["user_owned"] / "PIN")["origin"] == "user"


def test_user_manifest_does_not_bulk_remove_other_user_packs(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "repository")
    root = tmp_path / "Astrid" / "packs"
    install_group(source=repo.bare, revision=repo.commit, packs={"shared": "packs/shared"},
                  packs_root=root, python_packages={}, origin="user")

    install_release_manifest(manifest=release(repo, "alpha", "shared"), packs_root=root, origin="user")

    snapshot = read_group(root)
    assert set(snapshot.paths) == {"alpha", "shared"}
    assert {read_pin(path / "PIN")["origin"] for path in snapshot.paths.values()} == {"user"}


def test_actual_release_rollback_restores_previous_list_but_keeps_current_removals(
        tmp_path: Path) -> None:
    repo = Repository(tmp_path / "repository")
    root = tmp_path / "Astrid" / "packs"
    install_release_manifest(manifest=release(repo, "alpha", "shared"), packs_root=root,
                             refresh=accepted_refresh)
    install_group(source=repo.bare, revision=repo.commit, packs={}, packs_root=root,
                  python_packages={}, origin="default", remove=("alpha",))
    repo.next_revision()
    updated = install_release_manifest(manifest=release(repo, "shared"), packs_root=root,
                                        refresh=accepted_refresh)
    rolled_back = rollback_group(packs_root=root, transaction_id=updated.transaction_id,
                                 refresh=accepted_refresh)
    snapshot = read_group(root)
    assert "alpha" not in snapshot.paths
    assert snapshot.removals == ("alpha",)
    assert set(snapshot.paths) == {"shared"}
    assert (snapshot.paths["shared"] / "payload.txt").read_text() == "shared-v1\n"
    assert snapshot.revision == rolled_back.revision
    assert snapshot.refresh_pending is False


def test_rollback_after_release_list_explicit_readd_uses_current_removal_snapshot(
        tmp_path: Path) -> None:
    repo = Repository(tmp_path / "repository")
    root = tmp_path / "Astrid" / "packs"
    initial_release = release(repo, "alpha", "shared")
    install_release_manifest(manifest=initial_release, packs_root=root)

    # A removal writes the user's current preference. An explicit re-add via
    # the shared release parser/transaction clears it before rollback.
    install_group(source=repo.bare, revision=repo.commit, packs={}, packs_root=root,
                  python_packages={}, remove=("alpha",))
    assert read_group(root).removals == ("alpha",)
    explicit_readd = {"release": initial_release["release"], "packs": {"alpha": initial_release["packs"]["alpha"]}}
    readded = install_release_manifest(manifest=explicit_readd, packs_root=root, origin="user")
    assert "alpha" in read_group(root).paths
    assert read_group(root).removals == ()

    rollback_group(packs_root=root, pack_ids=("alpha",), transaction_id=readded.transaction_id)
    rolled_back = read_group(root)
    assert "alpha" not in rolled_back.paths
    assert rolled_back.removals == ()

    # A later default-list reconciliation can install alpha again. The stale
    # tombstone from the re-add's retired generation must not suppress it.
    install_release_manifest(manifest=initial_release, packs_root=root)
    reconciled = read_group(root)
    assert "alpha" in reconciled.paths
    assert reconciled.removals == ()
    assert read_pin(reconciled.paths["alpha"] / "PIN")["origin"] == "default"


def test_failed_release_publication_keeps_live_generation(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "repository")
    root = tmp_path / "Astrid" / "packs"
    initial = install_release_manifest(manifest=release(repo, "alpha", "shared"), packs_root=root)
    before = {key: (path / "payload.txt").read_bytes() for key, path in read_group(root).paths.items()}
    repo.next_revision()

    def fail_before_swap(phase: str) -> None:
        if phase == "before_swap":
            raise RuntimeError("injected publication failure")

    with pytest.raises(RuntimeError, match="injected"):
        install_release_manifest(manifest=release(repo, "shared"), packs_root=root,
                                 boundary=fail_before_swap)
    after = read_group(root)
    assert after.transaction_id == initial.transaction_id
    assert {key: (path / "payload.txt").read_bytes() for key, path in after.paths.items()} == before


def test_pin_mismatch_refuses_publication(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "repository")
    root = tmp_path / "Astrid" / "packs"
    bad = release(repo, "alpha")
    bad["packs"]["alpha"]["digest"] = "0" * 64
    with pytest.raises(PackInstallError) as failure:
        install_release_manifest(manifest=bad, packs_root=root)
    assert failure.value.code == "pin_mismatch"
    assert read_group(root).revision == 0


def test_interleaved_writer_cannot_be_deleted_by_stale_release_list(
        tmp_path: Path) -> None:
    repo = Repository(tmp_path / "repository")
    root = tmp_path / "Astrid" / "packs"
    install_release_manifest(manifest=release(repo, "alpha", "shared"), packs_root=root)
    repo.next_revision()
    at_before_swap = threading.Event()
    writer_started = threading.Event()

    def boundary(phase: str) -> None:
        if phase == "before_swap":
            at_before_swap.set()
            assert writer_started.wait(timeout=5)

    def replace_stale_default() -> None:
        assert at_before_swap.wait(timeout=5)
        writer_started.set()
        install_group(source=repo.bare, revision=repo.commit, packs={"alpha": "packs/alpha"},
                      packs_root=root, python_packages={}, origin="user")

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(replace_stale_default)
        release_run = pool.submit(
            install_release_manifest, manifest=release(repo, "shared"), packs_root=root,
            boundary=boundary,
        )
        release_run.result(timeout=15)
        writer.result(timeout=15)

    snapshot = read_group(root)
    assert read_pin(snapshot.paths["alpha"] / "PIN")["origin"] == "user"
    assert set(snapshot.paths) == {"alpha", "shared"}
