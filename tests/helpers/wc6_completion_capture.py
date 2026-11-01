"""Opt-in observation of the preserved WC6 call; never an executor or authority.

Load with ``-p tests.helpers.wc6_completion_capture --wc6-completion-sink=...``.
The exclusive, owner-only JSONL sink must be below the process TMPDIR. No
exception messages, headers, credentials, store reads or extra calls are captured.
Python tracing adds overhead: this is diagnostic evidence, not a timing proof.
"""
from __future__ import annotations

import dis
import hashlib
import json
import os
import re
import sys
import threading
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

ORIGINAL = (
    "tests/timeline/test_authoring_media.py::"
    "test_real_runtime_document_script_same_alias_rebind_reload_undo_retry_and_aba"
)
KEY = "wc6-undo"
_SECRET = re.compile(r"authorization|credential|password|secret|token|cookie|api.?key", re.I)


def _safe(value: Any) -> Any:
    if type(value) is dict:
        return {str(k): "[redacted]" if _SECRET.search(str(k)) else _safe(v)
                for k, v in value.items()}
    if type(value) in (list, tuple):
        return [_safe(v) for v in value]
    if type(value) is str:
        return "[redacted]" if re.search(r"bearer\s+\S+", value, re.I) else value
    if value is None or type(value) in (bool, int, float):
        return value
    return {"unavailable": "non-JSON value", "type": type(value).__name__}


