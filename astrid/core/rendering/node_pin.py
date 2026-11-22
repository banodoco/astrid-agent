"""Renderer Node pin exports and running-host diagnostics."""

from __future__ import annotations

import json
import re
import shutil as shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from runtime_protocol import platform

from astrid.core.foundation.renderer_toolchain import (
    OVERRIDE_ENV,
    NodeResolution,
    candidates,
    node_pin,
    probe_version,
    resolve_pinned_node,
)


def process_env(pid: int | None, names: Sequence[str]) -> dict[str, str]:
    """Some variables of a running process's environment (same user; macOS ``ps -E``, Linux /proc)."""
    if not pid:
        return {}
    try:
        if sys.platform == "darwin":
            text = subprocess.run(["ps", "-E", "-ww", "-p", str(int(pid)), "-o", "command="], stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=5, check=False).stdout
        else:
            text = Path(f"/proc/{int(pid)}/environ").read_text(encoding="utf-8", errors="replace").replace("\0", " ")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    found = {}
    for name in names:
        match = re.search(rf"(?:^|\s){re.escape(name)}=(\S+)", text)
        if match:
            found[name] = match.group(1)
    return found


def node_report(project_dir: Path, in_use: str | None) -> dict[str, object]:
    """For ``doctor``: the Node the renderer runs against the project's pin."""
    pin = node_pin(project_dir)
    version = probe_version(in_use) if in_use else None
    if not in_use:
        status, detail = "unknown", "the pack host is not running (or its environment cannot be read)"
    elif version is None:
        status, detail = "broken", f"{in_use} is not a working Node"
    elif pin and version != pin:
        status, detail = "mismatch", f"Node {version} at {in_use}, but {Path(project_dir) / 'package.json'} pins {pin}"
    else:
        status, detail = "ok", f"Node {version} at {in_use}" + (f" (the pin is {pin})" if pin else " (no pin)")
    return {"status": status, "node": in_use, "version": version, "pin": pin, "detail": detail,
            "fix": "python -m astrid dev promote (the launch resolves the pinned Node)" if status in ("mismatch", "broken") else None}


def doctor_section(support_root: Path | str | None) -> dict[str, object]:
    """The running pack host's renderer Node against its served project's pin (read-only)."""
    if support_root is None:
        return {"status": "unknown", "detail": "support root not resolved"}
    try:
        host = json.loads((Path(support_root) / "runtime" / "generic-host.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        host = {}
    checkout = host.get("source_checkout")
    project = Path(str(checkout)) / "remotion" if checkout else None
    if project is None or not (project / "package.json").is_file():
        return {"status": "unknown", "detail": "no served Remotion project recorded"}
    pid = host.get("pid") if isinstance(host.get("pid"), int) else None
    alive = False
    if pid:
        try:
            if platform.process_identity(pid) is None:
                raise ProcessLookupError(pid)
            alive = True
        except OSError:
            alive = False
    in_use = process_env(pid, [OVERRIDE_ENV]).get(OVERRIDE_ENV) if alive else None
    return {**node_report(project, in_use), "host_pid": pid if alive else None}


__all__ = ["NodeResolution", "OVERRIDE_ENV", "candidates", "doctor_section", "node_pin", "node_report", "probe_version",
           "process_env", "resolve_pinned_node"]
