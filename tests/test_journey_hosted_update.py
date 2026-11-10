"""The journey's hosted Update request and the runner-level astrid.haus intercept."""
import hashlib
import http.server
import subprocess
import time
import json
import threading
from pathlib import Path

import pytest

from scripts import journey_hosted_update as hosted

OPERATION = "a" * 32


class Door:
    """A loopback stand-in for the Runtime door that records the raw requests it receives."""

    def __init__(self, replies):
        self.requests, self.replies = [], list(replies)
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                outer.requests.append((self.command, self.path, [(k, v) for k, v in self.headers.items()]))
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                reply = outer.replies.pop(0) if len(outer.replies) > 1 else outer.replies[0]
                if isinstance(reply, Exception):
                    self.connection.close()
                    return
                status, body = reply
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _answer

            def log_message(self, *_):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/runtime/v1/health"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def door():
    created = []

    def make(*replies):
        created.append(Door(replies))
        return created[-1]
    yield make
    for item in created:
        item.close()


def test_post_is_what_the_hosted_page_sends(door):
    server = door((202, {"accepted": True, "state": "accepted", "operation_id": OPERATION, "message": "Update accepted."}))
    assert hosted.post_update(server.url) == {"status": 202, "operation_id": OPERATION}
    (method, path, headers), = server.requests
    names = {key.lower(): value for key, value in headers}
    assert (method, path) == ("POST", "/api/runtime/v1/update")
    assert names["origin"] == "https://astrid.haus" and names["content-length"] == "0"
    # Nothing that identifies the caller beyond its origin, and no body or body type.
    assert {key.lower() for key, _ in headers} == {"host", "origin", "content-length"}
    assert sorted(key.lower() for key in hosted.SENT_HEADERS) == sorted(names)


@pytest.mark.parametrize("reply", [
    (409, {"error": "conflict", "details": {"reason": "busy"}}),
    (403, {"error": "browser_credentials_forbidden"}),
    (202, {"accepted": False, "state": "recovery-needed"}),
    (202, {"accepted": True, "state": "accepted", "operation_id": "short"}),
])
def test_post_refusals_are_reported_with_the_door_answer(door, reply):
    server = door(reply)
    with pytest.raises(RuntimeError, match="Update"):
        hosted.post_update(server.url)


def terminal(outcome="updated"):
    return {"state": "terminal", "outcome": outcome, "operation_id": OPERATION}


def test_wait_terminal_tolerates_the_restart_and_returns_on_updated(door):
    server = door((200, {"state": "active", "operation_id": OPERATION}), ConnectionResetError(),
                  (200, {"state": "none"}), (200, terminal()))
    assert hosted.wait_terminal(server.url, OPERATION, timeout=30, interval=0)["outcome"] == "updated"
    assert all(method == "GET" and path == "/api/runtime/v1/update/status" for method, path, _ in server.requests)


@pytest.mark.parametrize("outcome", ["failed", "rolled_back"])
def test_wait_terminal_fails_on_any_other_outcome(door, outcome):
    with pytest.raises(RuntimeError, match=outcome):
        hosted.wait_terminal(door((200, terminal(outcome))).url, OPERATION, timeout=30, interval=0)


def test_wait_terminal_only_fails_on_recovery_needed_after_the_grace(door):
    server = door((200, {"state": "recovery-needed", "operation_id": OPERATION}))
    now = [0.0]
    with pytest.raises(RuntimeError, match="needs recovery"):
        hosted.wait_terminal(server.url, OPERATION, timeout=1000, interval=10, recovery_grace=30,
                             sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), clock=lambda: now[0])
    assert 3 <= len(server.requests) <= 6


def test_wait_terminal_times_out_with_the_last_status(door):
    server = door((200, {"state": "active", "operation_id": OPERATION}))
    now = [0.0]
    with pytest.raises(RuntimeError, match="did not finish.*active"):
        hosted.wait_terminal(server.url, OPERATION, timeout=20, interval=10,
                             sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), clock=lambda: now[0])


def test_status_of_a_different_operation_is_not_this_update(door):
    server = door((200, {"state": "terminal", "outcome": "updated", "operation_id": "b" * 32}))
    now = [0.0]
    with pytest.raises(RuntimeError, match="did not finish"):
        hosted.wait_terminal(server.url, OPERATION, timeout=5, interval=1,
                             sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), clock=lambda: now[0])


