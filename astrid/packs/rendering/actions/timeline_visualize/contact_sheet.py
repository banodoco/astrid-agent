"""One-page overview of a timeline: the whole film at a glance.

The paired filmstrip is for drill-down; this page answers "what does the video
look like" in one image.  It keeps one tile per cut (or per shot) and labels
each tile with its timecode, shot name and the first words of the VO.  A thin
chapter band above the grid shows where each authored shot falls in time.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .filmstrip_cards import (
    _PNG_FRAME_ASPECT_DEFAULT,
    _png_bounded_lines,
    _png_chrome_lines,
    _png_draw_text,
    _png_ellipsis,
    _png_font,
    _png_frame_aspect,
    _png_image_height,
    _png_shot_label,
    _png_text_width,
    _word_caption_text,
)

CONTACT_MAX_TILES = 120
CONTACT_DEFAULT_COLUMNS = 10
CONTACT_TILE_WIDTH = 188
CONTACT_GUTTER = 12
CONTACT_MARGIN = 16
CONTACT_BAND_HEIGHT = 22
CONTACT_WORDS_MAX = 12
CONTACT_AUDIO_HEIGHT = 34
_CONTACT_PALETTE = ('#2f6f73', '#5a4f8a', '#7a5a2e', '#2e5b8a', '#6b3f5e', '#3d6b3a', '#8a4b3a', '#3c5f7a')
_CONTACT_BACKGROUND = '#111827'
_CONTACT_INK = '#e5e7eb'
_CONTACT_MUTED = '#9fb0bf'
_CONTACT_TIME = '#8ce0d0'


def contact_band_width(columns: int) -> int:
    """Pixel width of the chapter band (and the audio lane) for a sheet with ``columns``."""
    columns = max(1, min(int(columns or CONTACT_DEFAULT_COLUMNS), 12))
    return columns * (CONTACT_TILE_WIDTH + CONTACT_GUTTER) - CONTACT_GUTTER


def contact_silence_findings(audio, *, minimum: float = 1.0) -> list[str]:
    """Silence worth a look: quiet stretches *where a line is placed or planned* (``audio.expected``).

    A quiet stretch with no line there (a logo opening, the gap between two lines) is intended and is
    summarised on one info line, not flagged. Audio that could not be read is named, never called silent."""
    lines = []
    expected = getattr(audio, 'expected', None)
    for begin, finish in audio.missing_lines(minimum=minimum if expected is not None else 0.3):
        where = 'where a line is placed or planned' if expected is not None else '(no VO, music or sfx)'
        lines.append(f'SILENT  {begin:.2f}–{finish:.2f}s ({finish - begin:.2f} s) silent {where}')
    if expected is not None:
        quiet = [(b, f) for b, f in audio.silences('vo', minimum=minimum)
                 if not any(lo < f and b < hi for lo, hi in expected)]
        if quiet:
            spans = ', '.join(f'{b:.1f}–{f:.1f}s' for b, f in quiet[:8]) + (' …' if len(quiet) > 8 else '')
            lines.append(f'QUIET   no line placed or planned (not flagged): {spans}')
    else:
        for begin, finish in audio.silences('vo', minimum=minimum):
            music = audio.level_db('music', begin, finish)
            under = f'music under it at {music:.0f} dBFS' if music > -60 else 'no music either'
            lines.append(f'NO-VO   {begin:.2f}–{finish:.2f}s ({finish - begin:.2f} s) no narration; {under}')
    return lines[:24] + audio.notes[:3]


def split_sequence_steps(cards: Sequence[Mapping[str, Any]]) -> tuple[list, dict[int, list]]:
    """Tiles, and the extra step frames of sequence cuts keyed by cut number (drawn as a mini-strip)."""
    tiles, steps = [], {}
    for card in cards:
        cut = card.get('cut') if isinstance(card.get('cut'), Mapping) else None
        if cut and set(card.get('sample_reasons') or []) == {'sequence_step'}:
            steps.setdefault(int(cut['index']), []).append(card)
        else:
            tiles.append(card)
    return tiles, steps


def contact_reasons(reasons: Mapping[int, set], *, mode: str, limit: int = CONTACT_MAX_TILES) -> tuple[dict, int | None]:
    """Reduce planned frames to one tile per cut (or per shot), bounded by ``limit``.

    Cut sampling also plans the frame before each cut; an overview keeps only
    the frame where a cut lands.  An explicit interval is a strict grid and is
    kept as requested.  Returns ``(reasons, thinned_from)``; ``thinned_from`` is
    the pre-cap count when the cap evenly thinned the tiles.
    """
    if mode == 'interval':
        selected = dict(reasons)
    else:
        selected = {frame: why for frame, why in reasons.items() if set(why) - {'before_cut'}}
    frames = sorted(selected)
    thinned_from = None
    if len(frames) > limit:
        thinned_from = len(frames)
        step = (len(frames) - 1) / (limit - 1) if limit > 1 else 0.0
        frames = sorted({frames[round(index * step)] for index in range(limit)})
    return {frame: selected[frame] for frame in frames}, thinned_from


def _tile_caption(card: Mapping[str, Any], *, voice: bool | None = None) -> str:
    """The tile's words line; a tile with no words says plainly why (never a bare dash).

    ``voice``: whether a VO clip plays under this cut (None when unknown). With no voice there is
    nothing to store, so the tile says so instead of "no words stored"."""
    words = _tile_words(card)
    if words:
        return words
    if voice is False:
        return 'no voice under this cut'
    if voice is True:
        return 'voice here; no words stored (timelines transcripts run)'
    return 'no words stored'


def _tile_words(card: Mapping[str, Any]) -> str:
    """First words of the VO under this tile's cut (or at its frame), from the best timing available."""
    cut = card.get('cut') if isinstance(card.get('cut'), Mapping) else None
    if cut and cut.get('say'):
        text = str(cut['say'])
    elif card.get('captions'):
        text = ' '.join(str(item.get('canonical_text') or item.get('text') or '') for item in card['captions'])
    elif card.get('timed_words'):
        text = _word_caption_text(card)
    else:
        # A shot script is untimed; show it on the first tile of its shot only,
        # the same once-per-occurrence rule the filmstrip applies.
        text = ' '.join(str(item.get('canonical_text') or item.get('text') or '')
                        for item in card.get('display_scripts') or [])
    words = ' '.join(text.split()).split(' ') if text.strip() else []
    if not words:
        return ''
    head = ' '.join(words[:CONTACT_WORDS_MAX])
    return head + ('…' if len(words) > CONTACT_WORDS_MAX else '')


