"""Human editable cut sheet for detached timeline checkouts.

The sheet is a projection of the working copy: timing gutters and narration
are read-only, while narration, cut, and layer rows carry edits back to that same copy.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Iterable

from astrid.core.timeline import clip_fields as cf
from astrid.core.timeline import moments as mo
from astrid.sdk.timeline_checkout import Checkout, TimelineEditError, intent

TRACK_ORDER = ["plate", "sprite", "fx", "type", "chrome"]
ELIDED = object()
CLAUSES = ("on", "until", "for")
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\\S+')


class SheetError(ValueError):
    """A sheet row could not be applied safely."""


def _canvas(tl):
    parent=tl.bundle.get("parent",{}); cfg=parent.get("config",{}) if isinstance(parent,dict) else {}
    c=cfg.get("theme_overrides",{}).get("visual",{}).get("canvas",{}) if isinstance(cfg,dict) else {}
    w,h=c.get("width",1920),c.get("height",1080)
    return f"{w}×{h}"


def _quote(text): return '"'+str(text).replace('"',"'")+'"'


def sheet_version(tl):
    raw = json.dumps(tl.document(), sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def render_sheet(tl: Checkout, *, start=None, end=None, banner=None, film=None, cuts: Iterable[str]|None=None, detail=False):
    groups = tl._cut_groups()
    if not groups:
        return "this timeline has no named cuts yet (import its intent first)\n"
    spans = tl._cut_spans()
    wanted = set(cuts) if cuts is not None else None
    picked = [g for g in groups if (wanted is not None and g["id"] in wanted) or
              (wanted is None and (end is None or g["start"] < end - 1e-6) and
               (start is None or (spans[g["id"]][1] or tl.duration) > start + 1e-6))]
    if wanted is not None:
        start = min((spans[g["id"]][0] for g in picked), default=0.0)
    notes = tl.cut_notes_all()
    version = sheet_version(tl)
    exported = tl.__dict__.setdefault("_exported", {})
    if version not in exported:
        exported[version] = copy.deepcopy(tl.bundle)
        for old in list(exported)[:-5]:
            exported.pop(old, None)
    out = [f"film {film or tl.bundle.get('project_id', '')} · {tl.bundle.get('timeline_id', '')} · {_canvas(tl)} · {tl.fps:g} fps · v {version}"]
    if banner:
        out.append(banner)
    last_picture = max((c for c in tl.clips() if not c.is_audio and c.track == "plate"),
                       key=lambda c: c.end, default=None)
    film_end = intent.until(last_picture.data) if last_picture else None
    if film_end and abs(last_picture.end - tl.duration) < 0.5 / tl.fps:
        out.append(f"end {film_end}")
    if getattr(tl, "music", "keep_end") == "plays_on":
        out.append("music plays-on")
    if start is not None or end is not None:
        ids = [g["id"] for g in picked]
        named = ", ".join(ids) if wanted is not None else f"{ids[0]}..{ids[-1]}" if ids else ""
        out.append(f"scope {named} ({len(ids)} cuts): apply changes only these cuts" if ids else "scope: no cuts")
    line_ids = []
    for group in picked:
        lo, hi = spans[group["id"]]
        for word in tl.words(between=(lo, hi if hi is not None else tl.duration + 1)):
            if word.segment not in line_ids:
                line_ids.append(word.segment)
    if line_ids:
        out += ["", "lines" + " " * 44 + "# the narration: everything hangs on it"]
        width = max(len(seg) for seg in line_ids)
        texts = {seg: _quote(tl.voice(seg).text) for seg in line_ids}
        text_width = max(map(len, texts.values()))
        for seg in line_ids:
            gap = tl.voice(seg).gap_after
            out.append((f"  {seg:<{width}}  {texts[seg]:<{text_width}}" +
                        (f"  gap {round(gap, 2):g}" if gap is not None else "")).rstrip())
    sound = [c for c in sound_clips(tl) if (end is None or c.start < end - 1e-6) and (start is None or c.end > start + 1e-6)]
    if sound:
        out += ["", "sound" + " " * 44 + "# under the film, in no cut: asset, for, volume= (the grid: --beats)"]
        out.extend(_align([_sound_row(tl, c) for c in sound]))
    orphans = tl.orphans()
    if orphans:
        out += ["", "orphans" + " " * 42 + "# moments whose words are gone: re-home or remove"]
        out.extend(f"  {orphan}" for orphan in orphans)
    chapter_names = {row.get("from"): row.get("name") for row in intent.chapters(tl.bundle)}
    order = [g["id"] for g in groups]
    carried = _carried(tl, groups)
    chapter = None
    for group in picked:
        lo, hi = spans[group["id"]]
        hi = hi if hi is not None else tl.duration
        picture = group["picture"]
        first = picture or min(group["clips"], key=lambda c: c.start)
        if chapter_names:
            name = next((chapter_names[cid] for cid in reversed(order[:order.index(group["id"])+1]) if cid in chapter_names), chapter)
        else:
            name = _chapter(tl, first.shot_id)
        if name and name != chapter:
            out += ["", f"# {name}"]
            chapter = name
        on = intent.on(picture.data) if picture else None
        head = f"{lo:6.2f} ┃ {group['id']:<5}" + (f" on {on}" if on else "")
        out.append(f"{head:<74}· {hi-lo:.2f} s")
        said = " ".join(w.text for w in tl.words(between=(lo, hi)))
        if said:
            out.append(f"       > {said}")
        layers = _ordered(group["clips"] + _loose_in(tl, lo, hi))
        rows = [_layer_row(tl, clip, lo, hi, is_picture=(picture is not None and clip.data is picture.data), detail=detail) for clip in layers]
        out.extend(_align(rows))
        for clip in carried.get(group["id"], []):
            out.append(f"         ↳ {clip.address} carries on over this cut (until {clip.end:.2f} s)")
        note = notes.get(group["id"], {})
        if note.get("why"):
            out.append(f"         why: {note['why']}")
        if note.get("hold") == "deliberate":
            out.append("         hold: deliberate")
    text = "\n".join(out) + "\n"
    from astrid.sdk.timeline.checkout import _retain_sheet

    _retain_sheet(tl, text)
    return text


def _loose_in(tl, lo, hi):
    return [c for c in tl.clips() if not c.is_audio and not intent.cut_of(c.data) and lo-1e-6 <= c.start < hi-1e-6]


def sound_clips(tl):
    return sorted((c for c in tl.clips() if c.is_audio and not intent.cut_of(c.data) and not intent.line(c.data)), key=lambda c: (c.start, c.id))


def _sound_row(tl, clip):
    bits = [f"volume={clip.data['volume']:g}"] if isinstance(clip.data.get("volume"), (int, float)) else []
    bits.extend(_look_bits(clip, skip={"volume"}))
    if clip.data.get("from") is not None and clip.data.get("to") is not None:
        bits.append(f"trim={clip.data['from']:g}..{clip.data['to']:g}")
    label = intent.beats_label(clip.data)
    if label:
        bits.append(f"~beats={label}")
    on = intent.on(clip.data)
    until = intent.until(clip.data)
    timing_parts = [f"on {on}"] if on else []
    if until:
        timing_parts.append(f"until {until}")
    else:
        timing_parts.append(f"for {_length_text(clip.duration, tl.fps)}")
    timing = " ".join(timing_parts)
    return [f"{clip.start:6.2f}", clip.track or "sfx", intent.layer_of(clip.data) or clip.id,
            _short(clip.element), clip.asset or "", " ".join(bits), timing]


def _carried(tl, groups):
    out = {}
    for group in groups:
        lo = group["start"]
        for earlier in groups:
            if earlier["start"] >= lo - 1e-6:
                continue
            for clip in earlier["clips"]:
                if not clip.is_audio and clip.start < lo-1e-6 and clip.end > lo+0.5/tl.fps and \
                        not (intent.sequence(clip.data) or ("", 0))[1] and earlier["picture"] is not None and clip.data is not earlier["picture"].data:
                    out.setdefault(group["id"], []).append(clip)
    return out


def _chapter(tl, shot_id):
    payload = (tl.bundle.get("shots", {}).get(shot_id) or {}).get("payload") or {}
    return str(payload.get("name") or shot_id)


def _ordered(clips):
    def rank(clip):
        if clip.is_audio:
            return (len(TRACK_ORDER)+1, clip.start, clip.id)
        return (TRACK_ORDER.index(clip.track) if clip.track in TRACK_ORDER else len(TRACK_ORDER), clip.start, clip.id)
    return sorted((c for c in clips if not ((intent.sequence(c.data) or ("", 0))[1] > 0)), key=rank)


def _short(element):
    return element[3:] if element.startswith("am-") else element


def _value(value):
    if isinstance(value, str):
        safe = re.fullmatch(r"[A-Za-z0-9_.#%:/+-]+", value) and value not in ("true", "false", "null") and not _is_number(value)
        return value if safe else json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_number(text):
    try:
        float(text)
        return True
    except ValueError:
        return False


def _layer_row(tl, clip, lo, hi, *, is_picture, detail=False):
    from astrid.sdk.timeline_checkout import formula_short, to_canvas
    gutter = f"{clip.start:6.2f}" if abs(clip.start-lo) > 0.5/tl.fps else ""
    name = intent.layer_of(clip.data) or clip.id
    params = dict(clip.params)
    text = params.pop("text", None) if clip.element == "am-type" else None
    what = clip.asset or ""
    if text is not None:
        what = (what + " " if what else "") + json.dumps(str(text), ensure_ascii=False)
    bits = []
    seq = intent.sequence(clip.data)
    if seq is not None:
        steps = [c for c in tl.clips() if (intent.sequence(c.data) or ("",))[0] == seq[0]]
        bits.append(f"~steps={len(steps)}")
        fit = intent.sequence_fit(clip.data)
        if fit and isinstance(fit.get("land"), str):
            bits.append(f"~fit-until={fit['land']}")
    formulas = intent.formulas(clip.data)
    for key, value in params.items():
        exact = formulas.get(f"params.{key}")
        if exact is not None:
            bits.append(f"{key}={formula_short(exact, clip.element)}")
        elif any(p.startswith((f"params.{key}[", f"params.{key}.")) for p in formulas):
            bits.append(f"{key}=ƒ…")
        else:
            shown = _value(to_canvas(clip.element, key, value))
            if detail and len(shown) > 28:
                shown = json.dumps(to_canvas(clip.element, key, value), ensure_ascii=False, separators=(",", ":"))
            elif len(shown) > 28:
                shown = "…"
            bits.append(f"{key}={shown}")
    bits.extend(_look_bits(clip, skip=set(params)))
    timing = []
    on = intent.on(clip.data)
    if on and not is_picture:
        timing.append(f"on {on}")
    until, length = intent.until(clip.data), intent.for_s(clip.data)
    if until:
        timing.append(f"until {until}")
    elif length is not None:
        timing.append(f"for {_length_text(length, tl.fps)}")
    track = clip.track if not clip.is_audio else (clip.track or "sfx")
    return [gutter, track, name, _short(clip.element), what, " ".join(bits), " ".join(timing)]


def _look_keys(element, keys):
    """The keys that are clip-level look fields for this element: declared by clip_fields, not by the element."""
    from astrid.sdk.timeline_address import declared_params
    declared = set(declared_params(element))
    return [k for k in keys if k in cf.FIELDS and k not in declared]


def _look_bits(clip, *, skip=()):
    """`opacity=0.6 entrance="fade 8f"`: the clip's set look fields, in their human form (what clip.look.describe shows)."""
    from astrid.sdk.timeline.look import Look
    shown = Look(clip).describe()
    if clip.is_audio:
        shown.update(cf.describe(clip.data, fps=clip._tl.fps))
    keys = _look_keys(clip.element, [k for k in shown if k not in skip])
    return [f"{k}={shown[k] if _is_number(shown[k]) else _value(shown[k])}" for k in keys]


