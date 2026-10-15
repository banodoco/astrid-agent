"""Schema-owned intent accessors for detached timeline editing.

Reads retain compatibility with the historical flat ``app`` representation;
all writes target ``app.astrid`` and leave unrelated metadata intact.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from astrid.sdk.timeline.loops import LoopSpec

from astrid.core.timeline import astrid_ns as _ns


def _legacy(clip: Mapping[str, Any]) -> Mapping[str, Any]:
    value = clip.get("app")
    return value if isinstance(value, Mapping) else {}


def _app(clip: Mapping[str, Any]) -> Mapping[str, Any]:
    app = _legacy(clip)
    nested = app.get("astrid")
    return {**app, **(nested if isinstance(nested, Mapping) else {})}


def _app_w(clip: dict[str, Any]) -> dict[str, Any]:
    return _ns.clip_ns(clip)


_INTENT_KEYS = frozenset({"on", "until", "until_film_end", "for", "cut", "layer", "why", "why_layers",
    "deliberate", "deliberate_hold", "formulas", "segment", "words", "text",
    "gap_after_s", "beats", "beats_label", "slot", "replaces", "standin",
    "standin_intent", "sequence", "sequence_index", "sequence_fit", "orphan", "loop"})


def _tidy(clip: dict[str, Any]) -> None:
    app = clip.get("app")
    if isinstance(app, dict) and isinstance(app.get("astrid"), dict) and not app["astrid"]:
        app.pop("astrid")
        if not app:
            clip.pop("app")

def migrate(bundle: dict[str, Any]) -> dict[str, Any]:
    """Migrate the detached copy using the shared, lossless ownership rule."""
    _ns.migrate_bundle(bundle)
    return bundle


def _get(clip: Mapping[str, Any], key: str, default: Any = None): return _app(clip).get(key, default)
def _set(clip: dict[str, Any], key: str, value: Any) -> None:
    app = _app_w(clip)
    if value is None or value == "": app.pop(key, None)
    else: app[key] = value
    _tidy(clip)

def _set_or_drop(clip: dict[str, Any], key: str, value: Any) -> None:
    """Compatibility name used by timeline model and edit code for intent writes."""
    _set(clip, key, value)

def on(clip):
    value = _get(clip, "on"); return str(value) if isinstance(value, str) and value.strip() else None
def set_on(clip, value): _set(clip, "on", value)

def loop(clip: Mapping[str, Any]) -> "LoopSpec | None":
    return _get(clip, "loop")

def set_loop(clip: dict[str, Any], value: "LoopSpec | None") -> None:
    """Author phase separately from onset; resolve emits sprite params.loop."""
    import copy
    _set(clip, "loop", None if value is None else copy.deepcopy(value))
    # Clearing the authored recipe also removes its computed consumer output.
    if value is None and isinstance(clip.get('params'), dict):
        clip['params'].pop('loop', None)

def until(clip):
    value = _get(clip, "until"); return str(value) if isinstance(value, str) and value.strip() else None
def until_film_end(clip):
    """Original timeline end for an ``until`` authored by film_end, independent of cut ownership."""
    value = _get(clip, "until_film_end")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
def set_until(clip, value, *, film_end=None):
    _set(clip, "until", value)
    _set(clip, "until_film_end", film_end if value else None)
def for_s(clip):
    value = _get(clip, "for"); return float(value) if isinstance(value, (int,float)) and not isinstance(value,bool) else None
def set_for(clip, value): _set(clip, "for", None if value is None else round(float(value),6))
def cut_of(clip):
    value=_get(clip,"cut"); return str(value) if value else None
def set_cut(clip,value): _set(clip,"cut",value)
def layer_of(clip):
    value=_get(clip,"layer"); return str(value) if value else None
def set_layer(clip,value): _set(clip,"layer",value)
def deliberate(clip):
    a=_app(clip); return bool(a.get("deliberate_hold") or a.get("deliberate"))
def set_deliberate(clip,value):
    app=_app_w(clip); app.pop("deliberate",None); _set(clip,"deliberate_hold",True if value else None)
def why_layers(clip):
    value=_get(clip,"why_layers"); return [str(v) for v in value] if isinstance(value,list) else None
def why(clip):
    value=_get(clip,"why"); return str(value) if isinstance(value,str) and value.strip() else None
def set_why(clip,value): _set(clip,"why",value)
def formulas(clip):
    value=_get(clip,"formulas"); return dict(value) if isinstance(value,Mapping) else {}
def set_formula(clip,path,expr):
    value=formulas(clip)
    if expr is None: value.pop(path,None)
    else: value[path]=dict(expr)
    _set(clip,"formulas",value or None)
def line(clip):
    value=_get(clip,"segment"); return str(value) if value else None
def words(clip):
    value=_get(clip,"words"); return [list(row) for row in value] if isinstance(value,list) else []
def set_line(clip,segment,rows):
    app=_app_w(clip); app["segment"]=segment; app["words"]=[list(row) for row in rows]
def line_text(clip):
    value=_get(clip,"text"); return value if isinstance(value,str) else None
def set_line_text(clip,value): _set(clip,"text",None if value is None else str(value))
def gap_after(clip):
    value=_get(clip,"gap_after_s"); return float(value) if isinstance(value,(int,float)) and not isinstance(value,bool) else None
def set_gap_after(clip,value): _set(clip,"gap_after_s",None if value is None else round(float(value),6))
def beat_sources(clip,kind="beat"):
    value=_get(clip,"beats")
    if isinstance(value,Mapping):
        key="downbeats" if kind=="downbeat" else "hits" if kind=="hit" else "beats"
        return [float(x[0] if isinstance(x,(list,tuple)) else x) for x in value.get(key,[]) if isinstance(x[0] if isinstance(x,(list,tuple)) else x,(int,float))]
    if kind=="beat" and isinstance(value,list):
        return [float(clip.get("from") or 0)+float(x[0] if isinstance(x,(list,tuple)) else x)*float(clip.get("speed") or 1) for x in value]
    return []
def beats_grid(data,source=None):
    def values(key):
        result=[]
        for row in data.get(key) or []:
            t=row.get("t") if isinstance(row,Mapping) else row[0] if isinstance(row,(list,tuple)) and row else row
            if isinstance(t,(int,float)) and not isinstance(t,bool): result.append(round(float(t),6))
        return result
    beats=values("beats")
    if not beats: raise ValueError("a beats file needs a non-empty beats list (seconds of the music file)")
    out={"beats":beats}
    for key in ("downbeats","bars"):
        if values(key): out[key]=values(key)
    hits=[]
    for row in data.get("hits") or []:
        if isinstance(row,Mapping) and isinstance(row.get("t"),(int,float)): hits.append([round(float(row["t"]),6),row.get("kind","hit")])
        elif isinstance(row,(list,tuple)) and row: hits.append([round(float(row[0]),6),row[1] if len(row)>1 else "hit"])
    if hits: out["hits"]=hits
    if data.get("bpm") is not None: out["bpm"]=data["bpm"]
    if source is not None: out["source"]=source
    return out
def set_beats(clip,grid): _set(clip,"beats",None if grid is None else dict(grid))
def beats_label(clip):
    value=_get(clip,"beats_label")
    if not value:
        grid=_get(clip,"beats")
        value=grid.get("source") if isinstance(grid,Mapping) else None
    return str(value) if value else None
def beat_period(clip):
    value=_get(clip,"beats"); bpm=value.get("bpm") if isinstance(value,Mapping) else None
    return 60/float(bpm) if isinstance(bpm,(int,float)) and bpm>0 else None
def map_timed(clip,fn):
    rows=words(clip)
    if rows: _app_w(clip)["words"]=fn(rows)
def slots(bundle):
    value = _film(bundle).get("slots")
    if isinstance(value, dict):
        return value
    ns = _film_w(bundle)
    if "slots" in ns:
        raise ValueError("cannot write Astrid slots: existing slots is not an object")
    value = {}
    ns["slots"] = value
    return value

def slot(clip):
    value=_get(clip,"slot"); return str(value) if value else None
def set_slot(clip,slot_id,*,replaces=None):
    app=_app_w(clip); app["slot"]=slot_id
    if replaces is not None: app["replaces"]=list(replaces)
def standin(clip):
    value=_get(clip,"standin")
    if not isinstance(value,Mapping): return None
    return value.get("wanted"),dict(value.get("intended") or {})
def set_standin(clip,wanted,intended=None): _set(clip,"standin",{"wanted":wanted,"intended":dict(intended or {})})
def clear_standin(clip): _set(clip,"standin",None)
def has_intent(clip): return any(k in _app(clip) for k in _INTENT_KEYS)
def sequence(clip):
    value=_get(clip,"sequence")
    if isinstance(value,Mapping) and isinstance(value.get("id"),str) and isinstance(value.get("index"),int): return value["id"],value["index"]
    # Frozen donor clips stored the sequence id and index as two flat values;
    # V3 writes the pair as one object under app.astrid.sequence.
    if isinstance(value,str) and value:
        index=_get(clip,"sequence_index",0)
        if isinstance(index,int) and not isinstance(index,bool): return value,index
    return None
def set_sequence(clip,sequence_id,index):
    app=_app_w(clip); app.pop("sequence_index",None)
    _set(clip,"sequence",{"id":sequence_id,"index":int(index)})
def sequence_fit(clip):
    value=_get(clip,"sequence_fit"); return dict(value) if isinstance(value,Mapping) else None
def set_sequence_fit(clip,spec): _set(clip,"sequence_fit",None if spec is None else dict(spec))
def _film(bundle: Mapping[str, Any]) -> Mapping[str, Any]:
    from astrid.core.timeline import astrid_ns as _ns
    return _ns.film_intent((bundle.get("parent") or {}).get("config"))


def _film_w(bundle: dict[str, Any]) -> dict[str, Any]:
    """The film's writable ``parent.config.app.astrid`` (legacy loose keys are moved in first)."""
    from astrid.core.timeline import astrid_ns as _ns
    return _ns.film_ns(bundle.setdefault("parent", {}).setdefault("config", {}))


