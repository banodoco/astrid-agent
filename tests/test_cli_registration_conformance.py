"""CLI registration conformance tests.

Validates that the shared :class:`astrid.core.cli.registration.CommandSpec`
helper works correctly with arbitrary argparse parser configuration and
that the phased allowlist tracks which CLI modules are migrated.

This is a phased gate: the allowlist starts with the first migrated CLI
module and expands as subsequent giant-file splits land.  Entries not yet
migrated are listed under ``_EXPECTED_FUTURE`` with a feasibility note.
"""

from __future__ import annotations

import argparse
import unittest

from astrid.core.cli.registration import CommandSpec, register_commands

# ── Phased allowlist ────────────────────────────────────────────────────────
#
# Every entry under _ALLOWLISTED names a CLI module that has adopted
# CommandSpec + register_commands.  The conformance test below asserts
# that these modules define a module-level ``COMMANDS`` list and that
# every entry passes the registration contract.
#
# _EXPECTED_FUTURE lists modules that still use traditional argparse
# build_parser() and are expected to adopt the convention later, together
# with a short feasibility reason.
#
# _FULLY_DECOMPOSED notes modules that were below-threshold split during
# M4 but retain their traditional argparse pattern (no CommandSpec adoption
# planned in this epic).  They are no longer in any gate list.

_ALLOWLISTED: tuple[str, ...] = (
    # m6 teardown: the seven product-family CLI modules (five product
    # families + the two runtime-declared nested mounts) are the pruned
    # allowlist. The legacy session/timeline/project CLI modules were
    # deleted by the m6 cutover (T5/T6-T9).
    "astrid.core.cli.domain_projects",
    "astrid.core.cli.domain_media",
    "astrid.core.cli.domain_tasks",
    "astrid.core.cli.domain_runs",
    "astrid.packs.timeline.cli",
    "astrid.core.cli.domain_shots",
    "astrid.core.cli.domain_references",
)

# Fully decomposed during M4 — below the 1,200-line threshold and using
# traditional argparse (no CommandSpec migration needed or planned):
#   astrid.core.timeline.cli  (split into cli_parser + 5 handler modules, T4-T12)
#   astrid.core.pack.cli      (split into cli_parser + 3 handler modules, T14-T16)

_EXPECTED_FUTURE: dict[str, str] = {
    "astrid.core.execution.executor.cli": (
        "Already uses a clean build_parser() pattern. "
        "Can adopt CommandSpec when the executor CLI is split."
    ),
    "astrid.core.element.cli": (
        "Follows the same build_parser() convention as executor.cli. "
        "Low-risk migration after the executor split validates the pattern."
    ),
}


# ── Unit tests for CommandSpec and register_commands ─────────────────────────

class CommandSpecContractTest(unittest.TestCase):
    """The CommandSpec dataclass must enforce its invariants."""

    def test_empty_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            CommandSpec("", help="bad")

    def test_empty_help_raises(self) -> None:
        with self.assertRaises(ValueError):
            CommandSpec("cmd", help="")

    def test_help_must_be_string(self) -> None:
        with self.assertRaises(ValueError):
            CommandSpec("cmd", help=42)  # type: ignore[arg-type]

    def test_default_aliases_is_empty(self) -> None:
        spec = CommandSpec("cmd", help="A command")
        self.assertEqual(spec.aliases, ())

    def test_configure_defaults_to_none(self) -> None:
        spec = CommandSpec("cmd", help="A command")
        self.assertIsNone(spec.configure)


