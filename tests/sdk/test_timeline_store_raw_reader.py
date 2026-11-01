"""Identity reads open production sessions without renderer dependencies."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from astrid.sdk import timeline
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.timeline import store
from astrid.sdk.timeline._common import TimelineEditError
from tests.sdk.test_timeline_restructure import restructure_runtime  # noqa: F401
from tests.sdk.test_timeline_restructure_guarded_runtime import (
    restructure_inverse_runtime,  # noqa: F401
)

pytest_plugins = ("tests.sdk.test_scene_save_core",)


def test_public_session_open_is_renderer_free_with_exact_runtime_base(
    restructure_inverse_runtime, tmp_path, request,  # noqa: F811
):
    env = restructure_inverse_runtime
    history = copy.deepcopy(env.client.timelines.history(env.project, "t", limit=200).data)
    stored = tuple(env.service.store.conn.iterdump())
    calls = copy.deepcopy(env.calls)
    request.getfixturevalue("no_renderer")

    opened = timeline.open(env.project, "t", client=env.client, outbox_path=tmp_path / "session.json")

    assert store.resolve_ids(env.project, "t", client=env.client) == (
        env.project, "t", history["head"]["revision_id"],
    )
    assert (opened.session.project_id, opened.session.timeline_id) == (env.project, "t")
    assert opened.head == history["head"]
    assert all(opened.bundle["base_parent"][key] == value for key, value in opened.head.items())
    assert env.client.timelines.history(env.project, "t", limit=200).data == history
    assert env.calls == calls
    assert tuple(env.service.store.conn.iterdump()) == stored


def test_resolve_ids_preserves_two_argument_custom_reader():
    calls = []

    def open_composition(project, ref):
        calls.append((project, ref))
        return DomainResult.success({
            "native_inspection": {"project_id": "p", "timeline_id": "t"},
            "summary": {"head_revision_id": "r"},
        })

    client = SimpleNamespace(timelines=SimpleNamespace(open_composition=open_composition))
    assert store.resolve_ids("project-slug", "timeline-slug", client=client) == ("p", "t", "r")
    assert calls == [("project-slug", "timeline-slug")]


def test_resolve_ids_does_not_retry_reader_type_error():
    calls = []

    def open_composition(project, ref, *, _raw_projection=False):
        calls.append((project, ref, _raw_projection))
        raise TypeError("reader failure")

    client = SimpleNamespace(timelines=SimpleNamespace(open_composition=open_composition))
    with pytest.raises(TypeError, match="reader failure"):
        store.resolve_ids("p", "t", client=client)
    assert calls == [("p", "t", True)]


@pytest.mark.parametrize("code", ["forbidden", "not_found"])
def test_resolve_ids_preserves_reader_refusals(code):
    calls = []

    def open_composition(project, ref, **kwargs):
        calls.append((project, ref, kwargs))
        return DomainResult.failure(ErrorObject(code, "read refused", {}))

    client = SimpleNamespace(timelines=SimpleNamespace(open_composition=open_composition))
    with pytest.raises(TimelineEditError, match=code):
        store.resolve_ids("p", "t", client=client)
    assert calls == [("p", "t", {"_raw_projection": True})]
