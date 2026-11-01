from __future__ import annotations

from typing import Any

import pytest

from astrid.sdk.remote import RemoteTimelines
from astrid.sdk.workspace_client import WorkspaceClient, WorkspaceClientError
from astrid_runtime_client import ApiError


def _client(generated: Any) -> WorkspaceClient:
    client = object.__new__(WorkspaceClient)
    client._generated = generated
    return client


def _request() -> dict[str, Any]:
    return {
        "api_version": 1,
        "project_id": "project-1",
        "timeline_id": "timeline-1",
        "base_revision_id": "revision-1",
        "base_publication_id": "publication-1",
        "commands": [
            {"schema_version": 1, "op": "clip.move", "version": "1", "select": None, "args": {}}
        ],
    }


def _receipt() -> dict[str, Any]:
    return {
        "receipt_id": "publication-2",
        "command_kind": "timeline.commit",
        "idempotency_key": "retry-1",
        "request_hash": "hash-1",
        "project_id": "project-1",
        "project_seq": [1, 1],
        "event_ids": ["event-1"],
        "result": {"status": "accepted"},
        "created_at": "2026-10-19T00:00:00Z",
    }


def test_workspace_client_commit_preserves_accepted_envelope_and_key() -> None:
    class Generated:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def commit_timeline(
            self, project: str, timeline: str, body: dict[str, Any], *, idempotency_key: str
        ):
            self.calls.append((project, timeline, body, idempotency_key))
            return {
                "data": {"status": "accepted", "accepted_revision_id": "revision-2"},
                "receipt": _receipt(),
            }

    generated = Generated()
    body = _request()
    result = _client(generated).commit_timeline(
        "project-1", "timeline-1", body, idempotency_key="retry-1"
    )

    assert result == {
        "data": {"status": "accepted", "accepted_revision_id": "revision-2"},
        "receipt": _receipt(),
    }
    assert generated.calls == [("project-1", "timeline-1", body, "retry-1")]


def test_workspace_client_commit_preserves_preview_null_receipt() -> None:
    class Generated:
        def commit_timeline(
            self, project: str, timeline: str, body: dict[str, Any], *, idempotency_key: str
        ):
            assert (project, timeline, body["project_id"], idempotency_key) == (
                "project-1",
                "timeline-1",
                "project-1",
                "preview-1",
            )
            return {
                "data": {
                    "status": "preview",
                    "evaluated_head": {
                        "revision_id": "revision-1",
                        "publication_id": "publication-1",
                    },
                },
                "receipt": None,
            }

    assert (
        _client(Generated()).commit_timeline(
            "project-1", "timeline-1", _request(), idempotency_key="preview-1"
        )["receipt"]
        is None
    )


def test_workspace_client_create_timeline_view_forwards_explicit_key() -> None:
    class Generated:
        def create_timeline_view(
            self,
            project: str,
            timeline: str,
            options: dict[str, Any],
            *,
            idempotency_key: str,
        ):
            assert (project, timeline, options, idempotency_key) == (
                "project-1",
                "timeline-1",
                {"revision_id": "revision-1", "formats": ["md"]},
                "view-retry-1",
            )
            return {"artifact_id": "artifact-1"}

    result = _client(Generated()).create_timeline_view(
        "project-1",
        "timeline-1",
        {"revision_id": "revision-1", "formats": ["md"]},
        idempotency_key="view-retry-1",
    )

    assert result == {"artifact_id": "artifact-1"}


def test_workspace_client_lists_exact_commit_history_page() -> None:
    class Generated:
        def list_timeline_commits(
            self,
            project: str,
            timeline: str,
            *,
            cursor: str | None,
            limit: int,
        ):
            assert (project, timeline, cursor, limit) == ("project-1", "timeline-1", "cursor-1", 7)
            return {
                "items": [],
                "next_cursor": None,
                "head": {"revision_id": "revision-2", "publication_id": "publication-2"},
            }

    page = _client(Generated()).list_timeline_commits(
        "project-1", "timeline-1", cursor="cursor-1", limit=7
    )
    assert page["head"] == {"revision_id": "revision-2", "publication_id": "publication-2"}


