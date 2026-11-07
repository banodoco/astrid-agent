"""PK-10 (new): one installed core-only render route.

The release wheel is built with only the four core packs under
``astrid/packs`` (no ``local``, no ``astrid_motion``, no optional packs) and
extracted into a disposable interpreter. Real discovery, the element catalog,
the renderer registry and the ``rendering.render`` facade then render a
timeline whose only clip is the core ``rendering`` pack's ``text-card``
effect. Only the external Node renderer is a deterministic harness (shared
with ``tests/helpers/installed_render.py``).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.helpers.installed_render import build_installed_render

CORE = ("generation", "rendering", "timeline", "vibecomfy")
PROBE = Path(__file__).with_name("core_only_render_probe.py")


@pytest.fixture(scope="module")
def core_only_install(tmp_path_factory):
    yield from build_installed_render(tmp_path_factory.mktemp("core-only-render"), packs=CORE,
                                      clip_type="text-card")


@pytest.mark.parametrize("mode", ["positive", "missing-element", "missing-renderer"])
def test_installed_core_only_render_route(core_only_install, mode):
    root, python, site, environment = core_only_install
    assert sorted(p.name for p in (site / "astrid" / "packs").iterdir() if p.is_dir() and p.name != "__pycache__") \
        == sorted(CORE)
    result = subprocess.run(
        [str(python), "-I", str(PROBE), str(root), str(site), mode],
        cwd=root, env=environment, text=True, capture_output=True, timeout=180,
    )
    (root / f"{mode}.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    proof = json.loads((root / f"{mode}.proof.json").read_text())
    assert proof["isolated"] is True
    assert proof["origin"].startswith(str(site) + os.sep)
    assert proof["selected_backend"] == "rendering.remotion"
    assert set(proof["packs"]) <= set(CORE) | {"hivemind"} and "rendering" in proof["packs"]
    assert proof["loaded"] == []
    assert "text-card" in proof["catalog_ids"]
    assert proof["published"] is (mode == "positive")
