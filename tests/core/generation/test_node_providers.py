"""Every class a workflow uses has one provider, the worker installs it, and the engine shows it.

Incident: a transcription run on a rented RunPod worker failed twice, after the machine was
claimed, with ``workflow contains unresolved class types: VibeComfySaveTranscript``.  The free
dry run had said ``ready``.  Two gaps:

* nothing resolved the workflow's classes to what the worker is provisioned with, so the dry run
  could not print what the worker would do, or refuse a class nothing provides
  (``node_providers``: core, a locked pack, or VibeComfy's own nodes, read at the recipe's pins);
* the worker linked VibeComfy's node package into ``custom_nodes`` but reused a warm engine that
  had started before the link, and ComfyUI reads ``custom_nodes`` only at start
  (``ManagedEnginePreparation._ensure_engine_classes``).
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import entries as generation_entries
from astrid.core.generation import node_providers
from astrid.core.generation.engine_contracts import IntegrityError
from astrid.core.generation.managed_preparation import ManagedEnginePreparation
from astrid.core.generation.node_providers import (
    NodePlan,
    NodeProviderMissing,
    ProviderSource,
    api_class_types,
    managed_source,
    resolve,
)
from tests import test_generation_entries_invocation_preflight as shipped

OWNED = "VibeComfySaveTranscript"
PIN = "79143f24ee4d990c796558a9e4b18f507c55f925"
BEFORE_THE_NODE = "aee8bd825c619cfb516feb63d38f500aa4ddb4d7"   # parent of the commit that added it


def _entry(entry_id: str, directory: Path):
    row = shipped._shipped_rows()[entry_id]
    values = shipped._with_media_files(shipped._example_values(row), directory, row)
    workflow, _plan, _assets = generation_entries.prepare(
        generation_entries.bundle_path(row), target_client=None, mode=row["modes"][0],
        binds=row.get("binds"), defaults=row.get("defaults"), values=values)
    return api_class_types(workflow), node_providers.declared_packs(workflow)


def _entry_classes(entry_id: str, directory: Path) -> frozenset[str]:
    return _entry(entry_id, directory)[0]


def _h3_classes(name: str) -> set[str]:
    root = Path(generation_entries.__file__).resolve().parents[2] / "packs/h3_av/workflows" / name
    return set(re.findall(r"raw_call\(\s*['\"]([^'\"]+)['\"]", (root / "workflow.py").read_text()))


def test_the_owned_list_and_lock_are_read_at_the_recipes_pin():
    source = managed_source()
    assert source.owned_pin == PIN and source.owned_note is None
    assert OWNED in source.owned_classes
    names = {entry.name for entry in source.lock}
    assert {"ComfyUI-Whisper", "ComfyUI-H3-Motion-Context-MultiRef"} <= names


def test_owned_classes_follow_the_pin_not_the_host_import():
    node_providers._owned_at_pin.cache_clear()
    before, note = node_providers._owned_at_pin(BEFORE_THE_NODE)
    assert note is None and OWNED not in before
    unreadable, note = node_providers._owned_at_pin("0" * 40)
    assert unreadable == frozenset() and "could not be read" in note


def test_transcription_resolves_and_its_plan_links_the_owned_node(tmp_path):
    classes, declared = _entry("transcription", tmp_path)
    plan = resolve(classes, managed_source(), declared)
    plan.require()
    assert plan.providers[OWNED].kind == "owned"
    steps = plan.steps()
    assert steps[0] == "link VibeComfy nodes (pin 79143f24)"
    assert any("ComfyUI-Whisper" in step and "provisioned" in step for step in steps)
    assert steps[-1].startswith("check the engine shows all")


@pytest.mark.parametrize("name", ["native_h3_continuation", "native_h3_continuation_refs"])
def test_native_h3_resolves_through_the_same_resolver(name):
    """No H3 exemption: MiniMaxH3* classes come from the locked, recipe-provisioned h3 pack."""
    classes = _h3_classes(name)
    plan = resolve(classes, managed_source())
    plan.require()
    assert any(c.startswith("MiniMaxH3") for c in classes)
    assert {p.name for p in plan.providers.values() if p.kind == "pack"} >= {"ComfyUI-H3-Motion-Context-MultiRef"}


def test_the_lanpaint_workflow_is_refused_by_name():
    plan = resolve(_h3_classes("lanpaint_h3_av_generalized"), managed_source())
    assert "LanPaint_AVEncode" in plan.refused
    with pytest.raises(NodeProviderMissing, match="LanPaint_AVEncode"):
        plan.require()


def test_a_locked_pack_the_recipe_does_not_install_is_planned_for_install():
    plan = resolve({"LTX2_NAG", "KSampler"}, managed_source())
    plan.require()
    assert [p.describe() for p in plan.packs()] == ["install pack ComfyUI-LTXVideo at commit 229437c6"]
    assert plan.steps()[0].startswith("install pack ComfyUI-LTXVideo")
    assert "restart the engine" in " ".join(plan.steps())


def test_a_declared_pack_provides_the_classes_its_schema_evidence_places_in_it_exactly():
    declared = (("ComfyUI-LTXVideo", "https://github.com/Lightricks/ComfyUI-LTXVideo.git",
                 "229437c6b65796d6a7a63ae34be2bd5ba31fa543"),)
    assert resolve({"LTXVTiledVAEDecode"}, managed_source()).refused       # in no lock class list
    plan = resolve({"LTXVTiledVAEDecode"}, managed_source(), declared)
    plan.require()
    assert plan.providers["LTXVTiledVAEDecode"].describe() == "install pack ComfyUI-LTXVideo at commit 229437c6"
    wrong = (("ComfyUI-LTX", declared[0][1], declared[0][2]),)              # a similar id is not the pack
    assert resolve({"LTXVTiledVAEDecode"}, managed_source(), wrong).refused


def test_a_class_with_no_provider_is_refused_and_nothing_is_guessed():
    plan = resolve({"KSampler", "NoSuchNodeAnywhere", "Apply Whisp"}, managed_source())
    assert set(plan.refused) == {"NoSuchNodeAnywhere", "Apply Whisp"}   # no fuzzy match to "Apply Whisper"


def test_unreadable_owned_list_refuses_owned_classes_by_name_with_the_reason():
    source = ProviderSource(owned_note="its node list at pin 00000000 could not be read (X)")
    plan = resolve({"vibecomfy.exec"}, source)
    assert "could not be read" in plan.refused["vibecomfy.exec"]


def test_the_dry_run_preflight_refuses_a_class_nothing_provides_and_prints_the_plan(tmp_path, monkeypatch):
    from astrid.sdk import generation as sdk_generation

    row = shipped._shipped_rows()["transcription"]
    values = shipped._with_media_files(shipped._example_values(row), tmp_path, row)
    workflow, plan, assets = generation_entries.prepare(
        generation_entries.bundle_path(row), target_client=None, mode=row["modes"][0],
        binds=row.get("binds"), defaults=row.get("defaults"), values=values)
    kwargs = dict(plan=plan, assets=assets or {}, comfy_url=None, lineage=None, source_video=True)

    steps = sdk_generation._offline_invocation_preflight(workflow, worker_nodes=True, **kwargs)
    assert steps[0] == "link VibeComfy nodes (pin 79143f24)"

    real = managed_source()
    monkeypatch.setattr(node_providers, "managed_source",
                        lambda: ProviderSource(lock=real.lock, owned_classes=frozenset(), core_classes=real.core_classes))
    with pytest.raises(Exception) as caught:
        sdk_generation._offline_invocation_preflight(workflow, worker_nodes=True, **kwargs)
    assert OWNED in str(caught.value) and "nothing was sent to a machine" in str(caught.value)
    assert sdk_generation._offline_invocation_preflight(workflow, worker_nodes=False, **kwargs) == []


_IN_SCOPE = [entry for entry in shipped._IN_SCOPE if entry not in shipped._KNOWN_DEFECTS]


def test_the_every_entry_check_is_not_vacuous():
    assert len(_IN_SCOPE) >= 12
    assert "transcription" in _IN_SCOPE


@pytest.mark.parametrize("entry_id", _IN_SCOPE)
def test_every_shipped_entry_resolves_to_providers_on_the_worker(entry_id):
    """A shipped entry with a class nothing provides would fail on a paid worker; it fails here, free."""
    with tempfile.TemporaryDirectory() as directory:
        classes, declared = _entry(entry_id, Path(directory))
    resolve(classes, managed_source(), declared).require()


def test_attached_readiness_uses_the_same_resolver_and_names_owned_nodes(monkeypatch):
    from astrid.core.generation import asset_gaps

    monkeypatch.setattr("vibecomfy.node_packs.resolve_node_packs", lambda classes, *, lockfile_path=None: [])
    readiness = asset_gaps.attached_asset_readiness("transcription", nodes=[OWNED, "CLIPLoader"])
    messages = [issue["message"] for issue in readiness["issues"]]
    assert "VibeComfy's own nodes" in messages[0] and "restart ComfyUI yourself" in messages[0]
    assert "part of ComfyUI itself" in messages[1]
    assert "align" not in str(readiness["fixes"])


# --- the worker side: a warm engine that predates the install ----------------------------------

class _Runner:
    def __init__(self):
        self.step_id = None
        self.health = SimpleNamespace(object_info_sha256="a" * 64)

    def check_health(self):
        return "skipped", {}


def _plan(*names):
    return NodePlan({name: node_providers.Provider("owned", "vibecomfy", commit=PIN) for name in names})


def _preparation(monkeypatch, *, probes, plan=None, start=None):
    """A preparation whose engine answers `probes` (missing-class lists) in order."""
    prep = object.__new__(ManagedEnginePreparation)
    if plan is not False:
        prep._node_plan = _plan(OWNED) if plan is None else plan
    prep._preparing_engine = False
    events, answers = [], list(probes)
    monkeypatch.setattr(prep, "_probe_missing", lambda classes: (events.append(("probe", tuple(classes))),
                                                                 answers.pop(0))[1])
    monkeypatch.setattr(prep, "_stop_preparing_engine", lambda: events.append(("stop",)))
    monkeypatch.setattr(prep, "_start_engine", start or (lambda runner: (events.append(("start",)), ("executed", {"new": 1}))[1]))
    monkeypatch.setattr(prep, "_time_engine", lambda operation: operation())
    return prep, events


def test_a_warm_engine_that_predates_the_install_is_restarted(monkeypatch):
    prep, events = _preparation(monkeypatch, probes=[[OWNED], []])
    assert prep._ensure_engine_classes(_Runner(), "skipped", {"old": 1}) == ("executed", {"new": 1})
    assert [event[0] for event in events] == ["probe", "stop", "start", "probe"]
    assert prep._preparing_engine is True


def test_a_warm_engine_that_already_has_every_class_is_reused(monkeypatch):
    prep, events = _preparation(monkeypatch, probes=[[]])
    assert prep._ensure_engine_classes(_Runner(), "skipped", {"old": 1}) == ("skipped", {"old": 1})
    assert [event[0] for event in events] == ["probe"]


def test_nothing_installed_after_the_start_means_no_restart(monkeypatch):
    """check/trust with no owned link made: a stale engine cannot be cured by a restart."""
    prep, events = _preparation(monkeypatch, probes=[[OWNED]])
    prep._installed_after_start = False
    with pytest.raises(IntegrityError, match=OWNED):
        prep._ensure_engine_classes(_Runner(), "skipped")
    assert [event[0] for event in events] == ["probe"]


def test_a_workflow_without_classes_does_not_probe_or_restart(monkeypatch):
    prep, events = _preparation(monkeypatch, probes=[], plan=NodePlan())
    assert prep._ensure_engine_classes(_Runner(), "skipped")[0] == "skipped"
    assert events == []


def test_an_unresolved_plan_fails_closed(monkeypatch):
    prep, events = _preparation(monkeypatch, probes=[], plan=False)
    with pytest.raises(IntegrityError, match="did not resolve"):
        prep._ensure_engine_classes(_Runner(), "skipped")
    assert events == []


@pytest.mark.parametrize("start_action,probes,stops", [("executed", [[OWNED]], 0), ("skipped", [[OWNED], [OWNED]], 1)])
def test_an_engine_that_still_lacks_a_class_is_refused_by_name(monkeypatch, start_action, probes, stops):
    prep, events = _preparation(monkeypatch, probes=probes)
    with pytest.raises(IntegrityError) as caught:
        prep._ensure_engine_classes(_Runner(), start_action)
    assert OWNED in str(caught.value)
    assert sum(event[0] == "stop" for event in events) == stops


def test_a_start_failure_after_the_restart_stop_is_reported_without_a_second_stop(monkeypatch):
    def failing_start(runner):
        raise RuntimeError("start refused")
    prep, events = _preparation(monkeypatch, probes=[[OWNED]], start=failing_start)
    with pytest.raises(RuntimeError, match="start refused"):
        prep._ensure_engine_classes(_Runner(), "skipped")
    assert [event[0] for event in events] == ["probe", "stop"]
    assert prep._preparing_engine is False and prep._engine_stopped is True


def test_doctor_reports_a_restart_start_error_instead_of_stopping_again(monkeypatch):
    prep, events = _preparation(monkeypatch, probes=[[OWNED]])
    runner = _Runner()
    prep._alignment_runner = runner
    prep._alignment_verification = (None, runner, {}, {}, None)
    prep.recipe = SimpleNamespace(start=SimpleNamespace(port=8188))
    prep.deadline = 1e18
    monkeypatch.setattr(prep, "_verify_alignment", lambda *a: None)
    monkeypatch.setattr(prep, "check", lambda: None)
    calls = iter([("skipped", {}), None])

    def start(r):
        item = next(calls)
        if item is None:
            raise RuntimeError("start refused")
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


def test_every_mode_resolves_links_and_ensures_after_the_engine_is_healthy():
    """check and trust modes skip nodes restore, so `_prepare` itself must cover them."""
    import inspect
    source = inspect.getsource(ManagedEnginePreparation._prepare)
    resolved = source.index("self._node_plan = self._resolve_node_plan(")
    link = source.index("self._link_owned_nodes()")
    start = source.index("self._start_engine(runner)")
    health = source.index("runner.check_health")
    ensure = source.index("self._ensure_engine_classes(")
    assert resolved < link < start < health < ensure
    assert "self.mode" not in source[link - 120:link]


def test_the_lock_written_for_nodes_restore_adds_planned_packs(monkeypatch):
    import inspect
    source = inspect.getsource(ManagedEnginePreparation._prepare)
    assert "self._node_plan.packs()" in source and "_write_managed_nodes_lock(lockfile, declared," in source


def test_doctor_ensures_classes_after_health_and_before_it_reads_the_schema(monkeypatch):
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
    monkeypatch.setattr(prep, "_ensure_engine_classes", lambda r, action, facts=None: (order.append("ensure"), ("executed", {}))[1])
    monkeypatch.setattr(prep, "_native", lambda *a, **k: order.append("doctor") or SimpleNamespace())
    prep._alignment_verification = (None, runner, {}, {}, None)
    prep._doctor(["doctor", "workflow.py"])
    assert order == ["start", "health", "ensure", "doctor", "health"]
