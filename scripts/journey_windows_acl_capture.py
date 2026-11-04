"""Bounded, read-only snapshot of the failed fresh-install schema's Windows ACL.

This is a later path-based observation, not the verifier's handle or creation history.
No exception text, paths, account names, SIDs, or file contents enter the record.
"""
from __future__ import annotations

import ctypes
import datetime
import json
import os
import stat
from pathlib import Path

SCHEMA = Path('astrid/core/timeline/schema/timeline-config.schema.json')
MAX_ACES = 32
OUTPUT = 'windows-schema-acl.json'


def _unalias(path: Path, boundary: Path) -> str:
    """Reject reparse points at every component under the known run boundary."""
    try:
        parts = path.relative_to(boundary).parts
        if '..' in parts:
            return 'alias'
        current = boundary
        for part in ('', *parts):
            if part:
                current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                return 'alias'
        return 'ok'
    except FileNotFoundError:
        return 'missing'
    except (OSError, ValueError):
        return 'unavailable'


def _candidate(root: Path, boundary: Path):
    versions = root / 'versions'
    status = _unalias(versions, boundary)
    if status != 'ok':
        return status, None
    try:
        with os.scandir(versions) as entries:
            found = []
            for entry in entries:
                found.append(entry.name)
                if len(found) > 1:
                    return 'multiple', None
        if not found:
            return 'missing', None
        candidate = versions / found[0]
        status = _unalias(candidate, boundary)
        if status != 'ok':
            return status, None
        if not candidate.is_dir():
            return 'unavailable', None
        return 'selected', candidate
    except OSError:
        return 'unavailable', None


def _aces(count, dacl, get_ace, valid_sid, sid_len, category):
    rows = []
    for index in range(min(count, MAX_ACES)):
        ace = ctypes.c_void_p()
        if not get_ace(dacl, index, ctypes.byref(ace)) or not ace:
            rows.append({'status': 'error'})
            break
        header = ctypes.string_at(ace, 4)
        kind, flags, size = header[0], header[1], int.from_bytes(header[2:4], 'little')
        row = {'type': kind, 'flags': flags}
        # Only basic allow/deny/audit ACEs have a SID at this fixed offset.
        if kind in (0, 1, 2) and size >= 16:
            row['mask'] = int.from_bytes(ctypes.string_at(ace.value + 4, 4), 'little')
            sid = ctypes.c_void_p(ace.value + 8)
            sid_header = ctypes.string_at(sid, 8)
            length = 8 + 4 * sid_header[1]
            if length <= size - 8 and valid_sid(sid) and sid_len(sid) == length:
                row['trustee'] = category(sid)
            else:
                row['trustee'] = 'invalid'
        rows.append(row)
    return rows