def _film_tidy(bundle: dict[str, Any]) -> None:
    from astrid.core.timeline import astrid_ns as _ns
    _ns.tidy_film(bundle.setdefault("parent", {}).setdefault("config", {}))


# ---- chapters: labels over runs of cuts (not containers) -----------------------------
def cut_notes(bundle: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``{cut id: {why, why_layers, hold}}``: notes belong to the CUT (``parent.config.app.astrid.cut_notes``), not to
    whichever clip is its picture today (that changes: a picture moved to another track, a new first step)."""
    value = _film(bundle).get("cut_notes")
    return {str(k): dict(v) for k, v in value.items() if isinstance(v, Mapping)} if isinstance(value, Mapping) else {}


def set_cut_note(bundle: dict[str, Any], cut_id: str, **fields: Any) -> None:
    """Set (or, with None, drop) fields of one cut's note: ``why``, ``why_layers``, ``hold`` ("deliberate")."""
    ns = _film_w(bundle)
    notes = dict(ns.get("cut_notes") or {})
    note = dict(notes.get(cut_id) or {})
    for key, value in fields.items():
        if value in (None, "", False, []):
            note.pop(key, None)
        else:
            note[key] = value
    if note:
        notes[cut_id] = note
    else:
        notes.pop(cut_id, None)
    if notes:
        ns["cut_notes"] = notes
    else:
        ns.pop("cut_notes", None)
        _film_tidy(bundle)


def chapters(bundle: Mapping[str, Any]) -> list[dict[str, str]]:
    """``[{"name": "06 ASTRID", "from": "c30"}, …]``: each chapter starts at a cut."""
    value = _film(bundle).get("chapters")
    return [dict(row) for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def set_chapters(bundle: dict[str, Any], table: list[Mapping[str, Any]]) -> None:
    ns = _film_w(bundle)
    if table:
        ns["chapters"] = [dict(row) for row in table]
    else:
        ns.pop("chapters", None)
        _film_tidy(bundle)


def orphan(clip):
    value=_get(clip,"orphan"); return str(value) if value else None
def set_orphan(clip,value): _set(clip,"orphan",value)
