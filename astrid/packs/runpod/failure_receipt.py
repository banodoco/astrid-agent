"""The local failure receipt for a generation that failed on a rented machine.

``failure-receipt.json`` (``astrid.generation.failure-receipt.v1``) is written under
``<runtime data root>/generation/failures/<task_id>/`` using the validated cause facts,
stage, identities, phase timing and any declared logs already fetched by the controller.
The ``collection`` section records the log fetch outcome.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence, TypeVar

from .failure_reason import Reason, attach_reason, installation_reason

SCHEMA = "astrid.generation.failure-receipt.v1"
STAGES = ("no_machine", "boot", "install", "model_download", "workflow_validation",
          "execution", "output_collection", "release")
KEEP_RECEIPTS = 20
ISSUE_MESSAGE_LIMIT = 1024
RECEIPT_NAME = "failure-receipt.json"
#: Pod exit codes the job command itself uses (``machine.generation_command``).
EXIT_NOT_READY, EXIT_VALIDATION = 75, 3
_CATEGORY_STAGE = {"input": "workflow_validation", "model": "model_download",
                   "installation": "install", "health": "install", "dependency": "install"}
_PROGRESS_STAGE = {"align": "model_download", "start_engine": "install"}
CAUSE_MARKERS = frozenset({"credential", "volume", "machine"})

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

_CheckpointError = TypeVar("_CheckpointError", bound=BaseException)


def checkpoint_message(reason: str) -> str:
    """One refusal line shared by checkpoint errors and their receipts."""
    detail = {
        "checkpoint_attestation_missing": "Changed installation has no matching completed-job attestation",
        "checkpoint_attestation_mismatch": "Live installation differs from completed-job attestation",
        "installation_receipt_changed": "Live installation differs from its canonical receipt",
        "checkpoint_installation_changed": "Retained installation changed during verification",
        "checkpoint_job_changed": "Retained installation checkpoint job changed during generation",
        "checkpoint_epoch_changed": "Machine owner epoch changed during verification",
        "checkpoint_invocation_changed": "Machine invocation changed during verification",
        "checkpoint_paid_expired": "Machine paid lease expired during verification",
        "checkpoint_observation_interrupted": "Machine release interrupted installation verification",
        "status_no_receipt": "Engine status returned no installation receipt",
        "status_deadline_exhausted": "Installation verification deadline expired",
        "receipt_read_failed": "Canonical installation receipt could not be read",
    }.get(reason, "Installation checkpoint refused")
    if reason == "checkpoint_paid_expired":
        action = "run astrid machines claim before retrying the generation"
    elif reason.startswith("checkpoint_") and reason.endswith("changed"):
        action = "run astrid machines to inspect custody before retrying"
    else:
        action = "inspect the installation and retry the generation on a prepared machine"
    return f"{detail} (reason: {reason}); {action}."


def checkpoint_refusal(error: _CheckpointError, reason: str) -> _CheckpointError:
    """Refuse through the shared reason boundary, keeping the exact custody exception type.

    A reason the error already carries (an earlier transport refusal) is kept;
    otherwise the checkpoint label becomes its validated ``Reason``. The label
    itself stays as ``install_unverified``: the detail the message renders.
    """
    existing = getattr(error, "reason", None)
    attach_reason(error, existing if isinstance(existing, Reason) else installation_reason(reason))
    setattr(error, "install_unverified", reason)
    line = checkpoint_message(reason)
    error.args = (line,)
    from astrid.core.contracts.errors import AstridError
    if isinstance(error, AstridError):
        error.cause = error.message = line
    return error


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
    if "install_unverified" in cause:
        return checkpoint_message(cause["install_unverified"])
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


def _log_role(row: Mapping[str, Any]) -> str:
    role = row.get("role")
    if isinstance(role, str) and role:
        return role
    name = str(row.get("name", ""))
    if name.startswith("job."):
        name = name[4:]
    if name == "engine.log" or name.startswith("engine-"):
        return "engine"
    return name if name in {"engine", "run", "stderr", "stdout", "install"} else "run"


def log_order(row: Mapping[str, Any]) -> tuple[int, str]:
    """Stable display order for declared logs."""
    return ({"engine": 0, "run": 1, "stderr": 2, "stdout": 3, "install": 4}.get(_log_role(row), 5),
            str(row.get("name", "")))


def _issue_message(logs: list[Mapping[str, Any]]) -> str | None:
    """Read the first issue from the collected job output, if available."""
    try:
        row = next((row for row in logs if row.get("name") == "generation.stdout"), None)
        if not row or row.get("missing") or not row.get("path"):
            return None
        with Path(row["path"]).open(encoding="utf-8") as stream:
            payload = json.load(stream)
        issues = payload.get("issues") if isinstance(payload, dict) else None
        first = issues[0] if isinstance(issues, list) and issues else None
        message = first.get("message") if isinstance(first, dict) else None
        return message[:ISSUE_MESSAGE_LIMIT] if isinstance(message, str) and message else None
    except Exception:  # noqa: BLE001 - optional evidence must not mask the job failure
        return None


def build_receipt(facts: Mapping[str, Any], *, job: Any, machine: Mapping[str, Any] | None,
                  timing: Any) -> dict[str, Any] | None:
    """The receipt body for one failed generation, or ``None`` when none is written (cancelled)."""
    from astrid.core.execution.machine_run import MachineRunCancelled

    from .failure_diagnostics import CODES, host_summary
    failure = facts["failure"]
    from .target_selection import CapacityWaitCancelled
    if isinstance(failure, (MachineRunCancelled, CapacityWaitCancelled)):
        return None  # Cancelled runs write no receipt.
    lease, run = facts.get("lease"), facts.get("run")
    diagnostic = getattr(lease, "failure_diagnostic", None)
    node_ids: frozenset[str] = getattr(lease, "node_ids", frozenset()) or frozenset()
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
    marker = getattr(failure, "cause", None)
    if marker in CAUSE_MARKERS:
        cause["cause"] = marker
    if exit_code is not None:
        cause["exit_code"] = exit_code
    reason = getattr(failure, "reason", None)
    # Receipts keep the RunPod gate names users already see (GPU, volume,
    # datacenter, ...); PlacementRefused carries them as runpod_gate.
    gate = getattr(failure, "runpod_gate", None) or getattr(failure, "gate", None)
    if isinstance(reason, Reason):
        stage, basis = reason.stage, "reason"
        cause["reason"] = reason.as_dict()
        install_unverified = getattr(failure, "install_unverified", None)
        if isinstance(install_unverified, str) and re.fullmatch(r"[A-Za-z0-9_.:;=-]{1,256}", install_unverified):
            cause["install_unverified"] = install_unverified
        cause["code"] = reason.code
        cause["category"] = CODES.get(reason.code, "installation" if reason.stage == "install" else "execution")
    if stage == "no_machine":
        # Only fixed names (offer reason, gate) are copied; exception text never is.
        from .target_selection import _OFFER_FAILURES
        if isinstance(reason, str) and reason in _OFFER_FAILURES:
            cause["reason"] = reason
    if isinstance(gate, str) and gate in {"volume", "GPU", "datacenter", "rate", "lifetime", "machine", "offer",
                                         "identity", "status", "image", "inventory", "provider"}:
        cause["gate"] = gate
    machine = dict(machine or {})
    claim_failure = getattr(failure, "machine_failure", None) or {}
    if claim_failure:
        machine.update({key: claim_failure.get(key) for key in ("id", "lease", "generation", "device")})
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
    if claim_failure:
        after = claim_failure.get("after", "unknown")
    cost = claim_failure.get("cost_usd") if claim_failure else getattr(lease, "cost_usd", None)
    # The failed job's own logs (stdout, stderr, engine log), brought back unredacted
    # from the user's own machine before the reset; write_receipt keeps a copy.
    collected = getattr(lease, "failure_logs", None)
    logs = []
    for original in (collected or {}).get("files", []):
        if not isinstance(original, Mapping):
            continue
        row = dict(original)
        row["role"] = _log_role(row)
        row.setdefault("bytes", 0)
        row.setdefault("truncated", False)
        row.setdefault("missing", False)
        row.setdefault("path", None if row["missing"] else row.get("path"))
        logs.append(row)
    issue_message = _issue_message(logs)
    if issue_message:
        cause["issue_message"] = issue_message
    unavailable = (collected or {}).get("unavailable") if isinstance(collected, Mapping) else None
    return {
        "logs": logs, **({"logs_unavailable": unavailable} if unavailable else {}),
        "schema": SCHEMA, "receipt_id": uuid.uuid4().hex, "outcome": "failed",
        "stage": stage, "stage_basis": basis, "phase": phase,
        "cause": cause, "message": message_for(stage, cause),
        "identity": {key: _clean(value) for key, value in identity.items()},
        "timing": block,
        "machine": {"after": after, "release_reason": claim_failure.get("release_reason") if claim_failure else getattr(lease, "release_reason", None),
                    "release_at": stamps.get("released_at"), "cost_usd": cost,
                    **({"expires_at": claim_failure.get("expires_at") if claim_failure else getattr(lease, "expires_at", None),
                        "preparation_state": claim_failure.get("preparation_state") if claim_failure else getattr(lease, "preparation_state", None)} if after == "kept" else {})},
        "collection": _collection(stage, logs, unavailable),
        "files": [],
    }


def _collection(stage: str, logs: Sequence[Mapping[str, Any]], unavailable: Any) -> dict[str, Any]:
    from astrid.core._shared import run_logs

    if stage == "no_machine":
        state, reason = "local_only", unavailable
    elif unavailable:
        state, reason = "unavailable", unavailable
    elif logs:
        state, reason = "collected", None
    else:
        state, reason = "not_collected", None
    return {"state": state, "reason": reason, "bytes": sum(row.get("bytes", 0) for row in logs),
            "cap_bytes": run_logs.CAP_BYTES}


def write_receipt(receipt: Mapping[str, Any], *, root: Path | None = None) -> Path:
    """Write atomically under ``<root>/<task_id>/`` and prune to the newest ``KEEP_RECEIPTS``."""
    root = Path(root) if root is not None else failures_root()
    task = receipt["identity"].get("task_id")
    name = "".join(ch for ch in str(task or "") if ch.isalnum() or ch in "-_")[:128] or f"local-{uuid.uuid4().hex}"
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    if receipt.get("logs"):
        receipt = dict(receipt)
        kept = []
        for row in receipt["logs"]:
            row = dict(row)
            row["role"] = _log_role(row)
            row.setdefault("bytes", 0)
            row.setdefault("truncated", False)
            row.setdefault("missing", False)
            if row["missing"]:
                row["path"] = None
                kept.append(row)
                continue
            source = Path(str(row.get("path", "")))
            safe = "".join(ch for ch in str(row.get("name", "")) if ch.isalnum() or ch in "-_.")[:64]
            if not safe or not source.is_file() or source.is_symlink():
                row["missing"] = True
                row.setdefault("reason", "FileNotFoundError")
                row["path"] = None
                kept.append(row)
                continue
            suffix = source.suffix if source.suffix in {".log", ".json", ".txt"} else ".log"
            filename = safe if Path(safe).suffix in {".log", ".json", ".txt"} else safe + suffix
            target = folder / "logs" / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            kept.append({**row, "missing": False, "path": str(target)})
        receipt["logs"] = kept
    path = folder / RECEIPT_NAME
    partial = path.with_name(RECEIPT_NAME + f".{os.getpid()}.partial")
    partial.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, path)
    _prune(root, keep=folder)
    return path


def _prune(root: Path, *, keep: Path) -> None:
    try:
        rows = sorted(((p.stat().st_mtime, p) for p in root.iterdir() if (p / RECEIPT_NAME).is_file()),
                      key=lambda row: row[0], reverse=True)
        for _mtime, folder in rows[KEEP_RECEIPTS:]:
            if folder != keep:
                shutil.rmtree(folder, ignore_errors=True)
    except OSError:
        pass


def record_failure(facts: Mapping[str, Any], *, job: Any, machine: Mapping[str, Any] | None, timing: Any,
                   root: Path | None = None) -> Path | None:
    """Build and write one receipt; a problem here is logged by class and never raised."""
    try:
        receipt = build_receipt(facts, job=job, machine=machine, timing=timing)
        return write_receipt(receipt, root=root) if receipt is not None else None
    except Exception as exc:  # noqa: BLE001 (never masks the original error)
        import sys
        print(f"failure receipt not written ({type(exc).__name__})", file=sys.stderr)
        return None


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


def render(receipt: Mapping[str, Any]) -> str:
    """The human form: a few fixed lines from the receipt."""
    cause, ident = receipt.get("cause") or {}, receipt.get("identity") or {}
    machine, collection = receipt.get("machine") or {}, receipt.get("collection") or {}
    timing = ((receipt.get("timing") or {}).get("phases") or {})
    lines = [f"Failure: {receipt.get('stage')} ({receipt.get('stage_basis')})",
             str(receipt.get("message")),
             "Cause: " + (", ".join(f"{k}={cause[k]}" for k in cause) or "unknown"),
             f"Task: {ident.get('task_id')}  attempt: {ident.get('attempt_id')}",
             f"Machine: {ident.get('machine_id') or 'none'}  after: {machine.get('after')}"
             + (f"  release: {machine['release_reason']}" if machine.get("release_reason") else ""),
             f"Logs: {collection.get('state')}"]
    if machine.get("after") == "kept":
        lines.append(f"Kept until: {machine.get('expires_at')}  cost: {machine.get('cost_usd')} USD  preparation: {machine.get('preparation_state')}")
    seconds = [f"{name}={row['seconds']}s" for name, row in timing.items()
               if isinstance(row, Mapping) and row.get("seconds") is not None]
    if seconds:
        lines.append("Timing: " + ", ".join(seconds))
    for log in sorted(receipt.get("logs", []), key=log_order):
        if log.get("missing"):
            lines.append(f"Log: {log.get('name')} missing ({log.get('reason', 'unavailable')})")
            continue
        suffix = ", truncated" if log.get("truncated") else ""
        lines.append(f"Log: {log.get('name')} ({_log_role(log)}, {log.get('bytes', 0)} bytes{suffix}) {log.get('path')}")
    return "\n".join(lines)


__all__ = ["SCHEMA", "STAGES", "build_receipt", "decide_stage", "find_receipt", "record_failure", "recorder", "render",
           "write_receipt"]


def kept_failure_details(lease: Any, generation: Any) -> dict[str, str]:
    """The local receipt's reason and log folder for one failed lease generation, if any.

    Best-effort: optional evidence never masks the job failure.
    """
    if lease is None or generation is None:
        return {}
    try:
        for receipt_path in failures_root().glob(f"*/{RECEIPT_NAME}"):
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                identity = receipt.get("identity") or {}
                if identity.get("lease") != lease or identity.get("generation") != generation:
                    continue
                details: dict[str, str] = {}
                reason = (receipt.get("cause") or {}).get("issue_message")
                if isinstance(reason, str) and reason:
                    details["issue_message"] = reason[:ISSUE_MESSAGE_LIMIT]
                if any(log.get("path") and not log.get("missing") for log in receipt.get("logs", [])):
                    details["logs_dir"] = str(receipt_path.parent / "logs")
                return details
            except (OSError, ValueError, TypeError, AttributeError):
                continue
    except OSError:
        pass
    return {}
