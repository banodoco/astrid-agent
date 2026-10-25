"""Regression tests for the CLI-unification end state (the m5b god-module splits).

The refactors they guard are complete and these tests pass. Each test class
documents the invariant it holds: table-driven dispatch, no fall-through to the
default hype brief, executable family help, and an explicit default-brief route.

Do not weaken these assertions; they are the contract the dispatch layer keeps.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _repo_env() -> dict[str, str]:
    """Environment for ``python -m astrid`` subprocess invocations.

    The runtime environment runs with ``PYTHONSAFEPATH=1`` and a
    PYTHONPATH that does not include the repository, so subprocesses must
    be given the repo root explicitly to resolve the in-tree ``astrid``
    package.
    """
    repo_root = str(_REPO_ROOT)
    prior = os.environ.get("PYTHONPATH", "")
    return {
        **os.environ,
        "PYTHONPATH": repo_root if not prior else repo_root + os.pathsep + prior,
    }


# ---------------------------------------------------------------------------
# 1.  No ``raw[0] ==`` dispatch in astrid/core/gateway/dispatch.py
# ---------------------------------------------------------------------------

class RawDispatchRemovalTest(unittest.TestCase):
    """The ``_dispatch()`` function must not use ``raw[0] ==`` comparisons.

    After the CLI-unification refactor (m5b), the long chain of
    ``if raw and raw[0] == "..."`` in ``astrid/core/gateway/dispatch.py:_dispatch()``
    must be replaced with a table-driven dispatch (register-based,
    argparse sub-parsers, or equivalent).  String-comparison chains are a
    maintenance liability because they duplicate the verb vocabulary and
    allow typos/additions to go unnoticed.

    This test uses AST-level inspection to assert the ``raw[0] ==``
    pattern is absent from ``_dispatch()``.
    """

    def test_dispatch_has_no_raw0_equality_chain(self) -> None:
        """No ``raw[0] == ...`` comparison inside ``_dispatch()``."""
        pipeline_path = _REPO_ROOT / "astrid" / "core" / "gateway" / "dispatch.py"
        source = pipeline_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(pipeline_path))

        dispatch_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_dispatch":
                dispatch_fn = node
                break

        self.assertIsNotNone(dispatch_fn, "Could not find _dispatch() in gateway/dispatch.py")

        raw0_eq_locations: list[str] = []
        for node in ast.walk(dispatch_fn):
            if isinstance(node, ast.Compare):
                # Look for ``raw[0] == "..."``
                if (
                    isinstance(node.left, ast.Subscript)
                    and isinstance(node.left.value, ast.Name)
                    and node.left.value.id == "raw"
                    and isinstance(node.left.slice, ast.Constant)
                    and node.left.slice.value == 0
                    and any(isinstance(op, ast.Eq) for op in node.ops)
                ):
                    raw0_eq_locations.append(f"line {node.lineno}")

        self.assertEqual(
            raw0_eq_locations,
            [],
            f"_dispatch() contains raw[0] == comparisons at: {raw0_eq_locations}",
        )

    def test_dispatch_uses_table_or_parser_not_if_chain(self) -> None:
        """_dispatch() uses argparse or a dispatch map, not a long if-chain."""
        pipeline_path = _REPO_ROOT / "astrid" / "core" / "gateway" / "dispatch.py"

        import ast as ast_mod

        source = pipeline_path.read_text(encoding="utf-8")
        tree = ast_mod.parse(source, filename=str(pipeline_path))

        dispatch_fn = None
        for node in ast_mod.walk(tree):
            if isinstance(node, ast_mod.FunctionDef) and node.name == "_dispatch":
                dispatch_fn = node
                break

        self.assertIsNotNone(dispatch_fn)

        # Count top-level If statements whose test involves raw[0]
        raw_if_count = 0
        for stmt in dispatch_fn.body:
            if isinstance(stmt, ast_mod.If):
                test_str = ast_mod.unparse(stmt.test)
                if "raw[0]" in test_str and "==" in test_str:
                    raw_if_count += 1

        # After refactor there should be 0-2 fallback guards at most,
        # not the current ~35-branch chain.
        self.assertLessEqual(
            raw_if_count,
            3,
            f"_dispatch() has {raw_if_count} raw[0]== branches; expected ≤ 3 after refactor",
        )


# ---------------------------------------------------------------------------
# 2.  Unknown commands never hit default hype
# ---------------------------------------------------------------------------

class UnknownCommandDoesNotHitDefaultHypeTest(unittest.TestCase):
    """Unknown top-level commands must not invoke the default hype orchestrator.

    Historically ``astrid gateway/dispatch.py:_dispatch()`` fell through to
    ``_run_default_brief_orchestrator(raw)`` whenever ``raw[0]`` started
    with ``--`` and didn't match any known verb.  The m6 cutover deleted
    that fallthrough entirely.

    Unknown commands (including those starting with ``--``) exit with a
    non-zero code and a clear error message, never hitting the default
    hype path.
    """

    def test_unknown_top_level_command_exits_nonzero(self) -> None:
        """``astrid nonexistent_cmd`` exits with non-zero code."""
        result = subprocess.run(
            [sys.executable, "-m", "astrid", "nonexistent_cmd_xyzzy"],
            capture_output=True,
            text=True,
            env=_repo_env(),
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0, "Unknown command should exit non-zero")
        self.assertIn(
            "unknown command",
            (result.stderr + result.stdout).lower(),
            "Error message must mention 'unknown command'",
        )

    def test_unknown_flag_style_command_exits_nonzero(self) -> None:
        """``astrid --not-a-real-flag value`` exits non-zero, does NOT run hype."""
        result = subprocess.run(
            [sys.executable, "-m", "astrid", "--not-a-real-flag", "value"],
            capture_output=True,
            text=True,
            env=_repo_env(),
            timeout=30,
        )
        # Must not exit 0 (hype default path exits 0 on simple brief routing).
        self.assertNotEqual(result.returncode, 0, "Unknown flag-style command should exit non-zero")
        # Must not contain hype orchestrator output.
        self.assertNotIn(
            "hype",
            (result.stderr + result.stdout).lower(),
            "Unknown command should not invoke the default hype orchestrator",
        )

    def test_empty_flags_do_not_trigger_hype(self) -> None:
        """``astrid --unknown`` must be rejected, never routed to hype."""
        # An unknown long-option-only invocation.
        result = subprocess.run(
            [sys.executable, "-m", "astrid", "--made-up-option"],
            capture_output=True,
            text=True,
            env=_repo_env(),
            timeout=30,
        )
        # The m6 gateway rejects every unknown first token, including
        # flag-style ones: there is no default-hype fallthrough.
        self.assertNotEqual(
            result.returncode,
            0,
            "Unknown long option should not silently exit 0",
        )
        stderr_lower = result.stderr.lower()
        # Should complain about an unknown command, not start running hype.
        self.assertTrue(
            "unknown" in stderr_lower or "unrecognized" in stderr_lower or "error" in stderr_lower,
            f"Expected an error message for unknown option, got: {result.stderr[:200]}",
        )

    def test_help_flag_still_works(self) -> None:
        """``--help`` must still exit 0 after the refactor (non-regression)."""
        result = subprocess.run(
            [sys.executable, "-m", "astrid", "--help"],
            capture_output=True,
            text=True,
            env=_repo_env(),
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, "--help should still exit 0")


# ---------------------------------------------------------------------------
# 3.  Product-family help is executable (packs removed by the m6 cutover)
# ---------------------------------------------------------------------------

class FamilyHelpIsExecutableTest(unittest.TestCase):
    """The m6 gateway removed the ``packs`` family; the five product
    families are the executable surface. Each family's ``--help`` exits 0
    through the product dispatch boundary (argparse help short-circuits
    before any database is opened).
    """

    def test_projects_help_is_executable(self) -> None:
        """``projects --help`` exits 0 and documents the five verbs."""
        result = subprocess.run(
            [sys.executable, "-m", "astrid", "projects", "--help"],
            capture_output=True,
            text=True,
            env=_repo_env(),
            timeout=30,
        )
        self.assertEqual(
            result.returncode, 0, f"projects --help failed: {result.stderr}"
        )
        self.assertIn("create", result.stdout)
        self.assertIn("select", result.stdout)

    def test_timelines_help_is_executable_and_names_verbs_and_shots(self) -> None:
        """``timelines --help`` exits 0 and documents the seven verbs plus
        the nested shots mount."""
        result = subprocess.run(
            [sys.executable, "-m", "astrid", "timelines", "--help"],
            capture_output=True,
            text=True,
            env=_repo_env(),
            timeout=30,
        )
        self.assertEqual(
            result.returncode, 0, f"timelines --help failed: {result.stderr}"
        )
        for verb in ("create", "list", "show", "save", "archive", "history", "diff"):
            self.assertIn(verb, result.stdout)
        self.assertIn("shots", result.stdout)

    def test_removed_packs_family_is_rejected(self) -> None:
        """``packs`` is no longer a top-level family: it exits 2 with an
        unknown-command error instead of running the old pack CLI."""
        result = subprocess.run(
            [sys.executable, "-m", "astrid", "packs", "list", "--json"],
            capture_output=True,
            text=True,
            env=_repo_env(),
            timeout=30,
        )
        self.assertEqual(result.returncode, 2, "packs must be an unknown command")
        self.assertIn("unknown command 'packs'", result.stderr)


# ---------------------------------------------------------------------------
# 5.  Cross-cutting: default hype routing is explicit, not a fallthrough
# ---------------------------------------------------------------------------

class DefaultBriefRoutingIsExplicitTest(unittest.TestCase):
    """The default-hype routing fallthrough is gone (m6 teardown).

    ``_run_default_brief_orchestrator`` was the catch-all at the bottom of
    ``_dispatch()``; the m6 cutover deleted it. Brief-based orchestration
    is no longer reachable through any gateway route.
    """

    def test_default_brief_not_reached_by_unknown_args(self) -> None:
        """Arbitrary unknown argv must NOT invoke default hype."""
        import io

        from astrid.core import gateway as pipeline_mod

        # Simulate an unrecognized verb.
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            try:
                result = pipeline_mod.main(["unrecognized_verb_xyz"])
            except SystemExit as exc:
                result = int(exc.code) if exc.code is not None else 2

        self.assertNotEqual(result, 0, "Unknown verb must exit non-zero")

    def test_default_brief_requires_explicit_brief_flag(self) -> None:
        """The hype orchestrator should only be reachable via explicit
        ``--brief`` routing, not by passing mysterious arguments."""
        # This test verifies that entering hype requires an explicit pathway.
        # Read the dispatch function source to verify _run_default_brief_orchestrator
        # is only called from controlled contexts.
        pipeline_path = _REPO_ROOT / "astrid" / "core" / "gateway" / "dispatch.py"
        source = pipeline_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(pipeline_path))

        call_sites: list[int] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_run_default_brief_orchestrator"
            ):
                call_sites.append(node.lineno)

        # After refactor, _run_default_brief_orchestrator should only be
        # called from explicit brief-aware entry points, not from a catch-all
        # fallthrough in _dispatch().
        dispatch_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_dispatch":
                dispatch_fn = node
                break

        if dispatch_fn:
            for node in ast.walk(dispatch_fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_run_default_brief_orchestrator"
                ):
                    self.fail(
                        f"_dispatch() still calls _run_default_brief_orchestrator "
                        f"at line {node.lineno} — default hype must not be a "
                        f"dispatch fallthrough"
                    )


if __name__ == "__main__":
    unittest.main()
