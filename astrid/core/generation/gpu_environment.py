"""Windows NVIDIA candidate environments and their qualification label.

Provisioning supplies an interpreter to the existing pack/session admission;
it never starts an engine or grants model/device qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrid.core.generation.engine_recipe import load_recipe

# NOTICE: ASTRID_ALLOW_UNQUALIFIED_GPU is gone; it no longer changes anything and is read nowhere. Old settings files still load (astrid/installer/service.py SERVICE_SETTINGS).
ALLOW_UNQUALIFIED_ENV = "ASTRID_ALLOW_UNQUALIFIED_GPU"
# Add an exact (pack, OS, device name) only after testers qualify it.
QUALIFIED_COMBINATIONS: frozenset[tuple[str, str, str]] = frozenset()
_PACKS = Path(__file__).resolve().parents[2] / "packs"
_OFFLINE_COMFY_ACTIONS = frozenset({"import", "inspect", "edit", "validate"})
UV_VERSION = "0.11.32"


class GPUEnvironmentError(RuntimeError):
    """A candidate environment could not be provisioned or admitted."""


def gpu_pack(capability_id: str) -> str | None:
    if capability_id == "wan2gp.generate_video":
        return "wan2gp"
    pack, _, action = capability_id.partition(".")
    if pack == "vibecomfy" and action and action not in _OFFLINE_COMFY_ACTIONS:
        return "vibecomfy"
    if capability_id in _vibecomfy_gpu_aliases():
        return "vibecomfy"
    return None


def _vibecomfy_gpu_aliases() -> frozenset[str]:
    """Capability IDs the VibeComfy pack declares as GPU routes (its gpu-capabilities.json).

    A missing, unreadable or malformed declaration means no aliases: it must never abort
    preflight for unrelated capabilities.
    """
    try:
        aliases = json.loads((_PACKS / "vibecomfy" / "gpu-capabilities.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(aliases, list) or not all(isinstance(item, str) and item for item in aliases):
        return frozenset()
    return frozenset(aliases)


def detect_nvidia() -> dict[str, Any]:
    """Bounded driver probe; never import torch into the host interpreter."""
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return {"available": False, "devices": [], "reason": "No NVIDIA GPU was detected because nvidia-smi is not on PATH."}
    try:
        result = subprocess.run(
            [executable, "--query-gpu=name", "--format=csv,noheader"],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "devices": [], "reason": "The NVIDIA GPU could not be detected because nvidia-smi did not respond."}
    devices = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode or not devices:
        return {"available": False, "devices": [], "reason": "No NVIDIA GPU with a working driver was detected."}
    return {"available": True, "devices": devices, "reason": ""}


def windows_gpu_status(
    capability_id: str, *, platform: str | None = None,
    environ: Mapping[str, str] | None = None, detection: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a host preflight check, or leave the existing OS path untouched."""
    platform = sys.platform if platform is None else platform
    pack = gpu_pack(capability_id)
    if platform != "win32" or pack is None:
        return None
    device = dict(detect_nvidia() if detection is None else detection)
    names = device.get("devices", [])
    qualified = bool(device.get("available") and names) and all(
        (pack, platform, name) in QUALIFIED_COMBINATIONS for name in names
    )
    label = "Wan2GP" if pack == "wan2gp" else "ComfyUI"
    reason = "" if qualified else f"{label} on Windows with this NVIDIA GPU is not qualified yet."
    if not device.get("available"):
        reason = str(device["reason"])
    warning = ""
    if pack == "vibecomfy" and not device.get("available"):
        warning = "VibeComfy uses your own ComfyUI, so Astrid does not need to detect an NVIDIA GPU."
    elif device.get("available") and not qualified:
        warning = (
            f"{label} on Windows with this NVIDIA GPU is untested. It will probably work, "
            "but please tell us how it went (GPU model and driver from `nvidia-smi`)."
        )
    return {
        "ok": bool(pack == "vibecomfy" or device.get("available")),
        "qualified": qualified,
        "status": "qualified" if qualified else "not qualified yet",
        "reason": reason,
        "warning": warning,
        "device": device,
    }


