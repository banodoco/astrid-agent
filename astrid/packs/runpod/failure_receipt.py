"""The local failure receipt for a generation that failed on a rented machine.

``failure-receipt.json`` (``astrid.generation.failure-receipt.v1``) is written under
``<runtime data root>/generation/failures/<task_id>/`` from what the controller holds:
the validated cause facts, the stage, identities and the phase-timing block. When the
machine collected a failure bundle before its reset (``RunPodMachine._collect_failure``),
its files, already redacted on the pod and again on the controller and size-capped,
land in ``files/`` beside the receipt. Receipts and bundles never leave this machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "astrid.generation.failure-receipt.v1"
STAGES = ("no_machine", "boot", "install", "model_download", "workflow_validation",
          "execution", "output_collection", "release")
KEEP_RECEIPTS = 20
CAP_BYTES = 262144
RECEIPT_NAME = "failure-receipt.json"
#: Pod exit codes the job command itself uses (``machine.generation_command``).
EXIT_NOT_READY, EXIT_VALIDATION = 75, 3
_CATEGORY_STAGE = {"input": "workflow_validation", "model": "model_download",
                   "installation": "install", "health": "install", "dependency": "install"}
_PROGRESS_STAGE = {"align": "model_download", "start_engine": "install"}

_MESSAGES = {
    "no_machine": "No machine was available for this generation.",
    "boot": "The machine did not become ready before the generation could start.",
    "install": "The machine's engine could not be installed or started.",
    "model_download": "The machine could not fetch the models this generation needs.",
    "workflow_validation": "The machine rejected the workflow before it ran.",
    "execution": "The generation failed while running on the machine.",
    "output_collection": "The generation finished but its outputs could not be verified and collected.",
    "release": "The generation finished but the machine could not be settled or released.",
}
_FACT_SENTENCES = (("node_id", "node {}"), ("node_class", "class {}"), ("field", "field '{}'"),
                   ("constraint", "({})"))


def decide_stage(*, phase: str, claim_context: Mapping[str, Any] | None = None, exit_code: int | None = None,
                 category: str | None = None, progress_phase: str | None = None,
                 digest_mismatch: bool = False, release_failed: bool = False) -> tuple[str, str]:
    """The stage and what decided it (``stage_basis``), in the designed order.

    1. The controller step that raised (claim, preparation, fetch without a pod exit, release).
    2. The pod exit code: 75 is ``boot``, 3 is ``workflow_validation``.
    3. The diagnostic category.
    4. The last progress phase copied from the pod.
    5. Otherwise ``execution``.
    """
    if phase == "release":
        return "release", "controller_step"
    if phase == "claim":
        context = claim_context or {}
        if not context.get("acquired"):
            return "no_machine", "controller_step"
        return ("install" if context.get("preparation_job") else "boot"), "controller_step"
    if digest_mismatch or (phase == "fetch" and exit_code is None and category is None):
        return "output_collection", "controller_step"
    if exit_code == EXIT_NOT_READY:
        return "boot", "pod_exit"
    if exit_code == EXIT_VALIDATION:
        return "workflow_validation", "pod_exit"
    if category in _CATEGORY_STAGE:
        return _CATEGORY_STAGE[category], "diagnostic"
    if progress_phase in _PROGRESS_STAGE:
        return _PROGRESS_STAGE[progress_phase], "progress"
    return "execution", "controller_step"


def message_for(stage: str, cause: Mapping[str, Any]) -> str:
    """A fixed sentence per stage with only the validated cause facts filled in."""
    text = _MESSAGES[stage]
    facts = [template.format(cause[name]) for name, template in _FACT_SENTENCES if name in cause]
    if facts:
        text = text.rstrip(".") + ": " + ", ".join(facts) + "."
    return text


def failures_root() -> Path:
    from astrid.sdk.storage_root import resolve_runtime_data_root
    return resolve_runtime_data_root() / "generation" / "failures"


def _clean(value: Any) -> Any:
    return value if isinstance(value, (str, int, float, bool)) or value is None else None


def build_receipt(facts: Mapping[str, Any], *, job: Any, machine: Mapping[str, Any] | None,
                  timing: Any) -> dict[str, Any] | None:
    """The receipt body for one failed generation, or ``None`` when none is written (cancelled)."""
    from astrid.core.execution.machine_run import MachineRunCancelled

    from .failure_diagnostics import host_summary
    failure = facts["failure"]
    from .target_selection import CapacityWaitCancelled
    if isinstance(failure, (MachineRunCancelled, CapacityWaitCancelled)):
        return None  # Cancelled runs write no receipt.
    lease, run = facts.get("lease"), facts.get("run")
    diagnostic = getattr(lease, "failure_diagnostic", None)
    node_ids = getattr(lease, "node_ids", frozenset()) or frozenset()
    phase = facts["stage"]
    summary = host_summary(failure, diagnostic, stage=phase, node_ids=node_ids)
    exit_code = getattr(lease, "failure_exit_code", None)
    progress = list(getattr(run, "progress", None) or [])
    progress_phase = next((row.get("phase") for row in reversed(progress) if isinstance(row, Mapping)
                           and row.get("phase")), None)
    from .remote_placement import DigestMismatch
    context = getattr(failure, "claim_context", None)
    stage, basis = decide_stage(
        phase=phase, claim_context=context if isinstance(context, Mapping) else None, exit_code=exit_code,
        category=summary.get("category") if diagnostic is not None else None, progress_phase=progress_phase,
        digest_mismatch=isinstance(failure, DigestMismatch), release_failed=bool(facts.get("release_failed")))
    cause: dict[str, Any] = {key: summary[key] for key in
                             ("code", "category", "exception", "node_id", "node_class", "field", "constraint")
                             if key in summary}
    if exit_code is not None:
        cause["exit_code"] = exit_code
    reason = getattr(failure, "reason", None)
    gate = getattr(failure, "gate", None)
    if stage == "no_machine":
        # Only fixed names (offer reason, gate) are copied; exception text never is.
        from .target_selection import _OFFER_FAILURES
        if isinstance(reason, str) and reason in _OFFER_FAILURES:
            cause["reason"] = reason
        if gate in {"volume", "GPU", "datacenter", "rate", "lifetime", "machine", "offer"}:
            cause["gate"] = gate
    machine = dict(machine or {})
    remote_root = getattr(run, "remote_root", None)
    kept = getattr(lease, "kept", None)
    identity = {
        "task_id": getattr(job, "task_id", None), "attempt_id": getattr(job, "attempt_id", None),
        "invocation_id": getattr(job, "invocation_id", None), "placement": machine.get("placement", "runpod"),
        "machine_id": machine.get("id"), "lease": machine.get("lease"), "generation": machine.get("generation"),
        "gpu": machine.get("device"), "warm": machine.get("warm"),
        "run_id": remote_root.rsplit("/", 1)[-1] if isinstance(remote_root, str) else None,
        "remote_job_id": getattr(run, "job_id", None), "pod_source_digest": None,
    }
    block = timing.block() if timing is not None else None
    stamps = (block or {}).get("stamps", {})
    after = ("none" if lease is None else "unknown" if kept is None or facts.get("release_failed")
             else "kept" if kept else "released")
    cost = None
    bundle = _bundle_of(facts)
    collection, files, redaction = _collection(stage, lease, bundle)
    return {
        "schema": SCHEMA, "receipt_id": uuid.uuid4().hex, "outcome": "failed",
        "stage": stage, "stage_basis": basis, "phase": phase,
        "cause": cause, "message": message_for(stage, cause),
        "identity": {key: _clean(value) for key, value in identity.items()},
        "timing": block,
        "machine": {"after": after, "release_reason": getattr(lease, "release_reason", None),
                    "release_at": stamps.get("released_at"), "cost_usd": cost},
        "collection": collection,
        "files": files,
        "redaction": redaction,
    }


def _bundle_of(facts: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The bundle the machine collected: on the lease (job path) or the error (claim path)."""
    for holder in (facts.get("lease"), facts.get("failure")):
        bundle = getattr(holder, "failure_bundle", None)
        if isinstance(bundle, Mapping):
            return bundle
    return None


