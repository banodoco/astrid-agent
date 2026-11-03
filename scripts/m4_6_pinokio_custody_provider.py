"""Fail-closed Pinokio custody and source-backed engine-request primitives.

This is the fixed B owner for the six ``managed_by=pinokio`` M4-6 slots.  It
does not run Node, invoke a Pinokio action, start an installer, open a socket,
or turn a local command into an action.  The request/event code below is a
pure interpreter for the immutable source contract retained in
``M4-4a-pinokio-source-contract``:

* pinokiod receives a URI request through ``kernel.api.process`` and queues it;
  a ``Queued`` event is not a result (``kernel/api/index.js:1678-1787``);
* the socket dispatch is deliberately not awaited
  (``server/socket.js:271-307``); and
* pterm can exit zero on ``disconnect`` without proving action success
  (``pterm/script.js:133-166,257-273``).

The pinned source contains no task readback API or custody guarantee for an
arbitrary process tree.  Consequently this module can construct and audit a
source-shaped request, but it cannot produce a successful live settlement or
authorize a custody transition.  A future reviewed, authenticated engine and
collector boundary must supply those missing facts.  Until then every live
owner method raises before a provider can authorize or construct a receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Mapping, Sequence
from urllib.parse import unquote, urlsplit

# B owns OS observation in this module.  Pinokio custody intentionally imports
# that primitive rather than creating an alternate OS/process probe here.  The
# current primitive is polling-only and therefore remains non-authoritative.
from scripts.m4_6_process_observer import (
    ObservationRefused,
    ObservationUnavailable,
    ProcessTransitionObserver,
)

if TYPE_CHECKING:
    from scripts.native_service_adapter import (
        CommandRunner,
        DataProbe,
        DurableJournalProbe,
        ProcessProbe,
    )


PINNED_PTERM_COMMIT: Final = "5be43718701c448b43daf0ce51d8935f170065bb"
PINNED_PINOKIOD_COMMIT: Final = "add4a674ad1ba95bb62ec427fce519fe5bf0e9bf"
_DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_IMPORT_RE: Final = re.compile(
    r"(?:require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
    r"|\bfrom\s+['\"]([^'\"]+)['\"]"
    r"|\bimport\s+['\"]([^'\"]+)['\"])",
    re.MULTILINE,
)
_REQUIRE_CALL_RE: Final = re.compile(r"\brequire\s*\(")
_DYNAMIC_IMPORT_RE: Final = re.compile(r"\bimport\s*\(")
_MAX_ACTION_BYTES: Final = 8 * 1024 * 1024
_MAX_GRAPH_FILES: Final = 128
_PINOKIO_SESSION_ROWS: Final = {
    "pinokio-backend-macos": {
        "mode": "pinokio-backend", "os": "macos", "runner": "macos-15", "shell": "sh",
    },
    "pinokio-backend-linux": {
        "mode": "pinokio-backend", "os": "linux", "runner": "ubuntu-24.04", "shell": "sh",
    },
    "pinokio-backend-windows": {
        "mode": "pinokio-backend", "os": "windows", "runner": "windows-2022",
        "shell": "powershell.exe",
    },
    "pinokio-fully-local-macos": {
        "mode": "pinokio-fully-local", "os": "macos", "runner": "macos-15", "shell": "sh",
    },
    "pinokio-fully-local-linux": {
        "mode": "pinokio-fully-local", "os": "linux", "runner": "ubuntu-24.04", "shell": "sh",
    },
    "pinokio-fully-local-windows": {
        "mode": "pinokio-fully-local", "os": "windows", "runner": "windows-2022",
        "shell": "powershell.exe",
    },
}
_PINOKIO_SESSION_ROW_FIELDS: Final = frozenset(
    {"job_id", "mode", "os", "runner", "shell"}
)
_ROLE_BY_OPERATION: Final = {
    "start": "pinokio/start.js",
    "stop": "pinokio/stop.js",
    "update": "pinokio/update.js",
    "uninstall": "pinokio/uninstall.js",
}


class PinokioCustodyRefused(RuntimeError):
    """A missing, drifted, ambiguous, or non-authoritative fact stopped custody."""


class PinokioSourceMismatch(PinokioCustodyRefused):
    """An immutable app/action/helper or selected-app fact did not match."""


class PinokioSettlementUnknown(PinokioCustodyRefused):
    """An engine request did not receive sufficient terminal settlement proof."""


def validate_pinokio_session_row(
    slot_id: object, contract: object
) -> tuple[str, str]:
    """Return the selected mode/OS only for one complete, exact M-QUAL row.

    The slot suffix alone is not an OS authority: mode, OS, runner image and
    shell must all correlate with the same frozen row. This validates request
    shape only; it does not authenticate a host or open session admission.
    """
    if not isinstance(slot_id, str) or slot_id not in _PINOKIO_SESSION_ROWS:
        raise PinokioCustodyRefused("Pinokio provider only accepts the fixed six Pinokio slots")
    if not isinstance(contract, Mapping):
        raise PinokioCustodyRefused("Pinokio session row is missing or malformed")
    expected = {"job_id": slot_id, **_PINOKIO_SESSION_ROWS[slot_id]}
    if set(contract) != _PINOKIO_SESSION_ROW_FIELDS or dict(contract) != expected:
        raise PinokioCustodyRefused("Pinokio slot/mode/OS/runner/shell do not match one fixed row")
    return expected["mode"], expected["os"]


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PinokioCustodyRefused(f"{field} is absent")
    return value


def _resolved_regular_file(path: Path, *, root: Path) -> tuple[Path, bytes, str]:
    """Read a bounded, non-symlink regular file and bind its resolved bytes."""
    try:
        candidate = path.absolute()
        if candidate.is_symlink():
            raise PinokioSourceMismatch(f"action/helper symlink is refused: {path}")
        resolved = candidate.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except OSError as exc:
        raise PinokioSourceMismatch(f"action/helper cannot be resolved: {path}") from exc
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PinokioSourceMismatch("action/helper escapes selected installed app") from exc
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise PinokioSourceMismatch(f"action/helper cannot be opened: {resolved}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > _MAX_ACTION_BYTES:
            raise PinokioSourceMismatch("action/helper is not a bounded regular file")
        parts: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            parts.append(chunk)
        contents = b"".join(parts)
        if len(contents) != file_stat.st_size:
            raise PinokioSourceMismatch("action/helper changed while being bound")
        return resolved, contents, _sha256(contents)
    except OSError as exc:
        raise PinokioSourceMismatch(f"action/helper cannot be read: {resolved}") from exc
    finally:
        os.close(descriptor)


def _resolve_helper(importer: Path, specifier: str, *, root: Path) -> Path | None:
    """Resolve only static relative JS/JSON imports inside the installed app."""
    if not specifier.startswith(("./", "../")):
        return None
    base = importer.parent / specifier
    candidates = [base]
    if not base.suffix:
        candidates.extend(base.with_suffix(suffix) for suffix in (".js", ".cjs", ".mjs", ".json"))
        candidates.extend(base / f"index{suffix}" for suffix in (".js", ".cjs", ".mjs", ".json"))
    for candidate in candidates:
        try:
            if candidate.is_file() and not candidate.is_symlink():
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root.resolve(strict=True))
                return resolved
        except (OSError, ValueError) as exc:
            raise PinokioSourceMismatch(
                "resolved helper is unavailable or outside app root"
            ) from exc
    raise PinokioSourceMismatch(f"static helper cannot be resolved: {specifier}")


@dataclass(frozen=True)
class ImmutableActionFile:
    """One descriptor/helper file bound by resolved path and exact bytes."""

    role: str
    path: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.role or not Path(self.path).is_absolute() or not _is_digest(self.sha256):
            raise ValueError("immutable action file is malformed")


# This is an exact-source inventory, not a general JS import allowlist. The
# generic action graph below deliberately retains its dynamic-import refusal.
U2_PINOKIO_COMMIT: Final = "f8605a3877ac76c0d7f65e4b013bc112b8c96992"
U2_AGENT_MAPPING_COMMIT: Final = "583a7f3fd6c8034d317d69bba5383f4b2298b6f1"
_U2_SOURCE_SHA256: Final = {
    "pinokio/install.js": "60f4473e91b769af0480098b96a36500f3c1628ac33a7617f68df4de1fe436e2",
    "pinokio/install-local.js": "6f1cf8c070263689571eadbcfabd6d30efd4ae9a011ec1d3bc8c87657cb197a8",
    "pinokio/start.js": "442ff8fb73d65047bc4dd34de997bd36920efab16733aeb6ddd967afe291c037",
    "pinokio/stop.js": "f9d62e4e6a826fccd9791ee211dae933f46c8f99019e145abb3db3bf9595fd48",
    "pinokio/update.js": "1be13ca4d415d1693e0493a0d81d7aecf616f27ab3875a94ea5d9ebb69aba9e5",
    "pinokio/uninstall.js": "f54ccc8221ec3c6fed9a61d2f67aeab4bb2b2c08f8f7522c81399b483b57a471",
    "pinokio/lib.js": "59bb0a556b425dafb4d986aaab54f0dbd23a988814a0d64a6cb4437c52d3d998",
    "pinokio/platforms/posix.js": "20cf174da5668607f2ab0d8f7856475e1d7bd0363bd7c86512658130ce73279b",
    "pinokio/platforms/windows.js": "bfcce0736bdff41afae7cbb36c579b2ea49420b4d91bc40c5e7263ada509c519",
    "pinokio/environment.js": "0da2a9d2186e7ce28d40a17d9943c973aa6edd79613caaf31328722d92db0a41",
    "pinokio/session.mjs": "460ff77a8cda2b711c785a607e4a399f8c33ddc903496e4c769b717d010aad99",
    "pinokio/lifecycle.mjs": "0ddc6a5596dc02a2f33afba799495c7834ebb193df33fe15c376c2b60859a3ad",
    "pinokio/update-stage.mjs": "e563c3d3fb8e5a3d11ddc3ba84f9f15d146eac0ccb359a3f403bb3c68dbda551",
    "pinokio/shell.mjs": "d8c2dfa774c0bda07d29f923f8b84d06055e76a8f4c5b6192980be2dffa8a7e9",
    "pinokio/installer-lock.py": "809937b3e6a4f2b48ef1cc75cd262f3acd828f9252cbbb4825a144dabb59fa2f",
}
# Exact reviewed sites, including the foreign runtime edge, never executed here.
U2_DYNAMIC_IMPORT_SITES: Final = (
    ("pinokio/lib.js", 159, "./session.mjs"),
    ("pinokio/lib.js", 276, "./session.mjs"),
    ("pinokio/lib.js", 303, "./session.mjs"),
    ("pinokio/lib.js", 310, "./session.mjs"),
    ("pinokio/lib.js", 394, "./lifecycle.mjs"),
    ("pinokio/lib.js", 438, "./lifecycle.mjs"),
    ("pinokio/lib.js", 450, "./update-stage.mjs"),
    ("pinokio/lib.js", 451, "./session.mjs"),
    ("pinokio/lifecycle.mjs", 53, "./session.mjs"),
    ("pinokio/lifecycle.mjs", 171, "./session.mjs"),
    ("pinokio/update-stage.mjs", 14, "./session.mjs"),
    ("pinokio/update-stage.mjs", 137, "node:fs/promises"),
)
U2_NODE_BUILTINS: Final = (
    "node:child_process", "node:crypto", "node:fs", "node:fs/promises", "node:http",
    "node:net", "node:os", "node:path", "node:url", "node:util",
)


@dataclass(frozen=True)
class PinokioNativeServiceProfile:
    """Inspect the reviewed six-row source predicate, without native authority.

    All listed bytes (including path-executed helpers and both install roots)
    must match the selected source. This neither authenticates an installed app
    nor closes its external execution dependencies. It cannot be used as a
    PinokioActionGraph or a request dispatcher. No caller identity is admitted.
    """

    slot_id: str
    mode: str
    os_name: str
    app_root: str
    files: tuple[ImmutableActionFile, ...]

    @classmethod
    def bind_source(
        cls, *, slot_id: str, contract: Mapping[str, object], app_root: Path,
        source_commit: str,
    ) -> "PinokioNativeServiceProfile":
        mode, os_name = validate_pinokio_session_row(slot_id, contract)
        if source_commit != U2_PINOKIO_COMMIT:
            raise PinokioSourceMismatch("U2 profile requires the exact selected Pinokio source commit")
        try:
            root = app_root.absolute()
            if root.is_symlink() or not root.is_dir():
                raise PinokioSourceMismatch("U2 source root is unavailable or a symlink")
            root = root.resolve(strict=True)
        except OSError as exc:
            raise PinokioSourceMismatch("U2 source root is unavailable") from exc
        files = []
        for relative, expected in sorted(_U2_SOURCE_SHA256.items()):
            candidate = root
            for part in Path(relative).parts:
                candidate = candidate / part
                if candidate.is_symlink():
                    raise PinokioSourceMismatch(f"U2 source symlink is refused: {relative}")
            resolved, _, digest = _resolved_regular_file(candidate, root=root)
            if digest != expected:
                raise PinokioSourceMismatch(f"U2 selected source bytes differ: {relative}")
            files.append(ImmutableActionFile(relative, str(resolved), digest))
        return cls(slot_id, mode, os_name, str(root), tuple(files))

    @property
    def pinokio_mode(self) -> str:
        return "local" if self.mode == "pinokio-fully-local" else "backend"

    @property
    def action_roles(self) -> dict[str, str]:
        return {
            "install": "pinokio/install-local.js" if self.pinokio_mode == "local" else "pinokio/install.js",
            **_ROLE_BY_OPERATION,
        }

    @property
    def source_owner_mapping(self) -> dict[str, str]:
        # backend.py:1102-1117,1302-1312 at U2_AGENT_MAPPING_COMMIT.
        # The bypass is source behavior, never an observed empty OS inventory.
        return {
            "managed_by": "pinokio", "action_session_owner": "pinokio-engine",
            "agent_os_registration": "bypassed",
        }

    @property
    def runtime_command_shape(self) -> tuple[str, ...]:
        """Source templates only; installed argv/cwd/environment remain unbound."""
        off = " --hosted-origin off" if self.pinokio_mode == "local" else ""
        if self.os_name == "windows":
            return ("<environmentCmd(installed.env)>", "/d", "/s", "/c",
                    f'""<program-root>\\run.cmd"{off}"')
        return ("<program-root>/run", *(("--hosted-origin", "off") if off else ()))

    @property
    def unbound_identities(self) -> dict[str, None]:
        """Unavailable facts, not writable identity inputs or absence claims."""
        roles = (
            "installed_app_and_mode", "engine_incarnation", "selected_principal",
            "node_runtime_and_builtin_closure", "python_runtime_and_libraries",
            "installed_agent_runtime_packages", "installed_launchers_and_profile",
            "shell_git_and_loader_environment", "installer_and_manifest_payloads",
            "resolved_runtime_argv_and_cwd", "controller_descendant_ancestry",
            "native_manager_target_inventory", "canonical_native_definitions",
            "protected_collector_request_plan_upload_binding",
            "independent_initial_active_final_census", "program_data_roots_and_sentinel",
            "actual_installer_child_exit", "authoritative_engine_terminal_readback",
        )
        if self.pinokio_mode == "local":
            roles += ("editor_script_and_dependency_closure", "npm_npx_package_closure")
        return dict.fromkeys(roles)

    def verify_current_bytes(self) -> None:
        refreshed = type(self).bind_source(
            slot_id=self.slot_id,
            contract={"job_id": self.slot_id, **_PINOKIO_SESSION_ROWS.get(self.slot_id, {})},
            app_root=Path(self.app_root), source_commit=U2_PINOKIO_COMMIT,
        )
        if refreshed != self:
            raise PinokioSourceMismatch("U2 profile binding changed")

    def require_native_binding(self) -> None:
        self.verify_current_bytes()
        raise PinokioCustodyRefused(
            "U2 source profile has unbound native/external identities: "
            + ", ".join(self.unbound_identities)
        )


def inspect_u2_source(
    *, slot_id: str, app_root: Path, source_commit: str = U2_PINOKIO_COMMIT,
) -> PinokioNativeServiceProfile:
    """Return the bounded U2 source match for a fixed slot; no native authority."""
    row = _PINOKIO_SESSION_ROWS.get(slot_id)
    if row is None:
        raise PinokioCustodyRefused("Pinokio provider only accepts the fixed six Pinokio slots")
    profile = PinokioNativeServiceProfile.bind_source(
        slot_id=slot_id,
        contract={"job_id": slot_id, **row},
        app_root=app_root,
        source_commit=source_commit,
    )
    profile.verify_current_bytes()
    return profile


def _u2_cli_payload(
    *, slot_id: str, app_root: str, source_commit: str, effective_uid: object,
    platform_name: str,
) -> tuple[dict[str, object], int]:
    payload: dict[str, object] = {
        "schema": "astrid.m46.u2-source-predicate.v1",
        "status": "source_refused_native_refused",
        "source_status": "refused",
        "native_status": "refused",
        "native_authority": "none",
        "native_refusal": "source matching does not authenticate an installed app or bind native identities",
        "slot_id": slot_id,
        "selected_source_commit": source_commit,
        "app_root": app_root,
        "diagnostics": {"effective_uid": effective_uid, "platform": platform_name},
    }
    try:
        root = Path(app_root)
        if not root.is_absolute():
            raise PinokioSourceMismatch("installed app root must be absolute")
        profile = inspect_u2_source(slot_id=slot_id, app_root=root, source_commit=source_commit)
    except (PinokioCustodyRefused, OSError, ValueError) as exc:
        payload["source_refusal"] = str(exc)
        return payload, 2

    payload.update({
        "status": "source_matched_native_refused",
        "source_status": "matched_and_rechecked",
        "source_match_is_install_authentication": False,
        "mode": profile.mode,
        "os": profile.os_name,
        "pinokio_mode": profile.pinokio_mode,
        "action_mapping": profile.action_roles,
        "source_owner_mapping": profile.source_owner_mapping,
        "runtime_command_shape": list(profile.runtime_command_shape),
        "inventory": [
            {"role": item.role, "path": item.path, "sha256": item.sha256}
            for item in profile.files
        ],
        "missing_native_identities": list(profile.unbound_identities),
    })
    if os.name != "posix":
        payload["status"] = "source_matched_native_diagnostic_refused_platform"
        payload["native_refusal"] = "native diagnostic requires a POSIX effective UID observation"
        return payload, 2
    if type(effective_uid) is not int or effective_uid < 0:
        payload["status"] = "source_matched_native_diagnostic_refused_uid"
        payload["native_refusal"] = "native diagnostic has no valid POSIX effective UID observation"
        return payload, 2
    if effective_uid == 0:
        payload["status"] = "source_matched_native_diagnostic_refused_root"
        payload["native_refusal"] = "native diagnostic command refuses effective UID 0"
        return payload, 2
    return payload, 0


def main(argv: Sequence[str] | None = None) -> int:
    """Print the read-only U2 source predicate result as JSON."""
    parser = argparse.ArgumentParser(
        description=(
            "Read-only U2 source match. Exit 0 means bounded bytes matched; native "
            "identities remain refused and no installed-app authentication is claimed."
        )
    )
    parser.add_argument("--slot-id", required=True, help="one of the six fixed Pinokio slots")
    parser.add_argument("--app-root", required=True, help="absolute installed app root")
    parser.add_argument(
        "--source-commit", default=U2_PINOKIO_COMMIT,
        help="exact selected Pinokio commit (defaults to the reviewed U2 commit)",
    )
    args = parser.parse_args(argv)
    effective_uid = getattr(os, "geteuid", lambda: None)()
    payload, exit_code = _u2_cli_payload(
        slot_id=args.slot_id,
        app_root=args.app_root,
        source_commit=args.source_commit,
        effective_uid=effective_uid,
        platform_name=platform.system(),
    )
    print(json.dumps(payload, sort_keys=True))
    return exit_code


@dataclass(frozen=True)
class PinokioActionGraph:
    """Four operation descriptors plus all statically resolvable helpers.

    This source graph does not resolve installed Runtime argv or authenticate
    an installation. Including uninstall supplies no removal authority.
    """

    app_root: str
    files: tuple[ImmutableActionFile, ...]
    graph_sha256: str

    @classmethod
    def collect(cls, app_root: Path) -> "PinokioActionGraph":
        try:
            root = app_root.absolute()
            if root.is_symlink():
                raise PinokioSourceMismatch("selected installed app root is a symlink")
            root = root.resolve(strict=True)
            if not root.is_dir():
                raise PinokioSourceMismatch("selected installed app root is not a directory")
        except OSError as exc:
            raise PinokioSourceMismatch("selected installed app root is unavailable") from exc

        entries: dict[Path, str] = {}
        pending: list[tuple[Path, str]] = []
        for role in _ROLE_BY_OPERATION.values():
            pending.append((root / role, role))
        while pending:
            source, role = pending.pop()
            resolved, contents, digest = _resolved_regular_file(source, root=root)
            prior = entries.get(resolved)
            if prior is not None:
                if prior != digest:
                    raise PinokioSourceMismatch("action/helper path changed during graph binding")
                continue
            if len(entries) >= _MAX_GRAPH_FILES:
                raise PinokioSourceMismatch("resolved action/helper graph exceeds the bound")
            entries[resolved] = digest
            try:
                text = contents.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            imports = _IMPORT_RE.findall(text)
            literal_requires = sum(1 for groups in imports if groups[0])
            if (
                len(_REQUIRE_CALL_RE.findall(text)) != literal_requires
                or _DYNAMIC_IMPORT_RE.search(text) is not None
            ):
                raise PinokioSourceMismatch(
                    f"dynamic action/helper import cannot be bound: {resolved.relative_to(root)}"
                )
            for groups in imports:
                specifier = next((item for item in groups if item), "")
                helper = _resolve_helper(resolved, specifier, root=root)
                if helper is not None:
                    pending.append((helper, f"helper:{helper.relative_to(root).as_posix()}"))

        files: list[ImmutableActionFile] = []
        for path, digest in entries.items():
            relative = path.relative_to(root).as_posix()
            role = relative if relative in _ROLE_BY_OPERATION.values() else f"helper:{relative}"
            files.append(ImmutableActionFile(role=role, path=str(path), sha256=digest))
        files.sort(key=lambda item: (item.role, item.path))
        roles = {item.role for item in files}
        if not set(_ROLE_BY_OPERATION.values()).issubset(roles):
            raise PinokioSourceMismatch("required start/stop/update/uninstall action is absent")
        payload = {"app_root": str(root), "files": [item.__dict__ for item in files]}
        return cls(
            app_root=str(root), files=tuple(files), graph_sha256=_sha256(_canonical(payload))
        )

    def action_for(self, operation: str) -> ImmutableActionFile:
        role = _ROLE_BY_OPERATION.get(operation)
        if role is None:
            raise ValueError("only start, stop, update, and uninstall actions are supported")
        matches = [item for item in self.files if item.role == role]
        if len(matches) != 1:
            raise PinokioSourceMismatch("bound action graph has ambiguous action identity")
        return matches[0]

    def verify_current_bytes(self) -> None:
        """Rebind from disk and reject every descriptor/helper graph change."""
        refreshed = type(self).collect(Path(self.app_root))
        if (
            refreshed.app_root != self.app_root
            or refreshed.files != self.files
            or refreshed.graph_sha256 != self.graph_sha256
        ):
            raise PinokioSourceMismatch("bound Pinokio action/helper bytes changed")


@dataclass(frozen=True)
class PinokioSelectedApp:
    """A source-shaped ``pinokio://.../api/...`` selection matched to app status."""

    ref: str
    app_id: str
    app_path: str
    control_protocol: str
    control_host: str
    control_port: int

    @classmethod
    def bind(
        cls,
        *,
        ref: str,
        expected_app_id: str,
        expected_app_path: Path,
        status: Mapping[str, object],
    ) -> "PinokioSelectedApp":
        parsed = urlsplit(_require_text(ref, "Pinokio app ref"))
        if parsed.scheme != "pinokio" or not parsed.hostname:
            raise PinokioSourceMismatch("Pinokio app ref is malformed")
        try:
            port = parsed.port
        except ValueError as exc:
            raise PinokioSourceMismatch("Pinokio app ref port is malformed") from exc
        if port is None or port < 1 or port > 65535:
            raise PinokioSourceMismatch("Pinokio app ref requires a positive explicit port")
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[0] != "api" or any(part in {".", ".."} for part in parts):
            raise PinokioSourceMismatch("Pinokio app ref must select one api app")
        app_id = "/".join(parts[1:])
        if app_id != _require_text(expected_app_id, "expected app id"):
            raise PinokioSourceMismatch("Pinokio ref app id differs from selected installed app")
        value = status.get("path")
        if not isinstance(value, str) or not value.strip():
            raise PinokioSourceMismatch("Pinokio status has no selected app path")
        try:
            expected = expected_app_path.absolute().resolve(strict=True)
            observed = Path(value).absolute().resolve(strict=True)
        except OSError as exc:
            raise PinokioSourceMismatch("selected installed app path cannot be resolved") from exc
        if expected != observed:
            raise PinokioSourceMismatch("Pinokio status path differs from selected installed app")
        status_id = status.get("id")
        if status_id is not None and status_id != app_id:
            raise PinokioSourceMismatch("Pinokio status app id differs from selected installed app")
        return cls(
            ref=ref,
            app_id=app_id,
            app_path=str(expected),
            control_protocol="http",
            control_host=parsed.hostname,
            control_port=port,
        )


