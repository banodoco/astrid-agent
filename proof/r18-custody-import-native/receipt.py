import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
out = Path('out')
def git(name):
    try:
        return subprocess.check_output(['git', '-C', name, 'rev-parse', 'HEAD'], text=True, stderr=subprocess.STDOUT).strip()
    except Exception as exc:
        return 'unavailable: ' + str(exc)
receipt = {'format': 'r18-custody-import-native/v1', 'run_id': os.getenv('GITHUB_RUN_ID'),
 'attempt': os.getenv('GITHUB_RUN_ATTEMPT'), 'source_ref': os.getenv('GITHUB_REF'),
 'source_sha': os.getenv('GITHUB_SHA'), 'workflow_ref': os.getenv('GITHUB_WORKFLOW_REF'),
 'workflow_sha': os.getenv('GITHUB_WORKFLOW_SHA'), 'runner_os': platform.platform(), 'uid': os.getuid(),
 'requested': {'agent': '23252c5da5b89b87ea38570b4b87b3a66f9e955e',
               'runtime': 'a95e0e836234bc6d27ba557ba1297da95ed55290', 'runtime_status': 'provisional'},
 'effective': {name: git(name) for name in ('venue', 'agent', 'runtime')},
 'command': 'python3 -B venue/proof/r18-custody-import-native/run.py',
 'inner_exit': (out / 'exit-code.txt').read_text().strip(),
 'limitations': ['Installed helper and fixture-based identity/refusal checks only; no launchd lifecycle, product acceptance, or protected review.',
                 'A missing step, child observation, or exit is not a pass.']}
(out / 'receipt.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
files = sorted(p for p in out.rglob('*') if p.is_file() and p.name != 'SHA256SUMS')
(out / 'SHA256SUMS').write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(out)}\n' for p in files))
