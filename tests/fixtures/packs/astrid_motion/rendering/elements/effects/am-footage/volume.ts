// Footage sound level. The clip-level `volume` (clip.volume, the field every
// clip uses) wins when it is a finite number >= 0; the legacy element param
// `volume` is read only when the clip has none. Zero when neither is set, so
// stand-in footage stays silent. The result is clamped to 0..1 because the
// <video> element cannot be louder than unity: gain above 1 is not supported
// on footage (use the audio track for louder sound).
export const footageVolume = (clipVolume: unknown, paramVolume: unknown): number => {
  const chosen =
    typeof clipVolume === 'number' && Number.isFinite(clipVolume) && clipVolume >= 0
      ? clipVolume
      : typeof paramVolume === 'number' && Number.isFinite(paramVolume)
        ? paramVolume
        : 0;
  return Math.min(1, Math.max(0, chosen));
};
