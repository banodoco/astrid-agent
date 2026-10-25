"""Physical-pod process ownership and private credential delivery.

This is a process boundary, not a task ledger or a readiness oracle. The Linux
host itself holds an inherited flock descriptor through exec. A coordinator or
SSH client can disappear without releasing it. All coordinators must use the
same canonical ownership root on a physical pod; runtime/executor identity is
in the marker, never in the lock key. Stopping requires Linux pidfd support;
there is deliberately no kill-by-PID or process-group fallback.

The Pod adapter uses exec_ssh and open_ssh_client().open_sftp(), both present in
the checked-in runpod_lifecycle-0.3.1.dev0 wheel (pod.py). upload_path is a tree
uploader and is unsuitable for a pre-reserved secret file. Credentials only go
through SFTP into an already-created 0600 file under a private operation root.
No credentials are enabled and no Runtime activation is committed here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import secrets
import select
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from runtime_protocol import platform
from runtime_protocol.dirfd import filesystem as os

from astrid.core.execution.platform_helpers import owned_by_current_user, process_control_report
from astrid.core.execution.process_group import process_birth_identity

OWNERSHIP_ROOT = "/tmp/astrid-runpod-worker-owners"
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:/@+-]{1,512}\Z")


class WorkerProcessError(RuntimeError):
    """A bounded, secret-free process or credential-boundary failure."""


class WorkerOwnerConflict(WorkerProcessError):
    """Another host owns this physical pod."""


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _identity(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise WorkerProcessError("ownership identifiers must be bounded secret-free identifiers")
    return value


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise WorkerProcessError("ownership path must be a canonical absolute path")
    return path


def _safe_path(value: str) -> Path:
    path = _absolute(value)
    for part in (path, *path.parents):
        if part.is_symlink():
            raise WorkerProcessError("ownership path traverses a symlink")
    return path


def _private_dir(path: Path, *, exclusive: bool = False) -> None:
    _safe_path(str(path))
    path.mkdir(mode=0o700, parents=True, exist_ok=not exclusive)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or not owned_by_current_user(path, info):
        raise WorkerProcessError("ownership directory has a different owner")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise WorkerProcessError("ownership directory must have mode 0700")


@dataclass(frozen=True)
class WorkerBinding:
    account_ref: str
    pod_id: str
    target: str | Mapping[str, Any]
    runtime_instance_id: str
    runtime_epoch: int
    runtime_session_id: str
    executor_id: str
    provider: str = "runpod"

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            if key == "target" and isinstance(value, Mapping):
                if not value:
                    raise WorkerProcessError("effective target must be explicit")
                def validate_target(item: Any, *, field: str) -> Any:
                    if isinstance(item, Mapping):
                        clean: dict[str, Any] = {}
                        for name, nested in item.items():
                            if not isinstance(name, str) or not name:
                                raise WorkerProcessError("effective target keys must be non-empty strings")
                            if any(
                                word in name.casefold()
                                for word in ("token", "secret", "credential", "password")
                            ):
                                raise WorkerProcessError("effective target must contain no secret fields")
                            clean[name] = validate_target(nested, field=f"{field}.{name}")
                        return clean
                    if isinstance(item, list):
                        return [validate_target(nested, field=f"{field}[]") for nested in item]
                    if item is None or type(item) in {str, int, float, bool}:
                        if isinstance(item, float) and not math.isfinite(item):
                            raise WorkerProcessError("effective target numbers must be finite")
                        if isinstance(item, str) and any(char in item for char in "\x00\r\n"):
                            raise WorkerProcessError("effective target contains a control character")
                        return item
                    raise WorkerProcessError("effective target must be JSON-compatible")

                normalized = validate_target(value, field="target")
                try:
                    json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
                except (TypeError, ValueError) as exc:
                    raise WorkerProcessError("effective target must be JSON-compatible") from exc
                object.__setattr__(self, "target", normalized)
            elif key == "runtime_epoch":
                if type(value) is not int or value <= 0:
                    raise WorkerProcessError("runtime epoch must be a positive integer")
            else:
                _identity(value)
        if self.provider != "runpod":
            raise WorkerProcessError("this process boundary only owns RunPod targets")

    @property
    def physical_key(self) -> str:
        return _digest([self.provider, self.account_ref, self.pod_id])[7:]


@dataclass(frozen=True)
class WorkerProcessHandle:
    binding: WorkerBinding
    incarnation: str
    marker_path: str
    lock_path: str
    pid: int
    birth_id: str
    pgid: int
    sid: int
    lock_fd: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkerProcessHandle:
        row = dict(value)
        row["binding"] = WorkerBinding(**row["binding"])
        _identity(row["incarnation"])
        for field in ("pid", "pgid", "sid", "lock_fd"):
            if type(row[field]) is not int or row[field] < (0 if field == "lock_fd" else 1):
                raise WorkerProcessError("invalid process identity in ownership marker")
        _identity(row["birth_id"])
        _absolute(row["marker_path"])
        _absolute(row["lock_path"])
        return cls(**row)


class TargetProcessLock:
    """Portable flock primitive; keep this FD open in the actual host after exec."""

    def __init__(self, binding: WorkerBinding, root: str = OWNERSHIP_ROOT) -> None:
        directory = _absolute(root)
        _private_dir(directory)
        self.path = directory / (binding.physical_key + ".lock")
        self.marker_path = directory / (binding.physical_key + ".json")
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(self.fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not owned_by_current_user(self.fd, info):
            os.close(self.fd)
            raise WorkerProcessError("unsafe ownership lock file")
        try:
            platform.lock_file(self.fd, blocking=False)
        except BlockingIOError as exc:
            os.close(self.fd)
            raise WorkerOwnerConflict("physical pod already has a managed host") from exc
        os.set_inheritable(self.fd, True)

    def close(self) -> None:
        # Do not LOCK_UN: another process may still hold this inherited open file
        # description. Closing this process's copy preserves the host's lock.
        os.close(self.fd)


@contextmanager
def _target_transition_guard(binding: WorkerBinding, root: str):
    """Serialize host launches with the durable pod-release fence."""
    directory = _absolute(root)
    _private_dir(directory)
    path = directory / (binding.physical_key + ".transition.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or not owned_by_current_user(fd, info, mode=0o600)):
            raise WorkerProcessError("unsafe physical target transition lock")
        try:
            platform.lock_file(fd, blocking=False)
        except BlockingIOError as exc:
            raise WorkerOwnerConflict("another physical target transition is in progress") from exc
        yield directory
    finally:
        try:
            platform.unlock_file(fd)
        finally:
            os.close(fd)


def _release_fence_path(binding: WorkerBinding, root: Path) -> Path:
    return root / (binding.physical_key + ".release.json")


def _read_release_fence(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1
                or not owned_by_current_user(path, info, mode=0o600)):
            raise WorkerProcessError("physical target release fence is unsafe")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerProcessError("physical target release fence is unreadable") from exc
    if not isinstance(value, dict):
        raise WorkerProcessError("physical target release fence is malformed")
    return value


def _write_release_fence(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(dict(value), sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".release-")
    try:
        platform.owner_only(fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def retire_owned_process_for_release(
    handle: WorkerProcessHandle, *, operation_id: str, ownership_root: str
) -> dict[str, Any]:
    """Persist the exact pod's terminal launch fence before its host is stopped."""
    _identity(operation_id)
    binding = handle.binding
    root = _absolute(ownership_root)
    if (Path(handle.marker_path) != root / (binding.physical_key + ".json")
            or Path(handle.lock_path) != root / (binding.physical_key + ".lock")):
        raise WorkerProcessError("release fence handle belongs to another physical owner root")
    with _target_transition_guard(binding, str(root)):
        path = _release_fence_path(binding, root)
        prior = _read_release_fence(path)
        expected_identity = {
            "schema_version": 1,
            "operation_id": operation_id,
            "provider": binding.provider,
            "account_ref": binding.account_ref,
            "pod_id": binding.pod_id,
            "incarnation": handle.incarnation,
        }
        if prior is not None:
            if any(prior.get(key) != value for key, value in expected_identity.items()):
                raise WorkerOwnerConflict("physical pod is fenced for a different release operation")
            fence_digest = _digest(prior)
        else:
            # The transition lock serializes this observation with every new
            # host launch. A running exact host proves ownership from its
            # kernel lock; after task retirement, acquire that same lock to
            # prove the old process is gone and no replacement is present.
            state = _exact_incarnation_state(handle)
            if state == "same":
                observe_owned_process(handle)
            elif state in {"absent", "different"}:
                marker_path = _safe_path(handle.marker_path)
                try:
                    marker_value = json.loads(marker_path.read_text())
                except FileNotFoundError:
                    marker_value = None
                except (OSError, ValueError, TypeError) as exc:
                    raise WorkerProcessError("physical owner marker is unresolved during release") from exc
                if marker_value is not None and WorkerProcessHandle.from_dict(marker_value) != handle:
                    raise WorkerOwnerConflict("physical pod has a different process marker")
                lock_path = _safe_path(handle.lock_path)
                lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    lock_info = os.fstat(lock_fd)
                    if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                            or not owned_by_current_user(lock_fd, lock_info)):
                        raise WorkerProcessError("physical owner lock file is unsafe")
                    try:
                        platform.lock_file(lock_fd, blocking=False)
                    except BlockingIOError as exc:
                        raise WorkerOwnerConflict("another process owns this physical RunPod target") from exc
                    if _exact_incarnation_state(handle) not in {"absent", "different"}:
                        raise WorkerProcessError("old process identity became unresolved during release")
                    fence = {**expected_identity, "recorded_at": time.time_ns()}
                    _write_release_fence(path, fence)
                    fence_digest = _digest(fence)
                finally:
                    try:
                        platform.unlock_file(lock_fd)
                    finally:
                        os.close(lock_fd)
            else:
                raise WorkerProcessError("exact process identity is unresolved during release")
            if state == "same":
                fence = {**expected_identity, "recorded_at": time.time_ns()}
                _write_release_fence(path, fence)
                fence_digest = _digest(fence)
        return {
            "retired": True,
            "operation_id": operation_id,
            "provider": binding.provider,
            "account_ref": binding.account_ref,
            "pod_id": binding.pod_id,
            "incarnation": handle.incarnation,
            "release_fence_digest": fence_digest,
        }


