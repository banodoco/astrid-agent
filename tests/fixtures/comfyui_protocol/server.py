"""Deterministic ComfyUI HTTP boundary for the installed Qwen child regression.

Only the server is simulated. Workflow loading, rewriting, trust, validation,
approval, submission, and artifact settlement execute in the installed child.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DATA = Path(__file__).resolve().parent
PROMPT_ID = "installed-qwen-prompt"


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    body: Any = None


class ComfyUIProtocolServer:
    """Serve committed schemas/history/PNG, recording every request in order."""

    def __init__(self) -> None:
        self.object_info = json.loads((DATA / "object_info.json").read_text(encoding="utf-8"))
        self.history = json.loads((DATA / "history.json").read_text(encoding="utf-8"))
        self.png = (DATA / "output.png").read_bytes()
        self.fail_object_info = False
        self._requests: list[Request] = []
        self._lock = threading.Lock()
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def send(self, status: int, value: Any, *, content_type: str = "application/json") -> None:
                data = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                with state._lock:
                    state._requests.append(Request("GET", self.path))
                path = urlsplit(self.path).path
                if path == "/object_info":
                    if state.fail_object_info:
                        self.send(500, {"error": "forced object_info failure"})
                    else:
                        self.send(200, state.object_info)
                elif path == f"/history/{PROMPT_ID}":
                    self.send(200, state.history)
                elif path == "/view":
                    self.send(200, state.png, content_type="image/png")
                else:
                    self.send(404, {"error": "unexpected fixture request"})

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.loads(raw or b"{}")
                with state._lock:
                    state._requests.append(Request("POST", self.path, body))
                if self.path == "/prompt":
                    self.send(200, {"prompt_id": PROMPT_ID, "number": 1, "node_errors": {}})
                else:
                    self.send(404, {"error": "unexpected fixture request"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    @property
    def requests(self) -> list[Request]:
        with self._lock:
            return list(self._requests)

    def __enter__(self) -> ComfyUIProtocolServer:
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
