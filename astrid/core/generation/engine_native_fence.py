"""Agent's private, process-local Comfy admission and worker barrier.

Loaded by engine_native_launcher, never by a Comfy HTTP extension. The only
control transport is an inherited socket. A public request cannot carry control
authority. This module intentionally has no Runtime dependency.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import gc
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import select
import socket
import threading
import time

VERSION = "astrid.native-fence.v1"
LIMIT = 65536
_admission = contextvars.ContextVar("native_admission", default=None)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def identity(pid):
    birth = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    return {"pid": pid, "birth": birth, "uid": Path(f"/proc/{pid}").stat().st_uid,
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


def decode(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate control key")
            result[key] = value
        return result
    if not data or len(data) >= LIMIT:
        raise ValueError("invalid control frame")
    value = json.loads(data, object_pairs_hook=pairs,
                       parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    if not isinstance(value, dict):
        raise ValueError("invalid control record")
    return value


# Strict path syntax is checked before router resolution, Expect and middleware.
# All routes outside this small Agent client surface are permanently denied.
_READS = {"/system_stats", "/queue", "/prompt", "/features", "/history", "/object_info"}
_WRITES = {"/prompt", "/upload/image", "/upload/mask", "/queue", "/interrupt"}


def policy(method, raw_path):
    path, _, query = raw_path.partition("?")
    if (not path.startswith("/") or "%" in path or "\\" in path or "//" in path
            or any(p in {".", ".."} for p in path.split("/"))
            or not re.fullmatch(r"/[A-Za-z0-9_./-]*", path)):
        return "deny"
    if path.startswith("/api/"):
        path = path[4:]
    if method == "GET" and (path in _READS or re.fullmatch(r"/history/[A-Za-z0-9_-]+", path)):
        return "read"
    if method == "GET" and path == "/view":
        return "tracked"  # File response lifetime is included in the ledger.
    if method == "POST" and path in _WRITES:
        return "tracked"
    # WebSockets (including feature-negotiation frames), public free, all UI,
    # assets, VHS GETs, internal routes, upgrades and unknown routes are denied.
    return "deny"


class Fence:
    def __init__(self, channel, enrollment, source_files):
        self.channel = channel
        self.channel.setblocking(False)
        os.set_inheritable(channel.fileno(), False)
        self.enrollment = enrollment
        self.source_files = source_files
        self.lock = threading.RLock()
        self.state = "CLOSED"
        self.handlers = set()
        self.tasks = {}  # Descendants remain obligations after HTTP acceptance.
        self.worker_thread = None
        self.queue = None
        self.queue_admissions = 0
        self.worker_iterations = 0
        self.worker_ready = False
        self.record = None
        self.bound = None
        self.route_digest = None
        self.server = None
        self.object_info = None
        self.dispatch = None
        self.make_handler = None
        self.http_server = None
        self.controller_fd = os.pidfd_open(enrollment["controller"]["pid"])
        if identity(enrollment["controller"]["pid"]) != enrollment["controller"]:
            raise ValueError("controller birth changed")

    def poison(self):
        with self.lock:
            self.state = "UNCERTAIN"

    def dispatch_ready(self):
        return (self.http_server is not None and self.server.app._handle is self.dispatch
                and self.server.app._make_handler is self.make_handler
                and self.http_server.request_handler is self.dispatch)

    def connected(self):
        if select.select([self.controller_fd], [], [], 0)[0]:
            self.poison()
            return False
        try:
            if select.select([self.channel], [], [], 0)[0] and not self.channel.recv(1, socket.MSG_PEEK):
                self.poison()
                return False
        except (OSError, ValueError):
            self.poison()
            return False
        return True

    def authorized_work(self):
        # Context tokens belong to live handlers. Copied contexts in detached
        # tasks lose authority as soon as their admitting handler terminates.
        return (_admission.get() in self.handlers or _admission.get() in self.tasks.values() or
                threading.get_ident() == self.worker_thread and not self.worker_ready)

    def prepare(self, execution):
        fence = self
        base = execution.PromptQueue

        class TrackedQueue(base):
            def __init__(self, server):
                super().__init__(server)
                if fence.queue is not None:
                    raise ValueError("multiple prompt queues")
                self.mutex = fence.lock
                self.not_empty = threading.Condition(self.mutex)
                fence.queue = self

            def put(self, item):
                with fence.lock:
                    if not fence.authorized_work() or fence.state == "RETIRED":
                        fence.poison()
                        raise RuntimeError("untracked queue insertion")
                    result = super().put(item)
                    fence.queue_admissions += 1
                    return result

            def get(self, timeout=None):
                # Return to the real worker's whole-iteration boundary even
                # when idle. This is not an empty-queue settlement assertion.
                item = super().get(timeout=min(timeout if timeout is not None else .02, .02))
                with fence.lock:
                    fence.worker_ready = False
                return item

            def set_flag(self, name, data):
                with fence.lock:
                    if not fence.authorized_work():
                        fence.poison()
                        raise RuntimeError("untracked worker flag")
                    return super().set_flag(name, data)

        execution.PromptQueue = TrackedQueue

    def worker(self, fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            with self.lock:
                if self.worker_thread is not None:
                    raise RuntimeError("multiple prompt workers")
                self.worker_thread = threading.get_ident()
            try:
                return fn(*args, **kwargs)
            finally:
                self.poison()  # An exited/faulted worker can never certify drain.
        return run

    def boundary(self, q, executor, need_gc, last_gc):
        """Called at the top of the pinned *whole* worker iteration.

        task_done is deliberately insufficient: flags, executor cleanup, GC
        and synchronous descendants all precede the next boundary.
        """
        operation = None
        with self.lock:
            self.worker_ready = False
            if q is not self.queue or threading.get_ident() != self.worker_thread:
                self.poison()
                raise RuntimeError("foreign worker boundary")
            self.connected()
            if self.http_server is not None and not self.dispatch_ready():
                self.poison()
            self.worker_iterations += 1
            quiet = not (self.handlers or self.tasks or q.queue or q.currently_running or q.flags or need_gc)
            self.worker_ready = quiet
            record = self.record
            if record and record["status"] in {"draining", "settled", "ready_unload"}:
                if time.monotonic_ns() >= record["request"]["deadline_ns"]:
                    record.update(status="unresolved", error="settlement_timeout",
                                  queue_running=len(q.currently_running), queue_pending=len(q.queue))
                    self.state = "UNCERTAIN"
                elif quiet and self.state == "DRAINING":
                    if record["status"] == "draining":
                        record.update(status="settled", barrier_ns=time.monotonic_ns(),
                                      admitted_serial=self.queue_admissions,
                                      worker_iterations=self.worker_iterations)
                    elif record["status"] == "ready_unload":
                        record.update(status="unloading", effect_sent=True)
                        self.worker_ready = False
                        operation = record
        if operation is not None:
            try:
                import comfy.model_management as management
                management.unload_all_models()
                executor.reset()
                gc.collect()
                management.soft_empty_cache()
                with self.lock:
                    operation.update(status="completed", unload_ack=True, error=None,
                                     settled_ns=time.monotonic_ns(), queue_running=0, queue_pending=0)
                    self.state = "RETIRED" if self.state == "DRAINING" else "UNCERTAIN"
            except BaseException:
                with self.lock:
                    operation.update(status="failed", unload_ack=False, error="native_unload_failed")
                    self.state = "UNCERTAIN"
        # Force the original worker's existing GC branch to finish its pending
        # cleanup during drain, rather than waiting a ten-second idle interval.
        return 0 if self.state != "OPEN" and need_gc else last_gc

    def _source(self, fn):
        while isinstance(fn, functools.partial):
            fn = fn.func
        fn = inspect.unwrap(fn)
        path = Path(inspect.getsourcefile(fn) or "").resolve()
        expected = self.source_files.get(str(path))
        if expected is None or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("unreviewed live route closure")
        return {"source": expected, "name": fn.__qualname__}

    def install_server(self, server, args):
        from aiohttp import web
        if (self.server is not None or args.listen != "127.0.0.1" or args.port != 8188
                or not args.disable_assets or args.enable_manager
                or getattr(args, "enable_agent", False) or server.asset_manager.enabled):
            raise ValueError("unsupported native launch configuration")
        self.server = server
        if server.loop.get_task_factory() is not None:
            raise ValueError("unknown task factory")
        def task_factory(loop, coroutine, **kwargs):
            context = kwargs.get("context")
            token = context.get(_admission) if context is not None else _admission.get()
            task = asyncio.Task(coroutine, loop=loop, **kwargs)
            with self.lock:
                if token is not None:
                    if token not in self.handlers and token not in self.tasks.values():
                        self.poison()
                    self.tasks[task] = token
            def finished(done):
                with self.lock:
                    self.tasks.pop(done, None)
                    if done.cancelled() or done.exception() is not None:
                        self.poison()
            if token is not None:
                task.add_done_callback(finished)
            return task
        server.loop.set_task_factory(task_factory)
        rows = []
        def audit(app):
            for middleware in app.middlewares:
                self._source(middleware)
            for route in app.router.routes():
                rows.append({"method": route.method, "path": route.resource.canonical,
                             "handler": self._source(route.handler),
                             "expect": self._source(route._expect_handler)})
            for subapp in app._subapps:
                audit(subapp)
        audit(server.app)
        self.route_digest = digest(rows)
        # Object-info calls arbitrary INPUT_TYPES code. Cache it at startup;
        # it is never recomputed by a supposedly read-only drain request.
        info_handler = next(route.handler for route in server.app.router.routes()
                            if route.method == "GET" and route.resource.canonical == "/object_info")
        self.object_info = server.loop.run_until_complete(info_handler(None)).body
        original = server.app._handle

        async def dispatch(request):
            kind = policy(request.method, request.raw_path)
            token = None
            with self.lock:
                if (not self.connected() or not self.dispatch_ready()
                        or request.headers.get("Upgrade") or kind == "deny"):
                    return web.Response(status=403)
                if kind == "tracked":
                    if self.state != "OPEN":
                        return web.Response(status=503)
                    token = object()
                    self.handlers.add(token)
            context = _admission.set(token)
            try:
                if kind == "read" and request.path in {"/object_info", "/api/object_info"}:
                    return web.Response(body=self.object_info, content_type="application/json")
                response = await original(request)
                if kind == "tracked" and not response.prepared:
                    await response.prepare(request)
                    await response.write_eof()
                return response
            except BaseException:
                # Cancellation can leave validation executor work outstanding.
                # Retain uncertainty instead of certifying its disappearance.
                if token is not None:
                    self.poison()
                raise
            finally:
                _admission.reset(context)
                with self.lock:
                    self.handlers.discard(token)
        self.dispatch = dispatch
        server.app._handle = dispatch
        original_make_handler = server.app._make_handler
        def make_handler(*positional, **kwargs):
            if self.http_server is not None or server.app._handle is not dispatch:
                self.poison()
                raise ValueError("native dispatcher changed or multiple servers")
            actual = original_make_handler(*positional, **kwargs)
            if actual.request_handler is not dispatch:
                self.poison()
                raise ValueError("native server callback differs")
            self.http_server = actual
            return actual
        self.make_handler = make_handler
        server.app._make_handler = make_handler
        server.loop.create_task(self.control())

    def snapshot(self):
        return {"version": VERSION, "enrollment": self.enrollment, "engine": identity(os.getpid()),
                "routes": self.route_digest, "state": self.state, "bound": self.bound,
                "worker_ready": self.worker_ready, "handlers": len(self.handlers),
                "tasks": len(self.tasks), "dispatch_ready": self.dispatch_ready(), "record": self.record}

    async def control(self):
        loop = asyncio.get_running_loop()
        try:
            while True:
                value = decode(await loop.sock_recv(self.channel, LIMIT))
                with self.lock:
                    if (set(value) != {"version", "enrollment", "sequence", "operation", "payload"}
                            or value["version"] != VERSION or value["enrollment"] != self.enrollment
                            or type(value["sequence"]) is not int
                            or value["sequence"] != getattr(self, "sequence", 0) + 1):
                        raise ValueError("foreign native control")
                    self.sequence = value["sequence"]
                    op, payload = value["operation"], value["payload"]
                    if not self.connected():
                        raise ValueError("native controller lost")
                    if op != "enroll" and not self.dispatch_ready():
                        raise ValueError("native dispatcher is not enrolled")
                    if op == "enroll":
                        if self.state != "CLOSED" or self.bound is not None or payload != {}:
                            raise ValueError("native enrollment reused")
                        self.state = "OPEN"
                    elif op == "bind":
                        first = self.state == "OPEN" and self.bound is None
                        successor = (self.state == "RETIRED" and self.bound is not None
                                     and self.record is not None and self.record["status"] == "completed")
                        if (not (first or successor) or not isinstance(payload, dict)
                                or set(payload) != {"binding", "generation", "association_id"}
                                or type(payload["generation"]) is not int or payload["generation"] < 1
                                or successor and (payload["generation"] <= self.bound["generation"]
                                    or payload["association_id"] == self.bound["association_id"])):
                            raise ValueError("native binding differs")
                        self.bound = payload
                        self.record = None
                        self.state = "OPEN"  # New verified generation only, never a timer/retry.
                    elif op == "release":
                        if not isinstance(payload, dict) or set(payload) != {"binding", "generation", "association_id", "request"}:
                            raise ValueError("native release malformed")
                        request = payload["request"]
                        if ({k: payload[k] for k in self.bound or {}} != self.bound
                                or not isinstance(request, dict) or type(request.get("deadline_ns")) is not int):
                            raise ValueError("native release binding differs")
                        if self.record is not None:
                            if canonical(request) != canonical(self.record["request"]):
                                raise ValueError("native release request changed")
                        elif self.state == "OPEN" and 0 < request["deadline_ns"] - time.monotonic_ns() <= 10_000_000_000:
                            # Close linearization precedes the ACK and all waits.
                            self.state = "DRAINING"
                            self.record = {"request": request, "digest": digest(request), "status": "draining",
                                           "effect_sent": False, "unload_ack": None, "error": None,
                                           "closed_ns": time.monotonic_ns()}
                        else:
                            raise ValueError("native release gate unknown")
                    elif op == "observe":
                        if payload != (self.record or {}).get("request"):
                            raise ValueError("unknown native observation")
                    elif op == "unload":
                        if (payload != (self.record or {}).get("request")
                                or self.record["status"] not in {"settled", "ready_unload", "unloading", "completed", "failed", "unresolved"}):
                            raise ValueError("unsettled native unload")
                        if self.record["status"] == "settled":
                            if self.state != "DRAINING" or time.monotonic_ns() >= payload["deadline_ns"]:
                                raise ValueError("native unload deadline or gate differs")
                            self.record["status"] = "ready_unload"
                    elif op == "status":
                        if payload != {}:
                            raise ValueError("native status malformed")
                    else:
                        raise ValueError("unsupported native control")
                    reply = {"sequence": self.sequence, **self.snapshot()}
                data = canonical(reply)
                if len(data) >= LIMIT:
                    raise ValueError("native reply overflow")
                await loop.sock_sendall(self.channel, data)
        except BaseException:
            self.poison()
            self.channel.close()
