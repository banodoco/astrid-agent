from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import math
import os
import subprocess
import sys
from unittest.mock import MagicMock, call

import pytest

from astrid.core.generation import gpu_environment as gpu

OPERATIONS = ("matmul_fp32", "matmul_fp16", "matmul_bf16", "conv2d_fp16", "softmax_fp32")
DEVICE = {"index": 0, "name": "NVIDIA GeForce RTX 5070 Ti Laptop GPU", "capability": [12, 0]}
OTHER = {"index": 1, "name": "NVIDIA GeForce RTX 3090", "capability": [8, 6]}
CONTEXT = dict(python=sys.executable, backend="cuda", device="cuda:0",
               required_operations=OPERATIONS)


def report(*, required=OPERATIONS, **changes):
    numeric = {
        "matmul_fp32": [[2., 7.], [6., 15.]],
        "matmul_fp16": [[2., 7.], [6., 15.]],
        "matmul_bf16": [[2., 7.], [6., 15.]],
        "conv2d_fp16": [[[[-4., -4.], [-4., -4.]]]],
        "softmax_fp32": [[0.0900305732, 0.2447284711, 0.6652409558]],
    }
    return {
        "schema": gpu.COMPUTE_SCHEMA,
        "ok": True,
        "interpreter": sys.executable,
        "backend": "cuda",
        "device": "cuda:0",
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "cuda_device_order": os.environ.get("CUDA_DEVICE_ORDER"),
        "required_operations": list(required),
        "torch": "2.5.1+cu124",
        "cuda": "12.4",
        "hip": None,
        "devices": [DEVICE],
        "arch_list": ["sm_120"],
        "driver_version": "576.80",
        "driver_error": None,
        "wheel_versions": {"torch": "2.5.1+cu124"},
        "operations": dict.fromkeys(required, True),
        "numeric_outputs": {name: numeric[name] for name in required},
        "error": None,
        **changes,
    }


def classify(raw, **kwargs):
    return gpu.classify_compute(raw, **{**CONTEXT, **kwargs})


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
                error=failed(message="CUDA error: no kernel image is available")), {},
         "wheel_arch_mismatch", ("2.5.1+cu124", "sm_120", "5070 Ti")),
        # Missing an exact architecture is advisory, even on a failure. Do
        # not replace the actual memory failure with a guessed wheel mismatch.
        (report(ok=False, arch_list=["sm_50", "sm_90"], operations={},
                error=failed(message="out of memory")), {}, "out_of_memory",
         ("2.5.1+cu124", "matmul_fp16", "5070 Ti")),
        (report(ok=False, error=failed(message="CUDA error: no kernel image is available for execution on the device")),
         {}, "wheel_arch_mismatch", ("2.5.1+cu124", "5070 Ti")),
        (report(ok=False, error=failed(message="driver version 440 is too old")), {},
         "driver_too_old", ("12.4", "2.5.1+cu124", "too old")),
        (report(ok=False, devices=[], operations={}, error=failed("cuda", "CUDA driver version is insufficient")),
         {}, "driver_too_old", ("12.4", "2.5.1+cu124", "too old")),
        (report(ok=False, error=failed(message="out of memory")), {"stderr": "driver too old"},
         "out_of_memory", ("matmul_fp16", "5070 Ti")),
        (report(ok=False, error=failed()), {"stderr": "CUDA out of memory"}, "out_of_memory",
         ("2.5.1+cu124", "matmul_fp16", "5070 Ti")),
        (report(ok=False, error=failed("conv2d_fp16")), {}, "operation_failed",
         ("2.5.1+cu124", "conv2d_fp16")),
        (report(operations={"matmul_fp32": True}), {}, "operation_failed", ("matmul_fp16",)),
        (report(operations={**dict.fromkeys(OPERATIONS, True), "softmax_fp32": False}),
         {}, "operation_failed", ("softmax_fp32",)),
        (report(numeric_outputs={}), {}, "operation_failed", ("matmul_fp32",)),
        (report(hip="6.2", cuda=None), {}, "unsupported_backend", ("non-NVIDIA",)),
    ],
)
def test_classify_compute(raw, kwargs, cause, fragments):
    result = classify(raw, **kwargs)
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
        assert result["selected_device"] == DEVICE
        assert result["numeric_outputs"] == raw["numeric_outputs"]
        assert result["driver_version"] == raw["driver_version"]
        assert result["wheel_versions"] == raw["wheel_versions"]
    if cause == "wheel_arch_mismatch" and raw["arch_list"] == ["sm_50", "sm_90"]:
        assert "12.8" in result["works"]


