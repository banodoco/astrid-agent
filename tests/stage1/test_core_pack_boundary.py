"""Real host composition with exactly the four core pack resource directories."""
from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys



_CORE = ("generation", "rendering", "timeline", "vibecomfy")
_GUARD = Path(__file__).resolve().parents[2] / "scripts" / "check_core_pack_boundary.py"


def _core_tree(tmp_path):
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "core-only"
    root.mkdir()
    shutil.copytree(source / "astrid", root / "astrid", ignore=shutil.ignore_patterns("packs", "__pycache__"))
    packs = root / "astrid" / "packs"
    packs.mkdir()
    for owner in _CORE:
        shutil.copytree(source / "astrid" / "packs" / owner, packs / owner,
                        ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("__init__.py", "engine-index.json"):
        path = source / "astrid" / "packs" / name
        if path.is_file():
            shutil.copy2(path, packs / name)
    shutil.copytree(source / "astrid_runtime_client", root / "astrid_runtime_client",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(source / "pyproject.toml", root / "pyproject.toml")
    assert sorted(path.name for path in packs.iterdir() if path.is_dir()) == sorted(_CORE)
    return root


def _probe(root):
    # Only declared source roots enter the child. No parent pack selector or
    # editable Agent checkout can rescue a missing optional resource.
    runtime = Path(os.environ["ASTRID_RUNTIME_CHECKOUT"])
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    env.update(PYTHONPATH=os.pathsep.join((str(root), str(runtime))),
               ASTRID_RUNTIME_CHECKOUT=str(runtime), ASTRID_PACKS_PATH="", PYTHONDONTWRITEBYTECODE="1")
    script = r'''
import importlib.util, json, pathlib, sys
root = pathlib.Path.cwd()
core = ('generation', 'rendering', 'timeline', 'vibecomfy')
reads = set()
def audit(event, args):
    if event not in ('open', 'os.listdir', 'os.scandir') or not args:
        return
    if not isinstance(args[0], (str, bytes)):
        return
    path = pathlib.Path(args[0].decode() if isinstance(args[0], bytes) else args[0])
    if not path.is_absolute():
        path = root / path
    try:
        relative = path.relative_to(root / 'astrid' / 'packs')
    except ValueError:
        return
    reads.add(relative.as_posix())
    assert not relative.parts or relative.parts[0] in (*core, '__init__.py', '__pycache__', 'engine-index.json'), str(path)
sys.addaudithook(audit)
import astrid
assert pathlib.Path(astrid.__file__).is_relative_to(root)
import astrid.core.gateway
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.generation import engine_recipe as recipes
from astrid.core.pack.canonical import BundledCatalog, read_normalize_validate
packs = root / 'astrid' / 'packs'
entries = tuple(read_normalize_validate(packs / owner / 'pack.yaml', source='local') for owner in core)
selected = BundledCatalog(packs, entries)
recipe = recipes.load_recipe('vibecomfy', 'windows-x86_64-cu130', registry=selected)
models = recipes.load_model_lock(recipe.models_lock, registry=selected)
plain = recipes.load_recipe('comfyui', 'linux-x86_64-cu130', registry=selected)
assert plain.pins == recipe.pins and plain.models_lock == recipe.models_lock
assert plain.environments[0].requirements.packaged_lock.path == 'packs/vibecomfy/requirements.linux-cu130.lock'
host = GenericPackHost(pack_roots=[packs / owner for owner in core], credential_source={})
records = host.discover()
assert records and any(record.id.startswith('vibecomfy.') for record in records)
assert recipe.pins.path == 'packs/vibecomfy/pins.json'
assert {s.name for s in recipe.sources} == {'comfyui', 'vibecomfy'}
assert len(models.models) == 3
for name in ('runpod', 'wan2gp', 'h3_av', 'comfyui'):
    assert not (packs / name).exists()
    assert importlib.util.find_spec('astrid.packs.' + name) is None
assert not any(name.startswith(('astrid.packs.runpod', 'astrid.packs.wan2gp', 'astrid.packs.h3_av')) for name in sys.modules)
print(json.dumps({'capabilities': len(records), 'engine': recipe.engine, 'reads': sorted(reads)}))
'''
    return subprocess.run([sys.executable, "-c", script], cwd=root, env=env, text=True, capture_output=True)


def _boundary_tree(tmp_path):
    """Copy the real core checkout into a self-contained, four-pack tree."""
    source = Path(__file__).resolve().parents[2]
    root = _core_tree(tmp_path)
    scripts = root / "scripts"
    baseline = scripts / "reshape" / "baselines"
    baseline.mkdir(parents=True)
    shutil.copy2(_GUARD, scripts / _GUARD.name)
    shutil.copy2(source / "scripts" / "reshape" / "baselines" / "core_pack_boundary.json",
                 baseline / "core_pack_boundary.json")
    return root


def _run_guard(root, *args):
    # The child sees only the independent tree and its ordinary interpreter
    # environment; the source checkout cannot satisfy accidental imports.
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    env.update(PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, str(root / "scripts" / _GUARD.name), *args],
                          cwd=root, env=env, text=True, capture_output=True)


def _guard_module():
    spec = importlib.util.spec_from_file_location("core_pack_boundary_guard", _GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_core_only_real_host_and_selected_recipe_composition(tmp_path):
    result = _probe(_core_tree(tmp_path))
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof["capabilities"] > 0
    assert proof["engine"] == "vibecomfy"
    assert "vibecomfy/pins.json" in proof["reads"]


def test_core_only_composition_detects_an_optional_import(tmp_path):
    root = _core_tree(tmp_path)
    gateway = root / "astrid" / "core" / "gateway" / "__init__.py"
    gateway.write_text(gateway.read_text() + "\nimport astrid.packs.h3_av\n")
    result = _probe(root)
    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr
    assert "astrid.packs.h3_av" in result.stderr


def test_core_pack_boundary_ratchet_passes(tmp_path):
    root = _boundary_tree(tmp_path)
    assert sorted(path.name for path in (root / "astrid" / "packs").iterdir() if path.is_dir()) == sorted(_CORE)
    result = _run_guard(root, "--check")
    assert result.returncode == 0, result.stderr


def test_core_pack_boundary_counts_imports_literals_and_duplicates():
    guard = _guard_module()
    findings = guard.scan_source(
        "astrid/core/probe.py",
        "import astrid.packs.hivemind as hm\n"
        "from astrid.packs import hivemind as pack\n"
        "name = 'hivemind'\nname = 'hivemind'\n",
        {"hivemind"}, {"hivemind.search"},
    )
    assert findings[("astrid/core/probe.py", "pack_import", "astrid.packs.hivemind")] == 1
    assert findings[("astrid/core/probe.py", "pack_import", "astrid.packs")] == 1
    assert findings[("astrid/core/probe.py", "pack_name_literal", "hivemind")] == 2


def test_mixed_import_aliases_detected():
    guard = _guard_module()
    findings = guard.scan_source(
        "astrid/core/probe.py",
        "import astrid.packs.hivemind as hm, importlib as il\n"
        "from astrid import packs as pack_root\n"
        "il.import_module('astrid.packs.hivemind')\n",
        {"hivemind"}, set(),
    )
    assert findings[("astrid/core/probe.py", "pack_import", "astrid.packs.hivemind")] == 1
    assert findings[("astrid/core/probe.py", "pack_import", "astrid.packs")] == 1
    assert findings[("astrid/core/probe.py", "dynamic_pack_import", "astrid.packs.hivemind")] == 1
    # Exact-string rule: a dotted module path is a dynamic import, not a bare name literal.
    assert findings[("astrid/core/probe.py", "pack_name_literal", "hivemind")] == 0


def test_moved_pack_identity_detected(tmp_path):
    root = tmp_path / "repo"
    packs = root / "astrid" / "packs"
    (packs / "moved_pack").mkdir(parents=True)
    # The directory remains the identity source while its manifest is absent.
    guard = _guard_module()
    names, _ = guard._pack_inventory(root)
    assert "moved_pack" in names
    findings = guard.scan_source("astrid/core/probe.py", "owner = 'moved_pack'\n", names, set())
    assert findings[("astrid/core/probe.py", "pack_name_literal", "moved_pack")] == 1


def test_manifest_action_key_detected(tmp_path):
    root = tmp_path / "repo"
    pack = root / "astrid" / "packs" / "hivemind"
    pack.mkdir(parents=True)
    (pack / "pack.yaml").write_text("id: hivemind\nactions:\n  search: {}\n", encoding="utf-8")
    names, capabilities = _guard_module()._pack_inventory(root)
    assert "hivemind" in names
    assert "hivemind.search" in capabilities
    findings = _guard_module().scan_source(
        "astrid/core/probe.py", "action = 'hivemind.search'\n", names, capabilities
    )
    assert findings[("astrid/core/probe.py", "capability_id", "hivemind.search")] == 1


def test_write_baseline_refuses_growth(tmp_path):
    root = tmp_path / "repo"
    (root / "astrid" / "core").mkdir(parents=True)
    packs = root / "astrid" / "packs"
    for name in _CORE:
        (packs / name).mkdir(parents=True)
    (packs / "hivemind").mkdir()
    script = root / "scripts" / _GUARD.name
    script.parent.mkdir(parents=True)
    shutil.copy2(_GUARD, script)
    baseline = root / "scripts" / "reshape" / "baselines" / "core_pack_boundary.json"
    baseline.parent.mkdir(parents=True)
    original = '{"schema": 1, "findings": []}\n'
    baseline.write_text(original, encoding="utf-8")
    (root / "astrid" / "core" / "probe.py").write_text("pack = 'hivemind'\n", encoding="utf-8")
    result = _run_guard(root, "--write-baseline")
    assert result.returncode == 1
    assert "refusing to grow" in result.stderr
    assert baseline.read_text(encoding="utf-8") == original


def test_remove_then_reintroduce_fails(tmp_path):
    root = tmp_path / "repo"
    (root / "astrid" / "core").mkdir(parents=True)
    packs = root / "astrid" / "packs"
    for name in _CORE:
        (packs / name).mkdir(parents=True)
    (packs / "hivemind").mkdir()
    script = root / "scripts" / _GUARD.name
    script.parent.mkdir(parents=True)
    shutil.copy2(_GUARD, script)
    baseline = root / "scripts" / "reshape" / "baselines" / "core_pack_boundary.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({"schema": 1, "findings": [
        {"file": "astrid/core/probe.py", "kind": "pack_name_literal", "value": "hivemind", "count": 1}
    ]}), encoding="utf-8")
    probe = root / "astrid" / "core" / "probe.py"
    probe.write_text("pack = 'hivemind'\n", encoding="utf-8")
    assert _run_guard(root, "--check").returncode == 0
    probe.write_text("pack = 'clear'\n", encoding="utf-8")
    assert _run_guard(root, "--check").returncode == 0
    probe.write_text("a = 'hivemind'\nb = 'hivemind'\n", encoding="utf-8")
    regrown = _run_guard(root, "--check")
    assert regrown.returncode == 1
    assert "pack_name_literal 'hivemind' x1" in regrown.stderr


