"""Loopback proxy failures and cross-manager refusal preserve selected state."""

import hashlib
import socketserver
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

import pytest

from astrid.installer import cli, core, tools
from astrid.installer.backend import Backend
from astrid.installer.core import Version, write_json
from astrid.installer.service import LABEL, install_selection
from runtime_protocol import platform


@pytest.mark.parametrize("failure", ["truncated", "proxy_auth"])
def test_proxy_download_failure_refuses_update_before_stop(tmp_path, monkeypatch, failure):
    # A loopback CONNECT proxy emits a truncated HTTPS response or a 407.
    # The certificate is generated only inside pytest's temporary directory.
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    requests = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                request += chunk
            requests.append(request.split(b"\r\n", 1)[0])
            if failure == "proxy_auth":
                self.request.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                return
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            with tls.wrap_socket(self.request, server_side=True) as stream:
                incoming = b""
                while b"\r\n\r\n" not in incoming:
                    chunk = stream.recv(4096)
                    if not chunk:
                        return
                    incoming += chunk
                stream.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\npartial")

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    root, data = tmp_path / "program", tmp_path / "workspace"
    old_root = root / "versions" / "old"
    old_root.mkdir(parents=True)
    python = old_root / "python"
    python.touch()
    old = Version(old_root, python, old_root / "profile.json", {}, release=1)
    write_json(old_root / "version.json", old.document())
    core.switch(root, old)
    data.mkdir()
    (data / "user-work").write_bytes(b"preserve me")
    backend = Backend(root, tmp_path / "log", tmp_path / "uv")
    monkeypatch.setattr(backend, "workspace", lambda *_: {"realm_id": "old", "realm_root": str(data)})
    monkeypatch.setattr(backend, "has_workspace", lambda *_: True)
    for name in ("preflight_stop", "stop", "backup", "upgrade", "register"):
        monkeypatch.setattr(backend, name, Mock(side_effect=AssertionError(f"{name} called")))
    selection = root / "selection.json"
    write_json(selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data)})
    backend.service.selection = selection
    before = selection.read_bytes()

    with Server(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            proxy = f"http://127.0.0.1:{server.server_address[1]}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy}),
                                                 urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(cert))))
            monkeypatch.setattr(urllib.request, "proxy_bypass", lambda *_: False)
            monkeypatch.setattr(urllib.request, "_opener", opener)
            def stage(*_):
                tools.download("https://localhost:443/candidate", tmp_path / "candidate.zip",
                               sha256=hashlib.sha256(b"complete archive").hexdigest())
                pytest.fail("bad download accepted")
            monkeypatch.setattr(backend, "stage", stage)
            with pytest.raises((ValueError, urllib.error.URLError, OSError)) as caught:
                core.Installer(root, data, backend, say=lambda _: None).run("update", {"release": 2})
            assert ("checksum mismatch" if failure == "truncated" else "407") in str(caught.value)
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert len(requests) == 1 and requests[0].startswith(b"CONNECT localhost:443 HTTP/1.")
    assert core.current(root) == old
    assert (data / "user-work").read_bytes() == b"preserve me"
    assert selection.read_bytes() == before
    assert not (root / "update-transaction.json").exists()
    for name in ("preflight_stop", "stop", "backup", "upgrade", "register"):
        getattr(backend, name).assert_not_called()


@pytest.mark.parametrize("owner", ["pinokio", "installer"])
def test_cross_manager_claim_refuses_and_preserves_records(tmp_path, monkeypatch, capsys, owner):
    home, root, data = tmp_path / "home", tmp_path / "selected", tmp_path / "workspace"
    for key, path in {"HOME": home, "USERPROFILE": home, "LOCALAPPDATA": home / "local",
                      "XDG_DATA_HOME": home / "data", "XDG_CONFIG_HOME": home / "config"}.items():
        monkeypatch.setenv(key, str(path))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    marker, selection = root / "install.json", install_selection(home, sys.platform)
    record = {"owner": LABEL, "program_root": str(root), "data_root": str(data)}
    if owner == "pinokio":
        record["managed_by"] = "pinokio"
    write_json(marker, record)
    write_json(selection, record)
    data.mkdir()
    (data / "user-work").write_bytes(b"original")
    before = marker.read_bytes(), selection.read_bytes()
    monkeypatch.setattr(core.Installer, "run", Mock(side_effect=AssertionError("lifecycle entered")))
    argv = [] if owner == "pinokio" else ["--managed-by", "pinokio", "--root", str(tmp_path / "other")]
    assert cli.main(argv) == 1
    output = capsys.readouterr().out
    assert ("installed with Pinokio" if owner == "pinokio" else "Uninstall that copy first") in output
    assert (marker.read_bytes(), selection.read_bytes()) == before
    assert (data / "user-work").read_bytes() == b"original"
    core.Installer.run.assert_not_called()
