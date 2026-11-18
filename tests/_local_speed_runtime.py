"""Observe the exact Runtime used by the meter without changing its behavior."""

import os
import re
import threading
import time
from collections import Counter
from urllib.parse import unquote, urlsplit

from tests._local_speed_process import epoch_time_ns


def request_path(method, path):
    path = unquote(urlsplit(path).path)
    path = re.sub(r"(?<=/)[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}(?=/|$)", "<id>", path)
    path = re.sub(r"(/v1/projects/)[^/]+", r"\1<project>", path)
    path = re.sub(r"(/v1/tasks/)(?!claim(?:/|$)|wait(?:/|$))[^/]+", r"\1<task>", path)
    path = re.sub(r"(/v1/attempts/)[^/]+", r"\1<attempt>", path)
    path = re.sub(r"(?<=/)(?:sha256:)?[0-9a-f]{64}(?=/|$)", "<digest>", path)
    return method + " " + path


class RuntimeObserver:
    def __init__(self, daemon, process):
        self.daemon, self.process = daemon, process
        self.requests = []
        self.background = []
        self.database = str(daemon.service.store.db_path)
        process.runtime_connection = daemon.service.store.conn
        process.runtime_commit_code = daemon.service.store._transaction.__wrapped__.__code__
        process.runtime_background_code = type(daemon.httpd)._reap_task_lifetimes.__code__
        process.runtime_background_owner = daemon.httpd
        process.runtime_reap_code = daemon.service.store.reap_overdue_leases.__func__.__code__
        process.runtime_reap_owner = daemon.service.store

    @staticmethod
    def _span(kind, context, route=None):
        return {"kind": kind, "write_context": context, "route": route,
                "pid": os.getpid(), "tid": threading.get_native_id(),
                "monotonic_start_ns": time.perf_counter_ns(), "monotonic_end_ns": None,
                "epoch_start_ns": epoch_time_ns(), "epoch_end_ns": None}

    @staticmethod
    def _finish(span):
        span["epoch_end_ns"] = epoch_time_ns()
        span["monotonic_end_ns"] = time.perf_counter_ns()

    def sqlite_mode(self):
        store = self.daemon.service.store
        with store._mutex:
            return {name: store.conn.execute("PRAGMA " + name).fetchone()[0]
                    for name in ("journal_mode", "synchronous", "wal_autocheckpoint")}

    def install(self):
        from runtime_protocol import platform
        from runtime_protocol.server import RuntimeHandler, RuntimeHTTPServer

        owner, original = self, RuntimeHandler._dispatch

        def dispatch(handler):
            if handler.server is not owner.daemon.httpd:
                return original(handler)
            route = request_path(handler.command, handler.path)
            span = owner._span("runtime_request_span", "http_heartbeat" if route.endswith("/heartbeat") else "http_request", route)
            owner.requests.append(span)
            previous = getattr(owner.process.context, "route", None)
            owner.process.context.route = route
            try:
                return original(handler)
            finally:
                owner._finish(span)
                owner.process.context.route = previous

        self.process.patches.setattr(RuntimeHandler, "_dispatch", dispatch)
        reap = RuntimeHTTPServer._reap_task_lifetimes

        def background_reap(server, *args, **kwargs):
            if server is not owner.daemon.httpd:
                return reap(server, *args, **kwargs)
            span = owner._span("runtime_background_span", "background_lifetime_reaper")
            owner.background.append(span)
            try:
                return reap(server, *args, **kwargs)
            finally:
                owner._finish(span)
        self.process.patches.setattr(RuntimeHTTPServer, "_reap_task_lifetimes", background_reap)
        if hasattr(platform, "_move_file_write_through"):
            move = platform._move_file_write_through

            def write_through(*args, **kwargs):
                start = epoch_time_ns()
                success = False
                try:
                    result = move(*args, **kwargs)
                    success = True
                    return result
                finally:
                    owner.process.event("platform_write_through_" + ("success" if success else "error"),
                        epoch_start_ns=start, epoch_end_ns=epoch_time_ns(),
                        route=getattr(owner.process.context, "route", None))
            self.process.patches.setattr(platform, "_move_file_write_through", write_through)
        return self

    def report(self, start_ns, reply_ns):
        return dict(sorted(Counter(span["route"] for span in self.requests
                                   if start_ns <= span["monotonic_start_ns"] <= reply_ns).items()))

    def write_context_spans(self, start_ns, reply_ns, epoch_reply_ns):
        """Retain known request/reaper scope even if it is still active at reply."""
        spans = []
        for original in [*self.requests, *self.background]:
            span = dict(original)
            if (span["monotonic_start_ns"] > reply_ns or
                    (span["monotonic_end_ns"] is not None and span["monotonic_end_ns"] < start_ns)):
                continue
            if span["epoch_end_ns"] is None or span["epoch_end_ns"] > epoch_reply_ns:
                span["epoch_end_ns"] = epoch_reply_ns
                span["clipped_at_reply"] = True
            spans.append(span)
        return spans
