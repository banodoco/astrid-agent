"""Original supervisor's retained endpoint for the inherited native fence."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import time

from .engine_contracts import CustodyUnresolved, EndpointOwnershipError
from .engine_native_fence import VERSION, LIMIT, canonical, decode, identity


class NativeController:
    def __init__(self, owner):
        here = Path(__file__).resolve().parent
        self.owner = owner
        self.channel, self.child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.sequence = 0
        self.engine = None
        self.routes = None
        self.failed = False
        self.bound = None
        self.enrollment = {"mode": "comfyui-linux-cu130-v1", "controller": identity(os.getpid()),
            "nonce": secrets.token_hex(32), "installation": owner.identity, "launch": owner.launch,
            "adapter": {name: hashlib.sha256((here / name).read_bytes()).hexdigest()
                        for name in ("engine_native_launcher.py", "engine_native_fence.py", "engine_native_policy.json")},
            "policy": hashlib.sha256((here / "engine_native_policy.json").read_bytes()).hexdigest()}

    def child_environment(self):
        return {"ASTRID_NATIVE_CONTROL": canonical({"fd": self.child.fileno(), "enrollment": self.enrollment}).decode()}

    def close(self):
        self.failed = True
        self.channel.close()
        self.child.close()

    def spawned(self, engine):
        self.child.close()
        self.engine = engine

    def request(self, operation, payload, deadline):
        from .engine_release_authority import process_identity
        try:
            if (self.failed or self.engine is None or self.owner.engine is not self.engine
                    or not self.engine.live()):
                raise ValueError("original native engine unavailable")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("native control deadline expired")
            self.channel.settimeout(remaining)
            self.sequence += 1
            value = {"version": VERSION, "enrollment": self.enrollment, "sequence": self.sequence,
                     "operation": operation, "payload": payload}
            data = canonical(value)
            if len(data) >= LIMIT or self.channel.send(data) != len(data):
                raise ValueError("native control overflow")
            reply = decode(self.channel.recv(LIMIT))
            if (time.monotonic() >= deadline or not self.engine.live()
                    or reply.get("sequence") != self.sequence or reply.get("version") != VERSION
                    or reply.get("enrollment") != self.enrollment
                    or reply.get("engine") != process_identity(self.engine.process.pid)
                    or reply.get("state") not in {"OPEN", "DRAINING", "RETIRED", "UNCERTAIN"}
                    or type(reply.get("worker_ready")) is not bool
                    or type(reply.get("dispatch_ready")) is not bool
                    or any(type(reply.get(k)) is not int or reply[k] < 0 for k in ("handlers", "tasks"))
                    or not isinstance(reply.get("routes"), str) or len(reply["routes"]) != 64
                    or self.routes is not None and reply["routes"] != self.routes
                    or set(reply) != {"sequence", "version", "enrollment", "engine", "routes", "state",
                                      "bound", "worker_ready", "handlers", "tasks", "dispatch_ready", "record"}):
                raise ValueError("native control attestation differs")
            self.routes = reply["routes"]
            return reply
        except (OSError, ValueError, CustodyUnresolved, EndpointOwnershipError) as exc:
            self.close()  # Uncertain transport cannot issue an unload or reopen.
            raise CustodyUnresolved("original native fence control unresolved") from exc

    def attest(self, deadline):
        reply = self.request("status", {}, deadline)
        if reply["state"] not in {"OPEN", "DRAINING", "RETIRED"} or not reply["dispatch_ready"]:
            raise CustodyUnresolved("native gate state is unknown")
        return {k: reply[k] for k in ("version", "enrollment", "engine", "routes")}

    def bind(self, association, binding, generation, deadline):
        payload = {"association_id": association, "binding": binding, "generation": generation}
        reply = self.request("bind", payload, deadline)
        if reply["bound"] != payload or reply["state"] != "OPEN":
            self.close()
            raise CustodyUnresolved("native association unacknowledged")
        self.bound = payload

    def release(self, request, deadline, observe=False, unload=False):
        if self.bound is None:
            raise EndpointOwnershipError("native engine has no release association")
        operation = "unload" if unload else "observe" if observe else "release"
        payload = request if observe or unload else {**self.bound, "request": request}
        reply = self.request(operation, payload, deadline)
        record = reply["record"]
        if (not isinstance(record, dict) or record.get("request") != request
                or record.get("digest") != hashlib.sha256(canonical(request)).hexdigest()
                or record.get("status") not in {"draining", "settled", "ready_unload", "unloading", "completed", "failed", "unresolved"}
                or type(record.get("effect_sent")) is not bool
                or type(record.get("unload_ack")) not in (bool, type(None))
                or reply["state"] == "OPEN"):
            self.close()
            raise CustodyUnresolved("native release record differs")
        if record["status"] == "completed" and not (
                record["effect_sent"] and record["unload_ack"] is True
                and type(record.get("barrier_ns")) is int and type(record.get("settled_ns")) is int
                and record["closed_ns"] <= record["barrier_ns"] <= record["settled_ns"]):
            self.close()
            raise CustodyUnresolved("native settlement record malformed")
        return record
