"""Strict parent-mirror normalization for commit effect projection."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline._commit_patch import Closure, effect_diff
from tests.editing_conformance.test_recipe_m1_commands import bundle


def parent_closure(value):
    closure = object.__new__(Closure)
    closure.images = {"parent": {"payload": copy.deepcopy(value["parent"])}}
    return closure


@pytest.mark.parametrize("null_side", ["payload", "config"])
def test_effect_diff_accepts_a_null_clip_mirror_and_uses_the_list(null_side):
    before = bundle()
    parent = before["parent"]
    if null_side == "payload":
        parent["clips"] = None
    else:
        parent["config"]["clips"] = None

    after = copy.deepcopy(before)
    authoritative = (after["parent"]["config"]["clips"]
                     if null_side == "payload" else after["parent"]["clips"])
    authoritative.clear()

    effects = effect_diff(parent_closure(before), parent_closure(after))

    assert effects
    assert all(effect["ref"] == "parent" for effect in effects)
    if null_side == "payload":
        assert before["parent"]["clips"] is None
        assert after["parent"]["clips"] is None
    else:
        assert before["parent"]["config"]["clips"] is None
        assert after["parent"]["config"]["clips"] is None


@pytest.mark.parametrize(
    ("mirror", "shape"),
    [("clips", "list"), ("clips", "scalar"), ("registry", "null")],
)
def test_effect_diff_still_rejects_non_null_contradictory_mirrors(mirror, shape):
    before = bundle()
    before_parent = before["parent"]
    before_parent["config"][mirror] = copy.deepcopy(before_parent[mirror])
    after = copy.deepcopy(before)
    if shape == "list":
        after["parent"]["config"][mirror][0]["id"] = "different" if mirror == "clips" else "different"
    else:
        after["parent"]["config"][mirror] = None if shape == "null" else False

    with pytest.raises(CommandError, match="contradictory parent mirrors"):
        effect_diff(parent_closure(before), parent_closure(after))


@pytest.mark.parametrize("missing_side", ["payload", "config"])
def test_effect_diff_preserves_absent_mirror_topology(missing_side):
    before = bundle()
    after = copy.deepcopy(before)
    if missing_side == "payload":
        after["parent"].pop("clips")
    else:
        after["parent"]["config"].pop("clips")

    effects = effect_diff(parent_closure(before), parent_closure(after))

    assert effects == [
        {
            "op": "set",
            "ref": "parent",
            "path": [],
            "before": {"state": "present", "value": before["parent"]},
            "value": after["parent"],
        }
    ]
