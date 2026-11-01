"""Agent-facing pack index: deterministic, machine-readable pack summary.

``build_agent_index()`` assembles a JSON-serializable dict that describes every
manifest-ledger pack so agents can inspect
available packs and choose the right entrypoint without reading every manifest
or doc file themselves.

No LLM calls, no heuristics — purely deterministic assembly from structured
manifest fields, component metadata, doc paths, and bounded STAGE.md excerpts.

**Resolution strategy:**

1.  **Primary — pack-system canonical resolver** (``discover_packs()``):
    Enumerates built-in packs via the pack-system's canonical discovery
    mechanism (``packs_root()`` → ``discover_packs()``).  This is the
    authoritative source and always runs first.

External roots are supplied explicitly through the pack discovery API.
"""

from __future__ import annotations

import json as _json
import re as _re
from pathlib import Path
from typing import Any

import yaml

from astrid.core.pack import (
    EXECUTOR_MANIFEST_NAMES,
    ORCHESTRATOR_MANIFEST_NAMES,
    discover_packs,
    pack_manifest_path,
    packs_root,
)

# ---------------------------------------------------------------------------
# STAGE.md excerpt helpers
# ---------------------------------------------------------------------------

_STAGE_HEADING_RE = _re.compile(r"^##\s")


def _read_stage_excerpt(stage_path: Path, *, max_lines: int = 30) -> str | None:
    """Return a bounded excerpt from a STAGE.md file.

    Reads at most *max_lines* lines, stopping early at the first ``##``
    heading (ATX level-2).  Returns ``None`` when the file cannot be read.
    """
    if not stage_path.is_file():
        return None
    try:
        text = stage_path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    excerpt_lines: list[str] = []
    for i, line in enumerate(lines):
        if i >= max_lines:
            break
        if _STAGE_HEADING_RE.match(line) and i > 0:
            break
        excerpt_lines.append(line)
    return "\n".join(excerpt_lines).strip() or None


# ---------------------------------------------------------------------------
# Component manifest scanning
# ---------------------------------------------------------------------------

from astrid.core.pack._common import ELEMENT_MANIFEST_NAMES

# Recognised manifest filenames keyed by kind.
_COMPONENT_MANIFEST_NAMES: dict[str, tuple[str, ...]] = {
    "executor": EXECUTOR_MANIFEST_NAMES,
    "orchestrator": ORCHESTRATOR_MANIFEST_NAMES,
    "element": ELEMENT_MANIFEST_NAMES,
}


def _find_manifest(comp_dir: Path, kind: str) -> Path | None:
    """Return the first manifest file found in *comp_dir* for *kind*."""
    names = _COMPONENT_MANIFEST_NAMES.get(kind, ())
    for name in sorted(names):
        candidate = comp_dir / name
        if candidate.is_file():
            return candidate
    return None


