import contextlib
import io
import sys
import unittest
from unittest import mock

from astrid.core import gateway


class PipelineDispatchAliasTest(unittest.TestCase):
    def test_root_help_explains_canonical_gateway(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(gateway.main(["--help"]), 0)

        help_text = stdout.getvalue()
        self.assertIn("Astrid command gateway", help_text)
        self.assertIn("python3 -m astrid is the package entry point", help_text)
        self.assertIn("doctor emits", help_text)
        self.assertIn("backup emits its runtime result object", help_text)
        # The seven Stage1 families are all documented.
        for family in (
            "projects",
            "timelines",
            "media",
            "tasks",
            "runs",
            "doctor",
            "backup",
        ):
            self.assertIn(f"python3 -m astrid {family}", help_text)
        # Removed families must not be documented anywhere.
        for removed in (
            "orchestrators",
            "executors",
            "elements",
            "packs",
            "orchestrate",
            "author",
            "renderers",
            "replay",
            "publish",
        ):
            self.assertNotIn(removed, help_text)
        self.assertNotIn("gateway.py", help_text)
        self.assertNotIn("conductors", help_text)
        self.assertNotIn("performers", help_text)

    def test_legacy_public_dispatch_tokens_are_rejected(self) -> None:
        for token in (
            "performers",
            "instruments",
            "conductors",
            "primitives",
            "run",
            "author",
            "orchestrate",
            "renderers",
            "replay",
            "elements",
            "publish",
        ):
            with self.subTest(token=token):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    exit_code = gateway.main([token, "list"])
                self.assertEqual(exit_code, 2)
                self.assertIn(f"unknown command '{token}'", stderr.getvalue())

    def test_top_level_handlers_are_exactly_the_current_families(self) -> None:
        from astrid.core.gateway.dispatch import _TOP_LEVEL_HANDLERS

        # documents/preferences (3ecf5b1b) and dev (39240d8d) are deliberate
        # top-level families added after the Stage1 seven.
        self.assertEqual(
            set(_TOP_LEVEL_HANDLERS),
            {
                "projects",
                "timelines",
                "media",
                "tasks",
                "runs",
                "documents",
                "preferences",
                "dev",
                "doctor",
                "backup",
            },
        )
        # The removed handlers (run/author/orchestrate/renderers/replay/...) do
        # not appear anywhere in the handler table (m6 teardown).
        for removed in (
            "run",
            "author",
            "orchestrate",
            "renderers",
            "replay",
            "elements",
            "executors",
            "orchestrators",
            "publish",
            "setup",
            "packs",
        ):
            self.assertNotIn(removed, _TOP_LEVEL_HANDLERS)

    def test_product_families_route_through_product_dispatch(self) -> None:
        """All five product families prepend their token to _dispatch_product."""
        from astrid.core.gateway import dispatch

        seen: dict[str, object] = {}

        def _fake_product(args):  # noqa: ANN001
            seen["args"] = list(args)
            return 7

        with mock.patch.object(dispatch, "_dispatch_product", _fake_product):
            for family in ("projects", "timelines", "media", "tasks", "runs"):
                with self.subTest(family=family):
                    handler = dispatch._TOP_LEVEL_HANDLERS[family]
                    self.assertEqual(handler(["list"]), 7)
                    self.assertEqual(seen["args"], [family, "list"])

    def test_operational_families_have_their_own_routes(self) -> None:
        """doctor/backup dispatch through their own handlers, never
        through the product boundary."""
        from astrid.core.gateway import dispatch

        for family in ("doctor", "backup"):
            with self.subTest(family=family):
                handler = dispatch._TOP_LEVEL_HANDLERS[family]
                self.assertNotEqual(handler, dispatch._dispatch_product)
        self.assertIs(
            dispatch._TOP_LEVEL_HANDLERS["doctor"], dispatch._dispatch_doctor
        )
        self.assertIs(
            dispatch._TOP_LEVEL_HANDLERS["backup"], dispatch._dispatch_backup
        )

    def test_unknown_command_exits_2_with_message(self) -> None:
        """Unknown non-flag command prints to stderr and exits 2."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = gateway.main(["boguscmd"])
        self.assertEqual(exit_code, 2)
        self.assertIn("unknown command 'boguscmd'", stderr.getvalue())

    def test_flag_style_first_token_is_rejected_as_unknown_command(self) -> None:
        """Flag-style invocations are rejected: there is no default brief
        orchestrator fallthrough in the m6 gateway."""
        from astrid.core.gateway import dispatch

        self.assertFalse(hasattr(dispatch, "_run_default_brief_orchestrator"))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = gateway.main(["--brief", "some brief text"])
        self.assertEqual(exit_code, 2)
        self.assertIn("unknown command '--brief'", stderr.getvalue())

    def test_package_is_executable(self) -> None:
        import runpy

        old_argv = sys.argv
        stdout = io.StringIO()
        try:
            sys.argv = ["python3 -m astrid", "projects", "--help"]
            with contextlib.redirect_stdout(stdout):
                with self.assertRaises(SystemExit) as raised:
                    runpy.run_module("astrid", run_name="__main__")
        finally:
            sys.argv = old_argv

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("create", stdout.getvalue())

    def test_dispatch_helpers_captured_in_top_level_handlers(self) -> None:
        """Characterize: the product/operational handler dict holds the exact
        function references from astrid.core.gateway.dispatch, so patching the
        module attribute intercepts direct calls (tasks/runs are lambdas that
        prepend the family token)."""
        import astrid.core.gateway.dispatch as dispatch

        for family, attr in (
            ("projects", "_dispatch_projects"),
            ("timelines", "_dispatch_timelines"),
            ("media", "_dispatch_media"),
            ("doctor", "_dispatch_doctor"),
            ("backup", "_dispatch_backup"),
        ):
            with self.subTest(family=family):
                self.assertIs(
                    dispatch._TOP_LEVEL_HANDLERS[family],
                    getattr(dispatch, attr),
                )

        # The tasks/runs handlers are lambdas that forward to the product
        # boundary with the family token prepended.
        for family in ("tasks", "runs"):
            with self.subTest(family=family):
                seen: dict[str, object] = {}

                def _fake_product(args):  # noqa: ANN001
                    seen["args"] = list(args)
                    return 8

                with mock.patch.object(
                    dispatch, "_dispatch_product", _fake_product
                ):
                    result = dispatch._TOP_LEVEL_HANDLERS[family](["show"])
                self.assertEqual(result, 8)
                self.assertEqual(seen["args"], [family, "show"])


if __name__ == "__main__":
    unittest.main()
