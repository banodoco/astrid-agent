#!/usr/bin/env python3
"""One isolated hosted diagnostic install. Never authorizes observer custody."""
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from m46_selected_install_preflight import REFS, Refusal, absolute_existing_directory, exact_keys, git, pairs_no_duplicates, validate_checkout
import m46_selected_install_executor as admission

BASE = 'b7b9f61fab4aa727254cace56699787869b27cbb'
DAEMON = 'add4a674ad1ba95bb62ec427fce519fe5bf0e9bf'
PTERM = '5be43718701c448b43daf0ce51d8935f170065bb'
REQUEST = 'astrid.m46.selected-install-docker-request.v2'
RESULT = 'astrid.m46.selected-install-docker-result.v2'
CLOSURE = {
 'pinokio/install.js':'60f4473e91b769af0480098b96a36500f3c1628ac33a7617f68df4de1fe436e2',
 'pinokio/lib.js':'59bb0a556b425dafb4d986aaab54f0dbd23a988814a0d64a6cb4437c52d3d998',
 'pinokio/shell.mjs':'d8c2dfa774c0bda07d29f923f8b84d06055e76a8f4c5b6192980be2dffa8a7e9',
 'pinokio/environment.js':'0da2a9d2186e7ce28d40a17d9943c973aa6edd79613caaf31328722d92db0a41',
 'pinokio/platforms/posix.js':'20cf174da5668607f2ab0d8f7856475e1d7bd0363bd7c86512658130ce73279b',
 'pinokio/platforms/windows.js':'bfcce0736bdff41afae7cbb36c579b2ea49420b4d91bc40c5e7263ada509c519',
 'pinokio/session.mjs':'460ff77a8cda2b711c785a607e4a399f8c33ddc903496e4c769b717d010aad99',
 'pinokio/lifecycle.mjs':'0ddc6a5596dc02a2f33afba799495c7834ebb193df33fe15c376c2b60859a3ad',
 'pinokio/update-stage.mjs':'e563c3d3fb8e5a3d11ddc3ba84f9f15d146eac0ccb359a3f403bb3c68dbda551',
 'pinokio/installer-lock.py':'809937b3e6a4f2b48ef1cc75cd262f3acd828f9252cbbb4825a144dabb59fa2f',
}

def digest(data): return hashlib.sha256(data).hexdigest()
def validate(req):
    exact_keys(req, ('schema','runner_head','request_id','action','mode','checkouts','engine_checkout','pterm_checkout','output_directory'), 'request')
    if req['schema'] != REQUEST: raise Refusal('unsupported request schema')
    old = {k:v for k,v in req.items() if k not in ('engine_checkout','pterm_checkout')}
    old['schema'] = admission.REQUEST_SCHEMA
    admission.BASE = BASE
    roots, out, hashes = admission.validate_request(old, Path(__file__).with_name('m46_selected_install_executor.py'))
    extra = [absolute_existing_directory(req[k], k) for k in ('engine_checkout','pterm_checkout')]
    paths = [*roots.values(), out, *extra]
    if any(a == b or a in b.parents or b in a.parents for i,a in enumerate(paths) for b in paths[i+1:]): raise Refusal('paths overlap')
    for root, ref in zip(extra, (DAEMON, PTERM)): validate_checkout(root, ref)
    for name, expected in CLOSURE.items():
        p = roots['pinokio'] / name
        if p.is_symlink() or p.resolve(strict=True) != p or digest(p.read_bytes()) != expected: raise Refusal('closure mismatch: ' + name)
        hashes['pinokio/' + name] = expected
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_SHA') != req['runner_head']:
        raise Refusal('hosted action-run checkout binding unavailable or mismatched')
    return roots, out, hashes, extra

