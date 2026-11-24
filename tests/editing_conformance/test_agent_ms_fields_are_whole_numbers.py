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
from astrid.sdk.timeline._clip_commands import capture_clip_add
from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._shot_commands import capture_shot_set
from astrid.sdk.timeline._split_commands import capture_clip_split
from astrid.sdk.timeline._trim_commands import capture_clip_trim
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.native_occurrence_duplicate import capture_occurrence_duplicate
from astrid.sdk.timeline.native_occurrence_fork import capture_occurrence_fork
from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_add_media
from astrid.sdk.timeline.native_occurrence_shared import (
    capture_occurrence_place_shared,
    foreign_shared_command,
    resolve_source,
)
from astrid.sdk.timeline.native_parent_media_commands import (
    capture_clip_add_media,
    capture_clip_duplicate_media,
)
from astrid.sdk.timeline.native_placement_commands import capture_placement, capture_placement_create
from astrid.sdk.timeline.native_shot_pool_commands import capture_shot_pool
from astrid.sdk.timeline.native_timeline_duplicate import copy_command
from tests.editing_conformance.test_recipe_occurrence_create import birth, empty_bundle
from tests.editing_conformance.test_recipe_occurrence_duplicate import duplicate_document
from tests.editing_conformance.test_recipe_occurrence_media import inputs
from tests.sdk.test_timeline_commit_provider import runtime_abi  # noqa: F401
from tests.sdk.test_timeline_commit_provider_document import _digest


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
    "occurrence.place-shared-foreign",
    "timeline.duplicate",
    "clip.move-ms-only",
    "clip.move-dual",
    "placement.move-parent",
    "placement.move-child",
    "placement.create",
    "clip.trim",
    "clip.split",
    "occurrence.add-media",
    "clip.add-media",
    "clip.duplicate-media",
    "clip.add",
    "clip.set",
    "shot.set",
    "shot.pool",
]

