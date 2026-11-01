"""Features that work on the live line and must stay on the combined trunk."""
from __future__ import annotations

import pytest

from astrid.packs.timeline import cli as timeline_cli
from astrid.sdk.contracts import DomainResult, ErrorObject


class _Projects:
    def __init__(self, project):
        self._project = project

    def current(self, **_kwargs):
        if self._project is None:
            return DomainResult(
                ok=False, data=None,
                error=ErrorObject("not_found", "no current project is selected", {}),
                receipt=None, idempotency_key="",
            )
        return DomainResult.success({"project": {"slug": self._project, "project_id": "P-" + self._project}})


class _Client:
    def __init__(self, project="demo"):
        self.projects = _Projects(project)


VERBS = {
    "checkout": ["checkout", "main"],
    "apply": ["apply", "main", "sheet.txt"],
    "assets": ["assets", "main"],
    "check": ["check", "main"],
    "discard": ["discard", "main"],
    "find": ["find", "main", "hello"],
    "lines": ["lines", "main"],
    "publish": ["publish", "main", "-m", "why"],
    "redo": ["redo", "main"],
    "status": ["status", "main"],
    "undo": ["undo", "main"],
}


@pytest.mark.parametrize("verb", sorted(VERBS))
def test_project_defaults_to_the_current_project(verb) -> None:
    parsed = timeline_cli.build_parser(_Client("demo")).parse_args(VERBS[verb])
    assert parsed.project == "P-demo"


@pytest.mark.parametrize("verb", sorted(VERBS))
def test_explicit_project_wins_and_is_not_looked_up(verb) -> None:
    class Boom(_Client):
        projects = None  # any lookup would fail

    parsed = timeline_cli.build_parser(Boom()).parse_args([*VERBS[verb], "--project", "other"])
    assert parsed.project == "other"


@pytest.mark.parametrize("verb", sorted(VERBS))
def test_no_current_project_is_a_clear_error(verb, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        timeline_cli.build_parser(_Client(None)).parse_args(VERBS[verb])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "no current project" in err and "--project" in err


def test_publish_requires_a_message(capsys) -> None:
    parser = timeline_cli.build_parser(_Client())
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["publish", "main", "--project", "p"])
    assert exc.value.code == 2
    assert "-m/--message" in capsys.readouterr().err
    assert parser.parse_args(["publish", "main", "--project", "p", "-m", "tighten c3"]).message == "tighten c3"


class _CreateTimelines:
    def __init__(self):
        self.calls = []

    def create_empty(self, **kwargs):
        self.calls.append(kwargs)
        return DomainResult.success({"timeline_id": kwargs["timeline_id"]})


def _create(argv, capsys):
    client = _Client()
    client.timelines = _CreateTimelines()
    parsed = timeline_cli.build_parser(client).parse_args(argv)
    return parsed.handler(parsed), client.timelines.calls, capsys.readouterr()


def test_create_slug_names_the_new_timeline(capsys) -> None:
    rc, calls, _ = _create(["create", "--project", "p", "--slug", "intro"], capsys)
    assert rc == 0 and calls[0]["timeline_id"] == "intro" and calls[0]["project"] == "p"


def test_create_slug_matching_the_positional_id_is_fine_a_different_one_is_refused(capsys) -> None:
    rc, calls, _ = _create(["create", "same", "--project", "p", "--slug", "same"], capsys)
    assert rc == 0 and calls[0]["timeline_id"] == "same"
    rc, calls, out = _create(["create", "one", "--project", "p", "--slug", "two"], capsys)
    assert rc == 2 and calls == [] and "not both" in out.err


def _local_checkout(tmp_path):
    from astrid.sdk.timeline_checkout import Checkout
    from tests.packs.timeline.test_cli_edit_flags import bundle

    path = tmp_path / "t.checkout.json"
    Checkout(bundle()).save(path)
    return path


def test_find_file_reads_a_local_checkout_without_a_project_or_runtime(tmp_path, capsys) -> None:
    path = _local_checkout(tmp_path)
    parsed = timeline_cli.build_parser(_Client(None)).parse_args(["find", "--file", str(path), "viral"])
    assert parsed.file == str(path)
    assert parsed.handler(parsed) == 0
    out = capsys.readouterr().out
    assert "viral" in out and "s1" in out


def test_find_file_with_a_filter_and_no_query(tmp_path, capsys) -> None:
    path = _local_checkout(tmp_path)
    parsed = timeline_cli.build_parser(_Client(None)).parse_args(["find", "--file", str(path), "--role", "voice"])
    assert parsed.handler(parsed) == 0
    assert "a-vo" in capsys.readouterr().out


def test_find_without_a_timeline_or_file_is_refused(capsys) -> None:
    parsed = timeline_cli.build_parser(_Client()).parse_args(["find", "--project", "p", "--text", "x"])
    assert parsed.handler(parsed) == 2
    assert "--file" in capsys.readouterr().err
