"""``publish_bundle`` writes through the connection that read the head (found by the F2 three-product journey).

It used to ask the launcher for a Runtime again at the write, so a caller that connected explicitly
(``ASTRID_TIMELINE_EVAL_*``) had its reads served by one Runtime and its publication sent to another, or failed with
"the Astrid Runtime is not installed". The fakes are the client's seams; no Runtime starts.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrid.sdk import AstridClient, authoring_bundle, autobootstrap
from astrid.sdk.timeline import store

CANDIDATE = {"project_id": "p", "timeline_id": "t", "base_parent": {"revision_id": "rev-0"}}


class FakeTransport:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, dict, str]] = []

    def publish_parent_composition(self, project_id, timeline_id, publication, *, idempotency_key):
        self.published.append((project_id, timeline_id, publication, idempotency_key))
        return {"data": {"new_head": "rev-1"}}


class FakeClient:
    def __init__(self) -> None:
        self.transport = FakeTransport()
        self._remote = SimpleNamespace(_transport=self.transport)
        self.timelines = SimpleNamespace(open_composition=lambda project, timeline: SimpleNamespace(ok=True, data={"summary": {"head_revision_id": "rev-0"}}))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def no_launcher_and_a_recording_publisher(monkeypatch):
    monkeypatch.setattr(autobootstrap, "ensure_runtime", lambda **kw: pytest.fail("the write asked the launcher for a Runtime"))

    def publish_authoring_candidate(candidate, writer, *, idempotency_key):
        return {"publication": writer.publish_parent_composition("p", "t", {"candidate": True}, idempotency_key=idempotency_key)}

    monkeypatch.setattr(authoring_bundle, "publish_authoring_candidate", publish_authoring_candidate)


def test_a_publish_with_no_client_writes_through_the_connection_it_opens(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(AstridClient, "open_from_launcher", classmethod(lambda cls, **kw: client))
    receipt = store.publish_bundle(dict(CANDIDATE), "key-1")
    assert receipt["new_head"] == "rev-1" and receipt["old_head"] == "rev-0"
    assert client.transport.published == [("p", "t", {"candidate": True}, "key-1")]


def test_a_publish_with_a_client_writes_through_that_client(monkeypatch):
    monkeypatch.setattr(AstridClient, "open_from_launcher", classmethod(lambda cls, **kw: pytest.fail("a second connection was opened")))
    client = FakeClient()
    receipt = store.publish_bundle(dict(CANDIDATE), "key-2", client=client)
    assert receipt["new_head"] == "rev-1"
    assert client.transport.published == [("p", "t", {"candidate": True}, "key-2")]
