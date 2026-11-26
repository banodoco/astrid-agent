"""Consent offers and resumable installs for Windows ComfyUI Portable.

The Astrid engine recipe is not a Windows installer: Portable avoids the
Linux-only runner, custody, locking and endpoint-root assumptions and the
network-dependent recipe build. Download and extraction are injected steps;
this module owns consent, the journal and the destination's write boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Literal, TypedDict
from urllib.parse import urlsplit

from filelock import FileLock, Timeout

PIN_PATH = Path(__file__).resolve().parents[2] / "packs" / "vibecomfy" / "portable.pin.json"
MARGIN_BYTES = 1 << 30
_PIN_SCHEMA = "astrid.comfy-portable-pin.v1"
_OFFER_SCHEMA = "astrid.comfy-install-offer.v1"
_JOURNAL_SCHEMA = "astrid.comfy-install-journal.v1"
_REQUIRED = {"python_embeded/python.exe", "ComfyUI/main.py"}
_SUPPORTED = "a computer with an NVIDIA RTX 20, 30, 40 or 50 series card, or your own running ComfyUI"


class PortableError(RuntimeError):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


Phase = Literal["started", "downloaded", "extracted", "complete"]
Progress = Callable[[str, int, int], None]


@dataclass(frozen=True)
class Steps:
    """The transfer, extraction and verification operations of an install."""

    download: Callable[[dict, Path, Path, Progress | None], None]
    extract: Callable[[Path, Path, dict], None]
    missing: Callable[[Path, dict], list[str]]


class InstallResult(TypedDict):
    status: Literal["installed", "already_installed"]
    resumed_from: Phase | None
    root: str
    comfy_dir: str
    python: str
    release: str
    kept_previous: str | None


def _write_json(path, record):
    """Publish a durable JSON record using a temporary file beside it."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_journal(path, record):
    _write_json(path, record)


def _read_journal(destination):
    path = destination / ".astrid-install" / "journal.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise PortableError("destination_changed", "The install journal cannot be read.") from exc
    if not isinstance(record, dict) or record.get("schema") != _JOURNAL_SCHEMA:
        raise PortableError("destination_changed", "The install journal is not ours.")
    return record


def _redirected(path):
    """Do not traverse links or Windows junctions in an install-owned path."""
    return any(
        part.is_symlink() or getattr(part, "is_junction", lambda: False)()
        for part in (path, *path.parents)
    )


def _check_layout(destination, release):
    metadata = destination / ".astrid-install"
    root = destination / release["inner_directory"]
    # The release must not place its final tree inside the install metadata.
    if root == metadata or metadata in root.parents:
        raise PortableError("pin_invalid", "The Portable root overlaps its install metadata.")
    directories = (destination, metadata, metadata / "archive", metadata / "staging", root)
    files = (
        metadata / "journal.json", metadata / "lock", metadata / "receipt.json",
        metadata / "archive" / release["archive_name"],
        metadata / "archive" / (release["archive_name"] + ".part"),
    )
    for path in (*directories, *files):
        if _redirected(path):
            raise PortableError("destination_changed", f"An install path was redirected: {path}")
        if path.exists() and (not path.is_dir() if path in directories else not path.is_file()):
            raise PortableError("destination_changed", f"An install path changed type: {path}")
    for path in files:
        if path.exists() and path.stat().st_nlink != 1:
            raise PortableError("destination_changed", f"An install file has another owner: {path}")


def _required_missing(root, pin):
    return [
        relative for relative in pin["release"]["required_files"]
        if _redirected(root / relative) or not (root / relative).is_file()
    ]


