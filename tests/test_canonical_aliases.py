import importlib.util
import unittest
from typing import get_args

from astrid.core.element import ElementDefinition, ElementRegistry
from astrid.core.execution.executor import ExecutorDefinition, ExecutorRegistry
from astrid.core.execution.executor import load_default_registry as load_executor_registry
from astrid.core.execution.orchestrator import OrchestratorDefinition, OrchestratorRegistry
from astrid.core.execution.orchestrator import load_default_registry as load_orchestrator_registry
from astrid.core.pack import PACK_ALIAS_KINDS, PackAliasKind


class CanonicalAliasTest(unittest.TestCase):
    def test_pack_alias_kinds_include_rendering_extensions(self) -> None:
        expected = ("executor", "orchestrator", "renderer", "planner", "finalizer")
        self.assertEqual(PACK_ALIAS_KINDS, expected)
        self.assertEqual(get_args(PackAliasKind), expected)

    def test_orchestrator_api_uses_canonical_implementation(self) -> None:
        self.assertEqual(OrchestratorDefinition.__module__, "astrid.core.execution.orchestrator.schema")
        self.assertEqual(OrchestratorRegistry.__module__, "astrid.core.execution.orchestrator.registry")

        registry = load_orchestrator_registry()

        # Only actions that declare metadata.local_orchestrator are orchestrators.
        # video_editing workflows are host executors under the schema v3 pack.yaml.
        self.assertEqual(set(registry.as_mapping()), {"h3_av.transform"})
        self.assertIsInstance(registry, OrchestratorRegistry)

    def test_executor_api_uses_canonical_implementation(self) -> None:
        self.assertEqual(ExecutorDefinition.__module__, "astrid.core.execution.executor.schema")
        self.assertEqual(ExecutorRegistry.__module__, "astrid.core.execution.executor.registry")

        registry = load_executor_registry()

        self.assertIn("editorial.transcribe", registry.as_mapping())
        self.assertIn("vibecomfy.run", registry.as_mapping())
        self.assertIsInstance(registry, ExecutorRegistry)

    def test_legacy_public_packages_are_absent(self) -> None:
        self.assertIsNone(importlib.util.find_spec("astrid.performers"))
        self.assertIsNone(importlib.util.find_spec("astrid.conductors"))
        self.assertIsNone(importlib.util.find_spec("astrid.executors"))
        self.assertIsNone(importlib.util.find_spec("astrid.orchestrators"))

    def test_element_framework_api_exports(self) -> None:
        self.assertEqual(ElementRegistry.__module__, "astrid.core.element.registry")
        self.assertEqual(ElementDefinition.__module__, "astrid.core.element.schema")

    def test_top_level_orchestrator_modules_are_absent(self) -> None:
        self.assertIsNone(importlib.util.find_spec("astrid.event_talks"))
        self.assertIsNone(importlib.util.find_spec("astrid.thumbnail_maker"))
        self.assertIsNone(importlib.util.find_spec("astrid.understand"))


class TimelineProductParserAliasTest(unittest.TestCase):
    """The m4 product timelines parser (plan step 26, task T28) is alias-free.

    The planned timeline verbs (including ``visualize`` and canonical ``render``)
    are registered — plus the
    manifest-owned nested ``shots`` mount (``astrid timelines shots``,
    plan step 26, task T29) — while obsolete aliases (``ls``, ...) and the
    legacy migration/push/pull/sync/audit/erase/repair verbs are absent,
    and ``copy`` (reserved save-as-copy, m6) is never registered.
    """

    @staticmethod
    def _choices():
        import argparse

        from astrid.packs.timeline.cli import build_parser

        parser = build_parser(client=object())
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return set(action.choices)
        raise AssertionError("timelines product parser has no subparsers")

    def test_product_timeline_parser_has_no_obsolete_aliases(self) -> None:
        from astrid.packs.timeline.cli import COMMANDS

        # ``save`` (whole-document CAS) was retired with the legacy timeline
        # document routes; ``publish`` is the SDK merge/CAS boundary.
        self.assertEqual(
            tuple(spec.name for spec in COMMANDS),
            (
                "checkout",
                "edit",
                "assets",
                "find",
                "apply",
                "undo",
                "redo",
                "check",
                "status",
                "publish",
                "discard",
                "duplicate",
                "rename",
                "restructure",
                "lines",
                "words",
                "create",
                "list",
                "show",
                "script",
                "elements",
                "update-element",
                "replace-parent-media",
                "archive",
                "recover",
                "history",
                "log",
                "diff",
                "visualize",
                "lint",
                "inspect",
                "render",
            ),
        )
        for spec in COMMANDS:
            with self.subTest(command=spec.name):
                self.assertEqual(spec.aliases, ())

    def test_product_timeline_parser_exposes_only_the_supported_verbs(self) -> None:
        # The manifest-owned nested ``shots`` mount and the ``transcripts``
        # mount (narration transcript sync) are declared parser choices
        # beneath ``timelines``; timeline verbs are the only SDK-adapter verbs, and
        # ``copy`` plus every legacy/obsolete alias stays absent.
        self.assertEqual(
            self._choices(),
            {
                "checkout",
                "edit",
                "assets",
                "find",
                "apply",
                "undo",
                "redo",
                "check",
                "status",
                "publish",
                "discard",
                "duplicate",
                "rename",
                "restructure",
                "lines",
                "words",
                "create",
                "list",
                "show",
                "script",
                "elements",
                "update-element",
                "replace-parent-media",
                "archive",
                "recover",
                "history",
                "log",
                "diff",
                "visualize",
                "lint",
                "inspect",
                "render",
                "shots",
                "transcripts",
            },
        )

    def test_legacy_timeline_verbs_are_absent_from_product_parser(self) -> None:
        choices = self._choices()
        for verb in (
            "migration",
            "migrate",
            "push",
            "pull",
            "sync",
            "audit",
            "erase",
            "repair",
            "ls",
            "finalize",
            "tombstone",
            "purge",
            "set-default",
            "export",
        ):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, choices)

    def test_timelines_copy_is_absent_from_product_parser(self) -> None:
        # Save-as-copy is reserved contractually (plan step 2) and
        # implemented in m6; the CLI verb must never be registered.
        self.assertNotIn("copy", self._choices())
        from astrid.packs.timeline.cli import build_parser

        parser = build_parser(client=object())
        with self.assertRaises(SystemExit) as raised:
            parser.parse_args(["copy", "main"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
