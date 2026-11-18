"""One declaration-only pass on the base and one on the candidate must grant the exemptions the retired
per-declaration probes granted (``legacy_probe_oracle``), and must cost a bounded number of passes."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline import _commit_timing_validation as cv
from astrid.sdk.timeline import _timing_evaluators as evaluators
from tests.editing_conformance import legacy_probe_oracle as oracle
from tests.editing_conformance.test_commit_v2_validation import candidate

SHOT = "chapter-shot"


def _child(bundle, index):
    return bundle["shots"][SHOT]["internal_timeline"]["clips"][index]


def _declare(bundle, index, **fields):
    _child(bundle, index).setdefault("app", {}).setdefault("astrid", {}).update(fields)
    return bundle


def _unrelated(bundle):
    bundle = copy.deepcopy(bundle)
    bundle["parent"]["config"]["tracks"].append({"id": "extra", "kind": "visual"})
    return bundle


def _parent_on(bundle, text):
    bundle["parent"]["clips"][0].setdefault("app", {}).setdefault("astrid", {})["on"] = text
    return bundle


CASES = {
    "unresolved-cut-anchor": lambda: _declare(candidate(), 1, cut="c1", on="c999"),
    "resolvable-cut-anchor": lambda: _declare(_declare(candidate(), 0, cut="c1"), 1, cut="c1", on="c1"),
    "unresolved-until": lambda: _declare(candidate(), 1, until="c999"),
    "unparseable": lambda: _declare(candidate(), 1, on="!!"),
    "child-word-anchor": lambda: _declare(candidate(), 1, on="word:line-1:first"),
    "parent-word-without-binding": lambda: _parent_on(candidate(), "word:line-1:first"),
    "parent-cut-anchor": lambda: _parent_on(candidate(), "c999"),
    "beat-anchor": lambda: _declare(candidate(), 1, on="beat 1 after c1"),
    "nothing-declared": candidate,
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_new_exemptions_equal_the_probe_oracle(name):
    head = CASES[name]()
    edited = _unrelated(head)
    assert cv._legacy_exemptions(edited, head) == oracle.old_exemptions(edited, head)


def test_changed_declaration_is_not_exempt():
    head = CASES["unresolved-cut-anchor"]()
    edited = _unrelated(head)
    _child(edited, 1)["app"]["astrid"]["on"] = "c998"
    assert cv._legacy_exemptions(edited, head) == oracle.old_exemptions(edited, head) == set()


def test_unresolved_declaration_on_a_base_carrying_gap_after_s_is_exempt_where_the_probe_never_was():
    """The one known difference: the probe ran without a baseline, so a base with ``gap_after_s`` made every
    probe fail with an unmarked error and report 'resolvable' (see test_legacy_probe_gap_after_fault)."""
    head = _declare(CASES["unresolved-cut-anchor"](), 0, gap_after_s=1.5)
    edited = _unrelated(head)
    key = (("shot", SHOT, _child(head, 1)["id"]), "on")
    assert oracle.old_exemptions(edited, head) == set()
    assert cv._legacy_exemptions(edited, head) == {key}


def test_a_commit_makes_at_most_two_declaration_passes_and_no_per_declaration_compiles(monkeypatch):
    head = _declare(_declare(candidate(), 0, cut="c1", on="c999", until="c998"), 1, cut="c1", on="c997")
    counts = {"collect": 0, "compile": 0}
    original = evaluators.evaluate

    def counting(*args, **kwargs):
        counts["collect" if kwargs.get("collect") is not None else "compile"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(evaluators, "evaluate", counting)
    cv.validate_candidate_timing(_unrelated(head), baseline=head)
    assert counts["collect"] <= 2
    assert counts["compile"] == 1
