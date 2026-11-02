import hashlib, json, pathlib, re, sys
H = lambda b: hashlib.sha256(b).hexdigest()
required = set('options errors preview inputs raw_engine media witnesses receipt manifest delivery export lifecycle shared'.split())
missing = {'$missing': True}
report = {'schema': 'h3-comparison/v1', 'status': 'BLOCKED', 'cases': [], 'missing_surfaces': []}
code = 2
def read_json(p):
    return json.loads(p.read_bytes())
def artifact(root, item):
    raw = item['path']
    if not isinstance(raw, str) or not raw or '\\' in raw or '\x00' in raw or any(x in ('', '..', '.') for x in raw.split('/')):
        raise ValueError('unsafe artifact path')
    rel = pathlib.PurePosixPath(raw)
    if rel.is_absolute() or not rel.parts:
        raise ValueError('unsafe artifact path')
    p = root
    for part in rel.parts:
        p = p / part
        if p.is_symlink(): raise ValueError('symlink artifact')
    if not p.is_file() or not p.resolve().is_relative_to(root.resolve()):
        raise ValueError('artifact absent or escaped')
    b = p.read_bytes()
    if type(item['size']) is not int or item['size'] < 0 or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256']) or H(b) != item['sha256'] or len(b) != item['size']: raise ValueError('artifact hash/size')
    return b
def packet(p):
    d = read_json(p)
    if d['schema'] != 'h3-capture/v1' or not isinstance(d['tuple'], dict) or not d['tuple']: raise ValueError('capture schema/tuple')
    if set(d['coverage']) != {f'P{x}' for x in range(1, 13)}: raise ValueError('coverage keys')
    if any(v != 'captured' for v in d['coverage'].values()): raise ValueError('uncaptured P coverage')
    cases, seen = {}, set()
    for c in d['cases']:
        if not isinstance(c['id'], str) or not c['id'] or c['id'] in cases or not re.fullmatch('[0-9a-f]{64}', c['inputs_sha256']):
            raise ValueError('case id/input digest')
        surfaces = {}
        for a in c['artifacts']:
            if a['surface'] in surfaces or a['kind'] not in ('bytes', 'json'): raise ValueError('artifact key/kind')
            b = artifact(p.parent, a)
            surfaces[a['surface']] = (a['kind'], json.loads(b) if a['kind'] == 'json' else H(b))
            if a['surface'] == 'media' and a['kind'] != 'bytes': raise ValueError('media must be bytes')
        for e in c.get('evidence', []): artifact(p.parent, e)
        if not surfaces: raise ValueError('empty case')
        cases[c['id']] = (c['inputs_sha256'], surfaces)
        seen.update(surfaces)
    return cases, required - seen
def diffs(a, b, pointer=''):
    if type(a) is dict and type(b) is dict:
        for k in sorted(set(a) | set(b)):
            q = pointer + '/' + k.replace('~', '~0').replace('/', '~1')
            if k not in a or k not in b: yield q, a.get(k, missing), b.get(k, missing)
            else: yield from diffs(a[k], b[k], q)
    elif type(a) is list and type(b) is list and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)): yield from diffs(x, y, pointer + '/' + str(i))
    elif type(a) != type(b) or a != b: yield pointer, a, b
try:
    output = pathlib.Path(sys.argv[4])
    if output.name == 'capture-index.json' or any(output.resolve() == pathlib.Path(p).resolve() for p in sys.argv[1:4]):
        raise ValueError('output would overwrite an input/index')
    left, lm = packet(pathlib.Path(sys.argv[1])); right, rm = packet(pathlib.Path(sys.argv[2]))
    report['missing_surfaces'] = sorted(lm | rm)
    if lm or rm or set(left) != set(right): raise ValueError('missing surface/case')
    allowed = read_json(pathlib.Path(sys.argv[3]))
    if allowed['schema'] != 'h3-allowed-diff/v1': raise ValueError('allowed diff schema')
    entries = allowed['differences']
    for e in entries:
        if e['surface'] not in ('receipt', 'preview', 'lifecycle') or not e['reason'] or not e['pointer'].startswith('/'):
            raise ValueError('forbidden allowed diff')
        if isinstance(e['old'], (dict, list)) and e['old'] != missing: raise ValueError('nonleaf allowance')
        if isinstance(e['new'], (dict, list)) and e['new'] != missing: raise ValueError('nonleaf allowance')
    failed = False
    for case in sorted(left):
        li, la = left[case]; ri, ra = right[case]
        if li != ri or set(la) != set(ra): raise ValueError('input/surface mismatch: ' + case)
        row = {'id': case, 'status': 'equal', 'differences': []}
        for surface in sorted(la):
            lk, lv = la[surface]; rk, rv = ra[surface]
            if lk != rk: raise ValueError('artifact kind mismatch')
            for pointer, old, new in diffs(lv, rv):
                ok = lk == 'json' and any(e['case'] == case and e['surface'] == surface and e['pointer'] == pointer and type(e['old']) == type(old) and e['old'] == old and type(e['new']) == type(new) and e['new'] == new for e in entries)
                row['differences'].append(dict(surface=surface, pointer=pointer, old=old, new=new, allowed=ok))
                failed |= not ok
        if row['differences']: row['status'] = 'allowed' if all(x['allowed'] for x in row['differences']) else 'different'
        report['cases'].append(row)
    report['status'] = 'FAIL' if failed else 'PASS'; code = 1 if failed else 0
except (OSError, ValueError, KeyError, TypeError, IndexError, UnicodeError) as exc:
    report['error'] = str(exc)
if len(sys.argv) == 5:
    output = pathlib.Path(sys.argv[4])
    if output.name != 'capture-index.json' and not any(output.resolve() == pathlib.Path(p).resolve() for p in sys.argv[1:4]):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')
sys.exit(code)
