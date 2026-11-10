#!/usr/bin/env python3
"""Print the VibeComfy revision Astrid is pinned to.

The pin authority is the packaged VibeComfy engine recipe. The dependency
adapter, CI and this command all read its digest-verified source pin.

    python scripts/vibecomfy_pin.py            # print the pinned revision

A bump rewrites the ``vibecomfy`` revision in every
package-owned pin manifest, then the dependent ``{"path", "sha256"}`` pin
digests and the recipe digests in the generation.engine contributions, and finally prints the revision
read back through the digest-verified recipe authority. It does not update the
source node lock, requirements locks or test literals that name the old
revision; coordinate those by hand. Run the VibeComfy-related tests against a
checkout at the new revision afterwards and set the same sha in the hosted
``install-manifest.json`` (or drop its ``vibecomfy`` entry so the installer
uses this pin).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

if __package__ in {None, ""}:
    # Keep the documented checkout command usable without an editable install.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrid.core.generation.engine_recipe import engine_source_revision

ROOT = Path(__file__).resolve().parents[1] / "astrid"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read() -> str:
    return engine_source_revision("comfyui", "windows-x86_64-cu130", "vibecomfy")


def bump(revision: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise SystemExit("expected a full 40-character lowercase commit sha")
    for path in (ROOT / "packs").glob("*/pins.json"):
        data = _load(path)
        changed = False
        for name, source in data.get("sources", {}).items():
            if name == "vibecomfy":
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
    for manifest in (ROOT / "packs").glob("*/extensions/generation.engine/*.yaml"):
        text = manifest.read_text(encoding="utf-8")
        for recipe in set(re.findall(r"^\s+path: (packs/\S+)$", text, re.M)):
            text = re.sub(rf"(path: {re.escape(recipe)}\n\s+sha256: )\"?[0-9a-f]+\"?",
                          lambda m: f'{m.group(1)}"{_digest(ROOT / recipe)}"', text)
        manifest.write_text(text, encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        raise SystemExit(__doc__)
    if argv:
        bump(argv[0])
    print(read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
