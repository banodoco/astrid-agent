"""One declaration-only pass on the base and one on the candidate, then a probe only for a declaration a
resolve-time failure points at, must accept and refuse exactly what the per-declaration probes did
(``legacy_probe_oracle``), and must cost a bounded number of passes and probes."""
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
    # Fail only while times resolve: the declaration-only pass cannot see them.
    "resolve-no-beat": lambda: _declare(_declare(candidate(), 0, cut="c1"), 1, cut="c1", on="beat 1 after c1"),
    "resolve-no-downbeat": lambda: _declare(_declare(candidate(), 0, cut="c1"), 1, cut="c1", on="downbeat 1 after c1"),
    "resolve-before-occurrence": lambda: _declare(_declare(candidate(), 0, cut="c1"), 1, cut="c1", on="c1 -5s"),
}
RESOLVE_TIME = ["resolve-no-beat", "resolve-no-downbeat", "resolve-before-occurrence"]


def _accepts(edited, head, exemptions=None):
    try:
        if exemptions is None:
            cv.validate_candidate_timing(edited, baseline=head)
        else:
            cv._validate_candidate_timing(edited, head, True, (), (), exemptions)
    except Exception as exc:  # noqa: BLE001 - the verdict is what is compared
        return str(exc)
    return None


@pytest.mark.parametrize("name", sorted(CASES))
def test_new_path_accepts_and_refuses_what_the_probe_oracle_did(name):
    head = CASES[name]()
    edited = _unrelated(head)
    old = oracle.old_exemptions(edited, head)
    assert cv._legacy_exemptions(edited, head) <= old
    if name not in RESOLVE_TIME:
        assert cv._legacy_exemptions(edited, head) == old
    assert (_accepts(edited, head) is None) == (_accepts(edited, head, old) is None)
    if name in RESOLVE_TIME:
        assert _accepts(edited, head) is None


@pytest.mark.parametrize("name", RESOLVE_TIME)
def test_resolve_time_declaration_is_exempt_only_while_unchanged(name):
    head = CASES[name]()
    assert oracle.old_exemptions(_unrelated(head), head)  # the probes exempted it
    changed = _unrelated(head)
    value = _child(changed, 1)["app"]["astrid"]["on"]
    _child(changed, 1)["app"]["astrid"]["on"] = value.replace("1", "2") if "1 after" in value else "c1 -6s"
    assert _accepts(changed, head, oracle.old_exemptions(changed, head)) is not None
    assert _accepts(changed, head) is not None
    fresh = _unrelated(head)
    _child(fresh, 0)["app"]["astrid"]["until"] = "c1 -5s"
    assert _accepts(fresh, head) is not None  # a new declaration refuses


def test_changed_declaration_is_not_exempt():
    head = CASES["unresolved-cut-anchor"]()
    edited = _unrelated(head)
    _child(edited, 1)["app"]["astrid"]["on"] = "c998"
    assert cv._legacy_exemptions(edited, head) == oracle.old_exemptions(edited, head) == set()


def test_unresolved_declaration_on_a_base_carrying_gap_after_s_is_exempt():
    """The one expected difference from the frozen oracle: its probe ran with no baseline, so the base's own
    ``gap_after_s`` made every probe fail with an unmarked error and no exemption was ever granted. Ruled a
    bug fix by the main session on 18 Nov, consistent with the documented rule of 2ad707b7a."""
    head = _declare(CASES["unresolved-cut-anchor"](), 0, gap_after_s=1.5)
    edited = _unrelated(head)
    key = (("shot", SHOT, _child(head, 1)["id"]), "on")
    assert oracle.old_exemptions(edited, head) == set()  # the frozen probe never exempted on such a base
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


def _count_probes(monkeypatch):
    calls = []
    original = cv._legacy_declaration_is_resolvable
    monkeypatch.setattr(cv, "_legacy_declaration_is_resolvable",
                        lambda *args: calls.append(args[1:3]) or original(*args))
    return calls


def test_a_clean_commit_runs_no_probe(monkeypatch):
    calls = _count_probes(monkeypatch)
    cv.validate_candidate_timing(_unrelated(candidate()), baseline=candidate())
    head = CASES["unresolved-cut-anchor"]()
    cv.validate_candidate_timing(_unrelated(head), baseline=head)  # pre-pass exemption: still no probe
    assert calls == []


def test_a_base_with_k_resolve_time_failures_runs_at_most_k_probes(monkeypatch):
    head = _declare(_declare(candidate(), 0, cut="c1"), 1, cut="c1", on="beat 1 after c1",
                              until="downbeat 1 after c1")
    calls = _count_probes(monkeypatch)
    cv.validate_candidate_timing(_unrelated(head), baseline=head)
    assert len(calls) == 2
    assert len(set(calls)) == len(calls)  # each declaration is probed once


def test_resolve_time_declaration_is_exempt_on_a_base_carrying_gap_after_s():
    head = _declare(CASES["resolve-no-beat"](), 0, gap_after_s=1.5)
    edited = _unrelated(head)
    assert _accepts(edited, head) is None  # exempt: the probe no longer fails on gap_after_s
    changed = _unrelated(head)
    _child(changed, 1)["app"]["astrid"]["on"] = "beat 2 after c1"
    assert _accepts(changed, head) is not None


# --- real fixture heads, against the frozen oracle ------------------------------------------------------------

