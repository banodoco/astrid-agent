"""Bind render validation to the selected Agent's Runtime-pinned contract."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

from astrid.core.execution.platform_helpers import owned_by_current_user

SCHEMA_RELATIVE_PATH = Path("astrid/core/timeline/schema/timeline-config.schema.json")
READER_INPUTS = (
    Path("astrid/__init__.py"),
    Path("astrid/core/__init__.py"),
    Path("astrid/core/timeline/banodoco_schema.py"),
    Path("astrid/core/rendering/_local_schema.py"),
    Path("astrid/core/timeline/__init__.py"),
    Path("astrid/core/rendering/__init__.py"),
)


def _windows_context(paths: list[Path]) -> tuple[dict, dict]:
    """Read the same native owner/DACL and process token used by admission."""
    import ctypes
    from ctypes import wintypes as w
    from runtime_protocol import windows_security as security

    adv, kernel = security._api()
    convert = adv.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR),
                        ctypes.POINTER(w.DWORD)]
    convert.restype = w.BOOL
    volume_path = kernel.GetVolumePathNameW
    volume_path.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
    volume_path.restype = w.BOOL
    volume_info = kernel.GetVolumeInformationW
    volume_info.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD, ctypes.POINTER(w.DWORD),
                            ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), w.LPWSTR, w.DWORD]
    volume_info.restype = w.BOOL
    process = {}
    try:
        process['user_sid'] = security._sid_text(security._user_sid())
        process['owner_sid'] = security._sid_text(security._default_owner_sid())
        token = w.HANDLE()
        security._check(adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)))
        try:
            elevated, size = w.DWORD(), w.DWORD()
            security._check(adv.GetTokenInformation(token, 20, ctypes.byref(elevated),
                                                    ctypes.sizeof(elevated), ctypes.byref(size)))
            process['elevated'] = bool(elevated.value)
        finally:
            kernel.CloseHandle(token)
    except Exception:
        process['error'] = 'unavailable'

    result = {}
    for path in paths:
        entry = {}
        result[str(path)] = entry
        try:
            with security._handle(path) as (handle, _info):
                owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
                error = adv.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None,
                                            ctypes.byref(dacl), None, ctypes.byref(descriptor))
                if error:
                    raise OSError(error, 'GetSecurityInfo failed')
                try:
                    entry['owner_sid'] = security._sid_text(owner) if owner.value else None
                    text = w.LPWSTR()
                    security._check(convert(descriptor, 1, 5, ctypes.byref(text), None))
                    try:
                        entry['owner_dacl_sddl'] = text.value
                    finally:
                        kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
                finally:
                    kernel.LocalFree(descriptor)
        except Exception:
            entry['security_error'] = 'unavailable'
        try:
            volume, filesystem = ctypes.create_unicode_buffer(32768), ctypes.create_unicode_buffer(256)
            security._check(volume_path(str(path), volume, len(volume)))
            security._check(volume_info(volume, None, 0, None, None, None, filesystem, len(filesystem)))
            entry['filesystem'] = filesystem.value
        except Exception:
            entry['filesystem'] = 'unavailable'
    return process, result


def _posix_filesystem(path: Path) -> str:
    # Linux mountinfo supplies filesystem names and handles nested mounts.
    if not sys.platform.startswith('linux'):
        return 'unavailable'
    try:
        import re
        candidate = str(path.resolve())
        matches = []
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            left, right = line.split(' - ', 1)
            mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), left.split()[4])
            if candidate == mount or candidate.startswith(mount.rstrip('/') + '/'):
                matches.append((len(mount), right.split()[0]))
        return max(matches)[1] if matches else 'unavailable'
    except Exception:
        return 'unavailable'


def _owner_failure_diagnostics(root: Path, configured: str, leaf: Path, rejected: Path) -> str:
    """Best effort only: diagnostic failures must never replace admission failure."""
    try:
        expected = root / SCHEMA_RELATIVE_PATH
        paths = [expected, expected.parent, leaf, rejected, rejected.parent]
        # During installation the validator is passed only the installed root.
        # The checkout is a sibling of the venv in the installer's version tree.
        source = None
        prefix = Path(sys.prefix).absolute()
        if root.is_relative_to(prefix) and prefix.name == 'py':
            candidate = prefix.parent / 'a' / SCHEMA_RELATIVE_PATH
            if candidate.is_file():
                source = candidate
                paths.extend((source, source.parent))
        paths = list(dict.fromkeys(paths))
        details = {'configured_schema_resolved': str(Path(configured).resolve()),
                   'selected_schema_resolved': str(expected.resolve()),
                   'source_schema_inferred_resolved': str(source.resolve()) if source else 'unavailable',
                   'rejected_leaf_resolved': str(leaf.resolve()),
                   'rejected_path_resolved': str(rejected.resolve())}
        if sys.platform == 'win32':
            details['process'], details['paths'] = _windows_context(paths)
        else:
            details['process'] = {'uid': os.getuid(), 'euid': os.geteuid(),
                                  'gid': os.getgid(), 'egid': os.getegid(),
                                  'elevated': os.geteuid() == 0}
            details['paths'] = {}
            for path in paths:
                try:
                    facts = path.stat()
                    details['paths'][str(path)] = {'uid': facts.st_uid, 'gid': facts.st_gid,
                        'mode': oct(stat.S_IMODE(facts.st_mode)), 'filesystem': _posix_filesystem(path)}
                except OSError:
                    details['paths'][str(path)] = {'stat': 'unavailable',
                                                   'filesystem': _posix_filesystem(path)}
        return json.dumps(details, ensure_ascii=True, sort_keys=True)
    except Exception:
        return '{"diagnostics": "unavailable"}'


def verified_schema_path(source_root: Path, configured: str) -> Path:
    root = source_root.absolute()
    expected = root / SCHEMA_RELATIVE_PATH
    if configured != str(expected):
        raise ValueError("local timeline schema origin differs from selected Agent source")
    for leaf in (expected, root / "runtime.lock.json", *(root / p for p in READER_INPUTS)):
        if leaf.resolve(strict=True) != leaf:
            raise ValueError("local timeline schema source is aliased")
        for path in (leaf, *leaf.parents):
            if path.is_symlink():
                raise ValueError("local timeline schema source is aliased")
            facts = path.stat()
            if not owned_by_current_user(path, facts, forbidden=0o022):
                raise ValueError(
                    f"local timeline schema input has a foreign owner/mode: {path.name}; "
                    f"diagnostics: {_owner_failure_diagnostics(root, configured, leaf, path)}")
            if path == root:
                break
        facts = leaf.stat()
        if not stat.S_ISREG(facts.st_mode):
            raise ValueError(f"local timeline schema input is not a regular file: {leaf.name}")
    lock = json.loads((root / "runtime.lock.json").read_text(encoding="utf-8"))
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
