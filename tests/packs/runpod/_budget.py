"""Shared CPU-fake budget for public remote calls and submitted process starts.

Counts attempted calls (including failures), not HTTP requests hidden by a
transport. Shell launches count the Python/Astrid command lines submitted to the
pod, not subprocesses inside their Python bodies or the simulated engine.
"""
from __future__ import annotations

import inspect
import re
import shlex
from collections import Counter
from functools import wraps


class RemoteBudget:
    def __init__(self):
        self.counts = Counter()

    def wrap(self, monkeypatch, target, name, *, kind=None, command=False):
        original = getattr(target, name)
        key = kind or name

        def count(args, kwargs):
            self.counts[key] += 1
            if command:
                position = 1 if isinstance(target, type) else 0
                self.counts["process_start"] += self.process_starts(
                    args[position] if len(args) > position else kwargs["command"])

        if inspect.iscoroutinefunction(original):
            @wraps(original)
            async def wrapped(*args, **kwargs):
                count(args, kwargs)
                return await original(*args, **kwargs)
        else:
            @wraps(original)
            def wrapped(*args, **kwargs):
                count(args, kwargs)
                return original(*args, **kwargs)
        monkeypatch.setattr(target, name, wrapped)

    @staticmethod
    def process_starts(command):
        # Tokenizing keeps quoted multiline Python bodies opaque. Preserve shell
        # newlines as operators so every submitted command is examined.
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|(){}\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
        boundary = True
        starts = 0
        for token in tokens:
            if token and all(char in ";&|(){}\n" for char in token):
                boundary = True
                continue
            if boundary and (token in {"then", "do", "exec", "env", "!"}
                             or re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", token)):
                continue
            if boundary:
                binary = token.rsplit("/", 1)[-1]
                if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|astrid(?:-tools)?", binary):
                    starts += 1
            boundary = False
        return starts

    def pod(self, pod, monkeypatch):
        for name in ("exec", "put", "get", "job_start", "job_poll", "job_cancel"):
            self.wrap(monkeypatch, pod, name, command=name in {"exec", "job_start"})

    def https(self, transport, monkeypatch):
        """For HTTPS-only fakes; use instead of pod() when it delegates there."""
        for name, kind in (("execute_command", "exec"), ("put", "put"), ("get", "get"),
                           ("detached_start", "job_start"), ("detached_poll", "job_poll"),
                           ("detached_cancel", "job_cancel")):
            self.wrap(monkeypatch, transport, name, kind=kind, command=kind in {"exec", "job_start"})

    def machine_case(self, case, monkeypatch):
        import runpod_lifecycle
        from runpod_lifecycle import api, lifecycle
        from runpod_lifecycle.pod import Pod

        from astrid.packs.runpod import target_selection, worker_preparation

        self.pod(case.pod, monkeypatch)
        self.wrap(monkeypatch, case.machine, "_observe_mounts", kind="provider.observe_mounts")
        for target, names in (
            (api, ("get_network_volumes", "get_pod_status", "list_pods", "terminate_pod",
                   "reconcile_pod_cleanup")),
            (lifecycle, ("create_pod", "find_gpu_type", "get_storage_volume_id", "check_and_expand_storage")),
            (runpod_lifecycle, ("gpu_availability", "get_pod")),
            (worker_preparation, ("read_exact_pod", "confirm_acquired_machine", "_get_provider_pod")),
            (target_selection, ("read_exact_pod",)),
            (case.adapter, ("observe_mounts",)),
            (Pod, ("wait_ready",)),
            (case.provider, ("claim", "release", "pod_exists")),
        ):
            for name in names:
                self.wrap(monkeypatch, target, name, kind="provider." + name.lstrip("_"))

    def assert_within(self, bounds):
        excess = {kind: count for kind, count in self.counts.items() if count > bounds.get(kind, 0)}
        assert not excess, f"remote budget exceeded: {excess}; per-kind counts={dict(sorted(self.counts.items()))}; bounds={bounds}"
