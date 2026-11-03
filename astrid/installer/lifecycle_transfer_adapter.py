"""Offline, non-authoritative pieces of the proposed lifecycle wire v2.

There is no connected issuer, private object store, durable replay ledger or
native producer here. Decoding a frame or a record never authorizes an action.
"""

import base64
import binascii
import hashlib
import ntpath
import posixpath
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
    if type(expected_kind) is not str:
        raise WireError('invalid frame kind')
    if expected_kind == 'evidence':
        if type(evidence_kind) is not str or evidence_kind not in KINDS:
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


_ID = re.compile(r'[0-9a-f]{32}\Z', re.ASCII)


_PHASES = frozenset(('OBSERVED', 'PREPARED', 'HELD_PROVISIONAL', 'RESTART_FENCED',
    'HELD_ADMITTED', 'PREPARING_DATA', 'SELECTED', 'WRITER_CLAIMED', 'READY',
    'COMMIT_INTENT', 'COMMIT_DECIDED', 'COMMITTED', 'ROLLBACK_INTENT',
    'ROLLBACK_DECIDED', 'ROLLED_BACK_STOPPED', 'RECOVERY_REQUIRED'))
_ROLES = frozenset(('offline-backup', 'migration', 'candidate', 'rollback-r1'))


def _matches(value, pattern):
    return type(value) is str and bool(pattern.fullmatch(value))


def _id(value):
    if not _matches(value, _ID):
        raise WireError('invalid ID')


def _digest(value):
    if not _matches(value, _DIGEST):
        raise WireError('invalid digest')


def _text(value):
    if type(value) is not str or '\x00' in value:
        raise WireError('invalid Text')
    try:
        if len(value.encode('utf-8')) > 4096:
            raise WireError('Text too large')
    except UnicodeEncodeError as exc:
        raise WireError('invalid Text') from exc


def _bytes(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_-]*', value, re.ASCII):
        raise WireError('invalid Bytes')
    try:
        decoded = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except (ValueError, binascii.Error) as exc:
        raise WireError('invalid Bytes') from exc
    if base64.urlsafe_b64encode(decoded).decode('ascii').rstrip('=') != value:
        raise WireError('noncanonical Bytes')


def _file_id(value, platform=None):
    if type(value) is not dict:
        raise WireError('invalid FileID')
    kind = value.get('platform')
    if kind == 'posix':
        _closed(value, ('platform', 'device', 'inode'))
        _u64(value['device'])
        _u64(value['inode'])
    elif kind == 'windows':
        _closed(value, ('platform', 'volume', 'file_id'))
        _bytes(value['volume'])
        _bytes(value['file_id'])
    else:
        raise WireError('invalid FileID platform')
    if platform is not None and kind != platform:
        raise WireError('inconsistent FileID platform')


def _file_object(value, platform):
    _closed(value, ('path', 'id', 'kind', 'mode', 'uid', 'gid', 'acl_digest', 'content_digest'))
    _text(value['path'])
    if not (posixpath.isabs(value['path']) if platform == 'posix' else ntpath.isabs(value['path'])):
        raise WireError('path is not absolute')
    _file_id(value['id'], platform)
    if value['kind'] not in ('file', 'directory'):
        raise WireError('invalid file kind')
    _u64(value['mode'])
    for key in ('uid', 'gid'):
        if platform == 'posix':
            _u64(value[key])
        elif value[key] is not None:
            raise WireError('Windows uid/gid must be null')
    _digest(value['acl_digest'])
    if value['kind'] == 'directory':
        if value['content_digest'] is not None:
            raise WireError('directory content digest')
    else:
        _digest(value['content_digest'])


def _principal(value, platform):
    if platform == 'posix':
        _closed(value, ('platform', 'uid', 'gid', 'groups'))
        if value['platform'] != 'posix' or type(value['groups']) is not list:
            raise WireError('invalid principal')
        _u64(value['uid'])
        _u64(value['gid'])
        for group in value['groups']:
            _u64(group)
        encoded = [canonical_bytes(group) for group in value['groups']]
        if encoded != sorted(set(encoded)):
            raise WireError('unsorted or duplicate groups')
    else:
        _closed(value, ('platform', 'sid', 'token_digest'))
        if value['platform'] != 'windows':
            raise WireError('invalid principal')
        _text(value['sid'])
        _digest(value['token_digest'])


def _observation(value, check):
    if type(value) is not dict:
        raise WireError('invalid observation')
    if value.get('state') == 'known':
        _closed(value, ('state', 'value', 'evidence'))
        check(value['value'])
        _digest(value['evidence'])
    elif value.get('state') == 'unknown':
        _closed(value, ('state', 'reason', 'evidence'))
        if type(value['reason']) is not str or value['reason'] not in _REASONS:
            raise WireError('invalid reason')
        if value['evidence'] is not None:
            _digest(value['evidence'])
    else:
        raise WireError('invalid observation')


