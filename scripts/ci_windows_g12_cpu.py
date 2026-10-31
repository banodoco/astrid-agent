"""Installed Windows CPU journey for G-12; evidence is isolated per workflow run."""

from __future__ import annotations

import argparse
import functools
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "G12-windows-cpu"
TARGET = "http://127.0.0.1:8188"
HEALTH_URL = "http://127.0.0.1:17340/api/runtime/v1/health"
RUNTIME_SHA = "8f3d1091ada773af63f730f6340226a40e86ecc4"
VIBECOMFY_SHA = "3bebae39655eb460454885040be4d3aefaf2ef31"
COMFYUI_SHA = "0dbfd493b4cc604db743c699f0f7ce01348e54a4"
TASK_ID_FILE = EVIDENCE / "task-id.json"
DISCOVERY_JSON = EVIDENCE / "generate-list.json"


def save(name: str, value: object) -> Path:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / name
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _tail(path: Path, limit: int = 6000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return "(log unavailable)"


def run(argv: list[str | Path], name: str, *, env=None, timeout=300, show=True) -> str:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    log = EVIDENCE / name
    print(f"Running {name}", flush=True)
    try:
        with log.open("w", encoding="utf-8") as output:
            result = subprocess.run([str(arg) for arg in argv], env=env, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{name}: timed out after {timeout}s; see {log}") from exc
    if show:
        print(_tail(log), flush=True)
    if result.returncode:
        raise RuntimeError(f"{name}: exit {result.returncode}; see {log}\n{_tail(log)}")
    return log.read_text(encoding="utf-8-sig", errors="replace")


def _git_source(repo: Path, expected: str | None = None) -> dict[str, str]:
    try:
        sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                      text=True, stderr=subprocess.STDOUT, timeout=15).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"source checkout identity failed for {repo}: {exc}") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError(f"source checkout did not resolve to a full commit SHA: {repo}")
    if expected and sha != expected:
        raise RuntimeError(f"source checkout mismatch for {repo}: expected {expected}, got {sha}")
    return {"sha": sha, "repo": str(repo.resolve())}


class _ManifestHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, document: bytes, **kwargs):
        self.document = document
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path != "/install-manifest.json":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.document)))
        self.end_headers()
        self.wfile.write(self.document)

    def log_message(self, *_args):
        pass


