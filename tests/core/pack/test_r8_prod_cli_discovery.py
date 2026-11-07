"""End-to-end CLI mutations observed through normal pack discovery."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from astrid.core.pack.discovery import discover_pack_metadata
from astrid.core.pack.loader import load_pack_manifest
from astrid.core.pack.transaction import read_group


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def _manifest(pack_id: str, *, version: str = "1.0.0", dependencies: tuple[str, ...] = ()) -> str:
    dependency_section = ""
    if dependencies:
        dependency_section = "dependencies:\n  packs: [" + ", ".join(dependencies) + "]\n"
    return (
        f"schema_version: 3\nid: {pack_id}\nname: {pack_id.title()}\nversion: {version}\n"
        'pack_api: "2.0"\n'
        + dependency_section
        + "documentation: {kind: none, reason: R8 CLI discovery fixture.}\n"
    )


class _Source:
    """Small local Git source with two packs and a retained dependency edge."""

    def __init__(self, root: Path):
        self.work = root / "work"
        self.bare = root / "source.git"
        self.work.mkdir(parents=True)
        _git(self.work, "init", "-b", "main")
        _git(self.work, "config", "user.name", "R8 CLI Test")
        _git(self.work, "config", "user.email", "r8-cli@example.invalid")
        self._write("dependency", version="1.0.0")
        self._write("consumer", version="1.0.0", dependencies=("dependency",))
        self.first = self._publish("initial")
        _git(root, "clone", "--bare", str(self.work), str(self.bare))

    def _write(self, pack_id: str, *, version: str, dependencies: tuple[str, ...] = ()) -> None:
        pack = self.work / "packs" / pack_id
        pack.mkdir(parents=True, exist_ok=True)
        (pack / "pack.yaml").write_text(
            _manifest(pack_id, version=version, dependencies=dependencies), encoding="utf-8"
        )
        (pack / "payload.txt").write_text(f"{pack_id} payload {version}\n", encoding="utf-8")

    def _publish(self, message: str) -> str:
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-m", message)
        commit = _git(self.work, "rev-parse", "HEAD")
        if self.bare.exists():
            _git(self.work, "push", str(self.bare), "HEAD:refs/heads/main")
        return commit

    def update(self) -> str:
        self._write("dependency", version="2.0.0")
        self._write("consumer", version="2.0.0", dependencies=("dependency",))
        return self._publish("update both packs")


@pytest.fixture
def source(tmp_path: Path) -> _Source:
    return _Source(tmp_path / "source")


@pytest.fixture
def default_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    return home / "Astrid" / "packs"


@pytest.fixture(autouse=True)
def accept_cli_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")


def _real_inventory() -> dict[str, object]:
    discovered = discover_pack_metadata()
    result: dict[str, object] = {}
    for item in discovered:
        # Exercise the production manifest loader on each discovered path.
        loaded = load_pack_manifest(item.pack_dir / "pack.yaml", expected_pack_id=item.id)
        result[item.id] = loaded
    return result


def _update_args(source: _Source, *pack_ids: str) -> list[str]:
    return [
        "--source",
        str(source.bare),
        "--revision",
        source.first,
        *[value for pack_id in pack_ids for value in ("--pack", f"{pack_id}=packs/{pack_id}")],
    ]


def _add_args(source: _Source, pack_id: str) -> list[str]:
    # The real `add` takes one pack, a positional source and --subpath/--revision.
    return [
        "--pack-id",
        pack_id,
        "--subpath",
        f"packs/{pack_id}",
        "--revision",
        source.first,
        str(source.bare),
    ]


def _assert_listed(cli_main, *pack_ids: str) -> str:
    import io
    from contextlib import redirect_stdout

    output = io.StringIO()
    with redirect_stdout(output):
        assert cli_main(["list"]) == 0
    rendered = output.getvalue()
    for pack_id in pack_ids:
        assert pack_id in rendered
    return rendered


def test_default_collection_add_then_list_discovers_new_pack(
    source: _Source, default_collection: Path
) -> None:
    from astrid.core.pack.cli import main as packs_cli_main

    assert default_collection == Path.home() / "Astrid" / "packs"
    assert packs_cli_main(["add", *_add_args(source, "dependency")]) == 0

    inventory = _real_inventory()
    assert "dependency" in inventory
    assert inventory["dependency"].version == "1.0.0"
    assert Path(inventory["dependency"].root).resolve() == (default_collection / "dependency").resolve()
    _assert_listed(packs_cli_main, "dependency")


def test_remove_refuses_retained_dependent_without_changing_live_generation(
    source: _Source, default_collection: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from astrid.core.pack.cli import main as packs_cli_main

    assert packs_cli_main(["update", *_update_args(source, "dependency", "consumer")]) == 0
    before = read_group(default_collection)
    before_revision = before.revision
    before_transaction = before.transaction_id
    before_payloads = {
        pack_id: (pack_dir / "payload.txt").read_bytes()
        for pack_id, pack_dir in before.paths.items()
    }
    capsys.readouterr()

    result = packs_cli_main(["remove", "dependency"])

    reported = capsys.readouterr().err.lower()
    assert result != 0 or reported
    assert "consumer" in reported or "depend" in reported, (
        "the CLI must explain that the retained consumer prevents removal"
    )
    after = read_group(default_collection)
    assert (after.revision, after.transaction_id) == (before_revision, before_transaction)
    assert {
        pack_id: (pack_dir / "payload.txt").read_bytes()
        for pack_id, pack_dir in after.paths.items()
    } == before_payloads
    inventory = _real_inventory()
    assert {"dependency", "consumer"} <= inventory.keys()
    assert "dependency" in inventory["consumer"].dependencies["packs"]


def test_update_cli_replaces_default_collection_and_runtime_discovery(
    source: _Source, default_collection: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astrid.core.pack.cli import main as packs_cli_main

    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert packs_cli_main(["update", *_update_args(source, "dependency", "consumer")]) == 0
    target = source.update()
    args = [
        "update",
        "--source",
        str(source.bare),
        "--revision",
        target,
        "--pack",
        "dependency=packs/dependency",
        "--pack",
        "consumer=packs/consumer",
    ]
    assert packs_cli_main(args) == 0

    inventory = _real_inventory()
    assert {pack_id: inventory[pack_id].version for pack_id in ("dependency", "consumer")} == {
        "dependency": "2.0.0",
        "consumer": "2.0.0",
    }
    assert all(
        (default_collection / pack_id / "payload.txt").read_text(encoding="utf-8")
        == f"{pack_id} payload 2.0.0\n"
        for pack_id in ("dependency", "consumer")
    )


def test_rollback_cli_restores_prior_default_collection_generation(
    source: _Source, default_collection: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astrid.core.pack.cli import main as packs_cli_main

    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert packs_cli_main(["update", *_update_args(source, "dependency", "consumer")]) == 0
    target = source.update()
    packs = ("dependency", "consumer")
    update_args = [
        "update",
        "--source",
        str(source.bare),
        "--revision",
        target,
        "--pack",
        "dependency=packs/dependency",
        "--pack",
        "consumer=packs/consumer",
    ]
    assert packs_cli_main(update_args) == 0
    active = read_group(default_collection)

    assert packs_cli_main(["rollback", "--pack", "dependency", "--pack", "consumer"]) == 0

    rolled_back = read_group(default_collection)
    assert rolled_back.revision > active.revision
    inventory = _real_inventory()
    assert {pack_id: inventory[pack_id].version for pack_id in packs} == {
        "dependency": "1.0.0",
        "consumer": "1.0.0",
    }
    assert all(
        (default_collection / pack_id / "payload.txt").read_text(encoding="utf-8")
        == f"{pack_id} payload 1.0.0\n"
        for pack_id in packs
    )
