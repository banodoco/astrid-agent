from __future__ import annotations

import json
import socketserver
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

from astrid.core.cli.domain_output import DomainResult
from astrid.packs.vibecomfy import local


@contextmanager
def _workflow_server(
    body: bytes = b'{"nodes": []}', *, redirect: bool = False, size: int | None = None
):
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            if redirect:
                self.send_response(302)
                self.send_header("Location", "/followed")
                self.end_headers()
                return
            length = len(body) if size is None else size
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            if size is None:
                self.wfile.write(body)
            else:
                chunk = b"x" * (1024 * 1024)
                while length:
                    part = chunk[: min(len(chunk), length)]
                    self.wfile.write(part)
                    length -= len(part)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@contextmanager
def _malformed_workflow_server():
    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.recv(4096)
            self.request.sendall(b"not an HTTP status line\r\n\r\n")

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _fake_media_import(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[object, ...]]
) -> list[dict[str, object]]:
    opened: list[dict[str, object]] = []

    class Media:
        def import_file(self, *, project, path, realm, idempotency_key):
            calls.append(
                (project, Path(path).name, Path(path).read_bytes(), realm, idempotency_key)
            )
            return DomainResult.success({"media_id": "M1"})

    class ClientContext:
        def __init__(self, **kwargs):
            opened.append(kwargs)

        def __enter__(self):
            return SimpleNamespace(media=Media())

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(local, "AstridClient", SimpleNamespace(open_from_launcher=ClientContext))
    return opened


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8188",
        "https://127.0.0.1:8188",
        "http://127.0.0.1:8188/workflows",
        "http://127.0.0.1:8188?dir=workflows",
        "http://127.0.0.1:8188#workflow",
        "http://user@127.0.0.1:8188",
        "http://[::1",
    ],
)
def test_import_workflow_refuses_non_loopback_or_non_root_urls(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_request(*_args, **_kwargs):
        raise AssertionError("invalid ComfyUI URL was used")

    monkeypatch.setattr(urllib.request, "build_opener", unexpected_request)
    with pytest.raises(ValueError):
        local.import_workflow("portrait.json", "demo", url)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "portrait.txt",
        "../portrait.json",
        ".hidden.json",
        "folder\\portrait.json",
        "bad\x00.json",
    ],
)
def test_import_workflow_refuses_bad_names(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []
    _fake_media_import(monkeypatch, calls)
    with _workflow_server() as (url, hits):
        with pytest.raises(ValueError):
            local.import_workflow(name, "demo", url)
    assert hits == []
    assert calls == []


def test_import_workflow_refuses_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []
    _fake_media_import(monkeypatch, calls)
    with _workflow_server(redirect=True) as (url, hits):
        with pytest.raises(ValueError):
            local.import_workflow("portrait.json", "demo", url)
    assert hits == ["/userdata/workflows%2Fportrait.json"]
    assert calls == []


def test_import_workflow_enforces_64_mib_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []
    _fake_media_import(monkeypatch, calls)
    with _workflow_server(size=64 * 1024 * 1024 + 1) as (url, hits):
        with pytest.raises(ValueError):
            local.import_workflow("portrait.json", "demo", url)
    assert hits == ["/userdata/workflows%2Fportrait.json"]
    assert calls == []


def test_import_workflow_rejects_non_json(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []
    _fake_media_import(monkeypatch, calls)
    with _workflow_server(body=b"not JSON") as (url, hits):
        with pytest.raises(ValueError):
            local.import_workflow("portrait.json", "demo", url)
    assert hits == ["/userdata/workflows%2Fportrait.json"]
    assert calls == []


def test_import_workflow_disables_http_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []
    _fake_media_import(monkeypatch, calls)
    with (
        _workflow_server() as (target_url, target_hits),
        _workflow_server() as (proxy_url, proxy_hits),
    ):
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": proxy_url})
        monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
        local.import_workflow("portrait.json", "demo", target_url)
    assert target_hits == ["/userdata/workflows%2Fportrait.json"]
    assert proxy_hits == []
    assert calls[0][0:3] == ("demo", "portrait.json", b'{"nodes": []}')


def test_import_workflow_uses_media_import_and_prints_its_json_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[object, ...]] = []
    opened = _fake_media_import(monkeypatch, calls)
    with _workflow_server() as (url, hits):
        assert (
            local.main(
                [
                    "import-workflow",
                    "portrait.json",
                    "--project",
                    "demo",
                    "--comfy-url",
                    url,
                ]
            )
            == 0
        )

    assert hits == ["/userdata/workflows%2Fportrait.json"]
    assert calls == [("demo", "portrait.json", b'{"nodes": []}', "managed_local", None)]
    assert opened == [{"start_pack_host": False}]
    assert json.loads(capsys.readouterr().out) == DomainResult.success({"media_id": "M1"}).as_dict()


def test_import_workflow_rejects_malformed_http_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []
    _fake_media_import(monkeypatch, calls)
    with _malformed_workflow_server() as url:
        with pytest.raises(ValueError):
            local.import_workflow("portrait.json", "demo", url)
    assert calls == []
