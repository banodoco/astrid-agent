"""Pinned-source revalidation ignores only Python bytecode.

A third-party node may ship no .gitignore, and an import or compile on the pod writes
__pycache__/*.pyc into its checkout. Those files are not uncommitted source; any other
untracked or modified path still makes the source dirty.
"""

import subprocess

from astrid.core.generation import engine_runner


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _dirty(repo):
    argv = engine_runner._source_status_argv(str(repo))
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def _source(tmp_path):
    repo = tmp_path / "node"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-q", "-m", "pin")
    (repo / "node.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "node.py")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "src")
    return repo


def test_generated_bytecode_is_not_uncommitted_source(tmp_path):
    repo = _source(tmp_path)
    cache = repo / "qwen_tts" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "modeling.cpython-312.pyc").write_bytes(b"\x00bytecode")
    (repo / "node.pyc").write_bytes(b"\x00stray")
    assert _dirty(repo) == ""


def test_other_untracked_files_still_fail_the_source(tmp_path):
    repo = _source(tmp_path)
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "a.pyc").write_bytes(b"\x00")
    (repo / "notes.txt").write_text("generated\n", encoding="utf-8")
    assert "notes.txt" in _dirty(repo)


def test_modified_tracked_source_still_fails(tmp_path):
    repo = _source(tmp_path)
    (repo / "node.py").write_text("x = 2\n", encoding="utf-8")
    assert "node.py" in _dirty(repo)
