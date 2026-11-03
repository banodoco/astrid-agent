"""Gate 1: fresh current-realm word anchors through native frontends.

The realm is born through Runtime's normal project/timeline and managed-output
APIs.  The measured edit is the exact retained ``clip.anchor`` carrier; no
historical document or legacy composition writer is used.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
from fractions import Fraction
from http.client import HTTPResponse
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk import workspace_client
from astrid.sdk.client import AstridClient
from astrid.sdk.media_transcript import build_transcript, transcript_document_id
from astrid.sdk.timeline import Checkout
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commands import execute, operations
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_add_media
from astrid.sdk.timeline.native_parent_media_commands import capture_clip_add_media
from astrid.sdk.timeline.sync import TimelineSession
from astrid.sdk.timeline_sheet import NativeSheet
from tests.helpers.runtime import new_runtime_daemon, use_runtime

pytestmark = pytest.mark.integration

PROJECT = "fresh-word-anchor"
TIMELINE = "film"
TARGET = "parent/clip:target"
SOURCE = "occurrence:source-occurrence/clip:source"
NORMAL_RUNTIME = "f103bb46866999c961a8865636f2e66d544e0c9b"
ORDINARY_SCHEMA = "d106dfe8212500709b4072d7bc0486f83c27d5d8de33d215cb52b9ea7d46357d"


class MemorySocket:
    def __init__(self, data):
        self.input = io.BytesIO(data)
        self.output = bytearray()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)

    def settimeout(self, timeout):
        pass


def _command(op, select, args):
    return {"schema_version": 1, "op": op, "version": "1", "select": select, "args": args}


@pytest.fixture
def fresh_realm(tmp_path, monkeypatch):
    """Create one small realm with a verified source and target clip."""
    assert (hashlib.sha256(Path("astrid/core/timeline/schema/edit-command.schema.json").read_bytes()).hexdigest()
            == ORDINARY_SCHEMA)
    assert "clip.anchor" in operations()
    assert json.loads(Path("runtime.lock.json").read_text())["source_commit"] == NORMAL_RUNTIME
    runtime_root = use_runtime(commit=NORMAL_RUNTIME)
    monkeypatch.syspath_prepend(str(runtime_root / "tests"))
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
    from test_generation_publish_v1_settlement import _effect, _fixture, _output, _settle

    def effect(project_id):
        value = _effect(project_id, groups=[{"group_key": "main", "selectors": [
            {"selector": "source", "ordinal": 0, "variant_key": "source", "output_port": "video"},
        ]}])
        value["payload"]["metadata"] = {"params": {"duration_seconds": 2.0}}
        return value

    service, project, task, attempt, admitted = _fixture(tmp_path, effect_factory=effect, slug=PROJECT)
    root = service.store.root
    try:
        _settle(service, attempt, [_output(b"fresh-source", output_port="video", group_key="main",
            variant_key="source", ordinal=0, filename="source.mp4", media_type="video/mp4",
            duration_seconds=2.0)], effect=admitted)
        association, = service.managed_outputs(task["task"]["id"])
        pin = service.get_project_media_selection(project["id"], {
            "association_id": association["association_id"],
            "object_id": association["object_id"],
            "version": association["lifecycle"]["version"],
        }, authorize=lambda: None)
        verified_media = service.get_project_verified_media(project["id"], pin, authorize=lambda: None)
        content = build_transcript(pin["object_id"], "line", "fresh boundary", [
            [0.25, 0.75, "fresh"],
            [1.0, 1.5, "boundary"],
        ])
        document_id = transcript_document_id(project["id"], pin["object_id"], content["source_id"])
        document = service.create_document(project["id"], {
            "document_id": document_id,
            "kind": "media.transcript",
            "content": content,
        }, idempotency_key="fresh-transcript")["data"]
        from runtime_protocol.timeline_commit_inputs import content_digest

        transcript_pin = {
            "document_id": document_id,
            "version": document["version"],
            "content_digest": content_digest(content),
        }
        service.register_timeline_commit_provider(load_timeline_commit_provider("astrid"))
        service.create_timeline(project["id"], TIMELINE, idempotency_key="fresh-timeline")
    finally:
        service.close()

    daemon = new_runtime_daemon(root, support_root=tmp_path / "support",
                                authoring_profile="astrid").start()
    world = SimpleNamespace(project=project["id"], root=root, daemon=daemon, calls=[],
                            pin=transcript_pin, media_pin=pin, verified_media=verified_media,
                            words=content["segments"][0]["words"], target=TARGET, source=SOURCE)

    def transport(method, path, headers, body):
        body = body or b""
        request = (f"{method} {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                   f"Content-Length: {len(body)}\r\n" +
                   "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n").encode() + body
        sock = MemorySocket(request)
        from runtime_protocol.server import RuntimeHandler

        RuntimeHandler(sock, ("127.0.0.1", 1), world.daemon.httpd)
        response = HTTPResponse(MemorySocket(bytes(sock.output)))
        response.begin()
        data = response.read()
        if path.endswith("/commit"):
            world.calls.append((body, response.status, json.loads(data)))
        return response.status, dict(response.headers), data

    monkeypatch.setattr(workspace_client, "GeneratedWorkspaceClient",
                        lambda endpoint, credential: __import__(
                            "astrid_runtime_client.generated", fromlist=["WorkspaceClient"]
                        ).WorkspaceClient(endpoint, credential, transport=transport))
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "client"))
    token = daemon.token

    def client():
        return AstridClient.open(endpoint="http://127.0.0.1:1", credential=token,
                                 realm_id=world.daemon.httpd.runtime.realm["id"], actor_id="owner",
                                 client_name="gate1-fresh", client_version="1", protocol_version="workspace.v1")

    def session(name="session"):
        return TimelineSession.open(world.project, TIMELINE, client=client(),
                                    outbox_path=tmp_path / f"{name}.json")

    world.client = client
    world.session = session
    world.restart = lambda: _restart(world, tmp_path, monkeypatch, transport)

    try:
        setup = session("setup")
        bundle = setup.client.timelines.open_bundle(world.project, TIMELINE).data["bundle"]
        tracks = [capture_track_create(bundle, "parent", {"id": "picture", "kind": "visual"})]
        bundle = execute(bundle, tracks).bundle
        tracks.append(capture_track_create(bundle, "parent", {"id": "anchor", "kind": "visual"}))
        setup.commit_many(tracks, idempotency_key="fresh-parent-tracks")

        bundle = setup.client.timelines.open_bundle(world.project, TIMELINE).data["bundle"]
        occurrence = capture_occurrence_create(bundle, "source-occurrence", "source-shot",
                                                name="Fresh source", track="picture", on=0, duration=4)
        staged = execute(bundle, [occurrence]).bundle
        child_track = capture_track_create(staged, "occurrence:source-occurrence",
                                            {"id": "source-track", "kind": "visual"})
        staged = execute(staged, [child_track]).bundle
        source_row = {"id": "source", "clipType": "video", "asset": pin_alias(world),
                      "track": "source-track", "at": 0, "from": 0, "to": 2, "hold": 2,
                      "speed": 1, "opaque": {"source": ["fresh", None, False]}}
        source_add = capture_occurrence_add_media(staged, "occurrence:source-occurrence", source_row,
                                                  world.media_pin, verified_media=[world.verified_media])
        staged = execute(staged, [source_add], verified_media=[world.verified_media]).bundle
        target_row = {"id": "target", "clipType": "video", "asset": pin_alias(world),
                      "track": "anchor", "at": 0, "from": 0, "to": 2, "hold": 2,
                      "speed": 1, "opaque": {"raw": ["keep", None, False, 7]}}
        target_add = capture_clip_add_media(staged, target_row, world.media_pin,
                                            verified_media=[world.verified_media])
        _raw_commit(world, [occurrence, child_track, source_add, target_add], "fresh-source-target", via="python")
    except Exception:
        world.daemon.stop()
        raise
    yield world
    world.daemon.stop()


def _restart(world, tmp_path, monkeypatch, transport):
    world.daemon.stop()
    world.daemon = new_runtime_daemon(world.root, support_root=tmp_path / "support",
                                      authoring_profile="astrid").start()


def pin_alias(world):
    return world.media_pin["asset"]


def _choice(world):
    return {"source": world.source, "document": copy.deepcopy(world.pin),
            "word_ids": [world.words[0]["id"]], "edge": "start",
            "offset_s": 0, "offset_frames": 0}


def _carrier(world):
    return {"field": "on", "declaration": '"fresh" in line #1', "choice": _choice(world)}


def _pins(world):
    value = world.daemon.httpd.runtime.get_project_timeline(world.project, TIMELINE)
    return {"base_revision_id": value["head_revision_id"], "base_publication_id": value["head_publication_id"]}


def _raw_commit(world, commands, key, *, via):
    pins = _pins(world)
    body = {"api_version": 1, "project_id": world.project, "timeline_id": TIMELINE,
            **pins, "commands": commands, "source": {"via": via}}
    return world.client().timelines._client.commit_timeline(
        world.project, TIMELINE, body, idempotency_key=key)


def _cli(world, path, capsys, *, pins):
    from astrid.packs.timeline.cli import build_parser

    parsed = build_parser(world.client()).parse_args([
        "edit", "commands-file", TIMELINE, str(path), "--project", world.project, "--json",
        "--base-revision-id", pins["base_revision_id"],
        "--base-publication-id", pins["base_publication_id"],
    ])
    assert parsed.handler(parsed) == 0
    return json.loads(capsys.readouterr().out)


def _assert_roundtrip(world, result, before, *, entry):
    accepted = bundle_from_document(result["data"]["canonical_document"])
    before_target = State(before).entity(TARGET)
    after_target = State(accepted).entity(TARGET)
    assert result["data"]["commands"][0]["op"] == "clip.anchor"
    command = result["data"]["commands"][0]
    assert command["select"] == TARGET
    assert command["args"]["choice"]["document"] == world.pin
    assert command["args"]["choice"]["word_ids"] == [world.words[0]["id"]]
    assert after_target["opaque"] == before_target["opaque"]
    assert State(accepted).entity(SOURCE) == State(before).entity(SOURCE)
    assert accepted["parent"]["registry"] == before["parent"]["registry"]
    assert after_target["app"]["astrid"]["word_anchors"]["on"] == world.choice
    occurrence = next(row for row in accepted["placements"] if row["occurrence_id"] == "source-occurrence")
    occurrence_start = Fraction(str(occurrence["placement"]["start_ms"])) / 1000
    occurrence_end = occurrence_start + Fraction(str(occurrence["duration_ms"])) / 1000
    word = world.words[0]
    word_start, word_end = Fraction(str(word["start"])), Fraction(str(word["end"]))
    assert occurrence_start <= word_start < word_end <= occurrence_end
    assert word_end < occurrence_end  # the end boundary is half-open
    assert json.loads(world.calls[-1][0])["source"] == {"via": entry}

    world.restart()
    reloaded = world.session(f"{entry}-reloaded")
    stored = reloaded.client.timelines.open_bundle(world.project, TIMELINE,
                                                    revision_id=result["data"]["accepted_revision_id"])
    assert stored.ok
    assert State(stored.data["bundle"]).entity(TARGET) == after_target
    undone = reloaded.undo([result["receipt"]["receipt_id"]], via=entry)
    assert State(bundle_from_document(undone["data"]["canonical_document"])).entity(TARGET) == before_target
    redone = reloaded.undo([undone["receipt"]["receipt_id"]], via=entry)
    assert State(bundle_from_document(redone["data"]["canonical_document"])).entity(TARGET) == after_target


@pytest.mark.parametrize("entry", ["python", "cli", "sheet"])
def test_fresh_word_transcript_frontends_exact_pin_roundtrip(fresh_realm, tmp_path, capsys, entry):
    world = fresh_realm
    session = world.session(entry)
    before = session.client.timelines.open_bundle(world.project, TIMELINE).data["bundle"]
    world.choice = _choice(world)
    carrier = _carrier(world)
    if entry == "python":
        result = Checkout.attach(session)
        result.clip(TARGET).set(on=carrier)
        accepted = result.flush(via="python")
    elif entry == "cli":
        from astrid.sdk.timeline.native_source_inputs import materialize_transcript_inputs
        from astrid.sdk.timeline.native_word_anchor import capture_anchor

        raw = _command("clip.anchor", TARGET, {
            "field": "on", "declaration": carrier["declaration"], "choice": carrier["choice"]})
        materialize_transcript_inputs(before, [raw], client=world.client().timelines._client,
                                      project=world.project)

        command = capture_anchor(before, TARGET, "on", carrier["declaration"], carrier["choice"])
        path = tmp_path / "anchor.json"
        path.write_text(json.dumps([command]))
        accepted = _cli(world, path, capsys, pins=_pins(world))
    else:
        sheet = NativeSheet.export(session)
        encoded = json.dumps(carrier, separators=(",", ":"))
        edited = sheet.text.replace(' # ref "parent/clip:target"',
                                    f" on={encoded} # ref \"parent/clip:target\"")
        accepted = sheet.apply(edited, session=session, dry_run=False)
    _assert_roundtrip(world, accepted, before, entry=entry)
