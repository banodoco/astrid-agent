"""Public sessions retain Runtime's authenticated pair across local boundaries."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk import timeline
from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline import store
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.checkout import Checkout
from tests.sdk.test_timeline_helper_capture import (
    test_genuine_anchor_capture_has_exact_source_scope_and_base_pair as genuine_anchor,
)
from tests.sdk.test_timeline_restructure import restructure_runtime  # noqa: F401
from tests.sdk.test_timeline_restructure_guarded_runtime import (
    restructure_inverse_runtime,  # noqa: F401
)


def test_genuine_anchor_with_public_pair_reader(restructure_inverse_runtime):  # noqa: F811
    genuine_anchor(restructure_inverse_runtime)


def test_session_capture_reload_and_function_keep_verified_pair(restructure_inverse_runtime, tmp_path):  # noqa: F811
    env = restructure_inverse_runtime
    path = tmp_path / "session.json"
    opened = timeline.open(env.project, "t", client=env.client, outbox_path=path)
    original = copy.deepcopy(opened.head)
    assert all(opened.bundle["base_parent"][key] == value for key, value in original.items())
    draft = Checkout(copy.deepcopy(opened.bundle), path=tmp_path / "draft.json")
    draft.clip("cross").nudge(0.5)
    draft.save(quiet=True)
    reloaded = Checkout.load(draft.path)
    assert reloaded.bundle["base_parent"] == opened.bundle["base_parent"]
    assert (reloaded.bundle["project_id"], reloaded.bundle["timeline_id"]) == (env.project, "t")

    evaluations = []

    def edit(working):
        evaluations.append(copy.deepcopy(working.bundle["base_parent"]))
        working.bundle["parent"]["opaque"]["pair-test"] = True

    accepted = opened.commit(edit, idempotency_key="pair-function")
    assert len(evaluations) == 1
    assert all(evaluations[0][key] == value for key, value in original.items())
    landed = {"revision_id": accepted["data"]["accepted_revision_id"],
              "publication_id": accepted["receipt"]["receipt_id"]}
    assert opened.head == landed
    assert all(opened.bundle["base_parent"][key] == value for key, value in landed.items())
    reopened = timeline.open(env.project, "t", client=env.client, outbox_path=path)
    assert reopened.head == landed
    assert reopened.bundle == opened.bundle

    historical = store.fetch_bundle(env.project, "t", client=env.client, **original)
    assert all(historical["base_parent"][key] == value for key, value in original.items())
    assert historical["source_mapping"]["_runtime_document"]["parent_revision"]["revision_id"] == original["revision_id"]
    revision_only = store.fetch_bundle(env.project, "t", client=env.client, revision_id=original["revision_id"])
    assert "publication_id" not in revision_only["base_parent"]


@pytest.mark.parametrize("damage", ["wrong-revision", "wrong-publication", "missing-entry"])
def test_session_pair_authentication_refuses_without_writes(restructure_inverse_runtime, tmp_path, monkeypatch, damage):  # noqa: F811
    env = restructure_inverse_runtime
    history = copy.deepcopy(env.client.timelines.history(env.project, "t", limit=200).data)
    document = copy.deepcopy(env.document())
    stored = tuple(env.service.store.conn.iterdump())
    calls = copy.deepcopy(env.calls)
    original_history = env.client.timelines.history
    publication = history["head"]["publication_id"]

    def damaged(*args, **kwargs):
        result = copy.deepcopy(original_history(*args, **kwargs).data)
        if damage == "missing-entry":
            result["items"] = []
            result["next_cursor"] = None
        else:
            for row in result["items"]:
                if row["publication_id"] == publication:
                    if damage == "wrong-revision":
                        row["revision_id"] = "forged-revision"
                    else:
                        row["receipt"]["receipt_id"] = "forged-publication"
        return DomainResult.success(result)

    with monkeypatch.context() as patch:
        patch.setattr(env.client.timelines, "history", damaged)
        with pytest.raises(TimelineEditError, match=("original publication is unavailable" if damage == "missing-entry"
                                                   else "original revision/publication pair changed")):
            timeline.open(env.project, "t", client=env.client, outbox_path=tmp_path / "bad-session.json")
    assert env.calls == calls
    assert env.client.timelines.history(env.project, "t", limit=200).data == history
    assert env.document() == document
    assert tuple(env.service.store.conn.iterdump()) == stored
