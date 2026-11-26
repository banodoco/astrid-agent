"""Read-only consent offers for official Windows ComfyUI Portable.

The Astrid engine recipe is not a Windows installer: Portable avoids the
Linux-only runner, custody, locking and endpoint-root assumptions and the
network-dependent recipe build. No archive is fetched or unpacked here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

PIN_PATH = Path(__file__).resolve().parents[2] / "packs" / "vibecomfy" / "portable.pin.json"
MARGIN_BYTES = 1 << 30
_PIN_SCHEMA = "astrid.comfy-portable-pin.v1"
_OFFER_SCHEMA = "astrid.comfy-install-offer.v1"
_JOURNAL_SCHEMA = "astrid.comfy-install-journal.v1"
_REQUIRED = {"python_embeded/python.exe", "ComfyUI/main.py"}
_SUPPORTED = "a computer with an NVIDIA RTX 20, 30, 40 or 50 series card, or your own running ComfyUI"


class PortableError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _require(condition, field):
    if not condition:
        raise PortableError("pin_invalid", f"Invalid Portable pin field: {field}")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _relative(value):
    if not _text(value) or "\\" in value or ":" in value or "\0" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and all(part not in ("", ".", "..") for part in value.split("/"))
        and not PureWindowsPath(value).drive
    )


def _positive_int(value):
    return type(value) is int and value > 0


def _validate_pin(pin):
    _require(isinstance(pin, dict), "root")
    _require(pin.get("schema") == _PIN_SCHEMA, "schema")
    _require(pin.get("platform") == "win32", "platform")
    _require(_text(pin.get("note")), "note")
    patterns = pin.get("supported_gpu_patterns")
    _require(isinstance(patterns, list) and bool(patterns), "supported_gpu_patterns")
    for pattern in patterns:
        _require(_text(pattern), "supported_gpu_patterns")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise PortableError("pin_invalid", "Invalid GPU pattern in Portable pin") from exc
    _require("release" in pin, "release")
    release = pin["release"]
    if release is None:
        return pin
    _require(isinstance(release, dict), "release")
    _require(_text(release.get("tag")), "release.tag")
    url = release.get("url")
    _require(_text(url), "release.url")
    try:
        parts = urlsplit(url)
        _require(parts.scheme == "https" and bool(parts.hostname), "release.url")
    except ValueError as exc:
        raise PortableError("pin_invalid", "Invalid Portable release URL") from exc
    _require(release.get("archive_format") in ("7z", "zip"), "release.archive_format")
    name = release.get("archive_name")
    _require(_relative(name) and "/" not in name, "release.archive_name")
    for field in ("archive_bytes", "extracted_bytes", "estimated_minutes"):
        _require(_positive_int(release.get(field)), f"release.{field}")
    digest = release.get("archive_sha256")
    _require(
        isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest) is not None,
        "release.archive_sha256",
    )
    for field in ("inner_directory", "start_script"):
        _require(_relative(release.get(field)), f"release.{field}")
    required = release.get("required_files")
    _require(
        isinstance(required, list)
        and all(_relative(item) for item in required)
        and _REQUIRED.issubset(required),
        "release.required_files",
    )
    args = release.get("launch_args")
    _require(
        isinstance(args, list) and all(isinstance(arg, str) and "\0" not in arg for arg in args),
        "release.launch_args",
    )
    return pin


def load_pin(path=PIN_PATH):
    """Read and validate the release descriptor without performing any setup."""
    try:
        pin = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PortableError("pin_invalid", f"Cannot read Portable pin: {path}") from exc
    return _validate_pin(pin)


def peak_bytes(release):
    archive = release["archive_bytes"]
    return max(2 * archive, archive + release["extracted_bytes"]) + MARGIN_BYTES


def default_destination(environ, platform):
    """Portable's user-local destination; no directories are created."""
    home = Path(environ.get("USERPROFILE") or environ.get("HOME") or Path.home())
    local = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    return local / "Astrid" / "ComfyUI"


def destination_state(destination):
    destination = Path(destination)
    if destination.is_symlink():
        return "foreign"
    if not destination.exists():
        return "absent"
    if not destination.is_dir():
        return "foreign"
    journal = destination / ".astrid-install" / "journal.json"
    try:
        if not any(destination.iterdir()):
            return "empty"
        record = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "foreign"
    if isinstance(record, dict) and record.get("schema") == _JOURNAL_SCHEMA:
        return "ours"
    return "foreign"


