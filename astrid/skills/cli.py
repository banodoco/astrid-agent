"""CLI for ``python3 -m astrid skills``."""

from __future__ import annotations

import argparse
import json
from typing import Any

from astrid.core.cli_choices import RecoverableArgumentParser, add_choice_arg
from astrid.core.contracts.errors import AstridError

from . import check as check_fn
from . import doctor as doctor_fn
from . import install as install_fn
from . import list_state
from . import sync as sync_fn
from . import uninstall as uninstall_fn
from .harnesses import ADAPTERS

HARNESS_CHOICES = ("claude", "codex", "hermes", "all")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return int(handler(args))
    except (KeyError, FileExistsError, ValueError, RuntimeError) as exc:
        raise AstridError(str(exc), recovery_command="check --help for usage and try again") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = RecoverableArgumentParser(
        prog="python3 -m astrid skills",
        description="Install the Astrid skills layer into supported agent harnesses (claude, codex, hermes).",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", help="List installable packs and per-harness install state.")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(handler=_cmd_list)

    install_parser = subparsers.add_parser("install", help="Install pack(s) into harness(es).")
    install_parser.add_argument("pack", nargs="?", help="Pack id, or omit with --all.")
    install_parser.add_argument("--all", action="store_true", help="Install every available pack.")
    add_choice_arg(install_parser, "--harness", values=HARNESS_CHOICES, action="append", help="One or more harnesses (default: all).")
    add_choice_arg(install_parser, "--mechanism", values=("symlink", "external-dir"), default="symlink", help="Hermes-only mechanism (default: symlink).")
    install_parser.add_argument("--force", action="store_true", help="Overwrite existing non-symlink targets.")
    install_parser.add_argument("--dry-run", action="store_true")
    install_parser.add_argument("--json", action="store_true")
    install_parser.set_defaults(handler=_cmd_install)

    uninstall_parser = subparsers.add_parser("uninstall", help="Remove pack(s) from harness(es).")
    uninstall_parser.add_argument("pack", nargs="?")
    uninstall_parser.add_argument("--all", action="store_true")
    add_choice_arg(uninstall_parser, "--harness", values=HARNESS_CHOICES, action="append")
    uninstall_parser.add_argument("--dry-run", action="store_true")
    uninstall_parser.add_argument("--json", action="store_true")
    uninstall_parser.set_defaults(handler=_cmd_uninstall)

    sync_parser = subparsers.add_parser(
        "sync",
        help="Link the gateway skill + regenerate its pack registry into every detected harness; prune orphans.",
    )
    add_choice_arg(sync_parser, "--mechanism", values=("symlink", "external-dir"), default="symlink")
    sync_parser.add_argument(
        "--deep",
        action="store_true",
        help="Also link each pack's skill as astrid-<pack> (default: gateway only; alias: --all).",
    )
    sync_parser.add_argument("--all", action="store_true", dest="deep", help="Alias for --deep.")
    sync_parser.add_argument(
        "--check",
        action="store_true",
        help="Dry-run drift report only; make no changes and exit non-zero if drift exists.",
    )
    add_choice_arg(sync_parser, "--harness", values=HARNESS_CHOICES, action="append")
    sync_parser.add_argument("--force", action="store_true")
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--json", action="store_true")
    sync_parser.set_defaults(handler=_cmd_sync)

    doctor_parser = subparsers.add_parser("doctor", help="Verify symlinks, fenced block, frontmatter, and lint.")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.add_argument(
        "--heal",
        action="store_true",
        help="Rewrite the state file from filesystem reality where they disagree.",
    )
    doctor_parser.set_defaults(handler=_cmd_doctor)

    return parser


def _cmd_list(args: argparse.Namespace) -> int:
    report = list_state()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    detected = ", ".join(report["detected"]) or "(none)"
    print(f"detected harnesses: {detected}")
    for item in report["packs"]:
        states = []
        for harness in ADAPTERS:
            entry = item["harnesses"][harness]
            if entry.get("drift"):
                mark = "~"
            elif entry["installed"]:
                mark = "x"
            elif entry["detected"]:
                mark = "-"
            else:
                mark = " "
            states.append(f"{harness}[{mark}]")
        print(f"  {item['pack_id']:<12} {' '.join(states)}  {item['short_description']}")
    return 0


def _resolve_packs(args: argparse.Namespace) -> list[str] | None:
    if getattr(args, "all", False):
        return None
    if not args.pack:
        raise AstridError("specify a pack id or pass --all", recovery_command="specify a pack id or pass --all to install/uninstall all packs")
    return [args.pack]


def _resolve_harness_arg(args: argparse.Namespace) -> list[str] | None:
    raw = getattr(args, "harness", None) or []
    if not raw or "all" in raw:
        return None
    return list(raw)


def _cmd_install(args: argparse.Namespace) -> int:
    pack_ids = _resolve_packs(args)
    harness_names = _resolve_harness_arg(args)
    report = install_fn(
        pack_ids=pack_ids,
        harness_names=harness_names,
        mechanism=args.mechanism,
        force=args.force,
        dry_run=args.dry_run,
    )
    return _emit_report(report, args)


def _cmd_uninstall(args: argparse.Namespace) -> int:
    pack_ids = _resolve_packs(args)
    harness_names = _resolve_harness_arg(args)
    report = uninstall_fn(pack_ids=pack_ids, harness_names=harness_names, dry_run=args.dry_run)
    return _emit_report(report, args)


def _cmd_sync(args: argparse.Namespace) -> int:
    if getattr(args, "check", False):
        report = check_fn(deep=args.deep)
        return _emit_check_report(report, args)
    report = sync_fn(
        harness_names=_resolve_harness_arg(args),
        mechanism=args.mechanism,
        force=args.force,
        deep=args.deep,
        dry_run=args.dry_run,
    )
    rc = _emit_report(report, args)
    broken = report.get("broken_links", [])
    if not args.json:
        _print_link_summary(report.get("links", {}))
        _print_warnings(report.get("warnings", []))
        for entry in broken:
            _print_broken_link(entry)
    # A sync that leaves a broken route in a view is not a success.
    return 1 if broken else rc


def _print_link_summary(links: dict) -> None:
    for harness, summary in links.items():
        print(
            f"[links] {harness}: {summary['checked']} links in {summary['files']} files, "
            f"{summary['broken']} broken"
        )


def _print_broken_link(entry: dict) -> None:
    print(
        f"  [broken link] {entry['harness']:<7} {entry['file']}:{entry['line']} "
        f"({entry['target']}) -> missing {entry['missing']}"
    )


def _print_warnings(warnings: list) -> None:
    for warning in warnings:
        where = " ".join(part for part in (warning.get("harness"), warning.get("pack")) if part)
        print(f"  [warn] {where + ': ' if where else ''}{warning['message']}")


def _emit_check_report(report: dict[str, Any], args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        detected = ", ".join(report["detected"]) or "(none)"
        print(f"detected harnesses: {detected}")
        if report["registry_stale"]:
            print("  [drift] registry block in _core/docs/creative-work/references/packs.md is out of date")
        for entry in report["missing"]:
            print(f"  [drift] {entry['harness']:<7} {entry['pack']:<14} not linked")
        for entry in report["stale_links"]:
            print(f"  [drift] {entry['harness']:<7} {entry['link']:<14} stale link (pack gone)")
        _print_link_summary(report.get("links", {}))
        for entry in report.get("broken_links", []):
            _print_broken_link(entry)
        if not report["has_drift"]:
            print("clean: registry current and all links present")
    return 1 if report["has_drift"] else 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    report = doctor_fn(heal=getattr(args, "heal", False))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        detected = ", ".join(report["detected"]) or "(none)"
        print(f"detected: {detected}")
        for finding in report["lint"]:
            print(f"lint  {finding['pack']}: {finding['finding']}")
        for drift in report.get("drift", []):
            print(f"  [drift] {drift['harness']:<7} {drift['pack']:<12} {drift['message']}")
        for healed in report.get("healed", []):
            print(f"  [heal]  {healed['harness']:<7} {healed['pack']:<12} {healed['action']}")
        for entry in report["results"]:
            mark = "ok" if entry["ok"] else "FAIL"
            print(f"  [{mark}] {entry['harness']:<7} {entry['pack']:<12} {entry['message']}")
        _print_link_summary(report.get("links", {}))
        for entry in report.get("broken_links", []):
            _print_broken_link(entry)
        _print_warnings(report.get("warnings", []))
    failures = sum(1 for r in report["results"] if not r["ok"]) + len(report["lint"])
    failures += len(report.get("broken_links", []))
    # Drift not yet healed counts as a soft failure; healed drift does not.
    if not getattr(args, "heal", False):
        failures += len(report.get("drift", []))
    # Warnings (optional packs not linked, skipped external discovery) never fail.
    return 0 if failures == 0 else 1


def _emit_report(report: dict[str, Any], args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    registry = report.get("registry")
    if registry is not None:
        state = "updated" if registry["changed"] else "ok"
        print(f"[registry] _core/docs/creative-work/references/packs.md pack block {state}")
    if not report["actions"]:
        if registry is None:
            print("no detected harnesses; nothing to do")
        return 0
    for action in report["actions"]:
        print(f"[{action['harness']}]")
        for step in action["steps"]:
            print(f"  {step['description']}")
    return 0


__all__ = ["build_parser", "main"]


if __name__ == "__main__":  # pragma: no cover - exercised by the subprocess test
    raise SystemExit(main())
