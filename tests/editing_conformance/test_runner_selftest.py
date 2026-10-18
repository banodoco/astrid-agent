"""Proves the harness catches each class of bug. These scenarios are written in the test, not in tests/editing_scenarios."""

from __future__ import annotations

import ast
import copy
import json
import shlex
from types import SimpleNamespace

import pytest
import yaml

from tests.editing_conformance.runner import (
    PathResult,
    ScenarioError,
    classify,
    comparable_records,
    load_scenario,
    parse_scenario,
    run_case,
    run_scenario,
)
from tests.editing_conformance.test_conformance import (
    _authored_restoration_matches,
    _cut_ids,
    _reservation_invariant_error,
    _reservation_state,
)
from tests.editing_conformance.test_conformance import (
    reservation_aware_evaluate as evaluate,
)

ROCKET = {
    "id": "look.set.rocket-x",
    "source": "new",
    "intent": "Move the rocket to x=120 (canvas px).",
    "fixture": "tiny",
    "expect": "change",
    "cli": "edit --clip c1.rocket --set x=120",
    "sheet": {"cuts": ["c1"], "replace": [["x=60", "x=120"]]},
    "python": 'tl.clip("c1.rocket").set(x=120)',
    "check": ['tl.clip("c1.rocket").get("x") == 120'],
}


def _scenario(**overrides):
    data = copy.deepcopy(ROCKET)
    for key, value in overrides.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return parse_scenario(data)


def _failures(scenario):
    return evaluate(scenario, run_scenario(scenario))


def _repo_scenario(scenario_id):
    from tests.editing_conformance.runner import SCENARIOS

    return next(load_scenario(path) for path in SCENARIOS.rglob("*.yaml")
                if path.stem == scenario_id)


def _run_native_input(scenario_id, path, value):
    scenario = _repo_scenario(scenario_id)
    data = yaml.safe_load(scenario.file.read_text(encoding="utf-8"))
    data[path] = value
    return run_case(parse_scenario(data), path)


def test_clean_scenario_has_no_failures_on_all_three_paths():
    scenario = _scenario()
    assert scenario.runnable() == ["cli", "sheet", "python"]
    assert _failures(scenario) == {}


def test_reservation_comparison_still_fails_on_planted_authored_state_loss():
    from tests.editing_conformance.runner import canon

    base = {
        "parent": {"config": {"app": {"astrid": {"author_note": {"keep": True}}}}},
        "shots": {"A": {"internal_timeline": {"clips": [
            {"id": "picture", "app": {"cut": "c1"}, "params": {"x": 60}},
        ]}}},
    }
    after = copy.deepcopy(base)
    after["shots"]["A"]["internal_timeline"]["clips"][0]["params"]["x"] = 120
    reservation = {"number": 2, "suffixes": {}}
    reservation_only_undo = copy.deepcopy(base)
    reservation_only_redo = copy.deepcopy(after)
    for document in (reservation_only_undo, reservation_only_redo):
        document["parent"]["config"]["app"]["astrid"]["cut_id_high_water"] = copy.deepcopy(reservation)

    scenario = _scenario(check=None)
    reservation_only = PathResult(
        name="python", exit_codes=[], stdout="", stderr="", error=None,
        base=canon(base), after=canon(after), undone=canon(reservation_only_undo),
        redone=canon(reservation_only_redo),
        new_steps=1, undo_error=None, redo_error=None, checks={},
        records=[{"calls": [{"op": "clip.set"}]}],
    )
    assert evaluate(scenario, {"python": reservation_only}) == {}
    assert reservation_only.undone == canon(reservation_only_undo)
    assert reservation_only.redone == canon(reservation_only_redo)

    undone = copy.deepcopy(reservation_only_undo)
    del undone["shots"]["A"]["internal_timeline"]["clips"][0]["params"]["x"]
    redone = copy.deepcopy(reservation_only_redo)
    del redone["shots"]["A"]["internal_timeline"]["clips"][0]["params"]["x"]
    result = PathResult(
        name="python", exit_codes=[], stdout="", stderr="", error=None,
        base=canon(base), after=canon(after), undone=canon(undone), redone=canon(redone),
        new_steps=1, undo_error=None, redo_error=None, checks={},
        records=[{"calls": [{"op": "clip.set"}]}],
    )
    failures = evaluate(scenario, {"python": result})
    assert ("python", "undo") in failures
    assert ("python", "redo") in failures
    assert "params" in failures[("python", "undo")]
    assert "params" in failures[("python", "redo")]
    assert ("python", "reservation") not in failures
    assert result.undone == canon(undone) and result.redone == canon(redone)
    assert not _authored_restoration_matches(result.base, result.undone)
    assert not _authored_restoration_matches(result.after, result.redone)


