"""End-to-end release-pin snapshot coverage through the packs CLI."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from astrid.core.pack.identity import pack_digest, read_pin
from astrid.core.pack.transaction import read_group


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def _manifest(pack_id: str, dependencies: tuple[str, ...] = ()) -> str:
    dependency_field = ""
    if dependencies:
        dependency_field = "dependencies:\n  packs: [" + ", ".join(dependencies) + "]\n"
    return (f"schema_version: 3\nid: {pack_id}\nname: {pack_id.title()}\nversion: 1.0.0\n"
            'pack_api: "1.0"\n' + dependency_field +
            "documentation: {kind: none, reason: R8 release-pin fixture.}\n")


class _Source:
    def __init__(self, root: Path):
        self.work = root / "work"
        self.bare = root / "source.git"
        self.work.mkdir(parents=True)
        _git(self.work, "init", "-b", "main")
        _git(self.work, "config", "user.name", "Release Pin Test")
        _git(self.work, "config", "user.email", "release-pin@example.invalid")
        self.write("shared", "shared-v1\n")
        self.write("alpha", "alpha-v1\n", dependencies=("shared",))
        self.revision = self.publish()
        _git(root, "clone", "--bare", str(self.work), str(self.bare))

    def write(self, pack_id: str, payload: str, *, dependencies: tuple[str, ...] = ()) -> None:
        pack = self.work / "packs" / pack_id
        pack.mkdir(parents=True, exist_ok=True)
        (pack / "pack.yaml").write_text(_manifest(pack_id, dependencies), encoding="utf-8")
        (pack / "payload.txt").write_text(payload, encoding="utf-8")

    def publish(self) -> str:
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-m", "fixture")
        revision = _git(self.work, "rev-parse", "HEAD")
        if self.bare.exists():
            _git(self.work, "push", str(self.bare), "HEAD:refs/heads/main")
        return revision

    def next_revision(self) -> str:
        self.write("shared", "shared-v2\n")
        self.write("alpha", "alpha-v2\n", dependencies=("shared",))
        return self.publish()


@pytest.fixture
def source(tmp_path: Path) -> _Source:
    return _Source(tmp_path / "repository")


@pytest.fixture
def packs_root(tmp_path: Path) -> Path:
    return tmp_path / "Astrid" / "packs"


def _write_snapshot(path: Path, pins: dict[str, dict[str, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    path.write_text(json.dumps(pins, sort_keys=True), encoding="utf-8")
    monkeypatch.setenv("ASTRID_RELEASE_PACK_PINS", str(path))


def _install_args(source: _Source, root: Path, revision: str) -> list[str]:
    return ["update", "--source", str(source.bare), "--revision", revision,
            "--packs-root", str(root), "--pack", "alpha=packs/alpha", "--pack", "shared=packs/shared"]


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def _assert_loader_discovers(root: Path) -> None:
    from astrid.core.pack.loader import discover_packs

    discovered = discover_packs(root)
    assert {pack.id for pack in discovered} == {"alpha", "shared"}


@pytest.fixture(autouse=True)
def _cli_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(tmp_path / "configured-packs"))
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")


def test_cli_installs_declaration_without_tree_digest_from_empty_release_snapshot(
        source: _Source, packs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from astrid.core.pack.cli import main as packs_cli_main

    declaration = tmp_path / "source-declarations.json"
    declaration.write_text(json.dumps({"sources": [{
        "pack_id": "alpha", "repository": str(source.bare), "revision": source.revision,
    }]}), encoding="utf-8")
    assert "tree_sha256" not in json.loads(declaration.read_text(encoding="utf-8"))["sources"][0]
    monkeypatch.setenv("ASTRID_SOURCE_DECLARATIONS", str(declaration))
    snapshot = tmp_path / "release-pins.json"
    _write_snapshot(snapshot, {}, monkeypatch)

    assert packs_cli_main(_install_args(source, packs_root, source.revision)) == 0

    installed = read_group(packs_root)
    assert set(installed.paths) == {"alpha", "shared"}
    _assert_loader_discovers(packs_root)
    assert (installed.paths["alpha"] / "payload.txt").read_text(encoding="utf-8") == "alpha-v1\n"
    assert read_pin(installed.paths["alpha"] / "PIN")["digest"] == pack_digest(installed.paths["alpha"])


@pytest.mark.parametrize("wrong_field", ["commit", "digest"])
def test_cli_refuses_each_release_pin_mismatch_without_changing_live_generation(
        source: _Source, packs_root: Path, tmp_path: Path, wrong_field: str,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from astrid.core.pack.cli import main as packs_cli_main

    empty_snapshot = tmp_path / "empty-release-pins.json"
    _write_snapshot(empty_snapshot, {}, monkeypatch)
    assert packs_cli_main(_install_args(source, packs_root, source.revision)) == 0
    newer_revision = source.next_revision()
    live_before = _snapshot_tree(packs_root)
    active_before = read_group(packs_root)

    actual_commit = newer_revision
    actual_digest = pack_digest(source.work / "packs" / "alpha")
    pin = {"commit": actual_commit, "digest": actual_digest}
    if wrong_field == "commit":
        pin["commit"] = "f" * 40 if actual_commit != "f" * 40 else "e" * 40
    else:
        pin["digest"] = hashlib.sha256(b"intentionally wrong release digest").hexdigest()
    _write_snapshot(tmp_path / f"release-pins-{wrong_field}.json", {"alpha": pin}, monkeypatch)

    result = packs_cli_main(_install_args(source, packs_root, newer_revision))

    assert result == 2
    assert "Installed pack does not match its release pin." in capsys.readouterr().err
    assert _snapshot_tree(packs_root) == live_before
    active_after = read_group(packs_root)
    assert active_after.transaction_id == active_before.transaction_id
    assert active_after.revision == active_before.revision
    _assert_loader_discovers(packs_root)
    assert read_pin(active_after.paths["alpha"] / "PIN")["commit"] == source.revision


def test_cli_rolls_back_to_prior_generation_matching_release_snapshot(
        source: _Source, packs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from astrid.core.pack.cli import main as packs_cli_main

    empty_snapshot = tmp_path / "empty-release-pins.json"
    _write_snapshot(empty_snapshot, {}, monkeypatch)
    assert packs_cli_main(_install_args(source, packs_root, source.revision)) == 0
    prior = read_group(packs_root)
    prior_pin = read_pin(prior.paths["alpha"] / "PIN")
    newer_revision = source.next_revision()
    assert packs_cli_main(_install_args(source, packs_root, newer_revision)) == 0
    updated = read_group(packs_root)
    assert updated.transaction_id != prior.transaction_id

    matching_snapshot = tmp_path / "matching-release-pins.json"
    _write_snapshot(matching_snapshot, {"alpha": {
        "commit": prior_pin["commit"], "digest": prior_pin["digest"],
    }}, monkeypatch)
    assert packs_cli_main(["rollback", "--packs-root", str(packs_root),
                           "--pack", "alpha", "--pack", "shared"]) == 0

    restored = read_group(packs_root)
    assert restored.transaction_id != updated.transaction_id
    assert restored.revision > updated.revision
    assert read_pin(restored.paths["alpha"] / "PIN") == prior_pin
    assert (restored.paths["alpha"] / "payload.txt").read_text(encoding="utf-8") == "alpha-v1\n"
    _assert_loader_discovers(packs_root)
