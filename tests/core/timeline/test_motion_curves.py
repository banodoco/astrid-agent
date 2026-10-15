"""Declared motion maths and generated cross-language agreement, without rendering."""
from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess

import pytest

from astrid.core.timeline.motion_curves import (
    MOTION_CURVES, motion_curve_inverse, motion_curve_name, motion_curve_progress,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("name,expected", [
    ("in", [.0625, .25, .5625]), ("out", [.4375, .75, .9375]),
    ("in-out", [.125, .5, .875]),
])
def test_declared_quadratic_intermediate_values(name, expected):
    assert [motion_curve_progress(name, p) for p in [.25, .5, .75]] == expected


@pytest.mark.parametrize("name", MOTION_CURVES)
def test_curve_exact_endpoints_finite_clamp_and_monotonicity(name):
    assert motion_curve_progress(name, -1) == 0
    assert motion_curve_progress(name, 2) == 1
    values = [motion_curve_progress(name, p / 100) for p in range(101)]
    assert values[0] == 0 and values[-1] == 1
    assert values == sorted(values)


@pytest.mark.parametrize("name", ["linear", "in", "out", "in-out"])
def test_analytic_inverse_round_trips_and_exact_endpoints(name):
    assert motion_curve_inverse(name, -1) == 0
    assert motion_curve_inverse(name, 2) == 1
    for p in [0, 1e-9, .0625, .125, .25, .5, .75, .875, 1 - 1e-9, 1]:
        assert motion_curve_progress(name, motion_curve_inverse(name, p)) == pytest.approx(p, abs=1e-15)


@pytest.mark.parametrize("name", MOTION_CURVES)
@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_nonfinite_progress_is_refused(name, value):
    with pytest.raises(ValueError, match="must be finite"):
        motion_curve_progress(name, value)
    with pytest.raises(ValueError, match="must be finite"):
        motion_curve_inverse(name, value)


def test_step_holds_until_exact_end_and_never_has_an_inverse():
    assert motion_curve_progress("step", math.nextafter(1, 0)) == 0
    assert motion_curve_progress("step", 1) == 1
    for p in [0, .5, 1]:
        with pytest.raises(ValueError, match="has no inverse progress"):
            motion_curve_inverse("step", p)


@pytest.mark.parametrize("alias,canonical", [
    ("inOut", "in-out"), ("inout", "in-out"), ("in_out", "in-out"),
    ("ease-in", "in"), ("ease-out", "out"), ("ease-in-out", "in-out"),
])
def test_registry_aliases_normalize(alias, canonical):
    assert motion_curve_name(alias) == canonical


def test_unknown_curve_refuses_instead_of_becoming_linear():
    for unknown in ["elastic", "__proto__", "constructor"]:
        with pytest.raises(ValueError, match="unknown motion easing"):
            motion_curve_name(unknown)


def test_generated_python_and_typescript_are_fresh():
    subprocess.run(["python3", "scripts/gen_motion_curves.py", "--check"], cwd=ROOT, check=True)


def test_generated_typescript_matches_python_maths_and_refusals():
    module = (ROOT / "astrid/core/timeline/motion_curves.generated.ts").as_uri()
    script = f'''import {{ motionCurveName, motionCurveProgress, motionCurveInverse }} from {json.dumps(module)};
const names = ['linear', 'in', 'out', 'in-out', 'step'];
const progress = [-1, 0, 0.0625, .25, .5, .75, .875, 1, 2];
const rows = names.map(name => [name, progress.map(p => motionCurveProgress(name, p)),
  name === 'step' ? null : progress.map(p => motionCurveInverse(name, p))]);
let refusals = 0;
for (const p of [NaN, Infinity, -Infinity]) for (const name of names) {{
  for (const fn of [motionCurveProgress, motionCurveInverse]) {{
    try {{ fn(name, p); }} catch {{ refusals++; }}
  }}
}}
for (const p of [0, .5, 1]) {{ try {{ motionCurveInverse('step', p); }} catch {{ refusals++; }} }}
for (const name of ['unknown', '__proto__', 'constructor']) {{ try {{ motionCurveName(name); }} catch {{ refusals++; }} }}
console.log(JSON.stringify({{rows, refusals, aliases: ['inOut','inout','in_out','ease-in','ease-out','ease-in-out'].map(motionCurveName),
  stepBeforeEnd: motionCurveProgress('step', 1-Number.EPSILON)}}));'''
    result = subprocess.run(["node", "--experimental-strip-types", "--input-type=module", "-e", script],
                            capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    for name, values, inverse in data["rows"]:
        points = [-1, 0, .0625, .25, .5, .75, .875, 1, 2]
        assert values == [motion_curve_progress(name, p) for p in points]
        if inverse is not None:
            assert inverse == [motion_curve_inverse(name, p) for p in points]
    assert data["refusals"] == 36
    assert data["aliases"] == ["in-out", "in-out", "in-out", "in", "out", "in-out"]
    assert data["stepBeforeEnd"] == 0