def _apply_look(clip, fields):
    """Make the clip's look fields equal `fields` (a name omitted from a row that shows it is cleared)."""
    from astrid.sdk.timeline.look import Look
    audio = clip.is_audio
    fps = clip._tl.fps
    wanted = {k: v for k, v in fields.items() if v is not ELIDED}
    changes = {}
    for name, value in wanted.items():
        if name not in cf.FIELDS:
            continue
        stored = cf.coerce(name, value, fps=fps, audio=audio)[1]
        if audio and name in {"fade_in", "fade_out"}:
            candidate = copy.deepcopy(clip.data)
            cf.write(candidate, name, value, fps=fps, audio=True)
            if cf.describe(candidate, fps=fps).get(name) != cf.describe(clip.data, fps=fps).get(name):
                changes[name] = value
        elif stored != cf.read(clip.data, name, fps=fps, audio=audio):
            changes[name] = value
    for name in _look_keys(clip.element, list(Look(clip).describe())):
        if name not in fields:
            changes[name] = None
    if changes:
        clip.set_look(**changes)


def _length_text(seconds, fps):
    return mo.offset_text(seconds, fps).lstrip("+") or "0s"


def _align(rows):
    if not rows:
        return []
    order = (1, 2, 3, 4, 6, 5)
    widths = {i: max(len(row[i]) for row in rows) for i in order}
    return [(f"{row[0]:>6}   " + "  ".join(row[i].ljust(widths[i]) for i in order[:-1]) + "  " + row[5]).rstrip() for row in rows]


def parse_sheet(text: str) -> dict[str, Any]:
    """Parse narration, sound, cut headers, notes, and editable layer rows."""
    sheet: dict[str, Any] = {"lines": {}, "cuts": [], "has_lines_section": False}
    section = None
    cut = None
    fps = 30.0
    for number, raw in enumerate(text.splitlines(), 1):
        if raw.startswith("film "):
            match = re.search(r"([\d.]+) fps", raw)
            fps = float(match.group(1)) if match else fps
            version = re.search(r"· v ([0-9a-f]{8,})", raw)
            if version:
                sheet["version"] = version.group(1)
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        body = line.strip()
        if body.startswith("end "):
            sheet["film_end"] = _canon(body[4:], number)
            continue
        if body == "music plays-on":
            sheet["music"] = "plays_on"
            continue
        if raw.startswith(("film ", "WORKING COPY", "PUBLISHED", "next:", "scope ")) or body.startswith((">", "↳")) or raw.startswith("# "):
            continue
        if body in ("lines", "orphans", "sound", "clips", "tracks"):
            section, cut = body, None
            if body in ("clips", "tracks"):
                sheet.setdefault(body, [])
            if body == "lines":
                sheet["has_lines_section"] = True
            continue
        if "┃" in line:
            section = "cuts"
            try:
                cut = _parse_header(line, number)
            except SheetError as exc:
                raise SheetError(_with_text(str(exc), number, body)) from None
            sheet["cuts"].append(cut)
            continue
        if section == "lines" and cut is None:
            try:
                entry = _parse_line_entry(body, number)
            except SheetError as exc:
                raise SheetError(_with_text(str(exc), number, body)) from None
            sheet["lines"][entry["id"]] = entry
            continue
        if section == "orphans":
            continue
        if section in ("sound", "clips", "tracks") and cut is None:
            try:
                sheet.setdefault(section, []).append(_parse_layer(body, number, fps))
            except SheetError as exc:
                raise SheetError(_with_text(str(exc), number, body)) from None
            sheet[section][-1]["text_line"] = body
            continue
        if cut is None:
            raise SheetError(f"line {number}: {body[:60]!r} is outside any cut (a cut starts with a '┃ cNN' line)")
        if body.startswith("why:"):
            cut["why"] = body[4:].strip()
            continue
        if body.startswith("hold:"):
            value = body[5:].strip().lower()
            if value not in ("deliberate", "off"):
                raise SheetError(_with_text(f"line {number}: hold: takes deliberate (or off)", number, body))
            cut["hold"] = value
            continue
        try:
            cut["layers"].append(_parse_layer(body, number, fps))
        except SheetError as exc:
            raise SheetError(_with_text(str(exc), number, body)) from None
        cut["layers"][-1]["text_line"] = body
    return sheet


def _with_text(message, number, body):
    shown = body if len(body) <= 90 else body[:87] + "…"
    prefix = f"line {number}: "
    rest = message[len(prefix):] if message.startswith(prefix) else message
    return f"line {number}  «{shown}»\n  {rest}"


