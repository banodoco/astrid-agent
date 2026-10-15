"""Read-only copy inventory/lint and ordinary command-based quote authoring.

All reads consume one supplied detached document. The snapshot hash is labelled
as such; it is never conflated with a separately fetched Runtime script snapshot.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping

from jsonschema import ValidationError

from astrid.core.timeline import copy_sync as core
from astrid.core.timeline.cuts import clip_duration
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline.doc import _read_words, _row_start


def _documents(bundle):
    yield None, None, 0.0, bundle.get("parent") or {}
    for row in bundle.get("placements") or []:
        sid, oid = str(row.get("shot_id")), row.get("occurrence_id")
        shot = (bundle.get("shots") or {}).get(sid) or {}
        yield oid, sid, _row_start(row) / 1000, shot.get("internal_timeline") or {}


def _scoped(oid, clip):
    return f"occurrence:{oid}/clip:{clip['id']}" if oid is not None else f"parent/clip:{clip['id']}"


def _audio(clip, tracks):
    from astrid.sdk.timeline._common import AUDIO_KINDS
    return tracks.get(clip.get("track"), {}).get("kind", "").lower() in AUDIO_KINDS or clip.get("track") in AUDIO_KINDS


def _support(clip, offset):
    start = offset + float(clip.get("at") or 0)
    return [start, start + float(clip_duration(clip))]


def source_context(bundle):
    """Pinned local script AND actual word-producer evidence from the same bundle.

    Missing binding identity is unavailable, not replaced by an invented binding.
    No media, live heads or independent transcript endpoints are read.
    """
    base = bundle.get("base_parent") or {}
    revision = base.get("revision_id") or (bundle.get("parent") or {}).get("revision_id")
    result = {"revision_id": revision, "snapshot_digest": core.digest(bundle),
              "snapshot_kind": "detached-document-sha256", "lines": [], "coverage": [],
              "status": "available" if revision else "unavailable"}
    oids = [r.get("occurrence_id") for r in bundle.get("placements") or []]
    for oid, sid, offset, document in _documents(bundle):
        if sid is None:
            continue
        shot = bundle["shots"].get(sid) or {}
        bindings = [b for b in (shot.get("payload") or {}).get("text_bindings", [])
                    if b.get("kind") == "voiceover_script"]
        valid = (revision and oid and oids.count(oid) == 1 and len(bindings) == 1
                 and all(isinstance(bindings[0].get(k), str) and bindings[0][k]
                         for k in ("binding_id", "content_hash")))
        if not valid:
            result["coverage"].append({"occurrence_id": oid, "reason": "missing or ambiguous pinned script/occurrence identity"})
        binding = bindings[0] if len(bindings) == 1 else {}
        tracks = {t["id"]: t for t in document.get("tracks") or []}
        occurrence_lines = []
        spoken_by_segment = {}
        for part in sorted(document.get("clips") or [], key=lambda c: float(c.get("at") or 0)):
            if _audio(part, tracks) and intent.line(part):
                spoken_by_segment.setdefault(intent.line(part), []).extend(
                    text for _a, _b, text in _read_words({"words": intent.words(part)}, relative=True))
        for clip in document.get("clips") or []:
            if not _audio(clip, tracks):
                continue
            segment = intent.line(clip)
            if not segment:
                continue
            authored_script = intent.line_text(clip)
            pending_script = (authored_script is not None and core.normalized(authored_script) !=
                              core.normalized(" ".join(spoken_by_segment.get(segment, []))))
            raw = _read_words({"words": intent.words(clip)}, relative=True)
            words = []
            for index, (start, end, text) in enumerate(raw):
                words.append({"id": f"{segment}:{index}", "text": text,
                              "start": offset + float(clip.get("at") or 0) + start,
                              "end": offset + float(clip.get("at") or 0) + end})
            assets = document.get("assets") or {}
            assets = assets.get("assets", assets)
            producer = {"clip": _scoped(oid, clip), "words_digest": core.digest(intent.words(clip)),
                        "lexical_digest": core.digest([core.normalized(w["text"]) for w in words]),
                        "asset": clip.get("asset"), "asset_dependency": copy.deepcopy(assets.get(clip.get("asset")))}
            source = {"revision_id": revision, "snapshot_digest": result["snapshot_digest"],
                      "snapshot_kind": result["snapshot_kind"], "occurrence_id": oid,
                      "binding_id": binding.get("binding_id"), "content_hash": binding.get("content_hash"),
                      "clip": _scoped(oid, clip), "segment": segment, "word_producer": producer}
            occurrence_lines.append({**source, "words": words, "interval": [min(w["start"] for w in words), max(w["end"] for w in words)] if words else None,
                                    "status": "available" if valid and not pending_script else "unavailable"})
            if pending_script:
                result["coverage"].append({"source": source["clip"], "reason": "authored script text differs from word producer; narration pending"})
            if not valid:
                result["coverage"].append({"source": source["clip"], "reason": "missing or ambiguous pinned script/occurrence identity"})
        # F's segment:index generator, applied to the actual occurrence's word
        # read (not the legacy lookup that collapses repeated placement clocks).
        counters = {}
        ordered = [(w, line["segment"], i) for line in occurrence_lines for i, w in enumerate(line["words"])]
        ordered.sort(key=lambda item: (item[0]["start"], item[1], item[2]))
        for word, segment, _i in ordered:
            index = counters.get(segment, 0)
            counters[segment] = index + 1
            word["id"] = f"{segment}:{index}"
        result["lines"].extend(occurrence_lines)
    if not result["lines"]:
        result["coverage"].append({"reason": "no admitted word producers"})
    return result


def _definition(component):
    from astrid.core.element.registry import load_default_registry
    registry = load_default_registry()
    try:
        return registry.get("effect", component)
    except KeyError:
        return None
    except ValueError:
        return None


def _get(data, path):
    for part in path.split("."):
        if not isinstance(data, Mapping) or part not in data:
            return None
        data = data[part]
    return data


def _slots(clip):
    definition = _definition(str(clip.get("clipType") or "media"))
    if definition is None:
        return [], None
    metadata = definition.metadata or {}
    paths = metadata.get("text") or []
    paths = [paths] if isinstance(paths, str) else paths
    return ["params." + p for p in paths], f"{definition.metadata.get('pack_id', definition.source)}:{definition.kind}:{definition.id}"


def _declarations(clip):
    value = intent.copy_quotes(clip)
    return value if isinstance(value, list) else [value]


def inventory(checkout, *, context=None, evaluated_support=None):
    """Enumerate every actual parent/occurrence visual layer without authoring.

    evaluated_support, when supplied, maps existing ScopedIds to resolved half-open
    support intervals in seconds. Otherwise evidence is explicitly authored support.
    """
    bundle = checkout.bundle
    context = source_context(bundle) if context is None else context
    if context["snapshot_digest"] != core.digest(bundle):
        raise ValueError("copy context does not belong to this detached document")
    rows, coverage = [], copy.deepcopy(context["coverage"])
    for oid, sid, offset, document in _documents(bundle):
        tracks = {t["id"]: t for t in document.get("tracks") or []}
        for clip in document.get("clips") or []:
            if _audio(clip, tracks):
                continue
            layer = _scoped(oid, clip)
            track = tracks.get(clip.get("track"), {})
            source_only = not track or track.get("kind") in {"source", "data", "transcript"}
            hidden = source_only or any(x.get("hidden") is True or x.get("visible") is False or x.get("enabled") is False
                         for x in (clip, track))
            paths, resource = _slots(clip)
            declarations = _declarations(clip)
            declared_paths = [q["display"].get("path") for q in declarations
                              if isinstance(q, Mapping) and isinstance(q.get("display"), Mapping)]
            missing_paths = [p for p in declared_paths if isinstance(p, str) and p not in paths]
            paths = list(dict.fromkeys(paths + missing_paths))
            intervals = (copy.deepcopy(evaluated_support.get(layer, [])) if evaluated_support is not None
                         else [_support(clip, offset)])
            base = {"layer": layer, "component": clip.get("clipType"), "resource": resource,
                    "revision_id": context["revision_id"], "snapshot_digest": context["snapshot_digest"],
                    "snapshot_kind": context["snapshot_kind"], "display_interval": _support(clip, offset),
                    "display_support": intervals, "support_evidence": "shared-evaluated-support" if evaluated_support is not None else "authored-support",
                    "visibility": "source-only" if source_only else "hidden" if hidden else "authored-visual-support",
                    "declarations": copy.deepcopy(declarations), "limitations": []}
            if not paths:
                rows.append({**base, "path": None, "text": None, "tokens": [], "matches": [], "status": "unavailable",
                             "limitations": ["component has no declared displayed-text slots; dynamic/native text not inferred"]})
                coverage.append({"layer": layer, "reason": "unsupported displayed-text coverage"})
            for path in paths:
                text = _get(clip, path)
                limitations = ["declaration points at a slot no longer declared by this component"] if path in missing_paths else []
                # No evaluation of dynamic values/formulas or arbitrary string params.
                dynamic = path in intent.formulas(clip) or path in missing_paths
                if not isinstance(text, str) or dynamic:
                    limitations.append("displayed slot is missing, nonliteral or dynamic")
                if hidden:
                    limitations.append("hidden authored text; rendered visibility not claimed")
                matches = []
                if isinstance(text, str) and not dynamic:
                    for line in context["lines"]:
                        if line["status"] != "available":
                            continue
                        for hit in core.runs(text, line):
                            source = {k: copy.deepcopy(line[k]) for k in (
                                "revision_id", "snapshot_digest", "snapshot_kind", "occurrence_id", "binding_id",
                                "content_hash", "clip", "segment", "word_producer")}
                            source["words"] = hit
                            matches.append({"source": source,
                                            "tokens": [{"token": t["token"], "word_id": w["id"], "source_range": t["range"]}
                                                       for w in hit for t in core.tokens(w["text"])],
                                            "speech_interval": line["interval"],
                                            "nearby": not hidden and any(core.overlap(line["interval"], x) for x in intervals)})
                available = context["status"] == "available" and not context["coverage"]
                status = ("unavailable" if limitations or not available else "empty" if not core.normalized(text)
                          else "unique" if len(matches) == 1 else "ambiguous" if matches else "unmatched")
                rows.append({**base, "path": path, "text": text, "tokens": core.tokens(text) if isinstance(text, str) else [],
                             "declarations": [q for q in declarations if
                                              (isinstance(q, Mapping) and isinstance(q.get("display"), Mapping) and q["display"].get("path") == path)
                                              or (path == paths[0] and (not isinstance(q, Mapping) or not isinstance(q.get("display"), Mapping)))],
                             "matches": matches, "status": status, "limitations": limitations})
    return {k: context[k] for k in ("revision_id", "snapshot_digest", "snapshot_kind")} | {"rows": rows, "coverage": coverage}


def _line(context, source):
    hits = [line for line in context["lines"] if core.line_key(line) == core.line_key(source)]
    return hits[0] if len(hits) == 1 else None


def _plain_words(words):
    return [{"id": w["id"], "text": w["text"]} for w in words]


def _source_at(line, words):
    return {k: copy.deepcopy(v) for k, v in line.items() if k not in {"interval", "status", "words"}} | {"words": _plain_words(words)}


def _association(quote, row, current, *, before=None, tracking=None):
    source = quote["source"]
    prior = tracking or {}
    reasons = []
    status = prior.get("status")
    selected = prior.get("source", source)
    if status in {"stale", "unavailable"}:
        reasons.extend(prior.get("reasons") or [{"code": "COPY_STALE", "reason": "source continuity lost; explicit rebinding required"}])
    line = _line(current, selected)
    new_source = None
    if not reasons:
        if line is None:
            known_before = before is not None and _line(before, selected) is not None
            missing = any((x["reason"] == "no admitted word producers" and not known_before)
                          or x.get("source") == selected["clip"]
                          or x.get("occurrence_id") == selected["occurrence_id"] for x in current["coverage"])
            code = "COPY_SOURCE_UNAVAILABLE" if current["status"] != "available" or missing else "COPY_STALE"
            reasons.append({"code": code, "reason": "source occurrence/line missing"})
        elif line["status"] != "available":
            reasons.append({"code": "COPY_SOURCE_UNAVAILABLE", "reason": "pinned source binding unavailable"})
        else:
            old_line = _line(before, selected) if before else None
            if selected.get("word_producer", {}).get("lexical_digest") == line["word_producer"]["lexical_digest"]:
                # Whole-line lexical identity also covers timing-only and raw-case edits.
                baseline = copy.deepcopy(line["words"])
                by_id = {w["id"]: w for w in selected["words"]}
                for word in baseline:
                    if word["id"] in by_id and core.normalized(word["text"]) == core.normalized(by_id[word["id"]]["text"]):
                        word["text"] = by_id[word["id"]]["text"]
                mapped, error = core.correspondence(baseline, line["words"], selected["words"])
            elif (old_line is not None and old_line["status"] == "available"
                  and selected.get("word_producer") == old_line["word_producer"]
                  and selected.get("content_hash") == old_line["content_hash"]):
                mapped, error = core.correspondence(old_line["words"], line["words"], selected["words"])
            else:
                mapped, error = None, "source changed without a complete correspondence chain; explicit rebinding required"
            if error:
                reasons.append({"code": "COPY_STALE", "reason": error})
            else:
                new_source = _source_at(line, mapped)
                if _plain_words(mapped) != selected["words"]:
                    # Index shifts are provenance, not a lexical mismatch.
                    if [w["text"] for w in mapped] != [w["text"] for w in selected["words"]]:
                        reasons.append({"code": "COPY_RAW_CHANGE", "reason": "raw source changed; normalized words equal"})
    display = quote["display"]
    actual = row.get("text")
    if actual != display["text"]:
        if isinstance(actual, str):
            now = {**display, "text": actual}
            equivalent = core.normalized(core.selected_display(now)) == core.normalized(core.selected_display(display))
        else:
            equivalent = False
        reasons.append({"code": "COPY_RAW_CHANGE" if equivalent else "COPY_STALE",
                        "reason": "raw display changed; normalized words equal" if equivalent else "displayed quotation changed"})
    lexical = any(r["code"] == "COPY_STALE" for r in reasons)
    unavailable = any(r["code"] == "COPY_SOURCE_UNAVAILABLE" for r in reasons)
    if lexical:
        reasons.append({"code": "COPY_SYNC", "reason": "declared quotation no longer has a resolved matching source run"})
    if line and not unavailable and not any(core.overlap(line["interval"], x) for x in row["display_support"]):
        reasons.append({"code": "COPY_SYNC", "reason": "source line no longer overlaps display support"})
    if row.get("limitations") and row.get("visibility") != "hidden":
        reasons.append({"code": "COPY_SOURCE_UNAVAILABLE", "reason": "display slot coverage unavailable"})
    if row.get("visibility") in {"hidden", "source-only"}:
        reasons.append({"code": "COPY_SYNC", "reason": "declared display is hidden"})
    return reasons, {"status": "unavailable" if unavailable else "stale" if lexical else "current",
                     "source": new_source or selected,
                     "reasons": [r for r in reasons if r["code"] in {"COPY_STALE", "COPY_SOURCE_UNAVAILABLE"}]}


def lint(checkout, *, before=None, evaluated_support=None):
    """Read-only findings, aggregated once per layer. No inferred intent is persisted."""
    current = source_context(checkout.bundle)
    inv = inventory(checkout, context=current, evaluated_support=evaluated_support)
    old_context = source_context(before.bundle) if before is not None else None
    old_rows = {(r["layer"], r["path"]): r for r in inventory(before)["rows"]} if before is not None else {}
    clips = {layer: clip for layer, clip in _layer_clips(checkout.bundle)}
    findings = {}
    old_layers = {r["layer"] for r in old_rows.values() if r["declarations"]}
    new_layers = {r["layer"] for r in inv["rows"]}
    for layer in sorted(old_layers - new_layers):
        findings[layer] = {"layer": layer, "support_evidence": "authored-support",
                           "reasons": [{"code": "COPY_STALE", "reason": "declared display layer removed"}]}
    for row in inv["rows"]:
        reasons = []
        clip = clips[row["layer"]]
        tracking = intent.copy_quote_tracking(clip)
        if not isinstance(tracking, Mapping):
            reasons.append({"code": "COPY_SOURCE_UNAVAILABLE", "reason": "invalid source correspondence evidence", "path": row["path"]})
            tracking = {}
        for quote in row["declarations"]:
            try:
                core.validate_declarations([quote])
                found, _ = _association(quote, row, current, before=old_context, tracking=tracking.get(core.digest(quote)))
            except (ValueError, TypeError, KeyError, ValidationError) as exc:
                found = [{"code": "COPY_SOURCE_UNAVAILABLE", "reason": f"invalid quote declaration: {exc}"}]
            reasons.extend({**r, "path": row["path"], "range": quote.get("display", {}).get("range") if isinstance(quote, Mapping) else None} for r in found)
        if not row["declarations"] and before is not None:
            previous = old_rows.get((row["layer"], row["path"]))
            if previous and previous["status"] == "unique" and previous["matches"][0]["nearby"]:
                selected = previous["matches"][0]["source"]
                quote = {"display": {"path": previous["path"], "text": previous["text"]}, "source": selected}
                found, _ = _association(quote, row, current, before=old_context)
                if any(r["code"] in {"COPY_SYNC", "COPY_STALE", "COPY_SOURCE_UNAVAILABLE"} for r in found):
                    reasons.append({"code": "COPY_SYNC_INFERRED", "reason": "previously unique overlapping exact candidate lost",
                                    "path": row["path"], "evidence": found})
        if reasons:
            findings.setdefault(row["layer"], {"layer": row["layer"], "support_evidence": row["support_evidence"], "reasons": []})["reasons"].extend(reasons)
    return list(findings.values())


def _layer_clips(bundle):
    for oid, sid, offset, document in _documents(bundle):
        for clip in document.get("clips") or []:
            yield _scoped(oid, clip), clip


def format_findings(findings):
    return [f"{f['layer']}: " + "; ".join(f"{r['code']} {r.get('path', '')}: {r['reason']}" for r in f["reasons"])
            for f in findings]


def after_script(checkout, before_bundle):
    """Timing-workspace hook: carry correspondence/invalidation evidence, never rebind.

    Evidence is namespaced and saved with the command so deleted runs cannot
    resurrect after another script edit or save/reload. Undo restores it as usual.
    """
    from astrid.sdk.timeline.checkout import Checkout
    before = Checkout(before_bundle)
    old_context, current = source_context(before.bundle), source_context(checkout.bundle)
    rows = inventory(checkout, context=current)["rows"]
    clips = dict(_layer_clips(checkout.bundle))
    states = {}
    for row in rows:
        clip = clips[row["layer"]]
        prior = intent.copy_quote_tracking(clip)
        for quote in row["declarations"]:
            try:
                core.validate_declarations([quote])
            except (ValueError, TypeError, KeyError, ValidationError):
                continue  # lint below reports malformed persisted declarations explicitly
            key = core.digest(quote)
            _, state = _association(quote, row, current, before=old_context, tracking=prior.get(key))
            states.setdefault(row["layer"], {})[key] = state
    findings = lint(checkout, before=before)
    for layer, state in states.items():
        intent.set_copy_quote_tracking(clips[layer], state)
    lines = format_findings(findings)
    checkout.notes.extend(lines)
    return lines


def declare_quotes(self, declarations):
    """Explicitly declare/reconfirm/rebind quote associations through EditCommand."""
    from astrid.sdk.timeline.commands import CommandError, scoped_clip
    try:
        declarations = core.validate_declarations(declarations)
    except (ValueError, TypeError, KeyError, ValidationError) as exc:
        raise CommandError(f"invalid copy quote: {exc}") from None
    layer = scoped_clip(self._tl, self)
    context = source_context(self._tl.bundle)
    rows = {r["path"]: r for r in inventory(self._tl, context=context)["rows"] if r["layer"] == layer}
    for quote in declarations:
        display, source = quote["display"], quote["source"]
        row = rows.get(display["path"])
        if row is None or row["text"] != display["text"] or row["limitations"]:
            raise CommandError("quote display expectation does not match a literal declared text slot")
        line = _line(context, source)
        if line is None or line["status"] != "available":
            raise CommandError("quote source expectation is unavailable")
        expected = _source_at(line, source["words"])
        if source != expected or source["words"] not in core.runs(core.selected_display(display), line):
            raise CommandError("quote source expectation does not match the current pinned word run")
    intent.set_copy_quotes(self.data, declarations)
    intent.set_copy_quote_tracking(self.data, None)
    return self


def copy_inventory(self, *, evaluated_support=None):
    return inventory(self, evaluated_support=evaluated_support)


def copy_lint(self, *, before=None, evaluated_support=None):
    # Use an actual stored checkout base when present; never fetch a live head.
    if before is None and any(isinstance(shot.get("base_internal_timeline"), Mapping)
                              for shot in self.bundle.get("shots", {}).values()):
        from astrid.sdk.timeline.checkout import Checkout
        from astrid.sdk.timeline_cuts import base_bundle
        before = Checkout(base_bundle(self.bundle))
    return lint(self, before=before, evaluated_support=evaluated_support)
