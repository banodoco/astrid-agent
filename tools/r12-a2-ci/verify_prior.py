"""Fail-closed verification of the 18 frozen prior receipt byte streams."""
import collections, hashlib, json
from runner import HERE, MANIFEST_SHA, PINS, validate
from receipt_guard import read_regular

PRIOR = {
 ('r10','019'): '0d0aa5ec4497608117ee1efda6d880ace1022282d6e0fef35b354c185d1c3444',
 ('r11','019'): 'bedf88c1f4305b9dd940d878340853c03019166fcb91c961f0550d124f631bc7',
 ('base','019'): '783769392cc4c3e943895aef7e815cdf25c7b6632d0f23c29c47fbfded02394d',
 ('candidate','019'): 'd0253725bc612dd83aeb1a7a470abe1c911306b6f5d3384c7723d29442a9558e',
 ('r10','020'): '82b28a74b88297e56e0cb0b547a43bcea04cfdd75215594f17ab2f55bdfe7d21',
 ('base','020'): '29d67f81154bf8a7b8fb06777bf09fd337c02b8845e7e34fd24472c3199b24a8',
 ('base','021'): '00a85ae2164712cdee458bd5c372ded35ab671a27213493119493dbef24beba1',
 ('candidate','020'): '46e8c1b07994134e0df8c3790ef8ae909b2734d6d1ed22bf14e8f946a0803f50',
 ('candidate','021'): '204b2c7bb5f92c0b1fbaa31cc451e7b49671d01e019eb5f2e0cf16954b32e7a6',
 ('r10','021'): '7a2e17d701d0503106ee32bbe3be56fc7833db74feffa16c1ed36a7d4ae210e8',
 ('r10','022'): '4eb3c10747d88e413b0798fe50d9b42fe695bc5ac315f2a051cd5adcae61168a',
 ('r10','023'): 'a8f656b7438f2244363ba2a0091a1e17905ef570b7d7a436203e029ff4556dac',
 ('r10','024'): 'bbe0af1a990fdf36c682fb5e110b48db9f188e1f3b989a0ab9a4018dbc11a155',
 ('r10','025'): 'c4a143a006d81b159743ed8c1a1a89654195fef89c2c8cb7484f512c393656b1',
 ('r10','026'): '2089d7f346ea0af199c5a7ac69ccf7a19120270bb840c0aa6fc03e13ec1aba2e',
 ('r10','027'): 'a5c2bfe1a696bd427daf8ddced73ddbc403afa993e6a9bd189601630f76d1f80',
 ('r11','020'): 'c4f95e45cdea881a10ef0c3104ee8f1629f6de9b2acdbe951bff032d9c35ac6c',
 ('r11','021'): '210cf2304189e91294465965275959999abb09946c9733c03f023ea182d0ebd7',
}
ORIGINAL = {('r10','019'),('r11','019'),('base','019'),('candidate','019'),('r10','020')}
NO_RESULTS = {('r10','021'),('r10','022'),('r10','024'),('r10','025')}
FAILED_027 = {
 'tests/test_generic_host.py::test_render_uses_free_space_through_execution_evidence_and_settlement',
 'tests/test_generic_host.py::test_render_is_ready_without_remotion_and_reports_remotion_separately',
 'tests/test_generic_host.py::test_host_execution_failure_keeps_coded_fields_and_recovery[astrid]',
 'tests/test_generic_host.py::test_host_execution_failure_keeps_coded_fields_and_recovery[exec]',
 'tests/test_generic_host.py::test_host_execution_failure_keeps_coded_fields_and_recovery[sdk]',
}

def verify_prior_receipts():
    for (side,group), expected_hash in PRIOR.items():
        data=read_regular(HERE,'prior-receipts',f'{side}-{group}.json')
        if hashlib.sha256(data).hexdigest() != expected_hash: raise ValueError('prior receipt hash mismatch')
        receipt=json.loads(data); batch, expected=validate(side,group); meta=receipt.get('metadata',{})
        expected_sha = 'fdf95fa5abfe894bb9954c93631367726e391bf1' if (side,group) in ORIGINAL else '366c5acb08fe143a520d9eb8228d0218e17f67d2'
        absent={x for x in (HERE/f'inventory/{side}-absent-ids.txt').read_text().splitlines() if x.split('::',1)[0] in batch['modules']}
        if (receipt.get('preflight_complete') is not True or receipt.get('process_census') != {'status':'clear'}
            or meta.get('side') != side or meta.get('group') != group or meta.get('pins') != list(PINS[side])
            or meta.get('manifest_sha256') != MANIFEST_SHA or meta.get('workflow_sha') != expected_sha
            or receipt.get('expected_count') != len(expected) or set(receipt.get('absent_ids',[])) != absent
            or receipt.get('missing_ids') or receipt.get('duplicate_ids') or receipt.get('unexpected_result_count') != 0
            or receipt.get('malformed_outcome_count') != 0 or receipt.get('malformed_summary_count') != 0): raise ValueError('prior receipt incomplete')
        outcomes=receipt.get('outcomes',[])
        if len(outcomes) != len(expected) or {x.get('id') for x in outcomes} != set(expected): raise ValueError('prior receipt ID mismatch')
        counts=collections.Counter(x.get('status') for x in outcomes)
        if dict(counts) != receipt.get('outcome_counts'): raise ValueError('prior outcome counts mismatch')
        if (side,group) in NO_RESULTS:
            if expected or not absent or receipt.get('classification') != 'NO_RESULTS' or receipt.get('exit_code') != 0 or counts: raise ValueError('prior absence mismatch')
        elif (side,group) == ('r10','027'):
            failures={x.get('id') for x in outcomes if x.get('status') == 'FAILED'}
            if receipt.get('classification') != 'COMPLETE' or receipt.get('exit_code') != 1 or failures != FAILED_027 or counts != {'PASSED':263,'FAILED':5}: raise ValueError('prior failure mismatch')
        elif receipt.get('classification') != 'COMPLETE' or receipt.get('exit_code') != 0 or counts != {'PASSED':len(expected)}:
            raise ValueError('prior pass mismatch')
    return True
