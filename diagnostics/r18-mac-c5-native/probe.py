#!/usr/bin/env python3
"""Disposable launchd diagnostic. Invoke only as a non-root macOS GUI user."""
import hashlib, json, os, pathlib, plistlib, re, signal, subprocess, sys, tempfile, time, uuid

if len(sys.argv) > 1 and sys.argv[1] == 'worker':
    root, marker = pathlib.Path(sys.argv[2]), sys.argv[3]
    child = subprocess.Popen([sys.executable, __file__, 'child', marker])
    (root / 'pids.json').write_text(json.dumps({'parent': os.getpid(), 'child': child.pid, 'marker': marker}))
    while True: time.sleep(1)
if len(sys.argv) > 1 and sys.argv[1] == 'child':
    while True: time.sleep(1)

out = pathlib.Path(os.environ.get('PROBE_OUTPUT', 'r18-mac-c5-output')).resolve()
out.mkdir(parents=True, exist_ok=True)
log = (out / 'commands.jsonl').open('w', buffering=1)
def record(**kw):
    kw['time_ns'] = time.time_ns(); log.write(json.dumps(kw, sort_keys=True) + '\n')
def run(args, timeout=15, redact=False):
    start = time.monotonic()
    try:
        p = subprocess.run([str(x) for x in args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        result = {'exit': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}
    except (OSError, subprocess.TimeoutExpired) as e:
        result = {'exit': None, 'error': str(e)}
    logged = dict(result)
    if redact and 'stdout' in logged:
        data = logged.pop('stdout'); logged['stdout_sha256'] = hashlib.sha256(data.encode()).hexdigest(); logged['stdout_bytes'] = len(data.encode())
    record(command=[str(x) for x in args], elapsed_seconds=round(time.monotonic()-start, 3), **logged)
    return result

def census():
    r = run(['/bin/ps', '-axo', 'pid=,ppid=,pgid=,uid=,lstart='])
    if r['exit'] != 0: return None
    rows = []
    for line in r['stdout'].splitlines():
        parts = line.split()
        if len(parts) != 9: return None
        try:
            rows.append({'pid': int(parts[0]), 'ppid': int(parts[1]), 'pgid': int(parts[2]), 'uid': int(parts[3]), 'start': ' '.join(parts[4:9])})
        except ValueError: return None
    return rows

def fixture_command(pid, marker):
    r = run(['/bin/ps', '-p', str(pid), '-o', 'command='])
    return r['exit'] == 0 and marker in r['stdout']

def main():
    if sys.platform != 'darwin' or os.geteuid() == 0 or os.getuid() != os.geteuid():
        record(preflight='refused: require Darwin and matching non-root real/effective UID'); (out / 'summary.json').write_text(json.dumps({'status': 'preflight-refused'}) + '\n'); return 2
    uid = os.getuid(); domain = f'gui/{uid}'
    token = uuid.uuid4().hex
    label = 'org.astrid.c5probe.' + token
    target = domain + '/' + label
    root = pathlib.Path(tempfile.mkdtemp(prefix='c5probe-', dir=os.environ.get('TMPDIR'))).resolve()
    plist = root / (label + '.plist')
    marker = 'c5probe-' + token
    summary = {'uid': uid, 'euid': os.geteuid(), 'domain': domain, 'label': label, 'target': target, 'scratch': str(root), 'status': 'unknown', 'cleanup': 'not attempted'}
    bootstrap_attempted = False; identities = {}; bootout = None
    def save(): (out / 'summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
    try:
        run(['/usr/bin/id']); run(['/usr/bin/sw_vers']); run(['/usr/bin/uname', '-a'])
        domain_before = run(['/bin/launchctl', 'print', domain], redact=True)
        disabled = run(['/bin/launchctl', 'print-disabled', domain], redact=True); record(disabled_target_lines=[line for line in disabled.get('stdout', '').splitlines() if label in line], disabled_exit=disabled['exit'])
        if domain_before['exit'] != 0:
            summary['reason'] = 'GUI domain unavailable or inaccessible'; return 2
        pre = run(['/bin/launchctl', 'print', target])
        if pre['exit'] == 0:
            summary['reason'] = 'unique label collision'; return 2
        # A failed print alone is not a general absence predicate.
        summary['prior_unique_target_print_exit'] = pre['exit']
        # This deliberate failure must be classified unknown, never empty.
        bad = run([str(root / 'missing-ps')]); summary['unknown_control'] = 'unknown' if bad['exit'] is None else 'invalid'
        definition = {'Label': label, 'ProgramArguments': [sys.executable, str(pathlib.Path(__file__).resolve()), 'worker', str(root), marker], 'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ExitTimeOut': 10, 'StandardOutPath': str(root / 'stdout'), 'StandardErrorPath': str(root / 'stderr')}
        raw = plistlib.dumps(definition, sort_keys=True); plist.write_bytes(raw)
        (out / 'definition.plist').write_bytes(raw)
        st = plist.stat(); summary['definition_before'] = {'sha256': hashlib.sha256(raw).hexdigest(), 'mode': oct(st.st_mode & 0o7777), 'uid': st.st_uid, 'gid': st.st_gid, 'size': st.st_size}
        bootstrap_attempted = True
        b = run(['/bin/launchctl', 'bootstrap', domain, plist], timeout=30)
        if b['exit'] != 0:
            summary['reason'] = 'bootstrap did not succeed; registration effects unknown'; return 2
        deadline = time.monotonic() + 20
        while not (root / 'pids.json').exists() and time.monotonic() < deadline: time.sleep(.2)
        if not (root / 'pids.json').exists(): summary['reason'] = 'worker did not publish PIDs'; return 2
        pids = json.loads((root / 'pids.json').read_text()); summary['published_pids'] = pids
        loaded = run(['/bin/launchctl', 'print', target]); disabled = run(['/bin/launchctl', 'print-disabled', domain], redact=True); record(disabled_target_lines=[line for line in disabled.get('stdout', '').splitlines() if label in line], disabled_exit=disabled['exit'])
        rows = census()
        if rows is None or loaded['exit'] != 0 or str(plist) not in loaded.get('stdout', '') or marker not in loaded.get('stdout', '') or not re.search(r'^\s*pid = ' + str(pids['parent']) + r'\s*$', loaded.get('stdout', ''), re.MULTILINE):
            summary['reason'] = 'prior loaded state or process census unavailable'; return 2
        identities = {str(p): next((x for x in rows if x['pid'] == p and x['uid'] == uid and fixture_command(p, marker)), None) for p in (pids['parent'], pids['child'])}
        summary['identities_before'] = identities
        if any(x is None for x in identities.values()) or identities[str(pids['child'])]['ppid'] != pids['parent']:
            summary['reason'] = 'parent/child live control failed'; return 2
        pgid = identities[str(pids['parent'])]['pgid']; pgids = sorted({x['pgid'] for x in identities.values()}); summary['parent_pgid'] = pgid; summary['tracked_pgids'] = pgids
        summary['live_control'] = 'live' if any(x['pgid'] in pgids for x in rows) else 'invalid'
        summary['before_group'] = [x for x in rows if x['pgid'] in pgids]
        # Observe while bootout runs, and then for a fixed bounded interval.
        start = time.monotonic(); proc = subprocess.Popen(['/bin/launchctl', 'bootout', target], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        observations = []; deadline = start + 45; after_exit = None
        while time.monotonic() < deadline:
            code = proc.poll()
            if code is not None and after_exit is None: after_exit = time.monotonic()
            rows = census()
            group = None if rows is None else [x for x in rows if x['pgid'] in pgids]
            tracked = None if rows is None else [x for x in rows if x['pid'] in pids.values() and isinstance(x['pid'], int)]
            observations.append({'seconds': round(time.monotonic()-start, 3), 'bootout_exit_so_far': code, 'group': group, 'tracked_pid_rows': tracked})
            if after_exit is not None and time.monotonic()-after_exit >= 15: break
            time.sleep(.5)
        if proc.poll() is None:
            proc.kill(); stdout, stderr = proc.communicate(); bootout = {'exit': None, 'timeout': True, 'stdout': stdout, 'stderr': stderr}
        else:
            stdout, stderr = proc.communicate(); bootout = {'exit': proc.returncode, 'stdout': stdout, 'stderr': stderr}
        record(command=['/bin/launchctl', 'bootout', target], elapsed_seconds=round(time.monotonic()-start, 3), **bootout)
        (out / 'observations.json').write_text(json.dumps(observations, indent=2) + '\n')
        summary['bootout'] = bootout
        post = run(['/bin/launchctl', 'print', target]); disabled = run(['/bin/launchctl', 'print-disabled', domain], redact=True); record(disabled_target_lines=[line for line in disabled.get('stdout', '').splitlines() if label in line], disabled_exit=disabled['exit']); run(['/bin/launchctl', 'print', domain], redact=True)
        summary['post_target_print_exit'] = post['exit']
        raw_after = plist.read_bytes(); st = plist.stat()
        summary['definition_after'] = {'sha256': hashlib.sha256(raw_after).hexdigest(), 'mode': oct(st.st_mode & 0o7777), 'uid': st.st_uid, 'gid': st.st_gid, 'size': st.st_size}
        last = observations[-1] if observations else None
        if bootout['exit'] != 0 or not last or last['group'] is None or last['tracked_pid_rows'] is None:
            summary['status'] = 'unknown'
        elif last['group'] or last['tracked_pid_rows']:
            summary['status'] = 'still-live-or-reused-pid-or-group'
        else:
            summary['status'] = 'observed-empty-at-final-sample'
        return 0 if summary['status'] == 'observed-empty-at-final-sample' and summary['unknown_control'] == 'unknown' and summary['live_control'] == 'live' else 1
    except Exception as e:
        summary['reason'] = type(e).__name__ + ': ' + str(e); return 2
    finally:
        # Never signal an unverified label. Bootstrap success plus a fresh loaded
        # print matching this exact scratch plist and marker gates cleanup bootout.
        if bootstrap_attempted:
            current = run(['/bin/launchctl', 'print', target])
            text = current.get('stdout', '')
            if current['exit'] == 0 and str(plist) in text and marker in text:
                c = run(['/bin/launchctl', 'bootout', target], timeout=30); summary['cleanup_bootout'] = c['exit']
            elif current['exit'] == 0:
                summary['cleanup_label'] = 'identity mismatch; untouched'
            else: summary['cleanup_label'] = 'not observable; no additional bootout'
        # Signal only same-UID, same-start, marker-matching tracked fixture processes.
        if not identities and (root / 'pids.json').exists():
            try:
                pending = json.loads((root / 'pids.json').read_text()); available = census()
                if available is not None:
                    identities = {str(p): next((x for x in available if x['pid'] == p and x['uid'] == uid and fixture_command(p, marker)), None) for p in (pending['parent'], pending['child'])}
            except (ValueError, OSError, KeyError): pass
        rows = census() if identities else None
        if rows is not None:
            for pid, old in identities.items():
                now = next ( (x for x in rows if x['pid'] == int(pid)), None )
                if old and now and now['uid'] == uid and now['start'] == old['start'] and fixture_command(pid, marker):
                    try: os.kill(int(pid), signal.SIGTERM); record(cleanup_signal_pid=int(pid), signal='SIGTERM', exit=0)
                    except OSError as e: record(cleanup_signal_pid=int(pid), error=str(e), exit=None)
            time.sleep(1)
            remaining = census()
            summary['cleanup_remaining'] = None if remaining is None else [x for x in remaining if str(x['pid']) in identities and x['uid'] == uid]
        else: summary['cleanup_remaining'] = None if identities else []
        summary['cleanup'] = 'scratch retained for inspection; label and tracked-process cleanup attempted where authorized'
        save()

rc = main()
if rc == 0:
    final = json.loads((out / 'summary.json').read_text())
    if final.get('cleanup_remaining') != [] or final.get('cleanup_label') == 'identity mismatch; untouched' or final.get('cleanup_bootout', 0) not in (0,): rc = 1
record(inner_exit=rc)
log.close()
files = sorted(p for p in out.iterdir() if p.is_file() and p.name != 'SHA256SUMS')
(out / 'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n' for p in files))
sys.exit(rc)
