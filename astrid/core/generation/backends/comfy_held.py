"""Guarded local factories for VibeComfy's held transport.

The owner retains a transport pair; each ComfyTargetClient retains exactly one
attempt. A retired pair never reconnects. Known-prompt recovery is an explicit
separate connection, not a way to revive a client's generation.
"""
from __future__ import annotations

import asyncio
import io
import json
import threading
import uuid
from contextlib import contextmanager
from http.client import HTTPConnection
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from astrid.core.generation.engine_contracts import TransportError

from .comfy_target import (
    HTTP_TRANSFER_SECONDS,
    LOCAL_HTTP_SECONDS,
    MAX_JSON_BYTES,
    ComfyTarget,
    ComfyTargetClient,
    open_comfy_http,
    validate_attached_url,
)


class TargetGeneration:
    """One retirement latch shared by async events and synchronous Agent I/O."""

    def __init__(self, sequence: int):
        self.identity = uuid.uuid4().hex
        self.sequence = sequence
        self.reason: str | None = None
        self.lock = threading.RLock()
        self._on_retire = []

    @property
    def retired(self) -> bool:
        with self.lock:
            return self.reason is not None

    @contextmanager
    def guard(self):
        with self.lock:
            if self.reason is not None:
                raise TransportError("Comfy target generation retired; reconcile the original attempt")
            yield

    def retire(self, reason: str) -> None:
        with self.lock:
            if self.reason is not None:
                return
            self.reason = reason
            callbacks = tuple(self._on_retire)
        for callback in callbacks:
            callback()


class _HeldResponse:
    def __init__(self, pool, response):
        self.pool, self.response = pool, response
        self.status, self.headers = response.status, response.headers
        self.fp = response
        self.closed = False

    def __enter__(self):
        return self

    def read(self, size=-1):
        try:
            return self.response.read(size)
        except BaseException:
            self.pool.generation.retire("HTTP response interrupted")
            raise

    def __exit__(self, *_args):
        if self.closed:
            return
        self.closed = True
        try:
            if self.response.will_close or (
                not self.response.isclosed() and self.response.length != 0
            ):
                self.pool.generation.retire("HTTP connection closed or response incomplete")
            self.response.close()
        finally:
            self.pool.finish()


class _HeldHTTP:
    """One serialized keep-alive connection, with no implicit reconnect/retry."""

    def __init__(self, target, generation):
        self.target, self.generation = target, generation
        parsed = urlsplit(validate_attached_url(target.url))
        self.connection = HTTPConnection(parsed.hostname, parsed.port, timeout=LOCAL_HTTP_SECONDS)
        self.connection.auto_open = 0
        self.connected = False
        self.dispatch = threading.Lock()
        self.inflight = False
        generation._on_retire.append(self.close_idle)

    def close_idle(self):
        with self.generation.lock:
            if not self.inflight:
                self.connection.close()

    def connect(self):
        with self.dispatch:
            with self.generation.guard():
                if self.connected:
                    return
            try:
                self.connection.connect()
                self.connected = True
                with self.generation.guard():
                    pass
            except BaseException:
                self.generation.retire("HTTP connect failed")
                self.connection.close()
                raise

    def open(self, request, *, timeout):
        parsed = urlsplit(request.full_url)
        if (f"{parsed.scheme}://{parsed.netloc}" != self.target.url
                or parsed.fragment or parsed.username is not None):
            raise ValueError("held Comfy HTTP request escaped its selected origin")
        self.dispatch.acquire()
        try:
            with self.generation.guard():
                self.inflight = True
            if not self.connected or self.connection.sock is None:
                raise TransportError("held Comfy HTTP connection was replaced")
            self.connection.sock.settimeout(timeout)
            self.connection.request(request.get_method(), parsed.path + ("?" + parsed.query if parsed.query else ""),
                                    body=request.data, headers=dict(request.header_items()))
            response = self.connection.getresponse()
            if 300 <= response.status < 400:
                self.generation.retire("HTTP redirect refused")
            return _HeldResponse(self, response)
        except BaseException:
            self.generation.retire("HTTP request failed")
            self.finish()
            raise

    def finish(self):
        with self.generation.lock:
            self.inflight = False
            if self.generation.reason is not None:
                self.connection.close()
        self.dispatch.release()


