"""ltx-2.3 t2v: the catalog and the pinned template agree on the image requirement.

The pinned template (video/ltx2_3_t2v) is image-conditioned, so the catalog must
require an image for t2v too. A prompt-only dry run is therefore refused by input
validation (naming the image), not accepted and later failing at execution.
"""

import json
from pathlib import Path

import yaml

from astrid import console

CATALOG = Path(__file__).resolve().parents[3] / "astrid/core/model_catalog/models.yaml"


def _ltx_t2v():
    rows = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))["models"]
    entry = next(row for row in rows if row["id"] == "ltx-2.3")
    return entry["modes"]["t2v"]


def test_catalog_t2v_requires_the_image_the_template_needs():
    mode = _ltx_t2v()
    assert "image_ref" in mode["supports"]
    assert "image_ref" in mode["requires"]


def test_prompt_only_t2v_dry_run_is_refused_for_the_missing_image(tmp_path, capsys):
    rc = console.main(["generate", "video", "A quiet street at dusk.", "--entry", "ltx-2.3",
                       "--mode", "t2v", "--placement", "runpod", "--dry-run", "--json"])
    assert rc != 0
    refused = json.loads(capsys.readouterr().out)
    assert "input_rejected" in json.dumps(refused)
    assert "image" in json.dumps(refused)
