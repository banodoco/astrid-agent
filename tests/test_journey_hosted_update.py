"""The journey's hosted Update request and the runner-level astrid.haus intercept."""
import http.server
import json
import threading

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