@pytest.mark.parametrize("raw", [None, [], 1, "garbage", {}, {"schema": gpu.COMPUTE_SCHEMA,
    "devices": [None, {"name": [], "capability": [{}, []]}],
    "arch_list": [{}], "operations": {"matmul_fp32": []}, "error": [],
    "torch": {}, "cuda": []}])
def test_classify_malformed_does_not_raise(raw):
    result = classify(raw)
    assert not result["ok"]
    assert result["cause"]
    assert result["message"] and result["works"]


@pytest.mark.parametrize("change", [
    {"interpreter": "other-python"},
    {"device": "cuda:1"},
    {"backend": "mps"},
    {"required_operations": ["matmul_fp32"]},
    {"cuda_visible_devices": "different-visibility"},
    {"cuda_device_order": "different-order"},
])
def test_wrong_selection_is_never_reused(change):
    result = classify(report(**change))
    assert result["cause"] == "selection_mismatch"
    assert not result["ok"]


@pytest.mark.parametrize("numeric", [[[2., 7.], [6., 99.]], [[2., 7.]], [[float("nan"), 7.], [6., 15.]]])
def test_true_boolean_without_correct_numeric_output_cannot_pass(numeric):
    raw = report()
    raw["numeric_outputs"]["matmul_fp32"] = numeric
    result = classify(raw)
    assert result["cause"] == "operation_failed"
    assert result["operations"]["matmul_fp32"] is False


def test_probe_compute_success(monkeypatch):
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        monkeypatch.setenv(name, "must-not-leak")
    monkeypatch.setenv("ASTRID_COMPUTE_TEST", "preserved")
    runner = MagicMock(return_value=subprocess.CompletedProcess(
        [], 0, "torch notice\n" + json.dumps(report()) + "\n\n", "",
    ))
    result = gpu.probe_compute(**CONTEXT, run=runner, timeout=3.0)
    assert result["ok"]
    args, kwargs = runner.call_args
    assert args[0][:4] == [sys.executable, "-I", "-c", gpu.COMPUTE_PROBE_SOURCE]
    assert len(args[0]) == 5
    assert json.loads(args[0][4]) == {
        "interpreter": sys.executable, "device": "cuda:0", "backend": "cuda",
        "required_operations": list(OPERATIONS),
    }
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 3.0
    assert kwargs["env"]["ASTRID_COMPUTE_TEST"] == "preserved"
    assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"} & kwargs["env"].keys()


