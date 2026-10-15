"""Proves the harness catches each class of bug. These scenarios are written in the test, not in tests/editing_scenarios."""

from __future__ import annotations

import copy

import pytest
import yaml

from tests.editing_conformance.runner import (
    ScenarioError,
    classify,
    evaluate,
    load_scenario,
    parse_scenario,
    run_case,
    run_scenario,
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


def test_clean_scenario_has_no_failures_on_all_three_paths():
    scenario = _scenario()
    assert scenario.runnable() == ["cli", "sheet", "python"]
    assert _failures(scenario) == {}


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


def test_a_missing_path_is_an_exists_xfail_and_not_run():
    scenario = _scenario(cli={"missing": "AUDIT bug 3: the CLI has no add verb"})
    assert scenario.runnable() == ["sheet", "python"]
    results = run_scenario(scenario)
    assert set(results) == {"sheet", "python"}
    failures = evaluate(scenario, results)
    assert failures == {("cli", "exists"): "missing: AUDIT bug 3: the CLI has no add verb"}
    unexpected, expected, fixed = classify(scenario, failures)
    assert unexpected == [] and expected == [("cli", "exists")] and fixed == []


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


def test_tool_key_is_stored_and_validated():
    assert _scenario().tool is None
    assert _scenario(tool="timeline.opacity").tool == "timeline.opacity"
    with pytest.raises(ScenarioError, match="'tool'"):
        _scenario(tool="  ")


def test_fixture_lookup_names_both_places_when_missing():
    with pytest.raises(ScenarioError, match="editing_scenarios/fixtures"):
        _scenario(fixture="no-such-fixture")


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


def test_repository_scenarios_have_all_three_real_paths():
    from tests.editing_conformance.runner import SCENARIOS
    scenarios = [load_scenario(p) for p in SCENARIOS.rglob("*.yaml")]
    # The exact admitted 08a tree already contains 50, although the card says 49.
    assert len(scenarios) == 50
    for scenario in scenarios:
        assert scenario.present() == ["cli", "sheet", "python"]
        assert scenario.runnable() == scenario.present()
