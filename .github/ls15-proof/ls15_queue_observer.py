"""External, opt-in observation for one unchanged LS-15 managed-audio test.

This file is a prepared diagnostic, not a product/test patch or qualification.
No retry, explicit sleep, timeout, input or error-handling policy is added.
Only parent-process method boundaries are observed; times are outer bounds.
"""
from __future__ import annotations

import ast
import dis
import os
import inspect
import json
import re
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

SELECTOR = (
    "tests/integrations/test_runpod_slot_path_cpu.py::"
    "test_capability_reaches_generation_on_each_target[audio/music-local-managed]"
)
HEADS = {
    "base": "e379565641414bc183cd02edebab1cbf77617931",
    "candidate": "c54a6099cf74c6f37e96ff36be906c3f4b188bee",
}
MAX_EVENTS = 2048
MAX_BYTES = 1024 * 1024
MAX_CHAIN = 12
MAX_ALIASES = 128
MAX_THREADS = 64
_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,159}\Z")


def exception_types(error):
    """Bounded type graph, including URLError.reason only if it is an exception."""
    pending = [(None, "raised", error)]
    seen, rows = set(), []
    while pending and len(rows) < MAX_CHAIN:
        parent, relation, current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        name = f"{type(current).__module__}.{type(current).__qualname__}"
        row = {"parent": parent, "relation": relation,
               "type": name if _TYPE.fullmatch(name) else "redacted.ExceptionType"}
        index = len(rows)
        rows.append(row)
        cause, context = current.__cause__, current.__context__
        if cause is not None:
            pending.append((index, "cause", cause))
        elif not current.__suppress_context__ and context is not None:
            pending.append((index, "context", context))
        reason = vars(current).get("reason")
        if isinstance(reason, BaseException):
            pending.append((index, "reason", reason))
    return {"nodes": rows, "truncated": bool(pending)}


class Recorder:
    """In-memory boundaries; lock is never held across a product call."""

    def __init__(self, *, max_events=MAX_EVENTS):
        self.origin = time.monotonic_ns()
        self.lock = threading.Lock()
        self.local = threading.local()
        self.events = []
        self.max_events = max_events
        self.calls = 0
        self.dropped = 0
        self.errors = []
        self.threads = {}
        self.aliases = {}
        self.first_queue_failure = None
        self.reports = []
        self.restored = False

    def alias(self, kind, value):
        # Raw task/attempt/install/endpoint values never leave memory.
        if type(value) is not str:
            return None
        with self.lock:
            key = (kind, value)
            if key not in self.aliases:
                if len(self.aliases) >= MAX_ALIASES:
                    if "observer_alias_limit" not in self.errors:
                        self.errors.append("observer_alias_limit")
                    return None
                self.aliases[key] = f"{kind}-{len(self.aliases) + 1}"
            return self.aliases[key]

    def problem(self, error):
        # Diagnostic errors invalidate this observation, never mask product errors.
        name = error if type(error) is str else type(error).__name__
        with self.lock:
            if len(self.errors) < 16:
                self.errors.append(name if _TYPE.fullmatch(name) else "ObserverError")

    def _event(self, call, operation, phase, fields):
        with self.lock:
            thread = threading.get_ident()
            if thread not in self.threads:
                if len(self.threads) >= MAX_THREADS:
                    if "observer_thread_limit" not in self.errors:
                        self.errors.append("observer_thread_limit")
                else:
                    self.threads[thread] = f"thread-{len(self.threads) + 1}"
            row = {"sequence": len(self.events) + self.dropped + 1,
                   "elapsed_ns": time.monotonic_ns() - self.origin,
                   "thread": self.threads.get(thread, "thread-unmapped"), "call": call,
                   "operation": operation, "phase": phase, **fields}
            if operation == "queue" and phase == "raise" and self.first_queue_failure is None:
                self.first_queue_failure = call
                row["first_queue_failure"] = True
            if len(self.events) < self.max_events:
                self.events.append(row)
            else:
                self.dropped += 1

def _uncertain(instance):
    value = vars(instance).get("uncertain")
    return value if type(value) is bool else None


def _identity(recorder, instance):
    data = vars(instance)
    report = data.get("report")
    return {"task": recorder.alias("task", data.get("task")),
            "attempt": recorder.alias("attempt", data.get("attempt")),
            "installation": recorder.alias("installation", data.get("root")),
            "endpoint": recorder.alias("endpoint", report.get("endpoint"))
            if type(report) is dict else None}


