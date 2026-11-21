"""``validate_final_candidate`` on a synthetic 600-clip film accepts and refuses exactly what it did.

The film is built here (no realm content): 600 child clips in cut groups of three, with legacy
``on``/``until`` declarations, some of which can never resolve. Each scenario's verdict, and a digest
of every signed legacy failure the declaration pass produced, were recorded from the validator before
the commit-path copy trim; they must not change. The signing shortcut is also checked against the
plain ``json.dumps`` it replaces.
"""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from astrid.sdk.timeline import _commit_timing_validation as cv
from astrid.sdk.timeline.validation import _timing_parent_view, validate_final_candidate
from tests.editing_conformance.test_commit_v2_validation import candidate

SHOT = "chapter-shot"
CLIPS = 600


def film():
    value = candidate()
    internal = value["shots"][SHOT]["internal_timeline"]
    template = copy.deepcopy(internal["clips"][0])
    clips = []
    for index in range(CLIPS):
        clip = copy.deepcopy(template)
        clip.update(id=f"synthetic-{index:03d}", at=(index // 3) * 0.04, hold=0.04)
        astrid = clip.setdefault("app", {}).setdefault("astrid", {})
        astrid["cut"] = f"c{index // 3}"
        if index % 3 == 1:
            astrid["on"] = f"c{index // 3 + 1000}"      # names no cut: never resolves
        clips.append(clip)
    internal["clips"] = clips
    base = value["shots"][SHOT].get("base_internal_timeline")
    if base is not None:
        base["clips"] = copy.deepcopy(clips)
    return value


def unrelated(value):
    value = copy.deepcopy(value)
    value["parent"]["config"]["tracks"].append({"id": "extra", "kind": "visual"})
    return value


def retarget(value):
    value = unrelated(value)
    value["shots"][SHOT]["internal_timeline"]["clips"][1]["app"]["astrid"]["on"] = "c4000"
    return value


def newly_unresolved(value):
    value = unrelated(value)
    value["shots"][SHOT]["internal_timeline"]["clips"][0]["app"]["astrid"]["on"] = "c5000"
    return value


def verdict(candidate_value, baseline):
    try:
        validate_final_candidate(candidate_value, baseline=baseline)
    except Exception as exc:  # noqa: BLE001 - the verdict is what is compared
        return f"{type(exc).__name__}: {exc}"
    return "accepted"


def signed_digest(value):
    failures = cv._signed_failures(_timing_parent_view(value))
    text = json.dumps(sorted((repr(key), signature) for key, signature in failures.items()))
    return len(failures), hashlib.sha256(text.encode()).hexdigest()


SCENARIOS = {
    "unrelated-edit": unrelated,
    "retargeted-legacy-declaration": retarget,
    "newly-unresolved-declaration": newly_unresolved,
}
GOLDEN = {
    ("verdict", "unrelated-edit"): "accepted",
    ("verdict", "retargeted-legacy-declaration"): (
        "FinalCandidateError: candidate.timing: missing cut anchor 'c4000' for "
        "('occ-a', 'chapter-shot', 'synthetic-001')"
    ),
    ("verdict", "newly-unresolved-declaration"): (
        "FinalCandidateError: candidate.timing: missing cut anchor 'c5000' for "
        "('occ-a', 'chapter-shot', 'synthetic-000')"
    ),
    ("verdict", "no-baseline"): (
        "FinalCandidateError: candidate.timing: missing cut anchor 'c1000' for "
        "('occ-a', 'chapter-shot', 'synthetic-001')"
    ),
    ("signed", "head"): [200, "d02f72eb2d06423383901fc6fdd38c2231da27e97c1b00e2829dd2c8ff9fcdd3"],
    ("signed", "unrelated-edit"): [200, "d02f72eb2d06423383901fc6fdd38c2231da27e97c1b00e2829dd2c8ff9fcdd3"],
    ("signed", "retargeted-legacy-declaration"): [200, "922c0532a5d21f1ed7d12297f994fe19068104f3c425ecd3f9b42f8951c94e14"],
    ("signed", "newly-unresolved-declaration"): [201, "bbaec3cc86f28f9b290e5e8412ed6c70d6086c53e962166e10976e03bbbf3202"],
}


@pytest.fixture(scope="module")
def head():
    return film()


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_verdict_with_a_baseline_is_unchanged(head, name):
    assert verdict(SCENARIOS[name](head), head) == GOLDEN["verdict", name]


def test_verdict_without_a_baseline_is_unchanged(head):
    assert verdict(unrelated(head), None) == GOLDEN["verdict", "no-baseline"]


@pytest.mark.parametrize("name", ["head", *sorted(SCENARIOS)])
def test_signed_legacy_failures_are_unchanged(head, name):
    value = head if name == "head" else SCENARIOS[name](head)
    assert list(signed_digest(value)) == GOLDEN["signed", name]


def test_signing_shortcut_equals_json_dumps_of_the_signature(head):
    view = _timing_parent_view(head)
    rows = dict(cv._legacy_rows(view))
    reader = cv._BasisReader(view)
    checked = 0
    for key, field in cv._declaration_failures(view):
        value = cv._legacy_declarations(rows[key])[field]
        assert reader.signed(key, rows[key], value) == json.dumps([value, reader.signature(key, rows[key], value)])
        checked += 1
    assert checked > 100