def _description(pack: str) -> tuple[Path, dict[str, Any]]:
    if pack not in {"wan2gp", "vibecomfy"}:
        raise GPUEnvironmentError(f"unknown GPU pack: {pack}")
    directory = _PACKS / pack
    description = json.loads((directory / "environment.windows-nvidia.json").read_text(encoding="utf-8"))
    platform = "windows-x86_64-cu130" if pack == "vibecomfy" else "windows-x86_64-cu128"
    # The only registered engine for the VibeComfy pack is "comfyui" (R1.6).
    recipe = load_recipe("comfyui" if pack == "vibecomfy" else pack, platform)
    sources = {source.name: source.pin for source in recipe.sources}
    if pack == "vibecomfy":
        comfy = sources["comfyui"]
        vibecomfy = sources["vibecomfy"]
        description["source"] = {"repository": comfy.repository, "revision": comfy.revision}
        description["vibecomfy_source"] = {"repository": vibecomfy.repository,
                                             "revision": vibecomfy.revision}
        for node in description["custom_nodes"]:
            pin = sources[node.pop("pin")]
            node.update(repository=pin.repository, revision=pin.revision)
    else:
        pin = sources["wan2gp"]
        description["source"] = {"repository": pin.repository, "revision": pin.revision}
    return directory, description


def _unmanaged_guidance(pack: str, platform: str) -> str:
    platform_name = {"win32": "Windows", "linux": "Linux", "darwin": "macOS"}.get(platform, platform)
    if pack == "vibecomfy":
        return (
            f"Astrid doesn't install ComfyUI on {platform_name} yet. Run your own ComfyUI, "
            "then pass `comfy_url` (find it with `astrid generate scan`)."
        )
    return (
        f"Astrid doesn't provision Wan2GP environments on {platform_name} yet. "
        "Use an admitted Wan2GP checkout and interpreter."
    )


def environment_plan(
    pack: str, checkout: Path, *, destination: Path | None = None,
    vibecomfy_checkout: Path | None = None, platform: str | None = None,
    environ: Mapping[str, str] | None = None, uv: str = "uv",
) -> dict[str, Any]:
    """Render argv without installing, detecting hardware, or changing admission."""
    platform = sys.platform if platform is None else platform
    if platform != "win32":
        return {"pack": pack, "platform": platform, "managed": False, "commands": [],
                "reason": _unmanaged_guidance(pack, platform)}
    directory, description = _description(pack)
    env = os.environ if environ is None else environ
    identity = hashlib.sha256(
        (UV_VERSION + json.dumps(description, sort_keys=True) + (directory / "requirements.windows-nvidia.txt").read_text(encoding="utf-8")).encode()
    ).hexdigest()[:16]
    local = Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    root = destination if destination is not None else local / "Astrid GPU" / pack / identity
    python = root / "Scripts" / "python.exe"
    lock = root / "requirements.lock"
    sources = [{"path": str(checkout), **description["source"]}]
    requirements = [directory / "requirements.windows-nvidia.txt", checkout / "requirements.txt"]
    if pack == "vibecomfy":
        if vibecomfy_checkout is None:
            raise GPUEnvironmentError("ComfyUI provisioning requires the admitted VibeComfy checkout")
        sources.append({"path": str(vibecomfy_checkout), **description["vibecomfy_source"]})
        requirements.append(vibecomfy_checkout / "pyproject.toml")
        for node in description["custom_nodes"]:
            path = checkout / "custom_nodes" / node["name"]
            sources.append({"path": str(path), **node})
            requirements.append(path / "requirements.txt")
    # CUDA wheels are exact official URLs in the pack requirements. Resolve
    # supporting dependencies only from the dated PyPI index.
    indexes = ["--index-url", "https://pypi.org/simple"]
    compile_argv = [uv, "--no-config", "pip", "compile", "--python", str(python), "--python-platform", "x86_64-pc-windows-msvc", "--no-sources",
                    "--generate-hashes", "--only-binary", ":all:", "--exclude-newer", description["exclude_newer"],
                    *indexes,
                    "--output-file", str(lock), *map(str, requirements)]
    if pack == "vibecomfy":
        compile_argv.append(str(root / "build-requirements.in"))
    commands = [
        [uv, "--no-config", "venv", "--python", description["python"], "--managed-python", str(root)],
        compile_argv,
        [uv, "--no-config", "pip", "sync", "--python", str(python), *indexes, "--only-binary", ":all:", "--require-hashes", str(lock)],
    ]
    if vibecomfy_checkout is not None and pack == "vibecomfy":
        # The selected source pin is already the dependency identity checked by
        # VibeComfy admission. Do not substitute an ambient PyPI package.
        commands.append([uv, "--no-config", "pip", "install", "--python", str(python), "--no-deps", "--no-build-isolation", str(vibecomfy_checkout)])
    commands.extend([
        [uv, "--no-config", "pip", "check", "--python", str(python)],
        [str(python), "-X", "utf8", "-I", "-c", _TORCH_PROBE, json.dumps(description["torch"], sort_keys=True)],
    ])
    if pack == "vibecomfy":
        commands.append([str(python), "-X", "utf8", "-I", "-c", "import cv2"])
    return {"pack": pack, "platform": platform, "managed": True, "qualified": False,
            "status": "not qualified yet", "root": str(root), "python": str(python),
            "checkout": str(checkout), "sources": sources, "requirements": list(map(str, requirements)),
            "commands": commands, "lock": str(lock), "description": description,
            "uv_version": UV_VERSION}


