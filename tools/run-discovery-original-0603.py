#!/usr/bin/env python3
"""Run the unchanged discovery row through the normal managed test service.

The worker snapshot owns this runner; the separate pinned carrier owns the test
and source bytes. Both entry and installed phases resolve the normal managed
child registration. A script digest is an accidental-call check, not authority.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import marshal
import os
import re
import subprocess
import sys
import tarfile
import time
import types
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ORCH = Path("/tmp/claude-0/-home-user-repo/7dc61be1-7066-52e0-a5d0-94f90e61430a/scratchpad/orch")
SOURCE = Path("/home/user/work/wt/agt-pin-0ff3d103")
RUNTIME = Path("/home/user/work/wt/rt-pin-becfe6db")
CHILD_NAME = "venv-agent-child-final-0ff3-becfe"
RECEIPT = ORCH / "tests/provision" / (CHILD_NAME + ".txt")
MAKER = ORCH / "bin/make-child-venv"
MAKER_SHA256 = "c887a1bef4831f2ca5a43372ee28430000b2f20b9d290bffb237f27ffc404ff2"
SCRATCH = Path(os.environ.get("TMPDIR", "/tmp"))
EVIDENCE = Path(os.environ.get("DISCOVERY_EVIDENCE", str(SCRATCH / "discovery-evidence")))
TARGET = "tests/packs/rendering/test_threejs_backend.py::test_threejs_support_rejects_unsupported_timelines_with_clip_reasons"
TARGET_FILE = SOURCE / TARGET.split("::")[0]
EXPECTED_AGENT = "0ff3d103fb892977b0853829f42b748a5d567851"
EXPECTED_RUNTIME = "becfe6db88b2cc034dba3fafa6c050117cdf2b06"
EXPECTED_PROFILE = "agent-remotion-v3-819acf-1110"
PROFILE = Path("/home/user/work/runtimes") / EXPECTED_PROFILE
NODE = Path("/home/user/work/toolchain/node-v20.19.4-linux-x64/bin/node")
SCHEMA_SITE = Path("/home/user/work/venv-agent/lib/python3.13/site-packages")
EXPECTED_NODE_VERSION = "v20.19.4"
EXPECTED_COMPOSITION_ARCHIVE_SHA256 = (
    "819acf6b3815d6eeabe772a9569581fedbe4f9c9cce35946efca9454e2040234"
)
EXPECTED_PACKAGE_LOCK_SHA256 = "f4fc0a32a8822e956dfa21ff35d19c0c83d147c754cdb3546f91c84e330be18d"
EXPECTED_DISCOVERY_SHA256 = "40ae7c5945529bb65dbeb3853a80b1b03650e7dcc5f4a60ddaa04c4dc9f37999"
EXPECTED_DISCOVERY_TEST_SHA256 = "9faabadd340c1b2df04f0ab6e83c9d00ac1e6f9cf68fd2f8ed2b81af7e108e5f"
EXPECTED_TARGET_FILE_SHA256 = "9efe493cb2f8339177e07616b976afe6f9813ae4d415923ff25701b471e991c7"
EXPECTED_TARGET_FUNCTION_SHA256 = "ca54f2d5059bb3b5cca1e8ee43156df6762d3a86739caa2aa18ffcb83f3ded82"
EXPECTED_TARGET_ASSERTIONS_SHA256 = (
    "087c80d0211e714a7c073d12adb2e260d5de299ba3d251ac31b853562eb6de1e"
)
EXPECTED_CLIENT_IDENTITY_SHA256 = "d79568df4fe4bed2a217b519d73824e8fd1ca17f61dfe0ce8492f1903764eb44"
# Values are restricted to this declared environment; never dump ambient secrets.
SAFE_ENV_KEYS = (
    "PATH",
    "PYTHONPATH",
    "ASTRID_HOST_EXECUTION_PROFILE",
    "ASTRID_REMOTION_PROJECT_DIR",
    "ASTRID_NODE_EXECUTABLE",
    "ASTRID_TEST_NODE_MODULES",
)
DECLARED_ENV_KEYS = (
    *SAFE_ENV_KEYS,
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "PYTHONDONTWRITEBYTECODE",
    "ASTRID_RUNTIME_CHECKOUT",
    "ASTRID_RUNTIME_SOURCE_ROOT",
    "ASTRID_TIMELINE_SCHEMA_PYTHONPATH",
)
PRODUCT_MODULES = (
    "astrid",
    "astrid.sdk.rendering",
    "astrid.core.pack.discovery",
    "astrid.core.rendering.registry",
    "astrid.core.rendering.transport",
    "astrid.core.rendering.contracts",
    "astrid.core.rendering.remotion_runtime",
    "astrid.core.rendering.service",
    "astrid.core.timeline.banodoco_schema",
    "astrid.core.subprocess_env",
    "astrid.packs.rendering.shared.rendering_backend",
    "astrid.packs.rendering.rendering.renderers.threejs.run",
    "astrid.packs.rendering.rendering.renderers.remotion.run",
    "astrid.packs.rendering.actions.render.run",
    "astrid_runtime_client",
    "astrid_runtime",
    "runtime_protocol",
)


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def git(*args: str, cwd: Path = SOURCE) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def write_json(name: str, value: Any) -> None:
    data = json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"
    try:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / name).write_text(data, encoding="utf-8")
    except OSError:
        # The normal service captures stderr durably even if artifact setup fails.
        print(name + "=" + data, file=sys.stderr)
        raise


def scrub(value: str) -> str:
    return re.sub(
        r"(?i)(api[_-]?key|authorization|credential|password|secret|token)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        value,
    ).replace("\x00", "\ufffd")[:65536]


def error_record(exc: BaseException) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": scrub(str(exc))}


def declared(env: dict[str, str]) -> dict[str, str | None]:
    return {key: env.get(key) for key in DECLARED_ENV_KEYS}


def body_facts(path: Path) -> dict[str, Any]:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_threejs_support_rejects_unsupported_timelines_with_clip_reasons"
    )
    assertions: list[ast.stmt] = [
        node for node in ast.walk(function) if isinstance(node, ast.Assert)
    ]
    reasons_calls = sorted(
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "reasons_for"
    )
    # The first call is inside the four-value clip-type loop.  The transition
    # assignment is therefore invocation six, while the body has nine asserts.
    return {
        "file_sha256": sha(path),
        "function_ast_sha256": sha_bytes(
            ast.dump(function, annotate_fields=True, include_attributes=False).encode()
        ),
        "assertions_ast_sha256": sha_bytes(
            ast.dump(
                ast.Module(body=assertions, type_ignores=[]),
                annotate_fields=True,
                include_attributes=False,
            ).encode()
        ),
        "assertion_count": len(assertions),
        "reasons_for_call_lines": reasons_calls,
        "support_invocation_count": 10
        if reasons_calls == [208, 217, 229, 241, 295, 309, 324]
        else -1,
        "transition_invocation": 6 if 229 in reasons_calls else -1,
        "transition_source_line": 229,
        "line_start": function.lineno,
        "line_end": function.end_lineno,
    }


def source_facts() -> dict[str, Any]:
    facts: dict[str, Any] = {"runner_sha256": sha(Path(__file__)), "runner_root": str(ROOT)}
    for name, root, commit in (
        ("agent", SOURCE, EXPECTED_AGENT),
        ("runtime", RUNTIME, EXPECTED_RUNTIME),
    ):
        if root != root.resolve(strict=True):
            raise RuntimeError(f"{name} source carrier is linked")
        head = git("rev-parse", "HEAD", cwd=root)
        status = git("status", "--porcelain", "--untracked-files=all", cwd=root)
        facts[name] = {"root": str(root), "head": head, "status": status}
        if head != commit or status:
            raise RuntimeError(f"{name} source carrier mismatch or dirty tree: {facts[name]}")
    target = body_facts(TARGET_FILE)
    expected = {
        "file_sha256": EXPECTED_TARGET_FILE_SHA256,
        "function_ast_sha256": EXPECTED_TARGET_FUNCTION_SHA256,
        "assertions_ast_sha256": EXPECTED_TARGET_ASSERTIONS_SHA256,
        "assertion_count": 9,
        "support_invocation_count": 10,
        "transition_invocation": 6,
    }
    if any(target[key] != value for key, value in expected.items()):
        raise RuntimeError("original body hash/assertions/support/transition changed")
    for relative, wanted in (
        ("astrid/core/pack/discovery.py", EXPECTED_DISCOVERY_SHA256),
        ("tests/packs/test_pack_discovery_single_scan.py", EXPECTED_DISCOVERY_TEST_SHA256),
    ):
        if sha(SOURCE / relative) != wanted:
            raise RuntimeError(f"pinned source hash changed: {relative}")
    facts["body"] = target
    return facts


def load_store() -> Any:
    spec = importlib.util.spec_from_file_location("bench_store", ORCH / "bin/bench_store.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("normal bench store helper unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.Store("/home/user/work/bench-store", ORCH / "tests")


def client_identity() -> str:
    files = git(
        "ls-tree",
        "-r",
        "--name-only",
        EXPECTED_RUNTIME,
        "packages/python/astrid_runtime_client/",
        cwd=RUNTIME,
    ).splitlines()
    rows = [
        path
        + "="
        + sha_bytes(
            subprocess.check_output(["git", "show", EXPECTED_RUNTIME + ":" + path], cwd=RUNTIME)
        )
        for path in files
    ]
    identity = "\n".join(rows)
    if not rows or sha_bytes(identity.encode()) != EXPECTED_CLIENT_IDENTITY_SHA256:
        raise RuntimeError("immutable Runtime client tuple mismatch")
    return identity


def resolve_child() -> tuple[Path, dict[str, Any]]:
    entries = load_store().registration_entries(CHILD_NAME)
    children = [entry for entry in entries if entry["component"] == "child"]
    if len(children) != 1:
        raise RuntimeError("normal registration must bind exactly one child")
    entry = children[0]
    child = Path(entry["path"])
    identity = {
        "agent_sha": EXPECTED_AGENT,
        "backend_sha": EXPECTED_RUNTIME,
        "client_tuple": dict(line.split("=", 1) for line in client_identity().splitlines()),
    }
    if entry["identity"] != identity or child != child.resolve(strict=True):
        raise RuntimeError("managed child tuple/root mismatch")
    report = (child / ".provision-report").read_bytes()
    if RECEIPT.read_bytes() != report:
        raise RuntimeError("normal transactional provision receipt differs from child")
    lines = report.decode().splitlines()
    for key, value in (
        ("status", "passed"),
        ("agent_sha", EXPECTED_AGENT),
        ("runtime_sha", EXPECTED_RUNTIME),
        ("runtime_client_identity_sha256", EXPECTED_CLIENT_IDENTITY_SHA256),
    ):
        if [line for line in lines if line.startswith(key + "=")] != [key + "=" + value]:
            raise RuntimeError(f"duplicate/missing/wrong provision receipt identity: {key}")
    return child, {"entry": entry, "receipt": str(RECEIPT), "receipt_sha256": sha_bytes(report)}


def installed_custody(child: Path) -> dict[str, Any]:
    # Reuse the normal maker's full verifier, including all Agent/Runtime RECORD
    # rows, immutable snapshots, complete module sets and unowned-file rejection.
    # No caller-selected path, synthetic RECORD, installation or provisioning.
    if sha(MAKER) != MAKER_SHA256:
        raise RuntimeError("normal maker verifier changed; review runner binding")
    source = MAKER.read_text()
    start, end = "# STORE_VERIFY_BEGIN\n", "# STORE_VERIFY_END\n"
    if source.count(start) != 1 or source.count(end) != 1:
        raise RuntimeError("normal maker verifier boundaries are ambiguous")
    verifier = source.split(start, 1)[1].split(end, 1)[0]
    subprocess.run(
        [
            "/usr/bin/python3",
            "-I",
            "-",
            str(child),
            client_identity(),
            str(SOURCE),
            EXPECTED_AGENT,
            str(RUNTIME),
            EXPECTED_RUNTIME,
            "/home/user/work",
            "/usr/bin/python3.13",
        ],
        input=verifier,
        text=True,
        check=True,
        timeout=180,
    )
    for marker, expected in (
        (".astrid-source-commit", EXPECTED_AGENT),
        (".astrid-runtime-source-commit", EXPECTED_RUNTIME),
        (".astrid-runtime-client-identity", client_identity()),
    ):
        if (child / marker).read_text().rstrip("\n") != expected:
            raise RuntimeError(f"installed marker mismatch: {marker}")
    site = child / "lib/python3.13/site-packages"
    metadata = {
        str(path.relative_to(child)): sha(path)
        for pattern in ("*.dist-info/RECORD", "*.dist-info/direct_url.json")
        for path in site.glob(pattern)
    }
    caches = {}
    for package in ("astrid", "astrid_runtime_client", "astrid_runtime", "runtime_protocol"):
        for cache in (site / package).rglob("*.pyc"):
            # PYTHONDONTWRITEBYTECODE prevents writes, not reads. The maker
            # permits unhashed pip bytecode; verify its code against the pinned
            # source as well, without changing Python's normal startup policy.
            source_path = Path(importlib.util.source_from_cache(str(cache)))
            data = cache.read_bytes()
            if (
                not source_path.is_file()
                or len(data) < 16
                or data[:4] != importlib.util.MAGIC_NUMBER
                or int.from_bytes(data[4:8], "little") & ~3
            ):
                raise RuntimeError(f"invalid installed product cache: {cache}")
            code = marshal.loads(data[16:])
            if not isinstance(code, types.CodeType):
                raise RuntimeError(f"installed cache is not module bytecode: {cache}")
            optimization = re.search(r"\.opt-([12])\.pyc$", cache.name)
            compiled = compile(
                source_path.read_bytes(),
                code.co_filename,
                "exec",
                dont_inherit=True,
                optimize=int(optimization[1]) if optimization else 0,
            )
            if code != compiled:
                raise RuntimeError(f"installed cached code differs from pinned source: {cache}")
            caches[str(cache.relative_to(child))] = sha_bytes(data)
    return {"maker_sha256": sha(MAKER), "metadata": metadata, "bytecode": caches}


def child_environment(child: Path) -> dict[str, str]:
    shim = SCRATCH / "discovery-import-shim"
    shim.mkdir(parents=True, exist_ok=True)
    for name in ("tests", "scripts"):
        link, target = shim / name, SOURCE / name
        if link.exists() or link.is_symlink():
            if link.resolve() != target.resolve():
                raise RuntimeError(f"private test import shim mismatch: {link}")
        else:
            link.symlink_to(target, target_is_directory=True)
    return {
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(SCRATCH),
        "DISCOVERY_EVIDENCE": str(EVIDENCE),
        "PYTHONPATH": str(shim),
        "PATH": f"{child / 'bin'}:{NODE.parent}:/usr/bin:/bin",
        "ASTRID_HOST_EXECUTION_PROFILE": "installed",
        "ASTRID_RUNTIME_CHECKOUT": str(RUNTIME),
        "ASTRID_RUNTIME_SOURCE_ROOT": str(RUNTIME),
        "ASTRID_REMOTION_PROJECT_DIR": str(PROFILE),
        "ASTRID_NODE_EXECUTABLE": str(NODE),
        "ASTRID_TEST_NODE_MODULES": str(PROFILE / "node_modules"),
        "ASTRID_TIMELINE_SCHEMA_PYTHONPATH": str(SCHEMA_SITE),
    }


def import_origins(child: Path) -> dict[str, str]:
    site = child / "lib/python3.13/site-packages"
    origins = {}
    for name in PRODUCT_MODULES:
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None:
            raise RuntimeError(f"missing production import: {name}")
        path = Path(spec.origin).resolve(strict=True)
        if not path.is_relative_to(site):
            raise RuntimeError(f"production import escaped installed child: {name}={path}")
        origins[name] = str(path)
    shared = importlib.util.find_spec("banodoco_timeline_schema")
    expected_shared = SCHEMA_SITE / "banodoco_timeline_schema/__init__.py"
    if shared is None or shared.origin is None or Path(shared.origin).resolve() != expected_shared:
        raise RuntimeError("shared schema import differs from the fixed profile schema root")
    origins["banodoco_timeline_schema"] = str(expected_shared)
    return origins


def profile_facts() -> dict[str, Any]:
    archive = (
        PROFILE
        / "artifacts/banodoco-timeline-composition-635f4de06fda22a85fc2e4cfc764d0153a4c9f40.tgz"
    )
    lock = PROFILE / "package-lock.json"
    if (
        sha(archive) != EXPECTED_COMPOSITION_ARCHIVE_SHA256
        or sha(lock) != EXPECTED_PACKAGE_LOCK_SHA256
    ):
        raise RuntimeError("fixed Composition archive/profile lock mismatch")
    package_manifest = json.loads((PROFILE / "package.json").read_text())
    locked_root = json.loads(lock.read_text())["packages"][""]
    for key in ("dependencies", "devDependencies"):
        if package_manifest.get(key, {}) != locked_root.get(key, {}):
            raise RuntimeError(f"profile package manifest differs from fixed lock: {key}")
    if package_manifest["dependencies"]["@banodoco/timeline-composition"] != "file:" + str(
        archive.relative_to(PROFILE)
    ):
        raise RuntimeError("profile does not select the pinned Composition archive")
    composition = PROFILE / "node_modules/@banodoco/timeline-composition"
    installed = {}
    with tarfile.open(archive) as package:
        for member in package:
            if member.isdir():
                continue
            relative = Path(member.name)
            if not member.isfile() or relative.parts[0] != "package" or ".." in relative.parts:
                raise RuntimeError("unexpected Composition archive member")
            relative = Path(*relative.parts[1:])
            content = package.extractfile(member)
            if content is None or (composition / relative).read_bytes() != content.read():
                raise RuntimeError(f"installed Composition differs from pinned archive: {relative}")
            installed[str(relative)] = sha(composition / relative)
    if {str(p.relative_to(composition)) for p in composition.rglob("*") if p.is_file()} != set(
        installed
    ):
        raise RuntimeError("installed Composition has extra/missing files")
    if (
        subprocess.check_output([str(NODE), "--version"], text=True).strip()
        != EXPECTED_NODE_VERSION
    ):
        raise RuntimeError("fixed Node20 version mismatch")
    # Resolve peers from the actual installed Composition, not from its manifest
    # alone or from an arbitrary caller override.
    script = """const fs=require('fs'), path=require('path'), {createRequire}=require('module');
