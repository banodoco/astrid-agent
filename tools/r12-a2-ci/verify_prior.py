"""Fail-closed checks for the five frozen, previously accepted receipt bytes."""
import hashlib, json
from runner import HERE, MANIFEST_SHA, PINS, validate
from receipt_guard import read_regular

PRIOR = {
    ('r10','019'): '0d0aa5ec4497608117ee1efda6d880ace1022282d6e0fef35b354c185d1c3444',
    ('r11','019'): 'bedf88c1f4305b9dd940d878340853c03019166fcb91c961f0550d124f631bc7',
    ('base','019'): '783769392cc4c3e943895aef7e815cdf25c7b6632d0f23c29c47fbfded02394d',
    ('candidate','019'): 'd0253725bc612dd83aeb1a7a470abe1c911306b6f5d3384c7723d29442a9558e',
    ('r10','020'): '82b28a74b88297e56e0cb0b547a43bcea04cfdd75215594f17ab2f55bdfe7d21',
}
PRIOR_WORKFLOW_SHA = 'fdf95fa5abfe894bb9954c93631367726e391bf1'

def verify_prior_receipts():
    for (side,group), expected_hash in PRIOR.items():
        data=read_regular(HERE,'prior-receipts',f'{side}-{group}.json')
        if hashlib.sha256(data).hexdigest() != expected_hash: raise ValueError('prior receipt hash mismatch')
        receipt=json.loads(data)
        batch, expected=validate(side,group)
        meta=receipt.get('metadata',{})
        if (receipt.get('classification') != 'COMPLETE' or receipt.get('exit_code') != 0
            or receipt.get('preflight_complete') is not True or receipt.get('process_census') != {'status':'clear'}
            or meta.get('side') != side or meta.get('group') != group or meta.get('pins') != list(PINS[side])
            or meta.get('manifest_sha256') != MANIFEST_SHA or meta.get('workflow_sha') != PRIOR_WORKFLOW_SHA
            or receipt.get('expected_count') != len(expected) or receipt.get('missing_ids') or receipt.get('duplicate_ids')
            or receipt.get('unexpected_result_count') != 0 or receipt.get('malformed_outcome_count') != 0
            or receipt.get('malformed_summary_count') != 0): raise ValueError('prior receipt incomplete')
        outcomes=receipt.get('outcomes',[])
        if len(outcomes) != len(expected) or {x.get('id') for x in outcomes} != set(expected) or any(x.get('status') != 'PASSED' for x in outcomes):
            raise ValueError('prior receipt IDs or outcome mismatch')
        if receipt.get('outcome_counts') != {'PASSED':len(expected)}: raise ValueError('prior outcome counts mismatch')
    return True
