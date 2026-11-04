"""Route-local HTTP adapter for one claimed engine and agent chunk transfers."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import socket
import threading
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

from .engine_contracts import AttestationError, DeadlineExceeded, TransportError
from .engine_endpoint import endpoint_to_dict, parse_endpoint

logger = logging.getLogger(__name__)
# Each actual claimed-agent request is short, below the provider's ~100 s cap.
REQUEST_SECONDS = 15.0
# Pause between unchanged retries of a provider no-route reply.
ROUTE_RETRY_PAUSE_SECONDS = 0.5
# One local gateway exchange (all its retries) ends before the pinned
# VibeComfy client's fixed 30 s prompt/history timeout, so that caller always
# observes the outcome rather than abandoning a request still being retried.
LOCAL_EXCHANGE_SECONDS = 25.0


def _peer_closed(sock):
    """True when the local caller has already closed its gateway connection."""
    import select
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        return bool(readable) and sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


class _GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    observation_policy = None

    def __init__(self, *args):
        self.active = set()
        self.active_lock = threading.Lock()
        super().__init__(*args)

    def get_request(self):
        connection, address = super().get_request()
        with self.active_lock:
            self.active.add(connection)
        return connection, address

    def shutdown_request(self, request):
        with self.active_lock:
            self.active.discard(request)
        super().shutdown_request(request)

    def server_close(self):
        with self.active_lock:
            for connection in self.active:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        super().server_close()


class EngineEndpointTransport:
    """Attach transport only; close never stops the engine or releases its pod."""

    def __init__(self, endpoint, *, pod_id: str, agent_port: int, deadline: float | None = None):
        # Validate *before* starting a listener, opening the bearer, or sending.
        from runpod_lifecycle.https import pod_url

        self._endpoint = parse_endpoint(endpoint_to_dict(endpoint))
        expected = pod_url(pod_id, agent_port) + "/engines/" + endpoint.endpoint_id
        if (endpoint.placement != "runpod" or endpoint.state != "ready"
                or endpoint.url != expected or endpoint.auth.kind != "bearer-file"
                or endpoint.auth.token is None):
            raise AttestationError("endpoint differs from the original claimed agent origin")
        self._origin = urlsplit(pod_url(pod_id, agent_port))
        if deadline is not None and (not math.isfinite(deadline) or deadline <= time.monotonic()):
            raise DeadlineExceeded("endpoint caller deadline exceeded")
        self._pod_id, self._agent_port = pod_id, agent_port
        self._deadline = deadline
        self._prompt_attempted = False
        self._server = None
        self._thread = None
        self._lock = threading.RLock()

    @property
    def endpoint(self):
        return self._endpoint

    def _client(self, timeout):
        """One HTTPS client for one actual claimed-agent request.

        This is the same sanctioned HTTPX egress HttpsPodTransport uses for the
        exact pod origin: environment proxy/CA configuration applies, redirects
        are never followed and the URL is always the validated claimed origin.
        """
        import httpx
        return httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    def _route_deadline(self):
        """The caller's absolute local deadline; otherwise the lifecycle grace."""
        if self._deadline is not None:
            return self._deadline
        from runpod_lifecycle.https import ROUTE_GRACE_SECONDS
        return time.monotonic() + ROUTE_GRACE_SECONDS

    async def _attempt(self, client, method, target, headers, body, *, budget, limit):
        async with asyncio.timeout(budget):
            async with client.stream(method, self._origin.geturl() + target, headers=headers,
                                     content=body) as response:
                blocks, total = [], 0
                async for block in response.aiter_bytes():
                    total += len(block)
                    if total > limit:
                        raise OSError("endpoint response exceeds limit")
                    blocks.append(block)
                return response.status_code, response.headers, b"".join(blocks)

    async def _exchange(self, method, target, headers, body, *, deadline, limit, abandoned=None):
        """Send one frozen request; retry ONLY the provider's empty no-route 404.

        Method, target, headers (including the original bearer) and body bytes
        are fixed before this loop and resent unchanged. An empty-bodied 404
        never reached the agent (the agent's own 404s carry a JSON body), so a
        prompt cannot have been accepted. A network error, timeout, 502, any
        nonempty 404 or other reply is returned/raised at once: after a lost
        reply prompt acceptance is unknown and must not be replayed. Every
        attempt is at most 15 seconds and all of them share one deadline. When
        the deadline leaves no room for another attempt, or the local caller has
        gone away, the last no-route reply is returned unchanged, still meaning
        "not delivered".
        """
        from runpod_lifecycle.https import HttpsPodTransport

        warned = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeadlineExceeded("endpoint caller deadline exceeded")
            budget = min(REQUEST_SECONDS, remaining)
            async with self._client(budget) as client:
                status, response_headers, data = await self._attempt(
                    client, method, target, headers, body, budget=budget, limit=limit)
            # Same classifier as the lifecycle transport's route grace.
            if not HttpsPodTransport.route_unavailable(SimpleNamespace(status_code=status, content=data)):
                return status, response_headers, data
            if not warned:
                # Fixed text only: no path, identity, header or body values.
                logger.warning("claimed engine HTTPS route unavailable (HTTP 404 without agent body); retrying")
                warned = True
            remaining = deadline - time.monotonic()
            if remaining <= ROUTE_RETRY_PAUSE_SECONDS:
                return status, response_headers, data
            await asyncio.sleep(ROUTE_RETRY_PAUSE_SECONDS)
            if abandoned is not None and abandoned():
                return status, response_headers, data

    def _relay(self, handler, target, bearer):
        """Relay one local gateway exchange to the exact claimed agent origin."""
        import httpx
        from runpod_lifecycle.pod_agent import CHUNK, ENGINE_BODY_LIMIT

        handler.close_connection = True
        deadline = min(self._route_deadline(), time.monotonic() + LOCAL_EXCHANGE_SECONDS)
        headers = {"Authorization": "Bearer " + bearer, "Connection": "close",
                   "Accept-Encoding": "identity"}
        try:
            if (handler.headers.get("Transfer-Encoding")
                    or len(handler.headers.get_all("Content-Length", [])) > 1):
                raise ValueError("explicit request framing required")
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 <= length <= ENGINE_BODY_LIMIT:
                raise ValueError("engine body exceeds limit")
            # Deliberate allowlist, as at the agent: no client Authorization,
            # Cookie, Host, Range, Upgrade, proxy or hop-by-hop headers.
            for name in ("Content-Type", "Accept"):
                if name in handler.headers:
                    headers[name] = handler.headers[name]
            blocks, remaining = [], length
            while remaining:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    raise DeadlineExceeded("endpoint caller deadline exceeded")
                handler.connection.settimeout(min(REQUEST_SECONDS, wait))
                block = handler.rfile.read(min(CHUNK, remaining))
                if not block:
                    raise ValueError("short engine upload")
                blocks.append(block)
                remaining -= len(block)
            body = b"".join(blocks)
            status, response_headers, data = asyncio.run(self._exchange(
                handler.command, target, headers, body, deadline=deadline, limit=ENGINE_BODY_LIMIT,
                abandoned=lambda: _peer_closed(handler.connection)))
        except DeadlineExceeded:
            return handler.reply(504, {"error": "engine caller deadline exceeded"})
        except (httpx.HTTPError, OSError, TimeoutError):
            # Includes lost replies: acceptance unknown, never retried here.
            return handler.reply(502, {"error": "engine transport unavailable"})
        except (ValueError, TypeError):
            return handler.reply(400, {"error": "invalid engine exchange"})
        if status == 101 or 300 <= status < 400:
            return handler.reply(502, {"error": "engine redirect or upgrade refused"})
        handler.send_response(status)
        handler.send_header("Connection", "close")
        handler.send_header("Cache-Control", "no-store")
        for name in ("Content-Type", "ETag"):
            if name in response_headers:
                handler.send_header(name, response_headers[name])
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _bearer(self):
        from runpod_lifecycle.https import read_token_file
        try:
            token = read_token_file(self.endpoint.auth.token.path)
            if any(ord(c) <= 32 or ord(c) >= 127 for c in token):
                raise ValueError()
            return token
        except (OSError, ValueError, UnicodeError):
            raise TransportError("endpoint bearer file is unavailable or unsafe") from None

    def request_budget(self):
        remaining = REQUEST_SECONDS if self._deadline is None else self._deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded("endpoint caller deadline exceeded")
        return min(REQUEST_SECONDS, remaining)

    @contextmanager
    def bounded(self, timeout_seconds):
        """Share the caller's remaining budget with every actual remote poll."""
        timeout_seconds = float(timeout_seconds)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("endpoint prompt timeout must be finite and positive")
        previous = self._deadline
        self._deadline = min(previous or math.inf, time.monotonic() + timeout_seconds)
        try:
            self.request_budget()
            yield
        finally:
            self._deadline = previous

    def _transfer(self, operation, *args):
        from runpod_lifecycle.https import HttpsPodTransport
        # The existing chunk client talks to the route-local adapter over
        # loopback (never through a proxy). The adapter restores the exact
        # claimed HTTPS origin and bearer, bounds that outbound leg and owns
        # the no-route retry, so this local client does not add a second one.
        client = HttpsPodTransport(self._pod_id, self._bearer(), port=self._agent_port,
                                  base_url=self.server_url, request_timeout=self.request_budget(),
                                  deadline=self._deadline, route_grace=0)
        try:
            if operation == "put":
                client.put(*args)
            else:
                client.get(*args, use_ranges=False)
        finally:
            client.disconnect()

    def put(self, local, remote):
        self._transfer("put", local, remote)

    def get(self, remote, local):
        self._transfer("get", remote, local)

    def raise_if_submission_uncertain(self, exc):
        """Keep missing acceptance conservative despite the pinned receipt label.

        The pinned runtime classifies non-timeout queue transport errors as
        rejection. It has no per-run failure-classifier hook. Preserve its
        receipt/cause and tell the original caller to reconcile, never replay.
        """
        if not self._prompt_attempted or getattr(exc, "prompt_id", None) is not None:
            return
        cursor, seen = exc, set()
        while cursor is not None and id(cursor) not in seen:
            seen.add(id(cursor))
            status = getattr(getattr(cursor, "response", None), "status_code", None)
            if status in {400, 401, 403, 404, 422}:
                return  # Explicit rejection, including agent authentication.
            cursor = cursor.__cause__
        error = TransportError("prompt acceptance unknown; do not resubmit; reconcile the original attempt")
        error.acceptance_known = False
        error.prompt_id = None
        if getattr(exc, "receipt_path", None) is not None:
            error.receipt_path = exc.receipt_path
        raise error from exc

    @property
    def server_url(self):
        """Start one in-process root adapter; it owns no child or engine."""
        from runpod_lifecycle.pod_agent import Handler, engine_operation

        transport = self

        class GatewayHandler(Handler):
            def dispatch(self):
                import re
                transfer = ((self.command == "POST" and self.path in {"/uploads", "/download-chunk"})
                            or (self.command in {"GET", "POST", "PUT"}
                                and re.fullmatch(r"/uploads/[a-f0-9]{64}(?:\?offset=[0-9]+)?", self.path)))
                engine = engine_operation(self.command, self.path)
                if (not (engine or transfer) or self.headers.get("Upgrade") is not None
                        or self.headers.get("Range") is not None
                        or "upgrade" in self.headers.get("Connection", "").lower()):
                    return self.reply(403, {"error": "engine operation denied"})
                try:
                    transport.request_budget()
                    bearer = transport._bearer()
                except DeadlineExceeded:
                    return self.reply(504, {"error": "engine caller deadline exceeded"})
                except TransportError:
                    return self.reply(503, {"error": "engine authentication unavailable"})
                if engine and self.command == "POST" and urlsplit(self.path).path in {"/prompt", "/api/prompt"}:
                    with transport._lock:
                        if transport._prompt_attempted:
                            return self.reply(409, {"error": "prompt already attempted; reconcile original attempt"})
                        transport._prompt_attempted = True
                target = urlsplit(transport.endpoint.url).path + self.path if engine else self.path
                return transport._relay(self, target, bearer)

        with self._lock:
            if self._server is None:
                server = _GatewayServer(("127.0.0.1", 0), GatewayHandler)
                self._server = server
                self._thread = threading.Thread(target=server.serve_forever, daemon=True)
                self._thread.start()
            return f"http://127.0.0.1:{self._server.server_port}"

    def object_info(self, *, deadline):
        """Probe the exact claimed engine through the same supported egress."""
        import httpx

        deadline = min(deadline, self._deadline or math.inf)
        if deadline <= time.monotonic():
            raise DeadlineExceeded("endpoint health deadline exceeded")
        try:
            status, _headers, data = asyncio.run(self._exchange(
                "GET", urlsplit(self.endpoint.url).path + "/object_info",
                {"Authorization": "Bearer " + self._bearer(), "Connection": "close",
                 "Accept-Encoding": "identity"}, b"", deadline=deadline, limit=32 * 1024 * 1024))
            if status != 200:
                raise TransportError("endpoint object_info rejected")
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (httpx.HTTPError, OSError, TimeoutError, ValueError):
            raise TransportError("endpoint health transport failed") from None

    def close(self):
        with self._lock:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()
                self._thread.join(timeout=5)
                self._server = self._thread = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
