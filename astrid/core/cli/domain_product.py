"""Product-family CLI registry.

This module is the ownership authority for the product command census and
runtime mounts. It declares **exactly** the five product families —
``projects``, ``media``, ``tasks``, ``runs``, and ``timelines`` — and attaches
the two nested mounts (``shots`` beneath ``timelines`` and ``references``
beneath ``media``). The mount table is code-declared because the workspace
runtime, not Astrid, owns the database schema and migration stream.

Rules enforced before any dispatch (sense check SC25):

The fixed table is validated for duplicate and unexpected paths before
dispatch. There is no schema loader, local schema registry, or dynamic mount
source, so a mount from an unreviewed source is impossible by construction.

The product census also excludes the operational commands (``serve``,
``doctor``, ``backup``): they are never part of the five-family product
registry, help, or product dispatch.

The dispatch boundary (:func:`run_product_family`) composes one
``AstridClient`` and passes it to the family's in-tree parser builder, so
every product handler is a rule-free SDK adapter.
"""

from __future__ import annotations

import argparse
import importlib
from collections.abc import Iterator
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from astrid.core.cli.registration import CommandSpec

__all__ = [
    "FAMILY_PARSER_MODULES",
    "family_parsers",
    "walk_parsers",
    "PRODUCT_FAMILIES",
    "PRODUCT_FAMILY_SET",
    "EXCLUDED_FROM_PRODUCT_CENSUS",
    "REQUIRED_RUNTIME_MOUNTS",
    "ProductMount",
    "ProductRegistryError",
    "build_product_mounts",
    "family_mount",
    "is_product_family",
    "is_registered_family",
    "product_top_level_commands",
    "read_runtime_cli_mounts",
    "command_requires_pack_host",
    "run_product_family",
]

PRODUCT_FAMILIES: tuple[str, ...] = (
    "projects",
    "documents",
    "preferences",
    "media",
    "tasks",
    "runs",
    "timelines",
)
"""The seven core product families."""

PRODUCT_FAMILY_SET: frozenset[str] = frozenset(PRODUCT_FAMILIES)

EXCLUDED_FROM_PRODUCT_CENSUS: frozenset[str] = frozenset(
    {"doctor", "backup"}
)
"""Operational commands excluded from the product census.

``doctor`` and ``backup`` are operational families (tooling over the single
SQLite file and managed-media root). Neither is a product family, appears in
the product census, or dispatches through the product boundary.
"""

REQUIRED_RUNTIME_MOUNTS: dict[str, tuple[str, ...]] = {
    # The runtime owns the top-level ``timelines`` family mount.
    "timelines": ("timelines",),
    # The runtime exposes its nested shots mount beneath timelines.
    "shots": ("timelines", "shots"),
    # The runtime exposes its nested references mount beneath media.
    "references": ("media", "references"),
}
"""The frozen runtime product mounts (family -> mount path)."""

# The runtime-owned families that are not core families: exactly the two
# nested mounts (sense check SC25: only the two nested mounts).
NESTED_FAMILIES: frozenset[str] = frozenset(
    family
    for family in REQUIRED_RUNTIME_MOUNTS
    if family not in PRODUCT_FAMILY_SET
)

class ProductRegistryError(ValueError):
    """Raised when the product mount registry is invalid.

    Covers missing, duplicate, unexpected, and dynamically sourced mounts:
    the registry fails closed before any dispatch can occur.
    """


@dataclass(frozen=True, slots=True)
class ProductMount:
    """One validated product mount declaration.

    ``mount_path`` is the CLI path segments: ``("projects",)`` for a core
    family, ``("timelines", "shots")`` for the nested shots family.
    ``declared_by`` is ``"core"`` for the four kernel families and
    ``"runtime:<domain>"`` for runtime-owned mounts (timelines, shots,
    references).
    """

    family: str
    mount_path: tuple[str, ...]
    declared_by: str

    def __post_init__(self) -> None:
        if not isinstance(self.family, str) or not self.family:
            raise ProductRegistryError("family must be a non-empty string")
        if not isinstance(self.mount_path, tuple) or not self.mount_path:
            raise ProductRegistryError(
                f"mount path for {self.family!r} must be non-empty"
            )

    @property
    def mount_token(self) -> str:
        """The space-joined CLI mount token (e.g. ``timelines shots``)."""
        return " ".join(self.mount_path)


@dataclass(frozen=True, slots=True)
class RuntimeMount:
    """One reviewed runtime-owned product mount."""

    family: str
    token: str
    pack_id: str


def read_runtime_cli_mounts() -> tuple[RuntimeMount, ...]:
    """Return the reviewed runtime-owned product mounts.

    This is deliberately a small code declaration. Product route ownership is
    not inferred from capability files or from a local schema description.
    """
    owners = {"timelines": "runtime", "shots": "runtime", "references": "runtime"}
    return tuple(
        RuntimeMount(family=family, token=" ".join(path), pack_id=owners[family])
        for family, path in REQUIRED_RUNTIME_MOUNTS.items()
    )


