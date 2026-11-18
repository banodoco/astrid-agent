import pytest

from astrid.sdk.shot_plan import ShotPlanError, merge_plan, plan_problem, plan_rows


def row(plan):
    bundle = {"shots": {"04": {"payload": {"metadata": {"plan": plan}}}},
              "placements": [{"shot_id": "04"}]}
    return plan_rows(bundle)[0]


@pytest.mark.parametrize("status", ["take_ready", "approved"])
def test_one_checked_take_must_cover_the_whole_plan(status):
    plan = {"method": ["LC", "R"], "takes": [{"generation": "first", "covers": ["LC"]}]}
    assert row(plan)["coverage"] == "LC done · R missing"
    with pytest.raises(ShotPlanError, match="R not covered"):
        merge_plan(plan, {"status": status})
    with pytest.raises(ShotPlanError, match="single take"):
        merge_plan(plan, {"status": status}, add_takes=[{"generation": "other", "covers": ["R"]}])
    final = merge_plan(plan, {"status": status}, add_takes=[{"generation": "final", "covers": ["LC", "R"]}])
    assert row(final)["coverage"] == "LC done · R done"


def test_legacy_ready_is_readable_but_cannot_be_approved_without_checks():
    plan = {"status": "take_ready", "method": ["LC"], "takes": [{"generation": "old"}]}
    assert plan_problem(plan) is None
    assert row(plan)["coverage"] == "coverage unknown"
    with pytest.raises(ShotPlanError, match="LC not covered"):
        merge_plan(plan, {"status": "approved"})
    assert row(plan)["plan"] == plan


@pytest.mark.parametrize("covers", [["M"], "LC"])
def test_covers_must_list_planned_methods(covers):
    with pytest.raises(ShotPlanError, match="plan.method|list of non-empty strings"):
        merge_plan({"method": ["LC"]}, add_takes=[{"media": "take", "covers": covers}])
