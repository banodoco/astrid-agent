"""Focused refusal coverage for journey predicates.

Synthetic fixtures exercise only parser and predicate rejection; they are not
producer observations or authenticated evaluator input.
"""
from __future__ import annotations

import copy

import pytest

from scripts import journey_failure_modes as failure
from scripts import journey_predicates as predicates
from tests.test_journey_observations import (
    m4_5_index,
    m4_6_index,
    producer_envelope,
    release1_index,
)


@pytest.mark.parametrize(
    ("checker", "factory", "label"),
    [
        (predicates.check_release1, release1_index, "Release-1"),
        (predicates.check_m4_5, m4_5_index, "M4-5"),
        (predicates.check_m4_6, m4_6_index, "M4-6"),
    ],
)
def test_every_available_predicate_remains_closed_without_independent_observations(checker, factory, label) -> None:
    ok, reasons = checker(factory())
    assert ok is False
    assert any(label in reason and "independent R-30 section 5" in reason for reason in reasons)
    assert any("authenticated producer export and collector seal are absent" in reason for reason in reasons)


@pytest.mark.parametrize(
    ("checker", "factory", "owner_kind", "label"),
    [
        (predicates.check_release1, release1_index, "native", "Release-1"),
        (predicates.check_m4_5, m4_5_index, "native", "M4-5"),
        (predicates.check_m4_6, m4_6_index, "native", "M4-6"),
    ],
)
def test_structural_producer_export_still_refuses_without_mci_finalization(
    checker, factory, owner_kind: str, label: str
) -> None:
    index = factory()
    index["producer"] = producer_envelope(owner_kind)
    ok, reasons = checker(index)
    assert ok is False
    assert not any("producer export or collector seal is structurally invalid" in reason for reason in reasons)
    assert any(label in reason and "M-CI authenticated finalization" in reason for reason in reasons)


@pytest.mark.parametrize(
    ("slot", "owner_kind"),
    [
        ("direct-linux", "native"),
        ("pinokio-backend-linux", "pinokio"),
        ("pinokio-fully-local-linux", "pinokio"),
    ],
)
def test_m4_6_dispatches_direct_and_pinokio_producer_owners_then_refuses(slot: str, owner_kind: str) -> None:
    index = m4_6_index(slot)
    index["producer"] = producer_envelope(owner_kind)
    ok, reasons = predicates.check_m4_6(index)
    assert ok is False
    assert not any("requires" in reason and "ownership" in reason for reason in reasons)
    assert any("M-CI authenticated finalization" in reason for reason in reasons)


def test_predicates_reject_foreign_producer_owner_and_malformed_producer_envelope() -> None:
    foreign = m4_6_index("direct-linux")
    foreign["producer"] = producer_envelope("pinokio")
    reasons = predicates.check_m4_6(foreign)[1]
    assert any("requires 'native' ownership" in reason for reason in reasons)
    assert any("M-CI authenticated finalization" in reason for reason in reasons)

    malformed = m4_6_index()
    malformed["producer"] = producer_envelope()
    malformed["producer"]["export"]["json"]["checks"][0]["observations"] = ["missing"]
    reasons = predicates.check_m4_6(malformed)[1]
    assert any("dangling observation" in reason for reason in reasons)
    assert any("structurally invalid" in reason for reason in reasons)


@pytest.mark.parametrize("slot", failure.REQUIRED_SLOT_IDS)
def test_m4_6_all_slots_dispatch_then_refuse(slot: str) -> None:
    ok, reasons = predicates.check_m4_6(m4_6_index(slot))
    assert ok is False
    assert any("independent R-30 section 5" in reason for reason in reasons)
    assert not any("parsed observation differs" in reason for reason in reasons)


def test_m4_6_refuses_missing_skipped_forged_duplicate_and_wrong_slot_evidence() -> None:
    missing = m4_6_index()
    missing["files"].pop()
    assert any("missing evidence" in reason for reason in predicates.check_m4_6(missing)[1])

    skipped = m4_6_index()
    skipped["files"][0]["json"]["status"] = "skipped"
    assert any("not pass" in reason for reason in predicates.check_m4_6(skipped)[1])

    forged = m4_6_index()
    sidecar = forged["files"][1]
    sidecar["json"]["observation"]["status"] = "forged"
    assert any("parsed observation differs" in reason for reason in predicates.check_m4_6(forged)[1])

    duplicate = m4_6_index()
    duplicate["files"].append(copy.deepcopy(duplicate["files"][1]))
    assert any("duplicate evidence path" in reason for reason in predicates.check_m4_6(duplicate)[1])

    wrong_slot = m4_6_index("direct-macos")
    wrong_slot["slot_id"] = "direct-linux"
    assert any("slot_id" in reason for reason in predicates.check_m4_6(wrong_slot)[1])


def test_predicates_reject_malformed_indexes_digest_tuple_and_unsafe_paths() -> None:
    ok, malformed = predicates.check_m4_6({"slot_id": [], "candidate": [], "files": [None]})
    assert ok is False and malformed

    index = m4_6_index()
    index["candidate"]["manifest_sha256"] = "f" * 64
    index["files"][0]["sha256"] = "A" * 64
    index["files"].append({"path": "../escape.json", "sha256": "a" * 64, "json": {}})
    reasons = predicates.check_m4_6(index)[1]
    assert any("SHA-256 required" in reason for reason in reasons)
    assert any("safe relative path" in reason for reason in reasons)
    assert any("manifest_sha256" in reason for reason in reasons)


def test_release1_and_m4_5_reject_skipped_wrong_selector_and_malformed_observation() -> None:
    release1 = release1_index()
    release1["files"][0]["json"]["status"] = "skipped"
    assert any("status" in reason for reason in predicates.check_release1(release1)[1])

    m4_5 = m4_5_index()
    m4_5["scenario_id"] = "release2-button-direct-windows"
    m4_5["files"][0]["json"]["runner_provenance"] = []
    reasons = predicates.check_m4_5(m4_5)[1]
    assert any("scenario_id" in reason or "malformed M4-5" in reason for reason in reasons)
