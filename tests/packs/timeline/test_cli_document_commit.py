from __future__ import annotations

import copy
import json
import sys
from types import SimpleNamespace

import pytest

from astrid.packs.timeline import cli as timeline_cli
from astrid.sdk import timeline
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.remote import RemoteTimelines
from astrid.sdk.timeline.commit import bundle_from_document, create_runtime_commit_service
from tests.helpers.runtime import use_runtime
from tests.sdk.test_timeline_commit_provider import runtime_abi  # noqa: F401


@pytest.fixture
def public_session(tmp_path, runtime_abi):  # noqa: F811
    """Use the real Runtime bootstrap and authenticated commit provider path."""
    use_runtime()
    for name in ("runtime_protocol", "runtime_protocol.contract_metadata",
                 "runtime_protocol.timeline_commit"):
        sys.modules.pop(name, None)

    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore

    root = tmp_path / "realm"
    RealmStore.initialize(root, realm_id="realm-1").close()
    service = RuntimeService(root)
    service.register_timeline_commit_provider(create_runtime_commit_service())
    project = service.create_project(
        {"slug": "project-1", "name": "Project 1"}, idempotency_key="project-1",
    )["id"]
    service.create_timeline(project, "film", idempotency_key="film")
    sent = []

    class RuntimeRealm:
        def __init__(self):
            self.project, self.timeline = project, "film"
            self.calls = []
            self.history_limits = []
            self.requests = []

        @property
        def head(self):
            return service.list_timeline_commits(
                self.project, self.timeline, authorize=lambda: None,
            )["head"]

        @property
        def entries(self):
            return service.list_timeline_commits(
                self.project, self.timeline, authorize=lambda: None,
            )["items"]

        @property
        def documents(self):
            commits, _ = service._timeline_commits()
            return {
                entry["revision_id"]: commits.revision_closure(
                    self.project, self.timeline, entry["revision_id"],
                )
                for entry in self.entries
            }

    realm = RuntimeRealm()

    class Reader(RemoteTimelines):
        def _resolve_timeline(self, *_args):
            return DomainResult.success({
                "project_id": project, "timeline_id": "film",
            })

        def open_composition(self, *_args, revision_id=None, **_kwargs):
            head = realm.head["revision_id"]
            return DomainResult.success({
                "native_inspection": {
                    "project_id": project, "timeline_id": "film",
                },
                "summary": {
                    "head_revision_id": head,
                    "revision_id": revision_id or head,
                },
            })

        def _typed(self, operation, requested_project, *args, **kwargs):
            timeline_id = args[0] if args else "film"
            try:
                if operation == "list_timeline_commits":
                    realm.history_limits.append(kwargs.get("limit", 50))
                    result = service.list_timeline_commits(
                        requested_project, timeline_id,
                        cursor=kwargs.get("cursor"), limit=kwargs.get("limit", 50),
                        authorize=lambda: None,
                    )
                    return DomainResult.success(result)
                if operation == "commit_timeline":
                    body = copy.deepcopy(args[1])
                    realm.calls.append(kwargs["idempotency_key"])
                    realm.requests.append(copy.deepcopy(body))
                    before = realm.head
                    result = service.commit_timeline(
                        requested_project, timeline_id, body,
                        idempotency_key=kwargs["idempotency_key"],
                        publisher="actor-1",
                        invocation_path=f"/v1/projects/{requested_project}/timelines/{timeline_id}/commit",
                        authorize=lambda: None,
                    )
                    if body.get("dry_run") or realm.head != before:
                        sent.append(body)
                    from astrid.core.receipts.contract import CommandReceipt

                    receipt = CommandReceipt.from_dict(result["receipt"]) if result["receipt"] else None
                    return DomainResult.success(result["data"], receipt=receipt)
                if operation == "get_project_parent_composition_revision":
                    result = service.get_project_parent_composition_revision(
                        requested_project, args[0], args[-1],
                    )
                    return DomainResult.success(result)
                if operation == "get_project_shot_revision":
                    result = service.get_project_shot_revision(
                        requested_project, args[0], args[-1],
                    )
                    return DomainResult.success(result)
                if operation == "get_project_timeline_revision":
                    result = service.get_project_timeline_revision(
                        requested_project, args[0], args[-1],
                    )
                    return DomainResult.success(result)
                raise AssertionError(f"unexpected Runtime operation {operation!r}")
            except RuntimeErrorBase as exc:
                return DomainResult.failure(ErrorObject(
                    exc.code, exc.message, exc.details or {},
                ))

    reader = Reader(object())
    client = SimpleNamespace(
        timelines=reader,
        _remote=SimpleNamespace(_transport=SimpleNamespace(
            _last_handshake={"realm_id": "realm-1", "actor_id": "actor-1"},
        )),
    )
    path = tmp_path / "outbox.json"
    opened = timeline.open(project, "film", client=client, outbox_path=path)
    assert "_runtime_document" not in opened.bundle["source_mapping"]
    try:
        yield opened, realm, sent, client, path
    finally:
        service.close()


