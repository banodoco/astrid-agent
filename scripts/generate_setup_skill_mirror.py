#!/usr/bin/env python3
"""Render the public setup skill mirror from the packaged canonical skill."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "astrid/packs/setup/docs/SKILL.md"
MIRROR = ROOT / "docs/setup/SKILL.md"
HEADER = (b"<!-- Generated from astrid/packs/setup/docs/SKILL.md; "
          b"regenerate with python scripts/generate_setup_skill_mirror.py. -->\n\n")


def render(source: bytes) -> bytes:
    if not source.startswith(b"---\n"):
        raise ValueError("setup skill must start with YAML frontmatter")
    end = source.find(b"\n---\n", 4)
    if end < 0:
        raise ValueError("setup skill frontmatter has no closing delimiter")
    end += len(b"\n---\n")
    return source[:end] + HEADER + source[end:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail if the mirror has drifted")
    mode.add_argument("--stdout", action="store_true", help="write the generated mirror to stdout")
    args = parser.parse_args()
    generated = render(SOURCE.read_bytes())
    if args.stdout:
        sys.stdout.buffer.write(generated)
    elif args.check:
        if not MIRROR.is_file() or MIRROR.read_bytes() != generated:
            print(f"{MIRROR} is out of date; run python scripts/generate_setup_skill_mirror.py", file=sys.stderr)
            return 1
    else:
        MIRROR.write_bytes(generated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
