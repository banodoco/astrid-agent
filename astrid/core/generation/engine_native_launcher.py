"""Pinned Linux Comfy entrypoint, executed by the original Agent supervisor.

The upstream file is verified and compiled in memory as __main__, preserving
its CLI, CUDA/environment setup and shutdown blocks. Only three reviewed seams
are instrumented; no installed upstream source is written.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import types


def instrument(source, filename):
    tree = ast.parse(source, filename)
    workers = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "prompt_worker"]
    starts = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "start_comfyui"]
    if len(workers) != 1 or len(starts) != 1:
        raise ValueError("unknown native worker/start seam")
    worker, start = workers[0], starts[0]
    worker.decorator_list.insert(0, ast.parse("_astrid_fence.worker", mode="eval").body)
    loops = [n for n in worker.body if isinstance(n, ast.While) and isinstance(n.test, ast.Constant) and n.test.value is True]
    if len(loops) != 1:
        raise ValueError("unknown native iteration seam")
    loops[0].body.insert(0, ast.parse(
        "last_gc_collect = _astrid_fence.boundary(q, e, need_gc, last_gc_collect)").body[0])
    start.body.insert(0, ast.parse("_astrid_fence.prepare(execution)").body[0])
    seams = [i for i, n in enumerate(start.body) if isinstance(n, ast.Expr)
             and ast.unparse(n) == "prompt_server.add_routes()"]
    if len(seams) != 1:
        raise ValueError("unknown native dispatch seam")
    start.body.insert(seams[0] + 1, ast.parse("_astrid_fence.install_server(prompt_server, args)").body[0])
    return compile(ast.fix_missing_locations(tree), filename, "exec")


def main():
    if sys.platform != "linux" or os.getuid() == 0 or os.geteuid() == 0:
        raise ValueError("native fence requires an ordinary Linux UID")
    here = Path(__file__).resolve().parent
    # Import only the adjacent byte-bound Agent adapter, not an ambient module.
    spec = importlib.util.spec_from_file_location("_astrid_native_fence", here / "engine_native_fence.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    config = adapter.decode(os.environ.pop("ASTRID_NATIVE_CONTROL").encode())
    if set(config) != {"fd", "enrollment"} or type(config["fd"]) is not int:
        raise ValueError("missing private native channel")
    enrollment = config["enrollment"]
    policy_bytes = (here / "engine_native_policy.json").read_bytes()
    policy = json.loads(policy_bytes)
    root = Path.cwd().resolve()
    # v1 deliberately enrolls only plain canonical Comfy. H3 and attached,
    # external, Windows/macOS and interactive launch modes keep refusing bind.
    if enrollment["mode"] != "comfyui-linux-cu130-v1" or enrollment["policy"] != hashlib.sha256(policy_bytes).hexdigest():
        raise ValueError("unknown native policy")
    if sys.version_info[:3] != (3, 12, 10):
        raise ValueError("native interpreter differs from recipe")
    import aiohttp
    if aiohttp.__version__ != "3.14.5":
        raise ValueError("native dispatch dependency changed")
    source_files = {}
    for source_root, files in ((root, policy["comfy_python"]),
                               (Path(aiohttp.__file__).parent, policy["aiohttp_python"])):
        for relative, expected in files.items():
            path = source_root / relative
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("native source closure changed: " + relative)
            source_files[str(path.resolve())] = expected
        actual = {str(p.relative_to(source_root)) for p in source_root.rglob("*.py")
                  if ".git" not in p.parts}
        if actual != set(files):
            raise ValueError("native source closure has extra or missing Python files")
    for relative, expected in enrollment["adapter"].items():
        if relative not in {"engine_native_launcher.py", "engine_native_fence.py", "engine_native_policy.json"}:
            raise ValueError("unknown native helper")
        if hashlib.sha256((here / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("native helper changed")
    if set(enrollment["adapter"]) != {"engine_native_launcher.py", "engine_native_fence.py", "engine_native_policy.json"}:
        raise ValueError("incomplete native helper closure")
    channel = socket.socket(fileno=config["fd"])
    fence = adapter.Fence(channel, enrollment, source_files)
    # The launch recipe provides only this exact argv. No arbitrary custom
    # nodes/partner API/background asset services can join this enrollment.
    if sys.argv[1:] != ["--listen", "127.0.0.1", "--port", "8188", "--disable-all-custom-nodes",
                         "--disable-partner-nodes", "--extra-model-paths-config", str(root.parent / "extra_model_paths.yaml")]:
        raise ValueError("unsupported native argv")
    sys.argv[0] = str(root / "main.py")
    sys.path.insert(0, str(root))
    source = (root / "main.py").read_bytes()
    module = types.ModuleType("__main__")
    module.__file__ = str(root / "main.py")
    module.__package__ = None
    module._astrid_fence = fence
    sys.modules["__main__"] = module
    exec(instrument(source, str(root / "main.py")), module.__dict__)


if __name__ == "__main__":
    main()
