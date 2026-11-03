from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrid.packs.timeline import cli as timeline_cli
from astrid.packs.timeline import cli_pending


class _Client:
    projects = None


class _Timeline:
    def __init__(self):
        self.calls = []
        self.records = [{"id": "pc-one", "key": "recut", "state": "pending"}]
        self.retry_result = {
            "status": "preview", "pending_id": "pc-one",
            "data": {"commands": [], "diff": {"changes": []},
                     "head": {"revision_id": "r2", "publication_id": "p2"},
                     "retry": {"token": "fresh-token"}},
            "summary": "Ready for review", "next": "tl.commit(r)",
        }

    def pending_list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return self.records

    def pending_show(self, ref):
        self.calls.append(("show", ref))
        return {"id": ref, "clash": {"summary": "Saved"}}

    def pending_drop(self, ref, *, reason=None):
        self.calls.append(("drop", ref, reason))
        return {"id": ref, "state": "dropped", "dropped_reason": reason}

    def retry(self, ref):
        self.calls.append(("retry", ref))
        return self.retry_result


def _parse(client, argv):
    return timeline_cli.build_parser(client).parse_args(argv)


def test_pending_json_and_all_actor_listing(monkeypatch, capsys):
    timeline = _Timeline()
    monkeypatch.setattr(cli_pending, "_open", lambda parsed: timeline)
    parsed = _parse(_Client(), ["pending", "list", "film", "--project", "p", "--all-actors", "--json"])

    assert parsed.handler(parsed) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["pending_changes"] == timeline.records
    assert timeline.calls == [("list", {"all": False, "actors": "all"})]


def test_pending_show_emits_saved_clash_packet_json(monkeypatch, capsys):
    timeline = _Timeline()
    monkeypatch.setattr(cli_pending, "_open", lambda parsed: timeline)
    parsed = _parse(_Client(), ["pending", "show", "film", "pc-one", "--project", "p", "--json"])

    assert parsed.handler(parsed) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["clash"]["summary"] == "Saved"


def test_pending_human_output_summarizes_records_and_recovery(monkeypatch, capsys):
    timeline = _Timeline()
    timeline.records = [{
        "id": "pc-one", "state": "pending", "origin": "function", "label": "Recut",
        "attempts": 2, "failure": {"details": {"reason": "clip hold changed"}},
    }]
    monkeypatch.setattr(cli_pending, "_open", lambda parsed: timeline)
    parsed = _parse(_Client(), ["pending", "list", "film", "--project", "p"])

    assert parsed.handler(parsed) == 0
    output = capsys.readouterr().out
    assert "pc-one  pending  function  2  Recut  clip hold changed" in output
    assert "astrid timelines pending show film <id> --project p" in output

    timeline.pending_show = lambda ref: {
        **timeline.records[0], "id": ref, "diff": {"changes": [{"path": "clip/a/hold"}]},
        "clash": {"summary": "Saved"},
    }
    parsed = _parse(_Client(), ["pending", "show", "film", "pc-one", "--project", "p"])
    assert parsed.handler(parsed) == 0
    output = capsys.readouterr().out
    assert "summary: Saved" in output
    assert '"path": "clip/a/hold"' in output
    assert "next: astrid timelines retry film pc-one --project p" in output


def test_retry_preview_has_stable_json_shape_and_token(monkeypatch, capsys):
    timeline = _Timeline()
    monkeypatch.setattr(cli_pending, "_open", lambda parsed: timeline)
    parsed = _parse(_Client(), ["retry", "film", "pc-one", "--project", "p", "--json"])

    assert parsed.handler(parsed) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data == {
        "status": "preview", "pending_id": "pc-one", "token": "fresh-token",
        "head": {"revision_id": "r2", "publication_id": "p2"},
        "diff": {"changes": []}, "conflicts": None, "receipts": None,
        "summary": "Ready for review", "next": "tl.commit(r)",
    }


def test_retry_commit_token_commits_fresh_preview(monkeypatch, capsys):
    timeline = _Timeline()
    monkeypatch.setattr(cli_pending, "_open", lambda parsed: timeline)
    from astrid.sdk.timeline import retry as retry_module

    monkeypatch.setattr(retry_module, "commit_preview", lambda _timeline, preview: {
        "status": "part_landed", "pending_id": preview["pending_id"],
        "summary": 'Part of "Recut" landed; pc-one still has saved work.',
        "data": {"publication_id": "pub-one"}, "receipt": {"receipt_id": "pub-one"},
        "record": {"id": preview["pending_id"], "state": "pending", "part": 1,
                   "label": "Recut"},
    })
    parsed = _parse(_Client(), ["retry", "film", "pc-one", "--project", "p", "--commit", "fresh-token", "--json"])

    assert parsed.handler(parsed) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["status"] == "part_landed"
    assert data["pending_id"] == "pc-one"
    assert data["summary"] == 'Part of "Recut" landed; pc-one still has saved work.'


def test_retry_rejects_stale_token_without_sending(monkeypatch, capsys):
    timeline = _Timeline()
    monkeypatch.setattr(cli_pending, "_open", lambda parsed: timeline)
    from astrid.sdk.timeline import retry as retry_module

    monkeypatch.setattr(retry_module, "commit_preview", lambda *_args: pytest.fail("stale preview sent"))
    parsed = _parse(_Client(), ["retry", "film", "pc-one", "--project", "p", "--commit", "old-token", "--json"])

    assert parsed.handler(parsed) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "timeline_edit_error"
    assert "stale" in result["error"]["message"]
    assert "astrid timelines retry film pc-one --project p again" in result["error"]["message"]


