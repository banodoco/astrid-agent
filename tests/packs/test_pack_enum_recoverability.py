"""Pack enum recoverability harness.

The default lane runs a deterministic 5-pack sample chosen from the
recoverability conformance fixture. An opt-in exhaustive lane can replay the
same recovery exercise across every importable pack enum surface.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from astrid.core.cli.choices_registry import RegistryChoices
from astrid.core.cli_choices import AstridArgumentError, StaticChoices


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "recoverability_conformance_worklist.json"
RECOVERY_STEP_BUDGET = 3
# Surviving first-party packs only. Packs that moved out of this tree (for example
# editorial, understanding and video_editing) are not sampled, and their scanner rows
# are skipped below rather than imported.
SAMPLED_PACK_ORDER = (
    "generation",
    "rendering",
    "training",
    "vibecomfy",
    "h3_av",
)


@dataclass(frozen=True)
class EnumSurface:
    pack: str
    module: str
    builder: str
    path: str
    parser_path: tuple[str, ...]
    dest: str
    option_strings: tuple[str, ...]
    valid_options: tuple[str, ...]

    @property
    def label(self) -> str:
        parser_suffix = ""
        if self.parser_path:
            parser_suffix = f" ({'/'.join(self.parser_path)})"
        option = self.option_strings[-1] if self.option_strings else self.dest
        return f"{self.pack}:{Path(self.path).stem}{parser_suffix}:{option}"


@dataclass(frozen=True)
class Inventory:
    applicable_by_pack: dict[str, tuple[EnumSurface, ...]]
    not_applicable_by_pack: dict[str, str]


def _pack_name(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) < 4 or parts[:2] != ["astrid", "packs"]:
        return None
    return parts[2]


@contextlib.contextmanager
def _internal_invocation_env() -> Iterator[None]:
    previous = os.environ.get("ASTRID_INTERNAL_INVOCATION")
    os.environ["ASTRID_INTERNAL_INVOCATION"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("ASTRID_INTERNAL_INVOCATION", None)
        else:
            os.environ["ASTRID_INTERNAL_INVOCATION"] = previous


def _enum_args(
    parser: argparse.ArgumentParser,
    *,
    parser_path: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in sorted(action.choices.items()):
                rows.extend(_enum_args(child, parser_path=(*parser_path, name)))
            continue
        choices = getattr(action, "choices", None)
        if choices is None:
            continue
        if isinstance(choices, (RegistryChoices, StaticChoices)):
            valid_options = tuple(choices.valid_options)
        else:
            valid_options = tuple(str(value) for value in choices)
        rows.append(
            {
                "parser_path": parser_path,
                "dest": action.dest,
                "option_strings": tuple(action.option_strings),
                "valid_options": valid_options,
            }
        )
    return rows


@lru_cache(maxsize=1)
def _inventory() -> Inventory:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    applicable_by_pack: dict[str, list[EnumSurface]] = defaultdict(list)
    scanned_surface_counts: dict[str, int] = defaultdict(int)

    for surface in payload["scanned_parser_surfaces"]:
        pack = _pack_name(surface["path"])
        if pack is None:
            continue
        if not (ROOT / "astrid" / "packs" / pack).is_dir():
            # The pack moved out of this tree; its parser cannot be imported here.
            continue
        scanned_surface_counts[pack] += 1
        if surface["enum_arg_count"] == 0:
            continue
        with _internal_invocation_env():
            module = importlib.import_module(surface["module"])
            parser = getattr(module, surface["builder"])()
        enum_args = _enum_args(parser)
        assert len(enum_args) == surface["enum_arg_count"], (
            f"{surface['path']} scanner recorded {surface['enum_arg_count']} enum surfaces "
            f"but the harness found {len(enum_args)}"
        )
        for enum_arg in enum_args:
            applicable_by_pack[pack].append(
                EnumSurface(
                    pack=pack,
                    module=surface["module"],
                    builder=surface["builder"],
                    path=surface["path"],
                    parser_path=tuple(enum_arg["parser_path"]),
                    dest=enum_arg["dest"],
                    option_strings=tuple(enum_arg["option_strings"]),
                    valid_options=tuple(enum_arg["valid_options"]),
                )
            )

    not_applicable_by_pack: dict[str, str] = {}
    for pack, surface_count in sorted(scanned_surface_counts.items()):
        if applicable_by_pack.get(pack):
            continue
        not_applicable_by_pack[pack] = (
            f"scanner found {surface_count} importable parser surface(s) for {pack} "
            "with zero enum-choice actions"
        )

    return Inventory(
        applicable_by_pack={
            pack: tuple(sorted(surfaces, key=lambda item: (item.path, item.parser_path, item.dest)))
            for pack, surfaces in sorted(applicable_by_pack.items())
        },
        not_applicable_by_pack=not_applicable_by_pack,
    )


def _surface_action(parser: argparse.ArgumentParser, surface: EnumSurface) -> argparse.Action:
    current = parser
    for name in surface.parser_path:
        subparsers = next(
            action for action in current._actions if isinstance(action, argparse._SubParsersAction)
        )
        current = subparsers.choices[name]
    return next(
        action
        for action in current._actions
        if action.dest == surface.dest and tuple(action.option_strings) == surface.option_strings
    )


def _invalid_value(valid_options: Sequence[str]) -> str:
    candidate = "recoverability-invalid-choice"
    while candidate in valid_options:
        candidate = f"{candidate}-x"
    return candidate


def _exercise_recovery(surface: EnumSurface) -> None:
    with _internal_invocation_env():
        module = importlib.import_module(surface.module)
        parser = getattr(module, surface.builder)()
    action = _surface_action(parser, surface)
    invalid_value = _invalid_value(surface.valid_options)
    stderr_buffer = io.StringIO()
    visible_error_text = ""
    steps_used = 1

    with contextlib.redirect_stderr(stderr_buffer):
        try:
            parser._check_value(action, invalid_value)
        except AstridArgumentError as exc:
            visible_error_text = str(exc)
            assert tuple(exc.valid_options) == surface.valid_options
        except argparse.ArgumentError as exc:
            visible_error_text = str(exc)
        except SystemExit as exc:
            assert exc.code == 2, f"{surface.label}: unexpected exit code {exc.code!r}"
            visible_error_text = stderr_buffer.getvalue()
        else:
            pytest.fail(f"{surface.label}: invalid value {invalid_value!r} was accepted")

    assert visible_error_text, f"{surface.label}: parser emitted no recoverable error text"
    assert invalid_value in visible_error_text, (
        f"{surface.label}: visible error text did not echo invalid value {invalid_value!r}"
    )
    assert "invalid choice" in visible_error_text, (
        f"{surface.label}: visible error text did not explain the enum failure"
    )

    recovered_value = surface.valid_options[0]
    steps_used += 1
    assert any(option in visible_error_text for option in surface.valid_options), (
        f"{surface.label}: visible error text omitted the valid options needed for recovery"
    )

    parser._check_value(action, recovered_value)
    steps_used += 1
    assert steps_used <= RECOVERY_STEP_BUDGET, (
        f"{surface.label}: recovery used {steps_used} steps (budget {RECOVERY_STEP_BUDGET})"
    )


def _sampled_surfaces() -> tuple[EnumSurface, ...]:
    inventory = _inventory()
    selected: list[EnumSurface] = []
    seen_packs: set[str] = set()

    for pack in SAMPLED_PACK_ORDER:
        surfaces = inventory.applicable_by_pack.get(pack)
        if not surfaces:
            continue
        selected.append(surfaces[0])
        seen_packs.add(pack)

    if len(selected) < 5:
        for pack, surfaces in inventory.applicable_by_pack.items():
            if pack in seen_packs:
                continue
            selected.append(surfaces[0])
            if len(selected) == 5:
                break

    assert 3 <= len(selected) <= 5, (
        "sampled pack recoverability harness must cover a representative 3-5 pack surfaces; "
        f"selected {len(selected)}"
    )
    return tuple(selected)


def test_pack_enum_recoverability_inventory_marks_not_applicable_packs() -> None:
    inventory = _inventory()
    assert inventory.not_applicable_by_pack, "expected at least one pack with no enum surfaces"
    for pack, reason in inventory.not_applicable_by_pack.items():
        assert pack not in inventory.applicable_by_pack
        assert "zero enum-choice actions" in reason


def test_pack_enum_recoverability_sampled_surfaces_recover_within_budget() -> None:
    sampled = _sampled_surfaces()
    for surface in sampled:
        _exercise_recovery(surface)


@pytest.mark.opt_in
def test_pack_enum_recoverability_exhaustive_surfaces_recover_within_budget() -> None:
    inventory = _inventory()
    applicable = [
        surface
        for surfaces in inventory.applicable_by_pack.values()
        for surface in surfaces
    ]
    assert applicable, "scanner did not find any applicable pack enum surfaces"
    for surface in applicable:
        _exercise_recovery(surface)
