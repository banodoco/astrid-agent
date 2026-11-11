#!/usr/bin/env python3
"""R12 A2 one-shot CI runner; no arbitrary ref or selector inputs."""
import hashlib, json, os, pathlib, re, subprocess, sys, time, traceback
from sanitize import sanitized_result, write_receipt
from stages import StageTracker

HERE = pathlib.Path(__file__).resolve().parent
MANIFEST_SHA = '885b264862a0e6e90698ed8a76a6b5b30e2dbeb1f3846ea96b356913b1a9e771'
BEFORE = '0000000000000000000000000000000000000000'
PINS = {
'r10': ('72462b04fa036736eb7fc9cb654def5f4ea2158c','52ec0578963847693a0df758d9bc1d55325f518f','e2905d068d5851b9c32856d32b8251852a01d6dd','96360a27a53c4608f780f7b370f747a2aa3c7ca8'),
'r11': ('a105de3a4852cb61b9647dfe2c920118bd29f272','4f5477cb0f5b03d28bec991a1fe34edcd8fc309f','e65f4941d09b1cb4d1dd5aae6171c73e648706f4','f2948817eeb94af8dee76be21f6d673724f1606e'),
'base': ('313bbd46f1e24bfe635655d78b27ab0b321145d5','42ae24aff8d2cb854f874ce134161077a3ba3641','e65f4941d09b1cb4d1dd5aae6171c73e648706f4','f2948817eeb94af8dee76be21f6d673724f1606e'),
'candidate': ('ec48555d8298d155390ffbb37d1950c3e80b43a4','42ae24aff8d2cb854f874ce134161077a3ba3641','8d065a8fef3dd602d5462e27d8eca088cf4e8262','f2948817eeb94af8dee76be21f6d673724f1606e')}
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def validate(side, group):
    if side not in PINS or not re.fullmatch(r'0(?:[2-4][0-9]|19|5[0-2])', group): raise ValueError('invalid side/group')
    manifest = HERE/'inventory/final-batches.json'
    if digest(manifest) != MANIFEST_SHA: raise ValueError('manifest digest mismatch')
    control = json.loads((HERE/'run-control.json').read_text())
    if control != {'purpose':'r12-a2-stage-019-20261111','before':BEFORE,'manifest_sha256':MANIFEST_SHA}: raise ValueError('control mismatch')
    batches = json.loads(manifest.read_text()); matches = [b for b in batches if b['batch'] == group]
    if len(matches) != 1: raise ValueError('invalid batch')
    b = matches[0]
    if any(not re.fullmatch(r'tests/[A-Za-z0-9_./-]+\.py', x) or '..' in pathlib.PurePosixPath(x).parts for x in b['modules']): raise ValueError('unsafe path')
    by_side = {}
    for name in PINS:
        ids = (HERE/f'inventory/{name}-ids.txt').read_text().splitlines()
        selected = [i for i in ids if i.split('::',1)[0] in b['modules']]
        if len(selected) != b['side_receipts'][name]['ids_count'] or hashlib.sha256(''.join(i+'\n' for i in selected).encode()).hexdigest() != b['side_receipts'][name]['ids_sha256']: raise ValueError('inventory mismatch')
        by_side[name] = selected
    expected = by_side[side]
    absent = {i for i in (HERE/f'inventory/{side}-absent-ids.txt').read_text().splitlines() if i.split('::',1)[0] in b['modules']}
    if absent != set().union(*map(set,by_side.values())) - set(expected): raise ValueError('absence inventory mismatch')
    return b, expected