def test_multi_undo_preserves_cut_reservations_and_allocates_a_fresh_identity():
    from astrid.sdk.timeline import Checkout
    from tests.editing_conformance.runner import _checkout, _document, _fresh

    with _fresh("tiny"):
        _checkout()
        base = _document()
        timeline = Checkout.draft("p", "t")
        with timeline.edit("first cut split", check=False, quiet=True):
            first = timeline.cut("c1").split('"went"')

        timeline = Checkout.draft("p", "t")
        with timeline.edit("second cut split", check=False, quiet=True):
            second = timeline.cut("c1a").split('"viral"')
        second_document = _document()
        assert (first.id, second.id) == ("c1a", "c1b")

        timeline = Checkout.draft("p", "t")
        timeline.undo(2)
        undone = _document()
        assert _authored_restoration_matches(base, undone)
        stacked = SimpleNamespace(base=base, after=second_document, undone=undone, redone=undone)
        assert _reservation_invariant_error(stacked) is None

        timeline = Checkout.draft("p", "t")
        with timeline.edit("allocate after multi-undo", check=False, quiet=True):
            next_cut = timeline.cut("c1").split('"went"')
        final = _document()
        assert next_cut.id == "c1c"
        final_result = SimpleNamespace(base=undone, after=final, undone=final, redone=final)
        assert _reservation_invariant_error(final_result) is None
        assert _reservation_state(second_document)["suffixes"]["1"] == 2
        assert _reservation_state(undone)["suffixes"]["1"] == 2
        assert _reservation_state(final)["suffixes"]["1"] == 3
        assert {"c1a", "c1b"}.isdisjoint(_cut_ids(final))


def test_python_setting_a_different_value_is_caught_as_a_mismatch():
    scenario = _scenario(python='tl.clip("c1.rocket").set(x=90)', check=None)
    failures = _failures(scenario)
    assert ("cli", "equal") in failures and ("sheet", "equal") in failures
    assert "-" in failures[("cli", "equal")] and "x" in failures[("cli", "equal")]
    assert not [k for k in failures if k[0] == "python"]


def test_silent_no_op_is_caught():
    scenario = _scenario(python='tl.clip("c1.rocket")', check=None)
    assert ("python", "changes") in _failures(scenario)


def test_undo_granularity_two_python_edits_in_one_save_is_caught():
    scenario = _scenario(
        python='tl.clip("c1.rocket").set(x=90)\ntl.clip("c1.rocket").set(x=120)', check=None)
    failures = _failures(scenario)
    assert ("python", "steps") not in failures  # one save is one undo step (AUDIT bug 10, fixed)
    assert ("python", "undo") not in failures  # undoing that one step restores the base


def test_refusal_parity_on_an_unknown_key():
    scenario = _scenario(
        expect="refused",
        cli="edit --clip c1.rocket --set zzbogus=2",
        sheet={"cuts": ["c1"], "replace": [["x=60", "x=60 zzbogus=2"]]},
        python='tl.clip("c1.rocket").set(zzbogus=2)',
        check=None,
    )
    assert _failures(scenario) == {}


@pytest.mark.parametrize("path", ["cli", "sheet"])
@pytest.mark.parametrize("field,value", [("label", "different persisted history"),
                                         ("calls", [{"op": "altered.call"}])])
