"""Custody runner for the one generic-host harvest/reopen qualification.

The default mode is a fail-closed preflight. Normal manager enqueue and bench
leases authorize execution. ``run-original`` also checks the reviewed digest
to catch accidental calls; this is the only mode that starts the unchanged pytest body.  Production imports are resolved
by the managed child interpreter, with no ``PYTHONPATH`` or installed-tree
overlay.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

SOURCE_COMMIT = "0ff3d103fb892977b0853829f42b748a5d567851"
RUNTIME_COMMIT = "becfe6db88b2cc034dba3fafa6c050117cdf2b06"
TEST_RELATIVE = Path("tests/test_generic_host.py")
TEST_FUNCTION = "test_structure_pack_members_survive_harvest_cleanup_and_reopen"
PREFIXES = ("astrid", "astrid_runtime", "runtime_protocol", "astrid_runtime_client")
EXPECTED_PROFILE = Path("/home/user/work/runtimes/agent-remotion-v3-819acf-1110")
EXPECTED_NODE = Path("/home/user/work/toolchain/node-v20.19.4-linux-x64/bin/node")
EXPECTED_NODE_VERSION = "v20.19.4"
EXPECTED_ARCHIVE_SHA256 = "819acf6b3815d6eeabe772a9569581fedbe4f9c9cce35946efca9454e2040234"
EXPECTED_PROFILE_FILES = {
    "package-lock.json": "f4fc0a32a8822e956dfa21ff35d19c0c83d147c754cdb3546f91c84e330be18d",
    ".astrid-registry.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ".astrid-registry-state.json": "d609b44b37d724358a33405e9d8539008fd1ac30bc2f0d9746575c3fbadaac96",
}
EXPECTED_TEST_IDENTITY = {
    "file_sha256": "b4b99d0bd4336f26d65c7763d87d28f0ca98fc5f92b02a70ba41a43c17453709",
    "function_source_sha256": "c5e09e12bf2a898571b9c6888604ac658f2884a5f2cb33532a24aee4d226f211",
    "function_ast_sha256": "549a1b07affc5d59365da430a8a31887074544b50a2a3b14575d30e1a275bb6b",
    "assertion_count": 12,
    "assertion_ast_sha256": [
        "589e2c1ea0e9c3c4f93b8b3b3a785884aa12cc484e3addf21bc977dd68807ec8",
        "ad50885719d8833eba7e8105ebbc20554541fc2263b2c2cca0d2fbb8400286a5",
        "e643f450fb4532b784d5fad2f3df8c641b043ff1dfbf35cc90f5e914e2cc4a41",
        "a494599e2450c152cbdfb815fb21a1fbdd626f5aacf8b41abef01fa83cddc12c",
        "72d2c7016083f09def50d83741d11050e66dcd48b04c1a119f76b49fcd162dc5",
        "1de374522d2309e4da5b40ceaa852c7b780a1eabc2a9c417291b4485cc435c7f",
        "6bfd1867fff52b71f7f671b1dbb0ea69663bfc2f3686e7b0707e239b291f6012",
        "815adbb0a394022f82c45d23de7682f396e10bc7448a6cc57994986994b7b480",
        "81bdc7f6948097c132454be5ab0758f6c4a623c150cc51340a6e1306d5dd1022",
        "22260ca4efefb72fd34c59be3578c363aa9cb8a0d6613e6d695514623158abf4",
        "edadff1d8207c852d74217d8b8fe5b42bc49b96115a0ea19b132971fcbb29195",
        "e961a9537c2f82344a3e220fad4beea000d79c4e4231d8586057b9a6c2f3d3ed",
    ],
    "body_bytes": 4870,
}
SCRIPT = Path(__file__).resolve()
ORCH = Path("/tmp/claude-0/-home-user-repo/7dc61be1-7066-52e0-a5d0-94f90e61430a/scratchpad/orch")
SOURCE_ROOT = Path("/home/user/work/wt/agt-pin-0ff3d103")
RUNTIME_ROOT = Path("/home/user/work/wt/rt-pin-becfe6db")
STORE_ROOT = Path("/home/user/work/bench-store")
CHILD_NAME = "venv-agent-child-final-0ff3-becfe"
SAFE_ENV_KEYS = (
    "PYTHONPATH", "PYTHONNOUSERSITE", "ASTRID_RUNTIME_CHECKOUT",
    "ASTRID_HOST_EXECUTION_PROFILE", "ASTRID_REMOTION_PROJECT_DIR", "ASTRID_NODE_EXECUTABLE",
)


class PreflightFailure(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    return sha256(path.read_bytes())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def require_file(path: Path, label: str) -> Path:
    if not path.is_file() or path.is_symlink():
        raise PreflightFailure(f"{label} is missing or is a symlink: {path}")
    return path


def real_child(path: Path) -> Path:
    if not path.is_dir() or path.is_symlink():
        raise PreflightFailure(f"managed child root is missing or is a symlink: {path}")
    resolved = path.resolve()
    if not (resolved / "bin/python").is_file():
        raise PreflightFailure(f"managed child has no bin/python: {resolved}")
    return resolved


def source_identity(root: Path) -> dict[str, Any]:
    test_path = require_file(root / TEST_RELATIVE, "target test source")
    data = test_path.read_bytes()
    text = data.decode("utf-8")
    tree = ast.parse(text, filename=str(test_path))
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == TEST_FUNCTION
        ),
        None,
    )
    if function is None:
        raise PreflightFailure(f"target function not found: {TEST_FUNCTION}")
    lines = text.splitlines()
    body = "\n".join(lines[function.lineno - 1 : function.end_lineno])
    assertions = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    identity = {
        "path": str(test_path),
        "file_sha256": sha256(data),
        "function_source_sha256": sha256(body.encode("utf-8")),
        "function_ast_sha256": sha256(ast.dump(function, include_attributes=True).encode()),
        "assertion_count": len(assertions),
        "assertion_ast_sha256": [
            sha256(ast.dump(node, include_attributes=True).encode()) for node in assertions
        ],
        "body_bytes": len(body.encode("utf-8")),
    }
    expected = {"path": str(test_path), **EXPECTED_TEST_IDENTITY}
    if identity != expected:
        raise PreflightFailure(
            "target test source does not match the independently reviewed body/function/assertion hashes"
        )
    return identity


def git_identity(root: Path, expected: str = SOURCE_COMMIT) -> dict[str, Any]:
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.STDOUT
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PreflightFailure(f"cannot read source git identity: {exc}") from exc
    changed = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        text=True,
    ).strip()
    if head != expected:
        raise PreflightFailure(f"source HEAD is {head}, expected reviewed source {expected}")
    if changed:
        raise PreflightFailure(f"source tree is modified: {changed}")
    return {"head": head, "source_tree_status": "clean"}


def client_identity() -> dict[str, str]:
    paths = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", RUNTIME_COMMIT, "packages/python/astrid_runtime_client/"],
        cwd=RUNTIME_ROOT, text=True,
    ).splitlines()
    if not paths:
        raise PreflightFailure("fixed Runtime pin has no generated client")
    return {path: sha256(subprocess.check_output(
        ["git", "show", f"{RUNTIME_COMMIT}:{path}"], cwd=RUNTIME_ROOT,
    )) for path in paths}


def bench_store() -> Any:
    spec = importlib.util.spec_from_file_location("bench_store", ORCH / "bin/bench_store.py")
    if spec is None or spec.loader is None:
        raise PreflightFailure("normal bench-store helper unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.Store(STORE_ROOT, ORCH / "tests")


def managed_custody() -> dict[str, Any]:
    """Use normal manager-owned registration, projection and immutable verifier."""
    store = bench_store()
    entries = store.registration_entries(CHILD_NAME)
    children = [entry for entry in entries if entry["component"] == "child"]
    expected: dict[str, Any] = {"agent_sha": SOURCE_COMMIT, "backend_sha": RUNTIME_COMMIT,
                "client_tuple": client_identity()}
    if len(children) != 1 or children[0]["identity"] != expected:
        raise PreflightFailure("normal registration does not bind the exact 0ff3/becfe/client tuple")
    entry = children[0]
    child = real_child(Path(entry["path"]))
    report = require_file(ORCH / "tests/provision" / (CHILD_NAME + ".txt"), "normal provision report")
    report_bytes = report.read_bytes()
    if report_bytes != require_file(child / ".provision-report", "retained provision report").read_bytes():
        raise PreflightFailure("normal projected/retained provision reports differ")
    lines = report_bytes.decode().splitlines()
    client = "\n".join(f"{key}={value}" for key, value in expected["client_tuple"].items())
    for marker, expected_value in ((".astrid-source-commit", SOURCE_COMMIT),
                                   (".astrid-runtime-source-commit", RUNTIME_COMMIT),
                                   (".astrid-runtime-client-identity", client)):
        if require_file(child / marker, marker).read_text().rstrip("\n") != expected_value:
            raise PreflightFailure(f"normal child source/client marker mismatch: {marker}")
    for key, value in {
        "status": "passed", "agent_sha": SOURCE_COMMIT, "runtime_sha": RUNTIME_COMMIT,
        "requirements_path": "requirements/proof.lock",
        "requirements_lock_sha256": file_digest(SOURCE_ROOT / "requirements/proof.lock"),
        "runtime_client_identity_sha256": sha256(client.encode()),
    }.items():
        if [line for line in lines if line.startswith(key + "=")] != [key + "=" + value]:
            raise PreflightFailure(f"normal provision report identity mismatch: {key}")
    for section, end, marker in (
        ("installed_import_origins:", "installed_distribution_metadata:", ".astrid-import-origins"),
        ("installed_distribution_metadata:", "pip_freeze:", ".astrid-distribution-metadata"),
    ):
        if lines.count(section) != 1 or lines.count(end) != 1:
            raise PreflightFailure("normal provision report sections missing or duplicated")
        if lines[lines.index(section) + 1:lines.index(end)] != (child / marker).read_text().splitlines():
            raise PreflightFailure(f"normal provision report marker mismatch: {marker}")
    # This is the normal maker's read-only verifier, not a build or reuse request.
    # It checks sealed sources against git archives, complete RECORD byte/ownership
    # coverage, directory provenance, interpreter and canonical generated client.
    maker = require_file(ORCH / "bin/make-child-venv", "normal child maker").read_text()
    begin, end = "# STORE_VERIFY_BEGIN\n", "# STORE_VERIFY_END"
    if maker.count(begin) != 1 or maker.split(begin)[1].count(end) != 1:
        raise PreflightFailure("normal maker verifier boundary is ambiguous")
    verifier = maker.split(begin)[1].split(end)[0]
    result = subprocess.run(
        [sys.executable, "-I", "-", str(child), client, str(SOURCE_ROOT), SOURCE_COMMIT,
         str(RUNTIME_ROOT), RUNTIME_COMMIT, "/home/user/work", "python3.13"],
        input=verifier, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise PreflightFailure(f"normal immutable source/RECORD verifier failed: {result.stderr}")
    identity_begin = "\nIDENTITY_SCRIPT=" + "'"
    identity_end = "'\nrun_stage"
    if maker.count(identity_begin) != 1:
        raise PreflightFailure("normal freeze identity verifier unavailable")
    identity_script = maker.split(identity_begin)[1].split(identity_end)[0]
    snapshots = list(child.glob(".child-venv-source-*"))
    if len(snapshots) != 1:
        raise PreflightFailure("normal child source snapshots are ambiguous")
    freeze = subprocess.run(
        [str(child / "bin/python"), "-B", "-I", "-c", identity_script,
         str(child / ".provision-report"), str(child), str(snapshots[0] / "agent"),
         str(snapshots[0] / "runtime"), SOURCE_COMMIT, RUNTIME_COMMIT],
        text=True, capture_output=True, check=False,
    )
    if freeze.returncode:
        raise PreflightFailure(f"normal installed distribution/freeze identity failed: {freeze.stderr}")
    # Also validate hash/size consistency for every dependency distribution. Only
    # Agent/Runtime/client RECORDs have immutable-source authority from the maker.
    record_check = (
        "import base64,csv,hashlib,pathlib,sys; root=pathlib.Path(sys.argv[1]); "
        "site=root/'lib/python3.13/site-packages'; count=0\n"
        "for record in site.glob('*.dist-info/RECORD'):\n"
        " seen=set()\n"
        " for rel,encoded,size in csv.reader(record.open()):\n"
        "  path=site/rel; resolved=path.resolve(); pure=pathlib.PurePosixPath(rel)\n"
        "  assert str(pure)==rel and rel and rel not in seen and not pure.is_absolute() and '\\\\' not in rel\n"
        "  assert resolved.is_relative_to(root) and path.is_file() and not path.is_symlink()\n"
        "  seen.add(rel); data=path.read_bytes()\n"
        "  if not encoded and not size:\n"
        "   assert path==record or (rel.endswith('.pyc') and '__pycache__' in pure.parts)\n"
        "  else:\n"
        "   assert encoded=='sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')\n"
        "   assert size==str(len(data))\n"
        " count+=1\n"
        "assert count; print(count)\n"
    )
    checked = subprocess.run([sys.executable, "-I", "-c", record_check, str(child)],
                             text=True, capture_output=True, check=False)
    if checked.returncode:
        raise PreflightFailure(f"dependency RECORD verification failed: {checked.stderr}")
    return {"child": str(child), "entry": entry, "registration": CHILD_NAME,
            "graph": entries, "receipt": {"path": str(report), "sha256": sha256(report_bytes)},
            "maker_verifier_sha256": sha256(verifier.encode()),
            "distribution_records_checked": int(checked.stdout),
            "authority": "normal bench-store projection + maker immutable source/RECORD/client verifier"}


def profile_inputs(profile: Path, node: Path) -> dict[str, Any]:
    if profile != EXPECTED_PROFILE:
        raise PreflightFailure(f"profile root is not the approved 819acf profile: {profile}")
    if node != EXPECTED_NODE:
        raise PreflightFailure(f"Node executable is not the approved Node20 path: {node}")
    if profile.resolve() != profile or not profile.is_dir() or profile.is_symlink():
        raise PreflightFailure(f"profile root is missing or symlinked: {profile}")
    if node.resolve() != node or not node.is_file() or node.is_symlink():
        raise PreflightFailure(f"profile Node executable is missing or symlinked: {node}")
    locks = []
    for name, expected in EXPECTED_PROFILE_FILES.items():
        path = require_file(profile / name, "profile lock/input")
        actual = file_digest(path)
        if actual != expected:
            raise PreflightFailure(f"approved profile {name} is {actual}, expected {expected}")
        locks.append({"path": str(path), "sha256": actual, "bytes": path.stat().st_size})
    package = json.loads(require_file(profile / "package.json", "profile package manifest").read_text(encoding="utf-8"))
    if package.get("engines") != {"node": "=20.19.4", "npm": "=10.8.2"}:
        raise PreflightFailure("profile engine requirements changed")
    dependency_spec = {
        **package.get("dependencies", {}),
        **package.get("devDependencies", {}),
    }
    package_lock = json.loads((profile / "package-lock.json").read_text(encoding="utf-8"))
    lock_packages = package_lock.get("packages", {})
    node_modules = profile / "node_modules"
    if not node_modules.is_dir() or node_modules.is_symlink():
        raise PreflightFailure(f"profile node_modules is missing or symlinked: {node_modules}")
    installed: dict[str, Any] = {}
    for dependency in sorted(dependency_spec):
        package_path = node_modules.joinpath(*dependency.split("/"))
        lock_key = f"node_modules/{dependency}"
        if lock_key not in lock_packages:
            raise PreflightFailure(f"profile lock lacks direct dependency {dependency}")
        if not package_path.is_dir() or package_path.is_symlink():
            raise PreflightFailure(f"required profile dependency is missing or symlinked: {dependency}")
        manifest = require_file(package_path / "package.json", f"{dependency} package manifest")
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if value.get("name") != dependency:
            raise PreflightFailure(f"profile dependency manifest name mismatch: {dependency}")
        expected_version = lock_packages[lock_key].get("version")
        if expected_version is not None and value.get("version") != expected_version:
            raise PreflightFailure(f"profile dependency version mismatch: {dependency}")
        installed[dependency] = {"path": str(package_path), "version": value.get("version")}
    composition = dependency_spec.get("@banodoco/timeline-composition")
    if composition != "file:artifacts/banodoco-timeline-composition-635f4de06fda22a85fc2e4cfc764d0153a4c9f40.tgz":
        raise PreflightFailure("profile composition dependency is not the approved archive")
    archive = profile / composition.removeprefix("file:")
    require_file(archive, "Composition archive")
    archive_sha256 = file_digest(archive)
    if archive_sha256 != EXPECTED_ARCHIVE_SHA256:
        raise PreflightFailure(f"Composition archive is {archive_sha256}, expected {EXPECTED_ARCHIVE_SHA256}")
    composition_manifest = json.loads(
        require_file(node_modules / "@banodoco" / "timeline-composition" / "package.json", "Composition package manifest").read_text(
            encoding="utf-8"
        )
    )
    if composition_manifest.get("version") != "0.0.14":
        raise PreflightFailure("installed Composition package is not version 0.0.14")
    composition_root = node_modules / "@banodoco/timeline-composition"
    archive_files: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(archive.read_bytes()), mode="r:gz") as packed:
        for member in packed:
            if member.isdir():
                continue
            relative = Path(member.name)
            if not member.isfile() or relative.parts[0] != "package" or ".." in relative.parts:
                raise PreflightFailure("unsafe Composition archive member")
            stream = packed.extractfile(member)
            if stream is None:
                raise PreflightFailure("unreadable Composition archive member")
            rel = str(relative.relative_to("package"))
            if rel in archive_files:
                raise PreflightFailure("duplicate Composition archive member")
            installed_file = require_file(composition_root / rel, "installed Composition file")
            digest = sha256(stream.read())
            if file_digest(installed_file) != digest:
                raise PreflightFailure(f"installed Composition differs from approved archive: {rel}")
            archive_files[rel] = digest
    actual_files = {str(path.relative_to(composition_root)) for path in composition_root.rglob("*") if path.is_file()}
    if actual_files != set(archive_files):
        raise PreflightFailure("installed Composition file set differs from approved archive")
    # Validate the full locked install and ask Node for actual dependency/peer
    # resolution from each installed package (including Composition).
    installed_tree: dict[str, Any] = {}
    edges = [[str(profile / "package.json"), name, str(node_modules / name)] for name in sorted(dependency_spec)]
    def installed_packages(folder: Path) -> set[str]:
        result = set()
        for item in folder.iterdir():
            if item.name.startswith("."):
                continue
            packages = list(item.iterdir()) if item.name.startswith("@") else [item]
            for package_path in packages:
                if package_path.is_symlink() or not package_path.is_dir():
                    raise PreflightFailure(f"unsafe installed package path: {package_path}")
                result.add(str(package_path.relative_to(profile)))
                nested = package_path / "node_modules"
                if nested.exists():
                    result.update(installed_packages(nested))
        return result
    extras = installed_packages(node_modules) - set(lock_packages)
    if extras:
        raise PreflightFailure(f"installed packages outside approved lock: {sorted(extras)}")
    for key, locked in sorted(lock_packages.items()):
        if not key:
            if dependency_spec != {**locked.get("dependencies", {}), **locked.get("devDependencies", {})}:
                raise PreflightFailure("profile manifest dependency set differs from approved lock")
            continue
        path = profile / key
        if not path.exists() and (locked.get("optional") or locked.get("os") or locked.get("cpu")):
            continue
        if not path.is_dir() or path.is_symlink() or not path.resolve().is_relative_to(node_modules):
            raise PreflightFailure(f"locked package missing or escaping profile: {key}")
        manifest = require_file(path / "package.json", "locked package manifest")
        value = json.loads(manifest.read_text())
        if value.get("version") != locked.get("version"):
            raise PreflightFailure(f"locked transitive package version differs: {key}")
        for field in ("dependencies", "optionalDependencies", "peerDependencies", "peerDependenciesMeta"):
            if value.get(field, {}) != locked.get(field, {}):
                raise PreflightFailure(f"installed dependency graph differs from lock: {key}/{field}")
        installed_tree[key] = {"manifest_sha256": file_digest(manifest), "version": value.get("version")}
        dependencies = {**locked.get("dependencies", {}), **locked.get("optionalDependencies", {}),
                        **locked.get("peerDependencies", {})}
        for name in sorted(dependencies):
            parent = path
            resolved = None
            while parent.is_relative_to(profile):
                candidate = parent / "node_modules" / name
                if candidate.is_dir():
                    resolved = candidate
                    break
                parent = parent.parent
            optional = name in locked.get("optionalDependencies", {}) or locked.get("peerDependenciesMeta", {}).get(name, {}).get("optional")
            if resolved is None:
                if optional:
                    continue
                raise PreflightFailure(f"required dependency/peer absent: {key}/{name}")
            resolved_key = str(resolved.relative_to(profile))
            if resolved_key not in lock_packages:
                raise PreflightFailure(f"dependency/peer resolved outside lock: {key}/{name}")
            edges.append([str(manifest), name, str(resolved.resolve())])
    resolution_probe = r"""
