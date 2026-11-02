#!/usr/bin/env python3
"""Refresh Runtime client artifacts from a clean, committed Runtime checkout.

Run ``python scripts/refresh_runtime_client.py --producer ../astrid-runtime``.
The Runtime generator and artifact manifest are checked in a disposable archive
of the producer commit before any consumer files are replaced.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "runtime.lock.json"
_spec = importlib.util.spec_from_file_location("vendor_lock", ROOT / "scripts" / "vendor_lock.py")
assert _spec is not None and _spec.loader is not None
vendor_lock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vendor_lock)


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def refresh(producer: Path) -> str:
    producer = producer.resolve()
    dirty = _run(["git", "-C", str(producer), "status", "--porcelain"])
    if dirty:
        raise SystemExit("Runtime producer checkout must be clean; commit its artifact changes first")
    commit = _run(["git", "-C", str(producer), "rev-parse", "HEAD"])
    if vendor_lock.check(LOCK_PATH):
        raise SystemExit("consumer artifacts are locally edited; restore them before refreshing")

    with tempfile.TemporaryDirectory(prefix="astrid-runtime-refresh-") as temporary:
        work = Path(temporary)
        archive = subprocess.run(
            ["git", "-C", str(producer), "archive", "--format=tar", commit],
            check=True,
            capture_output=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            bundle.extractall(work, filter="data")
        _run([sys.executable, "generators/generate.py"], cwd=work)
        _run([sys.executable, "scripts/artifact_manifest.py", "--check"], cwd=work)

        lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        lock["source_commit"] = commit
        candidate = work / "consumer" / "runtime.lock.json"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        staged = work / "consumer"
        vendor_lock.update(candidate, producer, root=staged)

        for consumer in lock["files"]:
            destination = ROOT / consumer
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staged / consumer, destination)
        shutil.copyfile(candidate, LOCK_PATH)
        packaged_lock = ROOT / "astrid" / "resources" / LOCK_PATH.name
        if packaged_lock.parent.is_dir():
            shutil.copyfile(candidate, packaged_lock)
    return f"refreshed Runtime artifacts from {commit[:12]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--producer", type=Path, required=True, help="clean Runtime producer checkout")
    args = parser.parse_args(argv)
    print(refresh(args.producer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
