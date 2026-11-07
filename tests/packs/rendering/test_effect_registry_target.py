"""Generation is confined to the selected host, including with an installed package."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _install_public_registry_probe(project: Path) -> Path:
    """Stage a private API-only Composition graph for generator target tests."""
    from astrid.core.element.registry import load_default_registry

    package = project / "node_modules/@banodoco/timeline-composition"
    dist = package / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    public_ids = {
        kind: [
            element.id
            for element in load_default_registry(project_root=ROOT).list(kind=kind)
            if element.source == "pack:rendering" and not element.component.is_file()
        ]
        for kind in ("effects", "animations", "transitions")
    }
    (package / "package.json").write_text(json.dumps({
        "type": "module",
        "exports": {
            ".": "./dist/index.js",
            "./registries": "./dist/registries.js",
        },
    }), encoding="utf-8")
    (dist / "index.js").write_text("export const DEFAULT_THEME = {id: 'default'};\n", encoding="utf-8")
    (dist / "registries.js").write_text(
        "export const resolveElementRegistries = () => ({\n"
        + "\n".join(
            f"  {kind}: Object.fromEntries({json.dumps(ids)}.map((id) => [id, () => null])),"
            for kind, ids in public_ids.items()
        )
        + "\n});\n",
        encoding="utf-8",
    )
    return package


def test_generator_targets_host_and_leaves_installed_package_unchanged(tmp_path):
    installed = tmp_path / "node_modules/@banodoco/timeline-composition/dist"
    installed.mkdir(parents=True)
    sentinel = installed / "effects.generated.js"
    sentinel.write_text("immutable package\n")
    _install_public_registry_probe(tmp_path)
    env = {**os.environ, "ASTRID_REMOTION_PROJECT_DIR": str(tmp_path),
           "ASTRID_TIMELINE_COMPOSITION_SRC": str(installed)}
    subprocess.run([sys.executable, str(ROOT / "scripts/gen_effect_registry.py")],
                   env=env, capture_output=True, text=True, check=True)
    for kind in ("effects", "animations", "transitions"):
        assert (tmp_path / "src" / f"{kind}.generated.ts").is_file()
    assert sentinel.read_text() == "immutable package\n"


def test_generator_refuses_host_staging_linked_into_installed_package(tmp_path):
    installed = tmp_path / "node_modules/@banodoco/timeline-composition/dist"
    installed.mkdir(parents=True)
    _install_public_registry_probe(tmp_path)
    (tmp_path / "src").symlink_to(installed, target_is_directory=True)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/gen_effect_registry.py")],
                            env={**os.environ, "ASTRID_REMOTION_PROJECT_DIR": str(tmp_path)},
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "outside installed packages" in result.stderr
    assert not (installed / "effects.generated.ts").exists()
