"""Provision the exact VibeComfy pack pin for SDK public-surface tests."""
from __future__ import annotations

import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path


ASTRID = Path(__file__).resolve().parents[2]
_provisioned = False


def use_vibecomfy() -> Path:
    """Install and import VibeComfy from the revision selected by the pack lock.

    The installation goes into ``sys.executable``'s site-packages. It does not
    expose a checkout through ``sys.path`` or rely on ambient ``PYTHONPATH``.
    """
    global _provisioned
    lock = json.loads((ASTRID / "astrid" / "packs" / "vibecomfy" / "pins.json").read_text())
    source = lock["sources"]["vibecomfy"]
    pin = source["revision"]
    repository = source["repository"]

    installed_from_pin = False
    try:
        dist = importlib.metadata.distribution("vibecomfy")
        direct_url = dist.read_text("direct_url.json")
        provenance = json.loads(direct_url) if direct_url else {}
        installed_from_pin = (
            provenance.get("url") == repository
            and provenance.get("vcs_info", {}).get("vcs") == "git"
            and provenance.get("vcs_info", {}).get("commit_id") == pin
        )
    except (importlib.metadata.PackageNotFoundError, json.JSONDecodeError):
        pass

    if not installed_from_pin:
        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "--yes", "vibecomfy"],
            check=False,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [sys.executable, str(ASTRID / "scripts" / "ci_install_vibecomfy.py")],
            cwd=ASTRID,
            check=True,
        )

    dist = importlib.metadata.distribution("vibecomfy")
    direct_url = dist.read_text("direct_url.json")
    provenance = json.loads(direct_url) if direct_url else {}
    vcs_info = provenance.get("vcs_info", {})
    if (
        provenance.get("url") != repository
        or vcs_info.get("vcs") != "git"
        or vcs_info.get("commit_id") != pin
    ):
        raise RuntimeError(f"installed VibeComfy provenance does not match {pin}")

    # ``pinned-env.sh`` deliberately supplies source roots through PYTHONPATH
    # for its separate checkout-import checks. Use isolated mode for the smoke
    # probe so that it proves the installed distribution itself is importable.
    smoke = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import pathlib, vibecomfy.entries; print(pathlib.Path(vibecomfy.entries.__file__).resolve())",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    origin = Path(smoke.stdout.strip()).resolve()
    origin.relative_to(Path(dist.locate_file("")).resolve())
    _provisioned = True
    return origin
