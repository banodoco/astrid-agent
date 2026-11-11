"""Execute one fixed matrix slot, never print subprocess output or arbitrary input."""
import json, os, pathlib, subprocess, sys
from full_plan import group_for
from runner import MANIFEST_SHA
from receipt_guard import rebuild
from sanitize import write_receipt

HERE = pathlib.Path(__file__).resolve().parent

def census():
    """Snapshot own-UID Linux processes by PID and start time; fail on scan errors."""
    entries = set()
    for path in pathlib.Path('/proc').iterdir():
        if not path.name.isdecimal(): continue
        try:
            if path.stat().st_uid != os.geteuid(): continue
            stat = (path/'stat').read_text()
            start_time = int(stat.rsplit(')',1)[1].split()[19])
            entries.add((int(path.name),start_time))
        except FileNotFoundError: continue
        except (OSError,ValueError,IndexError): raise RuntimeError('census unavailable') from None
    return entries

def decide(returncode, receipt, census_clear, side, group):
    if not census_clear or not isinstance(receipt,dict): return False
    meta = receipt.get('metadata')
    if not isinstance(meta,dict) or meta.get('side') != side or meta.get('group') != group or meta.get('manifest_sha256') != MANIFEST_SHA or receipt.get('preflight_complete') is not True: return False
    kind = receipt.get('classification')
    if kind == 'COMPLETE': return returncode in (0,1) and receipt.get('exit_code') == returncode
    if kind == 'NO_RESULTS': return returncode == 0 and receipt.get('exit_code') == 0 and receipt.get('expected_count') == 0 and bool(receipt.get('absent_ids'))
    return False

def main():
    side, group_range, slot = os.environ['A2_SIDE'], os.environ['A2_RANGE'], int(os.environ['A2_SLOT'])
    group = group_for(side,group_range,slot)
    if group is None: return 0
    root = pathlib.Path(os.environ['RUNNER_TEMP']).resolve() / ('r12-a2-full-'+side+'-'+group)
    root.mkdir(exist_ok=False)
    try:
        before=census()
    except RuntimeError:
        print('A2_PROCESS_CENSUS unverified',flush=True); return 3
    keys=('PATH','A2_SIDE','A2_RANGE','A2_REF','A2_SHA','A2_BEFORE','A2_ATTEMPT')
    env={key:os.environ[key] for key in keys}
    env.update({'RUNNER_TEMP':str(root),'A2_MODE':'full','A2_GROUP':group,'PYTHONDONTWRITEBYTECODE':'1'})
    result=subprocess.run([sys.executable,str(HERE/'runner.py')],env=env,start_new_session=True)
    try:
        after=census(); added=after-before; clear=not added; state='clear' if clear else 'residue'
    except RuntimeError:
        clear=False; state='unverified'
    print('A2_PROCESS_CENSUS '+state,flush=True)
    if not clear: return 3
    try:
        data=rebuild(root,side,group,os.environ['A2_SHA'])
        data['process_census']={'status':'clear'}
        output_dir=pathlib.Path(os.environ['RUNNER_TEMP'])/('r12-a2-final-slot-%02d'%slot)
        output_dir.mkdir(exist_ok=False)
        write_receipt(output_dir,data)
    except Exception:
        print('A2_RECEIPT invalid',flush=True); return 3
    accepted=decide(result.returncode,data,clear,side,group)
    if accepted and result.returncode == 1:
        (pathlib.Path(os.environ['RUNNER_TEMP'])/'r12-a2-full-had-failure').write_text('failure\n')
    return 0 if accepted else 3

if __name__ == '__main__':
    try: sys.exit(main())
    except Exception:
        print('A2_DRIVER failed',flush=True); sys.exit(3)
