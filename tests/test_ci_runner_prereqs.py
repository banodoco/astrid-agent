import hashlib
import io
import json
import os
import tarfile
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import ci_runner_prereqs as prereqs


def test_archive_rejects_escape_paths_and_external_symlinks(tmp_path):
    for kind in ("path", "link"):
        archive = tmp_path / (kind + ".tar.gz")
        with tarfile.open(archive, "w:gz") as tf:
            info = tarfile.TarInfo("../outside" if kind == "path" else "python/bin/escape")
            if kind == "link":
                info.type = tarfile.SYMTYPE
                info.linkname = "../../../outside"
                tf.addfile(info)
            else:
                payload = b"bad"
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))
        dest = tmp_path / (kind + "-dest")
        dest.mkdir()
        with pytest.raises(RuntimeError, match="unsafe|escapes"):
            prereqs.extract_safe(archive, dest)
        assert not (tmp_path / "outside").exists()


def test_owned_subdir_refuses_external_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(prereqs, "UID", os.getuid())
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "Python").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError):
        prereqs.owned_subdir(root, "Python")
    assert list(outside.iterdir()) == []


def test_stage_and_rollback_are_runner_owned_and_restore_exact_path(tmp_path, monkeypatch):
    monkeypatch.setattr(prereqs, "UID", os.getuid())
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    runner = home / "actions-runner"
    tool = runner / "_work/_tool"
    temp = runner / "_work/_temp"
    tool.mkdir(parents=True)
    temp.mkdir()
    old_path = b"/usr/local/bin:/usr/bin:/bin\n"
    (runner / ".path").write_bytes(old_path)
    (runner / ".env").write_bytes(b"EXISTING_SETTING=preserved\n")
    monkeypatch.setenv("RUNNER_TOOL_CACHE", str(tool))
    monkeypatch.setenv("RUNNER_TEMP", str(temp))
    def download(url, target, digest): target.write_bytes(b"test archive")
    def extract(archive, destination):
        if archive.name == "python.tar.gz":
            bindir = destination / "python/bin"
            bindir.mkdir(parents=True)
            (bindir / "python").write_text("placeholder")
        else:
            (destination / "pwsh").write_text("placeholder")
    def output(args, **kwargs):
        if args[0].endswith("pwsh"): return "7.6.9\n"
        return json.dumps({"version": prereqs.PY_VERSION, "arch": "arm64",
                           "prefix": str(tool / "Python" / prereqs.PY_VERSION / "arm64")})
    monkeypatch.setattr(prereqs, "download", download)
    monkeypatch.setattr(prereqs, "extract_safe", extract)
    monkeypatch.setattr(prereqs.subprocess, "check_output", output)
    monkeypatch.setattr(prereqs.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""))
    prereqs.stage(home, runner, tool, temp)
    assert (runner / ".path").read_bytes().endswith(old_path)
    assert (runner / ".env").read_bytes() == b"EXISTING_SETTING=preserved\n"
    marker = tool / "Python" / prereqs.PY_VERSION / "arm64.complete"
    assert marker.exists()
    prereqs.rollback(home, runner, tool)
    assert (runner / ".path").read_bytes() == old_path
    assert (runner / ".env").read_bytes() == b"EXISTING_SETTING=preserved\n"
    assert not marker.exists() and not marker.with_name("arm64").exists()
    assert temp.exists()


def test_rollback_refuses_changed_runner_path(tmp_path, monkeypatch):
    monkeypatch.setattr(prereqs, "UID", os.getuid())
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    runner = home / "actions-runner"
    tool = runner / "_work/_tool"
    tool.mkdir(parents=True)
    root = home / ".astrid-runner-prereqs-v1"
    root.mkdir()
    (runner / ".path").write_text("someone-changed-this\n")
    (runner / ".path").chmod(0o600)
    backup = b"original\n"
    (root / "path.backup").write_bytes(backup)
    journal = {"schema": 1, "state": "pending-restart", "run_id": "a" * 32, "new_sha256": {"path": "f" * 64},
               "before": {"path": {"exists": True, "mode": 0o600, "sha256": hashlib.sha256(backup).hexdigest()}}}
    (root / "journal.json").write_text(json.dumps(journal))
    with pytest.raises(RuntimeError, match="changed since staging"):
        prereqs.rollback(home, runner, tool)
    assert (runner / ".path").read_text() == "someone-changed-this\n"


def test_workflow_embeds_stage_and_uses_real_setup_python():
    base = Path(__file__).parents[1]
    workflow = (base / ".github/workflows/journey.yml").read_text()
    embedded = workflow.split('          /usr/bin/python3 - "$PREREQ_PHASE" <<\'PY\'\n', 1)[1].split("\n          PY\n", 1)[0]
    assert textwrap.dedent(embedded).strip() == (base / "scripts/ci_runner_prereqs.py").read_text().strip()
    assert "actions/setup-python@v5" in workflow
    assert "runner-owned standalone" in workflow


