"""Release-selected installer toolchain admission."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid import pack_build
from astrid.installer import backend, tools
from astrid.core.pack.cli_manage import _TOOLCHAIN_FIELDS


def digest(data):
    return hashlib.sha256(data).hexdigest()


def spec(payload=b''):
    return {"version": 1, "esbuild_version": "0.27.7", "app_release_sha256": "a" * 64,
            "shared_modules": {"url": "https://example.org/pack-build/lock.json", "sha256": digest(payload)}}


def shared_lock():
    return json.dumps({"version": 1, "shared_modules": {
        name: {"url": "/assets/module.js", "sha256": "a" * 64, "version": "1.2.3"}
        for name in pack_build._SINGLETONS}}).encode()


@pytest.mark.parametrize("mutate", [
    lambda x: x.update(version=True),
    lambda x: x.update(esbuild_version="0.28.1"),
    lambda x: x.update(app_release_sha256="A" * 64),
    lambda x: x.update(extra=1),
    lambda x: x["shared_modules"].update(url="http://example.org/lock"),
    lambda x: x["shared_modules"].update(url="https://user@example.org/lock"),
    lambda x: x["shared_modules"].update(sha256="bad"),
])
def test_rejects_bad_release_contract(mutate):
    value = spec()
    mutate(value)
    with pytest.raises(ValueError):
        tools.validate_pack_build(value)


@pytest.fixture
def staged(tmp_path, monkeypatch):
    folder = tmp_path / "version"
    agent = folder / "a"
    node = folder / "tools/node/bin/node"
    esbuild = agent / "remotion/node_modules/esbuild/bin/esbuild"
    api = esbuild.parent.parent / "lib/main.js"
    for path, content in ((node, b"node"), (esbuild, b"esbuild"), (api, b"api")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    monkeypatch.setattr(pack_build, "_API_HASH", digest(b"api"))
    monkeypatch.setattr(backend.subprocess, "run", lambda argv, **kwargs: SimpleNamespace(
        stdout="v20.19.4\n" if Path(argv[0]).name == "node" else "0.27.7\n"))
    payload = shared_lock()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, _size): return payload
    monkeypatch.setattr(backend.urllib.request, "build_opener", lambda *_: SimpleNamespace(
        open=lambda *_args, **_kwargs: Response()))
    manifest = {"pack_build": spec(payload), "tools": {"node": {"version": "20.19.4"}}}
    return SimpleNamespace(folder=folder, agent=agent, node=node, esbuild=esbuild, api=api,
                           env={"ASTRID_NODE_EXECUTABLE": str(node)}, manifest=manifest)


def admit(s):
    return backend._stage_pack_toolchain(s.folder, s.agent, s.env, s.manifest)


def test_publishes_exact_private_descriptor(staged):
    path = admit(staged)
    result = json.loads(path.read_text())
    assert set(result) == _TOOLCHAIN_FIELDS
    assert result["node_path"] == str(staged.folder / "pack-build/node")
    assert result["esbuild_path"] == str(staged.folder / "pack-build/esbuild/bin/esbuild")
    assert Path(result["shared_modules_lock"]).read_bytes() == shared_lock()
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
        assert path.parent.stat().st_mode & 0o077 == 0
        assert path.stat().st_nlink == 1


@pytest.mark.parametrize("damage", ["hash", "api", "alias", "version", "node_version"])
def test_refuses_tampering(staged, monkeypatch, damage):
    s = staged
    if damage == "hash":
        s.manifest["pack_build"]["shared_modules"]["sha256"] = "b" * 64
    elif damage == "api":
        s.api.write_bytes(b"bad")
    elif damage == "alias":
        outside = s.folder / "other"
        outside.write_bytes(s.esbuild.read_bytes())
        s.esbuild.unlink()
        s.esbuild.symlink_to(outside)
    elif damage == "version":
        monkeypatch.setattr(backend.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="wrong"))
    else:
        s.manifest["tools"]["node"]["version"] = "22.0.0"
    with pytest.raises(ValueError):
        admit(s)
    assert not (s.folder / "pack-build/toolchain.json").exists()


def test_existing_destination_alias_is_not_followed(staged):
    outside = staged.folder / "outside"
    outside.mkdir()
    (staged.folder / "pack-build").symlink_to(outside, target_is_directory=True)
    with pytest.raises(FileExistsError):
        admit(staged)
    assert list(outside.iterdir()) == []


def test_shared_input_hardlinks_are_not_modified(staged):
    cache = staged.folder / "npm-cache"
    cache.write_bytes(staged.esbuild.read_bytes())
    staged.esbuild.unlink()
    os.link(cache, staged.esbuild)
    original = (cache.read_bytes(), cache.stat().st_mode, cache.stat().st_nlink)
    descriptor = json.loads(admit(staged).read_text())
    assert (cache.read_bytes(), cache.stat().st_mode, cache.stat().st_nlink) == original
    assert not os.path.samefile(cache, descriptor["esbuild_path"])


def test_rejects_release_lock_with_incomplete_singletons(staged, monkeypatch):
    payload = json.dumps({"version": 1, "shared_modules": {}}).encode()
    staged.manifest["pack_build"]["shared_modules"]["sha256"] = digest(payload)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, _size): return payload
    monkeypatch.setattr(backend.urllib.request, "build_opener", lambda *_: SimpleNamespace(
        open=lambda *_args, **_kwargs: Response()))
    with pytest.raises(ValueError, match="shared-module contract"):
        admit(staged)
    assert not (staged.folder / "pack-build/toolchain.json").exists()


def test_rejects_node_outside_managed_tool_directory(staged):
    staged.env["ASTRID_NODE_EXECUTABLE"] = str(staged.folder / "tools/node/../../other")
    with pytest.raises(ValueError, match="outside the staged version|managed version"):
        admit(staged)


def test_rejects_foreign_owned_input(staged, monkeypatch):
    if os.name == "nt":
        return
    monkeypatch.setattr(backend.os, "geteuid", lambda: -1)
    with pytest.raises(ValueError, match="Foreign-owned"):
        admit(staged)
