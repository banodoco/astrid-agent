"""A provisioned worker always has every owned VibeComfy node a workflow uses.

Incident: a transcription run on a rented RunPod worker failed twice, after the machine was
claimed, with ``workflow contains unresolved class types: VibeComfySaveTranscript``.  The free
dry run had said ``ready``.  Two gaps, one test class each:

* the free check did not look at what the worker is provisioned with, so it could not refuse a
  class the worker would lack (``WorkerNodeSet``, the dry run, the paid-path preflight);
* the worker linked VibeComfy's node package into ``custom_nodes`` but reused a warm engine that
  had started before the link, and ComfyUI reads ``custom_nodes`` only at start
  (``ManagedEnginePreparation._ensure_owned_nodes``).
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import entries as generation_entries
from astrid.core.generation.engine_contracts import IntegrityError
from astrid.core.generation.managed_preparation import ManagedEnginePreparation
from astrid.core.generation.worker_node_set import (
    WorkerNodeMissing,
    managed_worker_node_set,
    owned_node_classes,
    worker_node_set,
    workflow_class_types,
)
from tests import test_generation_entries_invocation_preflight as shipped

OWNED = "VibeComfySaveTranscript"


def _compile_entry(entry_id: str, directory: Path):
    row = shipped._shipped_rows()[entry_id]
    values = shipped._with_media_files(shipped._example_values(row), directory, row)
    workflow, _plan, _assets = generation_entries.prepare(
        generation_entries.bundle_path(row), target_client=None, mode=row["modes"][0],
        binds=row.get("binds"), defaults=row.get("defaults"), values=values)
    declared = generation_entries.engine_table()["comfyui"].workflow_requirements([workflow])
    return workflow, workflow_class_types(workflow.compile("api", run_inputs={})), declared["extra"]["nodes"]


def _recipe(*, with_vibecomfy: bool):
    """The shipped recipe, optionally without the VibeComfy source the owned nodes are linked from."""
    from astrid.core.generation.engine_local_endpoint import DEFAULT_ENGINE, DEFAULT_PLATFORM
    from astrid.core.generation.engine_recipe import load_recipe

    recipe = load_recipe(DEFAULT_ENGINE, DEFAULT_PLATFORM)
    if with_vibecomfy:
        return recipe
    sources = tuple(source for source in recipe.sources if source.name != "vibecomfy")
    return SimpleNamespace(sources=sources, health=recipe.health)


def test_owned_node_classes_are_read_from_the_package_comfyui_loads():
    assert OWNED in owned_node_classes()


def test_transcription_workflow_is_covered_by_the_worker_recipe(tmp_path):
    """The incident workflow, against the recipe a RunPod worker is provisioned from."""
    _workflow, classes, declared = _compile_entry("transcription", tmp_path)
    assert OWNED in classes, "the transcription entry must use its owned output node"
    managed_worker_node_set(declared).require(classes)


def test_a_worker_recipe_that_cannot_link_the_owned_node_is_refused_by_name(tmp_path):
    _workflow, classes, declared = _compile_entry("transcription", tmp_path)
    node_set = worker_node_set(_recipe(with_vibecomfy=False), declared)
    with pytest.raises(WorkerNodeMissing) as caught:
        node_set.require(classes)
    assert OWNED in caught.value.missing
    assert OWNED in str(caught.value)
    assert "recipe has no 'vibecomfy' source" in str(caught.value)
    assert "before claiming a machine" in str(caught.value)


def test_a_class_nothing_provides_and_an_undeclared_pack_are_refused_by_name():
    node_set = managed_worker_node_set([])
    missing = node_set.unprovided({"KSampler", "NoSuchNodeAnywhere", "LTXVTiledVAEDecode"})
    assert set(missing) == {"NoSuchNodeAnywhere", "LTXVTiledVAEDecode"}
    assert "ComfyUI-LTXVideo" in missing["LTXVTiledVAEDecode"]
    # The same pack, declared by the workflow, is restored on the worker and so is provided.
    declared = [{"repo": "https://github.com/Lightricks/ComfyUI-LTXVideo.git", "commit": "0" * 40}]
    assert set(managed_worker_node_set(declared).unprovided({"LTXVTiledVAEDecode"})) == set()


def test_the_dry_run_preflight_refuses_a_class_the_worker_would_lack(tmp_path, monkeypatch):
    """Same helper the dry run and the paid-path request preflight call, before any claim."""
    from astrid.sdk import generation as sdk_generation

    row = shipped._shipped_rows()["transcription"]
    values = shipped._with_media_files(shipped._example_values(row), tmp_path, row)
    workflow, plan, assets = generation_entries.prepare(
        generation_entries.bundle_path(row), target_client=None, mode=row["modes"][0],
        binds=row.get("binds"), defaults=row.get("defaults"), values=values)
    kwargs = dict(plan=plan, assets=assets or {}, comfy_url=None, lineage=None, source_video=True)

    sdk_generation._offline_invocation_preflight(workflow, worker_nodes=True, **kwargs)

    import astrid.core.generation.engine_recipe as engine_recipe
    without_link = _recipe(with_vibecomfy=False)
    monkeypatch.setattr(engine_recipe, "load_recipe", lambda *args, **kwargs: without_link)
    with pytest.raises(Exception) as caught:
        sdk_generation._offline_invocation_preflight(workflow, worker_nodes=True, **kwargs)
    assert OWNED in str(caught.value)
    assert "nothing was sent to a machine" in str(caught.value)
    # A request that does not target a managed worker is not judged against the recipe.
    sdk_generation._offline_invocation_preflight(workflow, worker_nodes=False, **kwargs)


_IN_SCOPE = [entry for entry in shipped._IN_SCOPE if entry not in shipped._KNOWN_DEFECTS]


def test_the_every_entry_check_is_not_vacuous():
    assert len(_IN_SCOPE) >= 12
    assert "transcription" in _IN_SCOPE


@pytest.mark.parametrize("entry_id", _IN_SCOPE)
def test_every_shipped_entry_is_covered_by_the_worker_node_set(entry_id):
    """A shipped entry whose classes the recipe, its declared packs and the owned nodes do not
    cover would fail on a paid worker; it fails here, free, naming the class."""
    with tempfile.TemporaryDirectory() as directory:
        _workflow, classes, declared = _compile_entry(entry_id, Path(directory))
    managed_worker_node_set(declared).require(classes)


# --- the worker side: a warm engine that predates the custom_nodes link -----------------------

class _Runner:
    def __init__(self):
        self.step_id = None
        self.health = SimpleNamespace(object_info_sha256="a" * 64)

    def check_health(self):
        return "skipped", {}


def _preparation(monkeypatch, *, probes, used=(OWNED,)):
    """A preparation whose engine answers `probes` (missing-class lists) in order."""
    prep = object.__new__(ManagedEnginePreparation)
    nodes = {str(i): SimpleNamespace(class_type=name) for i, name in enumerate(used)}
    prep._alignment_verification = (SimpleNamespace(runtime_nodes=lambda: nodes),)
    prep._preparing_engine = False
    events, answers = [], list(probes)
    monkeypatch.setattr(prep, "_probe_missing", lambda classes: (events.append(("probe", tuple(classes))),
                                                                 answers.pop(0))[1])
    monkeypatch.setattr(prep, "_stop_preparing_engine", lambda: events.append(("stop",)))
    monkeypatch.setattr(prep, "_start_engine", lambda runner: (events.append(("start",)), ("executed", {}))[1])
    monkeypatch.setattr(prep, "_time_engine", lambda operation: operation())
    return prep, events


def test_a_warm_engine_that_predates_the_link_is_restarted_so_it_loads_the_owned_node(monkeypatch):
    prep, events = _preparation(monkeypatch, probes=[[OWNED], []])
    assert prep._ensure_owned_nodes(_Runner(), "skipped") == "executed"
    assert [event[0] for event in events] == ["probe", "stop", "start", "probe"]
    assert prep._preparing_engine is True


def test_a_warm_engine_that_already_has_the_node_is_reused(monkeypatch):
    prep, events = _preparation(monkeypatch, probes=[[]])
    assert prep._ensure_owned_nodes(_Runner(), "skipped") == "skipped"
    assert [event[0] for event in events] == ["probe"]


def test_a_workflow_without_owned_nodes_does_not_probe_or_restart(monkeypatch):
    prep, events = _preparation(monkeypatch, probes=[], used=("KSampler",))
    assert prep._ensure_owned_nodes(_Runner(), "skipped") == "skipped"
    assert events == []


@pytest.mark.parametrize("start_action,probes,stops", [("executed", [[OWNED]], 0), ("skipped", [[OWNED], [OWNED]], 1)])
def test_an_engine_that_still_lacks_the_node_is_refused_by_name(monkeypatch, start_action, probes, stops):
    prep, events = _preparation(monkeypatch, probes=probes)
    with pytest.raises(IntegrityError) as caught:
        prep._ensure_owned_nodes(_Runner(), start_action)
    assert OWNED in str(caught.value)
    assert sum(event[0] == "stop" for event in events) == stops


def test_doctor_ensures_owned_nodes_after_health_and_before_it_reads_the_schema(monkeypatch):
    order = []
    prep, _events = _preparation(monkeypatch, probes=[])
    runner = _Runner()
    prep._alignment_runner = runner
    prep.recipe = SimpleNamespace(start=SimpleNamespace(port=8188))
    prep.deadline = 1e18
    monkeypatch.setattr(prep, "_verify_alignment", lambda *a: None)
    monkeypatch.setattr(prep, "check", lambda: None)
    monkeypatch.setattr(prep, "_start_engine", lambda r: (order.append("start"), ("skipped", {}))[1])
    runner.check_health = lambda: order.append("health")
    monkeypatch.setattr(prep, "_ensure_owned_nodes", lambda r, action: (order.append("ensure"), "executed")[1])
    monkeypatch.setattr(prep, "_native", lambda *a, **k: order.append("doctor") or SimpleNamespace())
    prep._alignment_verification = (None, runner, {}, {}, None)
    prep._doctor(["doctor", "workflow.py"])
    assert order == ["start", "health", "ensure", "doctor", "health"]
