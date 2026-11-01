"""Attempt-bound entry into the existing engine installer.

This is an internal entry point; possession of task identifiers alone does not
constitute Runtime admission. The caller must verify the task envelope first.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import socket
import threading
import time

from .engine_attempt_custody import AttemptIdentity, _settle_attempt, arm_attempt
from .engine_contracts import DeadlineExceeded, IntegrityError
from .engine_custody import LinuxEngineServiceClient
from .engine_execution import LocalLinuxExecutionContext
from .engine_install_task import recheck_install_under_custody
from .engine_recipe import load_recipe
from .engine_runner import install_engine, plan_install


@dataclass(frozen=True)
class InstallCheckpoint:
    identity: AttemptIdentity
    receipt: dict


@dataclass(frozen=True)
class ObservedStop:
    identity: AttemptIdentity


@dataclass(frozen=True)
class RecoveryHandle:
    identity: AttemptIdentity
    reason: str


class _AttemptClient(LinuxEngineServiceClient):
    """Track only this attempt's channels, never the shared engine process."""
    def __init__(self, original, attempt):
        super().__init__(**original.identity)
        self._attempt = attempt
        self._channels = []
        self._channels_lock = threading.Lock()

    def _connect(self, fd, deadline, *, start):
        channel = super()._connect(fd, deadline, start=start)
        if channel is not None:
            with self._channels_lock:
                if self._attempt._cancel_requested.is_set():
                    channel.close()
                    raise DeadlineExceeded('installer attempt cancelled')
                self._channels.append(channel)
        return channel

    def interrupt(self):
        with self._channels_lock:
            for channel in self._channels:
                try:
                    channel.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class InstallAttempt:
    def __init__(self, identity):
        self.identity = identity
        self._admitted = threading.Event()
        self._done = threading.Event()
        self._cancel_requested = threading.Event()
        self._gate = threading.Lock()
        self._completed = False
        self._started_service = False
        self._root_identity = None
        self._client = None
        self._admission_error = None
        self._result = None
        self._error = None

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise TimeoutError('installer attempt is still in flight')
        if self._error is not None:
            raise self._error
        return self._result


class _AttemptContext:
    def __init__(self, context, attempt, callback, deadline):
        self._context, self._attempt, self._callback = context, attempt, callback
        self._deadline = deadline
        self._root_identity = None

    def __getattr__(self, name):
        return getattr(self._context, name)

    @contextmanager
    def acquire_install_lock(self, root, *, deadline):
        with self._context.acquire_install_lock(root, deadline=deadline):
            identity = self._attempt.identity
            self._root_identity = recheck_install_under_custody(identity.binding)
            self._attempt._root_identity = self._root_identity
            arm_attempt(self._context, identity, deadline=deadline, root_identity=self._root_identity)
            self._attempt._admitted.set()
            yield
            with self._attempt._gate:
                self._check_cancelled()
                _settle_attempt(self._context, identity, 'completed', deadline=deadline,
                                root_identity=self._root_identity)
                self._attempt._completed = True

    def _check_cancelled(self):
        if self._attempt._cancel_requested.is_set():
            raise DeadlineExceeded('installer attempt cancelled')

    def run(self, request):
        with self._attempt._gate:
            self._check_cancelled()
            if (tuple(request.argv[:5]) == ('python3', '-I', '-m',
                    'astrid.core.generation.engine_execution', '--engine-service')
                    and len(request.argv) > 5 and request.argv[5] == 'start'):
                self._attempt._started_service = True
        return self._context.run(request)

    def ensure_dir(self, path, *, deadline):
        self._check_cancelled()
        return self._context.ensure_dir(path, deadline=deadline)

    def write_json_atomic(self, path, value, *, deadline):
        self._check_cancelled()
        self._context.write_json_atomic(path, value, deadline=deadline)
        identity = self._attempt.identity
        if path == self._context.adapter.join(identity.binding.root, '.astrid-engine/install.json'):
            if (not isinstance(value, dict) or value.get('receipt_id') != identity.receipt_id
                    or any(value.get(k) != getattr(identity.binding, k) for k in
                           ('engine', 'platform', 'placement', 'root', 'recipe_sha256', 'models_lock_sha256'))):
                raise IntegrityError('committed installer receipt identity differs')
            recheck_install_under_custody(identity.binding, root_identity=self._root_identity)
            self._callback(InstallCheckpoint(identity, deepcopy(value)))

    def download_model(self, model, **kwargs):
        from .model_downloader import _cancellable_download
        self._check_cancelled()
        with _cancellable_download(self._attempt._cancel_requested):
            return self._context.download_model(model, **kwargs)