class RegisterCommandsTest(unittest.TestCase):
    """register_commands must configure argparse subparsers correctly."""

    def test_basic_registration(self) -> None:
        parser = argparse.ArgumentParser(prog="test")
        sub = parser.add_subparsers(dest="command", required=True)
        commands = [
            CommandSpec("ls", help="List things", configure=lambda p: p.set_defaults(handler=lambda: 1)),
            CommandSpec("show", help="Show one thing", configure=lambda p: p.set_defaults(handler=lambda: 2)),
        ]
        register_commands(sub, commands)

        args = parser.parse_args(["ls"])
        self.assertEqual(args.command, "ls")
        self.assertEqual(args.handler(), 1)

        args2 = parser.parse_args(["show"])
        self.assertEqual(args2.command, "show")
        self.assertEqual(args2.handler(), 2)

    def test_aliases_are_registered(self) -> None:
        parser = argparse.ArgumentParser(prog="test")
        sub = parser.add_subparsers(dest="command", required=True)
        register_commands(sub, [
            CommandSpec("list", help="List", aliases=["ls"], configure=lambda p: p.set_defaults(handler=lambda: 3)),
        ])

        # Primary name
        args = parser.parse_args(["list"])
        self.assertEqual(args.command, "list")
        self.assertEqual(args.handler(), 3)

        # Alias — argparse stores the token the user typed in dest
        args2 = parser.parse_args(["ls"])
        self.assertEqual(args2.command, "ls")
        self.assertEqual(args2.handler(), 3)

    def test_configure_can_add_arguments(self) -> None:
        parser = argparse.ArgumentParser(prog="test")
        sub = parser.add_subparsers(dest="command", required=True)

        def _cfg(p: argparse.ArgumentParser) -> None:
            p.add_argument("--json", action="store_true")
            p.add_argument("--limit", type=int, default=10)
            p.set_defaults(handler=lambda parsed: (parsed.json, parsed.limit))

        register_commands(sub, [CommandSpec("query", help="Query", configure=_cfg)])

        args = parser.parse_args(["query", "--json", "--limit", "5"])
        self.assertEqual(args.handler(args), (True, 5))

    def test_missing_configure_strict_raises(self) -> None:
        parser = argparse.ArgumentParser(prog="test")
        sub = parser.add_subparsers(dest="command", required=True)
        with self.assertRaises(ValueError) as ctx:
            register_commands(sub, [CommandSpec("bare", help="No config")])
        self.assertIn("bare", str(ctx.exception))
        self.assertIn("configure", str(ctx.exception))

    def test_missing_configure_non_strict_allows(self) -> None:
        parser = argparse.ArgumentParser(prog="test")
        sub = parser.add_subparsers(dest="command", required=True)
        # Should not raise when _strict_configure=False
        register_commands(sub, [CommandSpec("bare", help="No config")], _strict_configure=False)
        # The subparser was created but without a configure callback
        args = parser.parse_args(["bare"])
        self.assertEqual(args.command, "bare")
        self.assertFalse(hasattr(args, "handler"))

    def test_subparser_records_help_text(self) -> None:
        parser = argparse.ArgumentParser(prog="test")
        sub = parser.add_subparsers(dest="command", required=True)
        register_commands(sub, [
            CommandSpec("alpha", help="First letter", configure=lambda p: p.set_defaults(handler=lambda: 0)),
        ])
        # The subcommand is registered
        self.assertIn("alpha", sub.choices)
        # Help text flows into the parent parser's help output
        help_out = parser.format_help()
        self.assertIn("alpha", help_out)
        self.assertIn("First letter", help_out)


class PhasedAllowlistTest(unittest.TestCase):
    """The phased allowlist must be valid and internally consistent."""

    def test_allowlisted_modules_are_importable_and_have_commands(self) -> None:
        """Every allowlisted module must define a COMMANDS list of CommandSpec."""
        import importlib

        for module_name in _ALLOWLISTED:
            with self.subTest(module=module_name):
                mod = importlib.import_module(module_name)
                self.assertTrue(
                    hasattr(mod, "COMMANDS"),
                    f"{module_name} must define COMMANDS",
                )
                commands = getattr(mod, "COMMANDS")
                self.assertIsInstance(commands, (list, tuple))
                for spec in commands:
                    with self.subTest(command=spec.name):
                        self.assertIsInstance(spec, CommandSpec)

    def test_expected_future_has_feasibility_note(self) -> None:
        """Every expected-future entry must document why migration is feasible."""
        for module_name, note in _EXPECTED_FUTURE.items():
            with self.subTest(module=module_name):
                self.assertIsInstance(note, str)
                self.assertGreater(len(note), 10,
                                   f"Feasibility note for {module_name} is too short")

    def test_no_overlap_between_allowlisted_and_expected(self) -> None:
        """A module cannot be both allowlisted and in expected-future."""
        overlap = set(_ALLOWLISTED) & set(_EXPECTED_FUTURE)
        self.assertEqual(overlap, set(),
                         f"Modules in both allowlisted and expected-future: {overlap}")