def _load_yaml_or_json(path: Path) -> dict[str, Any] | None:
    """Load a YAML or JSON file.  Returns ``None`` on any error."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if path.suffix == ".json":
        try:
            data = _json.loads(text)
        except Exception:
            return None
    else:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            return None
    if not isinstance(data, dict):
        return None
    return data


def _scan_components(root: Path, content: dict[str, Any]) -> list[dict[str, Any]]:
    """Scan component manifests under declared content roots.

    Returns a deterministic (sorted by *id*) list of component overview dicts.
    Each dict includes: id, name, kind, description, runtime, is_entrypoint,
    docs_paths, stage_excerpt.
    """
    components: list[dict[str, Any]] = []

    for comp_kind in ("executors", "orchestrators"):
        comp_root_rel = content.get(comp_kind)
        if not isinstance(comp_root_rel, str) or not comp_root_rel.strip():
            continue
        comp_root = root / comp_root_rel
        if not comp_root.is_dir():
            continue

        manifest_kind = comp_kind.rstrip("s")  # "executors" -> "executor"

        for comp_dir in sorted(comp_root.iterdir()):
            if not comp_dir.is_dir() or comp_dir.name.startswith("."):
                continue
            if comp_dir.name == "__pycache__":
                continue

            manifest_path = _find_manifest(comp_dir, manifest_kind)
            if manifest_path is None:
                continue
            data = _load_yaml_or_json(manifest_path)
            if data is None:
                continue

            comp_id = data.get("id", comp_dir.name)
            name = data.get("name", comp_id)
            description = data.get("description", "")
            kind = data.get("kind", manifest_kind)

            # Runtime info
            runtime = data.get("runtime", {}) if isinstance(data.get("runtime"), dict) else {}
            runtime_info: dict[str, Any] | None = None
            if runtime:
                runtime_info = {
                    "type": runtime.get("type"),
                    "entrypoint": runtime.get("entrypoint"),
                    "callable": runtime.get("callable"),
                }

            # Is this component an entrypoint?
            is_entrypoint = False  # determined later via normal_entrypoints/entrypoints comparison

            # Docs paths
            docs = data.get("docs", {}) if isinstance(data.get("docs"), dict) else {}
            docs_paths: dict[str, str] = {}
            stage_rel = docs.get("stage", "STAGE.md")
            docs_paths["stage"] = str(comp_dir / stage_rel)

            # Stage excerpt
            stage_path = comp_dir / stage_rel
            stage_excerpt = _read_stage_excerpt(stage_path)

            components.append({
                "id": str(comp_id),
                "name": str(name),
                "kind": str(kind),
                "description": str(description) if description else "",
                "runtime": runtime_info,
                "is_entrypoint": is_entrypoint,
                "docs_paths": docs_paths,
                "stage_excerpt": stage_excerpt,
            })

    # Elements: two-level structure — elements/<kind>/<element_name>/
    elements_root_rel = content.get("elements")
    if isinstance(elements_root_rel, str) and elements_root_rel.strip():
        elements_root = root / elements_root_rel
        if elements_root.is_dir():
            for kind_dir in sorted(elements_root.iterdir()):
                if not kind_dir.is_dir() or kind_dir.name.startswith("."):
                    continue
                if kind_dir.name == "__pycache__":
                    continue

                for elem_dir in sorted(kind_dir.iterdir()):
                    if not elem_dir.is_dir() or elem_dir.name.startswith("."):
                        continue
                    if elem_dir.name == "__pycache__":
                        continue

                    manifest_path = _find_manifest(elem_dir, "element")
                    if manifest_path is None:
                        continue
                    data = _load_yaml_or_json(manifest_path)
                    if data is None:
                        continue

                    comp_id = data.get("id", elem_dir.name)
                    name = data.get("metadata", {}).get("label", comp_id) if isinstance(data.get("metadata"), dict) else comp_id
                    description = data.get("description", "")
                    kind = data.get("kind", kind_dir.name.rstrip("s"))

                    # Elements have no runtime/entrypoint
                    runtime_info = None
                    is_entrypoint = False

                    # Docs paths
                    docs = data.get("docs", {}) if isinstance(data.get("docs"), dict) else {}
                    docs_paths: dict[str, str] = {}
                    stage_rel = docs.get("stage", "STAGE.md")
                    docs_paths["stage"] = str(elem_dir / stage_rel)

                    # Stage excerpt
                    stage_path = elem_dir / stage_rel
                    stage_excerpt = _read_stage_excerpt(stage_path)

                    components.append({
                        "id": str(comp_id),
                        "name": str(name),
                        "kind": str(kind),
                        "description": str(description) if description else "",
                        "runtime": runtime_info,
                        "is_entrypoint": is_entrypoint,
                        "docs_paths": docs_paths,
                        "stage_excerpt": stage_excerpt,
                    })

    return components


# ---------------------------------------------------------------------------
# Main index builder
# ---------------------------------------------------------------------------


def _normalize_secrets(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a structured secrets list from a pack manifest.

    The manifest schema requires the structured list form.
    """
    secrets_raw = manifest.get("secrets")
    if isinstance(secrets_raw, list):
        result: list[dict[str, Any]] = []
        for s_obj in secrets_raw:
            if isinstance(s_obj, dict) and s_obj.get("name"):
                result.append({
                    "name": str(s_obj["name"]),
                    "required": bool(s_obj.get("required", False)),
                    "description": str(s_obj.get("description", "")),
                })
        return result
    return []


def _normalize_dependencies(manifest: dict[str, Any]) -> dict[str, list[str]]:
    """Return structured dependencies as ``{python:[...], npm:[...], system:[...]}``."""
    deps_raw = manifest.get("dependencies")
    result: dict[str, list[str]] = {}
    if isinstance(deps_raw, dict):
        for eco in ("python", "npm", "system"):
            eco_deps = deps_raw.get(eco)
            if isinstance(eco_deps, list):
                result[eco] = [str(d) for d in eco_deps if d]
    return result


