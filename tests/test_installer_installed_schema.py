"""The installer and installed Timeline consumers admit one pinned schema file.

Build the release wheel and install it noneditably into a real candidate venv.
Only acquisition, uv orchestration, tool downloads and workspace inspection are
stand-ins. Candidate probes, schema admission, service declaration, source digest
and the update transaction execute their production implementations. Dependency
packages are borrowed from the test interpreter's site-packages without running
its .pth files; every Astrid import must originate in the candidate itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer import app_update, core
from astrid.installer.backend import Backend
from astrid.installer.service import LABEL

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = Path("astrid/core/timeline/schema/timeline-config.schema.json")
PRODUCER = "1e6d899efa77a2d6ec247c38250f9748330bb587"
SCHEMA_SHA256 = "f870150ffd0e42365c048965e2f7c08efc959c36db76ed4cba59de2e58ab4e08"
pytestmark = pytest.mark.linux_native


def _run(argv, *, cwd, env=None):
    result = subprocess.run(
        list(map(str, argv)), cwd=cwd, env=env, capture_output=True, text=True,
        timeout=180, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@pytest.fixture(scope="module")
def release_wheel(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("installed-schema-wheel")
    source = sandbox / "source"
    source.mkdir()
    for name in ("pyproject.toml", "LICENSE", "NOTICE"):
        shutil.copy2(ROOT / name, source / name)
    for name in ("astrid", "astrid_runtime_client"):
        shutil.copytree(ROOT / name, source / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    _run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir",
          sandbox / "dist"], cwd=source)
    wheels = list((sandbox / "dist").glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _damage(schema, kind, outside):
    if kind == "missing":
        schema.unlink()
    elif kind == "symlink":
        target = outside / "identical-schema.json"
        target.write_bytes(schema.read_bytes())
        schema.unlink()
        schema.symlink_to(target)
    else:
        assert kind == "mismatch"
        schema.write_text("{}", encoding="utf-8")


@pytest.fixture
def staged_installer(tmp_path, monkeypatch, release_wheel):
    outside = tmp_path / "outside-checkout"
    outside.mkdir()
    root, data = tmp_path / "program", tmp_path / "data"
    root.mkdir()
    core.write_json(root / "install.json", {"owner": LABEL, "managed_by": "pinokio"})
    uv = tmp_path / "uv"
    uv.touch()
    backend = Backend(root, tmp_path / "install.log", uv, say=lambda _message: None,
                      managed_by="pinokio")
    manifest = {
        "release": 1, "agent": {"sha": "a" * 40}, "runtime": {"sha": PRODUCER},
        "tools": {"uv": {"version": "schema-test"},
                  "python": {"version": f"{sys.version_info.major}.{sys.version_info.minor}"}},
    }
    state = SimpleNamespace(backend=backend, root=root, data=data, outside=outside,
                            manifest=manifest, commands=[], damage_at=None, damage=None)

    def acquire(name, _sha, destination, **_kwargs):
        destination.mkdir(parents=True)
        if name == "agent":
            for relative in (SCHEMA, Path("runtime.lock.json")):
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            if state.damage_at == "source":
                _damage(destination / SCHEMA, state.damage, outside)
        return destination

    run_candidate = backend.run

    def run(argv, **kwargs):
        state.commands.append(list(map(str, argv)))
        if argv[0] != uv:
            return run_candidate(argv, cwd=outside, **kwargs)
        if argv[1] == "--version":
            return "uv schema-test"
        if argv[1] == "venv":
            environment = Path(argv[-1])
            venv.EnvBuilder(with_pip=True).create(environment)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            purelib = Path(_run([python, "-I", "-c",
                                 "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                cwd=outside))
            # Plain path entries do not execute the parent's editable-install
            # .pth hooks. Product origin assertions below forbid a fallback.
            (purelib / "test-dependencies.pth").write_text(
                sysconfig.get_path("purelib") + "\n", encoding="utf-8")
            state.product_root = purelib
            return ""
        assert argv[1:3] == ["pip", "install"]
        if "--require-hashes" in argv:
            return ""  # Dependency acquisition is outside this wheel proof.
        assert "--no-deps" in argv
        python = argv[argv.index("--python") + 1]
        diagnostic = subprocess.run(
            [python, "-I", "-m", "pip", "install", "--no-index", "--no-deps",
             "--ignore-installed", release_wheel],
            cwd=outside, capture_output=True, text=True, timeout=180, check=False,
        )
        pip_output = diagnostic.stdout + diagnostic.stderr
        assert diagnostic.returncode == 0, pip_output
        origins = json.loads(_run([python, "-I", "-c", r'''
import importlib, json
names = ("astrid", "astrid_runtime_client")
print(json.dumps({name: importlib.import_module(name).__file__ for name in names}))
'''], cwd=outside))
        escaped = {name: origin for name, origin in origins.items()
                   if not Path(origin).is_relative_to(state.product_root)}
        assert not escaped, (
            f"real wheel imports escaped child site-packages: {escaped}\n"
            f"child pip output:\n{pip_output}"
        )
        if state.damage_at == "wheel":
            _damage(state.product_root / SCHEMA, state.damage, outside)
        return ""

    def tools(_manifest, tool_root, _target, _run):
        npm = tool_root / "node/npm-cli.js"
        npm.parent.mkdir(parents=True)
        npm.touch()
        bun = tool_root / "bun"
        bun.touch()
        return {"PATH": os.defpath, "ASTRID_BUN_EXECUTABLE": str(bun)}

    monkeypatch.setattr("astrid.installer.backend.source", acquire)
    monkeypatch.setattr("astrid.installer.backend.install_tools", tools)
    monkeypatch.setattr(backend, "install_remotion_dependencies", lambda *_args: False)
    monkeypatch.setattr(backend, "run", run)
    return state


def _installed_script(state, version, script, *args):
    return json.loads(state.backend.run(
        [version.python, "-X", "utf8", "-I", "-B", "-c", script,
         state.product_root, ROOT, version.root / "a", *args], extra_env=version.env,
    ))


def test_stage_binds_installed_reader_validator_and_service_to_verified_schema(staged_installer, monkeypatch):
    state = staged_installer
    calls = []
    secure = state.backend._secure_installed_timeline_inputs
    verify = state.backend._verify_installed_timeline_schema

    def secure_inputs(root):
        calls.append(("secure", root))
        secure(root)

    def verify_inputs(python, root, env):
        calls.append(("verify", root))
        verify(python, root, env)

    monkeypatch.setattr(state.backend, "_secure_installed_timeline_inputs", secure_inputs)
    monkeypatch.setattr(state.backend, "_verify_installed_timeline_schema", verify_inputs)
    version = state.backend.stage(state.manifest, state.root, state.data)
    assert calls == [("secure", state.product_root), ("verify", state.product_root)]
    installed_schema = state.product_root / SCHEMA
    assert version.env["ASTRID_TIMELINE_SCHEMA_PYTHONPATH"] == str(installed_schema)
    assert hashlib.sha256(installed_schema.read_bytes()).hexdigest() == SCHEMA_SHA256
    assert (state.product_root / "runtime.lock.json").read_bytes() == (ROOT / "runtime.lock.json").read_bytes()
    assert state.backend._verify_local_timeline_schema(state.product_root) == installed_schema

    # The acquired checkout is deliberately different after installation. A
    # checkout or external package fallback cannot satisfy the assertions below.
    (version.root / "a" / SCHEMA).write_text("{}", encoding="utf-8")
    value = _installed_script(state, version, r'''
import hashlib, importlib, json, os, sys
from pathlib import Path
import jsonschema
from astrid.core.rendering import _local_schema
from astrid.core.timeline import banodoco_schema as reader
from astrid.core.execution.generic_host import source_checkout_digest
root, checkout, acquired = map(Path, sys.argv[1:4])
path = root / 'astrid/core/timeline/schema/timeline-config.schema.json'
assert sys.flags.isolated and Path(sys.prefix) == root.parents[2]
assert Path.cwd() != checkout and not Path.cwd().is_relative_to(checkout)
assert all(not Path(p).is_relative_to(checkout) and not Path(p).is_relative_to(acquired)
           for p in sys.path if p)
origins = {}
for name in ('astrid.services', 'astrid.core.rendering._local_schema',
             'astrid.core.timeline.banodoco_schema', 'astrid.core.execution.generic_host',
             'astrid_runtime_client'):
    origins[name] = importlib.import_module(name).__file__
    assert Path(origins[name]).is_relative_to(root)
assert reader._VENDORED_SCHEMA == path
assert os.environ['ASTRID_TIMELINE_SCHEMA_PYTHONPATH'] == str(path)
reads = []
sys.addaudithook(lambda event, args: reads.append(str(args[0]))
                if event == 'open' and str(args[0]) == str(path) else None)
schema = _local_schema.load_verified_schema(root, str(path))
assert reads
reads.clear()
assert reader.load_vendored_timeline_schema() == schema
assert reads  # The installed reader actually opened the admitted file.
reads.clear()
reader._shared_timeline_validator.cache_clear()
validator = reader._shared_timeline_validator()
assert validator.schema == schema and reads  # Compilation read that same file.
good = {'tracks': [{'id': 'v', 'kind': 'visual', 'label': 'Video', 'role': 'source'}], 'clips': []}
assert reader.validate_timeline_config_for_container(good) == good
for bad in ({'tracks': [], 'clips': 'invalid'},
            {'tracks': [{'id': 'v', 'kind': 'visual', 'label': 'Video', 'role': 'invalid'}], 'clips': []}):
    try:
        reader.validate_timeline_config_for_container(bad)
    except (ValueError, jsonschema.ValidationError):
        pass
    else:
        raise AssertionError('installed validator admitted an invalid Timeline')
lock = json.loads((root / 'runtime.lock.json').read_text())
print(json.dumps({'origins': origins, 'schema': str(path),
                  'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                  'producer': lock['source_commit'], 'digest': source_checkout_digest(root)}))
''')
    assert value["schema"] == str(installed_schema)
    assert value["sha256"] == SCHEMA_SHA256
    assert value["producer"] == PRODUCER
    profile = core.read_json(version.profile)
    assert profile["source_checkout"] == str(state.product_root)
    assert profile["source_digest"] == value["digest"]
    services = core.read_json(Path(profile["service_declaration"]))["services"]
    host = next(service for service in services if service["name"] == "pack-host")
    assert host["env"]["ASTRID_TIMELINE_SCHEMA_PYTHONPATH"] == str(installed_schema)
    assert host["argv"][host["argv"].index("--source-checkout") + 1] == str(state.product_root)
    chat = next(service for service in services if service["name"] == "chat")
    assert Path(chat["argv"][1]) == state.product_root / "astrid/chat/service.mjs"


@pytest.mark.parametrize("damage_at", ["source", "wheel"])
@pytest.mark.parametrize("damage", ["missing", "symlink", "mismatch"])
def test_stage_rejects_damaged_source_or_installed_schema_before_declaration(
        staged_installer, damage_at, damage):
    state = staged_installer
    state.damage_at, state.damage = damage_at, damage
    with pytest.raises(ValueError, match="local canonical timeline schema|Runtime source pin"):
        state.backend.stage(state.manifest, state.root, state.data)
    candidates = list((state.root / "versions").iterdir())
    assert len(candidates) == 1
    assert not (candidates[0] / "services.json").exists()
    assert not (candidates[0] / "profile.json").exists()
    assert not (state.root / "current").exists()
    assert not any("service_declaration" in arg or "source_checkout_digest" in arg
                   for command in state.commands for arg in command)
    if damage_at == "source":
        assert not any(command[1:2] == ["venv"] for command in state.commands)
    else:
        assert (candidates[0] / "a" / SCHEMA).read_bytes() == (ROOT / SCHEMA).read_bytes()


@pytest.mark.parametrize("damage", ["missing", "symlink", "mismatch"])
def test_cached_installed_consumers_and_update_refuse_changed_schema_before_stop(
        staged_installer, monkeypatch, damage):
    state = staged_installer
    candidate = state.backend.stage(state.manifest, state.root, state.data)
    result = _installed_script(state, candidate, r'''
import json, sys
from pathlib import Path
from astrid.core.rendering import _local_schema
from astrid.core.timeline import banodoco_schema as reader
root = Path(sys.argv[1])
path = root / 'astrid/core/timeline/schema/timeline-config.schema.json'
schema = _local_schema.load_verified_schema(root, str(path))
assert reader._shared_timeline_validator().schema == schema
assert reader.validate_timeline_config_for_container({'tracks': [], 'clips': []})
kind = sys.argv[4]
if kind == 'symlink':
    target = Path.cwd() / 'identical-installed-schema.json'
    target.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(target)
elif kind == 'missing':
    path.unlink()
else:
    assert kind == 'mismatch'
    path.write_text('{}')
rejected = []
for name, consume in (
    ('verified', lambda: _local_schema.load_verified_schema(root, str(path))),
    ('reader', reader.load_vendored_timeline_schema),
    ('cached-validator', lambda: reader.validate_timeline_config_for_container({'tracks': [], 'clips': []})),
):
    try:
        consume()
    except (OSError, ValueError):
        rejected.append(name)
    else:
        raise AssertionError(name + ' admitted a changed installed schema')
print(json.dumps(rejected))
''', damage)
    assert result == ["verified", "reader", "cached-validator"]

    old_root = state.root / "versions" / "old"
    old_python = old_root / "py/bin/python"
    old_python.parent.mkdir(parents=True)
    old_python.touch()
    old_profile = old_root / "profile.json"
    old_profile.write_text("{}", encoding="utf-8")
    old = core.Version(old_root, old_python, old_profile, {}, release=0, managed_by="pinokio")
    core.write_json(old_root / "version.json", old.document())
    core.switch(state.root, old)
    realm = {"realm_id": "preserved-realm", "realm_root": str(state.data / "realm")}
    owner = {"running": True, "pid": 12345, "birth_id": "original-owner"}
    record = {"owner": LABEL, "managed_by": "pinokio", "program_root": str(state.root),
              "data_root": str(state.data), **realm, "identity": old.document()["identity"]}
    core.write_json(state.root / "install.json", record)
    core.write_json(state.backend.service.selection, record)
    workspace = Path(realm["realm_root"])
    workspace.mkdir(parents=True)
    (workspace / "project-data.bin").write_bytes(b"retained project contents\x00\xff")
    core.write_json(workspace / "discovery.json", owner)
    preserved = [state.root / "current", state.root / "install.json",
                 state.backend.service.selection, workspace / "project-data.bin",
                 workspace / "discovery.json"]
    before = {path: path.read_bytes() for path in preserved}
    calls = []

    def workspace_selection(version, _data):
        calls.append("workspace:" + version.root.name)
        return realm.copy()

    def forbidden(name):
        def call(*_args, **_kwargs):
            calls.append(name)
            owner["running"] = False
            pytest.fail(name + " ran before candidate schema admission")
        return call

    monkeypatch.setattr(state.backend, "workspace", workspace_selection)
    monkeypatch.setattr(state.backend, "stage", lambda *_args: candidate)
    for name in ("stop", "backup", "upgrade", "register", "persist_identity"):
        monkeypatch.setattr(state.backend, name, forbidden(name))
    lifecycle = SimpleNamespace(
        validate=lambda version: calls.append("validate:" + version.root.name),
        stop=forbidden("lifecycle.stop"), start=forbidden("lifecycle.start"),
    )
    # Keep the real app selection, preparation and durable transaction engine.
    # A migration-capable update would stop, back up and upgrade after admission.
    with pytest.raises(ValueError, match="local canonical timeline schema|Runtime source pin"):
        app_update.update_pinokio(state.root, state.backend, state.manifest,
                                  lifecycle=lifecycle, say=lambda _message: None, force=True)
    assert calls == ["validate:old", "workspace:old"]
    assert owner == {"running": True, "pid": 12345, "birth_id": "original-owner"}
    assert core.current(state.root) == old
    assert {path: path.read_bytes() for path in preserved} == before
    assert not (state.root / "update-transaction.json").exists()
