"""Physical authoring placement patches retain occurrence identity and undo exactly."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline.authoring_commit import physical_occurrence_commands
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from tests.editing_conformance.test_doc_patch_v2 import step, submit
from tests.editing_conformance.test_provider_step_undo_v2 import undo
from tests.sdk.test_timeline_commit_provider import DetachedRealm
from tests.sdk.test_timeline_commit_provider_document import document_fixture


@pytest.mark.parametrize(
    "order,starts",
    [
        (("occ-a", "occ-b"), (30000, 40000)),
        (("occ-b", "occ-a"), (20000, 50000)),
    ],
)
def test_physical_arrangement_permutation_preserves_owners_and_undoes_exactly(order, starts):
    original = document_fixture()
    realm = DetachedRealm(original)
    before_rows = original["parent_revision"]["payload"]["occurrences"]
    before_by_id = {row["occurrence_id"]: row for row in before_rows}

    current = bundle_from_document(original)
    by_id = {row["occurrence_id"]: row for row in current["placements"]}
    desired = [copy.deepcopy(by_id[identity]) for identity in order]
    for row, start in zip(desired, starts):
        row["placement"]["start_ms"] = start

    commands = physical_occurrence_commands(current, desired)
    assert commands and all(command["op"] == "doc.patch" for command in commands)
    landed = submit(realm, *commands, reads=[], steps=[
        step(f"arrange-{ordinal}", ordinal, command)
        for ordinal, command in enumerate(commands)
    ])
    accepted = landed["data"]["canonical_document"]
    rows = accepted["parent_revision"]["payload"]["occurrences"]
    assert [row["occurrence_id"] for row in rows] == list(order)
    for row, start in zip(rows, starts):
        original_row = before_by_id[row["occurrence_id"]]
        assert row["placement"]["start_ms"] == start
        assert (row["shot_id"], row["shot_revision_id"]) == (
            original_row["shot_id"], original_row["shot_revision_id"])
        assert row["duration_ms"] == original_row["duration_ms"]
        assert row["source_offset"] == original_row["source_offset"]
        assert row["speed"] == original_row["speed"]
        assert row["unknown"] == original_row["unknown"]

    reverted = submit(realm, undo(landed["receipt"]["receipt_id"]), reads=[])
    assert reverted["data"]["canonical_document"] == original


def test_physical_arrangement_refuses_membership_changes():
    bundle = bundle_from_document(document_fixture())
    with pytest.raises(ValueError, match="cannot change closure membership"):
        physical_occurrence_commands(bundle, bundle["placements"][:1])

    forged = copy.deepcopy(bundle["placements"])
    forged[0]["shot_id"] = forged[1]["shot_id"]
    with pytest.raises(ValueError, match="cannot change shot ownership"):
        physical_occurrence_commands(bundle, forged)


def test_physical_arrangement_noop_emits_no_commands():
    bundle = bundle_from_document(document_fixture())
    assert physical_occurrence_commands(bundle, copy.deepcopy(bundle["placements"])) == []


def test_many_occurrence_moves_keep_each_inverse_boundary_exact():
    original = document_fixture()
    payload = original["parent_revision"]["payload"]
    template = payload["occurrences"][0]
    for index in range(2, 7):
        row = copy.deepcopy(template)
        row["occurrence_id"] = f"occ-{index}"
        row["placement"]["start_ms"] = (index - 1) * 10000
        payload["occurrences"].append(row)
    original["parent_revision"]["content_digest"] = _digest(payload)

    realm = DetachedRealm(original)
    current = bundle_from_document(original)
    desired = copy.deepcopy(current["placements"])[::-1]
    for index, row in enumerate(desired):
        row["placement"]["start_ms"] = (index + 10) * 10000
    commands = physical_occurrence_commands(current, desired)
    assert len(commands) == 2  # one timing patch, then one ordered collection patch

    landed = submit(realm, *commands, reads=[], steps=[
        step(f"arrange-{ordinal}", ordinal, command)
        for ordinal, command in enumerate(commands)
    ])
    accepted = landed["data"]["canonical_document"]
    assert [row["occurrence_id"] for row in accepted["parent_revision"]["payload"]["occurrences"]] == [
        row["occurrence_id"] for row in desired]
    reverted = submit(realm, undo(landed["receipt"]["receipt_id"]), reads=[])
    assert reverted["data"]["canonical_document"] == original
