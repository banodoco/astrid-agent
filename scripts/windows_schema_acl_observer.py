"""Read-only Windows schema ACL observations for the journey evidence artifact."""
from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time

SCHEMA = Path('astrid/core/timeline/schema/timeline-config.schema.json')
MAX_MATCHES = 32
MAX_CACHE_DIRECTORIES = 10000
MAX_OBSERVED_PATHS = 256
OBSERVATION_SECONDS = 90


def _read_token_sids(adv, kernel, last_error):
    """Read the observer process token; retain independent errors for each SID."""
    from ctypes import wintypes as w
    token = w.HANDLE()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        return {'error': f'OpenProcessToken: {last_error()}'}
    result = {}
    try:
        for name, information_class in (('user', 1), ('owner', 4)):
            size = w.DWORD()
            ok = adv.GetTokenInformation(token, information_class, None, 0, ctypes.byref(size))
            error = last_error()
            if ok or error != 122 or not ctypes.sizeof(ctypes.c_void_p) <= size.value <= 1024 * 1024:
                result[name + '_error'] = f'GetTokenInformation size: {error}; size={size.value}'
                continue
            buffer = ctypes.create_string_buffer(size.value)
            if not adv.GetTokenInformation(token, information_class, buffer, size.value, ctypes.byref(size)):
                result[name + '_error'] = f'GetTokenInformation: {last_error()}'
                continue
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid or not adv.IsValidSid(sid):
                result[name + '_error'] = 'IsValidSid: invalid SID'
                continue
            text = ctypes.c_void_p()
            if not adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                result[name + '_error'] = f'ConvertSidToStringSidW: {last_error()}'
                continue
            try:
                result[name + '_sid'] = ctypes.wstring_at(text)
            finally:
                kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)
    return result


def native_token_observation():
    """Get actual TokenUser and TokenOwner SIDs without changing the token."""
    from ctypes import wintypes as w
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    ptr = ctypes.c_void_p
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.OpenProcessToken.restype = w.BOOL
    adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ptr, w.DWORD, ctypes.POINTER(w.DWORD)]
    adv.GetTokenInformation.restype = w.BOOL
    adv.IsValidSid.argtypes = [ptr]
    adv.IsValidSid.restype = w.BOOL
    adv.ConvertSidToStringSidW.argtypes = [ptr, ctypes.POINTER(ptr)]
    adv.ConvertSidToStringSidW.restype = w.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.LocalFree.argtypes = [ptr]
    kernel.LocalFree.restype = ptr
    return _read_token_sids(adv, kernel, ctypes.get_last_error)


def ancestors(path: Path, root: Path):
    """Yield the leaf through its designated root, including missing paths."""
    if path != root and root not in path.parents:
        raise ValueError('path is outside observation root')
    yield path
    while path != root:
        path = path.parent
        yield path


def is_reparse(path: Path):
    try:
        st = path.lstat()
        return path.is_symlink() or bool(getattr(st, 'st_file_attributes', 0) & 0x400)
    except OSError:
        return True


def cache_schemas(root: Path):
    matches, errors = [], []
    if not root.is_dir():
        return matches, ['cache root not present']
    if is_reparse(root):
        return matches, ['cache root is a reparse point; traversal skipped']
    def onerror(error):
        errors.append(f'{type(error).__name__}: {error}')
    # Never follow directory links out of the cache or around a cycle.
    for index, (directory, dirs, files) in enumerate(os.walk(root, followlinks=False, onerror=onerror)):
        if index >= MAX_CACHE_DIRECTORIES:
            errors.append(f'directory limit reached ({MAX_CACHE_DIRECTORIES}); results may be incomplete')
            break
        dirs[:] = sorted(d for d in dirs if not is_reparse(Path(directory) / d))
        if SCHEMA.name in files and not is_reparse(Path(directory) / SCHEMA.name):
            matches.append(Path(directory) / SCHEMA.name)
            if len(matches) >= MAX_MATCHES:
                errors.append(f'match limit reached ({MAX_MATCHES}); results may be incomplete')
                break
    return matches, errors