def test_refusal_journals_are_compared_across_actual_boundaries(path, field, value):
    scenario = _repo_scenario("look.set.unknown-key")
    results = run_scenario(scenario)
    assert evaluate(scenario, results) == {}
    assert results["sheet"].boundary != results["python"].boundary
    assert all(result.records for result in results.values())
    changed = copy.deepcopy(results)
    changed[path].records[-1][field] = value
    changed[path].records_before[-1][field] = value
    assert (path, "journal") in evaluate(scenario, changed)


def test_implicit_refusal_rejects_an_extra_rollback_row():
    scenario = _repo_scenario("look.set.unknown-key")
    results = run_scenario(scenario)
    results["cli"].records.append({"kind": "edit", "outcome": "rolled-back", "error": "refused"})
    failures = evaluate(scenario, results)
    assert ("cli", "atomicity") in failures and ("cli", "journal") in failures


def test_refusal_cannot_pass_with_equally_empty_journals():
    scenario = _repo_scenario("look.set.unknown-key")
    results = run_scenario(scenario)
    for result in results.values():
        result.records = []
        result.records_before = []
    failures = evaluate(scenario, results)
    assert all((path, "journal") in failures for path in results)


@pytest.mark.parametrize("field", ["document", "journal", "history", "redo"])
def test_implicit_refusal_requires_all_persisted_bytes_unchanged(field):
    scenario = _repo_scenario("look.set.unknown-key")
    results = run_scenario(scenario)
    assert evaluate(scenario, results) == {}
    for path in results:
        changed = copy.deepcopy(results)
        changed[path].persistence_after[field] = b"corrupted persisted bytes"
        assert (path, "atomicity") in evaluate(scenario, changed)


def test_extension_refusal_retains_explicit_edit_rollback_audit():
    scenario = _repo_scenario("timing.fit-to-beats.refused")
    result = run_scenario(scenario)
    assert evaluate(scenario, result) == {}
    extension = result["extension"]
    assert extension.boundary == "transactional-edit"
    assert len(extension.records) == len(extension.records_before) + 1
    assert extension.records[-1]["outcome"] == "rolled-back"


def test_unexpected_crash_does_not_count_as_a_typed_refusal():
    from tests.editing_conformance.runner import PathResult

    scenario = _scenario(expect="refused", check=None)
    crashed = PathResult(
        name="cli", exit_codes=[1], stdout="", stderr="", error="RuntimeError: process crashed",
        base="unchanged", after="unchanged", undone="unchanged", redone="unchanged",
        new_steps=0, undo_error=None, redo_error=None, checks={},
    )
    failures = evaluate(scenario, {"cli": crashed})
    assert ("cli", "runs") in failures
    assert "typed refusal" in failures[("cli", "runs")]


def test_journal_comparison_rejects_guard_argument_binding_authorship_identity_and_cause_changes():
    scenario = _repo_scenario("look.add.sprite")
    results = run_scenario(scenario)
    assert evaluate(scenario, results) == {}

    def edit_call(records, call_index, mutate):
        row = next(row for row in records if row.get("calls"))
        mutate(row["calls"][call_index])

    # Keep the guard replacement and other mutations explicit so each planted
    # control names the exact loss it must detect.
    mutations = {
        "guard": lambda call: call["expect"][""].update(state="present"),
        "arguments": lambda call: call["args"]["params"].update(x=601),
        "binding": lambda call: call["bindings"].update({'"now"': "word:s2:2"}),
        "authorship": lambda call: call["authorship"].update({"": "consequence"}),
        "created identity": lambda call: call["created"].__setitem__(0, "occurrence:occ-B/clip:other"),
        "causal attribution": lambda call: call["authorship"].update({"hold": "direct"}),
    }
    for label, mutation in mutations.items():
        altered = copy.deepcopy(results)
        index = 1 if label == "causal attribution" else 0
        edit_call(altered["cli"].records, index, mutation)
        failures = evaluate(scenario, altered)
        assert ("cli", "journal") in failures, label
        assert ("cli", "equal") not in failures, label


