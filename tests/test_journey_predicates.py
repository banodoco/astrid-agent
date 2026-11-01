"""Focused positive and refusal coverage for the M-CI predicate seam.

Every positive fixture in this module is synthetic/rehearsal only.  It models
the context which M-CI supplies after authentication; it is not release
evidence and cannot qualify any journey on its own.
"""
from __future__ import annotations

import copy

import pytest

from scripts import journey_predicates as predicates
from tests.test_journey_observations import authenticated_index


@pytest.mark.parametrize(
    ("checker", "family", "selector"),
    [
        (predicates.check_release1, "release1", "direct-linux"),
        (predicates.check_release1, "release1", "pinokio-backend-linux"),
        (predicates.check_m4_5, "m4_5", "release2-install-line-direct-linux"),
        (predicates.check_m4_5, "m4_5", "release2-pinokio-update-pinokio-backend-linux"),
        (predicates.check_m4_6, "m4_6", "direct-linux"),
        (predicates.check_m4_6, "m4_6", "pinokio-fully-local-linux"),
    ],
)
def test_complete_mci_context_and_semantic_observation_is_positive_synthetic_rehearsal(
    checker, family: str, selector: str
) -> None:
    ok, reasons = checker(authenticated_index(family, selector))
    assert ok is True
    assert reasons == []


@pytest.mark.parametrize(
    ("checker", "family", "selector"),
    [
        (predicates.check_release1, "release1", "direct-linux"),
        (predicates.check_m4_5, "m4_5", "release2-install-line-direct-linux"),
        (predicates.check_m4_6, "m4_6", "direct-linux"),
    ],
)
def test_absent_or_structural_only_producer_is_not_an_authenticated_positive(
    checker, family: str, selector: str
) -> None:
    absent = authenticated_index(family, selector)
    absent.pop("authenticated_context")
    ok, reasons = checker(absent)
    assert ok is False
    assert any("finalized authenticated context is absent" in reason for reason in reasons)

    structural = authenticated_index(family, selector)
    structural.pop("authenticated_context")
    structural.pop("producer_files")
    ok, reasons = checker(structural)
    assert ok is False
    assert any("finalized authenticated context is absent" in reason for reason in reasons)


@pytest.mark.parametrize(
    ("checker", "family", "selector"),
    [
        (predicates.check_release1, "release1", "direct-linux"),
        (predicates.check_m4_5, "m4_5", "release2-install-line-direct-linux"),
        (predicates.check_m4_6, "m4_6", "direct-linux"),
    ],
)
def test_predicates_refuse_missing_skipped_forged_duplicate_and_wrong_tuple_evidence(
    checker, family: str, selector: str
) -> None:
    missing = authenticated_index(family, selector)
    missing["producer_files"] = []
    ok, reasons = checker(missing)
    assert ok is False
    assert any("producer_files" in reason and "exactly" in reason for reason in reasons)

    skipped = authenticated_index(family, selector)
    skipped["files"][0]["json"]["status"] = "skipped"
    ok, reasons = checker(skipped)
    assert ok is False
    assert any("status" in reason or "not pass" in reason for reason in reasons)

    forged = authenticated_index(family, selector)
    payload = forged["producer_files"][0]["json"]
    if payload["owner_kind"] == "native":
        payload["manager"]["foreground_fallback"] = True
    else:
        payload["settlement"]["terminal"]["outcome"] = "failed"
    ok, reasons = checker(forged)
    assert ok is False
    assert any("foreground_fallback" in reason or "engine terminal" in reason for reason in reasons)

    duplicate = authenticated_index(family, selector)
    duplicate["producer_files"].append(copy.deepcopy(duplicate["producer_files"][0]))
    ok, reasons = checker(duplicate)
    assert ok is False
    assert any("duplicate evidence path" in reason for reason in reasons)

    wrong_tuple = authenticated_index(family, selector)
    wrong_tuple["authenticated_context"]["binding"]["tuple_sha256"] = "f" * 64
    ok, reasons = checker(wrong_tuple)
    assert ok is False
    assert any("binding: differs from producer export" in reason for reason in reasons)


def test_context_rejects_wrong_selector_candidate_export_and_owner_bindings() -> None:
    wrong_selector = authenticated_index("m4_6", "direct-linux")
    wrong_selector["authenticated_context"]["selector_value"] = "direct-macos"
    reasons = predicates.check_m4_6(wrong_selector)[1]
    assert any("selector_value" in reason for reason in reasons)

    wrong_candidate = authenticated_index("m4_6", "direct-linux")
    wrong_candidate["authenticated_context"]["candidate"]["agent"] = "f" * 40
    reasons = predicates.check_m4_6(wrong_candidate)[1]
    assert any("candidate" in reason and "differs" in reason for reason in reasons)

    wrong_export = authenticated_index("m4_6", "direct-linux")
    wrong_export["authenticated_context"]["producer_export_sha256"] = "0" * 64
    reasons = predicates.check_m4_6(wrong_export)[1]
    assert any("producer_export_sha256" in reason for reason in reasons)

    wrong_owner = authenticated_index("m4_6", "direct-linux")
    wrong_owner["producer"]["export"]["json"]["owner_kind"] = "pinokio"
    reasons = predicates.check_m4_6(wrong_owner)[1]
    assert any("ownership" in reason for reason in reasons)


