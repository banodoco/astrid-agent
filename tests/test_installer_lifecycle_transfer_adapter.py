"""Offline wire checks are not native or Runtime transfer evidence."""

import hashlib
import os
import subprocess

import pytest

from astrid.installer import lifecycle_transfer_adapter as wire


DIGEST = 'sha256:' + 'a' * 64
OTHER = 'sha256:' + 'b' * 64
REF = {'type': 'Request', 'digest': DIGEST, 'byte_length': '1'}


def test_canonical_bytes_and_unicode():
    value = {'z': [True, None], 'a': 'é\n\x7f😀/"\\'}
    data = b'{"a":"\\u00e9\\u000a\\u007f\\ud83d\\ude00/\\"\\\\","z":[true,null]}'
    assert wire.canonical_bytes(value) == data
    assert wire.parse_canonical(data) == value


@pytest.mark.parametrize('data', [
    b'{"a":"x","a":"x"}', b'{"b":true,"a":false}', b'{"a": true}',
    b'"\\n"', b'"\\u00E9"', b'"\\/"', b'1', b'1.0', b'NaN',
    b'null\n', b'\xef\xbb\xbfnull', b'"\\ud800"', b'"\xc3\xa9"', b'',
])
def test_noncanonical_input_refused(data):
    with pytest.raises(wire.WireError):
        wire.parse_canonical(data)


@pytest.mark.parametrize('value', [1, 1.0, {1: 'x'}, ('x',), '\ud800'])
def test_unsupported_values_refused(value):
    with pytest.raises(wire.WireError):
        wire.canonical_bytes(value)


def test_object_size_and_cycles():
    data = b'"' + b'a' * (wire.MAX_OBJECT_BYTES - 2) + b'"'
    assert wire.canonical_bytes(wire.parse_canonical(data)) == data
    with pytest.raises(wire.WireError):
        wire.parse_canonical(data + b' ')
    value = []
    value.append(value)
    with pytest.raises(wire.WireError):
        wire.canonical_bytes(value)


@pytest.mark.parametrize('kind,body_type', list(wire.FRAME_TYPES.items()) + [
    ('evidence', kind) for kind in sorted(wire.KINDS)
])
def test_each_frame_mapping(kind, body_type):
    ref = dict(REF, type=body_type)
    kwargs = {'evidence_kind': body_type} if kind == 'evidence' else {}
    data = wire.encode_frame(kind, ref, **kwargs)
    assert wire.decode_frame(data, expected_kind=kind, **kwargs)['body'] == ref
    with pytest.raises(wire.WireError):
        wire.decode_frame(data, expected_kind='unarmed')


def test_frame_and_ref_are_closed_and_bounded():
    msg = wire.encode_frame('request', REF)
    for invalid in (msg + b'x', b'\x00\x00\x00\x00', b'\x00\x00\x40\x01' + b'x' * 16385,
                    msg[:3], (len(msg) + 1).to_bytes(4, 'big') + msg[4:]):
        with pytest.raises(wire.WireError):
            wire.decode_frame(invalid, expected_kind='request')
    for ref in (dict(REF, extra='x'), dict(REF, type='Query'), dict(REF, byte_length='01'),
                dict(REF, byte_length='1048577'), dict(REF, byte_length='18446744073709551616'),
                dict(REF, digest='sha256:' + 'A' * 64)):
        with pytest.raises(wire.WireError):
            wire.encode_frame('request', ref)
    with pytest.raises(wire.WireError):
        wire.encode_frame('evidence', dict(REF, type='ManagerEvidence'))
    frame = {'version': wire.VERSION, 'kind': 'request', 'body': REF, 'extra': None}
    data = wire.canonical_bytes(frame)
    with pytest.raises(wire.WireError):
        wire.decode_frame(len(data).to_bytes(4, 'big') + data, expected_kind='request')


def test_reference_integrity_and_aggregate_budget():
    data = b'abc'
    ref = {'type': 'Query', 'byte_length': '3', 'digest': 'sha256:' + hashlib.sha256(data).hexdigest()}
    wire.check_referenced_bytes(ref, 'Query', data)
    with pytest.raises(wire.WireError):
        wire.check_referenced_bytes(ref, 'Query', b'abd')
    full = dict(ref, byte_length=str(wire.MAX_OBJECT_BYTES))
    assert wire.check_aggregate([full] * 16) == wire.MAX_REQUEST_BYTES
    with pytest.raises(wire.WireError):
        wire.check_aggregate([full] * 17)


