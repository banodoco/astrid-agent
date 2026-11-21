"""One pod-side billing deadline, independent of the host's lifetime.

This file is transported as a standalone standard-library program. Only the
pod process reads provider identity; the host sends source and public bounds.
"""
from __future__ import annotations

import base64
import json
import math
import os
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request

try:
    import fcntl
except ModuleNotFoundError:  # The host imports this module on every platform; only the pod locks.
    fcntl = None  # type: ignore[assignment]

DEADLINE_PATH = Path("/root/astrid/pod-watchdog-deadline.json")
PROGRAM_PATH = Path("/root/astrid/pod-watchdog.py")
IDENTITY_LOG_PATH = Path("/root/astrid/engines/comfyui/runtime/out/sessions/comfyui-logs/engine.log")
IDENTITY_MARK = "pod_watchdog: identity="

# Filled by the host in the standalone program; the pod imports no package.
if globals().get("__package__"):
    from astrid.installer import _tls_policy
    from astrid.packs.runpod.provider_urls import POD_TERMINATE_METHOD, POD_URL_TEMPLATE

    os_trust_ssl_context = _tls_policy.os_trust_ssl_context
else:
    POD_URL_TEMPLATE = "__POD_URL_TEMPLATE__"
    POD_TERMINATE_METHOD = "__POD_TERMINATE_METHOD__"
    _tls_namespace: dict[str, Any] = {}
    exec("__TLS_POLICY_SOURCE__", _tls_namespace)
    os_trust_ssl_context = _tls_namespace["os_trust_ssl_context"]


def urlopen(request: Request, *, timeout: float):
    """Keep the injectable request seam while using the one verifying policy."""
    return urllib.request.urlopen(
        request, timeout=timeout, context=os_trust_ssl_context()
    )


class PodWatchdogError(RuntimeError):
    """A pod must be released rather than retained without its watchdog."""


class PodWatchdogTransportError(PodWatchdogError):
    """The initial transport may not yet be reachable."""


def _deadline(value: Any) -> tuple[float, int]:
    deadline, generation = value["deadline"], value["generation"]
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or deadline <= 0 or type(generation) is not int or generation < 1):
        raise ValueError("invalid pod deadline")
    return float(deadline), generation


