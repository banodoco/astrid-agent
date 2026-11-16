#!/usr/bin/env python3
"""Fail if a receipt directory's SHA256SUMS lists a file the upload would not carry.

actions/upload-artifact@v4.4+ skips hidden files and directories (any path component starting with ".")
unless include-hidden-files is true. A file named in the manifest but absent from the directory, or hidden
while the upload skips hidden files, would be missing from every archive. Run after the manifest is
written and before the upload, with --include-hidden-files matching the upload step's setting.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


def check(directory: Path, include_hidden_files: bool, manifest_name: str = "SHA256SUMS") -> list[str]:
    manifest = directory / manifest_name
    if not manifest.is_file():
        return [f"{manifest} is missing"]
    problems = []
    for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        digest, separator, name = line.partition("  ")
        if not separator or not name:
            problems.append(f"{manifest_name}:{number}: malformed line")
            continue
        path = directory / name
        if not path.is_file():
            problems.append(f"{name}: listed in {manifest_name} but not in the directory")
            continue
        if not include_hidden_files and any(part.startswith(".") for part in Path(name).parts):
            problems.append(f"{name}: hidden file listed in {manifest_name}; the upload skips it without include-hidden-files")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            problems.append(f"{name}: sha256 differs from {manifest_name}")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--include-hidden-files", action="store_true",
                        help="the upload step sets include-hidden-files: true")
    args = parser.parse_args(argv)
    problems = check(args.directory, args.include_hidden_files)
    for problem in problems:
        print(f"::error::{problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
