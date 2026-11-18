"""Compare diagnostics with the PR merge base, without a mutable count allowance.

The old count comparators remain available for debt reports. CI uses this gate:
both trees are scanned by the same installed tool and unchanged configuration.
Line numbers locate symbols but are not identities; duplicate findings count.
"""

from __future__ import annotations

import argparse
import ast
import collections
import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import tokenize
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

Identity = tuple[str, str, str, str]
Statement = tuple[int, int, int, int, str]
MYPY_ERROR = re.compile(r"^(.*?):(\d+):(?:(\d+):)? error: (.*?)\s+\[(.*?)\]$")
SUPPRESSION = re.compile(r"\bnoqa\b|\btype\s*:\s*ignore\b|\bmypy\s*:", re.IGNORECASE)
TOOL_CONFIGS = {"ruff.toml", ".ruff.toml", "pytest.ini", ".pytest.ini", "tox.ini",
                "setup.cfg", "mypy.ini", ".mypy.ini", "pyproject.toml", "pytest.toml", ".pytest.toml"}
TOOL_MODULES = {"ruff", "mypy", "pytest", "_pytest"}


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


@contextmanager
def base_checkout(root: Path, ref: str) -> Iterator[tuple[str, Path]]:
    """Keep the base outside the candidate's lint tree; never install base code."""
    revision = git(root, "merge-base", "HEAD", ref)
    with tempfile.TemporaryDirectory(prefix="astrid-gate-base-") as directory:
        checkout = Path(directory) / "source"
        git(root, "worktree", "add", "--detach", str(checkout), revision)
        try:
            yield revision, checkout
        finally:
            git(root, "worktree", "remove", "--force", str(checkout))


def symbol_ranges(source: str) -> list[tuple[int, int, str]]:
    """Parse a file once, retaining the source spans of its qualified scopes."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    scopes: list[tuple[int, int, str]] = []

    def visit(node: ast.AST, parents: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            scope = parents
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([child.lineno, *[d.lineno for d in child.decorator_list]])
                scope = (*parents, child.name)
                scopes.append((start, child.end_lineno or child.lineno, ".".join(scope)))
            visit(child, scope)

    visit(tree, ())
    return scopes


def symbol_at(scopes: list[tuple[int, int, str]], line: int) -> str:
    return max((name for start, end, name in scopes if start <= line <= end),
               key=lambda name: len(name.split(".")), default="<module>")


def statement_ranges(source: str) -> list[Statement]:
    """Distinguish equal-message findings inside one symbol, without line IDs."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    statements = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        header = copy.copy(node)
        # An error on a compound statement's header is not re-keyed when an
        # unrelated statement in its body changes.
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            if hasattr(header, field):
                setattr(header, field, [])
        digest = hashlib.sha256(ast.dump(header, include_attributes=False).encode()).hexdigest()[:16]
        statements.append((node.lineno, node.col_offset + 1, node.end_lineno or node.lineno,
                           (node.end_col_offset or 0) + 1, f"{type(node).__name__}:{digest}"))
    return statements


def statement_at(statements: list[Statement], line: int, column: int) -> str:
    candidates = [entry for entry in statements if entry[:2] <= (line, column) <= entry[2:4]]
    if not candidates:
        candidates = [entry for entry in statements if entry[0] <= line <= entry[2]]
    return min(candidates, key=lambda row: (row[2] - row[0], row[3] - row[1]),
               default=(0, 0, 0, 0, "<module>"))[4]


def identities(findings: list[dict[str, Any]], root: Path) -> collections.Counter[Identity]:
    result: collections.Counter[Identity] = collections.Counter()
    scopes: dict[str, list[tuple[int, int, str]]] = {}
    statements: dict[str, list[Statement]] = {}
    for finding in findings:
        path = Path(finding["path"])
        if path.is_absolute():
            try:
                path = path.relative_to(root)
            except ValueError as exc:
                raise RuntimeError(f"tool reported a diagnostic outside the source tree: {path}") from exc
        key = path.as_posix()
        if key not in scopes:
            source = (root / path).read_text(encoding="utf-8")
            scopes[key] = symbol_ranges(source)
            statements[key] = statement_ranges(source)
        message = " ".join(str(finding["message"]).replace(str(root) + os.sep, "").split())
        owner = symbol_at(scopes[key], finding["line"])
        site = statement_at(statements[key], finding["line"], finding.get("column", 0))
        result[(key, finding["code"], message, f"{owner} [{site}]")] += 1
    return result


