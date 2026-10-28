#!/usr/bin/env python3
"""GPU sanity check under the exact environment the engine's ComfyUI sees.

Usage: gpu_sanity.py --venv <engine venv prefix>

The child environment is built by ``engine_child_env(venv)``; the caller's
``LD_LIBRARY_PATH`` is never read. The child runs a 2048x2048 matmul and a
``torch.nn.functional.linear`` on cuda in bfloat16, float16 and float32, then
prints the libcublas / libcublasLt entries of /proc/self/maps. Fail closed: any
exception or non-zero exit prints the message plus the lib paths and exits 2.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

_BOOTSTRAP = Path(__file__).resolve().parents[1] / "astrid" / "core" / "generation" / "_engine_python_bootstrap.py"


def _load_engine_child_env():
    # Stdlib-only module, loaded by path so no astrid package import (and no caller site-packages) is needed.
    spec = importlib.util.spec_from_file_location("_astrid_engine_python_bootstrap", _BOOTSTRAP)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.engine_child_env


engine_child_env = _load_engine_child_env()

CHILD = r"""
import sys
def libs():
    out = []
    try:
        with open("/proc/self/maps") as fh:
            for line in fh:
                p = line.split(None, 5)
                if len(p) == 6 and ("libcublas" in p[5]):
                    out.append(p[5].strip())
    except OSError as exc:
        out.append("unreadable /proc/self/maps: %r" % (exc,))
    return sorted(set(out))
def report():
    for p in libs() or ["(no libcublas mapped)"]:
        print("LIB " + p, flush=True)
try:
    import torch
    import torch.nn.functional as F
    if not torch.cuda.is_available():
        raise RuntimeError("torch.cuda.is_available() is False")
    for name in ("bfloat16", "float16", "float32"):
        dt = getattr(torch, name)
        a = torch.randn(2048, 2048, device="cuda", dtype=dt)
        b = torch.randn(2048, 2048, device="cuda", dtype=dt)
        c = a @ b
        d = F.linear(a, b)
        torch.cuda.synchronize()
        if not (bool(torch.isfinite(c).all()) and bool(torch.isfinite(d).all())):
            raise RuntimeError("non-finite result for " + name)
        print("OK " + name, flush=True)
except BaseException as exc:
    print("FAIL %s: %s" % (type(exc).__name__, exc), flush=True)
    report()
    sys.exit(1)
report()
"""


def run(venv: str, *, timeout: float = 600.0) -> int:
    env = engine_child_env(venv, {})
    python = venv + "/bin/python"
    print("gpu_sanity: python=%s LD_LIBRARY_PATH=%s" % (python, env["LD_LIBRARY_PATH"]), flush=True)
    try:
        proc = subprocess.run([python, "-c", CHILD], env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        print("gpu_sanity: FAILED to run %s: %s\nlibs: (none loaded)" % (python, exc), file=sys.stderr)
        return 2
    out = proc.stdout + proc.stderr
    libs = [ln[4:] for ln in proc.stdout.splitlines() if ln.startswith("LIB ")]
    print(out, end="" if out.endswith("\n") or not out else "\n")
    ok = [ln for ln in proc.stdout.splitlines() if ln.startswith("OK ")]
    if proc.returncode != 0 or len(ok) != 3:
        print("gpu_sanity: FAILED (exit %s, %d/3 dtypes ok)\nlibs: %s"
              % (proc.returncode, len(ok), ", ".join(libs) or "(none loaded)"), file=sys.stderr)
        return 2
    print("gpu_sanity: PASSED\nlibs: " + ", ".join(libs))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--venv", required=True, help="engine venv prefix (absolute)")
    args = ap.parse_args(argv)
    try:
        return run(args.venv)
    except ValueError as exc:
        print("gpu_sanity: FAILED: %s\nlibs: (none loaded)" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
