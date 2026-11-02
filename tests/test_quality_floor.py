import json
from pathlib import Path

import pytest

from astrid.packs.iteration.api import AssembleError, assemble_iteration


UNRESOLVED_RUN_ID = "01ARZ3NDEKTSV4RRFFQ69G5FH0"
VALID_ROOT_ID = "01ARZ3NDEKTSV4RRFFQ69G5FH1"


def test_quality_floor_refuses_before_adapter_files_and_names_only_unresolved(tmp_path: Path) -> None:
    prepare_dir = _write_low_quality_prepare(tmp_path)
    out_dir = tmp_path / "assembled"

    with pytest.raises(AssembleError) as raised:
        assemble_iteration(prepare_dir=prepare_dir, out_path=out_dir, repo_root=tmp_path)

    message = str(raised.value)
    assert "data_quality 0.500" in message
    assert UNRESOLVED_RUN_ID in message
    assert VALID_ROOT_ID not in message
    assert "restore or regenerate lineage" in message
    assert not (out_dir / "hype.timeline.json").exists()
    assert not (out_dir / "hype.assets.json").exists()
    assert not (out_dir / "iteration.timeline.json").exists()


def test_quality_floor_force_writes_outputs_and_logs_forced(tmp_path: Path) -> None:
    prepare_dir = _write_low_quality_prepare(tmp_path)
    out_dir = tmp_path / "assembled"

    assemble_iteration(prepare_dir=prepare_dir, out_path=out_dir, repo_root=tmp_path, force=True)

    manifest = json.loads((out_dir / "iteration.manifest.json").read_text(encoding="utf-8"))
    quality = json.loads((out_dir / "iteration.quality.json").read_text(encoding="utf-8"))
    assert manifest["assembly"]["forced"] is True
    assert quality["forced"] is True
    assert (out_dir / "hype.timeline.json").is_file()
    assert (out_dir / "hype.assets.json").is_file()


def _write_low_quality_prepare(tmp_path: Path) -> Path:
    prepare_dir = tmp_path / "prepare"
    prepare_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "target_run_id": UNRESOLVED_RUN_ID,
        "thread_id": "01ARZ3NDEKTSV4RRFFQ69G5FH2",
        "runs": [
            {
                "run_id": UNRESOLVED_RUN_ID,
                "output_artifacts": [{"kind": "image", "role": "other", "path": "runs/source/image.png", "sha256": "a" * 64}],
            }
        ],
        "quality": {"data_quality": 0.5},
    }
    quality = {
        "schema_version": 1,
        "target_run_id": UNRESOLVED_RUN_ID,
        "data_quality": 0.5,
        "valid_roots": [VALID_ROOT_ID],
        "unresolved_producer_runs": [
            {
                "run_id": UNRESOLVED_RUN_ID,
                "missing_parent_run_ids": ["01ARZ3NDEKTSV4RRFFQ69G5FH3"],
                "reason": "missing referenced producer",
            }
        ],
    }
    _write_json(prepare_dir / "iteration.manifest.json", manifest)
    _write_json(prepare_dir / "iteration.quality.json", quality)
    return prepare_dir


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
