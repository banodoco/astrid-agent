import json
from pathlib import Path

from astrid.packs.iteration.api import assemble_iteration


def test_generic_card_fallback_payload_and_command_diagnostic(tmp_path: Path) -> None:
    prepare_dir = tmp_path / "prepare"
    prepare_dir.mkdir()
    manifest = {
        "schema_version": 1,
        "target_run_id": "01ARZ3NDEKTSV4RRFFQ69G5FJ0",
        "thread_id": "01ARZ3NDEKTSV4RRFFQ69G5FJ1",
        "runs": [
            {
                "run_id": "01ARZ3NDEKTSV4RRFFQ69G5FJ0",
                "output_artifacts": [{"kind": "unknown_kind", "role": "other", "sha256": "d" * 64}],
            }
        ],
        "quality": {"data_quality": 1.0},
    }
    quality = {
        "schema_version": 1,
        "target_run_id": "01ARZ3NDEKTSV4RRFFQ69G5FJ0",
        "data_quality": 1.0,
        "valid_roots": [],
        "unresolved_producer_runs": [],
    }
    (prepare_dir / "iteration.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (prepare_dir / "iteration.quality.json").write_text(json.dumps(quality), encoding="utf-8")

    result = assemble_iteration(prepare_dir=prepare_dir, out_path=tmp_path / "assembled", repo_root=tmp_path)

    final_manifest = json.loads((tmp_path / "assembled" / "iteration.manifest.json").read_text(encoding="utf-8"))
    decision = final_manifest["assembly"]["renderer_decisions"][0]
    assert decision["renderer"] == "generic_card"
    assert decision["fallback"] is True
    assert decision["diagnostic"] == "no renderer for kind:unknown_kind"
    assert decision["html_aside"] == '<aside class="renderer-fallback">no renderer for kind:unknown_kind</aside>'
    assert result["diagnostics"] == ["renderer-fallback: no renderer for kind:unknown_kind"]
