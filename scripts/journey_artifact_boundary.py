"""Validate and bound the staged lifecycle artifact before its hosted upload."""
from __future__ import annotations

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
STREAM = re.compile(r"runtime-service-service-seed-(\d+)-(stdout|stderr)\.log\Z")


def _regular(path: Path) -> bool:
    stat = path.lstat()
    return not path.is_symlink() and not (getattr(stat, "st_file_attributes", 0) & 0x400)


def prepare(root: Path, max_bytes: int = MAX_ARTIFACT_BYTES) -> int:
    """Preserve all names, reducing only seed stream bytes if the aggregate requires it."""
    files = []
    if not root.is_dir() or not _regular(root):
        raise ValueError(f"No staged journey-result.json receipt exists under {root}")
    for directory, dirs, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in dirs:
            path = parent / name
            if not _regular(path) or path.parent != root or name not in {"before-cleanup", "after-cleanup"}:
                raise ValueError(f"Unexpected staged evidence path: {path}")
        for name in names:
            path = parent / name
            relative = path.relative_to(root)
            if len(relative.parts) != 2 or relative.parts[0] not in {"before-cleanup", "after-cleanup"}:
                raise ValueError(f"Unexpected staged evidence path: {relative}")
            if name not in JOURNEY_EVIDENCE_NAMES and not re.fullmatch(r"runtime-service-service-.+\.log", name):
                raise ValueError(f"Unexpected staged evidence filename: {relative}")
            if not _regular(path) or not path.is_file():
                raise ValueError(f"Staged evidence must be a regular file: {relative}")
            files.append(path)
    files.sort(key=lambda path: path.relative_to(root).as_posix())
    if not any(path.name == "journey-result.json" for path in files):
        raise ValueError(f"No staged journey-result.json receipt exists under {root}")

    streams = []
    for path in files:
        match = STREAM.fullmatch(path.name)
        if match:
            exit_path = path.with_name(f"runtime-service-service-seed-{match[1]}-exit.log")
            status = json.loads(exit_path.read_text(encoding="utf-8"))
            original = status["streams"][match[2]]["total_bytes"]
            captured = path.stat().st_size
            if not isinstance(original, int) or original < captured:
                raise ValueError(f"Invalid seed stream size metadata: {path}")
            meta = path.with_name(path.name + ".metadata.log")
            if meta.exists():
                raise ValueError(f"Unexpected pre-existing boundary metadata: {meta}")
            streams.append((path, meta, original, captured))

    def metadata(original, captured, retained):
        # Fixed-width decimal strings keep the size calculation independent of allocation.
        return (json.dumps({"original_bytes": f"{original:020d}",
                            "captured_bytes": f"{captured:020d}",
                            "retained_bytes": f"{retained:020d}",
                            "truncated": "1" if retained < original else "0"},
                           sort_keys=True) + "\n").encode()

    stream_paths = {item[0] for item in streams}
    minimum = sum(path.stat().st_size + ENTRY_OVERHEAD for path in files if path not in stream_paths)
    minimum += sum(ENTRY_OVERHEAD * 2 + len(metadata(original, captured, 0))
                   for _, _, original, captured in streams)
    if minimum >= max_bytes:
        raise ValueError(f"Minimum named journey evidence estimate is {minimum} bytes; limit is below {max_bytes} bytes")
    budget = max_bytes - 1 - minimum
    # Equal share, redistributing unused capacity in sorted path order.
    allocations = [0] * len(streams)
    remaining = list(range(len(streams)))
    while remaining and budget:
        share = max(1, budget // len(remaining))
        next_remaining = []
        for i in remaining:
            need = streams[i][3] - allocations[i]
            grant = min(need, share, budget)
            allocations[i] += grant
            budget -= grant
            if allocations[i] < streams[i][3]:
                next_remaining.append(i)
        remaining = next_remaining
    for (path, meta, original, captured), retained in zip(streams, allocations):
        if retained < captured:
            with path.open("r+b") as output:
                output.truncate(retained)
        meta.write_bytes(metadata(original, captured, retained))
    estimate = sum(path.stat().st_size + ENTRY_OVERHEAD for path in files)
    estimate += sum(meta.stat().st_size + ENTRY_OVERHEAD for _, meta, _, _ in streams)
    if estimate >= max_bytes:
        raise ValueError(f"Named journey artifact estimate is {estimate} bytes; limit is below {max_bytes} bytes")
    return estimate


def main():
    try:
        estimate = prepare(Path(sys.argv[1]))
    except Exception as exc:
        print(f"Journey evidence boundary failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"Validated named lifecycle artifact estimate: {estimate} bytes (< {MAX_ARTIFACT_BYTES}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