def _work_bytes(destination):
    total = 0
    for name in ("archive", "staging"):
        directory = destination / ".astrid-install" / name
        if not directory.is_dir() or _redirected(directory):
            continue
        for folder, subdirs, filenames in os.walk(directory, followlinks=False):
            subdirs[:] = [name for name in subdirs if not _redirected(Path(folder) / name)]
            for filename in filenames:
                try:
                    info = (Path(folder) / filename).stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # A running install may just have moved this file.
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
    return total


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
    if offer["state"] == "ours":
        try:
            _check_layout(destination, release)
            journal = _read_journal(destination)
        except PortableError as exc:
            return refuse(exc.code, str(exc), "Keep the existing folder and choose another destination.")
        if journal is None:
            return refuse("destination_changed", "The install journal disappeared.", "Retry the offer.")
        done = _work_bytes(destination)
        offer["resume"] = {"phase": journal.get("phase"), "done_bytes": done}
        # Space already occupied by this attempt is not required a second time.
        # The consent identity above continues to use the original full peak.
        offer["peak_bytes"] = max(0, peak - done)
        if (
            journal.get("phase") == "complete"
            and journal.get("offer_sha256") == offer["offer_sha256"]
            and not _required_missing(destination / release["inner_directory"], pin)
        ):
            offer["state"] = "complete"
            offer["peak_bytes"] = 0
    ancestor = destination
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        return refuse("destination_invalid", f"Not a folder: {ancestor}", "Choose an absolute, unused folder.")
    offer["free_bytes"] = disk_usage(ancestor).free
    if offer["free_bytes"] < offer["peak_bytes"]:
        return refuse(
            "insufficient_space",
            f"Only {offer['free_bytes']} bytes are free; {offer['peak_bytes']} bytes are needed at the peak.",
            "Free enough disk space or choose a folder on a drive with enough space.",
        )
    return offer


def _validate_journal(journal, offer, release):
    if journal is None:
        return
    if journal.get("offer_sha256") != offer["offer_sha256"]:
        raise PortableError("consent_mismatch")
    if (
        journal.get("release") != release["tag"]
        or journal.get("phase") not in ("started", "downloaded", "extracted", "complete")
        or type(journal.get("renaming")) is not bool
        or (journal["renaming"] and journal["phase"] != "downloaded")
    ):
        raise PortableError("destination_changed", "The install journal has changed.")


def _check_final_root(journal, root, staged_root, pin, steps):
    """Check custody before touching even the lock or stale staging."""
    if journal is None:
        if root.exists():
            raise PortableError("destination_changed")
        return
    phase = journal["phase"]
    if phase == "started" and root.exists():
        raise PortableError("destination_changed")
    if phase == "downloaded" and root.exists():
        if (
            not journal["renaming"] or staged_root.exists() or staged_root.is_symlink()
            or _required_missing(root, pin) or steps.missing(root, pin)
        ):
            raise PortableError("destination_changed")
    if phase == "extracted" and (_required_missing(root, pin) or steps.missing(root, pin)):
        raise PortableError("destination_changed", "The promoted install no longer verifies.")


def _preserve_broken(root):
    """Move the owned root aside, never overwriting an earlier preserved one."""
    if not root.exists():
        return None
    stamp = datetime.now(timezone.utc)
    while True:
        kept = root.with_name(f"{root.name}.broken-{stamp:%Y%m%dT%H%M%S}")
        if not kept.exists() and not kept.is_symlink():
            root.rename(kept)
            return str(kept)
        stamp += timedelta(seconds=1)