class AttemptInstaller:
    """Direct installer entry owned by a previously verified Runtime host.

    ``receipt_id`` is supplied before any work and must be unique for this
    install root. Checkpoints are delivered synchronously under the install lock.
    """
    def __init__(self, *, context, adapter, deadline, receipt_id, on_checkpoint):
        if not callable(on_checkpoint):
            raise TypeError('checkpoint callback is required')
        self.context, self.adapter, self.deadline = context, adapter, deadline
        self.receipt_id, self.on_checkpoint = receipt_id, on_checkpoint
        self._attempts = set()
        self._attempts_lock = threading.Lock()

    def begin_attempt(self, task_id, attempt_id, fence, binding):
        identity = AttemptIdentity(task_id, attempt_id, fence, self.receipt_id, binding)
        if self.context.root != binding.root:
            raise ValueError('installer context root differs from the binding')
        attempt = InstallAttempt(identity)
        attempt_context = self.context
        if (type(self.context) is LocalLinuxExecutionContext
                and type(self.context.service_client) is LinuxEngineServiceClient):
            client = _AttemptClient(self.context.service_client, attempt)
            attempt._client = client
            attempt_context = LocalLinuxExecutionContext(engine=self.context.engine,
                platform=self.context.platform, root=self.context.root,
                adapter=self.adapter, service_client=client)
        with self._attempts_lock:
            self._attempts.add(attempt)
        def work():
            try:
                recipe = load_recipe(binding.engine, binding.platform)
                plan = plan_install(recipe, root=binding.root, placement=binding.placement)
                context = _AttemptContext(attempt_context, attempt, self.on_checkpoint, self.deadline)
                attempt._result = install_engine(plan, context=context, adapter=self.adapter,
                                                 deadline=self.deadline, _receipt_id=identity.receipt_id)
            except BaseException as exc:
                attempt._error = exc
                if not attempt._admitted.is_set():
                    attempt._admission_error = exc
            finally:
                attempt._admitted.set()
                attempt._done.set()
        threading.Thread(target=work, name='engine-install-attempt', daemon=True).start()
        attempt._admitted.wait()
        if attempt._admission_error is not None:
            raise attempt._admission_error
        return attempt

    def cancel(self, attempt):
        with self._attempts_lock:
            if attempt not in self._attempts:
                raise ValueError('attempt is not owned by this installer')
        identity = attempt.identity
        with attempt._gate:
            if attempt._completed:
                return RecoveryHandle(identity, 'installer already completed')
            attempt._cancel_requested.set()
        client = attempt._client
        if client is None:
            return RecoveryHandle(identity, 'attempt does not have local supervisor custody')
        limit = time.monotonic() + 3
        while not attempt._done.is_set() and time.monotonic() < limit:
            client.interrupt()
            attempt._done.wait(min(0.05, max(0, limit - time.monotonic())))
        if not attempt._done.is_set():
            return RecoveryHandle(identity, 'installer work has not stopped')
        if attempt._started_service:
            return RecoveryHandle(identity, 'engine start requires exact service recovery')
        try:
            # The worker can no longer submit commands. The supervisor's work
            # lock makes this an acknowledgement after command-tree cleanup.
            barrier = self.context.service_client._request('attempt-barrier', {},
                        time.monotonic() + 3, start=False)
            if barrier not in (None, {'quiescent': True}):
                raise ValueError('invalid supervisor barrier')
            deadline = time.monotonic() + 3
            with self.context.acquire_install_lock(identity.binding.root, deadline=deadline):
                _settle_attempt(self.context, identity, 'stopped', deadline=deadline,
                                root_identity=attempt._root_identity)
        except Exception:
            return RecoveryHandle(identity, 'installer stop could not be proven or recorded')
        return ObservedStop(identity)