def diagnostics(tool: str, root: Path) -> list[dict[str, Any]]:
    if tool == "ruff":
        # Respect Ruff's configured scope, but not Git's ignore list. Intersect
        # discovery with tracked paths so sibling dependency checkouts cannot
        # add diagnostics that the clean merge-base worktree does not contain.
        discovery = subprocess.run(
            [sys.executable, "-I", "-m", "ruff", "check", ".", "--no-respect-gitignore", "--show-files"],
            cwd=root, capture_output=True, text=True, check=False,
        )
        if discovery.returncode:
            raise RuntimeError(f"Ruff file discovery failed: {discovery.stdout}{discovery.stderr}")
        tracked = {str(root / path) for path in git(root, "ls-files", "-z", "--", "*.py").split("\0") if path}
        paths = sorted(tracked & set(discovery.stdout.splitlines()))
        rows = []
        for offset in range(0, len(paths), 100):
            command = [sys.executable, "-I", "-m", "ruff", "check", "--no-respect-gitignore", "--no-cache",
                       "--output-format", "json", *paths[offset:offset + 100]]
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
            if result.returncode not in (0, 1):
                raise RuntimeError(f"Ruff failed to execute: {result.stdout}{result.stderr}")
            rows.extend(json.loads(result.stdout))
        return [{"path": row["filename"], "code": row["code"], "message": row["message"],
                 "line": row["location"]["row"], "column": row["location"]["column"]} for row in rows]
    else:
        command = [
            sys.executable, "-I", "-m", "mypy", "astrid", "scripts/reshape",
            "--hide-error-context", "--no-color-output", "--no-pretty", "--show-error-codes",
            "--show-column-numbers",
            "--no-incremental", "--cache-dir", os.devnull,
        ]
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"{tool} failed to execute (exit {result.returncode}): {result.stdout}{result.stderr}")
    findings = []
    for line in (result.stdout + result.stderr).splitlines():
        match = MYPY_ERROR.match(line)
        if match:
            findings.append({"path": match[1], "line": int(match[2]), "column": int(match[3] or 0),
                             "message": match[4], "code": match[5]})
    if result.returncode == 1 and not findings:
        raise RuntimeError(f"{tool} failed without diagnostics: {result.stdout}{result.stderr}")
    return findings


def branch_definitions(body: list[ast.stmt]) -> dict[str, ast.AST]:
    """Find definitions bound by a branch without entering their nested scopes."""
    result: dict[str, ast.AST] = {}
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result[node.name] = node
        else:
            pending.extend(ast.iter_child_nodes(node))
    return result


def suppression_identities(root: Path) -> collections.Counter[Identity]:
    findings = []
    for name in git(root, "ls-files", "--", "*.py", "*.pyi").splitlines():
        path = root / name
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        if path.suffix == ".pyi":
            # A new or changed stub can replace the implementation mypy sees,
            # even without an explicit ignore directive inside the interface.
            findings.append({"path": name, "code": "stub-interface", "line": 1,
                             "message": hashlib.sha256(source.encode()).hexdigest()})
        try:
            tree = ast.parse(source)
        except SyntaxError:
            tree = ast.Module(body=[], type_ignores=[])
        aliases = {"no_type_check"}
        type_checking = {"TYPE_CHECKING"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in {"typing", "typing_extensions"}:
                aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "no_type_check")
                type_checking.update(alias.asname or alias.name for alias in node.names if alias.name == "TYPE_CHECKING")
        for node in ast.walk(tree):
            if ((isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in aliases)
                    or (isinstance(node, ast.Attribute) and node.attr == "no_type_check")):
                findings.append({"path": name, "code": "suppression", "message": "no_type_check",
                                 "line": node.lineno, "column": node.col_offset + 1})
            if isinstance(node, ast.If) and any(
                (isinstance(test, ast.Name) and test.id in type_checking)
                or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")
                for test in ast.walk(node.test)
            ):
                typed = branch_definitions(node.body)
                runtime = branch_definitions(node.orelse)
                for duplicate in sorted(typed.keys() & runtime.keys()):
                    findings.append({"path": name, "code": "suppression",
                                     "message": f"TYPE_CHECKING replaces runtime definition: {duplicate}",
                                     "line": node.lineno, "column": node.col_offset + 1})
        try:
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.COMMENT and SUPPRESSION.search(token.string):
                    # The attached statement matters: moving an old directive
                    # onto a different offending line must not buy an allowance.
                    attached = token.line[:token.start[1]].strip()
                    findings.append({"path": name, "code": "suppression",
                                     "message": f"{attached} {token.string}", "line": token.start[0]})
        except (tokenize.TokenError, IndentationError):
            # Invalid syntax is a diagnostic in both lint/type scans, never an allowance.
            continue
    return identities(findings, root)


