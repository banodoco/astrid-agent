"""Internal serial worker for the admitted pack-session protocol.

The parent owns Runtime admission, custody, deadlines and settlement. This
worker receives no Runtime credential. It loads one captured pack identity and
keeps only that interpreter's pack state between explicit, bounded job frames.
"""
from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from .resident_sessions import (
    _JOB,
    MAX_FRAME_BYTES,
    PROTOCOL,
    ResidentJobContext,
    ResidentProtocolError,
    decode_frame,
    encode_frame,
)


def _read(stream):
    raw = stream.readline(MAX_FRAME_BYTES + 1)
    return None if not raw else decode_frame(raw)


@contextmanager
def _capture_output(attempt, secrets):
    from .generic_host import GenericPackHost, _TaskOutputCapture

    streams, saved = {}, {}
    capture = _TaskOutputCapture(attempt, secrets, GenericPackHost._scrub_secret_text)
    stdout, stderr = sys.stdout, sys.stderr
    try:
        stdout.flush()
        stderr.flush()
        for name, fd in (("stdout", 1), ("stderr", 2)):
            reader, writer = os.pipe()
            streams[name] = os.fdopen(reader, "rb", buffering=0)
            saved[fd] = os.dup(fd)
            os.dup2(writer, fd)
            os.close(writer)
        capture.start(SimpleNamespace(**streams))
        yield capture
    finally:
        try:
            stdout.flush()
            stderr.flush()
        finally:
            sys.stdout, sys.stderr = stdout, stderr
            for fd, original in saved.items():
                os.dup2(original, fd)
                os.close(original)
            capture.finish()
        if capture.error:
            # A descendant retaining a job pipe is not a reusable session.
            raise ResidentProtocolError("resident job output capture did not close")


def _job_frame(frame, binding, generation):
    if set(frame) != {"protocol", "kind", "identity", "job", "request", "attempt"} or frame["kind"] != "job":
        raise ResidentProtocolError("invalid resident job frame")
    identity, job, request = frame["identity"], frame["job"], frame["request"]
    if (not isinstance(identity, dict)
            or set(identity) != {"token_id", "invocation_id", "session_id", "generation", "binding_identity"}
            or identity["binding_identity"] != binding or identity["session_id"] != binding[0]
            or type(identity["generation"]) is not int or identity["generation"] != generation
            or not isinstance(identity["token_id"], str) or not identity["token_id"]):
        raise ResidentProtocolError("resident admission is stale")
    if (not isinstance(job, dict) or set(job) != {"task_id", "attempt_id", "fence", "runtime_epoch"}
            or any(not isinstance(job[key], str) or not job[key] for key in ("task_id", "attempt_id"))
            or type(job["fence"]) is not int or job["fence"] < 0
            or job["runtime_epoch"] != binding[7]
            or (job["runtime_epoch"] is not None and (type(job["runtime_epoch"]) is not int or job["runtime_epoch"] < 1))
            or identity["invocation_id"] != f"{job['task_id']}:{job['attempt_id']}:{job['fence']}"):
        raise ResidentProtocolError("resident Runtime attempt differs from its admission")
    if not isinstance(request, dict) or not isinstance(frame["attempt"], str):
        raise ResidentProtocolError("resident request is not an object")
    attempt = Path(frame["attempt"])
    output = Path(request.get("out", ""))
    if (not attempt.is_absolute() or attempt.resolve(strict=True) != attempt or not attempt.is_dir()
            or request.get("run_root") != str(attempt) or not output.is_absolute()
            or output.resolve(strict=True) != output or not output.is_relative_to(attempt)
            or not output.is_dir()):
        raise ResidentProtocolError("resident job paths differ from the private attempt")
    return identity, job, request, attempt, output


