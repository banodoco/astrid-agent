"""One private Agent descriptor, independent of Runtime activation/child wires.

Socketpair SO_PEERCRED records the creating process, so it cannot authenticate
an inherited endpoint. Linux SCM_CREDENTIALS authenticates each actual sender.
No ticket is put in argv, environment, ready files, or public return values.
"""
from __future__ import annotations

import os
import secrets
import socket
import struct
import time
from types import SimpleNamespace

from astrid.core.generation.engine_contracts import EndpointOwnershipError
from astrid.core.generation.engine_release_authority import (
    ClaimedEngineRelease, PinnedOwner, canonical, digest, process_identity,
)

_LIMIT = 65536


class ReleaseHandoff:
    def __init__(self, channel):
        self.channel = channel
        channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        self._used = False

    @classmethod
    def pair(cls):
        if not hasattr(socket, "SCM_CREDENTIALS"):
            raise EndpointOwnershipError("release handoff requires Linux credentials")
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        return cls(left), cls(right)

    def fileno(self):
        return self.channel.fileno()

    def close(self):
        self.channel.close()

    def _send(self, value, deadline):
        remaining = deadline - time.monotonic()
        data = canonical(value)
        if remaining <= 0 or len(data) > _LIMIT:
            raise EndpointOwnershipError("release handoff deadline or frame invalid")
        self.channel.settimeout(remaining)
        if self.channel.send(data) != len(data):
            raise EndpointOwnershipError("release handoff frame incomplete")

    def _receive(self, deadline, expected_peer=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EndpointOwnershipError("release handoff expired")
        self.channel.settimeout(remaining)
        data, ancillary, flags, _address = self.channel.recvmsg(_LIMIT, socket.CMSG_SPACE(12))
        credentials = [raw for level, kind, raw in ancillary
                       if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS]
        if not data or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(credentials) != 1:
            raise EndpointOwnershipError("release handoff credentials or framing absent")
        pid, uid, _gid = struct.unpack("3i", credentials[0])
        peer = process_identity(pid)
        if uid != os.getuid() or uid != peer["uid"] or (expected_peer is not None and peer != expected_peer):
            raise EndpointOwnershipError("release handoff selected peer differs")
        from astrid.core.generation.engine_custody import _decode
        value = _decode(data)
        if type(value) is not dict:
            raise EndpointOwnershipError("release handoff frame must be an object")
        return value, peer

    def deliver(self, offer, *, selected_process, runtime, source, deadline):
        if self._used:
            raise EndpointOwnershipError("release handoff already consumed")
        self._used = True
        try:
            hello, _peer = self._receive(deadline, selected_process)
            target = hello.get("target")
            if (set(hello) != {"target", "runtime"} or hello["runtime"] != runtime
                    or not isinstance(target, dict) or target.get("process") != selected_process
                    or target.get("executor_id") != runtime["executor_id"] or target.get("source") != source):
                raise EndpointOwnershipError("release handoff does not match selected registration")
            ticket = offer.delegate(target=target, runtime=runtime, deadline=deadline)
            self._send(ticket, deadline)
            ack, _peer = self._receive(deadline, selected_process)
            if ack != {"status": "claimed", "issuance_id": ticket["issuance_id"], "target_digest": digest(target)}:
                raise EndpointOwnershipError("release claim acknowledgement uncertain")
            return {"status": "claimed", "target_digest": digest(target)}
        finally:
            self.close()

    def claim(self, *, executor_id, source, runtime, deadline, producer=None):
        if self._used:
            raise EndpointOwnershipError("release handoff already consumed")
        self._used = True
        if producer is None:
            self.close()
            raise EndpointOwnershipError("release handoff requires its pinned producer")
        target = {"process": process_identity(os.getpid()), "nonce": secrets.token_hex(16),
                  "executor_id": executor_id, "source": source}
        try:
            self._send({"target": target, "runtime": runtime}, deadline)
            ticket, peer = self._receive(deadline, producer)
            if (ticket.get("target") != target or ticket.get("runtime") != runtime
                    or peer != producer or producer is None):
                raise EndpointOwnershipError("release handoff producer or target differs")
            owner = PinnedOwner(SimpleNamespace(identity=ticket["identity"], control=ticket["control"]), ticket["issuer"])
            credential = {k: ticket[k] for k in ("issuance_id", "secret")}
            result = owner.request("claim", {**credential, "target": target, "runtime": runtime}, deadline)
            if result != {"status": "claimed"}:
                raise EndpointOwnershipError("release claim was not acknowledged")
            claimed = ClaimedEngineRelease(owner, credential, target, runtime)
            self._send({"status": "claimed", "issuance_id": ticket["issuance_id"], "target_digest": digest(target)}, deadline)
            return claimed
        finally:
            self.close()