@dataclass(frozen=True)
class PreparedPinokioRequest:
    """Immutable input for one daemon ``process`` request; it has not been sent."""

    request_id: str
    operation: str
    action_name: str
    action: ImmutableActionFile
    selected_app: PinokioSelectedApp
    action_graph_sha256: str
    input: Mapping[str, object]

    @classmethod
    def prepare(
        cls,
        *,
        request_id: str,
        operation: str,
        graph: PinokioActionGraph,
        selected_app: PinokioSelectedApp,
        input: Mapping[str, object],
    ) -> "PreparedPinokioRequest":
        if not _REQUEST_ID_RE.fullmatch(request_id):
            raise PinokioCustodyRefused("caller request id is not a bounded stable identifier")
        if not isinstance(input, Mapping):
            raise PinokioCustodyRefused("Pinokio action input must be an object")
        try:
            canonical_input = json.loads(_canonical(dict(input)))
        except (TypeError, ValueError) as exc:
            raise PinokioCustodyRefused("Pinokio action input is not canonical JSON") from exc
        if not isinstance(canonical_input, dict):
            raise PinokioCustodyRefused("Pinokio action input is not an object")
        graph_root = Path(graph.app_root)
        if graph_root != Path(selected_app.app_path):
            raise PinokioSourceMismatch("action graph does not belong to selected installed app")
        # The daemon's default for a URI request is ``run``.  Freeze it here;
        # no caller can substitute an arbitrary descriptor action.
        return cls(
            request_id=request_id,
            operation=operation,
            action_name="run",
            action=graph.action_for(operation),
            selected_app=selected_app,
            action_graph_sha256=graph.graph_sha256,
            input=canonical_input,
        )

    def process_payload(self) -> dict[str, object]:
        """Return data shaped for the pinned daemon; never opens a transport."""
        return {
            "uri": self.action.path,
            "id": self.request_id,
            "action": self.action_name,
            "input": dict(self.input),
        }


