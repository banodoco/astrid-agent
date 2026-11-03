#!/usr/bin/env python3
"""Run and verify the two checkout-based protected App browser journeys."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from check_journey_evidence import validate_tuple, read

CASES = {
    'second-writer-first': ('direct-journey.spec.ts', 'App: a second writer saves first, the App shows the conflict and keeps the draft'),
    'draft-first': ('agent-protected.spec.ts', 'App: draft first, then second writer; conflict keeps the draft'),
}

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def head(path):
    return subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()

def clean(path):
    subprocess.run(['git', '-C', str(path), 'diff', '--quiet', 'HEAD', '--'], check=True)
    subprocess.run(['git', '-C', str(path), 'diff', '--cached', '--quiet'], check=True)

def inspect_report(report, case):
    filename, title = CASES[case]
    specs = []
    def walk(suite):
        for spec in suite.get('specs', []):
            specs.append(spec)
        for child in suite.get('suites', []):
            walk(child)
    for suite in report.get('suites', []):
        walk(suite)
    if len(specs) != 1:
        raise ValueError(f'{case}: expected exactly one spec, got {len(specs)}')
    spec = specs[0]
    if spec.get('file') != filename or spec.get('title') != title or spec.get('ok') is not True:
        raise ValueError(f'{case}: wrong spec identity or outcome')
    tests = spec.get('tests', [])
    if len(tests) != 1:
        raise ValueError(f'{case}: expected exactly one project result')
    test = tests[0]
    if (test.get('projectName') != '' or test.get('projectId') != '' or
        test.get('expectedStatus') != 'passed' or test.get('status') != 'expected' or
        test.get('annotations') or len(test.get('results', [])) != 1):
        raise ValueError(f'{case}: unexpected, annotated, skipped, retried or flaky test')
    result = test['results'][0]
    if result.get('status') != 'passed' or result.get('retry') != 0 or result.get('annotations'):
        raise ValueError(f'{case}: result was not a first-attempt pass')
    stats = report.get('stats', {})
    if any(stats.get(k) != 0 for k in ('unexpected', 'flaky', 'skipped')) or stats.get('expected') != 1:
        raise ValueError(f'{case}: invalid report counts')
    return {'file': filename, 'title': title, 'project': '', 'test_id': spec.get('id')}

def verify(receipt, candidate, driver, directory):
    if receipt.get('schema') != 'agent.protected-browser.v1' or receipt.get('evidence_kind') != 'checkout-browser':
        raise ValueError('invalid receipt kind')
    if receipt.get('candidate') != candidate or receipt.get('driver_head') != driver:
        raise ValueError('candidate or driver mismatch')
    if receipt.get('checkout_heads_before') != {k: candidate[k] for k in ('app', 'agent', 'runtime', 'pinokio')} or receipt.get('checkout_heads_after') != receipt.get('checkout_heads_before'):
        raise ValueError('checkout identities mismatch')
    if receipt.get('driver_head_after') != driver or receipt.get('cases', {}).keys() != CASES.keys():
        raise ValueError('driver or case set mismatch')
    ids = set()
    for case, data in receipt['cases'].items():
        if type(data.get('inner_exit')) is not int or data['inner_exit'] != 0:
            raise ValueError(f'{case}: nonzero or missing inner exit')
        path = Path(directory) / (case + '.json')
        if data.get('report_sha256') != sha(path):
            raise ValueError(f'{case}: report hash mismatch')
        identity = inspect_report(read(path), case)
        if data.get('identity') != identity or not identity['test_id'] or identity['test_id'] in ids:
            raise ValueError(f'{case}: missing, duplicate or mismatched test id')
        ids.add(identity['test_id'])

def run(args):
    candidate = read(args.candidate)
    paths = {k: Path(getattr(args, k)).resolve() for k in ('app', 'agent', 'runtime', 'pinokio')}
    driver = Path(args.driver).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    expected_driver = args.driver_head
    if not re.fullmatch(r'[0-9a-f]{40}', expected_driver):
        raise ValueError('driver head must be a full SHA')
    validate_tuple(candidate, paths['app'], paths['app'] / 'public/install-manifest.json')
    before = {k: head(p) for k, p in paths.items()}
    if before != {k: candidate[k] for k in before} or head(driver) != expected_driver:
        raise ValueError('actual checkout head mismatch')
    for p in [driver, *paths.values()]: clean(p)
    staged = {paths['app'] / 'tests/e2e/local/agent-protected.spec.ts': driver / 'scripts/ci/protected_browser/draft-first.spec.ts',
              paths['app'] / 'playwright.agent-protected.config.ts': driver / 'scripts/ci/protected_browser/playwright.agent-protected.config.ts'}
    if any(p.exists() for p in staged):
        raise ValueError('refusing to overwrite staged browser files')
    receipt = {'schema': 'agent.protected-browser.v1', 'evidence_kind': 'checkout-browser', 'candidate': candidate,
               'driver_head': expected_driver, 'checkout_heads_before': before, 'cases': {}}
    try:
        for target, source in staged.items(): shutil.copyfile(source, target)
        for case, (filename, title) in CASES.items():
            report = output / (case + '.json')
            env = {k: v for k, v in os.environ.items() if not k.startswith(('ASTRID_', 'D66_', 'D79_', 'PLAYWRIGHT_'))}
            env.update({'PLAYWRIGHT_JSON_OUTPUT_NAME': str(report), 'PLAYWRIGHT_OUTPUT_DIR': str(output / (case + '-traces')),
                        'ASTRID_CHECKOUT': str(paths['agent']), 'ASTRID_PYTHON': str(paths['agent'] / '.venv/bin/python'),
                        'ASTRID_RUNTIME_CHECKOUT': str(paths['runtime']), 'ASTRID_REQUIRE_LANES': 'direct-journey',
                        'APP_CHECKOUT_SHA': before['app'], 'AGENT_CHECKOUT_SHA': before['agent'], 'RUNTIME_CHECKOUT_SHA': before['runtime']})
            cmd = ['npx', 'playwright', 'test', '-c', 'playwright.agent-protected.config.ts', filename,
                   '--grep', re.escape(title) + '$', '--reporter=json,list,./config/testing/playwrightSkipGuard.ts']
            result = subprocess.run(cmd, cwd=paths['app'], env=env, check=False)
            entry = {'inner_exit': result.returncode, 'report_sha256': sha(report) if report.is_file() else None}
            if report.is_file():
                try: entry['identity'] = inspect_report(read(report), case)
                except (ValueError, KeyError, TypeError) as exc: entry['report_error'] = str(exc)
            receipt['cases'][case] = entry
    finally:
        for target in staged: target.unlink(missing_ok=True)
        receipt['checkout_heads_after'] = {k: head(p) for k, p in paths.items()}
        receipt['driver_head_after'] = head(driver)
        try:
            validate_tuple(candidate, paths['app'], paths['app'] / 'public/install-manifest.json')
            for p in [driver, *paths.values()]: clean(p)
        except (ValueError, OSError, subprocess.SubprocessError):
            receipt['checkout_heads_after'] = {}
        (output / 'journey-result.json').write_text(json.dumps(receipt, indent=2) + '\n')
    verify(receipt, candidate, expected_driver, output)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('candidate', 'driver', 'driver-head', 'app', 'agent', 'runtime', 'pinokio', 'output'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--verify-only', action='store_true')
    try:
        args = p.parse_args()
        if args.verify_only:
            candidate = read(args.candidate)
            validate_tuple(candidate, Path(args.app), Path(args.app) / 'public/install-manifest.json')
            verify(read(Path(args.output) / 'journey-result.json'), candidate, args.driver_head, args.output)
        else:
            run(args)
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError) as exc:
        print(f'protected browser evidence rejected: {exc}', file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
