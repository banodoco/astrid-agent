from __future__ import annotations

import json

import pytest

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
    assert payload["data"] == {
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
    assert payload["data"] == {
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
        "publish", "main", "--project", "p1", "--idempotency-key", "edit-42", "--force", "-m", "edit", "--json",
    ])

    assert parsed.handler(parsed) == 0
    payload = _json_result(capsys)
    assert payload["ok"] is True
    assert payload["data"]["new_head"] == "head-4"
    assert payload["idempotency_key"] == "edit-42"
    assert calls == [("edit", {"idempotency_key": "edit-42", "client": parsed.client, "force": True})]


@pytest.mark.parametrize("as_json", [False, True])
def test_publish_partial_narration_error_preserves_receipt_and_does_not_retry(tmp_path, monkeypatch, capsys, as_json) -> None:
    parser = timeline_cli.build_parser(object())
    path = tmp_path / "draft.json"
    path.write_text("draft", encoding="utf-8")
    monkeypatch.setattr("astrid.sdk.timeline_checkout.find_draft", lambda *_args, **_kwargs: path)
    receipt = {
        "old_head": "head-1",
        "new_head": "head-2",
        "narration_pinned": [],
        "narration_error": "binding CAS conflict",
    }
    calls = []

    class FakeCheckout:
        @staticmethod
        def load(_path):
            return FakeCheckout()

        def edits(self):
            return {"changes": []}

        def changed_cut_ids(self):
            return []

        def publish(self, _message, **_kwargs):
            calls.append(True)
            return dict(receipt)

    monkeypatch.setattr(Checkout, "load", classmethod(lambda _cls, _path: FakeCheckout()))
    args = ["publish", "main", "--project", "p1", "--idempotency-key", "edit-43", "-m", "edit"]
    parsed = parser.parse_args(args + (["--json"] if as_json else []))

    assert parsed.handler(parsed) == 1
    captured = capsys.readouterr()
    if as_json:
        payload = json.loads(captured.out)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "partial_publication"
        assert payload["error"]["details"]["receipt"] == receipt
        assert payload["idempotency_key"] == "edit-43"
    else:
        assert captured.out == ""
        assert "error partial_publication:" in captured.err
    assert calls == [True]




def test_publish_human_output_is_a_short_summary_and_json_keeps_the_envelope(tmp_path, monkeypatch, capsys) -> None:
    parser = timeline_cli.build_parser(object())
    path = tmp_path / "draft.json"
    path.write_text("draft", encoding="utf-8")
    monkeypatch.setattr("astrid.sdk.timeline_checkout.find_draft", lambda *_args, **_kwargs: path)
    receipt = {
        "old_head": "authoring-parent-revision-abcdef1234567890",
        "new_head": "head-2",
        "narration_pinned": [],
        "message": "tighten c3",
        "structure": "x" * 200_000,
    }
    calls = []

    class FakeCheckout:
        @staticmethod
        def load(_path):
            return FakeCheckout()

        def edits(self):
            calls.append("edits")
            assert "publish" not in calls
            return {"changes": [{"clip_id": "a"}, {"clip_id": "b"}]}

        def changed_cut_ids(self):
            calls.append("cuts")
            assert "publish" not in calls
            return ["c3"]

        def publish(self, _message, **_kwargs):
            calls.append("publish")
            return dict(receipt)

    monkeypatch.setattr(Checkout, "load", classmethod(lambda _cls, _path: FakeCheckout()))

    human = parser.parse_args(["publish", "main", "--project", "p1", "-m", "tighten c3"])
    assert human.handler(human) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "published abcdef12 → head-2: tighten c3"
    assert lines[1] == "changed: 2 clips in 1 cut"
    assert lines[2].startswith("next: timelines edit main --project p1")
    assert len(out) < 1000 and "{" not in out
    assert calls == ["edits", "cuts", "publish"]

    calls.clear()
    as_json = parser.parse_args(["publish", "main", "--project", "p1", "-m", "tighten c3", "--json"])
    assert as_json.handler(as_json) == 0
    payload = _json_result(capsys)
    assert set(payload) == {"ok", "data", "error", "receipt", "idempotency_key"}
    assert payload["data"] == receipt
    assert calls == ["publish"]