def _query(path: Path) -> dict:
    """Query a single opened object, with bounded ACEs and fixed trustee categories."""
    from ctypes import wintypes as w
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    P = ctypes.c_void_p
    D = w.DWORD
    class ACL(ctypes.Structure):
        _fields_ = [('revision', w.BYTE), ('unused', w.BYTE), ('size', w.WORD),
                    ('count', w.WORD), ('unused2', w.WORD)]
    class INFO(ctypes.Structure):
        _fields_ = [('attributes', D), ('creation', w.FILETIME), ('access', w.FILETIME),
                    ('write', w.FILETIME), ('volume', D), ('size_high', D), ('size_low', D),
                    ('links', D), ('index_high', D), ('index_low', D)]
    def api(lib, name, args, result):
        fn = getattr(lib, name); fn.argtypes = args; fn.restype = result; return fn
    create = api(kernel, 'CreateFileW', [w.LPCWSTR, D, D, P, D, D, w.HANDLE], w.HANDLE)
    close = api(kernel, 'CloseHandle', [w.HANDLE], w.BOOL)
    free = api(kernel, 'LocalFree', [P], P)
    info_fn = api(kernel, 'GetFileInformationByHandle', [w.HANDLE, ctypes.POINTER(INFO)], w.BOOL)
    query = api(adv, 'GetSecurityInfo', [w.HANDLE, D, D, ctypes.POINTER(P), P, ctypes.POINTER(P), P, ctypes.POINTER(P)], D)
    control_fn = api(adv, 'GetSecurityDescriptorControl', [P, ctypes.POINTER(w.WORD), ctypes.POINTER(D)], w.BOOL)
    get_ace = api(adv, 'GetAce', [P, D, ctypes.POINTER(P)], w.BOOL)
    valid_acl = api(adv, 'IsValidAcl', [P], w.BOOL)
    valid_sid = api(adv, 'IsValidSid', [P], w.BOOL)
    sid_len = api(adv, 'GetLengthSid', [P], D)
    well_known = api(adv, 'IsWellKnownSid', [P, ctypes.c_int], w.BOOL)
    equal_sid = api(adv, 'EqualSid', [P, P], w.BOOL)
    token_open = api(adv, 'OpenProcessToken', [w.HANDLE, D, ctypes.POINTER(w.HANDLE)], w.BOOL)
    token_info = api(adv, 'GetTokenInformation', [w.HANDLE, D, P, D, ctypes.POINTER(D)], w.BOOL)
    current_process = api(kernel, 'GetCurrentProcess', [], w.HANDLE)
    # Keep token buffers alive while their embedded SID pointers are used.
    token_buffers = []
    token_sids = {}
    token = w.HANDLE()
    if token_open(current_process(), 8, ctypes.byref(token)):
        try:
            for kind, label in ((1, 'user'), (4, 'default_owner')):
                needed = D()
                token_info(token, kind, None, 0, ctypes.byref(needed))
                if 0 < needed.value <= 65536:
                    buf = ctypes.create_string_buffer(needed.value)
                    if token_info(token, kind, buf, needed, ctypes.byref(needed)):
                        token_buffers.append(buf)
                        token_sids[label] = ctypes.cast(buf, ctypes.POINTER(P)).contents
        finally:
            close(token)
    # READ_CONTROL, share all, OPEN_EXISTING, OPEN_REPARSE_POINT | BACKUP_SEMANTICS
    handle = create(str(path), 0x20000, 7, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        return {'status': 'open-error', 'code': ctypes.get_last_error()}
    descriptor, owner, dacl = P(), P(), P()
    try:
        facts = INFO()
        if not info_fn(handle, ctypes.byref(facts)):
            return {'status': 'identity-error', 'code': ctypes.get_last_error()}
        if facts.attributes & 0x400:
            return {'status': 'alias'}
        result = query(handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if result:
            return {'status': 'security-error', 'code': int(result)}
        def category(sid):
            if not sid or not valid_sid(sid):
                return 'invalid'
            for kind, label in ((22, 'system'), (26, 'administrators'), (27, 'users'),
                                (17, 'authenticated_users'), (1, 'everyone')):
                if well_known(sid, kind):
                    return label
            if token_sids.get('user') and equal_sid(sid, token_sids['user']):
                return 'user'
            return 'other'
        def owner_category(sid):
            if sid and valid_sid(sid):
                for label, token_sid in token_sids.items():
                    if token_sid and equal_sid(sid, token_sid):
                        return label
            return category(sid)
        control, revision = w.WORD(), D()
        if not control_fn(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            return {'status': 'control-error', 'code': ctypes.get_last_error()}
        result = {'status': 'ok', 'owner': owner_category(owner), 'control': int(control.value),
                  'dacl_protected': bool(control.value & 0x1000),
                  'dacl_auto_inherited': bool(control.value & 0x400),
                  'identity': {'volume': int(facts.volume),
                               'file_id': (int(facts.index_high) << 32) | int(facts.index_low),
                               'links': int(facts.links)}}
        if not dacl:
            result['dacl'] = 'null'
            return result
        if not valid_acl(dacl):
            result['dacl'] = 'invalid'
            return result
        count = ctypes.cast(dacl, ctypes.POINTER(ACL)).contents.count
        result.update(dacl='present', ace_count=int(count), truncated=count > MAX_ACES, aces=[])
        result['aces'] = _aces(count, dacl, get_ace, valid_sid, sid_len, category)
        return result
    finally:
        if descriptor:
            free(descriptor)
        close(handle)


def collect(root: Path, run_root: Path) -> dict:
    record = {'schema_version': 1, 'timing': 'after-bootstrap-failure-before-cleanup',
              'observed_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'note': 'path-based later observation; not the verifier handle or creation history',
              'objects': {}}
    status, version = _candidate(root, run_root)
    record['candidate'] = status
    def observe(label, path):
        status = _unalias(path, run_root)
        if status != 'ok':
            record['objects'][label] = {'status': status}
            return
        try:
            record['objects'][label] = _query(path)
        except Exception:
            record['objects'][label] = {'status': 'query-error'}
    observe('program', root)
    observe('temp', run_root / 'tmp')
    observe('localappdata', run_root / 'localappdata')
    cache = os.environ.get('UV_CACHE_DIR')
    if cache and Path(cache).is_absolute():
        try:
            Path(cache).relative_to(run_root)
        except ValueError:
            record['objects']['cache'] = {'status': 'outside-scope'}
        else:
            observe('cache', Path(cache))
    else:
        record['objects']['cache'] = {'status': 'not-identifiable'}
    if version is not None:
        leaf = version / 'py' / 'Lib' / 'site-packages' / SCHEMA
        path = leaf
        index = 0
        while path != root:
            observe('installed' if index == 0 else f'ancestor_{index}', path)
            path = path.parent
            index += 1
        observe('source', version / 'a' / SCHEMA)
    return record


def capture(root: Path, run_root: Path, evidence: Path) -> None:
    """Best effort only: no capture failure may affect the installer or cleanup."""
    try:
        result = collect(root, run_root)
        (evidence / OUTPUT).write_text(json.dumps(result, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    except Exception:
        pass
