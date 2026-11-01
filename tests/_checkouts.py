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


def _git_checkout_from_package(package_names: tuple[str, ...], env_var: str, label: str) -> Path:
    configured = os.environ.get(env_var)
    if configured:
        checkout = Path(configured).expanduser().resolve()
        package_roots = (checkout,)
    else:
        package_roots = tuple(
            Path(spec.origin).resolve().parent
            for package_name in package_names
            if (spec := importlib.util.find_spec(package_name)) is not None and spec.origin
        )
        assert package_roots, (
            f"Cannot derive the {label} checkout from an importable package; set {env_var}."
        )

    for package_root in package_roots:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=package_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip()).resolve()

    raise AssertionError(
        f"The derived {label} checkout is not a Git checkout; set {env_var} to a Git checkout."
    )


def agent_checkout() -> Path:
    return _git_checkout_from_package(("astrid",), "ASTRID_AGENT_CHECKOUT", "Agent")


def vibecomfy_checkout() -> Path:
    return _checkout_from_package(("vibecomfy",), "ASTRID_VIBECOMFY_CHECKOUT", "VibeComfy")


def runtime_checkout(*, require_git: bool = False) -> Path:
    if require_git:
        return _git_checkout_from_package(
            ("runtime_protocol", "astrid_runtime"), "ASTRID_RUNTIME_CHECKOUT", "runtime"
        )

    root = _checkout_from_package(
        ("runtime_protocol", "astrid_runtime"), "ASTRID_RUNTIME_CHECKOUT", "runtime"
    )
    return root


def runpod_checkout() -> Path:
    return _git_checkout_from_package(("runpod",), "ASTRID_RUNPOD_CHECKOUT", "RunPod")


def runpod_lifecycle_checkout() -> Path:
    return _git_checkout_from_package(
        ("runpod_lifecycle",), "ASTRID_RUNPOD_LIFECYCLE_CHECKOUT", "RunPod Lifecycle"
    )


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
