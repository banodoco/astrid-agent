from __future__ import annotations

from typing import Any

import pytest

from astrid.sdk.remote import RemoteMedia
from astrid.sdk.workspace_client import WorkspaceClient as AstridWorkspaceClient
from astrid.sdk.workspace_client import WorkspaceClientError


def _row(name: str) -> dict[str, Any]:
    return {
        "object_id": f"sha256:{name}",
        "digest": f"sha256:{name}",
        "filename": f"{name}.mp4",
        "media_type": "video/mp4",
    }


class _Runtime:
    """Runtime-call fake with the paired three-provider fixture and cursor scope."""

    def __init__(self) -> None:
        self.generations = {
            "sha256:managed": {"kind": "image", "model": "X", "target": "managed", "tags": ["hero", "local"]},
            "sha256:runpod": {"kind": "video", "model": "X", "target": "runpod", "tags": ["hero", "remote"]},
            "sha256:fal": {"kind": "video", "model": "Y", "target": "fal", "tags": ["remote"]},
            "sha256:runpod-2": {"kind": "video", "model": "X", "target": "runpod", "tags": ["hero", "remote"]},
        }
        self.rows = [_row("managed"), _row("runpod"), _row("fal"), _row("runpod-2")]
        self.calls: list[dict[str, Any]] = []
        self.cursor_filters: tuple[tuple[str, str], ...] | None = None

    def list_project_objects(self, project: str, *, cursor=None, limit=50, **filters):
        assert project == "demo"
        self.calls.append({"cursor": cursor, "limit": limit, **filters})
        normalized = tuple(sorted(filters.items()))
        if cursor is not None and normalized != self.cursor_filters:
            raise WorkspaceClientError(400, "invalid_cursor", "cursor filter scope changed", {})
        matched = [
            row
            for row in self.rows
            if all(
                self.generations[row["object_id"]].get(name) == value
                if name != "tag"
                else value in self.generations[row["object_id"]].get("tags", [])
                for name, value in filters.items()
            )
        ]
        start = 0
        if cursor is not None:
            start = 1
        page = matched[start : start + limit]
        next_cursor = None
        if start + len(page) < len(matched):
            self.cursor_filters = normalized
            next_cursor = "page-1"
        return page, next_cursor


def test_media_list_forwards_filters_preserves_rows_and_keeps_cursor_scope() -> None:
    runtime = _Runtime()
    media = RemoteMedia(runtime)

    filtered = media.list("demo", model="X", target="runpod")
    assert filtered.ok
    assert [row["object_id"] for row in filtered.data[0]] == ["sha256:runpod", "sha256:runpod-2"]
    assert runtime.calls[-1] == {
        "cursor": None,
        "limit": 50,
        "model": "X",
        "target": "runpod",
    }

    exact = media.list("demo", kind="video", model="X", target="runpod", tag="hero")
    assert exact.ok
    assert [row["object_id"] for row in exact.data[0]] == ["sha256:runpod", "sha256:runpod-2"]
    assert media.list("demo", target="RunPod").data[0] == []
    assert media.list("demo", tag="her").data[0] == []
    assert [row["object_id"] for row in media.list("demo").data[0]] == [
        "sha256:managed", "sha256:runpod", "sha256:fal", "sha256:runpod-2"
    ]

    first = media.list("demo", model="X", target="runpod", limit=1)
    assert first.ok
    assert [row["object_id"] for row in first.data[0]] == ["sha256:runpod"]
    cursor = first.data[1]
    assert cursor == "page-1"
    second = media.list("demo", model="X", target="runpod", cursor=cursor, limit=1)
    assert second.ok
    assert [row["object_id"] for row in second.data[0]] == ["sha256:runpod-2"]
    assert runtime.calls[-1] == {
        "cursor": cursor,
        "limit": 1,
        "model": "X",
        "target": "runpod",
    }

    rejected = media.list("demo", model="Y", target="runpod", cursor=cursor, limit=1)
    assert not rejected.ok
    assert rejected.error.code == "invalid_cursor"


def test_workspace_adapter_forwards_filters_to_generated_transport() -> None:
    calls = []
    client = object.__new__(AstridWorkspaceClient)
    client._generated = type(
        "Generated",
        (),
        {"list_project_objects": lambda _self, *args, **kwargs: (calls.append((args, kwargs)), ([], None))[1]},
    )()

    result = client.list_project_objects(
        "demo",
        cursor="cursor-1",
        limit=7,
        kind="video",
        model="X",
        target="runpod",
        tag="hero",
    )

    assert result == [[], None]
    assert calls == [
        (
            ("demo",),
            {
                "cursor": "cursor-1",
                "limit": 7,
                "kind": "video",
                "model": "X",
                "target": "runpod",
                "tag": "hero",
            },
        )
    ]