def install(installer_shell: str) -> None:
    runtime = ROOT / "journey-runtime"
    vibecomfy = ROOT / "journey-vibecomfy"
    app = ROOT / "journey-app"
    manifest_path = app / "public" / "install-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"install: cannot read official App install manifest {manifest_path}: {exc}") from exc
    manifest.update(
        agent=_git_source(ROOT),
        runtime=_git_source(runtime, RUNTIME_SHA),
        vibecomfy=_git_source(vibecomfy, VIBECOMFY_SHA),
    )
    document = json.dumps(manifest, sort_keys=True).encode("utf-8")
    save("install-manifest.json", manifest)
    save("sources.json", {name: manifest[name] for name in ("agent", "runtime", "vibecomfy")})
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_ManifestHandler, document=document))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = dict(os.environ)
    env["ASTRID_INSTALL_MANIFEST_URL"] = f"http://127.0.0.1:{server.server_port}/install-manifest.json"
    env["BROWSER"] = f'"{sys.executable}" -c "pass" %s'
    try:
        run([installer_shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", app / "public" / "install.ps1"], "install.log", env=env, timeout=3600)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    installed = version()
    actual_path = Path(installed["root"]) / "manifest.json"
    try:
        actual = json.loads(actual_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"install: installed manifest missing or invalid at {actual_path}: {exc}") from exc
    for source in ("agent", "runtime", "vibecomfy"):
        if actual.get(source) != manifest[source]:
            raise RuntimeError(f"install: installed {source} source differs from selected tuple; see install-manifest.json and install.log")
    save("installed-sources.json", {name: actual[name] for name in ("agent", "runtime", "vibecomfy")})


def program_root() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "Astrid"


def data_root() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "Astrid Runtime"


def version() -> dict:
    current_path = program_root() / "current"
    if not current_path.is_file():
        raise RuntimeError("installed Astrid current version pointer is missing")
    current = current_path.read_text(encoding="utf-8").strip()
    version_path = program_root() / "versions" / current / "version.json"
    try:
        value = json.loads(version_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"installed Astrid version metadata is unavailable: {exc}") from exc
    save("installed-version.json", value)
    return value


def cli(argv: list[str], output_name: str, *, timeout=300) -> dict:
    installed = version()
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(installed["env"])
    output_path = EVIDENCE / output_name
    stderr_path = EVIDENCE / (Path(output_name).stem + ".stderr.log")
    command = [installed["python"], "-I", "-m", "astrid", *argv]
    print("Running installed Astrid CLI: " + " ".join(argv), flush=True)
    try:
        with output_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            result = subprocess.run(command, env=env, stdout=stdout, stderr=stderr,
                                    timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Astrid CLI {' '.join(argv)} timed out after {timeout}s; see {output_path} and {stderr_path}") from exc
    if result.returncode:
        raise RuntimeError(f"Astrid CLI {' '.join(argv)} failed with exit {result.returncode}; see {output_path} and {stderr_path}: {_tail(stderr_path)}")
    try:
        value = json.loads(output_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Astrid CLI {' '.join(argv)} did not produce valid JSON in {output_path}: {exc}; see {stderr_path}") from exc
    return value


def discover_attached_target() -> dict:
    """Run the current discovery route; switch only this function after F04."""
    # Current G-13 contract: list rows expose an attached target URL in `target`.
    value = cli(["generate", "list", "--json"], "generate-list.json")
    if not isinstance(value, list):
        raise RuntimeError("target discovery: generate list JSON must be an array of inventory rows; see generate-list.json")
    matches = [row for row in value if isinstance(row, dict)
               and row.get("target") == TARGET and row.get("target_kind") == "attached"]
    if not matches:
        save("target-discovery-error.json", {
            "expected_target": TARGET,
            "contract": "generate list rows identify attached ComfyUI using target=<URL>, target_kind=attached",
            "observed_targets": sorted({str(row.get("target")) for row in value if isinstance(row, dict)}),
        })
        raise RuntimeError(f"target discovery: {TARGET} is absent from attached target rows; see generate-list.json and target-discovery-error.json")
    save("attached-target.json", {"target": TARGET, "target_kind": "attached", "matching_rows": len(matches)})
    return value


def reuse_discovery() -> None:
    if not DISCOVERY_JSON.is_file():
        raise RuntimeError("generate list reuse: discovery output is missing; run the target-discovery step first")
    value = json.loads(DISCOVERY_JSON.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not any(
            isinstance(row, dict) and row.get("target") == TARGET and row.get("target_kind") == "attached"
            for row in value):
        raise RuntimeError("generate list reuse: saved discovery JSON does not retain the attached target")
    print(f"Reusing saved `astrid generate list --json` result: {DISCOVERY_JSON}", flush=True)


def comfy_install() -> None:
    comfy = ROOT / "journey-comfy"
    try:
        revision = subprocess.check_output(["git", "-C", str(comfy), "rev-parse", "HEAD"], text=True,
                                           stderr=subprocess.STDOUT, timeout=15).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"ComfyUI install: cannot read pinned checkout identity: {exc}") from exc
    if revision != COMFYUI_SHA:
        raise RuntimeError(f"ComfyUI install: expected pinned commit {COMFYUI_SHA}, got {revision}")
    save("comfyui-source.json", {"repository": "https://github.com/Comfy-Org/ComfyUI", "sha": revision})
    run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], "comfy-pip-upgrade.log", timeout=600)
    run([sys.executable, "-m", "pip", "install", "torch", "torchvision", "torchaudio",
         "--index-url", "https://download.pytorch.org/whl/cpu"], "comfy-cpu-torch-install.log", timeout=1800)
    run([sys.executable, "-m", "pip", "install", "-r", comfy / "requirements.txt"],
        "comfy-requirements-install.log", timeout=1800)
    probe = run([sys.executable, "-c", "import json,torch; print(json.dumps({'torch':torch.__version__,'cuda_available':torch.cuda.is_available()}))"],
                "comfy-torch-probe.json", timeout=60, show=False)
    try:
        status = json.loads(probe)
    except ValueError as exc:
        raise RuntimeError("ComfyUI install: CPU PyTorch probe was not valid JSON; see comfy-torch-probe.json") from exc
    if status.get("cuda_available") is not False:
        raise RuntimeError(f"ComfyUI install: expected a CPU-only torch build, observed {status}; see comfy-torch-probe.json")
    save("comfy-torch.json", status)


def comfy_start() -> None:
    comfy = ROOT / "journey-comfy"
    log = (EVIDENCE / "comfyui.log").open("w", encoding="utf-8")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
    try:
        process = subprocess.Popen([sys.executable, "main.py", "--cpu", "--listen", "127.0.0.1", "--port", "8188"],
                                   cwd=comfy, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=flags, stdin=subprocess.DEVNULL, close_fds=True)
    except OSError as exc:
        log.close()
        save("comfyui-start-error.json", {"error": str(exc)})
        raise RuntimeError(f"ComfyUI start: could not launch pinned CPU server: {exc}") from exc
    log.close()
    save("comfyui-process.json", {"pid": process.pid, "target": TARGET, "args": ["main.py", "--cpu", "--listen", "127.0.0.1", "--port", "8188"]})
    print(f"Started pinned CPU ComfyUI process {process.pid}", flush=True)


def _http_json(url: str, timeout=5) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError(f"HTTP {response.status}")
        return json.load(response)


def wait_for(label: str, probe, *, seconds: int, interval=4) -> object:
    deadline = time.monotonic() + seconds
    last_error = "no response yet"
    while True:
        try:
            value = probe()
            if value:
                print(label, flush=True)
                return value
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = str(exc)
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{label}: timed out after {seconds}s; last probe: {last_error}")
        print(f"Waiting for {label}", flush=True)
        time.sleep(interval)


def runtime_start() -> None:
    try:
        run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "$ErrorActionPreference='Stop'; Start-ScheduledTask -TaskName 'Astrid Runtime' -TaskPath '\\'"],
            "runtime-start.log", timeout=60)
    except RuntimeError as exc:
        raise RuntimeError(f"Runtime start: Scheduled Task could not be started: {exc}") from exc


