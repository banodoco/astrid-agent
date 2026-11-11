"""Reconstruct a receipt from frozen inputs; reject any unknown/untrusted field."""
import hashlib, json, os, pathlib, re, stat, sys
from runner import HERE, MANIFEST_SHA, PINS, validate
from sanitize import sanitized_result
from stages import validate_events

MAX_BYTES = 64 * 1024 * 1024

def read_regular(root, directory, filename, missing_ok=False):
    """Read only regular files inside non-symlink directories, with a size ceiling."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        child = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            try: f = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=child)
            except FileNotFoundError:
                if missing_ok: return None
                raise
            try:
                info=os.fstat(f)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES: raise ValueError('unsafe file')
                with os.fdopen(f,'rb',closefd=False) as stream: data=stream.read(MAX_BYTES+1)
                if len(data)>MAX_BYTES: raise ValueError('oversized file')
                return data
            finally: os.close(f)
        finally: os.close(child)
    finally: os.close(fd)

def rebuild(root, side, group, workflow_sha, receipt_dir='r12-a2-receipt', include_census=False):
    if not re.fullmatch(r'[0-9a-f]{40}',workflow_sha): raise ValueError('invalid workflow sha')
    batch, expected = validate(side,group)
    absent = [x for x in (HERE/f'inventory/{side}-absent-ids.txt').read_text().splitlines() if x.split('::',1)[0] in batch['modules']]
    data = read_regular(root,receipt_dir,'receipt.json')
    candidate=json.loads(data,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON')))
    if not isinstance(candidate,dict): raise ValueError('invalid receipt')
    census = candidate.pop('process_census',None) if include_census else None
    if include_census and census != {'status':'clear'}: raise ValueError('unclean census')
    stage_events=candidate.get('stage_events')
    if not isinstance(stage_events,list) or len(stage_events)>18: raise ValueError('invalid stages')
    stage_events=validate_events(stage_events)
    preflight=candidate.get('preflight_complete'); rc=candidate.get('exit_code')
    if type(preflight) is not bool or (rc is not None and (type(rc) is not int or not -255 <= rc <= 255)): raise ValueError('invalid types')
    if preflight and {'stage':'toolchain_preflight','event':'passed','exit_code':None} not in stage_events: raise ValueError('missing preflight stage')
    metadata={'side':side,'group':group,'pins':list(PINS[side]),'manifest_sha256':MANIFEST_SHA,'workflow_sha':workflow_sha,
              'python_sha256':hashlib.sha256(pathlib.Path(sys.executable).resolve().read_bytes()).hexdigest(),
              'node_sha256':'1bec56ef7cfa9a76f3e0b7c0a87f220eb73f23102b9c0b4c7529a3f7c3ce7c31' if preflight else None,
              'esbuild_sha256':'c638273fcf95573ca74af586677800b4dd874c55f8f945adb54316d6902ba14b' if preflight else None}
    try: raw=read_regular(root,'r12-a2-private','pytest.log',missing_ok=True)
    except FileNotFoundError: raw=None
    rebuilt=sanitized_result(raw,expected,absent,metadata,rc,preflight,stage_events)
    # JSON canonicalization distinguishes bool from int and rejects unknown keys at every level.
    if json.dumps(candidate,sort_keys=True,separators=(',',':')) != json.dumps(rebuilt,sort_keys=True,separators=(',',':')): raise ValueError('receipt mismatch')
    if include_census: rebuilt['process_census']={'status':'clear'}
    return rebuilt