def _collection(stage: str, lease: Any, bundle: Mapping[str, Any] | None) -> tuple[dict, list, dict]:
    from .failure_bundle import FILE_CAPS, LINE_DROP, POLICY, PROMPT_LINE_MIN, PROMPT_MIN, SECRET_MIN, TOTAL_CAP

    not_applied = {"policy": POLICY, "applied": False, "reason": "no bundle collected"}
    if stage == "no_machine":
        return ({"state": "local_only", "reason": "local_only", "bundle_manifest_sha256": None, "bytes": 0,
                 "cap_bytes": CAP_BYTES}, [], not_applied)
    if bundle is None:
        reason = "no_job_started" if lease is not None else "no_machine_reached"
        return ({"state": "not_collected", "reason": reason, "bundle_manifest_sha256": None, "bytes": 0,
                 "cap_bytes": CAP_BYTES}, [], not_applied)
    state = bundle.get("state")
    collection = {"state": _clean(state), "reason": _clean(bundle.get("reason")),
                  "bundle_manifest_sha256": _clean(bundle.get("manifest_sha256")),
                  "bytes": bundle.get("bytes", 0) if type(bundle.get("bytes", 0)) is int else 0,
                  "cap_bytes": CAP_BYTES, "pod_folder": _clean(bundle.get("pod_folder")),
                  "seconds": _clean(bundle.get("seconds")), "local_only": True}
    if state != "collected":
        return collection, [], not_applied
    files = [{"name": row["name"], "path": f"files/{row['name']}", "bytes": row["bytes"], "sha256": row["sha256"],
              "pod_bytes": row.get("pod_bytes"), "pod_sha256": row.get("pod_sha256"),
              "truncated": bool(row.get("truncated")), "source_bytes": _clean(row.get("source_bytes"))}
             for row in bundle.get("files") or [] if isinstance(row, Mapping) and row.get("name") in FILE_CAPS]
    workflow = bundle.get("workflow") or {}
    redaction = {
        "policy": POLICY, "applied": True,
        "pod": dict(bundle.get("pod_counts") or {}), "local": dict(bundle.get("local_counts") or {}),
        "workflow_strings_replaced": workflow.get("strings_replaced", 0),
        "workflow_values_replaced": workflow.get("values_replaced", 0),
        "rule": {"order": "redact, then truncate", "prompt_min_chars": PROMPT_MIN,
                 "prompt_line_min_chars": PROMPT_LINE_MIN, "line_drop_chars": LINE_DROP,
                 "secret_min_chars": SECRET_MIN, "always_removed": ["keys and tokens", "URLs", "IP addresses"],
                 "workflow": "structure only", "total_cap_bytes": TOTAL_CAP, "file_caps": dict(FILE_CAPS)},
    }
    return collection, files, redaction