const root=process.argv[1], req=createRequire(root+'/package.json');
const own=req('@banodoco/timeline-composition/package.json');
const from=createRequire(req.resolve('@banodoco/timeline-composition/package.json'));
const lock=JSON.parse(fs.readFileSync(root+'/package-lock.json'));
function identity(resolve,name,want) {
 const entry=resolve.resolve(name.startsWith('@types/') ? name+'/package.json' : name);
 let dir=path.dirname(entry), file;
 while(dir!==path.dirname(dir)) { const p=path.join(dir,'package.json');
  if(fs.existsSync(p)&&JSON.parse(fs.readFileSync(p)).name===name){file=p;break;}
  dir=path.dirname(dir);
 }
 if(!file)throw Error('missing package '+name);
 const pkg=JSON.parse(fs.readFileSync(file));
 if(pkg.version!==want||!fs.realpathSync(file).startsWith(fs.realpathSync(root)+'/node_modules/'))
   throw Error('package identity/root mismatch '+name);
 return {version:pkg.version,entry:fs.realpathSync(entry),package:fs.realpathSync(file)};
}
const rows={root:{},peers:{}};
for(const name of Object.keys(lock.packages[''].dependencies))
 rows.root[name]=identity(req,name,lock.packages['node_modules/'+name].version);
