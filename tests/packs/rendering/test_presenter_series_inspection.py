"""Component-zero ROI prerequisite through the existing generated-host seam."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.packs.rendering.test_presenter_scene_capability import (
    presenter_query as _presenter_query,
)

presenter_query = _presenter_query
pytestmark = pytest.mark.integration


def test_series_inspection_retains_one_component_zero_query(presenter_query):
    for name in ("presenter_crop_geometry.mjs", "presenter_series_inspection.mjs"):
        shutil.copyfile(Path(__file__).with_name(name), presenter_query.project / name)
    result = subprocess.run(
        [str(presenter_query.node), "presenter_series_inspection.mjs"],
        cwd=presenter_query.project, env=presenter_query.env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    answer = json.loads(result.stdout)
    assert answer["ok"] is True
    assert answer["reads"] == 1
