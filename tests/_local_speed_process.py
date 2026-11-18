"""Test-only process observer; never install during the latency measurement.

The strict/captured worker startup is retained verbatim before observation is
installed. No environment, descriptor, network policy, or task-scope permission
is widened. Telemetry contains counts and process identities, never argv/env.
"""
from __future__ import annotations

from collections import Counter
import asyncio
import base64
import itertools
import json
import os
from pathlib import Path
import subprocess
import select
import selectors
import sys
import threading
import time
import zlib
from typing import Any

if sys.platform == "win32":
    import _overlapped
else:
    _overlapped = None

PREFIX = "PLAN55_PROCESS_OBSERVATION "
MARKER = "# PLAN55_OBSERVER_INSTALLED_AFTER_ADMISSION"
MAX_EVENTS = 100000
MAX_REPORT_BYTES = 16384
WAIT_KINDS = ("event_wait", "condition_wait", "asyncio_sleep", "selector_wait")
RUNTIME_WRITE_CONTEXTS = ("background_lifetime_reaper", "http_lifetime_reap",
                          "http_heartbeat", "http_request", "unattributed_runtime")
PACK_MODULE = "astrid.packs.vibecomfy.actions.run.run"
_UNSEEN = object()


class PatchSet:
    def __init__(self) -> None:
        self.saved = []

    def setattr(self, obj, key, value) -> None:
        self.saved.append((obj, key, getattr(obj, key)))
        setattr(obj, key, value)

    def undo(self) -> None:
        for obj, key, value in reversed(self.saved):
            setattr(obj, key, value)
        self.saved.clear()


def classify_command(argv: Any) -> str:
    if not isinstance(argv, (list, tuple)) or not argv:
        return "external"
    text = " ".join(str(x) for x in argv[1:])
    if "astrid.core.execution.generic_host_worker" in text or PACK_MODULE in text:
        return "pack_worker"
    if "runtime_protocol" in text or "astrid_runtime.daemon" in text:
        return "runtime"
    if "astrid.core.execution.generic_host" in text:
        return "host"
    if "-m" in argv:
        at = argv.index("-m")
        if argv[at + 1:at + 2] == ["astrid"] or tuple(argv[at + 1:at + 2]) == ("astrid",):
            return "cli"
    return "external"


def _execution_call(code: str) -> str | None:
    calls = (
        "runpy.run_module(config['module'], run_name='__main__', alter_sys=True)",
        "runpy.run_module('astrid.core.execution.generic_host_worker',run_name='__main__')",
    )
    return next((call for call in calls if code.count(call) == 1 and code.rstrip().endswith(call)), None)


def instrument_argv(argv: Any, hash_source: str, observer_source: str,
                    targets: dict[str, int], gaps: set[str]) -> Any:
    """Only the two known admitted worker wrappers and the public CLI are changed."""
    if not isinstance(argv, (list, tuple)) or not argv:
        return argv
    role = classify_command(argv)
    if role not in {"pack_worker", "cli"}:
        return argv
    values = list(argv)
    if "-c" in values:
        index = values.index("-c") + 1
        code = values[index]
        if not isinstance(code, str):
            gaps.add("non-text worker startup")
            return argv
        if MARKER in code:
            return argv
        call = _execution_call(code)
        if call is None:
            gaps.add("unrecognized admitted worker startup")
            return argv
        before = code[:code.rfind(call)]
    elif "-m" in values:
        index = values.index("-m")
        module = values[index + 1]
        if module not in {"astrid", PACK_MODULE}:
            gaps.add("unrecognized admitted module")
            return argv
        flags, arguments = values[1:index], values[index + 2:]
        values = [values[0], *flags, "-c", "", *arguments]
        index = values.index("-c") + 1
        before = f"import sys,runpy\nsys.argv=[{module!r},*sys.argv[1:]]\n"
        call = f"runpy.run_module({module!r},run_name='__main__',alter_sys=True)"
    else:
        gaps.add("unrecognized worker entrypoint")
        return argv
    # A fresh module gives dataclasses a real module identity. The source is
    # test-owned literal text, not imported from a pack-controlled search path.
    bootstrap = (
        "\n" + MARKER + "\n"
        "import types as _p55types\n"
        "_p55module=_p55types.ModuleType('_plan55_observer')\n"
        "sys.modules['_plan55_observer']=_p55module\n"
        "import base64 as _p55b64,zlib as _p55zlib\n"
        f"_p55hash_source=_p55zlib.decompress(_p55b64.b85decode({base64.b85encode(zlib.compress(hash_source.encode())).decode()!r})).decode()\n"
        f"_p55observer_source=_p55zlib.decompress(_p55b64.b85decode({base64.b85encode(zlib.compress(observer_source.encode())).decode()!r})).decode()\n"
        "exec(compile(_p55hash_source,'<plan55-hashes>','exec'),_p55module.__dict__)\n"
        "exec(compile(_p55observer_source,'<plan55-process>','exec'),_p55module.__dict__)\n"
        f"_p55meter=_p55module.ProcessObserver({targets!r},{role!r},"
        "hash_source=_p55hash_source,observer_source=_p55observer_source)\n"
        "_p55meter.start()\n"
        "try:\n    " + call + "\n"
        "finally:\n    _p55meter.emit_and_stop()\n"
    )
    values[index] = before + bootstrap
    return tuple(values) if isinstance(argv, tuple) else values


