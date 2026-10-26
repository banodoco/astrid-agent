from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrid.core.timeline.authoring_bundle import open_authoring_bundle
from astrid.packs.timeline import cli_document
from tests.timeline.test_authoring_bundle import _closure


def bundle():
    parent, shots, internals = _closure(shared=False)
    return open_authoring_bundle(parent, shot_revisions=shots, internal_timeline_revisions=internals)


def ok(data):
    return SimpleNamespace(ok=True, data=data)


class Client:
    def __init__(self, work=None):
        self.work = work or bundle()
        self.project_calls = []
        self.bundle_calls = []
        self.composition_calls = []
        self.binding_calls = []
        self.projects = SimpleNamespace(show=self.show_project)
        self.timelines = SimpleNamespace(open_bundle=self.open_bundle, open_composition=self.open_composition)
        self.shots = SimpleNamespace(set_text_binding=self.set_text_binding)

    def show_project(self, project):
        self.project_calls.append(project)
        return ok({"project_id": "project-1"})

    def open_bundle(self, project, timeline):
        self.bundle_calls.append((project, timeline))
        return ok({"project_id": project, "timeline_id": "main", "bundle": self.work,
                   "revision_id": "parent-1", "head_revision_id": "parent-1", "is_current_head": True})

    def open_composition(self, project, timeline):
        self.composition_calls.append((project, timeline))
        return ok({"summary": {"head_revision_id": "parent-1"}})

    def set_text_binding(self, project, **kwargs):
        self.binding_calls.append((project, kwargs))
        return ok({"kind": kwargs["kind"], "text": kwargs["text"], "binding_id": "binding-1"})


def invoke(client, argv):
    from astrid.packs.timeline.cli import build_parser

    parsed = build_parser(client).parse_args(["document", *map(str, argv)])
    return parsed.handler(parsed)


def envelope(capsys):
    return json.loads(capsys.readouterr().out)


def test_checkout_uses_canonical_bundle_scope_and_refuses_overwrite(tmp_path, capsys):
    client = Client()
    document = tmp_path / "checkout.json"
    result = invoke(client, ["checkout", "--project", "slug", "--timeline", "main", "--file", document, "--json"])
    assert result == 0
    assert envelope(capsys)["data"]["head"] == "parent-1"
    assert client.project_calls == ["slug"]
    assert client.bundle_calls == [("project-1", "main")]
    saved = json.loads(document.read_text())
    assert saved["project_id"] == "project-1"

    result = invoke(client, ["checkout", "--project", "slug", "--timeline", "main", "--file", document, "--json"])
    assert result == 1
    assert "Refusing to overwrite" in envelope(capsys)["error"]["message"]


def test_checkout_fails_closed_on_mismatched_scope(tmp_path, capsys):
    client = Client()
    client.timelines.open_bundle = lambda *_: ok({"project_id": "other", "timeline_id": "main", "bundle": client.work,
        "revision_id": "parent-1", "head_revision_id": "parent-1", "is_current_head": True})
    result = invoke(client, ["checkout", "--project", "slug", "--timeline", "main", "--file", tmp_path / "checkout.json", "--json"])
    assert result == 1
    assert "confirm the requested project" in envelope(capsys)["error"]["message"]


def test_checkout_fails_closed_on_inconsistent_head(tmp_path, capsys):
    client = Client()
    client.timelines.open_bundle = lambda *_: ok({"project_id": "project-1", "timeline_id": "main", "bundle": client.work,
        "revision_id": "old-head", "head_revision_id": "advanced-head", "is_current_head": False})
    document = tmp_path / "checkout.json"
    result = invoke(client, ["checkout", "--project", "slug", "--timeline", "main", "--file", document, "--json"])
    assert result == 1
    assert "consistent current head" in envelope(capsys)["error"]["message"]
    assert not document.exists()


def test_add_shot_edits_document_in_place_with_default_and_explicit_placement(tmp_path, capsys):
    work = bundle()
    document = tmp_path / "timeline.json"
    document.write_text(json.dumps(work))
    client = Client(work)
    result = invoke(client, ["add-shot", "--file", document, "--shot-id", "shot-added", "--json"])
    assert result == 0
    first = envelope(capsys)["data"]
    assert first["shot_id"] == "shot-added"
    saved = json.loads(document.read_text())
    placed = next(row for row in saved["placements"] if row["shot_id"] == "shot-added")
    assert placed["placement"]["start_ms"] == 0
    assert placed["duration_ms"] == 1

    result = invoke(client, ["add-shot", "--file", document, "--shot-id", "shot-second",
                             "--occurrence-id", "occ-second", "--start-ms", "1250", "--duration-ms", "2300", "--json"])
    assert result == 0
    envelope(capsys)
    saved = json.loads(document.read_text())
    placed = next(row for row in saved["placements"] if row["occurrence_id"] == "occ-second")
    assert placed["placement"]["start_ms"] == 1250
    assert placed["duration_ms"] == 2300


def test_add_shot_invalid_document_leaves_file_unchanged(tmp_path, capsys):
    document = tmp_path / "bad.json"
    original = '["not", "an object"]'
    document.write_text(original)
    assert invoke(Client(), ["add-shot", "--file", document, "--json"]) == 1
    assert "JSON object" in envelope(capsys)["error"]["message"]
    assert document.read_text() == original