def serve(*, captured=None) -> int:
    from .generic_host import GenericPackHost, _verify_action_admission, _verify_admitted_source
    from .generic_host_worker import run_payload
    from .network_policy import resident_job_policy
    from .process_group import process_birth_identity

    # Protocol output has a private duplicate, so print/os.write(1) by a pack
    # cannot accidentally become a completion frame. Parent pipes drain logs.
    wire = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    write_lock = threading.Lock()
    def send(frame):
        data = encode_frame(frame)
        with write_lock:
            wire.write(data)
            wire.flush()
    init = _read(sys.stdin.buffer)
    if (init is None or set(init) != {"protocol", "kind", "binding", "generation", "definition", "admission", "policy"}
            or init["kind"] != "init" or not isinstance(init["binding"], list)
            or len(init["binding"]) != 10 or type(init["generation"]) is not int or init["generation"] < 1):
        raise ResidentProtocolError("invalid resident startup")
    binding, generation = init["binding"], init["generation"]
    definition, admission, policy = init["definition"], init["admission"], init["policy"]
    _verify_action_admission(definition, admission)
    _verify_admitted_source(admission)
    if (definition.get("metadata", {}).get("action_declaration", {}).get("session")
            != {"kind": "resident", "protocol": PROTOCOL}
            or definition.get("metadata", {}).get("action_invocation", {}).get("kind") != "python"
            or any(key in admission for key in ("task_id", "attempt_id", "fence", "network_nonce"))):
        raise ResidentProtocolError("resident startup does not name a retained Python contract")
    send({"protocol": PROTOCOL, "kind": "ready", "binding": binding,
          "pid": os.getpid(), "birth": process_birth_identity()})
    baseline_environment = dict(os.environ)
    # Only manifest-scoped child secrets reach this process, and no secret is
    # written to a frame or an unsanitized persistent diagnostic.
    declared_secrets = frozenset(definition.get("metadata", {}).get("declared_secrets", ()))
    secret_values = {key: value for key, value in baseline_environment.items()
                     if key in declared_secrets
                     or key.upper().endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIAL"))}
    idle_cwd = Path.cwd()
    active = None
    previous = None
    cancel = threading.Event()
    finished = threading.Event()
    finished.set()

    def execute(parts, job_cancel):
        identity, job, request, attempt, output = parts
        context = ResidentJobContext(job["task_id"], job["attempt_id"], job["fence"],
                                     job["runtime_epoch"], attempt, output, job_cancel.is_set)
        context_token = _JOB.set(context)
        capture = None
        try:
            os.chdir(attempt)
            with _capture_output(attempt, secret_values) as capture:
                with resident_job_policy(policy, attempt / "network-evidence.json", admission=job):
                    result = run_payload({"capability_kind": "executor", "capability_id": definition["id"],
                                          "definition": definition, "admission": {**admission, **job},
                                          "request": request}, captured=captured)
            result["stdout"], result["stderr"] = capture.tails()
            if job_cancel.is_set():
                result = {"ok": False, "error": "resident job was cancelled"}
        except BaseException as exc:  # noqa: BLE001 - pack exits/errors are fenced terminal evidence
            result = {"ok": False, "error": GenericPackHost._scrub_secret_text(
                f"{type(exc).__name__}: {exc}", secret_values)[-3500:]}
        finally:
            job_cancel.set()  # A retained/copied context never becomes the next job's grant.
            _JOB.reset(context_token)
            os.chdir(idle_cwd)
            os.environ.clear()
            os.environ.update(baseline_environment)
        # Mark the job quiescent before publishing its terminal frame. The
        # next frame can arrive immediately; main joins this thread before
        # installing the next context, so cleanup cannot overlap jobs.
        finished.set()
        try:
            send({"protocol": PROTOCOL, "kind": "result", "identity": identity, "job": job, "result": result})
        except BaseException:  # noqa: BLE001 - no recoverable protocol state after a partial write
            # A partial/oversized frame is fatal, never a reason to re-execute.
            os._exit(70)

    try:
        while True:
            frame = _read(sys.stdin.buffer)
            if frame is None:
                return 0 if finished.is_set() else 70
            if frame.get("kind") == "cancel":
                if set(frame) != {"protocol", "kind", "identity"} or frame["identity"] != previous:
                    raise ResidentProtocolError("resident cancellation is stale")
                cancel.set()
                continue
            if not finished.is_set():
                raise ResidentProtocolError("resident serial worker already has a job")
            if active is not None:
                active.join()
            parts = _job_frame(frame, binding, generation)
            if parts[0] == previous:
                raise ResidentProtocolError("resident admission was already executed")
            previous = parts[0]
            cancel = threading.Event()
            finished.clear()
            active = threading.Thread(target=execute, args=(parts, cancel), name="astrid-resident-action", daemon=True)
            active.start()
    finally:
        cancel.set()
        wire.close()