def _chapter_index(occurrences: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    ordered = sorted((o for o in occurrences if isinstance(o, Mapping)),
                     key=lambda o: (float(o.get('start') or 0.0), str(o.get('occurrence_id'))))
    return {str(o.get('occurrence_id')): index for index, o in enumerate(ordered)}


def cut_tile_label(cut: Mapping[str, Any], chapter: int) -> str:
    """``c09a · ch02``: the cut's id (the one its layers' addresses start with) and its chapter (1-based).

    The id is the cut's own (``cut_id``: c09a, c30b), never its position; a cut without one shows ``#N``.
    No chapter (-1) leaves the cut alone."""
    cut_id = cut.get("cut_id")
    text = str(cut_id) if cut_id else f"#{int(cut['index'])}"
    return text if chapter < 0 else f"{text} · ch{chapter + 1:02d}"


def cut_name(cut: Mapping[str, Any]) -> str:
    """The one name a cut has on every page: its id (``c09a``) when it has one, else ``cut 8`` (the number
    ``timelines show`` prints and ``--cut`` takes). Shots are named by their own names, never renumbered."""
    cut_id = cut.get("cut_id")
    return str(cut_id) if cut_id else f"cut {int(cut['index'])}"


def _card_chapter(card: Mapping[str, Any], index_by_occurrence: Mapping[str, int]) -> int:
    for clip in card.get('clips') or []:
        if isinstance(clip, Mapping):
            key = clip.get('occurrence_id') or clip.get('shot_occurrence_id')
            if key is not None and str(key) in index_by_occurrence:
                return index_by_occurrence[str(key)]
    return -1


def _overlaps(lo: float, hi: float, spans: Sequence[tuple[float, float]]) -> bool:
    return any(a < hi and lo < b for a, b in spans)


def contact_title(timeline_name: str, *, duration_seconds: float | None, cuts: int | None, shots: int | None,
                  revision: str | None, tiles: int, thinned_from: int | None = None) -> tuple[str, str]:
    """The page's two header lines in plain words: the film's name, then what it is and what is shown.

    ``Rough cut C2`` / ``43.5 s · 19 cuts in 9 shots · revision 08dee21c · one tile per cut``.
    Internal ids, render runs and full revision ids belong to the findings and the JSON."""
    facts = []
    if duration_seconds:
        facts.append(f'{float(duration_seconds):.1f} s')
    if cuts:
        facts.append(f'{cuts} cut{"s" if cuts != 1 else ""}' + (f' in {shots} shot{"s" if shots != 1 else ""}' if shots else ''))
    if revision:
        facts.append(f'revision {revision}')
    if thinned_from:
        facts.append(f'{tiles} of {thinned_from} cuts shown (thinned to fit)')
    elif cuts and tiles == cuts:
        facts.append('one tile per cut')
    else:
        facts.append(f'{tiles} tile{"s" if tiles != 1 else ""}')
    return str(timeline_name), ' · '.join(facts)


LEGEND_ITEMS = (
    ('swatch', '#5b4a86', 'music (up)'),
    ('swatch', '#5fd4c4', 'voice (down)'),
    ('swatch', '#facc15', 'sfx'),
    ('strip', '#64748b', 'no voice'),
    ('box', '#ef4444', 'silent where a line is placed'),
    ('hatch', '#6b7280', 'audio not read'),
)


def _draw_legend(draw, x: int, y: int, font, items=LEGEND_ITEMS) -> int:
    """One row of swatch + words; returns the x after the last item. Drawn, never pasted."""
    for kind, colour, text in items:
        if kind == 'swatch':
            draw.rectangle((x, y + 3, x + 14, y + 15), fill=colour)
        elif kind == 'strip':
            draw.rectangle((x, y + 12, x + 14, y + 15), fill=colour)
        elif kind == 'box':
            draw.rectangle((x, y + 3, x + 14, y + 15), outline=colour, width=2)
        else:
            for dx in range(0, 14, 5):
                draw.line((x + dx, y + 12, x + dx + 3, y + 5), fill=colour, width=1)
        x += 20
        _png_draw_text(draw, (x, y), text, font, fill=_CONTACT_INK)
        x += _png_text_width(draw, text, font) + 18
    return x


def static_contact_png(cards, out_root: Path, *, columns: int, timeline_name: str, render_run_id: str,
                       render_selection: str, occurrences=(), duration_seconds: float | None = None,
                       show_output: bool = True, thinned_from: int | None = None, overlay=None, audio=None,
                       revision_label: Mapping[str, Any] | None = None,
                       event_coverage: Mapping[str, Any] | None = None,
                       revision_short: str | None = None, cut_count: int | None = None,
                       voice_spans: Sequence[tuple[float, float]] | None = None,
                       plan_labels: Mapping[str, str] | None = None) -> list[str]:
    """Write one bounded contact sheet (``contact-sheet.png``) and return its path.

    The header says what the film is in plain words (name, length, cuts, shots, short revision);
    ``render_run_id``/``render_selection``/``revision_label`` stay in the JSON and findings.
    ``voice_spans`` (VO clip spans) lets a tile say "no voice" rather than "no words stored";
    ``plan_labels`` (occurrence id -> ``LM+R · planned``) draws a shot plan's label on each tile."""
    from PIL import Image, ImageDraw

    del render_run_id, render_selection, revision_label  # provenance lives in frame-index.json / findings
    columns = max(1, min(int(columns or CONTACT_DEFAULT_COLUMNS), 12))
    cards, sequence_steps = split_sequence_steps(cards)
    tile_w = CONTACT_TILE_WIDTH
    mini_w = (tile_w - 4) // 3
    mini_h = max(1, round(mini_w * 9 / 16))
    stride = tile_w + CONTACT_GUTTER
    width = CONTACT_MARGIN * 2 + columns * stride - CONTACT_GUTTER
    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    title_font, facts_font, chrome_font = _png_font(24), _png_font(17), _png_font(15)
    band_font = _png_font(14)
    time_font, name_font, words_font, plan_font = _png_font(14), _png_font(15), _png_font(13), _png_font(13)

    index_by_occurrence = _chapter_index(occurrences)
    chapter_of = [_card_chapter(card, index_by_occurrence) for card in cards]
    image_heights = [
        _png_image_height(_png_frame_aspect(out_root, card) if show_output else _PNG_FRAME_ASPECT_DEFAULT, card_width=tile_w)
        for card in cards
    ]
    tile_layouts = []
    for card, image_h, chapter in zip(cards, image_heights, chapter_of):
        cut = card.get('cut') if isinstance(card.get('cut'), Mapping) else None
        if cut:
            # one numbering everywhere: the cut id timelines show prints (c05); the shot is named below
            time_text = f"{cut_name(cut)}  {float(cut['start']):.2f}–{float(cut['end']):.2f} s"
        else:
            time_text = f"{card.get('time_label', '')} · f{card.get('frame', '')}"
        label = _png_shot_label(card)
        name_lines, cut_short = _png_bounded_lines(measure, label, name_font, tile_w - 8, 2)
        if cut_short:
            name_lines[-1] = _png_ellipsis(measure, name_lines[-1], name_font, tile_w - 8)
        voice = None
        if voice_spans is not None and cut:
            voice = _overlaps(float(cut['start']), float(cut['end']), voice_spans)
        words = _tile_caption(card, voice=voice)
        word_lines, excerpt = _png_bounded_lines(measure, words, words_font, tile_w - 8, 2)
        if excerpt:
            word_lines[-1] = _png_ellipsis(measure, word_lines[-1], words_font, tile_w - 8)
        plan_text = None
        if plan_labels:
            key = next((str(c.get('occurrence_id') or c.get('shot_occurrence_id')) for c in card.get('clips') or []
                        if isinstance(c, Mapping) and str(c.get('occurrence_id') or c.get('shot_occurrence_id')) in plan_labels),
                       None)
            plan_text = plan_labels.get(key) if key else None
        text_h = 4 + 18 + 18 * len(name_lines) + 16 * len(word_lines) + 4
        strip_h = mini_h + 3 if cut and cut.get('sequence') and show_output else 0
        tile_layouts.append({'image_h': image_h, 'time': time_text, 'name': name_lines, 'strip_h': strip_h,
                             'words': word_lines, 'words_muted': not _tile_words(card), 'plan': plan_text,
                             'height': 3 + image_h + strip_h + 6 + text_h})

    rows = [tile_layouts[start:start + columns] for start in range(0, len(tile_layouts), columns)]
    row_heights = [max((tile['height'] for tile in row), default=0) for row in rows]

    cut_numbers = {card['cut']['index'] for card in cards if isinstance(card.get('cut'), Mapping)}
    shots = len({str(o.get('shot_id') or o.get('occurrence_id')) for o in occurrences if isinstance(o, Mapping)}) or None
    cuts = cut_count or (len(cut_numbers) if cut_numbers and len(cut_numbers) == len(cards) else None)
    if thinned_from:
        cuts = cut_count or thinned_from
    title, facts = contact_title(timeline_name, duration_seconds=duration_seconds, cuts=cuts, shots=shots,
                                 revision=revision_short, tiles=len(cards), thinned_from=thinned_from)
    title_lines = _png_chrome_lines(measure, title, title_font, width - 2 * CONTACT_MARGIN, 1)
    facts_lines = _png_chrome_lines(measure, facts, facts_font, width - 2 * CONTACT_MARGIN, 2)
    guide = ('Coloured band: the shots in time. Each tile: cut number and time, shot name, the words under it'
             + (', and the shot plan (methods · status).' if plan_labels else '.'))
    guide_lines = _png_chrome_lines(measure, guide, chrome_font, width - 2 * CONTACT_MARGIN, 2)
    if isinstance(event_coverage, Mapping) and event_coverage.get('event_count'):
        guide_lines += _png_chrome_lines(measure, (
            f"event moments: {event_coverage.get('sampled_event_moment_count', 0)} sampled, "
            f"{event_coverage.get('omitted_event_moment_count', 0)} omitted, "
            f"{event_coverage.get('clipped_event_count', 0)} clipped"), chrome_font, width - 2 * CONTACT_MARGIN, 1)
    header_bottom = 12 + 30 * len(title_lines) + 22 * len(facts_lines) + 4 + 20 * len(guide_lines)
    band_y = header_bottom + 10
    audio_y = band_y + CONTACT_BAND_HEIGHT + 2
    audio_h = CONTACT_AUDIO_HEIGHT if audio is not None else 0
    legend_h = 24 if audio is not None else 0
    grid_top = band_y + CONTACT_BAND_HEIGHT + audio_h + 24 + legend_h
    height = grid_top + sum(row_heights) + CONTACT_GUTTER * max(0, len(rows) - 1) + CONTACT_MARGIN
    if width * height > 64_000_000:
        raise ValueError('Contact sheet exceeds 64 million pixels; reduce the tile count.')

    sheet = Image.new('RGB', (width, height), _CONTACT_BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    y_text = 12
    for line in title_lines:
        _png_draw_text(draw, (CONTACT_MARGIN, y_text), line, title_font, fill='white')
        y_text += 30
    for line in facts_lines:
        _png_draw_text(draw, (CONTACT_MARGIN, y_text), line, facts_font, fill=_CONTACT_TIME)
        y_text += 22
    y_text += 4
    for line in guide_lines:
        _png_draw_text(draw, (CONTACT_MARGIN, y_text), line, chrome_font, fill=_CONTACT_MUTED)
        y_text += 20

    # Chapter band: authored shots on a proportional time axis, so the film's
    # structure is visible before any tile is read.
    band_left, band_right = CONTACT_MARGIN, width - CONTACT_MARGIN
    total = float(duration_seconds or 0.0)
    if not total:
        total = max([float(o.get('end') or 0.0) for o in occurrences if isinstance(o, Mapping)] or [0.0])
    segments = []
    if total > 0 and occurrences:
        for occurrence in occurrences:
            if not isinstance(occurrence, Mapping):
                continue
            start, end = float(occurrence.get('start') or 0.0), float(occurrence.get('end') or 0.0)
            segments.append((start, end, index_by_occurrence.get(str(occurrence.get('occurrence_id')), 0),
                             str(occurrence.get('shot_name') or occurrence.get('shot_id') or '')))
    if not segments:
        segments = [(0.0, total or 1.0, 0, timeline_name)]
    scale = (band_right - band_left) / (total or 1.0)
    for start, end, chapter, label in segments:
        x0 = band_left + start * scale
        x1 = max(x0 + 2, band_left + end * scale)
        colour = _CONTACT_PALETTE[chapter % len(_CONTACT_PALETTE)]
        draw.rectangle((x0, band_y, x1 - 1, band_y + CONTACT_BAND_HEIGHT), fill=colour)
        room = x1 - x0 - 8
        if label and room >= 24:
            fitted = label if _png_text_width(measure, label, band_font) <= room else _png_ellipsis(measure, label, band_font, room)
            _png_draw_text(draw, (x0 + 4, band_y + 3), fitted, band_font, fill='white')
    if audio is not None:
        from .motion.audio import draw_lane
        draw.rectangle((band_left, audio_y, band_right, audio_y + audio_h - 1), fill='#0b1220')
        draw_lane(draw, audio, x0=band_left, x1=band_right, y0=audio_y, y1=audio_y + audio_h,
                  colours={'music': '#5b4a86', 'vo': '#5fd4c4', 'sfx': '#facc15'}, silence_colour='#64748b')
    label_y = band_y + CONTACT_BAND_HEIGHT + audio_h + 4
    _png_draw_text(draw, (band_left, label_y), '0 s', chrome_font, fill=_CONTACT_MUTED)
    end_label = f'{total:.1f} s'
    _png_draw_text(draw, (band_right - _png_text_width(draw, end_label, chrome_font), label_y),
                   end_label, chrome_font, fill=_CONTACT_MUTED)
    if audio is not None:
        legend_y = label_y + 22
        x = _png_text_width(draw, 'audio:', chrome_font) + band_left + 10
        _png_draw_text(draw, (band_left, legend_y), 'audio:', chrome_font, fill=_CONTACT_MUTED)
        _draw_legend(draw, x, legend_y, chrome_font)

    y = grid_top
    index = 0
    for row_index, row in enumerate(rows):
        if row_index:
            y += row_heights[row_index - 1] + CONTACT_GUTTER
        for column, tile in enumerate(row):
            card = cards[index]
            chapter = chapter_of[index]
            index += 1
            x = CONTACT_MARGIN + column * stride
            colour = _CONTACT_PALETTE[chapter % len(_CONTACT_PALETTE)] if chapter >= 0 else '#405769'
            draw.rectangle((x, y, x + tile_w, y + 2), fill=colour)
            image_y = y + 3
            if show_output:
                with Image.open(Path(out_root) / card['image']) as source:
                    frame = source.convert('RGB').resize((tile_w, tile['image_h']), Image.LANCZOS)
                if overlay is not None:
                    frame = overlay(frame, float(card.get('time_seconds') or 0.0))
                sheet.paste(frame, (x, image_y))
                cut = card.get('cut') if isinstance(card.get('cut'), Mapping) else None
                if cut and cut.get('sequence'):
                    badge = f"SEQUENCE · {cut['sequence']['steps']} steps"
                    draw.rectangle((x, image_y, x + _png_text_width(draw, badge, chrome_font) + 8, image_y + 18), fill='#000000')
                    _png_draw_text(draw, (x + 4, image_y + 1), badge, chrome_font, fill='#facc15')
                    minis = sorted(sequence_steps.get(int(cut['index']), []) + [card], key=lambda c: int(c['frame']))
                    minis = [minis[0], card, minis[-1]] if len(minis) >= 3 else minis
                    for slot, mini in enumerate(minis[:3]):
                        with Image.open(Path(out_root) / mini['image']) as source:
                            thumb = source.convert('RGB').resize((mini_w, mini_h), Image.LANCZOS)
                        sheet.paste(thumb, (x + slot * (mini_w + 2), image_y + tile['image_h'] + 3))
            else:
                draw.rectangle((x, image_y, x + tile_w, image_y + tile['image_h']), fill='#162630', outline='#405769')
            if tile['plan']:
                # the shot plan on the picture's bottom edge: methods · status (· takes)
                plan = _png_ellipsis(draw, tile['plan'], plan_font, tile_w - 10) \
                    if _png_text_width(draw, tile['plan'], plan_font) > tile_w - 10 else tile['plan']
                bottom = image_y + tile['image_h']
                draw.rectangle((x, bottom - 19, x + min(tile_w, _png_text_width(draw, plan, plan_font) + 10), bottom - 1),
                               fill='#000000')
                _png_draw_text(draw, (x + 5, bottom - 18), plan, plan_font, fill='#fde68a')
            text_y = image_y + tile['image_h'] + tile['strip_h'] + 6
            _png_draw_text(draw, (x + 4, text_y), tile['time'], time_font, fill=_CONTACT_TIME)
            text_y += 18
            for line in tile['name']:
                _png_draw_text(draw, (x + 4, text_y), line, name_font, fill='white')
                text_y += 18
            for line in tile['words']:
                _png_draw_text(draw, (x + 4, text_y), line, words_font,
                               fill=_CONTACT_MUTED if tile['words_muted'] else _CONTACT_INK)
                text_y += 16
    path = Path(out_root) / 'contact-sheet.png'
    sheet.save(path)
    return [str(path)]


__all__ = ['CONTACT_MAX_TILES', 'contact_reasons', 'static_contact_png']


SELECTION_GLOSS = {
    "explicit_current_render": "the run you named; it renders the current head",
    "explicit_historical_render": "the run you named; it renders an earlier revision",
    "explicit_candidate_preview": "the run you named; it renders an unpublished working copy",
    "explicit_unverified_legacy_render": "the run you named; it predates render authority records, so its revision is not verified",
}


def selection_gloss(selection: str) -> str:
    """One plain clause for a render selection label."""
    return SELECTION_GLOSS.get(str(selection), "how the render was chosen")

