"""Checkout: check, lint, picture gaps, stale notes, verify; plus CheckReport and describe_changes."""
from __future__ import annotations

import copy
import dataclasses
import json
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Protocol

from astrid.core.timeline import moments as mo
from astrid.sdk import timeline_intent as intent
from astrid.core.timeline.cuts import (
    occurrences_from_bundle,
    picture_cuts,
)

from astrid.sdk.timeline import store as _store
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.doc import _get_path
from astrid.sdk.timeline.model import Clip
from astrid.sdk.timeline.units import (formula_short, to_canvas)

if TYPE_CHECKING:
    from astrid.sdk.timeline.checkout import Checkout


class _MutableSceneSpan(Protocol):
    """The mutable span shape used by managed Scene's advisory result."""

    start_frame: int
    end_frame: int
    cls: str
    content: str | None


def _broken_head() -> list[str]:
    """``[broken_line()]`` when a pack check module failed to load (so no lint silently lacks a check), else []."""
    try:
        from astrid.packs.rendering.actions.timeline_visualize.layers.base import broken_line
    except Exception:  # the visualize pack is optional for editing
        return []
    line = broken_line()
    return [line] if line else []


_ELEMENT_COVERAGE: dict[str, dict] = {}
_DAMAGED_ART_THRESHOLD = 0.75
_NO_OUTLINE_STYLE = 0.10  # below this the art has no outline by design (sparks, dots)


def _cut_in(table: list[tuple[float, float, str]], t: float) -> str | None:
    """The cut on screen at ``t`` in a ``_cut_table`` (the search ``_cut_on_screen`` makes, without rebuilding it)."""
    return next((cid for lo, hi, cid in reversed(table) if lo - 1e-6 <= t < hi - 1e-6), None)


def element_coverage_cached(element_ids: Iterable[str]) -> dict[str, dict]:
    """``scene_roles.element_coverage`` read once per process per element (the registry load is the cost)."""
    from astrid.core.timeline import scene_roles

    ids = list(element_ids)
    missing = sorted({i for i in ids if i not in _ELEMENT_COVERAGE})
    if missing:
        _ELEMENT_COVERAGE.update(scene_roles.element_coverage(missing))
    return {i: _ELEMENT_COVERAGE[i] for i in ids}