def test_write_baseline_shrinks_then_reintroduced_finding_fails(tmp_path):
    root = tmp_path / "repo"
    (root / "astrid" / "core").mkdir(parents=True)
    packs = root / "astrid" / "packs"
    for name in _CORE:
        (packs / name).mkdir(parents=True)
    (packs / "hivemind").mkdir()
    script = root / "scripts" / _GUARD.name
    script.parent.mkdir(parents=True)
    shutil.copy2(_GUARD, script)
    baseline = root / "scripts" / "reshape" / "baselines" / "core_pack_boundary.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({"schema": 1, "findings": [
        {"file": "astrid/core/probe.py", "kind": "pack_name_literal", "value": "hivemind", "count": 1}
    ]}), encoding="utf-8")
    probe = root / "astrid" / "core" / "probe.py"
    probe.write_text("pack = 'hivemind'\n", encoding="utf-8")
    assert _run_guard(root, "--check").returncode == 0

    probe.write_text("pack = 'clear'\n", encoding="utf-8")
    shrunk = _run_guard(root, "--write-baseline")
    assert shrunk.returncode == 0, shrunk.stderr
    assert json.loads(baseline.read_text(encoding="utf-8")) == {"schema": 1, "findings": []}

    probe.write_text("pack = 'hivemind'\n", encoding="utf-8")
    for args in (("--check",), ("--write-baseline",)):
        regrown = _run_guard(root, *args)
        assert regrown.returncode == 1
        assert "pack_name_literal 'hivemind' x1" in regrown.stderr
    assert json.loads(baseline.read_text(encoding="utf-8")) == {"schema": 1, "findings": []}


