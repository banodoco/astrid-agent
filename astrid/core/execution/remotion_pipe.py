"""Bounded renderer relay on the existing executor's standard pipes.

The original host lease/socket and retained native owner never cross exec.
Authority is the host's retained Popen and its original private pipe endpoints;
bootstrap metadata binds that launch, never grants an arbitrary descriptor.
"""
import hashlib
import json
import os
import secrets
import select
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .process_group import process_birth_identity

LIMIT = 8 * 1024 * 1024
PREFIX = b'\x1eASTRID_REMOTION_PIPE '
VERSION = 'astrid.remotion.pipe/v1'
_executor_client_required = False
_installed_executor_client = None
_installed_executor_channel = None


@dataclass(frozen=True)
class ExecutorSelection:
    project: str
    node: str
    cli: str
    attempt: str
    renderer: str
    generation: int
    digest: str


def _selection(binding, digest):
    """Validate metadata lexically; only the retained host may inspect these paths."""
    if not isinstance(binding, dict):
        raise ValueError('invalid renderer pipe bootstrap binding')
    admission = binding.get('admission')
    selection = binding.get('selection')
    if (binding.get('purpose') != 'rendering.timeline_visualize'
            or not isinstance(admission, dict)
            or admission.get('capability_id') != 'rendering.timeline_visualize'
            or not isinstance(admission.get('capability_digest'), str) or not admission['capability_digest']
            or type(binding.get('pid')) is not int or binding['pid'] != os.getpid()
            or not binding.get('birth') or binding['birth'] != process_birth_identity()):
        raise ValueError('renderer pipe bootstrap differs from original executor incarnation or capability')
    if (not isinstance(selection, dict) or set(selection) != {'projectDir', 'nodeExecutable', 'remotionCli'}
            or type(binding.get('generation')) is not int or binding['generation'] < 1
            or any(not isinstance(binding.get(key), str) or not binding[key]
                   for key in ('attempt', 'lease', 'channel', 'selected_renderer', 'launch_digest', 'executable'))):
        raise ValueError('invalid renderer pipe selection binding')
    for value in (*selection.values(), binding['attempt']):
        if (not isinstance(value, str) or '\0' in value or not Path(value).is_absolute()
                or '..' in Path(value).parts or str(Path(value)) != value):
            raise ValueError('renderer pipe selection requires canonical host paths')
    if selection['remotionCli'] != str(Path(selection['projectDir']) / 'node_modules/@remotion/cli/remotion-cli.js'):
        raise ValueError('renderer pipe CLI differs from host project')
    return ExecutorSelection(selection['projectDir'], selection['nodeExecutable'], selection['remotionCli'],
                             binding['attempt'], binding['selected_renderer'], binding['generation'], digest)


def executor_selection(client):
    """Return only the currently installed pipe selection; removal never enables local mode."""
    if _executor_client_required:
        if (client is None or client is not _installed_executor_client
                or client.channel is not _installed_executor_channel):
            raise ValueError('renderer pipe installed binding is absent or replaced')
        return client.channel.validate_binding()
    # Existing internal-invocation context only restricts use; it grants no
    # authority and cannot enable a pipe or a local renderer.
    if os.environ.get('ASTRID_INTERNAL_INVOCATION') == '1':
        raise ValueError('renderer pipe bootstrap is required in an executor')
    return None


