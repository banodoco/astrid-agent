"""R15-Q-03: check authored millisecond geometry at native command boundaries.

The expected values are computed independently of the command implementation.
Seconds-valued fields are not millisecond fields. Historical source images are
not the result of a write and are deliberately excluded from the walk.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from astrid.sdk.timeline import commands
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.emitter import emit_document_commands
from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
from astrid.sdk.timeline.native_occurrence_duplicate import capture_occurrence_duplicate
from astrid.sdk.timeline.native_occurrence_fork import capture_occurrence_fork
from astrid.sdk.timeline.native_occurrence_shared import capture_occurrence_place_shared
from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_add_media
from astrid.sdk.timeline.native_parent_media_commands import capture_clip_add_media
from astrid.sdk.timeline.native_placement_commands import capture_placement, capture_placement_create
from astrid.sdk.timeline.native_shot_pool_commands import capture_shot_pool
from astrid.sdk.timeline._shot_commands import capture_shot_set
from astrid.sdk.timeline._split_commands import capture_clip_split
from astrid.sdk.timeline._trim_commands import capture_clip_trim
from astrid.sdk.timeline.capture import capture_move
from tests.editing_conformance.test_recipe_occurrence_create import birth, empty_bundle
from tests.editing_conformance.test_recipe_occurrence_duplicate import duplicate_document
from tests.editing_conformance.test_recipe_occurrence_media import inputs
from tests.sdk.test_timeline_commit_provider_document import _digest, document_fixture


RATE = Fraction(30000, 1001)
FPS = {"numerator": 30000, "denominator": 1001}
OWNER = "occurrence:occ-a"
CREATE_CASES = {
    "occurrence.create-30": (30, Fraction(30), 4.25, 4),
    "occurrence.create-decimal": (29.97, Fraction("29.97"), 4.25, 10.01),
    "occurrence.create-rational": (FPS, RATE, 4.25, 10.01),
    "occurrence.create-half-even-down": (16, Fraction(16), 0.063, 4),
    "occurrence.create-half-even-up": (16, Fraction(16), 0.19, 4),
}
CASES = [
    *CREATE_CASES,
    "occurrence.duplicate",
    "occurrence.fork",
    "occurrence.place-shared",
    "clip.move-ms-only",
    "clip.move-dual",
    "placement.move-parent",
    "placement.move-child",
    "placement.create",
    "clip.trim",
    "clip.split",
    "occurrence.add-media",
    "clip.add-media",
    "shot.set",
    "shot.pool",
    "doc.patch",
]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("R15-Q-03 must not access the network")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, forbidden)


def frame_ms(seconds, fps):
    # Select the nearest frame (half-up), then encode that exact rational
    # frame position as integer milliseconds (half-even).
    frame = (Fraction(str(seconds)) * fps + Fraction(1, 2)) // 1
    return round(Fraction(frame * 1000, 1) / fps)


def _grid_ms(value, fps):
    frame = (Fraction(value, 1000) * fps + Fraction(1, 2)) // 1
    return round(Fraction(frame * 1000, 1) / fps)


def _ms_fields(node, path):
    if isinstance(node, dict):
        for key, value in node.items():
            here = (*path, str(key))
            if key.endswith("_ms"):
                yield here, value
            yield from _ms_fields(value, here)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _ms_fields(value, (*path, str(index)))


def _authored_ms(bundle):
    yield from _ms_fields(bundle["parent"], ("parent",))
    yield from _ms_fields(bundle["placements"], ("placements",))
    for identity, shot in bundle["shots"].items():
        for key in ("payload", "internal_timeline"):
            yield from _ms_fields(shot[key], ("shots", identity, key))


def _one_occurrence(document):
    parent = document["parent_revision"]
    parent["payload"]["occurrences"] = parent["payload"]["occurrences"][:1]
    row = parent["payload"]["occurrences"][0]
    row["placement"]["start_ms"] = 0
    parent["content_digest"] = _digest(parent["payload"])
    return document


def _media_bundle():
    document, old, new, verified = inputs()
    return bundle_from_document(_one_occurrence(document)), old, new, verified


def _plan(before, call, verified=None):
    kwargs = {"verified_media": verified} if verified is not None else {}
    return commands.execute(before, [call], **kwargs)


def _case(name):
    """Return the before image, actual command result, FPS, independent witnesses."""
    if name in CREATE_CASES:
        fps, rate, on, duration = CREATE_CASES[name]
        before = empty_bundle(fps=fps)
        plan = _plan(before, birth(before, on=on, duration=duration))
        return before, plan, rate, [
            ("occurrence:first-occurrence", ("placement", "start_ms"), frame_ms(on, rate)),
        ]

    if name in ("occurrence.duplicate", "occurrence.fork", "occurrence.place-shared"):
        document = duplicate_document(count=1, shared_shot=False)
        parent = document["parent_revision"]
        source_start = frame_ms(4.25, RATE)
        parent["payload"]["occurrences"][0]["placement"]["start_ms"] = source_start
        parent["content_digest"] = _digest(parent["payload"])
        before = bundle_from_document(document)
        owner = "occurrence:occ-0"
        if name == "occurrence.duplicate":
            call = capture_occurrence_duplicate(before, owner, "copy-occ", "copy-shot")
            ref, expected = "occurrence:copy-occ", source_start
        elif name == "occurrence.fork":
            call = capture_occurrence_fork(before, owner, "forked-shot")
            ref, expected = owner, source_start
        else:
            on = 14.25
            call = capture_occurrence_place_shared(before, owner, "shared-occ", on=on)
            ref, expected = "occurrence:shared-occ", frame_ms(on, RATE)
        return before, _plan(before, call), RATE, [
            (ref, ("placement", "start_ms"), expected),
        ]

    if name.startswith(("clip.move-", "placement.move-")):
        before, _, _, _ = _media_bundle()
        child = name == "placement.move-child"
        owner = OWNER if child else "parent"
        ref = owner + "/clip:clip-a"
        target = State(before).entity(ref)
        target["at_ms"] = 0
        if name == "clip.move-dual":
            target["at"] = 0
        else:
            target.pop("at", None)
        if owner == "parent":
            before["parent"]["config"]["clips"] = copy.deepcopy(before["parent"]["clips"])
        on = 0.04
        if name.startswith("clip.move-"):
            call = capture_move(before, ref, on=on)
        else:
            call = capture_placement(
                before, owner, "clip-a", on=on,
                ownership="occurrence" if child else "parent",
            )
        return before, _plan(before, call), RATE, [
            (ref, ("at_ms",), frame_ms(on, RATE)),
        ]

    before, old, new, verified = _media_bundle()
    del old
    row = {"id": "ms-image", "clipType": "image", "asset": new["asset"],
           "track": "picture", "at": 0.125, "hold": 1.25}
    if name == "placement.create":
        call = capture_placement_create(
            before, OWNER, "ms-image", new, on=0.125, hold=1.25,
            ownership="occurrence", verified_media=verified,
        )
    elif name == "occurrence.add-media":
        call = capture_occurrence_add_media(
            before, OWNER, row, new, verified_media=verified,
        )
    elif name == "clip.add-media":
        before = empty_bundle(fps=FPS)
        call = capture_clip_add_media(before, row, new, verified_media=verified)
    elif name == "shot.pool":
        call = capture_shot_pool(
            before, OWNER, "add", new, ownership="occurrence", verified_media=verified,
        )
    elif name == "shot.set":
        call = capture_shot_set(before, OWNER + "/shot", values={"name": "MS qualification"})
    elif name == "clip.trim":
        before["parent"]["registry"]["assets"]["picture"].update(type="video", duration=10.01)
        call = capture_clip_trim(before, "parent/clip:clip-a", in_s=0.125, out_s=2.25)
    elif name == "clip.split":
        call = capture_clip_split(before, "parent/clip:clip-a", on=2.001, right_id="right")
    elif name == "doc.patch":
        # Generic patch transports authored geometry, not seconds. Supply an
        # independently encoded frame position and require it to survive.
        candidate = copy.deepcopy(before)
        expected = frame_ms(4.25, RATE)
        State(candidate).occurrence(OWNER)["placement"]["start_ms"] = expected
        calls = emit_document_commands(before, candidate)
        return before, commands.execute(before, calls), RATE, [
            (OWNER, ("placement", "start_ms"), expected),
        ]
    else:
        raise AssertionError("unclassified case: " + name)
    return before, _plan(before, call, verified), RATE, []


def _assert_exercised(case, before, plan):
    operation = case.split("-")[0] if case.startswith(("clip.move-", "placement.move-",
                                                        "occurrence.create-")) else case
    assert plan.records, case
    assert plan.records[0]["command"]["op"] == operation, case
    assert plan.bundle != before, "command did not change its fixture: " + case


@pytest.mark.parametrize("case", CASES, ids=CASES)
def test_agent_ms_fields_are_whole_numbers(case):
    before, plan, fps, witnesses = _case(case)
    _assert_exercised(case, before, plan)
    errors = []
    for path, value in _authored_ms(plan.bundle):
        label = case + ": " + ".".join(path)
        if type(value) is not int:
            errors.append(f"{label}: {value!r} ({type(value).__name__}), expected int")
        elif path[-1] != "duration_ms" and value != _grid_ms(value, fps):
            errors.append(f"{label}: {value!r}, not the integer encoding of a frame")
        elif value < 0:
            errors.append(f"{label}: {value!r}, expected nonnegative")
    for ref, path, expected in witnesses:
        actual = State(plan.bundle).entity(ref)
        for part in path:
            actual = actual[part]
        if type(actual) is not int or actual != expected:
            errors.append(
                f"{case}: {ref}.{'.'.join(path)}: {actual!r}, expected int {expected}"
            )
    assert not errors, "\n".join(errors)


# Do not use or modify Q-01's files. This is a separate, small Node invocation
# of the real canonical-document adapter and the real App composition parser.
# No synthetic composition, rounding, filtering or schema relaxation.
APP_CHECK = r"""
import fs from 'node:fs';
import net from 'node:net';
import dns from 'node:dns';
import http from 'node:http';
import https from 'node:https';
import { syncBuiltinESMExports } from 'node:module';
const forbidden = () => { throw new Error('R15-Q-03: network access forbidden'); };
net.Socket.prototype.connect = forbidden;
dns.lookup = forbidden;
dns.resolve = forbidden;
http.request = http.get = https.request = https.get = forbidden;
globalThis.fetch = forbidden;
syncBuiltinESMExports();
const { runtimeGraphToContractFromCanonicalDocument } =
    await import('./src/integrations/runtime/dataProvider.ts');
