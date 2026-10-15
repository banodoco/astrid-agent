#!/usr/bin/env python3
"""Generate a deterministic inventory of the public timeline feature surface."""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


DEFAULT_ROOT = Path(__file__).resolve().parents[2]


def _json_value(value: Any) -> Any:
    """Return only stable, JSON-safe argparse metadata."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(value[key]) for key in sorted(value, key=str)}
    if value is argparse.SUPPRESS:
        return "SUPPRESS"
    if callable(value):
        return getattr(value, "__qualname__", type(value).__name__)
    return type(value).__name__


def _action_record(action: argparse.Action) -> dict[str, Any]:
    result: dict[str, Any] = {
        "options": sorted(action.option_strings),
        "dest": action.dest,
        "action": type(action).__name__,
        "required": bool(action.required),
        "nargs": action.nargs,
        "default": _json_value(action.default),
    }
    if action.choices is not None:
        result["choices"] = sorted((_json_value(value) for value in action.choices), key=lambda value: json.dumps(value, sort_keys=True))
    if action.type is not None:
        result["type"] = getattr(action.type, "__qualname__", type(action.type).__name__)
    if action.metavar is not None:
        result["metavar"] = _json_value(action.metavar)
    return result


def _parser_inventory(root: Path) -> dict[str, Any]:
    from astrid.packs.timeline.cli import build_parser

    commands: dict[str, Any] = {}

    def walk(current: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        flags: list[dict[str, Any]] = []
        positionals: list[dict[str, Any]] = []
        nested: list[argparse._SubParsersAction] = []
        for action in current._actions:
            if isinstance(action, argparse._HelpAction):
                continue
            if isinstance(action, argparse._SubParsersAction):
                nested.append(action)
            elif action.option_strings:
                flags.append(_action_record(action))
            else:
                positionals.append(_action_record(action))
        commands[".".join(path)] = {
            "flags": sorted(flags, key=lambda item: (item["dest"], item["options"])),
            "positionals": sorted(positionals, key=lambda item: item["dest"]),
        }
        for subparsers in nested:
            for name, child in sorted(subparsers.choices.items()):
                walk(child, (*path, name))

    parser = build_parser(client=None)
    walk(parser, ("timelines",))
    return commands


def _public_sdk() -> dict[str, Any]:
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.sdk.timeline.look import Look
    from astrid.sdk.timeline.model import Clip, Cut, StepSequence, Voice

    result: dict[str, Any] = {}
    for cls in (Checkout, Clip, Cut, Voice, StepSequence, Look):
        members: dict[str, Any] = {}
        for name, value in inspect.getmembers(cls):
            if name.startswith("_"):
                continue
            if isinstance(value, property):
                members[name] = {"kind": "property"}
            elif callable(value):
                try:
                    signature = str(inspect.signature(value))
                except (TypeError, ValueError):
                    signature = "unavailable"
                members[name] = {"kind": "callable", "signature": signature}
        result[cls.__name__] = {
            "signature": str(inspect.signature(cls)),
            "members": members,
        }
    return result


_CODE_PREFIX = re.compile(r"^\s*([A-Za-z][A-Za-z0-9-]*)(?:\s+|:)")


def _finding_codes(root: Path) -> dict[str, list[str]]:
    """Read declared lint codes and stable uppercase prefixes from check output."""
    from astrid.packs.rendering.actions.timeline_visualize.layers import base

    lint_codes: set[str] = set()
    for check in base.checks().values():
        lint_codes.update(str(code) for code in check.codes)

    # These output strings are the SDK check() finding vocabulary. Read the
    # formatted labels that feed its blocking/problems report fields, plus the
    # dedicated coverage and sprite findings below.
    check_codes: set[str] = set()
    check_path = root / "astrid/sdk/timeline/check.py"
    tree = ast.parse(check_path.read_text(encoding="utf-8"), filename=str(check_path))
    check_methods = {"check", "picture_gaps", "empty_cuts", "dips", "_sprite_problems"}
    checkout = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_CheckMixin"
    )
    for method in checkout.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or method.name not in check_methods:
            continue
        for node in ast.walk(method):
            if method.name == "check" and isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {target.id for target in targets if isinstance(target, ast.Name)}
                if names & {"blocking", "problems"}:
                    check_codes.update(_formatted_prefixes(node.value))
            elif method.name == "check" and isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
                if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) \
                        and call.func.value.id in {"blocking", "problems"} and call.func.attr == "append":
                    check_codes.update(_formatted_prefixes(call))
            elif method.name != "check" and isinstance(node, ast.JoinedStr):
                prefix = "".join(
                    part.value for part in node.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
                match = _CODE_PREFIX.match(prefix)
                if match:
                    check_codes.add(match.group(1))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_spans_of":
                check_codes.update(
                    arg.value for arg in node.args
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value in {"GAP", "DIP", "EMPTY"}
                )

    # LOOK-UNRENDERED is produced by Look and included in CheckReport.problems.
    look_path = root / "astrid/sdk/timeline/look.py"
    look_tree = ast.parse(look_path.read_text(encoding="utf-8"), filename=str(look_path))
    for node in ast.walk(look_tree):
        if isinstance(node, ast.JoinedStr):
            prefix = "".join(
                part.value for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            match = _CODE_PREFIX.match(prefix)
            if match:
                check_codes.add(match.group(1))

    # V3 Check codes live in admitted descriptors, not in the host's Python
    # source. Read only descriptors named by a pack manifest and admitted as
    # file resources; inventory generation must not import contribution code.
    packs_root = root / "astrid/packs"
    for manifest_path in sorted(packs_root.glob("*/pack.yaml")):
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        timeline = manifest.get("extensions", {}).get("timeline", {})
        declared = timeline.get("checks", ()) if isinstance(timeline, dict) else ()
        admitted_files = {
            row.get("path") for row in manifest.get("resources", ())
            if isinstance(row, dict) and row.get("kind") in {"support", "implementation"}
        }
        for descriptor_path in declared:
            if not isinstance(descriptor_path, str) or descriptor_path not in admitted_files:
                continue
            path = (manifest_path.parent / descriptor_path).resolve()
            if not path.is_relative_to(manifest_path.parent.resolve()) or not path.is_file():
                continue
            descriptor = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            finding = descriptor.get("finding", {}) if isinstance(descriptor, dict) else {}
            code = finding.get("code") if isinstance(finding, dict) else None
            if isinstance(code, str) and _CODE_PREFIX.fullmatch(code + " "):
                check_codes.add(code)
    return {"check": sorted(check_codes), "lint": sorted(lint_codes)}


def _formatted_prefixes(node: ast.AST) -> set[str]:
    result: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.JoinedStr):
            continue
        prefix = "".join(
            part.value for part in child.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
        match = _CODE_PREFIX.match(prefix)
        if match:
            result.add(match.group(1))
    return result


def _visualize(root: Path, commands: dict[str, Any]) -> dict[str, Any]:
    from astrid.packs.rendering.actions.timeline_visualize.layers import base
    from astrid.packs.rendering.actions.timeline_visualize.motion.window import ALL_PRESETS, PRESETS

    visualize_flags = commands["timelines.visualize"]["flags"]
    view_action = next(flag for flag in visualize_flags if "--view" in flag["options"])
    layers = base.discover(refresh=True)
    return {
        "presets": sorted(set(ALL_PRESETS) | set(PRESETS)),
        "views": sorted(view_action.get("choices", [])),
        "layer_views": sorted({view for layer in layers.values() for view in layer.views}),
        "layers": sorted(layers),
    }


def _assert_cached_modules_belong_to(root: Path) -> None:
    foreign: list[tuple[str, Path]] = []
    for name, module in tuple(sys.modules.items()):
        if name != "astrid" and not name.startswith("astrid."):
            continue
        origin = getattr(module, "__file__", None)
        if origin is None:
            origin = getattr(getattr(module, "__spec__", None), "origin", None)
        if not origin or origin in {"built-in", "frozen"}:
            continue
        module_path = Path(origin).resolve()
        if not module_path.is_relative_to(root):
            foreign.append((name, module_path))
    if foreign:
        examples = "; ".join(f"{name}={path}" for name, path in foreign[:3])
        raise RuntimeError(
            f"cannot inventory {root}: cached Astrid modules came from another source root ({examples}); "
            "run each source root in a fresh Python process"
        )


def build_inventory(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    root = root.resolve()
    _assert_cached_modules_belong_to(root)
    if str(root) in sys.path:
        sys.path.remove(str(root))
    sys.path.insert(0, str(root))
    commands = _parser_inventory(root)
    render = commands["timelines.render"]
    return {
        "schema": 1,
        "commands": commands,
        "sdk": _public_sdk(),
        "finding_codes": _finding_codes(root),
        "visualize": _visualize(root, commands),
        "render": render,
    }


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Astrid checkout whose source defines the inventory.")
    cli.add_argument("--output", type=Path, default=None, help="Write JSON here (default: tests/parity/inventory.json under --root).")
    args = cli.parse_args(argv)
    root = args.root.resolve()
    output = (args.output or root / "tests/parity/inventory.json").resolve()
    data = json.dumps(build_inventory(root), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(data, encoding="utf-8", newline="\n")
    print(f"wrote {output} ({len(data.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
