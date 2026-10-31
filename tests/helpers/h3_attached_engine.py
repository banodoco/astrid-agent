"""Run-path rehearsal support: an "installed" local engine attached to the fake ComfyUI.

TEST-ONLY.  The product engine gate is untouched: ``h3_av.transform --placement local`` still asks
``engine_local_endpoint.local_engine_endpoint`` for an installed engine, and that still asks
``engine_runner.engine_status``.  The real status reads the target through a custody supervisor, git
checkouts, environment locks and model files, none of which exist on this host (the install itself is
proven on a pod).  This module supplies the one thing the rehearsal replaces: the ``engine_status`` answer.

* The *record* is a genuine ``InstallReceipt`` (the installer's own record type), written as JSON to the
  engine's ``.astrid-engine/install.json``; ``local_engine_endpoint`` hashes that file into the attestation.
* The *status* is ``engine_status`` re-pointed, inside the pack-host process only, at that record.  The
  pack host is a separate process, so it is reached through a ``.pth`` file the test puts in (and removes
  from) the test interpreter's site-packages; the hook is answers only for an engine root carrying the rehearsal marker file.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import sysconfig
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path

MARKER = "rehearsal-attached-engine.marker"
ENGINE, PLATFORM = "vibecomfy-h3", "linux-x86_64-cu130"
OBJECT_INFO = Path(__file__).resolve().parents[1] / "fixtures" / "h3_cpu" / "object_info_h3_runpod.json"
PTH_NAME = "astrid_rehearsal_attached_engine.pth"
_HELPERS = Path(__file__).resolve().parent


# --- host side: runs inside the pack-host process -----------------------------------------------------------

def _receipt_from_record(path: Path):
    from astrid.core.generation.engine_contracts import DownloadResult, HealthEvidence, InstallReceipt

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["health"] = HealthEvidence(**raw["health"])
    raw["models"] = tuple(DownloadResult(**item) for item in raw["models"])
    raw["steps"] = tuple(raw["steps"])
    return InstallReceipt(**raw)


def _attached_engine_status(original):
    def engine_status(engine, *, platform, placement, root, context, deadline):
        # Only an engine root the rehearsal itself marked is answered here; anything else is the real status.
        marker = Path(root) / ".astrid-engine" / MARKER
        if not marker.is_file():
            return original(engine, platform=platform, placement=placement, root=root,
                            context=context, deadline=deadline)
        return _receipt_from_record(Path(root) / ".astrid-engine" / "install.json")
    return engine_status


def activate() -> None:
    """Called from the .pth line in every interpreter of the test environment (the .pth exists only while
    the rehearsal runs); the answer is given only for an engine root carrying the rehearsal MARKER."""
    import importlib.abc
    import importlib.machinery

    target = "astrid.core.generation.engine_runner"

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target_module=None):
            if name != target:
                return None
            sys.meta_path.remove(self)
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec is None or spec.loader is None:
                return spec
            original = spec.loader.exec_module

            def exec_module(module, _original=original):
                _original(module)
                module.engine_status = _attached_engine_status(module.engine_status)

            spec.loader.exec_module = exec_module
            return spec

    sys.meta_path.insert(0, _Finder())


# --- test side -----------------------------------------------------------------------------------------------

# The CPU runtime's fake ComfyUI answers /prompt, /object_info and /view.  The local placement also uploads
# the reference images (POST /upload/image) and reads GET /queue when it cancels; the attached engine adds those
# two routes to the same server code, and nothing else.
_UPLOAD_ROUTE = """        if self.path == "/upload/image":
            from email import message_from_bytes
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            message = message_from_bytes(b"Content-Type: " + self.headers["Content-Type"].encode() + b"\\r\\n\\r\\n" + raw)
            fields, upload = {}, None
            for part in message.get_payload():
                name = part.get_param("name", header="content-disposition")
                if part.get_filename():
                    upload = (part.get_filename(), part.get_payload(decode=True))
                else:
                    fields[name] = part.get_payload(decode=True).decode()
            if upload is None or fields.get("type") != "input":
                self.respond(400, {"error": "upload fixture: image and type=input are required"})
                return
            subfolder = fields.get("subfolder", "")
            target = input_directory / subfolder / upload[0]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(upload[1])
            self.respond(200, {"name": upload[0], "subfolder": subfolder, "type": "input"})
            return
"""
_QUEUE_ROUTE = """        elif parsed.path == "/queue" and not parsed.query:
            payload = {"queue_running": [], "queue_pending": []}