def archive(root, destination):
    destination.mkdir()
    data = subprocess.check_output(['git','-C',str(root),'archive','HEAD'], timeout=60)
    with tempfile.TemporaryFile() as f:
        f.write(data); f.seek(0)
        with tarfile.open(fileobj=f) as tar:
            for member in tar.getmembers():
                if member.issym() or member.islnk() or member.name.startswith('/') or '..' in Path(member.name).parts: raise Refusal('unsafe archive member')
            tar.extractall(destination, filter='data')

def safe_regular(home, path):
    # Container output is untrusted: never follow a symlink out of its private home.
    relative = path.relative_to(home)
    probe = home
    for part in relative.parts:
        probe = probe / part
        if probe.is_symlink(): raise ValueError('symlink in readback path')
    if not path.is_file() or path.stat().st_size > 65536: raise ValueError('not a bounded regular file')
    return path

def run(req):
    roots, out, hashes, (engine, pterm) = validate(req)
    result = {'schema':RESULT, 'status':'blocked', 'reason':'not-started', 'request_id':req['request_id'],
      'request_sha256':digest(json.dumps(req,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()),
      'runner_head':req['runner_head'], 'tuple':REFS, 'action':'pinokio/install.js', 'mode':'backend',
      'source_sha256':hashes, 'engine_ref':DAEMON, 'pterm_ref':PTERM,
      'hosted': {k:os.environ.get(k) for k in ('GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_JOB','GITHUB_SHA','RUNNER_NAME','RUNNER_OS','RUNNER_ARCH','ImageOS','ImageVersion')},
      'commands':[], 'container':None, 'action_invoked':False, 'child_exit':None, 'readback':None,
      'observer_binding':'not-produced','settlement':'unknown','slot_receipt':'not-produced', 'artifacts':{}}
    name = 'm46-' + req['request_id']
    image = name + ':local'
    work = None
    created = False
    image_created = False
    def command(label, argv, timeout=120, env=None):
        path = out / (label + '.log')
        def limit(): resource.setrlimit(resource.RLIMIT_FSIZE, (1048576,1048576))
        try:
            with open(path,'xb') as stream:
                p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout, env=env, preexec_fn=limit)
            code = p.returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            with open(path,'ab') as stream: stream.write(('\n' + str(e) + '\n').encode())
            code = None
        result['commands'].append({'label':label,'argv':[str(x) for x in argv],'exit_code':code,'log':path.name})
        if code != 0:
            result['reason'] = label + '-failed'
            raise Refusal(label + ' failed')
        return path.read_text(errors='replace').strip()
    try:
        tmp = os.environ.get('RUNNER_TEMP')
        if not tmp or not Path(tmp).is_absolute(): raise Refusal('RUNNER_TEMP missing')
        work = Path(tempfile.mkdtemp(prefix='m46-', dir=tmp))
        command('docker-info',['docker','info','--format','{{.ID}}'],20)
        archive(engine, work/'engine'); archive(pterm, work/'pterm')
        (work/'home'/'api').mkdir(parents=True); archive(roots['pinokio'], work/'home'/'api'/'astrid')
        (work/'capture').mkdir()
        command('engine-patch',['node',str(roots['runner']/'.github/venue/m46-engine-patch.cjs'),str(work/'engine'),str(roots['pinokio'])])
        command('pterm-dependencies',['npm','ci','--omit=dev','--prefix',str(work/'pterm'),'--silent'],300)
        prior_image = subprocess.run(['docker','image','inspect',image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)
        if prior_image.returncode == 0: raise Refusal('image name already exists')
        command('docker-build',['docker','build','-t',image,str(work/'engine')],1200)
        image_created = True
        image_id = command('image-identity',['docker','image','inspect','--format','{{.Id}}',image])
        existing = subprocess.run(['docker','inspect',name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)
        if existing.returncode == 0: raise Refusal('container name already exists')
        created = True  # also clean up if docker run times out after creating it
        cid = command('container-start',['docker','run','-d','--name',name,'-p','127.0.0.1::42000',
          '-e','PINOKIO_HTTPS_ACTIVE=0','-e','PINOKIO_NETWORK_ACTIVE=0', '-e','ASTRID_APP_REF='+REFS['app'],
          '-e','ASTRID_M46_ENGINE_CAPTURE_FILE=/capture/engine.jsonl','-e','NODE_OPTIONS=--require=/instrument/m46-trace.cjs',
          '-v',str(work/'home')+':/data/pinokio','-v',str(work/'capture')+':/capture',
          '-v',str(roots['runner']/'.github/venue')+':/instrument:ro',image],120)
        if not re.fullmatch('[0-9a-f]{64}',cid) or not re.fullmatch('sha256:[0-9a-f]{64}',image_id): raise Refusal('ambiguous container identity')
        result['container'] = {'id':cid,'name':name,'image_id':image_id}
        port_text = command('container-port',['docker','port',cid,'42000/tcp'])
        m = re.fullmatch(r'127\.0\.0\.1:(\d+)',port_text)
        if not m: raise Refusal('ambiguous daemon port')
        port = m.group(1)
        ready = False
        for _ in range(120):
            try:
                import urllib.request
                with urllib.request.urlopen('http://127.0.0.1:'+port+'/pinokio/version',timeout=2) as response:
                    data = response.read(65537)
                if len(data) <= 65536:
                    (out/'daemon-version.json').write_bytes(data); ready = True; break
            except OSError: time.sleep(2)
        if not ready: raise Refusal('daemon readiness unavailable')
        env = dict(os.environ, PTERM_DIR=str(work/'pterm'), M46_OUTPUT=str(out), M46_PORT=port, M46_REQUEST_ID=req['request_id'])
        try: command('dispatch',['node',str(roots['runner']/'.github/venue/m46-dispatch.cjs')],750,env)
        except Refusal: pass  # still collect independent child and post-action evidence
        for filename in ('engine.jsonl','child.jsonl'):
            source = work/'capture'/filename
            if source.is_file() and not source.is_symlink() and source.stat().st_size <= 65536: shutil.copyfile(source,out/filename)
        try:
            rows = [json.loads(x) for x in (out/'engine.jsonl').read_text().splitlines()]
            wanted = digest(b'/data/pinokio/api/astrid/pinokio/install.js')
            result['action_invoked'] = any(x.get('kind') == 'action-resolved' and x.get('request_id') == req['request_id'] and x.get('request_path_sha256') == wanted and x.get('action') == 'run' and x.get('graph_count') == 4 for x in rows)
        except (OSError,ValueError): pass
        try:
            rows = [json.loads(x) for x in (out/'child.jsonl').read_text().splitlines()]
            launches = [x for x in rows if x.get('kind') == 'spawn' and x.get('command') == '/bin/bash' and REFS['app'] in json.dumps(x.get('args')) and '/data/pinokio/api/astrid/app/astrid' in json.dumps(x.get('args'))]
            closes = [x for x in rows if x.get('kind') == 'close' and len(launches) == 1 and x.get('pid') == launches[0].get('pid') and x.get('parent_pid') == launches[0].get('parent_pid')]
            if len(closes) == 1 and ((isinstance(closes[0].get('code'), int) and closes[0].get('signal') is None) or (closes[0].get('code') is None and isinstance(closes[0].get('signal'),str))): result['child_exit'] = {'spawn':launches[0], 'close':closes[0]}
        except (OSError,ValueError): pass
        base = work/'home/api/astrid/app'
        # Preserve bounded partial readback even when it is absent or invalid.
        diagnostic = {}
        for rel in ('install-mode.json','astrid/current','astrid/install.json'):
            path = base / rel
            try:
                data = safe_regular(work/'home', path).read_bytes()
                dest = 'raw-' + rel.replace('/','-')
                (out/dest).write_bytes(data)
                diagnostic[rel] = {'present':True,'sha256':digest(data),'bytes':len(data)}
            except (OSError, ValueError): diagnostic[rel] = {'present':False}
        (out/'readback-diagnostic.json').write_text(json.dumps(diagnostic,sort_keys=True,indent=2)+'\n')
        try:
            mode_file = base/'install-mode.json'; current_file = base/'astrid/current'
            mode = json.loads(safe_regular(work/'home',mode_file).read_bytes(), object_pairs_hook=pairs_no_duplicates)
            current = safe_regular(work/'home',current_file).read_text().strip()
            if mode != {'schema_version':1,'mode':'backend'} or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',current): raise ValueError('invalid mode/current')
            version_file = base/'astrid/versions'/current/'version.json'
            version = json.loads(safe_regular(work/'home',version_file).read_bytes(), object_pairs_hook=pairs_no_duplicates)
            install_file = base/'astrid/install.json'
            install = json.loads(safe_regular(work/'home',install_file).read_bytes(), object_pairs_hook=pairs_no_duplicates)
            container_root = '/data/pinokio/api/astrid/app/astrid/versions/' + current
            node = version.get('env',{}).get('ASTRID_NODE_EXECUTABLE')
            if (version.get('identity') != {'release':1,'managed_by':'pinokio'} or version.get('root') != container_root
                or not isinstance(node,str) or not node.startswith(container_root + '/') or '..' in Path(node).parts
                or install.get('owner') != 'haus.astrid.runtime' or install.get('managed_by') != 'pinokio'):
                raise ValueError('invalid installation identity')
            local_node = work/'home'/node.removeprefix('/data/pinokio/')
            if not local_node.is_file() or not local_node.resolve().is_relative_to((base/'astrid/versions'/current).resolve()): raise ValueError('invalid node executable')
            node_sha = digest(local_node.read_bytes())
            node_version = command('node-version',['docker','exec',cid,node,'--version'])
            if node_version != 'v20.19.4': raise ValueError('unexpected node version')
            for source, dest in ((mode_file,'install-mode.json'),(current_file,'current.txt'),(version_file,'version.json'),(install_file,'install.json')):
                if source.stat().st_size > 65536: raise ValueError('readback too large')
                shutil.copyfile(source,out/dest)
            result['readback'] = {'mode':mode,'current':current,'identity':version['identity'],'node_executable':node,'node_sha256':node_sha,'node_version':node_version}
        except (OSError, ValueError, TypeError): pass
        result['reason'] = ('action-resolution-missing' if not result['action_invoked'] else 'child-exit-missing' if not result['child_exit'] else 'child-failed' if result['child_exit']['close']['code'] != 0 else 'invalid-readback' if not result['readback'] else 'none')
        if result['reason'] == 'none': result['status'] = 'observed'
    except (Refusal,OSError,subprocess.SubprocessError,ValueError) as e:
        if result['reason'] == 'not-started': result['reason'] = str(e)
    finally:
        prior_reason = result['reason']
        if created:
            try: command('container-logs',['docker','logs','--tail','500',name],30)
            except Refusal: pass
            try: command('container-remove',['docker','rm','-f',name],30)
            except Refusal: pass
        if image_created:
            try: command('image-remove',['docker','image','rm',image],30)
            except Refusal: pass
        result['reason'] = prior_reason
        if work: shutil.rmtree(work,ignore_errors=True)
        for p in out.iterdir():
            if p.is_file() and not p.is_symlink() and p.name != 'result.json': result['artifacts'][p.name] = {'sha256':digest(p.read_bytes()),'bytes':p.stat().st_size}
        (out/'result.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
    return 0 if result['status'] == 'observed' else 2

def main():
    try:
        if len(sys.argv) != 2 or not os.path.isabs(sys.argv[1]): raise Refusal('absolute request path required')
        data = Path(sys.argv[1]).read_bytes()
        if len(data)>65536: raise Refusal('request too large')
        return run(json.loads(data,object_pairs_hook=pairs_no_duplicates))
    except (OSError,ValueError,UnicodeError) as e:
        print('selected-install refused:',e,file=sys.stderr); return 2
if __name__ == '__main__': sys.exit(main())
