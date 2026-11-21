import hashlib
import json

import pytest

from scripts import journey_live_workspace as live


class CompositionClient:
    def __init__(self):
        self.timeline = {"head_revision_id": "base-r", "head_publication_id": "base-p"}
        self.documents = {"base-r": {"revision_id": "base-r", "project_id": "p1", "timeline_id": "t1",
                                      "payload": {"config": {"output": {"file": "output.mp4", "fps": 30}}}}}

    def request(self, method, route, body=None, key=None, **kwargs):
        if route.endswith("/commit"):
            change = body["commands"][0]["args"]["changes"][0]
            output = change["value"]
            revision = {"revision_id": "accepted-r", "project_id": "p1", "timeline_id": "t1",
                        "payload": {"config": {"output": output}}}
            self.documents["accepted-r"] = revision
            self.timeline.update(head_revision_id="accepted-r", head_publication_id="pub-2")
            return {"data": {"status": "accepted", "accepted_revision_id": "accepted-r"},
                    "receipt": {"receipt_id": "pub-2"}}
        if route.endswith("/timelines/t1"):
            return self.timeline
        if "/composition-revisions/" in route:
            return self.documents[route.rsplit("/", 1)[-1]]
        raise AssertionError(route)


def test_composition_edit_is_accepted_and_freshly_reopened_with_seed_identity():
    client = CompositionClient()
    seed = {"project": "p1", "timeline": "t1", "generation": "r13-seed",
            "revision_id": "base-r", "publication_id": "base-p"}
    saved = live.save_composition_after_update(client, seed)
    reopened = live.reopen_saved_composition(client, saved)
    assert saved["project_id"] == seed["project"]
    assert saved["timeline_id"] == seed["timeline"]
    assert saved["accepted_revision_id"] == "accepted-r"
    assert saved["publication_id"] == "pub-2"
    assert saved["document_sha256"] == hashlib.sha256(
        json.dumps(client.documents["accepted-r"]["payload"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert reopened["reopened"] is True


def test_reopen_rejects_an_accepted_revision_for_another_seed_timeline():
    client = CompositionClient()
    saved = live.save_composition_after_update(client, {"project": "p1", "timeline": "t1", "generation": "g",
                                                       "revision_id": "base-r", "publication_id": "base-p"})
    saved["project_id"] = ""
    with pytest.raises((ValueError, RuntimeError, KeyError)):
        live.reopen_saved_composition(client, saved)
