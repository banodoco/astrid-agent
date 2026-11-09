from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.packs.h3_av.shared.compile import compile_preparation
from astrid.packs.h3_av.shared.compile import _write_asset_bundle
from astrid.packs.h3_av.shared.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.vibecomfy.actions.run import run
from astrid.packs.vibecomfy.actions.run.run import _stage_managed_assets


def test_compiler_archive_stages_to_comfy_input(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture-video")
    request = normalize_request(
        {
            "version": 1,
            "operation": "continue",
            "source": {"asset": "source", "range": [0, 1]},
            "output": {"duration": 2},
            "content": {"prompt": "The speaker finishes the sentence in the same room."},
            "changes": {
                "video": [{"during": [1, 2], "area": {"full_frame": True}, "action": "generate"}],
                "audio": [{"during": [1, 2], "action": "generate"}],
            },
            "references": [],
            "overrides": {},
        }
    )
    compiled = compile_preparation(
        prepare_request(request, asset_map={"source": str(source)}),
        out_dir=tmp_path / "compiled",
    )

    bindings = _stage_managed_assets(
        compiled["managed_assets"]["path"],
        readiness_profile=None,
        output_root=tmp_path / "run",
    )

    assert bindings == {"source_video": compiled["workflow_inputs"]["source_video"]}
    assert (tmp_path / "run" / "engine-input" / bindings["source_video"]).read_bytes() == b"fixture-video"


def test_run_accepts_matching_workflow_and_managed_asset_binding(
    tmp_path: Path, monkeypatch,
) -> None:
    workflow = tmp_path / "workflow.py"
    workflow.write_text("# fixture\n", encoding="utf-8")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    managed_assets = tmp_path / "managed-assets.zip"
    manifest = _write_asset_bundle(
        managed_assets,
        {"source_video": source},
        workflow_inputs={"seed": 17, "prompt": "say the exact line"},
    )
    source_member = Path(manifest["assets"][0]["member"]).name
    engine_output = tmp_path / "run" / "engine-output" / "result.mp4"
    engine_output.parent.mkdir(parents=True)
    engine_output.write_bytes(b"result")
    preflight = Mock()
    monkeypatch.setattr("astrid.packs.vibecomfy.invocation_preflight.preflight_invocation", preflight)
    managed_run = Mock(
        return_value=SimpleNamespace(
            outputs=(engine_output,), managed_generation_result_path=None
        )
    )
    monkeypatch.setattr(
        "astrid.packs.vibecomfy.production_engine.run_workflow_result_path",
        managed_run,
    )

    run._run_and_settle(
        workflow,
        tmp_path / "run",
        task_identity="task-1",
        managed_assets=str(managed_assets),
        workflow_inputs=json.dumps(
            {
                "source_video": source_member,
                "seed": 17,
                "prompt": "say the exact line",
            }
        ),
    )

    assert managed_run.call_args.kwargs["workflow_input_bindings"] == {
        "source_video": source_member,
        "seed": 17,
        "prompt": "say the exact line",
    }
    assert preflight.call_args.kwargs["run_inputs"] == {
        "source_video": source_member,
        "seed": 17,
        "prompt": "say the exact line",
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [("seed", 18), ("prompt", "a conflicting prompt")],
)
def test_run_rejects_conflicting_manifest_scalar_before_production(
    tmp_path: Path, monkeypatch, name: str, value: object,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    managed_assets = tmp_path / "managed-assets.zip"
    _write_asset_bundle(
        managed_assets,
        {"source_video": source},
        workflow_inputs={"seed": 17, "prompt": "the manifest prompt"},
    )
    managed_run = Mock()
    monkeypatch.setattr(
        "astrid.packs.vibecomfy.production_engine.run_workflow_result_path",
        managed_run,
    )

    with pytest.raises(ValueError, match=f"managed assets conflict with workflow inputs: {name}"):
        run._run_and_settle(
            tmp_path / "workflow.py",
            tmp_path / "run",
            task_identity="task-1",
            managed_assets=str(managed_assets),
            workflow_inputs=json.dumps({name: value}),
        )

    managed_run.assert_not_called()