def _encode(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()+b'\n'
    if len(raw) > LIMIT:
        raise ValueError('renderer pipe frame exceeds bound')
    return raw


def _decode(raw):
    if len(raw) > LIMIT or not raw.endswith(b'\n') or b'\n' in raw[:-1]:
        raise ValueError('renderer pipe requires one bounded complete frame')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('renderer pipe frame must be an object')
    return value


def _write(fd, raw, *, deadline, guard=lambda: None):
    pending = memoryview(raw)
    blocking = os.get_blocking(fd)
    try:
        os.set_blocking(fd, False)
        while pending:
            guard()
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('renderer pipe write timed out')
            if select.select([], [fd], [], min(0.1, remaining))[1]:
                try:
                    pending = pending[os.write(fd, pending):]
                except BlockingIOError:
                    pass
    finally:
        os.set_blocking(fd, blocking)


def _line(fd, *, timeout=300):
    raw = bytearray()
    deadline = time.monotonic()+timeout
    # This is a serial request protocol: the peer cannot send the next frame
    # until this bootstrap/response has been consumed. Reject coalesced frames.
    while not raw.endswith(b'\n'):
        if len(raw) >= LIMIT:
            raise ValueError('renderer pipe frame exceeds bound')
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('renderer pipe read timed out')
        if select.select([fd], [], [], min(0.1, remaining))[0]:
            part = os.read(fd, min(65536, LIMIT-len(raw)))
            if not part:
                raise EOFError('renderer pipe closed before complete frame')
            raw.extend(part)
    return bytes(raw)


class ExecutorPipeChannel:
    """recv/sendall interface for the existing HostRemotionClient/parser."""
    def __init__(self, binding, bootstrap, read_fd=0, write_fd=1):
        for fd in (read_fd, write_fd):
            if not stat.S_ISFIFO(os.fstat(fd).st_mode):
                raise ValueError('renderer executor requires standard pipes')
        self.read_fd, self.write_fd = os.dup(read_fd), os.dup(write_fd)
        os.set_inheritable(self.read_fd, False)
        os.set_inheritable(self.write_fd, False)
        self.binding, self.sequence, self.pending = binding, 0, bytearray()
        self._bootstrap = _encode(bootstrap)
        self._selection = _selection(bootstrap, binding)
        self.closed = False
        self.failed = False

    def validate_binding(self):
        from .remotion_host_session import host_remotion_client
        if (self.closed or self.failed or _installed_executor_client is None
                or host_remotion_client() is not _installed_executor_client
                or _installed_executor_client.channel is not self
                or hashlib.sha256(self._bootstrap).hexdigest() != self.binding
                or _selection(_decode(self._bootstrap), self.binding) != self._selection):
            raise ValueError('renderer pipe installed binding is invalidated')
        return self._selection

    def sendall(self, raw):
        self.validate_binding()
        frame = _decode(raw)
        if set(frame) != {'identity', 'request'} or self.pending:
            raise ValueError('invalid renderer pipe request state')
        self.sequence += 1
        frame['_rendererPipe'] = {'binding': self.binding, 'sequence': self.sequence}
        try:
            _write(self.write_fd, PREFIX+_encode(frame), deadline=time.monotonic()+300)
        except BaseException:
            self.failed = True
            raise

    def recv(self, count):
        self.validate_binding()
        if not self.pending:
            try:
                raw = _line(self.read_fd)
                response = _decode(raw)
                metadata = response.get('_rendererPipe')
                if (not isinstance(metadata, dict) or type(metadata.get('sequence')) is not int
                        or metadata != {'binding': self.binding, 'sequence': self.sequence}):
                    raise ValueError('renderer pipe response binding or sequence differs')
            except BaseException:
                self.failed = True
                raise
            # Preserve the actual wire bytes for the original response parser.
            self.pending.extend(raw)
        answer = bytes(self.pending[:count])
        del self.pending[:count]
        return answer

    def close(self):
        if not self.closed:
            self.closed = True
            os.close(self.read_fd)
            os.close(self.write_fd)


def install_executor_client():
    """Trusted worker bootstrap, before any admitted executor code runs."""
    from . import remotion_host_session as host
    global _executor_client_required, _installed_executor_client, _installed_executor_channel
    _executor_client_required = True
    if host.host_remotion_client() is not None:
        raise RuntimeError('renderer client already installed')
    bootstrap = _decode(_line(0, timeout=30))
    if set(bootstrap) != {'protocol', 'binding', 'digest'} or bootstrap['protocol'] != VERSION:
        raise ValueError('invalid renderer pipe bootstrap')
    binding = bootstrap['binding']
    digest = hashlib.sha256(_encode(binding)).hexdigest()
    if (bootstrap['digest'] != digest or not isinstance(binding, dict)
            or binding.get('purpose') != 'rendering.timeline_visualize'
            or binding.get('pid') != os.getpid()
            or binding.get('birth') != process_birth_identity()
            or binding.get('executable') != str(Path(sys.executable).resolve())):
        raise ValueError('renderer pipe bootstrap differs from original executor incarnation')
    _selection(binding, digest)
    channel = ExecutorPipeChannel(digest, binding)
    host._client = host.HostRemotionClient(channel)
    _installed_executor_client = host._client
    _installed_executor_channel = channel
    # Ordinary pack print output stays separate from the reserved stdout pipe.
    sys.stdout = sys.stderr
    return host._client


class HostPipeBridge:
    """Trusted host dispatcher using its original private lease socket only."""
    def __init__(self, lease, process, *, admission, launch_digest):
        from .remotion_host_session import HostRemotionClient
        self.lease, self.process = lease, process
        self.original = HostRemotionClient(lease.child)
        birth = getattr(process, '_astrid_process_birth', None) or process_birth_identity(process.pid)
        if not birth:
            raise RuntimeError('renderer executor incarnation is unavailable')
        if admission.get('capability_id') != 'rendering.timeline_visualize' or not admission.get('capability_digest'):
            raise ValueError('renderer relay requires the admitted capability identity')
        self.generation = lease.owner.frame_owner.generation
        self.binding = {'purpose': 'rendering.timeline_visualize', 'admission': admission,
                        'attempt': str(lease.attempt), 'lease': secrets.token_hex(24),
                        'channel': secrets.token_hex(24), 'generation': self.generation,
                        'selected_renderer': hashlib.sha256(_encode(lease.selected_identity)).hexdigest(),
                        'selection': {'projectDir': str(lease.project), 'nodeExecutable': str(lease.node),
                                      'remotionCli': str(lease.cli)},
                        'launch_digest': launch_digest, 'pid': process.pid, 'birth': birth,
                        'executable': str(Path(sys.executable).resolve())}
        self.digest = hashlib.sha256(_encode(self.binding)).hexdigest()
        self.sequence = 0
        self.error = None
        self.cancelled = threading.Event()
        self.stdout, self.stderr = bytearray(), bytearray()
        self._closed = False
        self._released = False
        self._release_lock = threading.Lock()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.stderr_thread = threading.Thread(target=self._stderr, daemon=True)
        try:
            _write(process.stdin.fileno(), _encode({'protocol': VERSION, 'binding': self.binding, 'digest': self.digest}),
                   deadline=time.monotonic()+30)
            self.thread.start()
            self.stderr_thread.start()
        except BaseException as error:
            # The launch caller retains the original Popen. It must wait it,
            # then finish even a partially started dispatcher before settlement.
            error.remotion_bridge = self
            raise

    def _guard(self):
        if self.cancelled.is_set() or self.process.poll() is not None:
            raise RuntimeError('renderer executor is no longer active')
        if self.lease.owner.frame_owner.generation != self.generation:
            raise RuntimeError('renderer owner generation changed')

    def _dispatch(self, raw):
        from .remotion_host_session import _read
        frame = _decode(raw)
        metadata = frame.pop('_rendererPipe', None)
        if (not isinstance(metadata, dict) or type(metadata.get('sequence')) is not int
                or metadata != {'binding': self.digest, 'sequence': self.sequence+1}
                or set(frame) != {'identity', 'request'}):
            raise ValueError('renderer pipe request binding, sequence or fields differ')
        self._guard()
        self.sequence += 1
        # All original lease admission/native ownership still lives behind the
        # existing private host socket; this is not a callback renderer backend.
        response = self.original.request(frame['identity'], frame['request'], _read)
        self._guard()
        if self.process.poll() is None and not self.cancelled.is_set():
            response = {**response, '_rendererPipe': {'binding': self.digest, 'sequence': self.sequence}}
            _write(self.process.stdin.fileno(), _encode(response), deadline=time.monotonic()+300, guard=self._guard)

    def _serve(self):
        pending = bytearray()
        try:
            while True:
                part = os.read(self.process.stdout.fileno(), 65536)
                if not part:
                    if pending.startswith(PREFIX):
                        raise EOFError('renderer executor closed with an incomplete frame')
                    self.stdout.extend(pending)
                    if len(self.stdout) > LIMIT:
                        raise ValueError('renderer executor stdout exceeds bound')
                    return
                pending.extend(part)
                while b'\n' in pending:
                    index = pending.index(b'\n')+1
                    line = bytes(pending[:index]); del pending[:index]
                    if line.startswith(PREFIX):
                        self._dispatch(line[len(PREFIX):])
                    else:
                        self.stdout.extend(line)
                        if len(self.stdout) > LIMIT:
                            raise ValueError('renderer executor stdout exceeds bound')
                if len(pending) > LIMIT+len(PREFIX):
                    raise ValueError('renderer executor frame exceeds bound')
        except BaseException as error:
            self.error = error

    def _stderr(self):
        try:
            while True:
                chunk = os.read(self.process.stderr.fileno(), 65536)
                if not chunk:
                    return
                self.stderr.extend(chunk)
                if len(self.stderr) > LIMIT:
                    raise ValueError('renderer executor stderr exceeds bound')
        except BaseException as error:
            self.error = self.error or error

    def check(self):
        if self.error:
            raise RuntimeError('renderer pipe relay failed') from self.error

    def finish(self, *, cancelled=False):
        if cancelled:
            self.cancelled.set()
        # A task exit is not native cleanup. Keep the original request/owner
        # attached through its existing bounded exchange, then release normally.
        if self.thread.ident is not None:
            self.thread.join(timeout=315)
        if self.stderr_thread.ident is not None:
            self.stderr_thread.join(timeout=5)
        if self.thread.is_alive() or self.stderr_thread.is_alive():
            error = RuntimeError('renderer pipe dispatcher cleanup remains unresolved')
            self.lease.owner.uncertain = self.lease.owner.uncertain or error
            raise error
        try:
            if cancelled:
                from .remotion_host_session import _read
                with self._release_lock:
                    if not self._released:
                        response = self.original.request('cancelled-executor', {'type':'release','force':True}, _read)
                        if response.get('ok') is not True or response.get('exit') is not True:
                            raise RuntimeError('original renderer cancellation cleanup unproved')
                        self._released = True
            self.check()
            return self.stdout.decode('utf-8', errors='replace'), self.stderr.decode('utf-8', errors='replace')
        finally:
            if not self._closed:
                self._closed = True
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    stream.close()
