#!/usr/bin/env python3
"""Preinstall only Astrid's setup skill as a marker-owned harness copy."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import urllib.request

SKILL_URL = "https://raw.githubusercontent.com/banodoco/astrid-agent/main/astrid/packs/setup/docs/SKILL.md"
MARKER = ".astrid-managed-skill.json"
HARNESS = {
    "claude": lambda: Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser(),
    "codex": lambda: Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser(),
    "hermes": lambda: Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser(),
}


def directory_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"link\0" + str(path.readlink()).encode("utf-8"))
        elif path.is_file():
            digest.update(b"file\0" + path.read_bytes())
        else:
            digest.update(b"dir\0")
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> int:
    choices = ", ".join(HARNESS)
    harness = input(f"Install Astrid setup skill for which harness ({choices})? ").strip().lower()
    if harness not in HARNESS:
        print("No supported harness selected; nothing changed.", file=sys.stderr)
        return 2
    home = Path.home()
    if harness == "claude":
        durable = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")) / "astrid-preinstall/skills/astrid-setup"
    elif harness == "codex":
        durable = Path(os.environ.get("LOCALAPPDATA", home / ".local/share")) / "Astrid/preinstall/skills/astrid-setup"
    else:
        durable = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")) / "astrid-preinstall/skills/astrid-setup"
    source_override = os.environ.get("ASTRID_SETUP_SKILL_SOURCE")
    if source_override:
        skill_bytes = Path(source_override).read_bytes()
    else:
        with urllib.request.urlopen(SKILL_URL, timeout=30) as response:
            skill_bytes = response.read()
    if not skill_bytes.startswith(b"---\n"):
        print("Downloaded setup skill did not have expected Markdown frontmatter; nothing changed.", file=sys.stderr)
        return 2

    durable.parent.mkdir(parents=True, exist_ok=True)
    if durable.exists():
        print(f"Preserved existing source folder: {durable}", file=sys.stderr)
        return 2
    durable.mkdir()
    (durable / "SKILL.md").write_bytes(skill_bytes)
    target = HARNESS[harness]() / "skills" / "astrid-setup"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        shutil.rmtree(durable)
        print(f"Preserved existing harness entry: {target}", file=sys.stderr)
        return 2
    shutil.copytree(durable, target)
    (target / MARKER).write_text(json.dumps({
        "source": str(durable.resolve()),
        "digest": directory_digest(target),
        "mechanism": "copy",
    }, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Installed Astrid setup skill for {harness}: {target}")
    print("This installs the skill only; Astrid itself is not installed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
