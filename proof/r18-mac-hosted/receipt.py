import json, os, pathlib, platform, subprocess
p = pathlib.Path('out'); p.mkdir(exist_ok=True)
def git(name):
    try: return subprocess.check_output(['git','-C',name,'rev-parse','HEAD'], text=True).strip()
    except Exception as e: return 'unavailable: ' + str(e)
receipt = {'format':'r18-mac-hosted-native/v1', 'run_id':os.getenv('GITHUB_RUN_ID'),
 'attempt':os.getenv('GITHUB_RUN_ATTEMPT'), 'source_ref':os.getenv('GITHUB_REF'),
 'source_sha':os.getenv('GITHUB_SHA'), 'workflow_ref':os.getenv('GITHUB_WORKFLOW_REF'),
 'workflow_sha':os.getenv('GITHUB_WORKFLOW_SHA'), 'runner_os':platform.platform(),
 'uid':os.getuid(), 'checkouts':{n:git(n) for n in ('venue','agent','runtime','app')},
 'requested':{'agent':'098ae387521b3adb782f9b62bf60aa3ba2feb711',
 'runtime':'a95e0e836234bc6d27ba557ba1297da95ed55290',
 'app':'2145dc85b21f3c5a22f96a842c0d257ae29051e5',
 'manifest_sha256':'528615bf87637471ef38072e6899e745bdba7e54d5a65436b1bac5af3447b45c'},
 'command':'python3 venue/proof/r18-mac-hosted/native.py',
 'inner_exit':(p/'exit-code.txt').read_text().strip(),
 'limitations':['Focused admission and foreign refusal only; not a complete R0 to R1 lifecycle, product acceptance, or protected review.',
 'A missing native receipt or cleanup result is not a pass.']}
(p/'receipt.json').write_text(json.dumps(receipt,indent=2,sort_keys=True)+'\n')