def _document_file(tmp_path, realm):
    candidate = bundle_from_document(copy.deepcopy(realm.documents[realm.head["revision_id"]]))
    candidate["base_parent"]["publication_id"] = realm.head["publication_id"]
    candidate["source_mapping"].pop("_runtime_document", None)
    path = tmp_path / "timeline.json"
    path.write_text(json.dumps(candidate), encoding="utf-8")
    return path


def _run(client, path, *, message="Rename film", key="document-1", dry_run=False):
    args = [
        "commit", "film", "--project", "project-1", "--file", str(path),
        "--message", message, "--idempotency-key", key, "--json",
    ]
    if dry_run:
        args.append("--dry-run")
    parsed = timeline_cli.build_parser(client).parse_args(args)
    return parsed.handler(parsed)


def test_document_commit_uses_the_authenticated_commit_door(public_session, tmp_path, capsys):
    _opened, realm, sent, client, _outbox = public_session
    path = _document_file(tmp_path, realm)
    candidate = json.loads(path.read_text(encoding="utf-8"))
    candidate["parent"]["name"] = "Committed Film"
    path.write_text(json.dumps(candidate), encoding="utf-8")

    assert _run(client, path) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["data"]["status"] == "accepted"
    assert sent[-1]["label"] == "Rename film"
    assert output["receipt"]["idempotency_key"] == "document-1"
    assert sent[-1]["merge"] == "atomic"
    assert sent[-1]["label"] == "Rename film"
    assert sent[-1]["steps"][0]["reads"]
    assert path.with_suffix(".publication.json").exists() is False


def test_document_commit_retry_uses_runtime_idempotency_without_history_scan(
    public_session, tmp_path, capsys, monkeypatch,
):
    _opened, realm, sent, client, _outbox = public_session
    calls = realm.calls
    path = _document_file(tmp_path, realm)
    candidate = json.loads(path.read_text(encoding="utf-8"))
    candidate["parent"]["name"] = "Retryable Film"
    path.write_text(json.dumps(candidate), encoding="utf-8")

    assert _run(client, path, key="retry-document") == 0
    first = json.loads(capsys.readouterr().out)
    assert _run(client, path, key="retry-document") == 0
    second = json.loads(capsys.readouterr().out)
    assert second == first
    assert len(realm.entries) == 2
    assert len(sent) == 1  # Runtime replay returns the original accepted result.
    assert calls == ["retry-document", "retry-document"]


def test_document_commit_retry_does_not_scan_a_long_history_page(
    public_session, tmp_path, capsys, monkeypatch,
):
    _opened, realm, sent, client, _outbox = public_session
    history_limits = realm.history_limits
    calls = realm.calls
    path = _document_file(tmp_path, realm)
    candidate = json.loads(path.read_text(encoding="utf-8"))
    candidate["parent"]["name"] = "Long history retry"
    path.write_text(json.dumps(candidate), encoding="utf-8")

    assert _run(client, path, key="long-history") == 0
    capsys.readouterr()
    assert _run(client, path, key="long-history") == 0
    capsys.readouterr()
    assert len(realm.entries) == 2 and len(sent) == 1
    assert calls == ["long-history", "long-history"]
    assert history_limits and 200 not in history_limits


def test_document_commit_empty_key_refuses_before_outbox_flight(public_session, tmp_path, capsys):
    _opened, realm, sent, client, _outbox = public_session
    path = _document_file(tmp_path, realm)

    assert _run(client, path, key="   ") == 1
    output = json.loads(capsys.readouterr().out)
    assert output["error"]["code"] == "validation_error"
    assert sent == [] and len(realm.entries) == 1
    assert path.with_suffix(".outbox.json").exists() is False


def test_document_commit_clean_raw_empty_app_is_a_noop(public_session, tmp_path, capsys):
    _opened, realm, sent, client, _outbox = public_session
    path = _document_file(tmp_path, realm)
    candidate = json.loads(path.read_text(encoding="utf-8"))
    candidate["parent"]["config"]["app"] = {}
    path.write_text(json.dumps(candidate), encoding="utf-8")

    assert _run(client, path, key="clean-empty-app") == 0
    output = json.loads(capsys.readouterr().out)
    assert output["data"]["status"] == "unchanged"
    assert output["receipt"] is None
    assert sent == [] and len(realm.entries) == 1


def test_document_commit_preserves_unknown_parent_app_data(public_session, tmp_path, capsys):
    _opened, realm, sent, client, _outbox = public_session
    path = _document_file(tmp_path, realm)
    candidate = json.loads(path.read_text(encoding="utf-8"))
    candidate["parent"]["config"]["app"] = {"vendor": {"keep": None, "order": ["a", "b"]}}
    path.write_text(json.dumps(candidate), encoding="utf-8")

    assert _run(client, path, key="unknown-app") == 0
    json.loads(capsys.readouterr().out)
    document = realm.documents[realm.head["revision_id"]]
    assert document["parent_revision"]["payload"]["config"]["app"] == {
        "vendor": {"keep": None, "order": ["a", "b"]}
    }
    assert sent[-1]["label"] == "Rename film"


