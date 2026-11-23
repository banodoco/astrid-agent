from __future__ import annotations

from contextlib import nullcontext
import json
import time
from types import SimpleNamespace

from astrid.core.generation import cli as generation_cli
from astrid.sdk.client import AstridClient
from astrid.sdk.command_timing import TimingCollector, stage, timing_scope
from tests.sdk.test_timeline_sync import connected  # noqa: F401


def test_generation_cli_json_timings_cover_command(monkeypatch, capsys):
    def generate(**_kwargs):
        with stage("generation work"):
            time.sleep(0.05)
        return SimpleNamespace(ok=True, to_dict=lambda: {"image_paths": ["test.png"]})

    monkeypatch.setattr(generation_cli, "_generate_facade", lambda: SimpleNamespace(image=generate))
    monkeypatch.setattr(generation_cli, "_open_client", lambda: nullcontext(object()))
    start = time.monotonic()
    assert generation_cli.main(["image", "a dog", "--json", "--timings"]) == 0
    wall = time.monotonic() - start
    captured = capsys.readouterr()
    rows = json.loads(captured.out)["timings"]
    assert any(row["stage"] == "generation work" for row in rows)
    assert 0.8 * wall <= sum(row["seconds"] for row in rows) <= 1.1 * wall
    assert "generation work" in captured.err


def test_timeline_gateway_json_includes_timings(connected, monkeypatch, capsys):
    from astrid.core.gateway import main

    client, _, _ = connected
    original_open = client.timelines.open_composition
    monkeypatch.setattr(client.timelines, "open_composition", lambda project, timeline, **_options: original_open(project, timeline))
    monkeypatch.setattr(AstridClient, "open_from_launcher", lambda **_kwargs: nullcontext(client))
    code = main(["timelines", "show", "t", "--project", "p", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert "timings" not in payload


def test_nested_waiting_names_only_innermost_stage_and_does_not_double_count(capsys):
    collector = TimingCollector()
    start = time.monotonic()
    with timing_scope(collector):
        with stage("outer"):
            with stage("inner"):
                time.sleep(2.3)
                waiting = capsys.readouterr().err.splitlines()
    wall = time.monotonic() - start
    assert len(waiting) == 1
    assert "inner" in waiting[0].lower()
    assert "waiting" in waiting[0].lower()
    rows = collector.snapshot()
    assert {row["stage"] for row in rows} >= {"outer", "inner"}
    assert 0.9 * wall <= sum(row["seconds"] for row in rows) <= 1.1 * wall


def test_timeline_gateway_edit_reports_accepted_commit_and_timings(connected, monkeypatch, capsys):
    import hashlib
    from astrid.core.gateway import main
    from astrid.sdk import storage_root
    from astrid.sdk.timeline.outbox import request_bytes
    from tests.packs.timeline.test_cli_commit_move import _install_history

    client, generated, path = connected
    _install_history(generated)
    monkeypatch.setattr(storage_root, "resolve_runtime_data_root", lambda: path.parent)
    scope = {"realm_id": "realm", "actor_id": "actor", "project_id": "p", "timeline_id": "t"}
    generated.path = path.parent / "timeline-outbox" / (hashlib.sha256(request_bytes(scope)).hexdigest() + ".json")
    monkeypatch.setattr(AstridClient, "open_from_launcher", lambda **_kwargs: nullcontext(client))
    start = time.monotonic()
    code = main(["timelines", "edit", "t", "--project", "p", "--clip", "parent/clip:a", "--on", "5", "--json", "--timings"])
    wall = time.monotonic() - start
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["data"]["accepted_revision_id"] == "r1"
    assert generated.accepted
    assert 0.9 * wall <= sum(row["seconds"] for row in payload["timings"]) <= 1.1 * wall
    assert "other command work:" in captured.err
    code = main(["timelines", "status", "t", "--project", "p"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    implicit_json = json.loads(captured.out)
    assert implicit_json["ok"] is True
    assert "timings" not in implicit_json


def test_generation_refusal_is_immediate_with_timings(monkeypatch, capsys):
    def refuse(**_kwargs):
        raise ValueError("local refusal")

    monkeypatch.setattr(generation_cli, "_generate_facade", lambda: SimpleNamespace(image=refuse))
    start = time.monotonic()
    result = generation_cli.main(["image", "a dog", "--dry-run", "--json", "--timings"])
    elapsed = time.monotonic() - start
    captured = capsys.readouterr()
    assert result == 2
    assert "local refusal" in captured.out
    assert json.loads(captured.out)["timings"]
    assert elapsed < 2.0
    assert "Waiting for" not in captured.err
