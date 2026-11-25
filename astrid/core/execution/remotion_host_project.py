"""Host-owned project render selection for an admitted command source."""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path


def prepare_project_selection(project, attempt, environment, source):
    """Recreate the admitted project closure before the worker takes the render lock."""
    if source is None:
        return None
    from astrid.core.rendering.verified_source import SOURCE_ENV, VerifiedTimelineSource
    if not isinstance(source, VerifiedTimelineSource) or environment.get(SOURCE_ENV) != source._anchor:
        raise ValueError("command source is missing its host-owned digest-bound handoff authority")
    binding = source.read()
    prepared = binding["prepared"]
    timeline, assets = prepared["timeline"]["path"], prepared["registry"]["path"]
    source.validate(timeline, assets)
    from astrid.core.rendering.project_elements import selections, stage_project_elements
    from astrid.core.rendering.remotion_runtime import _validated_runtime, renderer_backend_modules
    lock, run = renderer_backend_modules()
    config = run._serialize_timeline(Path(timeline))
    source.validate(timeline, assets)
    if not selections(config):
        return None
    validated = _validated_runtime(project, environment=environment)["registry"]
    if validated.get("mode") != "source_settled" or run.gen_effect_registry is None:
        raise ValueError("command project source requires launch-validated registry authority")
    observation = validated["renderer"]["observation"]
    temporary = tempfile.TemporaryDirectory(prefix=".astrid-host-elements-", dir=attempt)
    try:
        with lock.remotion_render_lock():
            generator = run.gen_effect_registry
            registry = generator._element_registry()
            if (generator.render_pack_roots(registry=registry, include_aliases=True) != observation["render_pack_roots"]
                    or generator.compute_generated_registry_state(registry=registry) != observation["state"]):
                raise ValueError("host registry differs from launch validation")
            selected, staged = stage_project_elements(config, Path(temporary.name), registry=registry)
            registry_root = Path(temporary.name) / "host"
            run._write_selected_element_registries(registry_root, selected)
            roots = generator.render_pack_roots(registry=selected, include_aliases=True)
        source.validate(timeline, assets)
        return {"temporary": temporary, "roots": roots, "staged": staged,
                "registry_root": str(registry_root), "source": source,
                "timeline": timeline, "assets": assets}
    except BaseException:
        temporary.cleanup()
        raise


def same_staged_tree(candidate: Path, selected: Path) -> bool:
    """Compare only regular, non-private files without following staged links."""
    def inventory(root):
        rows = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if (path.is_symlink() or any(part.startswith(".env") or part.endswith(".env") or
                                         "credential" in part.lower() for part in relative.parts)):
                raise ValueError("invalid private or linked file in staged project root")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError("invalid file in staged project root")
            rows[relative.as_posix()] = (path.stat().st_size, hashlib.sha256(path.read_bytes()).digest())
        return rows
    return inventory(candidate) == inventory(selected)
