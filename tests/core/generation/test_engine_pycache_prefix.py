"""Pinned sources stay clean when the engine imports them.

A third-party node may commit stale bytecode and ship no .gitignore. Importing it
without a pycache prefix rewrites the tracked .pyc, which then fails the source
revalidation. The engine's one process env points bytecode at its own prefix tree.
"""

import os
import subprocess
import sys

from astrid.core.generation import engine_runner


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _source(tmp_path):
    repo = tmp_path / "node"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "node.py").write_text("VALUE = 1\n", encoding="utf-8")
    cache = repo / "__pycache__"
    cache.mkdir()
    stale = cache / f"node.{sys.implementation.cache_tag}.pyc"
    stale.write_bytes(b"\x00stale-bytecode-not-a-valid-pyc")
    _git(repo, "add", "node.py", f"__pycache__/{stale.name}")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "pin")
    return repo


def _import(repo, env):
    subprocess.run([sys.executable, "-c", "import node"], cwd=str(repo), env=env, check=True,
                   capture_output=True)


def test_engine_env_keeps_committed_bytecode_clean_under_import(tmp_path):
    repo = _source(tmp_path)
    root = tmp_path / "engine"
    env = engine_runner.engine_process_env({"PATH": os.environ["PATH"]}, str(root))
    _import(repo, env)
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""


def test_import_without_the_prefix_rewrites_the_tracked_bytecode(tmp_path):
    repo = _source(tmp_path)
    _import(repo, {"PATH": os.environ["PATH"]})
    assert _git(repo, "status", "--porcelain", "--untracked-files=all").strip() != ""


def test_unrelated_untracked_file_still_fails_after_the_prefix_import(tmp_path):
    repo = _source(tmp_path)
    env = engine_runner.engine_process_env({"PATH": os.environ["PATH"]}, str(tmp_path / "engine"))
    _import(repo, env)
    (repo / "notes.txt").write_text("generated\n", encoding="utf-8")
    assert "notes.txt" in _git(repo, "status", "--porcelain", "--untracked-files=all")


def test_engine_launch_env_carries_the_pycache_prefix(tmp_path):
    """The ComfyUI launch is built by engine_child_env from the engine's process env."""
    from astrid.core.generation._engine_python_bootstrap import engine_child_env

    root = tmp_path / "engine"
    venv = root / "runtime" / "venv"
    launch = engine_child_env(str(venv), engine_runner.engine_process_env({"PATH": "/bin"}, str(root)))
    assert launch["PYTHONPYCACHEPREFIX"] == f"{root}/.astrid-engine/pycache"