def _scope(value, process_platform):
    if type(value) is not dict:
        raise WireError('invalid scope')
    kind = value.get('kind')
    expected = {'linux': 'linux-cgroup', 'darwin': 'darwin-retained-scope', 'windows': 'windows-job'}
    if kind != expected[process_platform]:
        raise WireError('inconsistent scope platform')
    extra = {'linux': 'object', 'darwin': 'retention_digest', 'windows': 'job_creation_digest'}[process_platform]
    fields = ['kind', 'native_id', 'manager_epoch', 'launch_id', extra]
    if process_platform == 'windows':
        fields.append('retained_handle_slot')
    _closed(value, fields)
    for key in ('native_id', 'manager_epoch', 'launch_id'):
        _bytes(value[key])
    if process_platform == 'linux':
        _observation(value['object'], lambda obj: _file_object(obj, 'posix'))
    else:
        _digest(value[extra])
        if process_platform == 'windows':
            _id(value['retained_handle_slot'])


def _process(value):
    if type(value) is not dict:
        raise WireError('invalid process')
    kind = value.get('platform')
    common = ['platform', 'pid', 'principal', 'image', 'scope']
    if kind == 'linux':
        extra = ['start_ticks', 'boot_id', 'pidns_inode', 'userns_inode', 'argv_digest', 'environment_digest']
    elif kind == 'darwin':
        extra = ['birth_sec', 'birth_nsec', 'audit_token', 'argv_digest', 'environment_digest']
    elif kind == 'windows':
        extra = ['creation_filetime', 'command_line_digest']
    else:
        raise WireError('invalid process platform')
    _closed(value, common + extra)
    if _u64(value['pid']) == 0:
        raise WireError('invalid pid')
    platform = 'windows' if kind == 'windows' else 'posix'
    _principal(value['principal'], platform)
    _file_object(value['image'], platform)
    if kind == 'linux':
        for key in ('start_ticks', 'pidns_inode', 'userns_inode'):
            _u64(value[key])
        if type(value['boot_id']) is not str or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', value['boot_id']):
            raise WireError('invalid boot ID')
    elif kind == 'darwin':
        _u64(value['birth_sec'])
        if _u64(value['birth_nsec']) >= 1000000000:
            raise WireError('invalid birth nanoseconds')
        _bytes(value['audit_token'])
    else:
        _u64(value['creation_filetime'])
    for key in (('command_line_digest',) if kind == 'windows' else ('argv_digest', 'environment_digest')):
        _digest(value[key])
    presence = value['scope']
    if type(presence) is not dict:
        raise WireError('invalid scope presence')
    if presence.get('state') == 'absent':
        _closed(presence, ('state',))
    elif presence.get('state') == 'present':
        _closed(presence, ('state', 'value'))
        _scope(presence['value'], kind)
    else:
        raise WireError('invalid scope presence')


def validate_request(request):
    """Validate the closed seven-action Request shape, without authenticating it."""
    _closed(request, ('version', 'operation_id', 'transfer_id', 'generation', 'request_id',
                      'expected_phase', 'binding_digest', 'sequence', 'arguments'))
    if request['version'] != VERSION or not _matches(request['operation_id'], _TOKEN):
        raise WireError('invalid version or operation')
    for key in ('transfer_id', 'generation', 'request_id'):
        _id(request[key])
    if type(request['expected_phase']) is not str or request['expected_phase'] not in _PHASES:
        raise WireError('invalid phase')
    _digest(request['binding_digest'])
    _u64(request['sequence'])
    args = request['arguments']
    if type(args) is not dict or type(args.get('action')) is not str:
        raise WireError('invalid arguments')
    action = args['action']
    shapes = {'snapshot': ('binding',), 'install_suppression': ('expected', 'provisional'),
              'verify_suppression': ('receipt',), 'register_candidate': ('expected', 'armed_reference'),
              'restore_while_suppressed': ('expected', 'preimage'),
              'publish_ordinary_candidate': ('expected',),
              'remove_owned_suppression': ('expected', 'terminal_decision')}
    if action not in shapes:
        raise WireError('unsupported action')
    _closed(args, ('action',) + shapes[action])
    for key, kind in (('binding', 'Binding'), ('expected', 'ManagerEvidence'),
                      ('receipt', 'SuppressionReceipt'), ('preimage', 'PriorImage')):
        if key in args:
            validate_ref(args[key], kind)
    if action == 'install_suppression':
        session = args['provisional']
        _closed(session, ('transfer_id', 'generation', 'holder', 'endpoint_id', 'descriptor_generation', 'root', 'owner'))
        for key in ('transfer_id', 'generation', 'endpoint_id', 'descriptor_generation'):
            _id(session[key])
        if session['transfer_id'] != request['transfer_id'] or session['generation'] != request['generation']:
            raise WireError('session binding mismatch')
        _process(session['holder'])
        platform = 'windows' if session['holder']['platform'] == 'windows' else 'posix'
        _file_id(session['root'], platform)
        _file_id(session['owner'], platform)
    elif action == 'register_candidate':
        start = args['armed_reference']
        _closed(start, ('endpoint_id', 'ticket_slot', 'writer_id', 'role', 'source_digest', 'expected_definition_digest'))
        for key in ('endpoint_id', 'ticket_slot', 'writer_id'):
            _id(start[key])
        if type(start['role']) is not str or start['role'] not in _ROLES:
            raise WireError('invalid role')
        _digest(start['source_digest'])
        _digest(start['expected_definition_digest'])
    elif action == 'remove_owned_suppression':
        decision = args['terminal_decision']
        _closed(decision, ('outcome', 'journal_digest', 'ready_or_prior_digest', 'release_pending'))
        if decision['outcome'] not in ('complete', 'rolled_back') or decision['release_pending'] is not True:
            raise WireError('invalid terminal decision')
        _digest(decision['journal_digest'])
        _digest(decision['ready_or_prior_digest'])


