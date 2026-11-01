"""Element motion from the element itself, with ``model.py`` as the fallback.

An element folder may ship ``motion.ts`` exporting ``motionAt(params, clipFrame,
fps) -> {prop: number}`` (am-presenter, am-snap-plate). The component draws with
the same functions, so these values are the element's motion, not a copy of it.
visualize evaluates them once per run, for every frame of every such clip, with
the renderer's own Node and esbuild (~0.1 s). Without Node the hand mirror in
``model.py`` answers instead and ``note`` says why.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping

from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.pack.manifest import ManifestParseError, load_manifest_mapping

SCRIPT = Path(__file__).with_name("element_motion.mjs")
MOTION_FILE = "motion.ts"
TIMEOUT_S = 30.0
MAX_FRAMES = 20_000  # per clip; a longer clip falls back to the mirror

_KIND_ALIASES = {
    "animation": "animations",
    "animations": "animations",
    "effect": "effects",
    "effects": "effects",
    "transition": "transitions",
    "transitions": "transitions",
}


def _binding_hash(value: Any) -> str | None:
    """Hash the native element-resource map with the producer's exact rules."""

    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return None
    return hashlib.sha256(payload).hexdigest()


def _kind(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return _KIND_ALIASES.get(text)


def _identity(pack_id: Any, kind: Any, element_id: Any) -> str | None:
    pack = str(pack_id or "").strip()
    normalized_kind = _kind(kind)
    local_id = str(element_id or "").strip()
    if not pack or not normalized_kind or not local_id or "/" in pack or "/" in local_id:
        return None
    return f"{pack}/{normalized_kind}/{local_id}"


def canonical_element_identity(reference: Mapping[str, Any] | None) -> str | None:
    """Return the canonical identity of an admitted or authored element ref."""

    if not isinstance(reference, Mapping):
        return None
    return _identity(
        reference.get("packId", reference.get("pack_id")),
        reference.get("kind"),
        reference.get("id"),
    )


def _packs_root(root: Path | None) -> Path:
    """Return the checkout's declared pack root; never search beneath it."""
    if root is None:
        return (Path(REPO_ROOT) / "astrid" / "packs").resolve()
    candidate = Path(root).resolve()
    checkout_packs = candidate / "astrid" / "packs"
    return checkout_packs if checkout_packs.is_dir() else candidate


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _manifest_contract(path: Path) -> Mapping[str, Any] | None:
    try:
        manifest = load_manifest_mapping(path, manifest_kind="V3 element")
    except (ManifestParseError, OSError, TypeError, ValueError):
        return None
    return manifest


def _declared_contract(
    path: Path, source: str, manifest: Mapping[str, Any] | None = None,
) -> str | None:
    """Return an admitted motion contract only from an exact V3 declaration."""
    if "export function motionAt" in source or "export const motionAt" in source:
        return "motionAt"
    if manifest is None:
        manifest = _manifest_contract(path.parent / "element.yaml")
    if not isinstance(manifest, Mapping):
        return None
    schema = manifest.get("schema")
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    keyframes = properties.get("keyframes") if isinstance(properties, Mapping) else None
    keyframe_description = keyframes.get("description") if isinstance(keyframes, Mapping) else ""
    if (
        "export function transformAt" in source
        and "export function validateKeyframes" in source
        and "Clip-local seconds" in source
        and isinstance(keyframe_description, str)
        and "canvas pixels" in keyframe_description
        and manifest.get("id") == "animated-media-transform"
        and isinstance(keyframes, Mapping)
    ):
        return "transformAt_canvas_keyframes"
    return None


def _pack_declarations(packs_root: Path, pack_id: str) -> tuple[dict[str, Any], ...]:
    """Project one named V3 pack's rendering declarations and resources."""
    if not pack_id or "/" in pack_id or "\\" in pack_id:
        return ()
    pack_root = (packs_root / pack_id).resolve()
    pack_manifest_path = pack_root / "pack.yaml"
    try:
        pack = load_manifest_mapping(pack_manifest_path, manifest_kind="V3 pack")
    except (ManifestParseError, OSError, TypeError, ValueError):
        return ()
    if pack.get("schema_version") != 3 or pack.get("id") != pack_id:
        return ()
    rendering = pack.get("rendering")
    if not isinstance(rendering, Mapping):
        return ()
    declarations: list[dict[str, Any]] = []
    for public_key, raw in rendering.items():
        if not isinstance(public_key, str) or not isinstance(raw, Mapping) or raw.get("type") != "element":
            continue
        parts = public_key.split("/", 1)
        if len(parts) != 2 or _identity(pack_id, parts[0], parts[1]) is None:
            continue
        manifest_value = raw.get("path")
        resources = raw.get("resources")
        if not isinstance(manifest_value, str) or not isinstance(resources, list):
            continue
        manifest_path = (pack_root / manifest_value).resolve()
        if not _within(manifest_path, pack_root) or not manifest_path.is_file():
            continue
        declarations.append({
            "pack_id": pack_id,
            "kind": parts[0],
            "id": parts[1],
            "pack_root": str(pack_root),
            "manifest_path": str(manifest_path),
            "resources": resources,
        })
    return tuple(declarations)


def _default_declarations(packs_root: Path) -> tuple[dict[str, Any], ...]:
    """Read motion declarations only from packs admitted by the element registry."""
    from astrid.core.element.registry import load_default_registry

    if not packs_root.is_dir() or not any(
        (child / "pack.yaml").is_file() for child in packs_root.iterdir() if child.is_dir()
    ):
        return ()
    project_root = packs_root.parent.parent
    registry = load_default_registry(project_root=project_root)
    installed_roots: dict[tuple[str, Path], None] = {}
    for element in registry.list(include_shadowed=True):
        pack_id = str(element.metadata.get("pack_id") or element.source.removeprefix("pack:"))
        if not pack_id:
            continue
        pack_root = element.root.parents[3]
        installed_roots[(pack_id, pack_root)] = None
    rows: list[dict[str, Any]] = []
    for pack_id, pack_root in sorted(installed_roots, key=lambda item: (item[0], str(item[1]))):
        rows.extend(_pack_declarations(pack_root.parent, pack_id))
    return tuple(rows)


def _declaration_module(declaration: Mapping[str, Any]) -> tuple[str, Path] | None:
    """Resolve one declared resource, rejecting identity/path ambiguity."""
    identity = _identity(declaration.get("pack_id", declaration.get("packId")),
                         declaration.get("kind"), declaration.get("id"))
    pack_root_value = declaration.get("pack_root", declaration.get("root"))
    manifest_value = declaration.get("manifest_path", declaration.get("manifest"))
    resources = declaration.get("resources")
    if identity is None or not isinstance(pack_root_value, str) or not isinstance(manifest_value, str):
        return None
    pack_root = Path(pack_root_value).expanduser().resolve()
    manifest_path = Path(manifest_value).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = (pack_root / manifest_path).resolve()
    else:
        manifest_path = manifest_path.resolve()
    if (
        pack_root.name != identity.split("/", 1)[0]
        or not _within(manifest_path, pack_root)
        or not manifest_path.is_file()
        or not isinstance(resources, list)
    ):
        return None
    manifest = _manifest_contract(manifest_path)
    if not isinstance(manifest, Mapping):
        return None
    pack_id, kind_and_id = identity.split("/", 1)
    declared_kind, declared_id = kind_and_id.split("/", 1)
    if (
        manifest.get("id") != declared_id
        or _kind(manifest.get("kind")) != declared_kind
        or str(manifest.get("pack_id") or "") != pack_id
        or manifest.get("schema_version") != 1
        or manifest_path.parent.resolve() != (pack_root / "rendering" / "elements" / declared_kind / declared_id).resolve()
    ):
        return None
    candidate: Path | None = None
    for resource in resources:
        if (
            not isinstance(resource, Mapping)
            or not isinstance(resource.get("kind"), str)
            or not isinstance(resource.get("path"), str)
        ):
            return None
        resource_path = (pack_root / resource["path"]).resolve()
        if Path(resource["path"]).is_absolute() or not _within(resource_path, pack_root):
            return None
        if resource_path.name != MOTION_FILE:
            continue
        if resource["kind"] not in {"implementation", "support"}:
            return None
        if resource_path.parent != manifest_path.parent.resolve() or not resource_path.is_file():
            return None
        candidate = resource_path
    if candidate is None:
        return None
    try:
        source = candidate.read_text(encoding="utf-8")
    except OSError:
        return None
    if _declared_contract(candidate, source, manifest) is None:
        return None
    return identity, candidate


def _digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(char in "0123456789abcdef" for char in value)
    )


