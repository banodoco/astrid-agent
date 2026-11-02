"""Real CLI refusal checks for Python requirements and generation preservation."""

from __future__ import annotations

import json
import os
import subprocess
from importlib import metadata
from pathlib import Path

import pytest


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


class SourceRepo:
    def __init__(self, root: Path):
        self.work = root / "work"
        self.bare = root / "source.git"
        self.work.mkdir(parents=True)
        _git(self.work, "init", "-b", "main")
        _git(self.work, "config", "user.name", "R8 H3 Test")
        _git(self.work, "config", "user.email", "r8-h3@example.invalid")
        self.write_pack()
        self.commit = self.publish()
        _git(root, "clone", "--bare", str(self.work), str(self.bare))

    @property
    def pack(self) -> Path:
        return self.work / "packs" / "alpha"

    def write_pack(self, extra: str = "") -> None:
        self.pack.mkdir(parents=True, exist_ok=True)
        (self.pack / "pack.yaml").write_text(
            "schema_version: 3\nid: alpha\nname: Alpha\nversion: 1.0.0\n"
            'pack_api: "1.0"\ndocumentation: {kind: none, reason: R8 H3 fixture.}\n' + extra,
            encoding="utf-8",
        )
        (self.pack / "payload.txt").write_text("alpha payload\n", encoding="utf-8")

    def publish(self) -> str:
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-m", "fixture")
        commit = _git(self.work, "rev-parse", "HEAD")
        if self.bare.exists():
            _git(self.work, "push", str(self.bare), "HEAD:refs/heads/main")
        return commit


@pytest.fixture
def source_repo(tmp_path: Path) -> SourceRepo:
    return SourceRepo(tmp_path / "source")


@pytest.fixture
def packs_root(tmp_path: Path) -> Path:
    return tmp_path / "Astrid" / "packs"


def _tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }


def _admission_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.parent.parent.name: path.read_bytes()
        for path in root.glob("*/.build/admission.json")
    }


def _cli_update(source_repo: SourceRepo, packs_root: Path, *, revision: str | None = None) -> int:
    from astrid.core.pack.cli import main as packs_cli_main

    return packs_cli_main([
        "update", "--source", str(source_repo.bare), "--revision", revision or source_repo.commit,
        "--packs-root", str(packs_root), "--pack", "alpha=packs/alpha",
    ])


def _seed_admitted_generation(source_repo: SourceRepo, packs_root: Path) -> None:
    """Create the live baseline through the transaction and pinned real builder."""
    from astrid.core.pack.transaction import install_group
    from tests.pack_builder.test_pack_build import toolchain

    extension_manifest = {
        "id": "alpha.extension", "version": "1.0.0", "label": "Alpha",
        "apiVersion": 1, "contributions": [],
    }
    (source_repo.pack / "entry.tsx").write_text(
        "import {defineExtension} from '@astrid/editor-sdk';\n"
        "export default defineExtension({manifest: " + json.dumps(extension_manifest) +
        ", activate() { return {dispose() {}}; }});\n",
        encoding="utf-8",
    )
    source_repo.commit = source_repo.publish()
    assert all(os.environ.get(key) for key in (
        "ASTRID_BUILD_TEST_NODE", "ASTRID_BUILD_TEST_NODE_VERSION", "ASTRID_BUILD_TEST_ESBUILD",
    )), "R8-H3 requires the pinned Node/esbuild environment from its run instructions"
    install_group(
        source=source_repo.bare, revision=source_repo.commit,
        packs={"alpha": "packs/alpha"}, packs_root=packs_root,
        python_packages={
            dist.metadata.get("Name", "").lower().replace("_", "-").replace(".", "-"): dist.version
            for dist in metadata.distributions() if dist.metadata.get("Name")
        },
        builds={"alpha": {"entry": "entry.tsx", "extension_manifest": extension_manifest}},
        toolchain=toolchain(), intent="explicit",
    )
    assert _admission_bytes(packs_root).keys() == {"alpha"}


