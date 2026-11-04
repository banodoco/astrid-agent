"""Held progress uses the real authenticated lifecycle upload/download, CPU only."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from runpod_lifecycle.https import HttpsPodTransport, write_token_file
from runpod_lifecycle.pod_agent import AgentServer


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/pod_session.py"
spec = importlib.util.spec_from_file_location("progress_put_session", SCRIPT)
ps = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ps
spec.loader.exec_module(ps)


@pytest.fixture
def held(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    remote = root / "pod" / "progress.json"
    source_root = root / "pod" / "source"
    source_root.mkdir(parents=True)
    monkeypatch.setattr(ps, "PROGRESS_REMOTE", str(remote))
    monkeypatch.setattr(ps, "SRC_DIR", str(source_root))
    token_dir = root / "synthetic-token"
    token_dir.mkdir(mode=0o700)
    # This bearer exists only inside this CPU loopback fixture.
    token = "synthetic-cpu-only-" + "a" * 40
    write_token_file(token_dir / "pod-session-1234abcd.token", token)
    server = AgentServer(("127.0.0.1", 0), token, root / "agent-state")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    requests = []

    class Transport(HttpsPodTransport):
        def _request(self, method, path, **kwargs):
            assert self.http.headers["Authorization"] == "Bearer " + token
            requests.append((method, path))
            return super()._request(method, path, **kwargs)

        def detached_start(self, *_args, **_kwargs):
            raise AssertionError("progress publication must not launch a job")

        def detached_poll(self, *_args, **_kwargs):
            raise AssertionError("progress reconciliation must not poll a job")

    transport = Transport("fixturepod", token, base_url=f"http://127.0.0.1:{server.server_port}",
                          poll_interval=.001, request_timeout=2)
    args = ps.build_parser().parse_args([
        "--datacenters", ps.DATACENTER, "--sources-dir", str(root / "unused-sources"),
        "--workdir", str(root / "custody"), "--token-dir", str(token_dir),
        "--publish-held-progress"])
    messages, cli_calls = [], []

    def cli(argv, _env):
        cli_calls.append(argv)
        assert argv == ["list", "--name-prefix", "astrid-pod-session-1234abcd", "--json"]
        return 0, json.dumps([{"id": "fixturepod", "name": "astrid-pod-session-1234abcd"}]), ""

    def control():
        return ps.Session(args, cli=cli, transport_factory=lambda *_args: transport,
                          http_status=lambda *_args: pytest.fail("no provider operation"),
                          say=messages.append)

    session = control()
    session.sid, session.pod_id = "1234abcd", "fixturepod"
    source = {"heads": {name: str(i + 1) * 40 for i, name in enumerate(ps.BUNDLE_TREES)},
              "runtime_lock_sha256": "a" * 64}
    (source_root / ".pod-source.json").write_text(json.dumps(source))
    (session.workdir / "source-identity.json").write_text(json.dumps(source))
    (session.workdir / "packets").mkdir()
    session.budget.count = 80
    for step, result in [("install", "ok"), ("rerun", "ok"), ("job:original", "failed"), ("hold", "ok")]:
        session.record(step, result, {"primary_step": "job:original"} if step == "hold" else {})
    session.state = {"storage_diagnostic": {"fixture": "preserved"}, "claim_attempt": {"placement_attempt": 1}}
    original = session.progress_document()
    remote.write_bytes(original)
    (session.workdir / "pod-session-progress.json").write_bytes(original)
    (session.workdir / "pod-session-progress.remote.json").write_bytes(original)
    doc = {"schema": "astrid.pod-held-session.v1", "status": "held", "session": session.sid,
           "pod_id": session.pod_id, "volume_id": "fixture-volume", "volume_gb": args.volume_gb,
           "source": source, "runtime_identity": {"agent_head": source["heads"]["astrid-agent"],
                                                    "runtime_head": source["heads"]["astrid-runtime"]},
           "primary_step": "job:original", "jobs": 80, "charged_jobs": 86,
           "operation": {"id": "b" * 32, "label": "follow-up", "state": "exited", "exit_code": 0},
           "expires_at": time.time() + 1800, "progress_sha256": hashlib.sha256(original).hexdigest(),
           "token_dir": str(token_dir)}
    session._write_held(doc, create=True)
    fixture = SimpleNamespace(session=session, control=control, transport=transport, server=server,
                              remote=remote, original=original, doc=doc, requests=requests,
                              source_path=source_root / ".pod-source.json", messages=messages,
                              cli_calls=cli_calls, path=session._held_path())
    try:
        yield fixture
    finally:
        transport.disconnect()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_standalone_atomic_progress_put_is_authenticated_verified_and_uncharged(held, monkeypatch):
    promotions = []
    finish = held.server.finish_upload

    def promote(meta, state):
        assert Path(state["staging"]).parent == held.remote.parent
        assert Path(state["staging"]).name.startswith(".astrid-upload-")
        assert held.remote.read_bytes() == held.original
        assert hashlib.sha256(Path(state["staging"]).read_bytes()).hexdigest() == state["sha256"]
        durable = json.loads(held.path.read_text())
        assert durable["status"] == "in_use" and durable["operation"]["state"] == "starting"
        assert durable["charged_jobs"] == 86
        promotions.append(state["path"])
        finish(meta, state)

    monkeypatch.setattr(held.server, "finish_upload", promote)
    control = held.control()
    assert control.run_held(publish_progress=True) == 0, held.messages
    doc = control._read_held()
    assert doc["status"] == "held" and doc["operation"]["label"] == "progress-put"
    assert doc["operation"]["state"] == "exited" and doc["operation"]["exit_code"] == 0
    assert doc["jobs"] == doc["charged_jobs"] == control.budget.count == 86
    assert {key: doc[key] for key in ("source", "runtime_identity", "pod_id", "expires_at")} == {
        key: held.doc[key] for key in ("source", "runtime_identity", "pod_id", "expires_at")}
    progress = json.loads(held.remote.read_bytes())
    original = json.loads(held.original)
    assert progress == dict(original, jobs=86)
    assert promotions == [str(held.remote)]
    assert held.server.jobs == {} and not list(held.server.state_dir.glob("job-*.json"))
    assert ("POST", "/uploads") in held.requests
    assert ("POST", "/download-chunk") in held.requests
    assert all("jobs" not in path and "commands" not in path for _, path in held.requests)
    expected = held.remote.read_bytes()
    assert (control.workdir / "pod-session-progress.json").read_bytes() == expected
    assert (control.workdir / "pod-session-progress.remote.json").read_bytes() == expected
    assert doc["progress_sha256"] == doc["operation"]["progress_sha256"] == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("fault", ["interrupted", "lost-response", "checksum", "local-evidence", "terminal-receipt"])
def test_progress_put_fault_retains_exact_intent_and_later_get_only_reconciliation(held, monkeypatch, fault):
    original_put, original_get = held.transport.put, held.transport.get
    original_request = held.transport._request
    control = held.control()
    original_preserve, original_write = control._preserve_held_progress, control._write_held
    attempts = []

    def put(local, remote, **kwargs):
        durable = json.loads(held.path.read_text())
        attempts.append(durable["operation"].copy())
        assert durable["status"] == "in_use" and durable["charged_jobs"] == 86
        assert hashlib.sha256(Path(local).read_bytes()).hexdigest() == durable["operation"]["progress_sha256"]
        original_put(local, remote, **kwargs)
        if fault == "lost-response":
            raise TimeoutError("synthetic lost completed PUT response")

    def request(method, path, **kwargs):
        if fault == "interrupted" and method == "POST" and path.startswith("/uploads/"):
            # Metadata and bytes have reached the real server's same-directory
            # staging file; interrupt before the verification/promotion request.
            raise ConnectionError("synthetic interrupted upload before atomic replace")
        return original_request(method, path, **kwargs)

    def get(remote, local, **kwargs):
        original_get(remote, local, **kwargs)
        if fault == "checksum" and str(remote) == str(held.remote):
            Path(local).write_bytes(Path(local).read_bytes() + b"synthetic corruption")

    def preserve(data, *, filename="held-session-followup-progress.json"):
        if fault == "local-evidence" and filename == "pod-session-progress.json":
            raise OSError("synthetic local progress persistence failure")
        return original_preserve(data, filename=filename)

    def write(doc, *, create=False):
        operation = doc.get("operation") or {}
        if fault == "terminal-receipt" and operation.get("label") == "progress-put" and operation["state"] == "exited":
            raise OSError("synthetic terminal receipt persistence failure")
        return original_write(doc, create=create)

    monkeypatch.setattr(held.transport, "put", put)
    monkeypatch.setattr(held.transport, "get", get)
    monkeypatch.setattr(held.transport, "_request", request)
    monkeypatch.setattr(control, "_preserve_held_progress", preserve)
    monkeypatch.setattr(control, "_write_held", write)
    assert control.run_held(publish_progress=True) == 1
    doc = json.loads(held.path.read_text())
    assert len(attempts) == 1 and doc["status"] == "in_use"
    assert doc["operation"]["id"] == attempts[0]["id"] and doc["operation"]["label"] == "progress-put"
    assert doc["jobs"] == 80 and doc["charged_jobs"] == control.budget.count == 86
    assert doc["progress_sha256"] == held.doc["progress_sha256"]
    assert (control.workdir / "pod-session-progress.json").read_bytes() == held.original
    ledger = [json.loads(line) for line in (control.workdir / "held-session-followup-failures.jsonl").read_text().splitlines()]
    assert len(ledger) == 1 and ledger[0]["retryable"] is False
    assert ledger[0]["operation"]["id"] == attempts[0]["id"] and ledger[0]["charged_jobs"] == 86
    if fault == "interrupted":
        states = [json.loads(path.read_text()) for path in held.server.state_dir.glob("*.json")]
        assert len(states) == 1 and states[0]["complete"] is False
        staging = Path(states[0]["staging"])
        assert staging.is_file() and staging.parent == held.remote.parent
        assert states[0]["offset"] == staging.stat().st_size > 0
        assert held.remote.read_bytes() == held.original
    monkeypatch.setattr(held.transport, "get", original_get)
    # Later reconciliation may only GET. It must not retry the PUT even when
    # the original file is still visible after an interrupted transfer.
    monkeypatch.setattr(held.transport, "put", lambda *_args, **_kwargs: pytest.fail("PUT replayed"))
    reconcile = held.control()
    assert reconcile.run_held(publish_progress=True) == 1
    reconciled = json.loads(held.path.read_text())
    assert reconciled["charged_jobs"] == reconcile.budget.count == 86
    assert reconciled["operation"]["id"] == attempts[0]["id"]
    if fault == "interrupted":
        assert reconciled == doc and held.remote.read_bytes() == held.original
    else:
        assert reconciled["status"] == "held" and reconciled["jobs"] == 86
        assert reconciled["operation"] == dict(attempts[0], state="exited", exit_code=0)
        assert (reconcile.workdir / "pod-session-progress.json").read_bytes() == held.remote.read_bytes()
    assert held.server.jobs == {} and len(attempts) == 1


def test_progress_intent_persistence_failure_never_puts(held, monkeypatch):
    control = held.control()
    write = control._write_held

    def fail_intent(doc, *, create=False):
        if (doc.get("operation") or {}).get("label") == "progress-put":
            raise OSError("synthetic failed durable reservation")
        write(doc, create=create)

    monkeypatch.setattr(control, "_write_held", fail_intent)
    monkeypatch.setattr(held.transport, "put", lambda *_args, **_kwargs: pytest.fail("unreserved PUT"))
    assert control.run_held(publish_progress=True) == 1
    assert json.loads(held.path.read_text())["status"] == "in_use"
    assert control.budget.count == 86 and held.remote.read_bytes() == held.original
    assert not any(path == "/uploads" for _, path in held.requests)


@pytest.mark.parametrize("mismatch", ["remote-source", "candidate", "charge", "pod"])
def test_progress_reconciliation_requires_exact_source_candidate_charge_and_pod(held, monkeypatch, mismatch):
    original_put = held.transport.put

    def lost_response(*args, **kwargs):
        original_put(*args, **kwargs)
        raise TimeoutError("synthetic lost response")

    monkeypatch.setattr(held.transport, "put", lost_response)
    assert held.control().run_held(publish_progress=True) == 1
    doc = json.loads(held.path.read_text())
    operation = doc["operation"].copy()
    if mismatch == "remote-source":
        held.source_path.write_text("{}")
    elif mismatch == "candidate":
        (held.session.workdir / f"held-session-progress-put-{operation['id']}.json").write_text("{}")
    elif mismatch == "charge":
        doc["charged_jobs"] += 1
        held.session._write_held(doc)
    else:
        doc["pod_id"] = "differentpod"
        held.session._write_held(doc)
    before = held.path.read_bytes()
    monkeypatch.setattr(held.transport, "put", lambda *_args, **_kwargs: pytest.fail("PUT replayed"))
    assert held.control().run_held(publish_progress=True) == 1
    assert held.path.read_bytes() == before
    assert json.loads(before)["status"] == "in_use"
    assert held.server.jobs == {}


@pytest.mark.parametrize("operation", [
    {"id": "a" * 32, "label": "progress-put", "state": "starting", "exit_code": None},
    {"id": "a" * 32, "label": "progress-put", "state": "starting", "exit_code": None, "progress_sha256": "wrong"},
    {"id": "a" * 32, "label": "progress-put", "state": "exited", "exit_code": 9, "progress_sha256": "a" * 64},
    {"id": "a" * 32, "label": "follow-up", "state": "exited", "exit_code": 0, "progress_sha256": "a" * 64},
])
def test_typed_progress_receipt_rejects_ambiguous_shape(held, operation):
    doc = dict(held.doc, status="in_use", operation=operation)
    held.session._write_held(doc)
    with pytest.raises(ps.SessionError, match="malformed operation"):
        held.session._read_held()


@pytest.mark.parametrize("field,value", [("jobs", 80), ("progress_sha256", "f" * 64)])
def test_completed_progress_receipt_must_bind_published_digest_and_charges(held, field, value):
    assert held.control().run_held(publish_progress=True) == 0
    doc = json.loads(held.path.read_text())
    doc[field] = value
    held.session._write_held(doc)
    with pytest.raises(ps.SessionError, match="completed progress receipt differs"):
        held.session._read_held()


def test_progress_publish_mode_cannot_bypass_uncertain_job(held):
    doc = dict(held.doc, status="in_use", operation=dict(held.doc["operation"], state="starting", exit_code=None))
    held.session._write_held(doc)
    before = held.path.read_bytes()
    assert held.control().run_held(publish_progress=True) == 1
    assert held.path.read_bytes() == before and not held.requests and not held.cli_calls


def test_publish_held_progress_cli_dispatches_existing_custody_driver(tmp_path, monkeypatch):
    seen = []

    class Controller:
        def __init__(self, args, **_kwargs):
            assert args.publish_held_progress is True

        def run_held(self, **kwargs):
            seen.append(kwargs)
            return 7

    monkeypatch.setattr(ps, "controller_source_paths", lambda _path: [])
    monkeypatch.setattr(ps, "Session", Controller)
    assert ps.main(["--datacenters", ps.DATACENTER, "--sources-dir", str(tmp_path),
                    "--workdir", str(tmp_path), "--publish-held-progress"]) == 7
    assert seen == [{"publish_progress": True}]