# These recipes author literal seconds, not the millisecond codec. Assert
# their actual output geometry rather than counting retained fixture fields.
# The media/shot fixture has a zero occurrence origin and clip-a at 1 s
# with a 2 s hold. 0.125 s at 30000/1001 selects frame 4.
SECONDS_WITNESSES = {
    "placement.create": (
        (OWNER + "/clip:ms-image", "at", Fraction(4, 1) / RATE),
        (OWNER + "/clip:ms-image", "hold", Fraction(5, 4)),
    ),
    "clip.trim": (
        ("parent/clip:clip-a", "at", Fraction(1)),
        ("parent/clip:clip-a", "from", Fraction(1, 8)),
        ("parent/clip:clip-a", "to", Fraction(9, 4)),
        ("parent/clip:clip-a", "hold", Fraction(17, 8)),
    ),
    "clip.split": (
        ("parent/clip:clip-a", "at", Fraction(1)),
        ("parent/clip:clip-a", "hold", Fraction(1001, 1000)),
        ("parent/clip:right", "at", Fraction(2001, 1000)),
        ("parent/clip:right", "hold", Fraction(999, 1000)),
    ),
    "occurrence.add-media": (
        (OWNER + "/clip:ms-image", "at", Fraction(1, 8)),
        (OWNER + "/clip:ms-image", "hold", Fraction(5, 4)),
    ),
    "clip.add-media": (
        ("parent/clip:ms-image", "at", Fraction(1, 8)),
        ("parent/clip:ms-image", "hold", Fraction(5, 4)),
    ),
    "clip.duplicate-media": (
        ("parent/clip:source-video", "at", Fraction(1, 8)),
        ("parent/clip:source-video", "hold", Fraction(5, 4)),
        ("parent/clip:duplicate-video", "at", Fraction(11, 8)),
        ("parent/clip:duplicate-video", "hold", Fraction(5, 4)),
    ),
    "clip.add": (
        ("parent/clip:ms-title", "at", Fraction(17, 4)),
        ("parent/clip:ms-title", "hold", Fraction(5, 4)),
    ),
    "clip.set": (
        ("parent/clip:clip-a", "at", Fraction(1)),
        ("parent/clip:clip-a", "hold", Fraction(5, 4)),
    ),
}
# shot.set and shot.pool do not write geometry. They remain execution/App
# smoke cases only, not independently checked time writes.
NON_TIMING_CASES = {"shot.set", "shot.pool"}


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

    if name in ("occurrence.place-shared-foreign", "timeline.duplicate"):
        # A detached immutable source port supplies exact, real fixture images
        # to the production resolver; neither recipe nor recorder is mocked.
        before = empty_bundle(fps=FPS)
        source = duplicate_document(count=1, shared_shot=False)
        source["project_id"] = before["project_id"]
        source["timeline_id"] = "qualification-source"
        parent = source["parent_revision"]
        source_start = frame_ms(4.25, RATE)
        parent["payload"]["occurrences"][0]["placement"]["start_ms"] = source_start
        parent["content_digest"] = _digest(parent["payload"])
        pin = {"project_id": source["project_id"], "timeline_id": source["timeline_id"],
               "revision_id": parent["revision_id"], "content_digest": parent["content_digest"],
               "publication_id": "qualification-source-publication"}

        class SourcePort:
            def revision_closure(self, ref):
                assert ref == pin
                return copy.deepcopy(source)

            source_revision_closure = revision_closure

            def shot_closure(self, shot_id, revision_id):
                shot = source["shot_revisions"][0]
                assert (shot_id, revision_id) == (shot["shot_id"], shot["revision_id"])
                return {"shot_revision": copy.deepcopy(shot),
                        "internal_timeline_revision": copy.deepcopy(source["internal_timeline_revisions"][0])}

            def raw_media_info(self, media_pin):
                assert media_pin in source["dependency_manifest"]["media"]
                return copy.deepcopy(media_pin)

        resolver = resolve_source(SourcePort(), lambda ctx: [])
        if name == "timeline.duplicate":
            call = copy_command(pin, {"chapter-shot": "copied-shot"}, {"occ-0": "foreign-occ"})
            expected = source_start
        else:
            on = 14.25
            call = foreign_shared_command(pin, "occ-0", "foreign-occ", on=on)
            expected = frame_ms(on, RATE)
        plan = commands.execute(before, [call], resolve_inputs=resolver)
        return before, plan, RATE, [
            ("occurrence:foreign-occ", ("placement", "start_ms"), expected),
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

    if name == "clip.duplicate-media":
        _, old, new, verified = inputs("video")
        empty = empty_bundle(fps=FPS)
        row = {"id": "source-video", "clipType": "media", "asset": old["asset"],
               "track": "picture", "at": 0.125, "from": 0, "to": 1.25, "hold": 1.25}
        before = _plan(empty, capture_clip_add_media(
            empty, row, old, verified_media=verified), verified).bundle
        call = capture_clip_duplicate_media(
            before, "parent/clip:source-video", "duplicate-video", old, new,
            verified_media=verified,
        )
        return before, _plan(before, call, verified), RATE, []

    before, _, new, verified = _media_bundle()
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
    elif name == "clip.add":
        call = capture_clip_add(before, "parent", {"id": "ms-title", "clipType": "text",
                                "track": "picture", "at": 4.25, "hold": 1.25})
    elif name == "clip.set":
        call = capture_clip_set(before, "parent/clip:clip-a", values={"hold": 1.25})
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
    else:
        raise AssertionError("unclassified case: " + name)
    return before, _plan(before, call, verified), RATE, []


def _assert_exercised(case, before, plan):
    operation = ("occurrence.place-shared" if case == "occurrence.place-shared-foreign"
                 else case.split("-")[0] if case.startswith(("clip.move-", "placement.move-",
                                                            "occurrence.create-")) else case)
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
        elif path[-1] != "duration_ms" and value < 0:
            errors.append(f"{label}: {value!r}, expected nonnegative")
    for ref, path, expected in witnesses:
        actual = State(plan.bundle).entity(ref)
        for part in path:
            actual = actual[part]
        if type(actual) is not int or actual != expected:
            errors.append(
                f"{case}: {ref}.{'.'.join(path)}: {actual!r}, expected int {expected}"
            )
    seconds_witnesses = SECONDS_WITNESSES.get(case, ())
    assert witnesses or seconds_witnesses or case in NON_TIMING_CASES, (
        f"{case}: no independent time-value witness"
    )
    for ref, field, expected in seconds_witnesses:
        actual = State(plan.bundle).entity(ref)[field]
        if type(actual) not in (int, float) or actual != pytest.approx(
            float(expected), rel=0, abs=1e-9,
        ):
            errors.append(
                f"{case}: {ref}.{field}: {actual!r}, expected {expected} seconds"
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
    app_root = os.environ.get("ASTRID_APP_ROOT") or os.environ.get("ASTRID_APP_CHECKOUT")
    assert app_root, "Set ASTRID_APP_ROOT or ASTRID_APP_CHECKOUT to the prepared pinned App checkout"
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
