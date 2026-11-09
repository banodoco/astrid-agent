"""Migration regression + grep gate tests for Sprint 01 ecosystem reconciliation.

Ensures no duplicate HTTP/credential code remains in pack run.py files,
and that migrated packs (logo_ideas, fal_foley) can be dry-run without errors.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# (a) No duplicate HTTP helper definitions in packs/
# ---------------------------------------------------------------------------

DUPLICATE_NAMES = [
    "_http_post_json",
    "_http_get_json",
    "_http_get_bytes",
    "poll_fal_result",
    "submit_fal_job",
]


def test_no_duplicate_defs_in_packs() -> None:
    """Assert none of the old HTTP helper names are DEFINED under astrid/packs/.

    Only astrid/core/util/ may define these patterns now.
    """
    packs_dir = Path(__file__).resolve().parents[1] / "astrid/packs"
    violations: list[str] = []

    for py_file in packs_dir.rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except Exception:
            continue

        for name in DUPLICATE_NAMES:
            # Look for 'def name(' in the file — a definition, not just a call/import
            if f"def {name}(" in text:
                violations.append(f"{py_file}: defines {name}")

    assert not violations, (
        f"Duplicate HTTP helper definitions found in packs:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# (b) No urllib.request.urlopen in pack run.py files (except allowlist)
# ---------------------------------------------------------------------------

ALLOWLIST_URLLIB = {
    # Migrated in Sprint 01 — still uses urllib for OpenAI API (not fal)
    "astrid/packs/generation/actions/generate_image_openai/run.py",
    # Pre-existing packs NOT migrated this sprint (grandfathered):
    "astrid/packs/understanding/actions/visual_understand/run.py",
    "astrid/packs/understanding/actions/audio_understand/run.py",
    "astrid/packs/training/actions/search_loras/run.py",
    # seinfeld/script_pipeline was generalized into builtin/script_pipeline (builtin-training epic)
    "astrid/packs/editorial/actions/script_pipeline/run.py",
    # blender.render: downloads mesh assets from remote URLs (urlretrieve) and
    # POSTs render jobs to a user-supplied cloud render host (urlopen)
    "astrid/packs/blender/actions/render/run.py",
    # media.gif_search: GIPHY search API + GIF download through an injected
    # urllib.request.urlopen (legitimate third-party media HTTP)
    "astrid/packs/media/actions/gif_search/run.py",
}


def test_no_urllib_in_pack_run_py() -> None:
    """Assert no 'urllib.request.urlopen' or 'from urllib.request import' in pack
    run.py files, except for the explicit allowlist."""
    repo_root = Path(__file__).resolve().parents[1]
    packs_dir = repo_root / "astrid" / "packs"
    violations: list[str] = []

    for py_file in packs_dir.rglob("run.py"):
        rel = str(py_file.relative_to(repo_root))
        if rel in ALLOWLIST_URLLIB:
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except Exception:
            continue

        if "urllib.request.urlopen" in text:
            violations.append(f"{rel}: contains urllib.request.urlopen")
        if "from urllib.request import" in text:
            violations.append(f"{rel}: imports from urllib.request")

    assert not violations, (
        f"Pack run.py files using raw urllib outside allowlist:\n"
        + "\n".join(violations)
        + f"\n\nAllowlist: {ALLOWLIST_URLLIB}"
    )


# ---------------------------------------------------------------------------
# (c) Dry-run smoke for migrated packs
# ---------------------------------------------------------------------------


