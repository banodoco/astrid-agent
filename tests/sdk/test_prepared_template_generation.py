"""Real catalog/binder/emitter oracles, with an in-memory HTTP response only.

No runtime or GPU is invoked. The unchanged source-retention and local-server
integration suites remain the admission and attempt-prefix guards.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import prepared_templates as prepared
from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
from astrid.core.generation.backends.vibecomfy import VibeComfyBackend
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.sdk.command_timing import TimingCollector, timing_scope
from astrid.sdk.generation import _prepare_catalog_target_workflow
from vibecomfy.workflow_bundle import emit_bundle

_OWNER_SUPPORTED = hasattr(os, "geteuid") or hasattr(os, "getuid")
_OWNER_REQUIRED = pytest.mark.skipif(not _OWNER_SUPPORTED, reason="disk reuse requires current-user ownership verification")


def _events(collector):
    return [(event["outcome"], event.get("reason")) for event in collector.cache_event_snapshot()
            if event["cache"] == "prepared_template"]


def _catalog_command(raw, inputs, destination, *, session, retain_source=None):
    """The shared SDK path, using its current role resolution and binders."""
    requests = []

    def opener(request, **kwargs):
        requests.append((request.method, request.full_url))
        assert request.method == "GET" and request.full_url.endswith("/object_info")
        return io.BytesIO(raw)

    client = ComfyTargetClient(ComfyTarget("http://127.0.0.1:8188", "attached"), opener=opener)
    registry = ModelRegistry.load_default()
    collector = TimingCollector()
    with timing_scope(collector):
        workflow, plan, schema, assets = _prepare_catalog_target_workflow(
            registry=registry, candidate=registry.get("z-image"), mode="t2i", modality="image",
            inputs=inputs, target_client=client, backend=VibeComfyBackend(target_client=client),
            defer_target=True, preparation=session, retain_source=retain_source)
        assert not assets
        bundle = (emit_bundle(workflow, destination, {"operation": "authored"}) if session is None
                  else session.emit(emit_bundle, workflow, destination, {"operation": "authored"}))
        bundle.require_canonical_authority("prepared-template differential")
        receipt = bundle.compile(schema_provider=schema)
    result = {
        "python": destination.read_bytes(), "companion": destination.with_suffix(".vibe.json").read_bytes(),
        "api": bundle.workflow.compile("api"), "receipt": receipt.to_canonical_bytes(),
        "source": (bundle.workflow.source.id, bundle.workflow.source.source_type),
        "provenance": bundle.provenance, "sidecar": bundle.ui_sidecar,
        "identities": (bundle.semantic_digest, bundle.ui_digest, bundle.revision_id, bundle.parent_revision),
    }
    # Preserve bytes before using the exact same logical publication path for
    # the next independent result. The receipt retains that path as evidence.
    destination.unlink()
    destination.with_suffix(".vibe.json").unlink()
    return SimpleNamespace(result=result, workflow=workflow, plan=plan,
                           events=_events(collector), requests=requests, timings=collector.snapshot())


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    if not _OWNER_SUPPORTED:
        pytest.skip("disk reuse requires current-user ownership verification; full fallback has a separate witness")
    from tests.core.generation.backends.test_comfy_model_resolution import target_info
    from vibecomfy.registry import ready
    from vibecomfy.porting.emit import emit_ready
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr("astrid.core.generation.engine_readiness.current_comfy_provenance", lambda: None)
    raw = json.dumps(target_info(), ensure_ascii=False).encode()
    registry = ModelRegistry.load_default()
    spec = registry.get("z-image").modes["t2i"].backends["local"]
    source = ready.resolve_ready_template(spec.template, ready.repo_ready_template_discovery()).path
    original = source.read_bytes()
    authored = ready.workflow_from_ready(spec.template)
    builds, retained, layouts = [], [], []
    real_build = ready.workflow_from_ready
    real_layout = emit_ready._prepare_source_layout

    def layout(*args, **kwargs):
        layouts.append(True)
        return real_layout(*args, **kwargs)

    monkeypatch.setattr(emit_ready, "_prepare_source_layout", layout)

    def observe(*args, **kwargs):
        # Every source build remains behind the exact current-source retention.
        assert retained and retained[-1] == (source.name, original)
        builds.append(args[0])
        return real_build(*args, **kwargs)

    monkeypatch.setattr(ready, "workflow_from_ready", observe)

    def run(inputs, *, cached=True, response=None):
        session = prepared.PreparationSession() if cached else None
        result = _catalog_command(raw if response is None else response, inputs, tmp_path / "workflow.py",
            session=session, retain_source=lambda name, data: retained.append((name, data)))
        return result, session

    return SimpleNamespace(run=run, raw=raw, builds=builds, retained=retained, authored=authored, layouts=layouts)


@pytest.mark.parametrize("same_as_authored_default", [False, True])
def test_catalog_cold_repeated_and_changed_values_match_full_bytes_receipt_and_source(catalog, same_as_authored_default):
    prompt = (catalog.authored.inputs["prompt"].default if same_as_authored_default
              else 'A "quoted" apostrophe \' and backslash \\\nUnicode café 雪 🙂')
    inputs = {"prompt": prompt, "seed": 42, "width": 768, "height": 640}
    cold, cold_session = catalog.run(inputs)
    cold_layout_count = len(catalog.layouts)
    assert cold_layout_count > 0
    warm, warm_session = catalog.run(inputs)
    assert len(catalog.layouts) == cold_layout_count
    assert catalog.builds == ["image/z_image_turbo"]
    full, _ = catalog.run(inputs, cached=False)
    assert cold.result == warm.result == full.result
    assert ("hit", None) in warm.events
    assert not any(row["stage"] == "source workflow build" for row in warm.timings)
    assert any(row["stage"] == "prepared template key" for row in warm.timings)
    assert warm.requests == cold.requests == full.requests
    assert len(warm.requests) == 1
    assert len(catalog.retained) == 3
    assert cold_session.use.filled.snapshot().key == warm_session.use.filled.snapshot().key

    before = len(catalog.builds)
    before_layouts = len(catalog.layouts)
    changed_inputs = {"prompt": 'Another\n"prompt" — 雪', "seed": 314159, "width": 512, "height": 896}
    changed, changed_session = catalog.run(changed_inputs)
    assert len(catalog.builds) == before
    assert len(catalog.layouts) == before_layouts
    assert ("hit", None) in changed.events
    expected, _ = catalog.run(changed_inputs, cached=False)
    assert changed.result == expected.result
    assert changed.result["python"] != warm.result["python"]
    assert changed.result["identities"][2] != warm.result["identities"][2]
    assert changed_session.use.filled.snapshot().key == warm_session.use.filled.snapshot().key
    assert changed_inputs["prompt"].encode() not in changed_session.use.filled.snapshot().to_bytes()


def test_seedless_catalog_hit_uses_a_fresh_draw_and_matches_full_generation(catalog, monkeypatch):
    draws = iter((101, 202, 202))
    monkeypatch.setattr("secrets.randbelow", lambda upper: next(draws))
    inputs = {"prompt": "seedless prepared catalog"}
    cold, _ = catalog.run(inputs)
    warm, _ = catalog.run(inputs)
    assert catalog.builds == ["image/z_image_turbo"]
    expected, _ = catalog.run(inputs, cached=False)
    assert cold.workflow.inputs["seed"].value == 101
    assert warm.workflow.inputs["seed"].value == 202
    assert cold.result["receipt"] != warm.result["receipt"]
    assert warm.result == expected.result
    assert ("hit", None) in warm.events


@pytest.mark.parametrize("change", ["role_plan", "node_list_digest"])
def test_fresh_role_and_raw_node_response_changes_force_real_source_build(catalog, change):
    inputs = {"prompt": "current target", "seed": 5}
    catalog.run(inputs)
    if change == "role_plan":
        info = json.loads(catalog.raw)
        info["VAELoader"]["input"]["required"]["vae_name"] = [["ae.safetensors"]]
        raw = json.dumps(info).encode()
    else:
        raw = b" \n" + catalog.raw + b"\r\n"
    changed, _ = catalog.run(inputs, response=raw)
    assert catalog.builds == ["image/z_image_turbo", "image/z_image_turbo"]
    assert changed.events[:2] == [("miss", "key_changed:" + change),
                                  ("recompute", "key_changed:" + change)]
    assert ("hit", None) not in changed.events
    full, _ = catalog.run(inputs, cached=False, response=raw)
    assert changed.result == full.result


def test_helper_module_change_invalidates_catalog_source_closure(catalog, tmp_path, monkeypatch):
    from vibecomfy import resources
    import vibecomfy.templates as helpers
    helper = Path(helpers.__file__).resolve()
    copied_helper = tmp_path / "builder-helper.py"
    copied_helper.write_bytes(helper.read_bytes())
    actual = prepared._file_digest_current
    witnessed = []
    paths = set()

    def current_bytes(path):
        paths.add(path.resolve())
        if path.resolve() == helper:
            witnessed.append(path)
            # Edit a stand-in copy, never the installed package. The production
            # closure walk and stat/content witness still do the observation.
            return actual(copied_helper)
        return actual(path)

    monkeypatch.setattr(prepared, "_file_digest_current", current_bytes)
    inputs = {"prompt": "unchanged template, current helper", "seed": 5}
    cold, first = catalog.run(inputs)
    warm, _ = catalog.run(inputs)
    assert catalog.builds == ["image/z_image_turbo"]
    assert ("hit", None) in warm.events
    before = first.use.context["source_provenance"]
    assert before["builder_closure"] == first.use.context["compiler_identity"]
    copied_helper.write_bytes(copied_helper.read_bytes() + b"\n# current helper revision\n")
    changed, revised = catalog.run(inputs)
    after = revised.use.context["source_provenance"]
    assert witnessed
    assert resources._resource_location("template_index").resolve() in paths
    ready_root = resources._resource_location("ready_templates").resolve()
    assert ready_root not in helper.parents
    assert any(ready_root in path.parents for path in paths)
    assert before["members"] == after["members"]  # Template bytes/pin unchanged.
    assert before["builder_closure"] != after["builder_closure"]
    assert catalog.builds == ["image/z_image_turbo", "image/z_image_turbo"]
    assert changed.events[:2] == [("miss", "key_changed:compiler_identity"),
                                  ("recompute", "key_changed:compiler_identity")]
    assert ("hit", None) not in changed.events
    assert cold.result == warm.result == changed.result


@_OWNER_REQUIRED
def test_public_generate_admits_current_members_on_hit_and_exposes_command_counters(tmp_path, monkeypatch):
    from urllib.parse import urlsplit
    from astrid.core.generation.backends import comfy_target
    from astrid.sdk.command_timing import TimingOutput, print_cache_events
    from astrid.sdk.generation import GenerationFacade
    from tests.core.generation.backends.test_comfy_model_resolution import target_info
    from tests.sdk.test_generation_target import (
        attached_without_managed, storage_client, terminal_result, z_image_only_registry,
    )
    from vibecomfy.registry import ready
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import load_bundle

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    raw = json.dumps(target_info()).encode()
    requests = []

    def opener(request, **kwargs):
        path = urlsplit(request.full_url).path
        requests.append((request.method, path))
        assert request.method == "GET"
        if path == "/object_info":
            return io.BytesIO(raw)
        assert path == "/system_stats"
        return io.BytesIO(b'{"system":{"comfyui_version":"0.26.0"}}')

    monkeypatch.setattr(comfy_target, "open_comfy_http", opener)
    attached_without_managed(monkeypatch)
    z_image_only_registry(monkeypatch)
    runtime, imported = storage_client(store_root=tmp_path / "media")
    source = ready.resolve_ready_template("image/z_image_turbo", ready.repo_ready_template_discovery()).path
    original = source.read_bytes()
    source_digest = "sha256:" + hashlib.sha256(original).hexdigest()
    real_build = ready.workflow_from_ready
    builds, admissions, collectors = [], [], []

    def build(*args, **kwargs):
        assert runtime.media.snapshots[source_digest] == original
        builds.append(args[0])
        return real_build(*args, **kwargs)

    def admit(self, **kwargs):
        path = imported[kwargs["python"]["digest"]]
        bundle = load_bundle(path, trust=Provenance.USER_CONFIRMED)
        bundle.require_canonical_authority("prepared current admission")
        admissions.append({"python": path.read_bytes(),
            "companion": imported[kwargs["companion"]["digest"]].read_bytes(),
            "source": dict(kwargs["source"]), "api": bundle.workflow.compile("api")})
        assert runtime.media.retained_paths[source_digest].read_bytes() == original
        return terminal_result(tmp_path)

    monkeypatch.setattr(ready, "workflow_from_ready", build)
    monkeypatch.setattr(GenerationFacade, "workflow", admit)
    for prompt, seed in (("public café 雪", 41), ("public café 雪", 41), ('changed "prompt"\n雪', 79)):
        collector = TimingCollector()
        with timing_scope(collector):
            result = GenerationFacade().image(placement="http://127.0.0.1:8188", client=runtime,
                project="demo", prompt=prompt, seed=seed)
        assert result.ok
        collectors.append(collector)
    assert builds == ["image/z_image_turbo"]
    assert admissions[0] == admissions[1]
    assert admissions[2]["python"] != admissions[1]["python"]
    assert admissions[2]["source"] == admissions[1]["source"]
    sampler = next(node["inputs"] for node in admissions[2]["api"].values() if node["class_type"] == "KSampler")
    assert sampler["seed"] == 79
    assert admissions[2]["api"][sampler["positive"][0]]["inputs"]["text"] == 'changed "prompt"\n雪'
    assert requests.count(("GET", "/object_info")) == 3
    for collector in collectors[1:]:
        output, human = io.StringIO(), io.StringIO()
        TimingOutput(output, collector).write(json.dumps({"ok": True}))
        print_cache_events(collector.cache_event_snapshot(), file=human)
        assert {"cache": "prepared_template", "outcome": "hit"} in json.loads(output.getvalue())["cache_events"]
        assert "cache prepared_template: hit" in human.getvalue()


_CATALOG_PROCESS = r'''
import hashlib, json, runpy, sys
from pathlib import Path
ns = runpy.run_path(sys.argv[1])
from vibecomfy.registry import ready
real = ready.workflow_from_ready
def observe(*args, **kwargs):
    with Path(sys.argv[4]).open("a") as stream:
        stream.write("build\n")
    return real(*args, **kwargs)
ready.workflow_from_ready = observe
session = ns["prepared"].PreparationSession()
result = ns["_catalog_command"](Path(sys.argv[2]).read_bytes(),
    {"prompt": "process-independent café 雪", "seed": 73}, Path(sys.argv[3]), session=session)
fields = {name: hashlib.sha256(result.result[name]).hexdigest() for name in ("python", "companion", "receipt")}
fields["api"] = hashlib.sha256(json.dumps(result.result["api"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
print(json.dumps({"digests": fields, "events": result.events, "requests": result.requests,
                  "key": session.use.filled.snapshot().key}))
'''


@_OWNER_REQUIRED
def test_two_fresh_catalog_processes_share_store_and_all_current_output_digests(tmp_path):
    from tests.core.generation.backends.test_comfy_model_resolution import target_info
    response, output, marker = tmp_path / "object_info.json", tmp_path / "workflow.py", tmp_path / "builds"
    response.write_text(json.dumps(target_info()))
    env = {**os.environ, "ASTRID_RUNTIME_DATA_ROOT": str(tmp_path / "data"), "PYTHONDONTWRITEBYTECODE": "1"}

    def run():
        result = subprocess.run([sys.executable, "-c", _CATALOG_PROCESS, __file__, str(response), str(output), str(marker)],
                                capture_output=True, text=True, check=True, timeout=120, env=env)
        return json.loads(result.stdout)

    cold, warm = run(), run()
    assert marker.read_text().splitlines() == ["build"]
    assert cold["key"] == warm["key"]
    for name in ("python", "companion", "receipt", "api"):
        assert cold["digests"][name] == warm["digests"][name], (name, cold["digests"], warm["digests"])
    assert ["hit", None] in warm["events"]
    assert len(cold["requests"]) == len(warm["requests"]) == 1
