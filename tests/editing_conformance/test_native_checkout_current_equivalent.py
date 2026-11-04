"""Current-form companions for the retained native checkout journeys."""
from __future__ import annotations

import copy
import json
import subprocess

import pytest

from astrid.sdk.timeline.sync import CommitConflict, TimelineSession
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.editing_conformance import native_checkout_current_fixture as current_fixture
from tests.editing_conformance import test_commit_runtime_m1 as m1

pytestmark = pytest.mark.integration
retained = current_fixture.retained


@pytest.fixture
def checkout_current(retained):
    prepared = current_fixture.prepare(retained)
    retained.service.close()
    return prepared


@pytest.fixture
def gate3_current(retained, monkeypatch):
    prepared = current_fixture.prepare(retained, gate3=True)
    retained.service.close()
    m1._disable_scene(monkeypatch)
    return prepared


@pytest.fixture
def wire(monkeypatch):
    from runtime_protocol.server import RuntimeHandler

    observed = []
    lose = set()
    original = RuntimeHandler._send

    def send(handler, status, payload=None, **kwargs):
        if (handler.command == "POST" and handler.path.endswith("/commit")
                and getattr(handler, "_defer_response", False)):
            row = {
                "path": handler.path,
                "key": handler.headers["Idempotency-Key"],
                "body": handler._prefetched_body,
                "status": status,
                "response": copy.deepcopy(payload),
            }
            observed.append(row)
            if status == 200 and row["key"] in lose:
                lose.remove(row["key"])
                raise ConnectionError("current native-checkout lost ACK after durable commit")
        return original(handler, status, payload, **kwargs)

    monkeypatch.setattr(RuntimeHandler, "_send", send)
    return observed, lose


def _history(client, case):
    result = client.timelines.history(case.project, "film", limit=200)
    assert result.ok, result.as_dict()
    return result.data


def _cli(client, case, capsys, *args):
    from astrid.packs.timeline.cli import build_parser

    parsed = build_parser(client).parse_args([*args, "--project", case.project, "--json"])
    code = parsed.handler(parsed)
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert code == 0, (code, value, output.err)
    assert value["ok"], value
    return value


def _attach(case, client, path):
    return Checkout.attach(TimelineSession.open(
        case.project, "film", client=client, outbox_path=path,
    ))


@pytest.mark.parametrize(
    "conflicting",
    [False, True],
    ids=["disjoint-peer", "touching-peer-conflict"],
)
def test_current_checkout_move_trim_lost_ack_restart_and_guarded_undo(
    checkout_current, real_runtime, tmp_path, wire, conflicting,
):
    prepared = checkout_current
    case = prepared["case"]
    observed, lose = wire
    support = tmp_path / "support"
    outbox = tmp_path / "checkout-outbox.json"

    def start(port=0):
        return real_runtime(case.root, support_root=support, port=port, authoring_profile="astrid")

    daemon = start()
    client = m1.open_client(daemon)
    editing = _attach(case, client, outbox)
    editing.clip("parent/clip:music-bed").on(1)
    lose.add("native-checkout-move")
    with pytest.raises(WorkspaceClientError):
        editing.flush(idempotency_key="native-checkout-move")
    moved = copy.deepcopy(observed[-1]["response"])
    flight = json.loads(outbox.read_bytes())["flight"]
    editing.clip("music-bed").trim(1, 4)
    suffix = json.loads(outbox.read_bytes())["pending"][1]
    assert suffix["op"] == "clip.trim"
    assert editing.clip("music-bed").start == 1
    assert editing.clip("music-bed").data["hold"] == 3

    peer = _attach(case, client, tmp_path / "peer-outbox.json")
    peer.clip("music-other").on(2)
    peer.flush()
    if conflicting:
        peer.clip("music-bed").trim(0, 5)
        peer.flush()
    port = daemon.httpd.server_port
    daemon.stop()
    daemon = start(port)
    client = m1.open_client(daemon)
    editing = _attach(case, client, outbox)
    assert json.loads(outbox.read_bytes())["flight"] == flight
    retried = editing.flush()
    assert observed[-1]["response"] == moved
    assert retried["receipt"] == moved["receipt"]
    assert retried["data"]["accepted_publication_id"] == moved["receipt"]["receipt_id"]
    assert observed[-1]["body"] == flight["body"].encode()
    assert observed[-1]["key"] == flight["key"]
    assert json.loads(outbox.read_bytes())["pending"] == [suffix]
    if conflicting:
        with pytest.raises(CommitConflict):
            editing.flush()
        assert json.loads(outbox.read_bytes())["pending"] == [suffix]
        assert editing.clip("music-bed").data["from"] == 1
        return

    accepted = editing.flush()
    assert observed[-1]["key"] != flight["key"]
    assert accepted["data"]["commands"] == [suffix]
    loaded = _attach(case, client, tmp_path / "loaded-outbox.json")
    assert loaded.clip("music-bed").data["hold"] == 3
    assert loaded.clip("music-other").start == 2
    undone = loaded.undo([accepted["receipt"]["receipt_id"]])
    assert loaded.clip("music-bed").start == 1
    assert loaded.clip("music-bed").data["hold"] == 5.5
    assert loaded.clip("music-other").start == 2
    loaded.undo([undone["receipt"]["receipt_id"]])
    assert loaded.clip("music-bed").data["hold"] == 3
    with pytest.raises(CommitConflict):
        loaded.undo([accepted["receipt"]["receipt_id"]])
    assert _history(client, case)["head"]["publication_id"] != accepted["receipt"]["receipt_id"]


