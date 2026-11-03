"""Opt-in, observational macOS stop-order breadcrumbs; never stop authority."""

from __future__ import annotations

import json
import os
import stat
import time
import uuid

from runtime_protocol import platform

from .birth_identity import birth_ids_match


class MacStopCapture:
    def __init__(self, log, discovery_path, discovery):
        self.log = log
        self.path = discovery_path
        self.run = uuid.uuid4().hex
        self.seq = 0
        self.pending = []
        self.owner = None
        if isinstance(discovery, dict):
            pid, birth = discovery.get("pid"), discovery.get("process_birth_id")
            instance = discovery.get("runtime_instance_id")
            if (type(pid) is int and pid > 0 and isinstance(birth, str) and birth
                    and isinstance(instance, str) and instance):
                self.owner = (pid, birth, instance)

    def _discovery(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    return "not_regular"
                raw = os.read(fd, 65537)
            finally:
                os.close(fd)
            if len(raw) > 65536:
                return "oversize"
            value = json.loads(raw)
            if not isinstance(value, dict):
                return "malformed"
            if self.owner is None:
                return "present_unbound"
            return ("same" if (value.get("pid"), value.get("process_birth_id"),
                               value.get("runtime_instance_id")) == self.owner else "different")
        except FileNotFoundError:
            return "absent"
        except (ValueError, UnicodeError):
            return "malformed"
        except OSError:
            return "unreadable"

    def _process(self):
        if self.owner is None:
            return "unbound"
        pid, birth, _instance = self.owner
        try:
            observed = platform.process_identity(pid)
            if not isinstance(observed, dict):
                return "unobserved"
            if observed.get("pid") != pid or observed.get("uid") != platform.current_user():
                return "different"
            return ("same" if birth_ids_match(birth, observed.get("birth_id"), pid, pid)
                    else "different_or_unknown")
        except Exception:
            return "unobserved"

    def event(self, stage, runtime_owner=None):
        # Only fixed stage names are supplied by installer call sites. Errors in
        # diagnostics cannot change admission, stop, or rollback behavior.
        try:
            self.seq += 1
            value = {"format": "astrid-mac-stop-capture/v1", "capture": self.run,
                     "seq": self.seq, "stage": stage, "wall_ns": time.time_ns(),
                     "monotonic_ns": time.monotonic_ns(), "discovery": self._discovery(),
                     "process": self._process()}
            if runtime_owner is not None:
                value["runtime_owner"] = runtime_owner
            value["sample_end_wall_ns"] = time.time_ns()
            value["sample_end_monotonic_ns"] = time.monotonic_ns()
            with self.log.open("a", encoding="utf-8") as stream:
                platform.owner_only(stream.fileno())
                for pending in self.pending:
                    stream.write(json.dumps(pending, separators=(",", ":")) + "\n")
                self.pending.clear()
                stream.write(json.dumps(value, separators=(",", ":")) + "\n")
        except Exception:
            pass

    def bootout_event(self, stage):
        if stage != "bootout_enter":
            self.event(stage)
            return
        # No file or process observation between the final admission and
        # launchctl. Flush this timestamp only after launchctl returns.
        try:
            self.seq += 1
            self.pending.append({"format": "astrid-mac-stop-capture/v1", "capture": self.run,
                                 "seq": self.seq, "stage": stage, "wall_ns": time.time_ns(),
                                 "monotonic_ns": time.monotonic_ns()})
        except Exception:
            pass


class ObservedBoundary:
    def __init__(self, boundary, capture):
        self._boundary, self._capture = boundary, capture

    def __getattr__(self, name):
        return getattr(self._boundary, name)

    def stop_owner(self, **kwargs):
        selected = (kwargs.get("pid"), kwargs.get("process_birth_id"), kwargs.get("instance_id"))
        match = ("unbound" if self._capture.owner is None else
                 "same" if selected == self._capture.owner else "different")
        self._capture.event("runtime_stop_enter", match)
        try:
            result = self._boundary.stop_owner(**kwargs)
        except BaseException:
            self._capture.event("runtime_stop_error", match)
            raise
        self._capture.event("runtime_stop_return", match)
        return result
