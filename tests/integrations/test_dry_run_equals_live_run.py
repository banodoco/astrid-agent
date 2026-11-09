"""Plan 32 Completion 5: the dry run equals the live run.

Placement scope: attached only. Every default slot and the h3_av entry run against one CPU
stand-in attached ComfyUI, which is the only placement whose /prompt graph this harness can see.
The dry run (``dry_run=True``) runs first, then the live run. The test compares the built graph,
the bindings, the requirements manifest the live run recorded for its attempt, and the submitted
/prompt graph. local-managed and RunPod placements are NOT compared here.

Post-T8 follow-up: the attached live route never calls the requirements step, so its manifest is
recorded from the entry the run resolved; a managed-local cell and a RunPod cell need their own
comparison, and the wider call-site work (align and readiness receipts) is not covered here.
"""
from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from PIL import Image

from astrid.core.generation import entries as workflow_entries
from astrid.sdk import generation as facade_module
from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources  # noqa: F401  (fixture)
from tests.integrations.test_runpod_slot_path_cpu import (
    H3_REQUEST, PROMPTS, SLOTS, _drive, _h3_built_classes,
)

pytestmark = pytest.mark.integration

# The sound-effects slot and the attached speech slot need real weights the CPU harness keeps none of (Plan 34).
_NEEDS_WEIGHTS = {("audio", "sfx"), ("audio", "tts")}
CELLS = [pytest.param(cell, marks=pytest.mark.gpu) if cell in _NEEDS_WEIGHTS else cell
         for cell in [*SLOTS, ("video", "h3")]]
H3_CLASSES = Path(__file__).resolve().parents[1] / "fixtures" / "h3_cpu" / "object_info_h3_runpod_graph_closure.json"


def _graph(workflow) -> str:
    """The built graph as canonical text: every runtime node with its class and values."""
    nodes = {node_id: {"class_type": node.class_type, "inputs": node.inputs, "widgets": node.widgets}
             for node_id, node in workflow.runtime_nodes().items()}
    return json.dumps(nodes, sort_keys=True, default=str)