def _exception(exc: BaseException) -> dict[str, Any]:
    return {"type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "object_id": id(exc)}


def _result(value: Any) -> Any:
    """Keep boundary/receipt/history facts, without duplicating film documents."""
    if type(value) is not dict:
        return _safe(value) if value is None or type(value) in (str, bool, int, float) else {
            "type": type(value).__name__}
    fields = (
        "status", "would_status", "reason", "code", "http_status",
        "base_revision_id", "base_publication_id", "previous_revision_id", "previous_publication_id",
        "accepted_revision_id", "accepted_publication_id",
        "receipt_id", "command_kind", "idempotency_key", "request_hash", "project_id",
        "created_at", "revision_id", "publication_id",
    )
    result = {key: value[key] for key in fields if key in value and (
        value[key] is None or type(value[key]) in (str, bool, int, float))}
    for key in ("data", "receipt", "result", "head", "details", "previous", "rebased_onto"):
        if key in value:
            result[key] = _result(value[key])
    for key in ("event_ids", "project_seq"):
        if type(value.get(key)) is list:
            result[key] = [item for item in value[key] if type(item) in (str, int)]
    if type(value.get("items")) is list:
        result["items"] = [_result(row) for row in value["items"]]
    return result


class CompletionCapture:
    """Observe one existing coordinator invocation, including daemon threads."""

    def __init__(self, sink: Path):
        sink = sink.absolute()
        temporary = Path(os.environ["TMPDIR"]).resolve()
        if not sink.parent.resolve().is_relative_to(temporary) or sink.is_symlink():
            raise ValueError("WC6 sink must be a new file under TMPDIR")
        self.stream = os.fdopen(os.open(sink, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
        self.failures: list[str] = []
        self.codes: dict[Any, str] = {}
        self.frames: dict[int, tuple[str, BaseException | None]] = {}
        self.identities: dict[str, Any] = {}
        self.seen: set[str] = set()
        self.selected = False
        self.lookup_count = 0
        self.thread: int | None = None
        self.coordinator: FrameType | None = None
        self.active = False
        self.previous_thread_trace: Any = None

    def emit(self, phase: str, **data: Any) -> None:
        self.seen.add(phase)
        try:
            self.stream.write(json.dumps(_safe({"phase": phase, "key": KEY, **data})) + "\n")
            self.stream.flush()
        except Exception as exc:  # noqa: BLE001 - diagnostics must never replace the real outcome
            self.failures.append(type(exc).__name__)

    def identity(self, frame: FrameType) -> dict[str, Any]:
        path = frame.f_code.co_filename
        if path not in self.identities:
            self.identities[path] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        return {"module": frame.f_globals.get("__name__"),
                "qualname": frame.f_code.co_qualname, "file": path,
                "source_sha256": self.identities[path]}

    def start(self) -> None:
        if sys.gettrace() is not None or threading.gettrace() is not None:
            self.stream.close()
            raise ValueError("WC6 capture requires no existing trace hook")
        self.previous_thread_trace = threading.gettrace()
        self.active = True
        threading.settrace(self.trace)
        sys.settrace(self.trace)

    def stop(self) -> None:
        self.active = False
        if sys.gettrace() == self.trace:
            sys.settrace(None)
        if threading.gettrace() == self.trace:
            threading.settrace(self.previous_thread_trace)
        for phase in ("session.undo.call", "service.call", "coordinator.call", "port_factory.return",
                      "provider.return", "close.return", "lookup.2.return", "send_hook.call",
                      "authenticated_history.return", "authenticated_history_entry.return"):
            if phase not in self.seen:
                self.emit("absent", boundary=phase, reason="not observed within selected call; no additional call made")
        self.emit("capture.end", selected=self.selected, sink_failures=len(self.failures))
        try:
            self.stream.close()
        except Exception as exc:  # noqa: BLE001
            self.failures.append(type(exc).__name__)

    def trace(self, frame: FrameType, event: str, arg: Any) -> Any:
        if not self.active:
            return None
        try:
            return self._trace(frame, event, arg)
        except Exception as exc:  # noqa: BLE001 - includes observation/serialization failure
            self.failures.append(type(exc).__name__)
            return None

    def _trace(self, frame: FrameType, event: str, arg: Any) -> Any:
        code = frame.f_code
        if event == "call":
            phase = self.codes.get(code)
            module = frame.f_globals.get("__name__")
            if phase is None and module not in (
                "astrid.sdk.timeline.sync", "runtime_protocol.service",
                "runtime_protocol.timeline_commit_execution", "tests.timeline.test_authoring_media",
            ):
                return None
            local = frame.f_locals
            if (module == "astrid.sdk.timeline.sync" and code.co_qualname == "TimelineSession.undo"
                    and local.get("idempotency_key") == KEY and not self.selected):
                phase = "session.undo"
            elif (module == "runtime_protocol.service" and code.co_qualname == "RuntimeService._execute_timeline_commit"
                  and local.get("idempotency_key") == KEY and not self.selected):
                phase = "service"
            elif (module == "runtime_protocol.timeline_commit_execution"
                  and code.co_qualname == "CommitExecutionCoordinator.execute"
                  and not self.selected and local.get("context", {}).get("idempotency_key") == KEY):
                self.selected = True
                self.thread = threading.get_ident()
                self.coordinator = frame
                provider = local["provider"]
                self.codes[provider.commit.__func__.__code__] = "provider"
                self.codes[local["port_factory"].__code__] = "port_factory"
                self.codes[local["self"]._lookup.__func__.__code__] = "lookup"
                phase = "coordinator"
                self.emit("request", context={k: local["context"].get(k) for k in
                          ("request", "idempotency_key", "request_hash", "realm_id", "publisher")},
                          invocation={k: local["context"]["invocation"].get(k) for k in ("operation", "path")},
                          provider_type=f"{type(provider).__module__}.{type(provider).__qualname__}",
                          provider_object_id=id(provider))
            elif (code.co_name == "lose_undo_ack" and code.co_filename.endswith("/tests/timeline/test_authoring_media.py")
                  and self.selected):
                # The existing hook exposes only its own already-populated state.
                phase = "send_hook"
            if phase is None:
                return None
            if phase not in ("session.undo", "service", "send_hook", "coordinator") and (
                    threading.get_ident() != self.thread or self.coordinator is None):
                return None
            if phase == "send_hook":
                handler = local["handler"]
                if handler.headers.get("Idempotency-Key") != KEY:
                    return None
            if phase == "lookup":
                self.lookup_count += 1
                phase = f"lookup.{self.lookup_count}"
                coordinator = frame.f_back if frame.f_back is not None else self.coordinator
                self.emit("coordinator.saved", error=(
                    _exception(coordinator.f_locals["error"])
                    if coordinator and coordinator.f_locals.get("error") else None))
            self.frames[id(frame)] = (phase, None)
            frame.f_trace_lines = phase == "send_hook"
            self.emit(phase + ".call", source=self.identity(frame))
            return self.trace
        state = self.frames.get(id(frame))
        if state is None:
            return None
        local = frame.f_locals
        phase, error = state
        if phase == "send_hook" and event in ("line", "return", "exception"):
            self.emit("send_hook.state", status=local.get("status"), lost=local.get("lost"),
                      wire_count=len(local.get("undo_wire", [])),
                      command=local["handler"].command, path=local["handler"].path,
                      defer_response=getattr(local["handler"], "_defer_response", None))
        if event == "exception":
            self.frames[id(frame)] = (phase, arg[1])
            self.emit(phase + ".exception", error=_exception(arg[1]))
        elif event == "return":
            normal = dis.opname[code.co_code[frame.f_lasti]].startswith("RETURN")
            self.emit(phase + ".return", completion="returned" if normal else "raised",
                      result=_result(arg) if normal else None,
                      error=_exception(error) if error and not normal else None)
            if phase == "port_factory" and normal and arg is not None:
                self.emit("port.identity", object_id=id(arg),
                          type=f"{type(arg).__module__}.{type(arg).__qualname__}")
                self.codes[arg.close.__func__.__code__] = "close"
                if hasattr(type(arg), "history"):
                    self.codes[type(arg).history.__code__] = "authenticated_history"
                if hasattr(type(arg), "history_entry"):
                    self.codes[type(arg).history_entry.__code__] = "authenticated_history_entry"
            del self.frames[id(frame)]
            if phase == "coordinator":
                self.coordinator = None
        return self.trace


def pytest_addoption(parser: Any) -> None:
    parser.addoption("--wc6-completion-sink", default=None, help="Private new JSONL file under TMPDIR")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: Any) -> Any:
    sink = item.config.getoption("--wc6-completion-sink")
    capture = None
    setup_failure = None
    if sink and item.nodeid == ORIGINAL:
        try:
            capture = CompletionCapture(Path(sink))
            item._wc6_completion_capture = capture
            capture.start()
        except Exception as exc:  # noqa: BLE001 - preserve the original even when capture cannot start
            setup_failure = type(exc).__name__
            capture = None
    try:
        yield
    finally:
        if capture is not None:
            capture.stop()
        failures = len(capture.failures) if capture is not None else 0
        if setup_failure or failures:
            reporter = item.config.pluginmanager.get_plugin("terminalreporter")
            if reporter is not None:
                reporter.write_line("WC6 capture unavailable or incomplete: diagnostic sink/observer failure; original outcome preserved")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any) -> Any:
    outcome = yield
    capture = getattr(item, "_wc6_completion_capture", None)
    if capture is not None and capture.failures:
        outcome.get_result().sections.append((
            "WC6 capture sink/observer failure",
            f"{len(capture.failures)} diagnostic failures; original outcome preserved",
        ))
