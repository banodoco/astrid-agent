import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import astrid.installer.custody as custody
import runtime_protocol

scratch, source = map(Path, sys.argv[1:])
module = Path(custody.__file__).resolve()
runtime = Path(runtime_protocol.__file__).resolve()
prefix = Path(sys.prefix).resolve()
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
distributions = {}
for name in ('astrid', 'astrid-runtime'):
    dist = metadata.distribution(name)
    direct = json.loads(dist.read_text('direct_url.json') or '{}')
    distributions[name] = {'version': dist.version, 'location': str(Path(dist.locate_file('')).resolve()), 'direct_url': direct}
    assert not direct.get('dir_info', {}).get('editable', False)
    assert Path(dist.locate_file('')).resolve().is_relative_to(prefix)
assert sys.flags.isolated and module.is_relative_to(prefix) and runtime.is_relative_to(prefix)
assert digest(module) == digest(source)
(scratch / 'helper-root').mkdir(mode=0o700, exist_ok=True)
command = [sys.executable, '-I', '-B', str(module), str(scratch / 'helper-root'), str(scratch / 'helper.lock')]
print(json.dumps({'uid': os.getuid(), 'euid': os.geteuid(), 'executable': sys.executable, 'prefix': str(prefix),
                  'isolated_parent': sys.flags.isolated, 'custody_file': str(module), 'custody_sha256': digest(module),
                  'source_sha256': digest(source), 'runtime_protocol': str(runtime), 'distributions': distributions,
                  'command': command}), flush=True)
p = subprocess.run(command, input='', text=True, capture_output=True)
print(json.dumps({'child_stdout': p.stdout, 'child_stderr': p.stderr, 'child_exit': p.returncode}), flush=True)
assert p.returncode == 0 and p.stderr == '' and json.loads(p.stdout) == {'acquired': True}
