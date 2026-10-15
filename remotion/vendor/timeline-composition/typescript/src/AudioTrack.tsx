import type {FC} from 'react';
import {Audio, staticFile} from 'remotion';
import type {AssetRegistryEntry, TimelineClip, TrackDefinition} from './types';
import {
  getClipDurationInFrames,
  getSanitizedPlaybackRate,
  getSanitizedVolume,
} from './lib/duration';
import {computeMediaTrim} from './lib/trim';

type AudioTrackProps = {
  clip: TimelineClip;
  track: TrackDefinition;
  assetEntry?: AssetRegistryEntry;
  fps: number;
};

const toRenderableFileUrl = (file: string | undefined): string | null => {
  if (typeof file !== 'string' || file.trim().length === 0) {
    return null;
  }

  if (file.startsWith('http://') || file.startsWith('https://')) {
    return file;
  }

  return staticFile(file);
};

const toFiniteSeconds = (value: unknown): number => {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) {
    return 0;
  }
  return value;
};

export const AudioTrack: FC<AudioTrackProps> = ({clip, track, assetEntry, fps}) => {
  const fileUrl = toRenderableFileUrl(assetEntry?.file);
  if (!fileUrl) {
    return null;
  }

  const baseVolume = track.muted
    ? 0
    : getSanitizedVolume(track.volume ?? 1) * getSanitizedVolume(clip.volume ?? 1);

  const params = (clip.params ?? {}) as Record<string, unknown>;
  const fadeInFrames = Math.round(toFiniteSeconds(params.fadeIn) * fps);
  const fadeOutFrames = Math.round(toFiniteSeconds(params.fadeOut) * fps);
  const durationFrames = getClipDurationInFrames(clip, fps);

  const volumeProp: number | ((frame: number) => number) =
    fadeInFrames > 0 || fadeOutFrames > 0
      ? (frame: number) => {
          let multiplier = 1;
          if (fadeInFrames > 0 && frame < fadeInFrames) {
            multiplier *= Math.max(0, frame / fadeInFrames);
          }
          if (fadeOutFrames > 0) {
            const fadeStart = durationFrames - fadeOutFrames;
            if (frame > fadeStart) {
              multiplier *= Math.max(
                0,
                (durationFrames - frame) / fadeOutFrames,
              );
            }
          }
          return baseVolume * multiplier;
        }
      : baseVolume;

  return (
    <Audio
      src={fileUrl}
      {...computeMediaTrim(clip, fps)}
      playbackRate={getSanitizedPlaybackRate(clip.speed)}
      volume={volumeProp}
    />
  );
};