def _strip_comment(raw):
    out, quote, escape = [], False, False
    for i, char in enumerate(raw):
        if escape:
            escape = False
        elif char == "\\" and quote:
            escape = True
        elif char == '"':
            quote = not quote
        if char == "#" and not quote and i+1 < len(raw) and raw[i+1] == " " and (i == 0 or raw[i-1] == " "):
            if raw[:i].strip() == "":
                return ""
            return "".join(out)
        out.append(char)
    return "".join(out)


def _parse_header(line, number):
    rest = re.sub(r"\s·\s*[\d.]+\s*s\s*$", "", line.split("┃", 1)[1]).strip()
    tokens = rest.split(None, 1)
    if not tokens or not mo.CUT_ID_RE.match(tokens[0]):
        raise SheetError(f"line {number}: a cut line reads '┃ c21 on \"word\"'; got {line.strip()[:60]!r}")
    on = None
    if len(tokens) > 1:
        tail = tokens[1].strip()
        if not tail.startswith("on "):
            raise SheetError(f"line {number}: after the cut id write on <moment>, e.g. on \"viral\"")
        on = _canon(tail[3:], number)
    return {"id": tokens[0], "on": on, "why": None, "hold": None, "layers": [], "line": number}


def _parse_line_entry(body, number):
    match = re.match(r'^([^\s]+)\s+"(.*)"\s*(?:gap[ =]\s*([\d.]+))?\s*$', body)
    if not match:
        raise SheetError(f"line {number}: a narration line reads  n21  \"text\"  gap 1.25")
    return {"id": match.group(1), "text": match.group(2), "gap": float(match.group(3)) if match.group(3) else None, "line": number}


def _tokens(body):
    out, buf, quote, depth, escape = [], [], False, 0, False
    for char in body:
        if escape:
            buf.append(char)
            escape = False
            continue
        if char == "\\" and quote:
            buf.append(char)
            escape = True
            continue
        if char == '"':
            quote = not quote
        elif not quote and char in "[{(":
            depth += 1
        elif not quote and char in "]})":
            depth = max(0, depth - 1)
        if char.isspace() and not quote and depth == 0:
            if buf:
                out.append("".join(buf))
                buf = []
            continue
        buf.append(char)
    if buf:
        out.append("".join(buf))
    return out


def _parse_layer(body, number, fps=30.0):
    tokens = _tokens(body)
    gutter = None
    if tokens and re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", tokens[0]):
        gutter, tokens = float(tokens[0]), tokens[1:]
    if len(tokens) < 3:
        raise SheetError(f"line {number}: a layer reads  track name element [ASSET] [\"text\"] [k=v …] [on …] [until …|for …]")
    layer = {"track": tokens[0], "name": tokens[1], "element": tokens[2], "asset": None, "text": None,
             "params": {}, "on": None, "until": None, "for": None, "trim": None, "slip": None,
             "line": number, "at": gutter}
    current, buf = None, []
    def close_clause():
        if current is None:
            return
        value = " ".join(buf).strip()
        if not value:
            raise SheetError(f"line {number}: {current} needs a value (e.g. {current} \"Astrid\")")
        if current == "for":
            layer["for"] = _read_seconds(value, number, fps)
        else:
            layer[current] = _canon(value, number)
    for token in tokens[3:]:
        if token in CLAUSES:
            close_clause()
            current, buf = token, []
            continue
        if token.startswith("trim="):
            match = re.fullmatch(r"trim=([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\.\."
                                 r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)", token)
            if not match:
                raise SheetError(f"line {number}: trim takes a source range like trim=3..5")
            layer["trim"] = (float(match.group(1)), float(match.group(2)))
            continue
        if token.startswith("slip="):
            try:
                layer["slip"] = float(token[5:])
            except ValueError:
                raise SheetError(f"line {number}: slip takes seconds like slip=2") from None
            continue
        is_param = "=" in token and not token.startswith('"') and not (layer["asset"] is None and _names_media(token, layer["element"]))
        if current is not None and not is_param and not token.startswith("~"):
            buf.append(token)
            continue
        close_clause()
        current, buf = None, []
        if token.startswith("~"):
            continue
        if is_param:
            key, value = token.split("=", 1)
            layer["params"][key] = _read_value(value, number)
        elif token.startswith('"'):
            layer["text"] = json.loads(token)
        elif layer["asset"] is None:
            layer["asset"] = token
        else:
            raise SheetError(f"line {number}: did not understand {token!r} (params are k=v; text goes in quotes)")
    close_clause()
    return layer


def _names_media(token, element):
    """KEY=HANDLE in the asset column: new media, named KEY (swap-asset's form). KEY must not be a param of the element."""
    from astrid.sdk.media_handles import is_media_handle
    from astrid.sdk.timeline_address import element_schema

    key, _, ref = token.partition("=")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key) or not is_media_handle(ref):
        return False
    return key not in (element_schema(_element(element)).get("properties") or {})


def _read_value(value, number):
    if value in ("…", "...", "ƒ") or value.endswith("…"):
        return ELIDED
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _read_seconds(value, number, fps=30.0):
    match = re.fullmatch(r"([\d.]+)\s*(s|f)?", value.strip())
    if not match:
        raise SheetError(f"line {number}: for takes a length like 1.9s or 12f; got {value!r}")
    seconds = float(match.group(1))
    return seconds / fps if match.group(2) == "f" else seconds


def _canon(text, number):
    try:
        return mo.format_moment(mo.parse(text))
    except mo.MomentError as exc:
        raise SheetError(f"line {number}: {exc}") from None


def moment_range(tl: Checkout, value: str):
    """Resolve a range endpoint through the same canonical address API as edits."""
    from astrid.sdk.timeline_address import AddressError, resolve

    try:
        target = resolve(tl, value, prefer="time")
    except AddressError as exc:
        raise SheetError(str(exc)) from None
    if target.kind in ("time", "word"):
        return target.start, target.start + 1e-3
    return target.start, target.end


def range_cuts(tl: Any, text: str) -> list[str]:
    """Return named cuts overlapping a comma-separated list of addresses or ranges."""
    spans = tl._cut_spans()
    order = [g["id"] for g in tl._cut_groups()]
    chosen: set[str] = set()
    for part in re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', text):
        part = part.strip()
        if not part:
            continue
        if part in spans:
            chosen.add(part)
            continue
        lo, hi = moment_range(tl, part)
        for cut_id in order:
            start, end = spans[cut_id]
            end = tl.duration if end is None else end
            if (hi is None or start < hi - 1e-6) and (lo is None or end > lo + 1e-6):
                chosen.add(cut_id)
    return [cut_id for cut_id in order if cut_id in chosen]


