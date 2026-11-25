"""Committed generated skill copies must match their sources.

Each entry names a generator script whose ``--check`` mode exits non-zero when
the committed copy has drifted from its source. Edit the source, then run the
generator without ``--check`` to refresh the copy.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]

GENERATED_SKILL_MIRRORS = [
    pytest.param(
        "scripts/generate_setup_skill_mirror.py",
        "docs/setup/SKILL.md",
        "astrid/packs/setup/docs/SKILL.md",
        id="setup",
    ),
]


@pytest.mark.parametrize(("generator", "generated", "source"), GENERATED_SKILL_MIRRORS)
def test_generated_copy_matches_generator(generator: str, generated: str, source: str) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / generator), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"{generated} has drifted from {source}: {result.stderr.strip()}"
    )


@pytest.mark.parametrize(("generator", "generated", "source"), GENERATED_SKILL_MIRRORS)
def test_generated_copy_carries_do_not_edit_header(generator: str, generated: str, source: str) -> None:
    text = (ROOT / generated).read_text(encoding="utf-8")
    header = (
        f"<!-- GENERATED from {source} by {generator}. "
        "Do not edit: edit the source and regenerate. -->"
    )
    lines = text.splitlines()
    assert lines[0] == "---", f"{generated} must keep its frontmatter"
    end = lines.index("---", 1)
    assert lines[end + 1] == header, f"{generated}: header line after frontmatter is wrong"
