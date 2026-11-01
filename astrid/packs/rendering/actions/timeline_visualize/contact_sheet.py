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
    """Stretches of 1 s+ without VO, and whether music or sfx keep sound under them."""
    lines = []
    for begin, finish in audio.silences('vo', minimum=minimum):
        music = audio.level_db('music', begin, finish)
        under = f'music under it at {music:.0f} dBFS' if music > -60 else 'no music: dead air'
        lines.append(f'NO-VO   {begin:.2f}–{finish:.2f}s ({finish - begin:.2f} s) no narration; {under}')
    for begin, finish in audio.dead_air():
        lines.append(f'DEADAIR {begin:.2f}–{finish:.2f}s ({finish - begin:.2f} s) no VO, music or sfx')
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


def _card_chapter(card: Mapping[str, Any], index_by_occurrence: Mapping[str, int]) -> int:
    for clip in card.get('clips') or []:
        if isinstance(clip, Mapping):
            key = clip.get('occurrence_id') or clip.get('shot_occurrence_id')
            if key is not None and str(key) in index_by_occurrence:
                return index_by_occurrence[str(key)]
    return -1


def static_contact_png(cards, out_root: Path, *, columns: int, timeline_name: str, render_run_id: str,
                       render_selection: str, occurrences=(), duration_seconds: float | None = None,
                       show_output: bool = True, thinned_from: int | None = None, overlay=None, audio=None) -> list[str]:
    """Write one bounded contact sheet (``contact-sheet.png``) and return its path."""
    from PIL import Image, ImageDraw

    columns = max(1, min(int(columns or CONTACT_DEFAULT_COLUMNS), 12))
    cards, sequence_steps = split_sequence_steps(cards)
    tile_w = CONTACT_TILE_WIDTH
    mini_w = (tile_w - 4) // 3
    mini_h = max(1, round(mini_w * 9 / 16))
    stride = tile_w + CONTACT_GUTTER
    width = CONTACT_MARGIN * 2 + columns * stride - CONTACT_GUTTER
    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    title_font, chrome_font = _png_font(20), _png_font(12)
    time_font, name_font, words_font = _png_font(13), _png_font(15), _png_font(13)

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
            time_text = f"{cut_tile_label(cut, chapter)}  {float(cut['start']):.2f}–{float(cut['end']):.2f}s"
        else:
            time_text = f"{card.get('time_label', '')} · f{card.get('frame', '')}"
        label = _png_shot_label(card)
        name_text = label if _png_text_width(measure, label, name_font) <= tile_w - 8 else _png_ellipsis(measure, label, name_font, tile_w - 8)
        words = _tile_words(card) or '—'
        word_lines, excerpt = _png_bounded_lines(measure, words, words_font, tile_w - 8, 2)
        if excerpt:
            word_lines[-1] = _png_ellipsis(measure, word_lines[-1], words_font, tile_w - 8)
        text_h = 4 + 17 + 18 + 16 * len(word_lines) + 4
        strip_h = mini_h + 3 if cut and cut.get('sequence') and show_output else 0
        tile_layouts.append({'image_h': image_h, 'time': time_text, 'name': name_text, 'strip_h': strip_h,
                             'words': word_lines, 'height': 3 + image_h + strip_h + 6 + text_h})

    rows = [tile_layouts[start:start + columns] for start in range(0, len(tile_layouts), columns)]
    row_heights = [max((tile['height'] for tile in row), default=0) for row in rows]

    cut_numbers = {card['cut']['index'] for card in cards if isinstance(card.get('cut'), Mapping)}
    if cut_numbers and len(cut_numbers) == len(cards):
        title = f'{timeline_name} · contact sheet · {len(cards)} cuts, one tile each'
    else:
        title = f'{timeline_name} · contact sheet · {len(cards)} tiles'
    if thinned_from:
        title += f' · {thinned_from} cuts, thinned to fit'
    title_lines = _png_chrome_lines(measure, title, title_font, width - 2 * CONTACT_MARGIN, 2)
    provenance = (f'Render {render_run_id} · selection: {render_selection} ({selection_gloss(render_selection)}) · '
                  'cNN = cut number, chNN = chapter; each tile is the cut once its layers have entered; '
                  'words are word-aligned only when VO app.words exist, else the shot script.')
    provenance_lines = _png_chrome_lines(measure, provenance, chrome_font, width - 2 * CONTACT_MARGIN, 2)
    header_bottom = 10 + 22 * len(title_lines) + 4 + 16 * len(provenance_lines)
    band_y = header_bottom + 10
    audio_y = band_y + CONTACT_BAND_HEIGHT + 2
    audio_h = CONTACT_AUDIO_HEIGHT if audio is not None else 0
    grid_top = band_y + CONTACT_BAND_HEIGHT + audio_h + 22
    height = grid_top + sum(row_heights) + CONTACT_GUTTER * max(0, len(rows) - 1) + CONTACT_MARGIN
    if width * height > 64_000_000:
        raise ValueError('Contact sheet exceeds 64 million pixels; reduce the tile count.')

    sheet = Image.new('RGB', (width, height), _CONTACT_BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    y_text = 10
    for line in title_lines:
        _png_draw_text(draw, (CONTACT_MARGIN, y_text), line, title_font, fill='white')
        y_text += 22
    y_text += 4
    for line in provenance_lines:
        _png_draw_text(draw, (CONTACT_MARGIN, y_text), line, chrome_font, fill=_CONTACT_MUTED)
        y_text += 16

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
            fitted = label if _png_text_width(measure, label, chrome_font) <= room else _png_ellipsis(measure, label, chrome_font, room)
            _png_draw_text(draw, (x0 + 4, band_y + 4), fitted, chrome_font, fill='white')
    if audio is not None:
        from .motion.audio import draw_lane
        draw.rectangle((band_left, audio_y, band_right, audio_y + audio_h - 1), fill='#0b1220')
        draw_lane(draw, audio, x0=band_left, x1=band_right, y0=audio_y, y1=audio_y + audio_h,
                  colours={'music': '#5b4a86', 'vo': '#5fd4c4', 'sfx': '#facc15'}, silence_colour='#f59e0b')
        legend = 'audio: music up (violet) · VO down (teal) · sfx (yellow) · amber = no VO 0.4 s+ · red = dead air'
        _png_draw_text(draw, (band_right - _png_text_width(draw, legend, chrome_font), audio_y + audio_h + 4),
                       legend, chrome_font, fill=_CONTACT_MUTED)
    label_y = band_y + CONTACT_BAND_HEIGHT + audio_h + 4
    _png_draw_text(draw, (band_left, label_y), '0s', chrome_font, fill=_CONTACT_MUTED)
    end_label = f'{total:.1f}s'
    if audio is None:
        _png_draw_text(draw, (band_right - _png_text_width(draw, end_label, chrome_font), label_y),
                       end_label, chrome_font, fill=_CONTACT_MUTED)
    else:
        _png_draw_text(draw, (band_left + 40, label_y), end_label + ' total', chrome_font, fill=_CONTACT_MUTED)

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
                    draw.rectangle((x, image_y, x + _png_text_width(draw, badge, chrome_font) + 8, image_y + 16), fill='#000000')
                    _png_draw_text(draw, (x + 4, image_y + 1), badge, chrome_font, fill='#facc15')
                    minis = sorted(sequence_steps.get(int(cut['index']), []) + [card], key=lambda c: int(c['frame']))
                    minis = [minis[0], card, minis[-1]] if len(minis) >= 3 else minis
                    for slot, mini in enumerate(minis[:3]):
                        with Image.open(Path(out_root) / mini['image']) as source:
                            thumb = source.convert('RGB').resize((mini_w, mini_h), Image.LANCZOS)
                        sheet.paste(thumb, (x + slot * (mini_w + 2), image_y + tile['image_h'] + 3))
            else:
                draw.rectangle((x, image_y, x + tile_w, image_y + tile['image_h']), fill='#162630', outline='#405769')
            text_y = image_y + tile['image_h'] + tile['strip_h'] + 6
            _png_draw_text(draw, (x + 4, text_y), tile['time'], time_font, fill=_CONTACT_TIME)
            _png_draw_text(draw, (x + 4, text_y + 17), tile['name'], name_font, fill='white')
            for line_no, line in enumerate(tile['words']):
                _png_draw_text(draw, (x + 4, text_y + 35 + line_no * 16), line, words_font, fill=_CONTACT_INK)
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

