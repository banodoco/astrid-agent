#!/usr/bin/env python3
"""Print or bump the VibeComfy revision Astrid is pinned to.

The pin lives in the engine pin manifests. Update their references and the
recipe index together so every shipped platform selects the same source.

    python scripts/vibecomfy_pin.py            # print the pinned revision
    python scripts/vibecomfy_pin.py <sha>      # bump it (full 40-hex commit)

After a bump, run the VibeComfy-related tests against a checkout at the new
revision and set the same sha in the hosted ``install-manifest.json`` (or drop
its ``vibecomfy`` entry so the installer uses this pin).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "astrid"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read() -> str:
    return _load(ROOT / "packs/vibecomfy/pins.json")["sources"]["vibecomfy"]["revision"]


def bump(revision: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise SystemExit("expected a full 40-character lowercase commit sha")
    for path in (ROOT / "packs").glob("*/pins.json"):
        data = _load(path)
        changed = False
        for name, source in data.get("sources", {}).items():
            if name in {"vibecomfy", "vibecomfy-h3"}:
                source["revision"] = revision
                changed = True
        if changed:
            _write(path, data)
    for path in (ROOT / "packs").glob("*/*.json"):
        data = _load(path)
        ref = data.get("pins") if isinstance(data, dict) else None
        if isinstance(ref, dict) and set(ref) == {"path", "sha256"}:
            ref["sha256"] = _digest(ROOT / ref["path"])
            _write(path, data)
    path = ROOT / "packs/engine-index.json"
    index = _load(path)
    for entry in index["entries"]:
        entry["recipe"]["sha256"] = _digest(ROOT / entry["recipe"]["path"])
    _write(path, index)


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        raise SystemExit(__doc__)
    if argv:
        bump(argv[0])
    print(read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
