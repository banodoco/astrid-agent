"""Non-authoritative canonical encoding for the R18 private wire contract.

The ManagerAdapter and its evidence producers are not implemented here.  This
module provides no request validation, authentication, native observation,
mutation, durable ledger, or admission authority.  A digest of these bytes is
not evidence that their contents are true.
"""

import json


MAX_OBJECT_BYTES = 1048576


class WireEncodingError(ValueError):
    """Bytes or values are outside the lifecycle wire canonical encoding."""


def _string(value):
    # Every scalar takes at least one byte in the canonical representation.
    if len(value) > MAX_OBJECT_BYTES:
        raise WireEncodingError("object too large")
    parts = ['"']
    for char in value:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise WireEncodingError("lone surrogate")
        if char == '"':
            parts.append('\\"')
        elif char == '\\':
            parts.append('\\\\')
        elif code < 0x20 or code >= 0x7F:
            if code <= 0xFFFF:
                parts.append(f'\\u{code:04x}')
            else:
                code -= 0x10000
                parts.append(f'\\u{0xD800 + (code >> 10):04x}'
                             f'\\u{0xDC00 + (code & 0x3FF):04x}')
        else:
            parts.append(char)
    parts.append('"')
    return ''.join(parts)


def canonical_bytes(value):
    """Encode JSON values as specified by wire v1; JSON numbers are forbidden.

    This checks encoding only, not any closed schema or semantic constraint.
    """
    parts = []
    size = 0

    def emit(piece):
        nonlocal size
        size += len(piece)  # Every emitted character is ASCII.
        if size > MAX_OBJECT_BYTES:
            raise WireEncodingError("object too large")
        parts.append(piece)

    def write(item):
        if item is None:
            emit('null')
        elif type(item) is bool:
            emit('true' if item else 'false')
        elif type(item) is str:
            emit(_string(item))
        elif type(item) is list:
            emit('[')
            for index, child in enumerate(item):
                if index:
                    emit(',')
                write(child)
            emit(']')
        elif type(item) is dict:
            if any(type(key) is not str for key in item):
                raise WireEncodingError("object keys must be strings")
            emit('{')
            for index, key in enumerate(sorted(item)):
                if index:
                    emit(',')
                emit(_string(key))
                emit(':')
                write(item[key])
            emit('}')
        else:
            raise WireEncodingError("unsupported JSON value")

    try:
        write(value)
    except RecursionError as exc:
        raise WireEncodingError("object nesting too deep or cyclic") from exc
    return ''.join(parts).encode('ascii')


def parse_canonical(data):
    """Parse only canonical bytes; never validate a schema or authenticate it."""
    if type(data) is not bytes or not data or len(data) > MAX_OBJECT_BYTES:
        raise WireEncodingError("invalid object bytes or size")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise WireEncodingError("duplicate object key")
            result[key] = value
        return result

    def reject_number(_value):
        raise WireEncodingError("JSON numbers are forbidden")

    try:
        value = json.loads(data.decode('ascii'), object_pairs_hook=pairs,
                           parse_int=reject_number, parse_float=reject_number,
                           parse_constant=reject_number)
        if canonical_bytes(value) != data:
            raise WireEncodingError("noncanonical object bytes")
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise WireEncodingError("invalid canonical JSON") from exc
    return value
