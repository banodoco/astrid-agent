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


def _h3_classes(name: str) -> set[str]:
    import re
    root = Path(generation_entries.__file__).resolve().parents[2] / "packs/h3_av/workflows" / name
    return set(re.findall(r"raw_call\(\s*['\"]([^'\"]+)['\"]", (root / "workflow.py").read_text()))


@pytest.mark.parametrize("name", ["native_h3_continuation", "native_h3_continuation_refs"])
def test_native_h3_classes_come_from_the_pack_the_recipe_installs(name):
    """MiniMaxH3* classes are in no core schema; the recipe pins h3-motion-context, whose
    classes the shipped capture records at that exact commit."""
    classes = _h3_classes(name)
    assert any(c.startswith("MiniMaxH3") for c in classes)
    assert managed_worker_node_set([]).unprovided(classes) == {}


def test_the_lanpaint_h3_workflow_is_not_provided_by_the_managed_worker():
    """LanPaint is in no recipe pin and no declaration; the check says so by pack name."""
    missing = managed_worker_node_set([]).unprovided(_h3_classes("lanpaint_h3_av_generalized"))
    assert "LanPaint_AVEncode" in missing and "'LanPaint'" in missing["LanPaint_AVEncode"]


def test_pack_ids_and_commits_are_matched_exactly(tmp_path):
    from astrid.core.generation import worker_node_set as module
    # Whisper's class is provided by the pack id "ComfyUI-Whisper" at the recipe pin, not by a
    # similar-looking pack, a substring, or the same pack at another commit.
    assert module._in_pack("Apply Whisper", "ComfyUI-Whisper", "006a709848259ed3d69e2f9fcfd7a75f6c860ddd", exact_commit=True)
    assert not module._in_pack("Apply Whisper", "Whisper", "006a709848259ed3d69e2f9fcfd7a75f6c860ddd", exact_commit=True)
    assert not module._in_pack("Apply Whisper", "ComfyUI-Whisper", "0" * 40, exact_commit=True)
    assert not module._in_pack("Apply Whisper", "ComfyUI-Whisper-Extra", "0" * 40, exact_commit=False)


# --- the worker side: a warm engine that predates the custom_nodes link -----------------------

class _Runner:
    def __init__(self):
        self.step_id = None
        self.health = SimpleNamespace(object_info_sha256="a" * 64)

    def check_health(self):
        return "skipped", {}


def _preparation(monkeypatch, *, probes, owned=(OWNED,), start=None):
    """A preparation whose engine answers `probes` (missing-class lists) in order."""
    prep = object.__new__(ManagedEnginePreparation)
    if owned is not None:
        prep._owned_classes = tuple(owned)
    prep._preparing_engine = False
    events, answers = [], list(probes)
    monkeypatch.setattr(prep, "_probe_missing", lambda classes: (events.append(("probe", tuple(classes))),
                                                                 answers.pop(0))[1])
    monkeypatch.setattr(prep, "_stop_preparing_engine", lambda: events.append(("stop",)))
    monkeypatch.setattr(prep, "_start_engine", start or (lambda runner: (events.append(("start",)), ("executed", {"new": 1}))[1]))
    monkeypatch.setattr(prep, "_time_engine", lambda operation: operation())
    return prep, events


def test_a_warm_engine_that_predates_the_link_is_restarted_so_it_loads_the_owned_node(monkeypatch):
    prep, events = _preparation(monkeypatch, probes=[[OWNED], []])
    assert prep._ensure_owned_nodes(_Runner(), "skipped", {"old": 1}) == ("executed", {"new": 1})
    assert [event[0] for event in events] == ["probe", "stop", "start", "probe"]
    assert prep._preparing_engine is True


def test_a_warm_engine_that_already_has_the_node_is_reused(monkeypatch):
    prep, events = _preparation(monkeypatch, probes=[[]])
    assert prep._ensure_owned_nodes(_Runner(), "skipped", {"old": 1}) == ("skipped", {"old": 1})
    assert [event[0] for event in events] == ["probe"]


def test_a_workflow_without_owned_nodes_does_not_probe_or_restart(monkeypatch):
    prep, events = _preparation(monkeypatch, probes=[], owned=())
    assert prep._ensure_owned_nodes(_Runner(), "skipped")[0] == "skipped"
    assert events == []