def test_partial_stage_rolls_back_only_its_marked_python(tmp_path, monkeypatch):
    monkeypatch.setattr(prereqs, "UID", os.getuid())
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    runner = home / "actions-runner"
    tool = runner / "_work/_tool"
    temp = runner / "_work/_temp"
    tool.mkdir(parents=True)
    temp.mkdir()
    (runner / ".path").write_text("/usr/bin:/bin\n")
    monkeypatch.setenv("RUNNER_TOOL_CACHE", str(tool))
    monkeypatch.setenv("RUNNER_TEMP", str(temp))
    monkeypatch.setattr(prereqs, "download", lambda url, target, digest: target.write_bytes(b"fixture"))
    def extract(archive, destination):
        if archive.name == "python.tar.gz":
            (destination / "python/bin").mkdir(parents=True)
            (destination / "python/bin/python").write_text("placeholder")
        else:
            (destination / "pwsh").write_text("placeholder")
    def output(args, **kwargs):
        if args[0].endswith("pwsh"): return "7.6.9\n"
        raise RuntimeError("simulated interpreter failure")
    monkeypatch.setattr(prereqs, "extract_safe", extract)
    monkeypatch.setattr(prereqs.subprocess, "check_output", output)
    monkeypatch.setattr(prereqs.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""))
    with pytest.raises(RuntimeError, match="simulated"):
        prereqs.stage(home, runner, tool, temp)
    python_dir = tool / "Python" / prereqs.PY_VERSION / "arm64"
    assert python_dir.exists() and (runner / ".path").read_text() == "/usr/bin:/bin\n"
    prereqs.rollback(home, runner, tool)
    assert not python_dir.exists() and (runner / ".path").read_text() == "/usr/bin:/bin\n"


def test_replace_preserves_original_mode_despite_umask(tmp_path, monkeypatch):
    monkeypatch.setattr(prereqs, "UID", os.getuid())
    path = tmp_path / "settings"
    path.write_text("old")
    previous = os.umask(0o077)
    try:
        prereqs.replace_owned(path, b"new", 0o664, tmp_path)
    finally:
        os.umask(previous)
    assert path.read_bytes() == b"new" and path.stat().st_mode & 0o777 == 0o664


def test_archive_rejects_link_as_member_parent_and_chained_target(tmp_path):
    for kind in ("member", "target"):
        archive = tmp_path / (kind + ".tgz")
        with tarfile.open(archive, "w:gz") as tf:
            link = tarfile.TarInfo("python/alias")
            link.type = tarfile.SYMTYPE
            link.linkname = "real"
            tf.addfile(link)
            if kind == "member":
                item = tarfile.TarInfo("python/alias/child")
                item.size = 1
                tf.addfile(item, io.BytesIO(b"x"))
            else:
                item = tarfile.TarInfo("python/other")
                item.type = tarfile.SYMTYPE
                item.linkname = "alias/child"
                tf.addfile(item)
        dest = tmp_path / (kind + "-extract")
        dest.mkdir()
        with pytest.raises(RuntimeError, match="traverses"):
            prereqs.extract_safe(archive, dest)


def test_main_attempts_rollback_of_own_failed_transaction(tmp_path, monkeypatch):
    monkeypatch.setattr(prereqs, "UID", os.getuid())
    monkeypatch.setattr(prereqs.os, "getuid", lambda: prereqs.UID)
    monkeypatch.setattr(prereqs.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    monkeypatch.setenv("RUNNER_NAME", "hannah-mac")
    monkeypatch.setattr(prereqs.sys, "argv", ["script", "stage"])
    calls = []
    def fail_stage(home, runner, tool, temp, token):
        root = home / ".astrid-runner-prereqs-v1"
        root.mkdir()
        (root / "journal.json").write_text(json.dumps({"run_id": token}))
        raise RuntimeError("fixture")
    monkeypatch.setattr(prereqs, "stage", fail_stage)
    monkeypatch.setattr(prereqs, "rollback", lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match="fixture"):
        prereqs.main()
    assert len(calls) == 1


def test_interrupted_owned_path_temporary_is_replaced_safely(tmp_path, monkeypatch):
    monkeypatch.setattr(prereqs, "UID", os.getuid())
    path = tmp_path / "settings"
    path.write_text("old")
    (tmp_path / "path.next").write_text("partial")
    prereqs.replace_owned(path, b"replacement", 0o600, tmp_path)
    assert path.read_text() == "replacement"
    assert not (tmp_path / "path.next").exists()
