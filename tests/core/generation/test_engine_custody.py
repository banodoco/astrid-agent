"""Real CPU subprocess acceptance for the installation-scoped v2 owner."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from astrid.core.generation import engine_custody as custody
from astrid.core.generation.engine_contracts import (
    CommandRequest, CustodyUnresolved, DeadlineExceeded, EndpointOwnershipError,
    IntegrityError, RecipeValidationError, StartSpec, UnsafePathError,
)
from astrid.core.generation.engine_recipe import load_recipe, recipe_digest
from astrid.core.generation.engine_runner import _json, _sha, helper_manifest

PLATFORM = "linux-x86_64-cu130"
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native owner")

_SERVER = r'''
import json,os,sys,time
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
port=int(sys.argv[1]); mode=Path(sys.argv[2]); started=Path(sys.argv[3])
with started.open('a') as f: f.write(str(os.getpid())+'\n')
if mode.read_text()=='exit': raise SystemExit(3)
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  setting=mode.read_text()
  if setting=='slow': time.sleep(30)
  if setting=='redirect':
   self.send_response(302);self.send_header('Location','http://127.0.0.1:1/');self.end_headers();return
  value={'system':{'comfyui_version':'0.36.0'}} if self.path=='/system_stats' else {'MiniMaxH3ReferenceToVideo':{},'VHS_VideoCombine':{}}
  if setting=='missing' and self.path=='/object_info': value={}
  data=b'{"bad":NaN}' if setting=='nan' else json.dumps(value).encode()
  self.send_response(200)
  self.send_header('Content-Length',str(33554433 if setting=='oversize' else len(data)))
  self.end_headers();self.wfile.write(data)
HTTPServer(('127.0.0.1',port),Handler).serve_forever()
'''


def _alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] not in {"Z", "X"}
    except FileNotFoundError:
        return False


def _wait(predicate, seconds=8):
    end = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < end, "tiny process fixture did not settle"
        time.sleep(0.02)


def _identity(root, recipe=None):
    recipe = recipe or load_recipe("vibecomfy-h3", PLATFORM)
    return dict(root=str(root), engine=recipe.engine, platform=recipe.platform,
                recipe_sha256=recipe_digest(recipe), helper_sha256=_sha(helper_manifest(recipe)))


def _client(identity):
    return custody.LinuxEngineServiceClient(**identity)


def _request(root, code, seconds=8, *, limit=262144, env=None):
    return CommandRequest((sys.executable, "-c", code), str(root), env or {},
                          time.monotonic() + seconds, limit)


def _payload(identity, recipe, launch, *, owner=None):
    return {**identity, "schema": custody._SCHEMA, "launch": launch, "owner_id": owner,
            "timeout_seconds": recipe.health.timeout_seconds,
            "poll_interval_seconds": recipe.health.poll_interval_seconds}


@pytest.fixture(autouse=True)
def native_scratch(tmp_path, monkeypatch):
    # Existing ProcessCustody's ephemeral native scratch belongs to this test.
    monkeypatch.setenv("TMPDIR", str(tmp_path))


@pytest.fixture
def session(tmp_path, monkeypatch):
    root = tmp_path / "install"
    comfy = root / "runtime/ComfyUI"
    comfy.mkdir(parents=True)
    (comfy / "output").mkdir()
    source = root / "runtime/vibecomfy"
    source.mkdir()
    (source / "tiny.py").write_text("# immutable fixture source\n")
    for argv in (["git", "init", "-q"], ["git", "add", "tiny.py"],
                 ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-qm", "fixture"]):
        subprocess.run(argv, cwd=source, check=True, capture_output=True, timeout=5)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True, timeout=5).strip()
    server = root / "tiny_http.py"
    server.write_text(_SERVER)
    mode, started = root / "mode", root / "started"
    mode.write_text("healthy")
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    base = load_recipe("vibecomfy-h3", PLATFORM)
    recipe = replace(base, sources=tuple(replace(s, pin=replace(s.pin, revision=revision))
                     if s.name == "vibecomfy" else s for s in base.sources),
                     start=StartSpec((sys.executable, str(server), str(port), str(mode), str(started)),
                                     str(comfy), {"VIBECOMFY_HEADLESS": "1"}, "127.0.0.1", port),
                     health=replace(base.health, timeout_seconds=4, poll_interval_seconds=0.02))
    identity = _identity(root, recipe)
    launch = {"argv": list(recipe.start.argv), "cwd": recipe.start.cwd, "env": dict(recipe.start.env)}
    # Exercise the real socket/lock/launch path. Only the internal recipe
    # dependency is replaced, to run an actual tiny HTTP child instead of GPU code.
    program = f'''
import json,sys
from dataclasses import replace
from astrid.core.generation.engine_contracts import StartSpec
from astrid.core.generation.engine_recipe import load_recipe
from astrid.core.generation.engine_custody import _serve
base=load_recipe('vibecomfy-h3',{PLATFORM!r})
recipe=replace(base,sources=tuple(replace(s,pin=replace(s.pin,revision={revision!r})) if s.name=='vibecomfy' else s for s in base.sources),start=StartSpec({recipe.start.argv!r},{str(comfy)!r},{{'VIBECOMFY_HEADLESS':'1'}},'127.0.0.1',{port}),health=replace(base.health,timeout_seconds=4,poll_interval_seconds=0.02))
_serve(json.loads(sys.argv[1]),recipe,int(sys.argv[2]),sys.argv[4])
'''
    monkeypatch.setattr(custody, "_supervisor_argv", lambda identity, lock_fd, token:
                        [sys.executable, "-c", program, _json(identity), str(lock_fd), "--launch-token", token])
    client = _client(identity)
    client._fixture_program = program
    payload = _payload(identity, recipe, launch)
    yield client, payload, recipe, root, mode, started
    client.stop_owned(deadline=time.monotonic() + 15)
    if started.exists():
        assert all(not _alive(int(pid)) for pid in started.read_text().splitlines())


def _call(client, payload, operation="start", seconds=8):
    return client.call(operation, payload, deadline=time.monotonic() + seconds)


def _finite_client(identity, payload, operation, *, bootstrap=None):
    program = '''
import json,sys,time
from astrid.core.generation.engine_custody import LinuxEngineServiceClient
from astrid.core.generation import engine_custody as custody
identity=json.loads(sys.argv[1]);payload=json.loads(sys.argv[2])
if len(sys.argv)>4:
 program=sys.argv[4]
 custody._supervisor_argv=lambda identity,lock_fd,token:[sys.executable,'-c',program,json.dumps(identity),str(lock_fd),'--launch-token',token]
print(json.dumps(LinuxEngineServiceClient(**identity).call(sys.argv[3],payload,deadline=time.monotonic()+10)))
'''
    return subprocess.run([sys.executable, "-c", program, _json(identity), _json(payload), operation,
                           *([bootstrap] if bootstrap is not None else [])],
                          capture_output=True, text=True, timeout=20, check=True)


def test_constructor_has_no_effects(tmp_path):
    root = tmp_path / "not-created"
    _client(_identity(root))
    assert not root.exists()


def test_real_default_supervisor_runs_finite_command_and_stops(tmp_path):
    # No injected recipe or server bootstrap here: production startup and
    # installed helper/recipe digest checks execute in a real supervisor.
    root = tmp_path / "install"
    root.mkdir()
    client = _client(_identity(root))
    try:
        result = client.run(_request(root, "print('real owner')"))
        assert result.exit_code == 0 and result.stdout == "real owner\n"
        assert not result.timed_out
        subprocess.run([sys.executable, "-m", "astrid.core.generation.engine_custody", "stop", str(root),
                        "--timeout", "15"], check=True, capture_output=True, text=True, timeout=25)
    finally:
        client.stop_owned(deadline=time.monotonic() + 15)
    assert not Path(client.control, "session.json").exists()
    assert not Path(client.control, "control.sock").exists()


def test_real_local_factory_context_client_default_supervisor(tmp_path, monkeypatch):
    # Exact production factory, canonical recipe and default isolated supervisor
    # launch. No injected client, recipe loader or supervisor bootstrap here.
    from astrid.core.generation import engine_runner as runner
    from astrid.core.generation.engine_execution import create_local_backend, LocalLinuxExecutionContext

    root = tmp_path / "factory-install"
    ctx, adapter = create_local_backend(engine="vibecomfy-h3", platform=PLATFORM,
                                        placement="local", root=str(root),
                                        deadline=time.monotonic() + 10)
    assert isinstance(ctx, LocalLinuxExecutionContext) and ctx.adapter is adapter
    client = ctx.service_client
    assert type(client) is custody.LinuxEngineServiceClient
    assert not root.exists() and client._spawned is None
    recipe = load_recipe(ctx.engine, ctx.platform)
    manifest = helper_manifest(recipe)
    required = {"core/generation/engine_execution.py", "core/generation/engine_platforms.py",
                "core/generation/_engine_target_files.py", "core/generation/engine_custody.py",
                "core/execution/__init__.py", "core/execution/process_group.py",
                "core/execution/custody_broker.py", "core/execution/platform_helpers.py"}
    assert set(runner._A2_PATHS) == required
    assert len(runner._A2_PATHS) == len(required)
    assert required <= manifest.keys()
    assert client.identity == _identity(root, recipe)
    for relative in required:
        assert manifest[relative] == hashlib.sha256((runner.recipes._ROOT / relative).read_bytes()).hexdigest()
    for field in ("recipe_sha256", "helper_sha256"):
        changed = {**client.identity, field: "f" * 64}
        rejected = subprocess.run(custody._supervisor_argv(changed, -1, "identity-check"),
                                  capture_output=True, text=True, timeout=10)
        assert rejected.returncode != 0
        assert "installed supervisor code/recipe differs from caller" in rejected.stderr
        assert not root.exists()

    ctx.ensure_dir(str(root), deadline=time.monotonic() + 5)
    ctx.write_json_atomic(str(root / "context.json"), {"real": True}, deadline=time.monotonic() + 5)
    assert ctx.read_json(str(root / "context.json"), deadline=time.monotonic() + 5) == {"real": True}
    monkeypatch.setenv("A2_FACTORY_UNSET", "inherited")
    witness = root / "timed-tree.json"
    timed_pids = []
    try:
        result = ctx.run(_request(root, "import os,json;print(json.dumps([os.getpid(),os.getppid(),os.getcwd(),os.environ.get('A2_FACTORY_UNSET'),os.environ['A2_FACTORY_VALUE']]));raise SystemExit(7)",
                                  env={"A2_FACTORY_UNSET": None, "A2_FACTORY_VALUE": "public"}))
        # A successful request also proves main() independently recomputed the
        # actual installed recipe/helper digests in the isolated supervisor.
        assert result.exit_code == 7 and not result.timed_out
        pid, owner_pid, cwd, unset, value = json.loads(result.stdout)
        assert owner_pid == client._spawned.pid and cwd == str(root)
        assert unset is None and value == "public" and not _alive(pid)
        assert result.stderr == "" and not result.stdout_truncated and not result.stderr_truncated
        assert result.job_id and result.duration_seconds >= 0
        marker = json.loads(Path(client.control, "session.json").read_text())
        assert marker["identity"] == client.identity

        result = ctx.run(_request(root, "import os;os.write(1,b'A'*200000);os.write(2,b'B'*200000)", limit=1024))
        assert result.exit_code == 0 and not result.timed_out
        assert result.stdout == "A" * 1024 and result.stderr == "B" * 1024
        assert result.stdout_truncated and result.stderr_truncated
        with pytest.raises(DeadlineExceeded):
            ctx.run(_request(root, "raise SystemExit(99)", seconds=-1))

        code = f'''import json,os,subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
Path({str(witness)!r}).write_text(json.dumps([os.getpid(),child.pid]))
time.sleep(60)
'''
        result = ctx.run(_request(root, code, seconds=2))
        assert result.timed_out is True and result.exit_code is None
        timed_pids = json.loads(witness.read_text())
        assert all(not _alive(pid) for pid in timed_pids)
        rerun = ctx.run(_request(root, "print('same supervisor')"))
        assert rerun.exit_code == 0 and rerun.stdout == "same supervisor\n"
        assert client._spawned.pid == owner_pid and client._spawned.poll() is None
        assert json.loads(Path(client.control, "session.json").read_text()) == marker
    finally:
        client.stop_owned(deadline=time.monotonic() + 15)
    assert client._spawned.poll() == 0
    assert not Path(client.control, "session.json").exists()
    assert not Path(client.control, "control.sock").exists()
    assert all(not _alive(pid) for pid in timed_pids)
    assert (root / "context.json").exists()


def test_real_context_service_bridge_with_tiny_recipe_fixture(session, monkeypatch):
    # Real factory/context/client/socket/native engine. The existing session
    # fixture substitutes a tiny CPU recipe and supervisor recipe bootstrap;
    # this case does not qualify canonical GPU startup or installation.
    from astrid.core.generation.engine_execution import create_local_backend
    from astrid.core.generation import engine_recipe

    fixture_client, payload, recipe, root, mode, started = session
    monkeypatch.setattr(engine_recipe, "load_recipe", lambda engine, platform: recipe)
    ctx, adapter = create_local_backend(engine=recipe.engine, platform=recipe.platform,
                                        placement="local", root=str(root),
                                        deadline=time.monotonic() + 10)
    client = ctx.service_client
    assert type(client) is custody.LinuxEngineServiceClient
    assert client.identity == fixture_client.identity

    def service(operation, owner=None):
        request = {**payload, "owner_id": owner}
        argv = ("python3", "-I", "-m", "astrid.core.generation.engine_execution",
                "--engine-service", operation, _json(request))
        result = ctx.run(CommandRequest(argv, str(root), {}, time.monotonic() + 8))
        assert result.exit_code == 0 and not result.timed_out and result.stderr == ""
        response = json.loads(result.stdout)
        assert set(response) == {"schema", "operation", "facts"}
        assert response["schema"] == custody._SCHEMA and response["operation"] == operation
        return response["facts"]

    try:
        assert service("probe")["state"] == "absent"
        first = service("start")
        assert first["state"] == "owned" and first["healthy"]
        second = service("health", first["owner_id"])
        assert second == first
        assert service("start", first["owner_id"])["owner_id"] == first["owner_id"]
        assert len(started.read_text().splitlines()) == 1
        marker = json.loads((root / "runtime/out/sessions/vibecomfy-h3/launch.json").read_text())
        assert marker["pid"] == client._spawned.pid
        assert marker["comfy_pid"] == int(started.read_text().strip())
        assert marker["launch_token"] == first["owner_id"]
    finally:
        client.stop_owned(deadline=time.monotonic() + 15)
    assert client._spawned.poll() == 0
    assert all(not _alive(int(pid)) for pid in started.read_text().splitlines())
    assert not Path(client.control, "control.sock").exists()
    assert not Path(client.control, "session.json").exists()


def test_engine_survives_two_finite_client_exits_and_uses_existing_vibe_registry(session):
    client, payload, recipe, root, mode, started = session
    # The first finite CLI establishes BOTH supervisor and engine, then exits.
    first = json.loads(_finite_client(client.identity, payload, "start", bootstrap=client._fixture_program).stdout)
    pid = int(started.read_text().strip())
    second = json.loads(_finite_client(client.identity, payload, "health").stdout)
    assert first["healthy"] and second["healthy"]
    assert first["owner_id"] == second["owner_id"]
    assert first["launch_sha256"] == _sha(payload["launch"])
    assert started.read_text().splitlines() == [str(pid)] and _alive(pid)
    assert _call(_client(client.identity), payload, "start")["owner_id"] == first["owner_id"]
    registry = root / "runtime/out/sessions/vibecomfy-h3"
    marker = json.loads((registry / "launch.json").read_text())
    assert marker["comfy_pid"] == pid and marker["pid"] != pid
    assert marker["launch_token"] == first["owner_id"]
    assert Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1] == str(marker["pid"])
    from vibecomfy.runtime import session as vibe
    assert vibe._session_composite_ownership_verified(registry, marker["pid"]) is True
    assert vibe._session_ready(registry) is True
    config = json.loads((registry / "config.json").read_text())
    assert config["models_root"] == str(root / "models")
    assert config["output_directory"] == str(root / "runtime/ComfyUI/output")
    assert (registry / "source_revision").read_text() == next(s.pin.revision for s in recipe.sources if s.name == "vibecomfy")
    expected = hashlib.sha256(b"tiny.py\0" + hashlib.sha256(b"# immutable fixture source\n").digest()).hexdigest()
    assert (registry / "source_content_digest").read_text() == "sha256:" + expected
    nodes = {"MiniMaxH3ReferenceToVideo": {}, "VHS_VideoCombine": {}}
    assert first["object_info_sha256"] == _sha(nodes)
    assert len(list(Path(client.control).glob("*.json"))) == 1


def test_timeout_drains_only_owned_descendants_then_healthy_rerun(session):
    client, payload, recipe, root, mode, started = session
    first = _call(client, payload)
    outside = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    child_file = root / "command-child"
    code = f'''import subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
Path({str(child_file)!r}).write_text(str(child.pid))
time.sleep(60)
'''
    try:
        result = client.run(_request(root, code, seconds=1))
        assert result.timed_out is True and result.exit_code is None
        assert not _alive(int(child_file.read_text()))
        assert outside.poll() is None
        rerun = client.run(_request(root, "print('rerun')"))
        assert rerun.exit_code == 0 and rerun.stdout == "rerun\n"
        assert _call(client, payload, "health")["owner_id"] == first["owner_id"]
        assert len(started.read_text().splitlines()) == 1
    finally:
        outside.terminate()
        outside.wait(timeout=5)


def test_command_exit_drains_descendants_and_preserves_nonzero_exit(session):
    client, payload, recipe, root, mode, started = session
    child_file = root / "child"
    result = client.run(_request(root, f'''import subprocess,sys,time
from pathlib import Path
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
Path({str(child_file)!r}).write_text(str(p.pid))
time.sleep(.2)
raise SystemExit(7)
'''))
    assert result.exit_code == 7 and not result.timed_out
    assert not _alive(int(child_file.read_text()))


def test_output_is_drained_bounded_and_env_unsets_are_applied(session):
    client, payload, recipe, root, mode, started = session
    result = client.run(_request(root, "import os;os.write(1,b'A'*200000);os.write(2,b'B'*200000);print(os.environ.get('CUSTODY_TEST'))",
                                 limit=1024, env={"CUSTODY_TEST": None}))
    assert result.exit_code == 0
    assert result.stdout_truncated and result.stderr_truncated
    assert len(result.stdout.encode()) <= 1024 and len(result.stderr.encode()) <= 1024
    assert result.stdout.endswith("None\n")


@pytest.mark.parametrize("limit", [0, 17])
def test_invalid_utf8_output_obeys_returned_byte_limit(session, limit):
    client, payload, recipe, root, mode, started = session
    result = client.run(_request(root, "import os;os.write(1,b'\\xff'*10);os.write(2,b'\\xfe'*10)", limit=limit))
    assert result.exit_code == 0
    assert result.stdout_truncated and result.stderr_truncated
    assert len(result.stdout.encode()) <= limit and len(result.stderr.encode()) <= limit


@pytest.mark.parametrize("mode_name", ["missing", "redirect", "nan", "oversize", "slow", "exit"])
def test_failed_launch_is_drained_and_same_supervisor_can_rerun(session, mode_name):
    client, payload, recipe, root, mode, started = session
    mode.write_text(mode_name)
    with pytest.raises((DeadlineExceeded, custody.EndpointHealthError)):
        _call(client, payload, seconds=1.5)
    assert started.exists()
    assert all(not _alive(int(pid)) for pid in started.read_text().splitlines())
    mode.write_text("healthy")
    result = _call(client, payload)
    assert result["healthy"] is True
    assert len(started.read_text().splitlines()) == 2


def test_unowned_port_never_adopted_or_stopped(session):
    client, payload, recipe, root, mode, started = session
    with socket.socket() as external:
        external.bind(("127.0.0.1", recipe.start.port))
        external.listen()
        assert _call(client, payload, "probe")["state"] == "unowned"
        assert _call(client, payload)["state"] == "unowned"
        assert external.getsockname()[1] == recipe.start.port
        assert not started.exists()


def test_changed_launch_or_installation_cannot_reuse_service(session):
    client, payload, recipe, root, mode, started = session
    first = _call(client, payload)
    bad = {**payload, "launch": {**payload["launch"], "argv": ["not-the-recipe"]}}
    with pytest.raises(EndpointOwnershipError):
        _call(client, bad)
    changed = _client({**client.identity, "helper_sha256": "f" * 64})
    with pytest.raises(IntegrityError):
        _call(changed, payload)
    assert _call(client, payload, "probe")["owner_id"] == first["owner_id"]
    assert len(started.read_text().splitlines()) == 1


def _running_client(client, root, witness):
    code = f"from pathlib import Path;import os,time;Path({str(witness)!r}).write_text(str(os.getpid()));time.sleep(60)"
    program = f'''
import time
from astrid.core.generation.engine_custody import LinuxEngineServiceClient
from astrid.core.generation.engine_contracts import CommandRequest
client=LinuxEngineServiceClient(**{client.identity!r})
print(client.run(CommandRequest(({sys.executable!r},'-c',{code!r}),{str(root)!r},{{}},time.monotonic()+30)))
'''
    return subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_client_disconnect_cancels_command_but_retains_engine(session):
    client, payload, recipe, root, mode, started = session
    first = _call(client, payload)
    witness = root / "cancelled-command"
    finite = _running_client(client, root, witness)
    try:
        _wait(witness.exists)
        finite.terminate()
        finite.wait(timeout=5)
        _wait(lambda: not _alive(int(witness.read_text())))
        assert _call(client, payload, "health")["owner_id"] == first["owner_id"]
    finally:
        if finite.poll() is None:
            finite.terminate()
            finite.wait(timeout=5)
        finite.communicate(timeout=5)


def test_explicit_stop_cancels_active_command_and_engine(session):
    client, payload, recipe, root, mode, started = session
    _call(client, payload)
    retained = [root / "models/retained-model", root / ".astrid-engine/environments/comfy/requirements.lock"]
    for path in retained:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"retained bytes\n")
    witness = root / "stopped-command"
    finite = _running_client(client, root, witness)
    try:
        _wait(witness.exists)
        _client(client.identity).stop_owned(deadline=time.monotonic() + 15)
        finite.communicate(timeout=10)
        assert not _alive(int(witness.read_text()))
        assert all(not _alive(int(pid)) for pid in started.read_text().splitlines())
        assert not Path(client.control, "session.json").exists()
        assert all(path.read_bytes() == b"retained bytes\n" for path in retained)
    finally:
        if finite.poll() is None:
            finite.terminate()
            finite.wait(timeout=5)


def test_cancelled_start_cleans_engine_and_allows_healthy_rerun(session):
    client, payload, recipe, root, mode, started = session
    _call(client, payload, "probe")
    mode.write_text("slow")
    program = f'''
import time
from astrid.core.generation.engine_custody import LinuxEngineServiceClient
LinuxEngineServiceClient(**{client.identity!r}).call('start',{payload!r},deadline=time.monotonic()+20)
'''
    finite = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _wait(started.exists)
        finite.terminate()
        finite.wait(timeout=5)
        _wait(lambda: not _alive(int(started.read_text().strip())))
        mode.write_text("healthy")
        assert _call(client, payload)["healthy"]
    finally:
        if finite.poll() is None:
            finite.terminate()
            finite.wait(timeout=5)
        finite.communicate(timeout=5)


def test_stale_session_marker_cannot_signal_a_live_unrelated_pid(tmp_path):
    root = tmp_path / "install"
    client = _client(_identity(root))
    outside = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    control = Path(client.control)
    control.mkdir(parents=True, mode=0o700)
    control.parent.chmod(0o700)
    (control / "owner.lock").touch(mode=0o600)
    (control / "session.json").write_text(_json({"pid": outside.pid, "owner_id": "stale"}))
    (control / "session.json").chmod(0o600)
    try:
        with pytest.raises(CustodyUnresolved):
            client.run(_request(root, "print('must not run')"))
        with pytest.raises(CustodyUnresolved):
            client.stop_owned(deadline=time.monotonic() + 1)
        assert outside.poll() is None
    finally:
        outside.terminate()
        outside.wait(timeout=5)


def test_original_native_owner_loss_refuses_replacement(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    client = _client(_identity(root))
    assert client.run(_request(root, "print('finished')")).exit_code == 0
    # Kill only this exact retained test owner, after its finite child drained.
    # No engine/command remains; the test intentionally retains its stale marker.
    client._spawned.kill()
    client._spawned.wait(timeout=5)
    replacement = _client(client.identity)
    with pytest.raises(CustodyUnresolved):
        replacement.run(_request(root, "print('must not run')"))
    assert replacement._spawned is None
    assert Path(client.control, "session.json").exists()


@pytest.mark.parametrize("unsafe", ["permissions", "symlink"])
def test_private_control_path_required(tmp_path, unsafe):
    root = tmp_path / "install"
    root.mkdir()
    state = root / ".astrid-engine"
    if unsafe == "symlink":
        destination = tmp_path / "elsewhere"
        destination.mkdir()
        state.symlink_to(destination, target_is_directory=True)
    else:
        state.mkdir(mode=0o777)
        state.chmod(0o777)
    with pytest.raises(UnsafePathError):
        _client(_identity(root)).run(_request(root, "print('must not run')"))


def test_invalid_command_deadline_and_output_budget_do_not_spawn(session):
    client, payload, recipe, root, mode, started = session
    with pytest.raises(DeadlineExceeded):
        client.run(_request(root, "raise SystemExit(99)", seconds=-1))
    with pytest.raises(RecipeValidationError):
        client.run(_request(root, "raise SystemExit(99)", limit=2 * 1024 * 1024))
    assert client.run(_request(root, "print('still healthy')")).exit_code == 0


@pytest.fixture
def installed_consumer(session, monkeypatch):
    """Real registry/engine, explicit CPU recipe and synthetic installed receipt.

    The receipt has the canonical lock inventory but this fixture does not
    download/qualify those models or claim a real installation.
    """
    from types import SimpleNamespace
    from astrid.core.generation import engine_recipe
    from astrid.core.generation.engine_contracts import DownloadResult, HealthEvidence, InstallReceipt, StepReceipt
    from astrid.core.generation.engine_runner import _plain, plan_install
    from astrid.packs.runpod.installed_session import LocalInstalledVibeComfySessionTransport
    from astrid.packs.runpod.worker_session import RunPodManagedVibeComfySessionOwner
    from vibecomfy.runtime import session as vibe

    client, payload, recipe, root, mode, started = session
    facts = _call(client, payload)
    monkeypatch.setattr(engine_recipe, "load_recipe", lambda engine, platform: recipe)
    monkeypatch.setattr(vibe, "find_repo_root", lambda: root / "runtime/vibecomfy")
    plan = plan_install(recipe, root=str(root), placement="local")
    models = engine_recipe.load_model_lock(recipe.models_lock)
    stamp = "2026-10-27T23:00:00+00:00"
    receipt = InstallReceipt(
        "astrid.engine-install.v1", "fixture-receipt", recipe.engine, recipe.platform,
        "local", str(root), "installed", False, plan.recipe_sha256, plan.models_lock_sha256,
        {e.name: "a" * 64 for e in recipe.environments},
        {s.name: s.pin.revision for s in recipe.sources},
        tuple(DownloadResult(m.name, str(root / "models" / m.destination), "skipped",
                             m.bytes, m.sha256, True, 0) for m in models.models),
        tuple(StepReceipt(s.id, s.kind, "executed", "passed",
                          {"owner_id": facts["owner_id"], "launch_sha256": facts["launch_sha256"]}
                          if s.kind == "start" else {}) for s in plan.steps),
        HealthEvidence("healthy", stamp, facts["object_info_sha256"], True), stamp, stamp, None,
    )
    (root / ".astrid-engine/install.json").write_text(_json(_plain(receipt)))
    registry = root / "runtime/out/sessions/vibecomfy-h3"
    # Noncanonical whitespace proves that selection hashes actual bytes.
    config = json.loads((registry / "config.json").read_bytes())
    raw = json.dumps(config, indent=2).encode() + b"\n"
    (registry / "config.json").write_bytes(raw)
    reference = SimpleNamespace(
        effective_target={"kind": "runpod", "provider_account_ref": "account-1", "pod_id": "pod-1"},
        executable=SimpleNamespace(path=root / "runtime/venv/bin/python"),
        model_root=root / "models", output_root=Path(config["output_directory"]),
        session_ref="vibecomfy-h3", session_config_digest="sha256:" + hashlib.sha256(raw).hexdigest(),
    )
    readiness = {
        "status": "cpu_ready", "gpu_qualified": False,
        "python_executable": str(reference.executable.path),
        "effective_paths": {"candidate_namespace": str(root), "comfy_root": str(root / "runtime/ComfyUI"),
                            "model_root": str(reference.model_root), "output_root": str(reference.output_root),
                            "vibecomfy_root": str(root / "runtime/vibecomfy")},
    }
    transport = LocalInstalledVibeComfySessionTransport()
    owner = RunPodManagedVibeComfySessionOwner(transport)
    return owner, transport, reference, readiness, registry


def test_installed_consumer_retains_exact_engine_and_detach_has_no_stop_authority(installed_consumer, session, monkeypatch):
    from astrid.packs.runpod.worker_session import ManagedSessionError
    from vibecomfy.commands import session as commands

    owner, transport, reference, readiness, registry = installed_consumer
    client, payload, recipe, root, mode, started = session
    before = {name: (registry / name).read_bytes() for name in (
        "pid", "comfy_pid", "comfy_process_start_identity", "launch.json", "config.json")}
    def forbidden(*args, **kwargs):
        pytest.fail("attachment called a legacy engine start/stop")
    monkeypatch.setattr(commands, "_cmd_session_start", forbidden)
    monkeypatch.setattr(commands, "_cmd_session_stop", forbidden)
    monkeypatch.setattr(transport, "start", forbidden)
    monkeypatch.setattr(transport, "stop", forbidden)
    selection = owner.select(reference, readiness, port=recipe.start.port)
    receipt = owner.start(selection)
    assert selection.config["cwd"] == str(root / "runtime/ComfyUI")
    assert selection.config_digest == reference.session_config_digest
    assert selection.config_digest != "sha256:" + _sha(selection.config)
    assert receipt.daemon_pid == client._spawned.pid
    assert receipt.comfy_pid == int(started.read_text().strip())
    assert owner.resume(selection, receipt.as_dict()) == receipt
    assert owner.observe_config(selection, receipt)["session_config_digest"] == reference.session_config_digest
    with pytest.raises(ManagedSessionError, match="original supervisor stop"):
        owner.stop(selection, receipt)
    assert all((registry / name).read_bytes() == raw for name, raw in before.items())
    assert len(started.read_text().splitlines()) == 1
    assert _alive(receipt.daemon_pid) and _alive(receipt.comfy_pid)
    assert _call(client, payload, "health")["owner_id"] == json.loads(before["launch.json"])["launch_token"]


@pytest.mark.parametrize("change", ["absent", "wrong_config", "source_revision", "source_content", "model_root",
                                         "receipt", "receipt_owner", "receipt_launch", "birth", "config_bytes",
                                         "live_source", "missing_receipt", "receipt_model"])
def test_installed_consumer_refuses_changed_evidence_without_legacy_control(installed_consumer, session, monkeypatch, change):
    from astrid.packs.runpod.worker_session import ManagedSessionError

    owner, transport, reference, readiness, registry = installed_consumer
    client, payload, recipe, root, mode, started = session
    selection = owner.select(reference, readiness, port=recipe.start.port)
    receipt = owner.start(selection)
    monkeypatch.setattr(transport, "start", lambda *_: pytest.fail("legacy start"))
    monkeypatch.setattr(transport, "stop", lambda *_: pytest.fail("legacy stop"))
    if change == "absent":
        (registry / "config.json").unlink()
    elif change == "live_source":
        (root / "runtime/vibecomfy/tiny.py").write_text("# changed effective source at the same commit\n")
    elif change == "missing_receipt":
        (root / ".astrid-engine/install.json").unlink()
    elif change in {"wrong_config", "model_root"}:
        config = json.loads((registry / "config.json").read_bytes())
        config["cwd" if change == "wrong_config" else "models_root"] = str(root / "foreign")
        (registry / "config.json").write_text(json.dumps(config))
    elif change in {"source_revision", "source_content"}:
        (registry / ("source_revision" if change == "source_revision" else "source_content_digest")).write_text(
            "0" * 40 if change == "source_revision" else "sha256:" + "0" * 64)
    elif change.startswith("receipt"):
        path = root / ".astrid-engine/install.json"
        value = json.loads(path.read_bytes())
        if change == "receipt_model":
            value["models"][0]["path"] = str(root / "foreign-model")
        elif change == "receipt":
            value["recipe_sha256"] = "0" * 64
        else:
            facts = next(s["facts"] for s in value["steps"] if s["kind"] == "start")
            facts["owner_id" if change == "receipt_owner" else "launch_sha256"] = "0" * 64
        path.write_text(json.dumps(value))
    elif change == "birth":
        marker = json.loads((registry / "launch.json").read_bytes())
        marker["process_start_identity"] = "stale-birth"
        (registry / "launch.json").write_text(json.dumps(marker))
    else:
        with (registry / "config.json").open("ab") as stream:
            stream.write(b"\n")
    with pytest.raises(ManagedSessionError):
        owner.start(selection)
    with pytest.raises(ManagedSessionError):
        owner.resume(selection, receipt)
    with pytest.raises(ManagedSessionError):
        owner.stop(selection, receipt)
    assert started.read_text().splitlines() == [str(receipt.comfy_pid)]
    assert _alive(receipt.comfy_pid)


def test_installed_consumer_capacity_replacement_refuses_before_controls(installed_consumer, monkeypatch):
    from astrid.core.generation.backends.vibecomfy import CheckoutServerAdapter
    from types import SimpleNamespace

    owner, transport, reference, readiness, registry = installed_consumer
    adapter = object.__new__(CheckoutServerAdapter)
    adapter._host_session = {"session_dir": str(registry)}
    def forbidden(*args, **kwargs):
        pytest.fail("capacity replacement reached engine/CLI control")
    adapter._engine = SimpleNamespace(_clear_warm=forbidden, _clear_prepared=forbidden,
                                      _clear_retention_hint=forbidden, release=forbidden)
    adapter._control_preflight = forbidden
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="capacity replacement"):
        adapter.release(reason="capacity_replacement")
    with pytest.raises(ValueError, match="capacity replacement"):
        adapter._restart_owned_session()


@pytest.mark.parametrize("failure", [None, "wrong_bearer", "denied_file", "corrupt_digest"])
def test_remote_installed_files_cannot_fabricate_live_attachment(installed_consumer, session, tmp_path, failure):
    import threading
    from types import SimpleNamespace
    from runpod_lifecycle.https import HttpsPodTransport
    from runpod_lifecycle.pod_agent import AgentServer, Handler
    from astrid.packs.runpod.installed_session import RunPodInstalledVibeComfySessionTransport
    from astrid.packs.runpod.worker_session import ManagedSessionError, create_managed_session_owner

    _, _, reference, readiness, registry = installed_consumer
    client, payload, recipe, root, mode, started = session
    token = "fixture-https-bearer-" + "x" * 40
    policy = {"schema": "astrid.pod-observation.v1",
              "roots": [str(registry)], "files": [str(root / ".astrid-engine/install.json")]}
    if failure == "denied_file":
        policy["files"] = []
    server = AgentServer(("127.0.0.1", 0), token, tmp_path / "agent-state", observation_policy=policy)
    calls = []
    class AuditedHandler(Handler):
        def handle_request(self):
            calls.append((self.command, self.path, list(self.headers)))
            return super().handle_request()

        def reply(self, status, data=None, **kwargs):
            if failure == "corrupt_digest" and isinstance(data, dict) and "sha256" in data:
                data = {**data, "sha256": "0" * 64}
            return super().reply(status, data, **kwargs)

        do_GET = do_POST = do_PUT = do_DELETE = handle_request
    server.RequestHandlerClass = AuditedHandler
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def https_transport():
        bearer = "wrong-fixture-bearer-" + "y" * 40 if failure == "wrong_bearer" else token
        return HttpsPodTransport("pod1", bearer, base_url=f"http://127.0.0.1:{server.server_port}")
    reference.effective_target["pod_id"] = "pod1"
    pod = SimpleNamespace(id="pod1", config=SimpleNamespace(transport="https"), https_transport=https_transport)
    owner = create_managed_session_owner(pod, session_ref=reference.session_ref)
    assert isinstance(owner.transport, RunPodInstalledVibeComfySessionTransport)
    try:
        if failure is None:
            selection = owner.select(reference, readiness, port=recipe.start.port)
            assert selection.config_digest == reference.session_config_digest
            with pytest.raises(ManagedSessionError, match="lacks live installer session composite verification"):
                owner.start(selection)
        else:
            with pytest.raises(ManagedSessionError):
                owner.select(reference, readiness, port=recipe.start.port)
        assert calls and all(method == "POST" and path == "/download-chunk" for method, path, _ in calls)
        assert all(not any(h.lower() in {"range", "upgrade"} for h in headers) for _, _, headers in calls)
        assert not server.jobs
        assert len(started.read_text().splitlines()) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_original_prepared_owner_records_installed_engine_without_allocation(installed_consumer, session, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from runtime_protocol.remote_worker_deployment import ProjectedLaunch
    from runtime_protocol.remote_worker_activation import RemotePreparationLaunch
    from astrid.packs.runpod.worker_owner import RunPodRemoteWorkerPreparer, RunPodWorkerOwnerError
    from astrid.packs.runpod.worker_preparation import write_target_record
    from tests.packs.runpod.test_worker_preparation import _claim_handle
    from runpod_lifecycle import api

    owner, transport, reference, readiness, registry = installed_consumer
    client, payload, recipe, root, mode, started = session
    claim_path = tmp_path / "original-claim.json"
    claim = _claim_handle(provider_account_ref="account-1", pod_id="pod-1")
    write_target_record(claim_path, claim)
    def forbidden(*args, **kwargs):
        pytest.fail("session attachment attempted a provider operation or legacy daemon control")
    for name in ("create_pod", "terminate_pod", "get_pod_status", "list_pods"):
        if hasattr(api, name):
            monkeypatch.setattr(api, name, forbidden)
    monkeypatch.setattr(transport, "start", forbidden)
    monkeypatch.setattr(transport, "stop", forbidden)
    reference.digest = lambda: "sha256:" + "e" * 64
    reference.credential_ref = str(root / "credentials/executor.token")
    reference.ready_file = None
    reference.support_root = root / "support"
    reference.source_checkout = root / "prepared/source/astrid"
    reference.source_closure_digest = "sha256:" + "f" * 64
    launch = ProjectedLaunch((str(reference.executable.path), "-m", "fixture-host"), (),
                             reference.digest(), reference.source_checkout)
    staging = {"cpu_readiness": readiness, "plan_digest": "sha256:" + "d" * 64}
    process = SimpleNamespace(as_dict=lambda: {"fixture_host": "selected-original-host"})
    launches = []
    class ProcessOwner:
        async def launch_projected(self, projected):
            launches.append(projected)
            return process
    preparer = RunPodRemoteWorkerPreparer(
        SimpleNamespace(id="pod-1"), provider_account_ref="account-1", runtime_session_id="runtime-1",
        claim_handle_path=claim_path, journal_dir=tmp_path / "journal", staging_inputs={},
        preparation_transport=object(), process_transport=object(), session_owner=owner,
        session_port=recipe.start.port,
    )
    monkeypatch.setattr(preparer, "_stage", lambda ref: staging)
    monkeypatch.setattr(preparer, "_owner", lambda ref: ProcessOwner())
    handle = preparer.prepare(RemotePreparationLaunch(reference, launch))
    assert handle.session_receipt.daemon_pid == client._spawned.pid
    assert handle.session_receipt.comfy_pid == int(started.read_text().strip())
    assert handle.session_selection.config_digest == reference.session_config_digest
    assert len(launches) == 1
    saved = json.loads(claim_path.read_bytes())
    # Existing claim fields, including sole provider-release ownership, survive.
    assert {key: saved[key] for key in claim} == claim
    assert saved["managed_session"]["claim_operation_id"] == claim["operation_id"]
    assert saved["managed_session"]["receipt"] == handle.session_receipt.as_dict()
    assert saved["managed_session"]["selection"]["config"] == json.loads((registry / "config.json").read_bytes())
    with preparer._claim_owner() as (current, lock_fd):
        with pytest.raises(RunPodWorkerOwnerError, match="conflicts with original session custody"):
            preparer._write_managed_session(
                current, handle.session_selection,
                replace(handle.session_receipt, comfy_birth_id="different-incarnation"), owner_lock_fd=lock_fd,
            )
    assert json.loads(claim_path.read_bytes()) == saved
    assert len(started.read_text().splitlines()) == 1