def test_selected_engine_environment_is_not_replaced_with_host_environment(monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/ambient")
    runner = MagicMock(return_value=subprocess.CompletedProcess(
        [], 0, json.dumps(report(cuda_visible_devices="3,2", cuda_device_order=None)), "",
    ))
    result = gpu.probe_compute(**CONTEXT, run=runner, environ={
        "CUDA_VISIBLE_DEVICES": "3,2", "LD_LIBRARY_PATH": "/selected", "PYTHONPATH": "discard",
    })
    assert result["ok"] and result["cuda_visible_devices"] == "3,2"
    assert runner.call_args.kwargs["env"] == {
        "CUDA_VISIBLE_DEVICES": "3,2", "LD_LIBRARY_PATH": "/selected",
    }


@pytest.mark.parametrize("changes,cause", [
    ({"python": None}, "interpreter_required"),
    ({"python": "python"}, "interpreter_required"),
    ({"device": None}, "device_required"),
    ({"device": "cuda"}, "device_required"),
    ({"device": "cuda:-1"}, "device_required"),
    ({"backend": "mps"}, "unsupported_backend"),
    ({"required_operations": None}, "operations_required"),
    ({"required_operations": []}, "operations_required"),
    ({"required_operations": ["unrecognized_fp16"]}, "operations_required"),
])
def test_unresolved_selection_or_requirements_never_spawn(changes, cause):
    runner = MagicMock()
    result = gpu.probe_compute(**{**CONTEXT, **changes}, run=runner)
    assert result["cause"] == cause and not result["ok"]
    runner.assert_not_called()


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
    result = gpu.probe_compute(**CONTEXT, run=runner)
    assert result["cause"] == cause
    assert not result["ok"]
    assert result["works"]
    if isinstance(outcome, OSError):
        assert "could not be started" in result["message"]
        assert sys.executable in result["message"]


def test_probe_compute_real_interpreter_without_torch():
    # Do not import torch, even to decide whether this case should be skipped.
    if importlib.util.find_spec("torch") is not None:
        pytest.skip("This interpreter has torch installed")
    assert gpu.probe_compute(**CONTEXT)["cause"] == "no_torch"


@pytest.fixture
def torch_mock(monkeypatch):
    # Numerical CPU fixture, not a physical CUDA or dtype implementation.
    # It performs the actual small arithmetic instead of making allclose
    # return True regardless of the values or device selected by the script.
    torch = MagicMock()
    torch.__version__ = "2.5.1+cu124"
    torch.version.cuda = "12.4"
    torch.version.hip = None
    torch.float32, torch.float16, torch.bfloat16 = "float32", "float16", "bfloat16"
    torch.cuda.is_available.return_value = True
    torch.cuda.device_count.return_value = 1
    torch.cuda.get_device_name.return_value = DEVICE["name"]
    torch.cuda.get_device_capability.return_value = (12, 0)
    torch.cuda.get_arch_list.return_value = ["sm_120"]
    torch.transfers = []
    torch.reject_bf16 = False

    class Tensor:
        def __init__(self, values, dtype, device="cpu"):
            self.values, self.dtype, self.device = values, dtype, device

        def to(self, *, device, dtype):
            torch.transfers.append(call(device=device, dtype=dtype))
            if dtype == "bfloat16" and torch.reject_bf16:
                raise RuntimeError("BF16 not supported by the selected GPU")
            return Tensor(self.values, dtype, device)

        def cpu(self):
            return Tensor(self.values, self.dtype)

        def float(self):
            return Tensor(self.values, "float32", self.device)

        def tolist(self):
            return self.values

    def matmul(left, right):
        assert left.device == right.device == f"cuda:{torch.cuda.set_device.call_args.args[0]}"
        return Tensor([[sum(a * b for a, b in zip(row, column))
                        for column in zip(*right.values)] for row in left.values],
                      left.dtype, left.device)

    def conv2d(left, right):
        assert left.device == right.device == f"cuda:{torch.cuda.set_device.call_args.args[0]}"
        image, kernel = left.values[0][0], right.values[0][0]
        height, width = len(kernel), len(kernel[0])
        values = [[sum(image[i + di][j + dj] * kernel[di][dj]
                       for di in range(height) for dj in range(width))
                   for j in range(len(image[0]) - width + 1)]
                  for i in range(len(image) - height + 1)]
        return Tensor([[values]], left.dtype, left.device)

    def softmax(value, *, dim):
        assert dim == -1
        assert value.device == f"cuda:{torch.cuda.set_device.call_args.args[0]}"
        rows = [[math.exp(x) for x in row] for row in value.values]
        return Tensor([[x / sum(row) for x in row] for row in rows], value.dtype, value.device)

    def close(actual, expected, *, rtol, atol):
        assert actual.device == expected.device == "cpu"
        # The device is synchronized before its observed output is compared.
        assert torch.cuda.synchronize.call_count > torch.allclose.call_count - 1

        def compare(a, b):
            if isinstance(b, list):
                return isinstance(a, list) and len(a) == len(b) and all(
                    compare(x, y) for x, y in zip(a, b)
                )
            return abs(a - b) <= atol + rtol * abs(b)

        return compare(actual.values, expected.values)

    torch.tensor.side_effect = Tensor
    torch.matmul.side_effect = matmul
    torch.nn.functional.conv2d.side_effect = conv2d
    torch.softmax.side_effect = softmax
    torch.allclose.side_effect = close
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, "576.80\n", "",
    ))

    def missing_package(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing_package)
    return torch


