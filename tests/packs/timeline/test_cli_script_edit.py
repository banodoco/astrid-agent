from __future__ import annotations

import copy
import hashlib
import json
import pytest

from astrid.packs.timeline import cli
from astrid.sdk.timeline import journal, store
from astrid.sdk.timeline_checkout import Checkout
from tests.packs.timeline.test_verbs_working_copy import bundle


@pytest.fixture
def runtime_draft(tmp_path, monkeypatch):
    data_root = tmp_path / "astrid-data"
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(data_root))
    monkeypatch.setattr(store, "resolve_ids", lambda *_args, **_kwargs: ("p1", "t1", "head-1"))
    def fetch_bundle(*_args, **_kwargs):
        result = copy.deepcopy(bundle())
        result["project_id"], result["timeline_id"] = "p1", "t1"
        return result

    monkeypatch.setattr(store, "fetch_bundle", fetch_bundle)
    from astrid.sdk import timeline_checkout as checkout_api

    monkeypatch.setattr(checkout_api, "resolve_ids", lambda *_args, **_kwargs: ("p1", "t1", "head-1"))
    return data_root


def _parse(*args):
    return cli.build_parser(None).parse_args(list(args))


def _edit_record(_data_root):
    rows = journal.read(journal.journal_path("p1", "t1"))
    return next(row for row in reversed(rows) if row.kind == "save" and row.outcome == "changed")


def test_lines_text_edit_is_one_voice_line_operation_and_keeps_timing(runtime_draft, capsys):
    parsed = _parse("lines", "main", "--project", "p1", "--line", "s1", "--text", "A revised line.")

    assert parsed.handler(parsed) == 0
    capsys.readouterr()
    path = store.draft_path("p1", "t1", "main")
    saved = Checkout.load(path)
    assert saved.voice("s1").text == "A revised line."
    assert [word.text for word in saved.voice("s1").words] == ["it", "went", "viral"]
    assert saved.voice("s1").clips[0].asset == "P"

    record = _edit_record(runtime_draft)
    assert [(call["op"], call["select"]) for call in record.calls] == [("voice.set-text", "s1")]


def test_lines_from_script_records_source_and_keeps_take_timing(runtime_draft, tmp_path, monkeypatch, capsys):
    script = tmp_path / "narration.json"
    payload = {"segments": [
        {"id": "s1", "text": "First revised line."},
        {"id": "s2", "text": "Second revised line."},
    ]}
    script_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    script.write_bytes(script_bytes)
    monkeypatch.setattr(Checkout, "reflow", lambda *_args, **_kwargs: pytest.fail("text-only edits must not reflow"))

    parsed = _parse("lines", "main", "--project", "p1", "--from-script", str(script))
    assert parsed.handler(parsed) == 0
    capsys.readouterr()

    record = _edit_record(runtime_draft)
    operations = [(call["op"], call["select"]) for call in record.calls]
    assert operations == [("timeline.apply-script", None)]

    path = store.draft_path("p1", "t1", "main")
    saved = Checkout.load(path)
    assert saved.voice("s1").text == "First revised line."
    assert [word.text for word in saved.voice("s1").words] == ["it", "went", "viral"]
    assert saved.voice("s1").clips[0].asset == "P"
    assert saved.voice("s2").text == "Second revised line."
    assert [word.text for word in saved.voice("s2").words] == ["Live", "now"]
    source = saved.bundle["parent"]["config"]["app"]["astrid"]["script_source"]
    expected = {"path": str(script.resolve()), "digest": "sha256:" + hashlib.sha256(script_bytes).hexdigest()}
    assert source == expected

    status = _parse("status", "main", "--project", "p1", "--json")
    assert status.handler(status) == 0
    status_data = json.loads(capsys.readouterr().out)["data"]
    assert status_data["script_source"] == expected

    text_status = _parse("status", "main", "--project", "p1", "--text")
    assert text_status.handler(text_status) == 0
    status_text = capsys.readouterr().out
    assert expected["path"] in status_text and expected["digest"] in status_text
    assert list(tmp_path.rglob("vo-v*.json")) == []


def test_repeated_take_folders_use_the_later_override(runtime_draft, tmp_path, monkeypatch, capsys):
    older, later = tmp_path / "older", tmp_path / "later"
    older.mkdir()
    later.mkdir()
    (older / "s1.wav").write_bytes(b"older disposable take")
    (later / "s1.wav").write_bytes(b"later disposable take")
    (older / "s1.words.json").write_text(json.dumps([[0.1, 0.2, "older"]]), encoding="utf-8")
    (later / "s1.words.json").write_text(json.dumps([[0.1, 0.4, "later"]]), encoding="utf-8")
    script = tmp_path / "take-script.json"
    script.write_text(json.dumps({"segments": [
        {"id": "s1", "text": "Later take text."},
        {"id": "s2", "text": "Live now"},
    ]}), encoding="utf-8")

    selected_assets = []
    monkeypatch.setattr(
        Checkout, "_register_asset",
        lambda _self, _shot, asset, **_kwargs: selected_assets.append(str(asset)) or "P",
    )
    parsed = _parse("lines", "main", "--project", "p1", "--from-script", str(script),
                    "--takes", str(older), str(later))
    assert parsed.handler(parsed) == 0
    capsys.readouterr()

    assert selected_assets == [str(later / "s1.wav")]
    saved = Checkout.load(store.draft_path("p1", "t1", "main"))
    assert [word.text for word in saved.voice("s1").words] == ["later"]


def test_script_read_route_rejects_edit_flags_and_lines_published_is_read_only(runtime_draft, capsys):
    with pytest.raises(SystemExit):
        _parse("script", "main", "--project", "p1", "--from-script", "script.json")

    parsed = _parse("lines", "main", "--project", "p1", "--published", "--line", "s1", "--text", "no")
    assert parsed.handler(parsed) == 2
    assert "read-only" in capsys.readouterr().err
