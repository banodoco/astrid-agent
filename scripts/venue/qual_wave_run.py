#!/usr/bin/env python3
"""Run a validated row and save evidence; never award a qualification verdict."""
import glob
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import qual_wave

packet = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
rows = qual_wave.validate(packet)
row = next(r for r in rows if r['id'] == os.environ['ROW_ID'])
out = pathlib.Path('venue-evidence') / row['id']
out.mkdir(parents=True, exist_ok=False)
(out / 'row.json').write_text(json.dumps(row, indent=2) + '\n', encoding='utf-8')
identity = {'matrix_sha256': packet['matrix_sha256'], 'requested_tuple': packet['published_tuple'],
            'run_id': os.environ.get('GITHUB_RUN_ID'), 'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
            'workflow_sha': os.environ.get('GITHUB_SHA'), 'row_id': row['id'], 'checkouts': {}}
for repo in ('agent', 'runtime', 'app', 'pinokio'):
    identity['checkouts'][repo] = subprocess.check_output(['git', '-C', repo, 'rev-parse', 'HEAD'], text=True).strip()
(out / 'identity.json').write_text(json.dumps(identity, indent=2) + '\n', encoding='utf-8')
with (out / 'command.log').open('wb') as log:
    result = subprocess.run(['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', row['entry_command']], stdout=log, stderr=subprocess.STDOUT)
(out / 'exit-code.txt').write_text(str(result.returncode) + '\n')
files = []
for pattern in row['expected_artifacts']:
    for name in glob.glob(pattern, recursive=True):
        src = pathlib.Path(name)
        if src.is_file() and not src.is_symlink():
            dest = out / 'artifacts' / src.as_posix().lstrip('/')
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            files.append({'source': name, 'sha256': hashlib.sha256(dest.read_bytes()).hexdigest()})
(out / 'artifacts.json').write_text(json.dumps(files, indent=2) + '\n', encoding='utf-8')