def _refusal_snapshot(packs_root: Path) -> tuple[dict[str, bytes], dict[str, bytes]]:
    tree = _tree_bytes(packs_root)
    admissions = _admission_bytes(packs_root)
    assert admissions.keys() == {"alpha"}
    return tree, admissions


def _assert_refused_unchanged(packs_root: Path, before: tuple[dict[str, bytes], dict[str, bytes]]) -> None:
    assert _tree_bytes(packs_root) == before[0]
    assert _admission_bytes(packs_root) == before[1]


def test_real_cli_add_accepts_installed_python_requirement_and_runtime_loads_pack(
        source_repo: SourceRepo, packs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from astrid.core.pack import discover_packs, load_pack_manifest

    installed = {
        dist.metadata.get("Name", "").lower().replace("_", "-").replace(".", "-")
        for dist in metadata.distributions() if dist.metadata.get("Name")
    }
    assert "pytest" in installed
    source_repo.write_pack("dependencies:\n  python_requirements: [pytest]\n")
    source_repo.commit = source_repo.publish()
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    assert _cli_update(source_repo, packs_root) == 0

    discovered = {pack.id: pack for pack in discover_packs(packs_root)}
    assert "alpha" in discovered
    loaded = load_pack_manifest(packs_root / "alpha" / "pack.yaml", expected_pack_id="alpha")
    assert loaded.id == "alpha"
    assert loaded.dependencies.get("python_requirements") == ["pytest"]


def test_real_cli_update_refuses_missing_python_and_preserves_live_generation(
        source_repo: SourceRepo, packs_root: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _seed_admitted_generation(source_repo, packs_root)
    missing = "astrid-r8-h3-uninstalled-31d0a8"
    with pytest.raises(metadata.PackageNotFoundError):
        metadata.version(missing)
    source_repo.write_pack(f"dependencies:\n  python_requirements: [{missing}]\n")
    revision = source_repo.publish()
    before = _refusal_snapshot(packs_root)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    assert _cli_update(source_repo, packs_root, revision=revision) == 2
    output = capsys.readouterr().err.lower()
    assert "python requirement" in output and "is missing" in output
    assert missing in output
    _assert_refused_unchanged(packs_root, before)


@pytest.mark.parametrize(
    ("refusal", "diagnostic"),
    [
        ("dependency", "missing_dep"),
        ("python", "missing_python"),
        ("pin", "pin_mismatch"),
        ("bad_source", "invalid_manifest"),
    ],
)
def test_real_cli_refusals_preserve_generation_and_admission_bytes(
        source_repo: SourceRepo, packs_root: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str], tmp_path: Path, refusal: str, diagnostic: str) -> None:
    _seed_admitted_generation(source_repo, packs_root)
    if refusal == "dependency":
        extra = "dependencies:\n  packs: [r8_h3_missing_dependency]\n"
        source_repo.write_pack(extra)
    elif refusal == "python":
        missing = "astrid-r8-h3-uninstalled-param-9be20c"
        with pytest.raises(metadata.PackageNotFoundError):
            metadata.version(missing)
        source_repo.write_pack(f"dependencies:\n  python_requirements: [{missing}]\n")
    elif refusal == "pin":
        source_repo.write_pack()
        (source_repo.pack / "payload.txt").write_text("updated candidate\n", encoding="utf-8")
        pin_file = tmp_path / "release-pins.json"
        pin_file.write_text(json.dumps({"alpha": {"commit": "f" * 40, "digest": "e" * 64}}),
                            encoding="utf-8")
        monkeypatch.setenv("ASTRID_RELEASE_PACK_PINS", str(pin_file))
    else:
        source_repo.write_pack("dependencies: [this is not a mapping]\n")
    revision = source_repo.publish()
    before = _refusal_snapshot(packs_root)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    assert _cli_update(source_repo, packs_root, revision=revision) == 2
    output = capsys.readouterr().err.lower()
    expected_text = {
        "missing_dep": "unavailable",
        "missing_python": "python",
        "pin_mismatch": "pin",
        "invalid_manifest": "canonical v3",
    }[diagnostic]
    assert expected_text in output
    _assert_refused_unchanged(packs_root, before)
