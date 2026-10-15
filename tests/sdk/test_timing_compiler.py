"""Compiler boundary checks using existing, disposable characterization inputs."""
from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline.compiler import TimingInputs, compile_timing
from astrid.sdk.timeline.model import retime_scope


TINY = Path(__file__).resolve().parents[1] / "fixtures/timeline_editing/tiny.json"


def tiny():
    return json.loads(TINY.read_text())


@pytest.mark.parametrize("method,args,kwargs", [
    ("resolve", (), {}),
    ("retime", (), {}),
    ("reflow", (), {"gaps": {"a-vo": 0.5}, "points": {"a-vo": 1.7}}),
    ("_shift_after", (1.0, 0.5), {"cut_window": None}),
])
def test_compiler_does_not_mutate_declared_bundle_state_or_commands(method, args, kwargs):
    bundle = tiny()
    bundle["app"] = {"foreign": {"list": [1, True, "opaque"]}}
    state = {"notes": ["existing"], "report": [], "_music": "keep_end"}
    commands = [(method, args, kwargs)]
    before = copy.deepcopy((bundle, state, commands))
    inputs = TimingInputs(bundle, state)
    result = compile_timing(inputs, commands)
    assert (bundle, state, commands) == before
    assert result.bundle is not bundle
    result.bundle["app"]["foreign"]["list"].append("output only")
    assert (bundle, state, commands) == before
    with pytest.raises(FrozenInstanceError):
        inputs.bundle = {}


def test_compiler_resolve_is_idempotent_on_the_available_fixture():
    first = compile_timing(tiny())
    second = compile_timing(first.bundle)
    assert second.bundle == first.bundle
    assert second.values == ([],)


def test_compiled_checkout_preserves_live_clip_and_params_handles():
    tl = Checkout(tiny())
    clip = tl.clip("c1.rocket")
    data, params = clip.data, clip.params
    tl.voice("s1").set_gap_after(0.5)
    clip.on('"went"')
    tl.resolve()
    assert clip.data is data is tl.clip("c1.rocket").data
    assert clip.params is params
    assert clip.start == pytest.approx(tl.word("went").start, abs=1 / tl.fps)
    clip.params["x"] = 123
    assert tl.clip("c1.rocket").params["x"] == 123


def test_open_ripple_scope_keeps_identity_based_music_exclusions():
    tl = Checkout(tiny())
    bed = next(c for c in tl.clips(audio=True) if c.track == "music")
    bed.data["to"] = 8.0
    original = copy.deepcopy(bed.data)
    with retime_scope(tl):
        tl.insert_time(1.0, 0.5, skip=[bed.data])
        assert tl._scope[0][2] == frozenset({id(bed.data)})
        tl.insert_time(2.0, 0.5, skip=[bed.data])
        assert all(record[2] == frozenset({id(bed.data)}) for record in tl._scope)
    assert bed.data == original
    assert len([c for c in tl.clips(audio=True) if c.track == "music"]) == 1


def test_compiler_routes_resolve_and_reflow_and_preserves_external_step_handles(monkeypatch):
    from astrid.sdk.timeline import compiler

    calls = []
    original = compiler.compile_timing

    def recorded(inputs, commands=()):
        calls.extend(command[0] for command in commands)
        return original(inputs, commands)

    monkeypatch.setattr(compiler, "compile_timing", recorded)
    tl = Checkout(tiny())
    tl.resolve()
    tl.reflow()
    steps = [{"id": "one", "at": 0.0, "hold": 1 / 30},
             {"id": "two", "at": 1 / 30, "hold": 3 / 30}]
    first = steps[0]
    tl._relay_steps(steps, 0.0, 8 / 30)
    assert steps[0] is first and first["hold"] == pytest.approx(2 / 30, abs=1e-6)
    assert calls == ["resolve", "reflow", "_relay_steps"]


def test_failed_compile_is_immutable_but_checkout_keeps_existing_partial_edit_semantics(monkeypatch):
    def boom(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(Checkout, "retime", boom)
    bundle = tiny()
    before = copy.deepcopy(bundle)
    with pytest.raises(RuntimeError, match="boom"):
        compile_timing(bundle, (("remove_line", ("s1",), {}),))
    assert bundle == before

    tl = Checkout(tiny())
    with pytest.raises(RuntimeError, match="boom"):
        tl.remove_line("s1")
    # Mutable compatibility entry points keep their existing error behavior:
    # the line was already removed; the failed scope never cuts the music.
    assert "s1" not in {line.segment for line in tl.lines()}
    assert len([c for c in tl.clips(audio=True) if c.track == "music"]) == 1
    assert tl._scope is None
