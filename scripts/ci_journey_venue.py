"""Run and publish one direct journey row through the git-only venue."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

RESULT_REF = 'refs/heads/venue-results/M-QUAL-R-05-1'


def run_row(args):
    dest = Path(os.environ['RUNNER_TEMP']) / 'journey-venue'
    dest.mkdir(parents=True, exist_ok=True)
    code = 1
    try:
        with (dest / 'journey.log').open('wb') as log:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for chunk in iter(lambda: proc.stdout.read(4096), b''):
                log.write(chunk)
                log.flush()
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            code = proc.wait()
    finally:
        (dest / 'inner-exit.txt').write_text(str(code) + '\n', encoding='utf-8')
    return code


def git(args, cwd, env, check=True):
    result = subprocess.run(['git', *args], cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        # Do not print git output, which could contain authentication material.
        raise RuntimeError('git operation failed: ' + args[0])
    return result


def publish():
    env = os.environ.copy()
    token = env.pop('GITHUB_TOKEN')
    auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
    env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
               GIT_CONFIG_VALUE_0='AUTHORIZATION: basic ' + auth, GIT_TERMINAL_PROMPT='0')
    temp = Path(os.environ['RUNNER_TEMP'])
    job = os.environ['ROW_ID']
    row = temp / 'journey-venue'
    row.mkdir(parents=True, exist_ok=True)
    evidence = temp / ('astrid-r1-' + job) / 'evidence' / 'evidence-upload'
    if evidence.exists():
        if os.environ['EVIDENCE_OVERSIZE'] == 'true':
            for path in evidence.rglob('*'):
                if path.is_file() and path.name in ('journey-result.json', 'driver-identity.json', 'artifact-size.txt'):
                    target = row / 'artifacts' / path.relative_to(evidence)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
        else:
            shutil.copytree(evidence, row / 'artifacts', dirs_exist_ok=True)
    candidate = temp / 'candidate.json'
    if candidate.exists():
        shutil.copy2(candidate, row / 'candidate.json')
    effective = {}
    for key, path in [('driver', 'agent'), ('app', 'app'), ('agent', 'candidate-agent'),
                      ('runtime', 'runtime'), ('pinokio', 'pinokio')]:
        result = git(['rev-parse', 'HEAD'], path, env, check=False) if Path(path).is_dir() else None
        effective[key] = result.stdout.strip() if result and result.returncode == 0 else None
    exit_path = row / 'inner-exit.txt'
    receipt = {'run_id': os.environ['GITHUB_RUN_ID'], 'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
               'source_ref': os.environ['GITHUB_REF'], 'source_sha': os.environ['GITHUB_SHA'],
               'workflow_ref': os.environ['GITHUB_WORKFLOW_REF'], 'workflow_sha': os.environ['GITHUB_WORKFLOW_SHA'],
               'row_id': job, 'runner': os.environ['ROW_RUNNER'], 'runner_os': os.environ['RUNNER_OS'],
               'effective_checkouts': effective, 'inner_exit': int(exit_path.read_text()) if exit_path.exists() else None,
               'journey_step_outcome': os.environ['JOURNEY_OUTCOME'],
               'evidence_step_outcome': os.environ['EVIDENCE_OUTCOME'],
               'oversize': os.environ['EVIDENCE_OVERSIZE']}
    (row / 'receipt.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    files = sorted(p for p in row.rglob('*') if p.is_file() and p.name != 'SHA256SUMS')
    (row / 'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.relative_to(row).as_posix() + '\n' for p in files), encoding='utf-8')
    remote = 'https://github.com/' + os.environ['GITHUB_REPOSITORY'] + '.git'
    run = os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT']
    with tempfile.TemporaryDirectory(prefix='journey-results-') as work:
        git(['init', '-q'], work, env)
        git(['config', 'user.name', 'github-actions[bot]'], work, env)
        git(['config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com'], work, env)
        git(['remote', 'add', 'origin', remote], work, env)
        for attempt in range(8):
            advertised = git(['ls-remote', '--exit-code', 'origin', RESULT_REF], work, env, check=False)
            if advertised.returncode == 0:
                git(['fetch', '--no-tags', 'origin', RESULT_REF], work, env)
                git(['checkout', '-B', 'results', 'FETCH_HEAD'], work, env)
            elif advertised.returncode == 2:
                git(['checkout', '--orphan', 'results'], work, env)
            else:
                raise RuntimeError('could not inspect results ref')
            target = Path(work) / 'runs' / run / job
            if target.exists():
                raise RuntimeError('row already exists on results ref')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(row, target)
            git(['add', 'runs/' + run + '/' + job], work, env)
            git(['commit', '-qm', 'Record ' + run + ' ' + job], work, env)
            pushed = git(['push', 'origin', 'HEAD:' + RESULT_REF], work, env, check=False)
            if pushed.returncode == 0:
                print('Published', run, job)
                return
            # A concurrent row may have won the race. Re-fetch and retry its new head.
            git(['reset', '--hard', 'HEAD'], work, env)
        raise RuntimeError('results push failed after concurrent update retries')


if __name__ == '__main__':
    if sys.argv[1] == 'run':
        sys.exit(run_row(sys.argv[2:]))
    if sys.argv[1] == 'publish':
        publish()
    else:
        raise SystemExit('expected run or publish')
