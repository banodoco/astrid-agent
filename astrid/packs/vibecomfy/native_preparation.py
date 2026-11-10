"""One native preparation transaction for host-managed and pod-owned engines."""
from __future__ import annotations


def prepare_native_workflow(workflow_path, runtime_root, python_executable, uv_executable,
                            launch_flags, capsule_sha256, expected_requirements=None,
                            sync_packages=None, offline=False):
    """Run inside the verified engine interpreter and its owned command scope.

    The caller has verified installation custody/base identities and selected the
    allowed add-on packages. This helper never changes the canonical capsule.
    """
    import ast
    import hashlib
    import json
    import subprocess
    import sys
    from pathlib import Path
    from vibecomfy.commands.runpod_setup import ensure_runtime_layout
    from vibecomfy.runtime.dependencies import (
        compare_runtime, inspect_runtime_target, runtime_requirements_from_workflow, sync_runtime,
    )
    from vibecomfy.runtime.prepared import prepare_workflow
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import load_bundle

    root, source = Path(runtime_root), Path(workflow_path)
    if (not root.is_absolute() or not source.is_absolute()
            or Path(sys.executable).resolve() != Path(python_executable).resolve()
            or any(path.is_symlink() for path in (root, *root.parents))):
        raise ValueError("native preparation requires the verified managed interpreter and root")
    def check_capsule():
        for name, expected in capsule_sha256.items():
            path = Path(name)
            if (not path.is_file() or any(part.is_symlink() for part in (path, *path.parents))
                    or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
                raise ValueError("native preparation canonical capsule changed")
    check_capsule()
    bundle = load_bundle(source, trust=Provenance.USER_CONFIRMED)
    bundle.require_canonical_authority("native managed preparation")
    requirements = runtime_requirements_from_workflow(bundle.workflow)
    declared = requirements.to_dict() if requirements is not None else None
    if declared != expected_requirements:
        raise ValueError("native preparation runtime requirements differ from the admitted workflow")
    ensure_runtime_layout(runtime_root=root)
    def observe():
        actual = inspect_runtime_target(runtime_root=root, python_executable=python_executable,
            package_names=[name for name, _ in requirements.packages] if requirements else [])
        actual['launch_flags'] = list(launch_flags)
        actual['managed'] = True
        # ComfyUI's source install need not register a distribution. Its version
        # file belongs to the already recipe-verified checkout; never import it.
        version_file = root / 'ComfyUI' / 'comfyui_version.py'
        if actual.get('comfy_version') is None and version_file.is_file() and not version_file.is_symlink():
            for statement in ast.parse(version_file.read_text()).body:
                if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == '__version__' for target in statement.targets):
                    value = ast.literal_eval(statement.value)
                    if isinstance(value, str): actual['comfy_version'] = value
        return actual
    def install(packages, offline_install):
        argv = [uv_executable, '--no-config', 'pip', 'install', '--python', python_executable, '--no-deps']
        if offline_install: argv.append('--offline')
        if requirements and requirements.package_indexes:
            argv += ['--index-url', requirements.package_indexes[0]]
            for index in requirements.package_indexes[1:]: argv += ['--extra-index-url', index]
        subprocess.run([*argv, *packages], check=True, capture_output=True, text=True)
    report = sync_runtime(requirements, runtime_root=root, installer=install, verify=observe,
                          launch_flags=list(launch_flags), sync_packages=sync_packages, offline=offline)
    check_capsule()
    compliance = compare_runtime(requirements, target=observe(), runtime_root=root)
    if requirements is not None and (compliance.get('status') != 'matching'
                                    or any(check.get('status') != 'matching' for check in compliance.get('checks', []))):
        raise ValueError("native preparation dependencies remain noncompliant or unverified")
    # This fresh complete observation makes a native repeated sync an early
    # no-op; retain the raw first transaction without changing its synced flag.
    target = compliance.get('actual', observe())
    prepared = prepare_workflow(bundle.workflow, reference=source, runtime_root=root,
        ensure_models=False, ensure_packs=False, quiet=True, dependency_mode='sync',
        runtime_target=target, runtime_dependency_report=report, offline=offline,
        sync_packages=sync_packages)
    check_capsule()
    return {'synchronization': report, 'observation': observe(), 'compliance': compliance, 'preparation': prepared.to_json()}