def runtime_ready() -> None:
    def probe():
        value = _http_json(HEALTH_URL, timeout=5)
        if not isinstance(value, dict) or value.get("status") != "ok":
            return None
        runtime_id = value.get("runtime_instance_id")
        ready_path = data_root() / "runtime" / "generic-host.ready.json"
        if not runtime_id or not ready_path.is_file():
            return None
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        host = next((item for item in value.get("services", []) if item.get("name") == "pack-host"), {})
        if host.get("state") != "running" or ready.get("status") != "ready" or ready.get("runtime_instance_id") != runtime_id:
            return None
        save("runtime-health.json", value)
        save("pack-host-ready.json", ready)
        return {"status": "ok", "runtime_instance_id": runtime_id, "pack_host": "ready"}
    result = wait_for("Installed Runtime and pack host are ready on 127.0.0.1:17340", probe, seconds=900)
    save("runtime-readiness.json", result)


def comfy_ready() -> None:
    def probe():
        value = _http_json(TARGET + "/system_stats", timeout=5)
        if not isinstance(value, dict) or not isinstance(value.get("devices"), list):
            return None
        info = _http_json(TARGET + "/object_info", timeout=15)
        if not isinstance(info, dict) or "EmptyImage" not in info or "SaveImage" not in info:
            return None
        save("comfyui-readiness.json", {"url": TARGET, "system_stats": value,
                                         "required_model_free_nodes": ["EmptyImage", "SaveImage"]})
        return value
    wait_for(f"Pinned CPU ComfyUI API is ready at {TARGET}", probe, seconds=900)


def workflow() -> None:
    workflow_path = EVIDENCE / "empty-image-save-image.json"
    graph = {
        "last_node_id": 2,
        "last_link_id": 1,
        "version": 0.4,
        "nodes": [
            {"id": 1, "type": "EmptyImage", "pos": [0, 0], "size": [220, 100],
             "flags": {}, "order": 0, "mode": 0, "inputs": [],
             "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [1], "slot_index": 0}],
             "properties": {"Node name for S&R": "EmptyImage"}, "widgets_values": [64, 64, 1]},
            {"id": 2, "type": "SaveImage", "pos": [320, 0], "size": [220, 100],
             "flags": {}, "order": 1, "mode": 0,
             "inputs": [{"name": "images", "type": "IMAGE", "link": 1}], "outputs": [],
             "properties": {"Node name for S&R": "SaveImage"}, "widgets_values": ["g12-windows-cpu"]},
        ],
        "links": [[1, 1, 0, 2, 0, "IMAGE"]],
    }
    workflow_path.write_text(json.dumps(graph, indent=2) + "\n", encoding="utf-8")
    before = {str(path.resolve()) for path in (ROOT / "journey-comfy" / "output").glob("**/*.png")}
    result = cli(["generate", "workflow", str(workflow_path), "--target", TARGET, "--json"],
                 "generate-workflow.json", timeout=1800)
    if isinstance(result, dict) and result.get("error"):
        save("generate-workflow-error.json", {"error": result["error"], "result": result})
        raise RuntimeError("workflow: CLI result contains an error; see generate-workflow.json and generate-workflow-error.json")
    task_id = _find_task_id(result)
    if not task_id:
        save("generate-task-id-error.json", {"error": "successful workflow result did not contain a task_id", "result": result})
        raise RuntimeError("workflow: successful result contained no task_id; see generate-workflow.json and generate-task-id-error.json")
    save("task-id.json", {"task_id": task_id})
    save("png-search-before.json", {"paths": sorted(before)})


