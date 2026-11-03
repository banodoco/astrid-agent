"""Legacy producer inverses retain authenticated physical effect bindings."""
from __future__ import annotations

import copy

import pytest

from tests.sdk.test_timeline_producer_guard_runtime import body, requester_session, submit

pytest_plugins = ("tests.sdk.test_timeline_producer_guard_runtime",)

pytestmark = pytest.mark.integration


def _inverse(case, publication, base):
    return {
        **body(case),
        **base,
        "commands": [{
            "schema_version": 1,
            "op": "timeline.inverse",
            "version": "1",
            "select": None,
            "args": {"publications": [publication]},
        }],
    }


def _record(case, publication):
    return case.service._timeline_commits()[0].history_entry(
        case.target["project_id"], "film", publication)["records"]["commands"][0]


def _forward(case, path, key):
    session = requester_session(case, path)
    session.queue_proposal(copy.deepcopy(case.proposal))
    return session.flush(idempotency_key=key)


def test_legacy_producer_forward_inverse_and_redo_bind_every_stage_effect(settled, tmp_path):
    case = settled
    request = body(case)
    assert "label" not in request and "steps" not in request

    forward = _forward(case, tmp_path / "legacy-forward.json", "legacy-forward")
    assert forward["data"]["status"] == "accepted"
    publication = forward["receipt"]["receipt_id"]
    current = {"base_revision_id": forward["data"]["accepted_revision_id"],
               "base_publication_id": publication}

    undone = submit(case, _inverse(case, publication, current), "legacy-undo")
    assert undone["data"]["canonical_document"]["parent_revision"]["payload"]["config"]["tracks"] == []
    inverse_record = _record(case, undone["receipt"]["receipt_id"])
    effects = [binding for binding in inverse_record["bindings"]
               if binding.get("key") == "commit.effect"]
    stages = [binding for binding in inverse_record["bindings"]
              if binding.get("key") == "inverse.stage"]
    assert stages and effects
    step = case.service._timeline_commits()[0].history_entry(
        case.target["project_id"], "film", undone["receipt"]["receipt_id"]
    )["records"]["steps"][0]
    expected = {(index, side) for index in range(len(step["patch"]))
                for side in ("before", "after")
                if step["patch"][index]["ref"] == "parent"}
    assert {(binding["effect_ordinal"], binding["boundary"]["side"]) for binding in effects} == expected
    assert all(binding["physical_ref"] == "parent" for binding in effects)

    redone = submit(case, _inverse(case, undone["receipt"]["receipt_id"], {
        "base_revision_id": undone["data"]["accepted_revision_id"],
        "base_publication_id": undone["receipt"]["receipt_id"],
    }), "legacy-redo")
    assert redone["data"]["canonical_document"] == forward["data"]["canonical_document"]


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrongside", "wrongeffect", "wrongphysicalsource"])
def test_legacy_inverse_rejects_incomplete_or_misattributed_effect_binding(
        settled, monkeypatch, mutation, tmp_path):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    case = settled
    forward = _forward(case, tmp_path / ("negative-forward-" + mutation + ".json"),
                       "negative-forward-" + mutation)
    publication = forward["receipt"]["receipt_id"]
    current = {"base_revision_id": forward["data"]["accepted_revision_id"],
               "base_publication_id": publication}
    original_accept = RequestCommitPort.accept

    def tampered_accept(port, prepared):
        if any(record["command"]["op"] == "timeline.inverse"
               for record in prepared["records"]["commands"]):
            record = prepared["records"]["commands"][0]
            bindings = [binding for binding in record["bindings"]
                        if binding.get("key") == "commit.effect"]
            if mutation == "missing":
                record["bindings"] = [binding for binding in record["bindings"]
                                       if binding.get("key") != "commit.effect"]
            elif mutation == "duplicate":
                record["bindings"].append(copy.deepcopy(bindings[0]))
            elif mutation == "wrongside":
                bindings[0]["boundary"]["side"] = (
                    "after" if bindings[0]["boundary"]["side"] == "before" else "before"
                )
            elif mutation == "wrongeffect":
                bindings[0]["effect_ordinal"] = len(prepared["records"]["steps"][0]["patch"])
            else:
                bindings[0]["physical_ref"] = "timeline"
        return original_accept(port, prepared)

    monkeypatch.setattr(RequestCommitPort, "accept", tampered_accept)
    before = case.service.get_project_timeline(case.target["project_id"], "film")
    with pytest.raises(RuntimeErrorBase) as error:
        submit(case, _inverse(case, publication, current), "negative-inverse-" + mutation)
    assert error.value.details["reason"] == "invalid_records"
    assert case.service.get_project_timeline(case.target["project_id"], "film") == before
