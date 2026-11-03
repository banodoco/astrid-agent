#!/usr/bin/env python3
"""C4 observations on an ephemeral fixture, never an admission producer."""
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(os.environ['C4_ROOT'])
OUT = ROOT / 'results'
UNIT = 'c4-fixture.service'


def cmd(*args, input=None):
    try:
        p = subprocess.run(args, input=input, text=True, capture_output=True, timeout=20)
        return {'argv': list(args), 'exit': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'argv': list(args), 'exit': None, 'error': repr(e)}


def read(path):
    try:
        return {'path': str(path), 'value': Path(path).read_text(), 'error': None}
    except OSError as e:
        return {'path': str(path), 'value': None, 'errno': e.errno, 'error': str(e)}


def show():
    return cmd('systemctl', '--user', 'show', UNIT, '--no-pager', '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'InvocationID', '-p', 'ExecMainStartTimestampMonotonic', '-p', 'ConditionResult', '-p', 'Conditions', '-p', 'Restart', '-p', 'DropInPaths', '-p', 'FragmentPath', '-p', 'TriggeredBy', '-p', 'Triggers', '-p', 'Job')


def lock_controls():
    path = ROOT / 'owner.lock'
    # Independent opens model a contender; no unlock on any borrowed description.
    with open(path, 'a+') as owner, open(path, 'a+') as contender:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = 'unexpected_acquire'
        except BlockingIOError as e:
            held = {'result': 'blocked', 'errno': e.errno}
    with open(path, 'a+') as free:
        try:
            fcntl.flock(free, fcntl.LOCK_EX | fcntl.LOCK_NB)
            available = 'acquired'
        except OSError as e:
            available = {'result': 'error', 'errno': e.errno}
    return {'held': held, 'free': available, 'path': str(path), 'scope': 'fixture file and cooperating flock users only'}


def scan_dir(path):
    # Each access failure is unknown. Listing is illustrative, not a complete route census.
    try:
        p = Path(path)
        entries = []
        for f in sorted(p.iterdir()):
            try:
                st = f.lstat()
                entry = {'name': f.name, 'uid': st.st_uid, 'mode': oct(st.st_mode & 0o7777), 'symlink': f.is_symlink()}
                if f.is_symlink():
                    entry['disposition'] = 'foreign_or_unresolved'
                elif f.is_file():
                    data = f.read_bytes()
                    entry.update(sha256=hashlib.sha256(data).hexdigest(), content=data.decode('utf-8', 'replace'))
                else:
                    entry['disposition'] = 'unclassified'
                entries.append(entry)
            except OSError as e:
                entries.append({'name': f.name, 'disposition': 'unknown', 'errno': e.errno})
        return {'path': str(p), 'entries': entries, 'disposition': 'observed_not_complete'}
    except FileNotFoundError:
        return {'path': str(path), 'disposition': 'absent_at_sample'}
    except OSError as e:
        return {'path': str(path), 'disposition': 'unknown', 'errno': e.errno}


def route_controls():
    fixture = ROOT / 'route-controls'
    # Only this newly-created account is modified. The commands are inert and
    # removed immediately; missing tools or failed cleanup remain explicit.
    cron_before = cmd('crontab', '-l')
    cron = {'before': cron_before}
    if cron_before.get('exit') == 1 and ('no crontab' in cron_before.get('stderr', '').lower()):
        cron['install'] = cmd('crontab', '-', input='0 0 31 2 * /bin/true # C4-DISPOSABLE\n')
        cron['after'] = cmd('crontab', '-l')
        cron['remove'] = cmd('crontab', '-r')
        cron['final'] = cmd('crontab', '-l')
    else:
        cron['disposition'] = 'unknown_or_existing_preserved'
    at_before = cmd('atq')
    at = {'before': at_before}
    if at_before.get('exit') == 0 and not at_before.get('stdout', '').strip():
        at['install'] = cmd('at', 'now', '+', '365', 'days', input='/bin/true # C4-DISPOSABLE\n')
        at['after'] = cmd('atq')
        match = re.search(r'\bjob (\d+)\b', at['install'].get('stderr', ''))
        if match:
            at['inspect'] = cmd('at', '-c', match.group(1))
            at['remove'] = cmd('atrm', match.group(1))
            at['final'] = cmd('atq')
        else:
            at['disposition'] = 'unknown_job_id'
    else:
        at['disposition'] = 'unknown_or_existing_preserved'
    autostart = Path.home()/'.config/autostart'
    autostart.mkdir(parents=True, exist_ok=True)
    xdg_before = scan_dir(autostart)
    desktop = autostart/'c4-disposable.desktop'
    with desktop.open('x') as f:
        f.write('[Desktop Entry]\nType=Application\nName=C4 disposable\nExec=/bin/true\n')
    xdg_after = scan_dir(autostart)
    desktop.unlink()
    xdg_final = scan_dir(autostart)
    return {
        'cron_control': cron, 'at_control': at, 'xdg_control': {'before': xdg_before, 'after': xdg_after, 'final': xdg_final},
        'commands': [cmd('systemctl', '--user', 'list-unit-files', '--no-pager'),
                     cmd('systemctl', '--user', 'list-units', '--all', '--no-pager'),
                     cmd('systemctl', '--system', 'list-units', '--all', '--no-pager'),
                     cmd('systemctl', '--system', 'show', os.environ['C4_SYSTEM_UNIT'].replace('.service', '.timer'), '-p', 'Id', '-p', 'Unit', '-p', 'ActiveState'),
                     cmd('systemctl', '--system', 'show', os.environ['C4_SYSTEM_UNIT'], '-p', 'Id', '-p', 'User', '-p', 'ExecStart', '-p', 'ActiveState')],
        'dirs': [scan_dir(p) for p in [Path.home()/'.config/autostart', '/etc/xdg/autostart', '/etc/cron.d', '/etc/cron.daily', fixture/'clean', fixture/'cron', fixture/'at', fixture/'xdg', fixture/'system', fixture/'inaccessible', fixture/'foreign']],
        'scope': 'sampled visible routes and synthetic negative files; system, other users, hidden or changing launchers are not proven absent',
        'disposition': 'unknown_refuse'}


def main(phase):
    assert os.getuid() != 0 and os.geteuid() != 0, 'collector must be actual non-root'
    OUT.mkdir(exist_ok=True)
    result = {'phase': phase, 'uid': os.getuid(), 'euid': os.geteuid(), 'gid': os.getgid(),
              'kernel': cmd('uname', '-a'), 'boot_id': read('/proc/sys/kernel/random/boot_id'),
              'consumer_argv': sys.argv, 'reboot_claim': False, 'authority': False}
    unit_dir = Path.home()/'.config/systemd/user'
    unit_dir.mkdir(parents=True, exist_ok=True)
    if phase == 'before':
        unit = unit_dir/UNIT
        unit.write_text('[Unit]\nDescription=C4 disposable invocation fixture\n[Service]\nType=oneshot\nExecStart=/usr/bin/touch '+str(ROOT/'invoked')+'\nRemainAfterExit=yes\n')
        result['reload'] = cmd('systemctl', '--user', 'daemon-reload')
        result['start'] = cmd('systemctl', '--user', 'start', UNIT)
        result['invoked'] = show()
        result['stop'] = cmd('systemctl', '--user', 'stop', UNIT)
        result['stopped_before_suppression'] = show()
        guard = ROOT/'transfer-active'
        guard.write_text('fixture active\n')
        dropdir = unit_dir/(UNIT+'.d')
        dropdir.mkdir(exist_ok=False)
        drop = dropdir/'90-c4-fixture.conf'
        drop.write_text('[Unit]\nConditionPathExists=!'+str(guard)+'\n[Service]\nRestart=no\n')
        result['dropin'] = {'path': str(drop), 'sha256': hashlib.sha256(drop.read_bytes()).hexdigest(), 'guard': read(guard)}
        result['reload_suppression'] = cmd('systemctl', '--user', 'daemon-reload')
        result['suppressed_before_restart'] = show()
        (ROOT/'invoked').unlink(missing_ok=True)
        result['blocked_start'] = cmd('systemctl', '--user', 'start', UNIT)
        result['after_blocked_start'] = show()
        result['marker_after_blocked_start'] = (ROOT/'invoked').exists()
    elif phase == 'after':
        result['post_manager_restart'] = show()
        result['guard'] = read(ROOT/'transfer-active')
        result['dropin'] = read(unit_dir/(UNIT+'.d')/'90-c4-fixture.conf')
        result['blocked_start'] = cmd('systemctl', '--user', 'start', UNIT)
        result['after_blocked_start'] = show()
        result['marker_after_blocked_start'] = (ROOT/'invoked').exists()
        result['journal_this_boot'] = cmd('journalctl', '--user-unit', UNIT, '-b', '--no-pager', '-o', 'json')
        result['journal_limit'] = 'A successful query or zero records does not establish complete retained history, journal access or authenticated source.'
        result['locks'] = lock_controls()
        result['routes'] = route_controls()
        result['admission'] = 'refuse: restart is not reboot; boot provenance, complete invocation history and route universe unestablished'
    else:
        raise ValueError(phase)
    (OUT/(phase+'.json')).write_text(json.dumps(result, indent=2, sort_keys=True)+'\n')
    return 0

if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
