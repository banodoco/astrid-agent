"""Features that work on the live line and must stay on the combined trunk."""
from __future__ import annotations

import json

import pytest

from astrid.packs.timeline import cli as timeline_cli
from astrid.sdk.contracts import DomainResult, ErrorObject
from tests.sdk.test_timeline_commit_provider import runtime_abi  # noqa: F401
from tests.sdk.test_timeline_public_command import (
    public_session as durable_public_session,  # noqa: F401
)


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
    "commit": ["commit", "main", "--file", "candidate.json", "-m", "why", "--idempotency-key", "parity-key"],
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


def test_commit_requires_a_message(capsys) -> None:
    parser = timeline_cli.build_parser(_Client())
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["commit", "main", "--file", "candidate.json", "--project", "p"])
    assert exc.value.code == 2
    assert "-m/--message" in capsys.readouterr().err
    assert parser.parse_args([
        "commit", "main", "--file", "candidate.json", "--project", "p", "-m", "tighten c3",
        "--idempotency-key", "parity-key",
    ]).message == "tighten c3"


def test_commit_migration_preserves_message_contract(capsys) -> None:
    parser = timeline_cli.build_parser(_Client())
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["commit", "main", "--file", "candidate.json", "--project", "p"])
    assert exc.value.code == 2
    assert "-m/--message" in capsys.readouterr().err
    assert parser.parse_args([
        "commit", "main", "--file", "candidate.json", "--project", "p", "-m", "tighten c3",
        "--idempotency-key", "parity-key",
    ]).message == "tighten c3"


def test_place_element_cli_exposes_occurrence_target_and_forwards_arguments(capsys) -> None:
    class Timelines:
        def __init__(self):
            self.calls = []

        def place_element(self, project, ref, **kwargs):
            self.calls.append((project, ref, kwargs))
            return DomainResult.success({'target': kwargs['target']})

    timelines = Timelines()
    client = _Client()
    client.timelines = timelines
    parser = timeline_cli.build_parser(client)
    help_text = parser._subparsers._group_actions[0].choices['place-element'].format_help()
    normalized_help = ' '.join(help_text.split())
    assert '--target' in help_text
    assert 'occurrence:<occurrence-' in help_text and 'id>/clip:<clip-id>' in help_text
    assert 'effect elements on am-discord clips only' in normalized_help
    parsed = parser.parse_args([
        'place-element', 'film', '--project', 'p', '--realm', 'realm',
        '--target', 'occurrence:shot-1/clip:discord', '--pack', 'project_pack',
        '--element', 'smooth-zoom', '--revision', 'sha256:' + 'a' * 64,
        '--expected-head', 'head-1', '--idempotency-key', 'place-key',
    ])
    assert parsed.handler(parsed) == 0
    assert timelines.calls == [(
        'p', 'film', {
            'realm': 'realm', 'target': 'occurrence:shot-1/clip:discord',
            'element': {'packId': 'project_pack', 'id': 'smooth-zoom',
                        'kind': 'effect', 'revision': 'sha256:' + 'a' * 64},
            'expected_head': 'head-1', 'idempotency_key': 'place-key',
        },
    )]
    assert capsys.readouterr().out


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
    assert "tracks" not in calls[0]["config"]
    assert calls[0]["config"]["theme_overrides"]["visual"]["canvas"] == {
        "width": 1920, "height": 1080, "fps": 30,
    }


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


