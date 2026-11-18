"""Probe: does a retained gap_after_s declaration hide an unresolvable legacy `on` from the exemption check?

``_legacy_declaration_is_resolvable`` deep-copies the bundle, clears every other on/until, and runs
``validate_candidate_timing`` on that probe with no baseline. A ``gap_after_s`` declaration on any clip
makes the probe raise ``unsupported timing declaration ... gap_after_s`` from ``_timing_evaluators``.
That message carries no marker in ``_known_unresolvable_timing_error``, so the check returns True
("resolvable") and no exemption is granted for the genuinely unresolvable ``on``.

The unresolvable declaration is ``c999``: the head has no cut ``c999``, so the probe raises
``missing cut anchor 'c999' ...``. Without gap_after_s that marker is known, so the check returns False.
"""
from __future__ import annotations

import copy

from astrid.core.timeline.authoring_bundle import AuthoringBundleError  # noqa: F401  (documents the refusal type)
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline import _commit_timing_validation as cv
from tests.editing_conformance import legacy_probe_oracle as oracle
from tests.editing_conformance.test_commit_v2_validation import candidate

UNRESOLVABLE_ON = "c999"


def _head(*, gap_after: bool) -> dict:
    bundle = candidate()
    clips = bundle["shots"]["chapter-shot"]["internal_timeline"]["clips"]
    app = clips[0].setdefault("app", {}).setdefault("astrid", {})
    app["on"] = UNRESOLVABLE_ON
    if gap_after:
        app["gap_after_s"] = 1.5
    return bundle


def _key_of(bundle: dict) -> tuple:
    return next(k for k, clip in cv._legacy_rows(bundle)
                if clip.get("app", {}).get("astrid", {}).get("on") == UNRESOLVABLE_ON)


def _probe_error(bundle: dict, key: tuple) -> str:
    """The exception the exemption probe sees, built exactly as _legacy_declaration_is_resolvable builds it."""
    probe = copy.deepcopy(dict(bundle))
    oracle._clear_legacy_declarations(probe, (key, "on"))
    target = dict(cv._legacy_rows(probe)).get(key)
    intent._set(target, "on", UNRESOLVABLE_ON)
    try:
        cv.validate_candidate_timing(probe)
    except Exception as exc:  # observed, not asserted: the message is the evidence
        return f"{type(exc).__name__}: {exc}"
    return "no error"


def test_gap_after_s_on_the_base_hides_an_unresolvable_legacy_on_from_the_exemption():
    without_gap = _head(gap_after=False)
    with_gap = _head(gap_after=True)
    key = _key_of(without_gap)
    assert _key_of(with_gap) == key

    a = oracle._legacy_declaration_is_resolvable(without_gap, key, "on", UNRESOLVABLE_ON)
    b = oracle._legacy_declaration_is_resolvable(with_gap, key, "on", UNRESOLVABLE_ON)
    error_b = _probe_error(with_gap, key)
    print(f"\n(a) head without gap_after_s: resolvable={a}")
    print(f"(b) head with gap_after_s:    resolvable={b}")
    print(f"(b) probe error: {error_b}")

    # Observed facts. (a) is the correct behaviour: the unresolvable `on` is exempt-eligible (False).
    # (b) shows the fault: the gap_after_s declaration makes the probe fail with an unmarked error,
    # so the check says the declaration is resolvable (True) and no exemption is granted.
    assert a is False
    assert b is True  # FAULT REPRODUCED: an unresolvable declaration is reported as resolvable
    assert error_b.startswith("TimingCompileError: unsupported timing declaration ")
    assert error_b.endswith(".gap_after_s")
    assert "missing cut anchor" not in error_b