def run_main(tracker):
    tracker.enter('input_validation')
    side, group = os.environ['A2_SIDE'], os.environ['A2_GROUP']
    b, expected = validate(side, group)
    if os.environ.get('A2_REF') != 'refs/heads/scratch/r12-a2-stage-019-20261111' or group != '019' or os.environ.get('A2_BEFORE') != BEFORE or os.environ.get('A2_ATTEMPT') != '1': raise ValueError('run control mismatch')
    root = pathlib.Path(os.environ['RUNNER_TEMP']).resolve(); receipt = root/'r12-a2-receipt'; out = root/'r12-a2-private'; out.mkdir(exist_ok=False)
    preflight = False; rc = None
    envdir = root/'r12-a2-isolated'; envdir.mkdir(exist_ok=False)
    for d in ('home','tmp','cache','basetemp'): (envdir/d).mkdir()
    env = {'PATH':os.environ['PATH'], 'HOME':str(envdir/'home'), 'TMPDIR':str(envdir/'tmp'), 'XDG_CACHE_HOME':str(envdir/'cache'), 'LANG':'C.UTF-8', 'PYTHONNOUSERSITE':'1', 'PYTHONDONTWRITEBYTECODE':'1', 'PYTHONSAFEPATH':'1', 'GIT_CONFIG_NOSYSTEM':'1', 'GIT_TERMINAL_PROMPT':'0'}
    def run(cmd, cwd=None, log=None, check=True):
        with (out/(log or 'setup.log')).open('ab') as f:
            f.write(('$ '+' '.join(map(str,cmd))+'\n').encode()); f.flush()
            return subprocess.run(list(map(str,cmd)), cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT, check=check).returncode
    def fetch(url, path, sha):
        run(['git','init','-q',path]); run(['git','-C',path,'fetch','-q','--depth=1',url,sha]); run(['git','-C',path,'checkout','-q','--detach','FETCH_HEAD'])
        got = subprocess.check_output(['git','-C',path,'rev-parse','HEAD'],env=env,text=True,stderr=subprocess.DEVNULL).strip()
        if got != sha: raise ValueError('checkout SHA mismatch')
    try:
        tracker.enter('checkout_verification')
        (out/'identity.json').write_text(json.dumps({'side':side,'group':group,'pins':PINS[side], 'manifest_sha256':MANIFEST_SHA,'workflow_sha':os.environ['A2_SHA'],'before':BEFORE},indent=2)+'\n')
        checkout = HERE.parent.parent
        if subprocess.check_output(['git','-C',checkout,'rev-parse','HEAD'],env=env,text=True,stderr=subprocess.DEVNULL).strip() != os.environ['A2_SHA']: raise ValueError('workflow checkout mismatch')
        if sys.version.split()[0] != '3.11.17': raise ValueError('Python version mismatch')
        (out/'python.txt').write_text(sys.version+'\n'+str(pathlib.Path(sys.executable).resolve())+' sha256='+digest(pathlib.Path(sys.executable).resolve())+'\n')
        tracker.enter('source_fetch')
        agent, rt, vc, lc = PINS[side]; co=envdir/'agent'; runtime=envdir/'runtime'; vibe=envdir/'vibecomfy'; lifecycle=envdir/'lifecycle'
        for repo, path, sha in [('astrid-agent',co,agent),('astrid-runtime',runtime,rt),('VibeComfy',vibe,vc),('runpod-lifecycle',lifecycle,lc)]: fetch('https://github.com/banodoco/'+repo+'.git',path,sha)
        tracker.enter('pin_verification')
        if json.loads((co/'runtime.lock.json').read_text())['source_commit'] != rt: raise ValueError('runtime pin mismatch')
        if not re.search(r'runpod-lifecycle @ git\+https://github.com/banodoco/runpod-lifecycle.git@'+lc, (co/'pyproject.toml').read_text()): raise ValueError('lifecycle pin mismatch')
        tracker.enter('dependency_setup')
        run([sys.executable,'-m','pip','install','uv==0.13.5']); run([sys.executable,'-m','uv','venv','--seed','--python',sys.executable,envdir/'venv'])
        py=envdir/'venv/bin/python'; run([sys.executable,'-m','uv','pip','install','--python',py,'-e',str(co)+'[dev]'])
        run([sys.executable,'-m','uv','pip','install','--python',py,'--no-deps',runtime,runtime/'packages/python']); run([sys.executable,'-m','uv','pip','install','--python',py,'--no-deps','-e',runtime])
        tracker.enter('runtime_preflight')
        run([py, HERE/'runtime_preflight.py', co, runtime, vc],cwd=co)
        tracker.enter('toolchain_preflight')
        nodearchive=envdir/'node.tar.xz'; esarchive=envdir/'esbuild.tgz'
        for url,path,sha in [('https://nodejs.org/dist/v22.22.0/node-v22.22.0-linux-x64.tar.xz',nodearchive,'9aa8e9d2298ab68c600bd6fb86a6c13bce11a4eca1ba9b39d79fa021755d7c37'),('https://registry.npmjs.org/@esbuild/linux-x64/-/linux-x64-0.27.7.tgz',esarchive,'2c3eb61a67f174520d3c586ef026430480fa92cfb5f285c82a9040fff41355de')]:
            run(['curl','--fail','--location','--silent','--show-error','--output',path,url]);
            if digest(path)!=sha: raise ValueError('archive hash mismatch')
        (envdir/'node').mkdir(); (envdir/'esbuild').mkdir(); run(['tar','-xf',nodearchive,'--strip-components=1','-C',envdir/'node']); run(['tar','-xf',esarchive,'--strip-components=1','-C',envdir/'esbuild'])
        node=envdir/'node/bin/node'; esbuild=envdir/'esbuild/bin/esbuild'
        for binary,sha,version in [(node,'1bec56ef7cfa9a76f3e0b7c0a87f220eb73f23102b9c0b4c7529a3f7c3ce7c31','v22.22.0'),(esbuild,'c638273fcf95573ca74af586677800b4dd874c55f8f945adb54316d6902ba14b','0.27.7')]:
            if digest(binary)!=sha or subprocess.check_output([binary,'--version'],env=env,text=True,stderr=subprocess.DEVNULL).strip()!=version: raise ValueError('tool verification failed')
        (out/'toolchain.txt').write_text(f'{node} {digest(node)} v22.22.0\n{esbuild} {digest(esbuild)} 0.27.7\n')
        preflight = True
        tracker.enter('selection')
        (out/'expected-ids.txt').write_text(''.join(x+'\n' for x in expected))
        absent=(HERE/f'inventory/{side}-absent-ids.txt').read_text().splitlines(); (out/'absent-ids.txt').write_text(''.join(x+'\n' for x in absent if x.split('::',1)[0] in b['modules']))
        paths=[str(co/x) for x in b['modules'] if (co/x).is_file()]; (out/'modules.json').write_text(json.dumps({'selected':b['modules'],'present':paths},indent=2)+'\n')
        if not expected:
            tracker.finish(True)
            (out/'result.txt').write_text('NO RESULTS: frozen inventory contains zero IDs; explicit absence only\n'); (out/'status.txt').write_text('NO RESULTS: explicit absence\n'); return 0
        if not paths: raise ValueError('expected IDs but no modules present')
        env.update({'PATH':str(node.parent)+':'+str(py.parent)+':'+env['PATH'],'PYTEST_ADDOPTS':'-rA','PYTHONPATH':str(vibe),'VIBECOMFY_SOURCE':str(vibe),'ASTRID_RUNTIME_CHECKOUT':str(runtime),'ASTRID_VIBECOMFY_CHECKOUT':str(vibe),'ASTRID_TEST_LIFECYCLE_CHECKOUT':str(lifecycle),'ASTRID_BUILD_TEST_NODE':str(node),'ASTRID_BUILD_TEST_NODE_VERSION':'22.22.0','ASTRID_BUILD_TEST_ESBUILD':str(esbuild)})
        tracker.enter('pytest')
        cmd=[py,'-m','pytest','-vv','--tb=line','-p','no:cacheprovider','--basetemp='+str(envdir/'basetemp'/('shard-'+group)),'--ignore=tests/core/pack/test_build_hook_contract.py','-k','not browser']+[x for x in b['modules'] if (co/x).is_file()]
        (out/'command.json').write_text(json.dumps(list(map(str,cmd)),indent=2)+'\n'); (out/'start.txt').write_text(time.strftime('%FT%TZ',time.gmtime())+'\n')
        rc=run(cmd,cwd=co,log='pytest.log',check=False); (out/'exit.txt').write_text(str(rc)+'\n'); (out/'end.txt').write_text(time.strftime('%FT%TZ',time.gmtime())+'\n')
        tracker.finish(rc == 0,rc if -255 <= rc <= 255 else None)
        # Sanitizer validates coverage in finally; avoid treating partial output as a pass.
        result = sanitized_result((out/'pytest.log').read_bytes(), expected, [], {}, rc, preflight)
        return rc if result['classification'] == 'COMPLETE' else 3
    except Exception as exc:
        tracker.exception(exc)
        (out/'incomplete.txt').write_text(traceback.format_exc()); return 3
    finally:
        absent = [x for x in (HERE/f'inventory/{side}-absent-ids.txt').read_text().splitlines() if x.split('::',1)[0] in b['modules']]
        raw = (out/'pytest.log').read_bytes() if (out/'pytest.log').is_file() else None
        # Values below come from the validated fixed inputs, not from process output.
        metadata = {'side':side, 'group':group, 'pins':list(PINS[side]), 'manifest_sha256':MANIFEST_SHA,
                    'workflow_sha':os.environ['A2_SHA'] if re.fullmatch(r'[0-9a-f]{40}',os.environ.get('A2_SHA','')) else None,
                    'python_sha256':digest(pathlib.Path(sys.executable).resolve()),
                    'node_sha256':'1bec56ef7cfa9a76f3e0b7c0a87f220eb73f23102b9c0b4c7529a3f7c3ce7c31' if preflight else None,
                    'esbuild_sha256':'c638273fcf95573ca74af586677800b4dd874c55f8f945adb54316d6902ba14b' if preflight else None}
        result = sanitized_result(raw, expected, absent, metadata, rc, preflight, tracker.events)
        receipt.mkdir(exist_ok=True)
        write_receipt(receipt, result)
        with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as output: output.write('receipt_ready=true\n')
def main():
    tracker = StageTracker()
    try: return run_main(tracker)
    except Exception as exc:
        tracker.exception(exc)
        # Early validation/finalization failures still get a minimal safe receipt when possible.
        try:
            receipt = pathlib.Path(os.environ['RUNNER_TEMP']).resolve()/'r12-a2-receipt'
            receipt.mkdir(exist_ok=True)
            write_receipt(receipt, sanitized_result(None, [], [], {}, None, False, tracker.events))
            with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as output: output.write('receipt_ready=true\n')
        except Exception:
            pass
        return 3
if __name__=='__main__': sys.exit(main())
