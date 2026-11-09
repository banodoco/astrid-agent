"""Resolve a first-party or installed pack's resource directory."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def astrid_root() -> Path:
    """Return the importlib.resources root for Astrid's core package."""
    return Path(str(files("astrid")))


def checkout_root() -> Path:
    """Return the checkout/container root containing the Astrid package."""
    return astrid_root().parent


def pack_root(pack_id: str) -> Path:
    """Return the filesystem root of an importable pack namespace."""
    if not pack_id or any(part in {".", ".."} for part in pack_id.split(".")):
        raise ValueError("pack id must be a non-empty import path")
    return Path(str(files("astrid.packs").joinpath(pack_id)))
