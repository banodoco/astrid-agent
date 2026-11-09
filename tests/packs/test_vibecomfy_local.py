from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from astrid.packs.vibecomfy.actions.run.run import _stage_managed_asset_manifest
from astrid.packs.vibecomfy.asset_manifest import read_archive
from astrid.packs.vibecomfy import local as local_scan
from astrid.packs.vibecomfy.local import pack_images, scan


class _ComfyHandler(BaseHTTPRequestHandler):
    base_dir: Path
    requested: list[str]

    def do_GET(self) -> None:
        self.requested.append(self.path)
        parsed = urlsplit(self.path)
        if parsed.path == "/system_stats":
            payload = {
                "system": {
                    "comfyui_version": "0.3.32",
                    "python_version": "3.11.8",
                    "embedded_python": False,
                    "argv": ["main.py", "--output-directory", str(self.base_dir / "renders")],
                },
                "devices": [],
            }
        elif parsed.path == "/internal/folder_paths":
            payload = {"custom_nodes": [str(self.base_dir / "custom_nodes")]}
        elif parsed.path == "/userdata":
            assert parse_qs(parsed.query) == {"dir": ["workflows"]}
            payload = ["portrait_flux.json", "nested/wan_i2v.json"]
        elif parsed.path == "/object_info":
            payload = {"KSampler": {"input": {"required": {}}}}
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _PinokioHandler(BaseHTTPRequestHandler):
    payload: dict[str, object]
    started: threading.Event | None = None
    release: threading.Event | None = None
    stream_until_release = False
    default_requests_seen = 0

    def do_GET(self) -> None:
        assert self.path == "/pinokio/info"
        type(self).default_requests_seen = len(_ComfyHandler.requested)
        if self.started is not None:
            self.started.set()
        if self.stream_until_release and self.release is not None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "10000")
            self.end_headers()
            self.wfile.write(b'{"home":')
            self.wfile.flush()
            while not self.release.wait(timeout=0.05):
                self.wfile.write(b" ")
                self.wfile.flush()
            return
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _NonComfyHandler(BaseHTTPRequestHandler):
    requested: list[str] = []

    def do_GET(self) -> None:
        self.requested.append(self.path)
        body = json.dumps({"status": "another local app"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _server(tmp_path: Path) -> tuple[ThreadingHTTPServer, threading.Thread]:
    custom_nodes = tmp_path / "custom_nodes"
    (custom_nodes / "ComfyUI-Manager").mkdir(parents=True)
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").touch()
    _ComfyHandler.base_dir = tmp_path
    _ComfyHandler.requested = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ComfyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _pinokio_server(
    payload: dict[str, object],
    *,
    started: threading.Event | None = None,
    release: threading.Event | None = None,
    stream_until_release: bool = False,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    _PinokioHandler.payload = payload
    _PinokioHandler.started = started
    _PinokioHandler.release = release
    _PinokioHandler.stream_until_release = stream_until_release
    _PinokioHandler.default_requests_seen = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PinokioHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _non_comfy_server() -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NonComfyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture(autouse=True)
def _default_to_absent_pinokio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_scan, "_PINOKIO_INFO_URL", "http://127.0.0.1:1/pinokio/info")


def test_scan_reports_local_comfyui_and_endpoint_data(tmp_path: Path) -> None:
    server, thread = _server(tmp_path)
    try:
        result = scan([server.server_address[1]])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result["errors"] == []
    assert len(result["servers"]) == 1
    found = result["servers"][0]
    assert found == {
        "url": f"http://127.0.0.1:{server.server_address[1]}",
        "comfyui_version": "0.3.32",
        "python_version": "3.11.8",
        "embedded_python": False,
        "argv": ["main.py", "--output-directory", str(tmp_path / "renders")],
        "folder_paths": {"custom_nodes": [str(tmp_path / "custom_nodes")]},
        "base_dir": str(tmp_path),
        "output_dir": str(tmp_path / "renders"),
        "workflows": ["portrait_flux.json", "nested/wan_i2v.json"],
        "manager_present": True,
        "python": str(tmp_path / "venv" / "bin" / "python"),
    }
    assert _ComfyHandler.requested == [
        "/system_stats",
        "/internal/folder_paths",
        "/userdata?dir=workflows",
    ]


def test_scan_returns_errors_for_unavailable_ports() -> None:
    result = scan([1])
    assert result["servers"] == []
    assert result["errors"]
    assert result["errors"][0]["port"] == 1
    assert result["errors"][0]["endpoint"] == "/system_stats"


def test_scan_discovers_pinokio_running_comfyui_and_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comfy, comfy_thread = _server(tmp_path / "app")
    pinokio_home = tmp_path / "pinokio"
    pinokio, pinokio_thread = _pinokio_server({
        "home": str(pinokio_home),
        "apps": ["comfy.git"],
        "scripts": [{
            "path": "comfy.git/start.js",
            "app": "comfy.git",
            "local": {"url": f"http://localhost:{comfy.server_address[1]}"},
        }],
    })
    env_python = tmp_path / "app" / "env" / "bin" / "python"
    env_python.parent.mkdir(parents=True)
    env_python.touch()
    monkeypatch.setattr(
        local_scan,
        "_PINOKIO_INFO_URL",
        f"http://127.0.0.1:{pinokio.server_address[1]}/pinokio/info",
    )
    try:
        result = scan([])
    finally:
        comfy.shutdown()
        comfy.server_close()
        comfy_thread.join(timeout=2)
        pinokio.shutdown()
        pinokio.server_close()
        pinokio_thread.join(timeout=2)

    assert result["pinokio"] == {"available": True, "home": str(pinokio_home)}
    assert len(result["servers"]) == 1
    found = result["servers"][0]
    assert found["url"] == f"http://127.0.0.1:{comfy.server_address[1]}"
    assert found["base_dir"] == str(tmp_path / "app")
    assert found["output_dir"] == str(tmp_path / "app" / "renders")
    assert found["python"] == str(env_python)
    assert found["pinokio_apps"] == ["comfy.git"]
    assert result["errors"] == []


def test_scan_continues_pinokio_discovery_after_incomplete_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken, broken_thread = _server(tmp_path / "broken")
    available, available_thread = _server(tmp_path / "available")
    pinokio_home = tmp_path / "pinokio"
    pinokio, pinokio_thread = _pinokio_server({
        "home": str(pinokio_home),
        "scripts": [
            {
                "app": "broken.git",
                "local": {"url": f"http://localhost:{broken.server_address[1]}"},
            },
            {
                "app": "available.git",
                "local": {"url": f"http://localhost:{available.server_address[1]}"},
            },
        ],
    })
    original_get_json = local_scan._get_json

    def get_json(url: str, timeout: float = 2) -> object:
        if url.endswith(f"{broken.server_address[1]}/system_stats"):
            raise http.client.IncompleteRead(b"", 1)
        return original_get_json(url, timeout=timeout)

    monkeypatch.setattr(local_scan, "_get_json", get_json)
    monkeypatch.setattr(
        local_scan,
        "_PINOKIO_INFO_URL",
        f"http://127.0.0.1:{pinokio.server_address[1]}/pinokio/info",
    )
    try:
        result = scan([])
    finally:
        broken.shutdown()
        broken.server_close()
        broken_thread.join(timeout=2)
        available.shutdown()
        available.server_close()
        available_thread.join(timeout=2)
        pinokio.shutdown()
        pinokio.server_close()
        pinokio_thread.join(timeout=2)

    assert result["pinokio"] == {"available": True, "home": str(pinokio_home)}
    assert [server["url"] for server in result["servers"]] == [
        f"http://127.0.0.1:{available.server_address[1]}"
    ]
    assert result["servers"][0]["pinokio_apps"] == ["available.git"]


def test_scan_reports_pinokio_when_no_script_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinokio_home = tmp_path / "pinokio"
    pinokio, thread = _pinokio_server({"home": str(pinokio_home), "scripts": []})
    monkeypatch.setattr(
        local_scan,
        "_PINOKIO_INFO_URL",
        f"http://127.0.0.1:{pinokio.server_address[1]}/pinokio/info",
    )
    try:
        result = scan([])
    finally:
        pinokio.shutdown()
        pinokio.server_close()
        thread.join(timeout=2)

    assert result == {
        "servers": [],
        "errors": [],
        "pinokio": {"available": True, "home": str(pinokio_home)},
    }


def test_scan_continues_when_pinokio_is_absent() -> None:
    result = scan([])

    assert result == {
        "servers": [],
        "errors": [],
        "pinokio": {"available": False, "home": None},
    }


def test_scan_probes_default_ports_before_a_stalled_pinokio_control_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comfy, comfy_thread = _server(tmp_path / "app")
    started = threading.Event()
    release = threading.Event()
    pinokio, pinokio_thread = _pinokio_server(
        {"home": str(tmp_path / "pinokio"), "scripts": []},
        started=started,
        release=release,
        stream_until_release=True,
    )
    monkeypatch.setattr(
        local_scan,
        "_PINOKIO_INFO_URL",
        f"http://127.0.0.1:{pinokio.server_address[1]}/pinokio/info",
    )
    outcome: dict[str, object] = {}
    done = threading.Event()

    def run_scan() -> None:
        outcome["result"] = scan([comfy.server_address[1]])
        done.set()

    scan_thread = threading.Thread(target=run_scan, daemon=True)
    scan_thread.start()
    try:
        assert started.wait()
        assert _PinokioHandler.default_requests_seen == 3
        assert done.wait()
        assert not release.is_set()
        result = outcome["result"]
        assert isinstance(result, dict)
        assert len(result["servers"]) == 1
    finally:
        release.set()
        scan_thread.join()
        comfy.shutdown()
        comfy.server_close()
        comfy_thread.join()
        pinokio.shutdown()
        pinokio.server_close()
        pinokio_thread.join()
        assert local_scan._PINOKIO_SCAN_LOCK.acquire()
        local_scan._PINOKIO_SCAN_LOCK.release()


def test_scan_caps_extra_pinokio_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _NonComfyHandler.requested = []
    unrelated = [_non_comfy_server() for _ in range(local_scan._PINOKIO_MAX_EXTRA_PORTS)]
    comfy, comfy_thread = _server(tmp_path / "comfy")
    scripts = [
        {
            "app": f"unrelated-{index}.git",
            "local": {
                "url": f"http://127.0.0.1:{server.server_address[1]}",
                "details": {"url": f"http://127.0.0.1:{comfy.server_address[1]}"}
                if index == 0 else {},
            },
        }
        for index, (server, _) in enumerate(unrelated)
    ]
    scripts.append({
        "app": "comfy.git",
        "local": {"url": f"http://127.0.0.1:{comfy.server_address[1]}"},
    })
    pinokio, pinokio_thread = _pinokio_server(
        {"home": str(tmp_path / "pinokio"), "scripts": scripts}
    )
    monkeypatch.setattr(
        local_scan,
        "_PINOKIO_INFO_URL",
        f"http://127.0.0.1:{pinokio.server_address[1]}/pinokio/info",
    )
    try:
        result = scan([])
    finally:
        for server, thread in unrelated:
            server.shutdown()
            server.server_close()
            thread.join()
        comfy.shutdown()
        comfy.server_close()
        comfy_thread.join()
        pinokio.shutdown()
        pinokio.server_close()
        pinokio_thread.join()

    assert len(_NonComfyHandler.requested) == local_scan._PINOKIO_MAX_EXTRA_PORTS
    assert _ComfyHandler.requested == []
    assert result["servers"] == []


def test_scan_omits_non_comfyui_pinokio_responder(monkeypatch: pytest.MonkeyPatch) -> None:
    _NonComfyHandler.requested = []
    other, other_thread = _non_comfy_server()
    pinokio, pinokio_thread = _pinokio_server({
        "home": "/fake/pinokio",
        "scripts": [{
            "app": "other.git",
            "local": {"url": f"http://localhost:{other.server_address[1]}"},
        }],
    })
    monkeypatch.setattr(
        local_scan,
        "_PINOKIO_INFO_URL",
        f"http://127.0.0.1:{pinokio.server_address[1]}/pinokio/info",
    )
    try:
        result = scan([])
    finally:
        other.shutdown()
        other.server_close()
        other_thread.join()
        pinokio.shutdown()
        pinokio.server_close()
        pinokio_thread.join()

    assert _NonComfyHandler.requested == ["/system_stats"]
    assert result["servers"] == []


def test_pack_images_round_trips_through_manifest_and_run_staging(tmp_path: Path) -> None:
    image = tmp_path / "portrait.png"
    image.write_bytes(b"fake image bytes")
    second_image = tmp_path / "reference.png"
    second_image.write_bytes(b"second image bytes")
    archive_path = tmp_path / "managed-assets.zip"

    result = pack_images(str(archive_path), [str(image), str(second_image)])

    resolved = read_archive(archive_path)
    assert result == {
        "zip": str(archive_path),
        "names": [record["member"].split("/")[-1] for record in resolved.manifest["assets"]],
    }
    assert resolved.bindings["image"] == result["names"][0]
    assert resolved.bindings["image_2"] == result["names"][1]
    output_root = tmp_path / "worker-output"
    staged = _stage_managed_asset_manifest(
        str(archive_path), readiness_profile=None, output_root=output_root
    )
    staged_file = output_root / "engine-input" / result["names"][0]
    assert staged.bindings["image"] == result["names"][0]
    assert staged.bindings["image_2"] == result["names"][1]
    assert staged_file.read_bytes() == image.read_bytes()
    assert (output_root / "engine-input" / result["names"][1]).read_bytes() == second_image.read_bytes()


def test_pack_images_refuses_to_overwrite(tmp_path: Path) -> None:
    image = tmp_path / "still.png"
    image.write_bytes(b"image")
    output = tmp_path / "managed.zip"
    output.write_bytes(b"keep")

    try:
        pack_images(str(output), [str(image)])
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing ZIP must not be overwritten")
    assert output.read_bytes() == b"keep"


def test_wan2gp_probe_reads_files_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install = tmp_path / "Wan2GP"
    (install / "outputs").mkdir(parents=True)
    (install / "wgp.py").write_text("# entrypoint, never executed by the probe\n")

    found = local_scan.wan2gp_folder(str(install))
    assert found == {
        "path": str(install),
        "exists": True,
        "is_wan2gp": True,
        "outputs": str(install / "outputs"),
    }

    other = tmp_path / "not-wan2gp"
    other.mkdir()
    assert local_scan.wan2gp_folder(str(other)) == {
        "path": str(other), "exists": True, "is_wan2gp": False, "outputs": None,
    }
    absent = tmp_path / "absent"
    assert local_scan.wan2gp_folder(str(absent))["exists"] is False

    assert local_scan.main(["wan2gp", str(install)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["is_wan2gp"] is True
