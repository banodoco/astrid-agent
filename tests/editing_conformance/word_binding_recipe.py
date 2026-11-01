"""Pure authored-reference adapter and test-local declaration recipe.

Nothing here is installed in the product registry or default compiler. History
supplies ordinary undo provenance; interpreting a canonical revision needs none.
"""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace

from jsonschema import Draft202012Validator

from astrid.core.timeline import moments
from astrid.sdk.timeline import commands
from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline.compiler import (
    TimingCompileError,
    TimingInputs,
    WordBinding,
    compile_timing,
)
from astrid.sdk.timeline.units import source_to_timeline_time

OP = "clip.anchor"
PATH = ["app", "astrid"]
REFERENCE = {
    "type": "object", "additionalProperties": False,
    "required": ["source", "document", "word_ids"],
    "properties": {
        "source": {"type": "string", "pattern": "^occurrence:[^/]+/clip:[^/]+$"},
        "document": {
            "type": "object", "additionalProperties": False,
            "required": ["document_id", "version", "content_digest"],
            "properties": {
                "document_id": {"type": "string", "minLength": 1},
                "version": {"type": "integer", "minimum": 1},
                "content_digest": commands._SCHEMA["$defs"]["Digest"],
            },
        },
        "word_ids": {"type": "array", "minItems": 1, "uniqueItems": True,
                     "items": {"type": "string", "pattern": "^word:[0-9a-f]{64}(?::[1-9][0-9]*)?$"}},
        "edge": {"enum": ["start", "end"]},
        "offset_s": {"type": "number"},
        "offset_frames": {"type": "integer"},
    },
}
PARAMETERS = {
    "type": "object", "additionalProperties": False,
    "required": ["field", "declaration", "choice"],
    "properties": {
        "field": {"enum": ["on", "until"]},
        "declaration": {"type": ["string", "null"]},
        "choice": {"oneOf": [{"type": "null"}, REFERENCE]},
    },
}


def authored(bundle):
    """Read namespaced values, without accepted records or a history fold."""
    return {(f"parent/clip:{clip['id']}", field): {
        "field": field, "declaration": clip.get("app", {}).get("astrid", {}).get(field),
        "choice": copy.deepcopy(choice)}
        for clip in bundle["parent"]["clips"]
        for field, choice in clip.get("app", {}).get("astrid", {}).get("word_anchors", {}).items()}