def test_release_routes_serve_the_stand_in_manifest_and_the_apps_own_bootstrap(tmp_path, monkeypatch):
    public = tmp_path / "public"
    public.mkdir()
    (public / "install").write_bytes(b"#!/bin/sh\n")
    (public / "install.ps1").write_bytes(b"# ps1\n")
    monkeypatch.setattr(hosted.sys, "platform", "linux")
    routes = hosted.release_routes(tmp_path, b"{}")
    assert routes == {"/install-manifest.json": b"{}", "/install": b"#!/bin/sh\n", "/install.ps1": b"# ps1\n"}
    (public / "install").unlink()
    with pytest.raises(RuntimeError, match="public/install"):
        hosted.release_routes(tmp_path, b"{}")
    monkeypatch.setattr(hosted.sys, "platform", "win32")
    assert "/install.ps1" in hosted.release_routes(tmp_path, b"{}")


def test_hosts_mapping_is_added_once_and_removed_exactly(tmp_path, monkeypatch):
    path = tmp_path / "hosts"
    monkeypatch.setattr(hosted, "hosts_path", lambda: path)
    for original in (b"127.0.0.1 localhost\n10.0.0.1 other\n", b"127.0.0.1 localhost", b"a\r\nb\r\n"):
        path.write_bytes(original)
        hosted.hosts_add()
        hosted.hosts_add()
        added = path.read_bytes()
        assert added.count(hosted.MARK.encode()) == 1 and b"127.0.0.1 astrid.haus" in added
        if b"\r\n" in original:
            assert added.endswith(b"\r\n")
        hosted.hosts_remove()
        assert path.read_bytes().rstrip() == original.rstrip()
        hosted.hosts_remove()


def test_pem_block_is_removed_without_touching_other_certificates(tmp_path):
    bundle = tmp_path / "cert.pem"
    other = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
    bundle.write_text(other + hosted.PEM_BEGIN + "-----BEGIN CERTIFICATE-----\nBBBB\n-----END CERTIFICATE-----\n"
                      + hosted.PEM_END)
    hosted._strip_pem_block(bundle)
    assert bundle.read_text() == other


def test_certificates_chain_to_the_throwaway_ca_for_astrid_haus_only(tmp_path):
    ca, leaf, key = hosted.make_certs(tmp_path)
    from cryptography import x509
    certificate = x509.load_pem_x509_certificate(leaf.read_bytes())
    authority = x509.load_pem_x509_certificate(ca.read_bytes())
    assert certificate.issuer == authority.subject
    assert certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
        x509.DNSName) == ["astrid.haus"]
    assert authority.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    assert certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    # The leaf verifies against the CA and a context that does not hold the CA rejects it.
    import ssl
    ssl.create_default_context(cafile=str(ca))  # loads
    assert hosted.CA_NAME in authority.subject.rfc4514_string()


def test_crypto_closure_comes_from_the_hashed_lock():
    text = hosted.CRYPTO_LOCK.read_text()
    for package in hosted.CRYPTO_PACKAGES:
        assert f"\n{package}==" in "\n" + text


class FakeIntercept:
    instances = []

    def __init__(self, routes, *, python=None):
        self.routes, self.python, self.reverted = routes, python, {}
        self.log = []
        FakeIntercept.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        self.reverted = {"done": True, "clean": True, "problems": []}

    def hits(self):
        return list(self.log)


def make_app(tmp_path, monkeypatch):
    (tmp_path / "public").mkdir(exist_ok=True)
    (tmp_path / "public" / "install").write_bytes(b"#!/bin/sh\n")
    monkeypatch.setattr(hosted.sys, "platform", "linux")