const fs = require('node:fs'); const path = require('node:path');
const {createRequire} = require('node:module');
const edges = JSON.parse(fs.readFileSync(0, 'utf8'));
for (const [manifest, name, expected] of edges) {
  const local = createRequire(manifest);
  let entry;
  try { entry = local.resolve(name + '/package.json'); }
  catch { entry = local.resolve(name); }
  let directory = path.dirname(entry);
  while (directory !== path.dirname(directory)) {
    const candidate = path.join(directory, 'package.json');
    if (fs.existsSync(candidate) && JSON.parse(fs.readFileSync(candidate, 'utf8')).name === name) break;
    directory = path.dirname(directory);
  }
  if (fs.realpathSync(directory) !== expected) throw Error('dependency/peer resolution mismatch: ' + manifest + '/' + name);
}
process.stdout.write(JSON.stringify(edges));
"""
    resolution = subprocess.run([str(node), "-e", resolution_probe], input=json.dumps(edges),
                                text=True, capture_output=True, check=False,
                                env={"PATH": f"{node.parent}:/usr/bin:/bin"})
    if resolution.returncode:
        raise PreflightFailure(f"actual Node dependency/peer resolution failed: {resolution.stderr}")
    version = subprocess.check_output([str(node), "--version"], text=True, stderr=subprocess.STDOUT).strip()
    if version != EXPECTED_NODE_VERSION:
        raise PreflightFailure(f"profile Node is {version}, expected {EXPECTED_NODE_VERSION}")
    return {
        "root": str(profile),
        "node": str(node),
        "node_version": version,
        "locks": locks,
        "composition_archive": {"path": str(archive), "sha256": archive_sha256},
        "node_modules": {"path": str(node_modules), "required": installed, "locked_tree": installed_tree,
                         "actual_resolution": json.loads(resolution.stdout)},
        "installed_composition_files": archive_files,
    }


def controlled_env(child: Path, profile: dict[str, Any], scratch: Path) -> dict[str, str]:
    node = Path(profile["node"])
    remotion = Path(profile["root"])
    return {
        "HOME": os.environ.get("HOME", "/root"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": f"{node.parent}:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(scratch),
        "ASTRID_HOST_EXECUTION_PROFILE": "installed",
        "ASTRID_NODE_EXECUTABLE": str(node),
        "ASTRID_REMOTION_PROJECT_DIR": str(remotion),
        "ASTRID_TEST_NODE_MODULES": str(remotion / "node_modules"),
    }


def child_probe(child: Path, root: Path, env: dict[str, str]) -> dict[str, Any]:
    probe = (
        "import importlib, json, sys; "
        f"names={PREFIXES!r}; "
        "mods={}; "
        "for_name=None; "
        "[mods.__setitem__(name, (m:=importlib.import_module(name)).__file__) for name in names]; "
        "print(json.dumps({'executable':sys.executable,'prefix':sys.prefix,'path':sys.path,'modules':mods}, sort_keys=True))"
    )
    result = subprocess.run(
        [str(child / "bin/python"), "-P", "-c", probe],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise PreflightFailure(f"installed child import probe failed ({result.returncode}): {result.stderr.strip()}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PreflightFailure(f"installed child import probe was not JSON: {result.stdout!r}") from exc
    site = (child / "lib" / "python3.13" / "site-packages").resolve()
    if Path(value.get("prefix", "")).resolve() != child:
        raise PreflightFailure("import probe used a different interpreter prefix")
    origins = {}
    for name, filename in value.get("modules", {}).items():
        if not isinstance(filename, str):
            raise PreflightFailure(f"module has no file origin: {name}")
        resolved = Path(filename).resolve()
        if resolved != site / name / "__init__.py":
            raise PreflightFailure(f"production module escaped managed site-packages: {name} -> {resolved}")
        carrier = SOURCE_ROOT if name in ("astrid", "astrid_runtime_client") else RUNTIME_ROOT
        expected_digest = file_digest(carrier / name / "__init__.py")
        if file_digest(resolved) != expected_digest:
            raise PreflightFailure(f"installed origin bytes differ from fixed source pin: {name}")
        origins[name] = {"file": filename, "realpath": str(resolved), "sha256": file_digest(resolved)}
    if set(origins) != set(PREFIXES):
        raise PreflightFailure(f"installed origin probe omitted modules: {set(PREFIXES) - set(origins)}")
    return {
        "probe_argv": [str(child / "bin/python"), "-P", "-c", probe],
        "result": value,
        "origins": origins,
        "observation_scope": "fresh child -P import probe; not an in-body observer",
    }


def suite_row(junit: Path, exit_code: int, started: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"nodeid": f"{TEST_RELATIVE}::{TEST_FUNCTION}",
                              "exit_code": exit_code, "junit": None, "outcome": "NOTRUN"}
    if not started:
        return result
    result.update(junit=str(junit), outcome="no-junit")
    if not junit.is_file():
        return result
    root = ET.parse(junit).getroot()
    cases = list(root.iter("testcase"))
    if len(cases) != 1:
        result.update(outcome="unexpected-testcase-count", testcase_count=len(cases))
        return result
    case = cases[0]
    result.update(name=case.get("name"), classname=case.get("classname"))
    if case.get("name") != TEST_FUNCTION or case.get("classname") != "tests.test_generic_host":
        result["outcome"] = "unexpected-testcase-identity"
    elif any(case.find(kind) is not None for kind in ("failure", "error", "skipped")):
        result["outcome"] = "failed"
    elif exit_code:
        result["outcome"] = "failed"
    else:
        result["outcome"] = "passed"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "run-original"), nargs="?", default="preflight")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--child-name", choices=(CHILD_NAME,), required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--tmpdir", type=Path, required=True)
    parser.add_argument("--reviewed-script-sha256")
    args = parser.parse_args()
    evidence = args.evidence.absolute()
    # A fresh directory prevents stale JUnit/results from qualifying a new run.
    evidence.mkdir(parents=True, exist_ok=False)
    started_at = time.time()
    leases = ExitStack()
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for name in ("source", "body", "provision", "profile", "origins"):
        before[name] = {"status": "NOTRUN", "reason": "not reached"}
        after[name] = {"status": "NOTRUN", "reason": "body not started"}
    env = controlled_env(Path("/unused"), {"node": str(EXPECTED_NODE), "root": str(EXPECTED_PROFILE)}, args.tmpdir)
    invocation = {"argv": sys.orig_argv, "cwd": str(Path.cwd()),
                  "safe_environment": {key: os.environ.get(key) for key in SAFE_ENV_KEYS},
                  "body_safe_environment": {key: env.get(key) for key in SAFE_ENV_KEYS},
                  "source_root": str(args.source_root), "runtime_root": str(RUNTIME_ROOT),
                  "registration": CHILD_NAME, "script_sha256": file_digest(SCRIPT),
                  "body_argv": None, "body_cwd": str(SOURCE_ROOT)}
    write_json(evidence / "invocation-before.json", invocation)
    write_json(evidence / "before.json", before)
    body_started = False
    body_exit: int | None = None
    exit_code = 2
    failure: dict[str, str] | None = None
    stdout = b""
    stderr = b""
    body_start: float | None = None
    body_end: float | None = None
    child: Path | None = None

    def check(name: str, fn: Any, destination: dict[str, Any], phase: str) -> Any:
        try:
            value = fn()
            destination[name] = {"status": "PASS", "value": value}
        except Exception as exc:  # noqa: BLE001 - persist partial custody on every failure.
            destination[name] = {"status": "FAIL", "error_type": type(exc).__name__, "error": str(exc)}
            raise
        finally:
            write_json(evidence / f"{phase}.json", destination)
        return value

    def sources() -> dict[str, Any]:
        if args.source_root != SOURCE_ROOT or SOURCE_ROOT.resolve() != SOURCE_ROOT or RUNTIME_ROOT.resolve() != RUNTIME_ROOT:
            raise PreflightFailure("explicit source carrier must be the approved physical 0ff3 path")
        return {"agent": git_identity(SOURCE_ROOT), "runtime": git_identity(RUNTIME_ROOT, RUNTIME_COMMIT)}

    def provision() -> dict[str, Any]:
        leases.enter_context(bench_store().acquire([CHILD_NAME]))
        return managed_custody()

    try:
        if args.mode == "run-original" and args.reviewed_script_sha256 != file_digest(SCRIPT):
            raise PreflightFailure("run-original requires the reviewed harness SHA")
        check("source", sources, before, "before")
        check("body", lambda: source_identity(SOURCE_ROOT), before, "before")
        custody = check("provision", provision, before, "before")
        child = Path(custody["child"])
        profile = check("profile", lambda: profile_inputs(EXPECTED_PROFILE, EXPECTED_NODE), before, "before")
        args.tmpdir.mkdir(parents=True, exist_ok=True)
        env = controlled_env(child, profile, args.tmpdir.resolve())
        check("origins", lambda: child_probe(child, SOURCE_ROOT, env), before, "before")
        if args.mode == "run-original":
            # Accidental-call check only. Execution authorization is the normal
            # manager enqueue/test-service lease; this digest is not source authority.
            if args.reviewed_script_sha256 != file_digest(SCRIPT):
                raise PreflightFailure("run-original requires the reviewed harness SHA")
            command = [str(child / "bin/python"), "-P", "-m", "pytest", "--import-mode=importlib",
                       "-q", "-oaddopts=", "-p", "no:cacheprovider",
                       f"{TEST_RELATIVE}::{TEST_FUNCTION}", f"--junitxml={evidence / 'junit.xml'}"]
            invocation.update(body_argv=command, body_safe_environment={key: env.get(key) for key in SAFE_ENV_KEYS})
            write_json(evidence / "invocation-before.json", invocation)
            body_start = time.time()
            write_json(evidence / "body-start.json", {"started_at": body_start, "argv": command,
                                                       "cwd": str(SOURCE_ROOT), "timeout_seconds": 30})
            try:
                with subprocess.Popen(command, cwd=SOURCE_ROOT, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, start_new_session=True) as process:
                    body_started = True
                    try:
                        stdout, stderr = process.communicate(timeout=30)
                        body_exit = process.returncode
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        stdout, stderr = process.communicate()
                        body_exit = 124
            finally:
                body_end = time.time()
            exit_code = body_exit if body_exit is not None else 2
        else:
            exit_code = 0
    except Exception as exc:  # noqa: BLE001 - durable diagnostics must survive setup/launch failures.
        failure = {"error_type": type(exc).__name__, "error": str(exc)}
        exit_code = 2
    finally:
        # Recheck every boundary, even if another postcheck fails. Before-body
        # failures retain explicit NOTRUN observations rather than invented data.
        if body_started and child is not None:
            checks = (("source", sources), ("body", lambda: source_identity(SOURCE_ROOT)),
                      ("provision", managed_custody),
                      ("profile", lambda: profile_inputs(EXPECTED_PROFILE, EXPECTED_NODE)),
                      ("origins", lambda: child_probe(Path(child), SOURCE_ROOT, env)))
            for name, fn in checks:
                try:
                    value = check(name, fn, after, "after")
                    if value != before[name].get("value"):
                        raise PreflightFailure(f"{name} custody changed during body")
                except Exception as exc:  # noqa: BLE001 - attempt all independent postchecks.
                    after[name] = {"status": "FAIL", "error_type": type(exc).__name__, "error": str(exc)}
                    exit_code = 2
        write_json(evidence / "after.json", after)
        try:
            row = suite_row(evidence / "junit.xml", body_exit or 0, body_started)
        except Exception as exc:  # noqa: BLE001 - malformed JUnit fails qualification.
            row = {"outcome": "invalid-junit", "error": str(exc)}
            exit_code = 2
        if body_started and row["outcome"] != "passed":
            exit_code = exit_code or 2
        row.update(body_exit_code=body_exit, exit_code=exit_code,
                   body_outcome=row["outcome"])
        if body_started and exit_code and row["outcome"] == "passed":
            row["outcome"] = "custody-failed"
        result = {"status": "NOTRUN" if not body_started else "PASS" if exit_code == 0 else "FAIL",
                  "mode": args.mode, "body_runs": int(body_started), "exit_code": exit_code,
                  "body_exit_code": body_exit, "failure": failure, "suite": row,
                  "preflight_status": "PASS" if all(item["status"] == "PASS" for item in before.values()) else "FAIL"}
        write_bytes(evidence / "stdout.txt", stdout)
        write_bytes(evidence / "stderr.txt", stderr)
        write_json(evidence / "exit.json", {"returncode": exit_code, "body_returncode": body_exit})
        write_json(evidence / "timing.json", {"started_at": started_at, "finished_at": time.time(),
                                              "body_started_at": body_start, "body_finished_at": body_end,
                                              "elapsed_seconds": time.time() - started_at})
        invocation_after = {**invocation, "argv": sys.orig_argv, "cwd": str(Path.cwd()),
                            "safe_environment": {key: os.environ.get(key) for key in SAFE_ENV_KEYS},
                            "script_sha256": file_digest(SCRIPT)}
        if invocation_after["script_sha256"] != invocation["script_sha256"]:
            exit_code = 2
            result.update(exit_code=exit_code, status="FAIL", failure={"error": "harness bytes changed"})
            row.update(exit_code=exit_code, outcome="custody-failed")
            write_json(evidence / "exit.json", {"returncode": exit_code, "body_returncode": body_exit})
        write_json(evidence / "invocation-after.json", invocation_after)
        write_json(evidence / "suite-row.json", row)
        write_json(evidence / "result.json", result)
        leases.close()
    print(json.dumps(result, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