class Watchdog:
    def __init__(self, path: Path, default_deadline: float, *,
                 terminate: Callable[[], None]) -> None:
        if not math.isfinite(default_deadline) or default_deadline <= 0:
            raise ValueError("a finite claim deadline is required")
        self.path, self.deadline, self.generation = path, default_deadline, 0
        self.terminate = terminate

    def _refresh(self) -> None:
        try:
            deadline, generation = _deadline(json.loads(self.path.read_text()))
            if generation > self.generation:
                self.deadline, self.generation = deadline, generation
        except (OSError, ValueError, TypeError, KeyError):
            pass  # Missing or malformed state never renews the last bound.
    def tick(self, now: float) -> bool:
        self._refresh()
        if now < self.deadline:
            return False
        with self.path.with_suffix(".update-lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Publication can have won after the first read. Decide again
            # under its lock, immediately before committing and deleting.
            self._refresh()
            if now < self.deadline:
                return False
            _atomic_write(self.path.with_suffix(".terminating"), {
                "deadline": self.deadline, "generation": self.generation})
            self.terminate()
        return True


def _identity() -> tuple[str, str]:
    for name in ("RUNPOD_POD_ID", "RUNPOD_API_KEY"):
        if not os.environ.get(name, "").strip():
            raise PodWatchdogError(name)
    return os.environ["RUNPOD_POD_ID"], os.environ["RUNPOD_API_KEY"]


def _request(pod: str, key: str, *, method: str = POD_TERMINATE_METHOD,
             status_callback: Callable[[int], None] | None = None) -> Any:
    url = POD_URL_TEMPLATE.format(pod_id=quote(pod, safe=""))
    request = Request(url, method=method, headers={"Authorization": "Bearer " + key})
    with urlopen(request, timeout=2 if method == "GET" else 10) as response:
        if status_callback is not None:
            status = getattr(response, "status", None)
            status_callback(status if type(status) is int else 0)
        if method == "GET":
            return json.load(response)
        response.read(1)


def _report_identity() -> str:
    """Log fixed vocabulary only; never provider response bodies or identity values."""
    names = [name for name in ("RUNPOD_POD_ID", "RUNPOD_API_KEY")
             if os.environ.get(name, "").strip()]
    access = "unreachable"
    probe_status = 0

    def record_status(status: int) -> None:
        nonlocal probe_status
        probe_status = status

    if len(names) == 2:
        try:
            # V2 actions are state transitions, not bearer-token permissions.
            # A successful pods:read probe cannot establish pods:write rights.
            observed = _request(*_identity(), method="GET", status_callback=record_status)
            if 200 <= probe_status < 300 and isinstance(observed, dict):
                access = "reachable"
        except HTTPError as exc:
            probe_status = exc.code
            if exc.code in (401, 403):
                access = "denied"
        except (OSError, URLError, ValueError):
            pass
    else:
        access = "denied"
    IDENTITY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with IDENTITY_LOG_PATH.open("a", encoding="utf-8") as log:
        log.write(f"{IDENTITY_MARK}{','.join(names) or 'none'} "
                  f"probe=GET-own-pod probe_status={probe_status} access={access} "
                  "delete_permission=unproven_until_used\n")
        log.flush()
        os.fsync(log.fileno())
    return access


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _alive(path: Path) -> bool:
    try:
        pid = json.loads(path.read_text())["pid"]
        if type(pid) is not int or pid <= 0:
            return False
        os.kill(pid, 0)
    except (OSError, ValueError, TypeError, KeyError):
        return False
    return True


def ensure_running(path: Path, default_deadline: float) -> None:
    """Start once, or require a live holder of the watchdog process lock."""
    _identity()
    ready = path.with_suffix(".ready")
    with path.with_suffix(".process-lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not _alive(ready):
                raise PodWatchdogError("startup_failed") from None
            _check_startup(ready)
            return
    ready.unlink(missing_ok=True)
    # Accepted limit: this detached process has no independent supervisor;
    # death after acknowledgement is detected only by a later publication.
    child = subprocess.Popen([sys.executable, str(PROGRAM_PATH), "watch", str(default_deadline)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    until = time.monotonic() + 3
    while time.monotonic() < until:
        try:
            _check_startup(ready)
        except PodWatchdogError:
            child.wait(timeout=1)
            raise
        if _alive(ready) and child.poll() is None:
            return
        if child.poll() is not None:
            break
        time.sleep(0.02)
    _check_startup(ready)
    child.kill()
    child.wait(timeout=1)
    raise PodWatchdogError("startup_failed")


def _check_startup(ready: Path) -> None:
    try:
        cause = json.loads(ready.read_text()).get("cause")
    except (OSError, ValueError, TypeError):
        return
    if cause in {"termination_denied", "termination_unknown"}:
        raise PodWatchdogError(cause)


def publish(path: Path, deadline: float, generation: int) -> None:
    """Serialize atomic updates; an old host message cannot undo a renewal."""
    _deadline({"deadline": deadline, "generation": generation})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".update-lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.with_suffix(".terminating").exists():
            raise PodWatchdogError("termination_committed")
        try:
            old_deadline, old_generation = _deadline(json.loads(path.read_text()))
        except (OSError, ValueError, TypeError, KeyError):
            old_generation = 0
            old_deadline = deadline
        if old_generation > generation or (old_generation == generation and old_deadline != deadline):
            raise PodWatchdogError("stale_generation")
        _atomic_write(path, {"deadline": deadline, "generation": generation})
        # Persist and validate the replacement's input before starting it.
        if _deadline(json.loads(path.read_text())) != (deadline, generation):
            raise PodWatchdogError("startup_failed")
        ensure_running(path, deadline)


def run(path: Path, default_deadline: float) -> None:
    pod, key = _identity()
    with path.with_suffix(".process-lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        access = _report_identity()
        if access != "reachable":
            cause = "termination_denied" if access == "denied" else "termination_unknown"
            _atomic_write(path.with_suffix(".ready"), {"cause": cause})
            raise PodWatchdogError(cause)
        watch = Watchdog(path, default_deadline, terminate=lambda: _request(pod, key))
        _atomic_write(path.with_suffix(".ready"), {"pid": os.getpid()})
        with IDENTITY_LOG_PATH.open("a", encoding="utf-8") as log:
            log.write("pod_watchdog: watchdog=armed delete_permission=unproven_until_used\n")
            log.flush()
            os.fsync(log.fileno())
        retry_seconds = 1.0
        while True:
            try:
                # Accepted limit: deadlines use host/pod wall clocks without
                # offset validation or a monotonic bound against clock steps.
                if watch.tick(time.time()):
                    return
            except (OSError, URLError):
                # Retry until accepted or the pod disappears; never stop first.
                time.sleep(retry_seconds)
                retry_seconds = min(30.0, retry_seconds * 2)
                continue
            time.sleep(min(1, max(0.1, watch.deadline - time.time())))


def write_deadline(ops: Any, deadline: float, generation: int) -> bool:
    """Bootstrap the watchdog and publish a bound over the existing transport."""
    from astrid.core.contracts.errors import AstridError

    _deadline({"deadline": deadline, "generation": generation})
    source_text = (Path(__file__).read_text()
                   .replace('"__POD_URL_TEMPLATE__"', repr(POD_URL_TEMPLATE), 1)
                   .replace('"__POD_TERMINATE_METHOD__"', repr(POD_TERMINATE_METHOD), 1)
                   .replace('"__TLS_POLICY_SOURCE__"',
                            repr(Path(_tls_policy.__file__).read_text()), 1))
    source = base64.b64encode(source_text.encode()).decode("ascii")
    loader = (
        "import base64,pathlib; "
        f"p=pathlib.Path({str(PROGRAM_PATH)!r}); p.parent.mkdir(parents=True,exist_ok=True); "
        f"p.write_bytes(base64.b64decode({source!r})); "
        "scope={'__name__':'__pod_watchdog__'}; exec(compile(p.read_bytes(),str(p),'exec'),scope); "
        f"scope['main'](['publish',{deadline!r},{generation!r}])")
    try:
        code, stdout, _ = ops.exec("python3 -c " + shlex.quote(loader), 10)
    except (AstridError, OSError, RuntimeError, ValueError) as exc:
        raise PodWatchdogTransportError("RunPod claim refused: pod watchdog transport failed; release the pod and claim again") from exc
    if code != 0:
        if stdout.strip() in {"pod_watchdog:termination_unknown", "pod_watchdog:termination_denied"}:
            print("RunPod pod watchdog unavailable: this pod can't stop itself", file=sys.stderr)
            return False
        cause = next((name for name in ("RUNPOD_POD_ID", "RUNPOD_API_KEY", "stale_generation",
                                      "termination_committed")
                      if stdout.strip() == "pod_watchdog:" + name), "startup_failed")
        detail = "missing " + cause if cause.startswith("RUNPOD_") else cause
        raise PodWatchdogError(
            f"RunPod claim refused: pod watchdog {detail}; use a template with pod identity and claim again")
    return True


def sync_deadline(record: Mapping[str, Any], path: Path, deadline: float) -> dict[str, Any]:
    from astrid.core.contracts.errors import AstridError
    from astrid.packs.runpod.remote_placement import connect_claimed_for_preparation
    from astrid.packs.runpod.worker_preparation import replace_target_record

    generation = record.get("self_stop", {}).get("generation", 0) + 1
    value = {**record, "self_stop": {"deadline": deadline, "generation": generation,
                                    "acknowledged": False}}
    # Accepted limit: a crash after saving a shorter host idle deadline but
    # before publication leaves the older pod deadline, bounded by paid expiry.
    replace_target_record(path, value, operation_id=record["operation_id"])
    try:
        ops = connect_claimed_for_preparation(value).ops
    except (AstridError, OSError, RuntimeError, ValueError, KeyError) as exc:
        raise PodWatchdogError("RunPod claim refused: pod watchdog transport unavailable; release the pod and claim again") from exc
    value["self_stop"]["acknowledged"] = write_deadline(ops, deadline, generation)
    replace_target_record(path, value, operation_id=record["operation_id"])
    if value["self_stop"]["acknowledged"] is not True:
        raise PodWatchdogError("RunPod paid claim refused: pod watchdog was not acknowledged")
    return value


def main(args: list[Any]) -> None:
    try:
        if args[0] == "watch":
            run(DEADLINE_PATH, float(args[1]))
        else:
            publish(DEADLINE_PATH, float(args[1]), int(args[2]))
    except (OSError, ValueError, PodWatchdogError) as exc:
        cause = str(exc) if isinstance(exc, PodWatchdogError) else "startup_failed"
        print("pod_watchdog:" + cause, flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main(sys.argv[1:])
