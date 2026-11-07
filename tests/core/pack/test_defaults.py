"""Release-manifest default installs compose through the real group transaction."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from astrid.core.pack.defaults import install_defaults, load_release_manifest
from astrid.core.pack.identity import pack_digest, read_pin
from astrid.core.pack.install import PackInstallError
from astrid.core.pack.transaction import read_group


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def pack_manifest(pack_id: str) -> str:
    return (f"schema_version: 3\nid: {pack_id}\nname: {pack_id}\nversion: 1.0.0\n"
            'pack_api: "2.0"\ndocumentation: {kind: none, reason: Fixture.}\n')


class Repository:
    def __init__(self, root: Path):
        self.work = root / "work"
        self.bare = root / "source.git"
        self.work.mkdir(parents=True)
        git(self.work, "init", "-b", "main")
        git(self.work, "config", "user.name", "Defaults Test")
        git(self.work, "config", "user.email", "defaults@example.invalid")
        self.write_pack("default_one", "default v1\n")
        self.write_pack("default_two", "default two\n")
        self.write_pack("user_owned", "user data\n")
        self.commit = self.publish()
        git(root, "clone", "--bare", str(self.work), str(self.bare))

    def write_pack(self, pack_id: str, payload: str) -> None:
        root = self.work / pack_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "pack.yaml").write_text(pack_manifest(pack_id), encoding="utf-8")
        (root / "payload.txt").write_text(payload, encoding="utf-8")

    def publish(self) -> str:
        git(self.work, "add", "-A")
        git(self.work, "commit", "-m", "fixture")
        self.commit = git(self.work, "rev-parse", "HEAD")
        if self.bare.exists():
            git(self.work, "push", str(self.bare), "HEAD:refs/heads/main")
        return self.commit

    def defaults_manifest(self, ids=("default_one", "default_two")) -> dict:
        packs = {}
        for pack_id in ids:
            packs[pack_id] = {
                "repo": str(self.bare),
                "commit": self.commit,
                "digest_rule": "pack-tree-v1",
                "digest": pack_digest(self.work / pack_id),
            }
        return {"agent": {"sha": "fixture"}, "packs": packs}


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    return Repository(tmp_path / "repository")


@pytest.fixture
def packs_root(tmp_path: Path) -> Path:
    return tmp_path / "Astrid" / "packs"


def accepted_refresh(**kwargs):
    return {"version": 1, "accepted": True,
            "revision": kwargs["committed_revision"], "reason": "ok"}


def test_release_defaults_install_as_one_group_and_record_default_provenance(repo, packs_root):
    result = install_defaults(repo.defaults_manifest(), packs_root=packs_root,
                              python_packages={}, refresh=accepted_refresh)

    assert result is not None
    snapshot = read_group(packs_root)
    assert set(snapshot.paths) == {"default_one", "default_two"}
    assert snapshot.transaction_id == result.transaction_id
    assert snapshot.revision == 1
    assert not snapshot.refresh_pending
    for pack_id, path in snapshot.paths.items():
        pin = read_pin(path / "PIN")
        assert pin["origin"] == "default"
        assert pin["commit"] == repo.commit
        assert pin["digest"] == repo.defaults_manifest()["packs"][pack_id]["digest"]
    durable_state = json.loads((packs_root / ".group.json").read_text(encoding="utf-8"))
    assert set(durable_state["admissions"]) == set()
    assert durable_state["removals"] == []


def test_defaults_leave_user_pack_untouched(repo, packs_root):
    from astrid.core.pack.transaction import install_group

    install_group(source=repo.bare, revision=repo.commit, packs={"user_owned": "user_owned"},
                  packs_root=packs_root, python_packages={}, origin="user")
    before = (packs_root / "user_owned" / "payload.txt").read_bytes()
    user_pin = read_pin(packs_root / "user_owned" / "PIN")

    install_defaults(repo.defaults_manifest(), packs_root=packs_root, python_packages={})

    snapshot = read_group(packs_root)
    assert set(snapshot.paths) == {"default_one", "default_two", "user_owned"}
    assert (snapshot.paths["user_owned"] / "payload.txt").read_bytes() == before
    assert read_pin(snapshot.paths["user_owned"] / "PIN") == user_pin


def test_failed_default_update_restores_previous_group_and_default_list(repo, packs_root):
    first = repo.defaults_manifest(("default_one",))
    initial = install_defaults(first, packs_root=packs_root, python_packages={})
    state_before = (packs_root / ".group.json").read_bytes()
    pin_before = read_pin(packs_root / "default_one" / "PIN")
    repo.write_pack("default_one", "default v2\n")
    repo.commit = repo.publish()
    update = repo.defaults_manifest(("default_one",))
    update["packs"]["default_one"]["digest"] = "0" * 64

    with pytest.raises(PackInstallError, match="expected digest"):
        install_defaults(update, packs_root=packs_root, python_packages={})

    assert (packs_root / ".group.json").read_bytes() == state_before
    assert read_group(packs_root).transaction_id == initial.transaction_id
    assert read_pin(packs_root / "default_one" / "PIN") == pin_before
    assert (packs_root / "default_one" / "payload.txt").read_text() == "default v1\n"


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(packs=[]),
    lambda value: value["packs"].update(default_one={"repo": "x", "commit": "bad", "digest_rule": "pack-tree-v1", "digest": "0" * 64}),
    lambda value: value["packs"]["default_one"].update(digest_rule="other"),
])
def test_invalid_release_default_declarations_fail_closed(repo, packs_root, mutate):
    manifest = repo.defaults_manifest(("default_one",))
    mutate(manifest)
    with pytest.raises(PackInstallError) as failure:
        install_defaults(manifest, packs_root=packs_root, python_packages={})
    assert failure.value.code == "invalid_release_manifest"
    assert not packs_root.exists()


def test_release_manifest_uses_shared_json_yaml_parser(tmp_path):
    path = tmp_path / "release.yaml"
    path.write_text("schema_version: 1\npacks: {}\n", encoding="utf-8")
    assert load_release_manifest(path) == {"schema_version": 1, "packs": {}}


def test_default_pin_conflict_does_not_take_over_user_pack(repo, packs_root):
    from astrid.core.pack.transaction import install_group

    install_group(source=repo.bare, revision=repo.commit, packs={"default_one": "default_one"},
                  packs_root=packs_root, python_packages={}, origin="user")
    before = (packs_root / ".group.json").read_bytes()
    with pytest.raises(PackInstallError) as failure:
        install_defaults(repo.defaults_manifest(("default_one",)), packs_root=packs_root,
                         python_packages={})
    assert failure.value.code == "default_pin_conflict"
    assert (packs_root / ".group.json").read_bytes() == before


def test_user_removal_tombstone_survives_default_reconciliation(repo, packs_root):
    from astrid.core.pack.transaction import install_group

    install_defaults(repo.defaults_manifest(("default_one",)), packs_root=packs_root,
                     python_packages={})
    install_group(source=repo.bare, packs={}, packs_root=packs_root,
                  python_packages={}, remove=("default_one",))

    install_defaults(repo.defaults_manifest(("default_one",)), packs_root=packs_root,
                     python_packages={})

    snapshot = read_group(packs_root)
    assert "default_one" not in snapshot.paths
    assert snapshot.removals == ("default_one",)
