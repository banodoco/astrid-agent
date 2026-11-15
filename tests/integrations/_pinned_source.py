"""Pinned CPU installations shared by managed generation integration seams.

The recipes, Git checks, installed environment, lock evidence and supervisor are
real. The HTTP peer and model-weight I/O remain CPU test inputs.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

# cause f96644329; ruling: machines 11:35 Sat
# Adapted from cells' verified-install fixture at 9d1a0cd66.

_SOURCE_CACHE = {}
SOURCE_COUNTS = {"verified": 0, "cloned": 0}


def pinned_comfy_checkout(cache_root):
    """Verified ComfyUI source pin used by the managed CPU execution fixture."""

    pins = Path(__file__).resolve().parents[2] / "astrid/packs/vibecomfy/pins.json"
    pin = json.loads(pins.read_text())["sources"]["comfyui"]["revision"]
    supplied = os.environ.get("ASTRID_TEST_COMFY_SOURCE")
    if not supplied and "ASTRID_CI_JOB" in os.environ:
        pytest.fail(f"ASTRID_TEST_COMFY_SOURCE must name a clean ComfyUI checkout at {pin} in CI")
    key = (pin, supplied)
    if key in _SOURCE_CACHE:
        return _SOURCE_CACHE[key]
    if supplied:
        source = Path(supplied)
    else:
        source = Path(cache_root) / "source"
        source.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", "--no-checkout", "--filter=blob:none",
                        "https://github.com/Comfy-Org/ComfyUI.git", str(source)], check=True, timeout=180)
        subprocess.run(["git", "-C", str(source), "checkout", "--quiet", "--detach", pin], check=True, timeout=180)
        SOURCE_COUNTS["cloned"] += 1
    assert subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip() == pin
    assert not subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip()
    SOURCE_COUNTS["verified"] += 1
    # One verified source per pytest process/session. Per-test native installs
    # still have independent Git checkouts and run the product integrity check.
    _SOURCE_CACHE[key] = source
    return source


@contextlib.contextmanager
def attached_comfy(comfy, port=0):
    """A human-attached ComfyUI on loopback: the stand-in pod's ComfyUI behind a real socket."""
    import http.server
    from urllib.parse import parse_qs, urlsplit

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, data, mime="application/json"):
            body = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/object_info":
                return self._send(200, comfy.object_info())
            if url.path == "/system_stats":
                return self._send(200, {"system": {"os": "linux"}})
            if url.path == "/queue":
                return self._send(200, {"queue_running": [], "queue_pending": []})
            if url.path.startswith("/history"):
                tail = url.path.partition("/history/")[2]
                history = getattr(comfy, "history", {})
                return self._send(200, {tail: history[tail]} if tail in history else {} if tail else history)
            if url.path == "/view":
                name = parse_qs(url.query)["filename"][0]
                comfy.views.append(name)
                data, mime = comfy.outputs[name]
                return self._send(200, data, mime)
            self._send(404, {})

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/prompt":
                status, payload = comfy.submit(json.loads(body))
                return self._send(status, payload)
            if self.path in ("/queue", "/interrupt", "/free"):
                return self._send(200, {})
            if self.path == "/upload/image":
                subfolder = re.search(rb'name="subfolder"\r\n\r\n([^\r]*)\r\n', body)
                name = re.search(rb'name="image"; filename="([^"]+)"', body)
                saved = name[1].decode()
                comfy.uploads[saved] = body
                comfy.input_files.add(f"{subfolder[1].decode()}/{saved}" if subfolder and subfolder[1] else saved)
                return self._send(200, {"name": saved, "subfolder": subfolder[1].decode() if subfolder else "",
                                        "type": "input"})
            self._send(404, {})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