def test_current_checkout_commands_file_preserves_opaque_unchanged_write_and_undo(
    checkout_current, real_runtime, tmp_path, capsys,
):
    prepared = checkout_current
    case = prepared["case"]
    daemon = real_runtime(case.root, support_root=tmp_path / "support", authoring_profile="astrid")
    client = m1.open_client(daemon)
    path = tmp_path / "params-outbox.json"
    editing = _attach(case, client, path)
    editing.clip("music-bed").set(opaque={"literal": [None, False, 0]}, _allow_new=True)
    captured = json.loads(path.read_bytes())
    commands_file = tmp_path / "params.json"
    commands_file.write_text(json.dumps(captured["pending"]), encoding="utf-8")
    accepted = _cli(
        client, case, capsys, "edit", "commands-file", "film", str(commands_file),
        "--base-revision-id", captured["base"]["revision_id"],
        "--base-publication-id", captured["base"]["publication_id"],
    )
    assert accepted["data"]["commands"] == captured["pending"]

    other = _attach(case, client, tmp_path / "same-outbox.json")
    other.clip("music-bed").set(opaque={"literal": [None, False, 0]})
    again = other.flush()
    assert again["data"]["accepted_revision_id"] == accepted["data"]["accepted_revision_id"]
    assert again["receipt"]["receipt_id"] != accepted["receipt"]["receipt_id"]
    other.clip("music-bed").set(opaque=None)
    cleared = other.flush()
    assert "opaque" not in other.clip("music-bed").params
    other.undo([cleared["receipt"]["receipt_id"]])
    assert other.clip("music-bed").params["opaque"] == {"literal": [None, False, 0]}
    with pytest.raises(CommitConflict):
        other.undo([accepted["receipt"]["receipt_id"]])


def test_current_checkout_gate3_reload_undo_and_visualize_report_unavailable(
    gate3_current, real_runtime, tmp_path, monkeypatch,
):
    prepared = gate3_current
    case = prepared["case"]
    daemon = real_runtime(case.root, support_root=tmp_path / "support", authoring_profile="astrid")
    client = m1.open_client(daemon)
    outbox = tmp_path / "gate3-outbox.json"

    def attach():
        return _attach(case, client, outbox)

    def renderer_process(*_args, **_kwargs):
        raise AssertionError("the stopped renderer must not be launched by current checkout")

    with monkeypatch.context() as stopped:
        stopped.setattr(subprocess, "Popen", renderer_process)
        stopped.setattr(subprocess, "run", renderer_process)
        editing = attach()
        editing.clip("parent/clip:music-bed").on(1)
        accepted = editing.flush(idempotency_key="native-checkout-gate3")
        assert accepted["data"]["accepted_revision_id"]
        reloaded = attach()
        assert reloaded.clip("parent/clip:music-bed").start == 1
        undone = reloaded.undo([accepted["receipt"]["receipt_id"]])
        assert undone["data"]["accepted_revision_id"]
        after_undo = attach()
        assert after_undo.clip("parent/clip:music-bed").start == 0
        preview = client.timelines.visualize(
            case.project, "film", mode="composed", options={"formats": ["png"]},
        )

    assert not preview.ok
    assert preview.error["sdk_error"] == "CapabilityRuntimeError"
    assert preview.error["sdk_category"] == "runtime"
    assert preview.error["code"] == "not_found"
    assert preview.error["details"]["capability_id"] == "rendering.timeline_visualize"
    assert preview.error["message"] == "capability is not registered"
