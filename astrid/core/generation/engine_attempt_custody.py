"""Durable, installation-local admission for managed installer attempts.

The caller must hold the existing install lock. An armed attempt is never
replayed simply because its process disappeared; only a future owner that can
establish the disposition of the exact attempt may retire it.
"""
from __future__ import annotations

from dataclasses import dataclass

from .engine_contracts import CustodyUnresolved, IntegrityError
from .engine_install_task import InstallBinding, recheck_install_under_custody

_SCHEMA = 'astrid.engine-attempt-custody.v1'
_FILE = '.astrid-engine/attempt-custody.json'


@dataclass(frozen=True)
class AttemptIdentity:
    task_id: str
    attempt_id: str
    fence: int
    receipt_id: str
    binding: InstallBinding

    def __post_init__(self):
        if any(type(value) is not str or not value or len(value) > 256 or '\0' in value
               for value in (self.task_id, self.attempt_id, self.receipt_id)):
            raise ValueError('invalid installer attempt identity')
        if type(self.fence) is not int or self.fence < 0:
            raise ValueError('invalid installer attempt fence')
        if not isinstance(self.binding, InstallBinding):
            raise TypeError('an exact install binding is required')

    def as_dict(self):
        return {'task_id': self.task_id, 'attempt_id': self.attempt_id,
                'fence': self.fence, 'receipt_id': self.receipt_id,
                'binding': self.binding.as_dict()}


def _read(context, path, deadline):
    fact = context.file_fact(path, hash_bytes=False, deadline=deadline)
    if not fact.exists:
        return {'schema': _SCHEMA, 'attempts': []}
    if fact.symlink or not fact.regular or fact.bytes is None or fact.bytes > 1024 * 1024:
        raise IntegrityError('invalid installer attempt custody record')
    value = context.read_json(path, deadline=deadline)
    if (not isinstance(value, dict) or set(value) != {'schema', 'attempts'}
            or value['schema'] != _SCHEMA or not isinstance(value['attempts'], list)):
        raise IntegrityError('invalid installer attempt custody record')
    seen_attempts, seen_receipts = set(), set()
    for entry in value['attempts']:
        try:
            if (not isinstance(entry, dict) or set(entry) != {'identity', 'state'}
                    or entry['state'] not in {'armed', 'completed', 'stopped'}):
                raise ValueError
            item = entry['identity']
            if not isinstance(item, dict) or set(item) != {
                'task_id', 'attempt_id', 'fence', 'receipt_id', 'binding'
            }:
                raise ValueError
            identity = AttemptIdentity(item['task_id'], item['attempt_id'], item['fence'],
                                       item['receipt_id'], InstallBinding.from_dict(item['binding']))
            if identity.attempt_id in seen_attempts or identity.receipt_id in seen_receipts:
                raise ValueError
            seen_attempts.add(identity.attempt_id)
            seen_receipts.add(identity.receipt_id)
        except (TypeError, ValueError, KeyError):
            raise IntegrityError('invalid installer attempt custody record') from None
    return value


def arm_attempt(context, identity: AttemptIdentity, *, deadline, root_identity):
    """Durably arm a unique attempt under the already-held installer lock.

    The returned record is evidence of admission, not evidence of termination.
    Do not run installer work if this operation fails or its outcome is unknown.
    """
    if not isinstance(identity, AttemptIdentity):
        raise TypeError('an installer attempt identity is required')
    if (not isinstance(root_identity, tuple) or len(root_identity) != 2
            or any(type(value) is not int or value < 0 for value in root_identity)):
        raise ValueError('a pinned install root identity is required')
    binding = identity.binding
    recheck_install_under_custody(binding, root_identity=root_identity)
    if context.root != binding.root:
        raise ValueError('installer context root differs from the binding')
    path = context.adapter.join(binding.root, _FILE)
    record = _read(context, path, deadline)
    if any(entry['state'] == 'armed' for entry in record['attempts']):
        raise CustodyUnresolved('prior installer attempt requires explicit recovery')
    if any(entry['identity']['attempt_id'] == identity.attempt_id
           or entry['identity']['receipt_id'] == identity.receipt_id for entry in record['attempts']):
        raise CustodyUnresolved('installer attempt or receipt was already used')
    record['attempts'].append({'identity': identity.as_dict(), 'state': 'armed'})
    context.write_json_atomic(path, record, deadline=deadline)
    return identity


def _complete_attempt(context, identity, *, deadline, root_identity):
    """Commit normal installer completion, while retaining replay history."""
    binding = identity.binding
    recheck_install_under_custody(binding, root_identity=root_identity)
    path = context.adapter.join(binding.root, _FILE)
    record = _read(context, path, deadline)
    matches = [entry for entry in record['attempts'] if entry['identity'] == identity.as_dict()]
    if len(matches) != 1 or matches[0]['state'] != 'armed':
        raise CustodyUnresolved('installer attempt custody changed during completion')
    matches[0]['state'] = 'completed'
    context.write_json_atomic(path, record, deadline=deadline)
