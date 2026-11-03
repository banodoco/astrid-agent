"""Check the known Worker source's entrypoint and private ABI without any profile."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
root=Path(__file__).resolve().parent
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--worker-source',type=Path,default=root/'evidence/legacy-supervisor.py')
parser.add_argument('--runtime-checkout',type=Path,default=root/'runtime')
args=parser.parse_args()
worker=args.worker_source.resolve(strict=True)
runtime=args.runtime_checkout.resolve(strict=True)/'runtime_protocol/local_worker_composition.py'
def constant(path,name):
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in node.targets):
            return ast.literal_eval(node.value)
    raise RuntimeError('missing selected protocol constant')
selected=constant(runtime,'CONTROL_VERSION')
reports=[]
for explicit_main in (False,True):
    parent,child=socket.socketpair()
    parent.settimeout(5)
    if explicit_main:
        code='import importlib.util,sys;s=importlib.util.spec_from_file_location("selected_worker",sys.argv[1]);m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m);sys.exit(m.main(sys.argv[2:]))'
        argv=[sys.executable,'-B','-c',code,str(worker),'--prepared-control-fd',str(child.fileno())]
    else:
        argv=[sys.executable,'-B',str(worker),'--prepared-control-fd',str(child.fileno())]
    process=subprocess.Popen(argv,pass_fds=(child.fileno(),),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    child.close()
    parent.sendall(json.dumps({'version':selected,'command':'report'}).encode()+b'\n')
    received=b''; transport_error=None
    try:
        while not received.endswith(b'\n'):
            chunk=parent.recv(4096)
            if not chunk: break
            received+=chunk
    except (ConnectionError,OSError) as error:
        transport_error=type(error).__name__
    finally:
        parent.close()
    stdout,stderr=process.communicate(timeout=5)
    reports.append({'mode':'explicit-main-protocol-only' if explicit_main else 'normal-module-body',
                    'exit':process.returncode,'response':json.loads(received) if received else None,
                    'transport_error':transport_error,'stderr':stderr.decode(),'stdout':stdout.decode()})
result={'schema':'astrid.pin.legacy-worker-canary.v1','uid':__import__('os').getuid(),
        'runtime_control_version':selected,'worker_control_version':constant(worker,'CONTROL_VERSION'),
        'worker_sha256':hashlib.sha256(worker.read_bytes()).hexdigest(),'runs':reports,
        'worker_profile_generated':False,'journey_qualification_claimed':False}
print(json.dumps(result,sort_keys=True))
sys.exit(0 if reports[0]['response'] and result['runtime_control_version']==result['worker_control_version'] else 2)