@pytest.mark.parametrize(
    ("status", "code", "details"),
    [
        (409, "conflict", {"status": "conflict", "reason": "overlap", "conflicts": []}),
        (503, "not_ready", {"reason": "runtime_unavailable"}),
    ],
)
def test_workspace_client_commit_keeps_typed_error_details(
    status: int, code: str, details: dict[str, Any]
) -> None:
    class Generated:
        def commit_timeline(self, *_args: Any, **_kwargs: Any):
            raise ApiError(status, code, "timeline commit failed", details=details)

    with pytest.raises(WorkspaceClientError) as caught:
        _client(Generated()).commit_timeline(
            "project-1", "timeline-1", _request(), idempotency_key="retry-1"
        )
    assert caught.value.status == status
    assert caught.value.code == code
    assert caught.value.details == details


def test_workspace_client_absent_commit_method_is_actionable() -> None:
    with pytest.raises(WorkspaceClientError) as caught:
        _client(object()).list_timeline_commits("project-1", "timeline-1")
    assert caught.value.status == 503
    assert caught.value.code == "not_ready"
    assert caught.value.details["reason"] == "runtime_unavailable"
    assert "generated Runtime client" in caught.value.message


def test_remote_timelines_commit_and_history_are_scoped() -> None:
    class Client:
        def __init__(self) -> None:
            self.commit_calls: list[tuple[Any, ...]] = []
            self.history_calls: list[tuple[Any, ...]] = []

        def list_timelines(self, project: str, *, cursor: str | None, limit: int):
            assert (project, cursor, limit) == ("project-ref", None, 50)
            return (
                [{"project_id": "project-1", "timeline_id": "timeline-1", "slug": "main"}],
                None,
            )

        def commit_timeline(self, *args: Any, **kwargs: Any):
            self.commit_calls.append((args, kwargs))
            return {"data": {"status": "preview"}, "receipt": None}

        def list_timeline_commits(self, *args: Any, **kwargs: Any):
            self.history_calls.append((args, kwargs))
            return {
                "items": [
                    {
                        "publication_id": "publication-1",
                        "revision_id": "revision-1",
                        "previous": None,
                    }
                ],
                "next_cursor": None,
                "head": {"revision_id": "revision-1", "publication_id": "publication-1"},
            }

    client = Client()
    timelines = RemoteTimelines(client)
    body = _request()
    committed = timelines.commit("project-ref", "main", body, idempotency_key="retry-1")
    history = timelines.history("project-ref", "main", limit=25)

    assert committed.ok and committed.data == {"status": "preview"}
    assert committed.receipt is None and committed.idempotency_key == "retry-1"
    assert client.commit_calls == [
        (("project-1", "timeline-1", body), {"idempotency_key": "retry-1"})
    ]
    assert history.ok and history.data["head"] == {
        "revision_id": "revision-1",
        "publication_id": "publication-1",
    }
    assert client.history_calls == [(("project-1", "timeline-1"), {"cursor": None, "limit": 25})]


def test_remote_timelines_commit_rejects_body_scope_mismatch() -> None:
    class Client:
        def list_timelines(self, project: str, *, cursor: str | None, limit: int):
            return ([{"project_id": "project-1", "timeline_id": "timeline-1"}], None)

        def commit_timeline(self, *_args: Any, **_kwargs: Any):
            raise AssertionError("scope mismatch must not reach Runtime")

    body = _request()
    body["project_id"] = "other-project"
    result = RemoteTimelines(Client()).commit(
        "project-ref", "timeline-1", body, idempotency_key="retry-1"
    )
    assert not result.ok
    assert result.error.code == "validation_error"
    assert result.idempotency_key == "retry-1"