def test_unknown_owned_classes_fail_closed(monkeypatch):
    prep, events = _preparation(monkeypatch, probes=[], owned=None)
    with pytest.raises(IntegrityError, match="did not determine"):
        prep._ensure_owned_nodes(_Runner(), "skipped")
    assert events == []


@pytest.mark.parametrize("start_action,probes,stops", [("executed", [[OWNED]], 0), ("skipped", [[OWNED], [OWNED]], 1)])
def test_an_engine_that_still_lacks_the_node_is_refused_by_name(monkeypatch, start_action, probes, stops):
    prep, events = _preparation(monkeypatch, probes=probes)
    with pytest.raises(IntegrityError) as caught:
        prep._ensure_owned_nodes(_Runner(), start_action)
    assert OWNED in str(caught.value)
    assert sum(event[0] == "stop" for event in events) == stops


def test_a_start_failure_after_the_restart_stop_is_reported_without_a_second_stop(monkeypatch):
    def failing_start(runner):
        raise RuntimeError("start refused")
    prep, events = _preparation(monkeypatch, probes=[[OWNED]], start=failing_start)
    with pytest.raises(RuntimeError, match="start refused"):
        prep._ensure_owned_nodes(_Runner(), "skipped")
    assert [event[0] for event in events] == ["probe", "stop"]
    assert prep._preparing_engine is False and prep._engine_stopped is True


def test_doctor_reports_a_restart_start_error_instead_of_stopping_again(monkeypatch):
    prep, events = _preparation(monkeypatch, probes=[[OWNED]], start=lambda r: (_ for _ in ()).throw(RuntimeError("start refused")))
    runner = _Runner()
    prep._alignment_runner = runner
    prep._alignment_verification = (None, runner, {}, {}, None)
    prep.recipe = SimpleNamespace(start=SimpleNamespace(port=8188))
    prep.deadline = 1e18
    started = []
    monkeypatch.setattr(prep, "_verify_alignment", lambda *a: None)
    monkeypatch.setattr(prep, "check", lambda: None)
    # first start (the warm engine) succeeds, the restart start fails
    calls = iter([("skipped", {}), None])
    def start(r):
        item = next(calls)
        if item is None:
            raise RuntimeError("start refused")
        started.append(item)
        return item
    monkeypatch.setattr(prep, "_start_engine", start)
    with pytest.raises(RuntimeError, match="start refused"):
        prep._doctor(["doctor", "workflow.py"])
    assert [e[0] for e in events].count("stop") == 1


def test_a_transient_probe_error_does_not_cost_a_healthy_engine_its_life(monkeypatch):
    prep = object.__new__(ManagedEnginePreparation)
    prep.recipe = SimpleNamespace(start=SimpleNamespace(port=8188))
    prep.deadline = 1e18
    outcomes = [IntegrityError("transient"), SimpleNamespace(stdout='{"missing": []}')]
    def native(*args, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(prep, "_native", native)
    assert prep._probe_missing([OWNED]) == []
    outcomes[:] = [IntegrityError("down"), IntegrityError("still down")]
    with pytest.raises(IntegrityError, match="still down"):
        prep._probe_missing([OWNED])


def test_every_mode_links_probes_and_ensures_after_the_engine_is_healthy():
    """check and trust modes skip nodes restore, so `_prepare` itself must cover them."""
    import inspect
    source = inspect.getsource(ManagedEnginePreparation._prepare)
    link = source.index("self._link_owned_nodes()")
    start = source.index("self._start_engine(runner)")
    health = source.index("runner.check_health")
    ensure = source.index("self._ensure_owned_nodes(")
    assert link < start < health < ensure
    between = source[link - 200:ensure]
    assert "self.mode" not in between.split("self._link_owned_nodes()")[0][-120:]
    assert "self._owned_classes = self._owned_classes_in(" in source


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
    monkeypatch.setattr(prep, "_ensure_owned_nodes", lambda r, action, facts=None: (order.append("ensure"), ("executed", {}))[1])
    monkeypatch.setattr(prep, "_native", lambda *a, **k: order.append("doctor") or SimpleNamespace())
    prep._alignment_verification = (None, runner, {}, {}, None)
    prep._doctor(["doctor", "workflow.py"])
    assert order == ["start", "health", "ensure", "doctor", "health"]