def native_observation(path: Path):
    """Get owner SID, DACL SDDL and file identity using read-only Win32 calls."""
    from ctypes import wintypes as w
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    ptr = ctypes.c_void_p
    adv.GetNamedSecurityInfoW.argtypes = [w.LPWSTR, w.DWORD, w.DWORD, ctypes.POINTER(ptr),
                                         ctypes.POINTER(ptr), ctypes.POINTER(ptr), ctypes.POINTER(ptr), ctypes.POINTER(ptr)]
    adv.GetNamedSecurityInfoW.restype = w.DWORD
    adv.ConvertSidToStringSidW.argtypes = [ptr, ctypes.POINTER(ptr)]
    adv.ConvertSidToStringSidW.restype = w.BOOL
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ptr, w.DWORD, w.DWORD, ctypes.POINTER(ptr), ctypes.POINTER(w.DWORD)]
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = w.BOOL
    kernel.LocalFree.argtypes = [ptr]
    kernel.LocalFree.restype = ptr
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ptr, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    class FileInfo(ctypes.Structure):
        _fields_ = [('attributes', w.DWORD), ('creation', w.FILETIME), ('access', w.FILETIME),
                    ('write', w.FILETIME), ('volume', w.DWORD), ('size_high', w.DWORD),
                    ('size_low', w.DWORD), ('links', w.DWORD), ('index_high', w.DWORD), ('index_low', w.DWORD)]
    kernel.GetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.POINTER(FileInfo)]
    kernel.GetFileInformationByHandle.restype = w.BOOL
    result = {}
    owner, dacl, descriptor = ptr(), ptr(), ptr()
    code = adv.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None,
                                      ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if code:
        result['security_error'] = f'GetNamedSecurityInfoW: {code}'
    else:
        try:
            result['dacl_is_null'] = not bool(dacl)
            sid, sddl = ptr(), ptr()
            if owner and adv.ConvertSidToStringSidW(owner, ctypes.byref(sid)):
                try:
                    result['owner_sid'] = ctypes.wstring_at(sid)
                finally:
                    kernel.LocalFree(sid)
            else:
                result['owner_error'] = f'ConvertSidToStringSidW: {ctypes.get_last_error()}'
            if adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, ctypes.byref(sddl), None):
                try:
                    result['dacl_sddl'] = ctypes.wstring_at(sddl)
                finally:
                    kernel.LocalFree(sddl)
            else:
                result['dacl_error'] = f'ConvertSecurityDescriptorToStringSecurityDescriptorW: {ctypes.get_last_error()}'
        finally:
            kernel.LocalFree(descriptor)
    handle = kernel.CreateFileW(str(path), 0, 7, None, 3, 0x02000000, None)
    if handle == ptr(-1).value:
        result['identity_error'] = f'CreateFileW: {ctypes.get_last_error()}'
    else:
        try:
            info = FileInfo()
            if kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
                result['file_identity'] = {'volume_serial': info.volume,
                                           'file_index': (info.index_high << 32) | info.index_low,
                                           'link_count': info.links}
            else:
                result['identity_error'] = f'GetFileInformationByHandle: {ctypes.get_last_error()}'
        finally:
            kernel.CloseHandle(handle)
    return result


