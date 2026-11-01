#!/usr/bin/env python3
"""Generate the Remotion element registry from discovered Astrid packs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from astrid.core.element.registry import ElementRegistry, load_default_registry
from astrid.core.element.schema import ELEMENT_MANIFEST_NAMES, ElementDefinition
from astrid.core.env_vars import ASTRID_NODE_EXECUTABLE, ASTRID_REMOTION_PROJECT_DIR
from astrid.core.foundation.paths import REPO_ROOT

TOOLS_DIR = REPO_ROOT

# Executable host maps live beside the host entry point, never in node_modules.
HOST_SRC = (Path(os.environ.get(ASTRID_REMOTION_PROJECT_DIR) or TOOLS_DIR / "remotion")
            / "src")
OUTPUTS = {
    kind: HOST_SRC / f"{kind}.generated.ts"
    for kind in ("effects", "animations", "transitions")
}

ElementKind = Literal["effects", "animations", "transitions"]
REGISTRY_STATE_VERSION = 1

_PUBLIC_REGISTRY_NODE_SCRIPT = r'''
import {DEFAULT_THEME} from '@banodoco/timeline-composition';
import {resolveElementRegistries} from '@banodoco/timeline-composition/registries';

const registries = resolveElementRegistries(DEFAULT_THEME);
console.log(JSON.stringify({
  effects: Object.keys(registries.effects ?? {}),
  animations: Object.keys(registries.animations ?? {}),
  transitions: Object.keys(registries.transitions ?? {}),
}));
'''
_PUBLIC_REGISTRY_CACHE: dict[tuple[str, str], dict[str, frozenset[str]]] = {}


@dataclass(frozen=True)
class PluginRecord:
    plugin_id: str
    kind: ElementKind
    scope: str
    root: Path
    meta: dict[str, Any]
    defaults: dict[str, Any]
    fingerprint: str
    import_scope: str | None = None


EffectRecord = PluginRecord


def _component_name(plugin_id: str) -> str:
    return "".join(part.capitalize() for part in plugin_id.split("-"))


def _validate_kind(kind: str) -> None:
    if kind not in {"effects", "animations", "transitions"}:
        raise ValueError(f"Invalid element kind {kind!r}")


def _singular(kind: ElementKind) -> str:
    return kind[:-1]


def _constant_prefix(kind: ElementKind) -> str:
    return _singular(kind).upper()


def _type_name(kind: ElementKind) -> str:
    return f"{_singular(kind).capitalize()}Id"


def _registry_name(kind: ElementKind) -> str:
    return f"{_constant_prefix(kind)}_REGISTRY"


def _ids_name(kind: ElementKind) -> str:
    return f"{_constant_prefix(kind)}_IDS"


def discover_plugins(
    kind: ElementKind,
    *,
    include_local: bool = True,
    registry: ElementRegistry | None = None,
) -> dict[str, PluginRecord]:
    _validate_kind(kind)
    registry = registry if registry is not None else _element_registry()
    plugins: dict[str, PluginRecord] = {}
    if include_local:
        elements = registry.list(kind=kind)
    else:
        elements = tuple(
            element for element in registry.list(kind=kind)
            if element.source != "pack:local"
        )
    for element in elements:
        # The public Composition package owns the bundled wrapper
        # implementations. Their descriptors remain in the normal element
        # registry so authored defaults, metadata and validation stay
        # discoverable, but a source-less public descriptor must not produce a
        # host import for a file that was retired.
        if _component_is_host_importable(element):
            plugins[element.id] = _plugin_from_element(element)
        elif element.id in _public_registry_ids()[element.kind]:
            plugins[element.id] = _plugin_from_element(element)
    for element in registry.list(kind=kind, include_shadowed=True):
        key = element.metadata.get("render_key")
        if key:
            plugins[key] = _plugin_from_element(element)
    return plugins


def _component_is_host_importable(element: ElementDefinition) -> bool:
    if element.component.is_file():
        return True
    if element.source != "pack:rendering":
        raise ValueError(
            f"element {element.kind}/{element.id} from {element.source!r} has no "
            "component.tsx; refusing to hide a broken custom component"
        )
    if element.id in _public_registry_ids()[element.kind]:
        return False
    raise ValueError(
        f"broken rendering component {element.kind}/{element.id} from "
        f"{element.source!r}: no component.tsx and the installed public "
        "Composition registry does not supply it"
    )


def _public_registry_ids() -> dict[str, frozenset[str]]:
    """Return IDs exposed by the installed Composition registry.

    The generated host map must not duplicate the public package's component
    implementations. Membership comes from Composition's existing public
    registry resolver rather than a copied list of bundled IDs.
    """
    project = Path(
        os.environ.get(ASTRID_REMOTION_PROJECT_DIR) or TOOLS_DIR / "remotion"
    ).resolve()
    package = (project / "node_modules" / "@banodoco" / "timeline-composition").resolve()
    manifest = package / "package.json"
    if not manifest.is_file():
        raise ValueError(
            "cannot verify missing rendering components: installed public "
            f"Composition package is missing at {manifest}"
        )
    node = os.environ.get(ASTRID_NODE_EXECUTABLE) or shutil.which("node")
    if not node:
        raise ValueError(
            "cannot verify missing rendering components: no Node executable "
            "is available for Composition's public registry API"
        )
    node_path = str(Path(node).resolve())
    cache_key = (str(project), node_path)
    cached = _PUBLIC_REGISTRY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            [node_path, "--input-type=module", "-e", _PUBLIC_REGISTRY_NODE_SCRIPT],
            cwd=project,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(
            "cannot verify missing rendering components through the installed "
            f"public Composition registry API: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        reason = detail[-1] if detail else f"Node exited {result.returncode}"
        raise ValueError(
            "cannot verify missing rendering components through the installed "
            f"public Composition registry API: {reason}"
        )
    try:
        raw = json.loads(result.stdout)
        membership = {
            kind: frozenset(str(item) for item in raw[kind])
            for kind in ("effects", "animations", "transitions")
            if isinstance(raw, dict) and isinstance(raw.get(kind), list)
        }
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(
            "installed public Composition registry API returned invalid "
            f"membership: {result.stdout!r}"
        ) from exc
    if set(membership) != {"effects", "animations", "transitions"}:
        raise ValueError(
            "installed public Composition registry API returned incomplete "
            f"membership: {result.stdout!r}"
        )
    _PUBLIC_REGISTRY_CACHE[cache_key] = membership
    return membership


def discover_effects(
    *,
    include_local: bool = True,
) -> dict[str, EffectRecord]:
    return discover_plugins("effects", include_local=include_local)


def discover_animations(
    *,
    include_local: bool = True,
) -> dict[str, PluginRecord]:
    return discover_plugins("animations", include_local=include_local)


def discover_transitions(
    *,
    include_local: bool = True,
) -> dict[str, PluginRecord]:
    return discover_plugins("transitions", include_local=include_local)


def _import_path(plugin: PluginRecord) -> str:
    scope = plugin.import_scope or plugin.scope
    return f"@{scope}-{plugin.kind}/{plugin.plugin_id}/component?astrid={plugin.fingerprint[:12]}"


def _element_registry() -> ElementRegistry:
    return load_default_registry(project_root=TOOLS_DIR)


def _plugin_from_element(element: ElementDefinition) -> PluginRecord:
    return PluginRecord(
        plugin_id=element.id,
        kind=element.kind,
        scope=element.source,
        root=element.root,
        meta=element.metadata,
        defaults=element.defaults,
        fingerprint=_fingerprint_element(element),
        import_scope=_import_scope_for_element(element),
    )


def _fingerprint_element(element: ElementDefinition) -> str:
    root = element.root.resolve()
    files: dict[str, Path] = {}

    manifest_path = _manifest_path(root)
    if manifest_path is not None:
        files[_fingerprint_key("manifest", manifest_path.relative_to(root))] = manifest_path

    component_path = element.component
    if not component_path.is_absolute():
        component_path = root / component_path
    if component_path.is_file():
        files[_fingerprint_key("component", component_path.resolve().relative_to(root))] = component_path.resolve()

    for suffix in (".ts", ".tsx", ".css"):
        for support_path in root.rglob(f"*{suffix}"):
            if support_path.is_file():
                files[_fingerprint_key("support", support_path.resolve().relative_to(root))] = support_path.resolve()

    for support_path in element.support_files:
        files[_fingerprint_key("support", Path(os.path.relpath(support_path, root)))] = support_path

    for asset in element.assets:
        asset_path = (root / asset.path).resolve()
        if asset_path.is_file():
            files[_fingerprint_key(f"asset:{asset.name}", asset.path)] = asset_path

    digest = hashlib.sha256()
    digest.update(b"astrid-element-fingerprint-v1\n")
    digest.update(f"{element.kind}/{element.id}\n".encode("utf-8"))
    for key in sorted(files):
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[key].read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _manifest_path(root: Path) -> Path | None:
    for name in ELEMENT_MANIFEST_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate.resolve()
    return None


def _fingerprint_key(role: str, path: Path) -> str:
    return f"{role}:{path.as_posix()}"


def _import_scope_for_element(element: ElementDefinition) -> str:
    if element.metadata.get("render_import_scope"):
        return element.metadata["render_import_scope"]
    if element.source == "overrides":
        return "override-elements"
    if element.source == "managed":
        return "managed-elements"
    if element.source.startswith("pack:"):
        return f"pack-{element.source.split(':', 1)[1]}-elements"
    return "managed-elements"


def _clip_type_aliases(effects: dict[str, EffectRecord]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    if "text-card" in effects:
        aliases["text"] = "text-card"
    for effect_id, effect in effects.items():
        raw_aliases = effect.meta.get("clipTypeAliases")
        if isinstance(raw_aliases, list):
            for alias in raw_aliases:
                if isinstance(alias, str) and alias:
                    aliases[alias] = effect_id
    return dict(sorted(aliases.items()))


def _ts_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _ts_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _ts_property_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", value) else _ts_string(value)


def _write_generated_registry(path: Path, content: str) -> bool:
    if "node_modules" in path.resolve().parts:
        raise ValueError(f"host registry output must be outside installed packages: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(content.encode("utf-8"))
    except PermissionError:
        return False
    return True


def compute_generated_registry_state(
    *,
    include_local: bool = True,
    registry: ElementRegistry | None = None,
) -> dict[str, Any]:
    """Return a deterministic fingerprint of the generated registry sources."""
    generated = {
        kind: generate_element_registry(kind, include_local=include_local, **({"registry": registry} if registry is not None else {}))
        for kind in sorted(OUTPUTS)
    }
    digest = hashlib.sha256()
    digest.update(f"astrid-registry-state-v{REGISTRY_STATE_VERSION}\n".encode("utf-8"))
    content_hashes: dict[str, str] = {}
    for kind, content in generated.items():
        content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        content_hashes[kind] = content_digest
        digest.update(kind.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_digest.encode("utf-8"))
        digest.update(b"\0")
    return {
        "version": REGISTRY_STATE_VERSION,
        "hash": digest.hexdigest(),
        "content_hashes": content_hashes,
    }


def generate(*, include_local: bool = True) -> str:
    return generate_element_registry("effects", include_local=include_local)


def generate_element_registry(
    kind: ElementKind,
    *,
    include_local: bool = True,
    registry: ElementRegistry | None = None,
) -> str:
    _validate_kind(kind)
    if kind == "effects":
        return _generate_effect_registry(include_local=include_local, registry=registry)

    component_type = "AnimationComponent" if kind == "animations" else "TransitionComponent"
    meta_type = "AnimationMeta" if kind == "animations" else "Record<string, unknown>"
    plugins = discover_plugins(kind, include_local=include_local, registry=registry)
    plugin_ids = sorted(plugins)
    public_plugins = {
        plugin_id: plugin
        for plugin_id, plugin in plugins.items()
        if plugin.scope == "pack:rendering"
        and not plugin.root.joinpath("component.tsx").is_file()
    }
    public_registry_name = f"PUBLIC_{_constant_prefix(kind)}_REGISTRY"
    imports = [
        f"import {_component_name(plugin_id)} from '{_import_path(plugins[plugin_id])}';"
        for plugin_id in plugin_ids
        if plugin_id not in public_plugins
    ]
    registry_entries = [
        f"  '{plugin_id}': "
        f"{public_registry_name}[{_ts_string(plugin_id)}],"
        if plugin_id in public_plugins
        else f"  '{plugin_id}': {_component_name(plugin_id)},"
        for plugin_id in plugin_ids
    ]
    defaults = {plugin_id: plugins[plugin_id].defaults for plugin_id in plugin_ids}
    if kind == "transitions":
        # Catalog defaults still own bundled transitions whose implementations
        # moved to Composition. Omitting their imports must not drop defaults.
        defaults = {
            element.id: element.defaults
            for element in (registry if registry is not None else _element_registry()).list(kind=kind)
            if include_local or element.source != "pack:local"
        }
        defaults.update({key: plugin.defaults for key, plugin in plugins.items()
                         if key not in defaults})
    defaults_entries = [
        f"  '{plugin_id}': {_ts_json(value)},"
        for plugin_id, value in sorted(defaults.items())
    ]
    meta_entries = [
        f"  '{plugin_id}': {_ts_json(plugins[plugin_id].meta)},"
        for plugin_id in plugin_ids
    ]
    fingerprint_entries = [
        f"  '{plugin_id}': {_ts_string(plugins[plugin_id].fingerprint)},"
        for plugin_id in plugin_ids
    ]
    ids = ", ".join(_ts_string(plugin_id) for plugin_id in plugin_ids)
    ids_name = _ids_name(kind)
    type_name = _type_name(kind)
    registry_name = _registry_name(kind)
    defaults_name = f"{_constant_prefix(kind)}_DEFAULTS"
    meta_name = f"{_constant_prefix(kind)}_META"
    fingerprints_name = f"{_constant_prefix(kind)}_FINGERPRINTS"
    blocks = [
        "// DO NOT EDIT - generated by tools/scripts/gen_effect_registry.py",
        f"import type {{{component_type}, {meta_type}}} from '@banodoco/timeline-composition/theme-api';"
        if kind == "animations"
        else f"import type {{{component_type}}} from '@banodoco/timeline-composition/theme-api';",
        *(
            [
                "import {DEFAULT_THEME} from '@banodoco/timeline-composition/theme-api';",
                "import {resolveElementRegistries} from '@banodoco/timeline-composition/registries';",
                "",
                f"const {public_registry_name} = resolveElementRegistries(DEFAULT_THEME).{kind};",
            ]
            if public_plugins
            else []
        ),
        *imports,
        "",
        f"export const {ids_name} = [{ids}] as const;",
        f"export type {type_name} = typeof {ids_name}[number];",
        f"export const {registry_name}: Record<{type_name}, {component_type}> = {{",
        *registry_entries,
        "};",
        f"export const {defaults_name}: Record<{'string' if kind == 'transitions' else type_name}, Record<string, unknown>> = {{",
        *defaults_entries,
        "};",
        f"export const {meta_name}: Record<{type_name}, {meta_type}> = {{",
        *meta_entries,
        "};",
        f"export const {fingerprints_name}: Record<{type_name}, string> = {{",
        *fingerprint_entries,
        "};",
        "",
    ]
    return "\n".join(blocks)


def _generate_effect_registry(
    *,
    include_local: bool = True,
    registry: ElementRegistry | None = None,
) -> str:
    registry = registry if registry is not None else _element_registry()
    effects = discover_plugins("effects", include_local=include_local, registry=registry)
    all_elements = registry.list(kind="effects", include_shadowed=True)

    # A source-less public winner is still the canonical registry entry.  If a
    # valid shadow exists, retain that winner in the host map so the existing
    # owner selector can route the qualified local reference without changing
    # canonical registry precedence.
    for element in all_elements:
        if element.id in effects:
            continue
        if not include_local and element.source == "pack:local":
            continue
        winner = registry.get("effects", element.id)
        if (
            winner.source == "pack:rendering"
            and not winner.component.is_file()
            and element.id in _public_registry_ids()["effects"]
        ):
            _component_is_host_importable(element)
            effects[element.id] = _plugin_from_element(winner)

    effect_ids = sorted(effects)
    public_effects = {
        effect_id: effect
        for effect_id, effect in effects.items()
        if effect.scope == "pack:rendering"
        and not effect.root.joinpath("component.tsx").is_file()
    }
    imports = [
        f"import {_component_name(effect_id)} from '{_import_path(effects[effect_id])}';"
        for effect_id in effect_ids
        if effect_id not in public_effects
    ]
    public_symbols = {
        effect_id: _component_name(effect_id)
        for effect_id in public_effects
    }
    winner_symbols = {
        effect_id: (
            public_symbols[effect_id]
            if effect_id in public_effects and not include_local
            else f"PUBLIC_EFFECT_REGISTRY[{_ts_string(effect_id)}] as EffectComponent"
            if effect_id in public_effects
            else _component_name(effect_id)
        )
        for effect_id in effect_ids
    }
    # Keep public IDs and the unqualified winner. Components receive the
    # existing editor ref through their clip props, so no composition schema
    # or second dispatch identity is needed for retained owner candidates.
    owners: dict[str, dict[str, str]] = {effect_id: {} for effect_id in effect_ids}
    pinned_owners = False
    for effect_id, effect in public_effects.items():
        pack_id = effect.meta.get("pack_id") or effect.scope.removeprefix("pack:")
        owners[effect_id][pack_id] = (
            winner_symbols[effect_id]
            if not include_local
            else f"PUBLIC_EFFECT_REGISTRY[{_ts_string(effect_id)}] as EffectComponent"
        )
    for element in all_elements:
        if not include_local and element.source == "pack:local":
            continue
        if element.id not in effects:
            continue
        if not _component_is_host_importable(element):
            # A source-less public descriptor is supplied by the installed
            # Composition registry.  It is not a host import and must not be
            # treated as an owner candidate for a custom winner.
            continue
        pack_id = ElementRegistry._pack_id(element)
        plugin = _plugin_from_element(element)
        if plugin.root == effects[element.id].root:
            symbol = _component_name(element.id)
        else:
            symbol = f"OwnerElement{len(imports)}"
            imports.append(f"import {symbol} from '{_import_path(plugin)}';")
        revision = element.metadata.get("render_revision")
        pinned_owners = pinned_owners or bool(revision)
        owner_key = f"{pack_id}@{revision}" if revision else pack_id
        owners[element.id].setdefault(owner_key, symbol)
    registry_entries = [
        f"  {_ts_string(effect_id)}: selectOwnedEffect({_ts_string(effect_id)}, "
        f"{winner_symbols[effect_id]}, {{{', '.join(_ts_string(owner) + ': ' + symbol for owner, symbol in sorted(owners[effect_id].items()))}}}),"
        for effect_id in effect_ids
    ]
    fingerprint_entries = [
        f"  '{effect_id}': {_ts_string(effects[effect_id].fingerprint)},"
        for effect_id in effect_ids
    ]
    ids = ", ".join(_ts_string(effect_id) for effect_id in effect_ids)
    aliases = _clip_type_aliases(effects)
    alias_entries = [
        f"  {_ts_property_key(alias)}: {_ts_string(effect_id)},"
        for alias, effect_id in aliases.items()
    ]
    blocks = [
        "// DO NOT EDIT - generated by tools/scripts/gen_effect_registry.py",
        "import type {EffectComponent} from '@banodoco/timeline-composition/theme-api';",
        "import {createElement} from 'react';",
        "",
        "import type {SceneCapableComponent, ComponentCapabilityContext} from '@banodoco/timeline-composition/evaluation';",
        *(
            [
                "import {DEFAULT_THEME} from '@banodoco/timeline-composition/theme-api';",
                "import {resolveElementRegistries} from '@banodoco/timeline-composition/registries';",
                "",
                "const PUBLIC_EFFECT_REGISTRY = resolveElementRegistries(DEFAULT_THEME).effects;",
                *(
                    f"const {public_symbols[effect_id]} = PUBLIC_EFFECT_REGISTRY[{_ts_string(effect_id)}] as EffectComponent;"
                    for effect_id in sorted(public_effects)
                ),
            ]
            if public_effects
            else []
        ),
        "",
        "function selectOwnedEffect(id: string, winner: EffectComponent, owners: Record<string, EffectComponent>): EffectComponent {",
        *(
            [
                "  const select = (clip: {elementRef?: {id: string; kind: string; packId?: string; revision?: string}}): EffectComponent & SceneCapableComponent => {",
                "    const ref = clip.elementRef;",
                "    if (!ref || ref.kind !== 'effect' || ref.id !== id || !('packId' in ref)) return winner;",
                "    const pinnedOwner = `${ref.packId}@${ref.revision}`;",
                "    const pinned = Object.keys(owners).some((key) => key.startsWith(`${ref.packId}@`));",
                "    const owner = pinned ? pinnedOwner : ref.packId;",
                "    const component = typeof owner === 'string' && Object.prototype.hasOwnProperty.call(owners, owner) ? owners[owner] : undefined;",
            ]
            if pinned_owners
            else [
                "  const select = (clip: {elementRef?: {id: string; kind: string; packId?: string}}): EffectComponent & SceneCapableComponent => {",
                "    const ref = clip.elementRef;",
                "    if (!ref || ref.kind !== 'effect' || ref.id !== id || !('packId' in ref)) return winner;",
                "    const component = typeof ref.packId === 'string' && Object.prototype.hasOwnProperty.call(owners, ref.packId) ? owners[ref.packId] : undefined;",
            ]
        ),
        "    if (!component) throw new Error(`Unknown effect owner ${String(ref.packId)} for ${id}`);",
        "    return component;",
        "  };",
        "  return Object.assign((props: Parameters<EffectComponent>[0]) => createElement(select(props.clip), props), {",
        "    prepareSceneCapability: (context: ComponentCapabilityContext) => select(context.clip).prepareSceneCapability?.(context),",
        "  });",
        "}",
        *imports,
        "",
        f"export const EFFECT_IDS = [{ids}] as const;",
        "export type EffectId = typeof EFFECT_IDS[number];",
        "export const EFFECT_REGISTRY: Record<EffectId, EffectComponent> = {",
        *registry_entries,
        "};",
        "export const CLIP_TYPE_ALIASES: Record<string, EffectId> = {",
        *alias_entries,
        "};",
        "export const EFFECT_FINGERPRINTS: Record<EffectId, string> = {",
        *fingerprint_entries,
        "};",
        "",
    ]
    return "\n".join(blocks)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate Remotion effect registry.")
    parser.add_argument(
        "--bundled-only",
        action="store_true",
        help="exclude ignored/local pack overrides when generating canonical bundled registries",
    )
    return parser


def _main_unlocked(argv: list[str] | None = None) -> int:
    """Write registry artifacts while the caller owns the Remotion lock."""

    args = build_parser().parse_args(argv)
    failed_outputs: list[Path] = []
    for kind, output in OUTPUTS.items():
        content = generate_element_registry(
            kind,
            include_local=not args.bundled_only,
        )
        if not _write_generated_registry(output, content):
            failed_outputs.append(output)
    if failed_outputs:
        formatted = "\n".join(f"  - {path}" for path in failed_outputs)
        print(
            "ERROR failed to write host generated registries:\n"
            f"{formatted}\n"
            "Generated registry outputs must be writable.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    from astrid.packs.rendering.rendering.renderers.remotion import lock as remotion_lock

    if remotion_lock.remotion_render_lock_held():
        result = _main_unlocked(argv)
        token = os.environ.get(remotion_lock.REMOTION_LOCK_TOKEN_ENV)
        if result == 0 and token is not None:
            print(remotion_lock.REMOTION_LOCK_RECEIPT_PREFIX + token)
        return result
    with remotion_lock.remotion_render_lock():
        return _main_unlocked(argv)


if __name__ == "__main__":
    raise SystemExit(main())
