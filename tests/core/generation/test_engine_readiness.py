"""Target-local ordinary HC03 readiness producer against a real tiny retained session.

What is real here: the installation-scoped supervisor (engine_custody), its
registry, a live loopback CPU HTTP "engine" child, the canonical installed
receipt parser, VibeComfy's own live composite/source verifiers imported from a
real clone of the pinned VibeComfy revision, the strict model-root binding with
full byte hashing, the private profile publication, and the existing consumers
(GenericHost readiness reader/normalizer, CheckoutServerAdapter.from_host_session).

What is SYNTHETIC and labelled as such: the CPU engine stands in for ComfyUI
(loopback HTTP serving /system_stats and /object_info only); two tiny SYNTHETIC
TEST MODELS replace the canonical 5-model lock; the installed receipt is written
by the fixture with the canonical schema; the canonical status operation is
replaced by a strict re-parse of that receipt (it would otherwise rerun
git/uv/env probes against a real install); the retained release manifest is
written by the fixture over a copy of this checkout; and the NVIDIA probe runs a
fixture executable printing SYNTHETIC driver/VRAM values. Nothing here is GPU,
inference, H3 or publication qualification.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import time

import pytest

from astrid.core.generation import engine_custody as custody
from astrid.core.generation import engine_readiness as readiness
from astrid.core.generation.engine_contracts import (
    DownloadResult, HealthEvidence, InstallReceipt, ModelLock, ModelSpec, StartSpec, StepReceipt,
)
from astrid.core.generation.engine_recipe import load_recipe, recipe_digest
from astrid.core.generation.engine_release import canonical, interpreter_identity
from astrid.core.generation.engine_runner import _json, _plain, _sha, helper_manifest

PLATFORM = "linux-x86_64-cu130"
ENGINE = "vibecomfy-h3"
CLONE = Path(__file__).resolve().parents[3]
RUNTIME_SOURCE = Path(os.environ.get("ASTRID_RUNTIME_CHECKOUT", "/home/user/proof/src/astrid-runtime"))
VIBECOMFY_SOURCE = Path(os.environ.get("ASTRID_VIBECOMFY_CHECKOUT", "/home/user/proof/src/VibeComfy"))
COMFY_VERSION = "0.36.0"
SYNTHETIC_DRIVER = "580.95.05"
SYNTHETIC_VRAM_MIB = 32607
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux target-local producer")

# SYNTHETIC CPU stand-in for ComfyUI: same contract as the custody fixture.
_SERVER = r'''
import json,os,sys
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
port=int(sys.argv[1]); mode=Path(sys.argv[2]); started=Path(sys.argv[3])
with started.open('a') as f: f.write(str(os.getpid())+'\n')
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  setting=mode.read_text()
  value={'system':{'comfyui_version':'0.36.0'}} if self.path=='/system_stats' else {'MiniMaxH3ReferenceToVideo':{},'VHS_VideoCombine':{}}
  if setting=='missing' and self.path=='/object_info': value={}
  data=json.dumps(value).encode()
  self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
HTTPServer(('127.0.0.1',port),Handler).serve_forever()
'''

# Shared by the supervisor program and the producer harness so both bind the
# identical tiny recipe (the custody fixture's technique). Only start/health
# are replaced; sources, pins (including real VibeComfy ac0ee1b), environments
# and the models-lock reference stay canonical.
_TINY_RECIPE = r'''
import json
from dataclasses import replace
from astrid.core.generation import engine_recipe
from astrid.core.generation.engine_contracts import ModelLock, ModelSpec, StartSpec
_cfg=json.loads(TINY)
_base=engine_recipe.load_recipe('vibecomfy-h3','linux-x86_64-cu130')
recipe=replace(_base,start=StartSpec(tuple(_cfg['argv']),_cfg['cwd'],{'VIBECOMFY_HEADLESS':'1'},'127.0.0.1',_cfg['port']),
               health=replace(_base.health,timeout_seconds=4,poll_interval_seconds=0.02))
lock=ModelLock('astrid.models-lock.v1',tuple(ModelSpec(**m) for m in _cfg['models']))
'''

_HARNESS = r'''
import json,sys,time
sys.dont_write_bytecode=True  # as run.py does; -I ignores PYTHONDONTWRITEBYTECODE
cfg=json.loads(sys.argv[1])
sys.path.insert(0,cfg['site'])
bearer_opens=[]
def audit(event,args):
    if event=='open' and args and 'engine-bearer.token' in str(args[0]):
        bearer_opens.append(str(args[0]))
sys.addaudithook(audit)
TINY=json.dumps(cfg['tiny'])
''' + _TINY_RECIPE + r'''
from pathlib import Path
engine_recipe.load_recipe=lambda engine,platform: recipe
engine_recipe.load_model_lock=lambda ref: lock
from astrid.core.generation import engine_custody, engine_readiness
from astrid.core.generation.engine_runner import _parse_receipt, plan_install
from astrid.packs.runpod.installed_session import LocalInstalledVibeComfySessionTransport
operations=[]
original=engine_custody.LinuxEngineServiceClient._request
def recorded(self,operation,payload,deadline,*,start=True):
    operations.append([operation,start])
    return original(self,operation,payload,deadline,start=start)
engine_custody.LinuxEngineServiceClient._request=recorded
def forbidden(*args,**kwargs):
    operations.append(['legacy-control',False]); raise RuntimeError('legacy control')
for name in ('start','stop','confirm_stopped'):
    setattr(LocalInstalledVibeComfySessionTransport,name,forbidden)
engine_custody.LinuxEngineServiceClient.stop_owned=forbidden
def status(*,root,deadline):
    # SYNTHETIC canonical-status stand-in: strict canonical parser of the receipt.
    receipt=_parse_receipt(json.loads(Path(root,'.astrid-engine/install.json').read_bytes()),
                           plan_install(recipe,root=root,placement='local'))
    if cfg.get('status')=='object_info':
        receipt=replace(receipt,health=replace(receipt.health,object_info_sha256='0'*64))
    elif cfg.get('status')=='failed':
        receipt=replace(receipt,status='failed')
    return receipt
hardware=lambda deadline: engine_readiness.probe_hardware(deadline,executable=cfg['smi'])
try:
    result=engine_readiness.run_readiness(root=cfg['root'],release_root=cfg['release'],profile_path=cfg['profile'],
                                          deadline=time.monotonic()+60,status=status,hardware=hardware)
    out={'ok':True,'path':result.path,'sha256':result.sha256,'size':result.size}
except engine_readiness.ReadinessError as exc:
    out={'ok':False,'reason':str(exc)}
module=sys.modules.get('vibecomfy')
out.update(vibecomfy_origin=getattr(module,'__file__',None),bearer_opens=bearer_opens,operations=operations,
           readiness_origin=engine_readiness.__file__)
print(json.dumps(out))
'''


def _alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] not in {"Z", "X"}
    except FileNotFoundError:
        return False


def _run(argv, cwd):
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True, timeout=60)


def _synthetic_release(destination):
    """SYNTHETIC retained release: fixture-written manifest over this checkout's files."""
    site = destination / "site"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(CLONE / "astrid", site / "astrid", ignore=ignore)
    shutil.copytree(RUNTIME_SOURCE / "runtime_protocol", site / "runtime_protocol", ignore=ignore)
    shutil.copyfile(CLONE / "astrid/core/generation/_engine_release_entry.py", destination / "run.py")
    # Minimal installed metadata so ``astrid.version`` resolves under -S, as in a wheel install.
    import tomllib
    version = tomllib.loads((CLONE / "pyproject.toml").read_text())["project"]["version"]
    (site / f"astrid-{version}.dist-info").mkdir()
    (site / f"astrid-{version}.dist-info/METADATA").write_text(
        f"Metadata-Version: 2.1\nName: astrid\nVersion: {version}\n")
    files = {}
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            files[path.relative_to(destination).as_posix()] = {"sha256": hashlib.sha256(data).hexdigest(),
                                                               "bytes": len(data)}
    manifest = {"format": 1, "complete": True, "fixture": False, "python": interpreter_identity(),
                "missing": [], "sources": {}, "wheels": [], "files": files}
    (destination / "release.json").write_bytes(canonical(manifest))
    return destination


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


