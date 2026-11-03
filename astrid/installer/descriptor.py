"""Read-only description of the current user's login-managed installation.

This is deliberately a product-state primitive, not an R-36 protocol driver.
It accepts no caller-provided roots, service names, process IDs, or removal
targets: all paths come from the current user's persisted install selection.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import xml.etree.ElementTree as element_tree
from dataclasses import dataclass
from pathlib import Path

from ..core.execution.platform_helpers import owned_by_current_user
from .core import Version, absolute
from .service import LABEL, Service, bootstrap_root, command, default_uv, install_selection, render

_MAX_DESCRIPTOR_FILE_BYTES = 1024 * 1024
_POSTCONDITIONS = (
    "owner-target-absent",
    "all-authorized-incarnations-absent",
    "no-owned-action-processes",
    "preserved-data-identity-unchanged",
)


class InstallationDescriptorError(ValueError):
    """The fixed installation selection cannot safely be described."""


@dataclass(frozen=True)
class KeepDataRemovalPlan:
    """Closed roots for a later product uninstall; this descriptor grants no removal authority."""

    operation: str
    keep_data: bool
    remove_paths: tuple[str, ...]
    preserve_paths: tuple[str, ...]
    expected_postconditions: tuple[str, ...]


@dataclass(frozen=True)
class InstallationDescriptor:
    """Verified installer state only; it neither performs nor authorizes removal."""

    program_root: str
    data_root: str
    selected_version_root: str
    definition_path: str
    definition_sha256: str
    action: tuple[str, ...]
    principal: str
    definition_owner: str
    working_directory: str
    removal_plan: KeepDataRemovalPlan


@dataclass(frozen=True)
class _FileSnapshot:
    path: Path
    identity: tuple[int, int, int, int, int, int, int, int]
    data: bytes


def _fail(message: str) -> None:
    raise InstallationDescriptorError(message)


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return tuple(getattr(info, name) for name in (
        "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns",
    ))


def _owned_directory(path: Path, what: str) -> None:
    try:
        canonical = absolute(path)
        info = canonical.lstat()
    except (OSError, ValueError) as exc:
        raise InstallationDescriptorError(f"{what} is unavailable or unsafe.") from exc
    if (not stat.S_ISDIR(info.st_mode) or canonical.is_symlink()
            or not owned_by_current_user(canonical, info, forbidden=0o022)):
        _fail(f"{what} is not an owned private directory.")


def _read_owned_regular(path: Path, what: str, *, required: bool = True) -> _FileSnapshot | None:
    try:
        canonical = absolute(path)
        if not required and not canonical.exists() and not os.path.lexists(canonical):
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(canonical, flags)
    except (OSError, ValueError) as exc:
        raise InstallationDescriptorError(f"{what} is unavailable or unsafe.") from exc
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size < 0 or before.st_size > _MAX_DESCRIPTOR_FILE_BYTES
                or not owned_by_current_user(descriptor, before, forbidden=0o022)):
            _fail(f"{what} is not an owned regular file.")
        chunks: list[bytes] = []
        remaining = _MAX_DESCRIPTOR_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        observed = canonical.lstat()
    except OSError as exc:
        raise InstallationDescriptorError(f"{what} could not be read safely.") from exc
    finally:
        os.close(descriptor)
    identity = _identity(before)
    if (len(data) > _MAX_DESCRIPTOR_FILE_BYTES or _identity(after) != identity
            or _identity(observed) != identity or len(data) != before.st_size):
        _fail(f"{what} changed while it was read.")
    return _FileSnapshot(canonical, identity, data)


def _unchanged(snapshot: _FileSnapshot | None, what: str) -> None:
    if snapshot is None:
        return
    current = _read_owned_regular(snapshot.path, what)
    if current is None or current.identity != snapshot.identity or current.data != snapshot.data:
        _fail(f"{what} changed while the installation was described.")


def _json_object(snapshot: _FileSnapshot, what: str) -> dict:
    try:
        value = json.loads(snapshot.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallationDescriptorError(f"{what} is malformed.") from exc
    if not isinstance(value, dict):
        _fail(f"{what} is malformed.")
    return value


def _selected_paths(home: Path, system: str) -> tuple[Path, Path, _FileSnapshot, dict]:
    selection_path = install_selection(home, system)
    selection_snapshot = _read_owned_regular(selection_path, "installation selection")
    assert selection_snapshot is not None
    selection = _json_object(selection_snapshot, "installation selection")
    if selection.get("owner") != LABEL or selection.get("managed_by") is not None or selection.get("uninstalled"):
        _fail("The selected installation is not a login-managed Astrid installation.")
    try:
        raw_root, raw_data = selection["program_root"], selection["data_root"]
        root, data = absolute(raw_root), absolute(raw_data)
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallationDescriptorError("The installation selection has unsafe roots.") from exc
    if not isinstance(raw_root, str) or not isinstance(raw_data, str) or raw_root != str(root) or raw_data != str(data):
        _fail("The installation selection roots are not canonical.")
    return root, data, selection_snapshot, selection


def _selected_version(root: Path, selection: dict) -> tuple[Version, _FileSnapshot, _FileSnapshot, _FileSnapshot]:
    marker_snapshot = _read_owned_regular(root / "install.json", "installation marker")
    pointer_snapshot = _read_owned_regular(root / "current", "current-version pointer")
    assert marker_snapshot is not None and pointer_snapshot is not None
    marker = _json_object(marker_snapshot, "installation marker")
    if (marker.get("owner") != LABEL or marker.get("managed_by") is not None
            or marker.get("data_root") != selection["data_root"]):
        _fail("The selected installation ownership changed.")
    try:
        version_name = pointer_snapshot.data.decode("utf-8").strip()
        if not version_name or len(version_name) > 64:
            raise ValueError
        version_record = root / "versions" / version_name / "version.json"
        record_snapshot = _read_owned_regular(version_record, "selected version record")
        assert record_snapshot is not None
        version = Version.load(_json_object(record_snapshot, "selected version record"))
    except (KeyError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise InstallationDescriptorError("The selected version is malformed.") from exc
    expected_root = root / "versions" / version_name
    if (version.root != expected_root or version.managed_by is not None
            or selection.get("identity") != version.document()["identity"]):
        _fail("The selected version does not match the installation selection.")
    _owned_directory(version.root, "selected version directory")
    if (not version.python.is_file() or version.python.is_symlink()
            or not version.python.is_relative_to(version.root)
            or not version.profile.is_file() or version.profile.is_symlink()
            or not version.profile.is_relative_to(version.root)):
        _fail("The selected service action is unsupported or changed.")
    return version, marker_snapshot, pointer_snapshot, record_snapshot


def _windows_principal() -> str:
    executable = Path(os.environ.get("SystemRoot", r"C:\\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    try:
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command",
             "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallationDescriptorError("The current Windows service principal is unavailable.") from exc
    value = result.stdout.strip()
    if not value:
        _fail("The current Windows service principal is unavailable.")
    return value


def _definition_principal(raw: bytes, system: str) -> str:
    if system != "win32":
        try:
            import pwd

            value = pwd.getpwuid(os.getuid()).pw_name
        except (KeyError, OSError) as exc:
            raise InstallationDescriptorError("The current service principal is unavailable.") from exc
        if not value:
            _fail("The current service principal is unavailable.")
        return value
    try:
        document = element_tree.fromstring(raw)
        values = [element.text.strip() for element in document.iter()
                  if element.tag.rsplit("}", 1)[-1] == "UserId" and element.text and element.text.strip()]
    except element_tree.ParseError as exc:
        raise InstallationDescriptorError("The scheduled task definition is malformed.") from exc
    if len(values) != 1 or values[0] != _windows_principal():
        _fail("The scheduled task principal is foreign or ambiguous.")
    return values[0]


def _removal_plan(root: Path, data: Path, *, home: Path, system: str) -> KeepDataRemovalPlan:
    if os.environ.get("ASTRID_INSTALL_UV"):
        _fail("The selected installation has an unsupported installer bootstrap override.")
    uv = default_uv()
    paths = [root]
    bootstrap = bootstrap_root(home, system)
    if root != bootstrap.parent and uv.parent == bootstrap and bootstrap.exists():
        extra = bootstrap.parent if system == "win32" else bootstrap
        paths.append(absolute(extra))
    if len(set(paths)) != len(paths) or any(data == path or data in path.parents or path in data.parents for path in paths):
        _fail("The workspace support directory overlaps a program removal directory.")
    return KeepDataRemovalPlan(
        operation="product-uninstall",
        keep_data=True,
        remove_paths=tuple(str(path) for path in paths),
        preserve_paths=(str(data),),
        expected_postconditions=_POSTCONDITIONS,
    )


def _describe(home: Path, system: str) -> InstallationDescriptor:
    """Internal seam for focused installer tests; public callers use no arguments."""
    if system not in {"darwin", "linux", "win32"}:
        _fail("The current operating system is unsupported.")
    home = absolute(home)
    root, data, selection_snapshot, selection = _selected_paths(home, system)
    _owned_directory(root, "selected program root")
    _owned_directory(data, "selected data root")
    journal_snapshot = _read_owned_regular(root / "update-transaction.json", "update journal", required=False)
    if journal_snapshot is not None:
        journal = _json_object(journal_snapshot, "update journal")
        if journal and journal.get("step") not in {"complete", "rolled_back"}:
            _fail("The selected installation has a pending update.")
    version, marker_snapshot, pointer_snapshot, version_snapshot = _selected_version(root, selection)
    service = Service(root, root / "logs" / "install.log", lambda *_args, **_kwargs: "",
                      home=home, system=system, say=lambda _line: None)
    env_snapshot = _read_owned_regular(service.env_file, "service environment")
    assert env_snapshot is not None
    try:
        service.settings()
    except (OSError, ValueError) as exc:
        raise InstallationDescriptorError("The service environment is malformed.") from exc
    definition_snapshot = _read_owned_regular(service.path, "service definition")
    assert definition_snapshot is not None
    principal = _definition_principal(definition_snapshot.data, system)
    action = tuple(command(version, data, windows=system == "win32", log=service.log, env_file=service.env_file))
    expected = render(system, version, data, service.log, user=principal if system == "win32" else "",
                      env_file=service.env_file).encode("utf-8")
    if definition_snapshot.data != expected:
        _fail("The service definition is foreign or changed.")
    # Equality above makes this generated argv the action in the bytes we read,
    # rather than a prediction for an unverified definition.
    plan = _removal_plan(root, data, home=home, system=system)
    # Backend.preflight_remove() remains mandatory for a later uninstall wrapper.
    # It can launch Runtime workspace inspection (and therefore append an
    # installer log), so calling it here would violate this descriptor's
    # read-only contract. The static overlap fence above is all this snapshot
    # may decide; a wrapper must re-run the full preflight while it holds the
    # normal installer lifecycle lock.
    for snapshot, name in (
        (selection_snapshot, "installation selection"), (marker_snapshot, "installation marker"),
        (pointer_snapshot, "current-version pointer"), (version_snapshot, "selected version record"),
        (journal_snapshot, "update journal"), (env_snapshot, "service environment"),
        (definition_snapshot, "service definition"),
    ):
        _unchanged(snapshot, name)
    return InstallationDescriptor(
        program_root=str(root), data_root=str(data), selected_version_root=str(version.root),
        definition_path=str(service.path), definition_sha256=hashlib.sha256(definition_snapshot.data).hexdigest(),
        action=action, principal=principal, definition_owner="product-installer",
        working_directory=str(version.root) if system == "win32" else "", removal_plan=plan,
    )


def describe_current_user_installation() -> InstallationDescriptor:
    """Return a bounded read-only descriptor for the selected login installation."""
    return _describe(Path.home(), sys.platform)
