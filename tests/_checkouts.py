"""Resolve source checkouts from their importable packages for source tests."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess


def _checkout_from_package(package_names: tuple[str, ...], env_var: str, label: str) -> Path:
    configured = os.environ.get(env_var)
    if configured:
        return Path(configured).expanduser().resolve()

    for package_name in package_names:
        spec = importlib.util.find_spec(package_name)
        if spec is not None and spec.origin:
            return Path(spec.origin).resolve().parents[1]
    raise AssertionError(
        f"Cannot derive the {label} checkout from an importable package; set {env_var}."
    )


def vibecomfy_checkout() -> Path:
    return _checkout_from_package(("vibecomfy",), "ASTRID_VIBECOMFY_CHECKOUT", "VibeComfy")


def runtime_checkout(*, require_git: bool = False) -> Path:
    root = _checkout_from_package(
        ("runtime_protocol", "astrid_runtime"), "ASTRID_RUNTIME_CHECKOUT", "runtime"
    )
    if not require_git:
        return root

    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, text=True
    )
    assert result.returncode == 0, (
        "The derived runtime checkout is not a Git checkout; set ASTRID_RUNTIME_CHECKOUT "
        "to a Git checkout."
    )
    return Path(result.stdout.strip()).resolve()


def vibecomfy_git_root() -> Path:
    checkout = vibecomfy_checkout()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=checkout, capture_output=True, text=True
    )
    assert result.returncode == 0, (
        "The derived VibeComfy checkout is not a Git checkout; set ASTRID_VIBECOMFY_CHECKOUT "
        "to a Git checkout."
    )
    return Path(result.stdout.strip()).resolve()


def clip_input_path(env_var: str) -> Path:
    configured = os.environ.get(env_var)
    assert configured, f"Set {env_var} to the required clip test input."
    return Path(configured).expanduser().resolve()
