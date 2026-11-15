"""Pinned sources stay clean when the engine imports them.

A third-party node may commit stale bytecode and ship no .gitignore. Importing it
without a pycache prefix rewrites the tracked .pyc, which then fails the source
revalidation. The engine's one process env points bytecode at its own prefix tree.
"""

import os
import subprocess
import sys

from astrid.core.generation import engine_runner


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _source(tmp_path):
    repo = tmp_path / "node"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "node.py").write_text("VALUE = 1\n", encoding="utf-8")
    cache = repo / "__pycache__"
    cache.mkdir()
    stale = cache / f"node.{sys.implementation.cache_tag}.pyc"
    stale.write_bytes(b"\x00stale-bytecode-not-a-valid-pyc")
    _git(repo, "add", "node.py", f"__pycache__/{stale.name}")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "pin")
    return repo


def _import(repo, env):
    subprocess.run([sys.executable, "-c", "import node"], cwd=str(repo), env=env, check=True,
                   capture_output=True)


def test_engine_env_keeps_committed_bytecode_clean_under_import(tmp_path):
    repo = _source(tmp_path)
    root = tmp_path / "engine"
    env = engine_runner.engine_process_env({"PATH": os.environ["PATH"]}, str(root))
    _import(repo, env)
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""


def test_import_without_the_prefix_rewrites_the_tracked_bytecode(tmp_path):
    repo = _source(tmp_path)
    _import(repo, {"PATH": os.environ["PATH"]})
    assert _git(repo, "status", "--porcelain", "--untracked-files=all").strip() != ""


def test_unrelated_untracked_file_still_fails_after_the_prefix_import(tmp_path):
    repo = _source(tmp_path)
    env = engine_runner.engine_process_env({"PATH": os.environ["PATH"]}, str(tmp_path / "engine"))
    _import(repo, env)
    (repo / "notes.txt").write_text("generated\n", encoding="utf-8")
    assert "notes.txt" in _git(repo, "status", "--porcelain", "--untracked-files=all")


_SERVER = r'''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
port = int(sys.argv[1]); mode = Path(sys.argv[2]); started = Path(sys.argv[3])
with started.open("a") as stream:
    stream.write("served\n")
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if mode.read_text() == "exit":
            raise SystemExit(3)
        if self.path == "/system_stats":
            value = {"system": {"comfyui_version": "0.36.0"}, "devices": [{"vram_total": 24000, "vram_free": 20000}]}
        else:
            value = {"MiniMaxH3ReferenceToVideo": {}, "VHS_VideoCombine": {}, "UNETLoader": {}, "VAELoader": {}}
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *args):
        pass
HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''

# Stands in for the ComfyUI entrypoint: records the env it was launched with, imports the
# pinned source (with its committed stale bytecode), then serves the health routes.
_LAUNCH = r'''
import json, os, runpy, sys
from pathlib import Path
port, mode, started, record, source, server = sys.argv[1:7]
Path(record).write_text(json.dumps({"prefix": os.environ.get("PYTHONPYCACHEPREFIX")}))
sys.path.insert(0, source)
import tiny  # noqa: F401  (committed stale tiny.cpython-*.pyc in the pinned source)
sys.argv = [server, port, mode, started]
runpy.run_path(server, run_name="__main__")
'''


def test_real_service_launch_through_supervisor_keeps_pinned_source_clean(tmp_path, monkeypatch):
    """The ComfyUI launch the engine actually starts: supervisor, start_request env, child import."""
    import json
    import socket
    import time
    from dataclasses import replace
    from pathlib import Path

    from astrid.core.generation import engine_custody as custody
    from astrid.core.generation.engine_contracts import StartSpec
    from astrid.core.generation.engine_platforms import LinuxPlatformAdapter
    from astrid.core.generation.engine_recipe import load_recipe, recipe_digest
    from astrid.core.generation.engine_runner import _json, _sha, helper_manifest

    platform = "linux-x86_64-cu130"
    root = tmp_path / "install"
    comfy = root / "runtime/ComfyUI"
    (comfy / "output").mkdir(parents=True)
    source = root / "runtime/vibecomfy"
    cache = source / "__pycache__"
    cache.mkdir(parents=True)
    (source / "tiny.py").write_text("VALUE = 1\n", encoding="utf-8")
    (cache / f"tiny.{sys.implementation.cache_tag}.pyc").write_bytes(b"\x00stale-bytecode-not-a-valid-pyc")
    _git(source, "init", "-q")
    _git(source, "add", "tiny.py", f"__pycache__/tiny.{sys.implementation.cache_tag}.pyc")
    _git(source, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "pin")
    revision = _git(source, "rev-parse", "HEAD").strip()
    (root / "tiny_http.py").write_text(_SERVER, encoding="utf-8")
    (root / "launch.py").write_text(_LAUNCH, encoding="utf-8")
    mode, started, record = root / "mode", root / "started", root / "record.json"
    mode.write_text("healthy", encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = load_recipe("comfyui", platform)
    argv = (sys.executable, str(root / "launch.py"), str(port), str(mode), str(started), str(record),
            str(source), str(root / "tiny_http.py"))
    recipe = replace(base, sources=tuple(replace(s, pin=replace(s.pin, revision=revision))
                     if s.name == "vibecomfy" else s for s in base.sources),
                     start=StartSpec(argv, str(comfy), {"VIBECOMFY_HEADLESS": "1"}, "127.0.0.1", port),
                     health=replace(base.health, timeout_seconds=4, poll_interval_seconds=0.02))
    identity = dict(root=str(root), engine=recipe.engine, platform=recipe.platform,
                    recipe_sha256=recipe_digest(recipe), helper_sha256=_sha(helper_manifest(recipe)))
    # The supervisor and the runner both build the launch from the same recipe, through the adapter.
    program = f'''
