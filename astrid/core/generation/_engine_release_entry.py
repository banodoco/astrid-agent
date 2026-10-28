"""Retained whole-wheel launcher. Run with the selected Python's -I -S flags."""
from __future__ import annotations

import importlib.metadata
import json
import runpy
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    sys.dont_write_bytecode = True
    # This file is verified by the trusted outer archive verifier before use.
    # Load that exact stdlib verifier without importing product/site code first.
    release = runpy.run_path(str(root / 'site/astrid/core/generation/engine_release.py'))
    operation, *args = sys.argv[1:]
    manifest = release['verify_tree'](root) if (root / 'release.json').exists() else None
    if manifest is None and operation != 'check':
        raise ValueError('missing release manifest')
    if manifest is not None and manifest['python'] != release['interpreter_identity']():
        raise ValueError('release requires its exact build interpreter/platform')
    if manifest is not None and (not manifest['complete'] or manifest['fixture']):
        if operation != 'cli' or args != ['engines', 'install', 'vibecomfy-h3',
                                         '--placement', 'local', '--dry-run']:
            raise ValueError('incomplete fixture permits only canonical planning')
    sys.path.insert(0, str(root / 'site'))
    if operation == 'cli':
        dist = importlib.metadata.distribution('astrid')
        entries = [e for e in dist.entry_points if e.group == 'console_scripts' and e.name == 'astrid']
        if len(entries) != 1 or entries[0].value != 'astrid.console:main':
            raise ValueError('unexpected installed canonical entrypoint')
        return entries[0].load()(args)
    if operation == 'agent':
        # Keep the existing stdlib token agent and observation policy. Loading
        # its exact standalone file avoids importing provider/controller APIs.
        runpy.run_path(str(root / 'site/runpod_lifecycle/pod_agent.py'), run_name='__main__')
        return 0
    if operation == 'startup':
        from astrid.core.generation.engine_startup import main as startup
        return startup(args, release_root=root)
    if operation == 'readiness':
        # Target-local ordinary HC03 profile for the already installed engine;
        # observes only, under this same verified release and interpreter.
        from astrid.core.generation.engine_readiness import main as readiness
        return readiness(args, release_root=root)
    if operation != 'check' or args:
        raise ValueError('unsupported release operation')
    from astrid.console import main as console
    if console(['engines', 'install', 'vibecomfy-h3', '--placement', 'local', '--dry-run']):
        raise ValueError('installed CLI dry-run failed')
    from astrid.core.generation.engine_recipe import load_recipe
    recipe = load_recipe('vibecomfy-h3', 'linux-x86_64-cu130')
    from astrid.core.generation.engine_runner_target import _entries
    for environment in recipe.environments:
        ref = environment.requirements.packaged_lock
        if environment.requirements.mode != 'packaged' or ref is None:
            raise ValueError('resolved Linux lock missing')
        _entries((root / 'site/astrid' / ref.path).read_text())
    # Import the actual whole-wheel native/context/custody closure without
    # creating another service during a packaging build. Real helper execution,
    # retained-source lifetime and shutdown remain supervisor integration gates.
    from runtime_protocol import platform
    from astrid.core.generation.engine_execution import LocalLinuxExecutionContext
    from astrid.core.generation.engine_custody import LinuxEngineServiceClient
    if not all(callable(getattr(LinuxEngineServiceClient, method, None))
               for method in ('call', 'run', 'stop_owned')):
        raise ValueError('supervisor API is incomplete')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('retained release execution refused', file=sys.stderr)
        raise SystemExit(1)
