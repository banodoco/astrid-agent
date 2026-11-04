from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from astrid.core.cli.domain_media import build_parser
from astrid.core.contracts.schema import Output
from astrid.sdk.invocation import _kernel_invoke, _validate_generation_intent
from astrid.sdk.remote import RemoteGenerations, RemoteMedia
from astrid.sdk.results import Capability
from astrid.sdk.workspace_client import WorkspaceClient
from astrid_runtime_client.generated import WorkspaceClient as GeneratedWorkspaceClient


class _Runtime:
    """The three-provider fixture shared by both existing listing surfaces."""

    def __init__(self):
        self.rows = [
            {"generation_id": name, "object_id": f"sha256:{name}", "metadata": {"gen": gen}}
            for name, gen in (
                ("managed", {"kind": "image", "model": "X", "target": "managed", "tags": ["hero"]}),
                ("runpod", {"kind": "video", "model": "X", "target": "runpod", "tags": ["hero", "remote"]}),
                ("fal", {"kind": "video", "model": "Y", "target": "fal", "tags": ["remote"]}),
            )
        ]
        self.calls = []

    def _list(self, project, *, cursor=None, limit=50, **filters):
        assert project == "demo"
        self.calls.append({"cursor": cursor, "limit": limit, **filters})
        rows = [
            row for row in self.rows
            if all(
                value in row["metadata"]["gen"]["tags"]
                if name == "tag" else row["metadata"]["gen"][name] == value
                for name, value in filters.items()
            )
        ]
        return [rows, None]

    list_generations = _list
    list_project_objects = _list


@pytest.mark.parametrize("filters, expected", [
    ({}, ["managed", "runpod", "fal"]),
    ({"model": "X", "target": "runpod"}, ["runpod"]),
    ({"kind": "video", "model": "X", "target": "runpod", "tag": "hero"}, ["runpod"]),
    ({"kind": "image", "model": "X", "target": "runpod", "tag": "hero"}, []),
    ({"tag": "remote"}, ["runpod", "fal"]),
    ({"tag": "rem"}, []),
])
def test_generation_sdk_filters_compose_without_changing_records(filters, expected):
    runtime = _Runtime()

    result = RemoteGenerations(runtime).list("demo", **filters)

    assert result.ok
    assert [row["generation_id"] for row in result.data[0]] == expected
    assert all(row in runtime.rows for row in result.data[0])
    assert runtime.calls == [{"cursor": None, "limit": 50, **filters}]


@pytest.mark.parametrize("extra, expected", [
    ([], ["runpod"]),
    (["--kind", "video", "--tag", "hero"], ["runpod"]),
    (["--kind", "image", "--tag", "hero"], []),
])
def test_media_list_three_provider_fixture(capsys, extra, expected):
    runtime = _Runtime()
    parsed = build_parser(SimpleNamespace(media=RemoteMedia(runtime))).parse_args([
        "list", "--project", "demo", "--model", "X", "--target", "runpod", *extra, "--json",
    ])

    assert parsed.handler(parsed) == 0

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"]
    assert [row["generation_id"] for row in envelope["data"][0]] == expected
    assert len(runtime.calls) == 1


def test_workspace_generation_list_passes_filters_and_page_to_generated_client():
    calls = []
    client = object.__new__(WorkspaceClient)
    client._generated = SimpleNamespace(list_generations=lambda *args, **kwargs: (
        calls.append((args, kwargs)), ([], "next")
    )[1])

    result = client.list_generations("demo", cursor="first", limit=7, kind="video", model="X", target="runpod", tag="hero")

    assert result == [[], "next"]
    assert calls == [(("demo",), {"cursor": "first", "limit": 7, "kind": "video", "model": "X", "target": "runpod", "tag": "hero"})]
    client.list_generations("demo")
    assert calls[-1] == (("demo",), {"cursor": None, "limit": 50})


@pytest.mark.parametrize("filters", [
    {},
    {"kind": "video", "model": "X /?+&", "target": "runpod", "tag": "hero /?+&"},
])
def test_generation_filters_reach_real_generated_http_query(filters):
    calls = []
    wire = {
        "generation_id": "runpod", "project_id": "demo/a", "type": "video",
        "status": "completed", "metadata": {"gen": {"model": "X /?+&", "target": "runpod"}},
        "version": 1, "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z",
        "source_task_id": None,
    }

    def transport(method, path, headers, body):
        calls.append((method, path, body))
        return 200, {}, json.dumps({"items": [wire], "next_cursor": "next-cursor"}).encode()

    client = object.__new__(WorkspaceClient)
    client._generated = GeneratedWorkspaceClient("http://runtime", transport=transport)

    result = RemoteGenerations(client).list("demo/a", cursor="first /?+&", limit=7, **filters)

    assert result.ok, result.error
    assert result.data == [[wire], "next-cursor"]
    assert len(calls) == 1
    method, path, body = calls[0]
    assert (method, body) == ("GET", None)
    split = urlsplit(path)
    assert split.path == "/v1/projects/demo%2Fa/generations"
    assert parse_qs(split.query) == {
        "limit": ["7"], "cursor": ["first /?+&"],
        **{name: [value] for name, value in filters.items()},
    }


@pytest.mark.parametrize("origin", ["generated", "imported"])
def test_complete_gen_contract_survives_admission_and_effect_without_losing_metadata(origin):
    calls = []
    metadata = {
        "provenance": {"origin": origin, "receipt": "legacy-import"},
        "review": {"selected": True},
        "gen": {
            "kind": "video", "origin": origin, "generator": "vibecomfy",
            "model": "X", "mode": "t2v", "backend": "comfyui", "target": "runpod",
            "machine": "fixture-machine", "workflow_digest": "sha256:" + "a" * 64,
            "task_id": "task-original", "run_id": "run-original", "seed": 42,
            "cost_usd": 0.125, "duration_ms": 6000, "tags": ["hero", "remote"],
        },
    }
    intent = _validate_generation_intent({
        "version": 1, "modality": "video", "partial_success_policy": "reject",
        "groups": [{"group_key": "main", "selectors": [{"selector": "main-0", "ordinal": 0, "variant_key": "original"}]}],
        "metadata": metadata,
    }, modality="video")
    capability = Capability(
        id="generation.generate_video", capability_type="executor", native_kind="executor",
        handle=SimpleNamespace(), outputs=(Output(name="generated_videos", type="file", artifact_type="video/clip"),),
    )
    client = SimpleNamespace(tasks=SimpleNamespace(create=lambda **kwargs: (
        calls.append(kwargs), SimpleNamespace(ok=True, data={"run_id": "r", "task_id": "t", "attempt_id": "a"})
    )[1]))

    _kernel_invoke(capability, kind="executor", project="demo", inputs={}, outputs={}, generation_intent=intent, _client=client)

    assert calls[0]["generation_intent"]["metadata"] == metadata
    assert calls[0]["settlement_effect"]["payload"]["metadata"] == metadata
    # Validation and publication own copies, so later caller edits cannot alter
    # the frozen metadata sent for settlement.
    metadata["gen"]["tags"].append("later-edit")
    assert calls[0]["settlement_effect"]["payload"]["metadata"]["gen"]["tags"] == ["hero", "remote"]
