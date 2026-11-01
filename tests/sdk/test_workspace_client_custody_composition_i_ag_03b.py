from types import SimpleNamespace

import pytest

from astrid.sdk.workspace_client import WorkspaceClient, WorkspaceClientError


def test_verified_media_and_filtered_object_operations_coexist():
    calls = []

    def record(name):
        def call(*args, **kwargs):
            calls.append((name, args, kwargs))
            return {"ok": True}
        return call

    client = WorkspaceClient("http://localhost:8000", "test-token")
    client._generated = SimpleNamespace(**{
        name: record(name) for name in (
            "get_project_media_selection", "get_project_verified_media",
            "get_project_verified_document", "get_project_verified_document_object",
            "commit_timeline", "list_timeline_commits", "list_project_objects", "list_generations",
        )
    })
    selection = {"object_id": "sha256:exact", "revision": 7}
    pin = {"timeline_id": "t", "revision": 3}
    client.get_project_media_selection("p", selection)
    client.get_project_verified_media("p", selection)
    client.get_project_verified_document("p", pin)
    client.get_project_verified_document_object("p", pin, "object")
    client.commit_timeline("p", "t", {"commands": []}, idempotency_key="key")
    client.list_timeline_commits("p", "t", cursor="next", limit=2)
    client.list_project_objects("p", kind="", model="m", target="x", tag="z", limit=4)
    client.list_generations("p", kind="", model="m", target="x", tag="z", limit=4)

    assert calls[0][1] == ("p", selection)
    assert calls[1][1] == ("p", selection)
    assert calls[2][1] == ("p", pin)
    assert calls[3][1] == ("p", pin, "object")
    assert calls[4][2] == {"idempotency_key": "key"}
    assert calls[5][2] == {"cursor": "next", "limit": 2}
    for _, _, kwargs in calls[6:]:
        assert kwargs == {"cursor": None, "limit": 4, "kind": "", "model": "m", "target": "x", "tag": "z"}

    client._generated = SimpleNamespace()
    with pytest.raises(WorkspaceClientError) as exc:
        client.get_project_verified_media("p", selection)
    assert exc.value.code == "not_ready"
    assert exc.value.details["operation"] == "get_project_verified_media"
