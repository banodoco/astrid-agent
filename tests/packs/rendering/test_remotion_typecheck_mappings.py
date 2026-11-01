"""Every module scope the registry generator emits must typecheck.

``scripts/gen_effect_registry.py`` writes ``@<scope>-<kind>/<id>/component?astrid=<hash>``
imports into the installed timeline-composition package, one scope per element
source (for packs ``@pack-<pack id>-elements-<kind>``). ``tsc`` resolves those
specifiers only through ``remotion/src/astrid-hashed-imports.d.ts`` and the
``paths`` of ``remotion/tsconfig.json``, so a pack the generator can emit but
those two files do not name fails ``npm run typecheck`` with TS2307 after any
real render has regenerated the registries.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts import gen_effect_registry

ROOT = Path(__file__).resolve().parents[3]
REMOTION = ROOT / "remotion"
KINDS: tuple[gen_effect_registry.ElementKind, ...] = ("effects", "animations", "transitions")
_SCOPE = re.compile(r"""from '(@[^/']+)/""")
_DECLARED = re.compile(r"""declare module '(@[^/']+)/\*'""")


def emitted_scopes(generated: str) -> set[str]:
    return set(_SCOPE.findall(generated))


def declared_scopes(declarations: str) -> set[str]:
    return set(_DECLARED.findall(declarations))


def tsconfig_paths(tsconfig_text: str) -> dict[str, list[str]]:
    without_comments = re.sub(r"^\s*//.*$", "", tsconfig_text, flags=re.MULTILINE)
    paths = json.loads(without_comments)["compilerOptions"]["paths"]
    return {key[: -len("/*")]: targets for key, targets in paths.items() if key.endswith("/*")}


def unmapped(emitted: set[str], declarations: str, tsconfig_text: str) -> list[str]:
    declared = declared_scopes(declarations)
    mapped = set(tsconfig_paths(tsconfig_text))
    return sorted(
        f"{scope}: {', '.join(missing)}"
        for scope in emitted
        if (missing := [
            where
            for where, names in (("astrid-hashed-imports.d.ts", declared), ("tsconfig.json paths", mapped))
            if scope not in names
        ])
    )


def test_unmapped_names_a_scope_missing_from_either_file() -> None:
    declarations = "declare module '@pack-a-elements-effects/*' {}\ndeclare module '@pack-b-elements-effects/*' {}\n"
    tsconfig = '{"compilerOptions": {"paths": {\n// comment\n"@pack-a-elements-effects/*": ["x/*"]}}}'
    assert unmapped({"@pack-a-elements-effects"}, declarations, tsconfig) == []
    assert unmapped({"@pack-b-elements-effects"}, declarations, tsconfig) == [
        "@pack-b-elements-effects: tsconfig.json paths"
    ]
    assert unmapped({"@pack-c-elements-effects"}, declarations, tsconfig) == [
        "@pack-c-elements-effects: astrid-hashed-imports.d.ts, tsconfig.json paths"
    ]


@pytest.mark.parametrize("kind", KINDS)
def test_every_scope_the_generator_emits_is_declared_and_mapped(kind: gen_effect_registry.ElementKind) -> None:
    emitted = emitted_scopes(gen_effect_registry.generate_element_registry(kind))
    assert emitted, f"the generator emitted no {kind} imports; the check would pass vacuously"
    declarations = (REMOTION / "src" / "astrid-hashed-imports.d.ts").read_text(encoding="utf-8")
    tsconfig = (REMOTION / "tsconfig.json").read_text(encoding="utf-8")
    assert unmapped(emitted, declarations, tsconfig) == []


def test_pack_scope_paths_point_at_the_pack_element_directories() -> None:
    paths = tsconfig_paths((REMOTION / "tsconfig.json").read_text(encoding="utf-8"))
    registry = gen_effect_registry._element_registry()
    checked = 0
    for kind in KINDS:
        for element in registry.list(kind=kind, include_shadowed=True):
            if not element.source.startswith("pack:"):
                continue
            scope = gen_effect_registry._import_scope_for_element(element)
            targets = paths[f"@{scope}-{kind}"]
            assert [(REMOTION / target).resolve() for target in targets] == [
                (element.root.parent / "*").resolve()
            ], f"{scope}-{kind} must map to {element.root.parent}"
            checked += 1
    assert checked
