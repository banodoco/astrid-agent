"""Offline, non-authoritative pieces of the proposed lifecycle wire v2.

There is no connected issuer, private object store, durable replay ledger or
native producer here. Decoding a frame or a record never authorizes an action.
"""

import json
import re

VERSION = 'astrid.lifecycle-transfer/v2'
MAX_OBJECT_BYTES = 1048576
MAX_FRAME_BYTES = 16384
MAX_REQUEST_BYTES = 16777216
KINDS = frozenset(('ManagerEvidence', 'SuppressionReceipt', 'NativeCloseProof',
                   'ReadyProof', 'DefinitionProof', 'PriorStateProof',
                   'ReleaseProof', 'DetachReceipt'))
ACTION_KINDS = {
    'snapshot': 'ManagerEvidence',
    'install_suppression': 'SuppressionReceipt',
    'verify_suppression': 'ManagerEvidence',
    'register_candidate': 'ManagerEvidence',
    'restore_while_suppressed': 'PriorStateProof',
    'publish_ordinary_candidate': 'DefinitionProof',
    'remove_owned_suppression': 'ReleaseProof',
}
FRAME_TYPES = {'request': 'Request', 'challenge': 'Challenge',
               'fact-reply': 'FactReply', 'seal': 'Seal',
               'verification': 'Verification', 'query': 'Query',
               'status': 'QueryStatus'}
_TOKEN = re.compile(r'[-A-Za-z0-9_.:/@+]{1,256}\Z', re.ASCII)
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z', re.ASCII)
_U64 = re.compile(r'(?:0|[1-9][0-9]*)\Z', re.ASCII)


class WireError(ValueError):
    """Malformed, noncanonical or unsupported wire input."""


class ProducerUnavailable(RuntimeError):
    """No authentic issuer and producers exist; no wire evidence was made."""

    def __init__(self, action):
        self.action = action
        self.expected_evidence_kind = ACTION_KINDS[action]
        super().__init__('lifecycle transfer producer unavailable: ' + action)


def canonical_bytes(value):
    """Encode only JSON wire values; this does not validate any schema."""
    pieces = []
    size = 0

    def emit(s):
        nonlocal size
        size += len(s)
        if size > MAX_OBJECT_BYTES:
            raise WireError('object too large')
        pieces.append(s)

    def string(s):
        emit('"')
        for ch in s:
            n = ord(ch)
            if 0xD800 <= n <= 0xDFFF:
                raise WireError('lone surrogate')
            if ch in ('"', '\\'):
                emit('\\' + ch)
            elif n < 32 or n >= 127:
                if n <= 0xFFFF:
                    emit('\\u%04x' % n)
                else:
                    n -= 0x10000
                    emit('\\u%04x\\u%04x' % (0xD800 + (n >> 10), 0xDC00 + (n & 1023)))
            else:
                emit(ch)
        emit('"')

    def write(item):
        if item is None:
            emit('null')
        elif type(item) is bool:
            emit('true' if item else 'false')
        elif type(item) is str:
            string(item)
        elif type(item) is list:
            emit('[')
            for i, child in enumerate(item):
                if i:
                    emit(',')
                write(child)
            emit(']')
        elif type(item) is dict:
            if any(type(k) is not str for k in item):
                raise WireError('non-string key')
            emit('{')
            for i, key in enumerate(sorted(item)):
                if i:
                    emit(',')
                string(key)
                emit(':')
                write(item[key])
            emit('}')
        else:
            raise WireError('unsupported JSON value')
    try:
        write(value)
    except RecursionError as exc:
        raise WireError('nesting too deep or cyclic') from exc
    return ''.join(pieces).encode('ascii')


def parse_canonical(data):
    if type(data) is not bytes or not 0 < len(data) <= MAX_OBJECT_BYTES:
        raise WireError('invalid object size')

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise WireError('duplicate key')
            result[key] = value
        return result

    def no_number(_):
        raise WireError('JSON numbers are forbidden')

    try:
        result = json.loads(data.decode('ascii'), object_pairs_hook=pairs,
                            parse_int=no_number, parse_float=no_number,
                            parse_constant=no_number)
        if canonical_bytes(result) != data:
            raise WireError('noncanonical bytes')
        return result
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise WireError('invalid canonical JSON') from exc


def _closed(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise WireError('missing or unknown fields')


def _u64(value):
    if type(value) is not str or not _U64.fullmatch(value) or len(value) > 20 or int(value) > 2**64 - 1:
        raise WireError('invalid U64')
    return int(value)


def validate_ref(ref, expected_type):
    """Validate a bounded reference against a caller-selected exact schema."""
    _closed(ref, ('type', 'digest', 'byte_length'))
    if (type(expected_type) is not str or not _TOKEN.fullmatch(expected_type)
            or ref['type'] != expected_type):
        raise WireError('wrong reference type')
    if type(ref['digest']) is not str or not _DIGEST.fullmatch(ref['digest']):
        raise WireError('invalid digest')
    if _u64(ref['byte_length']) > MAX_OBJECT_BYTES:
        raise WireError('reference exceeds object bound')


def decode_frame(message, *, expected_kind, evidence_kind=None):
    """Decode one armed frame. The caller must authenticate the live channel."""
    if type(message) is not bytes or len(message) < 5:
        raise WireError('short frame')
    length = int.from_bytes(message[:4], 'big')
    if not 1 <= length <= MAX_FRAME_BYTES or len(message) != length + 4:
        raise WireError('invalid frame length')
    frame = parse_canonical(message[4:])
    _closed(frame, ('version', 'kind', 'body'))
    if frame['version'] != VERSION or frame['kind'] != expected_kind:
        raise WireError('unexpected version or unarmed frame')
    if expected_kind == 'evidence':
        if evidence_kind not in KINDS:
            raise WireError('evidence kind not armed')
        expected_type = evidence_kind
    else:
        expected_type = FRAME_TYPES.get(expected_kind)
        if expected_type is None:
            raise WireError('unsupported frame kind')
    validate_ref(frame['body'], expected_type)
    return frame


def encode_frame(kind, body, *, evidence_kind=None):
    frame = {'version': VERSION, 'kind': kind, 'body': body}
    encoded = canonical_bytes(frame)
    message = len(encoded).to_bytes(4, 'big') + encoded
    decode_frame(message, expected_kind=kind, evidence_kind=evidence_kind)
    return message


class LifecycleTransferAdapter:
    """Seven non-authoritative entry points; all refuse before native effects."""

    def snapshot(self, binding, *, request, challenge):
        raise ProducerUnavailable('snapshot')

    def install_suppression(self, expected, provisional, *, request, challenge):
        raise ProducerUnavailable('install_suppression')

    def verify_suppression(self, receipt, *, request, challenge):
        raise ProducerUnavailable('verify_suppression')

    def register_candidate(self, expected, armed_reference, *, request, challenge):
        raise ProducerUnavailable('register_candidate')

    def restore_while_suppressed(self, expected, preimage, *, request, challenge):
        raise ProducerUnavailable('restore_while_suppressed')

    def publish_ordinary_candidate(self, expected, *, request, challenge):
        raise ProducerUnavailable('publish_ordinary_candidate')

    def remove_owned_suppression(self, expected, terminal_decision, *, request, challenge):
        raise ProducerUnavailable('remove_owned_suppression')
