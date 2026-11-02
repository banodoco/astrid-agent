#!/usr/bin/env python3
"""Plan the uncaptured G0 H3 surfaces for a later installed canary session.

Dry-run is metadata-only. Capture is opt-in and is deliberately blocked until
the missing P3-P10/P12 scenario commands and inputs have a reviewed mapping.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

from h3_r8_preflight import AGENT_SHA, OWNER, RUNTIME_PLACEHOLDER, VIBECOMFY_SHA

G0_REPORT = "/home/user/astrid/orch/tasks/G0-FIXTURES/REPORT.md"
BASELINE_COMMANDS = "/home/user/astrid/orch/tasks/G0-FIXTURES/GOLDENS/inputs/case-commands.json"

CASES = (
    ("P1", "CLI help, refusals, SDK options and combinations", "partial; G0 has help/refusal vectors only", ("cli-help-root", "cli-help-plan", "cli-help-run", "cli-help-compose", "cli-help-edit")),
    ("P2", "plans, previews and no-acquisition witnesses", "partial; one generate/reference plan exists and needs its approved input map", ("plan-generate-reference",)),
    ("P3", "admitted H3 frozen-input custody", "no reviewed G0 command/fixture map", ()),
    ("P4", "engine output roles and cardinality", "no reviewed G0 command/fixture map", ()),
    ("P5", "final media, masks, verification and tamper publication", "recovery is partial; no reviewed final-H3 command map", ()),
    ("P6", "managed delivery and atomic export", "no reviewed G0 command/fixture map", ()),
    ("P7", "final receipt, status, readback and publication chain", "recovery receipt is partial; no reviewed final-H3 command map", ()),
    ("P8", "accepted prompt, cancel/race/queue and interrupt", "no reviewed G0 command/fixture map", ()),
    ("P9", "host crash/retry and unknown engine ACK", "manual-settlement recovery is partial; no reviewed G0 command map", ()),
    ("P10", "installed qualification, endpoint custody and lease lifecycle", "no reviewed G0 command/fixture map", ()),
    ("P11", "error/status families", "partial; G0 has refusal vectors only", ("refusal-run", "refusal-compose", "refusal-edit")),
    ("P12", "child import receipt, training/iteration/stream and editorial controls", "no reviewed installed-session command map", ()),
)


def _tuple(path: Path | None) -> dict[str, str]:
    if path is None:
        return {"agent": AGENT_SHA, "runtime": RUNTIME_PLACEHOLDER, "vibecomfy": VIBECOMFY_SHA}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"agent", "runtime", "vibecomfy"}:
        raise ValueError("tuple record must contain exactly agent, runtime, and vibecomfy")
    if value["agent"] != AGENT_SHA or value["vibecomfy"] != VIBECOMFY_SHA:
        raise ValueError("tuple record differs from the frozen Agent/VibeComfy pins")
    if value["runtime"] != RUNTIME_PLACEHOLDER and not re.fullmatch(r"[0-9a-f]{40}", str(value["runtime"])):
        raise ValueError("runtime must be a full lowercase commit SHA or REQUIRED_FROM_NEW_MGPU_NOTE")
    return {key: str(value[key]) for key in ("agent", "runtime", "vibecomfy")}


def _installed_paths(prefix: Path, tuple_value: dict[str, str]) -> dict[str, str]:
    prefix = prefix.expanduser().absolute()
    python = prefix / "bin/python"
    version_root = prefix.parent
    manifest_path = version_root / "manifest.json"
    install_path = version_root.parent.parent / "install.json"
    if not prefix.is_dir() or prefix.is_symlink() or not python.is_file() or python.is_symlink() or not os.access(python, os.X_OK):
        raise ValueError(f"installed-prefix check failed at {prefix}: expected a real installer-created venv with executable bin/python")
    if not (prefix / "pyvenv.cfg").is_file() or not manifest_path.is_file() or not install_path.is_file():
        raise ValueError(f"installed-prefix check failed: expected pyvenv.cfg, {manifest_path}, and {install_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    install = json.loads(install_path.read_text(encoding="utf-8"))
    if install.get("owner") != OWNER:
        raise ValueError(f"installed-prefix check failed at {install_path}: owner must be {OWNER}")
    actual = {key: manifest.get(key, {}).get("sha") for key in ("agent", "runtime", "vibecomfy")}
    if actual != tuple_value:
        raise ValueError(f"installed-prefix tuple mismatch: manifest={actual!r}, supplied={tuple_value!r}")
    if tuple_value["runtime"] == RUNTIME_PLACEHOLDER:
        raise ValueError("capture blocked: Runtime custody SHA is still REQUIRED_FROM_NEW_MGPU_NOTE")
    if prefix.resolve() == Path(sys.prefix).resolve():
        raise ValueError("capture blocked: selected prefix is this shared/source interpreter")
    purelib = next(iter(sorted(prefix.glob("lib/python*/site-packages"))), None)
    if purelib is None or not purelib.is_dir():
        raise ValueError(f"installed-prefix check failed below {prefix}: no installed site-packages")
    for pattern in ("astrid-*.dist-info/RECORD", "astrid_runtime-*.dist-info/RECORD", "vibecomfy-*.dist-info/RECORD"):
        matches = [path for path in purelib.glob(pattern) if path.is_file()]
        if len(matches) != 1:
            raise ValueError(f"installed-prefix check failed below {purelib}: expected exactly one real {pattern}")
    return {"prefix": str(prefix), "python": str(python), "version_root": str(version_root),
            "manifest": str(manifest_path), "install_record": str(install_path), "product_root": str(purelib)}


def _load_baseline() -> dict:
    value = json.loads(Path(BASELINE_COMMANDS).read_text(encoding="utf-8"))
    if set(value) != {"cli-help-root", "cli-help-plan", "cli-help-run", "cli-help-compose", "cli-help-edit",
                      "plan-generate-reference", "refusal-compose", "refusal-edit", "refusal-run"}:
        raise ValueError(f"G0 command inventory changed at {BASELINE_COMMANDS}; review mapping before use")
    return value


def _commands(command_ids: tuple[str, ...], baseline: dict, python: str) -> list[list[str]]:
    result = []
    for command_id in command_ids:
        original = list(baseline[command_id]["argv"])
        # Preserve the old command arguments exactly; replace only the old
        # interpreter and stale absolute output/input roots with session slots.
        args = original[1:]
        if args and args[0] == "-m":
            pass
        if command_id == "plan-generate-reference":
            args = [part.replace("/home/user/astrid/orch/tasks/G0-RUN/GOLDENS/inputs/asset-map.json", "{session_root}/fixtures/asset-map.json")
                        .replace("/home/user/astrid/orch/tasks/G0-RUN/GOLDENS/cases/plan/output", "{output_dir}/P2-plan")
                    for part in args]
        if command_id.startswith("refusal-"):
            args = [part.replace("/home/user/astrid/orch/tasks/G0-RUN/tmp/", "{output_dir}/") for part in args]
        result.append([python, "-I", "-B", *args])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="list all uncaptured P1-P12 cases and commands; read no media and write nothing")
    mode.add_argument("--capture", action="store_true", help="opt in to capture (currently blocked until remaining command map is reviewed)")
    parser.add_argument("--prefix", type=Path, help="explicit installed Python prefix")
    parser.add_argument("--tuple-record", type=Path, help="JSON tuple record with exact frozen Agent, Runtime, and VibeComfy commits")
    parser.add_argument("--session-root", type=Path, help="existing task-local installed canary/R8 session root")
    parser.add_argument("--output-dir", type=Path, help="task-local capture output directory (created only during capture)")
    args = parser.parse_args()
    if bool(args.prefix) != bool(args.tuple_record):
        parser.error("--prefix and --tuple-record must be supplied together")
    if args.capture and not all((args.prefix, args.tuple_record, args.session_root, args.output_dir)):
        parser.error("--capture requires --prefix, --tuple-record, --session-root, and --output-dir")
    if not args.dry_run and not args.capture:
        parser.error("select --dry-run or explicit --capture")
    try:
        tuple_value = _tuple(args.tuple_record)
        paths = _installed_paths(args.prefix, tuple_value) if args.prefix else {}
        baseline = _load_baseline()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"G0 capture planning failed: {exc}\n")
    py = paths.get("python", "<installed-prefix>/bin/python")
    cases = []
    for case_id, purpose, status, command_ids in CASES:
        commands = _commands(command_ids, baseline, py)
        cases.append({"case": case_id, "purpose": purpose, "prior_status": "UNCAPTURED", "coverage_note": status,
                      "commands": commands, "command_source_ids": list(command_ids)})
    result = {
        "schema": "h3-g0-installed-capture-plan/v1",
        "mode": "dry-run" if args.dry_run else "capture-request",
        "tuple": tuple_value,
        "tuple_status": "Runtime SHA required from new M-GPU note" if tuple_value["runtime"] == RUNTIME_PLACEHOLDER else "supplied; must match installed prefix",
        "installed_paths": paths or {"prefix": "<required-installed-prefix>", "python": py},
        "session_root": str(args.session_root) if args.session_root else "<same-installed-canary/R8-session-root>",
        "output_dir": str(args.output_dir) if args.output_dir else "<task-local-output-directory>",
        "source_evidence": {"g0_report": G0_REPORT, "baseline_commands": BASELINE_COMMANDS,
                            "capture_harness": "/home/user/astrid/orch/tasks/G0-FIXTURES/fixtures/capture_h3.py",
                            "comparator": "/home/user/astrid/orch/tasks/G0-FIXTURES/fixtures/comparator.py"},
        "cases": cases,
        "capture_status": "NOT RUN",
        "capture_blocker": "No reviewed installed-session command/input mapping exists for P3-P10 and P12; known G0 commands cover only partial P1/P2/P11. The script refuses to fabricate coverage or emit a complete h3-capture/v1 packet.",
        "media_policy": "dry-run does not stat, open, hash, or copy media; future private-media metadata may contain digests only",
        "dry_run_guarantees": ["no installed product imports", "no command execution", "no media/private input access", "no credentials or network access", "no files written", "stdout only"],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.capture:
        parser.exit(2, "G0 capture stopped before media or command access: reviewed P3-P10/P12 installed-session cases are not mapped.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
