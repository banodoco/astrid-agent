"""Apply supported timeline edit files through one runner transaction."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from astrid.sdk.timeline.commands import CommandError
from astrid.sdk.timeline.commands import run as run_command
from astrid.sdk.timeline.transaction import edit_transaction
from astrid.sdk.timeline_sheet import apply_sheet


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

    with edit_transaction(
        tl,
        f"apply {source_path.name}",
        check=False,
        quiet=True,
        record_no_change=False,
        record_rollback=False,
    ) as tx:
        tx.canonical = True
        if suffix == ".py":
            _apply_python(tl, source_path, source)
        elif suffix == ".jsonl":
            _apply_commands_jsonl(tl, source)
        else:
            apply_sheet(tl, source)

    return list(tx.changes) if tx.changed else []
