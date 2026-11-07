"""Bind render validation to the selected Agent's Runtime-pinned contract."""

from __future__ import annotations

import ctypes
import hashlib
import json
import stat
import sys
from pathlib import Path

from astrid.core.execution.platform_helpers import owned_by_current_user
from astrid.core.product_resources import product_resource

SCHEMA_RELATIVE_PATH = Path("astrid/core/timeline/schema/timeline-config.schema.json")
READER_INPUTS = (
    Path("astrid/__init__.py"),
    Path("astrid/core/__init__.py"),
    Path("astrid/core/product_resources.py"),
    Path("astrid/core/timeline/banodoco_schema.py"),
    Path("astrid/core/rendering/_local_schema.py"),
    Path("astrid/core/timeline/__init__.py"),
    Path("astrid/core/rendering/__init__.py"),
)


def _windows_schema_handle_is_safe(handle, *, diagnostic: dict | None = None) -> bool:
    """Admit a schema input with only user and privileged allow ACEs."""
    from runtime_protocol import windows_security as security

    # Only facts observed by this invocation: no SID strings, paths, or ACL dump.
    # Keep the last ACE only, so diagnostic size is independent of ACL length.
    facts = diagnostic if diagnostic is not None else {}
    facts["predicate"] = "security_api"
    adv, kernel = security._api()
    owner, dacl, descriptor = security.P(), security.P(), security.P()
    facts["predicate"] = "security_query"
    result = adv.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None,
                                 ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if result:
        facts["status"] = result
        return False
    try:
        facts["predicate"] = "token_user"
        user = security._user_sid()
        facts["predicate"] = "token_default_owner"
        default_owner = security._default_owner_sid()
        facts["predicate"] = "owner_present"
        if not owner:
            return False
        facts["predicate"] = "owner_valid"
        if not adv.IsValidSid(owner):
            return False
        facts["predicate"] = "owner_matches_token"
        owner_is_user = adv.EqualSid(owner, user)
        if not (owner_is_user or adv.EqualSid(owner, default_owner)):
            facts["owner"] = "neither_user_nor_default"
            return False
        facts["owner"] = "user" if owner_is_user else "default_owner"
        facts["predicate"] = "dacl_present"
        if not dacl:
            return False
        facts["predicate"] = "dacl_valid"
        if not adv.IsValidAcl(dacl):
            return False
        acl = ctypes.cast(dacl, ctypes.POINTER(security._Acl)).contents
        facts["ace_count"] = acl.count
        user_access = 0
        facts["user_access"] = user_access
        for index in range(acl.count):
            # Discard the previous ACE's facts before any potentially failing call.
            for key in ("ace_type", "ace_flags", "ace_size", "ace_mask", "trustee"):
                facts.pop(key, None)
            facts.update(predicate="ace_read", ace_index=index)
            ace = security.P()
            if not adv.GetAce(dacl, index, ctypes.byref(ace)) or not ace:
                return False
            header = ctypes.string_at(ace, 4)
            size = int.from_bytes(header[2:4], "little")
            facts.update(predicate="ace_basic_allow", ace_type=header[0],
                         ace_flags=header[1], ace_size=size)
            if header[0] != 0 or size < 16:
                return False
            sid = security.P(ace.value + 8)
            facts["predicate"] = "ace_sid_valid"
            if not adv.IsValidSid(sid):
                return False
            facts["predicate"] = "ace_sid_fits"
            if 8 + adv.GetLengthSid(sid) > size:
                return False
            # A basic, size-checked ACE has a DWORD mask before its validated SID.
            facts["ace_mask"] = int.from_bytes(ctypes.string_at(ace.value + 4, 4), "little")
            facts["predicate"] = "ace_trustee_allowed"
            if not adv.EqualSid(sid, user):
                trustee = security._sid_text(sid)
                facts["trustee"] = {
                    "S-1-5-18": "system", "S-1-5-32-544": "administrators",
                    "S-1-5-32-545": "users", "S-1-5-11": "authenticated_users",
                    "S-1-1-0": "everyone",
                }.get(trustee, "other")
                if trustee not in {"S-1-5-18", "S-1-5-32-544"}:
                    return False
            elif not header[1] & 8:  # INHERIT_ONLY_ACE does not grant this object access.
                mask = int.from_bytes(ctypes.string_at(ace.value + 4, 4), "little")
                # ACE masks contain effective file-specific rights: read/list and execute/traverse.
                user_access |= mask
                facts["trustee"] = "user"
                facts["user_access"] = user_access
            else:
                facts["trustee"] = "user_inherit_only"
        facts.update(predicate="user_read_traverse", required_access=0x21)
        return user_access & 0x21 == 0x21
    finally:
        kernel.LocalFree(descriptor)


def _windows_schema_rejection(diagnostic: dict) -> None:
    """Best-effort, bounded stderr evidence; never affect admission or cleanup."""
    try:
        # All values originate above or are numeric OS error codes, never exception text.
        record = json.dumps(diagnostic, sort_keys=True, separators=(",", ":"))
        sys.stderr.write("windows_schema_rejection " + record[:1024] + "\n")
    except Exception:  # noqa: BLE001 - a broken diagnostic sink must not alter the guard.
        pass


