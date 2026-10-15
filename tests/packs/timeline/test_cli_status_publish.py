from __future__ import annotations

import json

from astrid.packs.timeline import cli as timeline_cli
from astrid.sdk.timeline import store as timeline_store
from astrid.sdk.timeline_checkout import Checkout


def _json_result(capsys):
    return json.loads(capsys.readouterr().out)


def test_status_absent_draft_reports_head_without_creating(tmp_path, monkeypatch, capsys) -> None:
    client = object()
    parser = timeline_cli.build_parser(client)
    path = tmp_path / "absent.json"
    monkeypatch.setattr(timeline_store, "resolve_ids", lambda *_args, **_kwargs: ("p1", "t1", "head-2"))
    monkeypatch.setattr(timeline_store, "current_draft", lambda *_args: "main")
    monkeypatch.setattr(timeline_store, "draft_path", lambda *_args: path)
    monkeypatch.setattr(Checkout, "load", classmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not load an absent draft"))))

    parsed = parser.parse_args(["status", "main", "--project", "p1"])

    assert parsed.handler(parsed) == 0
    payload = _json_result(capsys)
    assert payload["ok"] is True
    data = dict(payload["data"])
    sync = {key: data.pop(key) for key in ("base_state", "observed_head", "observed_publication_id", "accepted_revision", "accepted_state", "unimported_external_revisions", "external_state", "conflicts", "conflict_state", "choices", "sync_error")}
    assert sync["observed_head"] == data["current_head"]
    assert sync["accepted_revision"] is None
    if data["working_copy"] == "absent":
        assert data.pop("base_revision") is None
        assert sync["accepted_state"] == sync["external_state"] == sync["conflict_state"] == "absent"
    else:
        assert sync["accepted_state"] == sync["external_state"] == sync["conflict_state"] == "unknown"
    assert data == {
        "timeline_id": "t1",
        "current_head": "head-2",
        "draft": "main",
        "working_copy": "absent",
    }
    assert not path.exists()


def test_status_existing_draft_reports_base_head_changes_and_check(tmp_path, monkeypatch, capsys) -> None:
    client = object()
    parser = timeline_cli.build_parser(client)
    path = tmp_path / "draft.json"
    path.write_text("present", encoding="utf-8")
    monkeypatch.setattr(timeline_store, "resolve_ids", lambda *_args, **_kwargs: ("p1", "t1", "head-3"))
    monkeypatch.setattr(timeline_store, "current_draft", lambda *_args: "working")
    monkeypatch.setattr(timeline_store, "draft_path", lambda *_args: path)

    class Report:
        valid = False
        problems = ("missing media",)
        changed_cuts = ("c2",)

    class FakeCheckout:
        base_revision = "head-1"

        def changes(self):
            return ["cut c2 changed"]

        def edits(self):
            return {
                "changes": [{"kind": "changed", "clip_id": "c2", "fields": ["app"]}],
                "placement_changes": [],
                "cut_points": {"moved": [], "added": [], "removed": []},
            }

        def check(self):
            return Report()

    monkeypatch.setattr(Checkout, "load", classmethod(lambda *_args, **_kwargs: FakeCheckout()))
    monkeypatch.setattr(
        timeline_cli,
        "_checkout_change_units",
        lambda _checkout: (True, [["clip", "s1", "c2"]]),
    )
    parsed = parser.parse_args(["status", "main", "--project", "p1"])

    assert parsed.handler(parsed) == 0
    payload = _json_result(capsys)
    assert payload["ok"] is True
    data = dict(payload["data"])
    sync = {key: data.pop(key) for key in ("base_state", "observed_head", "observed_publication_id", "accepted_revision", "accepted_state", "unimported_external_revisions", "external_state", "conflicts", "conflict_state", "choices", "sync_error")}
    assert sync["observed_head"] == data["current_head"]
    assert sync["accepted_revision"] is None
    if data["working_copy"] == "absent":
        assert data.pop("base_revision") is None
        assert sync["accepted_state"] == sync["external_state"] == sync["conflict_state"] == "absent"
    else:
        assert sync["accepted_state"] == sync["external_state"] == sync["conflict_state"] == "unknown"
    assert data == {
        "timeline_id": "t1",
        "current_head": "head-3",
        "draft": "working",
        "working_copy": "present",
        "base_revision": "head-1",
        "head_moved": True,
        "changed": True,
        "change_state": "changed",
        "changed_units": [["clip", "s1", "c2"]],
        "changes": ["cut c2 changed"],
        "change_details": [{"kind": "changed", "clip_id": "c2", "fields": ["app"]}],
        "placement_changes": [],
        "cut_points": {"moved": [], "added": [], "removed": []},
        "check": {"valid": False, "problems": ["missing media"], "changed_cuts": ["c2"]},
    }


def test_checkout_fresh_keeps_dirty_draft_and_allows_named_fresh(tmp_path, monkeypatch, capsys) -> None:
    parser = timeline_cli.build_parser(object())
    dirty = tmp_path / "main.json"
    dirty.write_text("keep me", encoding="utf-8")
    monkeypatch.setattr(timeline_store, "resolve_ids", lambda *_args, **_kwargs: ("p1", "t1", "head-2"))
    monkeypatch.setattr(timeline_store, "current_draft", lambda *_args: "main")
    monkeypatch.setattr(timeline_store, "draft_path", lambda _p, _t, name: dirty if name == "main" else tmp_path / f"{name}.json")

    class DirtyCheckout:
        base_revision = "head-1"

        @staticmethod
        def load(_path):
            return DirtyCheckout()

        def changes(self):
            # Structured changes catch narration/app edits even if the
            # human-facing clip summary has no line to display.
            return []

        def edits(self):
            return {
                "changes": [{"kind": "changed", "clip_id": "vo-1", "fields": ["app"]}],
                "placement_changes": [],
                "cut_points": {"moved": [], "added": [], "removed": []},
            }

    calls = []
    monkeypatch.setattr(Checkout, "load", classmethod(lambda _cls, _path: DirtyCheckout()))
    monkeypatch.setattr(
        timeline_cli,
        "_checkout_change_units",
        lambda _checkout: (True, [["clip", "s1", "vo-1"]]),
    )
    monkeypatch.setattr(Checkout, "draft", classmethod(lambda _cls, *args, **kwargs: calls.append((args, kwargs)) or object()))

    refused = parser.parse_args(["checkout", "main", "--project", "p1", "--fresh"])
    assert refused.handler(refused) == 1
    payload = _json_result(capsys)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "conflict"
    assert dirty.read_text(encoding="utf-8") == "keep me"
    assert calls == []

    fresh = parser.parse_args(["checkout", "main", "--project", "p1", "--draft", "recovered", "--fresh"])
    assert fresh.handler(fresh) == 0
    assert calls == [(("p1", "main"), {"name": "recovered", "client": fresh.client, "fresh": True})]
    assert dirty.read_text(encoding="utf-8") == "keep me"


def test_checkout_fresh_refuses_parent_config_edit_when_clip_diff_is_empty(tmp_path, monkeypatch, capsys) -> None:
    from tests.packs.timeline.test_edit_verbs import bundle as editing_bundle

    parser = timeline_cli.build_parser(object())
    draft = tmp_path / "main.json"
    source = editing_bundle()
    Checkout._pin_initial_base(source)
    working = Checkout(source)
    working.bundle["parent"]["config"]["chapters"] = [{"id": "intro", "title": "Intro"}]
    draft.write_text(json.dumps(working.document(), sort_keys=True), encoding="utf-8")
    original_bytes = draft.read_bytes()
    previous = Checkout.load(draft)

    # The ordinary clip/cut diff omits this edited parent config unit.
    assert previous.edits()["changes"] == []
    assert previous.edits()["placement_changes"] == []
    base_known, changed_units = timeline_cli._checkout_change_units(previous)
    assert base_known is True
    assert any(path[-1] == "chapters" for path in changed_units)

    monkeypatch.setattr(timeline_store, "resolve_ids", lambda *_args, **_kwargs: ("p1", "t1", "head-2"))
    monkeypatch.setattr(timeline_store, "current_draft", lambda *_args: "main")
    monkeypatch.setattr(timeline_store, "draft_path", lambda *_args: draft)
    calls = []
    monkeypatch.setattr(Checkout, "draft", classmethod(lambda _cls, *args, **kwargs: calls.append((args, kwargs)) or object()))

    parsed = parser.parse_args(["checkout", "main", "--project", "p1", "--fresh"])
    assert parsed.handler(parsed) == 1
    payload = _json_result(capsys)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "conflict"
    details = payload["error"]["details"]
    assert details["change_state"] == "changed"
    assert any(path[-1] == "chapters" for path in details["changed_units"])
    assert draft.read_bytes() == original_bytes
    assert calls == []


def test_publish_forwards_key_and_force_to_sdk_once(tmp_path, monkeypatch, capsys) -> None:
    parser = timeline_cli.build_parser(object())
    path = tmp_path / "draft.json"
    path.write_text("draft", encoding="utf-8")
    monkeypatch.setattr("astrid.sdk.timeline_checkout.find_draft", lambda *_args, **_kwargs: path)
    calls = []

    class FakeCheckout:
        @staticmethod
        def load(_path):
            return FakeCheckout()

        def publish(self, message, **kwargs):
            calls.append((message, kwargs))
            return {"new_head": "head-4", "narration_pinned": []}

    monkeypatch.setattr(Checkout, "load", classmethod(lambda _cls, _path: FakeCheckout()))
    parsed = parser.parse_args([
        "publish", "main", "--project", "p1", "--idempotency-key", "edit-42", "--force", "-m", "edit",
    ])

    assert parsed.handler(parsed) == 0
    payload = _json_result(capsys)
    assert payload["ok"] is True
    assert payload["data"]["new_head"] == "head-4"
    assert payload["idempotency_key"] == "edit-42"
    assert calls == [("edit", {"idempotency_key": "edit-42", "client": parsed.client, "force": True})]


def test_publish_unavailable_preserves_runtime_result_and_does_not_retry(tmp_path, monkeypatch, capsys) -> None:
    parser = timeline_cli.build_parser(object())
    path = tmp_path / "draft.json"
    path.write_text("draft", encoding="utf-8")
    monkeypatch.setattr("astrid.sdk.timeline_checkout.find_draft", lambda *_args, **_kwargs: path)
    receipt = {"status": "error", "request_id": "saved-request", "error": {
        "code": "runtime_unavailable", "message": "retry this saved request"}}
    calls = []

    class FakeCheckout:
        def publish(self, _message, **_kwargs):
            calls.append(_kwargs)
            return dict(receipt)

    monkeypatch.setattr(Checkout, "load", classmethod(lambda _cls, _path: FakeCheckout()))
    parsed = parser.parse_args(["publish", "main", "--project", "p1", "-m", "edit"])
    assert parsed.handler(parsed) == 1
    payload = _json_result(capsys)
    assert payload["ok"] is False and payload["error"]["code"] == "runtime_unavailable"
    assert payload["error"]["details"]["receipt"] == receipt
    assert calls == [{"idempotency_key": None, "client": parsed.client, "force": False}]
    assert payload["idempotency_key"] == "saved-request"

