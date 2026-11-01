"""Attempt-bound entry into the existing engine installer.

This is an internal entry point; possession of task identifiers alone does not
constitute Runtime admission. The caller must verify the task envelope first.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import threading

from .engine_attempt_custody import AttemptIdentity, _complete_attempt, arm_attempt
from .engine_contracts import IntegrityError
from .engine_install_task import recheck_install_under_custody
from .engine_recipe import load_recipe
from .engine_runner import install_engine, plan_install


@dataclass(frozen=True)
class InstallCheckpoint:
    identity: AttemptIdentity
    receipt: dict


class InstallAttempt:
    def __init__(self, identity):
        self.identity = identity
        self._admitted = threading.Event()
        self._done = threading.Event()
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
            arm_attempt(self._context, identity, deadline=deadline, root_identity=self._root_identity)
            self._attempt._admitted.set()
            yield
            _complete_attempt(self._context, identity, deadline=deadline,
                              root_identity=self._root_identity)

    def write_json_atomic(self, path, value, *, deadline):
        self._context.write_json_atomic(path, value, deadline=deadline)
        identity = self._attempt.identity
        if path == self._context.adapter.join(identity.binding.root, '.astrid-engine/install.json'):
            if (not isinstance(value, dict) or value.get('receipt_id') != identity.receipt_id
                    or any(value.get(k) != getattr(identity.binding, k) for k in
                           ('engine', 'platform', 'placement', 'root', 'recipe_sha256', 'models_lock_sha256'))):
                raise IntegrityError('committed installer receipt identity differs')
            recheck_install_under_custody(identity.binding, root_identity=self._root_identity)
            self._callback(InstallCheckpoint(identity, deepcopy(value)))


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

    def begin_attempt(self, task_id, attempt_id, fence, binding):
        identity = AttemptIdentity(task_id, attempt_id, fence, self.receipt_id, binding)
        if self.context.root != binding.root:
            raise ValueError('installer context root differs from the binding')
        attempt = InstallAttempt(identity)
        def work():
            try:
                recipe = load_recipe(binding.engine, binding.platform)
                plan = plan_install(recipe, root=binding.root, placement=binding.placement)
                context = _AttemptContext(self.context, attempt, self.on_checkpoint, self.deadline)
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
