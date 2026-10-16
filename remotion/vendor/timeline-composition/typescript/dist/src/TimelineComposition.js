import { jsx as _jsx, Fragment as _Fragment, jsxs as _jsxs } from "react/jsx-runtime";
import { AbsoluteFill, Sequence, useCurrentFrame, useVideoConfig } from 'remotion';
import { AudioTrack } from './AudioTrack';
import { ClipLook } from './ClipLook';
import { CLIP_TYPE_ALIASES, EFFECT_REGISTRY } from './effects.generated';
import { DEFAULT_THEME, ThemeProvider, useTheme } from './ThemeContext';
import { VisualClip } from './VisualClip';
import { resolveParams } from './lib/effect-params';
import { getClipDurationInFrames } from './lib/duration';
import { assertLoopCoverage } from './lib/beat-loop';
import { getAudioTracks, getVisualTracks } from './lib/tracks';
import { resolveTransitionReference } from './lib/transitions';
import { prepareScene, querySceneFrame, } from './evaluation';
import { TransitionLayer } from './TransitionLayer';
import { mediaModifierBounds } from './lib/modifier-bounds';
import { ModifierAdmissionError } from './modifiers';
const sortClipsByAt = (clips) => {
    return [...clips].sort((left, right) => left.at - right.at);
};
const hasPositionOverride = (clip) => {
    return (clip.x !== undefined
        || clip.y !== undefined
        || clip.width !== undefined
        || clip.height !== undefined
        || clip.cropTop !== undefined
        || clip.cropBottom !== undefined
        || clip.cropLeft !== undefined
        || clip.cropRight !== undefined);
};
const getAssetEntry = (clip, assets) => {
    return clip.asset ? assets.assets[clip.asset] : undefined;
};
export const renderVisualClipContent = (track, clip, assets, fps, theme, globalFrame, modifiers) => {
    if (clip.clipType === 'effect-layer') {
        if (clip.modifiers?.length)
            throw new ModifierAdmissionError('unsupported-host', 'Effect layers are not owning visual clip subtrees');
        return null;
    }
    // Sprint 5 EFFECT_REGISTRY dispatch (the migration target cited at
    // tools/remotion/src/HypeComposition.tsx:58-64). Same shape; the
    // registry now lives inside the package.
    const effectId = (clip.clipType && clip.clipType in EFFECT_REGISTRY
        ? clip.clipType
        : clip.clipType ? CLIP_TYPE_ALIASES[clip.clipType] : undefined);
    if (effectId) {
        const EffectComponent = EFFECT_REGISTRY[effectId];
        const assetEntry = getAssetEntry(clip, assets);
        return (_jsx(ClipLook, { clip: clip, fps: fps, kind: "element", globalFrame: globalFrame, modifierRegistry: modifiers.registry, onModifierFinding: modifiers.onFinding, children: _jsx(EffectComponent, { clip: clip, params: resolveParams(clip), theme: theme, fps: fps, assetEntry: assetEntry, globalFrame: globalFrame }) }));
    }
    const assetEntry = getAssetEntry(clip, assets);
    const modifierBounds = mediaModifierBounds(clip, track, assetEntry, modifiers.width, modifiers.height);
    const lookProps = { clip, fps, globalFrame, modifierBounds, modifierRegistry: modifiers.registry,
        onModifierFinding: modifiers.onFinding };
    const baseVisual = (_jsx(VisualClip, { clip: clip, track: track, assetEntry: assetEntry, fps: fps, applyOpacity: (clip.modifiers?.length ?? 0) === 0 }));
    if (hasPositionOverride(clip)) {
        return _jsx(ClipLook, { ...lookProps, kind: "media", children: baseVisual });
    }
    if ((track.scale ?? 1) !== 1) {
        return (_jsx(ClipLook, { ...lookProps, kind: "media", children: _jsx(AbsoluteFill, { style: {
                    transform: `scale(${track.scale})`,
                    transformOrigin: 'center center',
                    overflow: 'hidden',
                    isolation: 'isolate',
                }, children: baseVisual }) }));
    }
    return _jsx(ClipLook, { ...lookProps, kind: "media", children: baseVisual });
};
export const renderVisualSequence = (track, clip, assets, fps, theme, plan, globalFrame, modifiers) => {
    if (clip.clipType === 'am-sprite' && clip.params?.loop !== undefined) {
        assertLoopCoverage(clip.params.loop, plan.drawStart, plan.drawDuration);
    }
    const content = renderVisualClipContent(track, clip, assets, fps, theme, globalFrame, modifiers);
    if (!content) {
        return null;
    }
    // A clip with no transition on either side renders exactly as before.
    const needsLayer = plan.drawDuration > plan.ownDuration || plan.fadeIn !== null || plan.fadeOut !== null;
    return (_jsx(Sequence, { from: plan.drawStart, durationInFrames: plan.drawDuration, children: needsLayer ? _jsx(TransitionLayer, { plan: plan, children: content }) : content }, clip.id));
};
const clipsCanTransition = (fromClip, toClip, fps) => {
    if (!fromClip.transition || fromClip.clipType === 'effect-layer' || toClip.clipType === 'effect-layer') {
        return false;
    }
    const fromStart = Math.round(fromClip.at * fps);
    const fromEnd = fromStart + getClipDurationInFrames(fromClip, fps);
    const toStart = Math.round(toClip.at * fps);
    return toStart >= fromStart && toStart <= fromEnd;
};
const resolveTransitionSpec = (fromClip, toClip, fps, theme) => {
    if (!clipsCanTransition(fromClip, toClip, fps)) {
        return null;
    }
    const resolved = resolveTransitionReference(fromClip.transition, theme, fps);
    if (!resolved) {
        return null;
    }
    return {
        T: resolved.durationFrames,
        kind: resolved.id === 'fade' ? 'dip' : 'dissolve',
        align: resolved.align,
    };
};
// Both native requested-frame metadata and drawing use this exact preparation boundary.
export const prepareCompositionScene = (tracks, assets, fps, theme, width, height, registry, roles) => prepareScene(tracks.map(({ track, clips }) => {
    const sorted = sortClipsByAt(clips);
    return { track, clips: sorted, transitions: sorted.slice(0, -1).map((clip, index) => resolveTransitionSpec(clip, sorted[index + 1], fps, theme)) };
}), assets, fps, width, height, { elements: new Set(Object.keys(EFFECT_REGISTRY).concat(Object.keys(CLIP_TYPE_ALIASES))),
    modifiers: registry, roles });
