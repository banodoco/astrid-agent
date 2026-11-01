"""CPU installed nested-child canary; no full installer, media or GPU execution.

Real Backend.stage/profile generation, installed wheels, Runtime service owner,
verified local host, SDK/HostChildBridge, HTTP delegated admission and claim.
Only tool downloads, dependency acquisition and renderer provisioning are
substituted. The test parent submits only the real H3 prepare declaration with
deliberately invalid CPU inputs, so no downstream GPU stage can be reached.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import pytest

from astrid.installer.backend import Backend
from astrid.installer.core import read_json, switch, write_json
from astrid.installer.service import LABEL
from tests.helpers.runtime import runtime_checkout

pytestmark = [pytest.mark.integration, pytest.mark.linux_native]


def _run(argv, **kwargs):
    result = subprocess.run([str(item) for item in argv], capture_output=True, text=True,
                            timeout=180, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def _pin(root):
    return _run(["git", "-C", root, "rev-parse", "HEAD"])


def _copy_cpu_dependencies(purelib):
    # Reuse the shared interpreter's already installed dependencies. Nothing is
    # downloaded, and the private installed host never imports a source checkout.
    owners = importlib.metadata.packages_distributions()
    for name in ("PIL", "yaml", "dotenv", "jsonschema", "attrs", "attr", "referencing", "rpds",
                 "jsonschema_specifications", "typing_extensions", "filelock", "jwt"):
        module = importlib.import_module(name)
        path = Path(module.__file__)
        if hasattr(module, "__path__"):
            shutil.copytree(path.parent, purelib / path.parent.name,
                            ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(path, purelib / path.name)
        for owner in owners.get(name, ()):
            distribution = importlib.metadata.distribution(owner)
            for item in distribution.files or ():
                libs = Path(item.parts[0])
                if libs.name.endswith(".libs") and not (purelib / libs.name).exists():
                    shutil.copytree(distribution.locate_file(libs), purelib / libs.name)


def _test_parent(root):
    pack = root / "canary-pack"
    (pack / "actions").mkdir(parents=True)
    write_json(pack / "pack.yaml", {
        "schema_version": 3, "id": "installed_canary", "name": "Installed CPU canary", "version": "1.0.0",
        "actions": {"parent": {"description": "Submit only H3 prepare", "invocation": {
            "kind": "python", "path": "actions/parent.py", "function": "run"},
            "inputs": [{"name": "request_oid", "type": "string", "required": True},
                       {"name": "bundle_oid", "type": "string", "required": True},
                       {"name": "request", "type": "file", "required": True},
                       {"name": "input_bundle", "type": "file", "required": True}], "outputs": {}}},
    })
    (pack / "actions/parent.py").write_text('''def run(request_oid, bundle_oid, request, input_bundle):
    from astrid.sdk.invocation import invoke_result
    objects = {"request": request_oid, "input_bundle": bundle_oid}
    result = invoke_result("h3_av.prepare", kind="executor",
        inputs={name: {"object_id": oid, "digest": oid, "filename": name + ".bin"}
                for name, oid in objects.items()},
        child_key="prepare", stage="prepare",
        input_refs=[{"name": name, "root_object_id": oid} for name, oid in objects.items()],
        timeout_seconds=30)
    return {"child_id": result.kernel_task_id}
''', encoding="utf-8")
    from runtime_protocol.local_worker_composition import _pack_inventory
    return {"path": str(pack), "files": _pack_inventory(pack)}


def test_ordinary_installed_host_admits_and_claims_h3_prepare(tmp_path, monkeypatch):
    import astrid.installer.backend as backend_module

    agent = Path(__file__).resolve().parents[1]
    runtime = runtime_checkout()
    vibe_pin = json.loads((agent / "astrid/packs/vibecomfy/pins.json").read_text())["sources"]["vibecomfy"]["revision"]
    vibe = Path(os.environ.get("ASTRID_TEST_VIBECOMFY_CHECKOUT", agent.parent / "vibecomfy"))
    assert _pin(runtime) == json.loads((agent / "runtime.lock.json").read_text())["source_commit"]
    assert _pin(vibe) == vibe_pin
    uv = Path(shutil.which("uv") or "")
    assert uv.is_file(), "The installed CPU canary requires the shared uv."
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    for key in tuple(os.environ):
        if key.startswith(("ASTRID_", "VIBECOMFY_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", str(tmp_path / "uv-python"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        door_port = listener.getsockname()[1]
    monkeypatch.setenv("ASTRID_RUNTIME_DOOR_PORT", str(door_port))
    program, data = tmp_path / "program", tmp_path / "data"
    program.mkdir()
    write_json(program / "install.json", {"owner": LABEL, "data_root": str(data)})
    backend = Backend(program, tmp_path / "installer.log", uv, say=lambda _line: None)
    real_run = backend.run
    uv_version = _run([uv, "--version"]).split()[1]
    manifest = {"schema_version": 1, "release": 1,
        **{name: {"sha": _pin(root), "repo": str(root)}
           for name, root in (("agent", agent), ("runtime", runtime), ("vibecomfy", vibe))},
        "tools": {"uv": {"version": uv_version}, "python": {"version": f"{sys.version_info.major}.{sys.version_info.minor}"}}}
    staged_source_hashes = {}

    def bounded_acquisition(argv, **kwargs):
        if argv[0] == uv and str(argv[1]) == "venv":
            from astrid.installer.tools import source_tree_sha256
            for name, staged in (("agent", "a"), ("runtime", "r"), ("vibecomfy", "v")):
                staged_source_hashes[name] = source_tree_sha256(Path(argv[-1]).parent / staged)
            # A small test-owned prefix reuses this interpreter; no Python download.
            return real_run([uv, "venv", "--python", sys.executable, argv[-1]], **kwargs)
        if argv[0] == uv and "--require-hashes" in argv:
            python = Path(argv[argv.index("--python") + 1])
            purelib = Path(_run([python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]))
            _copy_cpu_dependencies(purelib)
            return ""
        if argv[0] == uv and str(argv[1]) == "pip" and "--no-deps" in argv:
            wheels = tmp_path / "wheels"
            wheels.mkdir()
            for source in argv[argv.index("--no-deps") + 1:]:
                # VibeComfy's declared Hatch backend is built in isolation;
                # the Agent and Runtime reuse the shared setuptools backend.
                if Path(source).name == "v":
                    _run([uv, "build", "--wheel", "--out-dir", wheels, source])
                else:
                    _run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", wheels], cwd=source)
            return real_run([*argv[:argv.index("--no-deps") + 1], "--no-index", *sorted(wheels.glob("*.whl"))], **kwargs)
        return real_run(argv, **kwargs)

    def tools(_manifest, root, _target, _run_tool):
        root.mkdir(parents=True)
        bun = root / "bun"
        bun.write_text("#!/usr/bin/python3\nimport sys\nsys.stdin.read()\n", encoding="utf-8")
        bun.chmod(0o755)
        return {"PATH": os.environ.get("PATH", os.defpath),
                "ASTRID_NODE_EXECUTABLE": str(bun), "ASTRID_BUN_EXECUTABLE": str(bun)}

    monkeypatch.setattr(backend, "run", bounded_acquisition)
    monkeypatch.setattr(backend_module, "install_tools", tools)
    monkeypatch.setattr(backend_module, "binary", lambda *_args: tmp_path / "unused-npm")
    monkeypatch.setattr(backend, "install_remotion_dependencies", lambda *_args: False)
    version = backend.stage(manifest, program, data)
    assert read_json(version.root / "manifest.json") == manifest
    smoke = json.loads(_run([version.python, "-I", "-c", '''
import importlib, importlib.metadata, json, pathlib, sysconfig
root = pathlib.Path(sysconfig.get_path('purelib')).resolve()
origins = {}
for name in ('astrid', 'astrid_runtime', 'runtime_protocol', 'astrid_runtime_client', 'vibecomfy'):
    origin = pathlib.Path(importlib.import_module(name).__file__).resolve()
    assert origin.is_relative_to(root), (name, str(origin))
    origins[name] = str(origin)
print(json.dumps({'origins': origins, 'versions': {name: importlib.metadata.version(name)
    for name in ('astrid', 'astrid-runtime', 'astrid-runtime-client', 'vibecomfy')}}))
''']))
    print(json.dumps({"staged_sources": {name: manifest[name]["sha"]
        for name in ("agent", "runtime", "vibecomfy")}, "staged_tree_hashes": staged_source_hashes,
        "installed_smoke": smoke}), flush=True)
    profile = read_json(version.profile)
    assert profile["worker_profile"] == str(version.root / "worker-profile.json")
    worker = read_json(Path(profile["worker_profile"]))
    assert worker["execution_mode"] == "host_only_cpu"
    assert worker["source_checkout"] == profile["source_checkout"]
    assert worker["max_concurrency"] >= 2
    # A declared test parent adds no placement or credential authority. Its
    # inventory is verified by the unmodified production local-worker loader.
    from runtime_protocol.local_worker import _digest
    worker["additional_pack_roots"] = [_test_parent(tmp_path)]
    launch = {name: worker[name] for name in ("source_checkout", "pack_root", "boot_manifest_path",
        "boot_manifest_hash", "capability_matrix", "environment", "max_concurrency", "additional_pack_roots")}
    worker["session_config_digest"] = _digest(launch)
    worker["profile_digest"] = _digest({key: value for key, value in worker.items() if key != "profile_digest"})
    write_json(Path(profile["worker_profile"]), worker)
    services = read_json(Path(profile["service_declaration"]))
    host_service = next(row for row in services["services"] if row["name"] == "pack-host")
    assert host_service["argv"][-1] == "--verified-local-worker"
    assert host_service["ready"]["file"] == "worker-astrid-ready.json"

    realm = backend.runtime(version, data, "workspace", "create", "--realm-root", str(data / "realm"),
                            "--realm-id", "installed-canary", "--door-port", str(door_port),
                            "--hosted-origin", "off", profile=True)
    switch(program, version)
    backend.service.system = "linux"
    monkeypatch.setattr(backend.service, "systemd", lambda: False)
    started = False
    try:
        backend.register(version, data)
        started = True
        backend.start(version, data)
        ready_path = data / "runtime/worker-astrid-ready.json"
        deadline = time.monotonic() + host_service["ready"]["timeout_seconds"]
        while not ready_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert ready_path.is_file(), "Verified host did not become ready; inspect test-owned Runtime/service logs."
        ready = read_json(ready_path)
        assert ready["source_checkout"] == profile["source_checkout"]
        assert Path(ready["python_executable"]) == version.python
        assert ready["activation"]["executor_incarnation"]
        assert "h3_av.prepare" in ready["ready_capabilities"]
        assert "installed_canary.parent" in ready["ready_capabilities"]

        driver = r'''
import hashlib, json, sys, time
from pathlib import Path
from astrid.core.execution._child_bridge import resource, task_resource
from astrid.sdk.host_bootstrap import PACK_HOST_ACTOR, PACK_HOST_SCOPES
from astrid.sdk.execution_request import normalize_execution_request
from astrid_runtime_client import WorkspaceClient
from runtime_protocol.auth import CredentialStore
from runtime_protocol.daemon import WORKER_SCOPES
support = Path(sys.argv[1])
discovery = json.loads((support / 'discovery.json').read_text())
credentials = CredentialStore(support / 'credentials')
metadata = credentials.actor_metadata(PACK_HOST_ACTOR)
assert tuple(PACK_HOST_SCOPES) == tuple(WORKER_SCOPES)
assert metadata['scopes'] == sorted(PACK_HOST_SCOPES)
binding = metadata['execution_binding']
assert binding['actual']['kind'] == 'machine' and binding['verification']['verified'] is True
assert binding['verification']['method'] == 'credential_claim'
receipt = metadata['local_launch_receipt']
assert receipt['execution_mode'] == 'host_only_cpu'
ready = json.loads((support / 'worker-astrid-ready.json').read_text())
assert binding['actual']['id'] == receipt['machine_id']
assert binding['executor_incarnation'] == receipt['executor_incarnation'] == ready['activation']['executor_incarnation']
assert binding['verification']['evidence_digest'] == receipt['evidence_digest'] == ready['activation']['evidence_digest']
owner = WorkspaceClient(discovery['endpoint'], Path(discovery['credential_file']).read_text().strip())
project = resource(owner.create_project('Installed CPU canary', slug='installed-canary', idempotency_key='project'))
project_id = project.get('project_id') or project['id']
caps = {}
cursor = None
while True:
    rows, cursor = owner.list_capabilities(cursor=cursor, limit=100)
    caps.update({row.capability_id: row.definition_digest for row in rows})
    if cursor is None: break
objects = {}
files = {}
for name, content in [('request', b'{}'), ('input_bundle', b'CPU canary: deliberately invalid bundle')]:
    value = owner.ingest_project_object(project_id, content, filename=name+'.bin',
        media_type='application/octet-stream', idempotency_key='object-'+name)
    objects[name] = value.get('object_id') or value['digest']
    files[name] = {'object_id': objects[name], 'digest': objects[name], 'filename': name+'.bin',
                   'media_type': 'application/octet-stream', 'size': len(content)}
ordered = [{'name': name, 'root_object_id': oid} for name, oid in objects.items()]
policy = {'capabilities': [{'capability_id': 'h3_av.prepare', 'capability_digest': caps['h3_av.prepare']}],
    'targets': [{'kind': 'default'}], 'input_object_ids': list(objects.values()),
    'stages': [{'name': 'prepare', 'capability_id': 'h3_av.prepare', 'capability_digest': caps['h3_av.prepare'],
                'target': {'kind': 'default'}, 'inputs': ordered}]}
admitted = owner.admit_task(capability_id='installed_canary.parent', capability_digest=caps['installed_canary.parent'],
    project_id=project_id, input_object_ids=list(objects.values()),
    spec={'inputs': {'request_oid': objects['request'], 'bundle_oid': objects['input_bundle'], **files}},
    execution_request=normalize_execution_request({'target': {'kind': 'default'},
        'inputs': [{'name': name, **value, 'required': True} for name, value in files.items()]}),
    child_delegation=policy, idempotency_key='parent')
parent_id = admitted.task_id
deadline = time.monotonic() + 45
child = None
while time.monotonic() < deadline:
    events, _ = owner.list_events(limit=100)
    for event in events:
        if event.event_type == 'task.admitted' and event.aggregate_id != parent_id:
            candidate = task_resource(owner.get_task(event.aggregate_id))
            lineage = candidate.get('spec', {}).get('delegated_parent', {})
            if lineage.get('parent_task_id') == parent_id:
                child = candidate
    if child is not None and child.get('attempt_id'): break
    time.sleep(.1)
assert child is not None and child.get('attempt_id'), 'Real H3 prepare was not admitted and claimed'
assert child['capability_id'] == 'h3_av.prepare' and child['capability_digest'] == caps['h3_av.prepare']
assert child['project_id'] == project_id
lineage = child['spec']['delegated_parent']
assert lineage['project_id'] == project_id and lineage['stage'] == 'prepare'
assert lineage['parent_placement'] == binding
assert child['execution_binding']['actual_target']['kind'] == 'machine'
assert child['execution_binding']['actual_target'] == binding['actual']
assert child['execution_binding']['verification']['verified'] is True
assert child['input_object_ids'] == list(objects.values())
assert child['spec']['delegated_parent']['parent_placement']['actual'] == binding['actual']
print(json.dumps({'cpu_only': True, 'h3_prepare_admitted': True, 'h3_prepare_claimed': True,
    'production_scopes': True, 'verified_machine_placement': True, 'same_project': True}))
'''
        result = _run([version.python, "-I", "-c", driver, data / "runtime"], cwd=home,
                      env={key: value for key, value in os.environ.items()
                           if not key.startswith(("ASTRID_", "VIBECOMFY_")) and key != "PYTHONPATH"})
        assert json.loads(result)["h3_prepare_claimed"] is True
        print(result, flush=True)
    finally:
        if started:
            backend.stop(version, data)
