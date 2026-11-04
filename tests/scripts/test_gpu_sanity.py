import importlib.util
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gpu_sanity.py"
CUDA = "/usr/local/cuda-13.0/lib64"


def _load():
    spec = importlib.util.spec_from_file_location("gpu_sanity_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_venv(tmp_path, body, code):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text(f"#!/bin/sh\n{body}\nexit {code}\n")
    py.chmod(0o755)
    return str(venv)


def test_env_comes_from_engine_child_env_and_ignores_caller_ld_library_path(tmp_path, monkeypatch):
    module = _load()
    monkeypatch.setenv("LD_LIBRARY_PATH", CUDA)
    calls, seen = [], {}
    real = module.engine_child_env

    def spy(venv, env=None):
        calls.append((venv, dict(env or {})))
        return real(venv, env)

    def fake_run(argv, **kw):
        seen.update(argv=argv, env=kw["env"])
        return subprocess.CompletedProcess(argv, 0, "OK bfloat16\nOK float16\nOK float32\nLIB /v/libcublas.so.13\n", "")

    monkeypatch.setattr(module, "engine_child_env", spy)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    venv = str(tmp_path / "venv")
    assert module.run(venv) == 0
    assert calls == [(venv, {})]  # the caller's environment is never passed in
    assert CUDA not in seen["env"]["LD_LIBRARY_PATH"]
    assert seen["argv"][0] == venv + "/bin/python"
    for dtype in ("bfloat16", "float16", "float32"):
        assert dtype in seen["argv"][2]
    assert "libcublasLt" in seen["argv"][2] or "libcublas" in seen["argv"][2]


def test_failing_python_exits_2_with_message_and_lib_paths(tmp_path):
    venv = _stub_venv(tmp_path, 'echo "OK bfloat16"; echo "FAIL RuntimeError: CUBLAS_STATUS_EXECUTION_FAILED"; '
                      'echo "LIB /v/nvidia/cublas/lib/libcublas.so.13"; echo "LIB /v/nvidia/cublas/lib/libcublasLt.so.13"', 1)
    r = subprocess.run([sys.executable, str(SCRIPT), "--venv", venv], capture_output=True, text=True,
                       env={**os.environ, "LD_LIBRARY_PATH": CUDA})
    assert r.returncode == 2
    assert "CUBLAS_STATUS_EXECUTION_FAILED" in r.stdout + r.stderr
    assert "libcublas.so.13" in r.stderr and "libcublasLt.so.13" in r.stderr
    assert CUDA not in r.stdout + r.stderr


def test_zero_exit_without_all_three_dtypes_still_fails(tmp_path):
    venv = _stub_venv(tmp_path, 'echo "OK float32"', 0)
    r = subprocess.run([sys.executable, str(SCRIPT), "--venv", venv], capture_output=True, text=True)
    assert r.returncode == 2 and "1/3" in r.stderr


def test_missing_interpreter_and_relative_venv_fail_closed(tmp_path):
    for venv in (str(tmp_path / "nope"), "relative/venv"):
        r = subprocess.run([sys.executable, str(SCRIPT), "--venv", venv], capture_output=True, text=True)
        assert r.returncode == 2, venv


def test_passing_stub_exits_0(tmp_path):
    venv = _stub_venv(tmp_path, 'echo "OK bfloat16"; echo "OK float16"; echo "OK float32"; echo "LIB /v/libcublas.so.13"', 0)
    r = subprocess.run([sys.executable, str(SCRIPT), "--venv", venv], capture_output=True, text=True)
    assert r.returncode == 0 and "PASSED" in r.stdout