class _CheckMixin:
    """Checkout: check, lint, picture gaps, stale notes, verify; plus CheckReport and describe_changes."""

    def _text_overflow(self) -> list[str]:
        """Type whose measured box leaves title-safe, or has a word wider than its width (the fonts' widths)."""
        from astrid.core.timeline import text_fit
        from astrid.sdk.timeline_address import _safe_note

        out = []
        for clip in self.clips(element="am-type"):
            box = text_fit.type_box(dict(clip.params))
            if box is None:
                return []
            note = _safe_note(box)
            width = float(clip.params.get("width") or 1200)
            font = "label" if clip.params.get("font") == "label" else "display"
            if box["overflow"] > 1 or "outside" in note:
                fits = text_fit.max_size(str(clip.params.get("text") or ""), font=font,
                                         width=min(width, text_fit_safe_width(box, width)), lines=len(box["lines"]))
                out.append(f"{clip.address}: \"{(clip.text or '')[:30]}\" measures {box['w']:.0f}×{box['h']:.0f} px at size "
                           f"{box['size']:g}" + (f"; a word runs {box['overflow']:.0f} px past width {width:g}" if box["overflow"] > 1 else "")
                           + (f"; {note}" if "outside" in note else "") + f" (size {fits:g} fits; or move x/width)")
        return out

    def _on_screen(self: Checkout) -> list:
        """Every frame of the film, covered or not: ``onscreen.coverage`` over the checkout's visual clips in
        timeline seconds (the render config's shape), tracks top first. Run per check, not per clip."""
        from astrid.core.timeline import onscreen

        tracks: list[dict[str, Any]] = []
        seen: set[Any] = set()
        for sid in self._shot_ids():
            for track in self._internal(sid).get("tracks") or []:
                if track.get("id") not in seen:
                    seen.add(track.get("id"))
                    tracks.append(dict(track))
        clips = []
        for clip in self.clips():
            if clip.is_audio:
                continue
            data = dict(clip.data)
            data.update({"clipType": clip.element, "track": clip.track, "at": clip.start, "hold": clip.duration,
                         "app": {"cut": clip.cut_id, "layer": clip.layer_name}})
            clips.append(data)
        visual = ((((self.bundle.get("parent") or {}).get("config") or {}).get("theme_overrides") or {})
                  .get("visual") or {}).get("canvas") or {}
        canvas = {"width": visual.get("width", 1920), "height": visual.get("height", 1080)}
        elements = element_coverage_cached({c["clipType"] for c in clips})
        end_frame = int(round(self.duration * self.fps))
        return onscreen.coverage({"tracks": tracks, "clips": clips}, self.fps, canvas, elements,
                                 start_frame=0, end_frame=end_frame)

    def _showing(self, a: float, b: float) -> list[str]:
        """The addresses of the visual clips that overlap [a, b) in time (what the old picture gap listed)."""
        return sorted({c.address for c in self.clips() if not c.is_audio and c.start < b - 1e-6 and c.end > a + 1e-6})

    def _cut_table(self) -> list[tuple[float, float, str]]:
        """``(start, end, cut id)`` for every cut, in the order ``_cut_on_screen`` searches (built once per check)."""
        return sorted(((lo, hi if hi is not None else self.duration, cid) for cid, (lo, hi) in self._cut_spans().items()))

    def _spans_of(self, cls: str, content: str | None = None, spans: list | None = None) -> list[tuple[Any, float, float]]:
        """(span, start s, end s) for the on-screen spans of one class (and content), at least one frame long."""
        out = []
        for span in (self._on_screen() if spans is None else spans):
            if span.cls != cls or (content is not None and span.content != content):
                continue
            if span.end_frame - span.start_frame < 1:
                continue
            out.append((span, span.start_frame / self.fps, span.end_frame / self.fps))
        return out

    def picture_gaps(self, spans: list | None = None) -> list[str]:
        """GAP spans (TRUTH.md §2.4): frames where no picture covers the canvas and no declared fade or dip is
        under them. The on-screen model decides (element metadata, opacity, bounds), not a list of types.
        Black under whatever is left. Blocking: a re-flow must never tear the film. ``spans``: the film's
        on-screen spans when the caller has them (check() computes them once)."""
        out = []
        table = self._cut_table()
        for span, a, b in self._spans_of("GAP", spans=spans):
            cut = _cut_in(table, a)
            showing = self._showing(a, b)
            out.append(f"{a:.3f}–{b:.3f} s ({b - a:.2f} s{', in ' + cut if cut else ''}): nothing covers the canvas (GAP)"
                       + (f" (only {', '.join(showing[:4])}{' …' if len(showing) > 4 else ''})" if showing else " (black)")
                       + "; give the cut a picture there, or fit its sequence (edit --clip STEP --fit-sequence --until …)")
        return out

    def _picture_windows(self) -> dict[str, tuple[float, float]]:
        """Each cut's picture span: from its first to its last plate-track clip (a cut with no plate: its window)."""
        windows: dict[str, tuple[float, float]] = {}
        spans = self._cut_spans()
        for g in self._cut_groups():
            beds = [c for c in g["clips"] if c.track == "plate" and not c.is_audio]
            if beds:
                windows[g["id"]] = (min(c.start for c in beds), max(c.end for c in beds))
            else:
                lo, hi = spans.get(g["id"], (g["start"], None))
                windows[g["id"]] = (lo, hi if hi is not None else self.duration)
        return {cid: w for cid, w in windows.items() if w[1] - w[0] > 0}

    def empty_cuts(self, spans: list | None = None) -> tuple[list[str], list[str]]:
        """(blocking, info) for EMPTY spans: only a background on screen (TRUTH.md §2.4). An EMPTY span covering
        at least 90% of a cut's picture span blocks, unless that cut is a deliberate hold (then info). A shorter
        one of at least 1.0 s is info."""
        held = {cid for cid, note in self.cut_notes_all().items() if note.get("hold") == "deliberate"}
        windows = self._picture_windows()
        fps = self.fps
        blocking: list[str] = []
        info: list[str] = []
        table = self._cut_table()
        for span, a, b in self._spans_of("COVERED", "EMPTY", spans=spans):
            owner = None
            for cid, (w0, w1) in windows.items():
                overlap = min(b, w1) - max(a, w0)
                if overlap > 0 and overlap * fps >= 0.9 * (w1 - w0) * fps - 1e-6:
                    owner = cid
                    break
            cut = owner or _cut_in(table, a)
            if owner is None and b - a < 1.0:
                continue
            showing = self._showing(a, b)
            line = (f"EMPTY   {cut or 'no cut'} {a:.2f}–{b:.2f} s ({b - a:.2f} s): only a background "
                    f"({', '.join(showing[:4]) or 'black'}) on screen; give it a picture or layers, or mark it --hold deliberate")
            if owner is not None and owner not in held:
                blocking.append(line)
            else:
                info.append(line)
        return blocking, info

    def dips(self, spans: list | None = None) -> list[str]:
        """DIP spans: frames under a declared fade or dip on the picture. Intended, so info (never blocking)."""
        out = []
        table = self._cut_table()
        for _span, a, b in self._spans_of("DIP", spans=spans):
            out.append(f"DIP     {_cut_in(table, a) or 'no cut'} {a:.2f}–{b:.2f} s ({b - a:.2f} s): "
                       "a declared fade/dip (not a gap)")
        return out

    @staticmethod
    def _scene_warning_spans(spans: list) -> list:
        """Project adjacent equal coverage/content rows for display warnings only.

        Scene answers retain per-frame membership, findings and identity.  The
        warning format has no use for membership, so a fade whose visible
        membership changes can still be reported as one continuous DIP.  Copy
        rows before extending them so the detailed answer rows are untouched.
        """
        from astrid.core.timeline.onscreen import Span

        summary: list[Span | _MutableSceneSpan] = []
        for span in spans:
            if (summary and summary[-1].end_frame == span.start_frame
                    and summary[-1].cls == span.cls and summary[-1].content == span.content):
                previous = summary[-1]
                if isinstance(previous, Span):
                    summary[-1] = dataclasses.replace(previous, end_frame=span.end_frame)
                else:
                    merged = copy.copy(previous)
                    object.__setattr__(merged, "end_frame", span.end_frame)
                    summary[-1] = merged
            else:
                summary.append(copy.copy(span))
        return summary

    def _scene_problems(self, spans: list) -> list[str]:
        """Advisory scene findings, retaining cut addresses and deliberate-hold classification."""
        warning_spans = self._scene_warning_spans(spans)
        sustained, incidental = self.empty_cuts(warning_spans)
        out = [f"picture {line}" for line in self.picture_gaps(warning_spans)]
        out += sustained + incidental + self.dips(warning_spans)
        for span in spans:
            if span.cls not in {"UNKNOWN", "UNAVAILABLE"} and span.content != "UNKNOWN":
                continue
            kind = span.cls if span.cls in {"UNKNOWN", "UNAVAILABLE"} else "CONTENT UNKNOWN"
            out.append(f"scene   {kind} {span.start_frame / self.fps:.3f}–{span.end_frame / self.fps:.3f} s"
                       + (f": {', '.join(span.findings)}" if span.findings else ""))
        return out

    def _managed_scene_spans(self: Checkout) -> list | None:
        """Read Scene advice through an explicitly connected checkout, if bound.

        Save and detached checkouts have no managed target binding and retain
        the local structural coverage reader.  A connected checkout treats
        a missing or stale managed result as advisory unavailability instead
        of falling back to a different source of scene truth.
        """
        client = getattr(self, "_scene_client", None)
        project = getattr(self, "_scene_project", None)
        timeline = getattr(self, "_scene_timeline", None)
        if client is None or not project or not timeline:
            return None
        from astrid.sdk.timeline_filmstrip import (
            _invoke_scene_only,
            _scene_only_candidate_request,
            _scene_only_unavailable,
        )

        try:
            if self.path:
                request, _context = _scene_only_candidate_request(
                    self.document(), project=project, timeline_ref=timeline, client=client,
                )
                # The candidate request is already frozen through the reviewed
                # helper; _invoke_scene_only reuses that same managed path.
                spans, _prepared = _invoke_scene_only(
                    request, project=project, client=client,
                )
                return spans
            spans, _prepared = _invoke_scene_only(
                {"timeline_ref": timeline}, project=project, client=client,
            )
            return spans
        except Exception:  # noqa: BLE001 - diagnostics remain advisory
            return _scene_only_unavailable()

    def _length_problems(self) -> list[str]:
        """Visible clips that run past the end of their shot's placement: the render stops at the shot's end, so
        they would be cut off. Audio is exempt (a music bed runs out with the film). Blocking."""
        out = []
        tolerance = 0.5 / self.fps
        for clip in self.clips():
            if clip.is_audio:
                continue
            shot_end = self._shot_start(clip.shot_id) + self._shot_length(clip.shot_id)
            if clip.end > shot_end + tolerance:
                out.append(f"{clip.address} ends at {clip.end:.2f} s, after its shot ends at {shot_end:.2f} s: "
                           "the render would cut it off; extend its shot's picture or shorten the clip")
        return out

    def _sprite_problems(self) -> tuple[list[str], list[str]]:
        """(blocking, notes) for pixel sprites: canvas bounds, simultaneous scale, and source integrity."""
        from astrid.core.timeline import onscreen

        canvas = ((((self.bundle.get("parent") or {}).get("config") or {}).get("theme_overrides") or {})
                  .get("visual") or {}).get("canvas") or {}
        width, height = float(canvas.get("width", 1920)), float(canvas.get("height", 1080))
        blocking: list[str] = []
        notes: list[str] = []
        sprites = [clip for clip in self.clips() if not clip.is_audio and clip.element == "am-sprite"]
        for clip in sprites:
            params = dict(clip.params)
            entry = self._sprite_asset_entry(clip)
            resolution = str(entry.get("resolution") or "")
            match = re.fullmatch(r"(\d+)x(\d+)", resolution)
            if match:
                params["width"], params["height"] = map(int, match.groups())
                params["scale"] = params.get("scale", 6)
            else:
                # Without native dimensions, scale cannot meaningfully enlarge the model's canvas fallback.
                params.pop("scale", None)
            # x/y and their stepped keyframes share the element's declared logical units.
            x, y = params.get("x", 0), params.get("y", 0)
            step_frames = int(params.get("stepFrames", 2) or 2)
            poses = [(0, x, y)]
            keyframes = params.get("keyframes") if isinstance(params.get("keyframes"), list) else []
            for key in sorted((item for item in keyframes if isinstance(item, Mapping)
                               and isinstance(item.get("frame"), (int, float))), key=lambda item: item["frame"]):
                x, y = key.get("x", x), key.get("y", y)
                effective_frame = math.floor(float(key["frame"]) / step_frames) * step_frames
                poses.append((effective_frame, x, y))
            # A sprite that runs in from off screen is on canvas for part of its clip: that is
            # deliberate. Only a sprite that is never on canvas (every pose outside) is an error.
            measured, visible = 0, False
            for frame, px_x, px_y in poses:
                layout = dict(params)
                if px_x is not None:
                    layout["x"] = to_canvas(clip.element, "x", px_x)
                if px_y is not None:
                    layout["y"] = to_canvas(clip.element, "y", px_y)
                bounds, _source = onscreen.layout_bounds({"params": layout}, width, height)
                if bounds is None:
                    continue
                measured += 1
                x0, y0, x1, y1 = bounds
                if not (x1 <= 0 or y1 <= 0 or x0 >= width or y0 >= height):
                    visible = True
                    break
            if measured and not visible:
                blocking.append(f"OFFCANVAS {clip.address}: sprite is never inside the {width:g}×{height:g} canvas "
                                f"({clip.start:.2f}–{clip.end:.2f} s); move it into frame")

        # scale is the authored pixel-art size. Compare only time-overlapping sprites; equal values
        # share one art-pixel size even when they come from different sources.
        scale_rows = []
        for clip in sprites:
            params = clip.data.get("params") if isinstance(clip.data.get("params"), Mapping) else {}
            if "scale" in clip.data:
                value = clip.data["scale"]
            else:
                from astrid.sdk.timeline_address import element_schema

                schema = element_schema(clip.element)
                default = (((schema.get("properties") or {}).get("scale") or {}).get("default", 1))
                value = params.get("scale", default)
            try:
                scale = float(value)
            except (TypeError, ValueError):
                continue
            if scale >= 2:  # scale 1 draws an image at its native size (e.g. the real app icon): not pixel art
                scale_rows.append((clip, scale))
        reported: set[tuple[str, str, float, float]] = set()
        for index, (left, left_scale) in enumerate(scale_rows):
            for right, right_scale in scale_rows[index + 1:]:
                start, end = max(left.start, right.start), min(left.end, right.end)
                if end <= start + 1e-6 or abs(left_scale - right_scale) <= 1e-6:
                    continue
                pair = (left.address, right.address, left_scale, right_scale)
                if pair in reported:
                    continue
                reported.add(pair)
                notes.append(f"MIXED-SCALE {left.address} (scale {left_scale:g}) and {right.address} "
                             f"(scale {right_scale:g}) are on screen together at {start:.2f}–{end:.2f} s")

        for clip in sprites:
            if not clip.asset:
                continue
            entry = self._sprite_asset_entry(clip)
            provenance = entry.get("pixel_provenance") if isinstance(entry.get("pixel_provenance"), Mapping) else {}
            integrity = provenance.get("outline_integrity")
            if isinstance(integrity, Mapping):
                integrity = integrity.get("fraction")
            if integrity is None:
                integrity = self._native_outline_integrity(entry)
            try:
                score = float(integrity)
            except (TypeError, ValueError):
                continue
            # Damage is a partly broken outline (a lossy re-snap). Art drawn with no outline at all
            # (sparks, dots: integrity near 0) is a style, not damage.
            if _NO_OUTLINE_STYLE <= score < _DAMAGED_ART_THRESHOLD:
                notes.append(f"DAMAGED-ART {clip.address}: {clip.asset} outline integrity {score:.2f} "
                             f"(threshold {_DAMAGED_ART_THRESHOLD:.2f})")
        return blocking, notes

    def _sprite_asset_entry(self, clip: Clip) -> Mapping[str, Any]:
        registry = self._internal(clip.shot_id).get("registry") or {}
        assets = registry.get("assets") if isinstance(registry, Mapping) else {}
        entry = assets.get(clip.asset) if clip.asset and isinstance(assets, Mapping) else None
        return entry if isinstance(entry, Mapping) else {}

    def _native_outline_integrity(self, entry: Mapping[str, Any]) -> float | None:
        """Measure a registered native image from its path or an existing media-view copy."""
        raw_path = entry.get("local_path") or entry.get("path")
        from pathlib import Path

        source: Any = None
        if isinstance(raw_path, str):
            path = Path(raw_path).expanduser()
            if path.is_file() and path.stat().st_size <= 64 * 1024 * 1024:
                source = str(path)
        else:
            media_id = entry.get("media_id") or entry.get("content_sha256")
            if not isinstance(media_id, str) or not media_id.startswith("sha256:"):
                return None
            cache = self.__dict__.setdefault("_art_bytes", {})
            if media_id not in cache:  # the original, read by digest in memory: never a local copy
                try:
                    from astrid.sdk import AstridClient

                    with AstridClient.open_from_launcher(start_pack_host=False) as client:
                        cache[media_id] = client.media.read_bytes(media_id)
                except Exception:  # noqa: BLE001 - no runtime (offline check) is an ordinary case
                    cache[media_id] = None
            if cache[media_id] is None:
                return None
            import io

            source = io.BytesIO(cache[media_id])
        if source is None:
            return None
        # The imports are outside the try: a missing module is a bug to see, not "no measurement".
        import numpy as np
        from PIL import Image

        from astrid.packs.pixel.actions import _common as pixel

        try:
            rgba = pixel.load_rgba(source) if isinstance(source, str) else np.asarray(Image.open(source).convert("RGBA"))
        except (OSError, ValueError):  # an unreadable or non-image file: nothing to measure
            return None
        if (rgba[..., 3] < 255).any():
            return float(pixel.outline_integrity(rgba)["fraction"])
        return None

    def _stale_whys(self) -> list[str]:
        from astrid.sdk.timeline.checkout import Checkout
        """Cuts whose why may no longer say what is there: written when the cut had other layers, or its
        layers changed (a swap, a replace) since checkout while the why stayed the same. Info, never a block."""
        from astrid.sdk.timeline_cuts import base_bundle

        out = []
        notes = self.cut_notes_all()
        try:
            base = Checkout(base_bundle(self.document()))
            then_groups = {g["id"]: g for g in base._cut_groups()}
            then_notes = base.cut_notes_all()
        except Exception:  # noqa: BLE001
            then_groups, then_notes = {}, {}

        def signature(clips: Iterable[Clip]) -> list[str]:
            return sorted(f"{intent.layer_of(c.data) or c.id}:{c.element}:{c.asset or ''}" for c in clips)

        for g in self._cut_groups():
            note = notes.get(g["id"], {})
            why = note.get("why")
            old = then_groups.get(g["id"])
            if why and old and then_notes.get(g["id"], {}).get("why") == why and not note.get("why_layers") \
                    and (signature(old["clips"]) != signature(g["clips"])
                         or (round(old["start"] * self.fps), len(old["clips"])) != (round(g["start"] * self.fps), len(g["clips"]))
                         or _span_frames(old["clips"], self.fps) != _span_frames(g["clips"], self.fps)):
                out.append(f"{g['id']}: its why was written before this change; still true? \"{why}\" "
                           f"(edit --cut {g['id']} --why …)")
        for g in self._cut_groups():
            note = notes.get(g["id"], {})
            then = note.get("why_layers")
            if not then or not note.get("why"):
                continue
            now = sorted(intent.layer_of(c.data) or c.id for c in g["clips"])
            if now != list(then):
                gone, new = sorted(set(then) - set(now)), sorted(set(now) - set(then))
                out.append(f"{g['id']}: its why was written when it had other layers ("
                           + " · ".join(x for x in (f"since added {', '.join(new)}" if new else "",
                                                    f"since removed {', '.join(gone)}" if gone else "") if x)
                           + f"); still true? \"{note['why']}\" (edit --cut {g['id']} --why …)")
        return out

    def _note_bare_word(self, moment: Any, clip: Clip) -> None:
        """A bare word that is also a layer's name (``live`` vs c11.live) is read as the spoken word: say so."""
        if not isinstance(moment, str) or not re.fullmatch(r"[\w'-]+", moment.strip()):
            return
        name = moment.strip()
        layers = sorted({c.address for c in self.clips() if intent.layer_of(c.data) == name})
        if layers:
            self.notes.append(f"{clip.address}: {name} is read as the spoken word \"{name}\" (there is also a layer "
                              f"{', '.join(layers[:3])}; for where it ends write until end of its cut, or quote the word)")

    def expected_cuts(self, since: Mapping[str, Any] | None = None) -> list[str]:
        """The cuts the edits since ``since`` (default: checkout) meant to change (not ones a re-flow only moved)."""
        from astrid.packs.rendering.actions.timeline_visualize import sweep as sweep_mod
        from astrid.sdk.timeline.checkout import Checkout
        from astrid.sdk.timeline_cuts import base_bundle, diff_bundles

        before = dict(since) if since is not None else base_bundle(self.document())
        cut_of = {c.id: intent.cut_of(c.data) for c in Checkout(copy.deepcopy(before)).clips()}
        cut_of.update({c.id: intent.cut_of(c.data) for c in self.clips()})
        return sorted(sweep_mod.expected_cuts(diff_bundles(before, self.document())["changes"], cut_of))

    def verify(self, at: Any = None, *, since: Mapping[str, Any] | None = None, client: Any = None,
               zoom: str | None = None, region: Any = None, sweep: bool = False,
               labels: bool = False) -> dict[str, Any]:
        """One-shot verify (the CLI's ``edit --verify`` / ``check --at``): the published head and this working copy
        at the moments, on one page, with what differs and the new lint. ``at``: a moment, an address or a list
        of them; by default the frames that show what changed (since ``since``, a document, else since checkout).
        ``zoom="c08.app"`` (or ``region="x,y,w,h"`` in canvas px) adds a full-resolution crop of both sides around
        that layer under each moment. ``sweep=True`` compares the whole film instead (the timeline sweep: every
        cut, three frames each; the cuts the edits did not mean to change are UNEXPECTED). Returns the fast lane's
        result (``page``, ``rows`` with what each side shows and what differs per moment, ``lines`` to print).
        ``labels=True`` draws each visible layer's address on the page's tiles (the ``labels`` overlay)."""
        from astrid.packs.rendering.actions.timeline_visualize import fast_lane
        from astrid.packs.rendering.actions.timeline_visualize import sweep as sweep_mod
        from astrid.sdk.timeline_cuts import base_bundle, diff_bundles

        if sweep:
            expected = self.expected_cuts(since)
            project, timeline = str(self.bundle.get("project_id")), str(self.bundle.get("timeline_id"))
            return _store._with_client(client, lambda c: sweep_mod.sweep(c, project, timeline, draft=self,
                                                                          expected=expected))
        if at is not None:
            items = at if isinstance(at, (list, tuple)) else [at]
            moments = [fast_lane.Moment(self.time(m), str(m)) for m in items]
        else:
            before = dict(since) if since is not None else base_bundle(self.document())
            diff = diff_bundles(before, self.document())
            names = {c.id: c.address for c in self.clips()}
            moments = fast_lane.moments_from_changes(diff["changes"], float(diff.get("fps") or self.fps), names=names)
            if not moments and zoom is not None:  # nothing changed on screen: look at the zoomed layer's middle
                layer = self.clip(zoom)
                moments = [fast_lane.Moment((layer.start + layer.end) / 2, f"the middle of {layer.address}", layer.id)]
            if not moments:
                raise TimelineEditError("nothing on screen changed: no frame to compare (pass at=…)")
        footer = self.check().brief()
        project, timeline = str(self.bundle.get("project_id")), str(self.bundle.get("timeline_id"))

        def run(c: Any) -> dict[str, Any]:
            return fast_lane.verify(c, project, timeline, moments, draft=self, footer=footer,
                                    **({"zoom": zoom} if zoom else {}), **({"region": region} if region else {}),
                                    **({"labels": True} if labels else {}))

        return _store._with_client(client, run)

    # ---- check / publish ----------------------------------------------------
    def lint(self, *, cuts: Iterable[int] | None = None) -> list[str]:
        """Lint lines for this candidate (the same checks as ``timelines lint``), optionally only some cuts."""
        head = _broken_head()
        try:
            from astrid.packs.rendering.actions.timeline_visualize.motion import model
            from astrid.packs.rendering.actions.timeline_visualize.motion.conditions import run_checks
        except Exception as exc:  # the visualize pack is optional for editing
            return head + [f"(lint unavailable here: {exc})"]
        occurrences = occurrences_from_bundle(self.bundle)
        all_cuts = picture_cuts(occurrences, fps=self.fps)
        wanted = set(cuts) if cuts is not None else None
        selected = [c for c in all_cuts if wanted is None or c["index"] in wanted]
        elements = model.elements_from_occurrences(occurrences)
        order = next((o.get("track_order") for o in occurrences if o.get("track_order")), [])
        results, timeline_findings = run_checks(selected, elements, self.fps, track_order=order, all_cuts=all_cuts)
        lines = [f.line() for f in timeline_findings if f.severity != "info"]
        for cut, findings in results:
            lines += [f.line(cut["start"]) for f in findings if f.severity != "info"]
        return head + lines

    def _type_mismatches(self) -> list[str]:
        """Params whose value has the wrong type: text where the element (or a formula) wants a number."""
        from astrid.sdk.timeline_address import element_schema

        out = []
        for clip in self.clips():
            params = clip.data.get("params") if isinstance(clip.data.get("params"), dict) else {}
            if clip.is_audio:
                from astrid.core.timeline.audio_curves import validate as validate_audio_curve

                for key in ("fadeInCurve", "fadeOutCurve"):
                    if key in params:
                        try:
                            validate_audio_curve(params[key])
                        except (TypeError, ValueError) as exc:
                            out.append(f"{clip.address}.{key} is invalid: {exc}")
            props = (element_schema(clip.element).get("properties") or {}) if params else {}
            for key, value in params.items():
                for path, text, want in _text_where_number(key, value, props.get(key) or {}):
                    stripped = text.strip()
                    moment = stripped[2:-1] if stripped.startswith("ƒ(") and stripped.endswith(")") else stripped
                    hint = (f"; to put it on a moment: --set '{path}=ƒ({moment})' (or a number)"
                            if moment.startswith(('"', "“", "after ", "beat ", "downbeat ", "c")) else "")
                    out.append(f"{clip.address}.{path} is the text {text!r} where {clip.element} wants a {want}{hint}")
            for path, expr in intent.formulas(clip.data).items():
                if isinstance(expr, Mapping) and expr.get("as") == "clip_frame":
                    value = _get_path(clip.data, path)
                    if value is not None and not (isinstance(value, int) and not isinstance(value, bool)):
                        out.append(f"{clip.address}.{path.removeprefix('params.')} is {value!r} where a frame number belongs")
        return out

    def check(self) -> "CheckReport":
        """Validate, say what moved (timeline seconds, against the checked-out head) and lint the changed cuts."""
        return self._check(diagnostics=True)

    def _check_core(self) -> "CheckReport":
        """Save-time structure, timing and reference validity without scene or renderer diagnostics."""
        return self._check(diagnostics=False)

    def _check(self, *, diagnostics: bool) -> "CheckReport":
        from astrid.core.timeline.authoring_bundle import validate_authoring_candidate
        from astrid.sdk.timeline.checkout import Checkout
        from astrid.sdk.timeline_cuts import base_bundle, diff_bundles, render_diff

        resolved = self.resolve()  # formulas first: every anchored clip back on its word, every expression recalculated
        candidate = self.document()
        problems: list[str] = [f"resolved {line}" for line in resolved]
        try:
            validation = validate_authoring_candidate(candidate)
            valid = bool(validation.get("valid", True)) if isinstance(validation, Mapping) else True
        except Exception as exc:  # AuthoringBundleError and friends: one actionable line
            valid, validation = False, {"error": str(exc)}
            problems.append(f"invalid: {exc}")
        diff = diff_bundles(base_bundle(candidate), candidate)
        summary = render_diff(diff).splitlines()
        changed = {c["clip_id"] for c in diff["changes"]}
        cut_numbers = sorted({cut.n for cut in self.cuts for clip in cut.clips if clip.id in changed})
        from astrid.sdk.timeline_address import ordinals_to_ids

        lint = [ordinals_to_ids(line, self) for line in self.lint(cuts=cut_numbers)] if diagnostics and cut_numbers else []
        lint_new, lint_old = _new_findings(lint, Checkout(base_bundle(candidate))) if diagnostics else ([], 0)
        names = []
        for n in cut_numbers:
            pic = self.cuts[n - 1].picture
            names.append(intent.cut_of(pic.data) if pic is not None and intent.cut_of(pic.data) else f"cut {n}")
        # Blocking: moments whose word is gone (orphans) and clips with no length. Publish refuses until
        # each is re-homed (a new moment) or removed.
        blocking = [f"orphan  {line}" for line in self.orphans()]
        blocking += [f"empty   {c.address} has no length ({c.start:.3f}–{c.end:.3f} s): give it a length or remove it"
                     for c in self.clips() if not c.is_audio and c.duration < 0.5 / self.fps]
        blocking += [f"outside {line}" for line in self._outside]
        blocking += [f"type    {line}" for line in self._type_mismatches()]
        blocking += [f"beats   {line}" for line in self._stale_beats()]
        blocking += [f"length  {line}" for line in self._length_problems()]
        if diagnostics:
            spans = self._managed_scene_spans()
            if spans is None:
                spans = self._on_screen()  # one pass over the film for the three readers below
            problems += self._scene_problems(spans)
            sprite_blocking, sprite_notes = self._sprite_problems()
            blocking += sprite_blocking
            problems += [f"why     {line}" for line in self._stale_whys()]
            problems += sprite_notes
            problems += [f"text    {line}" for line in self._text_overflow()]
            from astrid.sdk.timeline.look import look_unrendered

            problems += [f"look    {line}" for line in look_unrendered(self)]  # set, but this composition will not show it
            music_end = max((c.end for c in self.clips(audio=True) if c.track == "music"), default=None)
            if music_end is not None and music_end < self.duration - 0.5:
                problems.append(f"music: the bed ends at {music_end:.2f} s; the film runs {self.duration:.2f} s "
                                f"({self.duration - music_end:.1f} s without music)")
        if blocking:
            valid = False
        return CheckReport(valid=valid, validation=validation, summary=summary, lint=lint,
                           problems=blocking + problems + list(dict.fromkeys(self.notes)), changed_cuts=cut_numbers,
                           diff=diff, cut_names=names, blocking=blocking, lint_new=lint_new, lint_old=lint_old,
                           addresses={c.id: c.address for c in self.clips() if c.address != c.id})


