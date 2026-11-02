#!/usr/bin/env python3
"""Print the installed-only H3/R8 qualification plan without executing it.

This is intentionally a read-only planner. It never imports Astrid modules,
starts a service, writes qualification state, or invokes the listed commands.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys


AGENT_SHA = "3390a9062072092626621135364a131cf7787950"
RUNTIME_PLACEHOLDER = "REQUIRED_FROM_NEW_MGPU_NOTE"
VIBECOMFY_SHA = "ac0ee1b80991f2414dae27d264af4b5e2890a7a6"
OWNER = "haus.astrid.runtime"
CONTRACTS = (
    {
        "id": "worker_profile_emission",
        "entrypoint": "astrid.installer.worker_profile.write_worker_profile",
        "source": "astrid/installer/worker_profile.py:write_worker_profile; astrid/installer/backend.py:_declare_worker_profile",
        "prerequisites": ["real installed astrid and astrid-runtime RECORDs", "task-owned output/data paths", "installed product site-packages as source_checkout"],
        "command": ["{python}", "-I", "-B", "-c", "from pathlib import Path; import sys; from astrid.installer.worker_profile import write_worker_profile; write_worker_profile(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))", "{version_root}", "{product_root}", "{task_data_root}"],
    },
    {
        "id": "strict_owner_projection",
        "entrypoint": "runtime_protocol.local_worker_composition.CrossProcessWorkerPreparer._prepare_host_only",
        "source": "runtime_protocol/local_worker_composition.py; runtime_protocol/local_execution_supervisor.py:RUNTIME_OWNER_FIELDS",
        "prerequisites": ["same installed Runtime composition as the canary", "verified host activation", "task-owned session and support roots", "assert exactly birth_id, coordinator_epoch, pid, runtime_instance_id, uid"],
        "command": ["{python}", "-I", "-B", "{owner_projection_driver}", "--prefix", "{prefix}", "--session-root", "{session_root}"],
    },
    {
        "id": "streamed_inventory_hashing",
        "entrypoint": "runtime_protocol.verified_host_launch._stream_member_digest",
        "source": "runtime_protocol/verified_host_launch.py:_stream_member_digest; retained installed probe: queue/failed/42_R8_VERIFIED_LAUNCH-r5/logs/probe-installed.py.txt",
        "prerequisites": ["locked selected inventory member and expected digest", "task-local evidence output", "preserve read_source 64 MiB refusal as a separate assertion"],
        "command": ["{python}", "-I", "-B", "{inventory_driver}", "--prefix", "{prefix}", "--member", "{locked_member}", "--sha256", "{expected_sha256}"],
    },
    {
        "id": "linux_role_bound_custody_broker",
        "entrypoint": "astrid_runtime.custody_broker.RoleBoundCustodyBroker",
        "source": "Runtime astrid_runtime/custody_broker.py:RoleBoundCustodyBroker; Agent host callsite astrid/core/execution/generic_host.py:_NativeEngineGraph.prepare",
        "prerequisites": ["Linux installed session", "new task-owned ledger root", "default_process_identity from installed Runtime", "no fake broker or patched platform boundary"],
        "command": ["{python}", "-I", "-B", "{custody_driver}", "--prefix", "{prefix}", "--ledger-root", "{session_root}/custody-ledger"],
    },
    {
        "id": "h3_child_admission_and_claim",
        "entrypoint": "tests/test_installer_verified_child.py::test_ordinary_installed_host_admits_and_claims_h3_prepare (journey assertions) ; astrid.sdk.invocation.invoke_result",
        "source": "tests/test_installer_verified_child.py:91-307; installed-child and h3-child drivers retained in queue/failed/42_R8_VERIFIED_LAUNCH-r5/logs",
        "prerequisites": ["same installed service session as canary/R8", "verified host readiness", "production scopes and real WorkspaceClient", "assert delegated child admission plus nonempty attempt_id", "never execute the admitted child"],
        "command": ["{python}", "-I", "-B", "{h3_child_driver}", "--prefix", "{prefix}", "--session-root", "{session_root}"],
    },
)


def _load_tuple(path: Path | None) -> dict[str, str]:
    if path is None:
        return {"agent": AGENT_SHA, "runtime": RUNTIME_PLACEHOLDER, "vibecomfy": VIBECOMFY_SHA}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"agent", "runtime", "vibecomfy"}:
        raise ValueError("tuple record must contain exactly agent, runtime, and vibecomfy")
    if value["agent"] != AGENT_SHA or value["vibecomfy"] != VIBECOMFY_SHA:
        raise ValueError("tuple record differs from the frozen Agent/VibeComfy pins")
    runtime = value["runtime"]
    if runtime != RUNTIME_PLACEHOLDER and not re.fullmatch(r"[0-9a-f]{40}", str(runtime)):
        raise ValueError("runtime must be a full lowercase commit SHA or REQUIRED_FROM_NEW_MGPU_NOTE")
    return {key: str(value[key]) for key in ("agent", "runtime", "vibecomfy")}


def _require_installed_prefix(prefix: Path, tuple_record: dict[str, str]) -> dict[str, str]:
    prefix = prefix.expanduser().absolute()
    if not prefix.is_dir() or prefix.is_symlink():
        raise ValueError(f"installed-prefix check failed at {prefix}: directory is absent or a symlink")
    executable = prefix / "bin/python"
    if not executable.is_file() or not os.access(executable, os.X_OK) or executable.is_symlink():
        raise ValueError(f"installed-prefix check failed at {executable}: expected executable venv Python")
    if not (prefix / "pyvenv.cfg").is_file():
        raise ValueError(f"installed-prefix check failed at {prefix / 'pyvenv.cfg'}: missing venv marker")
    version_root = prefix.parent
    manifest_path = version_root / "manifest.json"
    install_path = version_root.parent.parent / "install.json"
    if not manifest_path.is_file() or not install_path.is_file():
        raise ValueError(f"installed-prefix check failed: expected installer manifest {manifest_path} and owner record {install_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    install = json.loads(install_path.read_text(encoding="utf-8"))
    if install.get("owner") != OWNER:
        raise ValueError(f"installed-prefix check failed at {install_path}: owner is not {OWNER}")
    sources = {key: manifest.get(key, {}).get("sha") for key in ("agent", "runtime", "vibecomfy")}
    if sources != tuple_record:
        raise ValueError(f"installed-prefix tuple mismatch: manifest={sources!r}, supplied={tuple_record!r}")
    if tuple_record["runtime"] == RUNTIME_PLACEHOLDER:
        raise ValueError("future installed execution is closed: Runtime custody SHA is still REQUIRED_FROM_NEW_MGPU_NOTE")
    purelib_candidates = sorted(prefix.glob("lib/python*/site-packages"))
    product_root = next((p for p in purelib_candidates if p.is_dir()), None)
    if product_root is None:
        raise ValueError(f"installed-prefix check failed below {prefix}: no site-packages directory")
    for distribution in ("astrid-*.dist-info", "astrid_runtime-*.dist-info", "astrid_runtime_client-*.dist-info", "vibecomfy-*.dist-info"):
        records = list(product_root.glob(f"{distribution}/RECORD"))
        if len(records) != 1 or not records[0].is_file():
            raise ValueError(f"installed-prefix check failed in {product_root}: expected one real {distribution}/RECORD")
    if prefix.resolve() == Path(sys.prefix).resolve() or Path(sys.executable).resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("installed-prefix check failed: shared/source Python cannot qualify as the installed product")
    return {"prefix": str(prefix), "python": str(executable), "version_root": str(version_root),
            "product_root": str(product_root), "manifest": str(manifest_path), "install_record": str(install_path)}


def _render_command(command: list[str], paths: dict[str, str]) -> list[str]:
    values = {"python": paths.get("python", "<installed-prefix>/bin/python"),
              "prefix": paths.get("prefix", "<installed-prefix>"),
              "version_root": paths.get("version_root", "<installed-prefix-parent>"),
              "product_root": paths.get("product_root", "<installed-product-site-packages>"),
              "task_data_root": "<task-local-data-root>", "session_root": "<installed-session-root>",
              "owner_projection_driver": "<task-local-owner-projection-driver.py>",
              "inventory_driver": "<task-local-inventory-driver.py>",
              "custody_driver": "<task-local-custody-driver.py>",
              "h3_child_driver": "<task-local-h3-child-driver.py>",
              "locked_member": "<locked-inventory-member>", "expected_sha256": "<expected-sha256>"}
    return [part.format_map(values) for part in command]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print deterministic plan; do not import, execute, or write")
    parser.add_argument("--prefix", type=Path, help="explicit installer-created Python prefix (optional in plan-only dry-run)")
    parser.add_argument("--tuple-record", type=Path, help="JSON with exactly agent, runtime, vibecomfy full commits")
    args = parser.parse_args()
    if bool(args.prefix) != bool(args.tuple_record):
        parser.error("--prefix and --tuple-record must be supplied together")
    try:
        tuple_value = _load_tuple(args.tuple_record)
        paths = _require_installed_prefix(args.prefix, tuple_value) if args.prefix else {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"preflight: installed-prefix/tuple check failed: {exc}\n")
    contracts = []
    for row in CONTRACTS:
        contracts.append({**row, "status": "NOT RUN", "command": _render_command(row["command"], paths)})
    result = {
        "schema": "h3-r8-offline-preflight/v1",
        "mode": "dry-run" if args.dry_run else "plan-only",
        "tuple": tuple_value,
        "tuple_status": "runtime SHA required from new M-GPU note" if tuple_value["runtime"] == RUNTIME_PLACEHOLDER else "supplied; must match installed manifest",
        "installed_paths": paths or {"prefix": "<required-installed-prefix>", "python": "<installed-prefix>/bin/python"},
        "contracts": contracts,
        "execution": "not implemented in this preparation runner; listed argv are future operator runbook commands",
        "dry_run_guarantees": ["no Astrid imports", "no command execution", "no service or state creation", "no network", "stdout only"],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
