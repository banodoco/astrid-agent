"""Encoding is not schema validation, native evidence, or transfer authority."""

import pytest

from astrid.installer.lifecycle_transfer_adapter import (
    MAX_OBJECT_BYTES, WireEncodingError, canonical_bytes, parse_canonical,
)


def test_canonical_scalars_order_and_wire_escaping():
    value = {'z': [True, False, None], 'n': '0', 'a': 'é\n\x7f😀/"\\'}
    encoded = (b'{"a":"\\u00e9\\u000a\\u007f\\ud83d\\ude00/\\"\\\\",'
               b'"n":"0","z":[true,false,null]}')
    assert canonical_bytes(value) == encoded
    assert parse_canonical(encoded) == value


@pytest.mark.parametrize('data', [
    b'{"a":"x","a":"x"}', b'{"b":true,"a":false}', b'{"a": true}',
    b'{"a":"\\n"}', b'{"a":"\\u00E9"}', b'{"a":"\\/"}',
    b'{"a":1}', b'{"a":1.0}', b'{"a":NaN}', b'null\n', b'\xef\xbb\xbfnull',
    b'"\\ud800"', b'"\\udc00"', b'"\xc3\xa9"', b'',
])
def test_noncanonical_or_invalid_bytes_are_refused(data):
    with pytest.raises(WireEncodingError):
        parse_canonical(data)


@pytest.mark.parametrize('value', [1, 1.0, {1: 'value'}, ('tuple',), '\ud800'])
def test_non_json_or_non_scalar_values_are_refused(value):
    with pytest.raises(WireEncodingError):
        canonical_bytes(value)


def test_size_limit_is_exact():
    encoded = b'"' + b'a' * (MAX_OBJECT_BYTES - 2) + b'"'
    assert parse_canonical(encoded) == 'a' * (MAX_OBJECT_BYTES - 2)
    with pytest.raises(WireEncodingError):
        canonical_bytes('a' * (MAX_OBJECT_BYTES - 1))
    with pytest.raises(WireEncodingError):
        parse_canonical(encoded + b' ')


def test_cycles_refuse():
    value = []
    value.append(value)
    with pytest.raises(WireEncodingError):
        canonical_bytes(value)
