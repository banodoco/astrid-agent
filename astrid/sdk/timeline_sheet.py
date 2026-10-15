"""Human editable cut sheet for detached timeline checkouts.

The sheet is a projection of the working copy: timing gutters and narration
are read-only, while narration, cut, and layer rows carry edits back to that same copy.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shlex
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


def _clip_row(c, *, is_picture=False):
    element=str(c.element or "media"); element=element[3:] if element.startswith("am-") else element
    owner=c._tl
    shot_start=owner._shot_start(c.shot_id)
    bits=[f"{c.start:6.2f}" if abs(c.start-shot_start)>0.5/owner.fps else "",str(c.track or "visual"),c.layer_name,element,str(c.asset or "")]
    params=dict(c.data.get("params",{})); text=params.pop("text",None)
    if text is not None: bits[4]=((bits[4]+" ") if bits[4] else "")+json.dumps(str(text),ensure_ascii=False)
    extras=[]
    for key,value in sorted(params.items()):
        if isinstance(value,(str,int,float,bool)):
            shown=c.get(key) if key in {"x","y"} else value
            if isinstance(shown,str): shown=json.dumps(shown,ensure_ascii=False)
            extras.append(f"{key}={shown:g}" if isinstance(shown,float) else f"{key}={shown}")
    on=intent.on(c.data)
    until=intent.until(c.data)
    timing=(f"on {on}" if on else f"until {until}" if until else f"for {c.duration:g}s" if c.duration and not is_picture else "")
    bits.extend([timing," ".join(extras)])
    return bits


def _align(rows):
    if not rows:return []
    order=(1,2,3,4,5,6)
    widths={i:max(len(r[i]) for r in rows) for i in order}
    return [(f"{r[0]:>6}   " + "  ".join(r[i].ljust(widths[i]) for i in order[:-1]) + ("  " if r[5] and r[6] else "") + r[6]).rstrip() for r in rows]


def _legacy_render_sheet(tl: Checkout, *, start=None, end=None, banner=None, film=None, cuts: Iterable[str]|None=None, detail=False):
    groups=tl.cut_index.groups(tl)
    if not groups: return "this timeline has no named cuts yet (import its intent first)\n"
    spans=tl._cut_spans()
    ids=set(cuts) if cuts is not None else None
    selected=[g for g in groups if ids is None or g["id"] in ids]
    out=[f"film {film or tl.bundle.get('project_id','')} · {tl.bundle.get('timeline_id','')} · {_canvas(tl)} · {tl.fps:g} fps · v {tl.bundle.get('base_parent',{}).get('revision_id','0')}"]
    if banner: out.append(banner)
    # Narration projection, retaining stable segment names and opaque app data.
    voices=[]
    for c in tl.clips(audio=True):
        segment=intent.line(c.data)
        if segment and segment not in voices: voices.append(segment)
    if voices:
        out += ["", "lines                                            # the narration: everything hangs on it"]
        for seg in voices:
            text=next((intent._get(c.data,"text","") for c in tl.clips(audio=True) if intent.line(c.data)==seg),"")
            gap=tl.voice(seg).gap_after
            out.append(f"  {seg}  {_quote(text)}"+(f"  gap {gap:g}" if gap is not None else ""))
    sound=[c for c in tl.clips(audio=True) if not intent.line(c.data) and not intent.cut_of(c.data)]
    if sound:
        out += ["", "sound                                            # under the film, in no cut: asset, for, volume= (the grid: --beats)"]
        for c in sound:
            extra=f"volume={c.data['volume']:g}" if isinstance(c.data.get("volume"),(int,float)) else ""
            element=c.element or 'media'; element=element[3:] if element.startswith('am-') else element
            out.append(f"{c.start:6.2f}   {c.track or 'sfx'}  {c.layer_name}  {element}  {c.asset or ''}  for {c.duration:g}s{(' '+extra) if extra else ''}")
    for g in selected:
        cut_start, cut_end = spans[g["id"]]
        cut_end = tl.duration if cut_end is None else cut_end
        out += [f"{g['start']:6.2f} ┃ {g['id']:<5}"+" "*55+f"· {cut_end-g['start']:.2f} s"]
        said=" ".join(w.text for w in tl.words(between=(g["start"],cut_end)))
        if said: out.append(f"       > {said}")
        rows=[]
        for c in g["clips"]:
            if c.start+1e-6 < g["start"]: continue
            rows.append(_clip_row(c,is_picture=(c.track=="plate")))
        out.extend(_align(rows))
        note=next((intent._get(c.data,"why") for c in g["clips"] if intent._get(c.data,"why")),None)
        if note: out.append(f"         why: {note}")
    return "\n".join(out)+"\n"


def render_sheet(tl: Checkout, *, start=None, end=None, banner=None, film=None, cuts: Iterable[str]|None=None, detail=False):
    groups = tl.cut_index.groups(tl, include_orphans=True)
    if not groups:
        return "this timeline has no named cuts yet (import its intent first)\n"
    spans = tl.cut_index.spans()
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
    gaps = [row for row in tl.cut_index.rows if not row.get('cut_id') and row.get('clip') is None
            and (end is None or row['start'] < end) and (start is None or row['end'] > start)]
    for row in gaps:
        out.append(f"# picture gap · cut {row['index']} · {row['start']:.2f}–{row['end']:.2f} s")
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
    return "\n".join(out) + "\n"


def _loose_in(tl, lo, hi):
    return [c for c in tl.clips() if not c.is_audio and not intent.cut_of(c.data) and lo-1e-6 <= c.start < hi-1e-6]


def sound_clips(tl):
    return sorted((c for c in tl.clips() if c.is_audio and not intent.cut_of(c.data) and not intent.line(c.data)), key=lambda c: (c.start, c.id))


def _sound_row(tl, clip):
    bits = [f"volume={clip.data['volume']:g}"] if isinstance(clip.data.get("volume"), (int, float)) else []
    label = intent.beats_label(clip.data)
    if label:
        bits.append(f"~beats={label}")
    until = intent.until(clip.data)
    timing = f"until {until}" if until else f"for {_length_text(clip.duration, tl.fps)}"
    return [f"{clip.start:6.2f}", clip.track or "sfx", intent.layer_of(clip.data) or clip.id,
            _short(clip.element), clip.asset or "", " ".join(bits), timing]


def _carried(tl, groups):
    # The same actual intersections used by table and visualize; do not derive
    # carried coverage from next-opening timing boundaries.
    from astrid.sdk.timeline.model import Clip
    out = {}
    for group in groups:
        row = tl.cut_index.resolve(group['id'])
        own = {id(c.data) for c in group['clips']}
        out[group['id']] = [Clip(tl, row['shot_id'], s['clip']) for s in row['layers']
                            if id(s['clip']) not in own]
    return out


def cut_intersections(tl, selector):
    """The render-free sheet's machine-readable per-cut projection."""
    return tl.cut_index.intersections(selector)


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


