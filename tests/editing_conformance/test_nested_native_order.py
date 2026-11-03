"""Native inverse order remains bound to the accepted source, even for commuting writes."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline._step_undo import validate_nested_history
from tests.editing_conformance.test_captured_config_root_mirror import captured as root_capture  # noqa: F401
from tests.editing_conformance.test_nested_config_root_history import captured as nested_capture  # noqa: F401


@pytest.fixture(params=['root_capture', 'nested_capture'])
def history(request):
    return request.getfixturevalue(request.param)['history']['items']


def test_exact_accepted_native_order_is_valid_without_mutation(history):
    original = copy.deepcopy(history)
    entries = {entry['publication_id']: entry for entry in history}
    for entry in history:
        if entry.get('records'):
            validate_nested_history(entry, entries.__getitem__)
    assert history == original


@pytest.mark.parametrize('side', ['inverse', 'source'])
def test_commuting_native_order_must_match_accepted_source(history, side):
    entries = {entry['publication_id']: entry for entry in history}
    inverse = next(entry for entry in history if entry.get('records') and any(
        binding['key'] == 'inverse.stage' for record in entry['records']['commands']
        for binding in record['bindings']))
    record = inverse['records']['commands'][0]
    boundary = next(binding['boundary'] for binding in record['bindings']
                    if binding['key'] == 'inverse.stage')
    source = entries[boundary['publication_id']]['records']['commands'][0]
    writes = (record if side == 'inverse' else source)['writes']
    config = next(index for index, write in enumerate(writes)
                  if write['ref'] == 'parent' and write['path'] == ['config'])
    registry = next(index for index, write in enumerate(writes)
                    if write['ref'] == 'parent' and write['path'] == ['registry'])
    # Independent storage sites commute, so final images cannot prove their order.
    writes[config], writes[registry] = writes[registry], writes[config]
    original = copy.deepcopy(history)
    with pytest.raises(CommandError) as error:
        validate_nested_history(inverse, entries.__getitem__)
    assert error.value.kind == 'history_unavailable'
    assert history == original