class NativeSheet:
    """Pinned native adapter using the cut sheet's row grammar and alignment.

    The legacy named-cut view has neither scoped row bindings nor a publication
    pin, and Checkout may flatten/migrate its input. This opt-in projection reads
    the untouched authoring bundle instead. Retain this object beside edited
    ``text``, or use ``export``/``load`` to retain one export in its scoped
    session outbox. ``# ref`` comments bind stable owners and IDs. Start gutters,
    parent track labels, source trim, source-second hold and built-in literal params are
    editable. Unquoted ``key=clear`` removes presence; null and "" are values.
    Asset and incoming transition support explicit clear only. Omitting a cell
    never clears it. Omitting a bound parent clip row emits the existing
    ``clip.delete`` command; track and occurrence-row omissions remain refused.
    Built-in params retain the sheet's canvas units.

    ``basis`` is TimelineSession's revision/publication pair for the input bundle.
    ``apply`` previews locally by default. Its commit seam uses the session's
    atomic commit_many, preserving original capture on retries; it never
    invokes Checkout edits, save, or its journal.
    """

    def __init__(self, bundle, *, basis, clip=None, occurrence=None):
        from astrid.sdk.timeline._command_state import State
        from astrid.sdk.timeline.capture import number, occurrence_start
        from astrid.sdk.timeline.sync import _pin
        from astrid.sdk.timeline.units import to_canvas

        self._basis = _pin(basis)
        if bundle.get("base_parent", {}).get("revision_id") != self._basis["revision_id"]:
            raise SheetError("sheet basis does not match the bundle revision")
        self._bundle = copy.deepcopy(bundle)
        self._selection = {key: value for key, value in (("clip", clip), ("occurrence", occurrence)) if value is not None}
        state = State(self._bundle)
        header = json.dumps([bundle["project_id"], bundle["timeline_id"], self._basis], sort_keys=True)
        out = [f"film {header}", "clips"]
        rows, refs = [], []
        owners = [("parent", 0)] + [
            ("occurrence:" + row["occurrence_id"], occurrence_start(row))
            for row in bundle["placements"]
        ]
        clip_id = clip
        for owner, offset in owners:
            if occurrence is not None and owner != "occurrence:" + occurrence:
                continue
            if clip_id is not None and occurrence is None and owner != "parent":
                continue
            for clip in state.collection(owner, "clip"):
                if clip_id is not None and clip["id"] != clip_id:
                    continue
                start = (number(clip["at"], "clip start") if "at" in clip else
                         number(clip.get("at_ms", 0), "clip start") / 1000) + offset
                selected = clip.get("elementRef") or clip.get("element_ref") or {}
                element = clip.get("clipType") or clip.get("clip_type") or "media"
                params = clip.get("params") or {}
                props = {key: value if selected.get("packId") else to_canvas(element, key, value)
                         for key, value in params.items() if re.fullmatch(r"[A-Za-z_][\w.-]*", key)
                         and key not in {"hold", "asset", "transition", "trim", "slip"}}
                app = clip.get("app") if isinstance(clip.get("app"), dict) else {}
                astrid = app.get("astrid") if isinstance(app.get("astrid"), dict) else {}
                anchors = astrid.get("word_anchors") if isinstance(astrid.get("word_anchors"), dict) else {}
                if "on" in anchors:
                    props["on"] = {"field": "on", "declaration": astrid.get("on"),
                                   "choice": copy.deepcopy(anchors["on"])}
                for key in ("hold", "transition"):
                    if key in clip:
                        props[key] = clip[key]
                window = ("trim=" + repr(clip["from"]) + ".." + repr(clip["to"])
                          if all(type(clip.get(key)) in (int, float) for key in ("from", "to")) else "")
                rows.append([repr(start), _value(clip.get("track") or "visual"), _value(clip["id"]),
                             _value(_short(clip.get("clipType") or clip.get("clip_type") or "media")),
                             _value(clip["asset"]) if "asset" in clip else "",
                             " ".join(key + "=" + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                                      for key, value in props.items()), window])
                refs.append(owner + "/clip:" + clip["id"])
        if self._selection and not rows:
            raise SheetError("sheet selection has no matching authored clips")
        out.extend(line + " # ref " + json.dumps(ref) for line, ref in zip(_align(rows), refs))
        out.append("tracks")
        rows, refs = [], []
        for track in ([] if self._selection else state.collection("parent", "track")):
            label = json.dumps(track["label"], ensure_ascii=False) if "label" in track else "clear"
            rows.append(["", _value(track["id"]), "label", "track", "", "label=" + label, ""])
            refs.append("parent/track:" + track["id"])
        out.extend(line + " # ref " + json.dumps(ref) for line, ref in zip(_align(rows), refs))
        self.text = "\n".join(out) + "\n"
        self._rows, self._frame = self._read(self.text)

    @classmethod
    def export(cls, session, *, clip=None, occurrence=None):
        """Durably retain the original bundle and pin before emitting editable text.

        One export per scoped outbox; exporting again replaces that artifact.
        Pending intent must be acknowledged first so its retry basis survives.
        """
        with session.outbox.locked():
            state = session._state()
            if state["pending"] or state["flight"] is not None:
                raise SheetError("flush original pending intent before exporting another sheet")
            sheet = cls(session._capture_bundle(state), basis=state["base"], clip=clip, occurrence=occurrence)
            state["sheet"] = {"bundle": sheet._bundle, "basis": sheet._basis}
            if sheet._selection:
                state["sheet"]["selection"] = sheet._selection
            session.outbox.write(state)
        return sheet

    @classmethod
    def load(cls, session):
        """Reconstruct bindings from the retained original, never the current head."""
        with session.outbox.locked():
            artifact = session._state().get("sheet")
            if artifact is None:
                raise SheetError("no original native sheet export in this scoped outbox")
            return cls(artifact["bundle"], basis=artifact["basis"], **artifact.get("selection", {}))

    @staticmethod
    def _read(text):
        parsed = parse_sheet(text)
        lines = text.splitlines()
        rows, used = {}, set()
        for section in ("clips", "tracks"):
            for row in parsed.get(section, []):
                raw = lines[row["line"] - 1]
                body, separator, binding = raw.rpartition(" # ref ")
                try:
                    ref = json.loads(binding) if separator else None
                except ValueError:
                    raise SheetError("invalid sheet row binding") from None
                if not isinstance(ref, str) or ref in rows:
                    raise SheetError("missing or duplicate sheet row binding")
                rows[ref] = (section, _tokens(body.strip()), row)
                used.add(row["line"] - 1)
        return rows, [line for i, line in enumerate(lines) if i not in used]

    def preview(self, text, *, session=None):
        """Return native commands, records and preview bundle without writing anything."""
        from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
        from astrid.sdk.timeline._command_state import State, equal
        from astrid.sdk.timeline._scalar_commands import capture_clip_set_params, capture_track_set
        from astrid.sdk.timeline._trim_commands import capture_clip_trim
        from astrid.sdk.timeline.capture import capture_clip_delete, capture_move
        from astrid.sdk.timeline.commands import Plan, execute
        from astrid.sdk.timeline.native_source_inputs import materialize_transcript_inputs
        from astrid.sdk.timeline.native_word_anchor import capture_anchor
        from astrid.sdk.timeline.units import from_canvas

        rows, frame = self._read(text)
        missing = self._rows.keys() - rows.keys()
        extra = rows.keys() - self._rows.keys()
        if frame != self._frame or extra:
            raise SheetError("sheet structure/basis is read-only; additions and removals are unsupported")
        for ref in missing:
            section, _, _ = self._rows[ref]
            if section != "clips" or not ref.startswith("parent/clip:"):
                raise SheetError(f"only an addressed parent clip row may be omitted: {ref}")
        anchor_commands = []
        for ref, (section, _, _) in self._rows.items():
            if ref in missing or section != "clips":
                continue
            _, _, row = rows[ref]
            carrier = row["params"].get("on")
            if isinstance(carrier, dict):
                anchor_commands.append({"schema_version": 1, "op": "clip.anchor", "version": "1",
                                        "select": ref, "args": copy.deepcopy(carrier)})
        if anchor_commands and session is not None:
            materialize_transcript_inputs(
                self._bundle, anchor_commands,
                client=session.client.timelines._client, project=session.project_id)
        commands = []
        working = self._bundle
        capturers = {"clip.delete": capture_clip_delete, "clip.move": capture_move,
                     "clip.trim": capture_clip_trim,
                     "clip.set": capture_clip_set, "clip.set-params": capture_clip_set_params,
                     "track.set": capture_track_set}

        def emit(op, ref, args):
            nonlocal working
            command = capturers[op](working, ref, **args)
            commands.append(command)
            working = execute(working, [command]).bundle

        def emit_anchor(ref, carrier):
            nonlocal working
            if carrier is None:
                command = capture_anchor(working, ref, "on", None, None)
            elif isinstance(carrier, dict) and set(carrier) == {"field", "declaration", "choice"}:
                if carrier["field"] != "on":
                    raise SheetError(f"word anchor at {ref} must target field=on")
                command = capture_anchor(working, ref, **carrier)
            else:
                raise SheetError(f"word anchor at {ref} requires the exact clip.anchor carrier")
            commands.append(command)
            working = execute(working, [command]).bundle

        for ref, (section, before, original) in self._rows.items():
            if ref in missing:
                emit("clip.delete", ref, {})
                continue
            current_section, after, row = rows[ref]
            if current_section != section:
                raise SheetError("sheet row ownership is read-only")
            if before == after:
                continue
            if section == "clips":
                # Compare every token, including clauses the legacy parser ignores.
                def cells(tokens):
                    return {token.split("=", 1)[0]: token.split("=", 1)[1] for token in tokens[4:]
                            if re.match(r"^[A-Za-z_][\w.-]*=", token)}
                left, right = cells(before), cells(after)
                def fixed(tokens):
                    return [token for token in tokens[1:]
                            if not re.match(r"^[A-Za-z_][\w.-]*=", token)]
                allowed = set(original["params"]) | {"asset", "hold", "transition", "trim"}
                if (fixed(before) != fixed(after) or not left.keys() <= right.keys()
                        or not right.keys() <= allowed
                        or len(right) != len(row["params"]) + (row["trim"] is not None) or row["at"] is None
                        or sum(bool(re.match(r"^[A-Za-z_][\w.-]*=", token)) for token in after[4:]) != len(right)):
                    raise SheetError(f"unsupported row edit at {ref}; keep cells and use explicit key=clear")
                if row["at"] != original["at"]:
                    emit("clip.move", ref, {"on": _read_value(after[0], row["line"])})
                if row["trim"] != original["trim"]:
                    emit("clip.trim", ref, dict(zip(("in_s", "out_s"), row["trim"])))
                if "on" in right and left.get("on") != right["on"]:
                    emit_anchor(ref, row["params"].get("on"))
                clip = State(working).entity(ref)
                selected = clip.get("elementRef") or clip.get("element_ref") or {}
                for op, keys in (("clip.set", {"hold", "asset", "transition"}),
                                 ("clip.set-params", set(right) - {"hold", "asset", "transition", "trim", "on"})):
                    values, clear = {}, []
                    for key in sorted(keys & right.keys()):
                        if left.get(key) == right[key] or (key in original["params"]
                                and right[key] != "clear" and left[key] != "clear"
                                and equal(original["params"][key], row["params"][key])):
                            continue
                        if op == "clip.set-params" and selected.get("packId"):
                            raise SheetError(f"unsupported parameter {key} at {ref}; selected pack/opaque editing requires exact inputs")
                        if op == "clip.set-params" and intent.formulas(clip):
                            raise SheetError(f"unsupported formula-bound parameters at {ref}")
                        if right[key] == "clear":
                            clear.append(key)
                        else:
                            value = row["params"][key]
                            raw_value = right[key]
                            expression = (isinstance(value, str) and
                                          value.startswith(("ƒ", "moment(", "formula(")) and
                                          not (op == "clip.set-params" and raw_value.startswith('"')))
                            if (value is ELIDED or expression or
                                    (op != "clip.set-params" and isinstance(value, (dict, list)))):
                                raise SheetError(f"unsupported expression at {ref}: {key}")
                            values[key] = (from_canvas(clip.get("clipType") or clip.get("clip_type") or "media", key, value)
                                           if op == "clip.set-params" else value)
                    if values or clear:
                        emit(op, ref, {"values": values, "clear": clear})
            elif section == "tracks" and before[:-1] == after[:-1] and after[-1].startswith("label="):
                clear = after[-1] == "label=clear"
                if clear == (before[-1] == "label=clear") and equal(row["params"], original["params"]):
                    continue
                emit("track.set", ref, {"values": {} if clear else row["params"],
                                       "clear": ["label"] if clear else []})
            else:
                raise SheetError(f"unsupported cell edit at {ref}; parent tracks support label only")
        return execute(self._bundle, commands) if commands else Plan(copy.deepcopy(self._bundle), [])

    def apply(self, text, *, session=None, dry_run=True, idempotency_key=None):
        """Capture against the export once, then use the normal atomic native batch."""
        from astrid.sdk.timeline._command_state import equal

        def authored(bundle):
            bundle = copy.deepcopy(bundle)
            mapping = bundle.get("source_mapping")
            if isinstance(mapping, dict):
                mapping["pinned_documents"] = []
            return bundle

        plan = self.preview(text, session=session)
        if dry_run or (not plan.commands and session is None):
            return plan
        if session is None:
            raise SheetError("sheet commits require a TimelineSession")
        with session.outbox.locked():
            state = session._state()
            if (session.project_id != self._bundle["project_id"]
                    or session.timeline_id != self._bundle["timeline_id"] or state["base"] != self._basis):
                raise SheetError("sheet and session must share the original project, timeline and basis")
            prefix = state["pending"][:state["flight"]["count"]] if state["flight"] else state["pending"]
            if prefix and not equal(prefix, plan.commands):
                raise SheetError("flush original pending intent before applying another sheet")
        if not plan.commands:
            return plan
        if not prefix and not equal(authored(session._capture_bundle(state)), authored(self._bundle)):
            raise SheetError("sheet bundle differs from its pinned session basis")
        steps = [{"id": f"sheet-{index + 1}", "label": "Apply sheet", "intent": "",
                  "tags": ["sheet"], "command_ordinals": [index], "patch": []}
                 for index, _command in enumerate(plan.commands)]
        return session.commit_many(plan.commands, idempotency_key=idempotency_key, via="sheet",
                                   base=self._basis, label="Apply sheet", steps=steps,
                                   _omit_empty_reads=True)


