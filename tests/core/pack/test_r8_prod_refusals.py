"""Real CLI refusal checks for Python requirements and generation preservation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
            'pack_api: "2.0"\ndocumentation: {kind: none, reason: R8 H3 fixture.}\n' + extra,
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

    def next_revision(self) -> str:
        self.write_pack("dependencies:\n  packs: [shared]\n")
        shared = self.work / "packs" / "shared"
        shared.mkdir(parents=True, exist_ok=True)
        (shared / "pack.yaml").write_text(
            "schema_version: 3\nid: shared\nname: Shared\nversion: 1.0.0\npack_api: '2.0'\n"
            "documentation: {kind: none, reason: refusal fixture.}\n", encoding="utf-8")
        (shared / "payload.txt").write_text("shared v2\n", encoding="utf-8")
        (self.pack / "payload.txt").write_text("alpha v2\n", encoding="utf-8")
        self.commit = self.publish()
        return self.commit


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
    source_repo.write_pack("dependencies:\n  packs: [shared]\n")
    shared = source_repo.work / "packs" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "pack.yaml").write_text(
        "schema_version: 3\nid: shared\nname: Shared\nversion: 1.0.0\npack_api: '2.0'\n"
        "documentation: {kind: none, reason: admitted refusal fixture.}\n", encoding="utf-8")
    (shared / "payload.txt").write_text("shared baseline\n", encoding="utf-8")
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
        packs={"alpha": "packs/alpha", "shared": "packs/shared"}, packs_root=packs_root,
        python_packages={
            dist.metadata.get("Name", "").lower().replace("_", "-").replace(".", "-"): dist.version
            for dist in metadata.distributions() if dist.metadata.get("Name")
        },
        builds={"alpha": {"entry": "entry.tsx", "extension_manifest": extension_manifest}},
        toolchain=toolchain(), intent="explicit",
    )
    assert _admission_bytes(packs_root).keys() == {"alpha"}


def _cli_add_folder(source: Path, pack_id: str, packs_root: Path) -> int:
    from astrid.core.pack.cli import main as packs_cli_main

    return packs_cli_main(["add", str(source), "--pack-id", pack_id,
                           "--packs-root", str(packs_root), "--yes"])


def _refusal_snapshot(packs_root: Path) -> tuple[dict[str, bytes], dict[str, bytes]]:
    tree = _tree_bytes(packs_root)
    admissions = _admission_bytes(packs_root)
    assert admissions.keys() == {"alpha"}
    return tree, admissions


def _assert_refused_unchanged(packs_root: Path, before: tuple[dict[str, bytes], dict[str, bytes]]) -> None:
    assert _tree_bytes(packs_root) == before[0]
    assert _admission_bytes(packs_root) == before[1]


def test_real_cli_add_installs_python_requirement_and_runtime_executes_installed_action(
        source_repo: SourceRepo, packs_root: Path, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path, real_runtime) -> None:
    runtime_checkout = Path(os.environ["ASTRID_RUNTIME_CHECKOUT"])
    monkeypatch.syspath_prepend(str(runtime_checkout / "packages" / "python"))
    for name in tuple(sys.modules):
        if name == "astrid_runtime_client" or name.startswith("astrid_runtime_client."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
    from astrid_runtime_client import WorkspaceClient
    from astrid.core.pack.cli import main as packs_cli_main

    installed = {
        dist.metadata.get("Name", "").lower().replace("_", "-").replace(".", "-")
        for dist in metadata.distributions() if dist.metadata.get("Name")
    }
    assert "pytest" in installed
    source_repo.write_pack("dependencies:\n  python_requirements: [pytest]\n")
    action = source_repo.pack / "actions" / "use_pytest.py"
    action.parent.mkdir(parents=True)
    action.write_text("import pytest\ndef run():\n    return 'pytest-' + pytest.__version__\n", encoding="utf-8")
    (source_repo.pack / "pack.yaml").write_text(
        "schema_version: 3\nid: alpha\nname: Alpha\nversion: 1.0.0\npack_api: '2.0'\n"
        "dependencies:\n  python_requirements: [pytest]\n"
        "actions:\n  use_pytest:\n    description: Read the installed pytest version.\n"
        "    invocation:\n      kind: python\n      path: actions/use_pytest.py\n      function: run\n"
        "    inputs: []\n    outputs: {}\n    metadata:\n      adapter_family: cpu\n"
        "      resource_keys: [cpu]\n      project_scope: optional\n"
        "documentation: {kind: none, reason: installed action proof.}\n", encoding="utf-8")
    source_repo.commit = source_repo.publish()
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert packs_cli_main([
        "add", str(source_repo.bare), "--pack-id", "alpha", "--subpath", "packs/alpha",
        "--revision", source_repo.commit, "--packs-root", str(packs_root), "--yes",
    ]) == 0
    assert (packs_root / "alpha" / "actions" / "use_pytest.py").is_file()

    daemon = real_runtime()
    try:
        workspace = WorkspaceClient(daemon.endpoint, daemon.token)
        workspace.handshake("astrid-pack-add-action-proof", "0.1.0", ["projects:read", "worker:execute"])
        host = GenericPackHost(
            pack_roots=[packs_root],
            client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
            executor_id="astrid-pack-host",
        )
        record = next(item for item in host.discover() if item.id == "alpha.use_pytest")
        host.register()
        project = workspace.create_project("Installed action", slug="installed-action",
                                           idempotency_key="installed-action-project")
        task = workspace.admit_task(
            capability_id=record.id, capability_digest=record.capability_digest,
            input_object_ids=[], project_id=project.project_id,
            idempotency_key="installed-action-task",
        )
        results = host.run(once=True)
        assert len(results) == 1
        assert results[0]["state"] == "succeeded"
        assert "pytest-" + metadata.version("pytest") in json.dumps(results[0])
        assert workspace.get_task(task.task_id).state == "succeeded"
    finally:
        daemon.stop()


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


@pytest.mark.parametrize("refusal", ["missing_python", "dependency", "release_pin", "bad_source"])
def test_real_cli_add_refusals_preserve_admitted_baseline(
        source_repo: SourceRepo, packs_root: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], refusal: str) -> None:
    _seed_admitted_generation(source_repo, packs_root)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "pack.yaml").write_text(
        "schema_version: 3\nid: beta\nname: Beta\nversion: 1.0.0\npack_api: '2.0'\n"
        "documentation: {kind: none, reason: add refusal fixture.}\n", encoding="utf-8")
    if refusal == "missing_python":
        missing = "astrid-r9-add-missing-9c2007"
        with pytest.raises(metadata.PackageNotFoundError):
            metadata.version(missing)
        (candidate / "pack.yaml").write_text((candidate / "pack.yaml").read_text() +
            f"dependencies:\n  python_requirements: [{missing}]\n", encoding="utf-8")
    elif refusal == "dependency":
        (candidate / "pack.yaml").write_text((candidate / "pack.yaml").read_text() +
            "dependencies:\n  packs: [r9_add_missing_dependency]\n", encoding="utf-8")
    elif refusal == "release_pin":
        pin_file = tmp_path / "release-pins.json"
        pin_file.write_text(json.dumps({"beta": {"commit": "f" * 40, "digest": "e" * 64}}), encoding="utf-8")
        monkeypatch.setenv("ASTRID_RELEASE_PACK_PINS", str(pin_file))
    else:
        (candidate / "pack.yaml").write_text("schema_version: [bad\n", encoding="utf-8")
    before = _refusal_snapshot(packs_root)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    assert _cli_add_folder(candidate, "beta", packs_root) != 0
    assert capsys.readouterr().err
    _assert_refused_unchanged(packs_root, before)


def test_real_cli_remove_refuses_dependency_break_and_preserves_admitted_baseline(
        source_repo: SourceRepo, packs_root: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    from astrid.core.pack.cli import main as packs_cli_main

    _seed_admitted_generation(source_repo, packs_root)
    before = _refusal_snapshot(packs_root)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert packs_cli_main(["remove", "shared", "--packs-root", str(packs_root), "--yes"]) != 0
    assert "unavailable" in capsys.readouterr().err.lower()
    _assert_refused_unchanged(packs_root, before)


def test_real_cli_rollback_refuses_release_pin_and_preserves_admitted_baseline(
        source_repo: SourceRepo, packs_root: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from astrid.core.pack.cli import main as packs_cli_main
    from astrid.core.pack.transaction import read_group
    from astrid.core.pack.identity import read_pin

    _seed_admitted_generation(source_repo, packs_root)
    beta = tmp_path / "beta"
    beta.mkdir()
    (beta / "pack.yaml").write_text(
        "schema_version: 3\nid: beta\nname: Beta\nversion: 1.0.0\npack_api: '2.0'\n"
        "documentation: {kind: none, reason: rollback refusal fixture.}\n", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert _cli_add_folder(beta, "beta", packs_root) == 0
    active = read_group(packs_root)
    pin = read_pin(active.paths["alpha"] / "PIN")
    snapshot = tmp_path / "rollback-release-pins.json"
    snapshot.write_text(json.dumps({"alpha": {
        "commit": pin["commit"], "digest": "f" * 64 if pin["digest"] != "f" * 64 else "e" * 64,
    }}), encoding="utf-8")
    monkeypatch.setenv("ASTRID_RELEASE_PACK_PINS", str(snapshot))
    before = _refusal_snapshot(packs_root)

    assert packs_cli_main(["rollback", "--packs-root", str(packs_root),
                           "--pack", "alpha", "--pack", "shared"]) != 0
    assert "release pin" in capsys.readouterr().err.lower()
    _assert_refused_unchanged(packs_root, before)


def test_real_cli_update_refuses_dependency_digest_only_pin_mismatch(
        source_repo: SourceRepo, packs_root: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from astrid.core.pack.transaction import read_group
    from astrid.core.pack.identity import read_pin

    _seed_admitted_generation(source_repo, packs_root)
    active = read_group(packs_root)
    dependency_pin = read_pin(active.paths["shared"] / "PIN")
    pin_file = tmp_path / "dependency-release-pin.json"
    wrong_digest = "f" * 64 if dependency_pin["digest"] != "f" * 64 else "e" * 64
    pin_file.write_text(json.dumps({"shared": {
        "commit": dependency_pin["commit"], "digest": wrong_digest,
    }}), encoding="utf-8")
    monkeypatch.setenv("ASTRID_RELEASE_PACK_PINS", str(pin_file))
    source_repo.next_revision()
    before = _refusal_snapshot(packs_root)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    assert _cli_update(source_repo, packs_root) == 2
    assert "pin" in capsys.readouterr().err.lower()
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