def process_snapshot(pid: int) -> dict[str, Any]:
    """Kernel-derived Linux identity, including boot + process start ticks."""
    if type(pid) is not int or pid <= 0:
        raise WorkerProcessError("invalid process PID")
    try:
        proc = Path("/proc") / str(pid)
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] in {"Z", "X"}:
            raise WorkerProcessError("owned process is no longer live")
        birth_id = process_birth_identity(pid)
        if not birth_id:
            raise WorkerProcessError("owned process birth identity is unavailable")
        return {
            "pid": pid,
            "birth_id": birth_id,
            "pgid": int(fields[2]),
            "sid": int(fields[3]),
        }
    except (OSError, IndexError, ValueError) as exc:
        raise WorkerProcessError("exact Linux process identity could not be measured") from exc


def _lock_owned_by(handle: WorkerProcessHandle) -> bool:
    info = os.stat(handle.lock_path, follow_symlinks=False)
    held = os.stat(f"/proc/{handle.pid}/fd/{handle.lock_fd}")
    if (info.st_dev, info.st_ino) != (held.st_dev, held.st_ino):
        return False
    expected = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
    for line in Path("/proc/locks").read_text().splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[1:4] == ["FLOCK", "ADVISORY", "WRITE"]:
            if fields[4] == str(handle.pid) and fields[5] == expected:
                return True
    return False


