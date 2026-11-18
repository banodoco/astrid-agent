"""Test-only process observer; never install during the latency measurement.

The strict/captured worker startup is retained verbatim before observation is
installed. No environment, descriptor, network policy, or task-scope permission
is widened. Telemetry contains counts and process identities, never argv/env.
"""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

PREFIX = "PLAN55_PROCESS_OBSERVATION "
MARKER = "# PLAN55_OBSERVER_INSTALLED_AFTER_ADMISSION"
MAX_EVENTS = 100000
MAX_REPORT_BYTES = 16384
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
        f"_p55hash_source={hash_source!r}\n"
        f"_p55observer_source={observer_source!r}\n"
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

    def event(self, kind: str, **fields) -> None:
        with self.lock:
            if len(self.events) >= MAX_EVENTS:
                self.gaps.add("Python observation event limit exceeded")
                return
            self.events.append({"at_ns": time.monotonic_ns(), "kind": kind, **fields})

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
                    "fdatasync" if arg is self.fdatasync_function else None)
            if kind is not None:
                self.event(kind + {"c_call": "_attempt", "c_return": "_success", "c_exception": "_error"}[event])
        if self.chain_profiles:
            previous = self.previous_profile if threading.get_ident() == self.start_thread else self.previous_thread_profile
            if previous is not None:
                previous(frame, event, arg)

    def start(self) -> None:
        if self.started is not None:
            raise RuntimeError("process observer already installed")
        self.started = time.monotonic_ns()
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
        threading.setprofile(self.profile)
        sys.setprofile(self.profile)

    def snapshot(self, since_ns: int | None = None, until_ns: int | None = None) -> dict:
        if self.started is None or self.hashes is None:
            raise RuntimeError("process observer not installed")
        with self.lock:
            rows = [row for row in self.events if (since_ns is None or row["at_ns"] >= since_ns)
                    and (until_ns is None or row["at_ns"] <= until_ns)]
            counts = Counter(row["kind"] for row in rows)
            processes = [row for row in rows if row["kind"] == "process_start"]
            hashes = self.hashes.snapshot()
            return {
                "schema": "astrid.local-speed-process.v1", "pid": os.getpid(),
                "role": self.role, "start_ns": self.started, "end_ns": time.monotonic_ns(),
                "counts": dict(sorted(counts.items())), "process_starts": processes,
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
                key = (row["pid"], row.get("start_ns"))
                if key in found and found[key] != row:
                    gaps.append("conflicting process report")
                found[key] = row
            except (ValueError, KeyError, TypeError):
                gaps.append("malformed or truncated process report")
    return list(found.values()), gaps
