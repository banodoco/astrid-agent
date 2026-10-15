"""Bounded whole-film pixel sweep over exact Runtime-managed V3 renders."""
from __future__ import annotations

import math
import tempfile
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import fast_lane as fl

SWEEP_MAX_FRAMES = 90
BLACK_MEAN = 8.0  # a frame whose mean luma is under this (0-255) reads as black
BLACK_LABEL = "black frame (mean luma < 8) — check coverage"  # a luma guess, not coverage


def _cut_key(cut: Mapping[str, Any]) -> str:
    if cut.get("cut_id"):
        return str(cut["cut_id"])
    if cut.get("clip_id"):
        return "clip " + str(cut["clip_id"]).rsplit(":", 1)[-1]
    return f"gap at {float(cut['start']):.2f} s"


def _keyed(cuts: Sequence[Mapping[str, Any]]) -> list[tuple[str, Mapping[str, Any]]]:
    seen: dict[str, int] = {}
    out = []
    for cut in cuts:
        key = _cut_key(cut)
        seen[key] = seen.get(key, 0) + 1
        out.append((key if seen[key] == 1 else f"{key}#{seen[key]}", cut))
    return out


def _samples(cut: Mapping[str, Any], fps: float) -> list[tuple[str, int]]:
    first = fl.frame_of(float(cut["start"]), fps)
    last = max(first, fl.frame_of(float(cut["end"]), fps) - 1)
    if last - first + 1 < 5:
        return [("middle", (first + last) // 2)]
    out: list[tuple[str, int]] = []
    seen: set[int] = set()
    for label, frame in (("start", first + 2), ("middle", (first + last) // 2), ("end", last - 2)):
        if frame not in seen:
            seen.add(frame)
            out.append((label, frame))
    return out


def plan_sweep(published: Mapping[str, Any], working: Mapping[str, Any], fps: float) -> list[dict[str, Any]]:
    """Pair cut samples by cut identity and relative position, including re-flowed cuts."""
    if not math.isfinite(float(fps)) or float(fps) <= 0:
        raise ValueError("fps must be a positive finite number")
    from astrid.core.timeline.cuts import occurrences_from_snapshot
    from astrid.core.timeline.cut_index import cut_index

    before_cuts = cut_index(occurrences_from_snapshot(published), fps=fps).rows
    after_cuts = cut_index(occurrences_from_snapshot(working), fps=fps).rows
    before, after = dict(_keyed(before_cuts)), dict(_keyed(after_cuts))
    rows: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after), key=lambda item: min(
        float((before.get(item) or after[item])["start"]),
        float((after.get(item) or before[item])["start"]),
    )):
        old, new = before.get(key), after.get(key)
        if old is None:
            samples = _samples(new, fps)
            rows.append({"key": key, "cut_id": new.get("cut_id"), "start": float(new["start"]),
                         "end": float(new["end"]), "kind": "new", "moved": 0.0,
                         "samples": [{"label": label, "published_frame": None, "working_frame": frame}
                                     for label, frame in samples], "render": True})
        elif new is None:
            samples = _samples(old, fps)
            rows.append({"key": key, "cut_id": old.get("cut_id"), "start": float(old["start"]),
                         "end": float(old["end"]), "kind": "removed", "moved": 0.0,
                         "samples": [{"label": label, "published_frame": frame, "working_frame": None}
                                     for label, frame in samples], "render": True})
        else:
            before_samples, after_samples = dict(_samples(old, fps)), dict(_samples(new, fps))
            labels = list(dict.fromkeys((*before_samples, *after_samples)))
            samples = [{"label": label, "published_frame": before_samples.get(label),
                        "working_frame": after_samples.get(label)} for label in labels]
            rows.append({"key": key, "cut_id": new.get("cut_id") or old.get("cut_id"),
                         "start": float(new["start"]), "end": float(new["end"]), "kind": "same",
                         "moved": float(new["start"]) - float(old["start"]), "samples": samples,
                         "render": True})
    return rows


def expected_cuts(changes: Iterable[Mapping[str, Any]], cut_of: Mapping[str, str | None]) -> set[str]:
    """Return cuts directly touched by edits; a pure re-flow does not mark them expected."""
    found: set[str] = set()
    for change in changes:
        raw = str(change.get("clip_id") or "")
        cut = cut_of.get(raw) or cut_of.get(raw.rsplit(":", 1)[-1])
        if not cut:
            continue
        before, after = change.get("before"), change.get("after")
        fields = set(change.get("fields") or ())
        if change.get("kind") in ("added", "removed") or fields - {"start", "end"}:
            found.add(str(cut))
        elif before and after and abs((after[0] - before[0]) - (after[1] - before[1])) > 1e-3:
            found.add(str(cut))
    return found


def _budget(rows: list[dict[str, Any]], expected: set[str], max_frames: int) -> str:
    """Keep expected cuts first, then distribute the remaining frame allowance across the film."""
    if type(max_frames) is not int or max_frames < 1:
        raise ValueError("max_frames must be a positive integer")
    rendered = [row for row in rows if row["render"]]
    if sum(len(row["samples"]) for row in rendered) <= max_frames:
        return ""
    chosen: list[dict[str, Any]] = []
    used = 0
    for row in rendered:
        if row.get("cut_id") in expected and used + len(row["samples"]) <= max_frames:
            chosen.append(row)
            used += len(row["samples"])
    remaining_rows = [row for row in rendered if row not in chosen]
    capacity = max_frames - used
    slots = [row for row in remaining_rows if len(row["samples"]) <= capacity]
    if slots and capacity:
        count = min(len(slots), capacity // max(1, min(len(row["samples"]) for row in slots)))
        if count:
            for i in range(count):
                row = slots[min(len(slots) - 1, int(i * len(slots) / count))]
                if row not in chosen and used + len(row["samples"]) <= max_frames:
                    chosen.append(row)
                    used += len(row["samples"])
    chosen_ids = {id(row) for row in chosen}
    for row in rendered:
        if id(row) not in chosen_ids:
            row["render"] = False
            row["kind"] = "unchecked"
            row["verdict"] = "inputs changed; not captured (sampled out)"
    return f"sampled {used} frame(s) per side (the {max_frames}-frame verification allowance)"


def _is_black(path: str | Path) -> bool:
    from PIL import Image, ImageStat

    with Image.open(path) as image:
        return ImageStat.Stat(image.convert("L")).mean[0] < BLACK_MEAN


def _pixel_line(sample: Mapping[str, Any], canvas: tuple[int, int]) -> str:
    share = float(sample["share"])
    count = int(sample["count"])
    box = sample.get("box")
    size = sample["size"]
    return fl._pixel_line(share, count, box, size, canvas)


def sweep(client: Any, project: str, timeline: str, *, draft: Any, expected: Iterable[str] = (),
          root: Path | None = None, served: Mapping[str, Any] | None = None,
          max_frames: int = SWEEP_MAX_FRAMES) -> dict[str, Any]:
    """Compare bounded decoded samples from exact base and working managed renders."""
    del served
    root = root or fl.FAST_ROOT
    root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    run_dir = Path(tempfile.mkdtemp(prefix="verify-sweep-", dir=root))
    pair = None
    try:
        pair = fl.prepare_pair(client, project, timeline, draft, media_root=run_dir)
        base_snapshot, working_snapshot = pair["base_snapshot"], pair["working_snapshot"]
        rational = base_snapshot.get("fps_rational", [30, 1])
        fps = float(Fraction(int(rational[0]), int(rational[1])))
        rows = plan_sweep(base_snapshot, working_snapshot, fps)
        expected_set = {str(value) for value in expected}
        sampled = _budget(rows, expected_set, max_frames)
        frames = {"published": set(), "working": set()}
        for row in rows:
            if not row["render"]:
                continue
            for sample in row["samples"]:
                if sample["published_frame"] is not None:
                    frames["published"].add(int(sample["published_frame"]))
                if sample["working_frame"] is not None:
                    frames["working"].add(int(sample["working_frame"]))
        frame_lists = {side: sorted(values) for side, values in frames.items()}
        videos: dict[str, Path] = {}
        if frame_lists["published"]:
            videos["published"] = pair["base_video"]
        if frame_lists["working"]:
            videos["working"] = pair["working_video"]
        decoded = {
            side: fl.decode_frames(video, frame_lists[side], run_dir / side)
            for side, video in videos.items()
        }
        canvas = fl._canvas(working_snapshot)
        for row in rows:
            if not row["render"]:
                continue
            compared = []
            for sample in row["samples"]:
                old_frame, new_frame = sample["published_frame"], sample["working_frame"]
                old_path = decoded.get("published", {}).get(old_frame) if old_frame is not None else None
                new_path = decoded.get("working", {}).get(new_frame) if new_frame is not None else None
                sample.update(published=str(old_path) if old_path else None,
                              working=str(new_path) if new_path else None)
                if old_path and new_path:
                    share, count, box, size = fl.difference(old_path, new_path)
                    sample.update(share=share, count=count, box=box, size=size, differs=box is not None)
                    compared.append(sample)
                else:
                    sample["differs"] = None
            if row["kind"] == "removed":
                row["verdict"] = "removed cut"
                continue
            if row["kind"] == "new":
                work = next((sample.get("working") for sample in row["samples"]
                             if sample.get("label") == "middle" and sample.get("working")), None)
                span = f"{row['end'] - row['start']:.2f} s with no picture clip ({row['start']:.2f}–{row['end']:.2f} s)"
                if str(row["key"]).startswith("gap at"):  # a gap in the picture: what does the frame show there?
                    if work is None:
                        row["verdict"] = f"no picture clip for {span}; no frame captured"
                    elif _is_black(work):
                        row["verdict"] = f"{BLACK_LABEL}: {span}"
                    else:
                        row["verdict"] = f"no picture clip for {span}; the frame is not black — check coverage"
                else:
                    row["verdict"] = "new cut" + (f" · {BLACK_LABEL}" if work and _is_black(work) else "")
                continue
            differing = [sample for sample in compared if sample["differs"]]
            if not compared:
                row.update(kind="changed", verdict="changed (no decoded frame was captured)")
            elif not differing:
                row.update(kind="same", verdict="same picture (decoded samples are identical)")
            else:
                best = max(differing, key=lambda sample: sample["share"])
                went_black = _is_black(best["working"]) and not _is_black(best["published"])
                row.update(kind="missing" if went_black else "changed", published=best["published"],
                           working=best["working"], label=row["key"], frame=best["working_frame"],
                           t=best["working_frame"] / fps, pixels=_pixel_line(best, canvas),
                           box=best["box"], size=best["size"],
                           differs=[sample["label"] for sample in differing],
                           verdict=(f"{BLACK_LABEL}: the working frame is black where the published one is not"
                                    if went_black else
                                    f"changed ({_pixel_line(best, canvas)}; samples: "
                                    f"{', '.join(sample['label'] for sample in differing)})"))
        changed = [row for row in rows if row["kind"] in ("changed", "missing", "new", "removed")]
        changed_keys = [row["key"] for row in changed]
        unexpected = [key for key in changed_keys if key not in expected_set] if expected_set else []
        same = [row for row in rows if row["kind"] == "same"]
        moved = [row for row in same if abs(float(row.get("moved") or 0)) > 0.5 / fps]
        summary = (f"{len(changed)} cut(s) changed" + (f": {', '.join(changed_keys)}" if changed else "")
                   + (f" (expected: the edit touched {', '.join(sorted(expected_set))})" if expected_set else "")
                   + (f" · UNEXPECTED: {', '.join(unexpected)}" if unexpected else "")
                   + f" · {len(same)} same picture" + (f" ({len(moved)} moved by the re-flow)" if moved else ""))
        visible = [row for row in changed if row.get("published") or row.get("working")]
        page = fl._compose_page(visible, run_dir / "sweep.png", title=f"sweep {timeline} · {len(rows)} cuts · {summary}")
        result = {"cuts": rows, "summary": summary, "unexpected": unexpected, "page": str(page),
                  "timing": {"total_s": round(time.monotonic() - started, 1), "captured": sum(map(len, frame_lists.values())),
                             "cuts": len(rows)}, "sampled": sampled, "notes": []}
        result["lines"] = sweep_lines(result)
        return result
    except fl.FastLaneError:
        import shutil
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    except Exception as exc:
        import shutil
        shutil.rmtree(run_dir, ignore_errors=True)
        raise fl.FastLaneError(str(exc) or type(exc).__name__) from exc

    finally:
        import shutil
        if pair is not None:
            for key in ("base_video", "working_video"):
                video = pair.get(key)
                if isinstance(video, Path) and video.parent.parent == run_dir and video.parent.is_dir():
                    shutil.rmtree(video.parent)


def sweep_lines(result: Mapping[str, Any]) -> list[str]:
    lines = [str(result.get("summary") or "sweep completed")]
    if result.get("sampled"):
        lines.append(str(result["sampled"]))
    lines.extend(f"{row['key']}: {row.get('verdict', row.get('kind', 'unchecked'))}"
                 for row in result.get("cuts") or []
                 if row.get("kind") in ("changed", "missing", "new", "removed", "unchecked"))
    return lines