def protected_changes(base: Path, candidate: Path) -> list[str]:
    """A task PR cannot raise its own allowance or change what the tools inspect."""
    errors = []
    relative = Path("scripts/reshape/baselines")
    paths = {p.relative_to(base) for p in (base / relative).rglob("*") if p.is_file()}
    paths |= {p.relative_to(candidate) for p in (candidate / relative).rglob("*") if p.is_file()}
    for path in sorted(paths):
        before, after = base / path, candidate / path
        if not before.is_file() or not after.is_file() or before.read_bytes() != after.read_bytes():
            errors.append(f"baseline edited in task PR: {path.as_posix()}")
    configs: set[Path] = set()
    tool_paths: set[Path] = set()
    for root in (base, candidate):
        tracked = [Path(path) for path in git(root, "ls-files", "-z").split("\0") if path]
        configs.update(path for path in tracked if path.name in TOOL_CONFIGS)
        tool_paths.update(path for path in tracked if path.parts[0] in TOOL_MODULES
                          or (len(path.parts) == 1 and path.suffix in {".py", ".pyi"} and path.stem in TOOL_MODULES))
    for path in sorted(tool_paths):
        before, after = base / path, candidate / path
        if (not before.is_file() or not after.is_file() or before.is_symlink() or after.is_symlink()
                or before.read_bytes() != after.read_bytes()):
            errors.append(f"gate tool package changed in task PR: {path.as_posix()}")
    for path in sorted(configs):
        if path == Path("pyproject.toml"):
            continue  # The root's non-tool package metadata is not a lint allowance.
        before, after = base / path, candidate / path
        if (not before.is_file() or not after.is_file() or before.is_symlink() or after.is_symlink()
                or before.read_bytes() != after.read_bytes()):
            errors.append(f"protected tool configuration changed in task PR: {path.as_posix()}")
    before_config = tomllib.loads((base / "pyproject.toml").read_text())["tool"]
    after_config = tomllib.loads((candidate / "pyproject.toml").read_text())["tool"]
    for tool in ("ruff", "mypy"):
        if before_config.get(tool) != after_config.get(tool):
            errors.append(f"protected tool.{tool} configuration changed in task PR")
    before_pytest = before_config.get("pytest", {}).get("ini_options", {})
    after_pytest = after_config.get("pytest", {}).get("ini_options", {})
    for key in ("norecursedirs", "python_files", "python_classes", "python_functions", "testpaths", "addopts"):
        if before_pytest.get(key) != after_pytest.get(key):
            errors.append(f"protected pytest collection setting changed in task PR: {key}")
    for identity, count in sorted((suppression_identities(candidate) - suppression_identities(base)).items()):
        errors.append(f"new suppression ({count}): {' | '.join(identity)}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=("ruff", "mypy", "controls"))
    parser.add_argument("--base-ref", default=os.environ.get("ASTRID_GATE_BASE"))
    args = parser.parse_args(argv)
    if not args.base_ref or re.fullmatch("0+", args.base_ref):
        parser.error("--base-ref (or ASTRID_GATE_BASE) must name the trusted comparison base")
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel"))
    try:
        with base_checkout(root, args.base_ref) as (revision, base):
            print(f"Identity gate: tool={args.tool} base={revision} candidate={git(root, 'rev-parse', 'HEAD')}")
            errors = protected_changes(base, root)
            if args.tool != "controls":
                before = identities(diagnostics(args.tool, base), base)
                after = identities(diagnostics(args.tool, root), root)
                if before and not after:
                    errors.append(f"{args.tool} findings collapsed from a nonzero base to zero; review the tool scope")
                for identity, count in sorted((after - before).items()):
                    errors.append(f"new {args.tool} finding ({count}): {' | '.join(identity)}")
                print(f"{args.tool}: base={sum(before.values())} current={sum(after.values())} "
                      f"new={sum((after - before).values())} resolved={sum((before - after).values())}")
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return int(bool(errors))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Identity gate failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
