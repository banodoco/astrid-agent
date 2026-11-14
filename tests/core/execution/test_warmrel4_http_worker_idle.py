"""Idle races through the host's spawned production HTTP action worker."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from tests.core.execution.test_local_engine_idle import run, task
from tests.core.execution.test_warm_hold import Timers
from tests.core.generation.test_engine_custody import _call, _wait
from tests.core.generation.test_engine_custody import native_scratch as native_scratch  # noqa: F401
from tests.core.generation.test_engine_custody import session as session  # noqa: F401
from tests.packs.vibecomfy.test_local_server_run import _workflow

pytestmark = pytest.mark.linux_native

_SERVER = r'''
"""ComfyUI 6bfaacc6 contract: server.py:1195-1203 only sets free flags.

main.py:319-399 executes and publishes history before applying flags at
386-399. This fake deliberately exposes the queue-check/POST window, while
ensuring free never interrupts execution. Files are deterministic test gates.
"""
import json, os, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from PIL import Image
port = int(sys.argv[1]); started = Path(sys.argv[3]); root = started.parent
started.write_text(str(os.getpid()) + '\n')
histories = {}; running = []; flags = {}; lock = threading.Lock()
schema = {name: {} for name in ('MiniMaxH3ReferenceToVideo','UNETLoader','VAELoader','VHS_VideoCombine')}
schema.update({
 'EmptyImage': {'input': {'required': {'width': ['INT', {'default': 16}], 'height': ['INT', {'default': 16}], 'batch_size': ['INT', {'default': 1}], 'color': ['INT', {'default': 0}]}}, 'output': ['IMAGE'], 'output_name': ['IMAGE'], 'output_node': False, 'python_module': 'nodes'},
 'SaveImage': {'input': {'required': {'images': ['IMAGE'], 'filename_prefix': ['STRING', {'default': 'astrid'}]}}, 'output': [], 'output_node': True, 'python_module': 'nodes'}})
def wait_file(name):
 end = time.monotonic() + 30
 while not (root / name).exists():
  if time.monotonic() >= end: raise RuntimeError('fixture gate expired: ' + name)
  time.sleep(.01)
def execute(key, prompt):
 (root / 'prompt-running').write_text(key)
 if (root / 'pause-prompt').exists(): wait_file('finish-prompt')
 filename = key + '.png'
 Image.new('RGB', (16, 16), (10, 20, 30)).save(root / filename)
 outputs = {node: {'images': [{'filename': filename, 'subfolder': '', 'type': 'output'}]}
            for node, value in prompt.items() if value['class_type'] == 'SaveImage'}
 with lock:
  histories[key] = {'outputs': outputs, 'status': {'completed': True, 'status_str': 'success', 'messages': []}}
  running.remove(key)
  (root / 'history-published').write_text(key)
  if flags:
   assert (root / 'history-published').exists()
   (root / 'flags-applied').write_text(json.dumps(flags))
   flags.clear()
class Handler(BaseHTTPRequestHandler):
 def log_message(self, *args): pass
 def send(self, value):
  data = json.dumps(value).encode(); self.send_response(200)
  self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
 def do_GET(self):
  if self.path == '/object_info': return self.send(schema)
  if self.path == '/system_stats': return self.send({'system': {'comfyui_version': '0.36.0'}, 'devices': [{'vram_total': 24000, 'vram_free': 20000}]})
  if self.path == '/queue':
   with lock: value = {'queue_running': [[1, key] for key in running], 'queue_pending': []}
   if (root / 'pause-queue').exists():
    (root / 'queue-checked').write_text('yes'); wait_file('return-queue')
   return self.send(value)
  if self.path.startswith('/history/'):
   key = self.path.rsplit('/', 1)[1]
   with lock: value = {key: histories[key]} if key in histories else {}
   return self.send(value)
  if self.path.startswith('/view?'):
   name = parse_qs(urlsplit(self.path).query)['filename'][0]; data = (root / name).read_bytes()
   self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data); return
  self.send({})
 def do_POST(self):
  body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
  if self.path in {'/api/free', '/free'}:
   with lock:
    flags.update(body)
    with (root / 'release-posts').open('a') as stream: stream.write(self.path + '\n')
   # Stock ComfyUI acknowledges the flag; it does not report an unload result.
   return self.send({})
  if self.path == '/prompt':
   key = uuid.uuid4().hex
   with lock: running.append(key)
   threading.Thread(target=execute, args=(key, body['prompt']), daemon=True).start()
   return self.send({'prompt_id': key, 'number': 1, 'node_errors': {}})
  self.send({})
ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
'''


@pytest.fixture
def http_worker(session, tmp_path, monkeypatch):  # noqa: F811
    client, payload, recipe, root, mode, started = session
    (root / "tiny_http.py").write_text(_SERVER)
    _call(client, payload)
    workflow = _workflow()
    workflow["nodes"][0].update(type="EmptyImage", widgets_values=[16, 16, 1, 0],
                                properties={"Node name for S&R": "EmptyImage"})
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(workflow))
    host = GenericPackHost(pack_roots=[Path(__file__).resolve().parents[3] / "astrid/packs"],
                           attempt_root=tmp_path / "attempts")
    host.discover()
    timers = Timers()
    idle = host._local_engine_idle
    idle._holds._timer = timers
    attempts = []

    def execute(value, *, _terminal, **kwargs):
        idle.retain(client)
        attempt = tmp_path / value["task"]["id"]
        output = attempt / "outputs"
        output.mkdir(parents=True)
        result = host._run_command_definition(host.capabilities["vibecomfy.run"], {
            "workflow": str(path), "comfy_url": f"http://127.0.0.1:{recipe.start.port}",
            "comfy_output_dir": str(root), "task_identity": value["task"]["id"],
            "attempt_identity": attempt.name,
        }, output, attempt)
        attempts.append((attempt, result))
        _terminal["recorded"] = True
        return {"status": "succeeded"}

    monkeypatch.setattr(host, "_run_task_impl", execute)
    monkeypatch.setattr(host, "_write_task_result", lambda terminal: None)
    host.client = SimpleNamespace(fail=Mock(), settle=Mock())
    try:
        yield host, idle, timers, root, attempts
    finally:
        (root / "finish-prompt").touch()
        (root / "return-queue").touch()
        host.shutdown()


def _assert_published(attempts):
    attempt, result = attempts[-1]
    assert result.returncode == 0
    assert result.process_id != os.getpid(), "the production action must run in a child"
    manifest = json.loads((attempt / "outputs/manifest.json").read_text())
    managed = manifest["managed_generation_result"]
    assert managed["task_id"] == attempt.name
    output = next(row for row in manifest["outputs"] if row["role"] == "result")
    image = attempt / "outputs" / output["path"]
    assert image.is_file() and image.stat().st_size > 0
    return image


def test_delayed_idle_callback_after_completed_http_job_is_consumed(http_worker, monkeypatch):
    host, idle, timers, root, attempts = http_worker
    assert run(host)["status"] == "succeeded"
    entered, resume = threading.Event(), threading.Event()
    original = idle._release_if_idle

    def delayed(generation):
        entered.set()
        assert resume.wait(15)
        original(generation)

    monkeypatch.setattr(idle, "_release_if_idle", delayed)
    expiry = threading.Thread(target=timers.fire)
    expiry.start()
    try:
        assert entered.wait(5)
        assert run(host, task("next"))["status"] == "succeeded"
        _assert_published(attempts)
        fresh_deadline = timers.live()
        resume.set()
        expiry.join(5)
        assert not expiry.is_alive()
        assert not (root / "release-posts").exists()
        assert timers.live() is fresh_deadline, "only the next job's idle deadline remains"
        assert idle.status()["active_jobs"] == 0
    finally:
        resume.set()
        expiry.join(5)


def test_release_flag_during_spawned_http_prompt_preserves_output(http_worker):
    host, idle, timers, root, attempts = http_worker
    assert run(host)["status"] == "succeeded"
    (root / "prompt-running").unlink()
    (root / "history-published").unlink()
    for gate in ("pause-queue", "pause-prompt"):
        (root / gate).touch()
    expiry = threading.Thread(target=timers.fire)
    expiry.start()
    errors, results = [], []

    def job():
        try:
            results.append(run(host, task("arrived-during-release")))
        except BaseException as exc:  # noqa: BLE001 - relay child failures to the test thread
            errors.append(exc)

    worker = threading.Thread(target=job)
    try:
        _wait(lambda: (root / "queue-checked").exists())
        worker.start()
        _wait(lambda: (root / "prompt-running").exists())
        # Submission bypasses supervisor.work while release holds that lock.
        assert idle.status()["active_jobs"] == 1
        (root / "return-queue").touch()
        expiry.join(5)
        assert not expiry.is_alive()
        assert (root / "release-posts").read_text().splitlines() == ["/api/free"]
        assert worker.is_alive() and not errors
        assert not (root / "flags-applied").exists(), "free only sets flags during execution"
        assert not (root / "history-published").exists()
        (root / "finish-prompt").touch()
        worker.join(15)
        assert not worker.is_alive() and not errors, errors
        assert results == [{"status": "succeeded"}]
        image = _assert_published(attempts)
        key = (root / "history-published").read_text()
        assert hashlib.sha256(image.read_bytes()).digest() == hashlib.sha256((root / (key + ".png")).read_bytes()).digest()
        assert json.loads((root / "flags-applied").read_text()) == {"unload_models": True, "free_memory": True}
    finally:
        (root / "return-queue").touch()
        (root / "finish-prompt").touch()
        expiry.join(5)
        if worker.ident is not None:
            worker.join(15)
