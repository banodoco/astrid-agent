"""Apply supported timeline edit files through one runner transaction."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from astrid.sdk.timeline.commands import CommandError, EditCommand
from astrid.sdk.timeline.commands import run as run_command
from astrid.sdk.timeline import store
from astrid.sdk.timeline.transaction import edit_transaction
from astrid.sdk.timeline_sheet import apply_sheet, parse_sheet

_AUDITED_DYNAMIC_OPERATION = "timeline.operation/beat_tools.fit_to_beats"


def _dynamic_operation_audit(call: dict[str, Any]) -> str | None:
    """Return the pinned label for the one admitted dynamic OpCall, if eligible."""
    try:
        command = EditCommand.coerce(call)
        if (command.op != "timeline.operation" or command.select is not None or command.resolved
                or command.expect is not None or command.bindings is not None
                or command.created is not None or command.authorship is not None):
            return None
        args = dict(command.args)
        if set(args) != {"identity", "implementation_digest", "inputs"}:
            return None
        identity = args["identity"]
        if identity != _AUDITED_DYNAMIC_OPERATION or not isinstance(args["inputs"], dict):
            return None

        from astrid.core.pack.operations import resolve_operation

        operation = resolve_operation(identity)
        if (not operation.eligible or operation.identity != identity
                or args["implementation_digest"] != operation.implementation_digest):
            return None
        # Keep malformed operation payloads on the existing unaudited refusal path.
        import jsonschema

        jsonschema.Draft202012Validator(operation.descriptor["inputs"]).validate(args["inputs"])
    except Exception:
        # This is an audit-eligibility probe. Normal apply execution below remains
        # responsible for reporting malformed, stale, or unresolved OpCalls.
        return None
    return f"{operation.identity}@{operation.implementation_digest}"


def _no_change_operation(sheet: dict[str, Any]) -> tuple[str, bool] | None:
    """Return a label and rollback-audit flag for one explicit eligible OpCall."""
    calls = sheet.get("commands", [])
    if len(calls) != 1:
        return None
    call = calls[0]
    operation_id = call.get("op") if isinstance(call, dict) else None
    if operation_id == "timeline.operation":
        label = _dynamic_operation_audit(call)
        return (label, True) if label is not None else None
    if not isinstance(operation_id, str):
        return None
    from astrid.sdk.timeline.commands import REGISTRY

    operation = REGISTRY.get(operation_id)
    if (operation is None or operation.op != operation_id
            or operation.canonical_name != operation_id or operation.boundary != "edit"
            or operation_id == "timeline.restructure"):
        # Restructure intentionally keeps the runner's existing silent no-op policy.
        return None
    return operation_id, False


def _apply_commands_jsonl(tl: Any, text: str) -> None:
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            command = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CommandError(f"line {number}: invalid JSON: {exc.msg}") from None
        try:
            run_command(tl, command)
        except Exception as exc:  # noqa: BLE001 - preserve the input line for any command failure
            raise CommandError(f"line {number}: {exc}") from None


def _apply_python(tl: Any, path: Path, source: str) -> None:
    # Python files are trusted edit scripts. They use the existing Checkout SDK
    # and OpCall runner; the supplied `run` helper is shorthand for commands.run.
    # Defer save until the outer transaction commits, and keep history/publish
    # operations from opening a second persistence boundary inside the script.
    absent = object()
    previous = {name: tl.__dict__.get(name, absent) for name in ("save", "undo", "redo", "publish", "discard")}

    def defer_save(path: str | Path | None = None, **_kwargs):
        if path is not None and tl.path is not None and Path(path) != Path(tl.path):
            raise CommandError("an apply script can save only through its enclosing transaction")
        return Path(tl.path) if tl.path else None

    def reject_storage(name: str):
        def reject(*_args, **_kwargs):
            raise CommandError(f"{name} is managed by the enclosing apply transaction")
        return reject

    tl.save = defer_save
    for name in ("undo", "redo", "publish", "discard"):
        if callable(getattr(tl, name, None)):
            setattr(tl, name, reject_storage(name))
    namespace = {
        "__file__": str(path),
        "__name__": "__astrid_timeline_script__",
        "tl": tl,
        "run": lambda command: run_command(tl, command),
    }
    try:
        exec(compile(source, str(path), "exec"), namespace)
    finally:
        for name, value in previous.items():
            if value is absent:
                delattr(tl, name)
            else:
                setattr(tl, name, value)


def apply_file(tl: Any, path: str | Path) -> list[str]:
    """Apply a sheet, OpCall JSONL stream, or Python edit script atomically.

    All formats share one outer runner transaction. The document and journal are
    written only after the complete input has been processed successfully. A
    processing exception therefore restores memory without adding a rollback
    journal record or changing either file on disk.
    """
    source_path = Path(path)
    source = source_path.read_text(encoding="utf-8")
    suffix = source_path.suffix.lower()
    sheet_audit = None
    if suffix not in {".py", ".jsonl"}:
        sheet_audit = _no_change_operation(parse_sheet(source))
    sheet_operation = sheet_audit[0] if sheet_audit is not None else None
    record_sheet_rollback = sheet_audit is not None and sheet_audit[1]
    before_snapshot = store._snapshot(tl) if sheet_operation is not None else None
    # Exception audit uses edit_transaction's initial label argument.
    transaction_label = sheet_operation if record_sheet_rollback else f"apply {source_path.name}"

    with edit_transaction(
        tl,
        transaction_label,
        check=False,
        quiet=True,
        record_no_change=sheet_operation is not None,
        record_rollback=record_sheet_rollback,
    ) as tx:
        tx.canonical = True
        if suffix == ".py":
            _apply_python(tl, source_path, source)
        elif suffix == ".jsonl":
            _apply_commands_jsonl(tl, source)
        else:
            apply_sheet(tl, source)
            if (sheet_operation is not None and not tx.cancelled
                    and store._snapshot(tl) == before_snapshot):
                tx.label = sheet_operation

    return list(tx.changes) if tx.changed else []
