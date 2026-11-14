"""CPU-only round trips for the sealed engine environment snapshot on a network volume."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

import pytest

from astrid.core.generation import model_storage
from astrid.packs.runpod import engine_snapshot as es

REPO = Path(__file__).resolve().parents[3]
INPUTS = {"schema": es.SCHEMA, "engine": "comfyui", "platform": "linux-x86_64-cu130",
          "recipe_sha256": "a" * 64, "helper_sha256": "b" * 64,
          "requirements_locks": {"comfy": "c" * 64}, "python_versions": {"comfy": "3.12.10"},
          "python_abi": "cp312", "machine": "x86_64"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    inputs = json.loads(json.dumps(INPUTS))
    monkeypatch.setattr(es, "describe", lambda engine, platform: {
        "inputs": inputs, "pins": {"comfyui": "1" * 40, "vibecomfy": "2" * 40, "node_lock_sha256": "d" * 64},
        "venv": "runtime/venv"})
    root = tmp_path / "container" / "engines" / "comfyui"
    py = tmp_path / "managed" / "cpython-3.12.10-linux-x86_64-gnu"
    cache = tmp_path / "vol" / "astrid" / "engine-envs" / "comfyui"
    (py / "bin").mkdir(parents=True)
    (py / "lib").mkdir()
    (py / "bin" / "python3").write_text("#!/bin/sh\necho fake\n")
    (py / "bin" / "python3").chmod(0o755)
    (py / "lib" / "os.py").write_text("# stdlib\n")
    venv = root / "runtime" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "lib").mkdir()
    (venv / "pyvenv.cfg").write_text(f"home = {py}/bin\nversion_info = 3.12.10\n")
    (venv / "bin" / "python").symlink_to(py / "bin" / "python3")
    (venv / "bin" / "python3").symlink_to("python")
    (venv / "lib" / "a.so").write_bytes(b"shared-object" * 100)
    os.link(venv / "lib" / "a.so", venv / "lib" / "b.so")
    (venv / "bin" / "tool").write_text("#!/bin/sh\n")
    (venv / "bin" / "tool").chmod(0o4755)
    (root / "runtime" / "ComfyUI").mkdir()
    (root / "runtime" / "ComfyUI" / "main.py").write_text("print('comfy')\n")
    (root / "runtime" / "uv-cache").mkdir()
    (root / "runtime" / "uv-cache" / "blob").write_bytes(b"cache")
    control = root / ".astrid-engine"
    control.mkdir(mode=0o700)
    (control / "environments" / "comfy").mkdir(parents=True)
    (control / "environments" / "comfy" / "receipt.json").write_text('{"validated": true}')
    (control / "environments" / "comfy" / "requirements.lock").write_text("pkg==1 --hash=sha256:x\n")
    (control / "pycache").mkdir()
    (control / "pycache" / "m.pyc").write_bytes(b"pyc")
    (control / "install.json").write_text(json.dumps({"status": "installed"}))
    (control / "install.lock").write_text("")
    (control / "session").mkdir()
    (control / "session" / "x").write_text("owner")
    (control / "model-verification").mkdir()
    (control / "model-verification" / "r.json").write_text("{}")
    (root / "models").mkdir()
    (root / "models" / "big.safetensors").write_bytes(b"m")

    class World:
        pass

    w = World()
    w.root, w.py, w.cache, w.inputs, w.tmp = root, py, cache, inputs, tmp_path
    w.identity = lambda: es.identity_of(inputs)
    return w


def run(w, command, *extra, capsys=None):
    argv = [command, "--root", str(w.root), "--cache", str(w.cache), "--engine", "comfyui",
            "--platform", "linux-x86_64-cu130", *extra]
    import contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = es.main(argv)
    assert code == 0
    lines = out.getvalue().splitlines()
    assert lines and lines[-1].startswith("ENGINE_SNAPSHOT_JSON=")
    assert sum(line.startswith("ENGINE_SNAPSHOT_JSON=") for line in lines) == 1
    result = json.loads(lines[-1].split("=", 1)[1])
    assert isinstance(result["durations"], dict)
    if "reason" in result:
        assert result["reason"] in es.REASONS
    return result


def files(base):
    return sorted(str(p.relative_to(base)) for p in base.rglob("*"))


def snapshot_dir(w):
    return w.cache / w.identity()


def manifest_path(w):
    (path,) = snapshot_dir(w).glob("*.manifest.json")
    return path


def rewrite_manifest(path, edit):
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data))


def wipe(w, *, python=True):
    shutil.rmtree(w.root)
    if python:
        shutil.rmtree(w.py)


def test_publish_restore_round_trip(world):
    w = world
    published = run(w, "publish")
    assert published["action"] == "published", published
    assert set(published["durations"]) >= {"estimate", "write_root", "write_python", "manifest", "total"}
    manifest = json.loads(manifest_path(w).read_text())
    assert manifest["identity"] == w.identity()
    assert manifest["archives"]["root"]["destination"] == str(w.root)
    assert manifest["archives"]["python"]["destination"] == str(w.py)
    assert manifest["pins"]["comfyui"] == "1" * 40
    with tarfile.open(snapshot_dir(w) / (manifest_path(w).name.split(".")[0] + ".root.tar")) as tar:
        assert all(m.type != tarfile.LNKTYPE and m.uid == 0 and m.uname == "" for m in tar)
    before = files(w.root)
    wipe(w)
    restored = run(w, "restore")
    assert restored["action"] == "restored", restored
    assert restored["identity"] == w.identity()
    assert set(restored["durations"]) >= {"manifest_check", "restore_root", "restore_python", "total"}
    after = files(w.root)
    for absent in (".astrid-engine/install.json", ".astrid-engine/install.lock", ".astrid-engine/session",
                   ".astrid-engine/model-verification", "runtime/uv-cache", "models"):
        assert absent in before and absent not in after
    for present in ("runtime/venv/pyvenv.cfg", "runtime/venv/lib/a.so", "runtime/venv/lib/b.so",
                    ".astrid-engine/environments/comfy/requirements.lock",
                    ".astrid-engine/environments/comfy/receipt.json", ".astrid-engine/pycache/m.pyc",
                    "runtime/ComfyUI/main.py"):
        assert present in after
    assert (w.root / "runtime/venv/lib/b.so").read_bytes() == b"shared-object" * 100
    assert os.readlink(w.root / "runtime/venv/bin/python") == str(w.py / "bin" / "python3")
    assert (w.root / "runtime/venv/bin/python").resolve() == (w.py / "bin" / "python3").resolve()
    assert (w.py / "bin" / "python3").read_text().startswith("#!/bin/sh")
    for path in [*w.root.rglob("*"), *w.py.rglob("*")]:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            assert info.st_nlink == 1
            assert not info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
    for path in (w.root, w.root / ".astrid-engine"):
        info = path.lstat()
        assert stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.getuid()
    assert not list(w.root.parent.glob("*.restore-*")) and not list(w.py.parent.glob("*.restore-*"))


def test_restore_leaves_present_python_alone(world):
    w = world
    assert run(w, "publish")["action"] == "published"
    wipe(w, python=False)
    (w.py / "bin" / "python3").write_text("local copy\n")
    (w.py / "marker").write_text("keep")
    result = run(w, "restore")
    assert result["action"] == "restored"
    assert (w.py / "bin" / "python3").read_text() == "local copy\n"
    assert (w.py / "marker").read_text() == "keep"


def test_restore_into_hook_created_empty_root(world):
    w = world
    assert run(w, "publish")["action"] == "published"
    wipe(w)
    (w.root / ".astrid-engine").mkdir(parents=True, mode=0o755)
    result = run(w, "restore")
    assert result["action"] == "restored", result
    assert stat.S_IMODE((w.root / ".astrid-engine").lstat().st_mode) == 0o700
    assert stat.S_IMODE(w.root.lstat().st_mode) == 0o700


def test_second_publish_is_already_published(world):
    assert run(world, "publish")["action"] == "published"
    second = run(world, "publish")
    assert (second["action"], second["reason"]) == ("skipped", "already_published")
    assert len(list(snapshot_dir(world).glob("*.tar"))) == 2


def test_publish_requires_installed_receipt(world):
    (world.root / ".astrid-engine" / "install.json").write_text(json.dumps({"status": "installing"}))
    result = run(world, "publish")
    assert (result["action"], result["reason"]) == ("skipped", "not_installed")
    assert not world.cache.exists() or not any(world.cache.rglob("*.tar"))


def test_identity_change_is_no_snapshot(world):
    assert run(world, "publish")["action"] == "published"
    wipe(world)
    world.inputs["recipe_sha256"] = "e" * 64
    result = run(world, "restore")
    assert (result["action"], result["reason"]) == ("skipped", "no_snapshot")
    assert not world.root.exists()


def test_missing_manifest_is_no_snapshot(world):
    assert run(world, "publish")["action"] == "published"
    manifest_path(world).unlink()
    wipe(world)
    assert run(world, "restore")["reason"] == "no_snapshot"


@pytest.mark.parametrize("field", ["size", "mtime_ns"])
def test_recorded_tar_stat_drift_is_not_reused(world, field):
    assert run(world, "publish")["action"] == "published"
    rewrite_manifest(manifest_path(world), lambda m: m["archives"]["root"].update({field: m["archives"]["root"][field] + 1}))
    wipe(world)
    assert run(world, "restore")["reason"] == "no_snapshot"
    assert run(world, "report")["identities"][0]["valid"] == 0


def test_tar_touched_after_publish_is_not_reused(world):
    assert run(world, "publish")["action"] == "published"
    (tar,) = snapshot_dir(world).glob("*.root.tar")
    os.utime(tar, ns=(1, 1))
    wipe(world)
    assert run(world, "restore")["reason"] == "no_snapshot"


def _reseal(path, manifest):
    info = path.stat()
    rewrite_manifest(manifest, lambda m: m["archives"]["root"].update(size=info.st_size, mtime_ns=info.st_mtime_ns))


def test_truncated_tar_is_skipped_and_staging_removed(world):
    assert run(world, "publish")["action"] == "published"
    (tar,) = snapshot_dir(world).glob("*.root.tar")
    data = tar.read_bytes()
    tar.write_bytes(data[: len(data) // 2 + 100])
    _reseal(tar, manifest_path(world))
    wipe(world)
    result = run(world, "restore")
    assert result["action"] == "skipped"
    assert result["reason"] in {"corrupt_archive", "digest_mismatch"}
    assert not world.root.exists()
    assert not list(world.root.parent.glob("*.restore-*"))


def test_same_size_digest_mismatch(world):
    assert run(world, "publish")["action"] == "published"
    (tar,) = snapshot_dir(world).glob("*.root.tar")
    info = tar.stat()
    data = bytearray(tar.read_bytes())
    data[-1] ^= 0xFF
    tar.write_bytes(bytes(data))
    os.utime(tar, ns=(info.st_atime_ns, info.st_mtime_ns))
    wipe(world)
    result = run(world, "restore")
    assert (result["action"], result["reason"]) == ("skipped", "digest_mismatch")
    assert not world.root.exists()
    assert not list(world.root.parent.glob("*.restore-*"))


def _forge(w, members):
    """Write a hand-crafted root tar with a manifest that matches it exactly."""
    directory = snapshot_dir(w)
    directory.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex
    path = directory / f"{name}.root.tar"
    count = total = 0
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for info, data in members:
            count += 1
            if data is not None:
                info.size = len(data)
                total += len(data)
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    stat_ = path.stat()
    manifest = {"schema": es.SCHEMA, **w.inputs, "identity": w.identity(), "pins": {},
                "created_at": "2026-01-01T00:00:00.000000Z",
                "archives": {"root": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                      "size": stat_.st_size, "mtime_ns": stat_.st_mtime_ns,
                                      "members": count, "bytes": total, "destination": str(w.root)},
                             "python": None}}
    (directory / f"{name}.manifest.json").write_text(json.dumps(manifest))


def _member(name, kind=tarfile.REGTYPE, **fields):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
    for key, value in fields.items():
        setattr(info, key, value)
    return info


@pytest.mark.parametrize("case", ["dotdot", "absolute", "outside_symlink", "hardlink", "chardev",
                                  "through_symlink", "excluded_control", "empty"])
def test_unsafe_members_are_refused(world, case):
    w = world
    wipe(w)
    base = [(_member("runtime", tarfile.DIRTYPE), None)]
    bad = {
        "dotdot": [(_member("runtime/../../escape"), b"x")],
        "absolute": [(_member("/tmp/escape"), b"x")],
        "outside_symlink": [(_member("runtime/evil", tarfile.SYMTYPE, linkname="/etc"), None)],
        "hardlink": [(_member("runtime/a"), b"x"), (_member("runtime/b", tarfile.LNKTYPE, linkname="runtime/a"), None)],
        "chardev": [(_member("runtime/null", tarfile.CHRTYPE, devmajor=1, devminor=3), None)],
        "through_symlink": [(_member("runtime/d", tarfile.DIRTYPE), None),
                            (_member("runtime/l", tarfile.SYMTYPE, linkname=str(w.root / "runtime" / "d")), None),
                            (_member("runtime/l/f"), b"x")],
        "excluded_control": [(_member(".astrid-engine/install.json"), b'{"status":"installed"}')],
        "empty": [(_member(""), b"x")],
    }[case]
    _forge(w, base + bad)
    result = run(w, "restore")
    assert (result["action"], result["reason"]) == ("skipped", "unsafe_member"), result
    assert not w.root.exists()
    assert not list(w.root.parent.glob("*.restore-*"))
    assert not (w.tmp / "escape").exists()
    assert str(w.root) not in json.dumps(result)


def test_partial_content_is_untouched(world):
    assert run(world, "publish")["action"] == "published"
    (world.root / ".astrid-engine" / "install.json").unlink()
    before = files(world.root)
    result = run(world, "restore")
    assert (result["action"], result["reason"]) == ("skipped", "partial_content")
    assert files(world.root) == before


def test_publish_skips_on_insufficient_statvfs_space(world, monkeypatch):
    real = os.statvfs

    class Tiny:
        f_bavail, f_frsize = 1, 4096

    (world.tmp / "vol").mkdir()
    monkeypatch.setattr(os, "statvfs", lambda path: Tiny() if str(path).startswith(str(world.tmp / "vol")) else real(path))
    result = run(world, "publish")
    assert (result["action"], result["reason"]) == ("skipped", "insufficient_space")
    assert result["estimate_bytes"] > 0
    assert not world.cache.exists() or not any(p.is_file() for p in world.cache.rglob("*"))


def test_publish_skips_on_insufficient_declared_volume(world, monkeypatch):
    calls = []

    def measure(volume, directory, *, deadline, check):
        calls.append((volume, directory))
        return model_storage.VolumeSpace(volume.mount_path, 0, 10)

    monkeypatch.setattr(model_storage, "measure_declared_volume", measure)
    result = run(world, "publish", "--volume-size-gb", "10", "--mount", str(world.tmp / "vol"))
    assert result["reason"] == "insufficient_space"
    assert calls == [(model_storage.DeclaredVolume(world.tmp / "vol", 10), world.cache)]


def test_concurrent_publisher_loses_race(world, monkeypatch):
    real = es._valid_manifests
    calls = []

    def racing(*args, **kwargs):
        calls.append(1)
        found = real(*args, **kwargs)
        return found if len(calls) == 1 else [("other", {"created_at": "z"})]

    monkeypatch.setattr(es, "_valid_manifests", racing)
    result = run(world, "publish")
    assert (result["action"], result["reason"]) == ("skipped", "lost_race")
    assert len(calls) == 2
    assert not [p for p in world.cache.rglob("*") if p.is_file()]


def test_report_cleans_old_partials_and_keeps_snapshot(world):
    assert run(world, "publish")["action"] == "published"
    directory = snapshot_dir(world)
    old = time.time() - 7200
    stale = directory / "dead.root.tar.partial"
    stale.write_bytes(b"x" * 10)
    orphan = directory / "orphan.root.tar"
    orphan.write_bytes(b"y" * 10)
    for path in (stale, orphan):
        os.utime(path, (old, old))
    fresh = directory / "live.root.tar.partial"
    fresh.write_bytes(b"z")
    report = run(world, "report")
    assert report["action"] == "reported"
    assert report["cleaned"] == 2
    assert report["partials"] == 3
    (row,) = report["identities"]
    assert row["identity"] == world.identity() and row["valid"] == 1 and row["current"] is True
    assert row["bytes"] > 0 and report["total_bytes"] >= row["bytes"]
    assert not stale.exists() and not orphan.exists() and fresh.exists()
    wipe(world)
    assert run(world, "restore")["action"] == "restored"


def test_prune_requires_confirm(world):
    assert run(world, "publish")["action"] == "published"
    dry = run(world, "prune", "--identity", world.identity())
    assert dry["action"] == "dry_run" and dry["files"] >= 3
    assert snapshot_dir(world).exists()
    done = run(world, "prune", "--identity", world.identity(), "--confirm")
    assert done["action"] == "pruned"
    assert not snapshot_dir(world).exists()


def test_usage_errors_exit_nonzero(world):
    with pytest.raises(SystemExit) as raised:
        es.main(["restore", "--root", str(world.root), "--cache", str(world.cache), "--engine", "comfyui",
                 "--platform", "linux-x86_64-cu130", "--confirm"])
    assert raised.value.code != 0
    with pytest.raises(SystemExit):
        es.main(["prune", "--root", str(world.root), "--cache", str(world.cache), "--engine", "comfyui",
                 "--platform", "linux-x86_64-cu130", "--identity", "../x"])


def test_declared_volume_measure_counts_snapshot_bytes(world):
    mount = world.tmp / "vol"
    mount.mkdir(parents=True, exist_ok=True)
    volume = model_storage.DeclaredVolume(mount, 1)
    deadline = time.monotonic() + 30
    before = model_storage.measure_declared_volume(volume, world.cache, deadline=deadline, check=lambda: None)
    assert run(world, "publish", "--volume-size-gb", "1", "--mount", str(mount))["action"] == "published"
    after = model_storage.measure_declared_volume(volume, world.cache, deadline=deadline, check=lambda: None)
    tars = sum(p.stat().st_size for p in snapshot_dir(world).glob("*.tar"))
    reduced = before.free_bytes - after.free_bytes
    assert tars <= reduced <= tars + 1024 * 1024


def test_real_recipe_identity_is_offline():
    described = es.describe("comfyui", "linux-x86_64-cu130")
    inputs = described["inputs"]
    assert inputs["python_abi"] == "cp312" and inputs["schema"] == es.SCHEMA
    assert set(inputs["requirements_locks"]) == {"comfy"} == set(inputs["python_versions"])
    assert len(es.identity_of(inputs)) == 64
    assert described["venv"] == "runtime/venv"
    assert set(described["pins"]) == {"comfyui", "vibecomfy", "node_lock_sha256"}


def test_module_entry_point(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "astrid.packs.runpod.engine_snapshot", "report",
                           "--root", str(tmp_path / "r"), "--cache", str(tmp_path / "c"),
                           "--engine", "comfyui", "--platform", "linux-x86_64-cu130"],
                          cwd=REPO, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    line = proc.stdout.strip().splitlines()[-1]
    assert json.loads(line.split("=", 1)[1])["action"] == "reported"
