import {readFileSync, rmSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {compileAudioEnvelope} from '@banodoco/timeline-composition/evaluation';
import {prepareAttemptEnvelopePcm} from './attempt-envelope-pcm.mjs';
import {compiledAudioSourceClock} from './compiled-audio-source-clock.mjs';

const translate = (envelope, delta) => ({
  ...envelope,
  ...(envelope.keyframes && {
    keyframes: envelope.keyframes.map(point => ({...point, at: point.at - delta})),
  }),
  ...(envelope.multiply && {multiply: envelope.multiply.map(child => translate(child, delta))}),
  ...(envelope.minimum && {minimum: envelope.minimum.map(child => translate(child, delta))}),
});

// Private subprocess data from the existing asset/source owner, never a grant.
// originalClip must be retained before slicing, not reconstructed from app data.
export const prepareNormalAudioSources = ({clips, fps, attemptDirectory}) => {
  const prepared = [];
  try {
    for (const {clip, source, originalClip} of clips) {
      if (clip.audio?.render_source !== undefined || originalClip?.audio?.render_source !== undefined) {
        throw new Error('source already prepared');
      }
      const sourceClock = compiledAudioSourceClock(clip, fps);
      let derived = clip;
      if (originalClip) {
        const originalClock = compiledAudioSourceClock(originalClip, fps);
        if (originalClip.asset !== clip.asset || originalClip.track !== clip.track
          || originalClock.playback_rate !== sourceClock.playback_rate) {
          throw new Error('original compiled clip does not match sliced source');
        }
        const envelope = compileAudioEnvelope(originalClip, fps);
        if (envelope) {
          const delta = (sourceClock.source_from - originalClock.source_from) / sourceClock.playback_rate;
          derived = {...clip, audio: {...clip.audio, envelope: translate(envelope, delta)}};
        }
      }
      const row = prepareAttemptEnvelopePcm({clip: derived, fps, source, attemptDirectory, sourceClock});
      prepared.push(row ? {...row, envelope: JSON.parse(row.envelope_key)} : null);
    }
    return prepared;
  } catch (error) {
    for (const row of prepared) if (row) rmSync(path.dirname(row.path), {recursive: true, force: true});
    throw error;
  }
};

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    process.stdout.write(JSON.stringify(prepareNormalAudioSources(JSON.parse(readFileSync(0, 'utf8')))));
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}
