"""The timeline clock and file-frame mapping of a managed render."""
from collections.abc import Mapping
from copy import deepcopy
from fractions import Fraction


def normalize_time(value):
    """Normalize authored decimal and rational times consistently."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return Fraction(int(value[0]), int(value[1]))
    result = Fraction(str(value))
    return result.limit_denominator(1_000_000_000) if isinstance(value, float) else result


class RenderClock:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.fps = Fraction(*snapshot['fps_rational'])
        self.start = 0
        self.end = int(snapshot['duration_frames'])
        value = (snapshot.get('metadata') or {}).get('timeline_range')
        if value is not None:
            if not isinstance(value, Mapping):
                raise ValueError('Render timeline range is malformed; rerender the timeline.')
            start, end = value.get('start_frame'), value.get('end_frame')
            try:
                range_fps = Fraction(*value['fps_rational'])
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                raise ValueError('Render timeline range has invalid fps; rerender the timeline.') from None
            if type(start) is not int or type(end) is not int or start < 0 or end <= start or range_fps != self.fps:
                raise ValueError('Render timeline range is invalid; rerender the timeline.')
            self.start, self.end = start, end

    def file_frame(self, timeline_frame):
        if not self.start <= timeline_frame < self.end:
            self.refuse(Fraction(timeline_frame, 1) / self.fps)
        return timeline_frame - self.start

    def validate_frame(self, timeline_frame):
        """Refuse an explicitly requested frame outside this render's clock."""
        if not self.start <= timeline_frame < self.end and self._timeline_range() is None:
            raise ValueError(
                f'Requested frame {timeline_frame!r} is outside the rendered extent '
                f'0..{self.end - 1}.'
            )
        return self.file_frame(timeline_frame)

    def validate_timestamp(self, time):
        """Refuse an explicit timestamp outside the half-open rendered clock."""
        time = normalize_time(time)
        start = Fraction(self.start, 1) / self.fps
        end = Fraction(self.end, 1) / self.fps
        if time < start or time >= end:
            if self._timeline_range() is None:
                raise ValueError(
                    f'Requested timestamp {float(time):.6f}s is outside the rendered '
                    f'extent 0..{float(end):.6f}s.'
                )
            self.refuse(time)

    def validate_range(self, start, end):
        """Apply render-window bounds to a requested half-open range."""
        start, end = normalize_time(start), normalize_time(end)
        if self._timeline_range() is None:
            return
        if start < Fraction(self.start, 1) / self.fps:
            self.refuse(start)
        if end > Fraction(self.end, 1) / self.fps:
            self.refuse(end)

    def _timeline_range(self):
        return (self.snapshot.get('metadata') or {}).get('timeline_range')

    def timeline_seconds(self, file_seconds):
        return float(Fraction(self.start, 1) / self.fps) + file_seconds

    def project_audio_analysis(self, analysis):
        """Copy file-clock measurements onto the timeline clock.

        Speech annotations already use timeline time. Sample indices and
        durations remain relative measurements and are left unchanged.
        """
        result = deepcopy(analysis)
        offset = Fraction(self.start, 1) / self.fps

        def shifted(value):
            time = normalize_time(value) + offset
            return [time.numerator, time.denominator]

        origin = result.get('presentation_origin')
        if isinstance(origin, dict):
            origin['seconds'] = shifted(origin.get('seconds', [0, 1]))
        for gap in result.get('quiet_gaps', []):
            for key in ('start', 'end'):
                if gap.get(key) is not None:
                    gap[key] = shifted(gap[key])
        return result

    def refuse(self, time):
        raise ValueError(f'Requested time {float(time):.6f}s is outside the rendered range '
                         f'[{float(Fraction(self.start, 1) / self.fps):.6f}s, '
                         f'{float(Fraction(self.end, 1) / self.fps):.6f}s); '
                         'render that range, or ask for a time inside it.')