for(const [name,want] of Object.entries(own.peerDependencies)) {
 rows.peers[name]=identity(from,name,want);
 if(rows.peers[name].entry!==identity(req,name,want).entry)
  throw Error('Composition resolves another peer '+name);
}
console.log(JSON.stringify(rows));"""
    peers = json.loads(subprocess.check_output([str(NODE), "-e", script, str(PROFILE)], text=True))
    return {
        "path": str(PROFILE),
        "node": str(NODE),
        "node_version": EXPECTED_NODE_VERSION,
        "lock_sha256": sha(lock),
        "archive_sha256": sha(archive),
        "composition_files": installed,
        "peers": peers,
        "schema_files": {
            str(path.relative_to(SCHEMA_SITE)): sha(path)
            for path in (SCHEMA_SITE / "banodoco_timeline_schema").rglob("*")
            if path.is_file()
        },
    }


def custody(child: Path, installed: bool) -> dict[str, Any]:
    resolved, provision = resolve_child()
    if child != resolved:
        raise RuntimeError("managed child changed during execution")
    facts: dict[str, Any] = {
        "source": source_facts(),
        "provision": provision,
        "installed": installed_custody(child),
    }
    if installed:
        facts["origins"] = import_origins(child)
        facts["profile"] = profile_facts()
    return facts


def run_installed(mode: str, evidence: dict[str, Any]) -> int:
    from astrid.core.rendering import transport
    from astrid.core.subprocess_env import build_child_subprocess_env
    from tests.packs.conftest import renderer_execution_environment

    fixture = renderer_execution_environment.__wrapped__()
    next(fixture)
    try:
        caller = dict(os.environ)
        projected = build_child_subprocess_env(base=caller, parent=caller)
        actual = transport._build_environment(None)
        if actual != projected or "ASTRID_HOST_EXECUTION_PROFILE" in actual:
            raise RuntimeError("production transport environment projection changed")
        evidence["production_environment"] = {
            "caller": declared(caller),
            "projected": declared(projected),
            "actual": declared(actual),
        }
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass
    if mode == "preflight":
        return 0
    command = [
        str(Path(sys.executable)),
        "-P",
        "-m",
        "pytest",
        "--import-mode=importlib",
        "-q",
        "-oaddopts=",
        "-pno:cacheprovider",
        "--timeout=45",
        "--basetemp=" + str(SCRATCH / "pytest"),
        "--junitxml=" + str(EVIDENCE / "junit.xml"),
        TARGET,
    ]
    evidence["body_argv"] = command
    write_json("installed-before.json", evidence)
    with subprocess.Popen(
        command,
        cwd=SOURCE,
        env=dict(os.environ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        evidence.update(
            body_status="UNKNOWN", pytest_started_monotonic=time.monotonic(), pytest_pid=process.pid
        )
        write_json("installed-started.json", evidence)
        stdout, stderr = process.communicate()
        evidence.update(
            body_exit=process.returncode,
            stdout=scrub(stdout),
            stderr=scrub(stderr),
            pytest_finished_monotonic=time.monotonic(),
        )
    print(scrub(stdout), end="")
    print(scrub(stderr), end="", file=sys.stderr)
    return process.returncode


def main() -> int:
    started = time.monotonic()
    # This label only keeps early child failures from overwriting parent evidence;
    # resolve_child() below is always required before any installed execution.
    phase = "parent" if Path(sys.prefix) == Path("/usr") else "installed"
    code = 2
    child = None
    before = None
    record: dict[str, Any] = {
        "argv": sys.argv,
        "cwd": os.getcwd(),
        "environment": declared(dict(os.environ)),
        "six_safe_environment": {key: os.environ.get(key) for key in SAFE_ENV_KEYS},
        "source": None,
        "provision": None,
        "origins": {"status": "NOTCHECKED"},
        "custody_before": None,
        "body_status": "NOTRUN",
        "junit": {"status": "NOTRUN", "path": None},
        "result_paths": [],
        "started_monotonic": started,
        "expected": {
            "agent": EXPECTED_AGENT,
            "agent_carrier": str(SOURCE),
            "runtime": EXPECTED_RUNTIME,
            "runtime_carrier": str(RUNTIME),
            "candidate_name": CHILD_NAME,
            "provision_receipt": str(RECEIPT),
            "client_identity_sha256": EXPECTED_CLIENT_IDENTITY_SHA256,
            "target": TARGET,
            "target_file_sha256": EXPECTED_TARGET_FILE_SHA256,
            "function_sha256": EXPECTED_TARGET_FUNCTION_SHA256,
            "assertions_sha256": EXPECTED_TARGET_ASSERTIONS_SHA256,
            "assertions": 9,
            "support_calls": 10,
            "transition_invocation": 6,
            "watchdog_seconds": 45,
            "profile": str(PROFILE),
            "node": str(NODE),
            "node_version": EXPECTED_NODE_VERSION,
            "composition_archive_sha256": EXPECTED_COMPOSITION_ARCHIVE_SHA256,
            "package_lock_sha256": EXPECTED_PACKAGE_LOCK_SHA256,
        },
    }
    try:
        # Write a minimal before record before parsing or accessing any dependency.
        write_json(phase + "-before.json", record)
        parser = argparse.ArgumentParser()
        parser.add_argument("mode", choices=("run", "preflight"))
        parser.add_argument("--reviewed-harness-sha256", required=True)
        # These are row declarations only; custody always uses the constants and
        # normal store, never caller-provided equality as source authority.
        parser.add_argument("--source-carrier", choices=(str(SOURCE),), required=True)
        parser.add_argument("--runtime-carrier", choices=(str(RUNTIME),), required=True)
        parser.add_argument("--candidate-name", choices=(CHILD_NAME,), required=True)
        parser.add_argument("--managed-profile", choices=(str(PROFILE),), required=True)
        args = parser.parse_args()
        if args.reviewed_harness_sha256 != sha(Path(__file__)):
            raise RuntimeError("reviewed script digest mismatch (accidental-call check)")
        record["source"] = source_facts()
        write_json(phase + "-before.json", record)
        child, provision = resolve_child()
        record["provision"] = provision
        installed = Path(sys.prefix).resolve() == child
        phase = "installed" if installed else "parent"
        before = custody(child, installed)
        record["custody_before"] = before
        record["source"] = before["source"]
        record["origins"] = before.get("origins", {"status": "child-not-started"})
        write_json(phase + "-before.json", record)
        if installed:
            wanted_env = child_environment(child)
            if dict(os.environ) != wanted_env:
                raise RuntimeError(
                    "installed execution environment differs from fixed normal route"
                )
            code = run_installed(args.mode, record)
        else:
            environment = child_environment(child)
            command = [
                str(child / "bin/python"),
                "-P",
                str(Path(__file__).resolve()),
                *sys.argv[1:],
            ]
            record["child_argv"] = command
            record["child_cwd"] = str(SOURCE)
            record["child_environment"] = declared(environment)
            write_json("parent-before.json", record)
            completed = subprocess.run(
                command, cwd=SOURCE, env=environment, capture_output=True, text=True, check=False
            )
            code = completed.returncode
            record.update(stdout=scrub(completed.stdout), stderr=scrub(completed.stderr))
            print(scrub(completed.stdout), end="")
            print(scrub(completed.stderr), end="", file=sys.stderr)
            result = EVIDENCE / "installed-result.json"
            if result.is_file():
                installed_result = json.loads(result.read_text())
                for key in ("body_status", "body_exit", "body_argv", "junit", "result_paths"):
                    if key in installed_result:
                        record[key] = installed_result[key]
            else:
                raise RuntimeError("installed child did not produce a result record")
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - durable setup failure evidence
        record["failure"] = error_record(exc)
        print(json.dumps(record["failure"]), file=sys.stderr)
        code = 2
    finally:
        try:
            if child is not None:
                after = custody(child, phase == "installed")
                record["custody_after"] = after
                if before is None or after != before:
                    raise RuntimeError(
                        "post-body source/provision/install/profile/import custody changed"
                    )
            else:
                record["source_after"] = source_facts()
        except Exception as exc:  # noqa: BLE001 - postcheck failure must change exit status
            record["postcheck_failure"] = error_record(exc)
            code = 2
        if phase == "installed":
            try:
                record["result_paths"] = [
                    str(path) for path in SCRATCH.rglob("*support-result.json")
                ]
                junit = EVIDENCE / "junit.xml"
                if junit.is_file():
                    record["junit"] = {"path": str(junit), "sha256": sha(junit)}
                    cases = ET.parse(junit).findall(".//testcase")
                    targets = [case for case in cases if case.get("name") == TARGET.split("::")[1]]
                    ran_target = len(targets) == 1
                    record["body_status"] = "NOTRUN"
                    if ran_target:
                        case = targets[0]
                        errors = case.findall("error")
                        if case.find("skipped") is None and not any(
                            "failed on setup" in error.get("message", "") for error in errors
                        ):
                            record["body_status"] = "UNKNOWN" if errors else "FINISHED"
                    record["junit"]["target_recorded"] = ran_target
                elif record["body_status"] != "NOTRUN":
                    record["junit"] = {"status": "unavailable", "path": str(junit)}
                if code == 0 and record["body_status"] not in ("FINISHED", "NOTRUN"):
                    raise RuntimeError("pytest success without a completed original target record")
                if code == 0 and "body_argv" in record and record["body_status"] != "FINISHED":
                    raise RuntimeError(
                        "original target was not executed; a skip/setup error is not a pass"
                    )
            except Exception as exc:  # noqa: BLE001 - preserve partial artifacts on every failure
                record["artifact_failure"] = error_record(exc)
                code = 2
        record.update(
            exit_code=code, finished_monotonic=time.monotonic(), elapsed=time.monotonic() - started
        )
        print(f"discovery {phase}: exit={code} body={record['body_status']} evidence={EVIDENCE}")
        try:
            write_json(phase + "-after.json", record)
            write_json(phase + "-result.json", record)
        except OSError as exc:
            code = 2
            record.update(exit_code=2, evidence_failure=error_record(exc))
            print("discovery-evidence-failure=" + json.dumps(record, default=str), file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