def _validate_mounts(
    core_families: Sequence[str],
    runtime_mounts: Sequence[RuntimeMount],
) -> tuple[ProductMount, ...]:
    """Validate the frozen product census and return the mount registry.

    Raises :class:`ProductRegistryError` for a missing required
    declaration, a duplicate family or mount path, or an unexpected
    family/path (including any declaration outside the frozen
    ``REQUIRED_RUNTIME_MOUNTS`` contract).
    """
    core_set = frozenset(core_families)
    mounts: list[ProductMount] = []
    seen_paths: dict[tuple[str, ...], str] = {}

    for family in core_families:
        path = (family,)
        mounts.append(ProductMount(family, path, "core"))
        seen_paths[path] = family

    declared_families: set[str] = set()
    for declared in runtime_mounts:
        family = declared.family
        if family in declared_families:
            raise ProductRegistryError(
                f"duplicate mount declaration for family {family!r}: "
                f"both {declared.pack_id!r} and a prior runtime entry declare it"
            )
        declared_families.add(family)

        token = declared.token
        if not isinstance(token, str) or not token.strip():
            raise ProductRegistryError(
                f"missing mount for family {family!r}: runtime entry "
                f"{declared.pack_id!r} declares an empty route"
            )
        declared_path = tuple(token.split())

        expected = REQUIRED_RUNTIME_MOUNTS.get(family)
        if expected is None:
            raise ProductRegistryError(
                f"unexpected mount for family {family!r}: runtime "
                f"{declared.pack_id!r} declares {token!r} but the family is "
                "not part of the frozen product registry"
            )
        if declared_path != expected:
            raise ProductRegistryError(
                f"unexpected mount for family {family!r}: runtime "
                f"{declared.pack_id!r} declares {token!r}, expected "
                f"{' '.join(expected)}"
            )

        if family in core_set:
            # Runtime-owned confirmation of a core top-level mount (the
            # timelines family). The path is identical, so there is no
            # duplicate; ownership transfers to the runtime.
            mounts = [
                ProductMount(m.family, m.mount_path, f"runtime:{declared.pack_id}")
                if m.family == family
                else m
                for m in mounts
            ]
            continue

        if declared_path in seen_paths:
            raise ProductRegistryError(
                f"duplicate mount {' '.join(declared_path)} declared by family "
                f"{family!r} and {seen_paths[declared_path]!r}"
            )
        mounts.append(
            ProductMount(family, declared_path, f"runtime:{declared.pack_id}")
        )
        seen_paths[declared_path] = family

    missing = set(REQUIRED_RUNTIME_MOUNTS) - declared_families
    if missing:
        raise ProductRegistryError(
            f"missing mount for family {sorted(missing)}: runtime mount table "
            "does not declare the required route"
        )
    return tuple(mounts)


def build_product_mounts() -> tuple[ProductMount, ...]:
    """Validate and return the complete product mount registry.

    Exactly the five core families plus the two runtime nested mounts (shots,
    references) — seven mounts total (sense check SC25).
    """
    return _validate_mounts(PRODUCT_FAMILIES, read_runtime_cli_mounts())


def product_top_level_commands() -> frozenset[str]:
    """The exact five-family top-level product census."""
    return PRODUCT_FAMILY_SET


def is_product_family(name: object) -> bool:
    """True when *name* is one of the five product families."""
    return isinstance(name, str) and name in PRODUCT_FAMILY_SET


def is_registered_family(name: object) -> bool:
    """True for a core family or a runtime-owned nested family.

    Nested families (shots, references) are not top-level product
    commands but are registered parsers beneath their parents.
    """
    if not isinstance(name, str):
        return False
    return name in PRODUCT_FAMILY_SET or name in REQUIRED_RUNTIME_MOUNTS


def family_mount(family: str) -> ProductMount:
    """Return the validated mount for one registered family.

    Raises :class:`ProductRegistryError` for an unregistered family.
    """
    for mount in build_product_mounts():
        if mount.family == family:
            return mount
    raise ProductRegistryError(f"{family!r} is not a registered product family")


# The explicit in-tree parser builders per family (map
# declared mount tokens to explicit in-tree parser builders). The modules
# are created by the family cutover tasks (steps 25/27-31); the mapping is
# the static contract, and the import stays lazy until dispatch.
FAMILY_PARSER_MODULES: dict[str, str] = {
    "projects": "astrid.core.cli.domain_projects",
    "documents": "astrid.core.cli.domain_documents",
    "preferences": "astrid.core.cli.domain_preferences",
    "timelines": "astrid.packs.timeline.cli",
    "shots": "astrid.packs.shots.cli",
    "media": "astrid.core.cli.domain_media",
    "references": "astrid.packs.references.cli",
    "tasks": "astrid.core.cli.domain_tasks",
    "runs": "astrid.core.cli.domain_runs",
}