def test_run_serves_then_posts_then_waits_and_reports_the_served_files(tmp_path, monkeypatch):
    make_app(tmp_path, monkeypatch)
    FakeIntercept.instances.clear()
    order = []

    def post(base):
        order.append("post")
        FakeIntercept.instances[0].log += [{"method": "GET", "path": "/install-manifest.json", "status": 200},
                                           {"method": "GET", "path": "/install", "status": 200}]
        return {"status": 202, "operation_id": OPERATION}

    monkeypatch.setattr(hosted, "post_update", post)
    monkeypatch.setattr(hosted, "wait_terminal", lambda base, op, timeout: order.append("wait") or terminal())
    root = tmp_path / "program"
    root.mkdir()
    log = tmp_path / "update.log"
    log.write_text("worker output\n")
    (root / "install.json").write_text(json.dumps({"update_job": {"log_path": str(log)}}))
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = hosted.run_hosted_update(python="/installed/python", app=tmp_path, manifest=b'{"release": 2}',
                                      door_base="http://127.0.0.1:1/x", root=root, evidence=evidence,
                                      timeout=5, intercept=FakeIntercept)
    assert order == ["post", "wait"]
    assert FakeIntercept.instances[0].python == "/installed/python"
    assert record["request"] == {"method": "POST", "path": "/api/runtime/v1/update", "origin": "https://astrid.haus",
                                 "content_length": 0, "headers": ["Host", "Origin", "Content-Length"],
                                 "authorization": False, "cookie": False, "status": 202, "operation_id": OPERATION}
    assert record["served"]["/install-manifest.json"] == 1 and record["served"]["/install"] == 1
    assert record["intercept_reverted"] is True and record["final"]["outcome"] == "updated"
    assert (evidence / "update-bootstrap.log").read_text() == "worker output\n"


def test_run_fails_when_runtime_never_fetched_the_stand_in(tmp_path, monkeypatch):
    make_app(tmp_path, monkeypatch)
    monkeypatch.setattr(hosted, "post_update", lambda base: {"status": 202, "operation_id": OPERATION})
    monkeypatch.setattr(hosted, "wait_terminal", lambda *a, **k: terminal())
    with pytest.raises(RuntimeError, match="did not fetch the stand-in release"):
        hosted.run_hosted_update(python="p", app=tmp_path, manifest=b"{}", door_base="http://127.0.0.1:1/x",
                                 root=tmp_path, evidence=tmp_path, timeout=5, intercept=FakeIntercept)


def test_run_reverts_the_intercept_when_the_door_refuses(tmp_path, monkeypatch):
    make_app(tmp_path, monkeypatch)
    FakeIntercept.instances.clear()

    def refuse(base):
        raise RuntimeError("The door did not accept the Update request (HTTP 403)")

    monkeypatch.setattr(hosted, "post_update", refuse)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        hosted.run_hosted_update(python="p", app=tmp_path, manifest=b"{}", door_base="http://127.0.0.1:1/x",
                                 root=tmp_path, evidence=tmp_path, timeout=5, intercept=FakeIntercept)
    assert FakeIntercept.instances[0].reverted["clean"] is True


def _program(tmp_path, with_journal=True):
    root = tmp_path / "program"
    root.mkdir()
    log = root / "update.log"
    log.write_text("worker output\n")
    record = {"operation_id": OPERATION, "log_path": str(log), "state": "failed"}
    (root / "install.json").write_text(json.dumps({"update_job": record}))
    if with_journal:
        (root / "update-transaction.json").write_text('{"step": "switched"}')
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    return root, evidence


def test_a_failed_update_keeps_the_log_record_journal_and_unit_journal(tmp_path, monkeypatch):
    root, evidence = _program(tmp_path)
    seen = []
    monkeypatch.setattr(hosted.sys, "platform", "linux")
    monkeypatch.setattr(hosted.subprocess, "run", lambda argv, **kw: seen.append(argv) or hosted.subprocess.CompletedProcess(
        argv, 0, stdout="unit output\n", stderr=""))
    hosted.capture_update_evidence(root, evidence, failed=True)
    assert (evidence / "update-bootstrap.log").read_text() == "worker output\n"
    assert json.loads((evidence / "update-job-record.json").read_text())["operation_id"] == OPERATION
    assert (evidence / "update-transaction.json").read_text() == '{"step": "switched"}'
    assert (evidence / "update-job-journal.log").read_text() == "unit output\n"
    assert seen == [["journalctl", "--user", "-u", f"astrid-update-{OPERATION}.service", "--no-pager", "-o", "short-iso"]]


