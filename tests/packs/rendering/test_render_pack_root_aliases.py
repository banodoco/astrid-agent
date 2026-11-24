"""Managed webpack aliases use only the render registry's root carrier."""
import json
import subprocess
from pathlib import Path

import pytest


MODULE = Path(__file__).resolve().parents[3] / "remotion" / "webpack-alias.mjs"


def aliases(entries):
    # Dynamic import accepts an absolute module URL, independent of the cwd.
    program = "const m = await import(process.argv[1]); console.log(JSON.stringify(m.packElementAliases({env: {ASTRID_RENDER_PACK_ROOTS: process.argv[2]}})));"
    return subprocess.run(["node", "--input-type=module", "-e", program, MODULE.as_uri(), json.dumps(entries)],
                          capture_output=True, text=True, check=False)


def test_authoritative_roots_and_empty_carrier(tmp_path):
    pack = tmp_path / "rendering"
    element = pack / "rendering/elements/effects/title"
    element.mkdir(parents=True)
    (pack / "pack.yaml").write_text("id: rendering\nrendering:\n  effects/title:\n    type: element\n    path: rendering/elements/effects/title/element.yaml\n")
    result = aliases([{"root": str(pack), "scope": "pack-rendering-elements"}])
    assert result.returncode == 0, result.stderr
    selected = json.loads(result.stdout)
    assert selected["@astrid-rendering"] == str(pack)
    assert selected["@pack-rendering-elements-effects"] == str(element.parent)
    assert json.loads(aliases([]).stdout) == {}


@pytest.mark.parametrize("entry", [{"root": "relative", "scope": "x"}, {"root": "/missing", "scope": "../x"}])
def test_bad_carrier_is_refused(entry):
    assert aliases([entry]).returncode != 0
