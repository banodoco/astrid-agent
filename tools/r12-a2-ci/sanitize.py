"""Construct an allowlisted receipt. Never copy arbitrary subprocess output."""
import collections, hashlib, json, os, pathlib, re, tempfile
from stages import validate_events

STATUSES = ('PASSED', 'FAILED', 'ERROR', 'SKIPPED', 'XFAIL', 'XPASS')
SUMMARY_KEYS = ('passed', 'failed', 'error', 'errors', 'skipped', 'xfailed', 'xpassed', 'warnings', 'warning', 'deselected')
OUTCOME = re.compile(r'^(tests/.*?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?: \[\s*\d{1,3}%\])?$')
HEADINGS = {'test session starts', 'FAILURES', 'ERRORS', 'PASSES', 'XFAILURES', 'XPASSES', 'SKIPPED', 'warnings summary', 'short test summary info'}
SUMMARY = re.compile(r'^=+ ((?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|warnings?|deselected)(?:, )?)+) in \d+(?:\.\d+)?s(?: \([0-9:]+\))? =+$')

def sanitized_result(raw, expected, absent, metadata, exit_code, preflight, stage_events=None):
    """Only fixed keys, verified metadata and frozen IDs enter the result."""
    expected_set = set(expected)
    if len(expected_set) != len(expected): raise ValueError('duplicate frozen IDs')
    found = []; unknown_count = 0; malformed_outcome_count = 0; malformed_summary_count = 0; summaries = []
    if raw is not None:
        for line in raw.decode('utf-8', errors='replace').splitlines():
            match = OUTCOME.fullmatch(line)
            if match:
                node, status = match.groups()
                if node in expected_set: found.append((node, status))
                else: unknown_count += 1
            elif line.lstrip().startswith('tests/') and ('::' in line or re.search(r'\b(?:PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b', line)):
                malformed_outcome_count += 1
            match = SUMMARY.fullmatch(line)
            if match:
                pairs = re.findall(r'(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|warnings?|deselected)', match.group(1))
                counts = {}
                for number, name in pairs:
                    name = {'errors':'error','warning':'warnings'}.get(name,name)
                    if name in counts: counts = {}; break
                    counts[name] = int(number)
                if counts: summaries.append(counts)
                else: malformed_summary_count += 1
            elif line.lstrip().startswith('==') and line.strip('= ').strip() not in HEADINGS:
                malformed_summary_count += 1
    by_id = collections.Counter(node for node, _ in found)
    missing = sorted(expected_set - set(by_id)); duplicates = sorted(node for node,n in by_id.items() if n != 1)
    outcomes = [{'id':node,'status':status} for node,status in found]
    status_counts = dict(sorted(collections.Counter(x['status'] for x in outcomes).items()))
    summary = summaries[0] if len(summaries)==1 else None
    equivalent = {'PASSED':'passed','FAILED':'failed','ERROR':'error','SKIPPED':'skipped','XFAIL':'xfailed','XPASS':'xpassed'}
    counts_match = summary is not None and all(summary.get(name,0)==status_counts.get(status,0) for status,name in equivalent.items())
    bad = status_counts.get('FAILED',0) + status_counts.get('ERROR',0)
    valid_exit = (exit_code == 0 and bad == 0) or (exit_code == 1 and bad > 0)
    complete = bool(preflight and raw is not None and valid_exit and not missing and not duplicates and unknown_count == 0 and malformed_outcome_count == 0 and malformed_summary_count == 0 and counts_match)
    no_results = bool(preflight and not expected and absent and raw is None and exit_code == 0)
    classification = 'COMPLETE' if complete else ('NO_RESULTS' if no_results else 'INCOMPLETE')
    return {'schema':1, 'stage_events':validate_events(stage_events if stage_events is not None else []), 'classification':classification, 'metadata':metadata,
            'expected_count':len(expected), 'missing_ids':missing, 'duplicate_ids':duplicates,
            'unexpected_result_count':unknown_count, 'malformed_outcome_count':malformed_outcome_count, 'malformed_summary_count':malformed_summary_count, 'absent_ids':sorted(absent),
            'outcomes':outcomes, 'outcome_counts':status_counts, 'summary_counts':summary,
            'exit_code':exit_code if isinstance(exit_code,int) and -255 <= exit_code <= 255 else None,
            'preflight_complete':bool(preflight), 'raw_pytest_sha256':hashlib.sha256(raw).hexdigest() if raw is not None else None}

def write_receipt(directory, result):
    directory = pathlib.Path(directory)
    # Single exact artifact path; never write raw output to it, including on failure.
    fd, name = tempfile.mkstemp(prefix='.receipt-', dir=directory)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f: json.dump(result,f,sort_keys=True,indent=2); f.write('\n')
        os.replace(name,directory/'receipt.json')
    finally:
        if os.path.exists(name): os.unlink(name)