def observe_owned_process(handle: WorkerProcessHandle) -> dict[str, Any]:
    """Observe identity + actual kernel lock ownership, never marker readiness."""
    try:
        marker = WorkerProcessHandle.from_dict(
            json.loads(_safe_path(handle.marker_path).read_text())
        )
        if marker != handle:
            raise WorkerProcessError("ownership marker no longer matches the exact handle")
        observed = process_snapshot(handle.pid)
        expected = {key: getattr(handle, key) for key in ("pid", "birth_id", "pgid", "sid")}
        if observed != expected or not _lock_owned_by(handle):
            raise WorkerProcessError("owned process identity or kernel lock differs")
        if process_snapshot(handle.pid) != expected:
            raise WorkerProcessError("process changed during ownership observation")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise WorkerProcessError("owned process could not be observed exactly") from exc
    return {
        "target": handle.binding.target,
        "provider_identity": {
            "account_ref": handle.binding.account_ref,
            "pod_id": handle.binding.pod_id,
        },
        "process": observed,
        "runtime_instance_id": handle.binding.runtime_instance_id,
        "runtime_epoch": handle.binding.runtime_epoch,
        "runtime_session_id": handle.binding.runtime_session_id,
        "executor_id": handle.binding.executor_id,
        "incarnation": handle.incarnation,
    }


def stop_owned_process(
    handle: WorkerProcessHandle, *, timeout_seconds: float = 10
) -> dict[str, Any]:
    """Pin, signal, and prove the exact incarnation exited before acknowledging."""
    if not 0 < timeout_seconds <= 30:
        raise WorkerProcessError("exact stop timeout must be bounded")
    # Pods run a full kernel: this stays pidfd-only even in sandbox mode.
    if process_control_report().get("pidfd") != "available":
        raise WorkerProcessError("exact stop requires Linux pidfd support; no PID fallback")
    def stopped(*, already_exited: bool) -> dict[str, Any]:
        return {
            "stopped": True,
            "already_exited": already_exited,
            "pid": handle.pid,
            "birth_id": handle.birth_id,
            "incarnation": handle.incarnation,
        }

    state = _exact_incarnation_state(handle)
    if state in {"absent", "different"}:
        # The exact old process is gone. In particular, never signal a PID
        # which now belongs to a replacement incarnation.
        return stopped(already_exited=True)
    if state != "same":
        raise WorkerProcessError("exact process absence remains unresolved")
    try:
        fd = platform.pidfd_open(handle.pid)
    except ProcessLookupError:
        state = _exact_incarnation_state(handle)
        if state in {"absent", "different"}:
            return stopped(already_exited=True)
        raise WorkerProcessError("exact process handle could not be pinned") from None
    except OSError as exc:
        raise WorkerProcessError("exact process handle could not be pinned") from exc
    try:
        state = _exact_incarnation_state(handle)
        if state in {"absent", "different"}:
            return stopped(already_exited=True)
        if state != "same":
            raise WorkerProcessError("exact process identity became unresolved before stop")
        observe_owned_process(handle)
        try:
            platform.pidfd_send_signal(fd, platform.TERM)
        except ProcessLookupError:
            # It may exit between inspection and signal; prove exit on the same
            # pinned kernel handle rather than guessing from the integer PID.
            pass
        ready, _, _ = select.select([fd], [], [], timeout_seconds)
        if not ready:
            raise WorkerProcessError("exact owned process exit remains unresolved")
        state = _exact_incarnation_state(handle)
        if state == "same":
            raise WorkerProcessError("old process incarnation is still live after exit wait")
        if state == "unknown":
            raise WorkerProcessError("exact process exit remains unresolved")
    finally:
        os.close(fd)
    return stopped(already_exited=False)


def _exact_incarnation_state(handle: WorkerProcessHandle) -> str:
    """Classify the selected Linux PID as same, replaced, absent or uncertain."""
    proc = Path("/proc") / str(handle.pid)
    try:
        raw_stat = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if raw_stat[0] in {"Z", "X"}:
            return "absent"
        current_birth = process_birth_identity(handle.pid)
        if not current_birth:
            return "unknown"
    except WorkerProcessError:
        return "unknown"
    except (OSError, IndexError, UnicodeError):
        return "absent" if not proc.exists() else "unknown"
    return "same" if current_birth == handle.birth_id else "different"


