"""Effect projection must own its output while leaving input images untouched."""
import copy
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline._commit_patch import effect_diff


def closure(payload):
    return SimpleNamespace(images={'parent': {'payload': payload}})


def test_effect_diff_retains_mirror_inputs_and_owns_effects():
    old = {'config': {'clips': [{'id': 'a', 'params': {'x': [1]}}]},
           'clips': [{'id': 'a', 'params': {'x': [1]}}]}
    new = copy.deepcopy(old)
    new['config']['clips'][0]['params']['x'].append(2)
    new['clips'][0]['params']['x'].append(2)
    old_saved, new_saved = copy.deepcopy(old), copy.deepcopy(new)
    effects = effect_diff(closure(old), closure(new))
    assert effects == [{'op': 'set', 'ref': 'parent',
                        'path': ['config', 'clips', 'id:a', 'params', 'x'],
                        'before': {'state': 'present', 'value': [1]}, 'value': [1, 2]}]
    effects[0]['before']['value'].append(9)
    effects[0]['value'].append(9)
    assert (old, new) == (old_saved, new_saved)


def test_effect_diff_rejects_contradictory_bool_numeric_mirrors():
    with pytest.raises(CommandError) as error:
        effect_diff(closure({'registry': {'flag': True}, 'config': {'registry': {'flag': 1}}}),
                    closure({'config': {}}))
    assert error.value.kind == 'capture_mismatch'
