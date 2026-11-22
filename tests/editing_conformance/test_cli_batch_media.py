"""CLI media batches make one Runtime publication and report partial imports."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from astrid.packs.timeline.cli import build_parser
from astrid.sdk.timeline import store
from tests.editing_conformance.test_commit_runtime_m1 import open_client

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime_wire(monkeypatch):
    from tests.helpers.runtime import use_runtime

    use_runtime()
    from runtime_protocol.server import RuntimeHandler

    original = RuntimeHandler._send
    calls = []
    lose = set()

    def send(handler, status, payload=None, **kwargs):
        if (handler.command == "POST" and handler.path.endswith("/commit")
                and getattr(handler, "_defer_response", False)):
            key = handler.headers["Idempotency-Key"]
            calls.append({"key": key, "body": handler._prefetched_body,
                          "status": status, "response": copy.deepcopy(payload)})
            if status == 200 and key in lose:
                lose.remove(key)
                raise ConnectionError("test lost ACK after durable Runtime acceptance")
        return original(handler, status, payload, **kwargs)

    monkeypatch.setattr(RuntimeHandler, "_send", send)
    return calls, lose


@pytest.fixture
def editing_runtime(real_runtime, tmp_path, monkeypatch, runtime_wire):
    from tests.helpers.runtime import use_runtime

    use_runtime()
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "client-data"))
    root = tmp_path / "realm"
    support = tmp_path / "support"
    daemon = real_runtime(root, support_root=support, authoring_profile="astrid")
    service = daemon.service
    project = service.create_project({"slug": "batch-media", "name": "Batch media"})["id"]
    service.create_timeline(project, "film", idempotency_key="bootstrap")

    world = SimpleNamespace(root=root, support=support, daemon=daemon, project=project)

    def attach(current):
        world.daemon = current
        world.client = open_client(current)
        world.service = current.service
        return world.client

    def create_audio_track(client, track_key):
        from astrid.sdk.timeline.capture import capture_track_create

        opened = client.timelines.open_bundle(project, "film")
        assert opened.ok, opened.as_dict()
        command = capture_track_create(opened.data["bundle"], "parent", {
            "id": "A1", "kind": "audio", "label": "Audio",
        })
        history = client.timelines.history(project, "film", limit=100).data
        head = history["head"]
        body = {
            "api_version": 1, "project_id": project, "timeline_id": "film",
            "base_revision_id": head["revision_id"],
            "base_publication_id": head["publication_id"], "commands": [command],
        }
        created = client.timelines.commit(project, "film", body, idempotency_key=track_key)
        assert created.ok, created.as_dict()
        return created

    def restart():
        world.daemon.stop()
        return attach(real_runtime(root, support_root=support, authoring_profile="astrid"))

    world.restart = restart
    attach(daemon)
    world.seed_audio_track = lambda: create_audio_track(world.client, "seed-audio-track")
    world.seed_audio_track()
    runtime_wire[0].clear()
    return world


def _write(path, operations):
    path.write_text(json.dumps({"operations": operations}), encoding="utf-8")


def _run(client, project, capsys, path, key):
    parsed = build_parser(client).parse_args([
        "edit", "film", "--project", project, "--operations", str(path),
        "--idempotency-key", key, "--json",
    ])
    code = parsed.handler(parsed)
    output = capsys.readouterr()
    return code, json.loads(output.out or output.err)


def _png(path, color="red"):
    Image.new("RGB", (16, 16), color).save(path)
    return path


def test_twenty_adds_use_one_runtime_commit_and_receipt(
    editing_runtime, runtime_wire, tmp_path, capsys,
):
    world = editing_runtime
    client = world.client
    source = _png(tmp_path / "frame.png")
    operations = [
        {"op": "add", "source": str(source), "at": index * 2, "hold": 1,
         "track": "V1", "id": f"shot-{index + 1:02d}"}
        for index in range(20)
    ]
    path = tmp_path / "operations.json"
    _write(path, operations)
    calls, _lose = runtime_wire
    before = client.timelines.history(world.project, "film", limit=100).data

    code, result = _run(client, world.project, capsys, path, "batch-twenty")

    assert code == 0 and result["ok"], result
    assert result["receipt"]["receipt_id"]
    assert len(calls) == 1, calls
    assert calls[0]["key"] == "batch-twenty"
    body = json.loads(calls[0]["body"])
    assert body["project_id"] == world.project and body["timeline_id"] == "film"
    media_commands = [command for command in body["commands"] if command["op"] == "clip.add-media"]
    assert len(media_commands) == 20
    assert len(body["commands"]) in (20, 21)
    assert all(command["op"] in {"clip.add-media", "track.create"} for command in body["commands"])
    after = client.timelines.history(world.project, "film", limit=100).data
    assert len(after["items"]) == len(before["items"]) + 1
    assert after["items"][-1]["publication_id"] == result["receipt"]["receipt_id"]
    clips = result["data"]["canonical_document"]["parent_revision"]["payload"]["clips"]
    assert [clip["id"] for clip in clips] == [item["id"] for item in operations]
    assert [clip["at"] for clip in clips] == [item["at"] for item in operations]


def test_bad_second_add_reports_prior_import_and_publishes_nothing(
    editing_runtime, runtime_wire, tmp_path, capsys,
):
    world = editing_runtime
    client = world.client
    source = _png(tmp_path / "frame.png", "blue")
    path = tmp_path / "operations.json"
    _write(path, [
        {"op": "add", "source": str(source), "at": 0, "hold": 1,
         "track": "V1", "id": "first"},
        {"op": "add", "source": str(source), "at": 2, "hold": 1,
         "track": "A1", "id": "bad"},
    ])
    calls, _lose = runtime_wire
    before = client.timelines.history(world.project, "film", limit=100).data

    code, result = _run(client, world.project, capsys, path, "batch-bad-second")

    assert code == 1 and not result["ok"], result
    imports = result["error"]["details"]["imports"]
    assert len(imports) == 1 and imports[0]["catalog_write"] is True, imports
    assert imports[0]["retained_on_undo"] is True
    assert calls == []
    assert client.timelines.history(world.project, "film", limit=100).data == before
    assert store.find_draft(world.project, "film", client=client) is None


def test_lost_ack_retries_original_commit_after_restart_without_input_files(
    editing_runtime, runtime_wire, tmp_path, capsys,
):
    world = editing_runtime
    source = _png(tmp_path / "frame.png", "green")
    path = tmp_path / "operations.json"
    _write(path, [{"op": "add", "source": str(source), "at": 0, "hold": 1,
                  "track": "V1", "id": "first"}])
    client = world.client
    calls, lose = runtime_wire
    lose.add("batch-lost")

    code, failure = _run(client, world.project, capsys, path, "batch-lost")

    assert code == 1 and not failure["ok"], failure
    assert len(calls) == 1
    original = calls[0]
    history = client.timelines.history(world.project, "film", limit=100).data
    path.unlink()
    source.unlink()
    retry_client = world.restart()

    code, result = _run(retry_client, world.project, capsys, path, "batch-lost")

    assert code == 0 and result["ok"], result
    assert len(calls) == 2
    assert calls[1]["key"] == original["key"]
    assert calls[1]["body"] == original["body"]
    assert result["receipt"] == history["items"][-1]["receipt"]
    assert retry_client.timelines.history(world.project, "film", limit=100).data == history


def test_failing_first_add_reports_its_import(editing_runtime, runtime_wire, tmp_path, capsys):
    world = editing_runtime
    client = world.client
    source = _png(tmp_path / "frame.png", "purple")
    path = tmp_path / "operations.json"
    _write(path, [{"op": "add", "source": str(source), "at": 0, "hold": 1,
                  "track": "A1", "id": "bad"}])
    calls, _lose = runtime_wire
    before = client.timelines.history(world.project, "film", limit=100).data

    code, result = _run(client, world.project, capsys, path, "batch-bad-first")

    assert code == 1 and not result["ok"], result
    imports = result["error"]["details"]["imports"]
    assert len(imports) == 1 and imports[0]["catalog_write"] is True, imports
    assert imports[0]["retained_on_undo"] is True
    assert calls == []
    assert client.timelines.history(world.project, "film", limit=100).data == before
    assert store.find_draft(world.project, "film", client=client) is None


def test_duplicate_json_keys_are_refused_before_import_or_commit(
    editing_runtime, runtime_wire, tmp_path, capsys, monkeypatch,
):
    world = editing_runtime
    client = world.client
    source = _png(tmp_path / "frame.png", "orange")
    path = tmp_path / "operations.json"
    path.write_text(
        '{"operations": [{"op": "add", "source": ' + json.dumps(str(source))
        + ', "source": ' + json.dumps(str(source))
        + ', "at": 0, "hold": 1}]}',
        encoding="utf-8",
    )
    transport = client._remote._transport
    generated = transport._generated
    original_import = generated.import_project_media
    imports = []

    def observe_import(*args, **kwargs):
        imports.append((args, kwargs))
        return original_import(*args, **kwargs)

    monkeypatch.setattr(generated, "import_project_media", observe_import)
    calls, _lose = runtime_wire
    before = client.timelines.history(world.project, "film", limit=100).data

    code, result = _run(client, world.project, capsys, path, "batch-duplicate-key")

    assert code == 1 and not result["ok"], result
    assert "duplicate key" in result["error"]["message"]
    assert imports == []
    assert calls == []
    assert client.timelines.history(world.project, "film", limit=100).data == before
    assert store.find_draft(world.project, "film", client=client) is None
