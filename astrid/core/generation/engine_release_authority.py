"""Private, incarnation-pinned, release-only local Linux owner protocol.

This protocol does not extend the engine service or Runtime APIs. Public HC03
profiles are observations, never bearer credentials. A lost reply leaves the
original record occupied; reconnecting always checks the original issuer.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import math
import os
from pathlib import Path
import re
import secrets
import select
import socket
import struct
import threading
import time

from .engine_contracts import CustodyUnresolved, EndpointOwnershipError

VERSION = "astrid.engine-release.v1"
_ID = re.compile(r"[0-9a-f]{32}")


def canonical(value):
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def process_identity(pid):
    """Bracket a live process observation with a real pidfd, including reuse."""
    from .engine_custody import _vibe_birth
    if type(pid) is not int or pid < 1:
        raise EndpointOwnershipError("invalid release peer")
    try:
        fd = os.pidfd_open(pid)
        try:
            birth = _vibe_birth(pid)
            uid = Path(f"/proc/{pid}").stat().st_uid
            if not birth or select.select([fd], [], [], 0)[0] or _vibe_birth(pid) != birth:
                raise OSError("release peer changed")
            return {"pid": pid, "birth": birth, "uid": uid, "boot": boot_id()}
        finally:
            os.close(fd)
    except (OSError, ValueError) as exc:
        raise CustodyUnresolved("release peer is unobservable") from exc


def peer_identity(channel):
    pid, uid, _gid = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    observed = process_identity(pid)
    if uid != os.getuid() or observed["uid"] != uid:
        raise EndpointOwnershipError("release peer UID differs")
    return observed


def bounded_deadline(deadline):
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or not 0 < deadline - time.monotonic() <= 10):
        raise ValueError("release deadline must be finite and within ten seconds")
    return int(deadline * 1_000_000_000)


def binding_record(binding):
    from astrid.core.execution.managed_tool_session import SessionBinding
    if not isinstance(binding, SessionBinding):
        raise EndpointOwnershipError("release requires a complete SessionBinding")
    # asdict includes explicit None and empty values omitted by to_dict().
    return asdict(binding)


def _valid_sample(sample):
    if not isinstance(sample, dict) or type(sample.get("sample_ns")) is not int or sample["sample_ns"] < 1:
        return False
    if sample.get("available") is False:
        return (set(sample) == {"available", "sample_ns", "error"}
                and sample["error"] in {"vram_unavailable", "vram_device_changed"})
    return (sample.get("available") is True
        and set(sample) == {"available", "sample_ns", "total_bytes", "free_bytes", "device"}
        and type(sample["total_bytes"]) is int and type(sample["free_bytes"]) is int
        and 0 <= sample["free_bytes"] <= sample["total_bytes"] < 2**63
        and (sample["device"] is None or isinstance(sample["device"], str) and len(sample["device"]) <= 128))


class PinnedOwner:
    """No start, discovery, adoption, or successor fallback on any operation."""

    def __init__(self, client, issuer):
        self._identity = canonical(client.identity)
        self._control = client.control
        self._issuer = canonical(issuer)
        self._pidfd = os.pidfd_open(issuer["pid"])
        if process_identity(issuer["pid"]) != issuer:
            self.close()
            raise EndpointOwnershipError("original release issuer changed")

    def close(self):
        if getattr(self, "_pidfd", None) is not None:
            os.close(self._pidfd)
            self._pidfd = None

    def __del__(self):
        self.close()

    @property
    def identity(self):
        import json
        return json.loads(self._identity)

    @property
    def issuer(self):
        import json
        return json.loads(self._issuer)

    @property
    def control(self):
        return self._control

    def request(self, operation, payload, deadline):
        from .engine_custody import _directory, _socket_path, _send, _receive, _remaining, _private
        import stat
        _remaining(deadline)
        if self._pidfd is None or select.select([self._pidfd], [], [], 0)[0]:
            raise CustodyUnresolved("original release issuer exited")
        try:
            with _directory(self.control, create=False) as fd:
                _private(os.stat("control.sock", dir_fd=fd, follow_symlinks=False), stat.S_ISSOCK)
                with socket.socket(socket.AF_UNIX) as channel:
                    channel.settimeout(_remaining(deadline))
                    channel.connect(_socket_path(fd))
                    if peer_identity(channel) != self.issuer:
                        raise EndpointOwnershipError("original release issuer changed")
                    _send(channel, {"identity": self.identity, "operation": "bound_release",
                                    "payload": {"version": VERSION, "operation": operation,
                                                "issuer": self.issuer, **payload},
                                    "remaining_seconds": _remaining(deadline)}, deadline)
                    reply = _receive(channel, deadline)
                    if (time.monotonic() > deadline or peer_identity(channel) != self.issuer
                            or not isinstance(reply, dict) or set(reply) != {"identity", "result"}
                            or reply["identity"] != self.identity):
                        raise CustodyUnresolved("release reply is unbound or late")
                    return reply["result"]
        except (OSError, EOFError, ValueError) as exc:
            raise CustodyUnresolved("original release issuer reply uncertain") from exc


class ReleaseOffer:
    """Private producer ticket. Never serialize this object to a public profile."""

    def __init__(self, owner, receipt):
        self._owner, self._receipt = owner, receipt
        self._delegated = False

    def __repr__(self):
        return "<ReleaseOffer private>"

    def delegate(self, *, target, runtime, deadline):
        if self._delegated:
            raise EndpointOwnershipError("release offer already delegated")
        self._delegated = True  # An uncertain delivery must not retarget.
        result = self._owner.request("delegate", {**self._receipt, "target": target,
                                                   "runtime": runtime}, deadline)
        if result.get("status") != "delegated":
            raise EndpointOwnershipError("release delegation refused")
        return {"issuer": self._owner.issuer, "identity": self._owner.identity,
                "control": self._owner.control, "target": target, "runtime": runtime,
                **self._receipt}


class ClaimedEngineRelease:
    def __init__(self, owner, receipt, target, runtime):
        self._owner, self._receipt = owner, receipt
        self.target, self.runtime = target, runtime
        self._bound = False

    def __repr__(self):
        return "<ClaimedEngineRelease private>"

    def bind(self, binding, generation, *, runtime, admission, deadline):
        if self._bound or runtime != self.runtime:
            raise EndpointOwnershipError("release claim cannot be rebound")
        self._bound = True  # Bind ACK loss retains the original obligation.
        request = {**self._receipt, "binding": binding_record(binding), "generation": generation,
                   "runtime": runtime, "admission": admission}
        result = self._owner.request("bind", request, deadline)
        if result.get("status") != "bound" or result.get("binding_digest") != digest(request["binding"]):
            raise EndpointOwnershipError("release association refused")
        return BoundEngineRelease(self._owner, self._receipt, result, binding, generation)


class BoundEngineRelease:
    """One immutable session/generation; observation cannot resolve a new owner."""

    def __init__(self, owner, credential, association, binding, generation):
        self._owner, self._credential = owner, dict(credential)
        self._association = dict(association)
        self.binding, self.generation = binding, generation
        self.association_id = association["association_id"]
        self._requests = {}
        self._lock = threading.Lock()

    def __repr__(self):
        return "<BoundEngineRelease private>"

    def _validate(self, result, request):
        expected = {"version": VERSION, "request": request, "request_digest": digest(request),
                    "association_id": self.association_id}
        allowed = {*expected, "status", "effect_sent", "unload_ack", "error", "queue_running", "queue_pending",
                   "vram_before", "vram_after", "memory_observed_returned", "engine_alive"}
        if (not isinstance(result, dict) or set(result) - allowed
                or any(canonical(result.get(k)) != canonical(v) for k, v in expected.items())
                or result.get("status") not in {"busy", "completed", "failed", "unresolved"}
                or type(result.get("effect_sent")) is not bool
                or type(result.get("unload_ack")) not in (bool, type(None))
                or (result.get("error") is not None and (not isinstance(result["error"], str)
                    or not re.fullmatch(r"[a-z_]{1,128}", result["error"])))
                or (result["status"] == "completed" and (result.get("unload_ack") is not True
                    or result["effect_sent"] is not True or result.get("engine_alive") is not True
                    or result.get("queue_running") != 0 or result.get("queue_pending") != 0))
                or any(type(result[k]) is not int or not 0 <= result[k] < 10_000_000
                       for k in ("queue_running", "queue_pending") if k in result)
                or (result["status"] == "busy" and (result["effect_sent"]
                    or not result.get("queue_running", 0) + result.get("queue_pending", 0)))
                or any(not _valid_sample(result[k]) for k in ("vram_before", "vram_after") if k in result)
                or ("memory_observed_returned" in result and result["memory_observed_returned"] is not False)):
            raise CustodyUnresolved("release ACK does not match frozen request")
        return result

    def _uncertain(self, request):
        return {"version": VERSION, "association_id": self.association_id,
                "request": request, "request_digest": digest(request), "status": "unresolved",
                "effect_sent": None, "unload_ack": None, "error": "reply_uncertain"}

    def release(self, request_id, expected_binding, expected_generation, deadline):
        deadline_ns = bounded_deadline(deadline)
        if (not isinstance(request_id, str) or not _ID.fullmatch(request_id)
                or binding_record(expected_binding) != binding_record(self.binding)
                or type(expected_generation) is not int or expected_generation != self.generation):
            raise EndpointOwnershipError("release association does not match")
        request = {"version": VERSION, "operation": "release", "request_id": request_id,
                   "association_id": self.association_id, "issuance_id": self._credential["issuance_id"],
                   "engine_digest": self._association["engine_digest"],
                   "target_digest": self._association["target_digest"],
                   "runtime_digest": self._association["runtime_digest"],
                   "binding": binding_record(expected_binding), "generation": expected_generation,
                   "deadline_ns": deadline_ns, "boot": boot_id()}
        with self._lock:
            if request_id not in self._requests and len(self._requests) >= 1024:
                raise EndpointOwnershipError("local release request capacity reached")
            previous = self._requests.setdefault(request_id, canonical(request))
            if previous != canonical(request):
                raise EndpointOwnershipError("release request ID cannot change payload or deadline")
        try:
            result = self._owner.request("release", {**self._credential, "request": request}, deadline)
            return self._validate(result, request)
        except CustodyUnresolved:
            return self._uncertain(request)

    def observe(self, request_id, deadline):
        bounded_deadline(deadline)
        with self._lock:
            frozen = self._requests.get(request_id)
        if frozen is None:
            raise EndpointOwnershipError("observation requires the identical retained request")
        import json
        request = json.loads(frozen)
        try:
            result = self._owner.request("observe", {**self._credential, "request": request}, deadline)
            return self._validate(result, request)
        except CustodyUnresolved:
            return self._uncertain(request)


class ReleaseAuthority:
    """Supervisor-owned records. All mutations run under supervisor.work.

    The small table lock permits bounded readback while native work is blocked.
    No code holding it waits for work. Records are never evicted into executable
    unknown IDs; issuer shutdown destroys mutation authority.
    """

    def __init__(self, supervisor):
        self.owner = supervisor
        self.issuer = process_identity(os.getpid())
        self.records = {}
        self.table_lock = threading.Lock()

    @property
    def occupied(self):
        return any(r["state"] != "completed" for r in self.records.values())

    def revoke(self):
        for record in self.records.values():
            record["revoked"] = True

    def _record(self, value, peer, *, observe=False):
        record = self.records.get(value.get("issuance_id"))
        if (record is None or not secrets.compare_digest(str(value.get("secret", "")), record["secret"])
                or value.get("issuer") != self.issuer or peer["uid"] != self.issuer["uid"]):
            raise EndpointOwnershipError("release credential refused")
        if observe:
            if time.monotonic() > record["expires"] + 86400:
                raise EndpointOwnershipError("release observation expired")
        elif record["revoked"] or time.monotonic() >= record["expires"]:
            raise EndpointOwnershipError("release credential retired")
        return record

    def _current(self, record, channel, deadline):
        if record["engine"] != self.owner.release_identity(channel, deadline):
            record["revoked"] = True
            raise EndpointOwnershipError("original engine incarnation changed")

    def dispatch(self, value, peer, channel, deadline):
        if value.get("version") != VERSION or value.get("issuer") != self.issuer:
            raise EndpointOwnershipError("release protocol issuer differs")
        operation = value.get("operation")
        if operation == "issue":
            if self.occupied or len(self.records) >= 4096:
                raise CustodyUnresolved("original release obligation is still occupied")
            engine = self.owner.release_identity(channel, deadline)
            if engine != value.get("engine"):
                raise EndpointOwnershipError("final ready engine comparison failed")
            profile_digest = value.get("profile_digest")
            if not isinstance(profile_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", profile_digest):
                raise EndpointOwnershipError("private readiness digest is absent")
            issuance_id, secret = secrets.token_hex(16), secrets.token_hex(32)
            self.records[issuance_id] = {"engine": engine, "secret": secret, "producer": peer,
                "profile_digest": profile_digest, "expires": time.monotonic() + 60,
                "issued": time.monotonic(),
                "revoked": False, "state": "offered", "requests": {}, "target": None,
                "runtime": None, "association": None}
            return {"issuance_id": issuance_id, "secret": secret}
        record = self._record(value, peer, observe=operation in {"observe", "release"})
        if operation == "delegate":
            if record["state"] != "offered" or peer != record["producer"]:
                raise EndpointOwnershipError("release offer cannot be retargeted")
            target, runtime = value.get("target"), value.get("runtime")
            if (not isinstance(target, dict) or set(target) != {"process", "nonce", "executor_id", "source"}
                    or target["process"] != process_identity(target["process"].get("pid"))
                    or not isinstance(target["nonce"], str) or not _ID.fullmatch(target["nonce"])
                    or not target["source"] or not target["executor_id"]
                    or not isinstance(runtime, dict) or set(runtime) != {"instance_id", "endpoint", "epoch", "executor_id"}
                    or not runtime["instance_id"] or str(runtime["instance_id"]).startswith("epoch:")
                    or not runtime["endpoint"] or type(runtime["epoch"]) is not int or runtime["epoch"] < 1
                    or runtime["executor_id"] != target["executor_id"]):
                raise EndpointOwnershipError("selected host and Runtime evidence incomplete")
            self._current(record, channel, deadline)
            record.update(target=target, runtime=runtime, state="delegated")
            return {"status": "delegated"}
        target = record["target"]
        if target is None or peer != target["process"]:
            raise EndpointOwnershipError("release belongs to a different selected host")
        if operation == "revoke":
            record["revoked"] = True
            record["state"] = "revoked"
            return {"status": "revoked"}
        if operation == "claim":
            if value.get("target") != target or value.get("runtime") != record["runtime"]:
                raise EndpointOwnershipError("targeted release claim differs")
            if record["state"] not in {"delegated", "claimed"}:
                raise EndpointOwnershipError("release claim consumed")
            self._current(record, channel, deadline)
            record["state"] = "claimed"
            return {"status": "claimed"}
        if operation == "bind":
            if record["state"] != "claimed" or value.get("runtime") != record["runtime"]:
                raise EndpointOwnershipError("release association cannot change")
            self._current(record, channel, deadline)
            binding, generation = value.get("binding"), value.get("generation")
            engine = record["engine"]
            expected = {"session_id": self.owner.registry,
                "runtime_instance_id": engine["owner_instance"], "process_birth_id": engine["issuer"]["birth"],
                "endpoint": engine["endpoint"], "source_digest": engine["source_content_digest"],
                "config_digest": engine["config_digest"], "engine_birth_id": engine["engine"]["birth"],
                "launch_generation": engine["engine"]["birth"]}
            from astrid.core.execution.managed_tool_session import SessionBinding
            fields = set(SessionBinding.__dataclass_fields__)
            if (not isinstance(binding, dict) or set(binding) != fields
                    or any(binding.get(k) != v for k, v in expected.items())
                    or not binding["execution_identity"]
                    or binding["runtime_epoch"] not in (None, record["runtime"]["epoch"])
                    or (binding["runtime_epoch"] is not None and type(binding["runtime_epoch"]) is not int)
                    or type(generation) is not int or generation < 1):
                raise EndpointOwnershipError("complete session association differs from ready evidence")
            # The current canonical engine has unauthenticated mutation ingress.
            # Neither a host assertion nor queue-empty observation establishes
            # exclusive admission. No production topology is enrolled yet.
            if not self.owner.release_admission_supported(peer, value.get("admission")):
                raise EndpointOwnershipError("exclusive engine admission is unverified")
            association = {"status": "bound", "association_id": secrets.token_hex(16),
                "engine_digest": digest(engine), "target_digest": digest(target),
                "runtime_digest": digest(record["runtime"]), "binding_digest": digest(binding),
                "generation": generation}
            record.update(state="bound", binding=binding, generation=generation,
                          association=association, expires=record["issued"] + 86400)
            return association
        if operation in {"release", "observe"}:
            return self._release(record, value, channel, deadline, observe=operation == "observe")
        raise EndpointOwnershipError("unsupported release-only operation")

    def _release(self, record, value, channel, deadline, *, observe):
        request = value.get("request")
        association = record["association"]
        if (not isinstance(request, dict) or association is None
                or not isinstance(request.get("request_id"), str) or not _ID.fullmatch(request["request_id"])):
            raise EndpointOwnershipError("unassociated release request")
        request_id = request["request_id"]
        with self.table_lock:
            previous = record["requests"].get(request_id)
            if previous is not None:
                if canonical(previous["request"]) != canonical(request):
                    raise EndpointOwnershipError("release ID has changed bytes")
                return dict(previous)
            if observe:
                raise CustodyUnresolved("original release request is unavailable")
            if len(record["requests"]) >= 1024:
                raise EndpointOwnershipError("release request retention capacity reached")
            if record["revoked"] or time.monotonic() >= record["expires"]:
                raise EndpointOwnershipError("release mutation authority retired")
            expected = {"version": VERSION, "operation": "release", "request_id": request_id,
                "association_id": association["association_id"], "issuance_id": value["issuance_id"],
                "engine_digest": association["engine_digest"], "target_digest": association["target_digest"],
                "runtime_digest": association["runtime_digest"], "binding": record["binding"],
                "generation": record["generation"], "deadline_ns": request.get("deadline_ns"), "boot": boot_id()}
            if (canonical(request) != canonical(expected) or type(request.get("deadline_ns")) is not int
                    or not 0 < request["deadline_ns"] - time.monotonic_ns() <= 10_000_000_000
                    or record["state"] != "bound"):
                raise EndpointOwnershipError("release request identity or deadline differs")
            result = {"version": VERSION, "association_id": association["association_id"],
                "request": request, "request_digest": digest(request), "status": "unresolved",
                "effect_sent": False, "unload_ack": None, "error": "in_progress"}
            record["requests"][request_id] = result
            record["state"] = "releasing"
        deadline = min(deadline, request["deadline_ns"] / 1_000_000_000, record["expires"])
        try:
            self._current(record, channel, deadline)
            native = self.owner.release_bound_native(channel, deadline, result)
            with self.table_lock:
                result.update({k: v for k, v in native.items() if k not in {"status", "error"}})
            self._current(record, channel, deadline)
            with self.table_lock:
                result.update(native)
                record["state"] = "bound" if result["status"] == "busy" else result["status"]
                if result["status"] == "completed":
                    record["revoked"] = True
        except BaseException as error:
            with self.table_lock:
                status = "failed" if isinstance(error, EndpointOwnershipError) and not result["effect_sent"] else "unresolved"
                result.update(status=status, error="native_or_owner_uncertain")
                record["state"] = status
        return dict(result)
