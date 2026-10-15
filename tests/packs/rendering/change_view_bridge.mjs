import {createInterface} from 'node:readline';
import {pathToFileURL} from 'node:url';
import {prepareScene, querySceneFrame, compileAudioEnvelope, validateAudioEnvelope,
  audioEnvelopeAt, applyAudioEnvelope} from '@banodoco/timeline-composition/evaluation';
const {evaluationMetadata} = await import(pathToFileURL(`${process.cwd()}/remotion/src/Evaluation.tsx`));
const {secondsToFrames, getClipDurationInFrames} = await import(pathToFileURL(
  `${process.env.ASTRID_CHANGE_VIEW_EVAL_ROOT}/typescript/src/lib/duration.ts`));
for await (const line of createInterface({input: process.stdin})) {
  try {
    const input = JSON.parse(line);
    let result;
    if (input.mix) {
      const {clips, fps, rate, length} = input.mix;
      const mix = new Float64Array(length);
      for (const clip of clips) {
        const start = secondsToFrames(clip.at, fps);
        const end = start + getClipDurationInFrames(clip, fps);
        const envelope = compileAudioEnvelope(clip, fps);
        const pcm = new Float32Array(length);
        for (let n = 0; n < length; n++) {
          const local = n / rate - start / fps;
          if (local >= 0 && n / rate < end / fps) {
            const source = (clip.from ?? 0) + local * (clip.speed ?? 1);
            pcm[n] = Math.sin(source * (clip.asset === 'other' ? 117 : 73) + .3) * (clip.volume ?? 1);
          }
        }
        if (envelope) {
          // The PCM primitive operates in source coordinates. Bake that same
          // source buffer, then read it at the Sequence's local sample clock.
          validateAudioEnvelope(envelope);
          for (let n = 0; n < length; n++) pcm[n] *= audioEnvelopeAt(envelope, n / rate - start / fps);
        }
        for (let n = 0; n < length; n++) mix[n] += pcm[n];
      }
      result = [...mix];
    } else if (input.audio) {
      const {clip, fps, rate, length} = input.audio;
      const envelope = compileAudioEnvelope(clip, fps);
      const pcm = new Float32Array(length).fill(1);
      if (envelope) {
        validateAudioEnvelope(envelope);
        applyAudioEnvelope([pcm], rate, envelope, clip.from ?? 0, clip.speed ?? 1);
        // Check the public point query and actual PCM consumer agree.
        for (let n = 0; n < length; n++) {
          const expected = audioEnvelopeAt(envelope, (n / rate - (clip.from ?? 0)) / (clip.speed ?? 1));
          if (Math.abs(pcm[n] - expected) > 1e-6) throw new Error('PCM/point query disagreement');
        }
      }
      result = [...pcm];
    } else if (input.pure) {
      const p = input.pure;
      const scene = prepareScene(p.tracks, p.assets, p.fps, p.width, p.height,
        {elements: new Set(p.elements ?? []), roles: p.roles});
      result = p.frames.map(f => querySceneFrame(scene, f));
    } else result = evaluationMetadata({props: input}).props.evaluation;
    process.stdout.write(JSON.stringify({ok: true, evaluation: result}) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({ok: false, error: String(error)}) + '\n');
  }
}