def test_isolated_core_only_tree_has_no_optional_modules_and_injected_dependency_fails(tmp_path):
    root = _boundary_tree(tmp_path)
    packs = root / "astrid" / "packs"
    assert sorted(path.name for path in packs.iterdir() if path.is_dir()) == sorted(_CORE)
    source_packs = Path(__file__).resolve().parents[2] / "astrid" / "packs"
    discovered_optional_names = {
        path.name for path in source_packs.iterdir()
        if path.is_dir() and not path.name.startswith("_") and path.name not in _CORE
    }
    # CI removes optional pack directories before this test runs, so retain a
    # small known set to keep absence coverage for those packages in that lane.
    optional_names = sorted(discovered_optional_names | {"hivemind", "runpod", "wan2gp", "h3_av", "comfyui"})
    hivemind_core = root / "astrid" / "core" / "hivemind"
    assert hivemind_core.is_dir()
    assert (hivemind_core / "executors").is_dir()
    absence = subprocess.run(
        [sys.executable, "-c", "import importlib.util, json; names=json.loads(" + repr(json.dumps(optional_names)) + "); "
         "assert all(not __import__('pathlib').Path('astrid/packs', n).exists() for n in names); "
         "assert all(importlib.util.find_spec('astrid.packs.' + n) is None for n in names)"],
        cwd=root,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
    )
    assert absence.returncode == 0, absence.stderr
    assert _run_guard(root, "--check").returncode == 0
    (root / "astrid" / "core" / "injected_dependency.py").write_text(
        "import astrid.packs.h3_av\n", encoding="utf-8"
    )
    result = _run_guard(root, "--check")
    assert result.returncode == 1
    assert "pack_import" in result.stderr


