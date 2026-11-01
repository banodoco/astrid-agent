"""Installed wheel action identity at the same boundary as the C0 pod command.

Only wheel installation/import routing and synthetic invalid request data are fixtures.
The console, ephemeral workspace, neutral launcher, authenticated health/handshake,
pack host and action remain production implementations. No engine is installed or run.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.helpers.runtime import locate_runtime

ROOT = Path(__file__).resolve().parents[2]
_SITE_ENV = "ASTRID_ACTION_WHEEL_TEST_SITE"
_LOG_ENV = "ASTRID_ACTION_WHEEL_TEST_LOG"


def _run(argv, *, cwd, env, log, timeout=180):
    result = subprocess.run([str(part) for part in argv], cwd=cwd, env=env,
                            capture_output=True, text=True, timeout=timeout)
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    return result


def _git(root, *args, input=None, **overrides):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, **overrides)
    return subprocess.run(["git", "-C", str(root), *args], env=env, input=input,
                          capture_output=True, check=True, timeout=60).stdout


def _snapshot(root, commit, target, *, working=False):
    """Portable shallow Git snapshot; no remotes, credentials, or local .git pointers."""
    target.mkdir(parents=True)
    archive = _git(root, "archive", commit)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter="data")
    metadata = target / ".git"
    (metadata / "objects/pack").mkdir(parents=True)
    (metadata / "refs").mkdir()
    (metadata / "HEAD").write_text(commit + "\n")
    (metadata / "shallow").write_text(commit + "\n")
    (metadata / "config").write_text("[core]\nrepositoryformatversion = 0\nbare = false\nfilemode = true\n")
    objects = _git(root, "rev-list", "--objects", "--no-walk", "--no-object-names", commit)
    _git(root, "pack-objects", str(metadata / "objects/pack/pack"), input=objects)
    _git(root, "read-tree", commit, GIT_INDEX_FILE=str(metadata / "index"))
    _git(target, "update-index", "--refresh")
    if working:
        # Include candidate edits without letting a build backend dirty their provenance.
        diff = _git(root, "diff", "--binary", commit, "--")
        if diff:
            _git(target, "apply", "--index", "--binary", input=diff)
            _git(target, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "candidate production repair")


_OBSERVER = r'''
import contextlib,json,os
from pathlib import Path

_ACTIVATED = False

def activate():
    global _ACTIVATED
    if _ACTIVATED:
        return
    _ACTIVATED = True
    import ipaddress,socket
    connect=socket.socket.connect
    def loopback_only(sock,address):
        if isinstance(address,tuple):
            host=address[0]
            try: allowed=host == "localhost" or ipaddress.ip_address(host).is_loopback
            except ValueError: allowed=False
            if not allowed: raise RuntimeError("offline installed-action regression denied non-loopback network")
        return connect(sock,address)
    socket.socket.connect=loopback_only
    # Observe only values that production already authenticated/validated.
    from astrid.sdk import client
    from astrid.core import action_cli
    def record(kind,value):
        with open(os.environ["ASTRID_ACTION_WHEEL_TEST_LOG"],"a",encoding="utf-8") as stream:
            stream.write(json.dumps({"kind":kind,"value":value})+"\n")
    health, handshake = client._validate_health, client._validate_handshake
    def observe_health(value,**kwargs):
        result=health(value,**kwargs)
        record("health",{"identity":value["identity"],"instance":value["runtime_instance_id"]})
        return result
    def observe_handshake(value,**kwargs):
        result=handshake(value,**kwargs)
        record("handshake",{"realm":value["realm_id"],"actor":value["actor_id"]})
        return result
    client._validate_health,client._validate_handshake=observe_health,observe_handshake
    import astrid,astrid_runtime
    record("origins",{"agent":astrid.__file__,"runtime":astrid_runtime.__file__})
    source_manifest=action_cli._source_manifest
    def observe_manifest(path):
        result=source_manifest(path)
        record("action-profile",json.loads(result.read_text()))
        record("ephemeral-root",str(result.parent))
        return result
    action_cli._source_manifest=observe_manifest
    import importlib
    bootstrap=importlib.import_module("astrid_runtime.bootstrap")
    capture=bootstrap._capture_requested_identity
    def observe_capture(source):
        roots={"source_checkout":source.source_checkout,"runtime_checkout":source.runtime_checkout}
        try:
            identity=capture(source)
        except Exception:
            record("capture-failure",dict(roots,label="unverified-source-identity"))
            raise
        record("capture-success",dict(roots,identity=identity))
        return identity
    bootstrap._capture_requested_identity=observe_capture
'''


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    build = tmp_path_factory.mktemp("installed-action")
    runtime = locate_runtime()
    assert runtime is not None, "this regression requires the existing Runtime checkout"
    lock = json.loads((ROOT / "runtime.lock.json").read_text())
    sources = build / "controller"
    sources.mkdir()
    _snapshot(ROOT, _git(ROOT, "rev-parse", "HEAD").decode().strip(),
              sources / "astrid-agent", working=True)
    _snapshot(runtime, lock["source_commit"], sources / "astrid-runtime")
    for name in ("runpod-python", "runpod-lifecycle", "VibeComfy"):
        target = sources / name
        target.mkdir()
        (target / "fixture.txt").write_text("unused offline transport companion\n")
        _git(target, "init", "-q")
        _git(target, "add", ".")
        _git(target, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "commit", "-qm", "unused offline transport companion")
    spec = importlib.util.spec_from_file_location("installed_action_pod_session", ROOT / "scripts/pod_session.py")
    session = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = session
    spec.loader.exec_module(session)
    bundle = build / "pod-src.tgz"
    session.build_bundle(sources / "astrid-agent", sources, bundle)
    # The portable bundle preserves these identical staging snapshots.
    shutil.rmtree(sources)
    transported = build / "pod/src"
    transported.mkdir(parents=True)
    with tarfile.open(bundle) as tar:
        tar.extractall(transported, filter="data")
    manifest = json.loads((transported / ".pod-source.json").read_text())
    wheels, target = build / "wheels", build / "site-packages"
    wheels.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ASTRID_", "GIT_"))
           and k not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(PYTHONDONTWRITEBYTECODE="1", PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    # Build in separate staging copies: exact transported checkouts stay clean.
    for name in ("astrid-agent", "astrid-runtime"):
        stage = build / (name + "-build")
        shutil.copytree(transported / name, stage, ignore=shutil.ignore_patterns(".git"))
        done = _run([sys.executable, "-m", "pip", "wheel", "--no-cache-dir", "--no-deps",
                     "--no-build-isolation", "--wheel-dir", wheels, stage], cwd=build, env=env,
                    log=build / (name + "-wheel.log"))
        assert done.returncode == 0, "offline wheel build failed; inspect retained wheel log"
        shutil.rmtree(stage)
    done = _run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--no-deps", "--no-compile",
                 "--target", target, *wheels.glob("*.whl")], cwd=build, env=env, log=build / "install.log")
    assert done.returncode == 0, "offline wheel installation failed; inspect retained install.log"
    (target / "action_wheel_observer.py").write_text(_OBSERVER)
    hook = Path(sysconfig.get_paths()["purelib"]) / ("astrid_action_wheel_" + uuid4().hex + ".pth")
    hook.write_text(
        "import os,sys; _p=os.environ.get(\"ASTRID_ACTION_WHEEL_TEST_SITE\"); "
        "sys.path.insert(0,_p) if _p else None; "
        "__import__(\"action_wheel_observer\").activate() if _p and os.environ.get(\"ASTRID_ACTION_WHEEL_TEST_LOG\") else None\n")
    env[_SITE_ENV] = str(target)
    env["PATH"] = str(target / "bin") + os.pathsep + env.get("PATH", "")
    short_temp = Path(tempfile.mkdtemp(prefix="a222-", dir=Path("/tmp").resolve())).resolve()
    try:
        yield SimpleNamespace(build=build, root=transported, site=target, env=env, short_temp=short_temp,
                              manifest=manifest, session=session)
    finally:
        hook.unlink()
        shutil.rmtree(short_temp)


def _case(installed, tmp_path):
    home = tmp_path / "action-home"
    home.mkdir()
    support = tmp_path / "support"
    profile = support / "runtime/source-profiles/astrid.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"profile":"astrid", "runtime_checkout":str(installed.root / "astrid-runtime"),
                                  "source_checkout":str(installed.root / "astrid-agent"), "runtime_environment":sys.prefix}))
    events = tmp_path / "authenticated.jsonl"
    action_temp = installed.short_temp / uuid4().hex[:8]
    action_temp.mkdir()
    with socket.socket() as door:
        door.bind(("127.0.0.1", 0))
        door_port = door.getsockname()[1]
    env = dict(installed.env, HOME=str(home), TMPDIR=str(action_temp), ASTRID_RUNTIME_DATA_ROOT=str(support),
               ASTRID_RUNTIME_DOOR_PORT=str(door_port),
               ASTRID_ACTION_WHEEL_TEST_LOG=str(events))
    return SimpleNamespace(home=home, support=support, profile=profile, events=events, env=env)


def _events(case):
    return [json.loads(line) for line in case.events.read_text().splitlines()] if case.events.exists() else []


@pytest.mark.parametrize("selection", ["persisted", "explicit"])
def test_installed_console_reuses_the_authenticated_transported_identity(installed, tmp_path, selection):
    case = _case(installed, tmp_path)
    if selection == "explicit":
        case.env["ASTRID_RUNTIME_SOURCE_MANIFEST"] = str(case.profile)
    create = _run([installed.site / "bin/astrid-runtime", "workspace", "create", "--profile", "astrid",
                   "--data-root", case.support, "--source-manifest", case.profile,
                   "--realm-root", tmp_path / "pre-realm", "--realm-id", "pre-install", "--hosted-origin", "off", "--json"],
                  cwd=tmp_path, env=case.env, log=tmp_path / "create.log")
    assert create.returncode == 0, "real pre-install workspace creation failed"
    request = {"pod_id":"offline222", "volume_id":"offline-volume", "nonce":"a" * 32,
               "runtime_head":installed.manifest["heads"]["astrid-runtime"],
               "runtime_lock_sha256":installed.manifest["runtime_lock_sha256"]}
    encoded = base64.b64encode(json.dumps(request).encode()).decode()
    probe_code = ("import base64,json,sys; sys.path.insert(0,sys.argv[1]); from pod_runtime_ready import runtime_probe; "
                  "from pathlib import Path; print(json.dumps(runtime_probe(json.loads(base64.b64decode(sys.argv[2])), "
                  "source_root=Path(sys.argv[3]),data_root=Path(sys.argv[4]))))")
    ready = _run([sys.executable, "-c", probe_code, installed.root / "astrid-agent/scripts", encoded,
                  installed.root, case.support],
                 cwd=tmp_path, env=case.env, log=tmp_path / "pre-install-ready.log")
    assert ready.returncode == 0, "real pre-install identity/readiness probe failed"
    receipt = json.loads(ready.stdout)
    assert receipt["ready"] is True and receipt["stopped"] is True
    before = len(_events(case))
    # A deliberately invalid public request reaches the real action and refuses before any inference.
    request_path, assets = tmp_path / "request.json", tmp_path / "asset-map.json"
    request_path.write_text("{}")
    assets.write_text("{}")
    command = installed.session.action_script("offline", str(request_path), str(assets), str(tmp_path / "out"))
    command = command.replace("/opt/astrid/bin", str(installed.site / "bin")).replace(
        installed.session.CLIP_DIR, str(tmp_path / "clip"))
    (tmp_path / "clip").mkdir()
    (tmp_path / "actual-action.sh").write_text(command)
    action = _run(["bash", tmp_path / "actual-action.sh"], cwd=tmp_path, env=case.env,
                  log=tmp_path / "post-install-action.log", timeout=240)
    observed = _events(case)[before:]
    health = [event["value"] for event in observed if event["kind"] == "health"]
    assert health, "actual installed console never authenticated Runtime identity"
    assert all(value["identity"] == receipt["identity"] for value in health)
    assert any(event["kind"] == "handshake" for event in observed)
    profiles = [event["value"] for event in observed if event["kind"] == "action-profile"]
    assert len(profiles) == 1
    assert profiles[0]["source_checkout"] == str(installed.root / "astrid-agent")
    assert profiles[0]["runtime_checkout"] == str(installed.root / "astrid-runtime")
    assert action.returncode == 1
    summary_path = tmp_path / "clip/runs/offline.summary.json"
    summary = summary_path.read_text()
    assert "identity could not be verified" not in summary + action.stderr
    assert "h3_av.transform" in summary and '"ok": false' in summary
    assert '"run_id"' in summary or '"manifest"' in summary, "installed action did not reach real invocation"
    assert "site-packages/astrid" not in profiles[0]["source_checkout"]
    assert not (case.support / "runtime/discovery.json").exists()
    assert all(not Path(event["value"]).exists() for event in observed if event["kind"] == "ephemeral-root")
    assert all(str(installed.site) in event["value"]["agent"] and str(installed.site) in event["value"]["runtime"]
               for event in observed if event["kind"] == "origins")


@pytest.mark.parametrize("selection", ["absent", "invalid", "missing-explicit", "symlink", "missing-checkout"])
def test_installed_console_fails_closed_on_missing_or_invalid_source_profile(installed, tmp_path, selection):
    case = _case(installed, tmp_path)
    if selection == "absent":
        case.profile.unlink()
    elif selection == "invalid":
        case.profile.write_text('{"profile":"astrid","identity":{"managed_by":"source"}}')
        case.env["ASTRID_RUNTIME_SOURCE_MANIFEST"] = str(case.profile)
    elif selection == "missing-explicit":
        case.env["ASTRID_RUNTIME_SOURCE_MANIFEST"] = str(tmp_path / "absent-explicit.json")
    elif selection == "symlink":
        real = tmp_path / "real-profile.json"
        case.profile.replace(real)
        case.profile.symlink_to(real)
    else:
        value = json.loads(case.profile.read_text())
        value["runtime_checkout"] = str(tmp_path / "unverified-runtime")
        case.profile.write_text(json.dumps(value))
    req, assets = tmp_path / "request.json", tmp_path / "asset-map.json"
    req.write_text("{}")
    assets.write_text("{}")
    done = _run([installed.site / "bin/astrid", "action", "run", "h3_av.transform", "--request", req,
                 "--asset-map", assets, "--placement", "local", "--out", tmp_path / "out"],
                cwd=tmp_path, env=case.env, log=tmp_path / "refused-action.log")
    assert done.returncode == 1
    assert not any(event["kind"] == "handshake" for event in _events(case))
    assert not (case.support / "runtime/discovery.json").exists()


def test_named_runtime_head_cannot_replace_the_exact_lock_tuple(installed, tmp_path):
    """The named current Runtime is useful evidence, but cannot substitute for the C0 lock."""
    runtime = locate_runtime()
    named = "cad2c8031adea28d164d8b214302658101a4ce15"
    lock = json.loads((ROOT / "runtime.lock.json").read_text())
    assert named != lock["source_commit"]
    sources = tmp_path / "mismatched-tuple"
    sources.mkdir()
    _snapshot(runtime, named, sources / "astrid-runtime")
    for name in ("runpod-python", "runpod-lifecycle", "VibeComfy"):
        (sources / name).symlink_to(installed.root / name, target_is_directory=True)
    with pytest.raises(installed.session.SessionError, match="Runtime HEAD differs from Agent runtime.lock.json"):
        installed.session.build_bundle(installed.root / "astrid-agent", sources, tmp_path / "refused.tgz")
    assert not (tmp_path / "refused.tgz").exists()


def test_actual_installed_console_captures_the_exact_transported_source_identity(installed, tmp_path):
    """Root-cause regression independent of later platform custody availability.

    This is deliberately narrower than the two authenticated pre/post tests above:
    it verifies the real neutral launcher's identity capture, then leaves ordinary
    Runtime admission and cleanup running with every production check intact.
    """
    case = _case(installed, tmp_path)
    expected_code = ("import json,sys; sys.path.insert(0,sys.argv[1]); from pathlib import Path; "
                     "from pod_source_identity import verify; print(json.dumps(verify(Path(sys.argv[2]),Path(sys.argv[3]))))")
    expected = _run([sys.executable, "-c", expected_code, installed.root / "astrid-agent/scripts",
                     installed.root, case.profile], cwd=tmp_path, env=case.env,
                    log=tmp_path / "verified-transported-identity.log")
    assert expected.returncode == 0, "exact transported source/Runtime-lock verification failed"
    identity = json.loads(expected.stdout)
    assert identity["managed_by"] == "source" and identity["dirty"] is False
    assert identity["agent_head"] == installed.manifest["heads"]["astrid-agent"]
    assert identity["runtime_head"] == installed.manifest["heads"]["astrid-runtime"]
    req, assets = tmp_path / "request.json", tmp_path / "asset-map.json"
    req.write_text("{}")
    assets.write_text("{}")
    done = _run([installed.site / "bin/astrid", "action", "run", "h3_av.transform", "--request", req,
                 "--asset-map", assets, "--placement", "local", "--out", tmp_path / "out"],
                cwd=tmp_path, env=case.env, log=tmp_path / "actual-installed-console.log", timeout=240)
    observed = _events(case)
    captures = [event["value"] for event in observed if event["kind"] == "capture-success"]
    assert captures, "actual installed console failed neutral Runtime source identity capture"
    assert not any(event["kind"] == "capture-failure" for event in observed)
    assert all(value["identity"] == identity for value in captures)
    assert all(value["source_checkout"] == str(installed.root / "astrid-agent")
               and value["runtime_checkout"] == str(installed.root / "astrid-runtime") for value in captures)
    assert all(str(installed.site) in event["value"]["agent"] and str(installed.site) in event["value"]["runtime"]
               for event in observed if event["kind"] == "origins")
    assert done.returncode == 1, "synthetic invalid request unexpectedly completed"
    assert all(not Path(event["value"]).exists() for event in observed if event["kind"] == "ephemeral-root")