def test_a_successful_update_keeps_only_the_log_and_missing_pieces_never_raise(tmp_path, monkeypatch):
    root, evidence = _program(tmp_path, with_journal=False)
    hosted.capture_update_evidence(root, evidence, failed=False)
    assert [p.name for p in evidence.iterdir()] == ["update-bootstrap.log"]
    monkeypatch.setattr(hosted.sys, "platform", "win32")
    hosted.capture_update_evidence(root, evidence, failed=True)  # no journal file, no journalctl
    assert not (evidence / "update-transaction.json").exists() or (evidence / "update-transaction.json").read_bytes() == b""
    hosted.capture_update_evidence(tmp_path / "nothing", evidence, failed=True)


def test_run_captures_the_failure_evidence_when_the_row_fails(tmp_path, monkeypatch):
    make_app(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hosted, "capture_update_evidence", lambda root, evidence, *, failed: calls.append(failed))
    monkeypatch.setattr(hosted, "post_update", lambda base: (_ for _ in ()).throw(RuntimeError("HTTP 409")))
    with pytest.raises(RuntimeError):
        hosted.run_hosted_update(python="p", app=tmp_path, manifest=b"{}", door_base="http://127.0.0.1:1/x",
                                 root=tmp_path, evidence=tmp_path, timeout=5, intercept=FakeIntercept)
    assert calls == [True]


def test_the_new_evidence_names_are_allowlisted_everywhere():
    from scripts import ci_journey
    names = {"update-job-journal.log", "update-job-record.json", "update-transaction.json"}
    assert names <= ci_journey.JOURNEY_EVIDENCE_NAMES
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/journey.yml").read_text()
    assert all(f"'{name}'" in workflow for name in names)


def test_wait_terminal_observes_about_every_minute_and_once_more_on_timeout(door):
    server = door((200, {"state": "active", "operation_id": OPERATION, "job_name": "astrid-update-" + OPERATION}))
    now, seen = [0.0], []
    with pytest.raises(RuntimeError, match="did not finish"):
        hosted.wait_terminal(server.url, OPERATION, timeout=200, interval=10,
                             sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), clock=lambda: now[0],
                             tick=lambda body, final=False: seen.append(body is not None))
    # One observation at the start and about every 60 seconds after that, all with the status the door gave.
    assert 3 <= len(seen) <= 5
    assert all(seen)


# -- the button row: the hosted App build's own Update now, clicked in a browser ------------------