def test_replaced_native_inputs_remain_behavioral_and_journal_inequality_controls():
    # The native one-set route still reaches x=120; its trace remains distinct
    # from the retained Python x=90 -> 120 regression.
    one_set = _scenario()
    one_set_results = run_scenario(one_set)
    two_sets = _repo_scenario("code.python.two-sets-one-save")
    two_sets_python = run_case(two_sets, "python")
    assert one_set_results["cli"].after == two_sets_python.after
    assert one_set_results["sheet"].after == two_sets_python.after
    assert comparable_records(one_set_results["cli"].records) != comparable_records(two_sets_python.records)
    assert comparable_records(one_set_results["sheet"].records) != comparable_records(two_sets_python.records)

    controls = [
        # Original add-only Python/sheet inputs; native CLI add -> trim remains matched.
        ("audio.add.sfx-viral", "python",
         'tl.add("media", at=\'"viral"\', hold=0.3, asset="Q", track="vo", cut=None, clip_id="blip")',
         "audio.add.sfx-viral", "cli"),
        ("audio.add.sfx-viral", "sheet",
         {"text": 'edit {"op": "timeline.add", "select": null, "args": {"element": "media", "asset": "Q", "track": "vo", "at": "\\"viral\\"", "hold": 0.3, "cut": null, "clip_id": "blip"}}'},
         "audio.add.sfx-viral", "cli"),
        # Original one-command add-with-asset CLI input for the add -> swap case.
        ("look.add-then-swap.sprite", "cli",
         'edit --command \'{"op":"timeline.add","select":null,"args":{"element":"am-sprite","at":"\\"now\\"","layer":"pin","hold":1.5,"params":{"x":600,"y":300},"asset":"R"}}\'',
         "look.add-then-swap.sprite", "python"),
        # The old sheet add-with-asset row is retained by look.add.sprite, as a separate scenario.
        ("look.add.sprite", "sheet", None, "look.add-then-swap.sprite", "python"),
        # The native bare Live spelling still resolves, with a different binding key.
        ("structure.close-gap.live", "cli", "edit --close-gap-before Live",
         "structure.close-gap.live", "python"),
        # Immediate reflow remains an explicit behavioral control beside staged reflow.
        ("structure.gap-after.s1", "python", 'tl.voice("s1").set_gap_after(0.8)',
         "structure.gap-after.s1", "cli"),
        # Original implicit-ownership, inline-on/hold label producer.
        ("look.add.label", "cli",
         'edit --command \'{"op":"timeline.add","select":null,"args":{"element":"am-type","at":"\\"now\\"","layer":"beta","hold":1.5,"params":{"text":"beta","x":96,"y":84}}}\'',
         "look.add.label", "sheet"),
        ("look.add.label", "python",
         'tl.add("am-type", at=\'"now"\', layer="beta", hold=1.5, params={"text": "beta", "x": 96, "y": 84})',
         "look.add.label", "sheet"),
        # Original implicit-ownership, inline-on/hold sprite producer.
        ("look.add.sprite", "cli",
         'edit --command \'{"op":"timeline.add","select":null,"args":{"element":"am-sprite","at":"\\"now\\"","layer":"pin","hold":1.5,"asset":"R","params":{"x":600,"y":300}}}\'',
         "look.add.sprite", "sheet"),
        ("look.add.sprite", "python",
         'tl.add("am-sprite", at=\'"now"\', layer="pin", hold=1.5, asset="R", params={"x": 600, "y": 300})',
         "look.add.sprite", "sheet"),
    ]
    for scenario_id, path, original_input, matched_scenario_id, matched_path in controls:
        scenario = _repo_scenario(scenario_id)
        if original_input is None:
            native = run_case(scenario, path)
        else:
            native = _run_native_input(scenario_id, path, original_input)
        matched = run_case(_repo_scenario(matched_scenario_id), matched_path)
        assert native.error is None, f"{scenario_id}/{path}: {native.error}"
        assert native.after == matched.after, f"{scenario_id}/{path}: authored behavior changed"
        assert comparable_records(native.records) != comparable_records(matched.records), \
            f"{scenario_id}/{path}: original execution trace was not retained as distinct"