_TORCH_PROBE = """import json,sys
import torch,torchvision,torchaudio
expected=json.loads(sys.argv[1])
actual={name:module.__version__ for name,module in [('torch',torch),('torchvision',torchvision),('torchaudio',torchaudio)]}
if actual != expected: raise SystemExit('CUDA wheel versions differ: '+repr(actual))
tag=expected['torch'].split('+cu')[1]
if torch.version.cuda != tag[:-1]+'.'+tag[-1]: raise SystemExit('CUDA runtime version differs: '+str(torch.version.cuda))
if not torch.cuda.is_available(): raise SystemExit('No NVIDIA GPU is usable by the provisioned CUDA wheels.')
print(json.dumps({'packages':actual,'cuda':torch.version.cuda,'devices':[torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())],'qualified':False}))
"""


COMPUTE_SCHEMA = "astrid.compute-probe.v1"
# The caller selects a bounded set of concrete operator/dtype pairs. No
# arbitrary code, blanket BF16 requirement, or architecture-name admission.
_COMPUTE_CASES = {
    "matmul": (
        ([[1., 2.], [3., 4.]], [[2., 1.], [0., 3.]]),
        [[2., 7.], [6., 15.]],
    ),
    "conv2d": (
        ([[[[1., 2., 3.], [4., 5., 6.], [7., 8., 9.]]]],
         [[[[1., 0.], [0., -1.]]]]),
        [[[[-4., -4.], [-4., -4.]]]],
    ),
    "softmax": (
        ([[1., 2., 3.]],),
        [[0.0900305732, 0.2447284711, 0.6652409558]],
    ),
}
_COMPUTE_DTYPES = {
    "fp32": ("float32", 1e-5),
    "fp16": ("float16", 1e-2),
    "bf16": ("bfloat16", 3e-2),
}
COMPUTE_OPERATIONS = frozenset(
    f"{operator}_{dtype}" for operator in _COMPUTE_CASES for dtype in _COMPUTE_DTYPES
)

