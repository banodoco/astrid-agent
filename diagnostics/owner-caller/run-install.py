"""Run the unmodified journey install while instrumenting only its bootstrap venv."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

caller = Path(__file__).resolve().parent
agent = Path.cwd()
evidence = Path(os.environ['RUNNER_TEMP']) / 'X5-windows-journey'
out = evidence / 'owner-capture'
out.mkdir(parents=True, exist_ok=True)
tmp = Path(os.environ['RUNNER_TEMP']) / 'owner-caller-temp'
tmp.mkdir(parents=True, exist_ok=True)
env = dict(os.environ, TEMP=str(tmp), TMP=str(tmp), OWNER_CAPTURE_DIR=str(out),
           OWNER_CAPTURE_HELPER=str(caller / 'capture-timeline-acl.ps1'),
           OWNER_CAPTURE_SHELL=sys.argv[1])
stop = threading.Event()
def inject():
    while not stop.is_set():
        for site in tmp.glob('astrid-install-*/venv/Lib/site-packages'):
            target = site / 'sitecustomize.py'
            if site.is_dir() and not target.exists():
                try:
                    shutil.copyfile(caller / 'sitecustomize.py', target)
                    (out / 'injected-path.txt').write_text(str(target), encoding='utf-8')
                except OSError as exc:
                    (out / 'watcher-error.txt').write_text(repr(exc), encoding='utf-8')
        stop.wait(.05)
watcher = threading.Thread(target=inject, daemon=True)
watcher.start()
try:
    result = subprocess.run([sys.executable, '-u', str(agent / 'scripts/ci_windows_journey.py'),
                             'install', '--installer-shell', sys.argv[1], '--source-override'], env=env)
finally:
    stop.set()
    watcher.join()
missing = []
for phase in ('before', 'after'):
    try:
        meta = json.loads((out / ('installer-' + phase + '.json')).read_text(encoding='utf-8'))
        if 'installer_token' not in meta:
            missing.append(phase + ': installer token unavailable')
        if meta.get('helper_exit') != 0:
            missing.append(phase + ': helper did not succeed')
        for name in ('acl-', 'identity-', 'sha256-', 'token-'):
            suffix = '.json' if name == 'acl-' else '.txt'
            if not (out / (name + phase + suffix)).is_file():
                missing.append(name + phase)
    except (OSError, ValueError) as exc:
        missing.append(phase + ': ' + repr(exc))
(out / 'capture-status.json').write_text(json.dumps({'journey_install_exit': result.returncode,
    'capture_missing_or_failed': missing}, indent=2), encoding='utf-8')
raise SystemExit(result.returncode or (1 if missing else 0))