@dataclass(frozen=True)
class RequestAcceptance:
    request_id: str
    queued: bool
    action_sha256: str


@dataclass(frozen=True)
class RequestSettlement:
    status: str
    reason: str
    request_id: str
    terminal_event: str | None
    event_count: int


class PinokioRequestSettlement:
    """Separate queued acceptance from terminal evidence for one request ID.

    It models only packets that a future real engine transport has already
    received.  It deliberately exposes no send/connect API.  Since the pinned
    source supplies no general task-status readback, even a result followed by
    stop/disconnect settles as ``unknown`` rather than success.
    """

    def __init__(self, prepared: PreparedPinokioRequest) -> None:
        self._prepared = prepared
        self._accepted: RequestAcceptance | None = None
        self._settlement: RequestSettlement | None = None
        self._events: list[dict[str, object]] = []
        self._result_seen = False
        self._stop_seen = False
        self._lock = threading.RLock()

    @property
    def accepted(self) -> RequestAcceptance | None:
        return self._accepted

    @property
    def settlement(self) -> RequestSettlement | None:
        return self._settlement

    def record_sent(self) -> RequestAcceptance:
        """Record only that a future transport accepted bytes for this request."""
        with self._lock:
            if self._accepted is not None or self._settlement is not None:
                raise PinokioCustodyRefused("Pinokio request cannot be accepted twice")
            self._accepted = RequestAcceptance(
                request_id=self._prepared.request_id,
                queued=True,
                action_sha256=self._prepared.action.sha256,
            )
            return self._accepted

    def _finish(self, status: str, reason: str, terminal_event: str | None) -> RequestSettlement:
        if self._settlement is None:
            self._settlement = RequestSettlement(
                status=status,
                reason=reason,
                request_id=self._prepared.request_id,
                terminal_event=terminal_event,
                event_count=len(self._events),
            )
        return self._settlement

    def ingest(self, packet: Mapping[str, object]) -> RequestSettlement | None:
        """Interpret one subscribed engine packet without treating it as a receipt."""
        with self._lock:
            if self._accepted is None:
                raise PinokioCustodyRefused("Pinokio packet arrived before request acceptance")
            if self._settlement is not None:
                raise PinokioCustodyRefused("Pinokio packet arrived after terminal settlement")
            packet_id = packet.get("id")
            packet_type = packet.get("type")
            if packet_id != self._prepared.request_id or not isinstance(packet_type, str):
                return self._finish("unknown", "foreign-or-malformed-engine-packet", None)
            try:
                clean = json.loads(_canonical(dict(packet)))
            except (TypeError, ValueError):
                return self._finish("unknown", "noncanonical-engine-packet", None)
            if not isinstance(clean, dict):
                return self._finish("unknown", "nonobject-engine-packet", None)
            self._events.append(clean)
            if packet_type == "error":
                return self._finish("error", "engine-error", "error")
            if packet_type == "launch.requirements.control":
                data = packet.get("data")
                if isinstance(data, Mapping) and data.get("action") in {"cancelled", "blocked"}:
                    return self._finish(
                        "refused", "engine-launch-requirements-refusal", packet_type
                    )
                return self._finish("unknown", "malformed-launch-requirements-control", packet_type)
            if packet_type == "result":
                self._result_seen = True
                return None
            if packet_type == "event" and packet.get("data") == "stop":
                if not self._result_seen:
                    return self._finish("unknown", "stop-without-result", "event:stop")
                self._stop_seen = True
                return None
            if packet_type == "disconnect":
                if self._stop_seen:
                    return self._finish("unknown", "missing-engine-task-readback", "disconnect")
                return self._finish(
                    "unknown", "transport-lost-before-terminal-evidence", "disconnect"
                )
            return None

    def expire(self) -> RequestSettlement:
        with self._lock:
            if self._accepted is None:
                raise PinokioCustodyRefused("unaccepted Pinokio request cannot time out")
            return self._finish("unknown", "engine-settlement-timeout", None)

    def client_exit(self, returncode: int) -> RequestSettlement:
        """A pterm clean exit, including zero, is never action settlement."""
        with self._lock:
            if self._accepted is None:
                raise PinokioCustodyRefused("unaccepted Pinokio client exit is unrelated")
            return self._finish("unknown", f"client-exit-{returncode}-is-not-settlement", None)

    def require_terminal_readback(self) -> RequestSettlement:
        """Close a completed-looking stream when the source lacks task readback."""
        with self._lock:
            if self._accepted is None:
                raise PinokioCustodyRefused("unaccepted Pinokio request has no readback")
            if self._settlement is not None:
                return self._settlement
            if self._result_seen and self._stop_seen:
                return self._finish("unknown", "missing-engine-task-readback", "event:stop")
            return self._finish("unknown", "missing-terminal-engine-sequence", None)