def test_document_commit_human_output_keeps_label_and_receipt(public_session, tmp_path, capsys):
    _opened, realm, sent, client, _outbox = public_session
    path = _document_file(tmp_path, realm)
    candidate = json.loads(path.read_text(encoding="utf-8"))
    candidate["parent"]["name"] = "Human Film"
    path.write_text(json.dumps(candidate), encoding="utf-8")
    parsed = timeline_cli.build_parser(client).parse_args([
        "commit", "film", "--project", "project-1", "--file", str(path),
        "-m", "Human Film", "--idempotency-key", "human-output",
    ])

    assert parsed.handler(parsed) == 0
    output = capsys.readouterr().out
    assert "accepted revision" in output and "Human Film" in output
    assert realm.entries[-1]["receipt"]["receipt_id"] in output
    assert "canonical_document" not in output and len(output) < 1000
    assert sent[-1]["label"] == "Human Film"


def test_document_commit_changed_body_with_same_key_is_runtime_refusal(
    public_session, tmp_path, capsys, monkeypatch,
):
    _opened, realm, sent, client, _outbox = public_session
    calls = realm.calls
    path = _document_file(tmp_path, realm)
    candidate = json.loads(path.read_text(encoding="utf-8"))
    candidate["parent"]["name"] = "First body"
    path.write_text(json.dumps(candidate), encoding="utf-8")

    assert _run(client, path, key="same-key") == 0
    capsys.readouterr()
    candidate["parent"]["name"] = "Changed body"
    path.write_text(json.dumps(candidate), encoding="utf-8")
    assert _run(client, path, key="same-key") == 1
    output = json.loads(capsys.readouterr().out)
    assert output["error"]["details"]["reason"] == "idempotency_mismatch"
    assert len(realm.entries) == 2 and len(sent) == 1
    assert calls == ["same-key", "same-key"]


def test_document_commit_preview_has_no_accepted_receipt(public_session, tmp_path, capsys):
    _opened, realm, sent, client, _outbox = public_session
    path = _document_file(tmp_path, realm)
    candidate = json.loads(path.read_text(encoding="utf-8"))
    candidate["parent"]["name"] = "Preview Film"
    path.write_text(json.dumps(candidate), encoding="utf-8")
    before = path.read_bytes()

    assert _run(client, path, key="preview-document", dry_run=True) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["data"]["status"] == "preview"
    assert output["data"]["would_status"] == "accepted"
    assert output["receipt"] is None
    assert len(realm.entries) == 1
    assert sent[-1]["dry_run"] is True
    assert path.read_bytes() == before


def test_document_commit_requires_a_nonempty_message_and_key(public_session, tmp_path, capsys):
    _opened, realm, _sent, client, _outbox = public_session
    path = _document_file(tmp_path, realm)
    parser = timeline_cli.build_parser(client)

    empty = parser.parse_args([
        "commit", "film", "--project", "project-1", "--file", str(path),
        "-m", "", "--idempotency-key", "key", "--json",
    ])
    assert empty.handler(empty) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "validation_error"
    with pytest.raises(SystemExit):
        parser.parse_args([
            "commit", "film", "--project", "project-1", "--file", str(path),
            "-m", "label", "--json",
        ])


def test_document_commit_refuses_a_stale_base_pair(public_session, tmp_path, capsys):
    _opened, realm, _sent, client, _outbox = public_session
    path = _document_file(tmp_path, realm)
    candidate = json.loads(path.read_text(encoding="utf-8"))
    candidate["base_parent"]["publication_id"] = "publication-stale"
    path.write_text(json.dumps(candidate), encoding="utf-8")

    assert _run(client, path, key="stale-document") == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["error"]["details"]["path"] == "/file"
    assert len(realm.entries) == 1


def test_document_commit_parser_uses_current_project_for_file_edits():
    class Projects:
        @staticmethod
        def current():
            from astrid.sdk.contracts import DomainResult

            return DomainResult.success({"project": {"project_id": "current-project"}})

    class Client:
        projects = Projects()

    parsed = timeline_cli.build_parser(Client()).parse_args([
        "commit", "film", "--file", "timeline.json", "-m", "label", "--idempotency-key", "key",
    ])
    assert parsed.project == "current-project"

    class NoCurrentProjects:
        @staticmethod
        def current():
            from astrid.sdk.contracts import DomainResult, ErrorObject

            return DomainResult.failure(ErrorObject("no-current-project", "none", {}))

    class NoCurrentClient:
        projects = NoCurrentProjects()

    with pytest.raises(SystemExit):
        timeline_cli.build_parser(NoCurrentClient()).parse_args([
            "commit", "film", "--file", "timeline.json", "-m", "label", "--idempotency-key", "key",
        ])
