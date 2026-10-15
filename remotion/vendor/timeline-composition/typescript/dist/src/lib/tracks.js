const DEFAULT_VISUAL_TRACK = {
    id: 'v1',
    kind: 'visual',
    label: 'Video',
};
export const getVisualTracks = (timeline) => {
    const tracks = timeline.tracks ?? [DEFAULT_VISUAL_TRACK];
    return tracks.filter((track) => track.kind === 'visual');
};
export const getAudioTracks = (timeline) => {
    return (timeline.tracks ?? []).filter((track) => track.kind === 'audio');
};
