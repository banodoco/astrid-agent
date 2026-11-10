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
        inspect_runtime_target, runtime_requirements_from_workflow, sync_runtime,
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
    prepared = prepare_workflow(bundle.workflow, reference=source, runtime_root=root,
        ensure_models=False, ensure_packs=False, quiet=True, dependency_mode='sync',
        runtime_target=observe(), runtime_dependency_report=report, offline=offline,
        sync_packages=sync_packages)
    check_capsule()
    return {'synchronization': report, 'observation': observe(), 'preparation': prepared.to_json()}


def native_preparation_program():
    """Ship the same helper into an isolated, supervisor-owned engine command."""
    import inspect
    return (inspect.getsource(prepare_native_workflow) +
            "\nimport json,sys\nprint(json.dumps(prepare_native_workflow(**json.loads(sys.argv[1]))))\n")
