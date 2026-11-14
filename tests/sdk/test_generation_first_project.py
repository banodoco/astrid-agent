from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.exceptions import CapabilityPreconditionError
from astrid.sdk.generation import _resolve_invoke_destination

FIRST_PROJECT_NAME = "My first project"
NOTICE = f'Created and selected project "{FIRST_PROJECT_NAME}" for this generation.'


class _FakeProjects:
    """In-memory stand-in for the runtime projects family."""

    def __init__(self, rows=(), selected=None):
        self.rows = [dict(row) for row in rows]
        self.selected = selected
        self.created = []
        self.selects = []

    def list(self, *, cursor=None, limit=50):
        return DomainResult.success((list(self.rows), None))

    def create(self, *, slug, name, metadata=None, idempotency_key=None):
        row = {"project_id": f"P-{slug}", "slug": slug, "name": name}
        self.rows.append(row)
        self.created.append(row)
        return DomainResult.success({"project": row})

    def select(self, ref, *, scope="workspace", idempotency_key=None):
        for row in self.rows:
            if ref in (row.get("project_id"), row.get("slug")):
                self.selected = row
                self.selects.append(ref)
                return DomainResult.success({"project": row, "scope": scope})
        return DomainResult.failure(ErrorObject("not_found", f"no project {ref}", {}))

    def current(self):
        if self.selected is None:
            return DomainResult.failure(ErrorObject("not_found", "no project is selected", {}))
        return DomainResult.success({"project": self.selected, "scope": "workspace"})


def _client(projects):
    return SimpleNamespace(projects=projects)


def test_no_project_creates_and_selects_first_project(capsys):
    projects = _FakeProjects()
    client = _client(projects)

    out, selected = _resolve_invoke_destination(
        out="results", project=None, project_root=None, client=client,
    )

    assert out == "results"
    assert selected == "P-my-first-project"
    assert [row["name"] for row in projects.created] == [FIRST_PROJECT_NAME]
    assert projects.selects == ["my-first-project"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [NOTICE]


def test_existing_unselected_first_project_is_selected_not_duplicated(capsys):
    projects = _FakeProjects(rows=[
        {"project_id": "P-other", "slug": "other", "name": "Other"},
        {"project_id": "P-first", "slug": "first", "name": FIRST_PROJECT_NAME},
    ])
    client = _client(projects)

    _, selected = _resolve_invoke_destination(
        out=None, project=None, project_root=None, client=client,
    )

    assert selected == "P-first"
    assert projects.created == []
    assert projects.selects == ["first"]
    assert [row["name"] for row in projects.rows].count(FIRST_PROJECT_NAME) == 1
    assert capsys.readouterr().out == ""


def test_project_holding_starter_slug_under_other_name_is_selected(capsys):
    projects = _FakeProjects(rows=[
        {"project_id": "P-slug", "slug": "my-first-project", "name": "Renamed by a person"},
    ])
    client = _client(projects)

    out, selected = _resolve_invoke_destination(
        out="results", project=None, project_root=None, client=client,
    )

    assert (out, selected) == ("results", "P-slug")
    assert projects.created == []
    assert projects.selects == ["my-first-project"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        'Selected project "My first project" for this generation.'
    ]


def test_explicit_project_is_untouched(capsys):
    projects = _FakeProjects()
    client = _client(projects)

    out, selected = _resolve_invoke_destination(
        out=None, project="P-explicit", project_root=None, client=client,
    )

    assert (out, selected) == (None, "P-explicit")
    assert projects.created == []
    assert projects.selects == []
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_runtime_selected_project_is_untouched(capsys):
    projects = _FakeProjects(
        rows=[{"project_id": "P-selected", "slug": "demo", "name": "Demo"}],
        selected={"project_id": "P-selected", "slug": "demo", "name": "Demo"},
    )
    client = _client(projects)

    _, selected = _resolve_invoke_destination(
        out=None, project=None, project_root=None, client=client,
    )

    assert selected == "P-selected"
    assert projects.created == []
    assert projects.selects == []
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_no_runtime_keeps_today_error():
    with pytest.raises(CapabilityPreconditionError) as excinfo:
        _resolve_invoke_destination(out=None, project=None, project_root=None)

    assert str(excinfo.value) == (
        "project is required for generation; pass --project <project> "
        "or select a current project in the workspace runtime"
    )


def test_runtime_that_cannot_create_or_select_keeps_today_error(capsys):
    projects = SimpleNamespace(current=lambda: DomainResult.failure(
        ErrorObject("not_found", "no project is selected", {})
    ))
    client = _client(projects)

    with pytest.raises(CapabilityPreconditionError, match="project is required for generation"):
        _resolve_invoke_destination(out=None, project=None, project_root=None, client=client)
    assert capsys.readouterr().err == ""
