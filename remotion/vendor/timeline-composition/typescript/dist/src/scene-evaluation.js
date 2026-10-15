/** Pure requested-frame scene answers. Undeclared component pixels remain unknown. */
import { prepareTrack, queryTrackFrame } from './evaluation';
import { getClipDurationInFrames } from './lib/duration';
import { clipOpacity, fadeMultiplier } from './lib/clip-look';
import { admitModifiers } from './lib/modifier-admission';
import { mediaModifierBounds } from './lib/modifier-bounds';
// Track input order is bottom to top, exactly Composition's painter order.
export const prepareScene = (tracks, assets, fps, width, height, options = {}) => {
    if (![fps, width, height].every(value => Number.isFinite(value) && value > 0))
        throw new Error('evaluation_inputs_invalid');
    return { tracks: tracks.map(input => ({ ...input, prepared: prepareTrack(input.clips.map(clip => ({
                start: Math.round(clip.at * fps), duration: getClipDurationInFrames(clip, fps)
            })), input.transitions) })),
        assets, fps, width, height, elements: options.elements ?? new Set(), modifiers: options.modifiers ?? {}, roles: options.roles ?? {} };
};
const rectContains = (a, b) => a.status === 'known' && b.status === 'known'
    && a.rect.x <= b.rect.x && a.rect.y <= b.rect.y
    && a.rect.x + a.rect.width >= b.rect.x + b.rect.width && a.rect.y + a.rect.height >= b.rect.y + b.rect.height;