COMPUTE_PROBE_SOURCE = (
    "CASES = " + repr(_COMPUTE_CASES) + "\n"
    "DTYPES = " + repr(_COMPUTE_DTYPES) + "\n"
    """import contextlib
import importlib.metadata
import json
import os
import subprocess
import sys

request = json.loads(sys.argv[1])
result = {
    "schema": "astrid.compute-probe.v1", "ok": False,
    "interpreter": sys.executable, "backend": request["backend"],
    "device": request["device"], "required_operations": request["required_operations"],
    "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
    "cuda_device_order": os.environ.get("CUDA_DEVICE_ORDER"),
    "torch": None, "cuda": None, "hip": None, "devices": [], "arch_list": [],
    "device_inventory_errors": [],
    "driver_version": None, "driver_error": None, "wheel_versions": {},
    "operations": {}, "numeric_outputs": {}, "error": None,
}
stage = "import"
try:
    with contextlib.redirect_stdout(sys.stderr):
        import torch

        result["torch"] = str(torch.__version__)
        result["wheel_versions"]["torch"] = result["torch"]
        for package in ("torchvision", "torchaudio"):
            try:
                result["wheel_versions"][package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                pass
        stage = "cuda"
        result["cuda"] = torch.version.cuda
        result["hip"] = getattr(torch.version, "hip", None)
        # nvidia-smi reports the installed NVIDIA driver, not the wheel's
        # CUDA runtime. Do not equate its physical ordinal with a CUDA-visible
        # logical ordinal. Accept a single consistent host driver version.
        try:
            driver = subprocess.run(
                ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                check=False, timeout=5,
            )
            versions = {line.strip() for line in driver.stdout.splitlines() if line.strip()}
            if not driver.returncode and len(versions) == 1:
                version = next(iter(versions))
                if all(part.isdecimal() for part in version.split(".")) and "." in version:
                    result["driver_version"] = version
            if result["driver_version"] is None:
                result["driver_error"] = "NVIDIA driver version could not be established."
        except (OSError, subprocess.SubprocessError):
            result["driver_error"] = "NVIDIA driver version could not be read."

        if result["cuda"] is not None and result["hip"] is None and torch.cuda.is_available():
            selected_index = int(request["device"].split(":")[1])
            for index in range(torch.cuda.device_count()):
                try:
                    result["devices"].append({
                        "index": index,
                        "name": torch.cuda.get_device_name(index),
                        "capability": list(torch.cuda.get_device_capability(index)),
                    })
                except Exception as exc:
                    if index == selected_index:
                        stage = "device"
                        raise
                    result["device_inventory_errors"].append({
                        "index": index, "message": str(exc)[:300],
                    })
            result["arch_list"] = torch.cuda.get_arch_list()
            if any(item["index"] == selected_index for item in result["devices"]):
                stage = "device"
                device = request["device"]
                # The isolated child has no authority to choose a different GPU.
                torch.cuda.set_device(selected_index)
                for stage in request["required_operations"]:
                    result["operations"][stage] = False
                    operator, dtype_key = stage.rsplit("_", 1)
                    dtype_name, tolerance = DTYPES[dtype_key]
                    dtype = getattr(torch, dtype_name)
                    values, expected_values = CASES[operator]
                    inputs = (torch.tensor(value, dtype=torch.float32).to(
                        device=device, dtype=dtype,
                    ) for value in values)
                    if operator == "matmul":
                        actual = torch.matmul(*inputs)
                    elif operator == "conv2d":
                        actual = torch.nn.functional.conv2d(*inputs)
                    else:
                        actual = torch.softmax(*inputs, dim=-1)
                    if str(actual.device) != device or actual.dtype != dtype:
                        raise RuntimeError("The operation returned output on a different device or dtype.")
                    torch.cuda.synchronize(device)
                    observed = actual.cpu().float()
                    result["numeric_outputs"][stage] = observed.tolist()
                    expected = torch.tensor(expected_values, dtype=torch.float32)
                    result["operations"][stage] = bool(torch.allclose(
                        observed, expected, rtol=tolerance, atol=tolerance,
                    ))
                result["ok"] = all(result["operations"].values())
except Exception as exc:
    result["error"] = {
        "stage": stage, "type": type(exc).__name__, "message": str(exc)[:300],
    }
print(json.dumps(result))
"""
)