def collect(root: Path, phase: str):
    deadline = time.monotonic() + OBSERVATION_SECONDS
    result = {'phase': phase, 'image_os': os.environ.get('ImageOS'),
              'image_version': os.environ.get('ImageVersion'), 'uv': {}, 'paths': []}
    bootstrap_uv = Path(os.environ.get('LOCALAPPDATA', '')) / 'Astrid Installer' / 'bootstrap' / 'uv.exe'
    executable = str(bootstrap_uv) if bootstrap_uv.is_file() else 'uv'
    result['uv']['executable'] = 'installer-bootstrap' if bootstrap_uv.is_file() else 'PATH'
    try:
        uv = subprocess.run([executable, 'cache', 'dir'], capture_output=True, text=True, timeout=30, check=False)
        result['uv']['exit_code'] = uv.returncode
        cache = Path(uv.stdout.strip()) if uv.returncode == 0 and uv.stdout.strip() else None
        if cache is None or not cache.is_absolute():
            result['uv']['error'] = 'uv cache dir did not return an absolute path'
            cache = None
    except (OSError, subprocess.SubprocessError) as exc:
        cache = None
        result['uv']['error'] = type(exc).__name__
    if cache is None:
        fallback = os.environ.get('UV_CACHE_DIR')
        if not fallback and os.environ.get('LOCALAPPDATA'):
            fallback = str(Path(os.environ['LOCALAPPDATA']) / 'uv' / 'cache')
        if fallback and Path(fallback).is_absolute():
            cache = Path(fallback)
            result['uv']['root_source'] = 'environment/default fallback (unverified)'
    else:
        result['uv']['root_source'] = 'uv cache dir'
    # Only relative paths and explicit root aliases are emitted, avoiding personal paths.
    aliases = [(str(root), '<program>')]
    if cache:
        aliases.append((str(cache), '<uv-cache>'))
    def sanitize(value):
        for prefix, alias in sorted(aliases, key=lambda pair: -len(pair[0])):
            value = re.sub(re.escape(prefix), lambda _: alias, value, flags=re.IGNORECASE)
        return value
    groups = []
    versions = root / 'versions'
    candidates = sorted(p for p in versions.iterdir() if p.is_dir() and not is_reparse(p)) if versions.is_dir() else []
    result['version_roots'] = [sanitize(str(p)) for p in candidates]
    for version in candidates:
        groups.extend([('staged', version / 'a' / SCHEMA, version),
                       ('installed', version / 'py' / 'Lib' / 'site-packages' / SCHEMA, version)])
    if cache:
        matches, errors = cache_schemas(cache)
        result['uv'].update(root='<uv-cache>', matches=len(matches), scan_errors=[sanitize(e) for e in errors])
        groups.append(('cache-root', cache, cache))
        groups.extend(('cache', p, cache) for p in matches)
    seen = set()
    for kind, leaf, boundary in groups:
        for path in ancestors(leaf, boundary):
            if len(result['paths']) >= MAX_OBSERVED_PATHS or time.monotonic() >= deadline:
                result['truncated'] = 'path count or observation time limit reached'
                return result
            key = (kind, str(path))
            if key in seen:
                continue
            seen.add(key)
            item = {'kind': kind, 'path': sanitize(str(path)), 'exists': path.exists()}
            if item['exists']:
                schema_leaf = path == leaf and kind in ('staged', 'installed', 'cache')
                if schema_leaf:
                    item['observation_started_utc'] = datetime.now(timezone.utc).isoformat()
                    try:
                        item['observer_process_token'] = native_token_observation()
                    except Exception as exc:
                        item['observer_process_token'] = {'error': type(exc).__name__}
                try:
                    item.update(native_observation(path))
                except Exception as exc:
                    item['native_error'] = sanitize(f'{type(exc).__name__}: {exc}')
                if schema_leaf:
                    item['native_observation_finished_utc'] = datetime.now(timezone.utc).isoformat()
                try:
                    proc = subprocess.run(['icacls', str(path)], capture_output=True, text=True,
                                          errors='replace', timeout=5, check=False)
                    item['icacls'] = {'exit_code': proc.returncode, 'stdout': sanitize(proc.stdout),
                                      'stderr': sanitize(proc.stderr)}
                except (OSError, subprocess.SubprocessError) as exc:
                    item['icacls'] = {'error': sanitize(f'{type(exc).__name__}: {exc}')}
            result['paths'].append(item)
    return result


def observe(root: Path, evidence: Path, phase: str):
    """Never replace the lifecycle result with a diagnostic failure."""
    if sys.platform != 'win32':
        return
    try:
        data = collect(root, phase)
    except Exception as exc:
        # Do not serialize exception text here: it can contain an unaliased path.
        data = {'phase': phase, 'observer_error': type(exc).__name__}
    try:
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / f'windows-schema-acl-{phase}.json').write_text(
            json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    except Exception as exc:
        print(f'Schema ACL observer could not write evidence: {type(exc).__name__}', file=sys.stderr)
