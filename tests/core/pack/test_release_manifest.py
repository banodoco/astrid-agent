"""Release pack lists use the shared atomic group route and preserve provenance."""

from __future__ import annotations

from pathlib import Path

from astrid.core.pack.identity import pack_digest, read_pin
from astrid.core.pack.release_manifest import install_release_manifest
from astrid.core.pack.transaction import read_group, rollback_group

from tests.core.pack.test_transaction import Repository, accepted_refresh


def release(repo: Repository, *pack_ids: str) -> dict:
    return {
        "version": 1,
        "source": str(repo.bare),
        "revision": repo.commit,
        "packs": {
            pack_id: {
                "path": f"packs/{pack_id}",
                "sha256": pack_digest(repo.work / "packs" / pack_id),
            }
            for pack_id in pack_ids
        },
    }


def test_release_manifest_uses_same_route_for_default_and_user_provenance(
        tmp_path: Path) -> None:
    repo = Repository(tmp_path / "repository")
    default_root = tmp_path / "default" / "packs"
    user_root = tmp_path / "user" / "packs"

    install_release_manifest(manifest=release(repo, "alpha", "shared"),
                             packs_root=default_root, origin="default")
    install_release_manifest(manifest=release(repo, "alpha", "shared"),
                             packs_root=user_root, origin="user")

    for root, expected_origin in ((default_root, "default"), (user_root, "user")):
        snapshot = read_group(root)
        assert set(snapshot.paths) == {"alpha", "shared"}
        assert {read_pin(path / "PIN")["origin"] for path in snapshot.paths.values()} == {expected_origin}


def test_release_manifest_rollback_restores_previous_pack_list_and_provenance(
        tmp_path: Path) -> None:
    repo = Repository(tmp_path / "repository")
    root = tmp_path / "Astrid" / "packs"
    first = release(repo, "alpha", "shared")
    install_release_manifest(manifest=first, packs_root=root, origin="default",
                             refresh=accepted_refresh)
    repo.next_revision()
    second = release(repo, "shared")
    installed = install_release_manifest(manifest=second, packs_root=root, origin="default",
                                         refresh=accepted_refresh)
    assert set(read_group(root).paths) == {"shared"}

    rolled_back = rollback_group(packs_root=root, transaction_id=installed.transaction_id,
                                 refresh=accepted_refresh)
    snapshot = read_group(root)
    assert set(snapshot.paths) == {"alpha", "shared"}
    assert snapshot.revision == installed.revision + 1 == rolled_back.revision
    assert {read_pin(path / "PIN")["origin"] for path in snapshot.paths.values()} == {"default"}
    assert snapshot.refresh_pending is False