def apply_sheet(tl: Checkout, text: str, *, client: Any = None) -> list[str]:
    """Apply a full sheet or fragment atomically and return the checkout's change summary.

    A sheet exported from an earlier version merges three-way (what the sheet changed since it was exported,
    onto what is here now): an edit made after the export is never reverted."""
    parsed = parse_sheet(text)
    if "clips" in parsed or "tracks" in parsed:
        raise SheetError("native sheet rows require NativeSheet and TimelineSession")
    from astrid.sdk.timeline.checkout import _stage_sheet

    retained = _stage_sheet(tl, text, parsed, client=client)
    if retained is not None:
        return retained
    before = Checkout(copy.deepcopy(tl.bundle))
    if parsed.get("version") and parsed["version"] != sheet_version(tl):
        base = find_version(tl, parsed["version"], client=client)
        if base is None:
            raise SheetError(f"this sheet was exported from version {parsed['version']}, which this working copy no longer "
                             f"has (it is now {sheet_version(tl)}): re-export it (timelines show TL --as sheet) and make "
                             "your change again, so nothing edited since is undone")
        parsed = merge_sheet(parsed, base, tl)
    try:
        return _apply_parsed(tl, parsed, before)
    except Exception:
        tl.bundle = before.bundle
        tl._refresh()
        raise