class ProcessObserver:
    def __init__(self, targets: dict[str, int], role: str, *,
                 hash_source: str, observer_source: str) -> None:
        self.targets, self.role = targets, role
        self.hash_source, self.observer_source = hash_source, observer_source
        self.patches = PatchSet()
        self.events = []
        self.gaps: set[str] = set()
        self.lock = threading.RLock()
        self.started = None
        self.previous_profile = self.previous_thread_profile = None
        self.chain_profiles = False
        self.hashes = None
        self.code_kinds = {}
        self.sleep_function = time.sleep
        self.fsync_function = getattr(os, "fsync", None)
        self.fdatasync_function = getattr(os, "fdatasync", None)
        self.select_function = select.select
        # Default Windows asyncio waits on IOCP. The exact C-call identity
        # also covers aliases and proactor shutdown/draining polls.
        self.iocp_wait_function = getattr(_overlapped, "GetQueuedCompletionStatus", None)
        self.c_wait_ids = {}
        self.wait_state = threading.local()
        self.context = threading.local()
        self.runtime_connection = None
        self.runtime_commit_code = None
        self.runtime_background_code = self.runtime_background_owner = None
        self.runtime_reap_code = self.runtime_reap_owner = None
        self.commit_starts = {}
        self.wait_ids = itertools.count()

    def event(self, kind: str, **fields) -> None:
        with self.lock:
            if len(self.events) >= MAX_EVENTS:
                self.gaps.add("Python observation event limit exceeded")
                return
            # perf_counter is a system-wide monotonic clock on supported
            # Python versions, including Windows 3.11's high-resolution QPC.
            self.events.append({"at_ns": time.perf_counter_ns(), "kind": kind,
                                "tid": threading.get_native_id(), **fields})

    def _install_waits(self):
        owner = self

        def wrap(function, kind):
            def wait(*args, **kwargs):
                # Event.wait enters Condition.wait; count the public wait once.
                nested = getattr(owner.wait_state, "depth", 0)
                owner.wait_state.depth = nested + 1
                wait_id = next(owner.wait_ids)
                if not nested:
                    owner.event(kind + "_attempt", wait_id=wait_id)
                try:
                    result = function(*args, **kwargs)
                except BaseException:
                    if not nested:
                        owner.event(kind + "_error", wait_id=wait_id)
                    raise
                else:
                    if not nested:
                        owner.event(kind + "_success", wait_id=wait_id)
                    return result
                finally:
                    owner.wait_state.depth = nested
            return wait

        self.patches.setattr(threading.Event, "wait", wrap(threading.Event.wait, "event_wait"))
        self.patches.setattr(threading.Condition, "wait", wrap(threading.Condition.wait, "condition_wait"))
        # Patch each distinct implementation, never an inherited alias twice.
        for cls in {value for value in vars(selectors).values() if isinstance(value, type)}:
            if issubclass(cls, selectors.BaseSelector) and "select" in cls.__dict__:
                if cls is not selectors.BaseSelector:
                    self.patches.setattr(cls, "select", wrap(cls.select, "selector_wait"))
        original_sleep = asyncio.sleep

        async def async_sleep(*args, **kwargs):
            # No thread-local depth survives an await: unrelated tasks share it.
            wait_id = next(owner.wait_ids)
            owner.event("asyncio_sleep_attempt", wait_id=wait_id)
            try:
                result = await original_sleep(*args, **kwargs)
            except BaseException:
                owner.event("asyncio_sleep_error", wait_id=wait_id)
                raise
            else:
                owner.event("asyncio_sleep_success", wait_id=wait_id)
                return result
        self.patches.setattr(asyncio, "sleep", async_sleep)

    def runtime_write_context(self, frame):
        """A missing HTTP route is not evidence of autonomous background work."""
        reap = False
        while frame is not None:
            if (frame.f_code is self.runtime_background_code and
                    frame.f_locals.get("self") is self.runtime_background_owner):
                return "background_lifetime_reaper"
            if (frame.f_code is self.runtime_reap_code and
                    frame.f_locals.get("self") is self.runtime_reap_owner):
                reap = True
            frame = frame.f_back
        route = getattr(self.context, "route", None)
        if route:
            if reap:
                return "http_lifetime_reap"
            return "http_heartbeat" if route.endswith("/heartbeat") else "http_request"
        return "unattributed_runtime"

    def _write_origin(self, frame):
        """Classify observed stack ancestry, never assume unmatched is background."""
        context = self.runtime_write_context(frame)
        names = []
        while frame is not None:
            filename = frame.f_code.co_filename.replace("\\", "/")
            names.append((filename, frame.f_code.co_name))
            frame = frame.f_back
        if any("/runtime_protocol/" in f and n in {
            "_stage_outputs", "_begin_cas_publication_journal", "_copy_open_file_at",
            "_publish_staged_outputs", "_discard_staged_outputs", "_remove_publication_journal_at",
        } for f, n in names):
            return "runtime_publication"
        if any("/vibecomfy/" in f for f, _ in names):
            return "vibecomfy_evidence"
        if any("/astrid/core/generation/delivery.py" in f for f, _ in names):
            return "caller_delivery"
        # Agent callers also use Runtime's shared dirfd/platform helpers. The
        # filesystem shim does not make their evidence a Runtime-server write.
        if any("/astrid/core/execution/" in f for f, _ in names):
            return "agent_host_evidence"
        if any("/astrid/sdk/" in f for f, _ in names):
            return "agent_sdk_evidence"
        if any("/astrid/" in f for f, _ in names):
            return "agent_other"
        if context == "background_lifetime_reaper":
            return "runtime_background"
        if any("/runtime_protocol/" in f for f, _ in names):
            return "runtime_other"
        return "other_application"

    def _install_writes(self):
        owner = self
        for name in ("fsync", "fdatasync"):
            original = getattr(os, name, None)
            if original is None:
                continue

            def sync(fd, _original=original, _name=name):
                frame = sys._getframe(1)
                origin = owner._write_origin(frame)
                context = owner.runtime_write_context(frame) if origin.startswith("runtime_") else origin
                started = time.time_ns()
                success = False
                try:
                    result = _original(fd)
                    success = True
                    return result
                finally:
                    owner.event("application_sync", operation=_name, origin=origin,
                                write_context=context,
                                route=getattr(owner.context, "route", None),
                                epoch_start_ns=started, epoch_end_ns=time.time_ns(), success=success)
            self.patches.setattr(os, name, sync)

    def profile(self, frame, event, arg) -> None:
        # Reject unrelated Python calls before caching/formatting code metadata.
        # Deep-copy/source-discovery loops produce many calls but none of the
        # compile/emission boundaries. This changes observer work, not coverage.
        if event == "return" and not self.chain_profiles:
            return
        if event == "call":
            code = frame.f_code
            name = code.co_name
            if name in ("compile", "emit_bundle", "emit_bundle_with_candidate"):
                # Retain code identity: equality ignores filenames.
                cached = self.code_kinds.get(id(code))
                kind = cached[1] if cached is not None else _UNSEEN
                if kind is _UNSEEN:
                    filename = code.co_filename.replace("\\", "/")
                    kind = None
                    if filename.endswith("/vibecomfy/workflow.py") and name == "compile":
                        kind = "workflow_compile"
                    elif filename.endswith("/vibecomfy/workflow_bundle.py"):
                        kind = "bundle_compile" if name == "compile" else "bundle_emission"
                    self.code_kinds[id(code)] = (code, kind)
                if kind is not None:
                    self.event(kind)
        elif event in ("c_call", "c_return", "c_exception"):
            # Identity comparisons avoid hashing every unrelated bound C method.
            kind = ("sleep" if arg is self.sleep_function else
                    "fsync" if arg is self.fsync_function else
                    "fdatasync" if arg is self.fdatasync_function else
                    "selector_wait" if (arg is self.select_function or arg is self.iocp_wait_function)
                    and not getattr(self.wait_state, "depth", 0) else None)
            if kind is not None:
                fields = {}
                if kind == "selector_wait":
                    tid = threading.get_native_id()
                    if event == "c_call":
                        self.c_wait_ids[tid] = next(self.wait_ids)
                    wait_id = self.c_wait_ids.get(tid)
                    if wait_id is not None:
                        fields["wait_id"] = wait_id
                    if event != "c_call":
                        self.c_wait_ids.pop(tid, None)
                self.event(kind + {"c_call": "_attempt", "c_return": "_success", "c_exception": "_error"}[event], **fields)
            elif (self.runtime_connection is not None and
                  (self.runtime_commit_code is None or frame.f_code is self.runtime_commit_code) and
                  getattr(arg, "__self__", None) is self.runtime_connection and
                  getattr(arg, "__name__", None) == "commit"):
                tid = threading.get_native_id()
                if event == "c_call" and self.runtime_connection.in_transaction:
                    self.commit_starts[tid] = time.time_ns()
                    self.event("runtime_outer_commit_attempt")
                elif event in ("c_return", "c_exception") and tid in self.commit_starts:
                    started = self.commit_starts.pop(tid)
                    self.event("runtime_outer_commit_" + ("success" if event == "c_return" else "error"),
                               epoch_start_ns=started, epoch_end_ns=time.time_ns(),
                               write_context=self.runtime_write_context(frame),
                               route=getattr(self.context, "route", None))
        if self.chain_profiles:
            previous = self.previous_profile if threading.get_ident() == self.start_thread else self.previous_thread_profile
            if previous is not None:
                previous(frame, event, arg)

    def start(self) -> None:
        if self.started is not None:
            raise RuntimeError("process observer already installed")
        self.started = time.perf_counter_ns()
        self.start_thread = threading.get_ident()
        self.previous_profile = sys.getprofile()
        self.previous_thread_profile = threading.getprofile()
        self.chain_profiles = self.previous_profile is not None or self.previous_thread_profile is not None
        # Embedded workers have the hash class in this module; the parent uses
        # its canonical test helper. Neither path imports or modifies a pack.
        cls = globals().get("OutputHashObserver")
        if cls is None:
            from tests._local_speed_hashes import OutputHashObserver as cls
        self.hashes = cls(self.targets).install(self.patches)
        original = subprocess.Popen.__init__
        owner = self

        def spawn(process, args, *positional, **kwargs):
            role = classify_command(args)
            observed = instrument_argv(args, owner.hash_source, owner.observer_source, owner.targets, owner.gaps)
            result = original(process, observed, *positional, **kwargs)
            owner.event("process_start", child_pid=process.pid, child_role=role)
            return result

        self.patches.setattr(subprocess.Popen, "__init__", spawn)
        self._install_waits()
        self._install_writes()
        threading.setprofile(self.profile)
        sys.setprofile(self.profile)

    def snapshot(self, since_ns: int | None = None, until_ns: int | None = None) -> dict:
        if self.started is None or self.hashes is None:
            raise RuntimeError("process observer not installed")
        with self.lock:
            rows = [row for row in self.events if (since_ns is None or row["at_ns"] >= since_ns)
                    and (until_ns is None or row["at_ns"] <= until_ns)]
            counts = Counter(row["kind"] for row in rows)
            starts = {row["wait_id"]: row for row in self.events if "wait_id" in row and row["kind"].endswith("_attempt")}
            ends = {row["wait_id"]: row["at_ns"] for row in self.events if "wait_id" in row and not row["kind"].endswith("_attempt")}
            carry = Counter(row["kind"].removesuffix("_attempt") for key, row in starts.items()
                            if since_ns is not None and row["at_ns"] < since_ns <= ends.get(key, 2**64))
            processes = [row for row in rows if row["kind"] == "process_start"]
            hashes = self.hashes.snapshot(since_ns, until_ns)
            return {
                "schema": "astrid.local-speed-process.v1", "pid": os.getpid(),
                "role": self.role, "start_ns": self.started, "end_ns": time.perf_counter_ns(),
                "interval": {"monotonic_start_ns": since_ns, "monotonic_reply_ns": until_ns},
                "counts": dict(sorted(counts.items())), "process_starts": processes,
                "wait_coverage": list(WAIT_KINDS),
                "wait_carry_in": {name: carry[name] for name in WAIT_KINDS},
                "write_spans": [row for row in rows if row["kind"] in {
                    "application_sync", "runtime_outer_commit_success", "runtime_outer_commit_error",
                    "platform_write_through_success", "platform_write_through_error",
                }],
                "output_hash_streams": hashes.completed_streams,
                "output_hash_bytes": hashes.bytes_hashed,
                "coverage_gaps": sorted(self.gaps | set(hashes.coverage_gaps)),
                "native_flushes": None,
            }

    def stop(self) -> None:
        sys.setprofile(self.previous_profile)
        threading.setprofile(self.previous_thread_profile)
        self.patches.undo()

    def emit_and_stop(self) -> None:
        try:
            report = self.snapshot()
        finally:
            self.stop()
        # Keep the existing bounded pipe message. Raw per-sync spans are very
        # repetitive; lossless compression preserves attribution and its limit.
        spans = report.pop("write_spans", [])
        report["write_spans_zlib"] = base64.b85encode(zlib.compress(json.dumps(spans).encode())).decode()
        raw = (PREFIX + json.dumps(report, sort_keys=True) + "\n").encode()
        if len(raw) > MAX_REPORT_BYTES:
            raw = (PREFIX + json.dumps({"schema": "astrid.local-speed-process.v1", "pid": os.getpid(),
                "role": self.role, "coverage_gaps": ["process report overflow"], "complete": False}) + "\n").encode()
        # Original stderr/descriptor flags remain unchanged. Missing output is
        # detected by the parent; never downgrade it to zero observations.
        try:
            os.write(2, raw)
        except OSError:
            pass


def sources() -> tuple[str, str]:
    root = Path(__file__).parent
    return (root.joinpath("_local_speed_hashes.py").read_text(),
            root.joinpath("_local_speed_process.py").read_text())


def read_reports(texts: list[str]) -> tuple[list[dict], list[str]]:
    found, gaps = {}, []
    for text in texts:
        for line in text.splitlines():
            if not line.startswith(PREFIX):
                continue
            try:
                row = json.loads(line[len(PREFIX):])
                if row.get("schema") != "astrid.local-speed-process.v1":
                    raise ValueError("wrong schema")
                if "write_spans_zlib" in row:
                    decoder = zlib.decompressobj()
                    raw = decoder.decompress(base64.b85decode(row.pop("write_spans_zlib")), 8_000_000)
                    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                        raise ValueError("invalid or oversized write attribution")
                    row["write_spans"] = json.loads(raw)
                key = (row["pid"], row.get("start_ns"))
                if key in found and found[key] != row:
                    gaps.append("conflicting process report")
                found[key] = row
            except (ValueError, KeyError, TypeError, zlib.error):
                gaps.append("malformed or truncated process report")
    return list(found.values()), gaps
