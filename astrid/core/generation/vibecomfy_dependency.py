"""Selection and validation of Astrid's supported VibeComfy dependency."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

VIBECOMFY_CHECKOUT_ENV = "ASTRID_VIBECOMFY_CHECKOUT"
VIBECOMFY_ENGINE_REVISION = "ac0ee1b80991f2414dae27d264af4b5e2890a7a6"
VIBECOMFY_FORK_URL = "https://github.com/peteromallet/VibeComfy.git"
VIBECOMFY_CANDIDATE_KIND_ENV = "ASTRID_VIBECOMFY_CANDIDATE_KIND"
VIBECOMFY_CANDIDATE_REVISION_ENV = "ASTRID_VIBECOMFY_CANDIDATE_REVISION"
VIBECOMFY_CANDIDATE_CONTENT_DIGEST_ENV = "ASTRID_VIBECOMFY_CANDIDATE_CONTENT_DIGEST"
# These values are emitted only by a GenericPackHost after validating the
# digest-bound readiness profile.  They let a constrained child reuse
# trusted source identity without spawning ``git`` under the child network
# hook (which intentionally rejects arbitrary native descendants).
VIBECOMFY_ATTESTED_REVISION_ENV = "ASTRID_VIBECOMFY_ATTESTED_REVISION"
VIBECOMFY_ATTESTED_CONTENT_DIGEST_ENV = "ASTRID_VIBECOMFY_ATTESTED_CONTENT_DIGEST"


class VibeComfyDependencyError(ValueError):
    """The configured VibeComfy source cannot satisfy the supported pin."""


def configured_vibecomfy_checkout(environ: Mapping[str, str] | None = None) -> Path | None:
    """Return the explicitly configured, exact VibeComfy source checkout.

    A checkout is opt-in and must be an absolute Git worktree at the exact
    reviewed revision. This keeps an ambient ``PYTHONPATH`` or an arbitrary
    installed package from silently replacing the supported dependency.
    """

    env = os.environ if environ is None else environ
    raw = str(env.get(VIBECOMFY_CHECKOUT_ENV, "")).strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise VibeComfyDependencyError(
            f"{VIBECOMFY_CHECKOUT_ENV} must be an absolute VibeComfy checkout"
        )
    checkout = candidate.resolve()
    if not checkout.is_dir() or not (checkout / "vibecomfy" / "__init__.py").is_file():
        raise VibeComfyDependencyError(
            f"{VIBECOMFY_CHECKOUT_ENV} must name a VibeComfy source checkout"
        )
    candidate_kind = str(env.get(VIBECOMFY_CANDIDATE_KIND_ENV, "")).strip()
    candidate_revision = str(env.get(VIBECOMFY_CANDIDATE_REVISION_ENV, "")).strip()
    candidate_content_digest = str(
        env.get(VIBECOMFY_CANDIDATE_CONTENT_DIGEST_ENV, "")
    ).strip()
    attested_revision = str(env.get(VIBECOMFY_ATTESTED_REVISION_ENV, "")).strip()
    attested_content_digest = str(
        env.get(VIBECOMFY_ATTESTED_CONTENT_DIGEST_ENV, "")
    ).strip()
    if candidate_kind or candidate_revision or candidate_content_digest:
        if candidate_kind != "local_snapshot":
            raise VibeComfyDependencyError(
                f"{VIBECOMFY_CANDIDATE_KIND_ENV} must be 'local_snapshot' when a candidate is selected"
            )
        if not re.fullmatch(r"[0-9a-f]{40}", candidate_revision):
            raise VibeComfyDependencyError(
                f"{VIBECOMFY_CANDIDATE_REVISION_ENV} must be a full lowercase Git revision"
            )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", candidate_content_digest):
            raise VibeComfyDependencyError(
                f"{VIBECOMFY_CANDIDATE_CONTENT_DIGEST_ENV} must be sha256:<64hex>"
            )
        expected_revision = candidate_revision
    else:
        expected_revision = VIBECOMFY_ENGINE_REVISION
    if attested_revision or attested_content_digest:
        if not re.fullmatch(r"[0-9a-f]{40}", attested_revision):
            raise VibeComfyDependencyError(
                f"{VIBECOMFY_ATTESTED_REVISION_ENV} must be a full lowercase Git revision"
            )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", attested_content_digest):
            raise VibeComfyDependencyError(
                f"{VIBECOMFY_ATTESTED_CONTENT_DIGEST_ENV} must be sha256:<64hex>"
            )
        if attested_revision != expected_revision:
            raise VibeComfyDependencyError(
                f"{VIBECOMFY_ATTESTED_REVISION_ENV} does not match the selected VibeComfy revision"
            )
        # The host has already verified the checkout identity and content
        # digest against the signed readiness profile.  Do not repeat that
        # identity probe inside the network-hooked child: spawning git is a
        # native descendant and is correctly denied by the child policy.
        return checkout
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            stdin=subprocess.DEVNULL,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VibeComfyDependencyError(
            f"{VIBECOMFY_CHECKOUT_ENV} could not be identity-checked"
        ) from exc
    revision = result.stdout.strip() if result.returncode == 0 else ""
    if revision != expected_revision:
        raise VibeComfyDependencyError(
            f"{VIBECOMFY_CHECKOUT_ENV} must resolve to VibeComfy revision "
            f"{expected_revision}"
        )
    try:
        clean = subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all"],
            stdin=subprocess.DEVNULL,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VibeComfyDependencyError(
            f"{VIBECOMFY_CHECKOUT_ENV} could not verify clean source content"
        ) from exc
    if clean.returncode != 0 or clean.stdout.strip():
        raise VibeComfyDependencyError(
            f"{VIBECOMFY_CHECKOUT_ENV} must be a clean VibeComfy checkout"
        )
    return checkout


def dependency_pythonpath(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return approved dependency roots for both host and executor children."""

    env = os.environ if environ is None else environ
    values: list[str] = []
    checkout = configured_vibecomfy_checkout(env)
    if checkout is not None:
        values.append(str(checkout))
    for raw in str(env.get("PYTHONPATH", "")).split(os.pathsep):
        if not raw:
            continue
        path = Path(raw)
        if path.name in {"site-packages", "dist-packages"}:
            values.append(str(path))
    return tuple(dict.fromkeys(values))


__all__ = [
    "VIBECOMFY_CHECKOUT_ENV",
    "VIBECOMFY_ENGINE_REVISION",
    "VIBECOMFY_FORK_URL",
    "VIBECOMFY_CANDIDATE_KIND_ENV",
    "VIBECOMFY_CANDIDATE_REVISION_ENV",
    "VIBECOMFY_CANDIDATE_CONTENT_DIGEST_ENV",
    "VIBECOMFY_ATTESTED_REVISION_ENV",
    "VIBECOMFY_ATTESTED_CONTENT_DIGEST_ENV",
    "VibeComfyDependencyError",
    "configured_vibecomfy_checkout",
    "dependency_pythonpath",
]