def install_portable(destination, consent, *, pin=None, steps, platform=None, gpu=None,
                     disk_usage=shutil.disk_usage, environ=None, progress=None) -> InstallResult:
    """Apply one exact offer, resuming only work owned by its destination journal."""
    if consent is None:
        raise PortableError("consent_required")
    pin = load_pin() if pin is None else _validate_pin(pin)
    offer = build_offer(destination, pin=pin, platform=platform, gpu=gpu,
                        disk_usage=disk_usage, environ=environ)
    if not isinstance(consent, str) or consent != offer["offer_sha256"]:
        raise PortableError("consent_mismatch")
    if offer["status"] == "refused":
        raise PortableError(offer["refusal"]["code"], offer["refusal"]["message"])

    destination = Path(offer["destination"])
    release = pin["release"]
    metadata = destination / ".astrid-install"
    journal_path = metadata / "journal.json"
    archive = metadata / "archive" / release["archive_name"]
    part = archive.with_name(archive.name + ".part")
    staging = metadata / "staging"
    root = destination / release["inner_directory"]
    staged_root = staging / release["inner_directory"]
    _check_layout(destination, release)
    journal = _read_journal(destination)
    _validate_journal(journal, offer, release)
    _check_final_root(journal, root, staged_root, pin, steps)
    resumed_from = journal["phase"] if journal is not None else None
    kept_previous = None

    def result(status):
        return {
            "status": status, "resumed_from": resumed_from,
            "root": str(root), "comfy_dir": str(root / "ComfyUI"),
            "python": str(root / "python_embeded" / "python.exe"),
            "release": release["tag"], "kept_previous": kept_previous,
        }

    if journal is not None and journal["phase"] == "complete":
        if not _required_missing(root, pin) and not steps.missing(root, pin):
            # FileLock may create/truncate its file; a completed no-op is read-only.
            return result("already_installed")

    # Recheck before taking custody of a destination which had no journal.
    if journal is None and destination_state(destination) not in ("absent", "empty"):
        raise PortableError("destination_changed")
    metadata.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(metadata / "lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout as exc:
        raise PortableError("install_in_progress") from exc
    try:
        _check_layout(destination, release)
        locked_journal = _read_journal(destination)
        _validate_journal(locked_journal, offer, release)
        _check_final_root(locked_journal, root, staged_root, pin, steps)
        if journal is not None and locked_journal is None:
            raise PortableError("destination_changed", "The install journal disappeared.")
        journal = locked_journal
        if journal is None:
            journal = {
                "schema": _JOURNAL_SCHEMA, "offer_sha256": consent,
                "release": release["tag"], "phase": "started", "renaming": False,
            }
            _write_journal(journal_path, journal)

        def advance(phase, *, renaming=False):
            nonlocal journal
            record = {**journal, "phase": phase, "renaming": renaming}
            _write_journal(journal_path, record)
            journal = record

        if journal["phase"] == "complete":
            if not _required_missing(root, pin) and not steps.missing(root, pin):
                return result("already_installed")
            kept_previous = _preserve_broken(root)
            advance("started")

        if journal["phase"] == "started":
            archive.parent.mkdir(exist_ok=True)
            steps.download(pin, archive, part, progress)
            _check_layout(destination, release)
            if not archive.is_file():
                raise PortableError("archive_incomplete", "The download left no verified archive.")
            advance("downloaded")

        if journal["phase"] == "downloaded":
            _check_layout(destination, release)
            _check_final_root(journal, root, staged_root, pin, steps)
            if root.exists():
                # The rename completed, but its following journal write did not.
                advance("extracted")
            else:
                if staging.exists():
                    shutil.rmtree(staging)
                if progress is not None:
                    progress("extract", 0, release["extracted_bytes"])
                steps.extract(archive, staging, pin)
                _check_layout(destination, release)
                if _required_missing(staged_root, pin) or steps.missing(staged_root, pin):
                    raise PortableError("archive_incomplete")
                if root.exists():
                    raise PortableError("destination_changed")
                root.parent.mkdir(parents=True, exist_ok=True)
                advance("downloaded", renaming=True)
                os.replace(staged_root, root)
                advance("extracted")
                if progress is not None:
                    progress("extract", release["extracted_bytes"], release["extracted_bytes"])

        if journal["phase"] == "extracted":
            _check_layout(destination, release)
            _check_final_root(journal, root, staged_root, pin, steps)
            archive.unlink(missing_ok=True)
            part.unlink(missing_ok=True)
            _write_json(metadata / "receipt.json", {
                "release": release["tag"], "root": str(root),
                "python": str(root / "python_embeded" / "python.exe"),
                "installed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })
            advance("complete")
        return result("installed")
    finally:
        lock.release()


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
