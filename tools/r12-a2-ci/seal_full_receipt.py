"""Post-test receipt gate. Artifact path is fixed by workflow slot, never output data."""
import json, os, pathlib, sys
from full_plan import group_for
from receipt_guard import rebuild, read_regular

def main():
    side, group_range, slot=os.environ['A2_SIDE'],os.environ['A2_RANGE'],int(os.environ['A2_SLOT'])
    group=group_for(side,group_range,slot)
    if group is None: return 0
    temp=pathlib.Path(os.environ['RUNNER_TEMP']).resolve()
    root=temp/('r12-a2-full-'+side+'-'+group)
    expected=rebuild(root,side,group,os.environ['A2_SHA'])
    expected['process_census']={'status':'clear'}
    # Fixed slot directory and filename; never accept a path from the tested process.
    data=read_regular(temp,'r12-a2-final-slot-%02d'%slot,'receipt.json')
    candidate=json.loads(data)
    if json.dumps(candidate,sort_keys=True,separators=(',',':')) != json.dumps(expected,sort_keys=True,separators=(',',':')): raise ValueError('sealed receipt mismatch')
    with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as output: output.write('receipt_ready=true\n')
    return 0
if __name__=='__main__':
    try: sys.exit(main())
    except Exception:
        print('A2_RECEIPT invalid',flush=True); sys.exit(3)