def fixture_stop_sites(function):
    """Classify only the two actual calls in the unchanged generator source."""
    source, start = inspect.getsourcelines(function)
    tree = ast.parse(textwrap.dedent("".join(source)))
    calls = sorted(node.lineno + start - 1 for node in ast.walk(tree)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr == "stop_owned")
    yields = [node.lineno + start - 1 for node in ast.walk(tree)
              if isinstance(node, (ast.Yield, ast.YieldFrom))]
    if len(calls) != 2 or len(yields) != 1 or not calls[0] < yields[0] < calls[1]:
        raise ValueError("unexpected_owned_peer_stop_sites")
    return {calls[0]: "fixture_before_yield", calls[1]: "fixture_exit"}


def frame_tracer(recorder, methods, peer_code, sites):
    """Observe only selected CPython frames. Test and product functions keep their original identities."""
    parent_pid = os.getpid()
    active = True

    def trace(frame, event, arg):
        operation = methods.get(frame.f_code)
        if operation is None or event != "call" or not active or os.getpid() != parent_pid:
            return None
        try:
            values = frame.f_locals
            instance = values["self"]
            fields = {}
            if operation == "queue":
                if values["path"] != "/queue":
                    return None
                target = vars(instance).get("target")
                fields["method"] = "GET" if values["payload"] is None else "POST"
                fields["endpoint"] = recorder.alias("endpoint", vars(target).get("url"))
            elif operation == "owned_peer_stop":
                caller = frame.f_back
                fields["site"] = (sites.get(caller.f_lineno, "fixture_unrecognized")
                                  if caller and caller.f_code is peer_code else "other_original_stop")
                identity = vars(instance).get("identity")
                fields["installation"] = recorder.alias("installation", identity.get("root")
                                                         if type(identity) is dict else None)
            elif operation == "settlement":
                fields["task"] = recorder.alias("task", values["task_id"])
                fields["attempt"] = recorder.alias("attempt", values["attempt_id"])
            else:
                owner = values["preparation"] if operation == "retained_finish" else instance
                fields.update(_identity(recorder, owner))
                fields["uncertain"] = _uncertain(instance)
                if operation == "retained_finish":
                    drained = values["worker_drained"]
                    fields["worker_drained"] = drained if type(drained) is bool else None

            previous = getattr(recorder.local, "calls", ())
            with recorder.lock:
                recorder.calls += 1
                call = recorder.calls
            fields["parents"] = list(previous)
            recorder._event(call, operation, "enter", fields)
            recorder.local.calls = (*previous, call)
            frame.f_trace_lines = False
            last_exception = None
        except Exception as error:
            recorder.problem(error)
            return None

        def local(frame, event, arg):
            nonlocal last_exception
            if not active or os.getpid() != parent_pid:
                return None
            try:
                if event == "exception":
                    # Never retain exception objects/tracebacks in the recorder.
                    last_exception = exception_types(arg[1])
                elif event == "return":
                    final = {}
                    if operation in {"retained_finish", "retained_close", "managed_close"}:
                        final["uncertain"] = _uncertain(frame.f_locals["self"])
                    # CPython 3.11 emits a return event both for normal RETURN_VALUE
                    # (including an explicit None) and for exceptional unwind.
                    normal = dis.opname[frame.f_code.co_code[frame.f_lasti]] == "RETURN_VALUE"
                    if not normal:
                        if last_exception is None:
                            recorder.problem("missing_unwind_exception")
                        final["last_exception_types"] = last_exception
                    recorder._event(call, operation, "return" if normal else "raise", final)
            except Exception as error:
                recorder.problem(error)
            finally:
                if event == "return":
                    recorder.local.calls = previous
            return local
        return local

    def stop():
        nonlocal active
        active = False

    return trace, stop