import json, sys
from dataclasses import replace
from astrid.core.generation.engine_contracts import StartSpec
from astrid.core.generation.engine_recipe import load_recipe
from astrid.core.generation.engine_custody import _serve
base = load_recipe({"comfyui"!r}, {platform!r})
recipe = replace(base, sources=tuple(replace(s, pin=replace(s.pin, revision={revision!r})) if s.name == "vibecomfy" else s for s in base.sources),
                 start=StartSpec({argv!r}, {str(comfy)!r}, {{"VIBECOMFY_HEADLESS": "1"}}, "127.0.0.1", {port}),
                 health=replace(base.health, timeout_seconds=4, poll_interval_seconds=0.02))
_serve(json.loads(sys.argv[1]), recipe, int(sys.argv[2]), sys.argv[4])
'''
    monkeypatch.setattr(custody, "_supervisor_argv", lambda identity, lock_fd, token: [
        sys.executable, "-c", program, _json(identity), str(lock_fd), "--launch-token", token])
    adapter = LinuxPlatformAdapter(platform)
    request = adapter.start_request(recipe, str(root), deadline=time.monotonic() + 5)
    launch = {"argv": list(request.argv), "cwd": request.cwd,
              "env": {**request.env, "CUDA_VISIBLE_DEVICES": None}}
    client = custody.LinuxEngineServiceClient(**identity)
    payload = {**identity, "schema": custody._SCHEMA, "launch": launch, "owner_id": None,
               "timeout_seconds": recipe.health.timeout_seconds,
               "poll_interval_seconds": recipe.health.poll_interval_seconds}
    prefix = f"{root}/.astrid-engine/pycache"
    try:
        facts = client.call("start", payload, deadline=time.monotonic() + 30)
        assert facts["state"] == "owned", facts
        # The engine's ComfyUI child was launched with the prefix ...
        assert json.loads(record.read_text())["prefix"] == prefix
        # ... and so was the supervisor, which runs engine code of its own.
        supervisor = Path(f"/proc/{client._spawned.pid}/environ").read_bytes().split(b"\0")
        assert f"PYTHONPYCACHEPREFIX={prefix}".encode() in supervisor
        # The committed stale bytecode is ignored, so the pinned source tree is unchanged.
        assert _git(source, "status", "--porcelain", "--untracked-files=all") == ""
    finally:
        client.stop_owned(deadline=time.monotonic() + 15)