def execute_script(capsys, monkeypatch, *, device="cuda:0", operations=OPERATIONS):
    monkeypatch.setattr(sys, "argv", ["probe", json.dumps({
        "interpreter": sys.executable, "backend": "cuda",
        "device": device, "required_operations": list(operations),
    })])
    exec(gpu.COMPUTE_PROBE_SOURCE, {})
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result["schema"] == gpu.COMPUTE_SCHEMA
    return result


def test_compute_source_working_device(torch_mock, capsys, monkeypatch):
    result = execute_script(capsys, monkeypatch)
    assert classify(result)["ok"]
    assert result["operations"] == dict.fromkeys(OPERATIONS, True)
    assert result["numeric_outputs"]["matmul_fp32"] == [[2., 7.], [6., 15.]]
    assert result["numeric_outputs"]["conv2d_fp16"] == [[[[-4., -4.], [-4., -4.]]]]
    assert result["driver_version"] == "576.80"
    assert result["wheel_versions"] == {"torch": "2.5.1+cu124"}
    assert torch_mock.cuda.get_device_name.call_args_list == [call(0)]
    assert torch_mock.cuda.get_device_capability.call_args_list == [call(0)]
    torch_mock.cuda.set_device.assert_called_once_with(0)
    assert torch_mock.cuda.synchronize.call_args_list == [call("cuda:0")] * len(OPERATIONS)
    assert torch_mock.allclose.call_count == len(OPERATIONS)
    assert torch_mock.matmul.call_count == 3
    assert torch_mock.nn.functional.conv2d.call_count == 1
    assert torch_mock.softmax.call_count == 1
    assert torch_mock.transfers == [
        call(device="cuda:0", dtype=dtype)
        for dtype in (torch_mock.float32, torch_mock.float32, torch_mock.float16,
                      torch_mock.float16, torch_mock.bfloat16, torch_mock.bfloat16,
                      torch_mock.float16, torch_mock.float16, torch_mock.float32)
    ]


def test_selected_second_gpu_compatible_arch_and_optional_bf16(torch_mock, capsys, monkeypatch):
    torch_mock.cuda.device_count.return_value = 2
    torch_mock.cuda.get_device_name.side_effect = lambda index: [DEVICE, OTHER][index]["name"]
    torch_mock.cuda.get_device_capability.side_effect = lambda index: [DEVICE, OTHER][index]["capability"]
    torch_mock.cuda.get_arch_list.return_value = ["sm_80"]
    torch_mock.reject_bf16 = True
    operations = ("matmul_fp32", "conv2d_fp16", "softmax_fp32")
    raw = execute_script(capsys, monkeypatch, device="cuda:1", operations=operations)
    result = classify(raw, device="cuda:1", required_operations=operations)
    assert result["ok"]
    assert result["selected_device"] == OTHER
    assert result["arch_list"] == ["sm_80"]  # successful computation beats an exact-SM guess
    assert result["operations"] == dict.fromkeys(operations, True)
    assert result["numeric_outputs"]["matmul_fp32"] == [[2., 7.], [6., 15.]]
    assert result["numeric_outputs"]["conv2d_fp16"] == [[[[-4., -4.], [-4., -4.]]]]
    assert "5070 Ti" not in result["message"]
    torch_mock.cuda.set_device.assert_called_once_with(1)
    assert torch_mock.cuda.synchronize.call_args_list == [call("cuda:1")] * len(operations)
    assert all(transfer.kwargs["device"] == "cuda:1" for transfer in torch_mock.transfers)
    assert all(transfer.kwargs["dtype"] != "bfloat16" for transfer in torch_mock.transfers)


def test_other_gpu_inventory_failure_does_not_disqualify_selected_device(torch_mock, capsys, monkeypatch):
    torch_mock.cuda.device_count.return_value = 2
    torch_mock.cuda.get_device_name.side_effect = [RuntimeError("unselected GPU unavailable"), OTHER["name"]]
    torch_mock.cuda.get_device_capability.return_value = OTHER["capability"]
    operations = ("matmul_fp32",)
    raw = execute_script(capsys, monkeypatch, device="cuda:1", operations=operations)
    result = classify(raw, device="cuda:1", required_operations=operations)
    assert result["ok"]
    assert result["selected_device"] == OTHER
    assert result["device_inventory_errors"] == [{"index": 0, "message": "unselected GPU unavailable"}]
    torch_mock.cuda.synchronize.assert_called_once_with("cuda:1")