@contextlib.contextmanager
def pinned_cpu_install(session, monkeypatch, tmp_path, comfy_source):
    """Reuse the verified native CPU installation with both real source pins.

    The shared fixture supplies an isolated environment, its hash-bound lock,
    real uv pip check and owned service. Its default tiny VibeComfy Git witness
    is replaced here by the product-pinned checkout used by the shipped job.
    GPU dependencies and inference remain outside this CPU seam's claim.
    """
    import vibecomfy

    from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION
    from astrid.core.pack.discovery import pack_discovery_scope
    from tests.integrations.test_managed_host_preparation import managed_install

    _, _, recipe, root, _, _ = session
    checkout = root / 'runtime' / 'vibecomfy'
    shutil.rmtree(checkout)
    subprocess.run(['git', 'clone', '--quiet', '--shared',
                    str(Path(vibecomfy.__file__).resolve().parents[1]), str(checkout)], check=True)
    revision = VIBECOMFY_ENGINE_REVISION
    subprocess.run(['git', 'checkout', '--quiet', '--detach', revision], cwd=checkout, check=True)
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip() == revision
    # The shared fixture sets each source's canonical origin itself.
    subprocess.run(['git', 'remote', 'remove', 'origin'], cwd=checkout, check=True)
    recipe = replace(recipe, sources=tuple(
        replace(source, pin=replace(source.pin, revision=revision))
        if source.name == 'vibecomfy' else source for source in recipe.sources))
    pinned_session = (*session[:2], recipe, *session[3:])
    profile = SimpleNamespace(comfy_source=comfy_source,
        extra_launch_flags=('--use-ck-attention', '--disable-comfy-compiler'),
        standin_distributions={'torch': '2.10.0+cu130', 'comfy-kitchen': '0.2.35', 'comfy-aimdo': '0.5.5'})
    installation = managed_install.__wrapped__(pinned_session, monkeypatch, profile, tmp_path)
    installed = next(installation)
    try:
        # Reuse admitted pack metadata while constructing and replaying one job.
        # The product scope fingerprints the complete trees on every lookup;
        # changed inputs still require admission and all engine checks still run.
        with pack_discovery_scope():
            yield installed
    finally:
        installation.close()


@contextlib.contextmanager
def owned_cpu_peer(provider, pinned_install, monkeypatch, *, extra_files=()):
    """Serve the slot schemas through the real pinned engine supervisor on CPU."""
    client_engine, _payload, recipe, root, _started, _bootstrap = pinned_install
    from astrid.core.generation import entries
    from astrid.core.generation.engine_local_endpoint import record_engine_root
    from astrid.core.generation.engine_recipe import load_recipe_model_lock

    # Catalog metadata must bypass the one-model CPU installation recipe even
    # when a provider is constructed after that installation fixture starts.
    files = [model.destination for engine in dict.fromkeys(("comfyui", *entries.runpod_engines()))
             for model in load_recipe_model_lock(engine, recipe.platform).models]
    # Preserve the original peer's receipt-only loader menus. Workflow-specific
    # declarations are opt-in extra_files; sparse disk placeholders do not make
    # absent TTS/SFX inventory appear ready over HTTP.
    files += list(extra_files)
    provider.comfy.installed_files = lambda: files
    record_engine_root("comfyui", str(root))
    monkeypatch.setenv("ASTRID_ENGINE_ROOT", str(root))

    records = root / "peer-records"
    records.mkdir(exist_ok=True)
    write_peer_inventory(provider, records)
    program = f'''\
import sys,json,os,threading
sys.path[:0]={[str(Path(__file__).resolve().parents[2]), *sys.path]!r}
from pathlib import Path
from tests.integrations.runpod_slot_pod import PodComfy,pin_object_info
from tests.integrations._pinned_source import attached_comfy
class Recorded(list):
 def __init__(self,name): self.path=Path({str(records)!r})/name
 def append(self,value):
  with self.path.open('a') as out: out.write(json.dumps(value)+'\\n')
  super().append(value)
peer=PodComfy({provider.comfy.classes!r},lambda:json.loads((Path({str(records)!r})/'inventory.json').read_text()),extra=pin_object_info())
peer.h3_output={bool(getattr(provider.comfy, 'h3_output', False))!r}
peer.prompts=Recorded('prompts');peer.views=Recorded('views');peer.rejections=Recorded('rejections')
object_info=peer.object_info
def observed_object_info():
 value=object_info()
 pending=Path({str(records)!r})/('object-info-'+str(threading.get_ident())+'.next')
 pending.write_text(json.dumps(value));pending.replace(Path({str(records)!r})/'object-info.json')
 return value
peer.object_info=observed_object_info
port={recipe.start.port!r}; Path(sys.argv[3]).write_text(str(os.getpid())+'\\n')
with attached_comfy(peer,port): threading.Event().wait()
'''
    (root / "tiny_http.py").write_text(program)
    client_engine.stop_owned(deadline=__import__("time").monotonic() + 15)
    try:
        yield records
    finally:
        client_engine.stop_owned(deadline=__import__("time").monotonic() + 15)


@pytest.fixture(scope="session")
def pinned_comfy_source(tmp_path_factory):
    return pinned_comfy_checkout(tmp_path_factory.mktemp("pinned-comfy"))


@pytest.fixture
def pinned_managed_install(session, monkeypatch, tmp_path, pinned_comfy_source):
    with pinned_cpu_install(session, monkeypatch, tmp_path, pinned_comfy_source) as installed:
        yield installed


