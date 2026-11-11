"""Post-test receipt gate. Artifact paths and all failure fields are fixed."""
import json, os, pathlib, re, secrets, sys
from full_plan import group_for
from receipt_guard import rebuild, read_regular
from runner import MANIFEST_SHA
from sanitize import MALFORMED_SHAPES, MAX_SHAPE_COUNT

REASONS = frozenset(('PREFLIGHT_INCOMPLETE','NO_PYTEST_LOG','EXIT_INCONSISTENT','MISSING_IDS',
                     'DUPLICATE_IDS','UNEXPECTED_IDS','MALFORMED_OUTCOME','MALFORMED_SUMMARY',
                     'SUMMARY_MISSING_OR_MULTIPLE','SUMMARY_MISMATCH'))

def write_fixed(temp, slot, data):
    """Atomically replace the literal slot receipt without following symlinks."""
    rootfd=os.open(temp,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        dirname='r12-a2-final-slot-%02d'%slot
        try: os.mkdir(dirname,0o700,dir_fd=rootfd)
        except FileExistsError: pass
        dirfd=os.open(dirname,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=rootfd)
        try:
            name='.sealed-'+secrets.token_hex(16)
            fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=dirfd)
            try:
                with os.fdopen(fd,'w',encoding='utf-8',closefd=False) as stream:
                    json.dump(data,stream,sort_keys=True,indent=2);stream.write('\n')
                os.replace(name,'receipt.json',src_dir_fd=dirfd,dst_dir_fd=dirfd)
            finally:
                os.close(fd)
                try: os.unlink(name,dir_fd=dirfd)
                except FileNotFoundError: pass
        finally: os.close(dirfd)
    finally: os.close(rootfd)

def incomplete_receipt(root, side, group, workflow_sha, failure_kind):
    if failure_kind not in ('DRIVER_FAILED','SEAL_VERIFICATION_FAILED'): raise ValueError('invalid failure kind')
    shapes={key:0 for key in MALFORMED_SHAPES}; reasons=[]; source='UNAVAILABLE'
    try:
        candidate=rebuild(root,side,group,workflow_sha)
        values=candidate['malformed_outcome_shapes']; observed=candidate['incomplete_reasons']
        if (candidate['classification']=='INCOMPLETE' and set(values)==set(MALFORMED_SHAPES)
            and all(type(v) is int and 0<=v<=MAX_SHAPE_COUNT for v in values.values())
            and isinstance(observed,list) and all(x in REASONS for x in observed)):
            shapes={key:values[key] for key in MALFORMED_SHAPES};reasons=list(observed);source='RECONSTRUCTED_INCOMPLETE'
    except Exception:
        pass
    return {'schema':'a2-incomplete-v1','classification':'INCOMPLETE','side':side,'group':group,
            'workflow_sha':workflow_sha,'manifest_sha256':MANIFEST_SHA,
            'process_census':{'status':'unverified'},'failure_kind':failure_kind,
            'diagnostic_source':source,'incomplete_reasons':reasons,'malformed_outcome_shapes':shapes}

def main():
    side, group_range, slot=os.environ['A2_SIDE'],os.environ['A2_RANGE'],int(os.environ['A2_SLOT'])
    group=group_for(side,group_range,slot)
    if group is None: return 0
    temp=pathlib.Path(os.environ['RUNNER_TEMP']).resolve(); workflow_sha=os.environ['A2_SHA']
    if not re.fullmatch(r'[0-9a-f]{40}',workflow_sha): raise ValueError('invalid workflow sha')
    outcome=os.environ['A2_DRIVER_OUTCOME']
    if outcome not in ('success','failure'): raise ValueError('invalid driver outcome')
    root=temp/('r12-a2-full-'+side+'-'+group)
    failure_kind = 'DRIVER_FAILED' if outcome=='failure' else None
    if failure_kind is None:
        try:
            expected=rebuild(root,side,group,workflow_sha)
            data=read_regular(temp,'r12-a2-final-slot-%02d'%slot,'receipt.json')
            candidate=json.loads(data)
            if not isinstance(candidate,dict) or candidate.get('process_census') != {'status':'clear'} or candidate.get('classification') not in ('COMPLETE','NO_RESULTS'): raise ValueError('unclean or incomplete receipt')
            expected['process_census'] = candidate['process_census']
            if json.dumps(candidate,sort_keys=True,separators=(',',':')) != json.dumps(expected,sort_keys=True,separators=(',',':')): raise ValueError('sealed receipt mismatch')
        except Exception:
            failure_kind='SEAL_VERIFICATION_FAILED'
    if failure_kind is not None:
        write_fixed(temp,slot,incomplete_receipt(root,side,group,workflow_sha,failure_kind))
    with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as output: output.write('receipt_ready=true\n')
    return 3 if failure_kind is not None else 0
if __name__=='__main__':
    try: sys.exit(main())
    except Exception:
        print('A2_RECEIPT invalid',flush=True); sys.exit(3)