"""


def server_code() -> str:
    from tests.helpers import h3_cpu_runtime as helper

    post_anchor = '        if self.path != "/prompt":\n'
    get_anchor = '        elif parsed.path.startswith("/history/") and not parsed.query:\n'
    code = helper._SERVER_CODE
    assert code.count(post_anchor) == 1 and code.count(get_anchor) == 1, "fake ComfyUI server code changed"
    return code.replace(post_anchor, _UPLOAD_ROUTE + post_anchor).replace(get_anchor, _QUEUE_ROUTE + get_anchor)


def install_pth() -> Path:
    path = Path(sysconfig.get_paths()["purelib"]) / PTH_NAME
    path.write_text(
        f"import sys; sys.path.insert(0, {str(_HELPERS.parent.parent)!r}); "
        f"import tests.helpers.h3_attached_engine as _m; _m.activate()\n", encoding="utf-8")
    return path


class AttachedEngine:
    """An installed-engine record under ``$HOME/.astrid-data/engines/vibecomfy-h3`` plus a fake ComfyUI."""

    def __init__(self, home: Path, *, generated_seconds: float) -> None:
        from astrid.core.generation.engine_contracts import DownloadResult, HealthEvidence, InstallReceipt
        from astrid.core.generation.engine_local_endpoint import default_engine_root
        from astrid.core.generation.engine_recipe import load_model_lock, load_recipe, recipe_digest
        from tests.helpers import h3_cpu_runtime as helper

        self._home = home
        previous = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            self.root = Path(default_engine_root(ENGINE))
        finally:
            if previous is None:
                os.environ.pop("HOME")
            else:
                os.environ["HOME"] = previous
        recipe = load_recipe(ENGINE, PLATFORM)
        self.port = recipe.start.port
        comfy = next(s.relative_path for s in recipe.sources if s.name == "comfyui")
        self.output_dir = self.root / comfy / "output"
        self.input_dir = self.root / comfy / "input"
        for path in (self.output_dir, self.input_dir, self.root / ".astrid-engine"):
            path.mkdir(parents=True)
        # The fake ComfyUI answers /object_info with the complete schema the H3 graph needs: the pinned
        # VibeComfy corpus plus the captured H3 rows, composed exactly as the CPU runtime helper composes it.
        import vibecomfy

        schema = helper._object_info_from_checkout(Path(vibecomfy.__file__).resolve().parents[1])
        self._list_installed_models(schema, load_model_lock(recipe.models_lock).models)
        scratch = home.parent / "fake-comfy"
        scratch.mkdir()
        self.object_info = scratch / "object_info.json"
        self.object_info.write_text(json.dumps(schema, sort_keys=True), encoding="utf-8")
        object_info_sha = hashlib.sha256(self.object_info.read_bytes()).hexdigest()
        # The installer's model step leaves one verified file per locked model under <root>/models.  The
        # rehearsal writes a few bytes per file (the locked files are tens of gigabytes) and records the
        # sha256 of those bytes, so the receipt and the files agree exactly as a real install's do.
        models = []
        for locked in load_model_lock(recipe.models_lock).models:
            target = self.root / "models" / locked.destination
            target.parent.mkdir(parents=True, exist_ok=True)
            data = f"rehearsal model bytes: {locked.name}\n".encode()
            target.write_bytes(data)
            models.append(DownloadResult(locked.name, str(target), "downloaded", len(data),
                                         hashlib.sha256(data).hexdigest(), True, 0))
        now = "2026-10-28T00:00:00Z"
        receipt = InstallReceipt(
            "astrid.engine-install.v1", "rehearsal-attached-engine", ENGINE, PLATFORM, "local", str(self.root),
            "installed", False, recipe_digest(recipe), "0" * 64, {}, {}, tuple(models), (),
            HealthEvidence("healthy", now, object_info_sha, True), now, now, None)
        (self.root / ".astrid-engine" / MARKER).write_text("run-path rehearsal (attached test engine)\n")
        (self.root / ".astrid-engine" / "install.json").write_text(
            json.dumps(asdict(receipt), sort_keys=True), encoding="utf-8")
        (scratch / "config.json").write_text(json.dumps(
            {"input_directory": str(self.input_dir), "output_directory": str(self.output_dir)}), encoding="utf-8")
        (scratch / "expected.json").write_text(json.dumps({"generated_seconds": generated_seconds}), encoding="utf-8")
        self.evidence = scratch / "evidence.json"
        self.url = f"http://127.0.0.1:{self.port}"
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                raise RuntimeError(f"port {self.port} is already in use; the rehearsal needs it for the fake ComfyUI")
        self.process = subprocess.Popen(
            [sys.executable, "-c", server_code(), str(self.port), str(self.object_info),
             str(scratch / "config.json"), str(scratch / "expected.json"), str(self.evidence)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while True:
            try:
                urllib.request.urlopen(self.url + "/object_info", timeout=5).read()
                return
            except OSError:
                if time.monotonic() > deadline or self.process.poll() is not None:
                    self.close()
                    raise RuntimeError("fake ComfyUI did not start")
                time.sleep(0.05)

    # A running ComfyUI lists the files in its model folders as the choices of its loader nodes; the fake
    # lists exactly the files the install put there, so the workflow's model preflight finds them.
    _LOADERS = {"diffusion_models": ("UNETLoader", "unet_name"), "text_encoders": ("CLIPLoader", "clip_name"),
                "vae": ("VAELoader", "vae_name"), "loras": ("LoraLoaderModelOnly", "lora_name")}

    @classmethod
    def _list_installed_models(cls, schema: dict, models) -> None:
        for locked in models:
            folder, _, filename = locked.destination.rpartition("/")
            class_type, field = cls._LOADERS[folder]
            choices = schema[class_type]["input"]["required"][field][0]
            if filename not in choices:
                choices.append(filename)

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
