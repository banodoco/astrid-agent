"""Canonical entrypoint guard for pack run.py modules.

Each pack's ``run.py`` ``__main__`` block should call
``guard_canonical_entrypoint("<pack_id>")`` *before* any other logic. The
guard refuses direct invocation (e.g. ``python -m astrid.packs.video_editing.actions.hype.entrypoint``)
and only allows the call to proceed when launched from the canonical Astrid
runners, which set ``ASTRID_INTERNAL_INVOCATION=1`` in the subprocess env, or
from the sanctioned in-process runtime context.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

from astrid.core.contracts.errors import (
    AstridError,
    coerce_astrid_error,
    render_astrid_error,
)
from astrid.core.env_vars import ASTRID_INTERNAL_INVOCATION, ASTRID_PROJECT_RUN

_CANONICAL_RUNTIME_CAPABILITY: ContextVar[str | None] = ContextVar(
    "astrid_canonical_runtime_capability",
    default=None,
)


@contextmanager
def canonical_runtime_entrypoint(capability_id: str) -> Iterator[None]:
    """Allow guarded imports/execution for one capability in this call stack."""

    token = _CANONICAL_RUNTIME_CAPABILITY.set(capability_id)
    try:
        yield
    finally:
        _CANONICAL_RUNTIME_CAPABILITY.reset(token)


def guard_canonical_entrypoint(pack_id: str) -> None:
    """Refuse direct invocation. Call from each pack's ``__main__`` block."""
    import os
    import sys

    if os.environ.get(ASTRID_INTERNAL_INVOCATION):
        return
    if _CANONICAL_RUNTIME_CAPABILITY.get() == pack_id:
        return
    print(
        f"error: this pack ({pack_id}) is not meant to be invoked directly; "
        f"run `astrid --help`, `astrid doctor`, or use the SDK "
        f"(astrid.sdk.invoke) instead.\n"
        f"(direct `python -m astrid.packs.<...>.run` invocation is reserved\n"
        f"for internal use by the astrid runner.)",
        file=sys.stderr,
    )
    sys.exit(2)


def warn_if_unledgered() -> None:
    """Emit a stderr warning when a generation executor's main() runs without the
    harness marker ``ASTRID_PROJECT_RUN``.

    This is warning-only — no ledger entry is created.  When the environment
    variable is present the call is in-band and no warning is produced.
    """
    import os
    import sys

    if not os.environ.get(ASTRID_PROJECT_RUN):
        print(
            "[astrid] running unledgered — invoke through executors run"
            " or the SDK to persist a run record",
            file=sys.stderr,
        )


def run_pack_main(
    capability_id: str,
    runner: Callable[[], int],
    *,
    argv: list[str] | None = None,
    recovery_command: str | None = None,
) -> int:
    """Run a pack entrypoint with the canonical AstridError renderer."""

    snapshot = {"argv": list(argv or ()), "capability_id": capability_id}
    try:
        return runner()
    except AstridError as exc:
        if exc.recovery_command:
            return render_astrid_error(exc)
        # Pack actions raise bare AstridError/AstridValueError; give them the same
        # recovery hint and snapshot the generic ValueError/RuntimeError path gets.
        exc.recovery_command = recovery_command or f"python3 -m astrid executors run {capability_id} --help"
        exc.recovery = exc.recovery_command
        exc.state_snapshot = {**snapshot, **exc.state_snapshot}
        return render_astrid_error(exc)
    except (ValueError, RuntimeError) as exc:
        return render_astrid_error(
            AstridError(
                str(exc),
                recovery_command=recovery_command or f"python3 -m astrid executors run {capability_id} --help",
                state_snapshot=snapshot,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return render_astrid_error(
            coerce_astrid_error(
                exc,
                state_snapshot={**snapshot, "original_type": type(exc).__name__},
                degraded=True,
            )
        )