def test_query_closed_and_observational():
    query = {'version': wire.VERSION, 'operation_id': 'op', 'transfer_id': 'a' * 32,
             'generation': 'b' * 32, 'binding_digest': DIGEST}
    wire.validate_query(query)
    for bad in (dict(query, extra='x'), dict(query, generation='A' * 32),
                dict(query, version='astrid.lifecycle-transfer/v1'), dict(query, operation_id='')):
        with pytest.raises(wire.WireError):
            wire.validate_query(bad)


@pytest.mark.parametrize('result,state,value,reason,valid', [
    ('match', 'known', DIGEST, None, True), ('match', 'known', OTHER, None, False),
    ('mismatch', 'known', OTHER, None, True), ('mismatch', 'known', DIGEST, None, False),
    ('unknown', 'unknown', None, 'visibility', True), ('unsupported', 'unknown', None, 'unsupported_native', True),
    ('unknown', 'unknown', None, 'unsupported_native', False), ('match', 'unknown', None, 'visibility', False),
])
def test_field_check_consistency(result, state, value, reason, valid):
    obs = ({'state': 'known', 'value': value, 'evidence': DIGEST} if state == 'known'
           else {'state': 'unknown', 'reason': reason, 'evidence': None})
    check = {'field': '/field', 'producer_digest': DIGEST, 'native_readback_digest': obs, 'result': result}
    if valid:
        wire.validate_field_check(check, {'/field'})
    else:
        with pytest.raises(wire.WireError):
            wire.validate_field_check(check, {'/field'})
    with pytest.raises(wire.WireError):
        wire.validate_field_check(check, set())


def test_outcome_result_code_faults_and_effects():
    fault = {'field': '/evidence_digest', 'reason': 'unsupported_native', 'detail': 'unavailable'}
    ref = dict(REF)
    def check(**overrides):
        values = dict(result='verified', code='none', faults=[], side_effects='none',
                      recovery_refs=[], success='verified', declared_fields={'/evidence_digest'})
        values.update(overrides)
        wire.validate_outcome_fields(**values)
    check()
    check(result='unsupported', code='unsupported_transfer', faults=[fault])
    check(result='recovery_required', code='recovery_required', faults=[fault],
          side_effects='uncertain', recovery_refs=[ref])
    for invalid in (
        dict(code='busy_owner'), dict(faults=[fault]),
        dict(result='unknown', code='busy_owner'),
        dict(result='unsupported', code='busy_owner', faults=[fault]),
        dict(result='recovery_required', code='recovery_required', faults=[fault]),
        dict(result='refused', code='busy_owner', faults=[fault], side_effects='owned', recovery_refs=[ref]),
        dict(side_effects='uncertain', recovery_refs=[ref]),
        dict(result='refused', code='busy_owner', faults=[dict(fault, field='/invented')]),
    ):
        with pytest.raises(wire.WireError):
            check(**invalid)


@pytest.mark.parametrize('action,args', [
    ('snapshot', (object(),)), ('install_suppression', (object(), object())),
    ('verify_suppression', (object(),)), ('register_candidate', (object(), object())),
    ('restore_while_suppressed', (object(), object())),
    ('publish_ordinary_candidate', (object(),)),
    ('remove_owned_suppression', (object(), object())),
])
def test_all_calls_and_retries_refuse_before_native_effects(action, args, monkeypatch, tmp_path):
    sentinel = tmp_path / "native-state"
    sentinel.write_bytes(b"unchanged")
    def forbidden(*args, **kwargs):
        pytest.fail('native action attempted')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(os, 'system', forbidden)
    monkeypatch.setattr(os, 'open', forbidden)
    monkeypatch.setattr(os, 'unlink', forbidden)
    monkeypatch.setattr(os, 'replace', forbidden)
    adapter = wire.LifecycleTransferAdapter()
    for _ in range(2):
        with pytest.raises(wire.ProducerUnavailable) as error:
            getattr(adapter, action)(*args, request=object(), challenge=object())
        assert error.value.action == action
        assert error.value.expected_evidence_kind == wire.ACTION_KINDS[action]
    monkeypatch.undo()
    assert sentinel.read_bytes() == b"unchanged"