const { parseShotComposition } =
    await import('./src/tools/video-editor/data/shotComposition.ts');
const doc = JSON.parse(fs.readFileSync(0, 'utf8'));
const graph = runtimeGraphToContractFromCanonicalDocument(
    doc.project_id, doc.timeline_id, doc, 'r15-q03-publication');
parseShotComposition(graph);
if (graph.occurrences.length) {
    const invalid = structuredClone(graph);
    invalid.occurrences[0].at_ms = 0.5;
    let rejected = false;
    try { parseShotComposition(invalid); }
    catch (error) {
        if (!String(error).includes('occurrences[0].at_ms')) throw error;
        rejected = true;
    }
    if (!rejected) throw new Error('negative control: fractional at_ms accepted');
}
"""


@pytest.mark.app_checkout
@pytest.mark.parametrize("case", CASES, ids=CASES)
def test_agent_ms_composition_opens_in_app(case):
    app_root = os.environ.get("ASTRID_APP_ROOT")
    assert app_root, "Set ASTRID_APP_ROOT to the pinned, prepared sibling App checkout"
    root = Path(app_root).resolve()
    assert (root / "node_modules" / "tsx").exists(), "App tsx dependencies are not prepared"
    node = shutil.which("node")
    assert node, "Node is required for the explicitly selected App proof"
    before, plan, _, _ = _case(case)
    _assert_exercised(case, before, plan)
    doc = document_from_bundle(plan.bundle)
    proc = subprocess.run(
        [node, "--import", "tsx", "--input-type=module", "-e", APP_CHECK],
        cwd=root, input=json.dumps(doc, allow_nan=False), text=True,
        capture_output=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, f"{case}: App composition rejected:\n{proc.stdout}\n{proc.stderr}"
