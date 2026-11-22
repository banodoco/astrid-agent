"""Prevent execution host and package size regressions against a Git revision."""
from __future__ import annotations

import argparse
import ast
import io
import os
import subprocess
import tokenize
from bisect import bisect_right
from collections import Counter
from itertools import islice
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "astrid/core/execution/"
HOST = PACKAGE + "generic_host.py"


def _functions(source: str) -> tuple[dict[str, list[int]], Counter[str]]:
    tree = ast.parse(source)
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    lines = source.splitlines()
    token_lines = [token.start[0] for token in tokens]

    def span(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
        start = node.lineno
        if node.decorator_list:
            # The expression of a parenthesized decorator can begin after its @.
            first = node.decorator_list[0].lineno
            start = max(
                token.start[0] for token in tokens
                if token.type == tokenize.OP and token.string == "@"
                and token.start[1] == node.col_offset and token.start[0] <= first
            )
        end = node.end_lineno
        indent = len(lines[node.lineno - 1][:node.col_offset].expandtabs(8))
        for token in islice(tokens, bisect_right(token_lines, end), None):
            if token.type == tokenize.COMMENT:
                comment_indent = len(token.line[:token.start[1]].expandtabs(8))
                if comment_indent <= indent:
                    break
                end = token.start[0]
            elif token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
                break
        return end - start + 1
    functions: dict[str, list[int]] = {}
    methods: Counter[str] = Counter()

    class ClassBindings(ast.NodeVisitor):
        """Count class-level definitions and bindings, including in control flow."""

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            methods[node.name] += 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            # Nested classes have their own namespace.
            pass

        def _target(self, node: ast.AST) -> None:
            if isinstance(node, ast.Name):
                methods[node.id] += 1
            elif isinstance(node, (ast.Tuple, ast.List)):
                for item in node.elts:
                    self._target(item)

        def visit_Assign(self, node: ast.Assign) -> None:
            for target in node.targets:
                self._target(target)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if node.value is not None:
                self._target(node.target)

        def visit_AugAssign(self, node: ast.AugAssign) -> None:
            self._target(node.target)

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            if not self.scope and node.name == "GenericPackHost":
                bindings = ClassBindings()
                for statement in node.body:
                    bindings.visit(statement)
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            name = ".".join([*self.scope, node.name])
            functions.setdefault(name, []).append(span(node))
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

    Visitor().visit(tree)
    return functions, methods


Source = str | bytes


def _line_count(source: Source) -> int:
    """Count physical lines without decoding Git blobs."""
    return len(source.splitlines())


def _python_source(source: Source) -> str:
    """Decode Python source with the encoding declared by its source header."""
    if isinstance(source, str):
        return source
    encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
    return source.decode(encoding)


def compare(base: dict[str, Source], head: dict[str, Source]) -> list[str]:
    errors: list[str] = []
    old_host = _line_count(base.get(HOST, ""))
    new_host = _line_count(head.get(HOST, ""))
    if new_host > old_host:
        errors.append(f"{HOST}: lines grew from {old_host} to {new_host}")
    for path, source in sorted(head.items()):
        old_source = base.get(path, "")
        old_lines, new_lines = _line_count(old_source), _line_count(source)
        if new_lines > 1200 and old_lines <= 1200:
            errors.append(f"{path}: file newly exceeds 1200 lines ({new_lines})")
        if not path.endswith(".py"):
            continue
        old_functions, old_methods = _functions(_python_source(old_source))
        new_functions, new_methods = _functions(_python_source(source))
        if path == HOST:
            for name, count in sorted((new_methods - old_methods).items()):
                errors.append(f"{HOST}: GenericPackHost added method {name} ({count})")
        for name, sizes in sorted(new_functions.items()):
            previous = old_functions.get(name, [])
            for index, size in enumerate(sizes):
                old = previous[index] if index < len(previous) else 0
                if size > 200 and (old <= 200 or size > old):
                    errors.append(f"{path}: {name} grew from {old} to {size} lines (limit 200)")
    return errors


def _git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, stdout=subprocess.PIPE).stdout


def _git(*args: str) -> str:
    return _git_bytes(*args).decode("utf-8")


def sources(revision: str) -> dict[str, bytes]:
    entries = _git_bytes("ls-tree", "-r", "-z", revision, "--", PACKAGE).split(b"\0")
    result: dict[str, bytes] = {}
    for entry in entries:
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        _mode, object_type, object_id = metadata.split()
        if object_type != b"blob":
            continue
        path = os.fsdecode(raw_path)
        result[path] = _git_bytes("cat-file", "blob", object_id.decode("ascii"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="PR base commit")
    parser.add_argument("--head", default="HEAD", help="PR head commit")
    args = parser.parse_args()
    merge_base = _git("merge-base", args.base, args.head).strip()
    errors = compare(sources(merge_base), sources(args.head))
    for error in errors:
        print(f"EXECUTION SIZE ERROR: {error}")
    print(f"Execution size: merge-base={merge_base} violations={len(errors)}")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
