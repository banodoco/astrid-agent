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


def _windows_schema_handle_is_safe(handle) -> bool:
    """Admit a schema input with only user and privileged allow ACEs."""
    from runtime_protocol import windows_security as security

    adv, kernel = security._api()
    owner, dacl, descriptor = security.P(), security.P(), security.P()
    result = adv.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None,
                                 ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if result:
        return False
    try:
        user = security._user_sid()
        if (not owner or not adv.IsValidSid(owner)
                or not adv.EqualSid(owner, user)
                or not adv.EqualSid(security._default_owner_sid(), user)
                or not dacl or not adv.IsValidAcl(dacl)):
            return False
        acl = ctypes.cast(dacl, ctypes.POINTER(security._Acl)).contents
        user_access = False
        for index in range(acl.count):
            ace = security.P()
            if not adv.GetAce(dacl, index, ctypes.byref(ace)) or not ace:
                return False
            header = ctypes.string_at(ace, 4)
            size = int.from_bytes(header[2:4], "little")
            if header[0] != 0 or size < 16:
                return False
            sid = security.P(ace.value + 8)
            if not adv.IsValidSid(sid) or 8 + adv.GetLengthSid(sid) > size:
                return False
            if not adv.EqualSid(sid, user):
                if security._sid_text(sid) not in {"S-1-5-18", "S-1-5-32-544"}:
                    return False
            elif not header[1] & 8:  # INHERIT_ONLY_ACE does not grant this object access.
                user_access = True
        return user_access
    finally:
        kernel.LocalFree(descriptor)


def _windows_schema_path_is_safe(path: Path) -> bool:
    from runtime_protocol import windows_security as security

    try:
        with security._handle(path) as (handle, _info):
            return _windows_schema_handle_is_safe(handle)
    except OSError:
        return False


def _windows_installed_source(root: Path) -> Path | None:
    # Only the installer's version/py + version/a layout supplies both sides.
    prefix = Path(sys.prefix).absolute()
    if prefix.name != "py" or not root.is_relative_to(prefix):
        return None
    return prefix.parent / "a" / SCHEMA_RELATIVE_PATH


def verified_schema_path(source_root: Path, configured: str) -> Path:
    root = source_root.absolute()
    expected = root / SCHEMA_RELATIVE_PATH
    if configured != str(expected):
        raise ValueError("local timeline schema origin differs from selected Agent source")
    lock_path = product_resource("runtime.lock.json", installed=root / "runtime.lock.json",
                                 source=root / "runtime.lock.json",
                                 package_root=root / "astrid/resources")
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
                if installed_source is None:
                    safe = safe and owned_by_current_user(path, facts, forbidden=0o022)
            else:
                safe = owned_by_current_user(path, facts, forbidden=0o022)
            if not safe:
                raise ValueError(f"local timeline schema input has a foreign owner/mode: {path.name}")
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
