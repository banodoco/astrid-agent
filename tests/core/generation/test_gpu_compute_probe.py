from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from unittest.mock import MagicMock, call

import pytest

from astrid.core.generation import gpu_environment as gpu

OPERATIONS = ("matmul_fp32", "matmul_fp16", "matmul_bf16", "conv2d_fp16", "softmax_fp32")
DEVICE = {"name": "NVIDIA GeForce RTX 5070 Ti Laptop GPU", "capability": [12, 0]}


def report(**changes):
    return {
        "schema": gpu.COMPUTE_SCHEMA,
        "ok": True,
        "torch": "2.5.1+cu124",
        "cuda": "12.4",
        "devices": [DEVICE],
        "arch_list": ["sm_120"],
        "operations": dict.fromkeys(OPERATIONS, True),
        "error": None,
        **changes,
    }


def failed(stage="matmul_fp16", message="a CUDA operation failed"):
    return {"stage": stage, "type": "RuntimeError", "message": message}


@pytest.mark.parametrize(
    "raw,kwargs,cause,fragments",
    [
        (report(), {}, None, ("2.5.1+cu124", "12.4", "5070 Ti")),
        (None, {"timed_out": True, "returncode": 2}, "timed_out", ("timed out",)),
        (None, {}, "probe_failed", ("valid CUDA compute report",)),
        (report(), {"returncode": 17}, "probe_failed", ("17",)),
        (report(ok=False, torch=None, cuda=None, devices=[], arch_list=[], operations={},
                error=failed("import")), {}, "no_torch", ("PyTorch", "import")),
        (report(ok=False, cuda=None, devices=[], operations={}), {}, "cpu_only_wheel",
         ("2.5.1+cu124", "without CUDA")),
        (report(ok=False, devices=[], operations={}), {}, "no_device", ("12.4", "2.5.1+cu124")),
        (report(ok=False, arch_list=["sm_50", "sm_90"], operations={},
                error=failed(message="out of memory")), {}, "wheel_arch_mismatch",
         ("2.5.1+cu124", "sm_120", "5070 Ti")),
        (report(ok=False, error=failed(message="CUDA error: no kernel image is available for execution on the device")),
         {}, "wheel_arch_mismatch", ("2.5.1+cu124", "5070 Ti")),
        (report(ok=False, error=failed(message="driver version 440 is too old")), {},
         "driver_too_old", ("12.4", "2.5.1+cu124", "too old")),
        (report(ok=False, error=failed()), {"stderr": "CUDA out of memory"}, "out_of_memory",
         ("2.5.1+cu124", "matmul_fp16", "5070 Ti")),
        (report(ok=False, error=failed("conv2d_fp16")), {}, "operation_failed",
         ("2.5.1+cu124", "conv2d_fp16")),
        (report(operations={"matmul_fp32": True}), {}, "operation_failed", ("matmul_fp16",)),
        (report(operations={**dict.fromkeys(OPERATIONS, True), "softmax_fp32": False}),
         {}, "operation_failed", ("softmax_fp32",)),
    ],
)
def test_classify_compute(raw, kwargs, cause, fragments):
    result = gpu.classify_compute(raw, **kwargs)
    assert result["schema"] == gpu.COMPUTE_SCHEMA
    assert result["cause"] == cause
    assert result["ok"] is (cause is None)
    assert (result["works"] == "") is (cause is None)
    assert all(fragment in result["message"] for fragment in fragments)
    if cause is None:
        assert result["torch"] == raw["torch"]
        assert result["cuda"] == raw["cuda"]
        assert result["devices"] == [DEVICE["name"]]
        assert result["operations"] == raw["operations"]
    if cause == "wheel_arch_mismatch" and raw["arch_list"] == ["sm_50", "sm_90"]:
        assert "12.8" in result["works"]


@pytest.mark.parametrize("raw", [None, [], 1, "garbage", {}, {"schema": gpu.COMPUTE_SCHEMA,
    "devices": [None, {"name": [], "capability": [{}, []]}],
    "arch_list": [{}], "operations": {"matmul_fp32": []}, "error": [],
    "torch": {}, "cuda": []}])
def test_classify_malformed_does_not_raise(raw):
    result = gpu.classify_compute(raw)
    assert not result["ok"]
    assert result["cause"]
    assert result["message"] and result["works"]


def test_probe_compute_success(monkeypatch):
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        monkeypatch.setenv(name, "must-not-leak")
    monkeypatch.setenv("ASTRID_COMPUTE_TEST", "preserved")
    runner = MagicMock(return_value=subprocess.CompletedProcess(
        [], 0, "torch notice\n" + json.dumps(report()) + "\n\n", "",
    ))
    result = gpu.probe_compute("candidate-python", run=runner, timeout=3.0)
    assert result["ok"]
    args, kwargs = runner.call_args
    assert args == (["candidate-python", "-I", "-c", gpu.COMPUTE_PROBE_SOURCE],)
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 3.0
    assert kwargs["env"]["ASTRID_COMPUTE_TEST"] == "preserved"
    assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"} & kwargs["env"].keys()