def test_retry_human_output_prints_valid_cli_preview_command(monkeypatch, capsys):
    timeline = _Timeline()
    monkeypatch.setattr(cli_pending, "_open", lambda parsed: timeline)
    parsed = _parse(_Client(), ["retry", "film", "pc-one", "--project", "p"])

    assert parsed.handler(parsed) == 0
    output = capsys.readouterr().out
    assert "next: astrid timelines retry film pc-one --project p --commit fresh-token" in output
    assert "tl.commit(r)" not in output


def test_retry_rederive_human_output_explains_python_route(monkeypatch, capsys):
    timeline = _Timeline()
    timeline.retry_result = {"status": "re_derive", "pending_id": "pc-one",
                             "summary": "The reviewed list still clashes.",
                             "next": "preview = tl.commit(make_edit, dry_run=True)"}
    monkeypatch.setattr(cli_pending, "_open", lambda parsed: timeline)
    parsed = _parse(_Client(), ["retry", "film", "pc-one", "--project", "p"])

    assert parsed.handler(parsed) == 0
    output = capsys.readouterr().out
    assert "re-derive needs Python; see SKILL.md ‘When to re-derive’" in output
    assert "make_edit" not in output


def test_landed_retry_token_reports_existing_publication(monkeypatch, capsys):
    timeline = _Timeline()
    timeline.retry_result = {"status": "landed", "pending_id": "pc-one",
                             "summary": '"Recut" already landed as txn-123; pc-one is closed. Nothing was sent.'}
    monkeypatch.setattr(cli_pending, "_open", lambda parsed: timeline)
    parsed = _parse(_Client(), ["retry", "film", "pc-one", "--project", "p", "--commit", "old-token"])

    assert parsed.handler(parsed) == 0
    assert "already landed as txn-123" in capsys.readouterr().out


def test_cli_conflict_error_keeps_packet_fields():
    error = SimpleNamespace(
        details={"status": "conflict"}, failure={"details": {"status": "conflict"}},
        clash={"summary": "Two edits overlap"}, pending_id="pc-one", code="conflict",
    )
    result = cli_pending._error(error)
    assert result.error.details["clash"] == {"summary": "Two edits overlap"}
    assert result.error.details["pending_id"] == "pc-one"


def test_drop_is_a_tombstone_and_discard_path_keeps_saved_records(monkeypatch, capsys):
    timeline = _Timeline()
    monkeypatch.setattr(cli_pending, "_open", lambda parsed: timeline)
    parsed = _parse(_Client(), ["pending", "drop", "film", "pc-one", "--project", "p", "--reason", "superseded", "--json"])
    assert parsed.handler(parsed) == 0
    assert json.loads(capsys.readouterr().out)["data"]["state"] == "dropped"
    assert timeline.calls[-1] == ("drop", "pc-one", "superseded")

    # Discard deliberately retains pending_changes; the helper reports the kept count.
    state = {"scope": {"realm_id": "r", "project_id": "p", "timeline_id": "film"},
             "pending": [], "pending_changes": {"pc-one": {"id": "pc-one"}}}

    class Outbox:
        def locked(self):
            from contextlib import nullcontext
            return nullcontext()

        def read(self):
            return state

        def write(self, updated):
            state.update(updated)

    from astrid.packs.timeline.cli_working import _discard_recoverable_outbox

    assert _discard_recoverable_outbox(Outbox(), state["scope"], return_kept_count=True) == ("cleared", 1)
    assert state["pending_changes"]["pc-one"]["id"] == "pc-one"


def test_discard_reports_pending_records_kept(monkeypatch, capsys):
    from astrid.packs.timeline import cli_working
    from astrid.sdk.timeline import store

    monkeypatch.setattr(store, "resolve_ids", lambda *_args, **_kwargs: ("p", "film", "head"))
    monkeypatch.setattr(cli_working, "_connected_outbox", lambda *_args: (object(), {"status": "clear"}, {"project_id": "p"}))
    monkeypatch.setattr(cli_working, "_discard_recoverable_outbox", lambda *_args, **_kwargs: ("clear", 3))
    monkeypatch.setattr(cli_working, "working_copy", lambda *_args, **_kwargs: (None, False))
    monkeypatch.setattr(cli_working, "draft_name", lambda _parsed: "main")
    parsed = SimpleNamespace(project="p", ref="film", client=_Client())

    assert cli_working._cmd_discard(parsed) == 0
    assert "kept 3 pending change(s)" in capsys.readouterr().out


def test_editing_docs_explain_retry_and_rederive_contract():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    skill = (root / "astrid/packs/rendering/docs/SKILL.md").read_text()
    reference = (root / "astrid/packs/rendering/docs/references/editing.md").read_text()
    guide = (root / "docs/timeline-editing-guide.md").read_text()
    for document in (skill, reference, guide):
        assert "err.pending_id" in document
        assert "re_derive" in document
        assert "idempotency_key=" in document
        assert "timelines rebase" not in document.lower()
    assert "Commit only a `preview`" in guide
    assert "retry_error" in skill and "retry_error" in guide
