"""Build this checkout's release wheel once and expose it in a disposable interpreter.

Shared by the PK-5 installed-CLI and boot-conformance proofs. No installer and no
dependency acquisition: the real wheel is extracted into a fresh venv's
site-packages; already-qualified third-party dependencies are reachable through
one ``.pth`` line (the running interpreter's site-packages, whose own ``.pth``
hooks, including the editable Astrid checkout, are therefore *not* executed).
The pinned Runtime packages are copied from ``ASTRID_RUNTIME_CHECKOUT`` as the
deployment ships them beside the Agent.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import venv
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class InstalledWheel:
    root: Path
    python: Path
    site: Path
    wheel_names: frozenset[str]
    wheel_sha256: str
    env: dict[str, str]

    def run(self, *argv: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
        """Run the installed interpreter isolated (-I) from a neutral directory."""
        return subprocess.run([str(self.python), "-I", *argv], cwd=self.root / "cwd", env=self.env,
                              text=True, capture_output=True, timeout=timeout)


def build_installed_wheel(root: Path) -> InstalledWheel:
    build = root / "build-source"
    build.mkdir()
    for name in ("pyproject.toml", "LICENSE", "NOTICE", "README.md"):
        shutil.copy2(ROOT / name, build / name)
    for name in ("astrid", "astrid_runtime_client"):
        shutil.copytree(ROOT / name, build / name, ignore=shutil.ignore_patterns("__pycache__"))
    env = {name: os.environ[name] for name in ("PATH", "TMPDIR", "LANG") if name in os.environ}
    env.update(HOME=str(root / "home"), PYTHONDONTWRITEBYTECODE="1")
    Path(env["HOME"]).mkdir()
    built = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(root / "wheel")],
        cwd=build, env=env, capture_output=True, text=True, timeout=300,
    )
    (root / "build.log").write_text(built.stdout + built.stderr)
    assert built.returncode == 0, built.stdout[-2000:] + built.stderr[-2000:]
    wheels = list((root / "wheel").glob("*.whl"))
    assert len(wheels) == 1
    digest = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
    shutil.rmtree(build)
    interpreter = root / "interpreter"
    venv.EnvBuilder(with_pip=False).create(interpreter)
    python = interpreter / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    site = Path(subprocess.check_output(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True,
    ).strip())
    with zipfile.ZipFile(wheels[0]) as archive:
        names = frozenset(archive.namelist())
        archive.extractall(site)
    shutil.rmtree(root / "wheel")
    dependencies = Path(__import__("pytest").__file__).resolve().parent.parent
    (site / "qualified-dependencies.pth").write_text(str(dependencies) + "\n")
    runtime = Path(os.environ["ASTRID_RUNTIME_CHECKOUT"])
    for name in ("runtime_protocol", "astrid_runtime"):
        shutil.copytree(runtime / name, site / name, ignore=shutil.ignore_patterns("__pycache__"))
    (root / "cwd").mkdir()
    return InstalledWheel(root=root, python=python, site=site, wheel_names=names, wheel_sha256=digest, env=env)
