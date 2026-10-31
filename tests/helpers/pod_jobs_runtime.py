"""CPU model/action boundary for pod-job orchestration tests.

The lifecycle AgentServer, HTTPS transport, detached jobs, uploads, downloads,
hashes and media probes are real. Pod allocation, mounted storage observations,
Runtime readiness, installation/GPU checks and the H3 model/action boundary are
fixtures. The fake ComfyUI follows the
prompt/history/view pattern in h3_cpu_runtime._SERVER_CODE; this is runner
acceptance, not acceptance of the H3 transform implementation.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
import shlex
import struct
import subprocess
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from runpod_lifecycle.https import HttpsPodTransport
from runpod_lifecycle.pod_agent import AgentServer


def png_bytes(width=32, height=24):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    pixels = b"".join(b"\0" + b"\x38\x86\xc9" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


_ACTION_CODE = r'''
import hashlib, json, os, pathlib, shutil, sys, urllib.request
args = sys.argv[1:]
log = pathlib.Path(os.environ["POD_JOBS_SETUP_LOG"])
def record(value):
    with log.open("a") as stream: stream.write(value + "\n")
def option(name): return args[args.index(name) + 1]
if args[:2] == ["engines", "install"]:
    record("install")
    assert "--model-cache-root" in args
    assert pathlib.Path(option("--root")) not in pathlib.Path(option("--model-cache-root")).parents
    # Installation/GPU remain fixtures; model the engine hook's venv contract.
    primary_venv = pathlib.Path(option("--root")) / "runtime/venv"
    primary_venv.mkdir(parents=True, exist_ok=True)
    if os.environ.get("ASTRID_ENGINE_SANITY_CMD"):
        import shlex, subprocess
        subprocess.run(shlex.split(os.environ["ASTRID_ENGINE_SANITY_CMD"]) + ["--venv", str(primary_venv)], check=True)
    record("models")
    print(json.dumps({"status":"installed", "steps":[{"id":"cpu-fixture", "action":"skipped"}]}))
elif args[:2] == ["engines", "status"]:
    record("status")
    print(json.dumps({"status":"installed", "health":{"status":"healthy"}}))
elif args[:2] == ["action", "run"]:
    request = json.loads(pathlib.Path(option("--request")).read_text())
    assets = json.loads(pathlib.Path(option("--asset-map")).read_text())
    assert assets and all(pathlib.Path(value).is_file() for value in assets.values())
    seconds = request["output"]["duration"]
    record("h3:" + str(seconds))
    prompt = {"1":{"class_type":"VHS_VideoCombine", "inputs":{"save_output":True, "seconds":seconds}}}
    base = os.environ["POD_JOBS_COMFY_URL"]
    req = urllib.request.Request(base + "/prompt", json.dumps({"prompt":prompt}).encode(), {"Content-Type":"application/json"})
    result = json.load(urllib.request.urlopen(req))
    prompt_id = result["prompt_id"]
    history = json.load(urllib.request.urlopen(base + "/history/" + prompt_id))[prompt_id]
    assert history["status"]["completed"] is True
    filename = history["outputs"]["1"]["gifs"][0]["filename"]
    video = urllib.request.urlopen(base + "/view?filename=" + filename + "&type=output").read()
    out = pathlib.Path(option("--out"))
    final = out / "07-finalizer/retrieved/verified-candidate.media"
    final.parent.mkdir(parents=True)
    final.write_bytes(video)
    receipt = out / "08-final-receipt.json"
    receipt.write_text(json.dumps({"states":{"final_composition_publication":{"status":"verified", "evidence":{"publication":{"object_id":"sha256:" + hashlib.sha256(video).hexdigest()}}}}, "publication_contract":{"final_composition_publication":"verified"}}))
    manifest = out / "05-compose/retrieved/composition-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"fixture":"fake-comfy-cpu", "prompt":request["content"]["prompt"], "duration":seconds}))
    print(json.dumps({"ok":True, "out":str(out), "receipt":str(receipt), "manifest":str(manifest)}))
else:
    raise SystemExit("unexpected fixture action " + repr(args))
'''


class LoopbackPod:
    def __init__(self, root, module, monkeypatch, *, sanity_fail=False, runtime_fail=False, corrupt_download=None,
                 volume_id="fixture-volume"):
        self.root, self.module = Path(root), module
        self.volume_id = volume_id
        self.events, self.requests, self.downloads = [], [], []
        self.terminated = False
        self.corrupt_download = corrupt_download
        self.agent = None
        self.agent_thread = None
        self.transport = None
        self.comfy_histories, self.comfy_prompts = {}, []
        self.remote = self.root / "remote"
        self.remote.mkdir(parents=True)
        self.bin = self.root / "fixture-bin"
        self.bin.mkdir()
        self.setup_log = self.root / "setup.log"
        self.media_root = self.root / "comfy-output"
        self.media_root.mkdir()
        owner = self

        class ComfyHandler(BaseHTTPRequestHandler):
            def reply(self, value):
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                assert self.path == "/prompt"
                prompt = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.comfy_prompts.append(prompt)
                node = prompt["prompt"]["1"]
                assert node["class_type"] == "VHS_VideoCombine" and node["inputs"]["save_output"] is True
                seconds = node["inputs"]["seconds"]
                prompt_id = f"pod-jobs-cpu-{len(owner.comfy_prompts)}"
                filename = prompt_id + ".mp4"
                # Deterministic synthetic inference, as in h3_cpu_runtime.
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                                f"color=c=blue:s=1024x576:r=24:d={seconds}", "-f", "lavfi", "-i",
                                f"sine=frequency=880:sample_rate=48000:duration={seconds}",
                                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                                "-c:a", "aac", "-shortest", str(owner.media_root / filename)],
                               check=True, capture_output=True, timeout=45)
                owner.comfy_histories[prompt_id] = {"status":{"completed":True, "status_str":"success"},
                    "outputs":{"1":{"gifs":[{"filename":filename, "subfolder":"", "type":"output"}]}}}
                self.reply({"prompt_id":prompt_id, "node_errors":{}})

            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path.startswith("/history/"):
                    prompt_id = parsed.path.removeprefix("/history/")
                    return self.reply({prompt_id:owner.comfy_histories[prompt_id]})
                if parsed.path == "/view":
                    filename = parse_qs(parsed.query)["filename"][0]
                    assert filename == Path(filename).name
                    body = (owner.media_root / filename).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

            def log_message(self, *_): pass

        self.comfy = ThreadingHTTPServer(("127.0.0.1", 0), ComfyHandler)
        self.comfy_thread = threading.Thread(target=self.comfy.serve_forever, daemon=True)
        self.comfy_thread.start()
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("PATH", str(self.bin) + os.pathsep + os.environ["PATH"])
        monkeypatch.setenv("POD_JOBS_COMFY_URL", f"http://127.0.0.1:{self.comfy.server_port}")
        monkeypatch.setenv("POD_JOBS_SETUP_LOG", str(self.setup_log))
        # The real detached launcher uses bare python3. A host version-manager
        # shim can prepend its own bin and hide our astrid/stat fixtures from
        # the supervisor. Bind it to this test interpreter without changing the
        # production transport or depending on the host's Python/CLI setup.
        python = self.bin / "python3"
        python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        python.chmod(0o755)
        action = self.bin / "astrid"
        action.write_text(f"#!{sys.executable}\n" + _ACTION_CODE)
        action.chmod(0o755)
        # macOS's stat lacks Linux's -c; preserve the pod command's contract.
        stat = self.bin / "stat"
        stat.write_text(f"#!{sys.executable}\nimport os,sys\nassert sys.argv[1:3] == ['-c','%s']\nprint(os.stat(sys.argv[3]).st_size)\n")
        stat.chmod(0o755)
        for key, value in {
            "BUNDLE_DIR": self.remote / "bundle", "BUNDLE_REMOTE": self.remote / "bundle/pod-src.tgz",
            "SRC_DIR": self.remote / "bundle/src", "CLIP_DIR": self.remote / "clip",
            "JOB_ROOT": self.remote / "detached", "PROGRESS_REMOTE": self.remote / "progress.json",
            "ENGINE_ROOT": self.remote / "engine", "CACHE_MOUNT": self.remote / "cache-mount",
            "MODEL_CACHE_ROOT": self.remote / "cache-mount/models",
        }.items():
            monkeypatch.setattr(module, key, str(value))
        self.agent_root = self.root / "source-agent"
        scripts = self.agent_root / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "pod_bootstrap.sh").write_text('set -eu\nprintf "bootstrap\\n" >> "$POD_JOBS_SETUP_LOG"\n')
        (scripts / "pod_runtime_ready.py").write_text(
            'import base64,json,os,pathlib,sys\n'
            'request = json.loads(base64.b64decode(sys.argv[1]))\n'
            'with pathlib.Path(os.environ["POD_JOBS_SETUP_LOG"]).open("a") as log: log.write("runtime-ready\\n")\n'
            + f'print(json.dumps({{**request, "ready": {not runtime_fail!r}, "stopped": True}}))\n')
        (scripts / "gpu_sanity.py").write_text(
            'import argparse,os,pathlib,sys\n'
            'parser = argparse.ArgumentParser()\nparser.add_argument("--venv", required=True)\n'
            'venv = pathlib.Path(parser.parse_args().venv)\nassert venv.is_dir() and venv.parts[-2:] == ("runtime", "venv")\n'
            'with pathlib.Path(os.environ["POD_JOBS_SETUP_LOG"]).open("a") as log: log.write("sanity\\n")\n'
            + f'sys.exit({2 if sanity_fail else 0})\n')
        monkeypatch.setattr(module, "SANITY_CMD", f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(module.SRC_DIR) / 'astrid-agent/scripts/gpu_sanity.py'))}")
        self.sources = self.root / "sources"
        for name in module.BUNDLE_TREES[1:]:
            source = self.sources / name
            source.mkdir(parents=True)
            (source / "fixture.txt").write_text(name)

    def cli(self, argv, env):
        operation = argv[0]
        self.events.append(operation)
        if operation == "launch": return 0, json.dumps({"pod_id":"fakepod134"}), ""
        if operation == "mounts":
            nonce = argv[argv.index("--nonce") + 1]
            return 0, json.dumps({"schema":"runpod.provider-mounts.v1", "pod_id":"fakepod134", "nonce":nonce,
                "volume_mounts":[{"volume_id":self.volume_id, "volume_type":"NETWORK_VOLUME", "mount_path":self.module.CACHE_MOUNT,"read_only":False}],
                "legacy_evidence":{"present":False,"valid":True,"conflict":False}}), ""
        if operation == "terminate":
            assert argv[1] == "fakepod134"
            self.terminated = True
            return 0, "{}", ""
        if operation == "volumes":
            assert argv == ["volumes", "ls", "--json"]
            return 0, json.dumps([{"id":self.volume_id, "name":self.module.STORAGE_NAME,
                                   "dataCenterId":self.module.DATACENTER}]), ""
        if operation == "list":
            return 0, "[]" if self.terminated else json.dumps([{"id":"fakepod134", "network_volume_id":self.volume_id, "network_volume_evidence": {"present":True,"valid":True,"conflict":False}}]), ""
        raise AssertionError(argv)

    def http_status(self, pod_id):
        assert pod_id == "fakepod134"
        self.events.append("release-get")
        return 404 if self.terminated else 200

    def factory(self, pod_id, token):
        owner = self
        self.agent = AgentServer(("127.0.0.1", 0), token, self.root / "agent-state")
        self.agent_thread = threading.Thread(target=self.agent.serve_forever, daemon=True)
        self.agent_thread.start()

        class LocalTransport(HttpsPodTransport):
            def _request(self, method, path, **kwargs):
                owner.requests.append((method, path))
                return super()._request(method, path, **kwargs)

            def detached_poll(self, job_id, *, root):
                # These finite CPU fixture commands must finish publishing state
                # before acceptance polls it. Replacing an opened status inode
                # can change its ctime and correctly trigger the agent's HTTP 412
                # integrity guard. Keep the real HTTPS poll and every error from
                # it; this barrier only removes fixture scheduling from the test.
                directory = Path(root) / job_id
                deadline = time.monotonic() + 60
                published = threading.Event()
                while True:
                    state = (directory / "status").read_text().strip()
                    if state == "cancelled" or (state == "exited" and (directory / "exit").is_file()):
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("loopback supervisor did not publish terminal status")
                    published.wait(.01)
                return super().detached_poll(job_id, root=root)

            def execute_command(self, command, **kwargs):
                parts = shlex.split(command)
                if len(parts) == 4 and parts[:2] == ["python3", "-c"] and "def storage_probe(" in parts[2]:
                    request = json.loads(base64.b64decode(parts[3]))
                    assert request["pod_id"] == pod_id and request["volume_id"] == owner.volume_id
                    assert request["mount"] == owner.module.CACHE_MOUNT
                    assert request["model_cache_root"] == owner.module.MODEL_CACHE_ROOT
                    assert request["engine_root"] == owner.module.ENGINE_ROOT
                    owner.events.append("storage-probe")
                    receipt = {**{key:value for key,value in request.items() if key != "provider_mapping"}, "cache_device":47,"cache_mount_id":102, "free_bytes":request["required_volume_bytes"] + 1,
                               "total_bytes":request["required_volume_bytes"] + 2,
                               "local_free_bytes":request["required_local_bytes"] + 1}
                    # Preserve real transport/AgentServer execution and budget
                    # accounting; only machine-specific mount facts are fake.
                    source = ('import os,pathlib\n'
                              'with pathlib.Path(os.environ["POD_JOBS_SETUP_LOG"]).open("a") as log: log.write("storage\\n")\n'
                              + f'print({json.dumps(receipt)!r})\n')
                    command = "python3 -c " + shlex.quote(source)
                return super().execute_command(command, **kwargs)

            def get(self, remote, local, **kwargs):
                owner.downloads.append(remote)
                result = super().get(remote, local, **kwargs)
                if owner.corrupt_download and str(remote).endswith(owner.corrupt_download):
                    Path(local).write_bytes(Path(local).read_bytes() + b"corrupted-after-transport")
                return result

        self.transport = LocalTransport(pod_id, token, base_url=f"http://127.0.0.1:{self.agent.server_port}",
                                        request_timeout=10, poll_interval=.01)
        return self.transport

    def close(self):
        if self.agent is not None:
            self.agent.shutdown()
            self.agent.server_close()
            self.agent_thread.join(2)
        self.comfy.shutdown()
        self.comfy.server_close()
        self.comfy_thread.join(2)

    @property
    def record_count(self):
        return len(self.agent.jobs) if self.agent else 0
