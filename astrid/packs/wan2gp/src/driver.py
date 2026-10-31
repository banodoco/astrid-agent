"""Wan2GP adapter helpers for the host-owned native session.

The production path compiles typed inputs and asks GenericPackHost to invoke
the retained ``shared.api.init() → WanGPSession.submit_task()`` child.  Native
initialization and close therefore stay in the W2.1 host-owned interpreter;
this module never imports the heavy upstream runtime.  The fake persistent
runner below is fixture-only and never imports or starts the native engine.

This module deliberately avoids Worker/GW imports and does not depend on any
runtime database.  The host admission supplies the pinned Wan2GP checkout,
interpreter, config and lifecycle deadlines explicitly; if that owner artifact
is absent, GenericPackHost rejects admission before the child starts.
"""

import hashlib
import json
import shutil
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from runtime_protocol.dirfd import filesystem as os

from astrid.core._shared.result_manifest import build_manifest, write_manifest

from .compiler import compile_from_inputs, portable_digest, runner_fingerprint, warmth_identity

# W1.2 selected the unmodified official Python API candidate. The Windows
# candidate environment is pinned beside this pack; device qualification
# remains held until testers confirm it.
WAN2GP_PIN_SHA = "f3f204e50f6eeb73ce40d1dafc93bc97bfaa61e4"
WAN2GP_PIN_REF = "deepbeepmeep/Wan2GP"


class RunCancelled(RuntimeError):
    """Cooperative cancellation requested before a fake lifecycle step."""

    code = "cancelled"


