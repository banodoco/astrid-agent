"""Keep compact lifecycle failure evidence uploadable when bootstrap logs overflow."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys

if __package__:
    from .ci_journey import JOURNEY_EVIDENCE_NAMES
else:
    from ci_journey import JOURNEY_EVIDENCE_NAMES


MAX_ARTIFACT_BYTES = 49_000_000
ENTRY_OVERHEAD = 1024
PHASES = frozenset({"before-cleanup", "after-cleanup"})
BOOTSTRAP_LOG = "release0-bootstrap.log"
METADATA_SUFFIX = ".metadata.json"
SERVICE_LOG = re.compile(r"runtime-service-service-.+\.log\Z")
LIFECYCLE_EVIDENCE_NAMES = frozenset({
    "journey-result.json", "install.log", "uninstall.log", "final-uninstall.log",
    "install-bootstrap.log", "update-bootstrap.log", "release0-bootstrap.log", "start.log",
    "runtime-service.log",
    "manager-state-before-cleanup.json", "manager-state-after-cleanup.json",
    "manager-state-before-cleanup.log", "manager-state-after-cleanup.log",
    "unit-file-before-cleanup.txt", "unit-file-after-cleanup.txt",
})


def _regular(path: Path) -> bool:
    stat = path.lstat()
    return not path.is_symlink() and not (getattr(stat, "st_file_attributes", 0) & 0x400)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _metadata(*, captured_bytes: int, original_sha256: str, retained_bytes: int,
              retained_sha256: str) -> bytes:
    """Use fixed-width byte counts so allocation is independent of their value."""
    return _json_bytes({
        "captured_bytes": f"{captured_bytes:020d}",
        "original_sha256": original_sha256,
        "retained_bytes": f"{retained_bytes:020d}",
        "retained_sha256": retained_sha256,
        "truncated": retained_bytes < captured_bytes,
    })


def _receipt_bytes(receipt: dict, phase: str, *, captured_bytes: int,
                   original_sha256: str, retained_bytes: int, retained_sha256: str) -> bytes:
    """Describe the upload bytes without claiming a clipped file is complete."""
    diagnostic = receipt["failure_diagnostics"][phase]["release0_bootstrap"]
    diagnostic.update(
        status="truncated" if retained_bytes < captured_bytes else "captured",
        sha256=retained_sha256,
        original_sha256=original_sha256,
        captured_bytes=f"{captured_bytes:020d}",
        retained_bytes=f"{retained_bytes:020d}",
        truncated=retained_bytes < captured_bytes,
    )
    return _json_bytes(receipt)


def _bootstrap_candidates(root: Path, files: list[Path]) -> list[dict]:
    candidates = []
    for path in files:
        relative = path.relative_to(root)
        if path.name != BOOTSTRAP_LOG:
            continue
        phase = relative.parts[0]
        receipt_path = root / phase / "journey-result.json"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            diagnostic = receipt["failure_diagnostics"][phase]["release0_bootstrap"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Bootstrap evidence is missing a matching failure receipt entry: {relative}") from exc
        if diagnostic.get("status") != "captured" or diagnostic.get("path") not in {relative.as_posix(), (Path("evidence-upload") / relative).as_posix()}:
            raise ValueError(f"Bootstrap failure receipt does not describe staged evidence: {relative}")
        original_sha256 = _file_sha256(path)
        if diagnostic.get("sha256") != original_sha256:
            raise ValueError(f"Bootstrap failure receipt hash does not match staged evidence: {relative}")
        candidates.append({
            "path": path,
            "relative": relative,
            "receipt_path": receipt_path,
            "receipt": receipt,
            "captured_bytes": path.stat().st_size,
            "original_sha256": original_sha256,
            "metadata_path": path.with_name(path.name + METADATA_SUFFIX),
        })
    return candidates


def _is_lifecycle_evidence(name: str) -> bool:
    return (name in LIFECYCLE_EVIDENCE_NAMES or
            name.startswith("runtime-service-service-bootstrap-"))


def _report_additional_diagnostics(root: Path, files: list[Path]) -> None:
    """Make every non-lifecycle upload member an explicit Actions-console exception."""
    for path in files:
        if not _is_lifecycle_evidence(path.name):
            print("Journey evidence console exception (additional diagnostic; not lifecycle acceptance evidence): "
                  f"{path.relative_to(root).as_posix()}", flush=True)


def _allocate(candidates: list[dict], budget: int) -> list[int]:
    """Allocate retained prefixes deterministically and without exceeding budget."""
    allocations = [0] * len(candidates)
    remaining = list(range(len(candidates)))
    while remaining and budget:
        share = max(1, budget // len(remaining))
        next_remaining = []
        for index in remaining:
            need = candidates[index]["captured_bytes"] - allocations[index]
            grant = min(need, share, budget)
            allocations[index] += grant
            budget -= grant
            if allocations[index] < candidates[index]["captured_bytes"]:
                next_remaining.append(index)
        remaining = next_remaining
    return allocations


def prepare(root: Path, max_bytes: int = MAX_ARTIFACT_BYTES) -> int:
    """Validate staged evidence and retain bounded bootstrap prefixes when required.

    The receipt and all non-bootstrap named evidence are never discarded.  If their
    minimum footprint cannot fit, this refuses before changing the staged tree.
    """
    files = []
    if not root.is_dir() or not _regular(root):
        raise ValueError(f"No staged journey-result.json receipt exists under {root}")
    for directory, dirs, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in dirs:
            path = parent / name
            if not _regular(path) or path.parent != root or name not in PHASES:
                raise ValueError(f"Unexpected staged evidence path: {path}")
        for name in names:
            path = parent / name
            relative = path.relative_to(root)
            if len(relative.parts) != 2 or relative.parts[0] not in PHASES:
                raise ValueError(f"Unexpected staged evidence path: {relative}")
            if name.endswith(METADATA_SUFFIX):
                raise ValueError(f"Unexpected pre-existing boundary metadata: {relative}")
            if name not in JOURNEY_EVIDENCE_NAMES and not SERVICE_LOG.fullmatch(name):
                raise ValueError(f"Unexpected staged evidence filename: {relative}")
            if not _regular(path) or not path.is_file():
                raise ValueError(f"Staged evidence must be a regular file: {relative}")
            files.append(path)
    files.sort(key=lambda path: path.relative_to(root).as_posix())
    if not any(path.name == "journey-result.json" for path in files):
        raise ValueError(f"No staged journey-result.json receipt exists under {root}")

    _report_additional_diagnostics(root, files)

    estimate = sum(path.stat().st_size + ENTRY_OVERHEAD for path in files)
    if estimate < max_bytes:
        return estimate

    candidates = _bootstrap_candidates(root, files)
    if not candidates:
        raise ValueError(
            f"Named journey artifact estimate is {estimate} bytes; no bootstrap evidence can be bounded "
            f"under {max_bytes} bytes")
    candidates.sort(key=lambda item: item["relative"].as_posix())
    candidate_paths = {item["path"] for item in candidates}
    receipt_paths = {item["receipt_path"] for item in candidates}

    # Count the receipts and metadata exactly as they will exist with zero-byte
    # retained prefixes, before mutating anything.  That makes refusal atomic.
    minimum = sum(path.stat().st_size + ENTRY_OVERHEAD for path in files
                  if path not in candidate_paths and path not in receipt_paths)
    for item in candidates:
        receipt_copy = json.loads(json.dumps(item["receipt"]))
        receipt = _receipt_bytes(receipt_copy, item["relative"].parts[0],
                                 captured_bytes=item["captured_bytes"],
                                 original_sha256=item["original_sha256"], retained_bytes=0,
                                 retained_sha256=hashlib.sha256(b"").hexdigest())
        minimum += len(receipt) + ENTRY_OVERHEAD
        metadata = _metadata(captured_bytes=item["captured_bytes"],
                             original_sha256=item["original_sha256"], retained_bytes=0,
                             retained_sha256=hashlib.sha256(b"").hexdigest())
        minimum += len(metadata) + ENTRY_OVERHEAD * 2  # zero-byte log and its metadata
    if minimum >= max_bytes:
        raise ValueError(
            f"Minimum named journey evidence estimate is {minimum} bytes; minimum footprint cannot fit "
            f"below {max_bytes} bytes")

    allocations = _allocate(candidates, max_bytes - 1 - minimum)
    receipt_bytes = {}
    metadata_bytes = {}
    for item, retained_bytes in zip(candidates, allocations):
        with item["path"].open("rb") as stream:
            retained_sha256 = hashlib.sha256(stream.read(retained_bytes)).hexdigest()
        phase = item["relative"].parts[0]
        receipt_copy = json.loads(json.dumps(item["receipt"]))
        receipt_bytes[item["receipt_path"]] = _receipt_bytes(
            receipt_copy, phase, captured_bytes=item["captured_bytes"],
            original_sha256=item["original_sha256"], retained_bytes=retained_bytes,
            retained_sha256=retained_sha256)
        metadata_bytes[item["metadata_path"]] = _metadata(
            captured_bytes=item["captured_bytes"], original_sha256=item["original_sha256"],
            retained_bytes=retained_bytes, retained_sha256=retained_sha256)

    final_estimate = sum(path.stat().st_size + ENTRY_OVERHEAD for path in files
                         if path not in candidate_paths and path not in receipt_paths)
    final_estimate += sum(ENTRY_OVERHEAD + retained_bytes
                          for retained_bytes in allocations)
    final_estimate += sum(len(value) + ENTRY_OVERHEAD for value in receipt_bytes.values())
    final_estimate += sum(len(value) + ENTRY_OVERHEAD for value in metadata_bytes.values())
    if final_estimate >= max_bytes:
        raise AssertionError(f"Boundary allocation exceeded limit: {final_estimate} >= {max_bytes}")

    for item, retained_bytes in zip(candidates, allocations):
        with item["path"].open("r+b") as stream:
            stream.truncate(retained_bytes)
    for path, value in receipt_bytes.items():
        path.write_bytes(value)
    for path, value in metadata_bytes.items():
        path.write_bytes(value)
    return final_estimate


def main() -> int:
    try:
        estimate = prepare(Path(sys.argv[1]))
    except Exception as exc:
        print(f"Journey evidence boundary failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"Validated named lifecycle artifact estimate: {estimate} bytes (< {MAX_ARTIFACT_BYTES}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