def _validated_binding(clip_id: str, binding: Any) -> dict[str, Any] | None:
    """Validate one native producer row before it can reach ElementMaths."""

    if not isinstance(binding, Mapping) or binding.get("clip_id") != clip_id:
        return None
    reference = binding.get("element_ref")
    declaration = binding.get("declaration")
    resources = binding.get("motion_resources")
    if not isinstance(reference, Mapping) or not isinstance(declaration, Mapping) or not isinstance(resources, list):
        return None
    if not all(isinstance(reference.get(key), str) and reference.get(key) for key in ("packId", "kind", "id")):
        return None
    identity = canonical_element_identity(reference)
    if identity is None:
        return None
    if reference.get("kind") != identity.split("/", 2)[1]:
        return None
    declaration_identity = _identity(
        declaration.get("pack_id"), declaration.get("kind"), declaration.get("id")
    )
    if declaration_identity != identity:
        return None
    if declaration.get("kind") != identity.split("/", 2)[1]:
        return None
    if not all(
        isinstance(declaration.get(key), str) and declaration.get(key)
        for key in ("pack_id", "kind", "id", "pack_root", "manifest_path")
    ) or not isinstance(declaration.get("resources"), list):
        return None
    resolved = _declaration_module(declaration)
    if resolved is None or resolved[0] != identity:
        return None
    module_path = resolved[1].resolve()
    pack_root = Path(declaration["pack_root"]).expanduser().resolve()
    declared_resources = declaration["resources"]
    admitted_resources: list[dict[str, str]] = []
    for resource in resources:
        if not isinstance(resource, Mapping):
            return None
        kind = resource.get("kind")
        declared_path = resource.get("declared_path")
        absolute_path = resource.get("path")
        digest = resource.get("sha256")
        if (
            kind not in {"implementation", "support"}
            or not isinstance(declared_path, str)
            or not declared_path
            or Path(declared_path).is_absolute()
            or not isinstance(absolute_path, str)
            or not Path(absolute_path).is_absolute()
            or not _digest(digest)
        ):
            return None
        declaration_row = next(
            (
                row for row in declared_resources
                if isinstance(row, Mapping)
                and row.get("kind") == kind
                and row.get("path") == declared_path
            ),
            None,
        )
        if declaration_row is None:
            return None
        expected_path = (pack_root / declared_path).resolve()
        actual_path = Path(absolute_path).resolve()
        if (
            actual_path != expected_path
            or actual_path != module_path
            or not _within(actual_path, pack_root)
            or any(part.is_symlink() for part in (actual_path, *actual_path.parents) if part != pack_root.parent)
            or actual_path.parent != Path(declaration["manifest_path"]).expanduser().resolve().parent
            or not actual_path.is_file()
        ):
            return None
        try:
            actual_digest = hashlib.sha256(actual_path.read_bytes()).hexdigest()
        except OSError:
            return None
        if actual_digest != digest:
            return None
        admitted_resources.append({
            "kind": kind,
            "path": absolute_path,
            "declared_path": declared_path,
            "sha256": digest,
        })
    if not admitted_resources:
        return None
    return {
        "clip_id": clip_id,
        "element_ref": dict(reference),
        "declaration": dict(declaration),
        "motion_resources": admitted_resources,
    }


