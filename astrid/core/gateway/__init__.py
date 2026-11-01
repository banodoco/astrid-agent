#!/usr/bin/env python3
"""Astrid top-level command gateway.

The gateway dispatches to the five product families (``projects``,
``timelines``, ``media``, ``tasks``, ``runs``), the two operational families
(``doctor``, ``backup``), and installed external pack tools. ``--help``/``-h``
and ``help`` print documentation (help is documentation and never requires a
session); ``--version`` prints the version. Everything else is dispatched to
a core family or a discovered pack route.

The settled unbound allowlist is recorded in
``SPRINT1_UNBOUND_ALLOWLIST_CONTRACT`` below: ``doctor`` and
``backup`` are operational families that must run before any project is selected,
and ``help``/``--version`` are documentation.
"""

from __future__ import annotations

import os
import sys
import time

# Shared by `astrid-tools` and `python -m astrid`: start before gateway imports.
_GATEWAY_IMPORTED_MONO = time.monotonic()
_MAIN_CALLS = 0

from astrid.core.contracts.errors import (
    AstridError,
    render_astrid_error,
    wrap_degraded_error,
)
from astrid.core.gateway.dispatch import (
    _TOP_LEVEL_HANDLERS,
    _build_dispatch_parser,
    _dispatch_backup,
    _dispatch_doctor,
    _dispatch_product,
    _dispatch_status,
    _top_level_commands,
)
from astrid.core.gateway.help import (
    _print_entrypoint_help,
    _print_product_help,
    _product_help_text,
)
from astrid.version import ASTRID_VERSION

from . import dispatch as _gateway_dispatch


# Canonical accepted unbound contract for the gateway. Only the
# two operational families (which must run before any session exists) and
# help/version documentation are sessionless; everything else dispatches to a
# family handler.
SPRINT1_UNBOUND_ALLOWLIST_CONTRACT: tuple[tuple[str, ...], ...] = (
    ("-h",),
    ("--help",),
    ("help",),
    ("--version",),
    ("doctor",),
    ("backup",),
    ("install",),
    ("status",),
    ("auth",),
    ("login",),
    ("logout",),
    ("revoke",),
)


def main(argv: list[str] | None = None) -> int:
    global _MAIN_CALLS
    os.environ["ASTRID_CLI_START_MONO"] = str(_GATEWAY_IMPORTED_MONO if _MAIN_CALLS == 0 else time.monotonic())
    _MAIN_CALLS += 1
    raw = sys.argv[1:] if argv is None else list(argv)
    try:
        return _main_impl(raw)
    except SystemExit as exc:
        # Product-family argparse parsers use SystemExit for their normal
        # command-line result. Normalize only integer/empty exit codes at the
        # public gateway boundary so embedded callers receive an int while
        # ``python -m astrid`` retains the same shell status.
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        raise
    except AstridError as exc:
        return render_astrid_error(exc)
    except Exception as exc:  # noqa: BLE001
        bug = wrap_degraded_error(
            exc,
            state_snapshot={"argv": raw, "entrypoint": "astrid.core.gateway.main"},
        )
        return render_astrid_error(bug)


def _main_impl(raw: list[str]) -> int:
    first_arg = next(iter(raw), None)
    if first_arg == "--agent-context":
        # This public helper is intentionally session-light: agent_context
        # opens an already-running Runtime and turns unavailability into a
        # successful, empty-safe prompt block. It must not enter product
        # dispatch (which treats leading flags as command names).
        from astrid.agent_context import main as agent_context_main

        return agent_context_main(raw[1:])
    if first_arg in {"-h", "--help"}:
        _print_entrypoint_help()
        return 0
    # `astrid help` is the product-focused executable help: the seven core families, external pack routes, nested mounts, the --json envelope
    # convention, and stable exit codes. It is session-free by construction
    # (help is documentation).
    if first_arg == "help":
        _print_product_help()
        return 0
    if first_arg == "--version":
        print(f"astrid/{ASTRID_VERSION}")
        return 0
    # Reserved install/status commands sit outside the long-standing seven
    # gateway families so pack discovery cannot claim them and product-family
    # census consumers remain stable.
    if first_arg == "install":
        from astrid.installer.cli import main as install_main
        return install_main(raw[1:])
    if first_arg == "status":
        return _dispatch_status(raw[1:])
    if first_arg == "auth":
        from astrid.core.auth import run_auth
        return int(run_auth(raw[1:]))
    if first_arg in {"login", "logout", "revoke"}:
        from astrid.core.auth import run_auth
        return int(run_auth(raw))
    return _dispatch(raw)


def _dispatch(raw: list[str]) -> int:
    _configure_process_control()
    return _gateway_dispatch._dispatch(raw)


def _configure_process_control() -> None:
    from astrid.core.execution.platform_helpers import configure_process_control_from_install

    configure_process_control_from_install()


if __name__ == "__main__":
    raise SystemExit(main())
