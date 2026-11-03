import copy
import json
import sys
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ci_journey_browser as browser


def report(case):
    filename, title = browser.CASES[case]
    return {'suites': [{'specs': [{'file': filename, 'title': title, 'id': case, 'ok': True,
            'tests': [{'projectName': '', 'projectId': '', 'expectedStatus': 'passed', 'status': 'expected',
                       'annotations': [], 'results': [{'status': 'passed', 'retry': 0, 'annotations': []}]}]}]}],
            'stats': {'expected': 1, 'unexpected': 0, 'flaky': 0, 'skipped': 0}}


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'wrong_file', 'wrong_title', 'skipped', 'flaky', 'expected_failure', 'failed', 'retry', 'annotation', 'wrong_project', 'extra'])
def test_reject_bad_playwright_results(mutation):
    case = 'draft-first'
    data = report(case)
    specs = data['suites'][0]['specs']
    spec = specs[0]
    test = spec['tests'][0]
    if mutation == 'missing': specs.clear()
    elif mutation == 'duplicate': specs.append(copy.deepcopy(spec))
    elif mutation == 'wrong_file': spec['file'] = 'other.spec.ts'
    elif mutation == 'wrong_title': spec['title'] = 'other'
    elif mutation == 'skipped': test['results'][0]['status'] = 'skipped'
    elif mutation == 'flaky': data['stats']['flaky'] = 1
    elif mutation == 'expected_failure': test['expectedStatus'] = 'failed'
    elif mutation == 'failed': test['results'][0]['status'] = 'failed'
    elif mutation == 'retry': test['results'].append(copy.deepcopy(test['results'][0]))
    elif mutation == 'annotation': test['annotations'] = [{'type': 'fail'}]
    elif mutation == 'wrong_project': test['projectName'] = 'other'
    elif mutation == 'extra': spec['tests'].append(copy.deepcopy(test))
    with pytest.raises(ValueError): browser.inspect_report(data, case)


def test_receipt_binds_inputs_reports_and_separate_exits(tmp_path):
    candidate = {k: k * 40 for k in ('app', 'agent', 'runtime', 'pinokio')}
    driver = 'd' * 40
    cases = {}
    for case in browser.CASES:
        path = tmp_path / (case + '.json')
        data = report(case)
        path.write_text(json.dumps(data))
        cases[case] = {'inner_exit': 0, 'report_sha256': browser.sha(path), 'identity': browser.inspect_report(data, case)}
    receipt = {'schema': 'agent.protected-browser.v1', 'evidence_kind': 'checkout-browser', 'candidate': candidate,
               'driver_head': driver, 'driver_head_after': driver, 'checkout_heads_before': candidate,
               'checkout_heads_after': candidate.copy(), 'cases': cases}
    browser.verify(receipt, candidate, driver, tmp_path)
    for mutate in ('exit', 'head', 'missing', 'duplicate_id', 'hash', 'driver'):
        bad = copy.deepcopy(receipt)
        if mutate == 'exit': bad['cases']['draft-first']['inner_exit'] = 1
        elif mutate == 'head': bad['checkout_heads_after']['runtime'] = 'wrong'
        elif mutate == 'missing': del bad['cases']['draft-first']
        elif mutate == 'duplicate_id': bad['cases']['draft-first']['identity']['test_id'] = bad['cases']['second-writer-first']['identity']['test_id']
        elif mutate == 'hash': bad['cases']['draft-first']['report_sha256'] = 'bad'
        elif mutate == 'driver': bad['driver_head_after'] = 'wrong'
        with pytest.raises(ValueError): browser.verify(bad, candidate, driver, tmp_path)


def test_workflow_requires_and_rechecks_browser_artifact():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/journey.yml').read_text()
    assert 'needs: [config, journey, release2, m4_4b_local, failure_modes, protected_browser]' in workflow
    assert 'if [ "$PROTECTED_BROWSER_ENABLED" = true ]; then test "$PROTECTED_BROWSER_RESULT" = success; fi' in workflow
    assert 'name: protected-browser-checkout-receipt' in workflow
    assert 'ci_journey_browser.py --verify-only' in workflow
