"""Test-only injection into the bootstrap venv; wraps the candidate unchanged."""
import ctypes
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
    h = ctypes.c_void_p()
    if not ctypes.windll.advapi32.OpenProcessToken(ctypes.windll.kernel32.GetCurrentProcess(), 8, ctypes.byref(h)):
        raise ctypes.WinError()
    try:
        stats, length = Stats(), ctypes.c_uint32()
        if not ctypes.windll.advapi32.GetTokenInformation(h, 10, ctypes.byref(stats), ctypes.sizeof(stats), ctypes.byref(length)):
            raise ctypes.WinError()
        return {'token_id': [stats.token.high, stats.token.low],
                'authentication_id': [stats.authentication.high, stats.authentication.low],
                'type': stats.type, 'impersonation': stats.impersonation,
                'groups': stats.group_count, 'privileges': stats.privilege_count}
    finally:
        ctypes.windll.kernel32.CloseHandle(h)

try:
    from astrid.installer.backend import Backend
    import astrid.installer.backend as backend
    original = Backend._secure_installed_timeline_inputs
    def capture(root, phase):
        meta = {'installer_pid': os.getpid(), 'python': sys.executable, 'root': str(root),
                'backend': backend.__file__, 'backend_sha256': hashlib.sha256(Path(backend.__file__).read_bytes()).hexdigest(),
                'installer_token': token()}
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                   '-File', os.environ['OWNER_CAPTURE_HELPER'], '-ProductRoot', str(root),
                   '-OutputDirectory', str(out), '-Phase', phase]
        try:
            proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
            meta['helper_exit'] = proc.returncode
            (out / ('helper-' + phase + '.log')).write_text(proc.stdout, encoding='utf-8')
        except Exception as exc:
            meta['helper_error'] = repr(exc)
        (out / ('installer-' + phase + '.json')).write_text(json.dumps(meta, indent=2), encoding='utf-8')
    def wrapped(root):
        capture(root, 'before')
        try:
            return original(root)
        finally:
            capture(root, 'after')
    Backend._secure_installed_timeline_inputs = staticmethod(wrapped)
    (out / 'injection-loaded.txt').write_text('pid=%s\nbackend=%s\n' % (os.getpid(), backend.__file__), encoding='utf-8')
except BaseException:
    (out / 'injection-error.txt').write_text(traceback.format_exc(), encoding='utf-8')