# Bounded frontend qualification: public session and provider, in-memory transport.
# These fixtures do not claim a real Runtime/HTTP journey.
@pytest.fixture
def durable_cli(durable_public_session, monkeypatch, tmp_path):  # noqa: F811 - imported fixture
    import copy
    from dataclasses import replace

    from astrid.sdk import storage_root
    from tests.sdk.test_timeline_commit_provider import DetachedPort

    opened, realm, sent, client, path = durable_public_session
    monkeypatch.setattr(storage_root, "resolve_runtime_data_root", lambda: tmp_path / "cli-data")
    monkeypatch.setattr(timeline_cli, "_edit_checkout", lambda *_args: pytest.fail("durable undo opened a local draft"))
    accept = DetachedPort.accept

    def full_v2_accept(port, prepared):
        result = accept(port, prepared)
        records = prepared["records"]
        for key in ("label", "steps", "merge_mode"):
            result["data"][key] = copy.deepcopy(records[key])
        result["receipt"]["result"] = copy.deepcopy(result["data"])
        entry = realm.entries[-1]
        entry["receipt"] = copy.deepcopy(result["receipt"])
        for key in ("label", "steps", "merge_mode"):
            entry[key] = copy.deepcopy(result["data"][key])
        entry["merge"] = port.context["request"].get("merge", "fields")
        entry["reads"] = copy.deepcopy(port.context["request"].get("reads", []))
        return result

    monkeypatch.setattr(DetachedPort, "accept", full_v2_accept)
    typed = client.timelines._typed

    def public_envelope(operation, *args, **kwargs):
        request = copy.deepcopy(args[-1]) if operation == "commit_timeline" else None
        result = typed(operation, *args, **kwargs)
        if operation == "commit_timeline":
            # The borrowed port normalizes optional rationale internally. Keep
            # the public request observation exactly as delivered by the caller.
            sent[-1] = request
            return replace(result, idempotency_key=kwargs["key"])
        return result

    monkeypatch.setattr(client.timelines, "_typed", public_envelope)

    def run(verb, *args):
        parsed = timeline_cli.build_parser(client).parse_args([
            verb, "film", "--project", "project-1", *args, "--json",
        ])
        return parsed.handler(parsed)

    return run, opened, realm, sent, client, path


def test_normal_commit_undo_preserves_full_v2_request_reply_and_history(durable_cli, capsys):
    import copy

    from tests.sdk.test_timeline_public_command import rename

    run, opened, realm, sent, client, _ = durable_cli
    original = copy.deepcopy(realm.documents[realm.head["revision_id"]])
    opened.command(rename(), label="Rename film", reads=[])
    accepted = opened.commit("Clarify film", reads=[])
    publication = accepted["receipt"]["receipt_id"]
    head = copy.deepcopy(realm.head)
    realm.reads.clear()
    assert run("undo", "--commit", publication, "--idempotency-key", "cli-undo") == 0
    output = json.loads(capsys.readouterr().out)
    request = sent[-1]
    assert request == {
        "api_version": 1, "project_id": "project-1", "timeline_id": "film",
        "base_revision_id": head["revision_id"], "base_publication_id": publication,
        "commands": [{"schema_version": 1, "op": "timeline.inverse", "version": "1",
                      "select": None, "args": {"publications": [publication]}}],
        "source": {"via": "cli"}, "dry_run": False, "label": "Undo timeline edit", "merge": "fields",
        "steps": [{"id": "inverse", "label": "Undo timeline edit", "intent": "", "tags": [],
                   "command_ordinals": [0], "patch": []}],
    }
    assert output["ok"] and output["idempotency_key"] == "cli-undo"
    data, receipt = output["data"], output["receipt"]
    assert data["canonical_document"] == original
    assert receipt["receipt_id"] == data["accepted_publication_id"]
    assert receipt["result"] == {key: value for key, value in data.items()
                                 if key not in {"accepted_publication_id", "show_command", "summary", "replayed", "confirmed", "undo"}}
    entry = realm.entries[-1]
    assert entry["receipt"] == receipt and entry["records"]["undo_of"] == [publication]
    assert entry["records"]["undo_steps"] is None
    assert entry["label"] == data["label"] == "Undo timeline edit"
    assert entry["records"]["steps"] == data["steps"]
    assert data["steps"][0]["patch"]
    assert all({"bindings", "dependencies", "reads", "writes", "derived_writes"} <= set(record)
               for record in entry["records"]["commands"])
    assert ("entry", publication) in realm.reads
    assert any(read[0] == "history" for read in realm.reads)
    listed = client.timelines.history("project-1", "film")
    assert listed.ok and listed.data["items"][-1] == entry


def test_normal_commit_step_undo_uses_existing_public_selector(durable_cli, capsys):
    import json

    from tests.sdk.test_timeline_public_command import rename

    run, opened, realm, sent, _, _ = durable_cli
    opened.command(rename(), label="Name", reads=[])
    opened.command(rename(before="Public edit", value="Final name"), label="Name again", reads=[])
    accepted = opened.commit("Two names", reads=[])
    publication = accepted["receipt"]["receipt_id"]
    selector = {publication: ["step-2"]}
    assert run("undo", "--commit", publication, "--steps", json.dumps(selector)) == 0
    output = json.loads(capsys.readouterr().out)
    assert sent[-1]["commands"][0]["args"] == {"publications": [publication], "steps": selector}
    assert realm.entries[-1]["records"]["undo_steps"] == selector
    assert output["data"]["canonical_document"]["parent_revision"]["payload"]["name"] == "Public edit"