@pytest.mark.parametrize("device,dtype", [("cuda:1", "float32"), ("cuda:0", "float16")])
def test_wrong_output_device_or_dtype_cannot_pass(torch_mock, capsys, monkeypatch, device, dtype):
    output = torch_mock.tensor([[2., 7.], [6., 15.]], dtype=dtype)
    output.device = device
    torch_mock.matmul.side_effect = None
    torch_mock.matmul.return_value = output
    operations = ("matmul_fp32",)
    raw = execute_script(capsys, monkeypatch, operations=operations)
    result = classify(raw, required_operations=operations)
    assert result["cause"] == "operation_failed"
    assert result["operations"] == {"matmul_fp32": False}
    assert "different device or dtype" in raw["error"]["message"]
    assert result["numeric_outputs"] == {}


def test_required_bf16_failure_cannot_be_optional(torch_mock, capsys, monkeypatch):
    torch_mock.reject_bf16 = True
    operations = ("matmul_fp32", "matmul_bf16")
    raw = execute_script(capsys, monkeypatch, operations=operations)
    result = classify(raw, required_operations=operations)
    assert result["cause"] == "operation_failed"
    assert result["operations"] == {"matmul_fp32": True, "matmul_bf16": False}
    assert "matmul_bf16" in result["message"]


def test_selected_device_missing_never_runs_on_zero(torch_mock, capsys, monkeypatch):
    raw = execute_script(capsys, monkeypatch, device="cuda:1")
    result = classify(raw, device="cuda:1")
    assert result["cause"] == "device_unavailable"
    torch_mock.cuda.set_device.assert_not_called()
    torch_mock.matmul.assert_not_called()


def test_driver_read_failure_is_visible_not_invented(torch_mock, capsys, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi not found")

    monkeypatch.setattr(subprocess, "run", missing)
    raw = execute_script(capsys, monkeypatch)
    result = classify(raw)
    assert result["ok"]  # the numeric compute observation is independent of driver inventory
    assert result["driver_version"] is None
    assert result["driver_error"]


@pytest.mark.parametrize("cpu_only", [False, True])
def test_compute_source_without_device(torch_mock, capsys, monkeypatch, cpu_only):
    torch_mock.cuda.is_available.return_value = False
    if cpu_only:
        torch_mock.version.cuda = None
    result = execute_script(capsys, monkeypatch)
    assert not result["ok"]
    assert result["devices"] == []
    assert result["operations"] == {}
    assert result["error"] is None
    assert classify(result)["cause"] == ("cpu_only_wheel" if cpu_only else "no_device")
    torch_mock.matmul.assert_not_called()


def test_compute_source_missing_kernel(torch_mock, capsys, monkeypatch):
    message = "CUDA error: no kernel image is available for execution on the device"
    torch_mock.matmul.side_effect = RuntimeError(message)
    result = execute_script(capsys, monkeypatch)
    assert not result["ok"]
    assert result["operations"] == {"matmul_fp32": False}
    assert result["error"] == failed("matmul_fp32", message)
    assert classify(result)["cause"] == "wheel_arch_mismatch"
    torch_mock.cuda.synchronize.assert_not_called()
    torch_mock.nn.functional.conv2d.assert_not_called()


def test_compute_source_mismatch_and_truncated_error(torch_mock, capsys, monkeypatch):
    torch_mock.allclose.side_effect = [False, True, True, True, True]
    result = execute_script(capsys, monkeypatch)
    assert result["ok"] is False
    assert result["operations"] == {**dict.fromkeys(OPERATIONS, True), "matmul_fp32": False}
    assert classify(result)["cause"] == "operation_failed"
    torch_mock.matmul.side_effect = RuntimeError("x" * 400)
    result = execute_script(capsys, monkeypatch)
    assert result["error"]["message"] == "x" * 300
