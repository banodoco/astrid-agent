"""Guard user-facing CLI guidance against retired routes and fixture identities."""

from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUIDANCE_NAMES = {"hint", "next", "next_action", "next_actions", "recovery_command"}
FORBIDDEN = (
    (re.compile(r"astrid agent", re.IGNORECASE), "astrid agent"),
    (re.compile(r"executors run", re.IGNORECASE), "executors run"),
    (re.compile(r"references/editing\.md", re.IGNORECASE), "references/editing.md"),
    (re.compile(r"skill\.scripts", re.IGNORECASE), "skill.scripts"),
    (re.compile(r"c30\.cover", re.IGNORECASE), "c30.cover"),
    (re.compile(r"Astrid"), "Astrid"),
    (re.compile(r"\bTL\b"), "literal TL"),
)


def _target_names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _guidance_strings(node: ast.AST, assignments: dict[str, list[ast.AST]], seen: set[str] | None = None) -> list[str]:
    seen = set() if seen is None else seen
    values: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            values.append(child.value)
        elif isinstance(child, ast.Name) and child.id not in seen:
            seen.add(child.id)
            for assigned in assignments.get(child.id, ()):
                values.extend(_simple_string_literals(assigned))
    return values


def _simple_string_literals(node: ast.AST) -> list[str]:
    """Resolve a constant/string expression without following runtime objects."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _simple_string_literals(node.left) + _simple_string_literals(node.right)
    return []


def _module_guidance(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignments: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _target_names(target):
                    assignments.setdefault(name, []).append(node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            for name in _target_names(node.target):
                assignments.setdefault(name, []).append(node.value)

    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in GUIDANCE_NAMES:
                    findings.extend((node.lineno, value) for value in _guidance_strings(keyword.value, assignments))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in GUIDANCE_NAMES:
                    findings.extend((node.lineno, item) for item in _guidance_strings(value, assignments))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = set().union(*(_target_names(target) for target in targets))
            if names & GUIDANCE_NAMES:
                findings.extend((node.lineno, value) for value in _guidance_strings(node.value, assignments))

        # Timeline navigation output is commonly assembled with string
        # concatenation or f-strings, so inspect every literal carrying its
        # explicit `next:` marker as well as named fields above.
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search(r"\bnext\s*:", node.value, re.IGNORECASE):
            findings.append((node.lineno, node.value))

    return findings


def test_cli_guidance_has_no_retired_routes_or_fixture_addresses() -> None:
    failures: list[str] = []
    for path in sorted((ROOT / "astrid").rglob("*.py")):
        try:
            guidance = _module_guidance(path)
        except SyntaxError as exc:
            failures.append(f"{path.relative_to(ROOT)}:{exc.lineno}: cannot parse source: {exc.msg}")
            continue
        for line, text in guidance:
            for pattern, label in FORBIDDEN:
                if pattern.search(text):
                    failures.append(f"{path.relative_to(ROOT)}:{line}: {label!r} in guidance string {text!r}")

    assert not failures, "\n".join(failures)
