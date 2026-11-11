"""Verify original prior bytes and explicit historical schema variants; never synthesize fields."""
import collections, hashlib, json, re
from runner import HERE, MANIFEST_SHA, PINS, validate
from receipt_guard import read_regular
from full_plan import SIDES, SIDE_RANGES, group_for
from stages import validate_events

PINNED_INDEX_SHA256 = '064a69064ebe8edb36bfb2597b78c31b9ec806ad2065bdaae193b755b32e7ede'
OLD_KEYS = {'absent_ids','classification','duplicate_ids','exit_code','expected_count',
            'malformed_outcome_count','malformed_summary_count','metadata','missing_ids',
            'outcome_counts','outcomes','preflight_complete','process_census','raw_pytest_sha256',
            'schema','stage_events','summary_counts','unexpected_result_count'}
META_KEYS = {'esbuild_sha256','group','manifest_sha256','node_sha256','pins','python_sha256','side','workflow_sha'}
SOURCE_SHAS = {'fdf95fa5abfe894bb9954c93631367726e391bf1',
               '366c5acb08fe143a520d9eb8228d0218e17f67d2',
               'a417dae48cdd4b288406d7b16cc2d5dddcbb2afb'}
STATUS_KEYS = {'PASSED':'passed','FAILED':'failed','ERROR':'error','SKIPPED':'skipped','XFAIL':'xfailed','XPASS':'xpassed'}

def verify_prior_receipts():
    raw_index=read_regular(HERE.parent,HERE.name,'DESIGN-080-hashes.json')
    if hashlib.sha256(raw_index).hexdigest()!=PINNED_INDEX_SHA256: raise ValueError('prior index hash mismatch')
    index=json.loads(raw_index)
    if index.get('manifest_sha256')!=MANIFEST_SHA or index.get('source_commit')!='26041afe13b30e4b5d6b9520a9977def490f6c80': raise ValueError('prior index identity mismatch')
    records=index['prior_receipts']; remaining=index['remaining_pairs']; seen=set(); failed_sides=set(); variants=collections.Counter()
    if len(records)!=56 or len(remaining)!=80: raise ValueError('prior index count mismatch')
    for entry in records:
        side,group=entry['side'],entry['group']; pair=(side,group)
        if pair in seen or side not in SIDES: raise ValueError('duplicate prior pair')
        seen.add(pair)
        data=read_regular(HERE,'prior-receipts',f'{side}-{group}.json')
        if hashlib.sha256(data).hexdigest()!=entry['sha256']: raise ValueError('prior receipt hash mismatch')
        receipt=json.loads(data); batch,expected=validate(side,group); meta=receipt.get('metadata',{})
        has_reasons=entry.get('has_incomplete_reasons')
        if type(has_reasons) is not bool or entry.get('has_additional_error_ids') is not False: raise ValueError('unknown historical schema')
        keys=OLD_KEYS | ({'incomplete_reasons'} if has_reasons else set());variants[has_reasons]+=1
        if set(receipt)!=keys or set(meta)!=META_KEYS or receipt.get('schema')!=1: raise ValueError('historical schema mismatch')
        if has_reasons and receipt['incomplete_reasons'] != []: raise ValueError('historical incomplete reasons')
        if (meta.get('side')!=side or meta.get('group')!=group or meta.get('pins')!=list(PINS[side])
            or meta.get('manifest_sha256')!=MANIFEST_SHA or meta.get('workflow_sha') not in SOURCE_SHAS
            or meta.get('workflow_sha')!=entry['source_workflow_sha']
            or meta.get('node_sha256')!='1bec56ef7cfa9a76f3e0b7c0a87f220eb73f23102b9c0b4c7529a3f7c3ce7c31'
            or meta.get('esbuild_sha256')!='c638273fcf95573ca74af586677800b4dd874c55f8f945adb54316d6902ba14b'
            or meta.get('python_sha256')!='d601e62487858384d3000725c3b2e16e86ff11a6ad0b0810b76281e45426ea62'):
            raise ValueError('prior pin mismatch')
        validate_events(receipt['stage_events'])
        absent={x for x in (HERE/f'inventory/{side}-absent-ids.txt').read_text().splitlines() if x.split('::',1)[0] in batch['modules']}
        outcomes=receipt['outcomes']; ids=[x.get('id') for x in outcomes if isinstance(x,dict) and set(x)=={'id','status'}]
        if (len(ids)!=len(outcomes) or len(ids)!=len(expected) or len(set(ids))!=len(ids) or set(ids)!=set(expected)
            or set(receipt['absent_ids'])!=absent or receipt['expected_count']!=len(expected)
            or receipt['missing_ids'] or receipt['duplicate_ids'] or receipt['unexpected_result_count']!=0
            or receipt['malformed_outcome_count']!=0 or receipt['malformed_summary_count']!=0
            or receipt['process_census']!={'status':'clear'} or receipt['preflight_complete'] is not True): raise ValueError('prior coverage mismatch')
        counts=collections.Counter(x['status'] for x in outcomes)
        if set(counts)-set(STATUS_KEYS) or dict(counts)!=receipt['outcome_counts'] or dict(counts)!=entry['outcome_counts']: raise ValueError('prior outcome mismatch')
        classification=receipt['classification'];rc=receipt['exit_code']
        if classification!=entry['classification'] or rc!=entry['exit_code'] or receipt['expected_count']!=entry['expected_count']: raise ValueError('prior summary identity mismatch')
        if classification=='NO_RESULTS':
            if expected or not absent or rc!=0 or counts or receipt['raw_pytest_sha256'] is not None or receipt['summary_counts'] is not None: raise ValueError('prior no-results mismatch')
        elif classification=='COMPLETE':
            if not re.fullmatch(r'[0-9a-f]{64}',receipt.get('raw_pytest_sha256') or ''): raise ValueError('prior raw hash mismatch')
            summary=receipt.get('summary_counts');bad=counts['FAILED']+counts['ERROR']
            if not isinstance(summary,dict) or any(summary.get(label,0)!=counts.get(status,0) for status,label in STATUS_KEYS.items()): raise ValueError('prior summary mismatch')
            if not ((rc==0 and bad==0) or (rc==1 and bad>0)): raise ValueError('prior exit mismatch')
            if bad: failed_sides.add(side)
        else: raise ValueError('prior classification mismatch')
    if variants!={False:18,True:38} or failed_sides!=set(SIDES): raise ValueError('prior schema or failure coverage mismatch')
    mapped=[(side,group_for(side,ran,slot)) for side in SIDES for ran in SIDE_RANGES[side] for slot in range(1,12)]
    mapped=[pair for pair in mapped if pair[1] is not None]
    actual=set(mapped); wanted={(x['side'],x['group']) for x in remaining}
    universe={(side,f'{group:03}') for side in SIDES for group in range(19,53)}
    if len(mapped)!=80 or len(actual)!=80 or len(wanted)!=80 or actual!=wanted or actual & seen or actual | seen != universe: raise ValueError('remaining coverage mismatch')
    return True
