"""Bounded current-user keep-data uninstall product control.

This is deliberately not an R-36 driver or a source of native observations.
It exposes the existing installer uninstall only for the current user's
descriptor-verified login installation.  Callers cannot select roots, service
names, processes, or whether data is deleted.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import cli
from .descriptor import describe_current_user_installation


@dataclass(frozen=True)
class KeepDataUninstallResult:
    """The bounded request and the existing install command's exit code.

    ``program_root`` and ``data_root`` describe the selected installation
    bound before the command is invoked.  They are not post-removal claims:
    an exit code alone cannot attest manager, process, program, or data state.
    """

    exit_code: int
    requested: bool
    keep_data: bool
    program_root: str
    data_root: str


def uninstall_current_user_keep_data() -> KeepDataUninstallResult:
    """Run the normal locked uninstall for the descriptor-verified selection.

    The existing CLI takes its normal per-user installer lock, re-resolves the
    login selection while locked, and runs its real removal preflight before
    Stop/unregister/remove.  ``--delete-data`` is intentionally absent.
    """

    installation = describe_current_user_installation()
    exit_code = cli.main([
        "--uninstall",
        "--root", installation.program_root,
        "--data-root", installation.data_root,
    ])
    return KeepDataUninstallResult(
        exit_code=exit_code,
        requested=True,
        keep_data=True,
        program_root=installation.program_root,
        data_root=installation.data_root,
    )