@pytest.fixture(scope="module")
def installation(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("readiness")
    mp.setenv("TMPDIR", str(tmp))  # Existing ProcessCustody native scratch belongs to this test.
    root = tmp / "install"
    comfy = root / "runtime/ComfyUI"
    (comfy / "output").mkdir(parents=True)
    (comfy / "comfyui_version.py").write_text(f'__version__ = "{COMFY_VERSION}"\n')
    canonical_recipe = load_recipe(ENGINE, PLATFORM)
    pin = next(s.pin.revision for s in canonical_recipe.sources if s.name == "vibecomfy")
    # Real VibeComfy at the canonical pin (objects shared read-only with the source tree).
    _run(["git", "clone", "-q", "--shared", "--no-checkout", str(VIBECOMFY_SOURCE), str(root / "runtime/vibecomfy")], tmp)
    _run(["git", "-c", "advice.detachedHead=false", "checkout", "-q", pin], root / "runtime/vibecomfy")
    venv_python = root / "runtime/venv/bin/python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(sys.executable)  # SYNTHETIC primary-environment interpreter path.
    server, mode, started = root / "tiny_http.py", root / "mode", root / "started"
    server.write_text(_SERVER)
    mode.write_text("healthy")
    models = []
    for name, destination, data in (
            ("synthetic-test-diffusion", "diffusion_models/synthetic-test-a.safetensors", b"SYNTHETIC TEST MODEL A\n"),
            ("synthetic-test-lora", "loras/synthetic-test-b.safetensors", b"SYNTHETIC TEST MODEL B (lora)\n")):
        path = root / "models" / destination
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        models.append({"name": name, "repository": None, "revision": None, "path": None, "url": None,
                       "destination": destination, "bytes": len(data),
                       "sha256": hashlib.sha256(data).hexdigest()})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    tiny = {"argv": [str(venv_python), str(server), str(port), str(mode), str(started)],
            "cwd": str(comfy), "port": port, "models": models}
    namespace = {"TINY": json.dumps(tiny)}
    exec(_TINY_RECIPE, namespace)
    recipe, lock = namespace["recipe"], namespace["lock"]
    from astrid.core.generation import engine_recipe
    mp.setattr(engine_recipe, "load_recipe", lambda engine, platform: recipe)
    mp.setattr(engine_recipe, "load_model_lock", lambda ref: lock)
    program = ("import sys,json\nTINY=sys.argv[3]\n" + _TINY_RECIPE
               + "from astrid.core.generation.engine_custody import _serve\n"
               + "_serve(json.loads(sys.argv[1]),recipe,int(sys.argv[2]),sys.argv[5])\n")
    mp.setattr(custody, "_supervisor_argv", lambda identity, lock_fd, token:
               [sys.executable, "-c", program, _json(identity), str(lock_fd), json.dumps(tiny),
                "--launch-token", token])
    identity = dict(root=str(root), engine=ENGINE, platform=PLATFORM, recipe_sha256=recipe_digest(recipe),
                    helper_sha256=_sha(helper_manifest(recipe)))
    client = custody.LinuxEngineServiceClient(**identity)
    launch = {"argv": tiny["argv"], "cwd": str(comfy), "env": {"VIBECOMFY_HEADLESS": "1"}}
    payload = {**identity, "schema": custody._SCHEMA, "launch": launch, "owner_id": None,
               "timeout_seconds": recipe.health.timeout_seconds,
               "poll_interval_seconds": recipe.health.poll_interval_seconds}
    facts = client.call("start", payload, deadline=time.monotonic() + 30)
    try:
        from astrid.core.generation.engine_runner import plan_install
        plan = plan_install(recipe, root=str(root), placement="local")
        stamp = "2026-10-28T00:30:00+00:00"
        # SYNTHETIC installed receipt in the canonical schema (no real install ran).
        receipt = InstallReceipt(
            "astrid.engine-install.v1", "synthetic-readiness-receipt", ENGINE, PLATFORM, "local", str(root),
            "installed", False, plan.recipe_sha256, plan.models_lock_sha256,
            {e.name: "a" * 64 for e in recipe.environments}, {s.name: s.pin.revision for s in recipe.sources},
            tuple(DownloadResult(m.name, str(root / "models" / m.destination), "skipped", m.bytes, m.sha256, True, 0)
                  for m in lock.models),
            tuple(StepReceipt(s.id, s.kind, "executed", "passed",
                              {"owner_id": facts["owner_id"], "launch_sha256": facts["launch_sha256"]}
                              if s.kind == "start" else {}) for s in plan.steps),
            HealthEvidence("healthy", stamp, facts["object_info_sha256"], True), stamp, stamp, None,
        )
        (root / ".astrid-engine/install.json").write_text(_json(_plain(receipt)))
        release = _synthetic_release(tmp / "release")
        smi = tmp / "smi"
        smi.mkdir()
        scripts = {
            "ok": _script(smi / "ok", f"echo '{SYNTHETIC_DRIVER}, {SYNTHETIC_VRAM_MIB}'\n"),
            "missing_driver": _script(smi / "missing", "echo 'NVIDIA-SMI has failed' >&2\nexit 9\n"),
            "two_gpus": _script(smi / "two", f"echo '{SYNTHETIC_DRIVER}, 1'\necho '{SYNTHETIC_DRIVER}, 2'\n"),
            "garbage": _script(smi / "garbage", "echo 'N/A, [Not Supported]'\n"),
        }
        yield {"root": root, "tmp": tmp, "release": release, "recipe": recipe, "lock": lock, "tiny": tiny,
               "client": client, "payload": payload, "facts": facts, "started": started, "mode": mode,
               "receipt": receipt, "smi": scripts, "registry": root / "runtime/out/sessions" / ENGINE,
               "venv_python": venv_python}
    finally:
        try:
            client.stop_owned(deadline=time.monotonic() + 15)
        finally:
            mp.undo()
    assert all(not _alive(int(pid)) for pid in started.read_text().splitlines())


@pytest.fixture
def state(tmp_path):
    """The original agent's private state: 0700 dir with its bearer sibling."""
    directory = tmp_path / "agent-state"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    bearer = directory / "engine-bearer.token"
    bearer.write_text("original-agent-bearer-" + "z" * 40)
    bearer.chmod(0o600)
    return directory


def _snapshot(installation):
    registry = installation["registry"]
    return {name: (registry / name).read_bytes() for name in (
        "pid", "comfy_pid", "comfy_process_start_identity", "launch.json", "config.json", "url",
        "source_revision", "source_content_digest")}


def _harness(installation, state, *, smi="ok", root=None, status=None):
    cfg = {"site": str(installation["release"] / "site"), "tiny": installation["tiny"],
           "root": str(root or installation["root"]), "release": str(installation["release"]),
           "profile": str(state / "engine-readiness.json"), "smi": installation["smi"][smi], "status": status}
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1",
           "TMPDIR": str(installation["tmp"]), "HOME": str(installation["tmp"])}
    result = subprocess.run([sys.executable, "-I", "-c", _HARNESS, json.dumps(cfg)], env=env,
                            capture_output=True, text=True, timeout=100)
    assert result.returncode == 0, result.stderr[-4000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def _assert_engine_retained(installation, before):
    assert _snapshot(installation) == before
    pids = [int(p) for p in installation["started"].read_text().splitlines()]
    assert len(pids) == 1 and _alive(pids[0])  # one engine; never relaunched
    marker = json.loads(before["launch.json"])
    assert _alive(marker["pid"]) and marker["comfy_pid"] == pids[0]
    health = installation["client"].call("health", {**installation["payload"], "owner_id": installation["facts"]["owner_id"]},
                                         deadline=time.monotonic() + 10)
    assert health["owner_id"] == installation["facts"]["owner_id"] == marker["launch_token"]


def test_installed_engine_produces_private_profile_consumed_by_existing_validators(installation, state, monkeypatch):
    from astrid.core.execution.generic_host import _registration_verified_facts
    from astrid.core.generation.backends.vibecomfy import CheckoutServerAdapter
    from astrid.core.generation.model_root import canonical_model_inventory_digest, model_root_binding_from_profile

    root, release, recipe = installation["root"], installation["release"], installation["recipe"]
    before = _snapshot(installation)
    bearer = (state / "engine-bearer.token").read_bytes()
    out = _harness(installation, state)
    assert out["ok"] is True, out.get("reason")
    # Real producer ran from the retained release; VibeComfy came only from the selected checkout.
    assert Path(out["readiness_origin"]) == release / "site/astrid/core/generation/engine_readiness.py"
    assert Path(out["vibecomfy_origin"]) == root / "runtime/vibecomfy/vibecomfy/__init__.py"
    # Only an existing-supervisor health read: no start, stop, run, legacy control or new owner.
    assert out["operations"] == [["health", False]]
    assert out["bearer_opens"] == [] and (state / "engine-bearer.token").read_bytes() == bearer

    path = state / "engine-readiness.json"
    raw = path.read_bytes()
    info = path.stat()
    assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1 and info.st_uid == os.getuid()
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert sorted(p.name for p in state.iterdir()) == ["engine-bearer.token", "engine-readiness.json"]
    assert out["path"] == str(path) and out["size"] == len(raw)
    assert out["sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    profile = json.loads(raw)
    assert raw == json.dumps(profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    # Each fact recomputed here from its independent source, not copied from the producer.
    marker = json.loads(before["launch.json"])
    receipt = installation["receipt"]
    inventory = sorted(({"name": Path(m.destination).name, "sha256": "sha256:" + m.sha256, "size": m.bytes,
                         "subdir": str(Path(m.destination).parent)} for m in installation["lock"].models),
                       key=lambda e: (e["subdir"], e["name"]))
    custom = {s.name: s.pin.revision for s in recipe.sources if "/custom_nodes/" in s.relative_path}
    node_lock = recipe.environments[0].node_lock
    expected_exact = {
        "interpreter": str(installation["venv_python"]),
        "runtime_lock": "sha256:" + hashlib.sha256((release / "release.json").read_bytes()).hexdigest(),
        "engine_lock": readiness._digest({"engine": ENGINE, "platform": PLATFORM, "recipe_sha256": receipt.recipe_sha256,
                                          "requirements_locks": dict(receipt.requirements_locks),
                                          "source_commits": dict(sorted(receipt.source_commits.items()))}),
        "model_digest": canonical_model_inventory_digest(inventory),
        "custom_node_digest": readiness._digest({
            "custom_node_sources": dict(sorted(custom.items())),
            "node_lock": {"source": node_lock.source, "path": node_lock.path, "sha256": node_lock.sha256},
            "object_info_sha256": installation["facts"]["object_info_sha256"]}),
        "driver": SYNTHETIC_DRIVER, "root": str(root), "port": recipe.start.port,
    }
    assert profile["verified_facts"]["exact"] == expected_exact
    minimum = profile["verified_facts"]["minimum"]
    assert minimum["vram_bytes"] == SYNTHETIC_VRAM_MIB * 1024 * 1024
    assert 0 < minimum["scratch_bytes"] <= shutil.disk_usage(root).total
    assert profile["status"] == "ready" and profile["gpu_qualified"] is False
    assert "workflow_endpoint" not in profile and "endpoint_evidence" not in profile
    assert "vibecomfy_candidate" not in profile
    assert profile["runtime"] == {"runtime_instance_id": str(__import__("uuid").UUID(marker["launch_token"])),
                                  "comfyui_version": "==" + COMFY_VERSION}
    assert profile["vibecomfy_session"] == {
        "session_dir": str(installation["registry"]), "server_url": marker["url"],
        "pid": marker["pid"], "process_birth_id": marker["process_start_identity"],
        "comfy_pid": marker["comfy_pid"], "comfy_process_birth_id": marker["comfy_process_start_identity"],
        "launch_token": marker["launch_token"],
        "config_digest": "sha256:" + hashlib.sha256(before["config.json"]).hexdigest(),
        "source_revision": before["source_revision"].decode().strip(),
        "source_content_digest": before["source_content_digest"].decode().strip(),
    }
    assert profile["launch"]["output_root"] == json.loads(before["config.json"])["output_directory"]

    # Existing GenericHost reader (hash fence) + normalizer accept the exact bytes.
    monkeypatch.setenv("ASTRID_HOST_READINESS_PROFILE_PATH", str(path))
    monkeypatch.setenv("ASTRID_HOST_READINESS_PROFILE_HASH", out["sha256"])
    assert _registration_verified_facts() == profile["verified_facts"]
    monkeypatch.setenv("ASTRID_HOST_READINESS_PROFILE_HASH", "sha256:" + "0" * 64)
    with pytest.raises(Exception, match="hash does not match"):
        _registration_verified_facts()
    # Existing strict model-root binding re-hashes the declared files.
    binding = model_root_binding_from_profile(profile, verify_files=True)
    assert binding.inventory_digest == expected_exact["model_digest"] and binding.path == root / "models"
    # Existing checkout adapter: digest, registry, live ownership, source and version gates.
    adapter = CheckoutServerAdapter.from_host_session(
        hc03_profile=profile, model_id="vibecomfy.run", template_id="vibecomfy.run",
        invocation_identity="readiness-producer-test")
    assert adapter._host_session["pid"] == marker["pid"] and adapter._host_session["comfy_pid"] == marker["comfy_pid"]
    assert adapter._host_session["model_root"] == str(root / "models")
    _assert_engine_retained(installation, before)


def _restore_text(path, value):
    path.write_text(value)


_REFUSALS = {
    "missing_driver": "NVIDIA driver probe failed",
    "two_gpus": "exactly one visible NVIDIA GPU",
    "garbage_driver": "NVIDIA driver probe output is invalid",
    "release_tamper": "retained release verification failed",
    "release_incomplete": "incomplete or for another interpreter",
    "release_python": "incomplete or for another interpreter",
    "receipt_recipe": "installed session evidence could not be verified",
    "model_missing": "model root does not match the canonical lock",
    "model_extra": "model root does not match the canonical lock",
    "model_bytes": "model root does not match the canonical lock",
    "source_drift": "installed session evidence could not be verified",
    "health": "installed engine evidence could not be verified",
    "status_object_info": "canonical installed status differs",
    "status_failed": "canonical installed status differs",
    "wrong_root": "installed session evidence could not be verified",
}


@pytest.mark.parametrize("change", sorted(_REFUSALS))
def test_refusals_fail_closed_without_profile_or_engine_change(installation, state, change):
    root, release = installation["root"], installation["release"]
    before = _snapshot(installation)
    restore = []
    smi, status, chosen_root = "ok", None, None
    if change in {"missing_driver", "two_gpus"}:
        smi = change
    elif change == "garbage_driver":
        smi = "garbage"
    elif change == "release_tamper":
        target = release / "site/astrid/packs/runpod/pack.yaml"
        original = target.read_bytes()
        target.write_bytes(original + b" ")
        restore.append(lambda: target.write_bytes(original))
    elif change in {"release_incomplete", "release_python"}:
        target = release / "release.json"
        original = target.read_bytes()
        manifest = json.loads(original)
        if change == "release_incomplete":
            manifest.update(complete=False, fixture=True, missing=["resolved-linux-lock:comfy"])
        else:
            manifest["python"] = {**manifest["python"], "version": [3, 12, 10]}
        target.write_bytes(canonical(manifest))
        restore.append(lambda: target.write_bytes(original))
    elif change == "receipt_recipe":
        target = root / ".astrid-engine/install.json"
        original = target.read_bytes()
        target.write_text(json.dumps({**json.loads(original), "recipe_sha256": "0" * 64}))
        restore.append(lambda: target.write_bytes(original))
    elif change == "model_missing":
        target = root / "models/loras/synthetic-test-b.safetensors"
        target.rename(root / "moved-model")
        restore.append(lambda: (root / "moved-model").rename(target))
    elif change == "model_extra":
        target = root / "models/loras/unlisted.safetensors"
        target.write_bytes(b"not in the lock")
        restore.append(target.unlink)
    elif change == "model_bytes":
        target = root / "models/diffusion_models/synthetic-test-a.safetensors"
        original = target.read_bytes()
        target.write_bytes(original.replace(b"A", b"Z"))
        restore.append(lambda: target.write_bytes(original))
    elif change == "source_drift":
        target = root / "runtime/vibecomfy/README.md"
        original = target.read_bytes()
        target.write_bytes(original + b"\nsame-commit drift\n")
        restore.append(lambda: target.write_bytes(original))
    elif change == "health":
        installation["mode"].write_text("missing")
        restore.append(lambda: installation["mode"].write_text("healthy"))
    elif change.startswith("status_"):
        status = change.removeprefix("status_")
    else:
        chosen_root = installation["tmp"] / "not-the-install"
    try:
        out = _harness(installation, state, smi=smi, root=chosen_root, status=status)
    finally:
        for action in restore:
            action()
    assert out["ok"] is False and _REFUSALS[change] in out["reason"], out.get("reason")
    if change in {"missing_driver", "two_gpus", "garbage_driver", "model_missing", "model_extra", "model_bytes",
                  "status_object_info", "status_failed"}:
        # Refused after the real live session/health checks passed: nothing invented, nothing written.
        assert out["vibecomfy_origin"] == str(root / "runtime/vibecomfy/vibecomfy/__init__.py")
    assert not (state / "engine-readiness.json").exists()
    assert sorted(p.name for p in state.iterdir()) == ["engine-bearer.token"]
    assert out["bearer_opens"] == []
    assert all(op in (["health", False],) for op in out["operations"])
    _assert_engine_retained(installation, before)


@pytest.mark.parametrize("case", ["open_mode", "absent", "symlink_parent", "wrong_name", "bearer_name", "relative"])
def test_unsafe_private_profile_path_refuses_before_engine_observation(tmp_path, state, case, monkeypatch):
    profile = state / "engine-readiness.json"
    if case == "open_mode":
        state.chmod(0o755)
    elif case == "absent":
        profile = tmp_path / "missing-state/engine-readiness.json"
    elif case == "symlink_parent":
        alias = tmp_path / "alias"
        alias.symlink_to(state, target_is_directory=True)
        profile = alias / "engine-readiness.json"
    elif case == "wrong_name":
        profile = state / "profile.json"
    elif case == "bearer_name":
        profile = state / "engine-bearer.token"
    else:
        profile = Path("agent-state/engine-readiness.json")
    def forbidden(**kwargs):
        pytest.fail("engine observation before private path validation")
    monkeypatch.setattr(readiness, "produce_readiness", forbidden)
    with pytest.raises(readiness.ReadinessError):
        readiness.run_readiness(root="/nonexistent/install", release_root="/nonexistent/release",
                                profile_path=str(profile), deadline=time.monotonic() + 5)
    assert sorted(p.name for p in state.iterdir()) == ["engine-bearer.token"]


def test_profile_publication_is_exclusive_private_and_never_rewritten(state):
    path = str(state / "engine-readiness.json")
    previous = os.umask(0)
    try:
        digest = readiness.write_profile(path, b'{"status":"ready"}', deadline=time.monotonic() + 5)
    finally:
        os.umask(previous)
    info = os.stat(path)
    assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
    assert digest == "sha256:" + hashlib.sha256(b'{"status":"ready"}').hexdigest()
    # Identical bytes: readback only. Different bytes (e.g. a 52-finalized profile): retained.
    assert readiness.write_profile(path, b'{"status":"ready"}', deadline=time.monotonic() + 5) == digest
    with pytest.raises(readiness.ReadinessError, match="retained, not rewritten"):
        readiness.write_profile(path, b'{"status":"other"}', deadline=time.monotonic() + 5)
    assert Path(path).read_bytes() == b'{"status":"ready"}'
    os.chmod(path, 0o644)
    with pytest.raises(readiness.ReadinessError, match="private 0600"):
        readiness.write_profile(path, b'{"status":"ready"}', deadline=time.monotonic() + 5)
    assert sorted(p.name for p in state.iterdir()) == ["engine-bearer.token", "engine-readiness.json"]


def test_hardware_probe_is_bounded_and_never_invents_values(installation, tmp_path):
    scripts = installation["smi"]
    assert readiness.probe_hardware(time.monotonic() + 5, executable=scripts["ok"]) == {
        "driver": SYNTHETIC_DRIVER, "vram_bytes": SYNTHETIC_VRAM_MIB * 1024 * 1024}
    slow = _script(tmp_path / "slow", "exec sleep 5\n")
    for executable in (scripts["missing_driver"], scripts["two_gpus"], scripts["garbage"],
                       str(tmp_path / "absent-nvidia-smi"), slow):
        started = time.monotonic()
        with pytest.raises(readiness.ReadinessError):
            readiness.probe_hardware(time.monotonic() + 1, executable=executable)
        assert time.monotonic() - started < 4


def test_default_status_is_the_canonical_status_operation(monkeypatch):
    from astrid.core.generation import engine_execution, engine_runner
    calls = []
    context = object()
    monkeypatch.setattr(engine_execution, "create_local_backend",
                        lambda **kwargs: calls.append(("backend", kwargs)) or (context, object()))
    monkeypatch.setattr(engine_runner, "engine_status",
                        lambda engine, **kwargs: calls.append(("status", engine, kwargs)) or "receipt")
    assert readiness.canonical_status(root="/workspace/astrid/engines/vibecomfy-h3", deadline=12.0) == "receipt"
    assert calls == [
        ("backend", {"engine": ENGINE, "platform": PLATFORM, "placement": "local",
                     "root": "/workspace/astrid/engines/vibecomfy-h3", "deadline": 12.0}),
        ("status", ENGINE, {"platform": PLATFORM, "placement": "local",
                            "root": "/workspace/astrid/engines/vibecomfy-h3", "context": context, "deadline": 12.0}),
    ]


def _launcher(release, *args):
    return subprocess.run([sys.executable, "-I", "-S", str(release / "run.py"), *args],
                          env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
                          capture_output=True, text=True, timeout=100)


def test_retained_release_launcher_dispatches_readiness_and_fails_closed(installation, state):
    """Real ``run.py readiness`` under -I -S with the canonical (unpatched) recipe.

    The fixture install is a tiny CPU session, so the canonical recipe's launch
    and model lock cannot match it: the operation must refuse without a profile.
    """
    release, root = installation["release"], installation["root"]
    before = _snapshot(installation)
    profile = str(state / "engine-readiness.json")
    base = ["readiness", "--root", str(root), "--profile", profile, "--seconds", "60"]
    mismatch = _launcher(release, *base, "--release", str(installation["tmp"] / "other-release"))
    assert mismatch.returncode == 1 and "readiness release argument differs" in mismatch.stderr
    canonical_recipe = _launcher(release, *base, "--release", str(release))
    assert canonical_recipe.returncode == 1 and "readiness_refused" in canonical_recipe.stderr
    assert "installed session evidence could not be verified" in canonical_recipe.stderr
    absent = _launcher(release, "readiness", "--root", str(installation["tmp"] / "absent"), "--profile", profile,
                       "--release", str(release))
    assert absent.returncode == 1 and "readiness_refused" in absent.stderr
    unbounded = _launcher(release, *base[:-1], "7201", "--release", str(release))
    assert unbounded.returncode == 1 and "7200 seconds" in unbounded.stderr
    assert not Path(profile).exists() and canonical_recipe.stdout == ""
    # The launcher's incomplete-fixture gate still precedes the operation.
    manifest_path = release / "release.json"
    original = manifest_path.read_bytes()
    manifest = json.loads(original)
    manifest.update(complete=False, fixture=True, missing=["resolved-linux-lock:comfy"])
    manifest_path.write_bytes(canonical(manifest))
    try:
        fixture = _launcher(release, *base, "--release", str(release))
    finally:
        manifest_path.write_bytes(original)
    assert fixture.returncode == 1 and "retained release execution refused" in fixture.stderr
    assert sorted(p.name for p in state.iterdir()) == ["engine-bearer.token"]
    _assert_engine_retained(installation, before)


def test_main_requires_the_verified_launcher(state):
    with pytest.raises(SystemExit) as exit_info:
        readiness.main(["--root", "/r", "--release", "/x", "--profile", str(state / "engine-readiness.json")])
    assert exit_info.value.code == 2