@dataclass(frozen=True)
class PinokioProcessIncarnation:
    """A process fact supplied for reconciliation; never trusted as OS observation."""

    role: str
    pid: int
    birth_id: str
    executable_path: str
    executable_sha256: str
    parent_pid: int | None
    parent_birth_id: str | None
    engine_session_id: str

    def __post_init__(self) -> None:
        if (
            not self.role
            or type(self.pid) is not int
            or self.pid < 1
            or not self.birth_id
            or not Path(self.executable_path).is_absolute()
            or not _is_digest(self.executable_sha256)
            or not self.engine_session_id
        ):
            raise ValueError("Pinokio controller/descendant incarnation is malformed")
        if (self.parent_pid is None) != (self.parent_birth_id is None):
            raise ValueError("Pinokio parent incarnation identity must be paired")


class PinokioCustodyObserver:
    """Private Pinokio transition ledger layered over B's OS observer.

    This class does not reimplement OS process collection.  Until a reviewed
    Pinokio controller source can create a suitable B process observer, it
    records the missing observer as a permanent gap and refuses authorization.
    """

    def __init__(self, *, process_observer: ProcessTransitionObserver | None = None) -> None:
        self._process_observer = process_observer
        self._events: list[dict[str, object]] = []
        self._overflowed = False
        self._bound = False
        self._refusal: str | None = None
        self._max_events = 256
        self._lock = threading.RLock()
        self._append(
            "observer-created",
            "unknown",
            {"os_observer": "absent" if process_observer is None else "attached"},
        )
        if process_observer is None:
            self._refuse("no B-owned Pinokio controller/process observer is bound")

    def _append(self, kind: str, status: str, detail: Mapping[str, object]) -> None:
        if self._overflowed:
            return
        if len(self._events) >= self._max_events - 1:
            self._events.append(
                {
                    "sequence": len(self._events) + 1,
                    "monotonic_ns": time.monotonic_ns(),
                    "kind": "ledger-overflow",
                    "status": "overflow",
                    "detail": {"retention": "refuse"},
                }
            )
            self._overflowed = True
            self._refusal = self._refusal or "Pinokio custody ledger overflowed"
            return
        self._events.append(
            {
                "sequence": len(self._events) + 1,
                "monotonic_ns": time.monotonic_ns(),
                "kind": kind,
                "status": status,
                "detail": json.loads(_canonical(dict(detail))),
            }
        )

    def _refuse(self, reason: str) -> None:
        self._refusal = self._refusal or reason
        self._append("custody-gap", "gap", {"reason": reason})

    def bind(
        self,
        *,
        selected_app: PinokioSelectedApp,
        graph: PinokioActionGraph,
        engine_identity: str,
    ) -> None:
        with self._lock:
            if self._bound:
                raise PinokioCustodyRefused("Pinokio custody observer cannot bind twice")
            if not engine_identity:
                self._refuse("Pinokio engine/action/session identity is absent")
                raise PinokioCustodyRefused("Pinokio engine/action/session identity is absent")
            if graph.app_root != selected_app.app_path:
                self._refuse("Pinokio action graph differs from selected app")
                raise PinokioSourceMismatch("Pinokio action graph differs from selected app")
            self._bound = True
            self._append(
                "immutable-custody-bound",
                "observed",
                {
                    "app_id": selected_app.app_id,
                    "app_path": selected_app.app_path,
                    "action_graph_sha256": graph.graph_sha256,
                    "engine_identity_sha256": _sha256(engine_identity.encode()),
                },
            )

    def record_untrusted_census(self, incarnations: Sequence[PinokioProcessIncarnation]) -> None:
        """Record candidate process identities, marking them non-authoritative."""
        with self._lock:
            if not self._bound:
                raise PinokioCustodyRefused("Pinokio custody observer is not bound")
            identities = {(item.pid, item.birth_id) for item in incarnations}
            if len(identities) != len(incarnations):
                self._refuse("duplicate Pinokio controller/descendant incarnation")
                raise PinokioCustodyRefused("duplicate Pinokio controller/descendant incarnation")
            self._append(
                "untrusted-controller-census",
                "unknown",
                {"count": len(incarnations), "reason": "requires independent B OS observation"},
            )
            self._refuse("Pinokio controller/descendant census is not independently observed")

    def assert_authorizable(self, phase: str) -> None:
        with self._lock:
            if phase not in {"authorization", "finalization"}:
                raise ValueError("unknown Pinokio custody phase")
            if self._overflowed:
                raise PinokioCustodyRefused("Pinokio custody ledger overflowed")
            if self._process_observer is not None:
                try:
                    self._process_observer.assert_authorizable(phase)
                except (ObservationRefused, ObservationUnavailable) as exc:
                    self._refuse(f"B process observer refused: {exc}")
            if self._refusal is not None:
                raise PinokioCustodyRefused(f"Pinokio custody refused: {self._refusal}")
            # Even an attached current observer is polling-only.  Do not turn
            # snapshots into claimed controller continuity.
            raise PinokioCustodyRefused("Pinokio custody has no approved gap-free observer")

    def report(self) -> dict[str, object]:
        with self._lock:
            return {
                "bound": self._bound,
                "event_count": len(self._events),
                "overflowed": self._overflowed,
                "authorization": "refused",
                "refusal": self._refusal or "no approved Pinokio observer",
            }