def make_dist(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Astrid</title>")
    (dist / "build.json").write_text('{"app": "abc"}')
    (dist / "assets" / "index-1.js").write_text("export {};")
    (dist / "install-manifest.json").write_text('{"release": 9}')
    return dist


def test_page_routes_serve_the_build_its_entry_and_module_types(tmp_path):
    dist = make_dist(tmp_path)
    routes, types = hosted.page_routes(dist)
    assert routes["/"] == routes[hosted.PAGE_ENTRY_ROUTE] == dist / "index.html"
    assert routes["/assets/index-1.js"] == dist / "assets" / "index-1.js"
    # A module script is refused by the browser without a JavaScript type.
    assert types["/assets/index-1.js"].startswith("text/javascript")
    assert types[hosted.PAGE_ENTRY_ROUTE].startswith("text/html")
    (dist / "build.json").unlink()
    with pytest.raises(RuntimeError, match="not a hosted App build"):
        hosted.page_routes(dist)


def test_the_stand_in_manifest_replaces_the_builds_own(tmp_path, monkeypatch):
    make_app(tmp_path, monkeypatch)
    dist = make_dist(tmp_path)
    seen = {}

    class Capture(FakeIntercept):
        def __init__(self, routes, *, python=None, types=None):
            super().__init__(routes, python=python)
            seen.update(routes=routes, types=types)

        def __enter__(self):
            raise RuntimeError("stop here")

    with pytest.raises(RuntimeError, match="stop here"):
        hosted.run_hosted_update(python="p", app=tmp_path, manifest=b'{"release": 2}', door_base="http://127.0.0.1:1/x",
                                 root=tmp_path, evidence=tmp_path, timeout=5, intercept=Capture, dist=dist)
    assert seen["routes"]["/install-manifest.json"] == b'{"release": 2}'
    assert seen["routes"]["/install"] == b"#!/bin/sh\n" and hosted.PAGE_ENTRY_ROUTE in seen["routes"]


def _get(port, ca, path, agent="Python-urllib"):
    import ssl
    context = ssl.create_default_context(cafile=str(ca))
    # Present the certificate's own name so the throwaway CA verifies it.
    connection = hosted.http.client.HTTPSConnection("astrid.haus", port, context=context, timeout=10)
    connection._create_connection = lambda address, *a, **k: __import__("socket").create_connection(
        ("127.0.0.1", port), *a, **k)
    connection.request("GET", path, headers={"User-Agent": agent})
    response = connection.getresponse()
    return response.status, response.getheader("Content-Type"), response.read()


def test_serve_answers_the_entry_with_its_query_sets_types_and_tells_the_page_apart(tmp_path):
    import socket
    dist = make_dist(tmp_path)
    ca, leaf, key = hosted.make_certs(tmp_path)
    routes, types = hosted.page_routes(dist)
    (tmp_path / "manifest").write_bytes(b'{"release": 2}')
    routes["/install-manifest.json"] = tmp_path / "manifest"
    (tmp_path / "routes.json").write_text(json.dumps({k: str(v) for k, v in routes.items()}))
    (tmp_path / "types.json").write_text(json.dumps(types))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    args = hosted.argparse.Namespace(cert=str(leaf), key=str(key), routes=str(tmp_path / "routes.json"),
                                     types=str(tmp_path / "types.json"), log=str(tmp_path / "hits.jsonl"),
                                     ready=str(tmp_path / "ready"), stop=str(tmp_path / "stop"), port=port, max_seconds=30)
    thread = threading.Thread(target=hosted.serve, args=(args,), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if (tmp_path / "ready").exists():
                break
            __import__("time").sleep(0.05)
        browser = "Mozilla/5.0 HeadlessChrome " + hosted.PAGE_AGENT_MARK
        status, kind, body = _get(port, ca, hosted.PAGE_ENTRY, browser)
        assert (status, body) == (200, b"<!doctype html><title>Astrid</title>") and kind.startswith("text/html")
        assert _get(port, ca, "/assets/index-1.js", browser)[1].startswith("text/javascript")
        assert _get(port, ca, "/install-manifest.json?x=1", browser)[2] == b'{"release": 2}'
        status, kind, body = _get(port, ca, "/install-manifest.json")
        assert (status, kind, body) == (200, "application/json", b'{"release": 2}')
        assert _get(port, ca, "/missing")[0] == 404
    finally:
        (tmp_path / "stop").write_text("stop")
        thread.join(10)
    hits = [json.loads(line) for line in (tmp_path / "hits.jsonl").read_text().splitlines()]
    assert [(hit["path"], hit.get("page", False)) for hit in hits] == [
        (hosted.PAGE_ENTRY_ROUTE, True), ("/assets/index-1.js", True), ("/install-manifest.json", True),
        ("/install-manifest.json", False), ("/missing", False)]


def clicked(status=202, posts=1, headers=("accept", "content-length", "host", "origin", "user-agent"), **body):
    reply = {"accepted": True, "state": "accepted", "operation_id": OPERATION, "message": "Update accepted.", **body}
    return {"stage": "clicked", "posts": posts, "page_progress": True,
            "request": {"method": "POST", "path": hosted.UPDATE_PATH, "url": "http://127.0.0.1:17340" + hosted.UPDATE_PATH,
                        "origin": hosted.ORIGIN, "content_length": 0, "headers": list(headers),
                        "authorization": "authorization" in headers, "cookie": "cookie" in headers, "status": status},
            "response": {"status": status, "body": reply}}


def test_the_button_row_clicks_the_page_and_records_what_the_browser_sent(tmp_path, monkeypatch):
    make_app(tmp_path, monkeypatch)
    dist = make_dist(tmp_path)
    FakeIntercept.instances.clear()
    order = []

    class Pinned(FakeIntercept):
        def __init__(self, routes, *, python=None, types=None):
            super().__init__(routes, python=python)
            self.types, self.leaf_spki = types, "PIN="

    def click(*, app, door_base, spki, evidence, live_timeout=0):
        order.append(("click", spki, door_base))
        Pinned.instances[0].log += [
            {"method": "GET", "path": "/install-manifest.json", "status": 200, "page": True},
            {"method": "GET", "path": "/install-manifest.json", "status": 200},
            {"method": "GET", "path": "/install", "status": 200}]
        return clicked()

    monkeypatch.setattr(hosted, "post_update", lambda base: pytest.fail("the button row must not send the request itself"))
    monkeypatch.setattr(hosted, "wait_terminal", lambda base, op, timeout: order.append(("wait", op)) or terminal())
    record = hosted.run_hosted_update(python="p", app=tmp_path, manifest=b'{"release": 2}', door_base="http://127.0.0.1:1/x",
                                      root=tmp_path, evidence=tmp_path, timeout=5, intercept=Pinned, dist=dist, click=click)
    assert order == [("click", "PIN=", "http://127.0.0.1:1/x"), ("wait", OPERATION)]
    assert record["driver"] == "app-button" and record["route"] == "hosted-button"
    assert record["request"] == {"method": "POST", "path": hosted.UPDATE_PATH, "origin": hosted.ORIGIN,
                                 "content_length": 0, "headers": ["accept", "content-length", "host", "origin", "user-agent"],
                                 "authorization": False, "cookie": False, "status": 202, "operation_id": OPERATION}
    # The page's own read of the stand-in is not counted as Runtime fetching the release.
    assert record["served"] == {"/install-manifest.json": 1, "/install": 1}
    assert record["page"]["served_manifest"] == 1 and record["page"]["clicked"] is True
    assert record["page"]["build_sha256"] == hashlib.sha256((dist / "build.json").read_bytes()).hexdigest()
    assert Pinned.instances[0].types[hosted.PAGE_ENTRY_ROUTE].startswith("text/html")


def test_the_button_row_fails_when_only_the_page_fetched_the_stand_in(tmp_path, monkeypatch):
    make_app(tmp_path, monkeypatch)
    FakeIntercept.instances.clear()

    class Pinned(FakeIntercept):
        def __init__(self, routes, *, python=None, types=None):
            super().__init__(routes, python=python)
            self.leaf_spki = "PIN="

    def click(**_):
        Pinned.instances[0].log += [{"method": "GET", "path": "/install-manifest.json", "status": 200, "page": True}]
        return clicked()

    monkeypatch.setattr(hosted, "wait_terminal", lambda *a, **k: terminal())
    with pytest.raises(RuntimeError, match="did not fetch the stand-in release"):
        hosted.run_hosted_update(python="p", app=tmp_path, manifest=b"{}", door_base="http://127.0.0.1:1/x", root=tmp_path,
                                 evidence=tmp_path, timeout=5, intercept=Pinned, dist=make_dist(tmp_path), click=click)


@pytest.mark.parametrize("result, message", [
    (clicked(status=409, accepted=False, state="refused"), "did not accept"),
    (clicked(operation_id="short"), "no operation id"),
    (clicked(posts=2), "2 Update requests"),
    ({"stage": "offer"}, "did not accept"),
])
def test_a_click_the_door_did_not_accept_once_fails(result, message):
    with pytest.raises(RuntimeError, match=message):
        hosted.accepted_click(result)


def fake_node(tmp_path, result, code=0):
    """A stand-in for node that records its environment and config and writes ``result`` where the config says."""
    (tmp_path / "result.json").write_text(json.dumps(result))
    helper = tmp_path / "fake_node.py"
    helper.write_text("import json, shutil, sys\n"
                      "config = json.load(open(sys.argv[1]))\n"
                      f"shutil.copyfile({str(tmp_path / 'result.json')!r}, config['result'])\n"
                      f"json.dump(config, open({str(tmp_path / 'config.json')!r}, 'w'))\n")
    script = tmp_path / "node"
    script.write_text(f"#!/bin/sh\nenv > {tmp_path}/env.txt\npython3 {helper} \"$2\"\nexit {code}\n")
    script.chmod(0o755)
    return str(script)


def test_click_runs_the_browser_without_a_proxy_and_keeps_its_result(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:1")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:1")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    result = hosted.click_update_now(app=tmp_path, door_base="http://127.0.0.1:17340/api/runtime/v1/health", spki="PIN=",
                                     evidence=evidence, timeout=5, node=fake_node(tmp_path, clicked()))
    assert result["posts"] == 1
    names = {line.split("=", 1)[0].lower() for line in (tmp_path / "env.txt").read_text().splitlines() if "=" in line}
    assert not names & {"http_proxy", "https_proxy", "all_proxy"}
    config = json.loads((tmp_path / "config.json").read_text())
    assert (config["origin"], config["entry"], config["spki"], config["door"]) == (
        "https://astrid.haus", "/tools/video-editor?settings=updates", "PIN=", "http://127.0.0.1:17340")
    assert json.loads((evidence / "update-button.json").read_text())["posts"] == 1


def test_click_reports_the_stage_the_browser_stopped_at(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    with pytest.raises(RuntimeError, match="stage 'offer'.*stayed disabled"):
        hosted.click_update_now(app=tmp_path, door_base="http://127.0.0.1:17340/x", spki="PIN=", evidence=evidence, timeout=5,
                                node=fake_node(tmp_path, {"stage": "offer", "error": "Error: Update now stayed disabled"}, 1))
    assert json.loads((evidence / "update-button.json").read_text())["stage"] == "offer"


def test_spki_pin_is_the_base64_sha256_of_the_leaf_public_key(tmp_path):
    import base64
    _, leaf, _ = hosted.make_certs(tmp_path)
    der = subprocess.run(["openssl", "x509", "-in", str(leaf), "-pubkey", "-noout"], capture_output=True, check=True).stdout
    der = subprocess.run(["openssl", "pkey", "-pubin", "-outform", "der"], input=der, capture_output=True, check=True).stdout
    assert hosted.spki_pin(leaf) == base64.b64encode(hashlib.sha256(der).digest()).decode()


def test_the_button_evidence_names_are_allowlisted_everywhere():
    from scripts import ci_journey
    names = {"update-button.json", "update-button.png"}
    assert names <= ci_journey.JOURNEY_EVIDENCE_NAMES
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/journey.yml").read_text()
    assert all(f"'{name}'" in workflow for name in names)


def test_only_the_fresh_linux_row_builds_the_hosted_app_and_hands_it_to_the_driver():
    import yaml
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/journey.yml").read_text())
    steps = workflow["jobs"]["journey"]["steps"]
    build = next(step for step in steps if step.get("name", "").startswith("Build the candidate's hosted App"))
    assert build["if"] == "matrix.job_id == 'direct-linux-fresh'" and build["working-directory"] == "app"
    assert "npm run build:hosted" in build["run"] and "playwright install --with-deps chromium" in build["run"]
    driver = next(step for step in steps if step.get("name") == "Run the matrix-owned lifecycle driver")
    assert driver["env"]["ASTRID_CI_HOSTED_APP_DIST"] == (
        "${{ matrix.job_id == 'direct-linux-fresh' && format('{0}/app/dist', github.workspace) || '' }}")
    assert steps.index(build) < steps.index(driver)


def test_the_browser_script_pins_the_leaf_and_reads_the_request_it_saw():
    script = hosted.BUTTON_SCRIPT.as_uri()
    probe = (f"import({json.dumps(script)}).then((m) => console.log(JSON.stringify({{"
             "args: m.chromiumArgs({ origin: 'https://astrid.haus', spki: 'PIN=' }),"
             "seen: m.describeRequest({ method: 'POST', url: 'http://127.0.0.1:17340/api/runtime/v1/update?x=1',"
             "  headers: { Origin: 'https://astrid.haus', 'Content-Length': '0', Accept: 'application/json' }, status: 202 }),"
             "bare: m.describeRequest({ method: 'POST', url: 'http://127.0.0.1:17340/u', headers: { cookie: 'a' }, status: 403 }),"
             "label: m.UPDATE_NOW })))")
    out = json.loads(subprocess.run(["node", "--input-type=module", "-e", probe], capture_output=True, text=True,
                                    check=True).stdout)
    assert "--ignore-certificate-errors-spki-list=PIN=" in out["args"]
    assert "--ip-address-space-overrides=127.0.0.1:443=public" in out["args"]
    assert any(arg.startswith("--host-resolver-rules=MAP astrid.haus 127.0.0.1") for arg in out["args"])
    assert out["seen"] == {"method": "POST", "path": "/api/runtime/v1/update",
                           "url": "http://127.0.0.1:17340/api/runtime/v1/update?x=1", "origin": "https://astrid.haus",
                           "content_length": 0, "headers": ["accept", "content-length", "origin"],
                           "authorization": False, "cookie": False, "status": 202}
    assert out["bare"]["content_length"] is None and out["bare"]["cookie"] is True and out["bare"]["origin"] is None
    assert out["label"] == "Update now"


class FakeLive:
    """The browser left open on the page after the click (UR-15k)."""

    def __init__(self, final):
        self.final, self.closed, self.waited = final, False, False

    def wait(self):
        self.waited = True
        return self.final() if callable(self.final) else self.final

    def close(self):
        self.closed = True


def _live_row(tmp_path, monkeypatch, live, *, wait=None):
    make_app(tmp_path, monkeypatch)
    FakeIntercept.instances.clear()

    class Pinned(FakeIntercept):
        def __init__(self, routes, *, python=None, types=None):
            super().__init__(routes, python=python)
            self.types, self.leaf_spki = types, "PIN="

    def click(*, app, door_base, spki, evidence, live_timeout=0):
        assert live_timeout == 5  # the browser waits as long as the update may take
        Pinned.instances[0].log += [{"method": "GET", "path": "/install-manifest.json", "status": 200},
                                    {"method": "GET", "path": "/install", "status": 200}]
        return {**clicked(), "live": live}

    monkeypatch.setattr(hosted, "wait_terminal", wait or (lambda *a, **k: terminal()))
    return lambda: hosted.run_hosted_update(python="p", app=tmp_path, manifest=b'{"release": 2}',
                                            door_base="http://127.0.0.1:1/x", root=tmp_path, evidence=tmp_path,
                                            timeout=5, intercept=Pinned, dist=make_dist(tmp_path), click=click)


def test_the_open_page_shows_the_finished_update_within_30_seconds_without_a_reload(tmp_path, monkeypatch):
    live = FakeLive(lambda: {"live": {"text": "Astrid updated: Release 1 → Release 2", "seen_at": time.time() * 1000 + 12_000,
                                      "navigations": 1}})
    record = _live_row(tmp_path, monkeypatch, live)()
    assert live.waited and live.closed is False
    result = record["page"]["live_result"]
    assert result["text"] == "Astrid updated: Release 1 → Release 2" and result["journey_reloads"] == 0
    assert 0 < result["seconds_after_terminal"] <= hosted.LIVE_RESULT_SECONDS


@pytest.mark.parametrize("final, message", [
    (lambda: {"live": {"text": "Astrid updated: Release 1 → Release 2", "seen_at": time.time() * 1000 + 45_000}}, "45s after"),
    (lambda: {"live_error": "TimeoutError: waiting for Astrid updated"}, "never showed"),
])
def test_the_row_fails_when_the_open_page_is_late_or_silent(tmp_path, monkeypatch, final, message):
    with pytest.raises(RuntimeError, match=message):
        _live_row(tmp_path, monkeypatch, FakeLive(final))()


def test_the_open_browser_is_closed_when_the_update_fails(tmp_path, monkeypatch):
    live = FakeLive({})

    def failed(*_a, **_k):
        raise RuntimeError("The Update ended as 'failed'")

    with pytest.raises(RuntimeError, match="ended as 'failed'"):
        _live_row(tmp_path, monkeypatch, live, wait=failed)()
    assert live.closed and not live.waited


def test_click_keeps_the_browser_open_for_the_live_result(tmp_path):
    """The real click path: the browser writes the click at once, then its final result with the live line."""
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    final = {**clicked(), "stage": "live", "live": {"text": "Astrid updated: Release 1 → Release 2", "seen_at": 1, "navigations": 0}}
    helper = tmp_path / "fake_live_node.py"
    helper.write_text("import json, sys, time\n"
                      "config = json.load(open(sys.argv[1]))\n"
                      f"json.dump({clicked()!r}, open(config['clicked'], 'w'))\n"
                      "time.sleep(0.5)\n"
                      f"json.dump({final!r}, open(config['result'], 'w'))\n")
    script = tmp_path / "node"
    script.write_text(f"#!/bin/sh\npython3 {helper} \"$2\"\n")
    script.chmod(0o755)
    result = hosted.click_update_now(app=tmp_path, door_base="http://127.0.0.1:17340/x", spki="PIN=", evidence=evidence,
                                     timeout=5, node=str(script), live_timeout=5)
    assert result["posts"] == 1 and isinstance(result["live"], hosted.LiveResult)
    assert result["live"].wait()["live"]["text"] == "Astrid updated: Release 1 → Release 2"
    assert json.loads((evidence / "update-button.json").read_text())["stage"] == "live"