def _compute_request(python, device, backend, required_operations):
    """Validate caller-owned selection before starting any process."""
    try:
        python = os.fspath(python)
    except TypeError:
        python = None
    if not isinstance(python, str) or not os.path.isabs(python) or "\0" in python:
        return None, "interpreter_required", "The selected local Python interpreter must be an absolute path."
    if backend != "cuda":
        return None, "unsupported_backend", "This compute probe supports NVIDIA CUDA only."
    suffix = device[5:] if isinstance(device, str) and device.startswith("cuda:") else ""
    if (not suffix.isascii() or not suffix.isdecimal() or len(suffix) > 9
            or str(int(suffix)) != suffix):
        return None, "device_required", "Select an explicit CUDA device (cuda:<visible index>)."
    if (not isinstance(required_operations, (list, tuple)) or not required_operations
            or any(not isinstance(name, str) or name not in COMPUTE_OPERATIONS
                   for name in required_operations)
            or len(set(required_operations)) != len(required_operations)):
        return None, "operations_required", "Specify the workflow's supported operator/dtype pairs."
    return {
        "interpreter": python, "device": device, "backend": backend,
        "required_operations": list(required_operations),
    }, None, ""


def _numeric_matches(actual, expected, tolerance):
    """Verify the synchronized numeric receipt, including its shape."""
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(_numeric_matches(a, e, tolerance) for a, e in zip(actual, expected)))
    if type(actual) not in (int, float):
        return False
    try:
        return math.isfinite(actual) and abs(actual - expected) <= tolerance * (1 + abs(expected))
    except OverflowError:
        return False