@pytest.mark.parametrize("outcome,cause", [
    (subprocess.TimeoutExpired("candidate-python", 3.0), "timed_out"),
    (OSError("not executable"), "probe_failed"),
    (subprocess.CompletedProcess([], 0, "garbage\n", ""), "probe_failed"),
    (subprocess.CompletedProcess([], 2, json.dumps(report()), "error"), "probe_failed"),
])
def test_probe_compute_failures(outcome, cause):
    runner = MagicMock()
    if isinstance(outcome, Exception):
        runner.side_effect = outcome
    else:
        runner.return_value = outcome
    result = gpu.probe_compute("candidate-python", run=runner)
    assert result["cause"] == cause
    assert not result["ok"]
    assert result["works"]
    if isinstance(outcome, OSError):
        assert "could not be started" in result["message"]
        assert "candidate-python" in result["message"]


def test_probe_compute_real_interpreter_without_torch():
    # Do not import torch, even to decide whether this case should be skipped.
    if importlib.util.find_spec("torch") is not None:
        pytest.skip("This interpreter has torch installed")
    assert gpu.probe_compute(sys.executable)["cause"] == "no_torch"


@pytest.fixture
def torch_mock(monkeypatch):
    torch = MagicMock()
    torch.__version__ = "2.5.1+cu124"
    torch.version.cuda = "12.4"
    torch.cuda.is_available.return_value = True
    torch.cuda.device_count.return_value = 1
    torch.cuda.get_device_name.return_value = DEVICE["name"]
    torch.cuda.get_device_capability.return_value = (12, 0)
    torch.cuda.get_arch_list.return_value = ["sm_120"]
    torch.allclose.return_value = True
    monkeypatch.setitem(sys.modules, "torch", torch)
    return torch


def execute_script(capsys):
    exec(gpu.COMPUTE_PROBE_SOURCE, {})
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result["schema"] == gpu.COMPUTE_SCHEMA
    return result


def test_compute_source_working_device(torch_mock, capsys):
    result = execute_script(capsys)
    assert result == report()
    assert torch_mock.cuda.get_device_name.call_args_list == [call(0)]
    assert torch_mock.cuda.get_device_capability.call_args_list == [call(0)]
    assert torch_mock.cuda.synchronize.call_count == len(OPERATIONS)
    assert torch_mock.allclose.call_count == len(OPERATIONS)
    assert torch_mock.matmul.call_count == 6
    assert torch_mock.nn.functional.conv2d.call_count == 2
    assert torch_mock.softmax.call_count == 2
    assert torch_mock.tensor.return_value.to.call_args_list == [
        call(device="cuda:0", dtype=dtype)
        for dtype in (torch_mock.float32, torch_mock.float32, torch_mock.float16,
                      torch_mock.float16, torch_mock.bfloat16, torch_mock.bfloat16,
                      torch_mock.float16, torch_mock.float16, torch_mock.float32)
    ]


@pytest.mark.parametrize("cpu_only", [False, True])
def test_compute_source_without_device(torch_mock, capsys, cpu_only):
    torch_mock.cuda.is_available.return_value = False
    if cpu_only:
        torch_mock.version.cuda = None
    result = execute_script(capsys)
    assert not result["ok"]
    assert result["devices"] == []
    assert result["operations"] == {}
    assert result["error"] is None
    assert gpu.classify_compute(result)["cause"] == ("cpu_only_wheel" if cpu_only else "no_device")
    torch_mock.matmul.assert_not_called()


def test_compute_source_missing_kernel(torch_mock, capsys):
    message = "CUDA error: no kernel image is available for execution on the device"
    torch_mock.matmul.side_effect = [MagicMock(), RuntimeError(message)]
    result = execute_script(capsys)
    assert not result["ok"]
    assert result["operations"] == {"matmul_fp32": False}
    assert result["error"] == failed("matmul_fp32", message)
    assert gpu.classify_compute(result)["cause"] == "wheel_arch_mismatch"
    torch_mock.cuda.synchronize.assert_not_called()
    torch_mock.nn.functional.conv2d.assert_not_called()


def test_compute_source_mismatch_and_truncated_error(torch_mock, capsys):
    torch_mock.allclose.side_effect = [False, True, True, True, True]
    result = execute_script(capsys)
    assert result["ok"] is False
    assert result["operations"] == {**dict.fromkeys(OPERATIONS, True), "matmul_fp32": False}
    torch_mock.matmul.side_effect = RuntimeError("x" * 400)
    result = execute_script(capsys)
    assert result["error"]["message"] == "x" * 300
