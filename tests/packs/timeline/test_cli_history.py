"""Offline checks: timelines history and the TL@ref forms of show and diff, with a fake client injected.

Nothing here touches the Runtime or the workspace.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from astrid.packs.timeline import cli, cli_history
from astrid.sdk import workspace_client
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.remote import RemoteAstridClient

A = "authoring-parent-revision-aaaa1111"
B = "authoring-parent-revision-bbbb2222"
C = "astrid.editor.timeline.cccc3333"


def _history_item(publication_id, revision_id, previous=None):
    return {
        "publication_id": publication_id,
        "revision_id": revision_id,
        "previous": previous,
        "receipt": {"created_at": f"{publication_id}-time"},
        "intent": "recorded",
        "records": {},
        "provider_version": "astrid-v1",
        "compiler_version": "compiler-v1",
        "publisher": "actor-1",
    }


class FakeTimelines:
    """Head A, which names B as parent; B names C; C names nothing. ``named=False``: no parent is named."""

    def __init__(self, *, named=True):
        self.parents = {A: B, B: C, C: None} if named else {A: None}
        self.opened = []

    def open_composition(self, project, ref, *, revision_id=None, limit=50, **_):
        self.opened.append(revision_id)
        rid = revision_id or A
        if rid not in self.parents:
            return DomainResult.failure(ErrorObject("not_found", "revision not found", {}))
        summary = {"head_revision_id": A, "revision_id": rid, "created_at": "2026-10-0%dT09:00:00Z" % (len(rid) % 9)}
        if self.parents[rid]:
            summary["parent_revision_id"] = self.parents[rid]
        return DomainResult.success({"summary": summary, "native_inspection": {}})


def _parsed(timelines, **extra):
    base = dict(client=SimpleNamespace(timelines=timelines), project="P", ref="TL", limit=50, json=False,
                published=False, from_revision=None, to_revision=None, from_version=None, to_version=None,
                revision_id=None, ref_revision=None)
    base.update(extra)
    return SimpleNamespace(**base)


def test_history_lists_newest_first_with_source_time_and_message(capsys):
    code = cli_history.run_history(_parsed(FakeTimelines()))
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[0].startswith("Revisions of TL")
    assert out[1].split()[0] == "aaaa1111"
    assert out[1].split()[1] == "astrid"
    assert out[2].split()[0] == "bbbb2222"
    assert out[3].split()[0] == "cccc3333"
    assert "app" in out[3]


def test_history_json_envelope(capsys):
    code = cli_history.run_history(_parsed(FakeTimelines(), json=True))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True
    assert payload["data"]["complete"] is True
    assert [r["id"] for r in payload["data"]["revisions"]] == [A, B, C]


def test_history_uses_selected_project(monkeypatch, capsys):
    seen = {}
    def record(parsed):
        seen["project"] = parsed.project
        return 0

    monkeypatch.setattr(cli_history, "run_history", record)
    parsed = SimpleNamespace(
        project=None,
        client=SimpleNamespace(projects=SimpleNamespace(current=lambda: DomainResult.success({
            "project": {"project_id": "P-selected"},
        }))),
    )
    assert cli._cmd_history(parsed) == 0
    assert seen["project"] == "P-selected"


def test_recent_list_uses_latest_revision_and_prints_project(monkeypatch, capsys):
    class Timelines:
        def list(self, project, **_kwargs):
            assert project == "P"
            return DomainResult.success([[
                {"timeline_id": "promo", "name": "Promo"},
                {"timeline_id": "yesterday", "name": "Yesterday cut"},
            ], None])

    class Projects:
        def current(self):
            return DomainResult.success({"project": {"project_id": "P", "slug": "demo"}})

    monkeypatch.setattr(
        "astrid.sdk.timeline.revisions.RuntimeRevisions.list",
        lambda _self, _project, timeline, *, limit: [SimpleNamespace(
            created_at="2026-10-30T12:00:00Z" if timeline == "yesterday" else "2026-10-29T12:00:00Z",
        )],
    )
    parsed = SimpleNamespace(
        client=SimpleNamespace(projects=Projects(), timelines=Timelines()),
        recent=True, all_projects=False, json=False, include_archived=False,
    )
    assert cli._cmd_list(parsed) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[1] == "2026-10-30T12:00:00Z  demo  yesterday  Yesterday cut"


def test_recent_list_explicit_project_overrides_selected_project(monkeypatch, capsys):
    calls = []

    class Projects:
        def show(self, ref):
            calls.append(("show", ref))
            return DomainResult.success({"project_id": "P-beta", "slug": "beta"})

        def current(self):
            calls.append(("current", None))
            return DomainResult.success({"project": {"project_id": "P-alpha", "slug": "alpha"}})

    class Timelines:
        def list(self, project, **_kwargs):
            calls.append(("list", project))
            assert project == "P-beta"
            return DomainResult.success([[{"timeline_id": "promo", "name": "Promo"}], None])

    monkeypatch.setattr(
        "astrid.sdk.timeline.revisions.RuntimeRevisions.list",
        lambda _self, project, timeline, *, limit: [SimpleNamespace(created_at="2026-10-30T12:00:00Z")],
    )
    parsed = SimpleNamespace(
        client=SimpleNamespace(projects=Projects(), timelines=Timelines()),
        project="beta", recent=True, all_projects=False, json=False, include_archived=False,
    )

    assert cli._cmd_list(parsed) == 0
    output = capsys.readouterr().out
    assert "2026-10-30T12:00:00Z  beta  promo  Promo" in output
    assert calls == [("show", "beta"), ("list", "P-beta")]


def test_recent_all_projects_follows_project_cursor_pages(monkeypatch, capsys):
    class Projects:
        def list(self, *, cursor=None, limit=50):
            assert limit == 50
            if cursor is None:
                return DomainResult.success([[{"project_id": "P-1", "slug": "first"}], "p2"])
            assert cursor == "p2"
            return DomainResult.success([[{"project_id": "P-2", "slug": "second"}], None])

    class Timelines:
        def list(self, project, **_kwargs):
            return DomainResult.success([[{"timeline_id": f"film-{project}", "name": "Film"}], None])

    monkeypatch.setattr(
        "astrid.sdk.timeline.revisions.RuntimeRevisions.list",
        lambda _self, project, _timeline, *, limit: [SimpleNamespace(created_at=project)],
    )
    parsed = SimpleNamespace(
        client=SimpleNamespace(projects=Projects(), timelines=Timelines()),
        recent=True, all_projects=True, json=False, include_archived=False,
    )

    assert cli._cmd_list(parsed) == 0
    output = capsys.readouterr().out
    assert "first  film-P-1" in output
    assert "second  film-P-2" in output


def test_history_prints_the_head_and_says_earlier_revisions_cannot_be_listed(capsys):
    code = cli_history.run_history(_parsed(FakeTimelines(named=False)))
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[1].split()[0] == "aaaa1111"  # the head row
    assert len(out) == 3
    assert out[2].startswith("earlier revisions: this Runtime cannot list them")
    assert "parent_revision_id" in out[2]


def test_history_json_when_the_runtime_cannot_list(capsys):
    code = cli_history.run_history(_parsed(FakeTimelines(named=False), json=True))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert [r["id"] for r in payload["data"]["revisions"]] == [A]
    assert payload["data"]["complete"] is False
    assert payload["data"]["reason"].startswith("earlier revisions: this Runtime cannot list them")


def test_history_json_complete_when_the_runtime_can_list(capsys):
    cli_history.run_history(_parsed(FakeTimelines(), json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["complete"] is True
    assert payload["data"]["reason"] is None


def test_history_routes_through_the_cli_handler(capsys):
    code = cli._cmd_history(_parsed(FakeTimelines(), limit=2))
    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert len(lines) == 3  # header plus the two newest


def test_history_uses_real_remote_facade_and_keeps_repeated_publications(monkeypatch, capsys):
    """Exercise RemoteAstridClient -> RemoteTimelines.history -> WorkspaceClient."""

    same = "authoring-parent-revision-same"
    later = "authoring-parent-revision-later"
    pages = {
        "timelines": ([{"project_id": "project-id", "timeline_id": "timeline-id", "slug": "main"}], None),
        "history": {
            "items": [
                _history_item("pub-0", same),
                _history_item("pub-1", same, {"publication_id": "pub-0", "revision_id": same}),
                _history_item("pub-2", later, {"publication_id": "pub-1", "revision_id": same}),
            ],
            "next_cursor": None,
            "head": {"revision_id": later, "publication_id": "pub-2"},
        },
    }

    class GeneratedFixture:
        def __init__(self, endpoint, token):
            self.endpoint = endpoint
            self.token = token

        def list_timelines(self, project_id, *, cursor=None, limit=50):
            assert (project_id, cursor, limit) == ("project", None, 50)
            return pages["timelines"]

        def list_timeline_commits(self, project_id, timeline_id, *, cursor=None, limit=50):
            assert (project_id, timeline_id, cursor, limit) == ("project-id", "timeline-id", None, 50)
            return pages["history"]

    monkeypatch.setattr(workspace_client, "GeneratedWorkspaceClient", GeneratedFixture)
    workspace = workspace_client.WorkspaceClient("http://127.0.0.1:8765", "token")

    parsed = _parsed(
        RemoteAstridClient(workspace).timelines,
        project="project",
        ref="main",
    )
    assert cli_history.run_history(parsed) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines[1:]] == ["later", "same", "same"]


def test_show_ref_resolves_prev_into_revision_id():
    timelines = FakeTimelines()
    parsed = _parsed(timelines, ref="TL@prev")
    assert cli_history.show_ref(parsed) is None
    assert parsed.ref == "TL"
    assert parsed.revision_id == B


def test_show_ref_head_keeps_the_working_head():
    parsed = _parsed(FakeTimelines(), ref="TL@head")
    assert cli_history.show_ref(parsed) is None
    assert parsed.ref == "TL" and parsed.revision_id is None


def test_show_ref_refuses_prev_when_no_parent_is_named(capsys):
    parsed = _parsed(FakeTimelines(named=False), ref="TL@prev")
    assert cli_history.show_ref(parsed) == 2
    assert "cannot list revisions" in capsys.readouterr().out


def test_diff_ref_sets_base_for_the_working_copy_and_from_for_published():
    working = _parsed(FakeTimelines(), ref="TL@-2")
    assert cli_history.diff_ref(working) is None
    assert working.ref == "TL" and working.ref_revision == C
    assert working.from_revision is None  # the working copy is the other side

    published = _parsed(FakeTimelines(), ref="TL@prev", published=True)
    assert cli_history.diff_ref(published) is None
    assert published.from_revision == B


def test_diff_ref_refuses_ref_plus_from(capsys):
    parsed = _parsed(FakeTimelines(), ref="TL@prev", published=True, from_revision=A)
    assert cli_history.diff_ref(parsed) == 2
    assert "not both" in capsys.readouterr().out
