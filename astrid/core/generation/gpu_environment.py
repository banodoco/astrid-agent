"""Windows NVIDIA candidate environments and their qualification label.

Provisioning supplies an interpreter to the existing pack/session admission;
it never starts an engine or grants model/device qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION

# No longer changes anything; still accepted in service-env.json so old
# settings files load (astrid/installer/service.py SERVICE_SETTINGS).
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
    if pack == "comfy_wrap" and action == "run":
        return "vibecomfy"
    if pack == "vibecomfy" and action and action not in _OFFLINE_COMFY_ACTIONS:
        return "vibecomfy"
    return None


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
    if pack == "vibecomfy":
        description["vibecomfy_source"]["revision"] = VIBECOMFY_ENGINE_REVISION
    return directory, description


def environment_plan(
    pack: str, checkout: Path, *, destination: Path | None = None,
    vibecomfy_checkout: Path | None = None, platform: str | None = None,
    environ: Mapping[str, str] | None = None, uv: str = "uv",
) -> dict[str, Any]:
    """Render argv without installing, detecting hardware, or changing admission."""
    platform = sys.platform if platform is None else platform
    if platform != "win32":
        return {"pack": pack, "platform": platform, "managed": False, "commands": [],
                "reason": "Use the existing admitted checkout and interpreter on this OS."}
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
        raise GPUEnvironmentError("Managed GPU provisioning is currently for native Windows only")
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
