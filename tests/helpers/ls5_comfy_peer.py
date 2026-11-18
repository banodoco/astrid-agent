"""LS-5-only Comfy protocol peer; shared Plan 55 harnesses stay unchanged."""
from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter

from aiohttp import web


class ComfyPeer:
    def __init__(self, comfy=None, *, auto_complete=True, delay=0.025, keepalive_timeout=None):
        self.comfy = comfy
        self.auto_complete, self.delay = auto_complete, delay
        self.keepalive_timeout = keepalive_timeout
        self.websocket_status = None
        self.first_status = "status"
        self.history = {} if comfy is None else getattr(comfy, "history", {})
        self.posts, self.calls, self.connections, self.events = [], [], [], []
        self.sockets = {}
        self.pending = {}
        self.mode = "success"
        self.output = b"ls5-attributed-output"
        self.status = {"status_str": "success", "completed": True, "messages": []}
        self.outputs = {"9": {"images": [{"filename": "result.png", "subfolder": "", "type": "output"}]}}
        self.block_path = None
        self.block_entered = threading.Event()
        self.block_release = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._serve, name="ls5-comfy-peer", daemon=True)
        self.thread.start()
        assert self.ready.wait(10)

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._start())
        self.ready.set()
        self.loop.run_forever()
        self.loop.close()

    async def _start(self):
        app = web.Application(client_max_size=1024 * 1024 * 16)
        app.router.add_route("*", "/{path:.*}", self._handle)
        options = {} if self.keepalive_timeout is None else {"keepalive_timeout": self.keepalive_timeout}
        self.runner = web.AppRunner(app, **options)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.url = f"http://127.0.0.1:{self.site._server.sockets[0].getsockname()[1]}"

    def call(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=10)

    def observe(self, method, path, started):
        observer = getattr(self.comfy, "request_observer", None)
        if observer is not None:
            observer(method, path, started, time.monotonic())

    async def _handle(self, request):
        started = time.monotonic()
        self.calls.append((request.method, request.path))
        self.connections.append((request.path, id(request.transport)))
        if request.path == "/ws":
            if self.websocket_status is not None:
                return web.json_response({}, status=self.websocket_status)
            if self.mode == "unsupported":
                self.observe("GET", "/ws", started)
                return web.json_response({}, status=404)
            if self.mode == "ws-redirect":
                return web.Response(status=302, headers={"Location": self.url + "/recipient"})
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            client_id = request.query["clientId"]
            self.sockets[client_id] = socket
            self.events.append(("socket", client_id))
            if self.first_status is not None:
                await socket.send_json({"type": self.first_status, "data": {}})
            self.observe("GET", "/ws", started)
            try:
                async for _message in socket:
                    pass
            finally:
                self.sockets.pop(client_id, None)
            return socket
        try:
            if request.path == self.block_path:
                self.block_entered.set()
                assert await asyncio.to_thread(self.block_release.wait, 10)
            if self.mode == "http-redirect":
                return web.Response(status=302, headers={"Location": self.url + "/recipient"})
            if request.path == "/prompt":
                body = await request.json()
                self.posts.append(body)
                client_id = body.get("client_id")
                self.events.append(("submit", client_id))
                if client_id is not None:
                    assert client_id in self.sockets, "prompt precedes its websocket"
                if self.mode == "reject":
                    return web.json_response({"node_errors": {"9": "invalid input"}}, status=400)
                queued = time.monotonic()
                if self.comfy is None:
                    prompt_id = f"prompt-{len(self.posts)}"
                    result = {"prompt_id": prompt_id}
                    history = {"status": self.status, "outputs": self.outputs}
                else:
                    observer = getattr(self.comfy, "timeline_observer", None)
                    self.comfy.timeline_observer = None
                    try:
                        status, result = self.comfy.submit(body)
                    finally:
                        self.comfy.timeline_observer = observer
                    if status != 200:
                        return web.json_response(result, status=status)
                    prompt_id = result["prompt_id"]
                    self.history = self.comfy.history
                    history = self.history.pop(prompt_id)
                self.pending[prompt_id] = (client_id, history, queued)
                if self.mode == "lost-ack":
                    request.transport.close()
                    return web.Response()
                if self.mode == "drop-during-post":
                    await self.sockets[client_id].close()
                if self.auto_complete:
                    self.loop.call_later(self.delay, lambda: asyncio.create_task(self._complete(prompt_id)))
                return web.json_response(result)
            if request.path.startswith("/history/"):
                prompt_id = request.path.rsplit("/", 1)[1]
                return web.json_response({prompt_id: self.history[prompt_id]} if prompt_id in self.history else {})
            if request.path == "/object_info":
                return web.json_response(self.comfy.object_info() if self.comfy is not None else {})
            if request.path == "/system_stats":
                return web.json_response({"system": {"os": "linux"}})
            if request.path == "/queue":
                return web.json_response({"queue_running": [], "queue_pending": []})
            if request.path == "/view":
                if request.method == "HEAD":
                    return web.Response(status=404)
                if self.comfy is None:
                    data, mime = self.output, "image/png"
                else:
                    name = request.query["filename"]
                    self.comfy.views.append(name)
                    data, mime = self.comfy.outputs[name]
                return web.Response(body=data, content_type=mime)
            if request.path == "/upload/image":
                parts = await request.multipart()
                fields, name = {}, None
                async for part in parts:
                    if part.filename:
                        name = part.filename
                        await part.read()
                    else:
                        fields[part.name] = await part.text()
                if self.comfy is not None:
                    self.comfy.input_files.add(fields["subfolder"] + "/" + name)
                return web.json_response({"name": name, "subfolder": fields["subfolder"], "type": "input"})
            return web.json_response({}, status=404)
        finally:
            self.observe(request.method, request.path, started)

    async def _event(self, prompt_id, kind, **fields):
        client_id = self.pending[prompt_id][0]
        socket = self.sockets.get(client_id)
        if socket is not None and not socket.closed:
            await socket.send_json({"type": kind, "data": {"prompt_id": prompt_id, **fields}})

    def event(self, prompt_id, kind, **fields):
        self.call(self._event(prompt_id, kind, **fields))

    async def _binary_preview(self, data):
        for socket in list(self.sockets.values()):
            await socket.send_bytes(data)

    def binary_preview(self, data):
        self.call(self._binary_preview(data))

    async def _complete(self, prompt_id):
        client_id, history, queued = self.pending[prompt_id]
        await self._event(prompt_id, "execution_success")
        self.history[prompt_id] = history
        self.events.append(("history", prompt_id))
        observer = getattr(self.comfy, "timeline_observer", None)
        if observer is not None:
            observer(prompt_id, queued, queued, time.monotonic())
        await self._event(prompt_id, "executing", node=None)
        self.events.append(("history_ready", prompt_id))

    def complete(self, prompt_id):
        self.call(self._complete(prompt_id))

    async def _drop(self):
        for socket in list(self.sockets.values()):
            await socket.close()

    def drop(self):
        self.call(self._drop())

    def counts(self):
        return Counter(f"{method} {path}" for method, path in self.calls)

    def close(self):
        self.block_release.set()
        self.drop()
        self.call(self.runner.cleanup())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        assert not self.thread.is_alive()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
