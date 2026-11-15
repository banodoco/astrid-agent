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
from tests.local_engine_stub import local_engine_patch


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


def _fixture_workflow(tmp_path: Path) -> Path:
    """Emit a real Python workflow bundle: module plus companion."""
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow import VibeNode, VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate

    workflow = VibeWorkflow("fixture", WorkflowSource("fixture"))
    workflow.nodes["1"] = VibeNode("1", "FixtureNode", inputs={"seed": 0}, uid="fixture-node")
    path = tmp_path / "workflow.py"
    emit_bundle_with_candidate(workflow, path, Provenance.USER_CONFIRMED, None, source_format="scratchpad")
    return path


def _engine_result(engine_output: Path):
    """The installed engine's reply: one finished output, sealed in its result envelope."""
    import hashlib

    def run_workflow_result_path(_workflow, destination, **kwargs):
        root = Path(destination) / "engine-result"
        output = root / "outputs" / engine_output.name
        output.parent.mkdir(parents=True)
        output.write_bytes(engine_output.read_bytes())
        data = output.read_bytes()
        envelope = root / "managed-generation-result.json"
        envelope.write_text(json.dumps({
            "schema_version": 1,
            "kind": "managed-generation-result.v1",
            "inputs": {"values": kwargs["workflow_input_bindings"]},
            "outputs": [{"producer_output_id": "engine:video:0", "output_port": "video", "ordinal": 0,
                         "path": f"outputs/{engine_output.name}", "media_type": "video/mp4",
                         "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}],
            "created": "2026-09-21T12:00:00Z",
            "warnings": [],
            "task_id": kwargs["task_identity"],
            "attempt_id": kwargs["attempt_identity"],
            "producer_run_id": "producer-1",
            "outcomes": {"execution": {"status": "succeeded"}, "retrieval": {"status": "succeeded"},
                         "verification": {"status": "succeeded"}, "publication": {"status": "not_started"}},
            "evidence": {"producer": {"engine": {"name": "fixture-engine"}},
                         "transport": {"location": {"opaque": True}}},
        }), encoding="utf-8")
        return SimpleNamespace(outputs=(output,), managed_generation_result_path=envelope,
                               managed_generation_result=None)

    return run_workflow_result_path


def _stand_in_installed_engine(monkeypatch, tmp_path: Path) -> None:
    import astrid.core.generation.managed_preparation as managed

    class InstalledEngine:
        root = str(tmp_path / "engine-root")
        recipe = SimpleNamespace(engine="comfyui")

        def __init__(self, **_kwargs):
            pass

        def prepare(self, *_args, **_kwargs):
            return {"endpoint": "http://127.0.0.1:8188"}

        def close(self, **_kwargs):
            pass

    def prepared_attempt(*, execution_identity=None, **_kwargs):
        return {"placement": "managed", "alignment": {"mode": "check"}, "manifest": {"models": []},
                "execution_identity": execution_identity}

    monkeypatch.setattr(managed, "ManagedEnginePreparation", InstalledEngine)
    monkeypatch.setattr(managed, "read_preparation", prepared_attempt)


def test_run_accepts_matching_workflow_and_managed_asset_binding(
    tmp_path: Path, monkeypatch,
) -> None:
    workflow = _fixture_workflow(tmp_path)
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
    managed_run = Mock(side_effect=_engine_result(engine_output))
    monkeypatch.setattr(
        "astrid.packs.vibecomfy.production_engine.run_workflow_result_path",
        managed_run,
    )

    # Every local managed run goes through owned preparation; stand in for the
    # installed engine below that boundary so the run reaches the mocked engine call.
    _stand_in_installed_engine(monkeypatch, tmp_path)

    with local_engine_patch():
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
