"""Installed registry children use the host's existing executable source closure."""

from __future__ import annotations

from pathlib import Path

from astrid.core.source_identity import source_checkout_closure_digest


def canonical_file(raw: str) -> Path:
    path = Path(raw)
    if (not path.is_absolute() or str(path) != raw or '..' in path.parts
            or any(part.is_symlink() for part in (path, *path.parents))
            or not path.is_file() or path.resolve(strict=True) != path):
        raise ValueError('installed registry origin is missing or aliased')
    return path


def selected_root(runtime_file: str) -> Path:
    return canonical_file(runtime_file).parents[3]


def carrier_origins(root: Path) -> dict[str, str]:
    return {
        'astrid_origin': str(root / 'astrid/__init__.py'),
        'runtime_origin': str(root / 'astrid/core/rendering/remotion_runtime.py'),
        'generator_origin': str(root / 'astrid/core/rendering/_effect_registry.py'),
    }


# Only stdlib executes until the selected package paths have been checked. Load
# source_identity by its checked file, so even Astrid's __init__ waits until its
# existing closure and Runtime-origin guard have passed. Never add import roots.
_PREFLIGHT = '''
import importlib.machinery
import importlib.util
import pathlib
import sys

if not sys.flags.isolated:
    raise ValueError('installed registry requires an isolated interpreter')
root = pathlib.Path(sys.argv[1])
def checked(path):
    if (not path.is_absolute() or '..' in path.parts
            or any(part.is_symlink() for part in (path, *path.parents))
            or not path.is_file() or path.resolve(strict=True) != path):
        raise ValueError('installed registry carrier is missing or aliased')
    return path

specs = {}
for name in (
    'astrid', 'astrid.version', 'astrid.core', 'astrid.core.source_identity',
    'astrid.core.rendering', 'astrid.core.rendering.remotion_runtime',
    'astrid.core.rendering._effect_registry', 'astrid.packs',
    'astrid.packs.rendering', 'astrid.packs.rendering.rendering',
    'astrid.packs.rendering.rendering.renderers',
    'astrid.packs.rendering.rendering.renderers.remotion',
    'astrid.packs.rendering.rendering.renderers.remotion.run',
):
    parent = name.rpartition('.')[0]
    search = specs[parent].submodule_search_locations if parent else sys.path
    # Leaf lookup keeps namespace paths from consulting unloaded parents.
    spec = importlib.machinery.PathFinder.find_spec(name.rsplit('.', 1)[-1], search)
    path = root.joinpath(*name.split('.'))
    if path.is_dir():
        if (spec is None or spec.submodule_search_locations is None
                or list(spec.submodule_search_locations) != [str(path)]):
            raise ValueError('installed registry package search path differs')
        expected = path / '__init__.py'
        if expected.exists():
            checked(expected)
            if (spec.origin != str(expected)
                    or not isinstance(spec.loader, importlib.machinery.SourceFileLoader)):
                raise ValueError('installed registry package origin differs')
        elif spec.origin is not None:
            raise ValueError('installed registry namespace origin differs')
    else:
        expected = checked(path.with_suffix('.py'))
        if (spec is None or spec.origin != str(expected)
                or not isinstance(spec.loader, importlib.machinery.SourceFileLoader)):
            raise ValueError('installed registry module origin differs')
    specs[name] = spec

identity_spec = specs['astrid.core.source_identity']
identity = importlib.util.module_from_spec(identity_spec)
identity_spec.loader.exec_module(identity)
identity.installed_runtime_origins(root)
if identity.source_checkout_closure_digest(root) != sys.argv[2]:
    raise ValueError('installed registry executable source closure mismatch')
'''


def launch_binding(runtime_file: str, installed_source: tuple[str, str] | None) -> tuple[Path, str]:
    if installed_source is None:
        root = selected_root(runtime_file)
    else:
        raw, _ = installed_source
        root = canonical_file(str(Path(raw) / 'astrid/core/rendering/remotion_runtime.py')).parents[3]
        if str(root) != raw:
            raise ValueError('installed registry launch root differs')
    digest = source_checkout_closure_digest(root)
    if installed_source is not None and digest != installed_source[1]:
        raise ValueError('installed registry launch source closure changed')
    return root, digest


def installed_argv(python: str, runtime_file: str, *, generate: bool = False,
                   installed_source: tuple[str, str] | None = None) -> list[str]:
    root, digest = launch_binding(runtime_file, installed_source)
    generator = root / 'astrid/core/rendering/_effect_registry.py'
    if not generator.is_file():
        raise FileNotFoundError('installed registry generator carrier is unavailable')
    canonical_file(str(generator))
    operation = (
        'from astrid.core.rendering._effect_registry import main\nraise SystemExit(main([]))'
        if generate else
        'import json\nfrom astrid.core.rendering.remotion_runtime import _registry_observation\n'
        'print(json.dumps(_registry_observation(), sort_keys=True))'
    )
    return [python, '-X', 'utf8', '-I', '-c', _PREFLIGHT + operation, str(root), digest]


def validate_observation(observation: dict, runtime_file: str, cwd: Path,
                         installed_source: tuple[str, str] | None = None) -> None:
    root, _ = launch_binding(runtime_file, installed_source)
    for name, expected in carrier_origins(root).items():
        if canonical_file(observation[name]) != Path(expected):
            raise ValueError('installed registry carrier differs from selected distribution')
    expected = {
        'runtime_protocol': str(root / 'runtime_protocol/__init__.py'),
        'runtime_protocol.platform': str(root / 'runtime_protocol/platform.py'),
        'astrid_runtime': str(root / 'astrid_runtime/__init__.py'),
    }
    if observation['runtime_dependency_origins'] != expected or observation['probe_cwd'] != str(cwd):
        raise ValueError('installed registry dependency/cwd differs')
    for raw in observation['runtime_dependency_origins'].values():
        canonical_file(raw)
