"""Create a bounded, redacted copy of journey evidence for artifact upload."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_TOTAL_BYTES = 1024 * 1024
MAX_FILES = 100


def redactor(runtime_checkout: Path | None = None):
    for path in (ROOT, runtime_checkout):
        if path is not None and str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from astrid.core.gateway import doctor_bundle
    return doctor_bundle


def export(source: Path, destination: Path, runtime_checkout: Path | None = None) -> list[str]:
    doctor = redactor(runtime_checkout)
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Evidence source must be an existing non-symlink directory")
    source = source.resolve(strict=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Evidence export destination must not already exist")
    destination.mkdir(parents=True)
    used = 0
    retained = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in {".json", ".log", ".txt"}:
            continue
        relative = path.relative_to(source)
        if any((source / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts))):
            continue
        if len(retained) >= MAX_FILES:
            break
        size = path.stat().st_size
        # A partial JSON document cannot safely receive structured redaction.
        if path.suffix.lower() == ".json" and size > doctor._MAX_LOG_BYTES:
            continue
        raw = doctor._read_tail(path)
        decoded = raw.decode("utf-8", errors="replace")
        if path.suffix.lower() == ".json":
            try:
                json.loads(decoded)
            except (ValueError, TypeError):
                continue
        text = doctor.redact_log(decoded)
        encoded = text.encode("utf-8")
        if used + len(encoded) > MAX_TOTAL_BYTES:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)
        used += len(encoded)
        retained.append(relative.as_posix())
    if not retained:
        raise ValueError("Evidence export produced no files")
    return retained


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--runtime-checkout", required=True, type=Path)
    args = parser.parse_args()
    for path in export(args.source, args.destination, args.runtime_checkout):
        print(path)


if __name__ == "__main__":
    main()
