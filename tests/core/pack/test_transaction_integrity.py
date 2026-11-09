"""PK-9: the two transaction integrity cases no retained ref proves.

Uses the ``repo`` and ``packs_root`` fixtures of ``test_transaction.py``.
"""
from __future__ import annotations

from pathlib import Path

from astrid.core.pack.identity import pack_digest, read_pin
from astrid.core.pack.transaction import install_group, read_group, read_retired_group, rollback_group
from tests.core.pack.test_transaction import (  # noqa: F401  (pytest fixtures)
    GROUP, Repository, accepted_refresh, install, packs_root, repo, tree_bytes,
)


def _pack_bytes(root: Path) -> dict[str, dict[str, bytes]]:
    snapshot = read_group(root)
    # PIN and .build/definitions.json (which records the source commit) change with a pin-only exchange.
    return {pack_id: {name: data for name, data in tree_bytes(path).items()
                      if name not in {"PIN", ".build/definitions.json"}}
            for pack_id, path in snapshot.paths.items()}


def _pins(root: Path) -> dict[str, bytes]:
    return {pack_id: (path / "PIN").read_bytes() for pack_id, path in read_group(root).paths.items()}


def read_pin_bytes_digest(raw: bytes) -> str:
    import json
    return json.loads(raw)["digest"]


def _commit_outside_packs(repo: Repository) -> str:
    (repo.work / "README.md").write_text("only the repository changed\n", encoding="utf-8")
    return repo.publish()


def test_pin_only_exchange_keeps_pack_bytes(repo: Repository, packs_root: Path) -> None:
    install(repo, packs_root, refresh=accepted_refresh)
    old_bytes, old_pins = _pack_bytes(packs_root), _pins(packs_root)
    old_tree = tree_bytes(packs_root)
    new_commit = _commit_outside_packs(repo)

    update = install_group(source=repo.bare, revision=new_commit, packs=GROUP, packs_root=packs_root,
                           python_packages={}, intent="update", refresh=accepted_refresh)
    # Only the PIN changed: every other byte is identical and the PIN names the new commit.
    assert _pack_bytes(packs_root) == old_bytes
    for pack_id, path in read_group(packs_root).paths.items():
        pin = read_pin(path / "PIN")
        assert pin["commit"] == new_commit and pin["digest"] == pack_digest(path)
        assert pin["digest"] == read_pin_bytes_digest(old_pins[pack_id])
    # The old generation is retained intact.
    retired = read_retired_group(packs_root=packs_root, transaction_id=update.transaction_id)
    assert {pack_id: (path / "PIN").read_bytes() for pack_id, path in retired.paths.items()} == old_pins
    assert tree_bytes(update.retired_root) == old_tree

    rollback_group(packs_root=packs_root, pack_ids=tuple(GROUP), refresh=accepted_refresh)
    assert _pins(packs_root) == old_pins
    assert _pack_bytes(packs_root) == old_bytes


def test_equal_byte_exchange_is_a_no_op_update(repo: Repository, packs_root: Path) -> None:
    first = install(repo, packs_root, refresh=accepted_refresh)
    before_tree = tree_bytes(packs_root)
    before = read_group(packs_root)

    result = install_group(source=repo.bare, revision=repo.commit, packs=GROUP, packs_root=packs_root,
                           python_packages={}, intent="update", refresh=accepted_refresh)
    after = read_group(packs_root)
    assert (after.transaction_id, after.revision) == (before.transaction_id, before.revision) == (
        first.transaction_id, first.revision)
    assert result.transaction_id == first.transaction_id
    assert tree_bytes(packs_root) == before_tree
