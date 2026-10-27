"""Health-independent orchestration of the one canonical local engine installer.

Engine/session ownership belongs exclusively to engine_custody. This wrapper
owns only its finite CLI children and the separate read-only observation agent.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import selectors
import subprocess
import sys
import threading
import time
from pathlib import Path

from ._engine_target_files import _TargetFiles
from .engine_release import ReleaseError, verify_tree

SEGMENT_BYTES = 1024 * 1024
STREAM_BYTES = 8 * SEGMENT_BYTES
PUBLIC_PATH = '/usr/local/bin:/usr/bin:/bin'


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 604800:
        raise ValueError('invalid startup budget')
    return float(value)


def _public_environment(home):
    return {'PATH': PUBLIC_PATH, 'HOME': str(home), 'LANG': 'C.UTF-8',
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'}


class _Segments:
    """Drain continuously; publish only closed bounded segments by rename."""
    def __init__(self, public, private, stream, *, segment_bytes, stream_bytes):
        self.public, self.private, self.stream = public, private, stream
        self.segment_bytes, self.limit = segment_bytes, stream_bytes
        self.buffer = bytearray()
        self.retained = self.discarded = 0
        self.files = []

    def write(self, data):
        keep = data[:max(0, self.limit - self.retained)]
        self.discarded += len(data) - len(keep)
        self.retained += len(keep)
        self.buffer.extend(keep)
        while len(self.buffer) >= self.segment_bytes:
            self._publish(bytes(self.buffer[:self.segment_bytes]))
            del self.buffer[:self.segment_bytes]

    def _publish(self, data):
        name = f'{self.stream}.{len(self.files):06d}.log'
        temporary = self.private / name
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.public / name)
        sha = hashlib.sha256(data).hexdigest()
        sidecar = self.private / (name + '.sha256')
        with sidecar.open('x') as stream:
            stream.write(sha + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(sidecar, self.public / (name + '.sha256'))
        self.files.append({'path': str(self.public / name), 'sha256': sha, 'bytes': len(data)})

    def close_step(self):
        if self.buffer:
            self._publish(bytes(self.buffer))
            self.buffer.clear()

    def facts(self):
        return {'files': list(self.files), 'retained_bytes': self.retained,
                'discarded_bytes': self.discarded, 'truncated': self.discarded > 0}


def _run(argv, *, cwd, env, deadline, logs, stop_event, on_started=None):
    from runtime_protocol import platform

    process = platform.spawn_tree(tuple(argv), cwd=str(cwd), env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = bytearray()
    overflow = False
    timed_out = cancelled = False
    selector = selectors.DefaultSelector()
    stopped = False
    observation_launch_failed = False
    try:
        for stream, name in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        # Installer is already executing before agent creation or any observation.
        if on_started is not None:
            try:
                on_started()
            except Exception:
                observation_launch_failed = True
        while selector.get_map():
            if stop_event.is_set() or time.monotonic() >= deadline:
                cancelled, timed_out = stop_event.is_set(), time.monotonic() >= deadline
                platform.stop_tree(process, grace=0.2)
                stopped = True
                break
            events = selector.select(min(0.05, max(0, deadline - time.monotonic())))
            for key, _ in events:
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                logs[key.data].write(data)
                if key.data == 'stdout':
                    available = SEGMENT_BYTES - len(output)
                    output.extend(data[:max(0, available)])
                    overflow |= len(data) > available
            if not stopped and platform.tree_exited(process):
                # Retain the unreaped child until exact descendant cleanup.
                platform.stop_tree(process, grace=0.1)
                stopped = True
        if not stopped:
            # Pipes can close before the process exits; keep the same deadline.
            while not platform.tree_exited(process):
                if stop_event.is_set() or time.monotonic() >= deadline:
                    cancelled, timed_out = stop_event.is_set(), time.monotonic() >= deadline
                    break
                stop_event.wait(min(0.05, max(0, deadline - time.monotonic())))
            platform.stop_tree(process, grace=0.2)
            stopped = True
        # Drain bytes already buffered after exact stop, bounded by pipe contents.
        for key in list(selector.get_map().values()):
            while True:
                try:
                    data = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    break
                if not data:
                    break
                logs[key.data].write(data)
                if key.data == 'stdout':
                    available = SEGMENT_BYTES - len(output)
                    output.extend(data[:max(0, available)])
                    overflow |= len(data) > available
        return {'exit_code': process.returncode, 'timed_out': timed_out,
                'cancelled': cancelled, 'stdout_overflow': overflow,
                'observation_launch_failed': observation_launch_failed}, bytes(output)
    finally:
        try:
            if not stopped:
                platform.stop_tree(process, grace=0.2)
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()
            for log in logs.values():
                log.close_step()


def _validate_output(raw, *, root, rerun):
    from .engine_recipe import load_recipe
    from .engine_runner import _parse_receipt, plan_install
    recipe = load_recipe('vibecomfy-h3', 'linux-x86_64-cu130')
    plan = plan_install(recipe, root=str(root), placement='local')
    value = json.loads(raw)
    receipt = _parse_receipt(value, plan)
    if receipt.status != 'installed':
        raise ValueError('canonical operation did not return an installed receipt')
    if rerun and any(s.action != 'skipped' or s.validation != 'passed' for s in receipt.steps):
        raise ValueError('canonical rerun was not all skipped and passed')
    return value


def run_sequence(*, cli_prefix, root, observations, private, timeout_seconds=3600,
                 step_seconds=1800, on_install_started=None, stop_event=None,
                 segment_bytes=SEGMENT_BYTES, stream_bytes=STREAM_BYTES):
    """One finite install/status/rerun; injectable prefix is a local test seam.

    Production main supplies only the verified installed canonical entrypoint.
    No retries, health requests, provider operations, or replacement installer.
    """
    timeout_seconds, step_seconds = _seconds(timeout_seconds), _seconds(step_seconds)
    if (type(segment_bytes) is not int or type(stream_bytes) is not int
            or not 0 < segment_bytes <= SEGMENT_BYTES or not 0 < stream_bytes <= STREAM_BYTES):
        raise ValueError('invalid log budget')
    from .engine_runner import _root
    root = Path(_root(str(root), 'linux-x86_64-cu130'))
    observations, private = Path(observations).absolute(), Path(private).absolute()
    if observations == private or observations in private.parents or private in observations.parents:
        raise ValueError('public and private paths must be separate')
    end = time.monotonic() + timeout_seconds
    public_files = _TargetFiles(str(observations))
    private_files = _TargetFiles(str(private))
    public_files.ensure_dir(str(observations), deadline=end)
    private_files.ensure_dir(str(private), deadline=end)
    # Exclusive attempt admission; an existing attempt is never age-reset.
    marker = private / 'attempt'
    with marker.open('x') as stream:
        stream.write('startup\n')
    if any(observations.iterdir()):
        raise ValueError('observation attempt is not empty')
    root_files = _TargetFiles(str(root))
    receipt_path = root / '.astrid-engine/install.json'
    status_path = observations / 'status.json'
    stop_event = stop_event or threading.Event()
    logs = {name: _Segments(observations, private, name, segment_bytes=segment_bytes,
                            stream_bytes=stream_bytes) for name in ('stdout', 'stderr')}
    status = {'state': 'starting', 'stage': 'install', 'receipt_path': str(receipt_path),
              'receipt': None, 'steps': [], 'qualified': False}

    def publish():
        status['logs'] = {name: log.facts() for name, log in logs.items()}
        # Leave publication a small reserve even if an operation uses its budget.
        public_files.write_json_atomic(str(status_path), status, deadline=time.monotonic() + 2)

    def snapshot(label):
        limit = time.monotonic() + 2
        fact = root_files.file_fact(str(receipt_path), hash_bytes=True, deadline=limit)
        if not fact.exists:
            return
        if not fact.regular or fact.symlink or fact.bytes > SEGMENT_BYTES:
            raise ValueError('invalid canonical receipt file')
        value = root_files.read_json(str(receipt_path), deadline=limit)
        path = observations / (label + '-install.json')
        public_files.write_json_atomic(str(path), value, deadline=limit)
        saved = public_files.file_fact(str(path), hash_bytes=True, deadline=limit)
        status['receipt'] = {'path': str(receipt_path), 'sha256': fact.sha256,
                             'snapshot_path': str(path), 'snapshot_sha256': saved.sha256}

    publish()
    try:
        for label, command in (('first', 'install'), ('status', 'status'), ('rerun', 'install')):
            status['stage'] = label
            publish()
            remaining = min(step_seconds, end - time.monotonic())
            if remaining <= 0 or stop_event.is_set():
                raise TimeoutError('startup budget exhausted')
            argv = (*cli_prefix, 'engines', command, 'vibecomfy-h3', '--placement', 'local',
                    '--platform', 'linux-x86_64-cu130', '--root', str(root),
                    '--deadline', str(remaining))
            result, raw = _run(argv, cwd=private, env=_public_environment(private),
                               deadline=min(end, time.monotonic() + remaining), logs=logs,
                               stop_event=stop_event,
                               on_started=on_install_started if label == 'first' else None)
            status['steps'].append({'stage': label, **result})
            snapshot(label)
            publish()
            if (result['exit_code'] != 0 or result['timed_out'] or result['cancelled']
                    or result['stdout_overflow']):
                raise ValueError('canonical CLI step failed')
            output = _validate_output(raw, root=root, rerun=label == 'rerun')
            if label != 'status':
                stored = root_files.read_json(str(receipt_path), deadline=time.monotonic() + 2)
                if output != stored:
                    raise ValueError('CLI output differs from canonical receipt')
            public_files.write_json_atomic(str(observations / (label + '-result.json')),
                                            output, deadline=time.monotonic() + 2)
        status['state'] = 'succeeded'
    except Exception:
        status['state'] = 'failed'
        status['error'] = 'startup_step_failed'
        # Preserve any canonical failed/partial receipt; never manufacture one.
        try:
            snapshot('failure')
        except Exception:
            status['receipt_observation_failed'] = True
    finally:
        for log in logs.values():
            log.close_step()
        publish()
    return status


def _finish_session(stop_owned, observations):
    """Preserve shutdown uncertainty in public status without minting authority."""
    import re
    files = _TargetFiles(str(observations))
    path = str(Path(observations) / 'status.json')
    try:
        stop_owned(deadline=time.monotonic() + 5)
    except Exception as exc:
        try:
            status = dict(files.read_json(path, deadline=time.monotonic() + 2))
            status.update(state='failed', session_stop='unresolved')
            handle = getattr(exc, 'recovery_handle', None)
            if isinstance(handle, str) and re.fullmatch(r'[A-Za-z0-9._:/-]{1,512}', handle):
                status['recovery_handle'] = handle
            files.write_json_atomic(path, status, deadline=time.monotonic() + 2)
        except Exception:
            pass  # Preserve the original custody failure if observation also fails.
        raise
    status = dict(files.read_json(path, deadline=time.monotonic() + 2))
    status['session_stop'] = 'stopped'
    files.write_json_atomic(path, status, deadline=time.monotonic() + 2)


def main(argv=None, *, release_root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--volume', default='/workspace')
    parser.add_argument('--root', required=True)
    parser.add_argument('--attempt', required=True)
    parser.add_argument('--seconds', type=float, default=3600)
    parser.add_argument('--step-seconds', type=float, default=1800)
    args = parser.parse_args(argv)
    if release_root is None:
        parser.error('use the verified retained release launcher')
    import re
    if not re.fullmatch('[a-zA-Z0-9_-]{1,80}', args.attempt):
        parser.error('invalid attempt identifier')
    seconds, step_seconds = _seconds(args.seconds), _seconds(args.step_seconds)
    volume = Path(args.volume).absolute()
    root = Path(args.root).absolute()
    if not volume.is_mount() or root == volume or volume not in root.parents:
        raise ReleaseError('expected install root below the mounted selected volume')
    manifest = verify_tree(release_root)
    if not manifest['complete'] or manifest['fixture']:
        raise ReleaseError('incomplete release cannot self-start')
    token = os.environ.pop('ASTRID_POD_TOKEN', '')
    os.environ.pop('ASTRID_POD_AGENT_B64', None)
    os.environ.pop('ASTRID_POD_OBSERVATION_B64', None)
    if len(token) < 32 or not token.isascii():
        raise ReleaseError('existing observation-agent token is required')
    observation = volume / 'astrid/startup' / args.attempt
    private = volume / 'astrid/private/startup' / args.attempt
    prefix = (sys.executable, '-I', '-S', str(Path(release_root) / 'run.py'), 'cli')
    from runtime_protocol import platform
    stop = threading.Event()
    restore = platform.install_stop_handlers(stop.set)
    observer = None
    installer_started = False
    end = time.monotonic() + seconds

    def start_observer():
        nonlocal observer, installer_started
        installer_started = True
        policy = {'schema': 'astrid.pod-observation.v1', 'roots': [str(observation)],
                  'files': [str(root / '.astrid-engine/install.json')]}
        env = _public_environment(private)
        env.update(ASTRID_POD_TOKEN=token, ASTRID_POD_PORT='8000',
                   ASTRID_POD_OBSERVATION_B64=base64.b64encode(json.dumps(policy).encode()).decode())
        observer = platform.spawn_tree((sys.executable, '-I', '-S', str(Path(release_root) / 'run.py'),
                                        'agent'), cwd=str(private), env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        result = run_sequence(cli_prefix=prefix, root=root, observations=observation,
                              private=private, timeout_seconds=max(0.001, end - time.monotonic()),
                              step_seconds=step_seconds, on_install_started=start_observer,
                              stop_event=stop)
        # Keep the installation session and observation process within this
        # bounded original startup lifetime. Agent health is never consulted.
        while result['state'] == 'succeeded' and not stop.is_set() and time.monotonic() < end:
            stop.wait(min(0.2, max(0, end - time.monotonic())))
        return 0 if result['state'] == 'succeeded' else 1
    finally:
        try:
            if installer_started:
                def stop_owned(*, deadline):
                    from .engine_execution import create_local_backend
                    context, _ = create_local_backend(engine='vibecomfy-h3', platform='linux-x86_64-cu130',
                                                       placement='local', root=str(root), deadline=deadline)
                    context.service_client.stop_owned(deadline=deadline)
                _finish_session(stop_owned, observation)
        finally:
            try:
                if observer is not None:
                    platform.stop_tree(observer, grace=0.2)
            finally:
                restore()
