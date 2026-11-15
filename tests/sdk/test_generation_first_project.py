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


def _listing_line(names):
    return ("Projects in this workspace: " + ", ".join(names)
            + "; select one with: astrid projects select <project>")


def test_other_projects_without_selection_refuse_and_change_nothing(capsys):
    projects = _FakeProjects(rows=[
        {"project_id": "P-a", "slug": "alpha", "name": "Alpha"},
        {"project_id": "P-b", "slug": "beta", "name": "Beta"},
    ])
    client = _client(projects)

    with pytest.raises(CapabilityPreconditionError) as excinfo:
        _resolve_invoke_destination(out=None, project=None, project_root=None, client=client)

    assert str(excinfo.value) == (
        "project is required for generation; pass --project <project> "
        "or select a current project in the workspace runtime\n"
        + _listing_line(["Alpha", "Beta"])
    )
    assert projects.created == []
    assert projects.selects == []
    assert capsys.readouterr().err == ""


def test_refusal_listing_caps_at_five_names(capsys):
    names = [f"Project {letter}" for letter in "ABCDEFG"]
    projects = _FakeProjects(rows=[
        {"project_id": f"P-{n}", "slug": n.lower().replace(" ", "-"), "name": n} for n in names
    ])
    client = _client(projects)

    with pytest.raises(CapabilityPreconditionError) as excinfo:
        _resolve_invoke_destination(out=None, project=None, project_root=None, client=client)

    detail = str(excinfo.value).splitlines()[1]
    assert detail == ("Projects in this workspace: " + ", ".join(names[:5])
                      + " (and 2 more); select one with: astrid projects select <project>")
    assert projects.created == [] and projects.selects == []


def test_empty_workspace_creates_and_selects_only_when_it_has_no_projects(capsys):
    projects = _FakeProjects()
    client = _client(projects)

    _, selected = _resolve_invoke_destination(
        out=None, project=None, project_root=None, client=client,
    )

    assert selected == "P-my-first-project"
    assert len(projects.created) == 1
    assert projects.selects == ["my-first-project"]


class _RacingProjects(_FakeProjects):
    """Another generation creates the starter between our list and our create."""

    def create(self, *, slug, name, metadata=None, idempotency_key=None):
        self.rows.append({"project_id": f"P-{slug}", "slug": slug, "name": name})
        return DomainResult.failure(ErrorObject("conflict", f"project slug {slug} already exists", {}))


def test_create_that_loses_a_race_reselects_the_starter_without_duplicating(capsys):
    projects = _RacingProjects()
    client = _client(projects)

    _, selected = _resolve_invoke_destination(
        out=None, project=None, project_root=None, client=client,
    )

    assert selected == "P-my-first-project"
    assert projects.created == []
    assert projects.selects == ["my-first-project"]
    assert [row["slug"] for row in projects.rows] == ["my-first-project"]
    assert capsys.readouterr().err.splitlines() == [
        'Selected project "My first project" for this generation.'
    ]


def test_failed_create_without_a_starter_still_raises(capsys):
    class _Broken(_FakeProjects):
        def create(self, *, slug, name, metadata=None, idempotency_key=None):
            return DomainResult.failure(ErrorObject("internal", "store unavailable", {}))

    projects = _Broken()
    client = _client(projects)

    with pytest.raises(CapabilityPreconditionError, match="store unavailable"):
        _resolve_invoke_destination(out=None, project=None, project_root=None, client=client)
    assert projects.selects == []


def test_dry_run_in_empty_workspace_succeeds_creates_nothing_and_says_what_a_run_would_do(capsys):
    projects = _FakeProjects()
    client = _client(projects)

    out, selected = _resolve_invoke_destination(
        out="results", project=None, project_root=None, client=client, dry_run=True,
    )

    assert (out, selected) == ("results", None)
    assert projects.rows == [] and projects.created == []
    assert projects.selects == [] and projects.selected is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        'Dry run: a real generation would create and select project "My first project".'
    ]


def test_dry_run_reads_an_existing_starter_without_selecting_it(capsys):
    projects = _FakeProjects(rows=[
        {"project_id": "P-first", "slug": "first", "name": FIRST_PROJECT_NAME},
    ])
    client = _client(projects)

    _, selected = _resolve_invoke_destination(
        out=None, project=None, project_root=None, client=client, dry_run=True,
    )

    assert selected == "P-first"
    assert projects.created == [] and projects.selects == []
    assert projects.selected is None
    assert capsys.readouterr().err == ""


def test_describe_never_creates_or_selects_a_project(monkeypatch):
    import astrid.core.generation.entries as entries
    from astrid.sdk.generation import GenerationFacade

    monkeypatch.setattr(entries, "describe_entry", lambda *a, **k: {"entry": "demo"})
    projects = _FakeProjects()
    client = _client(projects)

    described = GenerationFacade().describe("demo", client=client)

    assert described == {"entry": "demo"}
    assert projects.rows == [] and projects.created == [] and projects.selects == []
    assert projects.selected is None
