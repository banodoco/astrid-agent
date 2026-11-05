"""PK-12 (new): installed packs reach other packs only through public dependency APIs.

The release wheel is built and extracted into a disposable interpreter, and
the production import checker (``astrid.pack_imports.check_pack_imports``,
the loader's contract) runs over every installed pack that is not core. Each
of those packs moves out of Agent (PK-15/PK-16) and is then admitted as an
external pack, so it must already import other packs only through a declared
dependency's ``api`` module, Astrid only through ``astrid.pack_api`` /
``astrid.sdk``, and third-party code only through declared requirements.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.packs.rendering.test_installed_render_composition import build_installed_render

PROBE = Path(__file__).with_name("installed_check_pack_imports_probe.py")
CORE = {"generation", "rendering", "timeline", "vibecomfy", "hivemind"}


@pytest.fixture(scope="module")
def installed_imports(tmp_path_factory):
    for root, python, site, environment in build_installed_render(tmp_path_factory.mktemp("installed-imports")):
        result = subprocess.run([str(python), "-I", str(PROBE), str(root), str(site)], cwd=root,
                                env=environment, text=True, capture_output=True, timeout=300)
        assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
        yield json.loads((root / "imports.json").read_text())


def test_installed_non_core_packs_use_only_public_dependency_apis(installed_imports):
    assert set(installed_imports) - CORE, "no installed non-core pack was checked"
    violations = {pack: rows for pack, rows in installed_imports.items() if pack not in CORE and rows}
    assert violations == {}, json.dumps(violations, indent=1)[:6000]


def test_installed_dependency_api_imports_are_admitted(installed_imports):
    """video_editing reaches editorial only through astrid.packs.editorial.api."""
    rows = installed_imports.get("video_editing", [])
    assert not [row for row in rows if "astrid.packs.editorial" in row[2]]