def classify_compute(
    raw, *, python=None, device=None, backend="cuda", required_operations=None,
    returncode=0, stderr="", timed_out=False, environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Classify evidence against the exact request, never against another GPU."""
    request, selection_cause, selection_message = _compute_request(
        python, device, backend, required_operations,
    )
    environment = os.environ if environ is None else environ
    valid = isinstance(raw, dict) and raw.get("schema") == COMPUTE_SCHEMA
    report = raw if valid else {}
    expected = request["required_operations"] if request else []
    interpreter = report.get("interpreter")
    interpreter = interpreter if isinstance(interpreter, str) else None
    torch_version = report.get("torch")
    torch_version = torch_version if isinstance(torch_version, str) else None
    cuda = report.get("cuda")
    cuda = cuda if isinstance(cuda, str) else None
    raw_devices = report.get("devices")
    devices = [
        item for item in raw_devices
        if isinstance(item, dict) and type(item.get("index")) is int
        and isinstance(item.get("name"), str)
    ] if isinstance(raw_devices, list) else []
    names = [item["name"] for item in devices]
    index = int(request["device"].split(":")[1]) if request else None
    matches = [item for item in devices if item["index"] == index]
    selected = matches[0] if len(matches) == 1 else None
    raw_arches = report.get("arch_list")
    arches = [item for item in raw_arches if isinstance(item, str)] if isinstance(raw_arches, list) else []
    raw_operations = report.get("operations")
    operations = {
        name: value for name, value in raw_operations.items()
        if name in expected and isinstance(value, bool)
    } if isinstance(raw_operations, dict) else {}
    raw_numeric = report.get("numeric_outputs")
    numeric = {name: raw_numeric[name] for name in expected if name in raw_numeric} if isinstance(raw_numeric, dict) else {}
    for name, passed in operations.items():
        operator, dtype = name.rsplit("_", 1)
        operations[name] = passed and _numeric_matches(
            numeric.get(name), _COMPUTE_CASES[operator][1], _COMPUTE_DTYPES[dtype][1],
        )
    raw_wheels = report.get("wheel_versions")
    wheels = {key: value for key, value in raw_wheels.items()
              if key in ("torch", "torchvision", "torchaudio") and isinstance(value, str)} if isinstance(raw_wheels, dict) else {}
    driver = report.get("driver_version")
    driver = driver if isinstance(driver, str) and driver else None
    driver_error = report.get("driver_error")
    driver_error = driver_error if isinstance(driver_error, str) else None
    error = report.get("error")
    error = error if isinstance(error, dict) else {}
    stage = error.get("stage")
    stage = stage if isinstance(stage, str) else ""
    detail = error.get("message")
    detail = detail if isinstance(detail, str) else ""
    error_type = error.get("type")
    def diagnostic_cause(value):
        value = value.lower()
        if "out of memory" in value or "outofmemoryerror" in value:
            return "out_of_memory"
        if "driver" in value and ("too old" in value or "insufficient" in value):
            return "driver_too_old"
        if "no kernel image" in value or "invalid device function" in value:
            return "wheel_arch_mismatch"
        return None

    # Prefer the actual selected operation's exception over ambient warnings.
    diagnostic = diagnostic_cause(" ".join((detail, error_type if isinstance(error_type, str) else "")))
    if diagnostic is None and isinstance(stderr, str):
        diagnostic = diagnostic_cause(stderr)
    capability = selected.get("capability") if selected else None
    architecture = ""
    if (isinstance(capability, (tuple, list)) and len(capability) == 2
            and all(type(part) is int and part >= 0 for part in capability)):
        architecture = f"sm_{capability[0]}{capability[1]}"
    bound = bool(
        request and interpreter and os.path.isabs(interpreter)
        and os.path.normcase(os.path.normpath(interpreter))
        == os.path.normcase(os.path.normpath(request["interpreter"]))
        and report.get("device") == request["device"]
        and report.get("backend") == request["backend"]
        and report.get("required_operations") == expected
        and report.get("cuda_visible_devices") == environment.get("CUDA_VISIBLE_DEVICES")
        and report.get("cuda_device_order") == environment.get("CUDA_DEVICE_ORDER")
    )
    version = torch_version or "unknown"
    gpu_name = selected["name"] if selected else (request["device"] if request else "selected GPU")

    if selection_cause:
        cause = selection_cause
        message = selection_message
        works = "Resolve the selected interpreter, CUDA device and required workflow operations before probing."
    elif timed_out:
        cause = "timed_out"
        message = "The CUDA compute probe timed out before it could finish."
        works = "Try a responsive interpreter with a working GPU driver."
    elif returncode != 0 or not valid:
        cause = "probe_failed"
        message = (f"The CUDA compute probe exited with code {returncode}."
                   if returncode != 0 else "The interpreter did not return a valid CUDA compute report.")
        works = "Use a working Python interpreter and rerun the CUDA compute probe."
    elif not bound:
        cause = "selection_mismatch"
        message = "The CUDA compute report does not match the selected interpreter, device or workflow operations."
        works = "Rerun the probe for the exact selected local environment."
    elif stage == "import":
        cause = "no_torch"
        message = "PyTorch could not be imported by this interpreter."
        works = "Use an interpreter with a compatible CUDA-enabled PyTorch installation."
    elif report.get("hip") is not None:
        cause = "unsupported_backend"
        message = "This PyTorch uses a non-NVIDIA backend; this probe cannot qualify it."
        works = "Use the supported backend-specific readiness path."
    elif torch_version is not None and report.get("cuda") is None:
        cause = "cpu_only_wheel"
        message = f"This PyTorch ({version}) was built without CUDA support."
        works = "Use a CUDA-enabled PyTorch build for your GPU."
    elif (report.get("ok") is True and report.get("error") is None
          and torch_version and wheels.get("torch") == torch_version and cuda
          and selected and architecture and expected
          and all(operations.get(name) is True for name in expected)):
        cause = None
        message = f"PyTorch ({version}, CUDA {cuda}) passed the {len(expected)} required compute checks on {gpu_name} ({device})."
        works = ""
    elif diagnostic == "driver_too_old":
        cause = "driver_too_old"
        message = f"The NVIDIA driver is too old for PyTorch ({version}, CUDA {cuda or 'unknown'})."
        works = "Update the NVIDIA driver to one that supports this CUDA build."
    elif diagnostic == "out_of_memory":
        cause = "out_of_memory"
        message = f"PyTorch ({version}) ran out of GPU memory during {stage or 'the compute probe'} on {gpu_name}."
        works = "Free GPU memory or use a GPU with more available memory."
    elif cuda is not None and not devices:
        cause = "no_device"
        message = f"PyTorch ({version}, CUDA {cuda}) cannot see a usable NVIDIA GPU."
        works = "Use a machine with an accessible NVIDIA GPU and a working driver."
    elif cuda is not None and not selected:
        cause = "device_unavailable"
        message = f"The selected CUDA device ({device}) is not available to PyTorch ({version})."
        works = "Select a visible CUDA device in the same interpreter environment."
    elif diagnostic == "wheel_arch_mismatch":
        # Architecture lists are advisory: compatible cubins or PTX can
        # compute without an exact SM entry. A real kernel error is decisive.
        cause = "wheel_arch_mismatch"
        target = f"{gpu_name} ({architecture})" if architecture else gpu_name
        message = f"This PyTorch ({version}) has no usable kernels for your GPU ({target})."
        works = ("Use a PyTorch built for CUDA 12.8 or newer; Astrid's separate ComfyUI install includes one."
                 if architecture == "sm_120" else "Use a PyTorch build that includes kernels for your GPU architecture.")
    else:
        cause = "operation_failed"
        operation = stage or next((name for name in expected if operations.get(name) is not True), "compute probe")
        message = f"PyTorch ({version}) failed the {operation} check on {gpu_name}."
        works = "Use a PyTorch installation that supports the required workflow operations and dtypes on your GPU."
    return {
        "schema": COMPUTE_SCHEMA, "ok": cause is None, "cause": cause,
        "message": message, "works": works, "torch": torch_version, "cuda": cuda,
        "devices": names, "operations": operations,
        "interpreter": interpreter, "requested_interpreter": request["interpreter"] if request else None,
        "backend": backend, "device": device, "selected_device": selected,
        "required_operations": expected, "numeric_outputs": numeric,
        "arch_list": arches, "wheel_versions": wheels, "driver_version": driver,
        "driver_error": driver_error,
        "cuda_visible_devices": report.get("cuda_visible_devices"),
        "cuda_device_order": report.get("cuda_device_order"),
        "device_inventory_errors": report.get("device_inventory_errors", []),
    }


def probe_compute(
    python, *, device=None, backend="cuda", required_operations=None,
    run=subprocess.run, timeout=120.0, environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Probe the explicitly selected local interpreter/device, not a fallback.

    Pass the selected engine's environment when its visibility or library
    setup differs from the caller's. Non-CUDA paths remain with their owner.
    """
    context = dict(python=python, device=device, backend=backend,
                   required_operations=required_operations)
    request, _, _ = _compute_request(python, device, backend, required_operations)
    if request is None:
        return classify_compute(None, **context)
    environment = {
        key: value for key, value in (os.environ if environ is None else environ).items()
        if key.upper() not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    context["environ"] = environment
    try:
        result = run(
            [request["interpreter"], "-I", "-c", COMPUTE_PROBE_SOURCE, json.dumps(request)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            check=False, timeout=timeout, env=environment, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return classify_compute(None, timed_out=True, **context)
    except OSError:
        diagnosis = classify_compute(None, **context)
        diagnosis["message"] = f"The Python interpreter ({python}) could not be started."
        return diagnosis
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    lines = [line for line in stdout.splitlines() if line.strip()]
    try:
        raw = json.loads(lines[-1]) if lines else None
    except (ValueError, RecursionError):
        raw = None
    return classify_compute(raw, returncode=result.returncode, stderr=result.stderr, **context)


def _verify_sources(plan: Mapping[str, Any]) -> None:
    # Use the same bounded Git inventory operation as existing pack setup.
    from astrid.core.pack.source_setup import _run_git

    for source in plan["sources"]:
        root = Path(source["path"])
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise GPUEnvironmentError(f"GPU source must be an admitted absolute checkout: {root}")
        revision = _run_git("", "-C", str(root), "rev-parse", "HEAD")
        if revision != source["revision"]:
            raise GPUEnvironmentError(f"GPU source revision differs from the pin: {root}")
        if _run_git("", "-C", str(root), "status", "--porcelain", "--untracked-files=all"):
            raise GPUEnvironmentError(f"GPU source is not clean: {root}")
    for raw in plan["requirements"]:
        if not Path(raw).is_file():
            raise GPUEnvironmentError(f"Pinned GPU requirements are missing: {raw}")
    if plan["pack"] == "vibecomfy":
        vibe_root = Path(plan["sources"][1]["path"])
        try:
            lock = tomllib.loads((vibe_root / "custom_nodes.lock").read_text(encoding="utf-8"))
            for node in plan["description"]["custom_nodes"]:
                entry = lock["nodepacks"][node["name"]]
                if entry.get("commit", entry.get("git_commit_sha")) != node["revision"]:
                    raise GPUEnvironmentError(f"VibeComfy custom-node lock differs for {node['name']}")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise GPUEnvironmentError("The admitted VibeComfy custom_nodes.lock is missing or incompatible") from exc
        try:
            build = tomllib.loads((vibe_root / "pyproject.toml").read_text(encoding="utf-8"))["build-system"]["requires"]
            if not isinstance(build, list) or any(not isinstance(item, str) or not item.strip() for item in build):
                raise ValueError("invalid build requirements")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise GPUEnvironmentError("The admitted VibeComfy build requirements are missing or incompatible") from exc


def provision_environment(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Create a user-local venv; return paths for the existing session owner."""
    if not plan.get("managed") or sys.platform != "win32":
        pack = str(plan.get("pack", "vibecomfy"))
        platform = sys.platform if sys.platform != "win32" else str(plan.get("platform", sys.platform))
        raise GPUEnvironmentError(_unmanaged_guidance(pack, platform))
    _verify_sources(plan)
    device = detect_nvidia()
    if not device["available"]:
        raise GPUEnvironmentError(device["reason"])
    root = Path(plan["root"])
    # A failed install is never reused as an admitted environment. Refuse to
    # overwrite any existing interpreter, including one held by a warm server.
    if root.exists():
        raise GPUEnvironmentError(f"GPU environment already exists; select a new destination: {root}")
    # Keep index/solver selection independent of an ambient pip/uv config.
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("UV_", "PIP_"))
                   and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
    try:
        version = subprocess.run([plan["commands"][0][0], "--version"], check=True,
                                 capture_output=True, text=True, encoding="utf-8", env=environment, timeout=10)
        if version.stdout.split()[:2] != ["uv", UV_VERSION]:
            raise GPUEnvironmentError(f"GPU environment resolution requires uv {UV_VERSION}")
    except (OSError, subprocess.SubprocessError) as exc:
        raise GPUEnvironmentError(f"GPU environment resolution requires uv {UV_VERSION}") from exc
    root.parent.mkdir(parents=True, exist_ok=True)
    for index, argv in enumerate(plan["commands"]):
        if index == 1 and plan["pack"] == "vibecomfy":
            project = Path(plan["sources"][1]["path"]) / "pyproject.toml"
            build = tomllib.loads(project.read_text(encoding="utf-8"))["build-system"]["requires"]
            (root / "build-requirements.in").write_text("\n".join(build) + "\n", encoding="utf-8")
        try:
            subprocess.run(argv, check=True, env=environment)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GPUEnvironmentError(f"GPU provisioning failed at {argv[0]}; environment is not admitted: {root}") from exc
    receipt = {key: plan[key] for key in ("pack", "python", "checkout", "sources", "status", "qualified")}
    receipt["lock_sha256"] = hashlib.sha256(Path(plan["lock"]).read_bytes()).hexdigest()
    receipt["device"] = device
    receipt["uv_version"] = UV_VERSION
    (root / "astrid-gpu-environment.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack", choices=("wan2gp", "vibecomfy"))
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--vibecomfy-checkout", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--plan", action="store_true", help="Print the plan without installing")
    args = parser.parse_args(argv)
    try:
        plan = environment_plan(args.pack, args.checkout, destination=args.destination,
                                vibecomfy_checkout=args.vibecomfy_checkout)
        result = plan if args.plan else provision_environment(plan)
    except GPUEnvironmentError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
