"""Test-only process observer; never install during the latency measurement.

The strict/captured worker startup is retained verbatim before observation is
installed. No environment, descriptor, network policy, or task-scope permission
is widened. Telemetry contains counts and process identities, never argv/env.
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import itertools
import json
import os
import select
import selectors
import subprocess
import sys
import threading
import time
import zlib
from collections import Counter
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    import _overlapped
else:
    _overlapped = None


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


_precise_file_time = None
if sys.platform == "win32":
    _precise_file_time = ctypes.WinDLL("kernel32", use_last_error=True).GetSystemTimePreciseAsFileTime
    _precise_file_time.argtypes = [ctypes.POINTER(_FileTime)]
    _precise_file_time.restype = None


def _windows_epoch_time_ns():
    value = _FileTime()
    _precise_file_time(ctypes.byref(value))
    return (((value.high << 32) | value.low) - 116444736000000000) * 100


# Python 3.11's Windows time.time_ns uses coarse GetSystemTimeAsFileTime.
# Match precise ETW UTC timestamps at the actual boundary, without padding.
epoch_time_ns = _windows_epoch_time_ns if sys.platform == "win32" else time.time_ns
EPOCH_CLOCK = ("GetSystemTimePreciseAsFileTime()" if sys.platform == "win32"
               else time.get_clock_info("time").implementation)

PREFIX = "PLAN55_PROCESS_OBSERVATION "
MARKER = "# PLAN55_OBSERVER_INSTALLED_AFTER_ADMISSION"
MAX_EVENTS = 100000
MAX_REPORT_BYTES = 16384
WAIT_KINDS = ("event_wait", "condition_wait", "asyncio_sleep", "selector_wait")
RUNTIME_WRITE_CONTEXTS = ("background_lifetime_reaper", "http_lifetime_reap",
                          "http_heartbeat", "http_request", "unattributed_runtime")
PACK_MODULE = "astrid.packs.vibecomfy.actions.run.run"
CENSUS_PRODUCER = "astrid.core.execution.process_group:_process_snapshot"
SDK_PRODUCER = "astrid.sdk.invocation:_wait_for_kernel_task_loop"
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
        self.wait_frames = {}
        self.wait_codes = {}
        self.invocation_ids = itertools.count()
        self.popen_poll_code = subprocess.Popen.poll.__code__
        self.popen_try_wait_code = getattr(getattr(subprocess.Popen, "_try_wait", None), "__code__", None)
        self.sleep_wrapper_code = self.poll_wrapper_code = None
        self.poll_ids = itertools.count()
        self.run_code = subprocess.run.__code__
        self.popen_wait_code = subprocess.Popen._wait.__code__
        self.census_ids = itertools.count()
        self.census_frames = {}
        self.census_wait_frames = {}
        self.sdk_frames = {}
        self.sdk_show_frames = {}
        self.sdk_sleep_frames = {}

    def _sdk_poll_end(self, frame, context):
        if context["poll_id"] is not None:
            state = frame.f_locals.get("state")
            self.event("sdk_poll_end", sdk_invocation=context["invocation"],
                       poll_id=context["poll_id"], state=state if type(state) is str else None)

    def _sdk_return(self, frame, arg):
        poll_id = self.sdk_show_frames.pop(id(frame), None)
        if poll_id is not None:
            # A Python exception also produces a return event, with None.
            # The SDK cannot treat a missing task-status result as a poll.
            self.event("sdk_show_result", poll_id=poll_id, result_known=arg is not None)
        context = self.sdk_frames.pop(id(frame), None)
        if context is not None:
            self._sdk_poll_end(frame, context)
            known = type(arg) is tuple and len(arg) == 3 and type(arg[1]) is bool
            self.event("sdk_wait_end", sdk_invocation=context["invocation"], result_known=known)

    def _sdk_poll_start(self, parent, show_frame):
        context = self.sdk_frames.get(id(parent))
        if context is None:
            return
        self._sdk_poll_end(parent, context)
        poll_id = next(self.poll_ids)
        context["poll_id"] = poll_id
        self.sdk_show_frames[id(show_frame)] = poll_id
        self.event("sdk_poll_start", sdk_invocation=context["invocation"], poll_id=poll_id)

    def _census_context(self, frame):
        while frame is not None:
            invocation = self.census_frames.get(id(frame))
            if invocation is not None:
                return invocation
            frame = frame.f_back
        return None

    @staticmethod
    def _child_pid(process):
        try:
            value = object.__getattribute__(process, "__dict__").get("pid")
        except (AttributeError, TypeError):
            return None
        return value if type(value) is int and value > 0 else None

    def _census_profile(self, frame, event, arg):
        """Observe the actual run/_try_wait code, including preinstalled aliases."""
        code = frame.f_code
        if code is self.run_code:
            if event == "call" and frame.f_back is not None and self._site(frame.f_back) == CENSUS_PRODUCER:
                invocation = next(self.census_ids)
                self.census_frames[id(frame)] = invocation
                args = frame.f_locals.get("popenargs", ())
                command = args[0] if len(args) == 1 else None
                known = (type(command) in (list, tuple) and len(command) == 3
                         and all(type(value) is str for value in command)
                         and command[0] in {"/bin/ps", "/usr/bin/ps", "ps"}
                         and tuple(command[1:]) == ("-axo", "pid=,ppid=,pgid=,lstart="))
                self.event("census_start", census_invocation=invocation, command_known=known)
            elif event == "return":
                invocation = self.census_frames.pop(id(frame), None)
                if invocation is not None:
                    result = vars(arg).get("returncode") if isinstance(arg, subprocess.CompletedProcess) else None
                    self.event("census_end", census_invocation=invocation,
                               returncode=result if type(result) is int else None)
        elif code is self.popen_try_wait_code:
            if event == "call":
                invocation = self._census_context(frame)
                if invocation is not None:
                    poll_id = next(self.poll_ids)
                    self.census_wait_frames[id(frame)] = poll_id
                    self.event("census_wait_start", census_invocation=invocation, poll_id=poll_id,
                               child_pid=self._child_pid(frame.f_locals.get("self")))
            elif event == "return":
                poll_id = self.census_wait_frames.pop(id(frame), None)
                if poll_id is not None:
                    known = type(arg) is tuple and len(arg) == 2 and all(type(value) is int for value in arg)
                    self.event("census_wait_result", poll_id=poll_id, result_known=known,
                               waited_pid=arg[0] if known else None)

    def event(self, kind: str, **fields) -> dict | None:
        with self.lock:
            if len(self.events) >= MAX_EVENTS:
                self.gaps.add("Python observation event limit exceeded")
                return
            # perf_counter is a system-wide monotonic clock on supported
            # Python versions, including Windows 3.11's high-resolution QPC.
            row = {"at_ns": time.perf_counter_ns(), "kind": kind,
                   "tid": threading.get_native_id(),
                   "runtime_scope": getattr(self.context, "scope", None), **fields}
            self.events.append(row)
            return row

    @staticmethod
    def _site(frame):
        return frame.f_globals.get("__name__", "<unknown>") + ":" + frame.f_code.co_qualname

    def _wait_fields(self, frame):
        code_id = id(frame.f_code)
        self.wait_codes[code_id] = frame.f_code
        key = (id(frame), code_id)
        if key not in self.wait_frames:
            self.wait_frames[key] = next(self.invocation_ids)
        return {"wait_site": self._site(frame), "wait_frame": id(frame),
                "wait_invocation": self.wait_frames[key]}

    def _poll_progress(self, frame):
        self.event("poll_progress", **self._wait_fields(frame))

    def _install_waits(self):
        owner = self

        def sleep(*args, **kwargs):
            # The C-call profile hook still records the actual operation. This
            # transparent frame exposes its argument without evaluating a
            # caller's conversion hooks or changing the underlying sleep.
            return owner.sleep_function(*args, **kwargs)

        self.sleep_wrapper_code = sleep.__code__
        self.patches.setattr(time, "sleep", sleep)

        original_poll = subprocess.Popen.poll

        def poll(process, *args, **kwargs):
            caller = sys._getframe(1)
            fields = owner._wait_fields(caller)
            del caller
            try:
                child_pid = object.__getattribute__(process, "__dict__").get("pid")
            except AttributeError:
                child_pid = None
            if type(child_pid) is not int or child_pid <= 0:
                child_pid = None
            poll_id = next(owner.poll_ids)
            owner.event("poll_progress", poll_id=poll_id, child_pid=child_pid, **fields)
            try:
                result = original_poll(process, *args, **kwargs)
            except BaseException:
                owner.event("poll_error", poll_id=poll_id)
                raise
            else:
                known = result is None or type(result) is int
                owner.event("poll_result", poll_id=poll_id,
                            result_known=known, returncode=result if known else None)
                return result

        self.poll_wrapper_code = poll.__code__
        self.patches.setattr(subprocess.Popen, "poll", poll)

        def wrap(function, kind):
            def wait(*args, **kwargs):
                # Event.wait enters Condition.wait; count the public wait once.
                nested = getattr(owner.wait_state, "depth", 0)
                owner.wait_state.depth = nested + 1
                wait_id = next(owner.wait_ids)
                if not nested:
                    owner.event(kind + "_attempt", wait_id=wait_id, **owner._wait_fields(sys._getframe(1)))
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
            owner.event("asyncio_sleep_attempt", wait_id=wait_id, **owner._wait_fields(sys._getframe(1)))
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
                started = epoch_time_ns()
                success = False
                try:
                    result = _original(fd)
                    success = True
                    return result
                finally:
                    owner.event("application_sync", operation=_name, origin=origin,
                                write_context=context,
                                route=getattr(owner.context, "route", None),
                                epoch_start_ns=started, epoch_end_ns=epoch_time_ns(), success=success)
            self.patches.setattr(os, name, sync)

    def profile(self, frame, event, arg) -> None:
        # Reject unrelated Python calls before caching/formatting code metadata.
        # Deep-copy/source-discovery loops produce many calls but none of the
        # compile/emission boundaries. This changes observer work, not coverage.
        if event in {"call", "return"} and (frame.f_code is self.run_code or frame.f_code is self.popen_try_wait_code):
            self._census_profile(frame, event, arg)
        if event == "return" and frame.f_code.co_name in {"show", "_wait_for_kernel_task_loop"}:
            self._sdk_return(frame, arg)
        if event == "return" and not self.chain_profiles:
            return
        if event == "call":
            code = frame.f_code
            name = code.co_name
            if id(code) in self.wait_codes:
                self.wait_frames[(id(frame), id(code))] = next(self.invocation_ids)
            if (name == "_wait_for_kernel_task_loop" and self.role == "parent_runtime_host_sdk"
                    and self._site(frame) == SDK_PRODUCER):
                invocation = self._wait_fields(frame)["wait_invocation"]
                self.sdk_frames[id(frame)] = {"invocation": invocation, "poll_id": None}
                task_id = frame.f_locals.get("task_id")
                self.event("sdk_wait_start", sdk_invocation=invocation,
                           task_id=task_id if type(task_id) is str else None)
            # A poll's work and its wait are different quantities. Record the
            # exact progress boundary so an extra sleep inside one iteration
            # cannot disappear into a whole-workload scheduling allowance.
            parent = frame.f_back if name in {"poll", "show", "_try_wait", "_observe_machines", "service_actions"} else None
            if (code is self.popen_poll_code or code is self.popen_try_wait_code) and parent is not None:
                # The wrapper supplies the original caller and one result.
                # Aliases captured before installation still retain the
                # original code-identity progress observation here.
                if parent.f_code is not self.poll_wrapper_code:
                    self._poll_progress(parent)
            elif name == "show" and parent is not None and parent.f_code.co_name == "_wait_for_kernel_task_loop":
                show = parent.f_locals.get("show")
                if code is getattr(getattr(show, "__func__", show), "__code__", None):
                    self._poll_progress(parent)
                    self._sdk_poll_start(parent, frame)
            elif (name == "_observe_machines" and parent is not None
                  and parent.f_code.co_qualname == "GenericPackHost._run_claim_slots"
                  and frame.f_globals.get("__name__") == "astrid.core.execution.generic_host"):
                self._poll_progress(parent)
            elif (name == "service_actions" and parent is not None
                  and parent.f_code.co_qualname == "BaseServer.serve_forever"
                  and parent.f_globals.get("__name__") == "socketserver"):
                self._poll_progress(parent)
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
                if event == "c_call" and kind in {"sleep", "selector_wait"}:
                    caller = frame
                    if kind == "sleep" and frame.f_code is self.sleep_wrapper_code:
                        caller = frame.f_back or frame
                        args = frame.f_locals.get("args", ())
                        kwargs = frame.f_locals.get("kwargs", {})
                        value = args[0] if len(args) == 1 and not kwargs else None
                        # Unsupported values remain unknown; delegate all
                        # validation/conversion/exception behavior to sleep.
                        fields["requested_sleep_ns"] = (
                            round(value * 1_000_000_000)
                            if type(value) in (int, float) and 0 <= value <= 9_000_000_000
                            else None)
                    fields.update(self._wait_fields(caller))
                    if kind == "sleep" and id(caller) in self.sdk_frames:
                        fields["sdk_sleep_id"] = next(self.wait_ids)
                        self.sdk_sleep_frames[(threading.get_native_id(), id(frame))] = dict(fields)
                    if kind == "sleep" and caller.f_code is self.popen_wait_code:
                        invocation = self._census_context(caller)
                        if invocation is not None:
                            fields["census_invocation"] = invocation
                            fields["census_child_pid"] = self._child_pid(caller.f_locals.get("self"))
                if kind == "sleep" and event != "c_call":
                    fields.update(self.sdk_sleep_frames.pop((threading.get_native_id(), id(frame)), {}))
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
                  getattr(arg, "__self__", None) is self.runtime_connection and
                  getattr(arg, "__name__", None) == "commit"):
                tid = threading.get_native_id()
                if event == "c_call":
                    self.event("runtime_commit_call_attempt")
                    if not self.runtime_connection.in_transaction:
                        self.event("runtime_commit_without_transaction")
                if event == "c_call" and self.runtime_connection.in_transaction:
                    self.commit_starts[tid] = epoch_time_ns()
                    self.event("runtime_outer_commit_attempt")
                elif event in ("c_return", "c_exception") and tid in self.commit_starts:
                    started = self.commit_starts.pop(tid)
                    self.event("runtime_outer_commit_" + ("success" if event == "c_return" else "error"),
                               epoch_start_ns=started, epoch_end_ns=epoch_time_ns(),
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
            owner.event("process_start", child_pid=process.pid, child_role=role,
                        census_invocation=owner._census_context(sys._getframe(1)))
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
            wait_rows = [row for row in rows if "wait_site" in row and row["kind"].endswith("_attempt")]
            wait_sites = Counter(row["kind"] + "|" + row["wait_site"] for row in wait_rows)
            per_invocation = Counter((row["wait_invocation"], row["kind"] + "|" + row["wait_site"]) for row in wait_rows)
            invocation_maxima = {}
            for (_, site), count in per_invocation.items():
                invocation_maxima[site] = max(invocation_maxima.get(site, 0), count)
            cycles, cycle_maxima, poll_sites = {}, {}, set()
            for row in rows:
                if row["kind"] == "poll_progress":
                    key = (row["tid"], row["wait_frame"])
                    cycles[key] = Counter()
                    poll_sites.add(row["wait_site"])
                elif "wait_site" in row and row["kind"].endswith("_attempt"):
                    key = (row["tid"], row["wait_frame"])
                    if key in cycles:
                        site = row["kind"] + "|" + row["wait_site"]
                        cycles[key][site] += 1
                        cycle_maxima[site] = max(cycle_maxima.get(site, 0), cycles[key][site])
            hashes = self.hashes.snapshot(since_ns, until_ns)
            return {
                "schema": "astrid.local-speed-process.v1", "pid": os.getpid(),
                "epoch_clock": EPOCH_CLOCK,
                "role": self.role, "start_ns": self.started, "end_ns": time.perf_counter_ns(),
                "interval": {"monotonic_start_ns": since_ns, "monotonic_reply_ns": until_ns},
                "counts": dict(sorted(counts.items())), "process_starts": processes,
                "wait_coverage": list(WAIT_KINDS),
                "wait_carry_in": {name: carry[name] for name in WAIT_KINDS},
                "wait_sites": dict(sorted(wait_sites.items())),
                "wait_invocation_maxima": dict(sorted(invocation_maxima.items())),
                "poll_sites": sorted(poll_sites),
                "poll_wait_maxima": dict(sorted(cycle_maxima.items())),
                "sleep_requests": self._sleep_requests(wait_rows),
                "poll_windows": self._poll_windows(rows, since_ns, until_ns),
                "census_invocations": self._census_invocations(since_ns, until_ns),
                "sdk_wait_invocations": self._sdk_invocations(since_ns, until_ns),
                "write_spans": [row for row in rows if row["kind"] in {
                    "application_sync", "runtime_outer_commit_success", "runtime_outer_commit_error",
                    "platform_write_through_success", "platform_write_through_error",
                }],
                "output_hash_streams": hashes.completed_streams,
                "output_hash_bytes": hashes.bytes_hashed,
                "coverage_gaps": sorted(self.gaps | set(hashes.coverage_gaps)),
                "native_flushes": None,
            }

    @staticmethod
    def _sleep_requests(rows):
        result = {}
        for row in rows:
            if row["kind"] != "sleep_attempt" or "wait_site" not in row:
                continue
            key = "sleep_attempt|" + row["wait_site"]
            item = result.setdefault(key, {"calls": 0, "observed_calls": 0,
                                           "unknown_calls": 0, "minimum_ns": None, "maximum_ns": None})
            item["calls"] += 1
            value = row.get("requested_sleep_ns")
            if value is None:
                item["unknown_calls"] += 1
            else:
                item["observed_calls"] += 1
                item["minimum_ns"] = value if item["minimum_ns"] is None else min(item["minimum_ns"], value)
                item["maximum_ns"] = value if item["maximum_ns"] is None else max(item["maximum_ns"], value)
        return dict(sorted(result.items()))

    def _census_invocations(self, since_ns, until_ns):
        """Join each census call to its child and the actual wait results."""
        endings = {row["census_invocation"]: row for row in self.events if row["kind"] == "census_end"}
        results = {row["poll_id"]: row for row in self.events if row["kind"] == "census_wait_result"}
        records = []
        for start in self.events:
            if start["kind"] != "census_start":
                continue
            invocation = start["census_invocation"]
            finish = endings.get(invocation)
            end = finish["at_ns"] if finish is not None else None
            if ((until_ns is not None and start["at_ns"] > until_ns)
                    or (since_ns is not None and end is not None and end < since_ns)):
                continue
            rows = [row for row in self.events if row.get("census_invocation") == invocation
                    and row["tid"] == start["tid"]]
            children = [row["child_pid"] for row in rows if row["kind"] == "process_start"]
            child = children[0] if len(children) == 1 else None
            waits = [row for row in rows if row["kind"] == "census_wait_start"]
            replies = [results.get(row["poll_id"]) for row in waits]
            sleeps = [row for row in rows if row["kind"] == "sleep_attempt"]
            known = [reply is not None and reply["result_known"]
                     and reply["waited_pid"] in {0, child}
                     and reply["at_ns"] >= wait["at_ns"]
                     and end is not None and reply["at_ns"] <= end
                     for wait, reply in zip(waits, replies)]
            records.append({
                "invocation_id": invocation, "producer": CENSUS_PRODUCER, "tid": start["tid"],
                "child_pid": child, "spawned_children": len(children),
                "start_ns": start["at_ns"], "end_ns": end,
                "carry_in": since_ns is not None and start["at_ns"] < since_ns,
                "carry_out": end is None or (until_ns is not None and end > until_ns),
                "command_known": start["command_known"],
                "returncode": finish["returncode"] if finish is not None else None,
                "wait_calls": len(waits), "missing_wait_results": known.count(False),
                "nonterminal_waits": sum(valid and reply["waited_pid"] == 0 for valid, reply in zip(known, replies)),
                "terminal_waits": sum(valid and reply["waited_pid"] == child for valid, reply in zip(known, replies)),
                "sleep_calls": len(sleeps), "sleep_requests": self._sleep_requests(sleeps),
                "ambiguous_children": (child is None or any(wait["child_pid"] != child for wait in waits)
                                       or any(sleep.get("census_child_pid") != child for sleep in sleeps)),
            })
        return records

    def _sdk_invocations(self, since_ns, until_ns):
        """Keep each SDK poll and its sleeps, including incomplete boundaries."""
        records = []
        for start in self.events:
            if start["kind"] != "sdk_wait_start":
                continue
            invocation, tid = start["sdk_invocation"], start["tid"]
            rows = [row for row in self.events if row.get("sdk_invocation") == invocation and row["tid"] == tid]
            endings = [row for row in rows if row["kind"] == "sdk_wait_end"]
            finish = endings[0] if len(endings) == 1 else None
            end = finish["at_ns"] if finish is not None else None
            if ((until_ns is not None and start["at_ns"] > until_ns)
                    or (since_ns is not None and end is not None and end < since_ns)):
                continue
            sleeps = [row for row in self.events if row["kind"] == "sleep_attempt"
                      and row.get("wait_invocation") == invocation and row["tid"] == tid
                      and row.get("wait_site") == SDK_PRODUCER]
            polls = []
            for call in (row for row in rows if row["kind"] == "sdk_poll_start"):
                poll_id = call["poll_id"]
                endings = [row for row in rows if row["kind"] == "sdk_poll_end" and row["poll_id"] == poll_id]
                ending = endings[0] if len(endings) == 1 else None
                poll_end = ending["at_ns"] if ending is not None else None
                results = [row for row in self.events if row["kind"] == "sdk_show_result" and row["poll_id"] == poll_id]
                known = (len(results) == 1 and results[0]["result_known"]
                         and results[0]["tid"] == tid and poll_end is not None
                         and call["at_ns"] <= results[0]["at_ns"] <= poll_end)
                waits = [row for row in sleeps if row["at_ns"] >= call["at_ns"]
                         and (poll_end is None or row["at_ns"] <= poll_end)]
                missing_sleeps = 0
                for wait in waits:
                    replies = [row for row in self.events if row["kind"] in {"sleep_success", "sleep_error"}
                               and row.get("sdk_sleep_id") == wait.get("sdk_sleep_id") and row["tid"] == tid]
                    if ("sdk_sleep_id" not in wait or len(replies) != 1 or replies[0]["kind"] != "sleep_success"
                            or poll_end is None or not wait["at_ns"] <= replies[0]["at_ns"] <= poll_end):
                        missing_sleeps += 1
                polls.append({"poll_id": poll_id, "start_ns": call["at_ns"], "end_ns": poll_end,
                              "state": ending["state"] if ending is not None else None,
                              "result_known": known, "sleep_calls": len(waits),
                              "sleep_requests": self._sleep_requests(waits),
                              "missing_sleep_results": missing_sleeps})
            records.append({"producer": SDK_PRODUCER, "invocation_id": invocation, "tid": tid,
                            "task_id": start["task_id"], "start_ns": start["at_ns"], "end_ns": end,
                            "carry_in": since_ns is not None and start["at_ns"] < since_ns,
                            "carry_out": end is None or (until_ns is not None and end > until_ns),
                            "result_known": finish is not None and finish["result_known"],
                            "polls": polls, "sleep_calls": len(sleeps)})
        return records

    def _poll_windows(self, rows, since_ns, until_ns):
        """Retain observed windows; missing results are never completed polls."""
        results = {row["poll_id"]: row for row in self.events
                   if row["kind"] in {"poll_result", "poll_error"}}
        groups = {}
        for row in self.events:
            if row["kind"] == "poll_progress" and "poll_id" in row:
                key = (row["tid"], row["wait_invocation"], row["child_pid"])
                groups.setdefault(key, []).append(row)
        peers = Counter((tid, invocation) for tid, invocation, _ in groups)
        windows = []
        for (tid, invocation, pid), calls in groups.items():
            replies = [results.get(call["poll_id"]) for call in calls]
            terminals = [reply for reply in replies if reply is not None and reply["kind"] == "poll_result"
                         and reply["result_known"] and reply["returncode"] is not None]
            start = calls[0]["at_ns"]
            end = terminals[-1]["at_ns"] if terminals else None
            if (until_ns is not None and start > until_ns) or (since_ns is not None and end is not None and end < since_ns):
                continue
            interval_start = max(start, since_ns) if since_ns is not None else start
            interval_end = min(end, until_ns) if end is not None and until_ns is not None else end or until_ns
            interval_calls = [call for call in calls if call["at_ns"] >= interval_start
                              and (interval_end is None or call["at_ns"] <= interval_end)]
            interval_replies = [results.get(call["poll_id"]) for call in interval_calls]
            interval_replies = [reply if reply is not None and (interval_end is None or reply["at_ns"] <= interval_end)
                                else None for reply in interval_replies]
            sleeps = [row for row in rows if row["kind"] == "sleep_attempt"
                      and row.get("wait_invocation") == invocation and row["tid"] == tid
                      and row["at_ns"] >= interval_start and (interval_end is None or row["at_ns"] <= interval_end)]
            missing = sum(reply is None or reply["kind"] != "poll_result" or not reply.get("result_known")
                          for reply in interval_replies)
            overlap = peers[(tid, invocation)] != 1
            windows.append({
                "wait_site": calls[0]["wait_site"], "wait_invocation": invocation,
                "pid": pid, "tid": tid, "poll_start_ns": start, "poll_end_ns": end,
                "interval_start_ns": interval_start, "interval_end_ns": interval_end,
                "duration_ns": interval_end - interval_start if interval_end is not None else None,
                "carry_in": since_ns is not None and start < since_ns,
                "carry_out": end is None or (until_ns is not None and end > until_ns),
                "poll_calls": len(interval_calls), "missing_results": missing,
                "nonterminal_polls": sum(reply is not None and reply["kind"] == "poll_result"
                                          and reply["result_known"] and reply["returncode"] is None for reply in interval_replies),
                "terminal_polls": sum(reply is not None and reply["kind"] == "poll_result"
                                      and reply["result_known"] and reply["returncode"] is not None for reply in interval_replies),
                "ambiguous_children": overlap,
                "sleep_calls": None if overlap else len(sleeps),
                "sleep_requests": None if overlap else self._sleep_requests(sleeps),
            })
        return windows

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
