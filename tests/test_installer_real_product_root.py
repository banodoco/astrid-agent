"""Stage a real product root and admit it with validate_candidate (R7b-S4).

Integration lane (``-m integration``). Needs uv on PATH, a Runtime checkout (``ASTRID_RUNTIME_CHECKOUT`` or
the sibling ``astrid-runtime``) and network or a warm uv cache for the hash-locked Runtime dependencies.

Real, not stubbed:
* ``Backend.stage()``: the pinned agent and Runtime commits are materialized with ``git archive``, a real
  ``uv venv`` is created, the agent's ``runtime.lock`` is installed with ``--require-hashes``, and agent,
  Runtime and the VibeComfy stand-in are installed non-editable into ``<version>/py``.
* ``Backend._installed_product_root()``: the real probe runs in the candidate interpreter.
* ``Backend.create()`` (a workspace realm) and ``Backend.validate_candidate()`` (the candidate's own
  SourceProfile, service declaration and schema inspection).

Left out on purpose:
* ``up``/``stop`` and the Pinokio binding (the daemon launch and the stop handoff). No Pinokio path here.
* npm, remotion and the tool downloads: ``install_tools`` (Node, Bun, OMP) is replaced with placeholder
  executables, and ``install_remotion_dependencies`` returns False.
* VibeComfy: the pinned engine is replaced by a one-module local repository, because the default source is
  a network download.
* Windows and macOS. The test carries the ``linux_native`` lane, and the proof was run on Linux only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.installer.backend import Backend
from astrid.installer.core import read_json, write_json
from astrid.installer.service import LABEL
from tests.helpers.runtime import runtime_checkout

pytestmark = [pytest.mark.integration, pytest.mark.linux_native]


def _git(repo: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Installer test",
         "-c", "user.email=installer@example.invalid", *args],
        env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()


def _vibecomfy_stand_in(root: Path) -> tuple[Path, str]:
    """A one-module local repository at a real commit, standing in for the pinned VibeComfy engine."""
    repo = root / "vibecomfy-stand-in"
    (repo / "vibecomfy").mkdir(parents=True)
    (repo / "vibecomfy" / "__init__.py").write_text('"""Local stand-in for the pinned VibeComfy engine."""\n',
                                                    encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools==80.9.0", "wheel==0.46.3"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        '[project]\nname = "vibecomfy"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    _git(repo, "init", "--quiet")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "VibeComfy stand-in")
    return repo, _git(repo, "rev-parse", "HEAD")


def _tool_stand_ins(root: Path) -> dict[str, str]:
    """Placeholders for install_tools' Node, npm and Bun downloads (out of scope for this proof)."""
    node = root / "node" / "bin" / "node"
    npm = root / "node" / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    bun = root / "bun" / "bun"
    for path in (node, npm, bun):
        path.parent.mkdir(parents=True, exist_ok=True)
    for path in (node, bun):
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    npm.write_text("// placeholder npm entry point\n", encoding="utf-8")
    return {
        "PATH": os.defpath,
        "ASTRID_NODE_EXECUTABLE": str(node),
        "ASTRID_BUN_EXECUTABLE": str(bun),
    }


def test_staged_product_root_is_admitted_by_validate_candidate(tmp_path, monkeypatch):
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("uv must be on PATH: Backend.stage needs the pinned uv to build a candidate.")
    checkout = Path(__file__).resolve().parents[1]
    if not (checkout / ".git").exists():
        pytest.fail("This proof stages the pinned agent commit, so the agent checkout must be a Git repository.")
    runtime = runtime_checkout()
    vibecomfy, vibecomfy_sha = _vibecomfy_stand_in(tmp_path)
    uv_version = subprocess.run([uv, "--version"], check=True, capture_output=True, text=True).stdout.split()[1]

    program = tmp_path / "program"
    data = tmp_path / "data"
    program.mkdir()
    write_json(program / "install.json", {"owner": LABEL, "data_root": str(data)})
    manifest = {
        "schema_version": 1,
        "release": 1,
        "agent": {"sha": _git(checkout, "rev-parse", "HEAD"), "repo": str(checkout)},
        "runtime": {"sha": _git(runtime, "rev-parse", "HEAD"), "repo": str(runtime)},
        "vibecomfy": {"sha": vibecomfy_sha, "repo": str(vibecomfy)},
        "tools": {
            "uv": {"version": uv_version},
            "python": {"version": f"{sys.version_info.major}.{sys.version_info.minor}"},
        },
    }

    backend = Backend(program, tmp_path / "install.log", Path(uv), say=lambda _line: None)
    monkeypatch.setattr("astrid.installer.backend.install_tools",
                        lambda _manifest, root, _target, _run: _tool_stand_ins(root))
    monkeypatch.setattr(backend, "install_remotion_dependencies", lambda *_args: False)

    try:
        version = backend.stage(manifest, program, data)
    except subprocess.CalledProcessError as exc:
        pytest.fail("Backend.stage could not build or install the candidate. It needs network or a warm "
                    f"uv cache for the pinned locks and wheels. uv output tail: {str(exc.output)[-1500:]}")

    version_root = version.root.resolve()
    profile = read_json(version.profile)
    product_root = Path(profile["source_checkout"]).resolve()
    purelib = subprocess.run(
        [str(version.python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert product_root.is_relative_to(version_root), (product_root, version_root)
    assert product_root == Path(purelib).resolve(), (product_root, purelib)
    assert product_root == backend._installed_product_root(version.python, version.env)

    realm = backend.create(version, data)
    assert realm["realm_id"] and realm["realm_root"]
    backend.validate_candidate(version, data, realm)
