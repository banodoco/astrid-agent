"""Pure argv for the recipe-owned Linux uv/managed-Python bootstrap.

This is the ONE construction shared by ``LinuxPlatformAdapter`` (runner
environment step) and the verified release pre-CLI interpreter handoff in
``engine_release``. It is stdlib-only and effect-free so the release loader can
run this exact verified retained file before any product import. Callers own
execution, deadlines and process custody; this module never runs anything.
"""
from __future__ import annotations

import glob
import re

PYPI_INDEX = "https://pypi.org/simple"
_PIN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def _root(root):
    # Same lexical Linux rules as engine_runner._root; no filesystem access.
    if (not isinstance(root, str) or not root or any(ord(c) < 32 for c in root)
            or not root.startswith("/") or root.startswith("//") or "\\" in root or ":" in root
            or root == "/" or root.endswith("/")
            or any(p in {"", ".", ".."} for p in root[1:].split("/"))):
        raise ValueError("install root must be an absolute normalized Linux path")
    return root


def _relative(path):
    if (not isinstance(path, str) or not path or path.startswith("/") or "\\" in path
            or ":" in path or any(ord(c) < 32 for c in path)
            or any(p in {"", ".", ".."} for p in path.split("/"))):
        raise ValueError("unsafe environment relative path")
    return path


def _pin(value, what):
    if not isinstance(value, str) or not _PIN.fullmatch(value):
        raise ValueError(what + " must be an exact major.minor.patch pin")
    return value


def linux_bootstrap_paths(*, install_root, venv_relative_path):
    """Target paths for the bootstrap prefix, pinned uv, venv and its Python."""
    root = _root(install_root)
    venv = root + "/" + _relative(venv_relative_path)
    return {"bootstrap": root + "/runtime/bootstrap", "uv": root + "/runtime/bootstrap/bin/uv",
            "venv": venv, "python": venv + "/bin/python"}


def linux_bootstrap_environment(*, install_root):
    """Public environment overlay used for every recipe command at this root."""
    return {"PYTHONDONTWRITEBYTECODE": "1", "UV_CACHE_DIR": _root(install_root) + "/runtime/uv-cache"}


def linux_bootstrap_pip_environment():
    """Extra public overlay for the system ``pip --prefix`` command ONLY.

    Debian/Ubuntu system interpreters otherwise select their ``posix_local``
    install scheme, so ``pip --prefix P`` writes scripts to ``P/local/bin``
    and the recipe's ``P/bin/uv`` never exists. ``deb`` selects the
    ``{base}/bin`` layout there; other interpreters ignore the variable.
    Apply it to ``linux_bootstrap_commands(...)[0]`` and nothing else.
    """
    return {"DEB_PYTHON_INSTALL_LAYOUT": "deb"}


def linux_bootstrap_commands(*, install_root, uv_version, python_version, venv_relative_path):
    """Exact uv pin via system ``python3 -m pip``, then the managed-Python venv."""
    paths = linux_bootstrap_paths(install_root=install_root, venv_relative_path=venv_relative_path)
    uv_version = _pin(uv_version, "uv version")
    python_version = _pin(python_version, "Python version")
    return (("python3", "-m", "pip", "--isolated", "install", "--index-url", PYPI_INDEX,
             "--ignore-installed", "--prefix", paths["bootstrap"], "uv==" + uv_version),
            (paths["uv"], "--no-config", "venv", "--allow-existing", "--python", python_version,
             "--managed-python", paths["venv"]))


SYSTEM_DRIVER_DIRS = ("/usr/local/nvidia/lib", "/usr/local/nvidia/lib64")


def engine_child_env(venv_prefix, env=None):
    """Child environment owned by the engine, for EVERY engine subprocess.

    ``LD_LIBRARY_PATH`` is rebuilt from scratch and the caller's value is never
    read: a system CUDA toolkit (``/usr/local/cuda*``, compat dirs included)
    would otherwise shadow the wheels' own CUDA libraries. Order: the venv's
    ``nvidia/*/lib`` directories (existing ones only), then the system driver
    directories. Install helpers, readiness probes and the engine launch all
    use this, so the launch fingerprint depends on the engine alone.
    """
    out = {k: v for k, v in dict(env or {}).items() if k != "LD_LIBRARY_PATH"}
    venv = _root(venv_prefix)
    pattern = glob.escape(venv) + "/lib/python*/site-packages/nvidia/*/lib"
    venv_dirs = sorted(d for d in glob.glob(pattern) if not d.startswith("/usr/local/cuda"))
    out["LD_LIBRARY_PATH"] = ":".join((*venv_dirs, *SYSTEM_DRIVER_DIRS))
    return out
