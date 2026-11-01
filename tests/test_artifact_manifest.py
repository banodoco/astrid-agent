"""artifacts.json and cli-surface.json must match what they describe (testing plan C1, R-3)."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

from scripts import gen_tool_catalog

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


artifact_manifest = _load("artifact_manifest")
gen_cli_surface = _load("gen_cli_surface")

SAMPLE = ["astrid/tools/catalog.json", "astrid/packs/rendering/elements/catalog.ts"]


def test_committed_artifact_manifest_is_current() -> None:
    assert artifact_manifest.check() == [], "run `python scripts/gen_cli_surface.py && python scripts/artifact_manifest.py`"


def test_cli_surface_is_current() -> None:
    assert gen_cli_surface.check() == [], "run `python scripts/gen_cli_surface.py`"


def test_tool_catalog_is_current() -> None:
    assert gen_tool_catalog.main(["--check"]) == 0


def test_planted_tool_catalog_drift_names_regenerate_command(tmp_path: Path, capsys) -> None:
    catalog = tmp_path / "catalog.json"
    shutil.copyfile(ROOT / "astrid/tools/catalog.json", catalog)
    catalog.write_bytes(catalog.read_bytes() + b" ")

    assert gen_tool_catalog.main([str(catalog), "--check"]) == 1
    assert "is stale; run scripts/gen_tool_catalog.py" in capsys.readouterr().err


def test_cli_surface_covers_the_timelines_verbs_consumers_prompt() -> None:
    timelines = json.loads((ROOT / "cli-surface.json").read_text(encoding="utf-8"))["families"]["timelines"]["verbs"]
    assert {"show", "checkout", "edit", "commit"} <= set(timelines)
    assert "--at" in timelines["show"]["flags"]


def test_cli_surface_is_an_artifact() -> None:
    assert "cli-surface.json" in json.loads((ROOT / "artifacts.json").read_text(encoding="utf-8"))["artifacts"]


def test_planted_one_byte_change_is_detected_and_named(tmp_path: Path) -> None:
    for relative in SAMPLE:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    patterns = ["astrid/tools/catalog.json", "astrid/packs/rendering/elements/catalog.ts"]
    artifact_manifest.write(tmp_path, patterns)
    assert artifact_manifest.check(tmp_path, patterns) == []

    planted = tmp_path / SAMPLE[0]
    data = bytearray(planted.read_bytes())
    data[0] ^= 1
    planted.write_bytes(bytes(data))

    assert artifact_manifest.check(tmp_path, patterns) == [f"{SAMPLE[0]}: changed"]


def test_planted_cli_surface_drift_is_named(tmp_path: Path) -> None:
    surface = json.loads((ROOT / "cli-surface.json").read_text(encoding="utf-8"))
    del surface["families"]["timelines"]["verbs"]["show"]["flags"]["--at"]
    (tmp_path / "cli-surface.json").write_text(json.dumps(surface, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert gen_cli_surface.check(tmp_path) == ["timelines show: changed"]