def test_a_replace_that_is_not_in_the_sheet_is_a_scenario_error():
    scenario = _scenario(sheet={"cuts": ["c1"], "replace": [["no-such-text", "x"]]})
    with pytest.raises(ScenarioError, match="exactly once"):
        run_scenario(scenario)


def test_an_xfail_with_an_unknown_check_name_is_a_scenario_error():
    data = copy.deepcopy(ROCKET)
    data["xfail"] = [{"path": "python", "check": "stepz", "reason": "typo"}]
    with pytest.raises(ScenarioError, match="stepz"):
        parse_scenario(data)


@pytest.mark.parametrize("overrides", [
    {"expect": "maybe"},
    {"colour": "red"},
    {"id": "look-set-rocket"},
    {"cli": None, "sheet": None, "python": None},
    {"xfail": [{"path": "cli", "check": "steps", "reason": "r"}], "cli": None},
    {"xfail": [{"path": "python", "check": "steps"}]},
])
def test_other_malformed_scenarios_are_scenario_errors(overrides):
    with pytest.raises(ScenarioError):
        _scenario(**overrides)


@pytest.mark.parametrize("path", ["cli", "sheet", "python"])
@pytest.mark.parametrize("mode", ["omitted", "missing"])
def test_required_frontend_cannot_be_omitted_or_declared_missing(path, mode):
    data = copy.deepcopy(ROCKET)
    if mode == "omitted":
        data.pop(path)
    else:
        data[path] = {"missing": "AUDIT: no runnable route"}
    with pytest.raises(ScenarioError, match="requires runnable front-ends|missing paths cannot stand in"):
        parse_scenario(data)


def test_exists_xfail_cannot_stand_in_for_a_required_frontend():
    data = copy.deepcopy(ROCKET)
    data["xfail"] = [{"path": "cli", "check": "exists", "reason": "AUDIT: no CLI"}]
    with pytest.raises(ScenarioError, match="'exists' cannot be xfailed"):
        parse_scenario(data)