def test_publish_still_requires_a_revision_message() -> None:
    parser = timeline_cli.build_parser(object())
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["publish", "main", "--project", "p1"])
    assert error.value.code == 2


@pytest.mark.parametrize("as_json", [False, True])
def test_publish_cas_refusal_is_not_retried_or_reported_as_success(tmp_path, monkeypatch, capsys, as_json) -> None:
    path = tmp_path / "draft.json"
    path.write_text("draft", encoding="utf-8")
    monkeypatch.setattr("astrid.sdk.timeline_checkout.find_draft", lambda *_args, **_kwargs: path)
    calls = []
    conflict = RuntimeError("CAS head changed")
    class FakeCheckout:
        def edits(self):
            return {"changes": []}

        def changed_cut_ids(self):
            return []

        def publish(self, message, **kwargs):
            calls.append((message, kwargs))
            raise conflict

    monkeypatch.setattr(Checkout, "load", classmethod(lambda _cls, _path: FakeCheckout()))
    args = ["publish", "main", "--project", "p1", "-m", "edit", "--idempotency-key", "cas-key"]
    parsed = timeline_cli.build_parser(object()).parse_args(args + (["--json"] if as_json else []))
    with pytest.raises(RuntimeError) as error:
        parsed.handler(parsed)
    assert error.value is conflict
    assert calls == [("edit", {"idempotency_key": "cas-key", "client": parsed.client, "force": False})]
    assert capsys.readouterr().out == ""


def test_publish_of_a_clean_working_copy_publishes_nothing(tmp_path, monkeypatch, capsys) -> None:
    parser = timeline_cli.build_parser(object())
    path = tmp_path / "draft.json"
    path.write_text("draft", encoding="utf-8")
    monkeypatch.setattr("astrid.sdk.timeline_checkout.find_draft", lambda *_args, **_kwargs: path)
    calls = []

    class FakeCheckout:
        @staticmethod
        def load(_path):
            return FakeCheckout()

        def document(self):
            return {}

        def narration_changes(self):
            return {}

        def publish(self, *_args, **_kwargs):
            calls.append("publish")
            return {"new_head": "head-empty"}

    monkeypatch.setattr(Checkout, "load", classmethod(lambda _cls, _path: FakeCheckout()))
    monkeypatch.setattr(timeline_cli, "_checkout_change_units", lambda _checkout: (True, []))
    parsed = parser.parse_args(["publish", "main", "--project", "p1", "-m", "nothing", "--json"])

    assert parsed.handler(parsed) == 0
    payload = _json_result(capsys)
    assert payload["ok"] is True
    assert payload["data"]["published"] is False
    assert calls == []


def test_list_follows_every_cursor_page(monkeypatch, capsys) -> None:
    from astrid.sdk.contracts import DomainResult

    pages = {
        None: [[{"timeline_id": "film", "archived": False, "version": 1}], "c1"],
        "c1": [[], "c2"],
        "c2": [[{"timeline_id": "trial-a", "archived": False, "version": 1}], None],
    }

    class FakeTimelines:
        def list(self, _project, *, cursor=None, include_archived=False):
            return DomainResult.success(pages[cursor])

    class FakeClient:
        timelines = FakeTimelines()

    parser = timeline_cli.build_parser(object())
    parsed = parser.parse_args(["list", "--project", "p1", "--json"])
    parsed.client = FakeClient()

    assert parsed.handler(parsed) == 0
    payload = _json_result(capsys)
    assert [row["timeline_id"] for row in payload["data"][0]] == ["film", "trial-a"]
    assert payload["data"][1] is None
