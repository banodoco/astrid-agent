"""``timelines visualize --view diff --from REV``: what an edit changed, cut by cut, and nothing else.

:func:`compare` matches cuts across two authoring bundles by their picture
clip (or sequence) and classifies each as changed, added, removed,
ripple-shifted (same content, new time) or untouched. The SCOPE condition
flags any change outside the cuts the editor says they edited (``--edited``).
:func:`compose_page` lays out before/after tiles of the changed cuts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from astrid.core.timeline.astrid_ns import read_app
from astrid.core.timeline.cuts import bundle_fps, occurrences_from_bundle
from astrid.core.timeline.cut_index import cut_index

from ..layers.base import Finding

MAX_ROWS = 12


def _clip_rows(cut: Mapping[str, Any], occurrence: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    start, end = float(cut["start"]), float(cut["end"])
    rows = {}
    for span in occurrence.get("spans") or ():
        if span["end"] <= start + 1e-6 or span["start"] >= end - 1e-6:
            continue
        clip = span["clip"]
        rows[span["id"]] = {
            "type": span["type"], "track": span["track"], "asset": clip.get("asset"),
            "at": round(span["start"] - start, 3), "length": round(span["end"] - span["start"], 3),
            "params": clip.get("params") or {}, "volume": clip.get("volume"),
            "app": {k: v for k, v in read_app(clip).items() if k not in ("data",)},
        }
    return rows


def _index(bundle: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], float]:
    occurrences = occurrences_from_bundle(bundle)
    fps = bundle_fps(bundle)
    by_occurrence = {o["occurrence_id"]: o for o in occurrences}
    cuts = cut_index(occurrences, fps=fps).rows
    keyed = {}
    for cut in cuts:
        key = (cut.get("sequence") or {}).get("id") or cut.get("clip_id") or f"{cut['occurrence_id']}@{cut['start']}"
        keyed[str(key)] = {"cut": cut, "rows": _clip_rows(cut, by_occurrence[cut["occurrence_id"]])}
    return cuts, keyed, fps


def _describe(before: Mapping[str, dict], after: Mapping[str, dict]) -> list[str]:
    notes = []
    for clip_id in sorted(set(before) | set(after)):
        a, b = before.get(clip_id), after.get(clip_id)
        if a is None:
            notes.append(f"+{clip_id}")
        elif b is None:
            notes.append(f"-{clip_id}")
        elif a != b:
            fields = []
            for field in ("at", "length", "asset", "volume"):
                if a.get(field) != b.get(field):
                    fields.append(f"{field} {a.get(field)}→{b.get(field)}")
            for key in sorted(set(a["params"]) | set(b["params"])):
                old, new = a["params"].get(key), b["params"].get(key)
                if old == new:
                    continue
                if isinstance(old, (dict, list)) or isinstance(new, (dict, list)):
                    fields.append(f"params.{key} changed")
                else:
                    fields.append(f"params.{key} {json.dumps(old)}→{json.dumps(new)}")
            if a.get("app") != b.get("app"):
                fields.append("app changed")
            notes.append(f"{clip_id.rsplit(':', 1)[-1]} " + ", ".join(fields[:3]))
    return notes


def compare(before: Mapping[str, Any], after: Mapping[str, Any], *, edited: Sequence[int] = ()) -> dict[str, Any]:
    """Cut-level diff of two bundles plus SCOPE/RIPPLE findings (cut numbers are the AFTER revision's)."""
    before_cuts, before_keyed, fps = _index(before)
    after_cuts, after_keyed, _fps = _index(after)
    frame = 1.0 / fps
    rows: list[dict[str, Any]] = []
    for key, entry in after_keyed.items():
        cut = entry["cut"]
        old = before_keyed.get(key)
        if old is None:
            rows.append({"status": "added", "after": cut, "before": None, "notes": ["new cut"]})
            continue
        notes = _describe(old["rows"], entry["rows"]) if old["rows"] != entry["rows"] else []
        shift = float(cut["start"]) - float(old["cut"]["start"])
        if notes:
            rows.append({"status": "changed", "after": cut, "before": old["cut"], "notes": notes, "shift": shift})
        elif abs(shift) > frame / 2 or abs(float(cut["duration"]) - float(old["cut"]["duration"])) > frame / 2:
            rows.append({"status": "ripple", "after": cut, "before": old["cut"], "notes": [], "shift": shift})
    for key, entry in before_keyed.items():
        if key not in after_keyed:
            rows.append({"status": "removed", "after": None, "before": entry["cut"], "notes": ["cut removed"]})
    rows.sort(key=lambda r: float((r["after"] or r["before"])["start"]))
    findings: list[Finding] = []
    wanted = set(int(n) for n in edited or ())
    for row in rows:
        cut = row["after"] or row["before"]
        number = int(row["after"]["index"]) if row["after"] else None
        label = "" if row["after"] else f"(was cut {cut['index']}) "
        if row["status"] == "ripple":
            continue
        message = f"{label}{row['status']}: " + "; ".join(row["notes"][:3])
        if wanted and (number is None or number not in wanted):
            findings.append(Finding("SCOPE", number, float(cut["start"]), f"{message} (outside --edited "
                                    f"{','.join(map(str, sorted(wanted)))})", "warn", None, "diff-scope"))
        else:
            findings.append(Finding("EDIT", number, float(cut["start"]), message, "info", None, "diff-scope"))
    ripple = [r for r in rows if r["status"] == "ripple"]
    if ripple:
        shifts = sorted({round(r["shift"], 3) for r in ripple})
        numbers = [int(r["after"]["index"]) for r in ripple]
        findings.append(Finding("RIPPLE", None, None, f"{len(ripple)} cut(s) moved without changing "
                                f"({min(numbers)}–{max(numbers)}; shift {', '.join(f'{s:+.2f} s' for s in shifts[:4])})",
                                "info", None, "diff-scope"))
    return {"rows": rows, "findings": findings, "before_cuts": len(before_cuts), "after_cuts": len(after_cuts),
            "changed_after": [int(r["after"]["index"]) for r in rows if r["after"] and r["status"] in ("changed", "added")],
            "changed_before": [int(r["before"]["index"]) for r in rows if r["before"] and r["status"] in ("changed", "removed")]}


def frames_by_cut(pack_root: Path, bundle: Mapping[str, Any]) -> dict[int, Path]:
    """Captured contact tiles of a bundle's pack, keyed by the cut number they show."""
    cuts = cut_index(occurrences_from_bundle(bundle), fps=bundle_fps(bundle)).rows
    index = json.loads((Path(pack_root) / "frame-index.json").read_text(encoding="utf-8"))
    found = {}
    for card in index.get("cards") or ():
        t = float(card.get("time_seconds") or 0.0)
        reasons = set(card.get("sample_reasons") or [])
        if reasons == {"sequence_step"}:
            continue
        for cut in cuts:
            if cut["start"] - 1e-6 <= t < cut["end"]:
                found.setdefault(int(cut["index"]), Path(pack_root) / str(card["image"]))
                break
    return found


def compose_page(report: Mapping[str, Any], before_frames: Mapping[int, Path], after_frames: Mapping[int, Path],
                 out: Path, *, title: str) -> Path:
    """Before | after tiles for the changed cuts, with what changed, one row per cut."""
    from PIL import Image, ImageDraw

    from ..layers.base import PALETTE, draw_text

    rows = [r for r in report["rows"] if r["status"] in ("changed", "added", "removed")][:MAX_ROWS]
    tile_w, tile_h = 384, 216
    width = 24 + 2 * tile_w + 16 + 560
    height = 70 + max(1, len(rows)) * (tile_h + 20) + 10
    page = Image.new("RGB", (width, height), PALETTE["bg"])
    draw = ImageDraw.Draw(page)
    draw_text(draw, (12, 10), title, 20, "white")
    draw_text(draw, (12, 40), f"before ({report['before_cuts']} cuts)", 14, PALETTE["muted"])
    draw_text(draw, (12 + tile_w + 8, 40), f"after ({report['after_cuts']} cuts)", 14, PALETTE["muted"])
    y = 64
    for row in rows:
        for column, (cut, frames) in enumerate(((row["before"], before_frames), (row["after"], after_frames))):
            x = 12 + column * (tile_w + 8)
            path = frames.get(int(cut["index"])) if cut else None
            if path and Path(path).is_file():
                with Image.open(path) as source:
                    page.paste(source.convert("RGB").resize((tile_w, tile_h), Image.LANCZOS), (x, y))
            else:
                draw.rectangle((x, y, x + tile_w, y + tile_h), outline=PALETTE["grid_strong"])
                draw_text(draw, (x + 8, y + 8), "(none)" if cut is None else "(not captured)", 13, PALETTE["muted"])
            if cut:
                draw_text(draw, (x + 4, y + tile_h + 2), f"#{cut['index']} {float(cut['start']):.2f}–{float(cut['end']):.2f}s",
                          12, PALETTE["ink"])
        tx = 12 + 2 * (tile_w + 8) + 8
        draw_text(draw, (tx, y + 4), row["status"].upper(), 15, PALETTE["warn"] if row["status"] != "removed" else PALETTE["bad"])
        for line_no, note in enumerate(row["notes"][:8]):
            draw_text(draw, (tx, y + 28 + line_no * 18), note[:70], 12, PALETTE["ink"])
        y += tile_h + 20
    page.save(out)
    return out


def window_frames_of(pack_root: Path) -> list[tuple[float, Path]]:
    """``(time, frame path)`` of every captured frame in a window bundle, in time order."""
    index = json.loads((Path(pack_root) / "frame-index.json").read_text(encoding="utf-8"))
    return sorted((float(card.get("time_seconds") or 0.0), Path(pack_root) / str(card["image"]))
                  for card in index.get("cards") or ())


def compose_compare(before: Sequence[tuple[float, Path]], after: Sequence[tuple[float, Path]], out: Path, *,
                    title: str, columns: int = 6) -> Path:
    """Two revisions of one window: the before frame above the after frame, column by column in time."""
    from PIL import Image, ImageDraw

    from ..layers.base import PALETTE, draw_text

    times = sorted({round(t, 3) for t, _p in before} | {round(t, 3) for t, _p in after})[:48]
    by_before = {round(t, 3): p for t, p in before}
    by_after = {round(t, 3): p for t, p in after}
    columns = max(1, min(columns, len(times) or 1))
    tile_w, tile_h = 300, 169
    rows = -(-len(times) // columns)
    block = 2 * tile_h + 44
    page = Image.new("RGB", (24 + columns * (tile_w + 8), 60 + rows * block), PALETTE["bg"])
    draw = ImageDraw.Draw(page)
    draw_text(draw, (12, 10), title, 20, "white")
    draw_text(draw, (12, 36), "each column: before (top) and after (bottom) at the same timeline second", 13, PALETTE["muted"])
    for index, t in enumerate(times):
        x = 12 + (index % columns) * (tile_w + 8)
        y = 60 + (index // columns) * block
        for row, frames in enumerate((by_before, by_after)):
            path = frames.get(t)
            top = y + row * (tile_h + 4)
            if path and Path(path).is_file():
                with Image.open(path) as source:
                    page.paste(source.convert("RGB").resize((tile_w, tile_h), Image.LANCZOS), (x, top))
            else:
                draw.rectangle((x, top, x + tile_w, top + tile_h), outline=PALETTE["grid_strong"])
        draw_text(draw, (x + 2, y + 2 * tile_h + 10), f"{t:.2f}s", 12, PALETTE["ink"])
    page.save(out)
    return out