def _length_text(seconds, fps):
    return mo.offset_text(seconds, fps).lstrip("+") or "0s"


def _align(rows):
    if not rows:
        return []
    order = (1, 2, 3, 4, 6, 5)
    widths = {i: max(len(row[i]) for row in rows) for i in order}
    return [(f"{row[0]:>6}   " + "  ".join(row[i].ljust(widths[i]) for i in order[:-1]) + "  " + row[5]).rstrip() for row in rows]


def _legacy_parse_sheet(text: str):
    parsed={"lines":{},"cuts":[],"rows":[]}; section=None; active_cut=None
    for number,line in enumerate(text.splitlines(),1):
        stripped=line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("film "): continue
        if stripped.startswith("lines"):
            section="lines"; continue
        if stripped.startswith("sound"):
            section="sound"; continue
        m=re.match(r"^\s*([0-9.]+)\s*┃\s*(\S+)(?:\s+on\s+(.+?))?(?:\s+·.*)?\s*$",line)
        if m:
            section="cut"; active_cut=m.group(2); parsed["cuts"].append({"time":float(m.group(1)),"id":active_cut,"on":m.group(3),"line":number}); continue
        if section=="lines":
            m=re.match(r'^\s+(\S+)\s+"(.*)"(?:\s+gap\s+([0-9.]+))?\s*$',line)
            if m: parsed["lines"][m.group(1)]={"text":m.group(2),"gap":float(m.group(3)) if m.group(3) else None,"line":number}
            continue
        if section in {"cut","sound"} and line[:1].isspace() and not stripped.startswith(">") and not stripped.startswith("why:"):
            parsed["rows"].append({"line":number,"section":section,"cut":active_cut,"text":stripped})
    return parsed


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
        if raw.startswith(("film ", "WORKING COPY", "PUBLISHED", "next:", "scope ")) or body.startswith((">", "↳")) or raw.startswith("# "):
            continue
        if body.startswith("edit "):
            from astrid.sdk.timeline.commands import EditCommand, CommandError
            try:
                command = EditCommand.coerce(json.loads(body[5:])).to_json()
            except (json.JSONDecodeError, CommandError) as exc:
                raise SheetError(f"line {number}: {exc}") from None
            sheet.setdefault("commands", []).append(command)
            continue
        if body in ("lines", "orphans", "sound"):
            section, cut = body, None
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
        if section == "sound" and cut is None:
            try:
                sheet.setdefault("sound", []).append(_parse_layer(body, number, fps))
            except SheetError as exc:
                raise SheetError(_with_text(str(exc), number, body)) from None
            sheet["sound"][-1]["text_line"] = body
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
    out, quote = [], False
    for i, char in enumerate(raw):
        if char == '"':
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
    if tokens and re.fullmatch(r"\d+(\.\d+)?", tokens[0]):
        gutter, tokens = float(tokens[0]), tokens[1:]
    if len(tokens) < 3:
        raise SheetError(f"line {number}: a layer reads  track name element [ASSET] [\"text\"] [k=v …] [on …] [until …|for …]")
    layer = {"track": tokens[0], "name": tokens[1], "element": tokens[2], "asset": None, "text": None,
             "params": {}, "on": None, "until": None, "for": None, "line": number, "at": gutter}
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
        is_param = "=" in token and not token.startswith('"')
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
    order = [g["id"] for g in tl.cut_index.groups(tl)]
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


