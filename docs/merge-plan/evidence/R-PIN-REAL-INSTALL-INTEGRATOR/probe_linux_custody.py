"""Run real pinned custody constructors; this is a dependency canary, not R-24 proof."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime-checkout', type=Path, default=root / 'runtime')
parser.add_argument('--agent-checkout', type=Path, default=root / 'agent')
parser.add_argument('--scratch', type=Path, default=root / 'probe-runs')
args = parser.parse_args()
args.scratch.mkdir(parents=True, exist_ok=True)
sys.path[:0] = [str(args.runtime_checkout.resolve(strict=True)), str(args.agent_checkout.resolve(strict=True))]
from astrid_runtime import custody_broker as runtime_broker
from astrid.core.execution import custody_broker as agent_broker

run = Path(tempfile.mkdtemp(prefix='linux-custody-', dir=args.scratch))
rows = []
for role, module in [('relay', runtime_broker), ('engine', agent_broker)]:
    ledger = run / role
    path = Path(module.__file__)
    row = {'role': role, 'module': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    try:
        broker = module.RoleBoundCustodyBroker(role=role, identity_provider=module.default_process_identity,
                                              ledger_root=ledger)
    except module.CustodyError as error:
        row.update(available=False, error_type=type(error).__name__, error=str(error))
    else:
        row.update(available=True)
        broker.abort_before_spawn()
    row['ledger_created'] = ledger.exists()
    rows.append(row)
print(json.dumps({'schema': 'astrid.pin.linux-custody-canary.v1', 'uid': os.getuid(),
                  'euid': os.geteuid(), 'platform': sys.platform, 'python': sys.executable,
                  'python_version': sys.version, 'run_root': str(run), 'brokers': rows,
                  'worker_profile_generated': False, 'journey_qualification_claimed': False}, sort_keys=True))
sys.exit(0 if all(row['available'] for row in rows) else 2)
