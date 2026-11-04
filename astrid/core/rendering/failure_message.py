"""Actionable render failures carried by Runtime's existing message field."""

from __future__ import annotations

import os
import shlex
import sys
from typing import Mapping


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _repair_install(environment: Mapping[str, str]) -> str | None:
    root = environment.get("ASTRID_INSTALL_PROGRAM_ROOT", "")
    pinokio = environment.get("ASTRID_INSTALL_MANAGED_BY") == "pinokio"
    if not root and not pinokio:
        return None
    if pinokio and not root:
        # This is also the launcher's guidance for an incomplete installation.
        return "Stop Astrid in Pinokio, choose Reset, then install again and choose Start."
    if sys.platform == "win32":
        command = ("& ([scriptblock]::Create((irm https://astrid.haus/install.ps1)))"
                   f" --root {_powershell_quote(root)}")
    else:
        command = f"curl -fsSL https://astrid.haus/install | sh -s -- --root {shlex.quote(root)}"
    if pinokio:
        command += " --managed-by pinokio"
        return f"Stop Astrid in Pinokio, rerun the installer to repair it: `{command}`. Then choose Start."
    return f"Rerun the Astrid installer to repair this installation: `{command}`."


def _repair_source(environment: Mapping[str, str]) -> str:
    from astrid.core.rendering import remotion_runtime
    from astrid.core.rendering.node_pin import node_pin, resolve_pinned_node

    try:
        project = remotion_runtime.resolve_remotion_project_dir(environment=environment)
    except ValueError:
        return "Set ASTRID_REMOTION_PROJECT_DIR to the absolute path of the source Remotion project, then restart the source host."
    node = resolve_pinned_node(project, override=environment.get("ASTRID_NODE_EXECUTABLE"),
                               search_path=environment.get("PATH"))
    pin = node_pin(project) or "the project's pinned version"
    if sys.platform == "win32":
        command = f"Set-Location -LiteralPath {_powershell_quote(str(project))}; "
        if node.path:
            command += f"$env:Path = {_powershell_quote(str(node.path.parent) + ';')} + $env:Path; "
        command += "npm ci"
    else:
        command = f"cd {shlex.quote(str(project))} && "
        if node.path:
            command += f'PATH={shlex.quote(str(node.path.parent))}:"$PATH" '
        command += "npm ci"
    preparation = f"With Node {pin} on PATH, run" if node.path else f"Install Node {pin} and put it on PATH, then run"
    return f"{preparation}: `{command}`. Then restart the source host."


def render_failure_message(detail: str, *, remotion_reason: str | None = None,
                           unavailable: bool = False, recovery: str = "",
                           environment: Mapping[str, str] | None = None) -> str:
    """Keep the root cause first, with the original refusal and a separate fix.

    Runtime already transports a free-text executor message. Labeled lines keep
    recovery readable for old clients without adding another wire error shape.
    """
    environment = os.environ if environment is None else environment
    # Keep the toolchain's npm advice out of both classification and detail;
    # the fix below owns the install-specific command and selected Node path.
    detail = detail.split("; run `npm ci` in ", 1)[0]
    if remotion_reason:
        reason = remotion_reason.split("; run `npm ci` in ", 1)[0]
        missing = ("node_modules is missing", "package(s) missing", "CLI is missing",
                   "project directory not found", "package.json missing")
        if any(part in reason for part in missing):
            headline = "Remotion isn't installed on this machine"
        elif "unexpected name:" in reason or "unexpected version:" in reason:
            headline = "Remotion's installed packages don't match this version of Astrid"
        elif "Node" in reason or "NODE_EXECUTABLE" in reason:
            headline = "Remotion needs the pinned Node version"
        else:
            headline = "Remotion isn't ready on this machine"
        fix = _repair_install(environment) or _repair_source(environment)
    else:
        headline = "The renderer isn't available" if unavailable else "The render couldn't finish"
        fix = recovery or "Review the details, correct the render settings or timeline, and try again."
    # Leave room for the fix inside the legacy bridge's 4,000-character message.
    budget = max(0, 3_950 - len(headline) - len(fix))
    if len(detail) > budget:
        detail = detail[:max(0, budget - 3)] + "..."
    return f"Headline: {headline}\nDetail: {detail}\nFix: {fix}"
