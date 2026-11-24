#!/usr/bin/env python3
"""Release 15 update/rollback leg: published Release 14 -> real candidate -> forced rollback -> update again.

``run`` drives a REAL isolated install on a hosted Linux runner (see docs/qualification/update-rollback-leg.md);
``check`` re-reads the receipts it wrote and fails closed on anything missing. Evidence logic lives in
journey_ur_evidence, the installed-stack observations in journey_ur_stack, the steps in journey_ur_steps and the
receipts in journey_ur_receipts; this file is only the CLI entry the workflow calls.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__:
    from . import ci_journey as journey
    from .journey_ur_evidence import RESULT_NAME
    from .journey_ur_receipts import EVIDENCE_DIR, report_problems
    from .journey_ur_steps import execute
else:
    import ci_journey as journey
    from journey_ur_evidence import RESULT_NAME
    from journey_ur_receipts import EVIDENCE_DIR, report_problems
    from journey_ur_steps import execute


def run(args) -> int:
    run_root, root, data, _ = journey.run_scope(args.run_root, args.root, args.data_root, "fresh-install")
    evidence = run_root / "evidence" / EVIDENCE_DIR
    evidence.mkdir(parents=True, exist_ok=True)
    result = {"schema_version": 1, "receipt_kind": "r15-update-rollback", "status": "not-run",
              "final_cleanup": "not-run", "hook_present": None, **journey.runner_identity()}
    try:
        journey.assert_runner_identity(journey.runner_identity())
        with journey.isolated_environment(run_root, root, data):
            journey.assert_host_available("fresh-install", root, data)
            try:
                execute(args, root, data, evidence, result)
                result["status"] = "pass" if all(
                    (result.get(step) or {}).get("status") == "pass" for step in ("step1", "step2", "step3", "step4")
                ) else "fail"
            except Exception as exc:  # noqa: BLE001
                result.update(status="fail", error=f"{type(exc).__name__}: {exc}")
                print(f"Journey failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            finally:
                try:
                    if (root / "current").is_file():
                        _, _, installed = journey.installation(root, data)
                        journey.installed_cli(installed, ["install", "--uninstall", "--root", str(root),
                                                          "--data-root", str(data)], evidence, "final-uninstall.log",
                                              timeout=1800)
                    if journey.runtime_processes(data):
                        raise RuntimeError("Runtime processes remained after final uninstall")
                    journey.assert_host_available("cleanup", root, data)
                    result["final_cleanup"] = "pass"
                except Exception as exc:  # noqa: BLE001
                    result.update(final_cleanup="fail", cleanup_error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        result.update(status="fail", error=f"{type(exc).__name__}: {exc}")
    finally:
        for key in ("step1", "step2", "step3", "step4"):
            result[key] = (result.get(key) or {}).get("status", "not-run")
        journey.write_json(evidence / RESULT_NAME, result)
    return 1 if report_problems(evidence) else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("--candidate", type=Path, required=True)
    runner.add_argument("--app-checkout", type=Path, required=True)
    runner.add_argument("--r14-app-checkout", type=Path, required=True)
    runner.add_argument("--candidate-agent-checkout", type=Path, required=True)
    runner.add_argument("--media", type=Path, required=True)
    runner.add_argument("--run-root", type=Path, required=True)
    runner.add_argument("--root", type=Path, required=True)
    runner.add_argument("--data-root", type=Path, required=True)
    runner.add_argument("--shell", default="sh")
    checker = sub.add_parser("check")
    checker.add_argument("receipts", type=Path)
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args)
    problems = report_problems(args.receipts)
    print(f"update-rollback receipts: {'FAIL' if problems else 'pass'} ({len(problems)} problems)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
