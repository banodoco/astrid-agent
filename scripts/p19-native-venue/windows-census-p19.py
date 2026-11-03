"""Run on an isolated Windows standard-user desktop against the exact dbe checkout.

This is a native census predicate probe, not an installed lifecycle test.
Invoke from the checkout root: python PATH_TO_THIS_FILE OUTPUT_DIRECTORY
"""
import ctypes
import hashlib
import getpass
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import traceback

HEAD = 'dbe29b0e9b0fc91a40baf8f4766154096f07303a'
DRIVER_SHA256 = '80c6006c4e1c060683730264feb1087b36682539101bbeff77d7ddf7e3ffdc2e'
WINDOWS_HELPER_SHA256 = 'b932192da5fe6dba0261a281ebd59359e114d1087a2bba0e634cb3c997aef2d1'

def main():
    if sys.platform != 'win32' or len(sys.argv) != 2:
        raise RuntimeError('Requires Windows and exactly one new output directory')
    if ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('Run under a non-elevated standard user token')
    root = Path.cwd().resolve()
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    driver = root / 'scripts' / 'ci_journey.py'
    digest = hashlib.sha256(driver.read_bytes()).hexdigest()
    helper_digest = hashlib.sha256((root / 'scripts' / 'ci_windows_journey.py').read_bytes()).hexdigest()
    if head != HEAD or digest != DRIVER_SHA256 or helper_digest != WINDOWS_HELPER_SHA256:
        raise RuntimeError(f'Wrong checkout or source bytes: {head} {digest} {helper_digest}')
    for cmd in (['git', 'diff', '--quiet'], ['git', 'diff', '--cached', '--quiet']):
        if subprocess.run(cmd, check=False).returncode != 0:
            raise RuntimeError('Tracked checkout changes or git diff failure')
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(root))
    from scripts import ci_journey as journey
    journey.windows.EVIDENCE = out
    result = {'head': head, 'driver_sha256': digest, 'helper_sha256': helper_digest,
              'windows_build': platform.platform(), 'windows_user': getpass.getuser(),
              'standard_token': True, 'checks': {}, 'status': 'fail'}
    child = None
    try:
        def census(label):
            owners = journey.runtime_processes(out)
            shutil.copyfile(out / 'runtime-owner-processes.json', out / (label + '.json'))
            result['checks'][label] = {'pids': list(owners), 'details': owners.details}
            return owners
        before = census('before')
        if before:
            raise RuntimeError(f'Host is not isolated; owners before control: {before}')
        # This is a live matcher control, not a claim that a Runtime is installed.
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)',
                                  '-m', 'runtime_protocol', 'start'])
        result['control_pid'] = child.pid
        owners = census('control-live')
        if list(owners) != [child.pid]:
            raise RuntimeError(f'Expected exactly live control PID {child.pid}; got {owners}')
        if len(owners.details) != 1 or owners.details[0]['matcher'] != 'runtime_protocol-start':
            raise RuntimeError('Control lacked expected native matcher identity')
        child.terminate()
        result['control_exit'] = child.wait(timeout=15)
        child = None
        after = census('after')
        if after:
            raise RuntimeError(f'Owners remain after control exit: {after}')
        result['status'] = 'pass'
    except Exception:
        result['error'] = traceback.format_exc()
        raise
    finally:
        if child is not None:
            child.terminate()
            result['cleanup_control_exit'] = child.wait(timeout=15)
        (out / 'p19-result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result, indent=2), flush=True)

if __name__ == '__main__':
    main()
