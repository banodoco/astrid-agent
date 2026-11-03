import json
from pathlib import Path
results = {}
def pytest_collection_modifyitems(items):
    for item in items:
        results[item.nodeid] = {'nodeid': item.nodeid, 'phases': {}}
def pytest_runtest_logreport(report):
    results.setdefault(report.nodeid, {'nodeid': report.nodeid, 'phases': {}})['phases'][report.when] = report.outcome
def pytest_sessionfinish(session, exitstatus):
    for result in results.values():
        phases = result['phases'].values()
        result['outcome'] = 'failed' if 'failed' in phases else 'skipped' if 'skipped' in phases else 'passed' if result['phases'].get('call') == 'passed' else 'not_run'
    Path(__file__).with_name('NODE-OUTCOMES.json').write_text(json.dumps({'exitstatus': int(exitstatus), 'nodes': list(results.values())}, indent=2) + '\n')