class PinokioCustodyOwner:
    """Q-shaped owner whose mutation methods are permanently closed for now.

    The method surface matches Q's fixed direct-Pinokio owner validation but
    performs no native-service fallback and no engine/installer action.  A
    future reviewed provider can replace only the refusal implementation after
    it has an authenticated context, installed-app binding, engine settlement,
    and independent controller/descendant observer.
    """

    def __init__(
        self,
        *,
        slot_id: str,
        os_name: str,
        runner: CommandRunner,
        process_probe: ProcessProbe,
        data_probe: DataProbe,
        durable_journal_probe: DurableJournalProbe,
    ) -> None:
        self.slot_id = slot_id
        self.os_name = os_name
        self.runner = runner
        self.process_probe = process_probe
        self.data_probe = data_probe
        self.durable_journal_probe = durable_journal_probe
        self.principal = "pinokio-unbound"
        self.target = f"pinokio-direct-custody:{slot_id}"
        self.raw: list[dict[str, object]] = [
            {"event": "owner-created", "managed_by": "pinokio", "slot_id": slot_id}
        ]
        self.custody_observer = PinokioCustodyObserver()

    def _closed(self, operation: str) -> None:
        self.raw.append({"event": "refused", "operation": operation})
        raise PinokioCustodyRefused(
            "NEEDS authenticated Pinokio engine/app/controller/descendant custody and "
            "gap-free B observer; live Pinokio admission is closed"
        )

    def probe(self) -> Mapping[str, object]:
        self._closed("probe")

    def assert_absent(self) -> None:
        self._closed("assert_absent")

    def custody_binding(self) -> object:
        self._closed("custody_binding")

    def register_start(self) -> Mapping[str, object]:
        self._closed("register_start")

    def registration(self) -> Mapping[str, object]:
        self._closed("registration")

    def observe(self, discovery: Mapping[str, object]) -> Mapping[str, object]:
        del discovery
        self._closed("observe")

    def revalidate(
        self, expected: Mapping[str, object], discovery: Mapping[str, object]
    ) -> Mapping[str, object]:
        del expected, discovery
        self._closed("revalidate")

    def removal_census(self, *args: object, **kwargs: object) -> Mapping[str, object]:
        del args, kwargs
        self._closed("removal_census")

    def _same_instance(self, left: Mapping[str, object], right: Mapping[str, object]) -> bool:
        del left, right
        self._closed("same_instance")

    def observe_external_controller(self, discovery: Mapping[str, object]) -> Mapping[str, object]:
        del discovery
        self._closed("observe_external_controller")

    def terminate_owned_runtime(
        self, expected: Mapping[str, object], discovery: Mapping[str, object]
    ) -> None:
        del expected, discovery
        self._closed("terminate_owned_runtime")

    def observe_absent(self, authorized: Mapping[str, object]) -> Mapping[str, object]:
        del authorized
        self._closed("observe_absent")

    def remove_cleanup(
        self,
        expected: Mapping[str, object],
        discovery: Mapping[str, object],
        authorized: Mapping[str, object],
    ) -> Mapping[str, object]:
        del expected, discovery, authorized
        self._closed("remove_cleanup")


class _PinokioCustodyProvider:
    """Fixed all-six-slot factory; direct slots are intentionally invalid here."""

    def create_owner(
        self,
        context: Mapping[str, object],
        *,
        runner: CommandRunner,
        process_probe: ProcessProbe,
        data_probe: DataProbe,
        durable_journal_probe: DurableJournalProbe,
    ) -> PinokioCustodyOwner:
        slot_id = context.get("slot_id")
        if not isinstance(slot_id, str):
            raise PinokioCustodyRefused("Pinokio provider only accepts the fixed six Pinokio slots")
        mode, os_name = validate_pinokio_session_row(slot_id, context.get("contract"))
        if context.get("mode", mode) != mode or context.get("os", os_name) != os_name:
            raise PinokioCustodyRefused("Pinokio selected mode/OS differs from the fixed session row")
        return PinokioCustodyOwner(
            slot_id=slot_id,
            os_name=os_name,
            runner=runner,
            process_probe=process_probe,
            data_probe=data_probe,
            durable_journal_probe=durable_journal_probe,
        )


PINOKIO_CUSTODY_PROVIDER = _PinokioCustodyProvider()


if __name__ == "__main__":
    raise SystemExit(main())
