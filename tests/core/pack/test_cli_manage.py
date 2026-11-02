"""Pack management commands use the M1-16 transaction boundary."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.pack.cli import main
from astrid.core.pack.identity import pack_digest, write_pin
from astrid.core.pack.personal import ensure_personal_pack
from astrid.core.pack.transaction import read_group


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def make_pack(root: Path, pack_id: str, *, dependencies: tuple[str, ...] = ()) -> None:
    pack = root / "packs" / pack_id
    pack.mkdir(parents=True)
    (pack / "pack.yaml").write_text(
        "schema_version: 3\n"
        f"id: {pack_id}\n"
        f"name: {pack_id.title()}\n"
        "version: 1.0.0\n"
        'pack_api: "1.0"\n'
        + ("dependencies:\n  packs: [" + ", ".join(dependencies) + "]\n" if dependencies else "")
        + "documentation: {kind: none, reason: CLI transaction fixture.}\n"
        "secrets:\n"
        "  - {name: API_TOKEN, reason: Authenticate to the service.}\n"
        "actions:\n"
        "  call_api:\n"
        "    name: Call API\n"
        "    version: \"1.0\"\n"
        "    description: Call the fixture API.\n"
        "    invocation: {kind: python, path: actions/call_api.py, function: run}\n"
        "    inputs: []\n"
        "    outputs: {type: object}\n"
        "    secrets: [ACTION_TOKEN]\n"
        "permissions:\n"
        "  - {id: network, reason: Call the declared service, services: [api.example.test]}\n",
        encoding="utf-8",
    )
    (pack / "payload.txt").write_text("fixture\n", encoding="utf-8")
    action = pack / "actions" / "call_api.py"
    action.parent.mkdir()
    action.write_text("def run():\n    return {}\n", encoding="utf-8")


def make_source(root: Path, *, dependencies: tuple[str, ...] = ()) -> tuple[Path, str]:
    source = root / "source"
    source.mkdir(parents=True)
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Pack CLI Test")
    git(source, "config", "user.email", "pack-cli@example.invalid")
    make_pack(source, "alpha", dependencies=dependencies)
    git(source, "add", "-A")
    git(source, "commit", "-m", "fixture")
    return source, git(source, "rev-parse", "HEAD")


def test_add_accepts_selected_core_and_external_dependencies(tmp_path: Path, monkeypatch) -> None:
    from astrid.core.pack import loader

    core = tmp_path / "core"
    external = tmp_path / "external"
    make_pack(core, "coredep")
    make_pack(external, "extdep")
    (core / "packs" / "coredep" / "api.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(loader, "packs_root", lambda: core / "packs")
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(external / "packs"))
    source, commit = make_source(tmp_path, dependencies=("coredep", "extdep"))
    collection = tmp_path / "collection"

    assert main(["add", str(source), "--pack-id", "alpha", "--subpath", "packs/alpha",
                 "--revision", commit, "--packs-root", str(collection), "--yes"]) == 0
    assert set(read_group(collection).paths) == {"alpha"}


def test_add_missing_dependency_refusal_keeps_live_generation(tmp_path: Path, capsys) -> None:
    source_ok, commit_ok = make_source(tmp_path / "initial")
    collection = tmp_path / "collection"
    assert main(["add", str(source_ok), "--pack-id", "alpha", "--subpath", "packs/alpha",
                 "--revision", commit_ok, "--packs-root", str(collection), "--yes"]) == 0
    before = read_group(collection)
    before_payload = (before.paths["alpha"] / "payload.txt").read_bytes()

    source_missing, commit_missing = make_source(tmp_path / "missing", dependencies=("absent",))
    assert main(["add", str(source_missing), "--pack-id", "alpha", "--subpath", "packs/alpha",
                 "--revision", commit_missing, "--packs-root", str(collection), "--yes"]) == 1
    after = read_group(collection)
    assert (after.revision, after.transaction_id) == (before.revision, before.transaction_id)
    assert (after.paths["alpha"] / "payload.txt").read_bytes() == before_payload
    assert "declared pack dependency is unavailable" in capsys.readouterr().err


def test_add_default_pin_conflict_refusal_keeps_live_generation(tmp_path: Path, monkeypatch, capsys) -> None:
    provider = tmp_path / "provider"
    provider.mkdir()
    make_pack(provider, "provider")
    collection = tmp_path / "collection"
    assert main(["add", str(provider), "--pack-id", "provider", "--subpath", "packs/provider",
                 "--packs-root", str(collection), "--yes"]) == 0
    before = read_group(collection)
    pin_file = tmp_path / "release-pins.json"
    pin_file.write_text(json.dumps({"provider": {"commit": "a" * 40, "digest": "b" * 64}}), encoding="utf-8")
    monkeypatch.setenv("ASTRID_RELEASE_PACK_PINS", str(pin_file))
    source, commit = make_source(tmp_path / "consumer", dependencies=("provider",))

    assert main(["add", str(source), "--pack-id", "alpha", "--subpath", "packs/alpha",
                 "--revision", commit, "--packs-root", str(collection), "--yes"]) == 1
    after = read_group(collection)
    assert (after.revision, after.transaction_id) == (before.revision, before.transaction_id)
    assert set(after.paths) == {"provider"}
    assert "release default pin" in capsys.readouterr().err


def test_remove_retained_dependent_refusal_keeps_live_generation(tmp_path: Path, capsys) -> None:
    provider = tmp_path / "provider"
    provider.mkdir()
    make_pack(provider, "provider")
    collection = tmp_path / "collection"
    assert main(["add", str(provider), "--pack-id", "provider", "--subpath", "packs/provider",
                 "--packs-root", str(collection), "--yes"]) == 0
    consumer, commit = make_source(tmp_path / "consumer", dependencies=("provider",))
    assert main(["add", str(consumer), "--pack-id", "alpha", "--subpath", "packs/alpha",
                 "--revision", commit, "--packs-root", str(collection), "--yes"]) == 0
    before = read_group(collection)
    payloads = {pack_id: (path / "payload.txt").read_bytes() for pack_id, path in before.paths.items()}

    assert main(["remove", "provider", "--packs-root", str(collection), "--yes"]) == 1
    after = read_group(collection)
    assert (after.revision, after.transaction_id) == (before.revision, before.transaction_id)
    assert set(after.paths) == set(before.paths)
    assert {pack_id: (path / "payload.txt").read_bytes() for pack_id, path in after.paths.items()} == payloads
    assert "declared pack dependency is unavailable" in capsys.readouterr().err


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
    assert "ACTION_TOKEN" in output
    assert "actions.call_api.secrets" in output
    assert "api.example.test" in output
    installed = read_group(collection)
    assert set(installed.paths) == {"alpha"}
    assert json.loads((installed.paths["alpha"] / "PIN").read_text())["origin"] == "user"

    assert main(["list"]) == 0
    listing = capsys.readouterr().out
    assert "origin=user" in listing
    assert "modified=false" in listing
    (installed.paths["alpha"] / "payload.txt").write_text("user edit\n", encoding="utf-8")
    assert main(["list"]) == 0
    modified_listing = capsys.readouterr().out
    assert "origin=user" in modified_listing
    assert "modified=true" in modified_listing

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


def test_changed_folder_after_consent_is_rejected(tmp_path: Path, monkeypatch, capsys) -> None:
    from astrid.core.pack import cli_manage

    source = tmp_path / "source"
    make_pack(source, "alpha")
    collection = tmp_path / "packs"
    original_install_group = cli_manage.install_group

    def mutate_after_consent(**kwargs):
        def boundary(phase: str) -> None:
            if phase == "before_swap":
                (source / "packs" / "alpha" / "payload.txt").write_text(
                    "changed after consent\n", encoding="utf-8"
                )
        kwargs["boundary"] = boundary
        return original_install_group(**kwargs)

    monkeypatch.setattr(cli_manage, "install_group", mutate_after_consent)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert main(["add", str(source), "--pack-id", "alpha", "--subpath", "packs/alpha",
                 "--packs-root", str(collection)]) == 1
    assert not (collection / "alpha").exists()
    assert "changed after consent" in capsys.readouterr().err


def test_core_id_collision_is_rejected_even_with_user_pin(tmp_path: Path, monkeypatch) -> None:
    from astrid.core.pack import loader
    from astrid.core.pack._common import PackValidationError
    from astrid.core.pack.discovery import discover_pack_metadata

    core_root = tmp_path / "core"
    user_root = tmp_path / "user"
    make_pack(core_root, "shared")
    make_pack(user_root, "shared")
    (user_root / "packs" / "shared" / "pack.yaml").write_text(
        (user_root / "packs" / "shared" / "pack.yaml").read_text().replace("name: Shared", "name: User Copy"),
        encoding="utf-8",
    )
    (user_root / "packs" / "shared" / "PIN").write_text("present\n", encoding="utf-8")
    monkeypatch.setattr(loader, "packs_root", lambda: core_root / "packs")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(user_root / "packs"))
    with pytest.raises(PackValidationError, match="conflicts with a core pack"):
        discover_pack_metadata(extra_pack_roots=(str(user_root / "packs"),))
    from astrid.core.pack.cli import main as packs_cli_main

    assert packs_cli_main(["list"]) != 0


def test_default_personal_pack_keeps_identity_with_single_and_multiple_extra_roots(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from astrid.core.pack.discovery import discover_pack_metadata

    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    default_personal = ensure_personal_pack()
    roots = [tmp_path / "extra-one" / "packs", tmp_path / "extra-two" / "packs"]
    for index, root in enumerate(roots, start=1):
        make_pack(root.parent, f"extra{index}")
    personal_root = home / "Astrid" / "packs"
    for configured in (roots[:1], roots):
        monkeypatch.setenv("ASTRID_PACKS_PATH", os.pathsep.join(map(str, configured)))
        records = discover_pack_metadata()
        personal = next(item for item in records if item.id == "personal")
        assert personal.source_kind == "personal"
        assert personal.pack.root.resolve() == (personal_root / "personal").resolve()
        assert {item.id for item in records} >= {"personal", "extra1"}
        if len(configured) == 2:
            assert "extra2" in {item.id for item in records}
        assert main(["list"]) == 0
        listing = capsys.readouterr().out
        assert "Personal" in listing
        assert "extra1" in listing
        assert default_personal.is_dir()


def test_personal_impostor_and_duplicate_are_rejected(tmp_path: Path, monkeypatch) -> None:
    import pytest

    from astrid.core.pack._common import PackValidationError
    from astrid.core.pack.discovery import discover_pack_metadata

    canonical_root = tmp_path / "canonical"
    impostor_root = tmp_path / "impostor"
    ensure_personal_pack(canonical_root)
    (impostor_root / "personal").mkdir(parents=True)
    (impostor_root / "personal" / "pack.yaml").write_text(
        (canonical_root / "personal" / "pack.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setenv("ASTRID_PACKS_PATH", "")
    with pytest.raises(PackValidationError, match="reserved for the canonical personal root"):
        discover_pack_metadata(
            personal_packs_root=canonical_root,
            extra_pack_roots=(str(canonical_root), str(impostor_root)),
        )


@pytest.mark.parametrize("duplicate_kind", ["hidden", "malformed", "manifestless"])
def test_duplicate_directory_name_refuses_discovery_and_real_list(
    tmp_path: Path, monkeypatch, capsys, duplicate_kind: str
) -> None:
    from astrid.core.pack import loader
    from astrid.core.pack._common import PackValidationError
    from astrid.core.pack.discovery import discover_pack_metadata
    from astrid.core.pack.cli import main as packs_cli_main

    core = tmp_path / "core" / "packs"
    source = tmp_path / "source" / "packs"
    make_pack(core.parent, "collision")
    make_pack(source.parent, "collision")
    source_manifest = source / "collision" / "pack.yaml"
    expected_message = "duplicate pack directory name"
    if duplicate_kind == "hidden":
        source_manifest.write_text(source_manifest.read_text().replace(
            "documentation:", "visibility: hidden\ndocumentation:"), encoding="utf-8")
        expected_message = "conflicts with a core pack"
    elif duplicate_kind == "malformed":
        source_manifest.write_text("schema_version: [malformed\n", encoding="utf-8")
    else:
        source_manifest.unlink()
    monkeypatch.setattr(loader, "packs_root", lambda: core)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(source))

    with pytest.raises(PackValidationError, match=expected_message):
        discover_pack_metadata()
    assert packs_cli_main(["list"]) != 0
    captured = capsys.readouterr()
    assert expected_message in captured.err


def test_personal_impostor_refuses_real_list(tmp_path: Path, monkeypatch, capsys) -> None:
    from astrid.core.pack.cli import main as packs_cli_main

    canonical = tmp_path / "home" / "Astrid" / "packs"
    impostor = tmp_path / "impostor"
    ensure_personal_pack(canonical)
    (impostor / "personal").mkdir(parents=True)
    (impostor / "personal" / "pack.yaml").write_bytes(
        (canonical / "personal" / "pack.yaml").read_bytes()
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(impostor))
    assert packs_cli_main(["list"]) != 0
    assert "reserved for the canonical personal root" in capsys.readouterr().err


def test_gateway_discovery_loads_installed_pack_and_refuses_duplicate(tmp_path: Path, monkeypatch) -> None:
    from astrid.core.gateway.hivemind import installed_pack_ids

    installed = tmp_path / "installed"
    make_pack(installed, "gatewayfixture")
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(installed / "packs"))
    assert "gatewayfixture" in installed_pack_ids()

    duplicate = tmp_path / "duplicate"
    make_pack(duplicate, "gatewayfixture")
    monkeypatch.setenv(
        "ASTRID_PACKS_PATH",
        os.pathsep.join((str(installed / "packs"), str(duplicate / "packs"))),
    )
    assert "gatewayfixture" not in installed_pack_ids()


def test_generic_host_constructs_and_refuses_external_pack_discovery(tmp_path: Path, monkeypatch) -> None:
    from astrid.core.execution.generic_host import GenericPackHost
    from astrid.core.pack._common import PackValidationError

    installed = tmp_path / "installed" / "packs"
    make_pack(installed.parent, "hostfixture")
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(installed))
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps({"schema_version": 1, "capabilities": []}), encoding="utf-8")
    host = GenericPackHost(
        pack_roots=[installed], client=None, capability_matrix=matrix
    )
    discovered = host.discover()
    assert any(record.id == "hostfixture.call_api" for record in discovered)

    duplicate = tmp_path / "duplicate" / "packs"
    make_pack(duplicate.parent, "hostfixture")
    monkeypatch.setenv("ASTRID_PACKS_PATH", os.pathsep.join((str(installed), str(duplicate))))
    refused_host = GenericPackHost(
        pack_roots=[installed, duplicate], client=None,
        capability_matrix=matrix,
    )
    with pytest.raises(PackValidationError, match="duplicate pack id"):
        refused_host.discover()
    assert "hostfixture.call_api" not in refused_host.capabilities