@contextlib.contextmanager
def managed_cpu_engine(provider, folder, monkeypatch):
    """The same pinned installation for the matrix and workflow-file seams."""
    from astrid.core.generation.engine_local_server import ensure_local_engine_server
    from tests.core.generation.test_engine_custody import session

    # Keep the local fallback beside per-test folders: pytest can remove a
    # passing test's tmp_path before the next case reuses the session source.
    source = pinned_comfy_checkout(folder.parent / "pinned-comfy-session")
    native_session = session.__wrapped__(folder, monkeypatch)
    session_value = next(native_session)
    try:
        with pinned_cpu_install(session_value, monkeypatch, folder, source) as installed:
            _sparse_model_inventory(provider, installed, monkeypatch)
            with owned_cpu_peer(provider, installed, monkeypatch) as records:
                ensure_local_engine_server("comfyui")
                yield records
                read_peer_events(provider, records)
    finally:
        try:
            next(native_session)
        except StopIteration:
            pass


def _sparse_model_inventory(provider, installed, monkeypatch):
    """Retain the matrix's sparse weights and model-I/O doubles.

    Source repositories, environment/lock checks and supervisor custody stay
    real. Sparse weights establish CPU inventory, never GPU qualification.
    """
    from astrid.core.generation import entries, model_downloader, model_root
    from astrid.core.generation._engine_target_files import _TargetFiles
    from astrid.core.generation.asset_gaps import declared_model_specs
    from astrid.core.generation.managed_preparation import configured_models_root

    models_root = Path(configured_models_root(root=str(installed[3])))
    monkeypatch.setenv('VIBECOMFY_MODELS_ROOT', str(models_root))
    declared = [(row['path'].split('/models/', 1)[-1], row['bytes'],
                 row['sha256'].removeprefix('sha256:'))
                for engine in dict.fromkeys(('comfyui', *entries.runpod_engines()))
                for row in provider._receipt_for(engine)['models']]
    for entry in entries.ready_entries():
        declared += [(model.destination, model.bytes, model.sha256)
                     for model in declared_model_specs(entry.get('model_assets', []), require_complete=False)]
    sizes = {}
    for destination, size, sha in declared:
        path = models_root / destination.removeprefix('models/')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('wb') as stream:
            stream.truncate(size)
        sizes[str(path)] = size, sha
    digest, file_sha = model_downloader._digest, model_root._file_sha256
    monkeypatch.setattr(model_downloader, '_digest',
                        lambda path, **kw: sizes.get(str(path)) or digest(path, **kw))
    monkeypatch.setattr(model_root, '_file_sha256',
                        lambda path: 'sha256:' + sizes[str(path)][1] if str(path) in sizes else file_sha(path))
    # cause 744585fa6b; ruling: machines 11:35 Sat
    # Preparation independently reads weights through the descriptor-based
    # reader. Extend the same CPU weight double to that observation point;
    # source files, unsafe paths and changed placeholders remain real checks.
    facts = _TargetFiles.file_fact
    witnesses = {path: Path(path).stat() for path in sizes}

    def weight_fact(self, path, *, hash_bytes, deadline, check=None, verification=None):
        # W3 supplies verification; older file_fact implementations omit it.
        extra = {'verification': verification} if verification is not None else {}
        if hash_bytes and path in sizes and Path(path).suffix in {'.safetensors', '.bin', '.ckpt', '.pt', '.pth'}:
            fact = facts(self, path, hash_bytes=False, deadline=deadline, check=check, **extra)
            before, now = witnesses[path], Path(path).lstat()
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            if (fact.exists and fact.regular and not fact.symlink and fact.bytes == sizes[path][0]
                    and all(getattr(before, key) == getattr(now, key) for key in fields)):
                return replace(fact, sha256=sizes[path][1])
        return facts(self, path, hash_bytes=hash_bytes, deadline=deadline, check=check, **extra)

    monkeypatch.setattr(_TargetFiles, 'file_fact', weight_fact)


def read_peer_events(provider, records):
    # cause f96644329; ruling: machines 11:35 Sat
    # Observe the native child that actually served the loader menus, rather
    # than a parent-only schema cache that the execution no longer uses.
    schema = records / 'object-info.json'
    if schema.exists():
        provider.comfy.extra = {**(provider.comfy.extra or {}), **json.loads(schema.read_text())}
    for name in ("prompts", "views", "rejections"):
        path = records / name
        setattr(provider.comfy, name,
                [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else [])


def write_peer_inventory(provider, records):
    """Keep the original peer's mutable loader menus across its process seam."""
    staged = records / 'inventory.next'
    staged.write_text(json.dumps(provider.comfy.installed_files()))
    staged.replace(records / 'inventory.json')