def validate_query(query):
    """Check the closed Query shape; it is observational and never fresh proof."""
    _closed(query, ('version', 'operation_id', 'transfer_id', 'generation',
                    'binding_digest'))
    if query['version'] != VERSION:
        raise WireError('wrong version')
    if type(query['operation_id']) is not str or not _TOKEN.fullmatch(query['operation_id']):
        raise WireError('invalid operation ID')
    for key in ('transfer_id', 'generation'):
        if type(query[key]) is not str or not _ID.fullmatch(query[key]):
            raise WireError('invalid ID')
    if type(query['binding_digest']) is not str or not _DIGEST.fullmatch(query['binding_digest']):
        raise WireError('invalid binding digest')


def check_referenced_bytes(ref, expected_type, data):
    """Check exact bytes and type, without authenticating their source or schema."""
    validate_ref(ref, expected_type)
    if (type(data) is not bytes or len(data) != _u64(ref['byte_length'])
            or 'sha256:' + hashlib.sha256(data).hexdigest() != ref['digest']):
        raise WireError('reference bytes do not match')


def check_aggregate(refs):
    """Enforce the request budget on an already resolved complete reference graph.

    The caller must enumerate all objects and attachments, including replies and
    seals. This routine neither resolves references nor proves graph completeness.
    """
    total = 0
    for ref in refs:
        if type(ref) is not dict or type(ref.get('type')) is not str:
            raise WireError('invalid reference')
        validate_ref(ref, ref['type'])
        total += _u64(ref['byte_length'])
        if total > MAX_REQUEST_BYTES:
            raise WireError('request budget exceeded')
    return total


def validate_field_check(check, declared_fields):
    """Check one FieldCheck's closed shape and digest relation only.

    A caller must derive declared_fields independently and recompute the producer
    digest and collect native readback; this function performs neither task.
    """
    _closed(check, ('field', 'producer_digest', 'native_readback_digest', 'result'))
    if type(check['field']) is not str or check['field'] not in declared_fields:
        raise WireError('undeclared check field')
    producer = check['producer_digest']
    if type(producer) is not str or not _DIGEST.fullmatch(producer):
        raise WireError('invalid producer digest')
    obs = check['native_readback_digest']
    if type(obs) is not dict:
        raise WireError('invalid observation')
    state = obs.get('state')
    result = check['result']
    if state == 'known':
        _closed(obs, ('state', 'value', 'evidence'))
        for key in ('value', 'evidence'):
            if type(obs[key]) is not str or not _DIGEST.fullmatch(obs[key]):
                raise WireError('invalid observation digest')
        if result not in ('match', 'mismatch') or (result == 'match') != (obs['value'] == producer):
            raise WireError('inconsistent readback')
    elif state == 'unknown':
        _closed(obs, ('state', 'reason', 'evidence'))
        reasons = ('unreadable', 'visibility', 'raced', 'missing_provenance',
                   'unsupported_native', 'unauthenticated', 'incomplete',
                   'expired', 'mismatch', 'io', 'cancelled', 'not_collected')
        if obs['reason'] not in reasons:
            raise WireError('invalid observation reason')
        if obs['evidence'] is not None and (type(obs['evidence']) is not str or not _DIGEST.fullmatch(obs['evidence'])):
            raise WireError('invalid observation evidence')
        if result != ('unsupported' if obs['reason'] == 'unsupported_native' else 'unknown'):
            raise WireError('inconsistent unknown readback')
    else:
        raise WireError('invalid observation state')


