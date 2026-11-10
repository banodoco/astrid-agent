"""Dry preparation and real local/attached execution agree on graph, bindings and alignment.

Audio sfx/tts remain GPU cells; every other default runs against the CPU ComfyUI stand-in.
The local child records the actual engine.align argument while preserving real execution.
"""
from __future__ import annotations

import json
import subprocess
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
CELLS = [pytest.param(cell, target, id=f"{'/'.join(cell)}-{target}",
                      marks=[pytest.mark.gpu] if cell in _NEEDS_WEIGHTS or
                      (cell, target) == (("video", "h3"), "local-managed") else [])
         for target in ("attached", "local-managed") for cell in [*SLOTS, ("video", "h3")]]
H3_CLASSES = Path(__file__).resolve().parents[1] / "fixtures" / "h3_cpu" / "object_info_h3_runpod_graph_closure.json"


def _graph(workflow) -> str:
    # Compile the built workflow independently of the submitted prompt. This resolves native
    # links/widgets into the API shape without consulting the eventual submission.
    return json.dumps({"api": workflow.compile("api"),
                       "outputs": [str(output.node_id) for output in workflow.outputs]}, sort_keys=True)


def _text(value) -> str:
    return json.dumps(value, sort_keys=True)


def _output_reachable(graph):
    api = graph["api"]
    pending = list(graph["outputs"])
    assert pending, "built graph declares no output nodes"
    reachable = set()
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        assert node_id in api, f"output ancestry references absent built node {node_id}"
        reachable.add(node_id)
        for value in api[node_id]["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int):
                assert value[0] in api, f"broken built link {value}"
                pending.append(value[0])
    return {node_id: api[node_id] for node_id in reachable}


def _spy_child_align(monkeypatch, path):
    """Observe the real action child across its process boundary, without replacing the engine."""
    from astrid.core.execution import generic_host
    bootstrap = f'''import json, runpy, sys
from pathlib import Path
with Path({str(path)!r}).open("a") as sink:
    sink.write(json.dumps({{"event": "bootstrap"}}) + "\\n")
from astrid.core.generation import entries
from astrid.packs.vibecomfy import local_server_engine
original_approve = local_server_engine._approve_live
def approve(bundle, bindings, provider):
    with Path({str(path)!r}).open("a") as sink:
        sink.write(json.dumps({{"event": "runtime_bindings", "bindings": bindings}}) + "\\n")
    return original_approve(bundle, bindings, provider)
local_server_engine._approve_live = approve
original_local_bundle = local_server_engine.local_server_bundle
def local_bundle(*args, **kwargs):
    identity = kwargs.get("output_identity")
    if identity is not None:
        with Path({str(path)!r}).open("a") as sink:
            sink.write(json.dumps({{"event": "output_namespace", "identity": identity}}) + "\\n")
    return original_local_bundle(*args, **kwargs)
local_server_engine.local_server_bundle = local_bundle
original_table = entries.engine_table
def table():
    engines = original_table()
    for engine in engines.values():
        cls = type(engine)
        if getattr(cls, "_dry_live_spy", False):
            continue
        align = cls.align
        def spy(self, manifest, mode, _align=align, **kwargs):
            with Path({str(path)!r}).open("a") as sink:
                sink.write(json.dumps({{"event": "align", "manifest": manifest}}, sort_keys=True) + "\\n")
            return _align(self, manifest, mode, **kwargs)
        cls.align = spy
        requirements = cls.requirements
        def spy_requirements(self, entry, inputs, _requirements=requirements):
            manifest = _requirements(self, entry, inputs)
            with Path({str(path)!r}).open("a") as sink:
                sink.write(json.dumps({{"event": "requirements", "manifest": manifest,
                                       "bundle": entry.get("bundle")}}, sort_keys=True) + "\\n")
            return manifest
        cls.requirements = spy_requirements
        cls._dry_live_spy = True
    return engines
entries.engine_table = table
sys.argv = ["astrid.core.execution.generic_host_worker", *sys.argv[1:]]
runpy.run_module("astrid.core.execution.generic_host_worker", run_name="__main__")
'''
    def observe_launch(original):
        def launch(argv, *args, **kwargs):
            env = kwargs.get("env") or {}
            if env.get("ASTRID_GENERATION_ENTRY"):
                if "astrid.packs.vibecomfy.actions.run.run" in argv:
                    index = argv.index("-m")
                    module = argv[index + 1]
                    direct_bootstrap = bootstrap.replace(
                        'sys.argv = ["astrid.core.execution.generic_host_worker", *sys.argv[1:]]',
                        f'sys.argv = [{module!r}, *sys.argv[1:]]').replace(
                        'runpy.run_module("astrid.core.execution.generic_host_worker", run_name="__main__")',
                        f'runpy.run_module({module!r}, run_name="__main__")')
                    argv = [*argv[:index], "-c", direct_bootstrap, *argv[index + 2:]]
                elif "astrid.core.execution.generic_host_worker" in argv:
                    index = argv.index("-m")
                    assert argv[index + 1] == "astrid.core.execution.generic_host_worker"
                    argv = [*argv[:index], "-c", bootstrap, *argv[index + 2:]]
                elif "-c" in argv:
                    index = argv.index("-c") + 1
                    marker = "runpy.run_module('astrid.core.execution.generic_host_worker',run_name='__main__')"
                    assert marker in argv[index], "unrecognized generation worker startup"
                    argv = list(argv)
                    argv[index] = argv[index].replace(marker, f"exec({bootstrap!r})")
            return original(argv, *args, **kwargs)
        return launch
    # Observe before process custody wraps the worker in its private tree launcher.
    for name in ("spawn_task_scope", "popen_owned_group"):
        monkeypatch.setattr(generic_host, name, observe_launch(getattr(generic_host, name)))



class Recorder:
    """Spies the seams a run builds its graph through; each run writes its rows into the current sink."""

    def __init__(self, monkeypatch):
        self.sink: list[tuple[str, str]] = []
        prepare = workflow_entries.prepare
        build_entry = workflow_entries.build_entry
        catalog = facade_module._prepare_catalog_target_workflow

        def spy_prepare(*args, **kwargs):
            workflow, plan, assets = prepare(*args, **kwargs)
            self.sink.append(("graph", _graph(workflow), _text(plan.get("bindings", {})), workflow.clone()))
            return workflow, plan, assets

        def spy_build_entry(entry, inputs, job_dir, **kwargs):
            built = build_entry(entry, inputs, job_dir, **kwargs)
            if built is not None:
                self.sink.append(("graph", _graph(built["workflow"]), _text(dict(built["bindings"])), built["workflow"].clone()))
            return built

        def spy_catalog(*args, **kwargs):
            workflow, plan, schema, assets = catalog(*args, **kwargs)
            self.sink.append(("graph", _graph(workflow), _text(dict(plan.get("bindings", {}))), workflow.clone()))
            return workflow, plan, schema, assets

        monkeypatch.setattr(workflow_entries, "prepare", spy_prepare)
        monkeypatch.setattr(workflow_entries, "build_entry", spy_build_entry)
        monkeypatch.setattr(facade_module, "_prepare_catalog_target_workflow", spy_catalog)

    def take(self) -> list[tuple[str, str]]:
        rows, self.sink = self.sink, []
        return rows


def _built(rows: list[tuple[str, str]]) -> tuple[str, str]:
    """The graph and bindings a run settled on: its last built workflow."""
    assert rows, "the run built no graph"
    _, graph, bindings, workflow = rows[-1]
    return graph, bindings, workflow


@pytest.mark.parametrize("cell,target_kind", CELLS)
def test_dry_run_equals_live_run(cell, target_kind, tmp_path, monkeypatch, real_runtime, tuple_sources):
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
    align_log = tmp_path / "actual-align.jsonl"
    _spy_child_align(monkeypatch, align_log)
    runs = {}
    replay_seed = None

    def run(facade, controls, tmp, **extra):
        if h3:
            return facade.video(entry="h3", request=str(H3_REQUEST), asset_map=str(h3_assets), **controls, **extra)
        inputs = dict(PROMPTS[cell])
        if replay_seed is not None:
            inputs["seed"] = replay_seed  # reproduce the concrete seed shown by the dry preparation
        if modality == "image" and mode == "edit":
            source = tmp / "source.png"
            Image.new("RGB", (64, 64), (200, 30, 30)).save(source)
            inputs["image"] = str(source)
        method = {"image": facade.image, "audio": facade.audio}.get(modality, facade.video)
        return method(mode=mode, **controls, **inputs, **extra)

    def call(facade, controls, tmp):
        nonlocal replay_seed
        recorder.take()
        dry = run(facade, controls, tmp, dry_run=True)
        runs["dry"] = (dry, recorder.take())
        if not h3:
            replay_seed = dry["entry"]["bindings"].get("seed")
        live = run(facade, controls, tmp)
        runs["live"] = (live, recorder.take())
        return live

    with _drive(cell, tmp_path, monkeypatch, real_runtime, tuple_sources, extra_classes=h3_classes, call=call,
                machine_identity=h3, target_kind=target_kind, h3_output=h3) as (client, project_id, provider, result):
        assert result.ok, json.dumps(result.to_dict(), default=str)[:2000]
        (dry, dry_rows), (live, live_rows) = runs["dry"], runs["live"]
        assert isinstance(dry, dict) and dry["status"] == "ready", dry
        dry_graph, dry_bindings, dry_workflow = _built(dry_rows)
        live_graph, live_bindings, live_workflow = _built(live_rows)
        # 1. The built graph is real, and a prompt cell binds its prompt.
        built = json.loads(live_graph)
        assert built["api"], "the built graph is empty"
        assert json.loads(dry_graph) == built, "built graph differs between dry and live"
        assert json.loads(dry_bindings) == json.loads(live_bindings), "bindings differ between dry and live"
        assert json.loads(dry_bindings) == dry["entry"]["bindings"], "dry result's bindings differ from its build"
        if not h3:
            assert json.loads(live_bindings), "a prompt cell bound no values"
        # The local child records the exact argument passed to real engine.align.
        assert align_log.is_file(), "the real child recorded no engine events"
        events = [json.loads(line) for line in align_log.read_text().splitlines()]
        if target_kind == "local-managed":
            actual = [event["manifest"] for event in events if event["event"] == "align"]
            assert len(actual) == 1, actual
            assert actual[0] == dry["entry"]["requirements"], "dry and actual align manifests differ"
        elif h3:
            # Attached defaults to trust, so it does not align. Still prove that the real child
            # reads the emitted built graph rather than h3's static bundle or rebuilding h3.
            actual = [event for event in events if event["event"] == "requirements"]
            assert len(actual) == 1, actual
            assert actual[0]["manifest"] == dry["entry"]["requirements"]
            assert Path(actual[0]["bundle"]) != workflow_entries.bundle_path(workflow_entries.find_entry("h3"))

        submitted = provider.comfy.prompts[-1]["prompt"]
        assert submitted, "the live run submitted an empty graph"
        namespaces = [event["identity"] for event in events if event["event"] == "output_namespace"]
        assert len(namespaces) == 1, namespaces
        from astrid.packs.vibecomfy.local_server_engine import run_scoped_overrides
        scoped = run_scoped_overrides(live_workflow, namespaces[0])
        # Uploads bind declared media inputs to the exact names returned by ComfyUI. Every
        # non-media value must remain the prepared value, including prompt and resolved seed.
        runtime = [event["bindings"] for event in events if event["event"] == "runtime_bindings"]
        assert len(runtime) == 1, runtime
        media = {name: value for name, value in runtime[0].items() if str(
            getattr(scoped.inputs[name], "media_semantics", "") or "").lower() in {"image", "mask", "audio", "video"}}
        for name, value in runtime[0].items():
            if name not in media:
                assert json.loads(live_bindings).get(name, scoped.inputs[name].value) == value, name
        for name, value in media.items():
            scoped.set_input(name, value)
        expected = _output_reachable(json.loads(_graph(scoped)))
        assert submitted == expected, (
            "submitted graph differs in nodes, inputs, links or values; "
            f"outputs={[(out.node_id, scoped.nodes[out.node_id].inputs, scoped.nodes[out.node_id].widgets) for out in scoped.outputs]!r}; "
            f"variant={scoped.default_variant!r} overrides={scoped.variants!r}"
        )