def _legacy_apply_sheet(tl: Checkout, text: str) -> list[str]:
    parsed=parse_sheet(text); before=tl.document(); report=[]
    try:
        touched={}
        existing={g["id"] for g in tl.cut_index.groups(tl)}
        for header in parsed["cuts"]:
            if header["id"] in existing and header.get("on"):
                picture=tl.cut(header["id"]).picture
                if picture is not None: picture.on(header["on"])
            elif header["id"] not in existing and header.get("on"):
                tl.add_cut(header["on"],id=header["id"])
                existing.add(header["id"])
        for line in parsed["rows"]:
            words=shlex.split(line["text"])
            if len(words)<4: continue
            if words and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?",words[0]): words=words[1:]
            if len(words)<4: continue
            track,layer,element,asset=words[:4]
            if line["section"]=="sound":
                address=next((c for c in tl.clips(audio=True) if (intent.layer_of(c.data) or c.id)==layer),None)
            else:
                address=next((c for c in tl.clips() if intent.cut_of(c.data)==line.get("cut") and (intent.layer_of(c.data) or c.id)==layer),None)
            params={}; i=4; timing={}
            while i<len(words):
                token=words[i]
                if token in {"on","until","for"}:
                    if i+1<len(words):
                        val=words[i+1]
                        timing[token]=val
                        i+=2; continue
                if "=" in token:
                    k,v=token.split("=",1)
                    try: params[k]=json.loads(v)
                    except Exception: params[k]=v
                i+=1
            if address is None:
                if line["section"]=="sound": continue
                at=timing.get("on")
                if at and at not in {"end"} and not re.fullmatch(r"[0-9.]+",at) and not at.startswith('"'): at=_quote(at)
                if at is None: at=next((g["start"] for g in tl.cut_index.groups(tl) if g["id"]==line.get("cut")),0)
                hold=float(timing["for"].rstrip("s")) if "for" in timing else 1.0
                if element in {"type","sprite","snap-plate"}: element="am-"+element
                if element=="am-type": params["text"]=asset
                address=tl.add(element,at=at,hold=hold,params={**params,**({"text":asset.strip('"')} if asset.startswith('"') else {})},layer=layer,track=track,asset=None if asset.startswith('"') or element=="am-type" else asset)
                touched.setdefault(line.get("cut"),set()).add(address.data.get("id"))
            elif line["section"]!="sound":
                touched.setdefault(line.get("cut"),set()).add(address.data.get("id"))
                address.data["track"]=track
                if element in {"type", "am-type"}:
                    params["text"]=asset
                elif asset and asset!=address.asset: address.data["asset"]=asset
                if params: address.set(**params)
                if "on" in timing:
                    val=timing["on"]; address.on(val if val in {"end"} or re.fullmatch(r"[0-9.]+",val) else _quote(val))
                if "until" in timing:
                    val=timing["until"]; address.until(val if val in {"end"} or re.fullmatch(r"[0-9.]+",val) else _quote(val))
                if "for" in timing:
                    duration=float(timing["for"].rstrip("s"))
                    if abs(address.duration-duration)>0.5/tl.fps: address.set_duration(duration)
            if line["section"]=="sound" and address:
                if "volume" in params: address.data["volume"]=params["volume"]
                if "for" in timing:
                    duration=float(timing["for"].rstrip("s"))
                    if abs(address.duration-duration)>0.5/tl.fps: address.set_duration(duration)
        # A complete cut section can delete omitted layers; the picture row is
        # required to remain present. A fragment without a cut header is never
        # interpreted as a deletion request.
        for cut in {entry["id"] for entry in parsed["cuts"]}:
            keep=touched.get(cut,set())
            group=next((g for g in tl.cut_index.groups(tl) if g["id"]==cut),None)
            if group and keep:
                for c in list(group["clips"]):
                    if c.data.get("id") not in keep and c.track!="plate": c.remove()
        # Apply explicit line gap edits, never line deletion as an accidental action.
        for seg,data in parsed["lines"].items():
            if data["gap"] is not None and tl.voice(seg).clips: tl.voice(seg).set_gap_after(data["gap"])
        if before!=tl.document():
            tl.save(quiet=True); report.append("applied sheet edits")
        return report
    except Exception as exc:
        tl.bundle=before; tl._refresh()
        raise SheetError(f"line {line.get('line', '?')}: «{line.get('text','')}» — {exc}; use a valid clip address and editable field") from exc


