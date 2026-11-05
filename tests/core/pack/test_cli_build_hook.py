"""PK-14 prerequisite: `packs add`/`packs update` build a pack's `ui.<name>` editor entry.

The CLI derives the transaction's ``builds`` from ``pack.yaml`` ``ui`` editor
entries (plus the extension manifest beside the entry) and passes the selected
release-pinned toolchain. A pack with a browser entry and no usable toolchain is
refused before any collection state is written. ``astrid packs <verb>`` routes to
the pack CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from astrid.core.pack.cli import main
from astrid.core.pack.identity import pack_digest
from astrid.core.pack.transaction import read_group

SINGLETONS = ("react", "react/jsx-runtime", "react/jsx-dev-runtime", "react-dom/client", "remotion",
              "@astrid/editor-sdk")
EXTENSION = {
    "id": "com.astrid.test.live-effect", "version": "1.0.0", "apiVersion": 1, "label": "Live effect",
    "publisher": "Astrid CLI build-hook fixture", "license": "MIT", "settingsSchema": {"version": 1},
    "contributions": [{"id": "effect", "kind": "effect", "effectId": "live-effect"}],
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def write_live_effect(pack: Path, marker: str) -> None:
    """The App PK-14 fixture's shape: pack.yaml ui.editor + ui/live-effect/{index.tsx,extension.json}."""
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "pack.yaml").write_text(
        "schema_version: 3\nid: live_effect\nname: Live Effect\nversion: 1.0.0\npack_api: \"1.0\"\n"
        "documentation: {kind: none, reason: CLI build-hook fixture.}\n"
        "ui:\n  editor:\n    type: editor\n    entry: ui/live-effect/index.tsx\n", encoding="utf-8")
    ui = pack / "ui" / "live-effect"
    ui.mkdir(parents=True, exist_ok=True)
    (ui / "extension.json").write_text(json.dumps(EXTENSION, indent=2) + "\n", encoding="utf-8")
    (ui / "index.tsx").write_text(textwrap.dedent(f"""\
        import React from 'react';
        import {{ defineExtension }} from '@astrid/editor-sdk';
        import manifest from './extension.json';

        export default defineExtension({{
          manifest,
          activate(ctx) {{
            ctx.effects.registerComponent('live-effect', ({{ children }}: {{ children?: React.ReactNode }}) =>
              React.createElement('div', {{ 'data-testid': 'live-effect' }}, {json.dumps(marker)}, children));
          }},
        }});
        """), encoding="utf-8")


def make_source(root: Path) -> tuple[Path, str]:
    source = root / "source"
    source.mkdir(parents=True)
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Pack CLI Test")
    git(source, "config", "user.email", "pack-cli@example.invalid")
    write_live_effect(source, "LIVE-EFFECT-V1")
    git(source, "add", "-A")
    git(source, "commit", "-m", "v1")
    return source, git(source, "rev-parse", "HEAD")


def toolchain_file(root: Path) -> Path:
    """A release toolchain lock for the explicitly supplied pinned Node/esbuild (pyt.sh env)."""
    required = ("ASTRID_BUILD_TEST_NODE", "ASTRID_BUILD_TEST_NODE_VERSION", "ASTRID_BUILD_TEST_ESBUILD")
    assert all(os.environ.get(key) for key in required), (
        "CLI build-hook tests require explicit pinned local Node/esbuild inputs: " + ", ".join(required))
    node, esbuild = Path(os.environ["ASTRID_BUILD_TEST_NODE"]), Path(os.environ["ASTRID_BUILD_TEST_ESBUILD"])
    lock = root / "extension-shared-modules.json"
    lock.write_text(json.dumps({"version": 1, "shared_modules": {
        name: {"url": f"/assets/shared-{index}.js", "sha256": sha(name.encode()), "version": "1.0.0"}
        for index, name in enumerate(SINGLETONS)}}, indent=2), encoding="utf-8")
    path = root / "toolchain.json"
    path.write_text(json.dumps({
        "version": 1,
        "node_path": str(node), "node_version": os.environ["ASTRID_BUILD_TEST_NODE_VERSION"],
        "node_sha256": sha(node.read_bytes()),
        "esbuild_path": str(esbuild), "esbuild_version": "0.27.7", "esbuild_sha256": sha(esbuild.read_bytes()),
        "shared_modules_lock": str(lock),
    }), encoding="utf-8")
    return path


def runtime_inventory(packs_root: Path) -> dict:
    """Read the collection with the PK-13 Runtime's admission reader (isolated interpreter)."""
    script = textwrap.dedent("""
        import json, sys
        from runtime_protocol.store import InstalledPackAdmissions, read_pack_admissions
        revision, records = read_pack_admissions(InstalledPackAdmissions(sys.argv[1]), pack_id="live_effect")
        print(json.dumps({"revision": revision, "records": records}, sort_keys=True))
    """)
    env = dict(os.environ, PYTHONPATH=os.environ["ASTRID_RUNTIME_ADMISSION_CHECKOUT"])
    done = subprocess.run([sys.executable, "-c", script, str(packs_root)], check=True,
                          capture_output=True, text=True, env=env)
    return json.loads(done.stdout)