def test_guard_rule_core_packs_exact_names_and_pinned_loader_exemption():
    """PK-3 guard rule (Plan 06 local run, STOP S5): core-pack names are not
    findings; other pack names count only as exact strings; the bare
    ``astrid.packs`` namespace is exempt only in the pinned loader modules."""
    guard = _guard_module()
    assert guard.CORE_PACKS == frozenset({"vibecomfy", "generation", "rendering", "timeline"})
    assert guard.PACK_LOADER_MODULES == frozenset({
        "astrid/core/execution/generic_host_worker.py",
        "astrid/core/pack/install.py",
        "astrid/core/pack/loader.py",
        "astrid/core/pack/transaction.py",
        "astrid/pack_imports.py",
        "astrid/core/pack/imports.py",
    })
    names = {"timeline", "media", "local"}
    source = (
        "DOC = 'A timeline of local media.'\n"
        "a = 'media'\nb = 'timeline'\nc = 'astrid.packs.'\nimport astrid.packs\n"
        "d = 'astrid.packs.timeline.cli'\n"
    )
    probe = guard.scan_source("astrid/core/probe.py", source, names, set())
    assert probe[("astrid/core/probe.py", "pack_name_literal", "media")] == 1
    assert probe[("astrid/core/probe.py", "pack_name_literal", "local")] == 0
    assert probe[("astrid/core/probe.py", "pack_name_literal", "timeline")] == 0
    assert probe[("astrid/core/probe.py", "dynamic_pack_import", "astrid.packs.")] == 1
    assert probe[("astrid/core/probe.py", "pack_import", "astrid.packs")] == 1
    assert probe[("astrid/core/probe.py", "dynamic_pack_import", "astrid.packs.timeline.cli")] == 0
    loader = guard.scan_source("astrid/core/pack/loader.py", source, names, set())
    assert loader[("astrid/core/pack/loader.py", "dynamic_pack_import", "astrid.packs.")] == 0
    assert loader[("astrid/core/pack/loader.py", "pack_import", "astrid.packs")] == 0


