"""Check the pinned Runtime fixtures with an explicitly loaded Agent tests package."""
import importlib, importlib.util, pathlib, sys


def verify_runtime_fixtures(agent, runtime):
    agent, runtime = pathlib.Path(agent).resolve(), pathlib.Path(runtime).resolve()
    if any(name == 'tests' or name.startswith('tests.') for name in sys.modules):
        raise RuntimeError('tests already imported')
    package_file = agent / 'tests' / '__init__.py'
    spec = importlib.util.spec_from_file_location('tests', package_file, submodule_search_locations=[str(package_file.parent)])
    if spec is None or spec.loader is None: raise RuntimeError('Agent tests package unavailable')
    package = importlib.util.module_from_spec(spec)
    sys.modules['tests'] = package
    spec.loader.exec_module(package)
    if pathlib.Path(package.__file__).resolve() != package_file: raise RuntimeError('Agent tests package mismatch')
    import runtime_protocol
    if pathlib.Path(runtime_protocol.__file__).resolve().parent.parent != runtime: raise RuntimeError('Runtime checkout mismatch')
    package.__path__.append(str(runtime / 'tests'))
    for name in ('test_verified_host_launch', 'test_remote_worker_activation'):
        module = importlib.import_module('tests.' + name)
        if pathlib.Path(module.__file__).resolve() != runtime / 'tests' / (name + '.py'):
            raise RuntimeError('Runtime fixture mismatch')


def main():
    if len(sys.argv) != 4: raise ValueError('expected Agent, Runtime and VibeComfy pin')
    from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION
    if VIBECOMFY_ENGINE_REVISION != sys.argv[3]: raise RuntimeError('VibeComfy pin mismatch')
    verify_runtime_fixtures(sys.argv[1], sys.argv[2])
    print('runtime fixture paths and VibeComfy pin verified')


if __name__ == '__main__': main()
