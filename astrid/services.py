"""Declare Astrid's services for the Runtime's generic profile supervisor."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


def prepare_pack_host_shutdown(*, support_root: Path, source_checkout: Path,
                               runtime_instance_id: str | None):
    """Bind Runtime down to this owner's registered host, before any effects.

    Canonical services are reaped by Runtime's supervisor. The SDK can also
    launch a resident host; its existing registration/retirement owner handles
    that process, under the same lock used by host startup.
    """
    from astrid.sdk import host_bootstrap as hosts
    from runtime_protocol import platform

    support_root = support_root.resolve()
    state_path = support_root / "generic-host.json"

    def read_state():
        value = hosts._read_object(state_path)
        if value is None and (state_path.exists() or state_path.is_symlink()):
            raise hosts.PackHostBootstrapError("pack host registration is unreadable; shutdown unresolved")
        return value

    state = read_state()
    kernel_host = None
    managed = bool(state and state.get("runtime_service_launch_id"))
    if state is not None:
        if (not runtime_instance_id or state.get("runtime_instance_id") != runtime_instance_id
                or state.get("support_root") != str(support_root)
                or state.get("source_checkout") != str(source_checkout.resolve())):
            raise hosts.PackHostBootstrapError("pack host registration differs from the selected Runtime owner")
        status = hosts._registered_host_status(state)
        if status == "foreign":
            raise hosts.PackHostBootstrapError("registered process is not this pack host; shutdown refused")
        if status != "ours":
            # The legacy registration has no PID-namespace scope. An
            # unreachable PID is not evidence that its host exited.
            raise hosts.PackHostBootstrapError(
                f"registered host process scope is unverified at {state_path}; "
                "retry down from the owning launcher after host readiness"
            )
        kernel_host = platform.process_identity(int(state["pid"]))
        if (kernel_host is None or not kernel_host.get("birth_id")
                or platform.process_birth_matches(kernel_host["birth_id"], kernel_host["birth_id"]) is not True
                or hosts._registered_host_status(state) != "ours"):
            raise hosts.PackHostBootstrapError("pack host kernel identity changed before Runtime shutdown")

    def verify_exit():
        if kernel_host is not None:
            observed = platform.process_identity(int(kernel_host["pid"]))
            if (observed is not None
                    and platform.process_birth_matches(kernel_host["birth_id"], observed.get("birth_id", "")) is not False):
                raise hosts.PackHostBootstrapError("pack host exit remains unverified after Runtime shutdown")

    def stop():
        with (support_root / "generic-host.lock").open("a+") as lock:
            platform.owner_only(lock.fileno())
            platform.lock_file(lock.fileno())
            current = read_state()
            # A supervised host can remove its own registration while Runtime
            # drains it. An independent replacement must pass a fresh down.
            if current is not None and current != state:
                raise hosts.PackHostBootstrapError("pack host registration changed during Runtime shutdown")
            if state is not None:
                if hosts._registered_host_status(state) == "foreign":
                    raise hosts.PackHostBootstrapError("registered process changed during Runtime shutdown")
                if managed:
                    # Runtime owns the supervised Job. Its next foreground
                    # service owns consumption of the existing drain receipt.
                    verify_exit()
                else:
                    hosts._terminate_old_host(state)
                    verify_exit()
                    # Keep registration custody until exit is positively
                    # observed, then let its existing owner remove markers.
                    hosts._retire_host_locked(state, support_root)

    return stop


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
    host_env = dict(env)
    # These paths bind the installed Python product to source-only resources
    # that are intentionally provisioned beside the candidate checkout.
    for key in (
        "ASTRID_HOST_EXECUTION_PROFILE",
        "ASTRID_SOURCE_PROVENANCE_ROOT",
        "ASTRID_REMOTION_PROJECT_DIR",
        "ASTRID_TIMELINE_SCHEMA_PYTHONPATH",
        "ASTRID_CAPABILITY_MATRIX_PATH",
        "ASTRID_INSTALL_UV",
    ):
        value = os.environ.get(key, "").strip()
        if value:
            host_env[key] = value
    if program_root := os.environ.get("ASTRID_INSTALL_PROGRAM_ROOT"):
        env["ASTRID_INSTALL_PROGRAM_ROOT"] = program_root
        host_env["ASTRID_INSTALL_PROGRAM_ROOT"] = program_root
    # The Windows console entry point belongs to this selected environment.
    # Resolve it at declaration time so the supervised bridge cannot select an
    # older launcher through the Scheduled Task's inherited PATH.
    default_agent_command = str(Path(python).with_name("astrid.exe")) if sys.platform == "win32" else "astrid"
    return {"version": 1, "services": [
        {"name": "pack-host", "argv": [python, "-X", "utf8", "-m", "astrid.core.execution.generic_host", "--service",
            "--source-checkout", str(source_checkout), "--support-root", str(discovery.parent)],
            "env": {**host_env, "ASTRID_PACK_HOST_PYTHON": python}, "restart": "always",
            "ready": {"file": "generic-host.ready.json", "timeout_seconds": 300}},
        {"name": "chat", "argv": [bun, str(Path(__file__).parent / "chat" / "service.mjs")],
            "env": {**env, **chat_options, "ASTRID_ACP_CWD": str(chat_cwd),
                "ASTRID_ACP_COMMAND": os.environ.get("ASTRID_ACP_COMMAND") or default_agent_command,
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