def test_core_cli_adds_no_pack_import_findings():
    """PK-4R (new): product parsers in astrid/core/cli never import pack modules
    beyond the baselined dispatch table. Reproduces the composed-head failure
    (park g4 astrid/core/cli/domain_timelines.py:23-82 name astrid.packs.timeline.*
    42 times) without running the whole ratchet."""
    from collections import Counter

    guard = _guard_module()
    source = Path(__file__).resolve().parents[2]
    names, capabilities = guard._pack_inventory(source)
    found: Counter = Counter()
    for path in sorted((source / "astrid" / "core" / "cli").glob("*.py")):
        relative = path.relative_to(source).as_posix()
        for key, count in guard.scan_source(relative, path.read_text(encoding="utf-8"), names, capabilities).items():
            if key[1] in ("pack_import", "dynamic_pack_import"):
                found[key] += count
    baseline = json.loads(guard.BASELINE.read_text(encoding="utf-8"))["findings"]
    allowed = Counter({(row["file"], row["kind"], row["value"]): int(row["count"]) for row in baseline
                       if row["file"].startswith("astrid/core/cli/")})
    grown = found - allowed
    assert not grown, sorted(grown)


def test_timelines_dispatch_parser_prints_help_with_core_packs_only(tmp_path):
    """PK-4R (new, behaviour guard): `astrid timelines --help` still works and its
    parser construction imports no non-core pack. Equivalent to the plan's
    `python -m astrid timelines --help` check minus Runtime/client composition."""
    script = r'''
import importlib, importlib.abc, sys
CORE = ("generation", "rendering", "timeline", "vibecomfy")
class OnlyCorePacks(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        parts = fullname.split(".")
        if parts[:2] == ["astrid", "packs"] and len(parts) > 2 and parts[2] not in CORE + ("_core",):
            raise AssertionError("timelines parser imported a non-core pack: " + fullname)
sys.meta_path.insert(0, OnlyCorePacks())
from astrid.core.cli.domain_product import FAMILY_PARSER_MODULES
parser = importlib.import_module(FAMILY_PARSER_MODULES["timelines"]).build_parser(None)
try:
    parser.parse_args(["--help"])
except SystemExit as exc:
    assert exc.code == 0, exc.code
else:
    raise AssertionError("--help did not exit")
'''
    source = Path(__file__).resolve().parents[2]
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "SYSTEMROOT", "ASTRID_RUNTIME_CHECKOUT") if key in os.environ}
    env.update(PYTHONPATH=os.pathsep.join(filter(None, (str(source), os.environ.get("ASTRID_RUNTIME_CHECKOUT")))),
               PYTHONDONTWRITEBYTECODE="1", HOME=str(tmp_path))
    result = subprocess.run([sys.executable, "-c", script], cwd=source, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "shots" in result.stdout and "render" in result.stdout