def test_direct_semantics_refuse_custody_loss_or_wrong_removal_set() -> None:
    custody_loss = authenticated_index("m4_6", "direct-linux")
    custody_loss["producer_files"][0]["json"]["custody"]["final_census"]["observer_loss"] = 1
    reasons = predicates.check_m4_6(custody_loss)[1]
    assert any("observer_loss" in reason for reason in reasons)

    removal_mismatch = authenticated_index("release1", "direct-linux")
    removal_mismatch["producer_files"][0]["json"]["custody"]["removed"] = ["controller-1"]
    reasons = predicates.check_release1(removal_mismatch)[1]
    assert any("exact controller-and-descendant set" in reason for reason in reasons)

    census_mismatch = authenticated_index("release1", "direct-linux")
    census_mismatch["producer_files"][0]["json"]["custody"]["final_census"][
        "absent_incarnations"
    ] = ["controller-1"]
    reasons = predicates.check_release1(census_mismatch)[1]
    assert any("absent_incarnations" in reason for reason in reasons)

    boolean_loss = authenticated_index("m4_6", "direct-linux")
    boolean_loss["producer_files"][0]["json"]["custody"]["final_census"]["observer_loss"] = False
    reasons = predicates.check_m4_6(boolean_loss)[1]
    assert any("observer_loss" in reason for reason in reasons)


def test_direct_semantics_refuse_stale_owner_and_duplicate_bindings() -> None:
    stale = authenticated_index("m4_6", "direct-linux")
    stale["producer"]["seal"]["json"]["context_state"] = "observing"
    ok, reasons = predicates.check_m4_6(stale)
    assert ok is False
    assert any("context_state" in reason or "producer.seal" in reason for reason in reasons)

    owner_mismatch = authenticated_index("m4_6", "direct-linux")
    owner_mismatch["producer"]["export"]["json"]["owner_kind"] = "pinokio"
    ok, reasons = predicates.check_m4_6(owner_mismatch)
    assert ok is False
    assert any("ownership" in reason or "owner_kind" in reason for reason in reasons)

    duplicate_observation = authenticated_index("m4_6", "direct-linux")
    duplicate_observation["producer"]["export"]["json"]["observations"].append(
        copy.deepcopy(duplicate_observation["producer"]["export"]["json"]["observations"][0])
    )
    ok, reasons = predicates.check_m4_6(duplicate_observation)
    assert ok is False
    assert any("duplicate" in reason or "exactly one" in reason for reason in reasons)


def test_direct_semantics_turn_malformed_lineage_into_refusal_not_an_exception() -> None:
    malformed = authenticated_index("m4_6", "direct-linux")
    custody = malformed["producer_files"][0]["json"]["custody"]
    custody["descendants"][0]["parent_incarnation"] = []
    custody["listeners"][0]["owner_incarnation"] = {}
    ok, reasons = predicates.check_m4_6(malformed)
    assert ok is False
    assert any("parent_incarnation" in reason or "owner_incarnation" in reason for reason in reasons)


def test_pinokio_semantics_refuse_duplicate_terminal_and_unrestored_unknown_case() -> None:
    duplicate_terminal = authenticated_index("m4_6", "pinokio-backend-linux")
    duplicate_terminal["producer_files"][0]["json"]["settlement"]["events"][1]["kind"] = "terminal"
    reasons = predicates.check_m4_6(duplicate_terminal)[1]
    assert any("wrong kind" in reason for reason in reasons)

    unsafe_unknown = authenticated_index("m4_5", "release2-pinokio-update-pinokio-fully-local-linux")
    unsafe_unknown["producer_files"][0]["json"]["stop_safety"]["unknown"]["no_retry"] = False
    reasons = predicates.check_m4_5(unsafe_unknown)[1]
    assert any("no_retry" in reason for reason in reasons)


def test_predicates_refuse_malformed_index_unsafe_paths_and_extra_context_fields() -> None:
    ok, reasons = predicates.check_m4_6({"slot_id": [], "candidate": [], "files": [None]})
    assert ok is False and reasons

    unsafe = authenticated_index("m4_6", "direct-linux")
    unsafe["producer_files"].append({"path": "../escape.json", "sha256": "a" * 64, "json": {}})
    reasons = predicates.check_m4_6(unsafe)[1]
    assert any("safe relative path" in reason for reason in reasons)

    extra_context = authenticated_index("m4_6", "direct-linux")
    extra_context["authenticated_context"]["authenticated"] = True
    reasons = predicates.check_m4_6(extra_context)[1]
    assert any("authenticated_context: unknown field" in reason for reason in reasons)