@pytest.mark.parametrize("extra", [
    ("--draft", "main"), ("2",), ("--steps", "1"), ("--commit", "publication-1"),
    ("--steps", '{"other":["step-1"]}'),
])
def test_normal_commit_selector_refuses_mixed_local_options_before_write(durable_cli, capsys, extra):
    run, _, realm, sent, _, path = durable_cli
    before = path.read_bytes()
    assert run("undo", "--commit", "publication-1", *extra) == 1
    output = json.loads(capsys.readouterr().out)
    assert not output["ok"] and output["error"]["code"] == "timeline_edit_error"
    assert {"path", "head", "hint", "suggested_command"} <= output["error"]["details"].keys()
    assert sent == [] and len(realm.entries) == 1 and path.read_bytes() == before


@pytest.mark.parametrize("verb", ["undo", "redo"])
def test_normal_commit_preview_preserves_full_v2_without_acceptance(durable_cli, capsys, verb):
    import copy
    import json

    from tests.sdk.test_timeline_public_command import rename

    run, opened, realm, sent, _, _ = durable_cli
    opened.command(rename(), label="Name", reads=[])
    accepted = opened.commit("Name", reads=[])
    publication = accepted["receipt"]["receipt_id"]
    if verb == "redo":
        assert run("undo", "--commit", publication) == 0
        publication = json.loads(capsys.readouterr().out)["receipt"]["receipt_id"]
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    assert run(verb, "--commit", publication, "--dry-run", "--idempotency-key", "preview-inverse") == 0
    output = json.loads(capsys.readouterr().out)
    assert output["data"]["status"] == "preview" and output["data"]["would_status"] == "accepted"
    assert output["receipt"] is None and output["idempotency_key"] == "preview-inverse"
    assert output["data"]["steps"][0]["patch"]
    assert (realm.head, realm.entries, realm.documents) == before
    assert sent[-1]["commands"][0]["args"]["publications"] == [publication]


@pytest.mark.parametrize("failure", ["missing", "receipt", "label", "history"])
def test_normal_commit_inverse_authenticates_history_and_receipt_without_accepting(durable_cli, capsys, failure):
    import copy
    import json

    from tests.sdk.test_timeline_public_command import rename

    run, opened, realm, sent, _, _ = durable_cli
    opened.command(rename(), label="Name", reads=[])
    accepted = opened.commit("Name", reads=[])
    publication = accepted["receipt"]["receipt_id"]
    if failure == "missing":
        publication = "not-an-accepted-receipt"
    elif failure == "receipt":
        realm.entries[-1]["receipt"]["receipt_id"] = "wrong-receipt"
    elif failure == "label":
        realm.entries[-1]["records"]["label"] = "untrusted label"
    else:
        realm.entries[-1]["previous"]["publication_id"] = "missing-ancestor"
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    assert run("undo", "--commit", publication) == 1
    output = json.loads(capsys.readouterr().out)
    assert not output["ok"] and output["receipt"] is None
    assert {"path", "head", "hint", "suggested_command"} <= output["error"]["details"].keys()
    assert (realm.head, realm.entries, realm.documents) == before and len(sent) == 1


def test_normal_commit_redo_selects_the_accepted_undo_receipt(durable_cli, capsys):
    from tests.sdk.test_timeline_public_command import rename

    run, opened, realm, sent, _, _ = durable_cli
    opened.command(rename(), label="Name", reads=[])
    accepted = opened.commit("Name", reads=[])
    canonical = accepted["data"]["canonical_document"]
    assert run("undo", "--commit", accepted["receipt"]["receipt_id"]) == 0
    undone = json.loads(capsys.readouterr().out)
    publication = undone["receipt"]["receipt_id"]
    assert run("redo", "--commit", publication, "--idempotency-key", "cli-redo") == 0
    output = json.loads(capsys.readouterr().out)
    assert output["data"]["canonical_document"] == canonical
    assert output["receipt"]["idempotency_key"] == output["idempotency_key"] == "cli-redo"
    assert sent[-1]["base_publication_id"] == publication
    assert sent[-1]["commands"][0]["args"] == {"publications": [publication]}
    assert realm.entries[-1]["records"]["undo_of"] == [publication]