def binding(bundle, target, field, choice, sources):
    try:
        json.dumps(choice, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise TimingCompileError("invalid authored word reference: expected finite JSON") from exc
    errors = list(Draft202012Validator(REFERENCE).iter_errors(choice))
    if errors:
        raise TimingCompileError("invalid authored word reference: " + errors[0].message)
    owner, clip = choice["source"].split("/clip:")
    occurrence = owner.removeprefix("occurrence:")
    placements = [row for row in bundle["placements"] if row["occurrence_id"] == occurrence]
    if len(placements) != 1:
        raise TimingCompileError("bound source occurrence/clip is missing or ambiguous")
    pin = choice["document"]
    captures = [row for row in sources if row["pin"] == pin]
    if len(captures) != 1:
        raise TimingCompileError("exact transcript pin is missing or revoked or ambiguous")
    words = {word["id"]: word for segment in captures[0]["document"]["content"]["segments"]
             for word in segment["words"]}
    if any(key not in words for key in choice["word_ids"]):
        raise TimingCompileError("bound stable word is missing; text rebinding is forbidden")
    # A private kernel spelling from exact IDs, never from the echo. The kernel
    # validates only these IDs; it cannot select a different transcript word.
    text = " ".join(words[key]["text"] for key in choice["word_ids"])
    moment = moments.Moment(kind="word", text=text, edge=choice.get("edge", "start"),
        offset_s=choice.get("offset_s", 0), offset_frames=choice.get("offset_frames", 0))
    declaration = moments.format_moment(replace(moment, offset_s=0))
    if moment.offset_s:
        declaration += f" {Decimal(str(moment.offset_s)):+f}s"
        if Fraction(str(moments.parse(declaration).offset_s)) != Fraction(str(moment.offset_s)):
            raise TimingCompileError("word reference second offset exceeds main Moment precision")
    return WordBinding(("parent", "", target.removeprefix("parent/clip:")), field,
        declaration, (occurrence, placements[0]["shot_id"], clip),
        pin["document_id"], pin["version"], pin["content_digest"], choice["word_ids"])


def legacy_binding(bundle, target, field, declaration, sources):
    """Only an unambiguous legacy text lookup is safe without a stored choice."""
    moment = moments.parse(declaration)
    hits = []
    for capture in sources:
        content = capture["document"]["content"]
        for row in bundle["placements"]:
            internal = bundle["shots"][row["shot_id"]]["internal_timeline"]
            for clip in internal["clips"]:
                asset = internal.get("registry", {}).get("assets", {}).get(clip.get("asset"), {})
                if asset.get("media_id") != content["media_id"]:
                    continue
                origin = Fraction(row["placement"]["start_ms"], 1000) + Fraction(str(clip["at"]))
                words = []
                for segment in content["segments"]:
                    for index, word in enumerate(segment["words"]):
                        if not clip["from"] <= word["start"] < word["end"] <= clip["to"]:
                            continue
                        times = {key: source_to_timeline_time(Fraction(str(word[key])),
                            source_start=Fraction(str(clip["from"])), timeline_start=origin,
                            speed=clip["speed"]) for key in ("start", "end")}
                        words.append(SimpleNamespace(**dict(word, **times), segment=segment["id"], index=index))
                for phrase in moments.find_words(replace(moment, n=None), words):
                    pin = capture["pin"]
                    hits.append(WordBinding(("parent", "", target.removeprefix("parent/clip:")), field,
                        declaration, (row["occurrence_id"], row["shot_id"], clip["id"]),
                        pin["document_id"], pin["version"], pin["content_digest"],
                        tuple(word.id for word in phrase)))
    if len(hits) > 1:
        raise TimingCompileError("ambiguous anchor; choose occurrence")
    if not hits:
        raise TimingCompileError("legacy word anchor is missing")
    return replace(hits[0], declaration=moments.format_moment(replace(moment, n=None)))


def compile_authored(bundle, sources):
    """Extract first, then compile. No history, IO, registry or authoring writes."""
    if any(clip.get("app", {}).get("astrid", {}).get("word_anchors")
           for shot in bundle["shots"].values() for clip in shot["internal_timeline"]["clips"]):
        raise TimingCompileError("spike requires parent word anchor owner")
    projection, bindings = copy.deepcopy(bundle), []
    for clip in projection["parent"]["clips"]:
        app = clip.get("app", {}).get("astrid", {})
        refs = app.get("word_anchors", {})
        if not isinstance(refs, dict) or set(refs) - {"on", "until"}:
            raise TimingCompileError("invalid authored word anchor fields")
        for field in ("on", "until"):
            target = "parent/clip:" + clip["id"]
            if field in refs:
                selected = binding(bundle, target, field, refs[field], sources)
            elif app.get(field) and moments.parse(app[field]).kind == "word":
                selected = legacy_binding(bundle, target, field, app[field], sources)
            else:
                continue
            bindings.append(selected)
            app[field] = selected.declaration
    result = compile_timing(TimingInputs(projection, sources=sources, bindings=bindings))
    return replace(result, bundle=copy.deepcopy(bundle))


def declare(ctx, command):
    target, args = command["select"], command["args"]
    if not isinstance(target, str) or not target.startswith("parent/clip:"):
        raise CommandError("spike requires a parent clip")
    ctx.read(target, ["id"])
    ctx.object(target, ["app"])
    ctx.object(target, PATH)
    ctx.object(target, PATH + ["word_anchors"])
    if args["declaration"] is None:
        if args["choice"] is not None:
            raise CommandError("clear has no word choice")
        ctx.clear(target, PATH + [args["field"]])
        ctx.clear(target, PATH + ["word_anchors", args["field"]])
        return
    if args["choice"] is None:
        raise CommandError("word declaration requires an exact choice")
    choice = args["choice"]
    source = choice["source"]
    owner = source.split("/clip:")[0]
    ctx.read(owner, ["occurrence_id"])
    ctx.read(owner + "/shot", ["shot_id"])
    ctx.read(source, ["id"])
    asset = ctx.read(source, ["asset"])
    ctx.read(owner + "/registry", ["assets", asset, "media_id"])
    ctx.write(target, PATH + [args["field"]], args["declaration"])
    ctx.write(target, PATH + ["word_anchors", args["field"]], choice)
    ctx.dependency("anchor.transcript", "transcript", choice["document"])
    ctx.bind("anchor.source", source, dependencies=["anchor.transcript"])
    ctx.record["bindings"][-1]["policy"] = "follow"


def install(monkeypatch):
    """Test-local registry/validation hook; product defaults stay untouched."""
    from astrid.sdk.timeline._recording import Recording

    registry, validate, compile_inputs = commands.operations, commands.validate_command, Recording.compile_inputs

    def registered():
        return {**registry(), OP: commands.Operation(PARAMETERS, declare, {"anchor": "follow"}, True)}

    def checked(command):
        if command.get("op") != OP:
            return validate(command)
        commands._VALIDATOR.validate(command)
        Draft202012Validator(PARAMETERS).validate(command["args"])
        if command["version"] != "1":
            raise CommandError("unsupported declaration version")

    def compiled(ctx):
        compile_inputs(ctx)
        # Bootstrap text-only ambiguity is the preexisting fixture. Once this
        # recipe owns a field, every native edit/restore validates the result.
        if any("word_anchors" in clip.get("app", {}).get("astrid", {})
               for clip in ctx.state.bundle["parent"]["clips"]):
            try:
                compile_authored(ctx.state.bundle,
                    ctx.state.bundle["source_mapping"].get("pinned_documents", []))
            except TimingCompileError as exc:
                raise CommandError(str(exc), kind="invalid_anchor") from exc

    monkeypatch.setattr(commands, "operations", registered)
    monkeypatch.setattr(commands, "validate_command", checked)
    monkeypatch.setattr(Recording, "compile_inputs", compiled)