def write_receipt(receipt: Mapping[str, Any], *, root: Path | None = None, bundle_dir: Path | None = None) -> Path:
    """Write atomically under ``<root>/<task_id>/`` and prune to the newest ``KEEP_RECEIPTS``.

    The collected bundle's redacted files are copied into ``files/`` first, each
    rechecked against the digest the receipt records; a file that differs is left out.
    """
    from .failure_bundle import BUNDLE_FILES, _open_directory, _open_regular, _write

    root = Path(root) if root is not None else failures_root()
    task = receipt["identity"].get("task_id")
    name = "".join(ch for ch in str(task or "") if ch.isalnum() or ch in "-_")[:128] or f"local-{uuid.uuid4().hex}"
    directory = _open_directory(str(root), create=True)
    try:
        try:
            os.mkdir(name, mode=0o700, dir_fd=directory)
        except FileExistsError:
            pass
        folder_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            # rmtree uses its symlink-resistant descriptor traversal; a symlink is refused.
            try:
                info = os.stat("files", dir_fd=folder_fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise OSError("symlink in receipt path")
                if stat.S_ISDIR(info.st_mode):
                    shutil.rmtree("files", dir_fd=folder_fd)
                else:
                    os.unlink("files", dir_fd=folder_fd)
            except FileNotFoundError:
                pass
            if receipt.get("files"):
                receipt = dict(receipt)
                os.mkdir("files", mode=0o700, dir_fd=folder_fd)
                files_fd = os.open("files", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder_fd)
                try:
                    kept = []
                    for row in receipt["files"]:
                        if row.get("name") not in BUNDLE_FILES:
                            continue
                        try:
                            if bundle_dir is None:
                                data = None
                            else:
                                source, _size = _open_regular(str(Path(bundle_dir) / row["name"]))
                                with source:
                                    data = source.read(CAP_BYTES + 1)
                        except OSError:
                            data = None
                        if (data is not None and len(data) <= CAP_BYTES
                                and hashlib.sha256(data).hexdigest() == row["sha256"]):
                            _write(files_fd, row["name"], data)
                            kept.append(row)
                    receipt["files"] = kept
                finally:
                    os.close(files_fd)
            partial = RECEIPT_NAME + f".{os.getpid()}.partial"
            _write(folder_fd, partial, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
            os.replace(partial, RECEIPT_NAME, src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
        finally:
            os.close(folder_fd)
        _prune(directory, keep=name)
    finally:
        os.close(directory)
    return root / name / RECEIPT_NAME


def _prune(root: int, *, keep: str) -> None:
    rows = []
    try:
        for name in os.listdir(root):
            info = os.stat(name, dir_fd=root, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                continue
            folder = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                receipt = os.stat(RECEIPT_NAME, dir_fd=folder, follow_symlinks=False)
                if stat.S_ISREG(receipt.st_mode):
                    rows.append((receipt.st_mtime, name))
            except FileNotFoundError:
                pass
            finally:
                os.close(folder)
        rows.sort(reverse=True)
        for _mtime, name in rows[KEEP_RECEIPTS:]:
            if name != keep:
                shutil.rmtree(name, dir_fd=root, ignore_errors=True)
    except OSError:
        pass


def record_failure(facts: Mapping[str, Any], *, job: Any, machine: Mapping[str, Any] | None, timing: Any,
                   root: Path | None = None) -> Path | None:
    """Build and write one receipt; a problem here is logged by class and never raised."""
    bundle = _bundle_of(facts)
    bundle_dir = Path(bundle["dir"]) if bundle is not None and isinstance(bundle.get("dir"), str) else None
    try:
        receipt = build_receipt(facts, job=job, machine=machine, timing=timing)
        return write_receipt(receipt, root=root, bundle_dir=bundle_dir) if receipt is not None else None
    except Exception as exc:  # noqa: BLE001 (never masks the original error)
        import sys
        print(f"failure receipt not written ({type(exc).__name__})", file=sys.stderr)
        return None
    finally:
        if bundle_dir is not None:
            # The attempt's staging copy is removed once the receipt holds the bundle.
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)


def recorder(job: Any, machine_of: Any, timing: Any, *, root: Path | None = None) -> Any:
    """The ``on_failure`` callback for ``run_on_machine``: stamps the final timing, then writes the receipt.

    ``machine_of()`` returns the attempt's mutable ``terminal["machine"]`` record (or None before a claim).
    """
    from astrid.core.execution import phase_timing

    def failed(facts: Mapping[str, Any]) -> None:
        machine = machine_of()
        phase_timing.attach(machine, timing)
        record_failure(facts, job=job, machine=machine, timing=timing, root=root)

    return failed


def find_receipt(task_id: str | None = None, *, root: Path | None = None) -> Path | None:
    """The receipt for ``task_id``, or the newest one."""
    root = Path(root) if root is not None else failures_root()
    if task_id:
        safe = "".join(ch for ch in task_id if ch.isalnum() or ch in "-_")
        path = root / safe / RECEIPT_NAME
        return path if safe == task_id and path.is_file() else None
    try:
        rows = [(p.stat().st_mtime, p) for p in root.glob(f"*/{RECEIPT_NAME}")]
    except OSError:
        return None
    return max(rows, key=lambda row: row[0])[1] if rows else None


def _printable(text: str) -> str:
    """Pod-written text with terminal control characters made visible."""
    return "".join(ch if ch == "\t" or (" " <= ch and ch != "\x7f" and not "\x80" <= ch <= "\x9f") else "?"
                   for ch in text)


def _last_lines(path: Path, count: int = 6) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [_printable(line)[:200] for line in lines if line.strip()][-count:]


def render(receipt: Mapping[str, Any], folder: Path | None = None) -> str:
    """The human form: a few fixed lines from the receipt, and the bundle's last error lines."""
    cause, ident = receipt.get("cause") or {}, receipt.get("identity") or {}
    machine, collection = receipt.get("machine") or {}, receipt.get("collection") or {}
    timing = ((receipt.get("timing") or {}).get("phases") or {})
    lines = [f"Failure: {receipt.get('stage')} ({receipt.get('stage_basis')})",
             str(receipt.get("message")),
             "Cause: " + (", ".join(f"{k}={cause[k]}" for k in cause) or "unknown"),
             f"Task: {ident.get('task_id')}  attempt: {ident.get('attempt_id')}",
             f"Machine: {ident.get('machine_id') or 'none'}  after: {machine.get('after')}"
             + (f"  release: {machine['release_reason']}" if machine.get("release_reason") else ""),
             "Bundle: " + (f"{str(collection.get('state')).replace('_', ' ')} "
                           f"({str(collection.get('reason') or 'n/a').replace('_', ' ')})")]
    files = [row for row in receipt.get("files") or [] if isinstance(row, Mapping)]
    if collection.get("state") == "collected":
        lines[-1] = (f"Bundle: collected, {len(files)} files, {collection.get('bytes')} bytes "
                     f"(cap {collection.get('cap_bytes')}), redacted on the pod and on this machine; local only")
        lines += [f"  {row.get('path')}  {row.get('bytes')} bytes" + ("  (tail)" if row.get("truncated") else "")
                  for row in files]
        if folder is not None:
            lines.append(f"Folder: {folder}")
            for name in ("error-tail.txt", "engine-log-tail.txt"):
                if any(row.get("name") == name for row in files):
                    tail = _last_lines(Path(folder) / "files" / name)
                    if tail:
                        lines.append(f"Last lines of {name}:")
                        lines += ["  " + line for line in tail]
                        break
    seconds = [f"{name}={row['seconds']}s" for name, row in timing.items()
               if isinstance(row, Mapping) and row.get("seconds") is not None]
    if seconds:
        lines.append("Timing: " + ", ".join(seconds))
    return "\n".join(lines)


__all__ = ["SCHEMA", "STAGES", "build_receipt", "decide_stage", "find_receipt", "record_failure", "recorder", "render",
           "write_receipt"]