class _VibeHTTP:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        await asyncio.to_thread(self.pool.connect)
        return self

    async def __aexit__(self, *_args):
        self.pool.generation.retire("held HTTP context closed")

    async def _request(self, url, payload=None):
        from time import monotonic

        import httpx

        if url != self.pool.target.url + "/prompt" and not url.startswith(self.pool.target.url + "/history/"):
            raise ValueError("unsupported held Comfy runtime operation")
        if payload is not None and url != self.pool.target.url + "/prompt":
            raise ValueError("held Comfy POST requires the prompt route")
        if payload is None:
            ComfyTargetClient._prompt_id(url.removeprefix(self.pool.target.url + "/history/"))
        body = None if payload is None else json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        if body is not None and len(body) > MAX_JSON_BYTES:
            raise ValueError("Comfy prompt request is too large")
        method = "GET" if body is None else "POST"
        request = Request(url, data=body, method=method,
                          headers={"Content-Type": "application/json", "Accept-Encoding": "identity"})

        def read():
            with self.pool.open(request, timeout=HTTP_TRANSFER_SECONDS) as response:
                raw = ComfyTargetClient._read_bounded(response, MAX_JSON_BYTES, monotonic() + HTTP_TRANSFER_SECONDS)
                status, headers = response.status, response.headers
            if 300 <= status < 400:
                self.pool.generation.retire("HTTP redirect refused")
                raise ValueError("comfy target remote request redirected unexpectedly")
            if not 200 <= status < 300:
                raise HTTPError(url, status, "Comfy request refused", headers, io.BytesIO(raw))
            return httpx.Response(status, content=raw, headers=dict(headers), request=httpx.Request(method, url))

        return await asyncio.to_thread(read)

    async def post(self, url, *, json):
        return await self._request(url, json)

    async def get(self, url):
        return await self._request(url)


class _GuardedSocket:
    def __init__(self, owner, socket, generation):
        self.owner, self.socket, self.generation = owner, socket, generation
        self.closed = False
        self._closing = None

    async def recv(self):
        from websockets.exceptions import ConnectionClosed

        try:
            return await self.socket.recv()
        except ConnectionClosed as exc:
            await self.close()
            raise ConnectionError("Comfy websocket disconnected") from exc
        except BaseException:
            await self.close()
            raise

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.recv()
        except ConnectionError:
            raise StopAsyncIteration from None

    async def close(self):
        if self._closing is None:
            self._closing = asyncio.create_task(self._finish_close())
        await asyncio.shield(self._closing)

    async def _finish_close(self):
        self.closed = True
        self.generation.retire("Comfy websocket disconnected")
        try:
            await self.socket.close()
        finally:
            await self.owner.close()


