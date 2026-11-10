"""Every installer mode must work in the dependency-free bootstrap environment."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
import runtime_protocol

ROOT = Path(__file__).resolve().parents[1]


def test_installer_has_no_lazy_third_party_imports():
    # Include imports inside functions: a dependency introduced only in update,
    # doctor or uninstall must not evade the cold-import check below.
    allowed = sys.stdlib_module_names | {"runtime_protocol", "astrid_runtime", "astrid_runtime_client"}
    for source in (ROOT / "astrid/installer").glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            for name in names:
                # The installer may import itself (astrid.installer.*), whose modules this same
                # check covers. Any other astrid module, e.g. astrid.core.pack (which pulls in
                # jsonschema), is a product dependency the bootstrap does not have.
                if name == "astrid.installer" or name.startswith("astrid.installer."):
                    continue
                assert name.split(".")[0] in allowed, (source.name, node.lineno, name)


@pytest.mark.parametrize("flags", [[], ["--update"], ["--doctor"], ["--uninstall"], ["--uninstall", "--delete-data"]])
def test_every_installer_mode_imports_without_third_party_packages(tmp_path, flags):
    script = r'''
import builtins, importlib, json, pkgutil, sys
from pathlib import Path
sys.path[:0] = json.loads(sys.argv[1])
allowed = sys.stdlib_module_names | {"astrid", "astrid_runtime", "astrid_runtime_client", "runtime_protocol"}
original = builtins.__import__
def guarded(name, globals=None, locals=None, fromlist=(), level=0):
    if not level and name.split(".")[0] not in allowed:
        raise ModuleNotFoundError("No module named " + repr(name.split(".")[0]), name=name.split(".")[0])
    return original(name, globals, locals, fromlist, level)
builtins.__import__ = guarded
# A genuine third-party import stays unavailable in this clean bootstrap.
try:
    import requests
except ModuleNotFoundError as error:
    assert error.name == "requests", error
else:
    raise AssertionError("Blocked third-party import unexpectedly succeeded")
import astrid.installer
# The installer imports these Runtime modules in-process; each must stay stdlib-only
# under the guard, because bootstrap installs Runtime with --no-deps.
for name in ("bootstrap", "runtime_boundary", "identity", "paths", "io"):
    importlib.import_module("astrid_runtime." + name)
# Import every installer module, including the backend and every mode's helpers.
for item in pkgutil.iter_modules(astrid.installer.__path__):
    importlib.import_module("astrid.installer." + item.name)
from astrid.installer import cli
from astrid.core import gateway
flags = json.loads(sys.argv[2])
parsed = cli.parser().parse_args(flags)
assert not hasattr(parsed, "repair")
# Exercise the actual gateway/CLI routing and argument parsing for each mode.
# The transaction is the external-operation boundary; literal bootstrap smoke
# separately runs every transaction with real packages and tools.
seen = []
cli.Installer.run = lambda self, mode, *args, **kwargs: seen.append(mode)
cli.Backend.existing = lambda *args: None
cli.validate_manifest = lambda value: value
cli.download = lambda *args: None
root = Path(sys.argv[3]) / "program"
data = Path(sys.argv[3]) / "workspace"
root.mkdir()
manifest = Path(sys.argv[3]) / "manifest.json"
manifest.write_text("{}")
from astrid.installer.core import write_json
from astrid.installer.service import LABEL
write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
code = gateway.main(["install", "--root", str(root), "--data-root", str(data),
                     "--manifest", str(manifest), "--uv", sys.executable, *flags])
expected = "update" if "--update" in flags else "doctor" if "--doctor" in flags else "uninstall" if "--uninstall" in flags else "install"
assert seen == [expected], seen
# The stubbed transaction installs nothing and no Runtime answers, so the run
# receipt must report "not updated" (exit 3) for install and update, never 0.
assert code == (3 if expected in {"install", "update"} else 0), code
'''
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script,
         json.dumps([str(ROOT), str(Path(runtime_protocol.__file__).resolve().parent.parent)]),
         json.dumps(flags), str(tmp_path)],
        env={"HOME": str(tmp_path), "PATH": str(Path(sys.executable).parent)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_install_tools_records_git_without_third_party_packages(tmp_path):
    # Exercise the lazy Git lookup reached during staging, not just cold imports.
    script = r'''
import builtins, json, os, sys
from pathlib import Path
sys.path[:0] = json.loads(sys.argv[1])
allowed = sys.stdlib_module_names | {'astrid', 'astrid_runtime', 'astrid_runtime_client', 'runtime_protocol'}
original = builtins.__import__
def guarded(name, globals=None, locals=None, fromlist=(), level=0):
    if not level and name.split('.')[0] not in allowed:
        raise ModuleNotFoundError('No module named ' + repr(name.split('.')[0]), name=name.split('.')[0])
    return original(name, globals, locals, fromlist, level)
builtins.__import__ = guarded
from astrid.installer import backend, tools
root = Path(sys.argv[2])
git = root / 'git'
git.write_text('fixture')
git.chmod(0o755)
os.environ['PATH'] = str(root)
tools.omp_source = lambda _: 'package'
tools.asset_downloads = lambda _: []
tools.binary = lambda folder, name: folder / name
tools._media_tools = lambda *args: {'ASTRID_FFMPEG_EXECUTABLE': str(root / 'ffmpeg'), 'ASTRID_FFPROBE_EXECUTABLE': str(root / 'ffprobe')}
def omp(_selected, _root, _run, env):
    (Path(env['BUN_INSTALL_BIN']) / 'omp').write_text('fixture')
tools._install_omp_package = omp
def run(argv, **kwargs):
    return {'bun': '1', 'node': 'v2', 'git': 'git version 3'}[Path(argv[0]).name]
manifest = {'tools': {'omp': {}, 'bun': {'version': '1', 'assets': {'linux-test': {'fixture': True}}}, 'node': {'version': '2', 'assets': {'linux-test': {'fixture': True}}}, 'ffmpeg': {}}}
assert backend.install_tools is tools.install_tools
env = tools.install_tools(manifest, root / 'tools', 'linux-test', run)
assert env['ASTRID_GIT_EXECUTABLE'] == str(git)
'''
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script,
         json.dumps([str(ROOT), str(Path(runtime_protocol.__file__).resolve().parent.parent)]),
         str(tmp_path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