@dataclass
class CheckReport:
    valid: bool
    validation: Any
    summary: list[str]
    lint: list[str]
    problems: list[str]
    changed_cuts: list[int]
    diff: Mapping[str, Any]
    cut_names: list[str] = dataclasses.field(default_factory=list)
    blocking: list[str] = dataclasses.field(default_factory=list)
    lint_new: list[str] | None = None   # lint findings the published head does not have (None: not computed)
    lint_old: int = 0                    # findings on the changed cuts that were already there
    addresses: Mapping[str, str] = dataclasses.field(default_factory=dict)  # clip id → its address (c30.cover)

    def brief(self, *, full: bool = False, cuts: Iterable[str] | None = None) -> list[str]:
        """Calm lines for after an edit: valid or not, what blocks publishing, and NEW lint (findings the
        published head does not have; the ones that were already there are counted). ``cuts`` (cut ids):
        only what concerns those cuts, with a count of the rest. ``full`` (``--all``): every line."""
        new = list(self.lint if full or self.lint_new is None else self.lint_new)
        old = 0 if full or self.lint_new is None else self.lint_old
        blocking, notes = list(self.blocking), [p for p in self.problems if p not in self.blocking and not p.startswith("resolved ")]
        said = set()  # a note about a param that a blocking line already names is said once
        for line in blocking:
            m = re.match(r"^\w+\s+(\S+?)(?:\s+params\.(\S+))?\s+=", line)
            if m:
                said.add(f"{m.group(1)}.{m.group(2)}" if m.group(2) else m.group(1))
        notes = [p for p in notes if p.split(": ", 1)[0] not in said]
        elsewhere = 0
        if cuts is not None and not full:
            wanted = {c for c in cuts if c}
            pattern = re.compile(r"\b(" + "|".join(map(re.escape, sorted(wanted))) + r")\b") if wanted else None

            def mine(line: str) -> bool:
                return bool(pattern and pattern.search(line))

            kept = [line for line in new if mine(line)]
            kept_blocking = [line for line in blocking if mine(line)]
            elsewhere = (len(new) - len(kept)) + (len(blocking) - len(kept_blocking))
            new, blocking = kept, kept_blocking
        head = "check   " + ("valid" if self.valid else "NOT VALID")
        if self.blocking:
            head += f" · {len(self.blocking)} to fix before publishing" + (
                f" ({len(blocking)} here)" if cuts is not None and not full else " (re-home each orphan with a new moment, or remove it)")
        if cuts is not None and not full:
            where = ", ".join(sorted(set(cuts))) or "no cut"
            head += f" · new lint on {where if len(where) <= 60 else where[:57] + '…'}: " + ("clean" if not new else f"{len(new)}")
        else:
            where = ", ".join(self.cut_names or [f"cut {n}" for n in self.changed_cuts])
            if where:
                head += f" · new lint on the {len(self.cut_names or self.changed_cuts)} changed cut(s): " + ("clean" if not new else f"{len(new)}")
        if full or len(blocking) <= 8:
            shown_blocking = [f"  ! {p}" for p in blocking]
        else:
            by_cut: dict[str, int] = {}
            for line in blocking:
                address = line.split()[1] if len(line.split()) > 1 else "?"
                by_cut[address.split(".")[0]] = by_cut.get(address.split(".")[0], 0) + 1
            shown_blocking = [f"  ! {len(blocking)} orphan(s) in {len(by_cut)} cut(s): "
                              + ", ".join(f"{cut} ({n})" for cut, n in by_cut.items()),
                              "    each one: timelines status TL --project P --all   ·   the sheet's orphans section"]
        lint = [f"  {line}" for line in new] if full or len(new) <= 12 else (
            [f"  {line}" for line in new[:12]] + [f"  … {len(new) - 12} more new finding(s) (timelines check TL --all)"])
        tail = []
        if old or elsewhere:
            tail.append("  (" + " · ".join(x for x in (
                f"{elsewhere} more on other cuts" if elsewhere else "",
                f"{old} lint finding(s) were already there before your changes" if old else "") if x)
                + ": timelines check TL --all)")
        return [head] + shown_blocking + [f"  · {p}" for p in notes[:12]] + lint + tail

    def full_text(self) -> str:
        """Everything: the whole diff summary, every problem, every lint line on the changed cuts."""
        lines = [("valid" if self.valid else "INVALID") + " · " + (self.summary[0] if self.summary else "no changes")]
        lines += self.summary[1:]
        lines += [f"! {p}" for p in self.problems]
        if self.changed_cuts:
            lines.append(f"lint on changed cuts {', '.join(self.cut_names or map(str, self.changed_cuts))}: "
                         + ("clean" if not self.lint else f"{len(self.lint)} line(s)"))
            lines += [f"  {line}" for line in self.lint]
        return "\n".join(lines)

    def __str__(self) -> str:
        """The default report: the diff in brief, what blocks publishing, notes, and NEW lint (counts for the rest)."""
        head = re.sub(r"\bcuts (\d+) → (\d+)", r"pictures \1 → \2", self.summary[0]) if self.summary else "no changes"
        lines = [("valid" if self.valid else "INVALID") + " · " + head]
        kinds: dict[str, int] = {}
        rest = []
        for line in self.summary[1:]:  # "cut added at 7.37 s" × 44 → "44 cuts added"
            m = re.match(r"^cuts? (added|removed|moved)\b", line.strip())
            if m:
                kinds[m.group(1)] = kinds.get(m.group(1), 0) + 1
            else:
                rest.append(line)
        if kinds:
            lines.append("  cut points: " + " · ".join(f"{n} {what}" for what, n in kinds.items()))
        def plain(line: str) -> str:  # addresses, not clip ids; "app" is the clip's intent
            line = re.sub(r"\b[\w.-]+\b", lambda m: self.addresses.get(m.group(0), m.group(0)), line)
            line = line.replace("(note changed)", "(why note)").replace(", note changed)", ", why note)")
            return line.replace("app changed", "intent changed: moments, cut, words").replace(", app,", ", intent,")
        lines += [plain(line) for line in rest[:10]] + ([f"  … {len(rest) - 10} more clip lines"] if len(rest) > 10 else [])
        if kinds or len(rest) > 10:
            lines.append("  (every line: timelines check TL --all · timelines diff TL)")
        orphans = sum(1 for p in self.blocking if p.startswith("orphan"))
        lines.append(f"  {orphans} orphan(s) · {len(self.blocking)} to fix before publishing")
        lines += self.brief()[1:]
        return "\n".join(lines)


