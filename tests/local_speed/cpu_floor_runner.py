"""Run Plan 55's existing floor probes against an isolated, real CPU ComfyUI.

This is a measurement runner, not the LS-2 product meter or GPU qualification.
It never connects to a user's server, downloads weights, or changes product pins.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import statistics
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


def summarize(path: Path, *, integrated: bool, samples: int) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["records"]
    if len(rows) != samples or {r["sample"] for r in rows} != set(range(samples)):
        raise ValueError(f"{path.name}: incomplete or duplicate sample set")
    wall_key = "caller_wall_ms" if integrated else "wall_ms"
    execution_key = "execution_observed_ms" if integrated else "execution_ms"
    overhead = []
    sizes = []
    for row in rows:
        wall, execution = row[wall_key], row[execution_key]
        if not all(isinstance(n, (int, float)) and 0 <= n < float("inf") for n in (wall, execution)):
            raise ValueError(f"{path.name}: invalid duration")
        if execution > wall:
            raise ValueError(f"{path.name}: execution exceeds caller wall time")
        overhead.append(wall - execution)
        if integrated:
            if not row.get("digest", "").startswith("sha256:") or row.get("bytes", 0) <= 0:
                raise ValueError(f"{path.name}: missing stored-output identity")
            sizes.append(row["bytes"])
        else:
            if not row.get("outputs") or any(o.get("bytes", 0) <= 0 or len(o.get("sha256", "")) != 64 for o in row["outputs"]):
                raise ValueError(f"{path.name}: missing fetched output")
            sizes.append(sum(o["bytes"] for o in row["outputs"]))
    return {
        "samples": samples,
        "overhead_observed_ms": {"median": statistics.median(overhead), "max": max(overhead)},
        "output_bytes": {"min": min(sizes), "max": max(sizes)},
        "phases_ms": data["summary_ms"],
        "queue_wait_subtracted": False,
        "execution_clock": "client-observed ComfyUI execution_start to execution_success",
        "product_meter": False,
        "gpu_qualification": False,
    }


def run_logged(argv: list[str], cwd: Path, path: Path, *, env=None, timeout=600) -> None:
    with path.open("w", encoding="utf-8") as log:
        log.write(json.dumps({"argv": argv, "cwd": str(cwd)}) + "\n")
        log.flush()
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                timeout=timeout, check=False)
    path.with_suffix(path.suffix + ".exit").write_text(str(result.returncode) + "\n")
    if result.returncode:
        raise RuntimeError(f"{path.name}: exit {result.returncode}; original output preserved")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--comfy", type=Path, required=True)
    p.add_argument("--runtime", type=Path, required=True)
    p.add_argument("--dev", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--samples", type=int, default=20)
    a = p.parse_args(argv)
    if a.samples < 1:
        p.error("--samples must be positive")
    comfy, runtime, dev, out = [x.resolve() for x in (a.comfy, a.runtime, a.dev, a.output)]
    out.mkdir(parents=True, exist_ok=False)
    probe = dev / "releases/14/evidence/ls1"
    metadata = {
        "run": "2026-11-15-local-speed", "manager": "chatgpt-workspace-6ee41b0f0cca/root",
        "owner_decision": "D9", "python": sys.version, "platform": platform.platform(),
        "samples_per_cell": a.samples, "gpu_used": False, "models_downloaded": False,
        "measurement": "real ComfyUI CPU floor; not installed Agent end-to-end latency",
        "sources": {}, "probe_sha256": {},
    }
    for name, root in (("agent", Path(__file__).resolve().parents[2]), ("comfyui", comfy),
                       ("runtime", runtime), ("astrid_dev", dev)):
        metadata["sources"][name] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    for name in ("comfy_floor.py", "integrated_floor.py", "trivial-graph.json"):
        raw = (probe / name).read_bytes()
        metadata["probe_sha256"][name] = hashlib.sha256(raw).hexdigest()
        (out / name).write_bytes(raw)
    (out / "environment.json").write_text(json.dumps(metadata, indent=2) + "\n")
    run_logged([sys.executable, "-m", "pip", "freeze"], comfy, out / "pip-freeze.log")
    data = out / "comfy-data"
    for directory in ("input", "output", "temp", "user"):
        (data / directory).mkdir(parents=True)
    from PIL import Image
    import random
    # Seeded 1-Mpixel RGB fixture with genuine encoded pixel data, not padding.
    # Synthetic image, not a photograph; the saved byte count is reported exactly.
    rng = random.Random(55)
    pixels = bytes((v & 3) * 85 for v in rng.randbytes(1024 * 1024 * 3))
    Image.frombytes("RGB", (1024, 1024), pixels).save(data / "input/ls1-cpu.png")
    image_graph = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "ls1-cpu.png"}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "__LS1_PREFIX__"}},
    }
    (out / "image-graph.json").write_text(json.dumps(image_graph, indent=2) + "\n")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    command = [sys.executable, "main.py", "--cpu", "--disable-all-custom-nodes", "--disable-api-nodes",
               "--listen", "127.0.0.1", "--port", str(port),
               "--input-directory", str(data / "input"), "--output-directory", str(data / "output"),
               "--temp-directory", str(data / "temp"), "--user-directory", str(data / "user")]
    env = dict(os.environ, CUDA_VISIBLE_DEVICES="", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    summaries = {}
    with (out / "comfy-server.log").open("w", encoding="utf-8") as log:
        log.write(json.dumps({"argv": command}) + "\n")
        log.flush()
        server = subprocess.Popen(command, cwd=comfy, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 180
            while True:
                if server.poll() is not None:
                    raise RuntimeError(f"ComfyUI exited {server.returncode}; see comfy-server.log")
                try:
                    with urlopen(url + "/system_stats", timeout=2) as r:
                        stats = json.load(r)
                    break
                except (URLError, TimeoutError, OSError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("ComfyUI readiness timed out")
                    time.sleep(0.25)
            (out / "system-stats.json").write_text(json.dumps(stats, indent=2) + "\n")
            child_env = dict(env, PYTHONPATH=os.pathsep.join((str(runtime), str(runtime / "tests"))))
            for graph_name, graph in (("trivial", probe / "trivial-graph.json"), ("image1mp", out / "image-graph.json")):
                # Separate unmeasured warm-up; retain it, never silently drop sample 0.
                common = ["--url", url, "--graph", str(graph), "--timeout", "120"]
                run_logged([sys.executable, str(probe / "comfy_floor.py"), *common, "--samples", "1",
                            "--output", str(out / f"warmup-{graph_name}.json")], runtime,
                           out / f"warmup-{graph_name}.log", env=child_env)
                cases = [(False, 1), (True, 1), (True, 2)]
                for integrated, callers in cases:
                    name = f"{graph_name}-{'integrated' if integrated else 'transport'}-{callers}"
                    target = out / f"{name}.json"
                    args = [sys.executable, str(probe / ("integrated_floor.py" if integrated else "comfy_floor.py")),
                            *common, "--samples", str(a.samples), "--output", str(target)]
                    if integrated:
                        args += ["--callers", str(callers)]
                    run_logged(args, runtime, out / f"{name}.log", env=child_env)
                    summaries[name] = summarize(target, integrated=integrated, samples=a.samples)
                    (out / "summary.json").write_text(json.dumps(summaries, indent=2) + "\n")
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=10)
            (out / "server.exit").write_text(str(server.returncode) + "\n")
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