_REASONS = frozenset(('unreadable', 'visibility', 'raced', 'missing_provenance',
                      'unsupported_native', 'unauthenticated', 'incomplete',
                      'expired', 'mismatch', 'io', 'cancelled', 'not_collected'))
_CODES = frozenset(('none', 'busy_owner', 'unknown_identity', 'binding_changed',
                    'incomplete_census', 'restart_unfenced', 'busy_work',
                    'unsupported_transfer', 'lease_uncertain', 'durability_failed',
                    'recovery_required'))


def validate_outcome_fields(*, result, code, faults, side_effects, recovery_refs,
                            success, declared_fields):
    """Check common outcome invariants, not the surrounding evidence schema.

    A structurally consistent outcome is not a verification or authority. The
    caller supplies the trusted schema's declared fault paths and success tag.
    """
    if success not in ('observed', 'verified'):
        raise WireError('invalid success tag')
    if type(result) is not str or result not in (success, 'unknown', 'refused', 'unsupported', 'recovery_required'):
        raise WireError('invalid result')
    if type(code) is not str or code not in _CODES or type(faults) is not list:
        raise WireError('invalid outcome')
    if type(recovery_refs) is not list or side_effects not in ('none', 'owned', 'uncertain'):
        raise WireError('invalid effects')
    for ref in recovery_refs:
        if type(ref) is not dict or type(ref.get('type')) is not str:
            raise WireError('invalid recovery reference')
        validate_ref(ref, ref['type'])
    for fault in faults:
        _closed(fault, ('field', 'reason', 'detail'))
        if type(fault['field']) is not str or fault['field'] not in declared_fields:
            raise WireError('undeclared fault field')
        if type(fault['reason']) is not str or fault['reason'] not in _REASONS:
            raise WireError('invalid fault reason')
        detail = fault['detail']
        if type(detail) is not str or '\x00' in detail:
            raise WireError('invalid fault detail')
        try:
            if len(detail.encode('utf-8')) > 4096:
                raise WireError('fault detail too large')
        except UnicodeEncodeError as exc:
            raise WireError('invalid fault detail') from exc
    if (result == success) != (code == 'none' and not faults):
        raise WireError('inconsistent result, code or faults')
    if result != success and (code == 'none' or not faults):
        raise WireError('failure requires code and faults')
    if result == 'unsupported' and (code != 'unsupported_transfer' or
                                    not any(f['reason'] == 'unsupported_native' for f in faults)):
        raise WireError('invalid unsupported result')
    if result == 'recovery_required' and not recovery_refs:
        raise WireError('recovery requires references')
    if side_effects in ('owned', 'uncertain') and not recovery_refs:
        raise WireError('effects require references')
    if side_effects == 'uncertain' and result != 'recovery_required':
        raise WireError('uncertain effects require recovery')
    if side_effects == 'owned' and result not in (success, 'recovery_required'):
        raise WireError('failure after effects requires recovery')



def validate_verification(record, *, declared_fields, required_fields):
    """Validate closed Verification and internal check consistency, without trust.

    Field sets must come from a trusted action and proof alternative. This does
    not derive them, recompute producer values, authenticate native readback,
    check the input Header, or establish live issuer/ledger provenance.
    """
    _closed(record, ('version', 'request_digest', 'challenge_id', 'evidence_digest',
                     'verifier', 'stage', 'sequence', 'result', 'code', 'checks',
                     'side_effects', 'recovery_refs', 'faults'))
    if record['version'] != VERSION:
        raise WireError('invalid version')
    _digest(record['request_digest'])
    _digest(record['evidence_digest'])
    _id(record['challenge_id'])
    _process(record['verifier'])
    if type(record['stage']) is not str or record['stage'] not in _PHASES:
        raise WireError('invalid phase')
    _u64(record['sequence'])
    checks = record['checks']
    if type(checks) is not list:
        raise WireError('invalid checks')
    for check in checks:
        validate_field_check(check, declared_fields)
    fields = [check['field'] for check in checks]
    if fields != sorted(set(fields)) or not set(required_fields).issubset(fields):
        raise WireError('missing, duplicate or unordered checks')
    validate_outcome_fields(result=record['result'], code=record['code'],
                            faults=record['faults'], side_effects=record['side_effects'],
                            recovery_refs=record['recovery_refs'], success='verified',
                            declared_fields={'/evidence_digest'} | {'/checks/%d' % i for i in range(len(checks))})
    if record['result'] == 'verified' and any(check['result'] != 'match' for check in checks):
        raise WireError('verified record has nonmatching checks')


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
