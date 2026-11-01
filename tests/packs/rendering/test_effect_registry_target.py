"""The registry generator writes where the renderer reads the composition."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _package_src(env):
    code = "import runpy,sys; g=runpy.run_path(sys.argv[1], run_name='probe'); print(g['PACKAGE_SRC'])"
    out = subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts" / "gen_effect_registry.py")],
                         env={**os.environ, **env}, capture_output=True, text=True, check=True)
    return Path(out.stdout.strip().splitlines()[-1])


def test_defaults_to_the_installed_package_the_renderer_reads(tmp_path):
    installed = tmp_path / "node_modules" / "@banodoco" / "timeline-composition" / "typescript" / "src"
    installed.mkdir(parents=True)
    env = {"ASTRID_REMOTION_PROJECT_DIR": str(tmp_path), "ASTRID_TIMELINE_COMPOSITION_SRC": ""}
    assert _package_src(env) == installed


def test_explicit_source_still_wins(tmp_path):
    assert _package_src({"ASTRID_TIMELINE_COMPOSITION_SRC": str(tmp_path)}) == tmp_path
