"""Read test definitions before imports or collection hooks can hide them.

This census counts source definitions, not parametrized cases. Pytest's item
census still counts every generated case and the execution evidence checks each
selected case. A declared whole-module lane omission is the only missing-source
exception. This module never imports test code to discover definitions.
"""

from __future__ import annotations

import ast
import fnmatch
import os
from pathlib import Path

import pytest


def matches(name, patterns):
    return any(name.startswith(pattern) or fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def qualified(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return qualified(node.value) + "." + node.attr
    return ""


class Source:
    def __init__(self, path, config):
        self.path = path
        self.tree = ast.parse(path.read_bytes(), filename=str(path))
        self.definitions = {}
        self.aliases = {}
        self.values = {}
        self.classes = {}
        self.integration_fixtures = set()
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.aliases[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self.aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        self.values[target.id] = node.value
            elif isinstance(node, ast.ClassDef):
                self.classes[node.name] = node
        declaration = self.values.get("INTEGRATION_FIXTURES")
        if declaration is not None:
            try:
                self.integration_fixtures = set(ast.literal_eval(declaration))
            except (ValueError, TypeError):
                raise pytest.UsageError(f"static inventory cannot resolve INTEGRATION_FIXTURES: {path}") from None
        self.visit(self.tree.body, (), self.marks(self.values.get("pytestmark")), config)

    def name(self, node):
        name = qualified(node)
        first, dot, rest = name.partition(".")
        return self.aliases.get(first, first) + dot + rest

    def marks(self, node, seen=frozenset()):
        if node is None:
            return set()
        if isinstance(node, ast.Name) and node.id in self.values and node.id not in seen:
            return self.marks(self.values[node.id], seen | {node.id})
        result = set()
        name = self.name(node)
        if name.startswith("pytest.mark."):
            result.add(name.split(".")[2])
        # Follow expressions/parameter data, never a function's body or strings.
        for child in ast.iter_child_nodes(node):
            result.update(self.marks(child, seen))
        return result

    def testcase(self, node, seen=frozenset()):
        if node.name in seen:
            return False
        return any(self.name(base) == "unittest.TestCase" or
                   (isinstance(base, ast.Name) and base.id in self.classes and
                    self.testcase(self.classes[base.id], seen | {node.name})) for base in node.bases)

    def lineage(self, node, seen=frozenset()):
        if node.name in seen:
            return
        for base in reversed(node.bases):
            if isinstance(base, ast.Name) and base.id in self.classes:
                yield from self.lineage(self.classes[base.id], seen | {node.name})
        yield node

    def visit(self, nodes, parents, inherited_marks, config, unittest=False):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                patterns = ["test"] if unittest else config.getini("python_functions")
                fixture = any(self.name(d.func if isinstance(d, ast.Call) else d)
                              in {"pytest.fixture", "pytest.yield_fixture"} for d in node.decorator_list)
                if matches(node.name, patterns) and not fixture:
                    marks = set(inherited_marks)
                    for decorator in node.decorator_list:
                        marks.update(self.marks(decorator))
                    self.definitions[(*parents, node.name)] = marks
            elif isinstance(node, ast.ClassDef):
                is_unittest = self.testcase(node)
                if not (is_unittest or matches(node.name, config.getini("python_classes"))):
                    continue
                marks = set(inherited_marks)
                ancestors = list(self.lineage(node))
                for ancestor in ancestors:
                    for decorator in ancestor.decorator_list:
                        marks.update(self.marks(decorator))
                    for statement in ancestor.body:
                        if isinstance(statement, ast.Assign) and any(
                                isinstance(t, ast.Name) and t.id == "pytestmark" for t in statement.targets):
                            marks.update(self.marks(statement.value))
                for ancestor in ancestors:
                    self.visit(ancestor.body, (*parents, node.name), marks, config, is_unittest)
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For)):
                # Definitions in a module/class control block still bind names
                # in that namespace. Do not descend into helper/test functions.
                self.visit(node.body, parents, inherited_marks, config, unittest)
                self.visit(getattr(node, "orelse", []), parents, inherited_marks, config, unittest)
                self.visit(getattr(node, "finalbody", []), parents, inherited_marks, config, unittest)
                for handler in getattr(node, "handlers", []):
                    self.visit(handler.body, parents, inherited_marks, config, unittest)


class StaticInventory:
    def __init__(self, config):
        self.config = config
        self.sources = {}
        self.expected = set()
        invocation = Path(config.invocation_params.dir)
        arguments = config.args or config.getini("testpaths") or [str(invocation)]
        for argument in arguments:
            path_arg, *selector = argument.split("::")
            root = (invocation / path_arg).resolve()
            for path in self.paths(root):
                if path not in self.sources:
                    self.sources[path] = Source(path, config)
                for names in self.sources[path].definitions:
                    if not selector or all(want.split("[", 1)[0] == got for want, got in zip(selector, names)):
                        self.expected.add((path, names))
        self.observed = set()
        self.errors = []

    def paths(self, root):
        if root.is_file():
            if root.suffix == ".py":
                yield root
            return
        for parent, directories, files in os.walk(root):
            directories[:] = sorted(name for name in directories if not name.startswith(".") and not any(
                fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(str(Path(parent) / name), pattern)
                for pattern in self.config.getini("norecursedirs")))
            for name in sorted(files):
                if any(fnmatch.fnmatch(name, pattern) for pattern in self.config.getini("python_files")):
                    yield Path(parent, name).resolve()

    def record(self, item):
        path = item.path.resolve()
        classes = tuple(parent.name for parent in item.listchain() if isinstance(parent, pytest.Class))
        names = (*classes, getattr(item, "originalname", None) or item.name.split("[", 1)[0])
        self.observed.add((path, names))
        source = self.sources.get(path)
        # The lane registry is protected. Its one source-backed dynamic marking
        # rule is INTEGRATION_FIXTURES, intersected with this case's fixtures.
        fixture_lane = bool(source and source.integration_fixtures & set(getattr(item, "fixturenames", ())))
        if source and names in source.definitions:
            from tests.lanes import LANES

            allowed = source.definitions[names] | ({"integration"} if fixture_lane else set())
            unexpected = {mark.name for mark in item.iter_markers()} & set(LANES) - allowed
            if unexpected:
                self.errors.append(f"{item.nodeid}: undeclared lane markers {sorted(unexpected)}")
        return fixture_lane

    def verify(self, ignored):
        ignored_paths = {(self.config.rootpath / path).resolve() for path in ignored}
        expected = {(path, names) for path, names in self.expected if path not in ignored_paths}
        missing = sorted(f"{path.relative_to(self.config.rootpath) if path.is_relative_to(self.config.rootpath) else path}"
                         f"::{'::'.join(names)}" for path, names in expected - self.observed)
        if missing or self.errors:
            raise pytest.UsageError(f"static collection inventory mismatch: missing={missing[:20]} "
                                    f"markers={self.errors[:20]}")
        return {"definitions": len(expected), "observed_definitions": len(self.observed),
                "omitted_definitions": len(self.expected) - len(expected)}