const renderVisualTrack = (track, clips, assets, fps, theme, globalFrame, modifiers) => {
    const sortedClips = sortClipsByAt(clips);
    if (sortedClips.length === 0) {
        return null;
    }
    const prepared = prepareCompositionScene([{ track, clips: sortedClips }], assets, fps, theme, modifiers.width, modifiers.height, modifiers.registry);
    const evaluated = querySceneFrame(prepared, globalFrame);
    const rendered = evaluated.layers.map(layer => renderVisualSequence(track, sortedClips[layer.clipIndex], assets, fps, theme, prepared.tracks[0].prepared.plans[layer.clipIndex], globalFrame, modifiers));
    return (_jsx(AbsoluteFill, { style: {
            opacity: track.opacity ?? 1,
            mixBlendMode: track.blendMode && track.blendMode !== 'normal' ? track.blendMode : undefined,
        }, children: rendered }, track.id));
};
export const renderAudioTrack = (track, clips, assets, fps) => {
    return sortClipsByAt(clips).map((clip) => {
        if (clip.modifiers?.length)
            throw new ModifierAdmissionError('unsupported-host', 'Audio tracks do not support visual modifiers');
        const assetEntry = getAssetEntry(clip, assets);
        return (_jsx(Sequence, { from: Math.round(clip.at * fps), durationInFrames: getClipDurationInFrames(clip, fps), children: _jsx(AudioTrack, { clip: clip, track: track, assetEntry: assetEntry, fps: fps }) }, clip.id));
    });
};
export const TimelineComposition = (props) => {
    const { timeline, assets } = props;
    const { fps, width, height } = useVideoConfig();
    const visualTracks = [...getVisualTracks(timeline)].reverse();
    const audioTracks = getAudioTracks(timeline);
    return (_jsx(AbsoluteFill, { style: {
            backgroundColor: props.theme?.visual.color.bg ?? 'black',
            width,
            height,
            overflow: 'hidden',
        }, children: _jsx(ThemeProvider, { value: props.theme ?? DEFAULT_THEME, children: _jsx(TimelineCompositionBody, { timeline: timeline, assets: assets, visualTracks: visualTracks, audioTracks: audioTracks, fps: fps, modifiers: { width, height, registry: props.modifierRegistry, onFinding: props.onModifierFinding } }) }) }));
};
// Backwards-compatibility alias for callers that haven't migrated off the
// HypeComposition name yet. The exported component identity is the same.
export const HypeComposition = TimelineComposition;
const TimelineCompositionBody = ({ timeline, assets, visualTracks, audioTracks, fps, modifiers, }) => {
    const theme = useTheme();
    const globalFrame = useCurrentFrame();
    return (_jsxs(_Fragment, { children: [_jsx(AbsoluteFill, { style: { position: 'relative', overflow: 'hidden' }, children: visualTracks.map((track) => {
                    const clips = timeline.clips.filter((clip) => clip.track === track.id);
                    return renderVisualTrack(track, clips, assets, fps, theme, globalFrame, modifiers);
                }) }), audioTracks.map((track) => {
                const clips = timeline.clips.filter((clip) => clip.track === track.id);
                return renderAudioTrack(track, clips, assets, fps);
            })] }));
};