def ensure_native_pip(python_executable):
    """Bootstrap only the verified interpreter's bundled pip, offline and without upgrade."""
    import hashlib
    import importlib.metadata
    import importlib.util
    import json
    import subprocess
    import sys
    import sysconfig
    from pathlib import Path
    import ensurepip
    if Path(sys.executable).resolve() != Path(python_executable).resolve():
        raise ValueError("pip bootstrap requires the verified managed interpreter")
    configured = sysconfig.get_config_var('WHEEL_PKG_DIR')
    if configured and not Path(configured).resolve().is_relative_to(Path(sys.base_prefix).resolve()):
        raise ValueError("pip bootstrap wheel directory is outside the verified interpreter")
    from contextlib import nullcontext
    from importlib import resources
    if hasattr(ensurepip, '_get_pip_whl_path_ctx'):
        wheel_context = ensurepip._get_pip_whl_path_ctx()
    else:
        # CPython 3.12 (the recipe's interpreter) exposes the exact selected
        # bundled/system package rather than the 3.13 wheel context helper.
        package = ensurepip._get_packages()['pip']
        wheel_context = (nullcontext(Path(package.wheel_path)) if package.wheel_path else
                         resources.as_file(resources.files('ensurepip').joinpath('_bundled', package.wheel_name)))
    with wheel_context as wheel:
        wheel = Path(wheel)
        import stat
        if (wheel.is_symlink() or not stat.S_ISREG(wheel.lstat().st_mode)
                or not wheel.resolve(strict=True).is_relative_to(Path(sys.base_prefix).resolve(strict=True))):
            raise ValueError("pip bootstrap wheel escaped the verified interpreter")
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        wheel_path = str(wheel)
        bundled_version = ensurepip.version()
        spec = importlib.util.find_spec('pip')
        try: installed = importlib.metadata.version('pip')
        except importlib.metadata.PackageNotFoundError: installed = None
        if (spec is None) != (installed is None):
            raise ValueError("managed pip installation is partial")
        existed = spec is not None
        if not existed:
            subprocess.run([python_executable, '-I', '-m', 'ensurepip', '--default-pip'],
                           check=True, capture_output=True, text=True)
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != digest:
            raise ValueError("pip bootstrap wheel changed during installation")
    program = "import pip,importlib.metadata,json;print(json.dumps({'version':pip.__version__,'distribution_version':importlib.metadata.version('pip'),'location':pip.__file__}))"
    probe = json.loads(subprocess.run([python_executable, '-I', '-c', program],
                                     check=True, capture_output=True, text=True).stdout)
    if (probe['version'] != probe['distribution_version']
            or not Path(probe['location']).resolve().is_relative_to(Path(sys.prefix).resolve())):
        raise ValueError("managed pip identity is inconsistent")
    help_text = subprocess.run([python_executable, '-I', '-m', 'pip', 'install', '--help'],
                              check=True, capture_output=True, text=True).stdout
    if '--dry-run' not in help_text or '--report' not in help_text:
        raise ValueError("managed pip lacks protected native installation preflight")
    return {'existed': existed, 'bundled_version': bundled_version, 'wheel_path': wheel_path,
            'wheel_sha256': digest, 'installed': probe}


def _native_program(function):
    import inspect
    return (inspect.getsource(function) +
            "\nimport json,sys\nprint(json.dumps(" + function.__name__ + "(**json.loads(sys.argv[1]))))\n")


def native_pip_program():
    return _native_program(ensure_native_pip)


def native_preparation_program():
    """Ship the same helper into an isolated, supervisor-owned engine command."""
    return _native_program(prepare_native_workflow)