class CancellationToken:
    """Small thread-safe cancellation seam shared by real and fake runners."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._reason: str | None = None

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def cancel(self, reason: str = "cancelled") -> bool:
        """Request cancellation, returning ``True`` only on the first request."""
        with self._lock:
            if self._cancelled:
                return False
            self._cancelled = True
            self._reason = str(reason) or "cancelled"
            return True

    def raise_if_cancelled(self) -> None:
        reason = self.reason
        if reason is not None:
            raise RunCancelled(reason)


@dataclass(frozen=True)
class CancellationPolicy:
    """Deterministic fake-work cancellation policy.

    ``cancel_after_steps=0`` cancels before the first unit of fake work.
    ``None`` disables policy cancellation; an explicit token can still cancel.
    """

    cancel_after_steps: int | None = None

    def __post_init__(self) -> None:
        if self.cancel_after_steps is not None and self.cancel_after_steps < 0:
            raise ValueError("cancel_after_steps must be non-negative")

    def should_cancel(self, step: int) -> bool:
        return (
            self.cancel_after_steps is not None
            and step >= self.cancel_after_steps
        )

    def apply(self, token: CancellationToken, step: int) -> None:
        if self.should_cancel(step):
            token.cancel(f"cancelled by policy at step {step}")


@dataclass(frozen=True)
class RunnerSnapshot:
    runner_id: str
    status: str
    fingerprint: str | None
    warmth_identity: str | None
    event_seq: int
    total_runs: int
    successful_runs: int
    cancelled_runs: int
    failed_runs: int
    last_event: str | None
    last_error: str | None


class PersistentRunnerState:
    """Append-only, deterministic JSONL state for the fake persistent runner."""

    SCHEMA_VERSION = 1
    COLD = "cold"
    WARM = "warm"
    CLOSED = "closed"

    def __init__(self, path: str | os.PathLike[str], runner_id: str = "wan2gp-fake") -> None:
        self.path = Path(path).expanduser().resolve()
        self.runner_id = str(runner_id)
        if not self.runner_id:
            raise ValueError("runner_id is required")

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "PersistentRunnerState":
        """Reopen an existing journal, deriving its runner id from its header."""
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_file():
            raise ValueError(f"runner state is empty: {resolved}")
        try:
            first = next(
                line for line in resolved.read_text(encoding="utf-8").splitlines() if line.strip()
            )
            header = json.loads(first)
            runner_id = header["runner_id"]
        except (OSError, StopIteration, KeyError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read runner state {resolved}: {exc}") from exc
        state = cls(resolved, runner_id=str(runner_id))
        if not state._events():
            raise ValueError(f"runner state is empty: {state.path}")
        return state

    def _events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            for line_number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                event = json.loads(line)
                if (
                    not isinstance(event, dict)
                    or event.get("schema_version") != self.SCHEMA_VERSION
                    or event.get("runner_id") != self.runner_id
                    or not isinstance(event.get("seq"), int)
                    or not isinstance(event.get("event"), str)
                ):
                    raise ValueError(f"invalid runner state record at line {line_number}")
                if event["seq"] != len(events) + 1:
                    raise ValueError(f"non-contiguous runner state at line {line_number}")
                events.append(event)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read runner state {self.path}: {exc}") from exc
        return events

    def _append(self, event: str, **payload: Any) -> None:
        events = self._events()
        if events and events[0]["runner_id"] != self.runner_id:
            raise ValueError("runner state belongs to another runner")
        record = {
            "event": event,
            "runner_id": self.runner_id,
            "schema_version": self.SCHEMA_VERSION,
            "seq": len(events) + 1,
            **payload,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _ensure_created(self) -> None:
        if not self._events():
            self._append("runner_created")

    @property
    def snapshot(self) -> RunnerSnapshot:
        events = self._events()
        status = self.COLD
        fingerprint = None
        warmth = None
        total = successful = cancelled = failed = 0
        last_event = last_error = None
        for event in events:
            kind = event["event"]
            last_event = kind
            if kind in {"runner_started", "runner_reused", "runner_reconfigured"}:
                status = self.WARM
                fingerprint = event.get("fingerprint", fingerprint)
                warmth = event.get("warmth_identity", warmth)
            elif kind == "runner_closed":
                status = self.CLOSED
            elif kind == "run_started":
                total += 1
                fingerprint = event.get("fingerprint", fingerprint)
                warmth = event.get("warmth_identity", warmth)
            elif kind == "run_succeeded":
                successful += 1
            elif kind == "run_cancelled":
                cancelled += 1
                last_error = event.get("reason")
            elif kind == "run_failed":
                failed += 1
                last_error = event.get("reason")
        return RunnerSnapshot(
            runner_id=self.runner_id,
            status=status,
            fingerprint=fingerprint,
            warmth_identity=warmth,
            event_seq=len(events),
            total_runs=total,
            successful_runs=successful,
            cancelled_runs=cancelled,
            failed_runs=failed,
            last_event=last_event,
            last_error=last_error,
        )

    def start(self, fingerprint: str, warmth_identity_value: str) -> RunnerSnapshot:
        self._ensure_created()
        current = self.snapshot
        payload = {
            "fingerprint": str(fingerprint),
            "warmth_identity": str(warmth_identity_value),
        }
        if current.status == self.WARM and current.fingerprint == fingerprint:
            self._append("runner_reused", **payload)
        else:
            self._append(
                "runner_reconfigured" if current.status == self.WARM else "runner_started",
                **payload,
            )
        return self.snapshot

    def begin_run(self, fingerprint: str, warmth_identity_value: str) -> None:
        if self.snapshot.status != self.WARM:
            raise RuntimeError("runner is not warm")
        self._append(
            "run_started",
            fingerprint=str(fingerprint),
            warmth_identity=str(warmth_identity_value),
        )

    def finish_run(self, status: str, *, reason: str | None = None) -> None:
        if status not in {"succeeded", "cancelled", "failed"}:
            raise ValueError("run status must be succeeded, cancelled, or failed")
        payload = {"reason": str(reason)} if reason is not None else {}
        self._append(f"run_{status}", **payload)

    def close(self) -> RunnerSnapshot:
        self._ensure_created()
        if self.snapshot.status != self.CLOSED:
            self._append("runner_closed")
        return self.snapshot

    def liveness_probe(self) -> dict[str, Any]:
        snapshot = self.snapshot
        alive = self.path.is_file() and snapshot.status == self.WARM
        return {
            "alive": alive,
            "event_seq": snapshot.event_seq,
            "fingerprint": snapshot.fingerprint,
            "runner_id": snapshot.runner_id,
            "status": snapshot.status,
            "warmth_identity": snapshot.warmth_identity,
        }

    def is_alive(self) -> bool:
        return bool(self.liveness_probe()["alive"])


@dataclass(frozen=True)
class FakeRunResult:
    status: str
    generated_files: list[str]
    errors: list[str]
    fingerprint: str
    warmth_identity: str
    runner_alive: bool
    containment_ok: bool
    cancelled: bool
    state: RunnerSnapshot


class FakePersistentRunner:
    """Fixture-only persistent runner; never imports or starts Wan2GP."""

    RUNNER_KIND = "wan2gp-cpu-fake"

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        output_root: str | os.PathLike[str] | None = None,
        runner_id: str = "wan2gp-fake",
        warmth_profile: str = "cpu-fake",
    ) -> None:
        self.state = PersistentRunnerState(state_path, runner_id)
        self.output_root = (
            Path(output_root).expanduser().resolve()
            if output_root is not None
            else self.state.path.parent / "outputs"
        )
        self.warmth_profile = str(warmth_profile)

    def liveness_probe(self) -> dict[str, Any]:
        return self.state.liveness_probe()

    def is_alive(self) -> bool:
        return self.state.is_alive()

    def run(
        self,
        settings: dict[str, Any],
        *,
        token: CancellationToken | None = None,
        policy: CancellationPolicy | None = None,
        work_steps: int = 1,
        output_name: str = "fake-output.json",
        escape_output: bool = False,
        fixture: dict[str, Any] | None = None,
    ) -> FakeRunResult:
        if work_steps < 0:
            raise ValueError("work_steps must be non-negative")
        token = token or CancellationToken()
        policy = policy or CancellationPolicy()
        fingerprint = runner_fingerprint(
            settings,
            runner_kind=self.RUNNER_KIND,
            engine_identity=f"wan2gp@{WAN2GP_PIN_SHA}",
        )
        warm_id = warmth_identity(
            settings,
            runner_kind=self.RUNNER_KIND,
            warmth_profile=self.warmth_profile,
            engine_identity=f"wan2gp@{WAN2GP_PIN_SHA}",
        )
        self.state.start(fingerprint, warm_id)
        self.state.begin_run(fingerprint, warm_id)
        try:
            for step in range(work_steps):
                policy.apply(token, step)
                token.raise_if_cancelled()
            policy.apply(token, work_steps)
            token.raise_if_cancelled()
            spool = self.output_root.expanduser().resolve()
            spool.mkdir(parents=True, exist_ok=True)
            candidate = (
                spool.parent / "escaped-fake-output.json"
                if escape_output
                else spool / output_name
            )
            if not _verify_within_spool(candidate, spool):
                error = f"output containment violated: {[str(candidate)]}"
                self.state.finish_run("failed", reason=error)
                return FakeRunResult(
                    status="failed",
                    generated_files=[],
                    errors=[error],
                    fingerprint=fingerprint,
                    warmth_identity=warm_id,
                    runner_alive=self.state.is_alive(),
                    containment_ok=False,
                    cancelled=False,
                    state=self.state.snapshot,
                )
            payload = fixture if fixture is not None else {
                "fixture": "wan2gp-cpu-fake",
                "fingerprint": fingerprint,
                "settings": settings,
            }
            candidate.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            self.state.finish_run("succeeded")
            return FakeRunResult(
                status="succeeded",
                generated_files=[str(candidate)],
                errors=[],
                fingerprint=fingerprint,
                warmth_identity=warm_id,
                runner_alive=self.state.is_alive(),
                containment_ok=True,
                cancelled=False,
                state=self.state.snapshot,
            )
        except RunCancelled as exc:
            self.state.finish_run("cancelled", reason=str(exc))
            return FakeRunResult(
                status="cancelled",
                generated_files=[],
                errors=[str(exc)],
                fingerprint=fingerprint,
                warmth_identity=warm_id,
                runner_alive=self.state.is_alive(),
                containment_ok=True,
                cancelled=True,
                state=self.state.snapshot,
            )

    def close(self) -> RunnerSnapshot:
        return self.state.close()


def fake_persistent_run(
    settings: dict[str, Any],
    state_path: str | os.PathLike[str],
    **kwargs: Any,
) -> FakeRunResult:
    """Run one deterministic fake attempt using a persisted runner journal."""
    return FakePersistentRunner(state_path).run(settings, **kwargs)


run_fake = fake_persistent_run


@dataclass(frozen=True)
class DriverResult:
    success: bool
    generated_files: list[str]
    errors: list[str]
    total_tasks: int
    successful_tasks: int
    failed_tasks: int
    disclosed_engine: dict[str, Any]
    spool: Path


def compile_host_settings(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Compile and validate native settings without importing upstream Wan.

    GenericPackHost calls this from the parent interpreter immediately before
    sending the settings to the already initialized W2.1 child.  The returned
    mapping is the native request; host/MTS identity stays in the control
    envelope rather than being mixed into it.
    """
    settings = compile_from_inputs(dict(inputs))
    return validate_settings(settings)