class ComfyTargetSession:
    """Keep VibeComfy's event loop and guarded pair alive across its attempts.

    The current per-job pack owns this context. LS-15 can retain the same owner
    in a resident pack; it must still allocate a new client for every attempt.
    """

    def __init__(self, target: ComfyTarget, *, opener=None):
        if target.kind == "pod":
            raise ValueError("held local transport cannot replace an attested pod transport")
        validate_attached_url(target.url)
        if opener is not None and opener is not open_comfy_http:
            raise ValueError("held Comfy transport refuses to bypass a custom opener")
        self.target = target
        self.generation = None
        self.transport = None
        self.pool = None
        self.sequence = 0
        self.fallback_reason = None
        self._loop = None
        self._thread = None
        self._closed = False
        self._transports = []
        self._sockets = []
        self._client_lock = threading.Lock()

    def _start_loop(self):
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self._loop)
            ready.set()
            self._loop.run_forever()
            self._loop.close()

        self._thread = threading.Thread(target=run, name="comfy-held-events", daemon=True)
        self._thread.start()
        ready.wait()

    def submit(self, coroutine):
        if self._closed or self._loop is None:
            coroutine.close()
            raise TransportError("Comfy held session is closed")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    async def wait(self, coroutine):
        return await asyncio.wrap_future(self.submit(coroutine))

    async def _connect(self):
        # VibeComfy already requires websockets>=12. Its protocol client neither
        # discovers proxies nor imports aiohttp's platform.system probe, which
        # can spawn cmd/ver inside the guarded Windows worker.
        from websockets.exceptions import InvalidMessage, InvalidStatusCode
        from websockets.legacy.client import WebSocketClientProtocol, connect
        from websockets.legacy.http import read_response
        from vibecomfy.runtime.transport import ComfyTransport

        incompatible_statuses = {200, 404, 405, 501}

        class GuardedProtocol(WebSocketClientProtocol):
            async def read_http_response(self):
                # Inspect a legacy HTTP/1.0 rejection with the same bounded
                # status/header parser. Such a response can select fallback,
                # but can never establish a websocket upgrade.
                class ResponseReader:
                    first = True
                    http10 = False

                    async def readline(reader):
                        line = await self.reader.readline()
                        if reader.first:
                            reader.first = False
                            if line.startswith(b"HTTP/1.0 "):
                                reader.http10 = True
                                return b"HTTP/1.1 " + line[9:]
                        return line

                reader = ResponseReader()
                try:
                    status, _reason, headers = await read_response(reader)
                except Exception as exc:
                    raise InvalidMessage("did not receive a valid HTTP response") from exc
                self.response_headers = headers
                if 300 <= status < 400:
                    raise ValueError("comfy target websocket redirected unexpectedly")
                if reader.http10:
                    if status in incompatible_statuses:
                        raise InvalidStatusCode(status, headers)
                    raise InvalidMessage("Comfy websocket upgrade requires HTTP/1.1")
                return status, headers

        self.sequence += 1
        generation = self.generation = TargetGeneration(self.sequence)
        pool = self.pool = _HeldHTTP(self.target, generation)

        async def socket_connect(url):
            parsed = urlsplit(url)
            expected = urlsplit(self.target.url)
            query = parse_qs(parsed.query)
            if (parsed.scheme != "ws" or parsed.netloc != expected.netloc or parsed.path != "/ws"
                    or parsed.fragment or set(query) != {"clientId"} or len(query["clientId"]) != 1):
                raise ValueError("held Comfy websocket escaped its selected origin")
            socket = None
            try:
                with generation.guard():
                    pass
                socket = await connect(
                    url, create_protocol=GuardedProtocol, open_timeout=HTTP_TRANSFER_SECONDS,
                    close_timeout=1, ping_interval=None, max_size=1024 * 1024,
                    max_queue=1, compression=None)
                wrapped = _GuardedSocket(socket, socket, generation)
                self._sockets.append(wrapped)

                def retired():
                    if not wrapped.closed and not self._loop.is_closed():
                        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(wrapped.close()))

                generation._on_retire.append(retired)
                with generation.guard():
                    pass
                return wrapped
            except BaseException:
                if socket is not None:
                    await socket.close()
                raise

        transport = self.transport = ComfyTransport(
            self.target.url, http_factory=lambda: _VibeHTTP(pool), websocket_connect=socket_connect,
        )
        self._transports.append(transport)
        try:
            await transport._connect()
        except InvalidStatusCode as exc:
            if exc.status_code not in incompatible_statuses:
                raise
            self.fallback_reason = "Comfy websocket unsupported"
        except BaseException:
            generation.retire("Comfy transport connection failed")
            raise

    def client(self):
        with self._client_lock:
            return self._client()

    def _client(self):
        if self._closed:
            raise TransportError("Comfy held session is closed")
        self._start_loop()
        if self.generation is None or self.generation.retired and self.fallback_reason is None:
            self.submit(self._connect()).result(timeout=HTTP_TRANSFER_SECONDS + 15)
        if self.fallback_reason is not None:
            client = ComfyTargetClient(self.target)
            client.transport_evidence = {"mode": "bounded-poll-incompatible", "reason": self.fallback_reason}
            return client
        return ComfyTargetClient(self.target, opener=self.pool.open,
                                 generation=self.generation, held_session=self)

    async def attempt(self, client):
        with client._generation_guard():
            if client.held_session is not self or client.generation is not self.generation:
                raise TransportError("Comfy attempt belongs to another target generation")
            return self.transport.attempt()

    async def _close(self):
        for transport in self._transports:
            await transport.close()
        for socket in self._sockets:
            await socket.close()
        self._transports.clear()
        self._sockets.clear()

    def close(self):
        if self._closed:
            return
        try:
            if self._loop is not None:
                self.submit(self._close()).result(timeout=HTTP_TRANSFER_SECONDS + 5)
        finally:
            self._closed = True
            if self.generation is not None:
                self.generation.retire("Comfy session closed")
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