def install(recorder, side):
    from astrid.core.execution.generic_host import RuntimeProtocolClient
    from astrid.core.generation.backends.comfy_target import ComfyTargetClient
    from astrid.core.generation.engine_custody import LinuxEngineServiceClient
    from astrid.core.generation.managed_preparation import ManagedEnginePreparation
    from tests.integrations._pinned_source import owned_cpu_peer

    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 11):
        raise pytest.UsageError("LS15 requires CPython 3.11 for unwind/return discrimination")
    if sys.gettrace() is not None or threading.gettrace() is not None:
        raise pytest.UsageError("LS15 requires no preexisting tracing hook")
    methods = {
        ComfyTargetClient.request_json.__code__: "queue",
        LinuxEngineServiceClient.stop_owned.__code__: "owned_peer_stop",
        RuntimeProtocolClient.settle.__code__: "settlement",
        ManagedEnginePreparation.close.__code__: "managed_close",
    }
    if side == "candidate":
        from astrid.core.generation.retained_preparation import RetainedPreparation
        methods[RetainedPreparation.finish.__code__] = "retained_finish"
        methods[RetainedPreparation.close.__code__] = "retained_close"
    peer = owned_cpu_peer.__wrapped__
    sites = fixture_stop_sites(peer)
    trace, stop = frame_tracer(recorder, methods, peer.__code__, sites)
    # CPython 3.11 supports the current thread plus threads started after installation.
    threading.settrace(trace)
    try:
        sys.settrace(trace)
    except BaseException:
        stop()
        threading.settrace(None)
        raise

    def restore():
        stop()
        if sys.gettrace() is trace and threading.gettrace() is trace:
            sys.settrace(None)
            threading.settrace(None)
            recorder.restored = True
        else:
            recorder.problem("trace_hook_replaced")
            if sys.gettrace() is trace:
                sys.settrace(None)
            if threading.gettrace() is trace:
                threading.settrace(None)
    return restore, sites


def pytest_addoption(parser):
    group = parser.getgroup("ls15 bounded observation")
    group.addoption("--ls15-observe-side", choices=tuple(HEADS), default=None)
    group.addoption("--ls15-observe-output", default=None)


def pytest_collection_finish(session):
    side = session.config.getoption("--ls15-observe-side")
    output = session.config.getoption("--ls15-observe-output")
    if side is None or output is None:
        raise pytest.UsageError("LS15 observer requires side and fresh external output")
    if [item.nodeid for item in session.items] != [SELECTOR]:
        raise pytest.UsageError("LS15 observer requires the single unchanged exact selector")
    root = Path(str(session.config.rootpath)).resolve()
    destination = Path(output).resolve()
    if destination.is_relative_to(root) or destination.exists() or not destination.parent.is_dir():
        raise pytest.UsageError("LS15 output must be a new file in an existing external directory")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    clean = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=root, check=False)
    if head != HEADS[side] or clean.returncode != 0:
        raise pytest.UsageError("LS15 observer refuses a different head or tracked source change")
    session.config._ls15_observation = {
        "side": side, "head": head, "output": destination,
        "recorder": Recorder(), "stop_sites": None,
    }


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    state = item.config._ls15_observation
    recorder = state["recorder"]
    restore, sites = install(recorder, state["side"])
    state["stop_sites"] = sites
    try:
        yield
    finally:
        restore()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    state = getattr(item.config, "_ls15_observation", None)
    if state is not None:
        state["recorder"].reports.append({"phase": report.when, "outcome": report.outcome})


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    yield
    state = getattr(session.config, "_ls15_observation", None)
    if state is None:
        return
    recorder = state["recorder"]
    coverage = {
        "queue_call": any(row["operation"] == "queue" and row["phase"] == "enter"
                          for row in recorder.events),
        "fixture_before_yield_stop": any(row.get("site") == "fixture_before_yield"
                                         for row in recorder.events),
        "fixture_exit_stop": any(row.get("site") == "fixture_exit"
                                 for row in recorder.events),
        "settlement_return": any(row["operation"] == "settlement" and row["phase"] == "return"
                                 for row in recorder.events),
        "managed_close": any(row["operation"] == "managed_close"
                             for row in recorder.events),
    }
    result = {
        "schema": 1, "kind": "LS15 bounded observation, not qualification",
        "side": state["side"], "head": state["head"], "selector": SELECTOR,
        "clock": "parent-process monotonic_ns relative to observer initialization",
        "bounds": "parent CPython method-frame call and return/unwind events; no socket-level timing",
        "child_process_coverage": False, "pytest_exit": int(session.exitstatus),
        "pytest_phases": recorder.reports, "fixture_stop_source_lines": state["stop_sites"],
        "trace_hooks_restored": recorder.restored,
        "dropped_events": recorder.dropped, "observer_errors": recorder.errors,
        "observation_valid": recorder.restored and not recorder.dropped and not recorder.errors,
        "coverage": coverage,
        "required_boundaries_observed": all(coverage.values()),
        "first_queue_failure_call": recorder.first_queue_failure,
        "events": recorder.events,
    }
    try:
        data = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(data) > MAX_BYTES:
            raise ValueError("observer_output_limit")
        with state["output"].open("xb") as stream:
            stream.write(data)
    except Exception as error:
        # Never replace the original test exit with a diagnostic-output failure.
        reporter = session.config.pluginmanager.getplugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"LS15 OBSERVATION INVALID: {type(error).__name__}")