def _request_arguments():
    file_id = {'platform': 'posix', 'device': '1', 'inode': '2'}
    image = {'path': '/bin/agent', 'id': file_id, 'kind': 'file', 'mode': '493',
             'uid': '1000', 'gid': '1000', 'acl_digest': DIGEST, 'content_digest': DIGEST}
    process = {'platform': 'linux', 'pid': '1', 'start_ticks': '2',
               'boot_id': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'pidns_inode': '3',
               'userns_inode': '4', 'principal': {'platform': 'posix', 'uid': '1000',
               'gid': '1000', 'groups': ['1', '2']}, 'image': image, 'argv_digest': DIGEST,
               'environment_digest': DIGEST, 'scope': {'state': 'absent'}}
    expected = dict(REF, type='ManagerEvidence')
    return {
        'snapshot': {'binding': dict(REF, type='Binding')},
        'install_suppression': {'expected': expected, 'provisional': {
            'transfer_id': 'a' * 32, 'generation': 'b' * 32, 'holder': process,
            'endpoint_id': 'c' * 32, 'descriptor_generation': 'd' * 32,
            'root': file_id, 'owner': file_id}},
        'verify_suppression': {'receipt': dict(REF, type='SuppressionReceipt')},
        'register_candidate': {'expected': expected, 'armed_reference': {
            'endpoint_id': 'c' * 32, 'ticket_slot': 'd' * 32, 'writer_id': 'e' * 32,
            'role': 'candidate', 'source_digest': DIGEST, 'expected_definition_digest': DIGEST}},
        'restore_while_suppressed': {'expected': expected, 'preimage': dict(REF, type='PriorImage')},
        'publish_ordinary_candidate': {'expected': expected},
        'remove_owned_suppression': {'expected': expected, 'terminal_decision': {
            'outcome': 'complete', 'journal_digest': DIGEST, 'ready_or_prior_digest': DIGEST,
            'release_pending': True}},
    }


@pytest.mark.parametrize('action', sorted(wire.ACTION_KINDS))
def test_request_mapping_and_closed_arguments(action):
    request = {'version': wire.VERSION, 'operation_id': 'op', 'transfer_id': 'a' * 32,
               'generation': 'b' * 32, 'request_id': 'c' * 32, 'expected_phase': 'OBSERVED',
               'binding_digest': DIGEST, 'sequence': '0',
               'arguments': {'action': action, **_request_arguments()[action]}}
    wire.validate_request(request)
    for bad in (dict(request, unexpected=True), dict(request, sequence='-1'),
                dict(request, expected_phase='UNRECOGNIZED'),
                dict(request, arguments=dict(request['arguments'], unexpected=True))):
        with pytest.raises(wire.WireError):
            wire.validate_request(bad)


def test_session_and_terminal_decision_are_bound():
    arguments = _request_arguments()
    request = {'version': wire.VERSION, 'operation_id': 'op', 'transfer_id': 'a' * 32,
               'generation': 'b' * 32, 'request_id': 'c' * 32, 'expected_phase': 'OBSERVED',
               'binding_digest': DIGEST, 'sequence': '0', 'arguments': {
                   'action': 'install_suppression', **arguments['install_suppression']}}
    request['arguments']['provisional']['holder']['principal']['groups'] = ['2', '1']
    with pytest.raises(wire.WireError):
        wire.validate_request(request)
    request['arguments'] = {'action': 'remove_owned_suppression', **arguments['remove_owned_suppression']}
    request['arguments']['terminal_decision']['release_pending'] = False
    with pytest.raises(wire.WireError):
        wire.validate_request(request)


def test_verification_requires_closed_unique_complete_matching_checks():
    verifier = _request_arguments()['install_suppression']['provisional']['holder']
    field = {'field': '/kind', 'producer_digest': DIGEST,
             'native_readback_digest': {'state': 'known', 'value': DIGEST, 'evidence': DIGEST},
             'result': 'match'}
    record = {'version': wire.VERSION, 'request_digest': DIGEST, 'challenge_id': 'a' * 32,
              'evidence_digest': DIGEST, 'verifier': verifier, 'stage': 'OBSERVED',
              'sequence': '0', 'result': 'verified', 'code': 'none', 'checks': [field],
              'side_effects': 'none', 'recovery_refs': [], 'faults': []}
    kwargs = {'declared_fields': {'/kind'}, 'required_fields': {'/kind'}}
    assert wire.validate_verification(record, **kwargs) is None
    for bad in (dict(record, checks=[]), dict(record, checks=[field, field]),
                dict(record, unknown='x'), dict(record, checks=[dict(field, result='mismatch')])):
        with pytest.raises(wire.WireError):
            wire.validate_verification(bad, **kwargs)