@contextmanager
def _custody_directory(path: Path, *, create: bool = False):
    """Walk with directory descriptors; never follow a symlink, even in parents."""
    path = path.expanduser().absolute()
    if ".." in path.parts:
        raise RuntimeError("Wan custody path contains traversal")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _file_observation(info: os.stat_result) -> list[int]:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError("native Wan output must be a private regular file")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


@contextmanager
def _native_output(raw: str, source_root: Path):
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = source_root / path
    if ".." in path.parts or not path.is_relative_to(source_root):
        raise RuntimeError("native Wan output escapes its owned spool")
    with _custody_directory(path.parent) as directory:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            _file_observation(os.fstat(stream.fileno()))
            yield path, stream


def snapshot_native_outputs(files: Any, source_root: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Observe exact terminal native files, without touching bytes or metadata."""
    if not isinstance(files, list) or any(not isinstance(raw, str) or not raw for raw in files):
        raise RuntimeError("native Wan generated_files must be an exact file list")
    root = Path(source_root).expanduser().absolute()
    snapshots: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for raw in files:
        with _native_output(raw, root) as (path, stream):
            before = _file_observation(os.fstat(stream.fileno()))
            identity = tuple(before[:2])
            if identity in seen:
                raise RuntimeError("duplicate native Wan output")
            seen.add(identity)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if _file_observation(os.fstat(stream.fileno())) != before:
                raise RuntimeError("native Wan output changed during terminal observation")
            snapshots.append({"path": str(path), "stat": before, "sha256": digest})
    return snapshots


def verify_host_result(mapped: Mapping[str, Any], *, attempt_root: Path) -> None:
    """Recheck staged bytes at the existing harvest/settlement boundaries."""
    if mapped.get("output_root") != str(attempt_root.absolute()):
        raise RuntimeError("Wan result belongs to another attempt root")
    expected = mapped.get("staged_outputs")
    if not isinstance(expected, list) or not expected:
        raise RuntimeError("Wan result has no observed staged outputs")
    observed = snapshot_native_outputs([item["path"] for item in expected], attempt_root)
    if observed != expected:
        raise RuntimeError("Wan staged output changed after terminal custody")


def materialize_host_result(
    evidence: Mapping[str, Any],
    *,
    attempt_root: str | os.PathLike[str],
    inputs: Mapping[str, Any],
    expected_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy exact native result files into one Runtime attempt spool.

    The caller holds the MTS admission until Runtime settlement. Native
    terminal observations are checked again during copying; this is only
    the handoff to the existing harvester, not another publication store.
    """
    result = evidence.get("result")
    if not isinstance(result, Mapping) or result.get("success") is not True:
        errors = result.get("errors", []) if isinstance(result, Mapping) else []
        raise RuntimeError("native Wan2GP job failed: " + "; ".join(str(item) for item in errors))
    if evidence.get("terminal") is not True or not evidence.get("native_job_id"):
        raise RuntimeError("native Wan result lacks terminal job evidence")
    if expected_identity is not None and any(evidence.get(key) != value for key, value in expected_identity.items()):
        raise RuntimeError("native Wan result belongs to another admission")
    source_root = Path(str(evidence["source_root"])).absolute()
    snapshots = evidence.get("output_snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        raise RuntimeError("native Wan result lacks terminal file observations")
    if snapshot_native_outputs(result.get("generated_files"), source_root) != snapshots:
        raise RuntimeError("native Wan output changed after terminal observation")
    compiled_settings = compile_host_settings(inputs)
    destination_root = Path(attempt_root).expanduser().absolute()
    output_files: list[str] = []
    names = [Path(item["path"]).name for item in snapshots]
    if len(set(names)) != len(names) or "manifest.json" in names:
        raise RuntimeError("duplicate or reserved native Wan output filename")
    with _custody_directory(destination_root, create=True) as directory:
        # Refuse a reused/shared output directory. Do not overwrite any bytes.
        if os.listdir(directory):
            raise RuntimeError("Wan output custody must be an empty attempt directory")
        for item, name in zip(snapshots, names):
            with _native_output(item["path"], source_root) as (_, stream):
                if _file_observation(os.fstat(stream.fileno())) != item["stat"]:
                    raise RuntimeError("native Wan output changed before copy")
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                digest = hashlib.sha256()
                with os.fdopen(fd, "wb") as target:
                    while chunk := stream.read(1024 * 1024):
                        target.write(chunk)
                        digest.update(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if digest.hexdigest() != item["sha256"] or _file_observation(os.fstat(stream.fileno())) != item["stat"]:
                    raise RuntimeError("native Wan output changed during copy")
                output_files.append(name)
        if snapshot_native_outputs(result.get("generated_files"), source_root) != snapshots:
            raise RuntimeError("native Wan output changed after copy")
    manifest = build_manifest(
        kind="video",
        inputs={key: inputs[key] for key in ("prompt", "model") if key in inputs},
        outputs=[
            {
                "path": name,
                "name": "generated_videos",
                "ordinal": ordinal,
                "role": "result",
                "is_primary": ordinal == 0,
                "content_hash": "sha256:" + snapshots[ordinal]["sha256"],
                "bytes": snapshots[ordinal]["stat"][2],
            }
            for ordinal, name in enumerate(output_files)
        ],
        created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        schema_version=2,
        warnings=[],
        model=str(inputs.get("model", "wan-2.2")),
        portable_digest=portable_digest(compiled_settings),
        disclosed_engine={
            "engine": "wan2gp",
            "pin_sha": WAN2GP_PIN_SHA,
            "pin_ref": WAN2GP_PIN_REF,
            "seam": "host-owned shared.api.init / WanGPSession.submit_task",
            "native_job_id": evidence.get("native_job_id"),
        },
        spool=str(destination_root),
    )
    manifest_path = destination_root / "manifest.json"
        # Open exclusively through the held directory descriptor, like media.
    with _custody_directory(destination_root) as directory:
        fd = os.open(manifest_path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, sort_keys=True)
    staged = snapshot_native_outputs([str(destination_root / name) for name in output_files], destination_root)
    if [item["sha256"] for item in staged] != [item["sha256"] for item in snapshots]:
        raise RuntimeError("Wan staged bytes differ from terminal native outputs")
    return {
        "output_root": str(destination_root),
        "generated_files": output_files,
        "manifest": manifest,
        "native_job_id": evidence.get("native_job_id"),
        "events": list(evidence.get("events", [])),
        "disclosed_engine": manifest["disclosed_engine"],
        "staged_outputs": staged,
    }


def _disclosed_engine(wan2gp_root: Path | None) -> dict[str, Any]:
    return {
        "engine": "wan2gp",
        "pin_sha": WAN2GP_PIN_SHA,
        "pin_ref": WAN2GP_PIN_REF,
        "wan2gp_root": str(wan2gp_root) if wan2gp_root is not None else None,
        "seam": "shared.api.init / WanGPSession.submit_task",
    }


def _verify_within_spool(path: Path, spool: Path) -> bool:
    try:
        resolved = path.resolve()
        spool_resolved = spool.resolve()
        return resolved == spool_resolved or resolved.is_relative_to(spool_resolved)
    except Exception:
        return False


def _verify_outputs_in_spool(files: list[str], spool: Path) -> list[str]:
    violations: list[str] = []
    for raw in files:
        candidate = Path(raw)
        if not _verify_within_spool(candidate, spool):
            violations.append(raw)
    return violations


_WALL_CLOCK_METADATA_KEYS = ("generation_time", "creation_date", "creation_timestamp")


def _canonicalize_generated_files(files: list[str]) -> None:
    """Drop wall-clock Wan2GP comment metadata so CAS is input-determined.

    Native Wan2GP embeds ``creation_date`` / ``generation_time`` in the MP4
    ``©cmt`` tag. Cold vs warm then differ by a handful of timestamp bytes
    even when the video payload is identical.
    """
    try:
        from mutagen.mp4 import MP4
    except ImportError:
        return
    for raw in files:
        path = Path(raw)
        if not path.is_file() or path.suffix.lower() not in {".mp4", ".m4v", ".mov"}:
            continue
        media = MP4(str(path))
        tags = media.tags
        if tags is None:
            continue
        comments = list(tags.get("\xa9cmt", []) or tags.get("©cmt", []) or [])
        changed = False
        rewritten: list[str] = []
        for comment in comments:
            text = comment.decode("utf-8") if isinstance(comment, (bytes, bytearray)) else str(comment)
            try:
                payload = json.loads(text)
            except ValueError:
                rewritten.append(text)
                continue
            if not isinstance(payload, dict):
                rewritten.append(text)
                continue
            for key in _WALL_CLOCK_METADATA_KEYS:
                if key in payload:
                    payload.pop(key, None)
                    changed = True
            rewritten.append(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        if changed:
            tags["\xa9cmt"] = rewritten
            media.save()


def resolve_wan2gp_root(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Resolve only a caller-supplied, independently owned source root."""
    if explicit is not None:
        candidate = Path(explicit).expanduser().resolve()
        if candidate.is_dir() and (candidate / "shared" / "api.py").is_file():
            return candidate
    return None

def validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Validate (but do not execute) a compiled Wan2GP settings dict.

    Returns a sanitized copy with known portable keys preserved.  Raises
    ValueError with a disclosed message on missing/invalid inputs.
    """
    if not isinstance(settings, dict):
        raise ValueError("settings must be a dict")
    prompt = settings.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required and must be a non-empty string")
    model = settings.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("model must be a non-empty string when provided")
    # Resolution sanity (if provided)
    res = settings.get("resolution")
    if res is not None:
        text = str(res).strip()
        if "x" not in text.lower():
            raise ValueError("resolution must be WxH, e.g. 1280x720")
        parts = text.lower().split("x")
        if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
            raise ValueError("resolution must be WxH with integer dimensions")
    # video_length sanity
    vl = settings.get("video_length")
    if vl is not None and int(vl) <= 0:
        raise ValueError("video_length must be a positive integer")
    return dict(settings)


def wan_session_main() -> int:
    """Serve the host's JSONL protocol in the target Wan2GP interpreter.

    The protocol descriptor is isolated before loading native code, which may
    write to stdout. Only the worker imports the upstream runtime.
    """
    import os as system_os
    import sys
    import tempfile
    import uuid

    protocol = system_os.fdopen(system_os.dup(1), "w", encoding="utf-8", buffering=1)
    system_os.dup2(2, 1)
    write_lock = threading.Lock()
    active_lock = threading.Lock()
    active: dict[str, Any] = {}
    birth: str | None = None
    session = None
    scratch: str | None = None
    exit_code = 0

    def emit(kind: str, **fields: Any) -> None:
        frame = {"birth": birth, "kind": kind, **fields}
        encoded = json.dumps(frame, ensure_ascii=True, allow_nan=False)
        with write_lock:
            protocol.write(encoded + "\n")
            protocol.flush()

    def parse(line: str) -> dict[str, Any]:
        def invalid(value: str) -> None:
            raise ValueError(f"invalid JSON constant {value}")
        frame = json.loads(line, parse_constant=invalid)
        if not isinstance(frame, dict):
            raise ValueError("control frame must be an object")
        return frame

    def finish(job: Any, identity: dict[str, Any], native_id: str, spool: Path) -> None:
        context = {"identity": identity, "native_job_id": native_id}
        try:
            for event in job.events.iter():
                # Native previews and completed results can contain tensors or
                # media bytes. Forward only the progress fields the host uses.
                if event.kind == "progress":
                    update = event.data
                    data = {key: getattr(update, key, None) for key in
                            ("phase", "status", "progress", "current_step", "total_steps")}
                    emit("event", **context, event={"kind": "progress", "data": data})
            result = job.result()
            files = result.generated_files
            try:
                snapshots = snapshot_native_outputs(files, spool)
            except Exception as exc:
                raise RuntimeError(f"output paths {files!r} in spool {spool}: {exc}") from exc
            mapped = {"success": result.success, "generated_files": files,
                      "errors": [str(error) for error in result.errors],
                      "total_tasks": result.total_tasks,
                      "successful_tasks": result.successful_tasks,
                      "failed_tasks": result.failed_tasks}
            with active_lock:
                active.clear()
                emit("result", **context, result=mapped, output_snapshots=snapshots)
        except Exception as exc:
            with active_lock:
                active.clear()
                emit("error", **context, error=f"Wan job result or output observation failed: {exc}")

    try:
        line = sys.stdin.readline()
        if not line:
            raise ValueError("missing initialization frame")
        initial = parse(line)
        birth = initial.get("birth")
        if not isinstance(birth, str) or not birth:
            raise ValueError("initialization requires a birth identity")
        raw_root, options = initial.get("root"), initial.get("init")
        if not isinstance(raw_root, str) or not raw_root or not isinstance(options, dict):
            raise ValueError("initialization requires root and init object")
        if set(options) - {"root", "config_path", "output_dir", "cli_args", "console_output", "console_isatty"}:
            raise ValueError("unsupported Wan initialization option")
        root = resolve_wan2gp_root(raw_root)
        if root is None:
            raise ValueError(f"invalid Wan root: {raw_root}")
        if options.get("root", str(root)) != str(root):
            raise ValueError("initialization root does not match admitted root")
        raw_spool = options.get("output_dir")
        if not isinstance(raw_spool, str) or not raw_spool or not Path(raw_spool).is_absolute():
            raise ValueError("initialization requires an absolute output_dir")
        spool = Path(raw_spool).absolute()
        with _custody_directory(spool, create=True):
            pass
        if spool == root or spool.is_relative_to(root):
            raise ValueError(f"output_dir must be outside Wan root: {spool}")
        config = Path(options.get("config_path") or root / "wgp_config.json").expanduser().resolve(strict=True)
        if not config.is_file():
            raise ValueError(f"Wan config is not a file: {config}")
        scratch = tempfile.mkdtemp(prefix="wan-config-", dir=spool.parent)
        scratch_config = Path(scratch) / "wgp_config.json"
        shutil.copyfile(config, scratch_config)
        system_os.chdir(root)
        sys.path.insert(0, str(root))
        from shared import api
        init_options = {key: options[key] for key in ("cli_args", "console_isatty") if key in options}
        session = api.init(root=str(root), config_path=str(scratch_config), output_dir=str(spool),
                           console_output=False, **init_options)
        emit("ready", pid=system_os.getpid())
        for line in sys.stdin:
            identity = None
            try:
                request = parse(line)
                if request.get("birth") != birth:
                    raise ValueError("control frame birth does not match session")
                op = request.get("op")
                identity = request.get("identity")
                if op == "close":
                    break
                if op == "cancel":
                    with active_lock:
                        if not active or identity != active["identity"]:
                            raise ValueError("cancel identity does not match active job")
                        active["job"].cancel()
                        emit("cancel_requested", identity=identity, native_job_id=active["id"])
                    continue
                if op != "run":
                    raise ValueError("operation must be run, cancel, or close")
                if not isinstance(identity, dict) or not identity:
                    raise ValueError("run requires an identity object")
                settings = request.get("settings")
                if not isinstance(settings, dict):
                    raise ValueError("run requires a settings object")
                with active_lock:
                    if active:
                        raise ValueError("a Wan job is already active")
                    try:
                        job = session.submit_task(settings)
                    except Exception as exc:
                        raise RuntimeError(f"Wan submit_task failed: {exc}") from exc
                    native_id = uuid.uuid4().hex
                    active.update(job=job, identity=identity, id=native_id)
                    emit("job_started", identity=identity, native_job_id=native_id)
                    thread = threading.Thread(target=finish, args=(job, identity, native_id, spool))
                    active["thread"] = thread
                    thread.start()
            except Exception as exc:
                emit("error", identity=identity, error=f"Wan control request failed: {exc}")
    except Exception as exc:
        emit("error", error=f"Wan session initialization failed: {exc}")
        exit_code = 1
    finally:
        with active_lock:
            job = active.get("job")
            thread = active.get("thread")
            if job is not None:
                try:
                    job.cancel()
                except Exception as exc:
                    print(f"Wan job cancellation failed: {exc}", file=sys.stderr)
                    exit_code = 1
        if thread is not None:
            thread.join()
        if session is not None:
            try:
                session.close()
            except Exception as exc:
                print(f"Wan session close failed: {exc}", file=sys.stderr)
                exit_code = 1
        if scratch is not None:
            shutil.rmtree(scratch)
        protocol.close()
    return exit_code


if __name__ == "__main__":
    import sys

    if sys.argv[1:] != ["--wan-session"]:
        raise SystemExit("usage: python -m astrid.packs.wan2gp.src.driver --wan-session")
    raise SystemExit(wan_session_main())
