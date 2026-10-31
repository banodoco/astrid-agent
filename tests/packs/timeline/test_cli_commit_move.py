"""The real product parser/dispatch emits the SDK's captured move and inverse."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from astrid.packs.timeline import cli
from astrid.sdk import storage_root
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.outbox import request_bytes
from astrid_runtime_client import ApiError
from tests.sdk.test_timeline_sync import connected as sync_connection  # noqa: F401
from tests.sdk.test_timeline_sync import initial_bundle


def _install_history(generated):
    """Expose accepted fake responses through the authenticated public history shape."""
    bootstrap = copy.deepcopy(generated.head)
    generated.bundles[bootstrap["revision_id"]]["base_parent"]["publication_id"] = bootstrap["publication_id"]

    def list_timeline_commits(project, timeline, *, cursor=None, limit):
        assert (project, timeline) == ("p", "t")
        items = [{
            "publication_id": bootstrap["publication_id"], "revision_id": bootstrap["revision_id"],
            "previous": None, "receipt": {"receipt_id": bootstrap["publication_id"]},
            "intent": "bootstrap", "records": None,
            "provider_version": None, "compiler_version": None, "publisher": None,
        }]
        for _raw, response in generated.accepted.values():
            data, receipt = response["data"], response["receipt"]
            body = next(json.loads(raw) for raw, key in generated.calls
                        if key == receipt["idempotency_key"])
            label = data.get("label", body.get("label", "Timeline edit"))
            steps = copy.deepcopy(data.get("steps", body.get("steps", [])))
            merge_mode = data.get("merge_mode", body.get("merge", "fields"))
            records = generated.records[receipt["receipt_id"]]
            generated.bundles[data["accepted_revision_id"]]["base_parent"].update(
                revision_id=data["accepted_revision_id"], publication_id=receipt["receipt_id"])
            if not isinstance(records, dict):
                records = {"commands": copy.deepcopy(records)}
            else:
                records = copy.deepcopy(records)
            records.setdefault("commands", copy.deepcopy(generated.records[receipt["receipt_id"]]))
            records.update(label=copy.deepcopy(label), steps=copy.deepcopy(steps), merge_mode=merge_mode)
            command = body.get("commands", [{}])[0]
            if command.get("op") == "timeline.inverse":
                records.setdefault("undo_of", copy.deepcopy(command.get("args", {}).get("publications", [])))
                if "steps" in command.get("args", {}):
                    records.setdefault("undo_steps", copy.deepcopy(command["args"]["steps"]))
            history_data = copy.deepcopy(data)
            history_data.update(label=copy.deepcopy(label), steps=copy.deepcopy(steps), merge_mode=merge_mode)
            history_receipt = copy.deepcopy(receipt)
            history_receipt["result"] = copy.deepcopy(history_data)
            items.append({
                "publication_id": receipt["receipt_id"], "revision_id": history_data["accepted_revision_id"],
                "previous": {"revision_id": data["previous_revision_id"],
                             "publication_id": data["previous_publication_id"]},
                "receipt": history_receipt, "intent": "recorded", "records": records,
                "provider_version": history_data["provider_version"],
                "compiler_version": history_data["compiler_version"],
                "publisher": history_data["publisher"], "source": history_data.get("source"),
                "label": label, "steps": copy.deepcopy(steps), "merge_mode": merge_mode,
            })
        return {"head": copy.deepcopy(generated.head), "items": items, "next_cursor": None}

    generated.list_timeline_commits = list_timeline_commits


@pytest.fixture
def command(sync_connection, monkeypatch):  # noqa: F811 - imported pytest fixture
    client, generated, path = sync_connection
    _install_history(generated)
    monkeypatch.setattr(storage_root, "resolve_runtime_data_root", lambda: path.parent)
    scope = {"realm_id": "realm", "actor_id": "actor", "project_id": "p", "timeline_id": "t"}
    generated.path = path.parent / "timeline-outbox" / (hashlib.sha256(request_bytes(scope)).hexdigest() + ".json")
    monkeypatch.setattr(cli, "_edit_checkout", lambda *a, **kw: pytest.fail("legacy draft editor called"))
    parser = cli.build_parser(client)

    def run(*args):
        parsed = parser.parse_args(args)
        return parsed.handler(parsed)

    return run, generated


def test_move_dispatch_matches_python_capture_and_reports_immutable_revision(command, capsys):
    run, generated = command
    assert run("edit", "timeline-slug", "--project", "project-slug", "--clip", "parent/clip:a", "--on", "5", "--json") == 0
    body = json.loads(generated.calls[0][0])
    assert body["commands"] == [capture_move(initial_bundle(), "parent/clip:a", 5)]
    assert body["source"] == {"via": "cli"}
    output = json.loads(capsys.readouterr().out)
    assert output["data"]["accepted_revision_id"] == "r1"
    assert output["data"]["accepted_publication_id"] == "pub1"
    assert output["data"]["show_command"] == "astrid timelines show t --project p --published --revision-id r1"
    assert output["receipt"]["receipt_id"] == "pub1"


def test_native_import_disclosure_preserves_canonical_receipt(command, capsys):
    from astrid.packs.timeline.cli_native import _commit_envelope

    run, generated = command
    assert run('edit', 't', '--project', 'p', '--clip', 'parent/clip:a',
               '--on', '5', '--idempotency-key', 'import-disclosure', '--json') == 0
    capsys.readouterr()
    raw = copy.deepcopy(generated.accepted['import-disclosure'][1])
    raw['imports'] = [{'object_id': 'sha256:' + 'a' * 64, 'catalog_write': True,
                       'retained_on_undo': True}]
    before = copy.deepcopy(raw)
    result = _commit_envelope(raw, 'import-disclosure').as_dict()
    assert result['data']['imports'] == raw['imports']
    assert result['receipt'] == raw['receipt']
    assert result['idempotency_key'] == 'import-disclosure'
    assert raw == before


def test_ordinary_edit_literal_move_uses_commit_outbox(command, capsys):
    run, generated = command
    assert run("edit", "t", "--clip", "a", "--on", "5", "--json") == 0
    body = json.loads(generated.calls[0][0])
    assert body["commands"] == [capture_move(initial_bundle(), "parent/clip:a", 5.0)]
    assert body["source"] == {"via": "cli"}
    output = json.loads(capsys.readouterr().out)
    assert output["data"]["accepted_publication_id"] == "pub1"


def test_ordinary_edit_literal_move_retries_the_persisted_flight(command, capsys):
    run, generated = command
    args = ("edit", "t", "--clip", "a", "--on", "5", "--json", "--idempotency-key", "ordinary-move")
    generated.lose_ack = True
    assert run(*args) == 1
    capsys.readouterr()
    assert run(*args) == 0
    capsys.readouterr()
    assert generated.calls[0] == generated.calls[1]


def test_status_and_discard_preserve_lost_ack_flight_truthfully(command, capsys):
    run, generated = command
    generated.lose_ack = True
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--json") == 1
    capsys.readouterr()
    before = json.loads(generated.path.read_bytes())
    assert before["flight"] is not None
    original_body = before["flight"]["body"]
    original_key = before["flight"]["key"]

    assert run("status", "t", "--project", "p", "--json") == 0
    status = json.loads(capsys.readouterr().out)["data"]["connected_session"]
    assert status == {
        "status": "in_flight", "outcome": "unknown", "key": before["flight"]["key"],
        "command_count": before["flight"]["count"], "pending_changes": 0,
    }

    assert run("discard", "t", "--project", "p") == 0
    output = capsys.readouterr().out
    assert "outcome is unknown" in output
    assert "did not undo it" in output
    after = json.loads(generated.path.read_bytes())
    assert after["flight"] == before["flight"]
    assert after["flight"]["body"] == original_body and after["flight"]["key"] == original_key
    assert after["pending"] == before["pending"]
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--json") == 0
    capsys.readouterr()
    assert generated.calls[0] == generated.calls[1]


def test_discard_unknown_flight_drops_only_unsent_suffix_and_keeps_replay_metadata(command, capsys):
    run, generated = command
    assert run("edit", "t", "--project", "p", "--clip", "b", "--on", "3", "--json",
               "--idempotency-key", "older-key") == 0
    capsys.readouterr()
    generated.lose_ack = True
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--json") == 1
    capsys.readouterr()
    before = json.loads(generated.path.read_bytes())
    prefix = before["flight"]["count"]
    before["pending"].append({"op": "clip.move", "select": "parent/clip:later", "args": {"on": 9.0}})
    generated.path.write_text(json.dumps(before), encoding="utf-8")

    assert run("discard", "t", "--project", "p") == 0
    capsys.readouterr()
    after = json.loads(generated.path.read_bytes())
    assert after["flight"] == before["flight"]
    assert after["pending"] == before["pending"][:prefix]
    assert after["acknowledged"] == before["acknowledged"]
    expected = copy.deepcopy(before)
    expected["pending"] = before["pending"][:prefix]
    assert after == expected
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--json") == 0
    capsys.readouterr()
    assert generated.calls[1] == generated.calls[2]
    assert run("edit", "t", "--project", "p", "--clip", "b", "--on", "3", "--json",
               "--idempotency-key", "older-key") == 0
    assert json.loads(capsys.readouterr().out)["receipt"]["receipt_id"] == "pub1"
    assert len(generated.accepted) == 2


def test_status_and_discard_report_and_clear_rejected_commands(command, capsys):
    run, generated = command
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--dry-run", "--json") == 0
    capsys.readouterr()
    state = json.loads(generated.path.read_bytes())
    rejected_command = capture_move(initial_bundle(), "parent/clip:a", 5.0)
    state["rejected"] = {
        "base": state["base"], "commands": [rejected_command], "flight": {"key": "refused"},
        "failure": {"code": "validation_error", "message": "refused", "details": {"status": "refused"}},
        "commit_options": None, "evaluation": None,
    }
    state["pending"] = []
    generated.path.write_text(json.dumps(state), encoding="utf-8")

    assert run("status", "t", "--project", "p", "--json") == 0
    status = json.loads(capsys.readouterr().out)["data"]["connected_session"]
    assert status == {"status": "rejected", "command_count": 1, "pending_changes": 0}

    assert run("discard", "t", "--project", "p") == 0
    output = capsys.readouterr().out
    assert "discarded recoverable connected-session commands" in output
    saved = json.loads(generated.path.read_bytes())
    assert saved.get("rejected") is None
    assert saved["pending"] == [] and saved["flight"] is None


def test_known_conflict_is_reported_not_applied_and_can_be_discarded(command, capsys):
    run, generated = command
    generated.refusal = ApiError(409, "conflict", "guards changed", details={
        "status": "conflict", "reason": "selection_changed", "conflicts": [{"ref": "parent/clip:a"}],
    })
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--json") == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["details"]["outcome"] == "not_applied"
    assert error["details"]["headline"]
    assert "Your edit is saved as" in error["details"]["fix"]
    assert error["details"]["cause"]["details"]["reason"] == "selection_changed"
    state = json.loads(generated.path.read_bytes())
    assert state["flight"] is None and state["pending"] == []
    assert not state.get("rejected")
    assert len(state["pending_changes"]) == 1
    pending_id, record = next(iter(state["pending_changes"].items()))
    assert error["details"]["pending_id"] == pending_id
    assert record["state"] == "pending"
    assert record["parts"][0]["commands"]

    assert run("status", "t", "--project", "p", "--json") == 0
    status = json.loads(capsys.readouterr().out)["data"]["connected_session"]
    assert status["status"] == "clear"
    assert status["pending_changes"] == 1

    # Exercise the compatibility path: a conflict written by an older SDK is
    # migrated before discard clears the working queue.
    base = record["parts"][0]["base"]
    commands = record["parts"][0]["commands"]
    flight_body = json.loads(generated.calls[0][0])
    flight_body.update(
        base_revision_id=base["revision_id"],
        base_publication_id=base["publication_id"],
        commands=commands,
    )
    legacy = {
        "base": base,
        "commands": commands,
        "flight": {"body": request_bytes(flight_body).decode(),
                   "key": "legacy-conflict", "count": 1, "fence": "legacy-fence"},
        "failure": record["failure"], "commit_options": None, "evaluation": None,
    }
    state.pop("pending_changes")
    state["rejected"] = legacy
    generated.path.write_text(json.dumps(state), encoding="utf-8")
    assert run("discard", "t", "--project", "p") == 0
    assert "kept 1 pending change" in capsys.readouterr().out
    saved = json.loads(generated.path.read_bytes())
    assert not saved.get("rejected")
    assert len(saved["pending_changes"]) == 1
    migrated = next(iter(saved["pending_changes"].values()))
    assert migrated["parts"][0]["commands"] == record["parts"][0]["commands"]
    generated.refusal = None
    assert run("edit", "t", "--project", "p", "--clip", "b", "--on", "7", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["accepted_publication_id"] == "pub1"


def test_status_reports_malformed_connected_outbox_as_invalid_and_discard_leaves_it(command, capsys):
    run, generated = command
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--dry-run", "--json") == 0
    capsys.readouterr()
    generated.path.write_text("{bad json", encoding="utf-8")

    assert run("status", "t", "--project", "p", "--json") == 0
    status = json.loads(capsys.readouterr().out)["data"]["connected_session"]
    assert status == {"status": "invalid"}

    assert run("discard", "t", "--project", "p") == 0
    assert "could not be verified and was left untouched" in capsys.readouterr().out
    assert generated.path.read_text(encoding="utf-8") == "{bad json"


@pytest.mark.parametrize("corruption", [
    {"kind": "unrecognized"}, {"kind": "commit"}, {"body": "["}, {"body": "[]"},
    {"count": 0}, {"count": True},
])
def test_status_and_discard_leave_unrecognized_flight_untouched(command, capsys, corruption):
    run, generated = command
    generated.lose_ack = True
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--json") == 1
    capsys.readouterr()
    state = json.loads(generated.path.read_bytes())
    state["flight"].update(corruption)
    generated.path.write_text(json.dumps(state), encoding="utf-8")

    assert run("status", "t", "--project", "p", "--json") == 0
    status = json.loads(capsys.readouterr().out)["data"]["connected_session"]
    assert status == {"status": "invalid"}
    before = generated.path.read_bytes()

    assert run("discard", "t", "--project", "p") == 0
    assert "could not be verified and was left untouched" in capsys.readouterr().out
    assert generated.path.read_bytes() == before


@pytest.mark.parametrize("status", ["refused", "invalid"])
def test_cli_final_refusal_can_be_reviewed_discarded_and_replanned(command, capsys, status):
    run, generated = command
    generated.refusal = ApiError(409, "refused", "edit refused", details={"status": status})
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--json") == 1
    capsys.readouterr()
    saved = json.loads(generated.path.read_bytes())
    assert saved["pending"] == [] and saved["flight"] is None
    assert not saved.get("rejected")
    assert len(saved["pending_changes"]) == 1
    pending_id, record = next(iter(saved["pending_changes"].items()))
    assert record["state"] == "pending"
    assert record["parts"][0]["commands"]
    assert run("status", "t", "--project", "p", "--json") == 0
    assert json.loads(capsys.readouterr().out)["data"]["connected_session"] == {
        "status": "clear", "pending_changes": 1,
    }
    assert run("status", "t", "--project", "p", "--text") == 0
    assert "1 pending change(s) saved" in capsys.readouterr().out
    assert run("pending", "list", "t", "--project", "p", "--json") == 0
    listed = json.loads(capsys.readouterr().out)["data"]["pending_changes"]
    assert [row["id"] for row in listed] == [pending_id]
    assert run("pending", "show", "t", pending_id, "--project", "p", "--json") == 0
    shown = json.loads(capsys.readouterr().out)["data"]
    assert shown["failure"]["details"]["status"] == status
    assert run("pending", "drop", "t", pending_id, "--project", "p", "--json") == 0
    assert json.loads(capsys.readouterr().out)["data"]["state"] == "dropped"
    generated.refusal = None
    assert run("edit", "t", "--project", "p", "--clip", "b", "--on", "7", "--json") == 0
    capsys.readouterr()
    assert len(generated.accepted) == 1
    assert json.loads(generated.calls[-1][0])["commands"][0]["select"] == "parent/clip:b"


def test_discard_rechecks_scope_after_status_read(command, monkeypatch, capsys):
    from astrid.packs.timeline import cli_working

    run, generated = command
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--dry-run", "--json") == 0
    capsys.readouterr()
    inspect = cli_working._connected_outbox
    changed = []

    def change_scope(*args):
        outbox, status, scope = inspect(*args)
        with outbox.locked():
            state = outbox.read()
            state["scope"]["actor_id"] = "another-actor"
            outbox.write(state)
        changed.append(outbox.path.read_bytes())
        return outbox, status, scope

    monkeypatch.setattr(cli_working, "_connected_outbox", change_scope)
    assert run("discard", "t", "--project", "p") == 0
    assert "could not be verified and was left untouched" in capsys.readouterr().out
    assert generated.path.read_bytes() == changed[0]


def test_discard_write_failure_does_not_claim_success(command, monkeypatch, capsys):
    from astrid.sdk.timeline.outbox import Outbox

    run, generated = command
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--dry-run", "--json") == 0
    capsys.readouterr()
    before = generated.path.read_bytes()

    def fail_write(self, state):
        raise OSError("outbox storage unavailable")

    monkeypatch.setattr(Outbox, "write", fail_write)
    assert run("discard", "t", "--project", "p") == 1
    output = capsys.readouterr()
    assert "discard could not be confirmed" in output.err
    assert "discarded" not in output.out
    assert generated.path.read_bytes() == before


def test_discard_preserves_unknown_assembly_admission_for_exact_retry(tmp_path):
    from astrid.packs.timeline.cli_working import _discard_recoverable_outbox
    from astrid.sdk.workspace_client import WorkspaceClientError
    from tests.sdk.test_timeline_assembly import _admit, _requester

    session, generated, selectors, _associations, calls = _requester(tmp_path)
    dispatch = session.client.tasks.dispatch_admission

    def lose_ack(arguments):
        dispatch(arguments)
        raise WorkspaceClientError(0, "transport_error", "lost admission acknowledgement")

    session.client.tasks.dispatch_admission = lose_ack
    with pytest.raises(WorkspaceClientError):
        _admit(session, selectors)
    before = generated.path.read_bytes()
    scope = json.loads(before)["scope"]
    assert _discard_recoverable_outbox(session.outbox, scope) == "in_flight"
    assert generated.path.read_bytes() == before
    session.client.tasks.dispatch_admission = dispatch
    assert _admit(session, selectors)["data"]["task_id"] == "assembler"
    assert calls[0] == calls[1]


def test_discard_clears_unsent_connected_commands(command, capsys):
    run, generated = command
    # Establish the connected outbox through the real session path, then
    # model a command that was queued before transport was attempted.
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5",
               "--dry-run", "--json") == 0
    capsys.readouterr()
    state = json.loads(generated.path.read_bytes())
    state["pending"] = [{"op": "clip.move", "select": "parent/clip:a", "args": {"on": 5.0}}]
    generated.path.write_text(json.dumps(state), encoding="utf-8")
    generated.calls.clear()

    assert run("status", "t", "--project", "p", "--json") == 0
    status = json.loads(capsys.readouterr().out)["data"]["connected_session"]
    assert status == {"status": "pending", "command_count": 1, "pending_changes": 0}

    assert run("discard", "t", "--project", "p") == 0
    assert "discarded recoverable connected-session commands" in capsys.readouterr().out
    saved = json.loads(generated.path.read_bytes())
    assert saved["pending"] == [] and saved["flight"] is None
    assert generated.calls == []


def test_status_counts_and_discard_migrates_legacy_conflicts(command, capsys):
    run, generated = command
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--dry-run", "--json") == 0
    capsys.readouterr()
    state = json.loads(generated.path.read_bytes())
    rejected_command = capture_move(initial_bundle(), "parent/clip:a", 5.0)
    state["rejected"] = {
        "base": state["base"], "commands": [rejected_command],
        "flight": {"key": "conflict", "logical_key": "conflict", "count": 1,
                   "body": json.dumps({
                       "base_revision_id": state["base"]["revision_id"],
                       "base_publication_id": state["base"]["publication_id"],
                       "commands": [rejected_command],
                       "label": "Recover this edit",
                   }, separators=(",", ":"))},
        "failure": {"code": "conflict", "message": "changed", "details": {
            "status": "conflict", "current": {"revision_id": "r1", "publication_id": "pub1"},
        }},
        "commit_options": {"label": "Recover this edit"}, "evaluation": None,
    }
    state["pending_changes"] = {
        "pc_existing": {"id": "pc_existing", "state": "pending"},
        "pc_landed": {"id": "pc_landed", "state": "landed"},
    }
    generated.path.write_text(json.dumps(state), encoding="utf-8")

    assert run("status", "t", "--project", "p", "--json") == 0
    status = json.loads(capsys.readouterr().out)["data"]["connected_session"]
    assert status["pending_changes"] == 1

    assert run("discard", "t", "--project", "p") == 0
    output = capsys.readouterr().out
    saved = json.loads(generated.path.read_bytes())
    assert saved.get("rejected") is None
    assert saved["pending"] == [] and saved["flight"] is None
    assert len(saved["pending_changes"]) == 3
    migrated = saved["pending_changes"]
    record_id = next(key for key in migrated if key not in {"pc_existing", "pc_landed"})
    record = migrated[record_id]
    assert record["id"] == record_id and record["state"] == "pending"
    assert record["parts"][0]["base"] == {
        "revision_id": state["base"]["revision_id"],
        "publication_id": state["base"]["publication_id"],
    }
    assert record["parts"][0]["commands"] == [rejected_command]
    assert "kept 3 pending change(s)" in output


def test_ordinary_edit_symbolic_move_keeps_legacy_moment_resolution(command, monkeypatch):
    run, generated = command

    def legacy(*args, **kwargs):
        raise RuntimeError("legacy moment resolution")

    monkeypatch.setattr(cli, "_edit_checkout", legacy)
    with pytest.raises(RuntimeError, match="legacy moment resolution"):
        run("edit", "t", "--clip", "a", "--on", 'after "Astrid"')
    assert generated.calls == []


def test_ordinary_edit_occurrence_move_keeps_legacy_owner_resolution(command, monkeypatch):
    run, generated = command
    from tests.editing_conformance.test_recipe_m2_clip_trim import bundle as trim_bundle

    generated.bundles['r0'] = trim_bundle()
    generated.bundles['r0']['base_parent'].update(revision_id='r0', publication_id='pub0')

    def legacy(*args, **kwargs):
        raise RuntimeError("legacy occurrence resolution")

    monkeypatch.setattr(cli, "_edit_checkout", legacy)
    with pytest.raises(RuntimeError, match="legacy occurrence resolution"):
        run("edit", "t", "--clip", "occurrence:occ-b/clip:clip-a", "--on", "5")
    assert generated.calls == []


def test_ordinary_edit_move_with_draft_keeps_legacy_draft(command, monkeypatch):
    from astrid.sdk.timeline import store
    from astrid.sdk.timeline.sync import TimelineSession

    run, generated = command
    monkeypatch.setattr(store, "find_draft", lambda *args, **kwargs: generated.path)
    monkeypatch.setattr(TimelineSession, "open",
                        lambda *args, **kwargs: pytest.fail("draft opened through native session"))

    def legacy(*args, **kwargs):
        raise RuntimeError("legacy draft resolution")

    monkeypatch.setattr(cli, "_edit_checkout", legacy)
    with pytest.raises(RuntimeError, match="legacy draft resolution"):
        run("edit", "t", "--clip", "a", "--on", "5")
    assert generated.calls == []


@pytest.mark.parametrize("extra", [("--trim-out", "4"), ("--set", "x=2"), ("--verify",)])
def test_ordinary_edit_move_with_unsupported_flags_keeps_legacy(command, monkeypatch, extra):
    run, generated = command

    def legacy(*args, **kwargs):
        raise RuntimeError("legacy mixed move")

    monkeypatch.setattr(cli, "_edit_checkout", legacy)
    with pytest.raises(RuntimeError, match="legacy mixed move"):
        run("edit", "t", "--clip", "a", "--on", "5", *extra)
    assert generated.calls == []


def test_move_preview_uses_current_project_and_does_not_claim_publication(command, capsys):
    run, generated = command
    assert run("edit", "t", "--clip", "parent/clip:a", "--on", "5", "--dry-run") == 0
    assert "nothing committed or reserved" in capsys.readouterr().out
    assert generated.accepted == {}
    assert generated.head["revision_id"] == "r0"


def test_cli_lost_ack_retry_and_accepted_undo(command, capsys):
    run, generated = command
    args = ("edit", "t", "--project", "p", "--clip", "parent/clip:a", "--on", "5", "--json")
    generated.lose_ack = True
    assert run(*args) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]
    assert run(*args) == 0
    capsys.readouterr()
    assert generated.calls[0] == generated.calls[1]
    assert run("undo", "t", "--project", "p", "--commit", "pub1", "--json") == 0
    output = json.loads(capsys.readouterr().out)
    assert output["data"]["accepted_publication_id"] == "pub2"
    inverse = json.loads(generated.calls[-1][0])["commands"][0]
    assert inverse["op"] == "timeline.inverse" and inverse["args"]["publications"] == ["pub1"]
    assert generated.bundles["r2"]["parent"] == initial_bundle()["parent"]


def test_edit_parser_refuses_retired_native_flag(command):
    run, generated = command
    with pytest.raises(SystemExit) as exc:
        run("edit", "t", "--project", "p", "--clip", "parent/clip:a", "--on", "5", "--native")
    assert exc.value.code == 2
    assert generated.calls == []


def test_preview_json_preserves_actual_bridge_request_key(command, capsys):
    run, generated = command
    assert run("edit", "t", "--clip", "parent/clip:a", "--on", "5", "--dry-run", "--json") == 0
    output = json.loads(capsys.readouterr().out)
    assert output["idempotency_key"] == generated.calls[0][1]
    assert output["idempotency_key"] and output["receipt"] is None


def test_cli_accepted_redo_inverts_undo_publication_and_preserves_disjoint_edit(command, capsys):
    run, generated = command
    for clip, on in [('a', '5'), ('b', '7')]:
        assert run('edit', 't', '--clip', 'parent/clip:' + clip, '--on', on, '--json') == 0
        capsys.readouterr()
    assert run('undo', 't', '--commit', 'pub1', '--json') == 0
    capsys.readouterr()
    assert run('redo', 't', '--commit', 'pub3', '--json') == 0
    output = json.loads(capsys.readouterr().out)
    assert output['data']['accepted_publication_id'] == 'pub4'
    assert generated.bundles['r4']['parent'] == generated.bundles['r2']['parent']
    inverse = json.loads(generated.calls[-1][0])['commands'][0]
    assert inverse['op'] == 'timeline.inverse' and inverse['args']['publications'] == ['pub3']