class ProductRegistryConformanceTest(unittest.TestCase):
    """The m4 product registry (plan step 24, task T25) conforms exactly.

    Exactly seven product families; shots/references attach as nested
    mounts from the explicit in-tree manifest declarations; serve, doctor,
    and backup stay outside the product census and product dispatch
    (m6 cutover); handlers receive the composed AstridClient.
    """

    def test_product_census_is_exactly_seven_families(self) -> None:
        from astrid.core.cli.domain_product import (
            PRODUCT_FAMILIES,
            product_top_level_commands,
        )

        self.assertEqual(
            frozenset(PRODUCT_FAMILIES),
            {"projects", "documents", "preferences", "media", "tasks", "runs", "timelines"},
        )
        self.assertEqual(len(PRODUCT_FAMILIES), 7)
        self.assertEqual(product_top_level_commands(), frozenset(PRODUCT_FAMILIES))

    def test_nested_mounts_attach_under_timelines_and_media(self) -> None:
        from astrid.core.cli.domain_product import (
            build_product_mounts,
            product_top_level_commands,
        )

        by_family = {mount.family: mount.mount_path for mount in build_product_mounts()}
        self.assertEqual(by_family["shots"], ("timelines", "shots"))
        self.assertEqual(by_family["references"], ("media", "references"))
        # Nested families are not top-level product commands.
        self.assertNotIn("shots", product_top_level_commands())
        self.assertNotIn("references", product_top_level_commands())

    def test_runtime_mounts_declared_in_tree(self) -> None:
        from astrid.core.cli.domain_product import read_runtime_cli_mounts

        declared = {mount.family: mount.token for mount in read_runtime_cli_mounts()}
        self.assertEqual(
            declared,
            {
                "timelines": "timelines",
                "shots": "timelines shots",
                "references": "media references",
            },
        )

    def test_operational_families_excluded_from_product_census(self) -> None:
        from astrid.core.cli.domain_product import (
            EXCLUDED_FROM_PRODUCT_CENSUS,
            is_product_family,
            product_top_level_commands,
        )

        self.assertEqual(
            EXCLUDED_FROM_PRODUCT_CENSUS,
            frozenset({"doctor", "backup"}),
        )
        for excluded in ("doctor", "backup"):
            with self.subTest(excluded=excluded):
                self.assertIn(excluded, EXCLUDED_FROM_PRODUCT_CENSUS)
                self.assertFalse(is_product_family(excluded))
                self.assertNotIn(excluded, product_top_level_commands())
        # The removed singular run alias is gone from the top-level surface.
        self.assertNotIn("run", product_top_level_commands())

    def test_register_product_commands_stamps_client_and_family(self) -> None:
        from astrid.core.cli.registration import (
            CommandSpec,
            register_product_commands,
        )

        parser = argparse.ArgumentParser(prog="astrid projects")
        sub = parser.add_subparsers(dest="command", required=True)
        client = object()
        register_product_commands(
            sub,
            [
                CommandSpec(
                    "ls",
                    help="List projects",
                    configure=lambda p: p.set_defaults(
                        handler=lambda parsed: (parsed.client, parsed.family)
                    ),
                )
            ],
            family="projects",
            client=client,
        )

        parsed = parser.parse_args(["ls"])
        self.assertIs(parsed.client, client)
        self.assertEqual(parsed.family, "projects")
        self.assertEqual(parsed.handler(parsed), (client, "projects"))

    def test_register_product_commands_rejects_unknown_family(self) -> None:
        from astrid.core.cli.registration import (
            CommandSpec,
            register_product_commands,
        )

        parser = argparse.ArgumentParser(prog="astrid")
        sub = parser.add_subparsers(dest="command", required=True)
        with self.assertRaises(ValueError):
            register_product_commands(
                sub,
                [
                    CommandSpec(
                        "ls",
                        help="List",
                        configure=lambda p: p.set_defaults(handler=lambda parsed: 0),
                    )
                ],
                family="serve",
                client=object(),
            )

    def test_product_registry_rejects_invalid_mounts(self) -> None:
        from astrid.core.cli.domain_product import (
            PRODUCT_FAMILIES,
            ProductRegistryError,
            RuntimeMount,
            _validate_mounts,
        )

        base = (
            RuntimeMount("timelines", "timelines", "runtime"),
            RuntimeMount("shots", "timelines shots", "runtime"),
            RuntimeMount("references", "media references", "runtime"),
        )
        # Valid base registry builds: seven core families (timelines is
        # confirmed in place, not duplicated) plus the shots and references nested mounts.
        self.assertEqual(len(_validate_mounts(PRODUCT_FAMILIES, base)), 9)

        # Missing declaration.
        with self.assertRaises(ProductRegistryError):
            _validate_mounts(PRODUCT_FAMILIES, base[:2])
        # Duplicate family declaration.
        with self.assertRaises(ProductRegistryError):
            _validate_mounts(PRODUCT_FAMILIES, base + base[1:2])
        # Unexpected family.
        with self.assertRaises(ProductRegistryError):
            _validate_mounts(
                PRODUCT_FAMILIES,
                base
                + (RuntimeMount("extra", "projects extra", "runtime-extra"),),
            )
        # Unexpected path for a declared family.
        with self.assertRaises(ProductRegistryError):
            _validate_mounts(
                PRODUCT_FAMILIES,
                (
                    RuntimeMount("timelines", "timelines", "runtime"),
                    RuntimeMount("shots", "media references", "runtime"),
                    RuntimeMount("references", "media references", "runtime"),
                ),
            )

    def test_allowlisted_has_no_unregistered_commands(self) -> None:
        """Every allowlisted module's COMMANDS must pass through register_commands
        without error (strict mode)."""
        import importlib

        for module_name in _ALLOWLISTED:
            with self.subTest(module=module_name):
                mod = importlib.import_module(module_name)
                commands = getattr(mod, "COMMANDS")
                parser = argparse.ArgumentParser(prog="conformance")
                sub = parser.add_subparsers(dest="command", required=True)
                # Should not raise
                register_commands(sub, list(commands))
                # Every command name is registered
                for spec in commands:
                    self.assertIn(spec.name, sub.choices)


if __name__ == "__main__":
    unittest.main()
