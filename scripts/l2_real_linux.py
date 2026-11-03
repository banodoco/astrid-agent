#!/usr/bin/env python3
"""L2 real Linux: authentic R0 user service -> pinned R1, data and owner lock.

Run only on the disposable hosted VM prepared by tests/l2_real_linux/run.sh.
This is a focused integration test, not a release journey receipt.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import ci_journey as j, journey_version_jumps as v, check_journey_evidence as gate

CANDIDATE = {
    'app': 'f069b6a3d324c05227833d1324dce62cacc48b68',
    'agent': '1a65fb7e7fae8af16617094167ad6dde9c266e69',
    'runtime': '2776ba973b6c8f3555b5629b0840d9691a98d3c2',
    'pinokio': '3a6a98ae251ca126505245286e2d3e760f4348e9',
    'manifest_sha256': '4345d0d3d1c1a99e19ea5eacc35d649557787adb0537fd33f81fad5e35983476',
    'artifacts': {'public/install': '100bab1deb15d620abd910c8fb0c9a6d77f4945428a3229dac6b506705e3fd0d',
                  'public/install.sh': '591c4c51b148fccef2a7d1c45782d9ff949d5fd03d46cd82b5591e3a23d1b285',
                  'public/install.ps1': 'bba3f2edc41c047cb51475ceeac4489a04610af73a3af44bdc05287964d28258'}}


def lock_observation(path, *, busy):
    # No create: the real installed realm must already contain the lock file.
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held = True
        else:
            held = False
            fcntl.flock(fd, fcntl.LOCK_UN)
        if held != busy:
            raise RuntimeError(f'owner.lock expected {"busy" if busy else "free"}, observed {"busy" if held else "free"}')
        return {'path': str(path), 'device': info.st_dev, 'inode': info.st_ino, 'busy': held}
    finally:
        os.close(fd)


def check_data(before, after):
    for key in ('project', 'timeline', 'preferences', 'cas_sha256'):
        if key not in before or key not in after or before[key] != after[key]:
            raise RuntimeError('Seeded data changed or missing: ' + key)
    if after['schema_version'] != after['target_schema_version']:
        raise RuntimeError('Candidate schema migration incomplete')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--app', required=True, type=Path)
    p.add_argument('--run-root', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    a = p.parse_args()
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    result = {'test': 'l2_real_linux_r0_to_r1', 'status': 'fail', 'candidate': CANDIDATE,
              'uid': os.getuid(), 'checks': {}, 'commands': []}
    original_run = j.run
    def recorded_run(argv, log, *, env=None, timeout=3600):
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        row = {'argv': list(map(str, argv)), 'log': str(log), 'timeout_seconds': timeout, 'exit': None}
        result['commands'].append(row)
        try:
            with log.open('w', encoding='utf-8') as stream:
                completed = subprocess.run(row['argv'], env=env, stdout=stream, stderr=subprocess.STDOUT,
                                           timeout=timeout, check=False)
            row['exit'] = completed.returncode
            if completed.returncode:
                raise RuntimeError(f'{log.name}: exit {completed.returncode}; see {log}')
            return log.read_text(encoding='utf-8-sig', errors='replace')
        except subprocess.TimeoutExpired:
            row['timeout'] = True
            raise
        finally:
            j.write_json(out / 'result.json', result)
    j.run = recorded_run
    root, data = a.run_root / 'program', a.run_root / 'data'
    try:
        if sys.platform != 'linux' or os.getuid() == 0:
            raise RuntimeError('L2 requires non-root Linux')
        if os.environ.get('GITHUB_ACTIONS') != 'true':
            raise RuntimeError('L2 requires the isolated hosted VM setup')
        j.run_scope(a.run_root, root, data, 'release0-to-release1')
        probe = subprocess.run(['systemctl', '--user', 'show-environment'], capture_output=True, text=True, timeout=10)
        result['manager_probe'] = {'exit': probe.returncode, 'stdout': probe.stdout, 'stderr': probe.stderr}
        if probe.returncode:
            raise RuntimeError('No real systemd user manager')
        j.assert_host_available('fresh-install', root, data)
        with j.isolated_environment(a.run_root, root, data):
            fixture, r0_manifest, r0 = v.verified_release0_fixture(v.RELEASE0_FIXTURE)
            result['r0_fixture'] = r0
            app = a.app.resolve(strict=True)
            if j.git_source(app)['sha'] != CANDIDATE['app']:
                raise RuntimeError('App checkout mismatch')
            manifest_path, manifest = j.selected_manifest(app, source_override=False, agent=None, runtime=None, evidence=out)
            if gate.validate_tuple(CANDIDATE, app, manifest_path) != manifest or manifest.get('release') != 1:
                raise RuntimeError('Candidate manifest mismatch')
            v.bootstrap_release0(fixture, r0_manifest, 'sh', out / 'r0', root, data)
            j.wait_ready(data, timeout=300)
            old_path, old_data, old = j.installation(root, data)
            if old_data != data or v.installed_release(old) != 0:
                raise RuntimeError('R0 installation identity mismatch')
            old_tuple = j.selected_tuple(j.read_json(r0_manifest), old)
            old_service = j.assert_direct_service_ownership(root, data)
            j.assert_one_runtime(data)
            old_unit = Path(old_service['service_path']).read_text(encoding='utf-8')
            if str(old['root']) not in old_unit or 'Restart=always' not in old_unit:
                raise RuntimeError('R0 unit does not name the selected R0 or its authentic restart policy')
            (out / 'r0-unit.service').write_text(old_unit, encoding='utf-8')
            old_health = j.healthy(data)
            realm = j.workspace_root(data)
            result['r0'] = {'service': old_service, 'health': old_health, 'tuple': old_tuple,
                            'lock_busy': lock_observation(realm / 'owner.lock', busy=True)}
            keys = v.direct_workspace(root, old, data, out / 'r0-data', 'seed',
                                      {'project': 'tst-l2-real-linux', 'generation': 'release0',
                                       'task_id': None, 'create_project': True})
            before = v.direct_workspace(root, old, data, out / 'r0-data', 'snapshot', keys)
            # Check the precise same lock becomes free after the real manager stops the unit.
            def free_when_stopped(stage):
                j.run(['systemctl', '--user', 'stop', 'astrid.service'], out / (stage + '-stop.log'), timeout=120)
                try:
                    deadline = time.monotonic() + 120
                    while j.runtime_processes(data):
                        if time.monotonic() >= deadline:
                            raise RuntimeError('Runtime did not stop')
                        time.sleep(1)
                    return lock_observation(realm / 'owner.lock', busy=False)
                finally:
                    j.run(['systemctl', '--user', 'start', 'astrid.service'], out / (stage + '-start.log'), timeout=120)
                    j.wait_ready(data, timeout=300)
            result['r0']['lock_free_stopped'] = free_when_stopped('r0-lock')
            result['r0']['lock_busy_restarted'] = lock_observation(realm / 'owner.lock', busy=True)
            if len({(x['device'], x['inode']) for x in (result['r0']['lock_busy'], result['r0']['lock_free_stopped'], result['r0']['lock_busy_restarted'])}) != 1:
                raise RuntimeError('R0 owner.lock was replaced during the stop/start check')
            old_health = j.healthy(data)
            old_pid, old_birth = int(old_health['pid']), j.process_birth(int(old_health['pid']))
            if not old_birth:
                raise RuntimeError('R0 owner birth unavailable')
            j.bootstrap(app, manifest_path, 'sh', 'update', out / 'r1', root, data)
            j.wait_ready(data, timeout=300)
            new_path, new_data, new = j.installation(root, data)
            if new_data != old_data or new_path == old_path or v.installed_release(new) != 1:
                raise RuntimeError('Candidate did not select a new version on the same data root')
            selected = v.selected_candidate(manifest, new, old_tuple)
            new_service = j.assert_direct_service_ownership(root, data)
            v.service_action(root, data, new, old)
            (out / 'r1-unit.service').write_text(Path(new_service['service_path']).read_text(encoding='utf-8'), encoding='utf-8')
            j.assert_one_runtime(data)
            new_health = j.healthy(data)
            if new_health['runtime_instance_id'] == old_health['runtime_instance_id'] or j.process_birth(old_pid) == old_birth:
                raise RuntimeError('R0 owner survived upgrade')
            if j.workspace_root(data) != realm:
                raise RuntimeError('Realm changed during upgrade')
            result['r1'] = {'service': new_service, 'health': new_health, 'tuple': selected,
                            'lock_busy': lock_observation(realm / 'owner.lock', busy=True)}
            after = v.direct_workspace(root, new, data, out / 'r1-data', 'snapshot', keys)
            check_data(before, after)
            result['data'] = {'before': before, 'after': after}
            result['r1']['lock_free_stopped'] = free_when_stopped('r1-lock')
            result['r1']['lock_busy_restarted'] = lock_observation(realm / 'owner.lock', busy=True)
            if len({(x['device'], x['inode']) for x in (result['r1']['lock_busy'], result['r1']['lock_free_stopped'], result['r1']['lock_busy_restarted'])}) != 1:
                raise RuntimeError('R1 owner.lock was replaced during the stop/start check')
            result['checks'] = {'r0_service': 'pass', 'r1_service': 'pass', 'data': 'pass', 'owner_lock': 'pass'}
            result['status'] = 'pass'
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        traceback.print_exc()
    finally:
        j.write_json(out / 'result.json', result)
        j.run = original_run
    return 0 if result['status'] == 'pass' else 1

if __name__ == '__main__':
    sys.exit(main())