import json
from pathlib import Path

from astrid.sdk.timeline._timing_evaluators import UNSUPPORTED_DECLARATIONS
from astrid.sdk.timeline.commit import bundle_from_document

ROOT = Path(__file__).resolve().parents[2]


def _documents():
    """Every real runtime document the repo's fixtures hold, by name."""
    found = {}
    redo = json.loads((ROOT / "tests/fixtures/timeline_redo_media_closure_1338.json").read_text())
    for name in ("before", "expected_document"):
        found[f"redo-1338:{name}"] = redo[name]
    cap = ROOT / "tests/editing_conformance/fixtures/main_image_cap_raw_closure.json"
    found["main-image-cap-raw"] = json.loads(cap.read_text())
    corpus = ROOT / "tests/sdk/fixtures/p1_feasibility"
    for case in json.loads((corpus / "manifest.json").read_text())["cases"]:
        if not case["fixture"].endswith(".json"):
            continue
        fixture = json.loads((corpus / case["fixture"]).read_text())
        document = fixture.get(case["document"]) if case["document"] == "input" else None
        if isinstance(document, dict):
            found[f"p1:{case['id']}"] = document
    return found


def _real_heads():
    heads = {}
    for name, document in _documents().items():
        try:
            heads[name] = bundle_from_document(document)
        except Exception:  # noqa: BLE001 - a fixture that is not a loadable document is not a head
            continue
    return heads


REAL_HEADS = _real_heads()


def _moved(bundle):
    bundle = copy.deepcopy(bundle)
    for key, clip in cv._legacy_rows(bundle):
        if isinstance(clip.get("at"), (int, float)):
            clip["at"] = clip["at"] + 1
            break
    return bundle


def _carries_unsupported(bundle):
    return any(intent._app(clip).get(name) is not None
               for _, clip in cv._legacy_rows(bundle) for name in UNSUPPORTED_DECLARATIONS)


def test_the_real_fixture_heads_were_found():
    assert len(REAL_HEADS) >= 3, sorted(REAL_HEADS)


def _declared(bundle, **fields):
    """The real head with a legacy declaration (and optionally an unsupported one) on its first child clip."""
    bundle = copy.deepcopy(bundle)
    for shot in bundle["shots"].values():
        clips = shot["internal_timeline"]["clips"]
        if clips:
            clips[0].setdefault("app", {}).setdefault("astrid", {}).update(fields)
            return bundle
    return None


VARIANTS = {
    "as-stored": lambda bundle: bundle,
    "unresolved-on": lambda bundle: _declared(bundle, on="c999"),
    "unresolved-on-and-gap-after-s": lambda bundle: _declared(bundle, on="c999", gap_after_s=1.5),
}


def _real_cases():
    cases = []
    for name in sorted(REAL_HEADS):
        for variant in sorted(VARIANTS):
            head = VARIANTS[variant](REAL_HEADS[name])
            if head is not None:  # a head with no child clip cannot carry a child declaration
                cases.extend((name, variant, head, edit) for edit in (_unrelated, _moved))
    return cases


REAL_CASES = _real_cases()
# Heads whose frozen probe never exempted, for a reason unrelated to the declaration: its own clearing of
# the other on/until makes these bundles fail authoring validation ("parent clips and config.clips
# disagree", an unclassified error), so it reported "resolvable". The pre-pass judges the declaration alone.
PROBE_BREAKS_ON = {"p1:nested-closure-trim", "p1:same-field-conflict"}


@pytest.mark.parametrize(
    ("name", "variant", "head", "edit"), REAL_CASES,
    ids=[f"{n}-{v}-{e.__name__.strip('_')}" for n, v, _, e in REAL_CASES])
def test_real_heads_verdict_matches_the_frozen_oracle(name, variant, head, edit):
    edited = edit(head)
    old = oracle.old_exemptions(edited, head)
    new = _accepts(edited, head) is None
    frozen = _accepts(edited, head, old) is None
    assert new or not frozen  # nothing that was accepted is refused now
    if new != frozen:  # the only differences are old-refuse to new-accept, each one named
        assert variant != "as-stored"
        assert _carries_unsupported(head) or name in PROBE_BREAKS_ON


# --- the basis costs the declarations, not declarations x document --------------------------------------------

def _deep_copies_during_exemptions(monkeypatch, count):
    head = candidate()
    for index in range(count):
        row = copy.deepcopy(_child(head, 0))
        row["id"] = f"extra-{index}"
        row.setdefault("app", {}).setdefault("astrid", {}).update(cut="c1", on=f"c9{index}")
        head["shots"][SHOT]["internal_timeline"]["clips"].append(row)
    edited = _unrelated(head)
    real, calls = copy.deepcopy, []
    with monkeypatch.context() as patch:
        patch.setattr(copy, "deepcopy", lambda *args, **kwargs: calls.append(1) or real(*args, **kwargs))
        assert len(cv._legacy_exemptions(edited, head)) == count
    return len(calls)


def test_exemptions_deep_copy_a_constant_number_of_times_whatever_the_declaration_count(monkeypatch):
    """The two passes copy the bundle a fixed number of times; no basis copies the document per declaration."""
    at_5 = _deep_copies_during_exemptions(monkeypatch, 5)
    at_50 = _deep_copies_during_exemptions(monkeypatch, 50)
    assert at_5 == at_50 <= 60, (at_5, at_50)
