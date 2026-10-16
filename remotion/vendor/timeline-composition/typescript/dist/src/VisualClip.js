import { jsx as _jsx } from "react/jsx-runtime";
import { Img, staticFile, useVideoConfig } from 'remotion';
import { Video } from '@remotion/media';
import { getSanitizedPlaybackRate, getSanitizedVolume, } from './lib/duration';
import { computeViewportMediaLayout, getIntrinsicMediaSize, normalizeRenderCropValues, } from './lib/render-bounds';
import { useFadeOpacity } from './lib/fade';
import { computeMediaTrim } from './lib/trim';
const toRenderableFileUrl = (file) => {
    if (typeof file !== 'string' || file.trim().length === 0) {
        return null;
    }
    if (file.startsWith('http://') || file.startsWith('https://')) {
        return file;
    }
    return staticFile(file);
};
export const VisualClip = ({ clip, track, assetEntry, fps, applyOpacity = true }) => {
    const { width: compositionWidth, height: compositionHeight } = useVideoConfig();
    const fileUrl = toRenderableFileUrl(assetEntry?.file);
    if (!fileUrl || !assetEntry) {
        return null;
    }
    const hasPositionOverride = (clip.x !== undefined
        || clip.y !== undefined
        || clip.width !== undefined
        || clip.height !== undefined
        || clip.cropTop !== undefined
        || clip.cropBottom !== undefined
        || clip.cropLeft !== undefined
        || clip.cropRight !== undefined);
    const useViewportLayout = (track.fit ?? 'contain') === 'manual' || hasPositionOverride;
    const effectiveVolume = track.muted
        ? 0
        : getSanitizedVolume(track.volume ?? 1) * getSanitizedVolume(clip.volume ?? 1);
    const fadedOpacity = useFadeOpacity(clip, fps) * (clip.opacity ?? 1);
    const opacity = applyOpacity ? fadedOpacity : undefined;
    const mixBlendMode = track.blendMode && track.blendMode !== 'normal' ? track.blendMode : undefined;
    const isImage = assetEntry.type?.startsWith('image');
    if (!useViewportLayout) {
        const sharedStyle = {
            width: '100%',
            height: '100%',
            objectFit: track.fit === 'cover' ? 'cover' : 'contain',
            opacity,
            mixBlendMode,
        };
        if (isImage) {
            return _jsx(Img, { src: fileUrl, style: sharedStyle, crossOrigin: "anonymous" });
        }
        return (_jsx(Video, { src: fileUrl, ...computeMediaTrim(clip, fps), playbackRate: getSanitizedPlaybackRate(clip.speed), volume: effectiveVolume, muted: effectiveVolume <= 0, style: sharedStyle }));
    }
    const fullBounds = {
        x: clip.x ?? 0,
        y: clip.y ?? 0,
        width: clip.width ?? compositionWidth,
        height: clip.height ?? compositionHeight,
    };
    const cropValues = normalizeRenderCropValues({
        cropTop: clip.cropTop,
        cropBottom: clip.cropBottom,
        cropLeft: clip.cropLeft,
        cropRight: clip.cropRight,
    });
    const intrinsicSize = getIntrinsicMediaSize(assetEntry.resolution);
    const viewportLayout = computeViewportMediaLayout({
        fullBounds,
        cropValues,
        compositionWidth,
        compositionHeight,
        intrinsicWidth: intrinsicSize.width,
        intrinsicHeight: intrinsicSize.height,
    });
    if (!viewportLayout) {
        return null;
    }
    const outerStyle = {
        position: 'absolute',
        left: viewportLayout.renderBounds.x,
        top: viewportLayout.renderBounds.y,
        width: viewportLayout.renderBounds.width,
        height: viewportLayout.renderBounds.height,
        overflow: 'hidden',
        opacity,
    };
    const innerStyle = {
        position: 'absolute',
        left: viewportLayout.mediaBounds.x,
        top: viewportLayout.mediaBounds.y,
        width: viewportLayout.mediaBounds.width,
        height: viewportLayout.mediaBounds.height,
        maxWidth: 'none',
        maxHeight: 'none',
        mixBlendMode,
    };
    if (isImage) {
        return (_jsx("div", { style: outerStyle, children: _jsx(Img, { src: fileUrl, style: innerStyle, crossOrigin: "anonymous" }) }));
    }
    return (_jsx("div", { style: outerStyle, children: _jsx(Video, { src: fileUrl, ...computeMediaTrim(clip, fps), playbackRate: getSanitizedPlaybackRate(clip.speed), volume: effectiveVolume, muted: effectiveVolume <= 0, style: innerStyle }) }));
};
