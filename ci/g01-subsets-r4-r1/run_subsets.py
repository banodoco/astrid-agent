#!/usr/bin/env python3
"""Bounded G-01 execution using the unchanged restored runner inventory/parser."""
import hashlib, importlib.util, json, os, pathlib, shutil, subprocess, sys, time, traceback, xml.etree.ElementTree as ET
P = pathlib.Path(__file__).resolve().parent
ROOT = pathlib.Path(os.environ['G01_ROOT']).resolve()
OUT = pathlib.Path(os.environ['G01_OUT']).resolve()
OUT.mkdir(parents=True, exist_ok=True)
spec = importlib.util.spec_from_file_location('restored_runner', P / 'job/runner.py')
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
def cmd(args, cwd, log, env, timeout=7200):
    with (OUT / log).open('w') as f:
        f.write('argv: ' + json.dumps([str(x) for x in args]) + '\ncwd: ' + str(cwd) + '\n'); f.flush()
        return subprocess.run([str(x) for x in args], cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=timeout).returncode

def main():
    expected_manifest = 'dd736439c464fe0cccfebd3b0dcfd556ce498f04ecd82d0d59add5ffde834b6c'
    if sha(P/'job/artifact-manifest.json') != expected_manifest or r.verify_uploaded_artifacts(): raise RuntimeError('restored package integrity failure')
    expected = {'agent':('ddd5d278796641901bd11508863a8ea21e6c9697','96c12053d230550ccf6327fe87bd8da5e183c5c4'), 'runtime':('47e5949465cb6f3afdfd1cb8a0e90009fd9d4020','248e0b2fb79ed0be6c68757b3af9162b2a4bf710'), 'lifecycle':('96360a27a53c4608f780f7b370f747a2aa3c7ca8','24e3562181f0ad64003d5cef5403b15d16415be6')}
    identities={}
    for name, (commit, tree) in expected.items():
        repo=ROOT/name
        got=(r.git(repo,'rev-parse','HEAD'),r.git(repo,'rev-parse','HEAD^{tree}'))
        if got != (commit, tree) or r.git(repo,'status','--porcelain','--untracked-files=no'): raise RuntimeError(f'wrong or dirty source {name}: {got}')
        identities[name]={'commit':got[0],'tree':got[1]}
    (OUT/'source-identities.json').write_text(json.dumps(identities,indent=2)+'\n')
    runtime=ROOT/'runtime'; agent=ROOT/'agent'; py=ROOT/'.venv/bin/python'
    paths=r.SUITES['runtime-focused']
    inventory={p:sha(runtime/p) for p in paths+['packages/typescript/package.json','packages/typescript/package-lock.json']}
    (OUT/'coverage-inventory.json').write_text(json.dumps({'runtime-focused':inventory,'runtime-typescript-client':r.SUITES['runtime-typescript-client'],'command':'npm ci && npm test'},indent=2)+'\n')
    env=r.clean_env(); env.pop('PYTHONPATH',None); env['PYTEST_ADDOPTS']=''; env['TMPDIR']=str(ROOT/'scratch'); env['TMP']=env['TEMP']=env['TMPDIR']; pathlib.Path(env['TMPDIR']).mkdir(exist_ok=True)
    # Install was qualified before execution. Verify the public provider and interpreter in a subprocess.
    probe="import sys, pathlib, subprocess, importlib.metadata as m; import astrid.sdk.timeline.commit as c; import runtime_protocol, runpod_lifecycle, pytest; from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider; print('python',sys.version); print('public_module',c.__file__); print('factory',c.create_runtime_commit_service); print('provider',type(load_timeline_commit_provider('astrid')).__name__); print('distributions',[(x,m.version(x)) for x in ('astrid','astrid-runtime','astrid-runtime-client','runpod-lifecycle','setuptools','wheel')]); print('node',subprocess.check_output(['node','--version'],text=True).strip()); print('npm',subprocess.check_output(['npm','--version'],text=True).strip()); assert sys.version_info[:2] == (3,11); assert pathlib.Path(c.__file__).resolve().is_relative_to(pathlib.Path("+repr(str(agent))+")); assert callable(c.create_runtime_commit_service)"
    if cmd([py,'-c',probe],runtime,'public-preflight.log',env): raise RuntimeError('PREFLIGHT_PUBLIC_PROVIDER failed; see public-preflight.log')
    if cmd([py,'-m','pip','check'],runtime,'pip-check.log',env): raise RuntimeError('PREFLIGHT_PIP_CHECK failed')
    cmd([py,'-m','pip','freeze'],runtime,'installed-packages.log',env)
    collection=cmd([py,'-m','pytest','--collect-only','-q','-o','addopts=',*paths],runtime,'runtime-focused-collection.log',env)
    if collection: raise RuntimeError(f'PREFLIGHT_COLLECTION failed exit={collection}')
    xml=OUT/'runtime-focused.xml'
    rc=cmd([py,'-m','pytest','-q','--junitxml',xml,'--basetemp',ROOT/'scratch/pytest',*paths],runtime,'runtime-focused.log',env)
    counts={k:0 for k in ('tests','failures','errors','skipped')}
    if xml.exists():
        root=ET.parse(xml).getroot(); suites=list(root) if root.tag=='testsuites' else [root]
        counts={k:sum(int(x.attrib.get(k,0)) for x in suites) for k in counts}
    tsroot=runtime/'packages/typescript'
    ci=cmd(['npm','ci'],tsroot,'typescript-npm-ci.log',env,3600)
    ts=cmd(['npm','test'],tsroot,'runtime-typescript-client.tap',env,3600) if ci==0 else None
    try: tap=r.parse_tap_counts((OUT/'runtime-typescript-client.tap').read_text()) if ts is not None else None
    except Exception as e: tap={'parse_error':str(e)}
    ok=rc==0 and counts['tests']>0 and not any(counts[k] for k in ('failures','errors','skipped')) and ci==0 and ts==0 and tap is not None and tap.get('tests',0)>0 and tap.get('pass')==tap.get('tests') and not any(tap.get(k,1) for k in ('fail','cancelled','skipped'))
    result={'ok':ok,'collection_exit':collection,'runs':[{'suite':'runtime-focused','exit_code':rc,'counts':counts}, {'suite':'runtime-typescript-client','npm_ci_exit':ci,'exit_code':ts,'tap':tap}],'scope':'two independent selectors; no full G-01 acceptance'}
    (OUT/'summary.json').write_text(json.dumps(result,indent=2)+'\n'); return 0 if ok else 1
if __name__=='__main__':
    try: code=main()
    except Exception as e:
        traceback.print_exc(); code=2
        (OUT/'summary.json').write_text(json.dumps({'ok':False,'error':str(e),'traceback':traceback.format_exc()},indent=2)+'\n')
    (OUT/'exit-codes.txt').write_text(f'runner_exit={code}\n')
    (OUT/'hashes.sha256').write_text(''.join(f'{sha(p)}  {p.name}\n' for p in sorted(OUT.iterdir()) if p.is_file() and p.name!='hashes.sha256'))
    sys.exit(code)