def _windows_schema_path_is_safe(path: Path) -> bool:
    from runtime_protocol import windows_security as security

    diagnostic = {"predicate": "handle_open"}
    try:
        with security._handle(path) as (handle, _info):
            safe = _windows_schema_handle_is_safe(handle, diagnostic=diagnostic)
    except OSError as exc:
        diagnostic.update(errno=exc.errno, winerror=getattr(exc, "winerror", None))
        _windows_schema_rejection(diagnostic)
        return False
    if not safe:
        _windows_schema_rejection(diagnostic)
    return safe


def _windows_installed_source(root: Path) -> Path | None:
    # Only the installer's version/py + version/a layout supplies both sides.
    prefix = Path(sys.prefix).absolute()
    if prefix.name != "py" or not root.is_relative_to(prefix):
        return None
    return prefix.parent / "a" / SCHEMA_RELATIVE_PATH


def runtime_lock_path(root: Path) -> Path:
    """The one resolution of the Runtime pin: the installer secures exactly this path."""
    return product_resource("runtime.lock.json", installed=root / "runtime.lock.json",
                            source=root / "runtime.lock.json",
                            package_root=root / "astrid/resources")


def verified_schema_path(source_root: Path, configured: str) -> Path:
    root = source_root.absolute()
    expected = root / SCHEMA_RELATIVE_PATH
    if configured != str(expected):
        raise ValueError("local timeline schema origin differs from selected Agent source")
    lock_path = runtime_lock_path(root)
    installed_source = _windows_installed_source(root) if sys.platform == "win32" else None
    if installed_source is not None:
        source_root = Path(sys.prefix).absolute().parent / "a"
        for path in (installed_source, *installed_source.parents):
            if not _windows_schema_path_is_safe(path):
                raise ValueError("local timeline schema input has a foreign owner/mode: source")
            if path == source_root:
                break
    for leaf in (expected, lock_path, *(root / p for p in READER_INPUTS)):
        if leaf.resolve(strict=True) != leaf:
            raise ValueError("local timeline schema source is aliased")
        for path in (leaf, *leaf.parents):
            if path.is_symlink():
                raise ValueError("local timeline schema source is aliased")
            facts = path.stat()
            if sys.platform == "win32":
                safe = _windows_schema_path_is_safe(path)
            else:
                safe = owned_by_current_user(path, facts, forbidden=0o022)
            if not safe:
                detail = ""
                if (sys.platform != "win32" and stat.S_ISDIR(facts.st_mode)
                        and facts.st_mode & 0o022):
                    detail = (f"; timeline schema input directory {path} is group- or "
                              f"world-writable; run: chmod 755 {path}")
                raise ValueError(
                    f"local timeline schema input has a foreign owner/mode: {path.name}{detail}")
            if path == root:
                break
        facts = leaf.stat()
        if not stat.S_ISREG(facts.st_mode):
            raise ValueError(f"local timeline schema input is not a regular file: {leaf.name}")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    files = lock.get("files") if isinstance(lock, dict) else None
    pin = files.get(SCHEMA_RELATIVE_PATH.as_posix()) if isinstance(files, dict) else None
    if (not isinstance(pin, dict)
            or pin.get("from") != "contract/schemas/timeline-config.schema.json"):
        raise ValueError("local timeline schema has no approved Runtime source pin")
    actual = "sha256:" + hashlib.sha256(expected.read_bytes()).hexdigest()
    if pin.get("sha256") != actual:
        raise ValueError("local timeline schema does not match its Runtime source pin")
    return expected


def load_verified_schema(source_root: Path, configured: str) -> dict:
    """Use the canonical reader without accepting an ambient or cached contract."""
    path = verified_schema_path(source_root, configured)
    from astrid.core.timeline import banodoco_schema

    if (Path(banodoco_schema.__file__).absolute()
            != source_root / "astrid/core/timeline/banodoco_schema.py"
            or banodoco_schema._VENDORED_SCHEMA != path):
        raise ValueError("canonical timeline schema reader has a foreign origin")
    schema = banodoco_schema.load_vendored_timeline_schema()
    raw = path.read_bytes()
    if schema != json.loads(raw):
        raise ValueError("canonical timeline schema reader returned a stale contract")
    verified_schema_path(source_root, configured)
    json.dumps(schema, allow_nan=False)

    # All references belong to this pinned document; never resolve a remote
    # resource, including in an otherwise unused definition.
    def check_refs(value):
        if isinstance(value, dict):
            if "$ref" in value:
                ref = value["$ref"]
                if not isinstance(ref, str) or not ref.startswith("#/"):
                    raise ValueError("local timeline schema has a foreign reference")
                target = schema
                for token in ref[2:].split("/"):
                    target = target[token.replace("~1", "/").replace("~0", "~")]
            for child in value.values():
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)

    check_refs(schema)
    import jsonschema

    jsonschema.validators.validator_for(schema).check_schema(schema)
    return schema
