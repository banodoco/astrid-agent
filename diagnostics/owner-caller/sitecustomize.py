"""Test-only injection into the bootstrap venv; wraps the candidate unchanged."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback

out = Path(os.environ['OWNER_CAPTURE_DIR'])
out.mkdir(parents=True, exist_ok=True)

def token():
    class Luid(ctypes.Structure):
        _fields_ = [('low', ctypes.c_uint32), ('high', ctypes.c_int32)]
    class Stats(ctypes.Structure):
        _fields_ = [('token', Luid), ('authentication', Luid), ('expiration', ctypes.c_int64),
                    ('type', ctypes.c_int32), ('impersonation', ctypes.c_int32),
                    ('dynamic_charged', ctypes.c_uint32), ('dynamic_available', ctypes.c_uint32),
                    ('group_count', ctypes.c_uint32), ('privilege_count', ctypes.c_uint32), ('modified', Luid)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    h = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(h)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        stats, length = Stats(), wintypes.DWORD()
        if not advapi.GetTokenInformation(h, 10, ctypes.byref(stats), ctypes.sizeof(stats), ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        return {'token_id': [stats.token.high, stats.token.low],
                'authentication_id': [stats.authentication.high, stats.authentication.low],
                'type': stats.type, 'impersonation': stats.impersonation,
                'groups': stats.group_count, 'privileges': stats.privilege_count}
    finally:
        kernel.CloseHandle(h)

try:
    from astrid.installer.backend import Backend
    import astrid.installer.backend as backend
    original = Backend._secure_installed_timeline_inputs
    def capture(root, phase):
        meta = {'installer_pid': os.getpid(), 'python': sys.executable, 'root': str(root),
                'backend': backend.__file__}
        try:
            meta['backend_sha256'] = hashlib.sha256(Path(backend.__file__).read_bytes()).hexdigest()
            meta['installer_token'] = token()
        except Exception as exc:
            meta['installer_token_error'] = repr(exc)
        command = [os.environ.get('OWNER_CAPTURE_SHELL', 'powershell.exe'),
                   '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                   '-File', os.environ['OWNER_CAPTURE_HELPER'], '-ProductRoot', str(root),
                   '-OutputDirectory', str(out), '-Phase', phase]
        try:
            proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
            meta['helper_exit'] = proc.returncode
            (out / ('helper-' + phase + '.log')).write_text(proc.stdout, encoding='utf-8')
        except Exception as exc:
            meta['helper_error'] = repr(exc)
        (out / ('installer-' + phase + '.json')).write_text(json.dumps(meta, indent=2), encoding='utf-8')
    def safe_capture(root, phase):
        try:
            capture(root, phase)
        except Exception:
            try:
                (out / ('capture-error-' + phase + '.txt')).write_text(traceback.format_exc(), encoding='utf-8')
            except OSError:
                pass
    def wrapped(root):
        safe_capture(root, 'before')
        try:
            return original(root)
        finally:
            safe_capture(root, 'after')
    Backend._secure_installed_timeline_inputs = staticmethod(wrapped)
    (out / 'injection-loaded.txt').write_text('pid=%s\nbackend=%s\n' % (os.getpid(), backend.__file__), encoding='utf-8')
except BaseException:
    (out / 'injection-error.txt').write_text(traceback.format_exc(), encoding='utf-8')