def _duration_sheet(sheet, artifact, parsed):
    """Translate existing static layer durations and retained literal parameter cells."""
    from astrid.sdk.timeline._command_state import State, equal

    def refuse():
        raise SheetError("unsupported retained sheet edit; supported fields are am-type text, literal parameters, and static duration")

    original = parse_sheet(artifact["text"])
    edited = copy.deepcopy(parsed, {id(ELIDED): ELIDED})
    left = {(cut, row["name"]): row for cut, row in iter_layers(original)}
    right = {(cut, row["name"]): row for cut, row in iter_layers(edited)}
    if left.keys() != right.keys() or len(left) != sum(len(c["layers"]) for c in original["cuts"]):
        refuse()
    text = sheet.text
    for key, row in right.items():
        before = left[key]
        before_params = before.get("params") or {}
        after_params = row.get("params") or {}
        field_changes = []
        for field in (set(before_params) | set(after_params)) - {"text"}:
            before_has = field in before_params
            after_has = field in after_params
            if before_has != after_has or (before_has and after_has and
                                           not equal(before_params[field], after_params[field])):
                field_changes.append((field, before_has, after_has))
        if equal(before["for"], row["for"]) and equal(before["text"], row["text"]) and not field_changes:
            continue
        ref = artifact["bindings"].get(".".join(key))
        if ref is None:
            refuse()
        clip = State(sheet._bundle).entity(ref)
        selected = clip.get("elementRef") or clip.get("element_ref") or {}
        if not equal(before["for"], row["for"]):
            if before["for"] is None or row["for"] is None:
                refuse()
            if clip.get("clipType") != "am-type" or type(clip.get("hold")) not in (int, float) or row["until"] or row["on"]:
                refuse()
            value = row["for"] * (clip.get("speed") or 1)
            suffix = " # ref " + json.dumps(ref)
            text = "\n".join(re.sub(r"(?<!\S)hold=\S+", "hold=" + repr(value), line)
                             if line.endswith(suffix) else line for line in text.splitlines()) + "\n"
            row["for"] = before["for"]
        if not equal(before["text"], row["text"]):
            if (before["text"] is None or not isinstance(row["text"], str)
                    or clip.get("clipType") != "am-type" or row["until"] or row["on"]):
                refuse()
            old_token = "text=" + json.dumps(before["text"], ensure_ascii=False)
            new_token = "text=" + json.dumps(row["text"], ensure_ascii=False)
            suffix = " # ref " + json.dumps(ref)
            replaced = []
            for line in text.splitlines():
                if not line.endswith(suffix):
                    replaced.append(line)
                    continue
                body = line[:-len(suffix)]
                if _tokens(body.strip()).count(old_token) != 1:
                    refuse()
                body, count = re.subn(rf"(?<!\S){re.escape(old_token)}(?=\s|$)",
                                      lambda _: new_token, body, count=1)
                if count != 1:
                    refuse()
                replaced.append(body + suffix)
            text = "\n".join(replaced) + "\n"
            row["text"] = before["text"]
        for field, before_has, after_has in field_changes:
            if (not before_has or not after_has or clip.get("clipType") != "am-type"
                    or selected.get("packId")):
                refuse()
            value = after_params[field]
            if value is ELIDED:
                refuse()
            raw_value = next((token for token in _tokens(row.get("text_line", ""))
                              if token.startswith(field + "=")), None)
            if raw_value is None:
                refuse()
            old_token = field + "=" + json.dumps(before_params[field], ensure_ascii=False, separators=(",", ":"))
            new_token = (field + "=clear" if raw_value == field + "=clear" else
                         field + "=" + json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            suffix = " # ref " + json.dumps(ref)
            replaced = []
            for line in text.splitlines():
                if not line.endswith(suffix):
                    replaced.append(line)
                    continue
                body = line[:-len(suffix)]
                if _tokens(body.strip()).count(old_token) != 1:
                    refuse()
                body, count = re.subn(rf"(?<!\S){re.escape(old_token)}(?=\s|$)",
                                      lambda _: new_token, body, count=1)
                if count != 1:
                    refuse()
                replaced.append(body + suffix)
            text = "\n".join(replaced) + "\n"
            row["params"][field] = before_params[field]
    def semantic(value):
        if isinstance(value, dict):
            return {k: semantic(v) for k, v in value.items() if k not in {"line", "text_line"}}
        if isinstance(value, list):
            return [semantic(v) for v in value]
        return "…" if value is ELIDED else value
    if not equal(semantic(original), semantic(edited)):
        refuse()
    return text


def find_version(tl: Any, version: str, *, client: Any = None) -> Any:
    """Find a sheet basis in unsent drafts or retained Runtime revisions.

    Local snapshots belong only to this published base. With a client, older
    accepted bases are read from Runtime, never from the local undo stack.
    """
    from astrid.sdk.timeline_checkout import Checkout, _history_dir, _read_step
    from astrid.sdk.timeline_cuts import base_bundle

    if sheet_version(tl) == version:
        return tl
    kept = (getattr(tl, "_exported", None) or {}).get(version)
    if kept is not None and kept.get("base_parent") == tl.bundle.get("base_parent"):
        return Checkout(copy.deepcopy(kept))
    candidates: list[Any] = []
    path = getattr(tl, "path", None)
    if path:
        from pathlib import Path

        target = Path(path)
        if target.is_file():
            candidates.append(lambda: target.read_text(encoding="utf-8"))
        for step in sorted(_history_dir(target).glob("*.json"), reverse=True):
            candidates.append(lambda step=step: _read_step(step)[1])
    for read in candidates:
        try:
            other = Checkout({})
            other.bundle = other._absorb(json.loads(read()))
            other = Checkout(other.bundle)
        except Exception:  # noqa: BLE001 - an unreadable step is skipped
            continue
        if other.bundle.get("base_parent") == tl.bundle.get("base_parent") and sheet_version(other) == version:
            return other
    try:
        base = Checkout(base_bundle(tl.document()))
        if sheet_version(base) == version:
            return base
    except Exception:  # noqa: BLE001
        pass
    if client is not None:
        cursor = None
        while True:
            result = client.timelines.history(tl.bundle["project_id"], tl.bundle["timeline_id"],
                                              limit=100, cursor=cursor)
            if not result.ok:
                raise SheetError(f"could not read retained sheet basis: {result.error}")
            for row in result.data["items"]:
                other = Checkout.open(tl.bundle["project_id"], tl.bundle["timeline_id"],
                                      revision_id=row["revision_id"],
                                      publication_id=row["publication_id"], client=client)
                if sheet_version(other) == version:
                    return other
            cursor = result.data.get("next_cursor")
            if cursor is None:
                break
    return None


def _norm_layer(layer: dict[str, Any] | None) -> str | None:
    if layer is None:
        return None
    keep = {k: v for k, v in layer.items() if k not in ("line", "text_line", "at")}
    keep["params"] = {k: ("…" if v is ELIDED else v) for k, v in (keep.get("params") or {}).items()}
    return json.dumps(keep, sort_keys=True, default=str)


def merge_sheet(sheet: dict[str, Any], base_tl: Any, tl: Any) -> dict[str, Any]:
    """Three-way, line by line: a line the sheet left as exported takes what is here now; a line the sheet
    changed applies; both changed (differently) is a clash, named, never a silent choice."""
    base, now = parse_sheet(render_sheet(base_tl)), parse_sheet(render_sheet(tl))
    base_cuts, now_cuts = {c["id"]: c for c in base["cuts"]}, {c["id"]: c for c in now["cuts"]}
    clashes: list[str] = []
    merged: dict[str, Any] = {"lines": {}, "cuts": [], "version": sheet.get("version")}

    def pick(what: str, b: Any, s: Any, c: Any) -> Any:
        if s == b:
            return c
        if c == b or s == c:
            return s
        clashes.append(what)
        return s

    for seg, entry in sheet["lines"].items():
        b, c = base["lines"].get(seg), now["lines"].get(seg)
        key = lambda e: None if e is None else (e.get("text"), e.get("gap"))
        chosen = pick(f"line {seg} (narration)", key(b), key(entry), key(c))
        if chosen is not None and chosen != key(c):
            merged["lines"][seg] = {**entry, "text": chosen[0], "gap": chosen[1]}
    for field in ("film_end", "music"):
        selected = pick(field.replace("_", " "), base.get(field), sheet.get(field), now.get(field))
        if selected is not None and selected != now.get(field):
            merged[field] = selected
    if sheet.get("sound"):
        b_sound = {r["name"]: r for r in base.get("sound") or []}
        c_sound = {r["name"]: r for r in now.get("sound") or []}
        rows = []
        for row in sheet["sound"]:
            b, c = _norm_layer(b_sound.get(row["name"])), _norm_layer(c_sound.get(row["name"]))
            s = _norm_layer(row)
            if s == b:
                continue
            if c != b and s != c:
                clashes.append(f"sound {row['name']} (line {row['line']})")
            rows.append(row)
        if rows:
            merged["sound"] = rows
    for cut in sheet["cuts"]:
        b_cut, c_cut = base_cuts.get(cut["id"]), now_cuts.get(cut["id"])
        if b_cut is None:  # new in the sheet: as written
            merged["cuts"].append(cut)
            continue
        if c_cut is None:
            clashes.append(f"{cut['id']} (line {cut['line']}): it is gone since the sheet was exported")
            continue
        out = dict(c_cut)
        for field in ("on", "why", "hold"):
            value = pick(f"{cut['id']} {field} (line {cut['line']})", b_cut.get(field), cut.get(field), c_cut.get(field))
            out[field] = value
        b_l = {l["name"]: l for l in b_cut["layers"]}
        s_l = {l["name"]: l for l in cut["layers"]}
        c_l = {l["name"]: l for l in c_cut["layers"]}
        layers = []
        for name in list(dict.fromkeys(list(c_l) + list(s_l) + list(b_l))):
            b, s, c = _norm_layer(b_l.get(name)), _norm_layer(s_l.get(name)), _norm_layer(c_l.get(name))
            line = (s_l.get(name) or {}).get("line") or (b_l.get(name) or {}).get("line")
            if s == b:
                chosen = c_l.get(name)
            elif c == b or s == c:
                chosen = s_l.get(name)
            else:
                clashes.append(f"{cut['id']}.{name} (line {line})")
                chosen = s_l.get(name)
            if chosen is not None:
                layers.append(chosen)
        out["layers"] = layers
        merged["cuts"].append(out)
    if clashes:
        raise SheetError("not applied: since this sheet was exported, these were changed both here and in the sheet: "
                         + "; ".join(clashes[:10]) + (f"; … {len(clashes) - 10} more" if len(clashes) > 10 else "")
                         + ". Re-export (timelines show TL --as sheet) and make your change again.")
    return merged


def _apply_parsed(tl, sheet, before):
    from astrid.sdk.timeline_checkout import TimelineEditError, describe_changes
    reflow = False
    # The lines section is a complete projection for the selected cuts. If a
    # line shown in that scope is omitted, treat it like an omitted layer in a
    # complete cut section and remove it through the normal journaled edit API.
    line_removals = set()
    if sheet.get("has_lines_section") and sheet["cuts"]:
        spans = tl._cut_spans()
        scoped_segments = set()
        for cut in sheet["cuts"]:
            if cut["id"] not in spans:
                continue
            lo, hi = spans[cut["id"]]
            hi = hi if hi is not None else tl.duration + 1
            scoped_segments.update(word.segment for word in tl.words(between=(lo, hi)))
        line_removals = scoped_segments.difference(sheet["lines"])
    if sheet.get("music"):
        tl.music = sheet["music"]
    if sheet.get("sound"):
        _apply_sound(tl, sheet["sound"])
    for segment, entry in sheet["lines"].items():
        voice = tl.voice(segment)
        if entry["text"] and entry["text"] != voice.text.replace('"', "'"):
            intent.set_line_text(voice.clips[0].data, entry["text"])
        if entry["gap"] is not None and (voice.gap_after is None or abs(voice.gap_after-entry["gap"]) > 0.0051):
            if entry["gap"] == 0:
                voices = tl.lines()
                index = next((i for i, item in enumerate(voices) if item.segment == segment), -1)
                if index >= 0 and index + 1 < len(voices) and voices[index + 1].words:
                    tl.close_gap(before=voices[index + 1].words[0])
                    continue
            voice.set_gap_after(entry["gap"], reflow=False)
            reflow = True
    for cut in sheet["cuts"]:
        for layer in cut["layers"]:
            if layer.get("trim") is not None or layer.get("slip") is not None:
                raise SheetError(_with_text(f"line {layer['line']}: trim= and slip= apply only to sound rows",
                                            layer["line"], layer.get("text_line", "")))
    groups = {g["id"]: g for g in tl._cut_groups()}
    # Add new cuts before processing omissions in existing cuts: splitting
    # automatically moves and carries the existing layers into the new cut.
    for cut in sheet["cuts"]:
        if cut["id"] not in groups:
            _add_cut(tl, cut)
            groups = {g["id"]: g for g in tl._cut_groups()}
    for cut in sheet["cuts"]:
        group = groups.get(cut["id"])
        if group is None:
            raise SheetError(f"line {cut['line']}: could not create cut {cut['id']}")
        picture = group["picture"]
        if cut["on"] is not None and picture is not None and cut["on"] != intent.on(picture.data):
            picture.on(cut["on"])
        note = tl.cut_note(cut["id"])
        if cut["why"] is not None and cut["why"] != note.get("why"):
            tl.set_cut_note(cut["id"], why=cut["why"])
        if cut.get("hold") is not None and (cut["hold"] == "deliberate") != (note.get("hold") == "deliberate"):
            tl.set_cut_note(cut["id"], hold=cut["hold"] == "deliberate")
        lo, hi = tl._cut_spans()[cut["id"]]
        loose = _loose_in(tl, lo, hi if hi is not None else tl.duration)
        existing = {(intent.layer_of(c.data) or c.id): c for c in _ordered(group["clips"] + loose)}
        named = {}
        for layer in cut["layers"]:
            if layer["name"] in named:
                raise SheetError(_with_text(
                    f"line {layer['line']}: {cut['id']} already has a layer named {layer['name']!r} (line {named[layer['name']]}); "
                    f"give each its own name ({layer['name']}, {layer['name']}-2, …)", layer["line"], layer.get("text_line", "")))
            named[layer["name"]] = layer["line"]
        seen = set()
        for layer in cut["layers"]:
            name = layer["name"]
            seen.add(name)
            clip = existing.get(name)
            try:
                if clip is None:
                    _add_layer(tl, cut["id"], group, layer)
                else:
                    loose_clip = not intent.cut_of(clip.data)
                    was = json.dumps(clip.data, sort_keys=True, default=str) if loose_clip else None
                    old_on = intent.on(clip.data)
                    _update_layer(tl, clip, layer, is_picture=picture is not None and clip.data is picture.data)
                    if picture is not None and clip.data is picture.data and intent.on(clip.data) != old_on:
                        reflow = True
                    if loose_clip and json.dumps(clip.data, sort_keys=True, default=str) != was:
                        if not intent.until(clip.data) and intent.for_s(clip.data) is None:
                            intent.set_for(clip.data, tl.quantize(clip.duration))
                        intent.set_cut(clip.data, cut["id"])
                        if name != clip.id:
                            intent.set_layer(clip.data, name)
            except (TimelineEditError, mo.MomentError) as exc:
                said = str(exc)
                said = said if said.startswith(f"{cut['id']}.{name}") else f"{cut['id']}.{name}: {said}"
                raise SheetError(_with_text(f"line {layer['line']}: {said}", layer["line"], layer.get("text_line", ""))) from None
        for name, clip in existing.items():
            if name not in seen and intent.cut_of(clip.data) == cut["id"]:
                clip.remove()
    tl._mcache = None
    tl.resolve()
    if reflow:
        tl.reflow()
    if sheet.get("film_end"):
        tl.film_end(sheet["film_end"])
    for segment in sorted(line_removals):
        if any(line.segment == segment for line in tl.lines()):
            tl.remove_line(segment)
    return describe_changes(before, tl)


def _add_cut(tl, cut):
    from astrid.sdk.timeline_checkout import TimelineEditError
    line = cut["line"]
    if not cut["on"]:
        raise SheetError(f"line {line}: a new cut needs its moment: ┃ {cut['id']} on \"word\" (or on c05 +1.2s)")
    plates = [layer for layer in cut["layers"] if layer["track"] == "plate"]
    if not plates:
        raise SheetError(f"line {line}: new cut {cut['id']} needs a plate line (its picture), e.g.  plate  field  snap-plate  FIELD")
    names = [layer["name"] for layer in cut["layers"]]
    if len(set(names)) != len(names):
        raise SheetError(f"line {line}: {cut['id']} names a layer twice; give each its own name")
    try:
        new = tl.add_cut(cut["on"], id=cut["id"], picture=plates[0]["asset"] or None)
    except (TimelineEditError, mo.MomentError) as exc:
        raise SheetError(_with_text(f"line {line}: {cut['id']}: {exc}", line, f"┃ {cut['id']} on {cut['on']}")) from None
    picture = new.picture
    intent.set_layer(picture.data, plates[0]["name"])
    tl._mcache = None
    group = {"id": cut["id"], "clips": [], "picture": picture, "start": picture.start}
    existing = {(intent.layer_of(c.data) or c.id): c for c in tl.clips() if intent.cut_of(c.data) == cut["id"]}
    for layer in cut["layers"]:
        try:
            clip = existing.get(layer["name"])
            if clip is None:
                _add_layer(tl, cut["id"], group, layer)
            else:
                _update_layer(tl, clip, layer, is_picture=clip.data is picture.data)
        except (TimelineEditError, mo.MomentError) as exc:
            raise SheetError(_with_text(f"line {layer['line']}: {cut['id']}.{layer['name']}: {exc}", layer["line"], layer.get("text_line", ""))) from None
    if cut.get("why"):
        tl.set_cut_note(cut["id"], why=cut["why"])
    if cut.get("hold") is not None:
        tl.set_cut_note(cut["id"], hold=cut["hold"] == "deliberate")


def _apply_sound(tl, rows):
    from astrid.sdk.timeline_checkout import TimelineEditError
    clips = {(intent.layer_of(c.data) or c.id): c for c in sound_clips(tl)}
    for row in rows:
        if row["element"] != "media":
            raise SheetError(_with_text(f"line {row['line']}: a sound row must use the media element",
                                        row["line"], row.get("text_line", "")))
        clip = clips.get(row["name"])
        if clip is None:
            if not row["asset"]:
                raise SheetError(_with_text(f"line {row['line']}: a new sound row needs a media asset", row["line"], row.get("text_line", "")))
            try:
                at = row["on"] or row["at"]
                if at is None:
                    raise SheetError(f"line {row['line']}: a new sound row needs a gutter time or on <moment>")
                clip = tl.add("media", at=at, asset=row["asset"], track=row["track"], hold=row["for"],
                              cut=None, clip_id=row["name"])
                if not clip.is_audio:
                    raise SheetError(f"line {row['line']}: track {row['track']!r} is not an audio track")
                if row["on"]:
                    clip.on(row["on"])
                if row["until"]:
                    clip.until(row["until"])
                elif row["for"] is None and row.get("trim") is None:
                    clip.trim(in_s=0, out_s=clip._source_duration())
                if row.get("trim"):
                    clip.trim(in_s=row["trim"][0], out_s=row["trim"][1])
                clips[row["name"]] = clip
            except (TimelineEditError, mo.MomentError) as exc:
                raise SheetError(_with_text(f"line {row['line']}: {exc}", row["line"], row.get("text_line", ""))) from None
        try:
            if row["asset"] and row["asset"] != clip.asset:
                clip.swap_asset(row["asset"])
            trim_changed = row.get("trim") is not None and (clip.data.get("from") != row["trim"][0] or clip.data.get("to") != row["trim"][1])
            if trim_changed:
                clip.trim(in_s=row["trim"][0], out_s=row["trim"][1])
            if row.get("slip") is not None:
                clip.slip(row["slip"])
            if row.get("on") and row["on"] != intent.on(clip.data):
                clip.on(row["on"])
            elif not row.get("on") and row.get("at") is not None and abs(row["at"]-clip.start) > 0.0051:
                clip.enter_at(row["at"], anchor=False)
            if row["until"] and row["until"] != intent.until(clip.data):
                clip.until(row["until"])
            elif row["for"] is not None and not trim_changed and abs(row["for"]-clip.duration) > 0.5/tl.fps:
                clip.set_duration(row["for"])
            for key in row["params"]:
                if key not in _look_keys(clip.element, [key]) or cf.FIELDS[key].applies_to == "visual":
                    audio_fields = sorted(k for k, f in cf.FIELDS.items() if f.applies_to != "visual")
                    raise SheetError(f"line {row['line']}: a sound line takes {', '.join(audio_fields)} (got {key}=)")
            _apply_look(clip, row["params"])
        except (TimelineEditError, mo.MomentError, cf.ClipFieldError) as exc:
            raise SheetError(_with_text(f"line {row['line']}: {clip.address}: {exc}", row["line"], row.get("text_line", ""))) from None


def _element(token, current=None):
    if current and _short(current) == token:
        return current
    if token == "media" or token.startswith("am-") or "/" in token:
        return token
    return "am-" + token


def _update_layer(tl, clip, layer, *, is_picture):
    data = clip.data
    element = _element(layer["element"], clip.element)
    if element != clip.element:
        data["clipType"] = element
    if layer["track"] != clip.track and not clip.is_audio:
        clip.set_track(layer["track"])
    if (layer["asset"] or None) != (clip.asset or None):
        if layer["asset"]:
            clip.swap_asset(layer["asset"])
        else:
            data.pop("asset", None)
    raw_params = data.get("params") if isinstance(data.get("params"), dict) else None
    current = dict(raw_params or {})
    wanted = dict(layer["params"])
    look = {k: wanted.pop(k) for k in _look_keys(element, list(wanted))}
    if element == "am-type":
        if layer["text"] is not None:
            wanted["text"] = layer["text"]
        elif "text" in current:
            wanted["text"] = current["text"]
    from astrid.sdk.timeline.model import validate_param_value
    for key, value in wanted.items():
        if value is not ELIDED:
            validate_param_value(element, key, value, where=clip.address)
    for key in list(current):
        if key not in wanted and key != "text" and raw_params is not None:
            raw_params.pop(key, None)
    from astrid.sdk.timeline_address import element_schema, unknown_params
    bad = unknown_params(element, [k for k, v in wanted.items() if v is not ELIDED], existing=current)
    if bad:
        props = sorted((element_schema(element).get("properties") or {}))
        raise TimelineEditError(f"{element} has no param {', '.join(bad)}; it takes: {', '.join(props)}")
    from astrid.sdk.timeline_checkout import formula_from_spec, formula_short, parse_formula_value, to_canvas
    formulas = intent.formulas(data)
    for key, value in wanted.items():
        if value is ELIDED:
            continue
        exact = formulas.get(f"params.{key}")
        if exact is not None and isinstance(value, str) and value.strip() == formula_short(exact, element):
            continue
        spec = parse_formula_value(value)
        if spec is not None:
            current_expr = formulas.get(f"params.{key}") or {}
            new_expr = formula_from_spec(spec, element, key, current_expr)
            if not (current_expr and current_expr.get("mark") == new_expr["mark"] and abs(float(current_expr.get("offset") or 0)-new_expr["offset"]) < 1e-6):
                clip.set(**{key: value})
            continue
        has_formula = any(p == f"params.{key}" or p.startswith((f"params.{key}[", f"params.{key}.")) for p in formulas)
        if has_formula or to_canvas(element, key, current.get(key)) != value:
            clip.set(**{key: value})
    _apply_look(clip, look)
    if (layer["on"] is not None or not is_picture) and layer["on"] != intent.on(data):
        clip.on(layer["on"]) if layer["on"] else tl._place_on(clip, None)
    same_for = (layer["for"] is None and intent.for_s(data) is None) or (layer["for"] is not None and intent.for_s(data) is not None and abs(layer["for"]-intent.for_s(data)) < 0.5/tl.fps)
    if layer["until"] != intent.until(data) or not same_for:
        if layer["until"]:
            clip.until(layer["until"])
        elif layer["for"] is not None:
            clip.hold_for(layer["for"])
        else:
            intent.set_until(data, None)
            intent.set_for(data, None)


def _add_layer(tl, cut_id, group, layer):
    from astrid.sdk.timeline_address import element_schema, unknown_params
    from astrid.sdk.timeline_checkout import parse_formula_value, parse_moment_value
    element = _element(layer["element"])
    layer = {**layer, "params": dict(layer["params"])}
    look = {k: layer["params"].pop(k) for k in _look_keys(element, list(layer["params"]))}
    computed = {k: v for k, v in layer["params"].items() if v is not ELIDED and (parse_formula_value(v) is not None or parse_moment_value(v) is not None)}
    # tl.add converts canvas px to each element's storage itself (one param path, like clip.set),
    # so the sheet passes the canvas values it shows; converting here too stored x=600 as 17.
    params = {k: v for k, v in layer["params"].items() if v is not ELIDED and k not in computed}
    bad = unknown_params(element, list(params)+list(computed))
    if bad:
        props = sorted((element_schema(element).get("properties") or {}))
        raise TimelineEditError(f"{element} has no param {', '.join(bad)}; it takes: {', '.join(props)}")
    from astrid.sdk.timeline.model import validate_param_value
    for key, value in layer["params"].items():
        if value is not ELIDED and key not in computed:
            validate_param_value(element, key, value, where=f"{cut_id}.{layer['name']}")
    if layer["text"] is not None:
        params["text"] = layer["text"]
    clip = tl.add(element, at=group["start"], asset=layer["asset"], params=params, track=layer["track"], anchor=False,
                  cut=cut_id, layer=layer["name"])
    intent.set_cut(clip.data, cut_id)
    intent.set_layer(clip.data, layer["name"])
    tl._mcache = None
    if layer["on"]:
        clip.on(layer["on"])
    if layer["until"]:
        clip.until(layer["until"])
    elif layer["for"] is not None:
        clip.hold_for(layer["for"])
    for key, value in computed.items():
        clip.set_param(key, value)
    _apply_look(clip, look)


def iter_layers(sheet: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for cut in sheet["cuts"]:
        for layer in cut["layers"]:
            yield cut["id"], layer
