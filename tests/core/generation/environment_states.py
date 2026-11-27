"""Small, CPU-only fixtures shared by environment setup and recovery tests."""

from __future__ import annotations

import io
import re
import socket
import threading
import zipfile
from collections import namedtuple
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

GIB = 1 << 30
_DiskUsage = namedtuple("usage", "total used free")


def fake_gpu(kind):
    """Return a fresh NVIDIA discovery result for a named machine."""
    devices = {
        "rtx5070ti": ["NVIDIA GeForce RTX 5070 Ti Laptop GPU"],
        "rtx4090": ["NVIDIA GeForce RTX 4090"],
        "gtx1060": ["NVIDIA GeForce GTX 1060"],
        "none": [],
    }[kind]
    return {
        "available": bool(devices),
        "devices": devices,
        "reason": (
            "" if devices else
            "No NVIDIA GPU was detected because nvidia-smi is not on PATH."
        ),
    }


def fake_disk_usage(free_bytes, total_bytes=2000 * GIB):
    """Return a path-independent stand-in for shutil.disk_usage."""
    result = _DiskUsage(
        total_bytes, total_bytes - free_bytes, free_bytes
    )

    def disk_usage(path):
        return result

    return disk_usage


def clean_machine(tmp_path):
    """Create only the empty home and local application data directories."""
    home = tmp_path / "home"
    localappdata = tmp_path / "localappdata"
    home.mkdir(exist_ok=True)
    localappdata.mkdir(exist_ok=True)
    if any(home.iterdir()) or any(localappdata.iterdir()):
        raise ValueError("clean machine folders must be empty")
    return SimpleNamespace(
        home=home,
        localappdata=localappdata,
        destination=localappdata / "Astrid" / "ComfyUI",
        environ={
            "LOCALAPPDATA": str(localappdata),
            "HOME": str(home),
            "USERPROFILE": str(home),
        },
    )


def no_comfyui(tmp_path):
    """Create a clean machine, including an absent home/.vibecomfy folder."""
    return clean_machine(tmp_path)


def foreign_destination(tmp_path):
    """Create a user-owned destination with a single personal file."""
    destination = tmp_path / "foreign"
    destination.mkdir()
    (destination / "notes.txt").write_text("My personal notes.\n", encoding="utf-8")
    return destination


def snapshot(path):
    """Record descendant sizes and modification times, not access times."""
    path = Path(path)
    result = {}
    for child in path.rglob("*"):
        stat = child.lstat()
        result[child.relative_to(path).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return result


def make_zip(entries: dict[str, bytes]) -> bytes:
    """Build a deflated ZIP archive in memory."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return output.getvalue()


@contextmanager
def serve_release(files: dict[str, bytes], *, break_after: int | None = None):
    """Yield (base_url, server) for an ephemeral, resumable HTTP release."""
    if break_after is not None and break_after < 0:
        raise ValueError("break_after must be non-negative")
    payloads = {"/" + name.lstrip("/"): data for name, data in files.items()}
    broken = set()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            range_header = self.headers.get("Range")
            with lock:
                self.server.requests.append((self.path, range_header))
            if self.path not in payloads:
                self.send_error(404)
                return
            data = payloads[self.path]
            start = 0
            if range_header is not None:
                match = re.fullmatch(r"bytes=(\d+)-", range_header)
                if match is None or int(match[1]) >= len(data):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(data)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start = int(match[1])
                self.send_response(206)
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}"
                )
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(data) - start))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            with lock:
                interrupt = (
                    break_after is not None
                    and range_header is None
                    and self.path not in broken
                )
                if interrupt:
                    broken.add(self.path)
            if interrupt:
                self.wfile.write(data[:break_after])
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_WR)
            else:
                self.wfile.write(data[start:])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
