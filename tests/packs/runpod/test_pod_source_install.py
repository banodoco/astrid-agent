"""The pod's real editable install, source-origin fence and launcher/reset path."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import venv

import pytest

from astrid.packs.runpod.pod_bootstrap import SCRIPT
from tests.helpers.runtime import runtime_checkout


def _function(name):
    start = SCRIPT.index(name + "() {")
    end = SCRIPT.index("\n}\n", start) + 3
    return SCRIPT[start:end]


def _staged_function():
    start = SCRIPT.index("staged_uv_install() (")
    end = SCRIPT.index("\n)\n", start) + 3
    return SCRIPT[start:end]


@pytest.fixture
def pod_install(tmp_path):
    root = Path(__file__).resolve().parents[3]
    src = tmp_path / "src"
    src.mkdir()
    for name, original in (("astrid-agent", root), ("astrid-runtime", runtime_checkout())):
        subprocess.run(["git", "clone", "--quiet", "--shared", str(original), str(src / name)], check=True)
    heads = {}
    for name in ("astrid-agent", "astrid-runtime", "runpod-lifecycle", "VibeComfy"):
        folder = src / name
        folder.mkdir(exist_ok=True)
        if not (folder / ".git").exists():
            subprocess.run(["git", "init", "-q", str(folder)], check=True)
            (folder / "fixture.txt").write_text("provider/model dependency not exercised\n")
            subprocess.run(["git", "-C", str(folder), "add", "."], check=True)
            subprocess.run(["git", "-C", str(folder), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
        heads[name] = subprocess.check_output(["git", "-C", str(folder), "rev-parse", "HEAD"], text=True).strip()
    (src / ".pod-source.json").write_text(json.dumps({"heads": heads, "runtime_lock_sha256": hashlib.sha256((src / "astrid-agent/runtime.lock.json").read_bytes()).hexdigest()}))
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(environment)
    python = environment / "bin/python"
    site = next((environment / "lib").glob("python*/site-packages"))
    # Borrow only dependency directories, without executing another venv's .pth hooks.
    dependencies = Path(sys.prefix).resolve() / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    # Do not put the parent site-packages on sys.path: its noneditable Runtime
    # and vendored client would outrank this venv's editable package finders.
    for dependency in dependencies.iterdir():
        if (dependency.name.startswith(("astrid", "runtime_protocol", "__editable__"))
                or dependency.suffix == ".pth" or dependency.name == "__pycache__"):
            continue
        (site / dependency.name).symlink_to(dependency, target_is_directory=dependency.is_dir())
    dependency_paths = []
    for name in ("vibecomfy", "runpod_lifecycle"):
        spec = importlib.util.find_spec(name)
        if spec and spec.origin:
            dependency_root = Path(spec.origin).resolve().parent.parent
            if dependency_root != dependencies:
                dependency_paths.append(str(dependency_root))
    (site / "test-dependencies.pth").write_text("\n".join(dependency_paths) + "\n")
    uv = shutil.which("uv")
    assert uv
    tools = tmp_path / "tools"
    tools.mkdir()
    # Dependencies are already provisioned by the test lane; exercise the real
    # build/install without resolving or fetching them a second time.
    (tools / "uv").write_text(f'#!/bin/sh\nexec "{uv}" "$@" --no-deps --no-build-isolation\n')
    (tools / "uv").chmod(0o755)
    data = tmp_path / "data"
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    manifest = tmp_path / "source-manifest.json"
    env = {"HOME": str(tmp_path), "PATH": f"{tools}:{environment / 'bin'}:{Path(uv).parent}:{os.defpath}", "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
           "ASTRID_RUNTIME_DATA_ROOT": str(data), "ASTRID_RUNTIME_SOURCE_MANIFEST": str(manifest),
           "ASTRID_RUNTIME_DOOR_PORT": "0", "ASTRID_VIBECOMFY_CHECKOUT": dependency_paths[0]}
    prefix = f'set -euo pipefail\nVENV="{environment}"\nBOOTSTRAP_ROOT="{bootstrap}"\nDATA_ROOT="{data}"\nREALM_ROOT="{tmp_path}/realm"\nSOURCE_MANIFEST="{manifest}"\n'
    return tmp_path, src, python, env, prefix, data, manifest, heads


def _shell(case, body):
    root, _, _, env, prefix, *_ = case
    return subprocess.run(["bash", "-c", prefix + body], cwd=root, env=env,
                          capture_output=True, text=True, timeout=180)


def test_pod_source_install_keeps_verified_git_identity_and_resets_with_real_launcher(pod_install):
    root, src, python, env, _, data, manifest, heads = pod_install
    installed = _shell(pod_install, _staged_function() + "staged_uv_install fresh src/astrid-runtime src/astrid-agent\n")
    assert installed.returncode == 0, installed.stderr
    if "source_install_current()" in SCRIPT:
        checked = _shell(pod_install, _function("source_install_current") + "source_install_current\n")
        assert checked.returncode == 0, checked.stderr
    # Disposable build copies have gone; editable origins must still be the retained originals.
    assert not list((root / "bootstrap").glob(".install.*"))
    setup = _shell(pod_install, _function("setup_runtime") + "setup_runtime\n")
    assert setup.returncode == 0, setup.stderr
    profile = json.loads(manifest.read_text())
    assert profile["source_checkout"] == str(src / "astrid-agent")
    assert profile["runtime_checkout"] == str(src / "astrid-runtime")
    program = '''import json,sys
from pathlib import Path
from astrid.packs.runpod import pod_reset
from astrid.sdk.host_bootstrap import _execution_profile, _capability_matrix_path
root=Path(sys.argv[1]);data=Path(sys.argv[2]);source=Path(sys.argv[3])
assert _capability_matrix_path(source).is_file()
paths=dict(data_root=data,template=root/'template',lock=root/'reset.lock',prepared=root/'prepared.json',ready=root/'ready.json',active=root/'active.json',runs_root=root/'runs')
try:
    assert 'template' in pod_reset.reset(**paths)
    assert _execution_profile()=='source'
    assert pod_reset.reset(**paths)['replace']['state']=='complete'
    client=pod_reset.open_prepared(paths['prepared'])
    assert client.projects.list().ok
    client.__exit__(None,None,None)
    discovery=json.loads((data/'runtime/discovery.json').read_text())
    identity=discovery['identity']
    assert identity['managed_by']=='source' and identity['dirty'] is False
    assert identity['agent_head']==sys.argv[4] and identity['runtime_head']==sys.argv[5]
except Exception as exc:
    reason=getattr(exc,'reason','unknown')
    if reason not in pod_reset.RESET_FAILURE_REASONS: reason='unknown'
    cause=exc.__cause__
    code=getattr(cause,'code','unknown')
    if not isinstance(code,str) or not code.replace('_','').isalnum(): code='unknown'
    raise SystemExit('pod reset/source identity failed: '+type(exc).__name__+' stage='+reason+' cause='+type(cause).__name__+' code='+code)
'''
    try:
        reset = subprocess.run([str(python), "-I", "-B", "-c", program, str(root), str(data), str(src / "astrid-agent"),
                                heads["astrid-agent"], heads["astrid-runtime"]], cwd=root, env=env,
                               capture_output=True, text=True, timeout=180)
        assert reset.returncode == 0, reset.stderr + reset.stdout
    finally:
        stopped = subprocess.run([str(python), "-I", "-B", "-m", "astrid_runtime", "down", "--data-root", str(data),
                                  "--source-manifest", str(manifest), "--json"], cwd=root, env=env,
                                 capture_output=True, text=True, timeout=90)
        assert stopped.returncode == 0, stopped.stderr


def test_pod_source_install_rejects_noneditable_or_foreign_origins(pod_install):
    # Before install, neither package can resolve to this pod's retained checkouts.
    checked = _shell(pod_install, _function("source_install_current") + "source_install_current\n")
    assert checked.returncode != 0