def apply_sheet(tl: Checkout, text: str) -> list[str]:
    """Apply a full sheet or fragment atomically and return the checkout's change summary.

    A sheet exported from an earlier version merges three-way (what the sheet changed since it was exported,
    onto what is here now): an edit made after the export is never reverted."""
    parsed = parse_sheet(text)
    before = Checkout(copy.deepcopy(tl.bundle))
    if parsed.get("version") and parsed["version"] != sheet_version(tl):
        base = find_version(tl, parsed["version"])
        if base is None:
            raise SheetError(f"this sheet was exported from version {parsed['version']}, which this working copy no longer "
                             f"has (it is now {sheet_version(tl)}): re-export the current timeline as a sheet and make "
                             "your change again, so nothing edited since is undone")
        parsed = merge_sheet(parsed, base, tl)
    try:
        with tl.step("apply sheet"):
            return _apply_parsed(tl, parsed, before)
    except Exception:
        tl.bundle = before.bundle
        tl._refresh()
        raise


def find_version(tl: Any, version: str) -> Any:
    """The Checkout this working copy was at ``version``: now, an earlier saved edit (undo history), or its
    published base. None when it is not kept any more."""
    from astrid.sdk.timeline_checkout import Checkout, _history_dir, _read_step
    from astrid.sdk.timeline_cuts import base_bundle

    if sheet_version(tl) == version:
        return tl
    kept = (getattr(tl, "_exported", None) or {}).get(version)
    if kept is not None:
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
        if sheet_version(other) == version:
            return other
    try:
        base = Checkout(base_bundle(tl.document()))
        if sheet_version(base) == version:
            return base
    except Exception:  # noqa: BLE001
        pass
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
                         + ". Re-export the current timeline as a sheet and make your change again.")
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
    for segment, entry in sheet["lines"].items():
        voice = tl.voice(segment)
        if entry["text"] and entry["text"] != voice.text.replace('"', "'"):
            intent.set_line_text(voice.clips[0].data, entry["text"])
        if entry["gap"] is not None and (voice.gap_after is None or abs(voice.gap_after-entry["gap"]) > 0.0051):
            voice.set_gap_after(entry["gap"], reflow=False)
            reflow = True
    if sheet.get("sound"):
        _apply_sound(tl, sheet["sound"])
    groups = {g["id"]: g for g in tl.cut_index.groups(tl)}
    removals = []
    for cut in sheet["cuts"]:
        group = groups.get(cut["id"])
        if group is None:
            _add_cut(tl, cut)
            continue
        picture = group["picture"]
        if cut["on"] is not None and picture is not None and cut["on"] != intent.on(picture.data):
            picture.on(cut["on"])
            reflow = True
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
            if name not in seen and intent.cut_of(clip.data) == cut["id"] and not tl._is_picture(clip):
                removals.append((clip, cut["id"]))
    for clip, original_cut in removals:
        # A later split can carry this very clip into its new cut. Decide
        # omission after all cut headers have been applied, preserving its uid.
        if intent.cut_of(clip.data) == original_cut:
            clip.remove()
    from astrid.sdk.timeline.commands import execute
    for command in sheet.get("commands", []):
        execute(tl, command)
    tl._mcache = None
    if reflow:
        tl.reflow()
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
                if clip.data is picture.data and layer['for'] is None and layer['until'] is None:
                    # The new header requests a split. An omitted picture
                    # length inherits the split's trim, rather than silently
                    # clearing the operation's authored coverage afterward.
                    layer = dict(layer, **{'for': intent.for_s(clip.data), 'until': intent.until(clip.data)})
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
        clip = clips.get(row["name"])
        if clip is None:
            raise SheetError(_with_text(f"line {row['line']}: no sound clip {row['name']!r} (sound clips: {', '.join(clips) or 'none'}); add one with timelines edit", row["line"], row.get("text_line", "")))
        try:
            if row["asset"] and row["asset"] != clip.asset:
                clip.swap_asset(row["asset"])
            if row.get("at") is not None and abs(row["at"]-clip.start) > 0.0051:
                clip.enter_at(row["at"], anchor=False)
            if row["until"] and row["until"] != intent.until(clip.data):
                clip.until(row["until"])
            elif row["for"] is not None and abs(row["for"]-clip.duration) > 0.5/tl.fps:
                clip.set_duration(row["for"])
            for key, value in row["params"].items():
                if key != "volume":
                    raise SheetError(f"line {row['line']}: a sound line takes volume= only (got {key}=)")
                if value is not ELIDED:
                    stored = cf.coerce(key, value, fps=tl.fps, audio=True)[1]
                    if stored != cf.read(clip.data, key, fps=tl.fps, audio=True):
                        clip.set_look(**{key: value})
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
            clip.clear_asset()
    raw_params = data.get("params") if isinstance(data.get("params"), dict) else None
    current = dict(raw_params or {})
    wanted = dict(layer["params"])
    if element == "am-type":
        if layer["text"] is not None:
            wanted["text"] = layer["text"]
        elif "text" in current:
            wanted["text"] = current["text"]
    from astrid.sdk.timeline_address import element_schema
    declared = set(element_schema(element).get("properties") or {})
    fields = {key: wanted.pop(key) for key in list(wanted) if key in cf.FIELDS and key not in declared}
    for key, value in fields.items():
        if value is not ELIDED:
            clip.set_look(**{key: value})
    for key in list(current):
        if key not in wanted and key != "text" and raw_params is not None:
            clip.clear(key)
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


def iter_layers(sheet: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for cut in sheet["cuts"]:
        for layer in cut["layers"]:
            yield cut["id"], layer
