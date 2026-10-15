"""A pure command macro over existing beat moments and timing setters."""
from astrid.core.timeline.moments import MomentError, parse
from astrid.sdk.timeline.commands import CommandError


def build(ctx, inputs):
    clips, beats = inputs["clips"], inputs["beats"]
    if not clips:
        if beats:
            raise CommandError("empty selection requires no beat references")
        return []
    if len(beats) != len(clips) + 1:
        raise CommandError("fit_to_beats requires exactly N+1 beat references for N clips")
    selected = [ctx.clip(address) for address in clips]
    if len({clip["identity"] for clip in selected}) != len(selected):
        raise CommandError("a clip may appear only once, including aliases")
    cuts = {clip["cut"] for clip in selected}
    if len(cuts) != 1 or None in cuts:
        raise CommandError("fit_to_beats supports overlays within one cut")
    if any(clip["is_audio"] or clip["is_picture"] for clip in selected):
        raise CommandError("fit_to_beats refuses audio and picture beds; select overlay clips")
    try:
        if any(parse(beat).kind not in {"beat", "downbeat"} for beat in beats):
            raise CommandError("fit_to_beats requires beat or downbeat references")
    except MomentError as exc:
        raise CommandError(str(exc)) from exc
    times = [ctx.quantize(ctx.time(beat)) for beat in beats]
    if any(b - a < 1 / ctx.fps - 1e-6 for a, b in zip(times, times[1:])):
        raise CommandError("beat boundaries must strictly increase by at least one frame")
    if times[0] < selected[0]["cut_start"] or times[-1] > selected[0]["cut_end"]:
        raise CommandError("beat intervals must stay inside the selected cut")
    calls = []
    for i, clip in enumerate(selected):
        calls.append(ctx.command("clip.on", clip["address"], moment=beats[i]))
        calls.append(ctx.command("clip.until", clip["address"], moment=beats[i + 1]))
    return calls
