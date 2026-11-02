# G0 comparator entry point — executable brief text, NOT run
M2 owns this packet interface; G0 is X per PACKS-TASKS v2.2. The standalone stdlib command below is the comparator entry point; it is embedded here to keep this task documents-only. After the separate run go, run it verbatim on captures in the indicated task-owned directories. No product import, new environment or repository file is needed. Missing/invalid/incomplete captures return BLOCKED; this does not make the blocked capture composition runnable.

Capture schema `h3-capture/v1`: `{schema,tuple:{agent,runtime,vibecomfy,interpreter,locks},coverage:{P1:state,...,P12:state},cases:[{id,inputs_sha256,artifacts:[{surface,path,kind,sha256,size}],evidence:[{path,sha256,size}]}]}`. State is captured/uncaptured/A-only. Required comparison surfaces across the packet: options, errors, preview, inputs, raw_engine, media, witnesses, receipt, manifest, delivery, export, lifecycle, shared. Each surface may occur in several named cases; candidate case/surface set must equal baseline. Paths are confined relative regular files, no symlink components. sha256/size computed from bytes; kind is bytes/json. Raw media is bytes, never a JSON hash assertion. All evidence files are verified even when raw tuple-dependent evidence is not compared.
`media` includes every final video/audio file as separate cases when necessary; `witnesses` records decoded FPS/duration/frame/sample coverage, protected masks/audio similarity and verify-before-publish facts. `delivery`/`manifest`/`export` compare role/cardinality/MIME/hash/size/ports/status/atomicity exactly. Both capture tuples use identical input-fixture bytes and ffmpeg identity. Publication absence after failure is an explicit observed JSON case, not an omitted artifact.
Capture raw receipts/rows/paths as evidence; comparison JSON uses ONLY deterministic one-to-one aliases for run-local task/attempt/generation/variant/association ids and task-root prefixes. Alias map is retained evidence and reviewed for relation preservation; no stripping roles, parent/variant associations, hash/size/status or editorial assertions. Wall times, scratch paths and removed stage witnesses require individually approved differences. Evidence isn't equivalence proof unless its required comparison surface is captured.
`h3-allowed-diff/v1`: `{schema,differences:[{case,surface,pointer,old,new,reason}]}`. JSON pointer addresses one exact leaf (missing uses `{"$missing":true}`). Only receipt/preview/lifecycle JSON fields may differ; no wildcards, whole-object approvals or media/delivery/manifest exceptions. M2+Astra review each entry BEFORE candidate comparison. New receipt h3-final/v2 maps removed child/CAS/stage facts explicitly; never fabricates witnesses. A failure/state transition cannot be excused as timing.
Output `h3-comparison/v1`: `{schema,status,cases:[{id,status,differences:[{surface,pointer,old,new,allowed}]}],missing_surfaces:[string],error?:string}`. Exit 0=PASS (equal/approved structural changes), 1=FAIL (unexplained difference), 2=BLOCKED (bad/missing evidence, UNCAPTURED coverage or forbidden allowance). Completeness review additionally checks H81→A537 coverage, not just equal packet contents. All coverage remains uncaptured until actually run/reviewed.

Exact future command (execute only after G0 run go; directories below are future capture outputs):
```bash
/home/user/astrid/proof-codex/work/21/python-env/bin/python - \
 /home/user/astrid/orch/tasks/PACKS-HANDOFF-r2/G0-capture/baseline/capture-index.json \
 /home/user/astrid/orch/tasks/PACKS-HANDOFF-r2/G0-capture/candidate/capture-index.json \
 /home/user/astrid/orch/tasks/PACKS-HANDOFF-r2/G0-capture/allowed-diff-v1.json \
 /home/user/astrid/orch/tasks/PACKS-HANDOFF-r2/G0-capture/comparison.json <<'PY'
import hashlib, json, pathlib, re, sys
H = lambda b: hashlib.sha256(b).hexdigest()
required = set('options errors preview inputs raw_engine media witnesses receipt manifest delivery export lifecycle shared'.split())
missing = {'$missing': True}
report = {'schema': 'h3-comparison/v1', 'status': 'BLOCKED', 'cases': [], 'missing_surfaces': []}
code = 2
def read_json(p):
    return json.loads(p.read_bytes())
def artifact(root, item):
    rel = pathlib.PurePosixPath(item['path'])
    if rel.is_absolute() or not rel.parts or any(x in ('..', '.') for x in rel.parts):
        raise ValueError('unsafe artifact path')
    p = root
    for part in rel.parts:
        p = p / part
        if p.is_symlink(): raise ValueError('symlink artifact')
    if not p.is_file() or not p.resolve().is_relative_to(root.resolve()):
        raise ValueError('artifact absent or escaped')
    b = p.read_bytes()
    if H(b) != item['sha256'] or len(b) != item['size']: raise ValueError('artifact hash/size')
    return b
def packet(p):
    d = read_json(p)
    if d['schema'] != 'h3-capture/v1' or not d['tuple']: raise ValueError('capture schema/tuple')
    if set(d['coverage']) != {f'P{x}' for x in range(1, 13)}: raise ValueError('coverage keys')
    if any(v != 'captured' for v in d['coverage'].values()): raise ValueError('uncaptured P coverage')
    cases, seen = {}, set()
    for c in d['cases']:
        if c['id'] in cases or not re.fullmatch('[0-9a-f]{64}', c['inputs_sha256']):
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
except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
    report['error'] = str(exc)
output = pathlib.Path(sys.argv[4]); output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')
sys.exit(code)
PY
```
This command has not been executed or product-qualified. No golden files have been created. The G0 completeness reviewer must reject forged/mislabelled captured states and verify each alias/evidence mapping; an offline comparator cannot establish that a real boundary ran.
