"""Mixed helper commits and selective undo through authenticated Runtime HTTP."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from astrid.sdk import timeline_intent
from astrid.sdk.timeline.api import Timeline
from astrid.sdk.timeline.sync import CommitConflict
from tests.editing_conformance import test_commit_runtime_m1 as m1
from tests.helpers.runtime import use_runtime
from tests.helpers.trusted_seed import seed_trusted_parent

pytestmark = pytest.mark.integration


@pytest.fixture
def film(tmp_path):
    use_runtime()
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore

    root = tmp_path / "realm"
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    try:
        project = service.create_project({"slug": "recut", "name": "Recut"},
                                         idempotency_key="project")["id"]
        service.create_timeline(project, "film", idempotency_key="timeline")
        clips = [
            {"id": "old-title", "clipType": "am-type", "track": "picture", "at": 6,
             "hold": 7, "params": {"text": "Old title", "color": "white"}},
            {"id": "old-intro", "clipType": "am-type", "track": "picture", "at": 0,
             "hold": 1, "params": {"text": "Old intro"}},
        ]
        timeline_intent.set_for(clips[0], 7)
        parent = {"config": {"output": {"fps": 30},
                             "tracks": [{"id": "picture", "kind": "visual"}]},
                  "clips": clips, "occurrences": [], "registry": {"assets": {}}}
        parent["config"]["clips"] = copy.deepcopy(clips)
        seed_trusted_parent(service, {
            "project_id": project, "timeline_id": "film", "expected_head": None,
            "parent_revision_id": "recut-baseline", "parent_composition": parent,
        }, idempotency_key="baseline")
    finally:
        service.close()
    return SimpleNamespace(duplicate_root=root, project_id=project, timeline_id="film")


@pytest.mark.parametrize("overlap", [False, True], ids=["immediate", "later-overlap"])
def test_mixed_function_receipt_and_authenticated_exact_undo(
    tmp_path, film, real_runtime, overlap,
):
    fixture = film
    daemon = real_runtime(fixture.duplicate_root, support_root=tmp_path / "support",
                          authoring_profile="astrid")
    client = m1.open_client(daemon)

    def editor(name):
        return Timeline.open(fixture.project_id, fixture.timeline_id, client=client,
                             outbox_path=tmp_path / (name + ".json"))

    def document():
        head = m1.history(client, fixture)["head"]
        return daemon.service._timeline_commits()[0].revision_closure(
            fixture.project_id, fixture.timeline_id, head["revision_id"])

    before = copy.deepcopy(document())
    history = copy.deepcopy(m1.history(client, fixture)["items"])
    tl = editor("recut")

    def recut(t):
        t.track("titles", above="picture").rename("Opening")
        t.add_text("Chapter one", at=12, hold=3, track="titles", id="chapter-one")
        t.clip("old-title").set_track("titles").hold_for(2.5).on(5)
        t.clip("old-intro").remove()

    accepted = tl.commit(recut)
    assert accepted["receipt"]["result"]["status"] == "accepted"
    authored = m1.history(client, fixture)["items"]
    assert authored[:-1] == history
    assert authored[-1]["publication_id"] == accepted["receipt"]["receipt_id"]
    parent = document()["parent_revision"]["payload"]
    assert [row["id"] for row in parent["config"]["tracks"]].index("titles") < [
        row["id"] for row in parent["config"]["tracks"]].index("picture")
    assert next(row for row in parent["config"]["tracks"] if row["id"] == "titles")["label"] == "Opening"
    assert "old-intro" not in [row["id"] for row in parent["clips"]]
    title = next(row for row in parent["clips"] if row["id"] == "old-title")
    assert (title["track"], title["hold"], title["at"]) == ("titles", 2.5, 5)
    assert timeline_intent.for_s(title) == 2.5
    assert next(row for row in parent["clips"] if row["id"] == "chapter-one")["params"]["text"] == "Chapter one"

    if overlap:
        editor("peer").commit(lambda t: t.clip("old-title").on(6))
        current, history = copy.deepcopy(document()), copy.deepcopy(m1.history(client, fixture))
        with pytest.raises(CommitConflict):
            tl.undo(publications=[accepted["receipt"]["receipt_id"]])
        assert document() == current
        assert m1.history(client, fixture) == history
    else:
        undone = tl.undo(publications=[accepted["receipt"]["receipt_id"]])
        assert undone["data"]["status"] == "accepted"
        assert document() == before
        restored = m1.history(client, fixture)["items"]
        assert restored[:-1] == authored
        assert restored[-1]["records"]["undo_of"] == [accepted["receipt"]["receipt_id"]]
