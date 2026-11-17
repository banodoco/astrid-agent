"""Executor-host artifact acquisition.

This module is deliberately independent of Astrid's workspace and runtime
database. A generic executor host may use it for a supported machine profile
by supplying an executor-owned root outside the realm. The host owns only
materialized bytes and their manifest digest; model availability is never
advertised through a workspace journal, sidecar, or model-management command.

Acquisition resumes from an existing ``.part`` file, verifies the signed
manifest's SHA-256 and size, and publishes the verified bytes with an atomic
same-filesystem rename. A partial file is operational scratch, not durable
product state.
"""

from __future__ import annotations

import hashlib
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from runtime_protocol.platform import durable_replace

from astrid.core.model_setup.manifest import (
    DistributionManifest,
    ManifestError,
    validate_artifact_id,
    verify_signature,
)
from astrid.core.model_setup.preflight import require_disk
from astrid.core.util.http import os_trust_ssl_context

CHUNK_SIZE = 1 << 20
ARTIFACTS_DIR_NAME = "artifacts"
TMP_DIR_NAME = "tmp"
PART_SUFFIX = ".part"
STAGED_SUFFIX = ".staged"


class AcquisitionError(RuntimeError):
    """Typed failure for host acquisition or manifest verification."""


class _HashMismatch(AcquisitionError):
    def __init__(self, found: str) -> None:
        super().__init__(f"hash mismatch: found {found}")
        self.found = found


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    """Outcome of one verified host artifact acquisition."""

    artifact_id: str
    path: Path
    sha256: str
    size: int


def artifacts_dir(executor_root: str | Path) -> Path:
    """Return the host-owned final artifact directory."""
    return Path(executor_root) / ARTIFACTS_DIR_NAME


def tmp_dir(executor_root: str | Path) -> Path:
    """Return the host-owned temporary acquisition directory."""
    return Path(executor_root) / TMP_DIR_NAME


def artifact_path(executor_root: str | Path, artifact_id: object) -> Path:
    """Return a safe final path for one manifest artifact."""
    return artifacts_dir(executor_root) / validate_artifact_id(artifact_id)


def part_path(executor_root: str | Path, artifact_id: object) -> Path:
    """Return a safe partial-download path for one manifest artifact."""
    return tmp_dir(executor_root) / f"{validate_artifact_id(artifact_id)}{PART_SUFFIX}"


def staged_path(executor_root: str | Path, artifact_id: object) -> Path:
    """Return a safe staging path for one manifest artifact."""
    return tmp_dir(executor_root) / f"{validate_artifact_id(artifact_id)}{STAGED_SUFFIX}"


def _assert_host_tree_is_owned(root: Path) -> None:
    """Reject links at host storage boundaries before creating or writing."""
    if root.is_symlink():
        raise AcquisitionError(f"executor artifact root must not be a symlink: {root}")
    for label, path in (
        ("artifact directory", root / ARTIFACTS_DIR_NAME),
        ("temporary directory", root / TMP_DIR_NAME),
    ):
        if path.is_symlink():
            raise AcquisitionError(f"executor {label} must not be a symlink: {path}")


def acquire_artifact(
    manifest: DistributionManifest,
    executor_root: str | Path,
    url: str,
    *,
    urlopen: Callable[..., object] | None = None,
) -> AcquisitionResult:
    """Acquire one signed artifact into an executor-host-owned root.

    The root is intentionally not a projects root. It may be shared by the
    host's supported adapters, but it is never opened by Astrid's workspace
    client and carries no journal or availability projection.
    """
    if not verify_signature(manifest):
        raise ManifestError(
            f"manifest signature mismatch for {manifest.artifact_id}; refusing to trust it"
        )
    root = Path(executor_root).expanduser()
    _assert_host_tree_is_owned(root)
    artifact_id = validate_artifact_id(manifest.artifact_id)
    final = artifact_path(root, artifact_id)
    require_disk(root, manifest.size)

    root.mkdir(parents=True, exist_ok=True)
    tmp_dir(root).mkdir(parents=True, exist_ok=True)
    artifacts_dir(root).mkdir(parents=True, exist_ok=True)
    _assert_host_tree_is_owned(root)

    if final.is_symlink():
        raise AcquisitionError(f"executor artifact path must not be a symlink: {final}")
    if final.is_file():
        try:
            digest, size = _verify(final, manifest)
        except _HashMismatch:
            final.unlink()
        else:
            return AcquisitionResult(artifact_id, final, digest, size)

    part = part_path(root, artifact_id)
    staged = staged_path(root, artifact_id)
    for label, path in (("partial", part), ("staged", staged)):
        if path.is_symlink():
            raise AcquisitionError(f"executor {label} path must not be a symlink: {path}")

    # A stale staged file is scratch from a previous failed host attempt.
    if staged.exists():
        staged.unlink()
    offset = part.stat().st_size if part.is_file() else 0
    if offset > manifest.size:
        part.unlink()
        offset = 0
    _download(manifest, url, part, offset, urlopen=urlopen)

    try:
        digest, size = _verify(part, manifest)
    except _HashMismatch as exc:
        raise AcquisitionError(
            f"{artifact_id}: downloaded bytes fail the signed manifest hash "
            f"(found {exc.found}, expected {manifest.sha256})"
        ) from None
    if size != manifest.size:
        raise AcquisitionError(
            f"{artifact_id}: downloaded size {size} does not match manifest size {manifest.size}"
        )

    durable_replace(part, staged)
    durable_replace(staged, final)
    return AcquisitionResult(artifact_id, final, digest, size)


def _download(
    manifest: DistributionManifest,
    url: str,
    part: Path,
    offset: int,
    *,
    urlopen: Callable[..., object] | None,
) -> int:
    """Resume a host scratch file with HTTP Range when possible."""
    part.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url)
    if offset:
        request.add_header("Range", f"bytes={offset}-")
    opened = (
        urllib.request.urlopen(request, context=os_trust_ssl_context())
        if urlopen is None
        else urlopen(request)
    )
    with opened as response:  # type: ignore[attr-defined]
        status = getattr(response, "status", 200)
        if offset and status == 200:
            offset = 0
            part.write_bytes(b"")
        elif offset and status != 206:
            raise AcquisitionError(
                f"{manifest.artifact_id}: server refused Range resume "
                f"(status {status}, offset {offset})"
            )
        with open(part, "ab") as handle:
            while True:
                block = response.read(CHUNK_SIZE)  # type: ignore[attr-defined]
                if not block:
                    break
                handle.write(block)
                handle.flush()
                offset += len(block)
    return offset


def _verify(path: Path, manifest: DistributionManifest) -> tuple[str, int]:
    """Stream the exact SHA-256 and size required by the signed manifest."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
            size += len(block)
    found = digest.hexdigest()
    if found != manifest.sha256:
        raise _HashMismatch(found)
    return found, size


__all__ = [
    "AcquisitionError",
    "AcquisitionResult",
    "acquire_artifact",
    "artifact_path",
    "artifacts_dir",
    "part_path",
    "staged_path",
    "tmp_dir",
]
