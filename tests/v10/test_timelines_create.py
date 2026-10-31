"""``timelines create``: the empty-timeline route from a fresh project.

The CLI verb is one SDK call (``client.timelines.create_empty``).  The SDK
method makes the runtime identity shell and then the first, empty
parent-composition head, because checkout, show, visualize, and render all
refuse a timeline that has no head.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from astrid.core.cli.domain_product import run_product_family
from astrid.core.ids import is_ulid
from astrid.core.receipts.contract import CommandReceipt
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.remote import RemoteTimelines
from astrid.sdk.workspace_client import WorkspaceClientError

ENVELOPE_KEYS = {"ok", "data", "error", "receipt", "idempotency_key"}
INTEGRATION_FIXTURES = {"runtime_create_case"}
DEFAULT_CONFIG = {
    "tracks": [],
    "theme_overrides": {
        "visual": {"canvas": {"width": 1920, "height": 1080, "fps": 30}}
    },
}


@pytest.fixture
def runtime_create_case(tmp_path, monkeypatch):
    """Use the current integration Runtime/TimelineSession bootstrap path."""
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "support"))
    from tests.helpers.runtime import use_runtime

    use_runtime()
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore

    from astrid.sdk.timeline import commit
    from astrid.sdk.workspace_client import WorkspaceClient

    root = tmp_path / "realm"
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    service.register_timeline_commit_provider(commit.create_runtime_commit_service())
    project = service.create_project({"slug": "render-smoke", "name": "Render smoke"})["id"]
    generated = SimpleNamespace()
    transport = WorkspaceClient.__new__(WorkspaceClient)
    transport._generated = generated
    transport._last_handshake = {"realm_id": service.realm["id"], "actor_id": "editor"}
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    timelines = RemoteTimelines(transport)
    timelines._resolve_timeline = lambda project_id, timeline: DomainResult.success(
        {"project_id": service.get_project_timeline(project_id, timeline)["project_id"],
         "timeline_id": timeline}
    )
    timelines.open_composition = lambda project_id, timeline, **_options: DomainResult.success({
        "native_inspection": {"project_id": project_id, "timeline_id": timeline},
        "summary": {"head_revision_id": service.get_project_timeline(project_id, timeline)["head_revision_id"]},
    })
    timelines.open_bundle = lambda project_id, timeline, revision_id=None, **_options: DomainResult.success({
        "bundle": commit.bundle_from_document(
            service._timeline_commits()[0].revision_closure(project_id, timeline, revision_id)
        )
    })

    def create_timeline(project_id, timeline, *, name=None, idempotency_key):
        calls.append(("create_timeline", (project_id, timeline), {
            "name": name, "idempotency_key": idempotency_key,
        }))
        return service.create_timeline(project_id, timeline, name=name, idempotency_key=idempotency_key)

    generated.create_timeline = create_timeline

    def list_timeline_commits(project_id, timeline, cursor=None, limit=200):
        calls.append(("list_timeline_commits", (project_id, timeline), {
            "cursor": cursor, "limit": limit,
        }))
        return service.list_timeline_commits(
            project_id, timeline, cursor=cursor, limit=limit, authorize=lambda: None
        )

    generated.list_timeline_commits = list_timeline_commits

    def commit_timeline(project_id, timeline, body, *, idempotency_key):
        calls.append(("commit_timeline", (project_id, timeline, body), {
            "idempotency_key": idempotency_key,
        }))
        return service.commit_timeline(
            project_id, timeline, body, idempotency_key=idempotency_key, publisher="editor",
            invocation_path=f"/v1/projects/{project_id}/timelines/{timeline}/commit",
            authorize=lambda: None,
        )

    generated.commit_timeline = commit_timeline
    retired_transport = Mock(side_effect=AssertionError("retired publication transport called"))
    retired_service = Mock(side_effect=AssertionError("retired Runtime publication called"))
    monkeypatch.setattr(transport, "publish_parent_composition", retired_transport)
    monkeypatch.setattr(service, "publish_parent_composition", retired_service)
    try:
        yield SimpleNamespace(service=service, project=project, timelines=timelines, calls=calls)
    finally:
        service.close()
        retired_transport.assert_not_called()
        retired_service.assert_not_called()


def _receipt(key: str) -> CommandReceipt:
    return CommandReceipt(
        command_kind="timeline.create",
        idempotency_key=key,
        project_id="P-1",
        receipt_id="txn-1",
        request_hash="0" * 64,
        result={},
        event_ids=(),
        project_seq=(1, 1),
        created_at="2026-10-09T12:00:00+00:00",
    )


class _RecordingTimelines:
    def __init__(self, owner: "_FakeClient") -> None:
        self._owner = owner

    def create_empty(self, *, project, timeline_id=None, config=None, idempotency_key=None):
        self._owner.calls.append(
            (
                "timelines.create_empty",
                {
                    "project": project,
                    "timeline_id": timeline_id,
                    "config": config,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"project_id": "P-1", "timeline_id": timeline_id or "T-1", "head_revision_id": "H-1", "created": True},
            receipt=_receipt(key),
            idempotency_key=key,
        )


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.timelines = _RecordingTimelines(self)


def _run(args: list[str], client: _FakeClient | None = None) -> int:
    return run_product_family("timelines", args, client=client or _FakeClient())


def test_timelines_create_is_one_sdk_call_with_default_canvas(capsys) -> None:
    client = _FakeClient()
    rc = _run(["create", "--project", "render-smoke", "--json"], client=client)
    assert rc == 0
    assert client.calls == [
        (
            "timelines.create_empty",
            {
                "project": "render-smoke",
                "timeline_id": None,
                "config": DEFAULT_CONFIG,
                "idempotency_key": None,
            },
        )
    ]
    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["ok"] is True
    assert envelope["data"]["head_revision_id"] == "H-1"


def test_timelines_create_human_output_is_short_and_actionable(capsys) -> None:
    client = _FakeClient()
    assert _run(["create", "--project", "render-smoke"], client=client) == 0
    output = capsys.readouterr().out
    assert "Created timeline T-1" in output
    assert "next: astrid timelines show T-1 --project render-smoke" in output


def test_timelines_create_forwards_id_canvas_fps_and_key(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        [
            "create",
            "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "--project",
            "render-smoke",
            "--canvas",
            "640x360",
            "--fps",
            "24",
            "--idempotency-key",
            "caller-key-1",
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    _, kwargs = client.calls[0]
    assert kwargs["timeline_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert kwargs["idempotency_key"] == "caller-key-1"
    assert kwargs["config"] == {
        "tracks": [],
        "theme_overrides": {"visual": {"canvas": {"width": 640, "height": 360, "fps": 24}}},
    }
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["idempotency_key"] == "caller-key-1"


def test_timelines_create_requires_project_flag() -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run(["create", "--json"], client=client)
    assert excinfo.value.code == 2
    assert client.calls == []


@pytest.mark.parametrize(
    "extra",
    [
        ["--canvas", "1920"],
        ["--canvas", "0x1080"],
        ["--canvas", "wide"],
        ["--fps", "0"],
        ["--fps", "thirty"],
    ],
)
def test_timelines_create_rejects_malformed_canvas_or_fps(extra: list[str]) -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run(["create", "--project", "render-smoke", *extra], client=client)
    assert excinfo.value.code == 2
    assert client.calls == []


def test_timelines_create_failure_envelope_exits_one(capsys) -> None:
    class _Conflict(_RecordingTimelines):
        def create_empty(self, **kwargs):
            return DomainResult.failure(
                ErrorObject("conflict", "timeline already exists", {"timeline_id": "T-1"}),
                idempotency_key="k",
            )

    client = _FakeClient()
    client.timelines = _Conflict(client)
    rc = _run(["create", "T-1", "--project", "render-smoke", "--json"], client=client)
    assert rc == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "conflict"


@pytest.mark.parametrize("argv", [["create", "--help"]])
def test_timelines_create_help_is_executable(argv: list[str]) -> None:
    from astrid.packs.timeline.cli import build_parser

    with pytest.raises(SystemExit) as excinfo:
        build_parser(_FakeClient()).parse_args(argv)
    assert excinfo.value.code == 0


# -- SDK route ------------------------------------------------------------


class _Writer:
    """Records the native shell/history/commit calls; no legacy writer exists."""

    _last_handshake = {"realm_id": "realm", "actor_id": "actor"}

    def __init__(self, *, shell_error: WorkspaceClientError | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._shell_error = shell_error
        self.history: list[dict[str, Any]] = []

    def create_timeline(self, project_id, timeline_id, *, idempotency_key, name=None):
        self.calls.append(("create_timeline", (project_id, timeline_id), {"idempotency_key": idempotency_key}))
        if self._shell_error is not None:
            raise self._shell_error
        return {
            "data": {"project_id": "P-1", "timeline_id": timeline_id,
                     "head_revision_id": "bootstrap", "head_publication_id": "bootstrap-publication"},
            "receipt": None,
        }

    def list_timeline_commits(self, project_id, timeline_id, *, cursor=None, limit=100):
        self.calls.append(("list_timeline_commits", (project_id, timeline_id),
                           {"cursor": cursor, "limit": limit}))
        return {"head": {"revision_id": "bootstrap", "publication_id": "bootstrap-publication"},
                "items": list(self.history), "next_cursor": None}


def test_remote_create_empty_creates_shell_then_native_commit(runtime_create_case) -> None:
    result = runtime_create_case.timelines.create_empty(
        project="render-smoke", timeline_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        config=DEFAULT_CONFIG, idempotency_key="caller-1",
    )
    assert result.ok, result.error
    assert result.data["project_id"] == runtime_create_case.project
    assert result.data["timeline_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert result.data["head_revision_id"] == result.data["canonical_document"]["parent_revision"]["revision_id"]
    assert result.data["created"] is True
    assert result.idempotency_key == "caller-1"
    assert runtime_create_case.calls[1:3] == [
        (
            "list_timeline_commits",
            (runtime_create_case.project, "01ARZ3NDEKTSV4RRFFQ69G5FAV"),
            {"cursor": None, "limit": 100},
        ),
        (
            "list_timeline_commits",
            (runtime_create_case.project, "01ARZ3NDEKTSV4RRFFQ69G5FAV"),
            {"cursor": None, "limit": 1},
        ),
    ]
    assert runtime_create_case.calls[0] == (
        "create_timeline", ("render-smoke", "01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        {"name": None, "idempotency_key": "caller-1-shell"},
    )
    assert runtime_create_case.calls[-1][0] == "commit_timeline"
    command = runtime_create_case.calls[-1][1][2]["commands"]
    assert runtime_create_case.calls[-1][2] == {"idempotency_key": "caller-1-head"}
    assert command[0]["op"] == "timeline.init"
    assert command[0]["args"] == {
        "name": "Main",
        "slug": "main",
        "output": {
            "resolution": "1280x720", "fps": 30, "file": "output.mp4",
            "background": None, "background_scale": None,
        },
    }
    history = runtime_create_case.timelines.history(runtime_create_case.project, result.data["timeline_id"], limit=100)
    assert history.ok
    assert result.data["head_revision_id"] == history.data["head"]["revision_id"]
    assert history.data["items"][-1]["receipt"]["result"]["steps"][0]["reads"] == [
        {"kind": "scope", "ref": "timeline", "path": []},
    ]


def test_remote_create_empty_generates_ulid_when_no_id_given(runtime_create_case) -> None:
    result = runtime_create_case.timelines.create_empty(project="render-smoke")
    assert result.ok, result.error
    timeline_key = result.data["timeline_id"]
    assert is_ulid(timeline_key)
    assert runtime_create_case.calls[0][1] == ("render-smoke", timeline_key)
    assert runtime_create_case.calls[1][0] == "list_timeline_commits"
    assert runtime_create_case.calls[-1][0] == "commit_timeline"
    assert result.data["canonical_document"]["parent_revision"]["payload"]["config"]["tracks"] == []


def test_remote_create_empty_emits_guarded_raw_config_and_replays_exact_receipt(runtime_create_case) -> None:
    config = {
        "tracks": [],
        "theme_overrides": {
            "visual": {"canvas": {"width": 640, "height": 360, "fps": 24}},
            "opaque_theme": [None, False],
        },
        "meta": {"org.example": {"null": None, "opaque": [None, False, 0]}},
    }
    result = runtime_create_case.timelines.create_empty(
        project="render-smoke", timeline_id="film", name="Second Cut",
        config=config, idempotency_key="raw-config",
    )
    assert result.ok, result.error
    assert runtime_create_case.calls[0] == (
        "create_timeline", ("render-smoke", "film"),
        {"name": "Second Cut", "idempotency_key": "raw-config-shell"},
    )
    payload = result.data["canonical_document"]["parent_revision"]["payload"]
    assert payload["name"] == "Second Cut" and payload["slug"] == "second-cut"
    assert payload["config"] == {
        "tracks": [], "theme_overrides": config["theme_overrides"], "meta": config["meta"],
        "output": {
            "resolution": "640x360", "fps": 24, "file": "output.mp4",
            "background": None, "background_scale": None,
        },
        "clips": [],
    }
    commit_calls_before_retry = [call for call in runtime_create_case.calls if call[0] == "commit_timeline"]
    assert any(command["op"] == "doc.patch" for command in commit_calls_before_retry[0][1][2]["commands"][1:])
    replayed = runtime_create_case.timelines.create_empty(
        project="render-smoke", timeline_id="film", name="Second Cut",
        config=config, idempotency_key="raw-config",
    )
    assert replayed.ok, replayed.error
    assert replayed.receipt == result.receipt
    assert replayed.data["canonical_document"] == result.data["canonical_document"]
    assert [call for call in runtime_create_case.calls if call[0] == "commit_timeline"] == commit_calls_before_retry


@pytest.mark.integration
def test_d69_upstream_remote_create_empty_generates_ulid_when_no_id_given(runtime_create_case) -> None:
    result = runtime_create_case.timelines.create_empty(project="render-smoke")
    assert result.ok, result.error
    timeline_id = result.data["timeline_id"]
    assert is_ulid(timeline_id)
    assert result.data["project_id"] == runtime_create_case.project
    assert result.data["head_revision_id"]
    shell, replay_read, head_read, head = runtime_create_case.calls
    assert shell == (
        "create_timeline", ("render-smoke", timeline_id),
        {"name": None, "idempotency_key": f"{result.idempotency_key}-shell"},
    )
    assert replay_read == (
        "list_timeline_commits", (runtime_create_case.project, timeline_id),
        {"cursor": None, "limit": 100},
    )
    assert head_read == (
        "list_timeline_commits", (runtime_create_case.project, timeline_id),
        {"cursor": None, "limit": 1},
    )
    name, args, kwargs = head
    assert name == "commit_timeline"
    assert args[:2] == (runtime_create_case.project, timeline_id)
    assert args[2]["project_id"] == runtime_create_case.project
    assert args[2]["timeline_id"] == timeline_id
    assert kwargs == {"idempotency_key": f"{result.idempotency_key}-head"}
    assert result.data["canonical_document"]["parent_revision"]["payload"]["config"] == {
        "tracks": [],
        "output": {
            "resolution": "1280x720", "fps": 30, "file": "output.mp4",
            "background": None, "background_scale": None,
        },
        "clips": [],
    }
    history = runtime_create_case.timelines.history(runtime_create_case.project, timeline_id, limit=100)
    assert history.ok
    assert result.data["head_revision_id"] == history.data["head"]["revision_id"]


def test_remote_create_empty_stops_when_shell_is_rejected() -> None:
    writer = _Writer(shell_error=WorkspaceClientError(409, "conflict", "timeline already exists"))
    result = RemoteTimelines(writer).create_empty(project="render-smoke", timeline_id="T-1", idempotency_key="k")
    assert not result.ok
    assert result.error.code == "conflict"
    assert [name for name, _, _ in writer.calls] == ["create_timeline"]


# D66 restore of upstream-924 selector 12306.  The old positive is retained
# beside the native commit case; no old-route retirement is inferred here.
@pytest.mark.integration
def test_d66_upstream_remote_create_empty_creates_shell_then_empty_head(runtime_create_case) -> None:
    result = runtime_create_case.timelines.create_empty(
        project="render-smoke",
        timeline_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        config=DEFAULT_CONFIG,
        idempotency_key="caller-1",
    )
    assert result.ok, result.error
    assert {
        key: result.data[key]
        for key in ("project_id", "timeline_id", "head_revision_id", "created")
    } == {
        "project_id": runtime_create_case.project,
        "timeline_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "head_revision_id": result.data["canonical_document"]["parent_revision"]["revision_id"],
        "created": True,
    }
    assert result.idempotency_key == "caller-1"
    assert runtime_create_case.calls[0] == (
        "create_timeline", ("render-smoke", "01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        {"name": None, "idempotency_key": "caller-1-shell"},
    )
    assert [call[0] for call in runtime_create_case.calls] == [
        "create_timeline", "list_timeline_commits", "list_timeline_commits", "commit_timeline",
    ]
    assert runtime_create_case.calls[1][1] == (
        runtime_create_case.project, "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    )
    assert runtime_create_case.calls[2][1] == runtime_create_case.calls[1][1]
    assert runtime_create_case.calls[1][2] == {"cursor": None, "limit": 100}
    assert runtime_create_case.calls[2][2] == {"cursor": None, "limit": 1}
    assert runtime_create_case.calls[-1][1][:2] == (
        runtime_create_case.project, "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    )
    assert runtime_create_case.calls[-1][1][2]["project_id"] == runtime_create_case.project
    assert runtime_create_case.calls[-1][1][2]["timeline_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert runtime_create_case.calls[-1][2] == {"idempotency_key": "caller-1-head"}
    assert runtime_create_case.calls[-1][1][2]["commands"][0]["args"] == {
        "name": "Main",
        "slug": "main",
        "output": {
            "resolution": "1280x720", "fps": 30, "file": "output.mp4",
            "background": None, "background_scale": None,
        },
    }
    payload = result.data["canonical_document"]["parent_revision"]["payload"]
    assert payload["name"] == "Main"
    assert payload["config"] == {
        **DEFAULT_CONFIG,
        "output": {
            "resolution": "1920x1080", "fps": 30, "file": "output.mp4",
            "background": None, "background_scale": None,
        },
        "clips": [],
    }
    assert payload["registry"] == {"assets": {}}
    assert payload["clips"] == []
    assert payload["occurrences"] == []
    assert result.data["canonical_document"]["shot_revisions"] == []
    assert result.data["canonical_document"]["internal_timeline_revisions"] == []
    history = runtime_create_case.timelines.history(runtime_create_case.project, result.data["timeline_id"], limit=100)
    assert history.ok
    assert result.data["head_revision_id"] == history.data["head"]["revision_id"]
    assert len(history.data["items"]) == 2
    assert history.data["items"][0]["intent"] == "bootstrap"
    assert runtime_create_case.calls[3][1][2]["base_revision_id"] == history.data["items"][0]["revision_id"]
    assert runtime_create_case.calls[3][1][2]["base_publication_id"] == history.data["items"][0]["publication_id"]
    assert len([item for item in history.data["items"] if item["intent"] == "recorded"]) == 1
    assert history.data["items"][1]["receipt"]["result"]["steps"][0]["reads"] == [
        {"kind": "scope", "ref": "timeline", "path": []},
    ]


def test_remote_create_empty_migration_preserves_shell_and_empty_head(runtime_create_case) -> None:
    result = runtime_create_case.timelines.create_empty(
        project="render-smoke",
        timeline_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        config=DEFAULT_CONFIG,
        idempotency_key="caller-1",
    )
    assert result.ok, result.error
    assert result.data["project_id"] == runtime_create_case.project
    assert result.data["timeline_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert result.data["head_revision_id"] == result.data["canonical_document"]["parent_revision"]["revision_id"]
    assert result.data["created"] is True
    assert result.idempotency_key == "caller-1"
    shell, replay_read, head_read, commit = runtime_create_case.calls
    assert shell == (
        "create_timeline",
        ("render-smoke", "01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        {"name": None, "idempotency_key": "caller-1-shell"},
    )
    assert replay_read[0] == head_read[0] == "list_timeline_commits"
    assert commit[0] == "commit_timeline"
    assert commit[1][:2] == (runtime_create_case.project, "01ARZ3NDEKTSV4RRFFQ69G5FAV")
    assert commit[1][2]["project_id"] == runtime_create_case.project
    assert commit[1][2]["timeline_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert commit[1][2]["commands"][0]["op"] == "timeline.init"
    assert commit[1][2]["commands"][0]["args"]["output"] == {
        "resolution": "1280x720", "fps": 30, "file": "output.mp4",
        "background": None, "background_scale": None,
    }
    assert commit[1][2]["commands"][0]["args"] == {
        "name": "Main",
        "slug": "main",
        "output": {
            "resolution": "1280x720", "fps": 30, "file": "output.mp4",
            "background": None, "background_scale": None,
        },
    }
    assert commit[1][2]["commands"][1:]
    assert commit[2] == {"idempotency_key": "caller-1-head"}
    payload = result.data["canonical_document"]["parent_revision"]["payload"]
    assert payload["name"] == "Main"
    assert payload["slug"] == "main"
    assert payload["config"] == {
        **DEFAULT_CONFIG,
        "output": {
            "resolution": "1920x1080", "fps": 30, "file": "output.mp4",
            "background": None, "background_scale": None,
        },
        "clips": [],
    }
    assert payload["registry"] == {"assets": {}}
    assert payload["clips"] == []
    assert payload["occurrences"] == []
    assert result.data["canonical_document"]["shot_revisions"] == []
    assert result.data["canonical_document"]["internal_timeline_revisions"] == []
    history = runtime_create_case.timelines.history(
        runtime_create_case.project, result.data["timeline_id"], limit=100,
    )
    assert history.ok
    assert result.data["head_revision_id"] == history.data["head"]["revision_id"]
    assert len(history.data["items"]) == 2
    assert history.data["items"][0]["intent"] == "bootstrap"
    assert commit[1][2]["base_revision_id"] == history.data["items"][0]["revision_id"]
    assert commit[1][2]["base_publication_id"] == history.data["items"][0]["publication_id"]
    assert len([item for item in history.data["items"] if item["intent"] == "recorded"]) == 1
    assert history.data["items"][1]["receipt"]["result"]["steps"][0]["reads"] == [
        {"kind": "scope", "ref": "timeline", "path": []},
    ]