def _find_task_id(value: object) -> str | None:
    if isinstance(value, dict):
        candidate = value.get("task_id")
        if isinstance(candidate, str) and candidate.strip():
            return candidate
        for child in value.values():
            found = _find_task_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_task_id(child)
            if found:
                return found
    return None


def tasks_show() -> None:
    if not TASK_ID_FILE.is_file():
        raise RuntimeError("tasks show: task-id.json is missing; workflow step must succeed first")
    task_id = json.loads(TASK_ID_FILE.read_text(encoding="utf-8")).get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError("tasks show: task-id.json does not contain a valid task_id")
    result = cli(["tasks", "show", task_id, "--json"], "tasks-show.json")
    if isinstance(result, dict) and result.get("ok") is False:
        raise RuntimeError(f"tasks show: Runtime returned ok=false for task {task_id}; see tasks-show.json")
    before_path = EVIDENCE / "png-search-before.json"
    before = set(json.loads(before_path.read_text(encoding="utf-8")).get("paths", [])) if before_path.is_file() else set()
    candidates = [path for path in (ROOT / "journey-comfy" / "output").glob("**/*.png")
                  if str(path.resolve()) not in before]
    if not candidates:
        raise RuntimeError(f"tasks show: no newly produced PNG found under {ROOT / 'journey-comfy' / 'output'}; see comfyui.log and tasks-show.json")
    source = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    destination = EVIDENCE / "generated.png"
    shutil.copyfile(source, destination)
    save("generated-png.json", {"source": str(source), "artifact": str(destination), "bytes": destination.stat().st_size})


def stop_comfy() -> None:
    process_file = EVIDENCE / "comfyui-process.json"
    if not process_file.is_file():
        save("comfyui-stop.json", {"stopped": False, "reason": "ComfyUI was not started"})
        return
    pid = json.loads(process_file.read_text(encoding="utf-8")).get("pid")
    if not isinstance(pid, int):
        save("comfyui-stop.json", {"stopped": False, "reason": "ComfyUI process record has no pid"})
        return
    result = subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                            capture_output=True, text=True, timeout=30, check=False)
    save("comfyui-stop.json", {"pid": pid, "exit_code": result.returncode,
                                "stdout": result.stdout[-2000:], "stderr": result.stderr[-2000:]})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("install", "runtime-start", "runtime-ready", "comfy-install",
                                          "comfy-start", "comfy-ready", "discover-target", "reuse-list",
                                          "workflow", "tasks-show", "comfy-stop"))
    parser.add_argument("--installer-shell", choices=("powershell.exe", "pwsh.exe"))
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This journey must run on Windows")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    actions = {
        "runtime-start": runtime_start, "runtime-ready": runtime_ready,
        "comfy-install": comfy_install, "comfy-start": comfy_start,
        "comfy-ready": comfy_ready, "discover-target": discover_attached_target,
        "reuse-list": reuse_discovery, "workflow": workflow,
        "tasks-show": tasks_show, "comfy-stop": stop_comfy,
    }
    if args.step == "install":
        if not args.installer_shell:
            parser.error("install requires --installer-shell")
        install(args.installer_shell)
    else:
        actions[args.step]()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        if "args" in locals() and getattr(args, "step", None):
            try:
                save(f"{args.step}-error.json", {"step": args.step, "error": str(exc)})
            except Exception:
                pass
        print(f"G-12 Windows CPU journey failed: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