def build_agent_index(*, pack_id: str | None = None) -> dict[str, Any]:
    """Build a deterministic agent-facing pack index.

    Enumeration uses the pack-system's canonical manifest resolver
    (``discover_packs()``).

    Parameters:
        pack_id: When set, return only the matching pack (or ``None``).

    Returns:
        A dict with key ``"packs"`` mapping to a list of pack-summary dicts
        sorted by ``pack_id``.  Each pack dict includes:
        pack_id, name, version, description, source_type, trust_tier, purpose,
        normal_entrypoints, do_not_use_for, required_context, secrets,
        dependencies, keywords, capabilities, component_counts, components,
        docs_paths, warnings.

        When *pack_id* is given and the pack is found the result is the
        single pack dict; when not found, ``None``.
    """
    from astrid.core.pack.validate import extract_trust_summary

    # Collect packs from the source/manifest ledger only.
    pack_map: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Primary: built-in packs via pack-system canonical resolver
    # ------------------------------------------------------------------
    for pack_def in discover_packs(packs_root(), include_hidden=True):
        pid = pack_def.id
        if pack_id is not None and pid != pack_id:
            continue

        try:
            trust = extract_trust_summary(pack_def.root)
        except Exception:
            trust = {}

        manifest = _load_manifest(pack_def.root)
        pack_map[pid] = _assemble_pack_entry(
            pack_def.root, pid, manifest, trust, source_type="built-in"
        )

    # ------------------------------------------------------------------
    # Filter and sort
    # ------------------------------------------------------------------
    if pack_id is not None:
        return pack_map.get(pack_id)

    sorted_packs = [pack_map[pid] for pid in sorted(pack_map)]

    # Post-process: mark is_entrypoint on components
    for pack_entry in sorted_packs:
        normal_eps = set(pack_entry.get("normal_entrypoints", []))
        for comp in pack_entry.get("components", []):
            comp["is_entrypoint"] = comp["id"] in normal_eps

    return {"packs": sorted_packs}


def _load_manifest(root: Path) -> dict[str, Any]:
    """Load the pack manifest from *root*, returning an empty dict on failure."""
    mf_path = pack_manifest_path(root)
    if mf_path is None:
        return {}
    return _load_yaml_or_json(mf_path) or {}


def _assemble_pack_entry(
    root: Path,
    pack_id: str,
    manifest: dict[str, Any],
    trust: dict[str, Any],
    *,
    source_type: str = "built-in",
) -> dict[str, Any]:
    """Assemble a single pack entry dict for the agent index."""
    agent_section = manifest.get("agent", {}) if isinstance(manifest.get("agent"), dict) else {}
    content = manifest.get("content", {}) if isinstance(manifest.get("content"), dict) else {}

    # Purpose
    purpose = agent_section.get("purpose", manifest.get("description", ""))

    # Entrypoints
    normal_entrypoints: list[str] = []
    if isinstance(agent_section.get("normal_entrypoints"), list):
        normal_entrypoints = [str(ep) for ep in agent_section["normal_entrypoints"] if ep]
    # do_not_use_for
    do_not_use_for = str(agent_section.get("do_not_use_for", "")) or None

    # required_context
    required_context_raw = agent_section.get("required_context")
    required_context: list[str] = []
    if isinstance(required_context_raw, list):
        required_context = [str(rc) for rc in required_context_raw if rc]

    # Secrets
    secrets = _normalize_secrets(manifest)

    # Dependencies
    dependencies = _normalize_dependencies(manifest)

    # Keywords
    kw_raw = manifest.get("keywords")
    keywords: list[str] = []
    if isinstance(kw_raw, list):
        keywords = [str(k) for k in kw_raw if k]

    # Capabilities
    cap_raw = manifest.get("capabilities")
    capabilities: list[str] = []
    if isinstance(cap_raw, list):
        capabilities = [str(c) for c in cap_raw if c]

    # Component counts
    component_counts = trust.get("component_counts", {})

    # Components (scanned from declared content roots)
    components = _scan_components(root, content)
    # Sort by id for determinism
    components.sort(key=lambda c: c["id"])

    # Docs paths (at pack level)
    docs = manifest.get("docs", {}) if isinstance(manifest.get("docs"), dict) else {}
    docs_paths: dict[str, str | None] = {}
    for doc_key in ("readme", "agents", "stage"):
        val = docs.get(doc_key)
        if val:
            docs_paths[doc_key] = str(root / val)
        else:
            # Check common defaults
            default_map = {"readme": "README.md", "agents": "AGENTS.md", "stage": "STAGE.md"}
            default_path = root / default_map[doc_key]
            docs_paths[doc_key] = str(default_path) if default_path.is_file() else None

    # Warnings from trust summary
    warnings = trust.get("warnings", [])

    return {
        "pack_id": pack_id,
        "name": trust.get("name", manifest.get("name", pack_id)),
        "version": trust.get("version", manifest.get("version", "0.0.0")),
        "description": manifest.get("description", ""),
        "source_type": source_type,
        "trust_tier": trust.get("trust_tier", "built-in"),
        "purpose": str(purpose) if purpose else None,
        "normal_entrypoints": normal_entrypoints,
        "do_not_use_for": do_not_use_for,
        "required_context": required_context,
        "secrets": secrets,
        "dependencies": dependencies,
        "keywords": keywords,
        "capabilities": capabilities,
        "component_counts": component_counts,
        "components": components,
        "docs_paths": docs_paths,
        "warnings": warnings,
        # Permissions and trust metadata — sourced from extract_trust_summary()
        "permissions": trust.get("permissions", []),
        "permission_ids": trust.get("permission_ids", []),
        "trust": trust.get("trust", {}),
    }


__all__ = [
    "build_agent_index",
    # Re-exported — used by tests that mock through astrid.core.pack.agent_index.
    "_assemble_pack_entry",
]