def test_normal_commit_undo_retry_keeps_exact_frozen_request(durable_cli, monkeypatch, capsys):
    import copy

    from astrid.sdk.workspace_client import WorkspaceClientError
    from tests.sdk.test_timeline_public_command import rename

    run, opened, realm, sent, client, _ = durable_cli
    opened.command(rename(), label="Name", reads=[])
    accepted = opened.commit("Name", reads=[])
    publication = accepted["receipt"]["receipt_id"]
    typed = client.timelines._typed
    deliveries = []
    retained = []

    def lost_ack(operation, *args, **kwargs):
        if operation != "commit_timeline":
            return typed(operation, *args, **kwargs)
        deliveries.append((copy.deepcopy(args[-1]), kwargs["idempotency_key"]))
        if retained:
            return retained[0]
        result = typed(operation, *args, **kwargs)
        retained.append(result)
        raise WorkspaceClientError(0, "transport_error", "lost acknowledgement", {})

    monkeypatch.setattr(client.timelines, "_typed", lost_ack)
    options = ("--commit", publication, "--idempotency-key", "retry-inverse")
    assert run("undo", *options) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]
    assert run("undo", *options) == 0
    output = json.loads(capsys.readouterr().out)
    assert deliveries[0] == deliveries[1]
    assert output["receipt"]["receipt_id"] == realm.entries[-1]["publication_id"]
    assert len(realm.entries) == 3 and len(sent) == 2


@pytest.mark.parametrize("reverse", [False, True], ids=["oldest-first", "reversed-refused"])
def test_normal_commit_batch_preserves_accepted_order(durable_cli, capsys, reverse):
    import copy

    from tests.sdk.test_timeline_public_command import rename

    run, opened, realm, sent, _, _ = durable_cli
    opened.command(rename(), label="First", reads=[])
    first = opened.commit("First", reads=[])
    opened.command(rename(before="Public edit", value="Final"), label="Second", reads=[])
    second = opened.commit("Second", reads=[])
    publications = [result["receipt"]["receipt_id"] for result in (first, second)]
    if reverse:
        publications.reverse()
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    rc = run("undo", "--commit", publications[0], "--commit", publications[1])
    output = json.loads(capsys.readouterr().out)
    if reverse:
        assert rc == 1 and not output["ok"]
        assert (realm.head, realm.entries, realm.documents) == before and len(sent) == 2
    else:
        assert rc == 0 and output["ok"]
        assert sent[-1]["commands"][0]["args"]["publications"] == publications
        assert output["data"]["canonical_document"]["parent_revision"]["payload"]["name"] == "Renamed Film"


@pytest.mark.parametrize("option", ["--publication", "--force", "--overwrite"])
def test_normal_commit_parser_has_no_retired_or_overwrite_options(durable_cli, option):
    run, _, realm, sent, _, _ = durable_cli
    with pytest.raises(SystemExit) as error:
        run("undo", option, "publication-1")
    assert error.value.code == 2 and sent == [] and len(realm.entries) == 1


@pytest.mark.parametrize("verb, key", [("undo", "undone"), ("redo", "redone")])
def test_normal_history_without_commit_keeps_local_numeric_steps(monkeypatch, capsys, verb, key):
    from types import SimpleNamespace

    from astrid.sdk.timeline.sync import TimelineSession

    counts = []
    local = SimpleNamespace(**{verb: lambda count: counts.append(count) or ["local edit"]})
    monkeypatch.setattr(timeline_cli, "_edit_checkout", lambda _parsed: local)
    monkeypatch.setattr(TimelineSession, "open", lambda *_args, **_kwargs: pytest.fail("local history opened Runtime session"))
    parsed = timeline_cli.build_parser(_Client()).parse_args([
        verb, "main", "--project", "p", "--steps", "2", "--json",
    ])
    assert parsed.handler(parsed) == 0
    output = json.loads(capsys.readouterr().out)
    assert counts == [2] and output["ok"] and output["data"] == {key: ["local edit"]}


# Parent coverage retained during the Release-1 + Timeline merge.
def test_publish_requires_a_message(capsys) -> None:
    parser = timeline_cli.build_parser(_Client())
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["publish", "main", "--project", "p"])
    assert exc.value.code == 2
    assert "-m/--message" in capsys.readouterr().err
    assert parser.parse_args(["publish", "main", "--project", "p", "-m", "tighten c3"]).message == "tighten c3"