# ----------------------------------------------------------------- document helpers

def _end_rule(tl: "Checkout", clip: Clip) -> str:  # noqa: F821
    """How a clip's end is decided, in words: until "Astrid" · for 1.9s · with its cut · its own length."""
    until, length = intent.until(clip.data), intent.for_s(clip.data)
    if until:
        return f"holds until {until}"
    if length is not None:
        return f"holds for {mo.offset_text(length, tl.fps).lstrip('+')}"
    if intent.cut_of(clip.data) and not clip.is_audio:
        return "ends with its cut"
    return "keeps its own length"


def _text_where_number(path: str, value: Any, spec: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """``(param path, the text, the type wanted)`` for every string where the schema wants a number
    (``allAt: 'ƒ("adapt" in w05c)'``, ``states[3].at: '"adapt"'``), nested lists and objects included."""
    want = spec.get("type")
    wants = want if isinstance(want, list) else [want]
    if isinstance(value, str):
        numeric = [w for w in wants if w in ("integer", "number")]
        return [(path, value, numeric[0])] if numeric and "string" not in wants else []
    out: list[tuple[str, str, str]] = []
    if isinstance(value, list) and isinstance(spec.get("items"), Mapping):
        for i, item in enumerate(value):
            out += _text_where_number(f"{path}[{i}]", item, spec["items"])
    elif isinstance(value, Mapping) and isinstance(spec.get("properties"), Mapping):
        for key, item in value.items():
            if isinstance(spec["properties"].get(key), Mapping):
                out += _text_where_number(f"{path}.{key}", item, spec["properties"][key])
    return out


def text_fit_safe_width(box: Mapping[str, Any], width: float) -> float:
    """The width a type box can use from where it starts to the title-safe right edge (1824 px)."""
    return max(1.0, 1824.0 - float(box["x0"]))


def _span_frames(clips: Iterable[Clip], fps: float) -> tuple[int, int]:
    """A group's picture span in frames (to tell a re-fit from no change)."""
    clips = list(clips)
    if not clips:
        return (0, 0)
    return (round(min(c.start for c in clips) * fps), round(max(c.end for c in clips) * fps))


def _lint_key(line: str) -> str:
    """A finding without its numbers: the same finding on a cut that moved is the same finding."""
    return re.sub(r"[-+]?\d+(?:\.\d+)?", "#", line)


def _new_findings(lint: list[str], base: "Checkout") -> tuple[list[str] | None, int]:  # noqa: F821
    """(findings the base does not have, how many it already had)."""
    from collections import Counter

    from astrid.sdk.timeline_address import ordinals_to_ids

    if not lint:
        return [], 0
    try:
        seen = Counter(_lint_key(ordinals_to_ids(line, base)) for line in base.lint())
    except Exception:  # noqa: BLE001 - without a base lint every finding counts as new
        return None, 0
    new, old = [], 0
    for line in lint:
        key = _lint_key(line)
        if seen[key] > 0:
            seen[key] -= 1
            old += 1
        else:
            new.append(line)
    return new, old


def describe_changes(before: "Checkout", after: "Checkout") -> list[str]:  # noqa: F821
    """Plain-words lines for what changed between two versions of a timeline (see ``Checkout.changes``)."""
    old = {c.id: c for c in before.clips()}
    new = {c.id: c for c in after.clips()}
    fps = after.fps
    lines: list[tuple[float, str]] = []
    shifts: dict[int, list[Clip]] = {}

    def secs(delta: float) -> str:
        return f"{delta:+.2f} s"

    width = max([len(c.address) for c in list(new.values()) + list(old.values())] + [8])
    for cid, clip in new.items():
        prev = old.get(cid)
        if prev is None:
            what = " ".join(x for x in (clip.element.removeprefix("am-"), clip.asset or (f'"{clip.text[:24]}"' if clip.text else "")) if x)
            on = f" on {clip.anchor}" if clip.anchor else ""
            lines.append((clip.start, f"+ {clip.address:<{width}}  added: {what}{on}  {clip.start:.3f}–{clip.end:.3f} s"))
            continue
        said: list[str] = []
        moved_start = round(prev.start * fps) != round(clip.start * fps)
        moved_end = round(prev.end * fps) != round(clip.end * fps)
        a_on, b_on = intent.on(prev.data), intent.on(clip.data)
        a_until, b_until = intent.until(prev.data), intent.until(clip.data)
        a_for, b_for = intent.for_s(prev.data), intent.for_s(clip.data)
        if a_on != b_on:
            what = f"now enters on {b_on} = {clip.start:.2f} s" if b_on else f"no longer tied to a moment: starts {clip.start:.2f} s"
            what += f" (was {prev.start:.2f} s, {secs(clip.start - prev.start)})" if moved_start else " (it was already there)"
            if not (a_until != b_until or a_for != b_for):
                what += f"; {_end_rule(after, clip)}, ending {clip.end:.2f} s"
            said.append(what)
            moved_start = False
        if a_until != b_until or a_for != b_for:
            what = f"now {_end_rule(after, clip)} = ends {clip.end:.2f} s"
            what += f" (was {prev.end:.2f} s, {secs(clip.end - prev.end)})" if moved_end else " (it already ended there)"
            said.append(what)
            moved_end = False
        if (clip.asset or None) != (prev.asset or None):
            said.append(f"asset {prev.asset} → {clip.asset}")
        if clip.track != prev.track:
            said.append(f"track {prev.track} → {clip.track} (stacking: chrome > type > fx > sprite > plate)")

        if clip.element != prev.element:
            said.append(f"element {prev.element} → {clip.element}")
        fa, fb = intent.formulas(prev.data), intent.formulas(clip.data)
        formula_keys = set()
        for path in sorted(set(fa) | set(fb)):
            a_expr, b_expr = fa.get(path), fb.get(path)
            if a_expr == b_expr:
                continue
            key = path.removeprefix("params.")
            top = key.split(".")[0].split("[")[0]
            formula_keys.add(top)
            value = to_canvas(clip.element, key, clip.params.get(key)) if "." not in key and "[" not in key else None
            shown = f" = {json.dumps(value, ensure_ascii=False)}" if value is not None else ""
            if a_expr and not b_expr:
                said.append(f"{key} was {formula_short(a_expr, clip.element)} → now fixed{shown}"
                            + (f" (write {key}={formula_short(a_expr, clip.element)} to keep it computed)" if "mark" in a_expr else ""))
            elif b_expr and not a_expr:
                said.append(f"{key} now {formula_short(b_expr, clip.element)}{shown}")
            else:
                said.append(f"{key} {formula_short(a_expr, clip.element)} → {formula_short(b_expr, clip.element)}{shown}")
        from astrid.sdk.timeline_cuts import AUDIO_LOOK_KEYS, look_lines

        for phrase in look_lines(prev.data, clip.data, audio=clip.is_audio, fps=fps):
            lines.append((clip.start, f"✎ {clip.address:<{width}}  look {phrase}"))
        for key in sorted(set(prev.params) | set(clip.params)):
            a_val, b_val = prev.params.get(key), clip.params.get(key)
            if a_val == b_val or key in formula_keys or (clip.is_audio and key in AUDIO_LOOK_KEYS):
                continue
            if isinstance(a_val, (dict, list)) or isinstance(b_val, (dict, list)):
                said.append(f"{key} changed")
            else:
                said.append(f"{key} {json.dumps(to_canvas(clip.element, key, a_val), ensure_ascii=False)} → "
                            f"{json.dumps(to_canvas(clip.element, key, b_val), ensure_ascii=False)}")
        same_shift = abs((clip.end - prev.end) - (clip.start - prev.start)) < 0.5 / fps
        if any(x.startswith(("now enters", "no longer")) for x in said) and same_shift:
            moved_end = False  # it kept its length
        if said:
            if moved_start and not any(x.startswith(("now enters", "no longer")) for x in said):
                said.append(f"starts {prev.start:.3f} → {clip.start:.3f} s")
            if moved_end and not any(x.startswith("now holds") or x.startswith("now ends") for x in said):
                said.append(f"ends {prev.end:.3f} → {clip.end:.3f} s")
            lines.append((clip.start, f"✎ {clip.address:<{width}}  " + " · ".join(said)))
        elif moved_start or moved_end:
            if moved_start and abs((clip.end - prev.end) - (clip.start - prev.start)) < 0.5 / fps:
                shifts.setdefault(round((clip.start - prev.start) * fps), []).append(clip)
            else:
                lines.append((clip.start, f"✎ {clip.address:<{width}}  {prev.start:.3f}–{prev.end:.3f} → {clip.start:.3f}–{clip.end:.3f} s"))
    for cid, clip in old.items():
        if cid not in new:
            lines.append((clip.start, f"− {clip.address:<{width}}  removed ({clip.element.removeprefix('am-')} {clip.asset or ''})".rstrip() + ")" * 0))
    for frames, clips in sorted(shifts.items(), key=lambda item: min(c.start for c in item[1])):
        first = min(clips, key=lambda c: c.start)
        if len(clips) <= 3:
            for c in clips:
                lines.append((c.start, f"✎ {c.address:<{width}}  {c.start - frames / fps:.3f} → {c.start:.3f} s ({secs(frames / fps)})"))
        else:
            lines.append((first.start, f"  {len(clips)} clips moved {secs(frames / fps)} from {first.start - frames / fps:.3f} s on (everything after the change)"))
    old_notes, new_notes = before.cut_notes_all(), after.cut_notes_all()
    spans = after._cut_spans()
    for cid in sorted(set(old_notes) | set(new_notes)):  # a cut's notes: said as the cut's, never a picture change
        a, b = old_notes.get(cid, {}), new_notes.get(cid, {})
        at = (spans.get(cid) or (0.0, None))[0]
        if a.get("why") != b.get("why"):
            lines.append((at, f"✎ {cid}  why note: \"{(b.get('why') or '(none)')[:70]}\""))
        if a.get("hold") != b.get("hold"):
            lines.append((at, f"✎ {cid}  hold: {b.get('hold') or 'off'}"))
    ordered = [text for _t, text in sorted(lines, key=lambda item: item[0])]
    # align the addresses to the widest one actually printed
    cells = [re.match(r"^(\S\s|\s\s)(\S+)\s+(.*)$", t) for t in ordered]
    pad = max([len(m.group(2)) for m in cells if m] + [0])
    return [f"{m.group(1)}{m.group(2):<{pad}}  {m.group(3)}" if m else t for m, t in zip(cells, ordered)]