def _exec_owned_host(request: Mapping[str, Any]) -> None:
    binding = WorkerBinding(**request["binding"])
    lock = TargetProcessLock(binding, request["ownership_root"])
    process = process_snapshot(os.getpid())
    handle = WorkerProcessHandle(
        binding,
        request["incarnation"],
        str(lock.marker_path),
        str(lock.path),
        lock_fd=lock.fd,
        **process,
    )
    # Atomic replace while the physical lock is held. This is only an identity
    # marker and cannot qualify the process as registered or ready.
    fd, temporary = tempfile.mkstemp(dir=lock.marker_path.parent, prefix=".owner-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(handle.as_dict(), stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, lock.marker_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if request.get("cwd"):
        os.chdir(request["cwd"])
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    environment.update(request.get("env_items", {}))
    os.execvpe(request["argv"][0], request["argv"], environment)


def launch_owned_process(request: Mapping[str, Any]) -> dict[str, Any]:
    """Detach one host, with no supervisor and no parent-held ownership lock."""
    binding = WorkerBinding(**request["binding"])
    _identity(request["incarnation"])
    root = _absolute(request["ownership_root"])
    _private_dir(root)
    with _target_transition_guard(binding, str(root)):
        if _read_release_fence(_release_fence_path(binding, root)) is not None:
            raise WorkerOwnerConflict("physical pod is retired for release")
        payload = base64.b64encode(json.dumps(request, sort_keys=True).encode()).decode()
        child = platform.spawn_tree(
            [request["python_executable"], str(Path(__file__).resolve()), "--owned-host", payload],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            detached=True,
            close_fds=True,
        )
        marker_path = root / (binding.physical_key + ".json")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                row = json.loads(marker_path.read_text())
                if row.get("incarnation") == request["incarnation"]:
                    handle = WorkerProcessHandle.from_dict(row)
                    observe_owned_process(handle)
                    return handle.as_dict()
            except (OSError, ValueError, WorkerProcessError):
                pass
            if child.poll() is not None:
                raise WorkerOwnerConflict("host did not acquire physical pod ownership")
            time.sleep(0.02)
        # Never guess a PID or kill a process after an ambiguous start. Reconcile
        # using the physical marker and an exact handle on the next call.
        raise WorkerProcessError("host launch is unresolved; inspect physical ownership before retry")


class WorkerProcessTransport(Protocol):
    async def call(self, operation: str, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    async def upload_private(self, local_path: Path, remote_path: str) -> None: ...


class RunPodWorkerProcessTransport:
    """Lifecycle Pod command/SFTP adapter; never puts credential bytes in SSH argv."""

    def __init__(
        self,
        pod: Any,
        *,
        account_ref: str,
        source_root: str,
        python_executable: str,
        timeout_seconds: int = 30,
    ) -> None:
        self.pod = pod
        self.account_ref = _identity(account_ref)
        self.source_root = str(_absolute(source_root))
        self.python_executable = str(_absolute(python_executable))
        self.timeout_seconds = timeout_seconds

    async def call(self, operation: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation not in {
            "launch",
            "reconcile",
            "observe",
            "stop",
            "retire_for_release",
            "reserve_secret",
            "verify_secret",
            "remove_secret",
            "remove_unissued_secret",
            "bootstrap",
            "read_ready",
            "inspect_source",
        }:
            raise WorkerProcessError("unsupported worker process operation")
        binding = request.get("binding") or request.get("handle", {}).get("binding")
        if binding and (
            binding["account_ref"] != self.account_ref or binding["pod_id"] != self.pod.id
        ):
            raise WorkerProcessError("selected provider/account/pod differs from the SSH transport")
        wrapper = (
            "import base64,json,sys; sys.path.insert(0,sys.argv[1]); "
            "from astrid.packs.runpod.worker_process import remote_dispatch; "
            "result=remote_dispatch(sys.argv[2],json.loads(base64.b64decode(sys.argv[3]))); "
            "print(json.dumps(result,sort_keys=True))"
        )
        payload = base64.b64encode(json.dumps(request, sort_keys=True).encode()).decode()
        command = " ".join(
            shlex.quote(value)
            for value in [
                self.python_executable,
                "-c",
                wrapper,
                self.source_root,
                operation,
                payload,
            ]
        )
        try:
            code, stdout, _stderr = await self.pod.exec_ssh(command, timeout=self.timeout_seconds)
        except Exception:  # noqa: BLE001 — provider errors must remain secret-free
            raise WorkerProcessError("private worker transport failed") from None
        if code:
            raise WorkerProcessError("remote worker operation failed")
        try:
            result = json.loads(stdout.strip().splitlines()[-1])
        except (ValueError, IndexError, TypeError):
            raise WorkerProcessError("remote worker operation returned invalid evidence") from None
        if not isinstance(result, dict):
            raise WorkerProcessError("remote worker operation returned invalid evidence")
        if result.get("__worker_error__") == "WorkerOwnerConflict":
            raise WorkerOwnerConflict("physical pod already has a different managed host")
        if "__worker_error__" in result:
            raise WorkerProcessError("remote worker operation failed")
        return result

    async def upload_private(self, local_path: Path, remote_path: str) -> None:
        def upload() -> None:
            client = self.pod.open_ssh_client()
            try:
                sftp = client.open_sftp()
                try:
                    # r+b neither creates nor truncates: reserve_secret already
                    # created the exact empty 0600 target in an exclusive 0700 dir.
                    with sftp.open(remote_path, "r+b") as remote, local_path.open("rb") as local:
                        for block in iter(lambda: local.read(65536), b""):
                            remote.write(block)
                        remote.flush()
                finally:
                    sftp.close()
            finally:
                client.close()

        try:
            await asyncio.to_thread(upload)
        except Exception:  # noqa: BLE001 — provider errors must remain secret-free
            # Provider exceptions can include application data. Never expose them.
            raise WorkerProcessError("private credential upload failed") from None


def _secret_fact(path: Path) -> dict[str, Any]:
    fd = os.open(_safe_path(str(path)), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not owned_by_current_user(fd, info, mode=0o600)
        ):
            raise WorkerProcessError("credential target is not an owned regular 0600 file")
        digest = hashlib.sha256()
        with os.fdopen(os.dup(fd), "rb") as stream:
            for block in iter(lambda: stream.read(65536), b""):
                digest.update(block)
        after = os.fstat(fd)
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise WorkerProcessError("credential file changed during verification")
        current = os.stat(path, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise WorkerProcessError("credential pathname changed during verification")
        return {
            "path": str(path),
            "device": info.st_dev,
            "inode": info.st_ino,
            "mode": 0o600,
            "size": info.st_size,
            "sha256": "sha256:" + digest.hexdigest(),
        }
    finally:
        os.close(fd)


def _read_ready_file(path_value: str) -> dict[str, Any] | None:
    path = _safe_path(path_value)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not owned_by_current_user(fd, info) or info.st_size > 1024 * 1024:
            raise WorkerProcessError("host readiness file is not a bounded owned regular file")
        with os.fdopen(os.dup(fd), "rb") as stream:
            payload = stream.read(1024 * 1024 + 1)
        current = os.stat(path, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise WorkerProcessError("host readiness file changed during observation")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise WorkerProcessError("host readiness file is malformed")
        return value
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerProcessError("host readiness file could not be observed") from exc
    finally:
        os.close(fd)


def _inspect_executor_source(request: Mapping[str, Any]) -> dict[str, Any]:
    """Measure the selected remote pack source under its exact live process owner."""
    handle = WorkerProcessHandle.from_dict(request["handle"])
    observe_owned_process(handle)
    source_root = _absolute(request["source_root"])
    roots_value = request.get("pack_roots")
    if not isinstance(roots_value, list) or not roots_value:
        raise WorkerProcessError("selected remote pack roots are missing")
    roots = []
    for raw in roots_value:
        root = _absolute(raw)
        if not root.is_relative_to(source_root) or root == source_root:
            raise WorkerProcessError("selected pack root is outside the owned source tree")
        roots.append(root)
    sys.path[:] = [entry for entry in sys.path if entry != str(source_root)]
    sys.path.insert(0, str(source_root))
    try:
        from astrid.core.execution.generic_host import GenericPackHost, _canonical_digest

        records = GenericPackHost(pack_roots=roots).discover()
        result = {
            "source_digest": _canonical_digest({item.id: item.source_digest for item in records}),
            "dependency_digest": _canonical_digest({item.id: item.dependency_digest for item in records}),
            "capabilities": [
                {
                    "capability_id": item.id,
                    "definition_digest": item.capability_digest,
                    "status": "ready" if item.ready else "unready",
                }
                for item in records
            ],
        }
    except Exception as exc:
        raise WorkerProcessError("selected remote executor source could not be measured") from exc
    if observe_owned_process(handle).get("process") != {
        "pid": handle.pid,
        "birth_id": handle.birth_id,
        "pgid": handle.pgid,
        "sid": handle.sid,
    }:
        raise WorkerProcessError("owned host changed during remote source inspection")
    return result


def _bootstrap_host(request: Mapping[str, Any]) -> dict[str, Any]:
    """Send the existing bounded activation frame over the host's local socket."""
    from astrid.core.execution.generic_host import (
        _ACTIVATION_ACCEPTED_VERSION,
        _ACTIVATION_BOOTSTRAP_MODE,
        _ACTIVATION_VERSION,
        _read_activation_frame,
        _send_activation_frame,
    )

    handle = WorkerProcessHandle.from_dict(request["handle"])
    observed = observe_owned_process(handle)
    operation_id = _identity(request["operation_id"])
    channel_id = _identity(request["channel_id"])
    activation_id = _identity(request["activation_id"])
    incarnation = _identity(request["executor_incarnation"])
    if incarnation != handle.incarnation:
        raise WorkerProcessError("bootstrap grant names a different process incarnation")
    evidence_digest = request["evidence_digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(evidence_digest)):
        raise WorkerProcessError("bootstrap grant evidence digest is invalid")
    credential_file = request["credential_file"]
    credential_path = _absolute(
        credential_file[5:] if isinstance(credential_file, str) and credential_file.startswith("file:")
        else credential_file
    )
    expected_file = request.get("file_identity")
    if not isinstance(expected_file, Mapping):
        raise WorkerProcessError("bootstrap credential identity is missing")
    actual_file = _secret_fact(credential_path)
    if any(actual_file.get(key) != expected_file.get(key) for key in (
        "path", "device", "inode", "mode", "size", "sha256",
    )):
        raise WorkerProcessError("bootstrap credential identity changed")

    socket_path = _safe_path(request["socket_path"])
    if len(os.fsencode(socket_path)) >= 108:
        raise WorkerProcessError("activation socket path exceeds the Linux Unix-socket limit")
    try:
        socket_info = socket_path.lstat()
        parent_info = socket_path.parent.stat()
    except OSError as exc:
        raise WorkerProcessError("activation socket is unavailable") from exc
    if (not stat.S_ISSOCK(socket_info.st_mode) or not owned_by_current_user(socket_path, socket_info)
            or not stat.S_ISDIR(parent_info.st_mode) or not owned_by_current_user(socket_path.parent, parent_info, mode=0o700)):
        raise WorkerProcessError("activation socket identity is unsafe")

    grant = {
        "version": _ACTIVATION_VERSION,
        "operation_id": operation_id,
        "channel_id": channel_id,
        "credential_file": str(credential_path),
        "executor_incarnation": incarnation,
        "evidence_digest": evidence_digest,
        "host": {"pid": observed["process"]["pid"], "birth_id": observed["process"]["birth_id"]},
        "acceptance_mode": _ACTIVATION_BOOTSTRAP_MODE,
        "activation_id": activation_id,
    }
    expected = {
        "version": _ACTIVATION_ACCEPTED_VERSION,
        "operation_id": operation_id,
        "channel_id": channel_id,
        "activation_id": activation_id,
        "executor_incarnation": incarnation,
        "evidence_digest": evidence_digest,
        "host": grant["host"],
    }
    control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        control.settimeout(float(request.get("timeout_seconds", 30)))
        control.connect(str(socket_path))
        _send_activation_frame(control, grant)
        control.shutdown(socket.SHUT_WR)
        acknowledgement = _read_activation_frame(control)
        if acknowledgement != expected or control.recv(1):
            raise WorkerProcessError("remote host activation acknowledgement differs")
    except (OSError, TimeoutError) as exc:
        raise WorkerProcessError("remote host activation exchange failed") from exc
    finally:
        control.close()
    return {
        "activation_id": activation_id,
        "executor_incarnation": incarnation,
        "evidence_digest": evidence_digest,
    }


def dispatch(operation: str, request: Mapping[str, Any]) -> dict[str, Any]:
    """Remote, bounded operations; the request contains metadata only."""
    if operation == "launch":
        return launch_owned_process(request)
    if operation == "retire_for_release":
        handle = WorkerProcessHandle.from_dict(request["handle"])
        return retire_owned_process_for_release(
            handle,
            operation_id=request["operation_id"],
            ownership_root=request["ownership_root"],
        )
    if operation == "reconcile":
        binding = WorkerBinding(**request["binding"])
        root = _absolute(request["ownership_root"])
        marker = root / (binding.physical_key + ".json")
        if not marker.exists():
            return {"handle": None}
        try:
            handle = WorkerProcessHandle.from_dict(json.loads(_safe_path(str(marker)).read_text()))
            if handle.marker_path != str(marker) or handle.lock_path != str(
                root / (binding.physical_key + ".lock")
            ):
                raise WorkerProcessError("physical ownership marker has aliased paths")
            observe_owned_process(handle)
        except WorkerProcessError:
            # A stale identity marker grants no ownership; a new launch still has
            # to acquire the physical flock. Never stop a stale/guessed process.
            return {"handle": None}
        if handle.binding != binding:
            raise WorkerOwnerConflict("physical pod is owned by a different runtime/executor")
        return {"handle": handle.as_dict()}
    if operation in {"observe", "stop"}:
        handle = WorkerProcessHandle.from_dict(request["handle"])
        return (
            observe_owned_process(handle) if operation == "observe" else stop_owned_process(handle)
        )
    if operation == "bootstrap":
        return _bootstrap_host(request)
    if operation == "read_ready":
        handle = WorkerProcessHandle.from_dict(request["handle"])
        observe_owned_process(handle)
        ready = _read_ready_file(request["ready_file"])
        return {"ready": ready}
    if operation == "inspect_source":
        return _inspect_executor_source(request)
    if operation == "reserve_secret":
        root = _absolute(request["operation_root"])
        try:
            _private_dir(root, exclusive=True)
        except FileExistsError as exc:
            raise WorkerProcessError(
                "credential operation directory already exists; refusing ambiguous reuse"
            ) from exc
        path = root / "executor.token"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        return _secret_fact(path)
    if operation in {"verify_secret", "remove_secret"}:
        original = request["file_identity"]
        path = _absolute(original["path"])
        try:
            fact = _secret_fact(path)
        except FileNotFoundError:
            if operation == "remove_secret":
                # Exact removal is retry-safe after a lost transport reply.
                # A replacement at this path still fails the identity check below.
                try:
                    _safe_path(str(path.parent))
                    path.parent.rmdir()
                except FileNotFoundError:
                    pass
                return {"removed": True}
            raise
        if any(fact[key] != original[key] for key in ("path", "device", "inode", "mode")):
            raise WorkerProcessError("credential target identity differs from the reserved file")
        if operation == "remove_secret":
            path.unlink()
            try:
                path.parent.rmdir()
            except FileNotFoundError:
                pass
            return {"removed": True}
        if fact["sha256"] != request["sha256"] or fact["size"] != request["size"]:
            raise WorkerProcessError("credential target hash or size differs")
        return fact
    if operation == "remove_unissued_secret":
        destination = _absolute(request["credential_path"])
        if destination.name != "executor.token" or destination.parent == Path("/"):
            raise WorkerProcessError("unissued credential path is invalid")
        root = destination.parent
        try:
            _safe_path(str(root))
            root_info = root.lstat()
        except FileNotFoundError:
            return {"removed": True}
        if (not stat.S_ISDIR(root_info.st_mode) or root.is_symlink()
                or not owned_by_current_user(root, root_info, mode=0o700)):
            raise WorkerProcessError("unissued credential directory identity is unsafe")
        try:
            entries = list(root.iterdir())
        except OSError as exc:
            raise WorkerProcessError("unissued credential directory is unreadable") from exc
        if entries != [destination]:
            raise WorkerProcessError("unissued credential directory contains unexpected files")
        fact = _secret_fact(destination)
        if (fact.get("size") != 0
                or fact.get("sha256") != "sha256:" + hashlib.sha256(b"").hexdigest()):
            raise WorkerProcessError("unissued credential file is not empty")
        destination.unlink()
        root.rmdir()
        return {"removed": True}
    raise WorkerProcessError("unsupported worker process operation")


def remote_dispatch(operation: str, request: Mapping[str, Any]) -> dict[str, Any]:
    """Sanitize transport errors without exposing provider or credential data."""
    try:
        return dispatch(operation, request)
    except WorkerOwnerConflict:
        return {"__worker_error__": "WorkerOwnerConflict"}
    except Exception:  # noqa: BLE001 — provider errors must remain secret-free
        return {"__worker_error__": "WorkerProcessError"}


class WorkerProcessOwner:
    """Injectable physical-process boundary used by a Runtime preparer."""

    def __init__(
        self,
        transport: WorkerProcessTransport,
        binding: WorkerBinding,
        *,
        ownership_root: str = OWNERSHIP_ROOT,
    ) -> None:
        self.transport = transport
        self.binding = binding
        self.ownership_root = str(_absolute(ownership_root))

    async def launch_projected(
        self, preparation_launch: Any, *, incarnation: str | None = None
    ) -> WorkerProcessHandle:
        """Accept Runtime's RemotePreparationLaunch without importing authority."""
        reference, launch = preparation_launch.reference, preparation_launch.launch
        if (
            launch.deployment_digest != reference.digest()
            or reference.effective_target != self.binding.target
            or reference.runtime_instance_id != self.binding.runtime_instance_id
            or reference.runtime_epoch != self.binding.runtime_epoch
            or reference.executor_id != self.binding.executor_id
        ):
            raise WorkerProcessError("projected launch differs from the exact selected binding")
        return await self.launch(
            python_executable=str(reference.executable.path),
            argv=list(launch.argv),
            cwd=str(launch.cwd),
            env_items=dict(launch.env_items),
            incarnation=incarnation,
        )

    async def launch(
        self,
        *,
        python_executable: str,
        argv: list[str],
        cwd: str | None = None,
        env_items: Mapping[str, str] | None = None,
        incarnation: str | None = None,
    ) -> WorkerProcessHandle:
        # ProjectedLaunch may contain nonsecret configuration. Credential material
        # must be handed over separately using deliver_credential, as file paths.
        if not argv or any(not isinstance(arg, str) or not arg for arg in argv):
            raise WorkerProcessError("host argv must be explicit and secret-free")
        # Exact keys emitted by Runtime remote_worker_deployment.project_launch,
        # plus the interpreter's explicit nonsecret path configuration.
        safe_env = {
            "PATH",
            "PYTHONPATH",
            "PYTHONUNBUFFERED",
            "PYTHONDONTWRITEBYTECODE",
            "ASTRID_DEPLOYMENT_ID",
            "ASTRID_DEPLOYMENT_REVISION",
            "ASTRID_DEPLOYMENT_DIGEST",
            "ASTRID_TASK_ID",
            "ASTRID_RUN_ID",
            "ASTRID_PROJECT_ID",
            "ASTRID_IDEMPOTENCY_KEY",
            "ASTRID_ADMISSION_DIGEST",
            "ASTRID_SPEC_DIGEST",
            "ASTRID_REQUEST_DIGEST",
            "ASTRID_CAPABILITY_ID",
            "ASTRID_CAPABILITY_DIGEST",
            "ASTRID_TARGET_REF",
            "ASTRID_ORIGINAL_TARGET_JSON",
            "ASTRID_EFFECTIVE_TARGET_JSON",
            "ASTRID_PLACEMENT_VERSION",
            "ASTRID_RUNTIME_ENDPOINT",
            "ASTRID_RUNTIME_INSTANCE_ID",
            "ASTRID_RUNTIME_EPOCH",
            "ASTRID_RUNTIME_SCHEMA_DIGEST",
            "ASTRID_VIBECOMFY_MODELS_ROOT",
            "ASTRID_SESSION_REF",
            "ASTRID_SESSION_CONFIG_DIGEST",
            "ASTRID_OUTPUT_ROOT",
            "ASTRID_CREDENTIAL_REF",
            "ASTRID_SOURCE_CLOSURE_DIGEST",
            "ASTRID_RUNTIME_DATA_ROOT",
            "ASTRID_EXECUTION_TARGET_JSON",
            "ASTRID_RECOVERY_DECISION_DIGEST",
            "ASTRID_INPUT_BINDINGS_JSON",
        }
        if set(env_items or {}) - safe_env:
            raise WorkerProcessError("host environment must use the explicit nonsecret allowlist")
        row = await self.transport.call(
            "launch",
            {
                "binding": asdict(self.binding),
                "ownership_root": self.ownership_root,
                "incarnation": _identity(incarnation or secrets.token_hex(16)),
                "python_executable": str(_absolute(python_executable)),
                "argv": argv,
                "cwd": str(_absolute(cwd)) if cwd else None,
                "env_items": dict(env_items or {}),
            },
        )
        handle = WorkerProcessHandle.from_dict(row)
        if handle.binding != self.binding:
            raise WorkerProcessError("launched host binding differs from the selected binding")
        return handle

    async def reconcile(self) -> WorkerProcessHandle | None:
        """Re-observe a matching physical owner across independent coordinators."""
        result = await self.transport.call(
            "reconcile", {"binding": asdict(self.binding), "ownership_root": self.ownership_root}
        )
        if result.get("handle") is None:
            return None
        handle = WorkerProcessHandle.from_dict(result["handle"])
        self._require_handle(handle)
        return handle

    async def observe(self, handle: WorkerProcessHandle) -> Mapping[str, Any]:
        self._require_handle(handle)
        return await self.transport.call("observe", {"handle": handle.as_dict()})

    async def stop(self, handle: WorkerProcessHandle) -> Mapping[str, Any]:
        self._require_handle(handle)
        return await self.transport.call("stop", {"handle": handle.as_dict()})

    async def retire_for_release(
        self, handle: WorkerProcessHandle, *, operation_id: str
    ) -> Mapping[str, Any]:
        """Fence future launches on this exact pod before stopping its host."""
        self._require_handle(handle)
        result = await self.transport.call("retire_for_release", {
            "handle": handle.as_dict(),
            "operation_id": _identity(operation_id),
            "ownership_root": self.ownership_root,
        })
        expected = {
            "retired": True,
            "operation_id": operation_id,
            "provider": self.binding.provider,
            "account_ref": self.binding.account_ref,
            "pod_id": self.binding.pod_id,
            "incarnation": handle.incarnation,
        }
        if any(result.get(key) != value for key, value in expected.items()):
            raise WorkerProcessError("release fence receipt differs from the exact physical worker")
        digest = result.get("release_fence_digest")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise WorkerProcessError("release fence receipt has no canonical digest")
        return dict(result)

    async def bootstrap(
        self,
        handle: WorkerProcessHandle,
        *,
        socket_path: str,
        operation_id: str,
        channel_id: str,
        grant: Mapping[str, Any],
        file_identity: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._require_handle(handle)
        return await self.transport.call("bootstrap", {
            "handle": handle.as_dict(),
            "socket_path": socket_path,
            "operation_id": operation_id,
            "channel_id": channel_id,
            **dict(grant),
            "file_identity": dict(file_identity),
        })

    async def read_ready(
        self, handle: WorkerProcessHandle, ready_file: str
    ) -> Mapping[str, Any] | None:
        self._require_handle(handle)
        result = await self.transport.call(
            "read_ready", {"handle": handle.as_dict(), "ready_file": ready_file}
        )
        ready = result.get("ready")
        if ready is not None and not isinstance(ready, Mapping):
            raise WorkerProcessError("host readiness evidence is malformed")
        return ready

    async def inspect_source(
        self,
        handle: WorkerProcessHandle,
        *,
        source_root: str,
        pack_roots: list[str],
    ) -> Mapping[str, Any]:
        self._require_handle(handle)
        return await self.transport.call("inspect_source", {
            "handle": handle.as_dict(),
            "source_root": source_root,
            "pack_roots": pack_roots,
        })

    async def remove_credential(
        self, handle: WorkerProcessHandle, file_identity: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self._require_handle(handle)
        return await self.transport.call("remove_secret", {
            "file_identity": dict(file_identity), "binding": asdict(self.binding),
        })

    async def remove_unissued_credential(self, credential_ref: str) -> Mapping[str, Any]:
        """Remove only the empty reservation left before its identity was journaled."""
        destination = _absolute(
            credential_ref[5:] if credential_ref.startswith("file:") else credential_ref
        )
        if destination.name != "executor.token" or destination.parent == Path("/"):
            raise WorkerProcessError("unissued credential reference is invalid")
        return await self.transport.call("remove_unissued_secret", {
            "credential_path": str(destination), "binding": asdict(self.binding),
        })

    def _require_handle(self, handle: WorkerProcessHandle) -> None:
        if handle.binding != self.binding:
            raise WorkerProcessError(
                "process handle belongs to a different runtime/executor binding"
            )
        root = Path(self.ownership_root)
        if Path(handle.lock_path) != root / (self.binding.physical_key + ".lock") or Path(
            handle.marker_path
        ) != root / (self.binding.physical_key + ".json"):
            raise WorkerProcessError("process handle uses a different physical ownership root")

    async def deliver_credential(
        self,
        token: bytes,
        *,
        activation_id: str,
        incarnation: str,
        evidence_digest: str,
        credential_ref: str,
        reservation_callback: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> Mapping[str, Any]:
        """Deliver to the exact validated reference path and bind a private ACK.

        The caller validates credential_ref against its DeploymentReference.
        This boundary requires a fresh private parent and executor.token leaf;
        the path is selected before activation, independently of incarnation.
        No existing operation directory or ambiguous file is reused.
        """
        _identity(activation_id)
        _identity(incarnation)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", evidence_digest):
            raise WorkerProcessError("credential ACK requires the activation evidence digest")
        if not isinstance(token, bytes) or not token:
            raise WorkerProcessError("credential must be nonempty bytes")
        if not isinstance(credential_ref, str):
            raise WorkerProcessError("credential_ref must be an exact reference path")
        destination = _absolute(
            credential_ref[5:] if credential_ref.startswith("file:") else credential_ref
        )
        if destination.name != "executor.token" or destination.parent == Path("/"):
            raise WorkerProcessError(
                "credential destination requires an executor.token leaf in a private operation directory"
            )
        root = destination.parent
        reserved = await self.transport.call(
            "reserve_secret", {"operation_root": str(root), "binding": asdict(self.binding)}
        )
        if (
            reserved.get("path") != str(destination)
            or reserved.get("mode") != 0o600
            or reserved.get("size") != 0
            or reserved.get("sha256") != "sha256:" + hashlib.sha256(b"").hexdigest()
        ):
            raise WorkerProcessError("credential reservation returned the wrong or ambiguous file")
        if reservation_callback is not None:
            try:
                reservation_callback(dict(reserved))
            except BaseException as exc:
                try:
                    await self.transport.call(
                        "remove_secret",
                        {"file_identity": dict(reserved), "binding": asdict(self.binding)},
                    )
                except Exception:  # noqa: BLE001 — preserve the original custody write failure
                    pass
                if isinstance(exc, Exception):
                    raise WorkerProcessError("credential reservation could not be journaled") from None
                raise
        digest = "sha256:" + hashlib.sha256(token).hexdigest()
        try:
            # mkstemp + 0700 TemporaryDirectory; no token in argv, env, or marker.
            with tempfile.TemporaryDirectory(prefix="astrid-private-credential-") as temporary:
                local = Path(temporary) / "executor.token"
                fd = os.open(local, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(token)
                await self.transport.upload_private(local, str(destination))
            verified = await self.transport.call(
                "verify_secret",
                {
                    "file_identity": dict(reserved),
                    "sha256": digest,
                    "size": len(token),
                    "binding": asdict(self.binding),
                },
            )
            if (
                any(
                    verified.get(key) != reserved.get(key)
                    for key in ("path", "device", "inode", "mode")
                )
                or verified.get("sha256") != digest
                or verified.get("size") != len(token)
            ):
                raise WorkerProcessError("private credential verification ACK differs")
        except BaseException as exc:
            # Best-effort exact-file cleanup; an ambiguous replacement is never
            # removed. Local temporary material is cleaned on every exit path.
            try:
                await self.transport.call(
                    "remove_secret",
                    {"file_identity": dict(reserved), "binding": asdict(self.binding)},
                )
            except Exception:  # noqa: BLE001 — provider errors must remain secret-free
                pass
            if isinstance(exc, Exception):
                raise WorkerProcessError("private credential delivery failed") from None
            raise
        ack = {
            "activation_id": activation_id,
            "incarnation": incarnation,
            "evidence_digest": evidence_digest,
            "provider_identity": {
                "account_ref": self.binding.account_ref,
                "pod_id": self.binding.pod_id,
            },
            "file_identity": dict(verified),
        }
        return {**ack, "ack_digest": _digest(ack)}


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--owned-host":
        raise SystemExit(2)
    _exec_owned_host(json.loads(base64.b64decode(sys.argv[2])))