def build_offer(destination, *, pin=None, platform=None, gpu=None,
                disk_usage=shutil.disk_usage, environ=None):
    """Construct a consent offer or the first applicable refusal. Never write."""
    pin = load_pin() if pin is None else _validate_pin(pin)
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    destination = Path(destination) if destination is not None else default_destination(environ, platform)
    release = pin["release"]
    peak = peak_bytes(release) if release is not None else None
    offer = {
        "schema": _OFFER_SCHEMA,
        "status": "offered",
        "refusal": None,
        "destination": str(destination),
        "release": release["tag"] if release is not None else None,
        "download_bytes": release["archive_bytes"] if release is not None else None,
        "installed_bytes": release["extracted_bytes"] if release is not None else None,
        "peak_bytes": peak,
        "free_bytes": None,
        "minutes": release["estimated_minutes"] if release is not None else None,
        "cost_usd": 0,
        "state": None,
        "offer_sha256": None,
    }
    if gpu is None and platform == "win32":
        from astrid.core.generation import gpu_environment

        gpu = gpu_environment.detect_nvidia()
    devices = list(gpu.get("devices", [])) if gpu is not None else []
    identity = {
        "destination": str(destination),
        "release": offer["release"],
        "archive_sha256": release["archive_sha256"] if release is not None else None,
        "platform": platform,
        "peak_bytes": peak,
        "gpu_devices": devices,
    }
    offer["offer_sha256"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    def refuse(code, message, works):
        offer["status"] = "refused"
        offer["refusal"] = {"code": code, "message": message, "works": works}
        return offer

    if platform != "win32":
        works = (
            "astrid engines install comfyui --platform linux-x86_64-cu130, or your own running ComfyUI"
            if platform == "linux" else "Use your own running ComfyUI."
        )
        return refuse("unsupported_platform", "ComfyUI Portable is a Windows install.", works)
    if not gpu or not gpu.get("available") or not devices:
        return refuse("no_gpu", "No usable NVIDIA GPU was detected.", _SUPPORTED)
    unsupported = [
        name for name in devices
        if not any(re.search(pattern, name) for pattern in pin["supported_gpu_patterns"])
    ]
    if unsupported:
        return refuse(
            "unsupported_gpu",
            f"Unsupported NVIDIA GPU: {', '.join(unsupported)}. Supported series: RTX 20, 30, 40 or 50.",
            _SUPPORTED,
        )
    if release is None:
        return refuse(
            "no_verified_release",
            "No verified official ComfyUI Portable release is pinned.",
            "Use your own running ComfyUI, or wait for a verified official Portable release.",
        )
    if not destination.is_absolute() or destination.is_file():
        return refuse(
            "destination_invalid",
            f"The destination must be an absolute folder path, not a file: {destination}",
            "Choose an absolute, unused folder.",
        )
    offer["state"] = destination_state(destination)
    if offer["state"] == "foreign":
        return refuse(
            "destination_occupied",
            f"A different folder is already there: {destination}. Nothing was touched.",
            "Choose a different, empty or absent folder.",
        )
    ancestor = destination
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        return refuse("destination_invalid", f"Not a folder: {ancestor}", "Choose an absolute, unused folder.")
    offer["free_bytes"] = disk_usage(ancestor).free
    if offer["free_bytes"] < peak:
        return refuse(
            "insufficient_space",
            f"Only {offer['free_bytes']} bytes are free; {peak} bytes are needed at the peak.",
            "Free enough disk space or choose a folder on a drive with enough space.",
        )
    return offer


def render_offer(offer):
    if offer["status"] == "refused":
        refusal = offer["refusal"]
        return f"{refusal['message']}\nWhat works: {refusal['works']}\nNothing was written."
    destination = offer["destination"]
    return (
        f"No working ComfyUI here. I can install one in {destination}: about "
        f"{offer['download_bytes'] / 1e9:.1f} GB to download, "
        f"{offer['peak_bytes'] / 1e9:.1f} GB of free disk needed at the peak, "
        f"about {offer['minutes']} minutes, nothing else on your computer touched, no cost. "
        "It becomes the ComfyUI Astrid uses, and your current choice is kept so you can switch back. Go ahead?\n"
        f"To confirm: astrid generate install-comfyui --destination {destination} --apply {offer['offer_sha256']}"
    )
