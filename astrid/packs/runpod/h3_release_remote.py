"""Linux provisioner, delivered with its shared modules by lifecycle transport.

The cold-target package needs only Python 3.11+ stdlib, without installed Astrid.

All writes live in the selected content-derived release directory. A volume flock
allows one writer. CPU import/metadata checks deliberately grant no GPU admission.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

from runtime_protocol import platform

from astrid.core.generation.engine_contracts import ModelSpec
from astrid.core.generation.model_downloader import _download_model


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def download_model(row, destination, repository, deadline, *, opener=None, chunk_bytes=64 << 20):
    """Adapt the old H3 row/signature onto the shared verified downloader."""
    destination = Path(destination)
    row_parts = Path(row["path"]).parts
    if len(destination.parts) >= len(row_parts) and destination.parts[-len(row_parts):] == row_parts:
        models_root = destination.parents[len(row_parts) - 1]
        relative_destination = "/".join(row_parts)
    else:
        models_root = destination.parent
        relative_destination = destination.name
    model = ModelSpec(name=str(row.get("path", Path(destination).name)),
                      repository=repository, revision=row["revision"], path=row["path"],
                      url=None, destination=relative_destination, bytes=row["bytes"],
                      sha256=row["sha256"])
    # H3's historical request deadline is wall-clock time. The shared contract
    # uses an absolute monotonic deadline, so preserve its remaining duration.
    shared_deadline = time.monotonic() + max(0.0, deadline - time.time())
    _download_model(model, models_root=str(models_root),
                    deadline=shared_deadline,
                    opener=urllib.request.urlopen if opener is None else opener,
                    range_bytes=min(chunk_bytes, 64 << 20))


def manifest_for(request, facts):
    recipe = request["recipe"]
    runtime = {
        "venv": "runtime/venv", "launcher": "runtime/launch-comfy.sh",
        "vibecomfy_source_commit": recipe["vibecomfy_source"]["revision"],
        "comfyui_commit": recipe["source"]["revision"],
        "custom_nodes_lock_sha256": recipe["custom_nodes_lock_sha256"],
        "dependency_check": "uv pip check",
        **recipe["torch"],
        "comfy_kitchen": recipe["support"]["comfy-kitchen"],
        "comfy_aimdo": recipe["support"]["comfy-aimdo"],
        "h3_custom_node_commit": recipe["custom_nodes"][0]["revision"],
        "videohelpersuite_commit": recipe["custom_nodes"][1]["revision"],
        "requirements_lock_sha256": facts["requirements_lock_sha256"],
    }
    return {
        "schema_version": 1, "release_id": request["release_id"],
        "state": "cpu_ready_gpu_qualification_pending", "gpu_qualified": False,
        "volume": request["volume"], "candidate_namespace": request["root"],
        "platform": {"python": recipe["python"], "os": "linux", "cuda": "13.0",
                     "image": recipe["image"]},
        "runtime": runtime, "models": request["lock"]["models"],
        "model_root": request["root"] + "/models",
        "recipe_sha256": request["recipe_sha256"],
    }


class Provisioner:
    def __init__(self, request):
        self.request = request
        self.recipe = request["recipe"]
        self.root = Path(request["root"])
        self.runtime = self.root / "runtime"
        self.python = self.runtime / "venv/bin/python"
        self.uv = self.runtime / "bootstrap/bin/uv"
        self.comfy = self.runtime / "ComfyUI"
        self.vibe = self.runtime / "vibecomfy"
        self.env = {k: v for k, v in os.environ.items()
                            if not k.upper().startswith(("PIP_", "UV_"))
                            and k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
        self.env.update({"UV_PYTHON_INSTALL_DIR": str(self.runtime / "python"),
                                 "UV_CACHE_DIR": str(self.runtime / "uv-cache"),
                                 "PYTHONDONTWRITEBYTECODE": "1"})
        # This provisioner establishes CPU readiness even when rented hardware
        # includes a GPU. Inference qualification belongs to the later worker.
        self.env["CUDA_VISIBLE_DEVICES"] = ""

    def run(self, argv, *, check=True):
        remaining = self.request["deadline"] - time.time()
        if remaining <= 0:
            raise TimeoutError("release build deadline exceeded")
        result = subprocess.run(list(map(str, argv)), env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=remaining, check=False)
        # Preserve diagnostics on the volume; stdout remains a small JSON response.
        with (self.runtime / "build.log").open("a") as log:
            log.write(result.stdout)
        if check and result.returncode:
            raise RuntimeError("release command failed: " + str(argv[0]) + "; see runtime/build.log")
        return result

    def sources(self):
        sources = [(self.comfy, self.recipe["source"]),
                   (self.vibe, self.recipe["vibecomfy_source"])]
        sources += [(self.comfy / "custom_nodes" / node["name"], node)
                    for node in self.recipe["custom_nodes"]]
        for path, pin in sources:
            if not (path / ".git").is_dir():
                path.mkdir(parents=True, exist_ok=True)
                self.run(["git", "init", path])
            origin = self.run(["git", "-C", path, "remote", "get-url", "origin"], check=False)
            if origin.returncode:
                self.run(["git", "-C", path, "remote", "add", "origin", pin["repository"]])
            elif origin.stdout.strip() != pin["repository"]:
                raise RuntimeError("release source origin differs from pin")
            current = self.run(["git", "-C", path, "rev-parse", "HEAD"], check=False)
            if current.returncode or current.stdout.strip() != pin["revision"]:
                self.run(["git", "-C", path, "fetch", "--depth=1", "origin", pin["revision"]])
                self.run(["git", "-C", path, "checkout", "--detach", pin["revision"]])
            if self.run(["git", "-C", path, "status", "--porcelain", "--untracked-files=all"]).stdout.strip():
                raise RuntimeError("release source has uncommitted content: " + str(path))
        lock = self.vibe / "custom_nodes.lock"
        if digest(lock) != self.recipe["custom_nodes_lock_sha256"]:
            raise RuntimeError("pinned custom_nodes.lock digest mismatch")
        entries = tomllib.loads(lock.read_text())["nodepacks"]
        for node in self.recipe["custom_nodes"]:
            if entries[node["name"]]["commit"] != node["revision"]:
                raise RuntimeError("custom-node source and lock disagree")

    def runtime_probe(self):
        receipt = self.runtime / "environment-receipt.json"
        lock = self.runtime / "requirements.lock"
        if not receipt.is_file() or not lock.is_file():
            return False
        facts = json.loads(receipt.read_text())
        if facts.get("recipe_sha256") != self.request["recipe_sha256"] or facts.get("requirements_lock_sha256") != digest(lock):
            return False
        return self.assert_runtime(check=False).returncode == 0

    def assert_runtime(self, *, check=True):
        expected = {**self.recipe["torch"], **self.recipe["support"]}
        program = '''import sys,json,importlib.metadata as m
import torch,torchvision,torchaudio,cv2
assert sys.version.split()[0] == sys.argv[2]
expected=json.loads(sys.argv[1])
assert {n:m.version(n) for n in expected} == expected
assert {n:mod.__version__ for n,mod in [('torch',torch),('torchvision',torchvision),('torchaudio',torchaudio)]} == {n:expected[n] for n in ['torch','torchvision','torchaudio']}
assert torch.version.cuda == '13.0'
assert not torch.cuda.is_initialized()
'''
        result = self.run([self.python, "-I", "-c", program, json.dumps(expected), self.recipe["python"]], check=check)
        if result.returncode:
            return result
        return self.run([self.uv, "--no-config", "pip", "check", "--python", self.python], check=check)

    def environment(self):
        if not self.uv.is_file():
            self.run(["python3", "-m", "pip", "--isolated", "install", "--index-url",
                      "https://pypi.org/simple", "--ignore-installed", "--prefix",
                      self.runtime / "bootstrap", "uv==" + self.recipe["uv"]])
        if self.run([self.uv, "--version"]).stdout.split()[:2] != ["uv", self.recipe["uv"]]:
            raise RuntimeError("uv bootstrap version differs")
        if not self.python.is_file():
            self.run([self.uv, "--no-config", "venv", "--allow-existing", "--python", self.recipe["python"],
                      "--managed-python", self.runtime / "venv"])
        if self.runtime_probe():
            return
        lock = self.runtime / "requirements.lock"
        if not lock.is_file():
            build = tomllib.loads((self.vibe / "pyproject.toml").read_text())["build-system"]["requires"]
            inputs = self.runtime / "release-requirements.in"
            inputs.write_text("\n".join([
                *(f"{name}=={version}" for name, version in self.recipe["support"].items()),
                *(f"{name} @ {url}" for name, url in self.recipe["torch_wheels"].items()),
                *build]) + "\n")
            temporary = lock.with_suffix(".tmp")
            requirements = [self.comfy / "requirements.txt", self.vibe / "pyproject.toml", inputs]
            requirements += [self.comfy / "custom_nodes" / node["name"] / "requirements.txt"
                             for node in self.recipe["custom_nodes"]
                             if (self.comfy / "custom_nodes" / node["name"] / "requirements.txt").is_file()]
            self.run([self.uv, "--no-config", "pip", "compile", "--python", self.python,
                      "--no-sources", "--generate-hashes", "--only-binary", ":all:",
                      "--exclude-newer", self.recipe["exclude_newer"],
                      "--index-url", "https://pypi.org/simple", "-o", temporary, *requirements])
            temporary.replace(lock)
        # Resolve torch's dependencies in the full lock, install its three wheels last.
        support = self.runtime / "support.lock"
        lines, skip = [], False
        for line in lock.read_text().splitlines(keepends=True):
            if line.strip() and not line[0].isspace() and not line.startswith("#"):
                skip = bool(re.match(r"^(torch|torchvision|torchaudio)(?:[ @=<>])", line))
            if not skip:
                lines.append(line)
        support.write_text("".join(lines))
        self.run([self.uv, "--no-config", "pip", "sync", "--python", self.python,
                  "--index-url", "https://pypi.org/simple", "--require-hashes", support])
        self.run([self.uv, "--no-config", "pip", "install", "--python", self.python,
                  "--no-deps", "--no-build-isolation", self.vibe])
        self.run([self.uv, "--no-config", "pip", "install", "--python", self.python,
                  "--force-reinstall", "--no-deps", *self.recipe["torch_wheels"].values()])
        self.assert_runtime()
        atomic_json(self.runtime / "environment-receipt.json", {
            "recipe_sha256": self.request["recipe_sha256"],
            "requirements_lock_sha256": digest(lock), "gpu_qualified": False})

    def launch(self):
        configuration = self.runtime / "extra_model_paths.yaml"
        text = "h3_release:\n  base_path: " + json.dumps(str(self.root / "models")) + "\n"
        text += "".join(f"  {name}: {name}\n" for name in ("diffusion_models", "text_encoders", "vae", "loras"))
        configuration.write_text(text)
        launcher = self.runtime / "launch-comfy.sh"
        script = '''#!/bin/sh
set -eu
RUNTIME=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export VIBECOMFY_HEADLESS=1
cd "$RUNTIME/ComfyUI"
exec "$RUNTIME/venv/bin/python" main.py --listen 0.0.0.0 --port 8188 --extra-model-paths-config "$RUNTIME/extra_model_paths.yaml" "$@"
'''
        if not launcher.is_file() or launcher.read_text() != script:
            launcher.write_text(script)
            launcher.chmod(0o755)
        self.run(["sh", "-n", launcher])

    def build(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.sources()
        self.environment()
        self.launch()
        self.run([self.python, "-I", "-c",
                  "import ast,sys; from pathlib import Path; "
                  "[ast.parse(p.read_bytes(),filename=str(p)) for root in sys.argv[1:] "
                  "for p in Path(root).rglob('*.py') if '.git' not in p.parts]",
                  self.comfy, self.vibe / "vibecomfy"])
        for row in self.request["lock"]["models"]:
            download_model(row, self.root / "models" / row["path"],
                           self.request["lock"]["repository"], self.request["deadline"])
        facts = json.loads((self.runtime / "environment-receipt.json").read_text())
        manifest = manifest_for(self.request, facts)
        manifest_path = self.root / "release-manifest.json"
        if manifest_path.is_file() and json.loads(manifest_path.read_text()) == manifest:
            receipt = self.root / "build-receipt.json"
            inventory = self.root / "model-inventory.json"
            model_lock = self.root / "models.lock.json"
            if (receipt.is_file() and inventory.is_file() and model_lock.is_file()
                    and json.loads(inventory.read_text()) == {"models": manifest["models"], "verified": True}
                    and json.loads(model_lock.read_text()) == self.request["lock"]):
                previous = json.loads(receipt.read_text())
                if previous.get("manifest_sha256") == digest(manifest_path):
                    atomic_json(self.root / "READY", {"manifest_sha256": previous["manifest_sha256"]})
                    return previous
        atomic_json(self.root / "models.lock.json", self.request["lock"])
        atomic_json(self.root / "model-inventory.json", {"models": manifest["models"], "verified": True})
        atomic_json(manifest_path, manifest)
        result = {"manifest": manifest, "manifest_sha256": digest(manifest_path),
                  "release_root": str(self.root), "release_id": self.request["release_id"],
                  "gpu_qualified": False, "verified_at": time.time()}
        atomic_json(self.root / "build-receipt.json", result)
        # No remote release writes may follow this commit marker.
        atomic_json(self.root / "READY", {"manifest_sha256": result["manifest_sha256"]})
        return result


def provision(request):
    if sys.platform != "linux":
        raise RuntimeError("H3 provisioner requires Linux")
    root = Path(request["root"])
    mount = Path(request["volume"]["mount"])
    if not root.is_relative_to(mount) or root == mount or ".." in root.parts:
        raise RuntimeError("release root is outside volume")
    if not os.path.ismount(mount):
        raise RuntimeError("network-volume mount is missing")
    root.mkdir(parents=True, exist_ok=True)
    if root.resolve() != root:
        raise RuntimeError("release root traverses a symlink")
    with (root / ".build.lock").open("a") as lock:
        platform.lock_file(lock.fileno(), blocking=False)
        # Stale READY cannot survive a failed revalidation/repair.
        (root / "READY").unlink(missing_ok=True)
        return Provisioner(request).build()