def _text(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


class Recorder:
    """Spies the seams a run builds its graph through; each run writes its rows into the current sink."""

    def __init__(self, monkeypatch):
        self.sink: list[tuple[str, str]] = []
        prepare = workflow_entries.prepare
        build_entry = workflow_entries.build_entry
        catalog = facade_module._prepare_catalog_target_workflow

        def spy_prepare(*args, **kwargs):
            workflow, plan, assets = prepare(*args, **kwargs)
            self.sink.append(("graph", _graph(workflow), _text(plan.get("bindings", {}))))
            return workflow, plan, assets

        def spy_build_entry(entry, inputs, job_dir, **kwargs):
            built = build_entry(entry, inputs, job_dir, **kwargs)
            if built is not None:
                self.sink.append(("graph", _graph(built["workflow"]), _text(dict(built["bindings"]))))
            return built

        def spy_catalog(*args, **kwargs):
            workflow, plan, schema, assets = catalog(*args, **kwargs)
            self.sink.append(("graph", _graph(workflow), _text(dict(plan.get("bindings", {})))))
            return workflow, plan, schema, assets

        monkeypatch.setattr(workflow_entries, "prepare", spy_prepare)
        monkeypatch.setattr(workflow_entries, "build_entry", spy_build_entry)
        monkeypatch.setattr(facade_module, "_prepare_catalog_target_workflow", spy_catalog)

    def take(self) -> list[tuple[str, str]]:
        rows, self.sink = self.sink, []
        return rows


def _resolved_entry(entry_id: str) -> dict:
    """The entry a dry run resolved an id to: the workflow entry, or a catalog id's local template."""
    described = workflow_entries.find_entry(entry_id)
    if described is None:  # a catalog id: its one local mode template owns the models
        from astrid.core.model_catalog.registry import ModelRegistry
        spec = ModelRegistry.load_default().get(entry_id)
        described = workflow_entries.find_entry(next(iter(spec.modes.values())).backends["local"].template)
    return described


def _built(rows: list[tuple[str, str]]) -> tuple[str, str]:
    """The graph and bindings a run settled on: its last built workflow."""
    assert rows, "the run built no graph"
    _, graph, bindings = rows[-1]
    return graph, bindings


@pytest.mark.parametrize("cell", CELLS, ids=lambda cell: "/".join(cell) if isinstance(cell, tuple) else "/".join(cell.values[0]))
def test_dry_run_equals_live_run(cell, tmp_path, monkeypatch, real_runtime, tuple_sources):
    modality, mode = cell
    h3 = cell == ("video", "h3")
    h3_classes, h3_assets = (), None
    if h3:
        source = tmp_path / "source.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=blue:s=1024x576:r=24", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                        "-frames:v", "57", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                        str(source)], check=True, timeout=120)
        request = json.loads(H3_REQUEST.read_text())
        h3_assets = tmp_path / "assets.json"
        h3_assets.write_text(json.dumps({request["media"][0]["asset"]["id"]: str(source), "source": str(source)}))
        h3_classes = set(json.loads(H3_CLASSES.read_text())) | _h3_built_classes(
            H3_REQUEST, json.loads(h3_assets.read_text()), tmp_path)
    recorder = Recorder(monkeypatch)
    runs = {}

    def run(facade, controls, tmp, **extra):
        if h3:
            return facade.video(entry="h3", request=str(H3_REQUEST), asset_map=str(h3_assets), **controls, **extra)
        inputs = dict(PROMPTS[cell])
        if modality == "image" and mode == "edit":
            source = tmp / "source.png"
            Image.new("RGB", (64, 64), (200, 30, 30)).save(source)
            inputs["image"] = str(source)
        method = {"image": facade.image, "audio": facade.audio}.get(modality, facade.video)
        return method(mode=mode, **controls, **inputs, **extra)

    def call(facade, controls, tmp):
        recorder.take()
        dry = run(facade, controls, tmp, dry_run=True)
        runs["dry"] = (dry, recorder.take())
        live = run(facade, controls, tmp)
        runs["live"] = (live, recorder.take())
        return live

    with _drive(cell, tmp_path, monkeypatch, real_runtime, tuple_sources, extra_classes=h3_classes, call=call,
                machine_identity=h3, target_kind="attached", h3_output=h3) as (client, project_id, provider, result):
        assert result.ok, json.dumps(result.to_dict(), default=str)[:2000]
        (dry, dry_rows), (live, live_rows) = runs["dry"], runs["live"]
        assert isinstance(dry, dict) and dry["status"] == "ready", dry
        dry_graph, dry_bindings = _built(dry_rows)
        live_graph, live_bindings = _built(live_rows)
        # 1. The built graph is real, and a prompt cell binds its prompt.
        built = json.loads(live_graph)
        assert built, "the built graph is empty"
        assert json.loads(dry_graph) == built, "built graph differs between dry and live"
        assert json.loads(dry_bindings) == json.loads(live_bindings), "bindings differ between dry and live"
        assert json.loads(dry_bindings) == dry["entry"]["bindings"], "dry result's bindings differ from its build"
        if not h3:
            assert json.loads(live_bindings), "a prompt cell bound no values"
        # 3. The requirements manifest: the one the live run recorded for its attempt equals the one the dry
        #    run's entry yields from the dry inputs, through the same requirements() call.
        expected = workflow_entries.requirements(_resolved_entry(dry["entry"]["id"]), dict(dry["entry"]["bindings"]))
        recorded = _recorded_requirements(live.to_dict())
        assert expected and recorded is not None, "the live run recorded no requirements manifest"
        assert json.loads(json.dumps(expected, sort_keys=True, default=str)) == recorded, (
            "requirements manifest differs between dry and live")

        # 2. Every submitted node is a built node with the same class and the same scalar values (links aside).
        submitted = provider.comfy.prompts[-1]["prompt"]
        assert submitted, "the live run submitted an empty graph"
        for node_id, node in submitted.items():
            assert node_id in built, f"submitted node {node_id} was not built"
            assert node["class_type"] == built[node_id]["class_type"], node_id
            for name, value in node["inputs"].items():
                if isinstance(value, (str, int, float, bool)) or value is None:
                    assert built[node_id]["inputs"].get(name) == value, (node_id, name)

def _recorded_requirements(value):
    """The requirements the live run recorded in its attempt's producer evidence (producer.entry.requirements)."""
    if isinstance(value, dict):
        producer = value.get("producer")
        if isinstance(producer, dict) and isinstance(producer.get("entry"), dict) and "requirements" in producer["entry"]:
            return producer["entry"]["requirements"]
        for child in value.values():
            found = _recorded_requirements(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _recorded_requirements(child)
            if found is not None:
                return found
    return None