// Exact rectangular support partition, using the renderer's evaluated bounds and alpha.
// This handles tiled pictures and overlapping translucent plates without inventing geometry.
const canvasCovered = (layers, width, height) => {
    const known = layers.filter(layer => layer.coverage !== 'unknown' && layer.bounds.status === 'known'
        && layer.opacity !== null && layer.opacity > 0);
    const rects = known.map(layer => ({ rect: layer.bounds.rect,
        opacity: layer.opacity }));
    const xs = [...new Set([0, width, ...rects.flatMap(({ rect }) => [Math.max(0, Math.min(width, rect.x)), Math.max(0, Math.min(width, rect.x + rect.width))])])].sort((a, b) => a - b);
    for (let i = 0; i < xs.length - 1; i++) {
        const x = (xs[i] + xs[i + 1]) / 2;
        const active = rects.filter(({ rect }) => rect.x <= x && rect.x + rect.width >= x);
        const ys = [...new Set([0, height, ...active.flatMap(({ rect }) => [Math.max(0, Math.min(height, rect.y)), Math.max(0, Math.min(height, rect.y + rect.height))])])].sort((a, b) => a - b);
        for (let j = 0; j < ys.length - 1; j++) {
            const y = (ys[j] + ys[j + 1]) / 2;
            const alpha = 1 - active.filter(({ rect }) => rect.y <= y && rect.y + rect.height >= y)
                .reduce((remaining, layer) => remaining * (1 - layer.opacity), 1);
            if (alpha < .999)
                return false;
        }
    }
    return true;
};
export const querySceneFrame = (scene, frame) => {
    if (!Number.isSafeInteger(frame) || frame < 0)
        throw new Error('evaluation_inputs_invalid');
    const layers = [];
    for (const { track, clips, prepared } of scene.tracks) {
        for (const draw of queryTrackFrame(prepared, frame).layers) {
            const clip = clips[draw.clipIndex];
            const findings = [];
            const unknown = (code, reason) => { findings.push({ code, clipId: clip.id, reason }); };
            const element = scene.elements.has(clip.clipType ?? '');
            const asset = clip.asset ? scene.assets.assets[clip.asset] : undefined;
            let bounds = element ? { status: 'unknown', reason: 'Component has no declared evaluated bounds' }
                : mediaModifierBounds(clip, track, asset, scene.width, scene.height);
            let opacity = Math.min(1, Math.max(0, track.opacity ?? 1))
                * clipOpacity(clip, draw.ownFrame, scene.fps) * draw.opacity;
            const absent = clip.clipType === 'effect-layer' || (!element && !asset?.file);
            if (element && !absent)
                unknown('unknown-component-internals', 'Final component alpha and support are undeclared');
            if (clip.entrance !== undefined || clip.exit !== undefined) {
                unknown('unknown-animation-geometry', 'Animation does not declare evaluated support and alpha');
                bounds = { status: 'unknown', reason: 'Animation geometry is undeclared' };
            }
            try {
                admitModifiers(clip.modifiers ?? [], scene.modifiers);
            }
            catch (error) {
                unknown('unknown-modifier-bounds', error instanceof Error ? error.message : String(error));
                bounds = { status: 'unknown', reason: 'Modifier bounds, alpha or support are undeclared' };
            }
            if (bounds.status === 'unknown' && !absent)
                unknown(clip.modifiers?.length ? 'unknown-modifier-bounds' : 'unknown-component-bounds', bounds.reason);
            // Unresolved intrinsic alpha (e.g. PNG/video alpha) cannot establish opaque coverage.
            const opaqueMedia = asset?.type === 'image/jpeg';
            if (!absent && !element && !opaqueMedia)
                unknown('unknown-media-alpha', 'Media does not declare opaque alpha');
            const uncertain = findings.some(item => item.code !== 'unknown-component-bounds');
            let visible = absent || opacity === 0 ? false : uncertain ? 'unknown' : true;
            if (bounds.status === 'known' && (bounds.rect.width === 0 || bounds.rect.height === 0))
                visible = false;
            if (visible !== false && uncertain)
                opacity = null;
            if (absent)
                opacity = 0;
            const fullFrame = bounds.status === 'known' && bounds.rect.x <= 0 && bounds.rect.y <= 0
                && bounds.rect.width >= scene.width && bounds.rect.height >= scene.height;
            const plan = prepared.plans[draw.clipIndex];
            const dip = !absent && (((plan.fadeIn?.kind === 'dip' || plan.fadeOut?.kind === 'dip') && draw.opacity < 1)
                || fadeMultiplier(clip, draw.ownFrame, scene.fps) < 1);
            layers.push({ clipId: clip.id, track: track.id, clipIndex: draw.clipIndex, z: 0,
                drawLocalFrame: draw.drawLocalFrame, ownFrame: draw.ownFrame, transitionOpacity: draw.opacity,
                opacity, visible, bounds, fullFrame, role: scene.roles[clip.id] ?? (track.id === 'plate' ? 'picture' : 'content'),
                coverage: bounds.status === 'unknown' || uncertain ? 'unknown' : fullFrame && opacity >= .999 ? 'covered' : 'partial', findings, dip });
        }
    }
    // Occlusion only from proven opaque support. Unknown/translucent layers never erase content.
    const front = [];
    for (const layer of [...layers].reverse()) {
        layer.z = front.length;
        if (front.some(top => top.visible === true && top.opacity === 1 && top.coverage !== 'unknown'
            && rectContains(top.bounds, layer.bounds)))
            layer.visible = false;
        front.push(layer);
    }
    const visible = front.filter(layer => layer.visible !== false);
    const picture = visible.filter(layer => ['picture', 'background'].includes(layer.role));
    const covered = canvasCovered(picture, scene.width, scene.height);
    const unknownCoverage = visible.some(layer => layer.coverage === 'unknown');
    const coverage = covered ? 'COVERED' : unknownCoverage ? 'UNKNOWN' : layers.some(layer => ['picture', 'background'].includes(layer.role) && layer.dip) ? 'DIP' : 'GAP';
    const content = coverage !== 'COVERED' ? null : visible.some(layer => layer.role !== 'background'
        && layer.visible === true && (layer.opacity ?? 0) >= .05) ? 'CONTENT'
        : visible.some(layer => layer.role !== 'background' && layer.visible === 'unknown') ? 'UNKNOWN' : 'EMPTY';
    return { frame, layers, coverage, content, findings: visible.flatMap(layer => layer.findings) };
};
