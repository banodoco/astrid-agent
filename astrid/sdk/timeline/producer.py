"""Settled producer carriers; only Runtime capture and acceptance grant authority."""
from __future__ import annotations

import copy

from ._command_state import CommandError, equal


def guard_pin(commands):
    guards = [i for i, call in enumerate(commands) if call.get('op') == 'producer.guard']
    if not guards:
        return None
    if guards != [0] or len(commands) < 2 or any(
            call.get('op') in ('timeline.inverse', 'timeline.restore') for call in commands[1:]):
        raise CommandError('producer guard must precede only proposal edits')
    from .commands import validate_command
    validate_command(commands[0])
    return copy.deepcopy(commands[0]['args']['pin'])


def producer_guard(ctx, command):
    pin = ctx.verified_producer if ctx.verified_producer is not None else command['args']['pin']
    if not equal(pin, command['args']['pin']):
        raise CommandError('verified producer pin changed', kind='producer_attempt_mismatch')
    ctx.dependency('producer_attempt:' + pin['task_id'], 'authority', pin)


def capture_producer(port, request):
    """Require the request-scoped Runtime capture, including on provider preview."""
    pin = guard_pin(request['commands'])
    if pin is None:
        return None
    target = pin['admitted_target']
    if any(target[key] != request[key] for key in target):
        raise CommandError('producer admission target changed', kind='producer_attempt_mismatch')
    capture = getattr(port, 'producer_attempt', None)
    if not callable(capture):
        raise CommandError('Runtime producer capture is required', kind='producer_dependency_unavailable')
    verified = capture(copy.deepcopy(pin))
    if not isinstance(verified, dict) or set(verified) != {'pin'} or not equal(verified['pin'], pin):
        raise CommandError('Runtime producer pin mismatch', kind='producer_attempt_mismatch')
    if any(type(verified['pin'][key]) is not int for key in ('fence', 'runtime_epoch')):
        raise CommandError('Runtime producer pin mismatch', kind='producer_attempt_mismatch')
    return copy.deepcopy(verified['pin'])


def build_producer_batch(proposal):
    """Forward the original proposal values; do not hash client-reserialized JSON."""
    from .commands import _VALIDATOR, validate_command

    errors = list(_VALIDATOR.evolve(schema={
        '$ref': '#/$defs/SettledProducerProposal'}).iter_errors(proposal))
    if errors:
        raise CommandError(errors[0].message)
    pin = {**proposal['producer'], 'admitted_target': proposal['admitted_target'],
           'proposal_digest': proposal['proposal_digest']}
    commands = [{'schema_version': 1, 'op': 'producer.guard', 'version': '1',
                 'select': None, 'args': {'pin': pin}}, *proposal['commands']]
    for command in commands:
        validate_command(command)
    guard_pin(commands)
    return copy.deepcopy({**proposal['admitted_target'], 'commands': commands,
                          'rationale': proposal['rationale']})
