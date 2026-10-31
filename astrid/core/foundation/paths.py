"""Canonical root-path constants.

This module is the public surface for PACKAGE_ROOT, REPO_ROOT, and WORKSPACE_ROOT.
"""

from __future__ import annotations

from pathlib import Path

# This module lives at astrid/core/foundation/paths.py; parents[2] is the
# astrid package root (PACKAGE_ROOT).
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PACKAGE_ROOT.parent
WORKSPACE_ROOT = REPO_ROOT.parent


def venv_python(venv_dir: str | Path, *, windows: bool | None = None) -> Path:
    """Return the platform's Python executable inside a virtual environment."""
    import os

    is_windows = os.name == "nt" if windows is None else windows
    return Path(venv_dir) / ("Scripts/python.exe" if is_windows else "bin/python")


def load_checkout_script(name: str):
    """Import ``scripts/<name>.py`` of this checkout by its file, not by the top-level name ``scripts``.

    ``scripts`` is a generic top-level name: a pack whose root is on a worker
    PYTHONPATH (Hivemind ships ``scripts/__init__.py``) shadowed ours, so
    ``import scripts.gen_effect_registry`` failed in that context. Returns None
    when the checkout has no such script (release wheels exclude them).
    """
    import importlib.util
    import sys

    path = REPO_ROOT / "scripts" / f"{name}.py"
    module_name = f"_astrid_checkout_scripts.{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module