def admitted_element_bindings(metadata: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Validate the native metadata handoff, or return an explicit mirror map."""

    if not isinstance(metadata, Mapping):
        return {}
    raw = metadata.get("element_resources")
    expected_hash = metadata.get("element_resource_binding_hash")
    if not isinstance(raw, Mapping) or not isinstance(expected_hash, str) or not _digest(expected_hash):
        return {}
    actual_hash = _binding_hash(raw)
    if actual_hash is None or actual_hash != expected_hash:
        return {}
    validated: dict[str, dict[str, Any]] = {}
    for clip_id, binding in raw.items():
        if not isinstance(clip_id, str) or not clip_id:
            return {}
        checked = _validated_binding(clip_id, binding)
        if checked is None:
            return {}
        validated[clip_id] = checked
    return validated


def _element_ref(element: Any) -> Mapping[str, Any]:
    clip = getattr(element, "clip", None)
    if not isinstance(clip, Mapping):
        return {}
    reference = clip.get("elementRef", clip.get("element_ref"))
    return reference if isinstance(reference, Mapping) else {}


def _element_identity(element: Any) -> str | None:
    reference = _element_ref(element)
    return _identity(
        reference.get("packId", reference.get("pack_id")),
        reference.get("kind"),
        reference.get("id", getattr(element, "type", None)),
    )


def modules(
    root: Path | None = None,
    *,
    declarations: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    elements: Iterable[Any] | None = None,
) -> dict[str, Path]:
    """Resolve admitted V3 motion resources by canonical pack/element identity.

    ``declarations`` is the action/context handoff: each row contains
    ``pack_id``, ``kind``, ``id``, ``pack_root``, ``manifest_path`` and the
    V3 rendering item's ``resources`` list.  With no handoff, ``modules()``
    reads the two named current motion packs' ``pack.yaml`` declarations only
    so existing callers retain a read-only discovery view.  Passing ``elements``
    makes that same declaration set resolve only the packs named by canonical
    ``elementRef.packId`` values; no directory scan or legacy fallback occurs.

    Bare element-id aliases are retained only when exactly one canonical
    declaration owns that id.  Ambiguous ids have no alias and therefore fall
    back to the mirror unless the clip carries its canonical pack identity.
    """
    packs_root = _packs_root(root)
    if declarations is None:
        if elements is None:
            rows = _default_declarations(packs_root)
        else:
            pack_ids = sorted({
                str(reference.get("packId", reference.get("pack_id")))
                for element in elements
                if (reference := _element_ref(element)).get("packId", reference.get("pack_id"))
            })
            installed = _default_declarations(packs_root)
            rows = (
                tuple(declaration for declaration in installed if declaration.get("pack_id") in pack_ids)
                if pack_ids
                else installed
            )
    elif isinstance(declarations, Mapping):
        rows = (declarations,)
    else:
        rows = tuple(declarations)

    found: dict[str, Path] = {}
    conflicts: set[str] = set()
    for declaration in rows:
        if not isinstance(declaration, Mapping):
            continue
        resolved = _declaration_module(declaration)
        if resolved is None:
            continue
        canonical, path = resolved
        if canonical in found and found[canonical] != path:
            conflicts.add(canonical)
            found.pop(canonical, None)
            continue
        if canonical not in conflicts:
            found[canonical] = path

    # Compatibility aliases preserve the existing motion API only when the
    # canonical declaration proves that no pack/id collision exists.
    owners: dict[str, list[str]] = {}
    for canonical in found:
        owners.setdefault(canonical.rsplit("/", 1)[-1], []).append(canonical)
    for local_id, canonicals in owners.items():
        if len(canonicals) == 1:
            found[local_id] = found[canonicals[0]]
    return found


class Unavailable(RuntimeError):
    """The element maths could not be evaluated here; the caller uses the mirror."""


def _runtime() -> tuple[str, Path]:
    from astrid.core.rendering.node_pin import resolve_pinned_node
    from astrid.core.rendering.remotion_runtime import resolve_remotion_project_dir

    project = resolve_remotion_project_dir()
    found = resolve_pinned_node(project, override=(os.environ.get("ASTRID_NODE_EXECUTABLE") or "").strip() or None)
    if not found.ok:
        raise Unavailable(f"no Node: {found.problem}")
    node = str(found.path)
    if not (project / "node_modules" / "esbuild").is_dir():
        raise Unavailable(f"no esbuild in {project}")
    return node, project


def evaluate(requests: list[Mapping[str, Any]], types: Mapping[str, Path], *, timeout: float = TIMEOUT_S) -> dict[str, list[dict]]:
    """Run ``motionAt`` for each request (``id, type, params, fps, frames``); raises ``Unavailable``."""
    if not requests:
        return {}
    node, project = _runtime()
    payload = json.dumps({"modules": {k: str(v) for k, v in types.items()}, "requests": list(requests)}, default=str)
    try:
        done = subprocess.run([node, str(SCRIPT), str(project)], input=payload, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unavailable(f"node failed: {exc}") from exc
    if done.returncode != 0:
        tail = (done.stderr.strip().splitlines() or ["no output"])[-1]
        raise Unavailable(f"node exited {done.returncode}: {tail[:160]}")
    try:
        result = json.loads(done.stdout)
    except ValueError as exc:
        raise Unavailable("node returned no JSON") from exc
    if result.get("errors") and not result.get("values"):
        raise Unavailable("; ".join(f"{k}: {v}" for k, v in result["errors"].items())[:200])
    return {str(k): [dict(row) for row in rows] for k, rows in (result.get("values") or {}).items()}


class ElementMaths:
    """One lazy batch for every clip of a run whose element ships ``motion.ts``."""

    def __init__(
        self,
        elements: Iterable[Any],
        types: Mapping[str, Path] | None = None,
        *,
        declarations: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    ) -> None:
        elements = list(elements)
        self.types = dict(
            modules(elements=elements, declarations=declarations) if types is None else types
        )
        self.elements = [
            e for e in elements
            if self._module_key(e) is not None and not getattr(e, "audio", False)
        ]
        self.values: dict[str, list[dict]] | None = None
        self.fps: float | None = None
        self.note: str | None = None  # why the mirror answered, when it did

    def _module_key(self, element: Any) -> str | None:
        canonical = _element_identity(element)
        if canonical in self.types:
            return canonical
        local_id = getattr(element, "type", None)
        return local_id if local_id in self.types else None

    def wants(self, element: Any) -> bool:
        return self._module_key(element) is not None

    def _load(self, fps: float) -> None:
        self.fps = fps
        requests = []
        for element in self.elements:
            module_key = self._module_key(element)
            if module_key is None:
                continue
            frames = int(math.ceil((element.end - element.start) * fps - 1e-6)) + 1
            if 0 < frames <= MAX_FRAMES:
                requests.append({"id": element.key, "type": module_key, "params": dict(element.params),
                                 "fps": fps, "frames": frames})
        try:
            self.values = evaluate(
                requests,
                {
                    module_key: self.types[module_key]
                    for element in self.elements
                    if (module_key := self._module_key(element)) is not None
                },
            )
        except Unavailable as exc:
            self.values, self.note = {}, str(exc)

    def at(self, element: Any, frame: int, fps: float | None = None) -> dict[str, float] | None:
        """The element's own values on a clip frame, or ``None`` (use the mirror)."""
        if not self.wants(element):
            return None
        if self.values is None or (fps is not None and self.fps is not None and abs(fps - self.fps) > 1e-6):
            self._load(float(fps or self.fps or 30.0))
        rows = (self.values or {}).get(element.key)
        if rows is None or not 0 <= frame < len(rows):
            return None
        return {k: float(v) for k, v in rows[frame].items() if isinstance(v, (int, float))}
