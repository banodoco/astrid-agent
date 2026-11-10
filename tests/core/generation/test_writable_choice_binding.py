"""A writable node choice widget is published with the workflow's own socket spelling.

Live ComfyUI reports a choice widget as COMBO; VibeComfy templates retain the same socket as CHOICE
(or ENUM), and VibeComfy refuses a public input whose type differs from its target socket. The
binding therefore takes the node's retained choice spelling, and leaves every other type alone."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrid.core.generation.entries import _binding_type


def _node(types):
    return SimpleNamespace(native_input_names=["model", "device"], native_input_types=types)


@pytest.mark.parametrize(("native", "provider", "expected"), [
    ("CHOICE", "COMBO", "CHOICE"),   # live capture vs pinned qwen template (node:6.device)
    ("ENUM", "COMBO", "ENUM"),
    ("COMBO", "CHOICE", "COMBO"),    # a workflow that retains COMBO keeps it
    ("COMBO", "COMBO", "COMBO"),
    ("INT", "COMBO", "COMBO"),       # not the same channel: VibeComfy still refuses the mismatch
    ("CHOICE", "INT", "INT"),
])
def test_choice_spellings_follow_the_retained_socket(native, provider, expected):
    assert _binding_type(_node(["MODEL", native]), "device", provider) == expected


def test_a_field_without_a_retained_socket_type_keeps_the_provider_type():
    assert _binding_type(_node(["MODEL"]), "device", "COMBO") == "COMBO"
    assert _binding_type(SimpleNamespace(), "device", "COMBO") == "COMBO"