def test_yaml_text_round_trips_through_the_loader(tmp_path):
    text = yaml.safe_dump(ROCKET, sort_keys=False)
    path = tmp_path / "look.set.rocket-x.yaml"
    path.write_text(text, encoding="utf-8")
    assert load_scenario(path).paths["sheet"].replace == (("x=60", "x=120"),)
    (tmp_path / "other.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ScenarioError, match="must match the file name"):
        load_scenario(tmp_path / "other.yaml")


def test_classify_splits_unexpected_expected_and_strict_fixed():
    data = copy.deepcopy(ROCKET)
    data["xfail"] = [
        {"path": "python", "check": "steps", "reason": "AUDIT bug 10"},
        {"path": "cli", "check": "undo", "reason": "stale: passes today"},
    ]
    scenario = parse_scenario(data)
    failures = {("python", "steps"): "2 new", ("sheet", "changes"): "silent"}
    unexpected, expected, fixed = classify(scenario, failures)
    assert unexpected == [("sheet", "changes")]
    assert expected == [("python", "steps")]
    assert fixed == [("cli", "undo")]


def test_exit_code_3_is_a_run_for_no_change_but_a_refusal_for_change():
    from tests.editing_conformance.runner import PathResult, _refused

    def result(codes):
        return PathResult(name="cli", exit_codes=codes, stdout="no change", stderr="", error=None,
                          base="b", after="b", undone="b", redone="b", new_steps=0,
                          undo_error=None, redo_error=None, checks={})

    scenario = _scenario(expect="no-change", check=None)
    failures = evaluate(scenario, {"cli": result([3])})
    assert not [k for k in failures if k == ("cli", "runs")]
    assert not _refused(result([3]), "no-change")
    assert _refused(result([3]), "change")  # for change, only 0 is a run


def _no_change_plan(**overrides):
    from astrid.sdk.timeline.transaction import Plan

    return json.dumps({**Plan("clip.set").to_json(), **overrides})


def _report_result(**overrides):
    values = dict(name="cli", exit_codes=[3], stdout=_no_change_plan(), stderr="", error=None,
                  base="{}", after="{}", undone="{}", redone="{}", new_steps=0,
                  undo_error=None, redo_error=None, checks={})
    return PathResult(**{**values, **overrides})


@pytest.mark.parametrize("stdout", [
    _no_change_plan(), _no_change_plan(message="no change (the working copy already matches)"),
    _no_change_plan() + "\nno change\n", "no change", "no change: clip.set",
])
def test_no_change_report_accepts_supported_json_and_legacy_text(stdout):
    result = _report_result(stdout=stdout)
    assert evaluate(_scenario(expect="no-change", check=None), {"cli": result}) == {}


@pytest.mark.parametrize("stdout", [
    '{"changed": false, "message": "no change"',
    _no_change_plan() + "\nmalformed no change",
    json.dumps({"changed": False}), json.dumps({"message": "no change"}),
    json.dumps("no change"), json.dumps([{"changed": False}]),
    _no_change_plan(changed=True, message="no change"), _no_change_plan(changed=0),
    _no_change_plan(calls=None), _no_change_plan(error="refused", message="no change"),
])
def test_no_change_report_rejects_malformed_or_unsupported_json(stdout):
    failures = evaluate(_scenario(expect="no-change", check=None), {"cli": _report_result(stdout=stdout)})
    assert ("cli", "reports") in failures


@pytest.mark.parametrize("overrides,check", [
    ({"exit_codes": [1]}, "runs"),
    ({"error": "refused:CommandError: no change is permitted"}, "runs"),
    ({"error": "RuntimeError: crashed"}, "runs"),
    ({"after": '{"x":1}'}, "changes"),
    ({"new_steps": 1}, "steps"),
])
def test_no_change_report_cannot_hide_failure_or_document_or_undo_changes(overrides, check):
    failures = evaluate(_scenario(expect="no-change", check=None), {"cli": _report_result(**overrides)})
    assert ("cli", check) in failures
    if "after" in overrides:
        assert ("cli", "reports") in failures  # a false no-change claim remains a reporting failure


@pytest.mark.parametrize("second_stdout,second_error,second_changed,second_steps,second_exits,passes", [
    (_no_change_plan(), None, False, 0, [3], True),
    ("no change", None, False, 0, [0], True),
    ('{"changed":false,"message":"no change"', None, False, 0, [3], False),
    (_no_change_plan(changed=True), None, False, 0, [3], False),
    (_no_change_plan(), "refused:CommandError: refused", False, 0, [3], False),
    (_no_change_plan(), "RuntimeError: crashed", False, 0, [3], False),
    (_no_change_plan(), None, True, 0, [3], False),
    (_no_change_plan(), None, False, 1, [3], False),
    (_no_change_plan(), None, False, 0, [1], False),
])
def test_no_change_report_uses_same_strict_predicate_for_idempotent_runs(
        second_stdout, second_error, second_changed, second_steps, second_exits, passes):
    result = _report_result(
        exit_codes=[0], stdout="edited working copy", after='{"x":1}', redone='{"x":1}',
        new_steps=1, records=[{"calls": [{"op": "clip.set"}]}],
        second_stdout=second_stdout, second_error=second_error, second_changed=second_changed,
        second_steps=second_steps, second_exits=second_exits)
    failures = evaluate(_scenario(idempotent=True, check=None), {"cli": result})
    assert (("cli", "idempotent") not in failures) is passes
    if passes:
        assert failures == {}


def test_tool_key_is_stored_and_validated():
    assert _scenario().tool is None
    assert _scenario(tool="timeline.opacity").tool == "timeline.opacity"
    with pytest.raises(ScenarioError, match="'tool'"):
        _scenario(tool="  ")


def test_fixture_lookup_names_both_places_when_missing():
    with pytest.raises(ScenarioError, match="editing_scenarios/fixtures"):
        _scenario(fixture="no-such-fixture")


def test_fixture_task_handle_uses_managed_output_and_project_media_rows():
    from astrid.sdk import AstridClient
    from astrid.sdk.media_handles import resolve_media_handle
    from astrid.sdk.timeline import store
    from tests.editing_conformance.runner import _fresh, fixture_path

    digest = "sha256:" + "d" * 64
    handle = "task:replace-fixture/sprites#0"
    media_fixture = json.loads(fixture_path("replace-asset").read_text(encoding="utf-8"))[
        "media_handle_resolver"
    ]
    output = media_fixture["managed_outputs"][0]
    media = media_fixture["project_media"][0]
    assert output["media_id"] == media["media_id"]
    assert output["digest"] == media["digest"] == output["object_id"] == media["object_id"] == digest

    with _fresh("replace-asset"):
        with AstridClient.open_from_launcher(start_pack_host=False) as client:
            descriptor, lineage = resolve_media_handle(client, "p", handle)
            registry_entry = store.resolve_handle_entry("p", handle, client=client)

    assert descriptor == {
        "digest": digest,
        "filename": "replacement.png",
        "media_type": "image/png",
        "size_bytes": 4096,
    }
    assert lineage == {
        "handle": handle,
        "digest": digest,
        "output": {
            "task_id": "replace-fixture",
            "output_port": "sprites",
            "ordinal": 0,
        },
    }
    assert registry_entry == {
        "key": "replacement",
        "media_id": digest,
        "content_sha256": digest,
        "type": "image/png",
        "handle": handle,
        "filename": "replacement.png",
    }


def test_run_case_rejects_an_unknown_front_end():
    with pytest.raises(ScenarioError, match="via must be"):
        run_case(_scenario(), "gui")


def test_idempotent_scenario_passes_when_the_second_run_is_a_no_change():
    scenario = _scenario(idempotent=True)
    results = run_scenario(scenario)
    assert _failures(scenario) == {}
    for name, r in results.items():
        assert r.second_changed is False and r.second_steps == 0, name
        if name != "python":
            assert "no change" in r.second_stdout, name


def test_idempotent_nudge_is_caught_on_the_second_run():
    scenario = _scenario(idempotent=True, python='tl.clip("c1.rocket").nudge(frames=1)', check=None)
    results = run_scenario(scenario)
    failures = evaluate(scenario, results)
    assert ("python", "idempotent") in failures
    assert "changed the document" in failures[("python", "idempotent")]
    assert results["python"].new_steps == 1  # the first run's step is still the one the undo/redo checks see
    assert ("python", "undo") not in failures and ("python", "redo") not in failures


def test_idempotent_is_only_for_expect_change():
    with pytest.raises(ScenarioError, match="idempotent applies only to expect: change"):
        _scenario(expect="no-change", idempotent=True, check=None)


def test_idempotent_must_be_a_bool():
    with pytest.raises(ScenarioError, match="true or false"):
        _scenario(idempotent="yes")


def test_journal_comparison_does_not_hide_calls_labels_or_undo_identity():
    scenario = _scenario()
    results = run_scenario(scenario)
    assert evaluate(scenario, results) == {}
    for field in ("calls", "label", "undo_step"):
        changed = copy.deepcopy(results)
        changed["cli"].records[-1][field] = {"tampered": True}
        failures = evaluate(scenario, changed)
        assert ("cli", "journal") in failures
        assert ("cli", "equal") not in failures  # the documents still agree


def test_repository_scenarios_have_the_expected_frontends():
    from tests.editing_conformance.runner import SCENARIOS
    scenarios = [load_scenario(p) for p in SCENARIOS.rglob("*.yaml") if p.parent.name != "extensions"]
    assert len(scenarios) == 62
    for scenario in scenarios:
        assert scenario.present() == scenario.runnable() == ["cli", "sheet", "python"]


def test_operation_scenarios_have_a_real_extension_path():
    from tests.editing_conformance.runner import SCENARIOS
    scenarios = [load_scenario(p) for p in (SCENARIOS / "extensions").glob("*.yaml")]
    assert {s.id for s in scenarios} == {
        "timing.fit-to-beats.overlays", "timing.fit-to-beats.empty", "timing.fit-to-beats.refused",
    }
    for scenario in scenarios:
        assert scenario.present() == scenario.runnable() == ["cli", "sheet", "python", "extension"]


def test_fit_to_beats_frontends_share_the_declared_operation_pin():
    from astrid.sdk.timeline_sheet import parse_sheet
    from astrid.sdk.timeline.tools import resolve_operation
    from tests.editing_conformance.runner import REPO, SCENARIOS, _operation_scope

    scenarios = [load_scenario(path) for path in (SCENARIOS / "extensions").glob("*.yaml")]
    for scenario in scenarios:
        extension = scenario.paths["extension"]
        expected = {
            "op": "timeline.operation",
            "select": None,
            "args": {
                "identity": extension.operation,
                "implementation_digest": extension.implementation_digest,
                "inputs": extension.inputs,
            },
        }
        command = shlex.split(extension_command := scenario.paths["cli"].commands[0])
        assert command[:2] == ["edit", "--command"], extension_command
        assert json.loads(command[2]) == expected
        sheet_command = parse_sheet(scenario.paths["sheet"].text)["commands"][0]
        assert {key: sheet_command[key] for key in expected} == expected

        python_tree = ast.parse(scenario.paths["python"].snippet)
        run_call = next(node for node in ast.walk(python_tree)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "run")
        assert ast.literal_eval(run_call.args[1]) == expected

        with _operation_scope(scenario):
            operation = resolve_operation(extension.operation)
        assert operation.implementation_digest == extension.implementation_digest
        assert operation.pack_root == (REPO / "examples/packs/beat_tools").resolve()


def test_sheet_transaction_boundary_requires_one_verified_declared_operation(monkeypatch):
    from astrid.sdk import timeline
    from tests.editing_conformance.runner import (
        _operation_scope,
        _sheet_transaction_boundary,
    )

    scenario = _repo_scenario("timing.fit-to-beats.refused")
    extension = scenario.paths["extension"]
    text = scenario.paths["sheet"].text
    assert text is not None

    with _operation_scope(scenario):
        assert _sheet_transaction_boundary(scenario, text) == "transactional-edit"
        assert _sheet_transaction_boundary(scenario, text + text) == "file-application"
        assert _sheet_transaction_boundary(scenario, "edit {malformed") == "file-application"
        stale = text.replace(extension.implementation_digest, "0" * 64)
        assert _sheet_transaction_boundary(scenario, stale) == "file-application"

    # A syntactically valid operation call still stays on file semantics when it has no declaration.
    assert _sheet_transaction_boundary(_scenario(), text) == "file-application"

    def unresolved(*_args, **_kwargs):
        raise RuntimeError("operation is unresolved")

    monkeypatch.setattr(timeline.tools, "resolve_operation", unresolved)
    with _operation_scope(scenario):
        assert _sheet_transaction_boundary(scenario, text) == "file-application"


@pytest.mark.parametrize("value", ["not-a-spec", {}, {"operation": "fit_to_beats", "pack_roots": ["examples/packs"], "inputs": {}},
    {"operation": "timeline.operation/p.name", "pack_roots": [], "inputs": {}},
    {"operation": "timeline.operation/p.name", "pack_roots": ["examples/packs"], "inputs": []}])
def test_extension_adapter_rejects_malformed_specs(value):
    with pytest.raises(ScenarioError, match="extension"):
        _scenario(extension=value)