def family_parsers() -> dict[str, argparse.ArgumentParser]:
    """Build the product-family parsers used by the CLI surface and MCP server."""
    parsers = {}
    for family in sorted(PRODUCT_FAMILIES):
        module = importlib.import_module(FAMILY_PARSER_MODULES[family])
        kwargs = {}
        if family == "media":
            references = importlib.import_module(FAMILY_PARSER_MODULES["references"])
            kwargs["reference_commands"] = references.COMMANDS
        parsers[family] = module.build_parser(None, **kwargs)
    return parsers


def walk_parsers(
    parser: argparse.ArgumentParser,
    path: tuple[str, ...] = (),
    summary: str = "",
) -> Iterator[tuple[tuple[str, ...], argparse.ArgumentParser, str]]:
    """Walk parser nodes in command order, carrying each verb's help summary."""
    yield path, parser, summary
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        helps = {choice.dest: choice.help or "" for choice in action._choices_actions}
        for name, subparser in sorted(action.choices.items()):
            yield from walk_parsers(subparser, (*path, name), helps.get(name, ""))


def command_requires_pack_host(family: str, args: Sequence[str]) -> bool:
    """Resolve worker-host readiness from the command's declaration.

    Product routes are workspace-service operations by default.  Only a
    command explicitly marked ``requires_pack_host`` may request the optional
    generic pack host.  Unknown routes fail closed while the real parser is
    still responsible for reporting the usage error.  This keeps lifecycle
    acquisition independent from an expanding, hand-maintained read allowlist.
    """
    if family == "preferences" and (not args or str(args[0]).startswith("--")):
        return False
    if family not in PRODUCT_FAMILY_SET:
        return True
    module_name = FAMILY_PARSER_MODULES.get(family)
    if not module_name or not args:
        return True
    module = importlib.import_module(module_name)
    commands = getattr(module, "COMMANDS", ())
    command = str(args[0])
    spec = next((item for item in commands if isinstance(item, CommandSpec) and item.name == command), None)
    if spec is None:
        # Nested mounts (shots/references) are workspace CRUD families and do
        # not need a pack host merely to compose their parser/client.
        if command in {"shots", "references"}:
            return False
        return True
    return bool(spec.requires_pack_host)


def run_product_family(
    family: str,
    args: Sequence[str],
    *,
    client: Any,
    _parser_modules: Mapping[str, str] | None = None,
) -> int:
    """Run one product-family command with the composed *client*.

    The family's in-tree parser builder is resolved from the static
    :data:`FAMILY_PARSER_MODULES` mapping (never discovered) and receives
    the shared ``AstridClient``; the configured handler then runs with
    zero domain rules in this module. The seven core families dispatch
    here — nested families (shots, references) are routed by their parent
    family parsers. ``_parser_modules`` is a test seam that replaces the
    module-name mapping with module objects.
    """
    if family not in PRODUCT_FAMILY_SET:
        raise ProductRegistryError(
            f"{family!r} is not a product family; product dispatch accepts "
            f"exactly {sorted(PRODUCT_FAMILY_SET)}"
        )
    modules = FAMILY_PARSER_MODULES if _parser_modules is None else _parser_modules
    module_name = modules.get(family)
    if not module_name:
        raise ProductRegistryError(
            f"no in-tree parser builder is declared for family {family!r}"
        )
    module = (
        module_name
        if not isinstance(module_name, str)
        else importlib.import_module(module_name)
    )
    build_parser = getattr(module, "build_parser", None)
    if build_parser is None:
        raise ProductRegistryError(
            f"family parser module {module.__name__!r} has no build_parser()"
        )
    parser_kwargs: dict[str, Any] = {}
    if family == "media":
        references_module_name = modules.get("references")
        if not references_module_name:
            raise ProductRegistryError(
                "media requires the runtime-owned references parser"
            )
        references_module = (
            references_module_name
            if not isinstance(references_module_name, str)
            else importlib.import_module(references_module_name)
        )
        reference_commands = getattr(references_module, "COMMANDS", None)
        if (
            not isinstance(reference_commands, SequenceABC)
            or not reference_commands
            or not all(isinstance(spec, CommandSpec) for spec in reference_commands)
        ):
            raise ProductRegistryError(
                "references parser module has no valid COMMANDS declaration"
            )
        parser_kwargs["reference_commands"] = reference_commands
    parser = build_parser(client, **parser_kwargs)
    parsed = parser.parse_args(list(args))
    handler = getattr(parsed, "handler", None)
    if handler is None:
        raise ProductRegistryError(
            f"family {family!r} parser did not configure a handler"
        )
    return int(handler(parsed))
