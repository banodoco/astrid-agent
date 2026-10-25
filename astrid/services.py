"""Declare Astrid's services for the Runtime's generic profile supervisor."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


def service_declaration(
    *, source_checkout: Path, data_root: Path, python: str | None = None, bun: str | None = None,
    chat_cwd: Path | None = None,
) -> dict[str, Any]:
    """Return contract/schemas/services.json v1 with concrete, shell-free argv."""
    if not source_checkout.is_absolute() or not data_root.is_absolute():
        raise ValueError("source checkout and data root must be absolute")
    python = python or sys.executable
    bun = bun or shutil.which("bun")
    if not bun:
        raise ValueError("Bun 1.3.14 is required for Astrid chat")
    if not Path(python).is_absolute() or not Path(bun).is_absolute():
        raise ValueError("service interpreters must be absolute")
    chat_cwd = chat_cwd or Path(os.environ.get("ASTRID_ACP_CWD") or data_root / "chat")
    if not chat_cwd.is_absolute():
        raise ValueError("chat cwd must be absolute")
    chat_options = {key: os.environ[key] for key in (
        "ASTRID_ACP_PROFILE", "ASTRID_ACP_SESSION_DIR", "ASTRID_ACP_SYSTEM_PROMPT_FILE",
        "ASTRID_ACP_BRIDGE_PORT",
    ) if os.environ.get(key, "").strip()}
    discovery = data_root / "runtime" / "discovery.json"
    env = {
        "ASTRID_RUNTIME_DATA_ROOT": str(data_root),
        "ASTRID_SUPPORT_ROOT": str(data_root),
        "ASTRID_WORKSPACE_DISCOVERY": str(discovery),
        "ASTRID_SERVICE_PYTHON": python,
    }
    if program_root := os.environ.get("ASTRID_INSTALL_PROGRAM_ROOT"):
        env["ASTRID_INSTALL_PROGRAM_ROOT"] = program_root
    return {"version": 1, "services": [
        {"name": "pack-host", "argv": [python, "-m", "astrid.core.execution.generic_host", "--service",
            "--source-checkout", str(source_checkout), "--support-root", str(discovery.parent)],
            "env": {**env, "ASTRID_PACK_HOST_PYTHON": python}, "restart": "always",
            "ready": {"file": "generic-host.ready.json", "timeout_seconds": 300}},
        {"name": "chat", "argv": [bun, str(Path(__file__).parent / "chat" / "service.mjs")],
            "env": {**env, **chat_options, "ASTRID_ACP_CWD": str(chat_cwd),
                "ASTRID_ACP_COMMAND": os.environ.get("ASTRID_ACP_COMMAND") or "astrid",
                "PATH": os.pathsep.join((str(Path(python).parent), str(Path(bun).parent), os.environ.get("PATH", ""))),
                "ASTRID_ACP_LOCAL_MEDIA_ONLY": "1"}, "restart": "always",
            "ready": {"file": "chat.json", "timeout_seconds": 30}},
    ]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    declare = sub.add_parser("declare")
    declare.add_argument("--source-checkout", type=Path, required=True)
    declare.add_argument("--data-root", type=Path, required=True)
    declare.add_argument("--python")
    declare.add_argument("--bun")
    declare.add_argument("--chat-cwd", type=Path)
    host = sub.add_parser("pack-host")
    host.add_argument("--source-checkout", type=Path, required=True)
    host.add_argument("--support-root", type=Path, required=True)
    secure = sub.add_parser("secure-file")
    secure.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if args.command == "declare":
        print(json.dumps(service_declaration(source_checkout=args.source_checkout,
            data_root=args.data_root, python=args.python, bun=args.bun, chat_cwd=args.chat_cwd), indent=2))
        return 0
    if args.command == "secure-file":
        from runtime_protocol import platform

        platform.owner_only(args.path)
        return 0
    from astrid.core.execution.platform_helpers import configure_process_control
    from astrid.sdk.host_bootstrap import run_pack_host_foreground

    discovery = Path(os.environ["ASTRID_WORKSPACE_DISCOVERY"])
    if not discovery.is_absolute() or not args.source_checkout.is_absolute():
        parser.error("discovery and source checkout must be absolute")
    if discovery.parent != args.support_root:
        parser.error("support root must match the Runtime discovery directory")
    configure_process_control(args.support_root)
    value = json.loads(discovery.read_text(encoding="utf-8"))
    value["source_checkout"] = str(args.source_checkout)
    return run_pack_host_foreground(value)


if __name__ == "__main__":
    raise SystemExit(main())