def assert_admitted_effect(packs_root: Path) -> dict:
    snapshot = read_group(packs_root)
    installed = snapshot.paths["live_effect"]
    record = json.loads((installed / ".build" / "admission.json").read_bytes())
    assert record["packId"] == "live_effect"
    assert record["sourceDigest"] == pack_digest(installed)
    assert record["committedRevision"] == snapshot.revision
    manifest_bytes = (installed / ".build" / f'{record["sourceDigest"]}.artifact-manifest.json').read_bytes()
    assert sha(manifest_bytes) == record["artifactManifestDigest"]
    artifact = json.loads(manifest_bytes)
    # The admission binds (by digest) the artifact manifest that names the effect.
    assert artifact["extensionManifest"] == EXTENSION
    assert {"id": "effect", "kind": "effect", "effectId": "live-effect"} in artifact["extensionManifest"]["contributions"]
    assert snapshot.admissions["live_effect"]["artifact_manifest"] == artifact
    inventory = runtime_inventory(packs_root)
    assert inventory["revision"] == snapshot.revision and len(inventory["records"]) == 1
    assert inventory["records"][0]["artifactManifest"]["extensionManifest"]["contributions"][0]["effectId"] == "live-effect"
    return record


def test_packs_add_builds_ui_editor_entry_and_writes_admission(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    monkeypatch.delenv("ASTRID_PACK_TOOLCHAIN", raising=False)
    source, commit = make_source(tmp_path)
    packs_root = tmp_path / "collection" / "packs"
    tools = toolchain_file(tmp_path)

    assert main(["add", str(source), "--pack-id", "live_effect", "--revision", commit,
                 "--packs-root", str(packs_root), "--toolchain", str(tools), "--yes"]) == 0, capsys.readouterr()
    assert_admitted_effect(packs_root)
    assert not (tmp_path / "home" / "Astrid").exists()


def test_packs_update_rebuilds_ui_editor_entry_from_toolchain_env(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    source, commit = make_source(tmp_path)
    packs_root = tmp_path / "collection" / "packs"
    monkeypatch.setenv("ASTRID_PACK_TOOLCHAIN", str(toolchain_file(tmp_path)))
    assert main(["add", str(source), "--pack-id", "live_effect", "--revision", commit,
                 "--packs-root", str(packs_root), "--yes"]) == 0, capsys.readouterr()
    first = assert_admitted_effect(packs_root)

    write_live_effect(source, "LIVE-EFFECT-V2")
    git(source, "commit", "-am", "v2")
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert main(["update", "--source", str(source), "--revision", git(source, "rev-parse", "HEAD"),
                 "--pack", "live_effect=.", "--packs-root", str(packs_root)]) == 0, capsys.readouterr()
    second = assert_admitted_effect(packs_root)
    assert second["sourceDigest"] != first["sourceDigest"]
    assert second["committedRevision"] == first["committedRevision"] + 1


def test_missing_or_mismatched_toolchain_refuses_before_any_state_write(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    monkeypatch.delenv("ASTRID_PACK_TOOLCHAIN", raising=False)
    source, commit = make_source(tmp_path)
    parent = tmp_path / "collection"
    packs_root = parent / "packs"
    add = ["add", str(source), "--pack-id", "live_effect", "--revision", commit,
           "--packs-root", str(packs_root), "--yes"]

    assert main(add) == 1
    err = capsys.readouterr().err
    assert "toolchain" in err and "--toolchain" in err and "ASTRID_PACK_TOOLCHAIN" in err
    assert not parent.exists(), "no packs root, retired generation or work directory may be created"

    tools = toolchain_file(tmp_path)
    value = json.loads(tools.read_text(encoding="utf-8"))
    value["esbuild_sha256"] = "0" * 64
    tools.write_text(json.dumps(value), encoding="utf-8")
    assert main([*add, "--toolchain", str(tools)]) == 1
    assert "esbuild" in capsys.readouterr().err
    assert not parent.exists()

    assert main([*add, "--toolchain", str(tmp_path / "absent.json")]) == 1
    assert "toolchain" in capsys.readouterr().err
    assert not parent.exists()
    assert not (tmp_path / "home" / "Astrid").exists()


def test_astrid_console_routes_packs_to_the_pack_cli(tmp_path: Path) -> None:
    env = dict(os.environ, HOME=str(tmp_path))
    done = subprocess.run([sys.executable, "-c", "from astrid.console import main; raise SystemExit(main())",
                           "packs", "add", "--help"], capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "--pack-id" in done.stdout and "--packs-root" in done.stdout and "--toolchain" in done.stdout
    assert "flags must precede the message" not in done.stderr
    assert not (tmp_path / "Astrid").exists()