def test_add_shot_to_empty_document_generates_and_returns_complete_shot(tmp_path, capsys):
    work = bundle()
    work["shots"] = {}
    work["placements"] = []
    work["source_mapping"]["shots"] = {}
    work["source_mapping"]["placements"] = {}
    document = tmp_path / "empty.json"
    document.write_text(json.dumps(work))
    result = invoke(Client(work), ["add-shot", "--file", document, "--name", "Title", "--json"])
    assert result == 0
    returned = envelope(capsys)["data"]["document"]
    saved = json.loads(document.read_text())
    assert returned == saved
    shot_id, shot = next(iter(saved["shots"].items()))
    assert shot["payload"]["name"] == "Title"
    assert saved["placements"][0]["shot_id"] == shot_id
    assert saved["placements"][0]["occurrence_id"].startswith("occurrence-")


def test_bind_script_registers_and_returns_binding_and_document(tmp_path, capsys):
    client = Client()
    work = bundle()
    document = tmp_path / "timeline.json"
    document.write_text(json.dumps(work))
    text = tmp_path / "narration.txt"
    text.write_text("hello narration")
    shot_id = next(iter(work["shots"]))
    result = invoke(client, ["bind-script", "--file", document, "--shot", shot_id, "--text-file", text,
                             "--expected-head", "0", "--idempotency-key", "bind-1", "--json"])
    assert result == 0
    output = envelope(capsys)["data"]
    assert output["binding"]["binding_id"] == "binding-1"
    assert output["document"] == json.loads(document.read_text())
    assert client.binding_calls[0][1]["text"] == "hello narration"
    assert json.loads(document.read_text())["shots"][shot_id]["payload"]["text_bindings"][-1]["text"] == "hello narration"


def test_bind_script_rejects_conflict_and_stale_checkout(tmp_path, capsys):
    work = bundle()
    shot_id = next(iter(work["shots"]))
    work["shots"][shot_id]["payload"].setdefault("metadata", {})["voiceover_script"] = "old"
    document = tmp_path / "timeline.json"
    document.write_text(json.dumps(work))
    text = tmp_path / "narration.txt"
    text.write_text("new")
    common = ["bind-script", "--file", document, "--shot", shot_id, "--text-file", text,
              "--expected-head", "0", "--idempotency-key", "bind-2", "--json"]
    client = Client()
    assert invoke(client, common) == 1
    assert "conflicts" in envelope(capsys)["error"]["message"]
    assert not client.binding_calls

    clean = bundle()
    document.write_text(json.dumps(clean))
    stale = Client(clean)
    stale.timelines.open_composition = lambda *_: ok({"summary": {"head_revision_id": "advanced"}})
    assert invoke(stale, common) == 1
    assert "Stale checkout" in envelope(capsys)["error"]["message"]
    assert not stale.binding_calls


def test_check_is_offline_and_returns_report_without_rewriting_document(tmp_path, monkeypatch, capsys):
    work = bundle()
    document = tmp_path / "timeline.json"
    document.write_text(json.dumps(work))
    monkeypatch.setattr(cli_document, "workspace", lambda: pytest.fail("check must remain offline"))
    result = invoke(Client(work), ["check", "--file", document, "--json"])
    assert result == 0
    data = envelope(capsys)["data"]
    assert "validation" in data and "diff" in data and "media_imports" in data
    assert data["validation"]["valid"] is True
    assert data["diff"]["change_count"] == 0
    assert data["media_imports"] == []
    assert json.loads(document.read_text()) == work


@pytest.mark.parametrize("verb", ["check", "publish"])
def test_document_rejects_unconsumed_local_locator_before_connection_or_rewrite(tmp_path, capsys, monkeypatch, verb):
    work = bundle()
    work["shots"][next(iter(work["shots"]))]["internal_timeline"]["clips"][0]["src"] = "new.png"
    document = tmp_path / "timeline.json"
    original = json.dumps(work)
    document.write_text(original)
    client = Client(work)
    monkeypatch.setattr(cli_document, "workspace", lambda: pytest.fail("unsupported locator must fail before Runtime connection"))
    argv = [verb, "--file", document, "--json"]
    if verb == "publish":
        argv.extend(["--idempotency-key", "unsupported"])
    assert invoke(client, argv) == 1
    assert "Unconsumed media locator" in envelope(capsys)["error"]["message"]
    assert client.composition_calls == []
    assert document.read_text() == original


@pytest.mark.parametrize("verb", ["check", "add-shot"])
def test_gateway_runs_offline_document_verbs_without_launcher(tmp_path, monkeypatch, capsys, verb):
    from astrid.core.gateway.dispatch import _dispatch_timelines
    from astrid.sdk import AstridClient

    def unexpected_launcher(*args, **kwargs):
        pytest.fail("offline document verbs must not open the launcher client")

    monkeypatch.setattr(AstridClient, "open_from_launcher", unexpected_launcher)
    work = bundle()
    document = tmp_path / "timeline.json"
    document.write_text(json.dumps(work))
    if verb == "check":
        argv = ["document", "check", "--file", str(document), "--json"]
        assert _dispatch_timelines(argv) == 0
    else:
        argv = ["document", "add-shot", "--file", str(document), "--shot-id", "offline-added", "--json"]
        assert _dispatch_timelines(argv) == 0
        assert any(row["shot_id"] == "offline-added" for row in json.loads(document.read_text())["placements"])
    assert json.loads(capsys.readouterr().out)["ok"] is True
