"""Pack management commands use the M1-16 transaction boundary."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from astrid.core.pack.cli import main
from astrid.core.pack.identity import pack_digest, write_pin
from astrid.core.pack.transaction import read_group


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def make_pack(root: Path, pack_id: str) -> None:
    pack = root / "packs" / pack_id
    pack.mkdir(parents=True)
    (pack / "pack.yaml").write_text(
        "schema_version: 3\n"
        f"id: {pack_id}\n"
        f"name: {pack_id.title()}\n"
        "version: 1.0.0\n"
        'pack_api: "1.0"\n'
        "documentation: {kind: none, reason: CLI transaction fixture.}\n"
        "secrets:\n"
        "  - {name: API_TOKEN, reason: Authenticate to the service.}\n"
        "permissions:\n"
        "  - {id: network, reason: Call the declared service, services: [api.example.test]}\n",
        encoding="utf-8",
    )
    (pack / "payload.txt").write_text("fixture\n", encoding="utf-8")


def make_source(root: Path) -> tuple[Path, str]:
    source = root / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Pack CLI Test")
    git(source, "config", "user.email", "pack-cli@example.invalid")
    make_pack(source, "alpha")
    git(source, "add", "-A")
    git(source, "commit", "-m", "fixture")
    return source, git(source, "rev-parse", "HEAD")


def test_add_and_remove_use_group_transaction_and_show_consent(tmp_path: Path, monkeypatch, capsys) -> None:
    source, commit = make_source(tmp_path)
    collection = tmp_path / "Astrid" / "packs"
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["add", str(source), "--pack-id", "alpha", "--subpath", "packs/alpha", "--revision", commit,
                 "--yes"]) == 0

    output = capsys.readouterr().out
    assert f"Source: {source}" in output
    assert f"Commit: {commit}" in output
    assert "API_TOKEN" in output
    assert "api.example.test" in output
    installed = read_group(collection)
    assert set(installed.paths) == {"alpha"}
    assert json.loads((installed.paths["alpha"] / "PIN").read_text())["origin"] == "user"

    assert main(["list"]) == 0
    listing = capsys.readouterr().out
    assert "origin=user" in listing
    assert "modified=false" in listing

    assert main(["remove", "alpha", "--yes"]) == 0
    removed = read_group(collection)
    assert not removed.paths
    assert removed.removals == ("alpha",)


def test_add_refusal_does_not_publish(tmp_path: Path, monkeypatch, capsys) -> None:
    source, commit = make_source(tmp_path)
    collection = tmp_path / "packs"
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert main(["add", str(source), "--pack-id", "alpha", "--subpath", "packs/alpha", "--revision", commit,
                 "--packs-root", str(collection)]) == 1
    assert not collection.exists()
    assert "Declared secrets" in capsys.readouterr().out


def test_folder_add_uses_transaction_and_folder_origin(tmp_path: Path, capsys) -> None:
    source = tmp_path / "alpha"
    source.mkdir()
    (source / "pack.yaml").write_text(
        "schema_version: 3\nid: alpha\nname: Alpha\nversion: 1.0.0\n"
        'pack_api: "1.0"\ndocumentation: {kind: none, reason: Folder fixture.}\n',
        encoding="utf-8",
    )
    (source / "payload.txt").write_text("fixture\n", encoding="utf-8")
    collection = tmp_path / "Astrid" / "packs"
    assert main(["add", str(tmp_path), "--pack-id", "alpha", "--subpath", "alpha",
                 "--packs-root", str(collection), "--yes"]) == 0
    installed = read_group(collection)
    pin = json.loads((installed.paths["alpha"] / "PIN").read_text(encoding="utf-8"))
    assert pin["origin"] == "folder"
    assert pin["commit"] is None
    assert "local folder; unpinned" in capsys.readouterr().out


def test_provenance_reports_modified_pinned_pack(tmp_path: Path) -> None:
    root = tmp_path / "alpha"
    root.mkdir()
    (root / "pack.yaml").write_text("fixture\n", encoding="utf-8")
    commit = "a" * 40
    write_pin(root / "PIN", {"version": 1, "source": "https://example.test/packs.git",
                              "commit": commit, "digest_rule": "pack-tree-v1",
                              "digest": pack_digest(root), "origin": "user"})
    pack = SimpleNamespace(root=root, origin="builtin")
    from astrid.core.pack.cli_manage import origin_and_modified

    assert origin_and_modified(pack) == ("user", False)
    (root / "pack.yaml").write_text("changed\n", encoding="utf-8")
    assert origin_and_modified(pack) == ("user", True)
