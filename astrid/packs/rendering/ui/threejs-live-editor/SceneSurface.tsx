import { createElement, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  SceneFrameHost,
  sceneDocument,
  validateScenePackage,
  verifyScenePackageIntegrity,
  type ValidatedScenePackage,
  type SceneFrame,
  type SceneStatus,
  type SceneInfo,
  type SceneView,
} from './runtime';
import { sceneTrace, traceId } from './diagnostics';
import { hasInlineHtml, readLiveScene, type LiveSceneDocument } from './liveSceneDocument';
import { peekLiveScene, resolveLiveScene, type SceneObjectStorage } from './liveSceneCache';

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function suppliedRevision(value: unknown): string {
  return typeof value === 'object' && value !== null && 'revision' in value && typeof value.revision === 'string'
    ? value.revision
    : '';
}

const UNAVAILABLE = "This scene's files could not be loaded";

type ResolvedScene = { document?: LiveSceneDocument; error?: string };

/**
 * The document for a stored scene. Inline HTML is used as-is; a reference is loaded from the project
 * object store once per revision (liveSceneCache). `document` is undefined while it loads.
 */
function useLiveSceneDocument(source: unknown, storage: SceneObjectStorage | undefined): ResolvedScene {
  const inline = hasInlineHtml(source) ? source : undefined;
  const cached = inline ? undefined : peekLiveScene(storage, source);
  const stored = inline || cached ? undefined : readLiveScene(source);
  const key = stored ? `${stored.source.objectId}@${stored.revision}` : '';
  const [loaded, setLoaded] = useState<{ key: string; document?: LiveSceneDocument; error?: string }>();
  useEffect(() => {
    if (!stored || !key) return;
    if (!storage) {
      setLoaded({ key, error: 'Project storage is unavailable' });
      return;
    }
    let active = true;
    resolveLiveScene(storage, stored).then(
      (document) => { if (active) setLoaded({ key, document }); },
      (error: unknown) => { if (active) setLoaded({ key, error: messageOf(error) }); },
    );
    return () => { active = false; };
    // `stored` is derived from `key` and `source`; the key is the identity that matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, storage]);
  if (inline) return { document: inline };
  if (cached) return { document: cached };
  if (!stored) return { error: 'Missing scene source/revision' };
  if (loaded?.key !== key) return {};
  return loaded.document ? { document: loaded.document } : { error: loaded.error ?? UNAVAILABLE };
}

type SceneRequest = { pkg?: ValidatedScenePackage; error?: string; revision: string; pending?: boolean };
type SceneDocument = {
  request: SceneRequest;
  generation: number;
  pkg: ValidatedScenePackage;
  instance: string;
  html: string;
};

// Each keyed document owns its transport. Moving a candidate to the displayed
// role must not navigate its iframe or restart initialization.
function SceneInstance({ document, component, frame, visible, width, height, onStatus }: {
  document: SceneDocument; component: object; frame?: SceneFrame; visible: boolean;
  width: number; height: number; onStatus: (document: SceneDocument, status: SceneStatus, info: SceneInfo) => void;
}) {
  const iframe = useRef<HTMLIFrameElement>(null);
  const host = useRef<SceneFrameHost>();
  const callback = useRef(onStatus); callback.current = onStatus;
  const desiredFrame = useRef(frame); desiredFrame.current = frame;
  const diagnostic = document.pkg.package.html.includes('<!-- astrid-l1b-diagnostic -->') || document.pkg.__l1bTrace === true;
  useEffect(() => {
    // Resolve the WindowProxy on each retry from this generation's element,
    // never from another candidate's ref.
    const frameElement = iframe.current;
    let active = true;
    const effectOwner = diagnostic ? {
      component: traceId(component, 'component'), source: traceId(document.pkg, 'source'),
      document: document.instance, revision: document.pkg.package.revision,
      effect: traceId({}, 'effect'), host: crypto.randomUUID(), listener: crypto.randomUUID(),
      iframe: frameElement && traceId(frameElement, 'iframe'),
    } : {};
    const effectTrace = (stage: string, detail: Record<string, unknown> = {}) => sceneTrace(diagnostic, effectOwner, stage, detail);
    effectTrace('parent-effect-setup', { sourceTime: desiredFrame.current?.sourceTime, connected: frameElement?.isConnected });
    const instance = new SceneFrameHost(document.pkg, document.instance,
      message => {
        const frameWindow = frameElement?.contentWindow;
        const detail = { message, connected: frameElement?.isConnected, endpoint: frameWindow && diagnostic ? traceId(frameWindow, 'window') : null };
        if (!frameWindow) { effectTrace('parent-send-skipped', detail); return; }
        try { frameWindow.postMessage(message, '*'); effectTrace('parent-send-posted', detail); }
        catch (error) { effectTrace('parent-send-threw', { ...detail, error: String(error) }); throw error; }
      },
      value => {
        effectTrace('parent-status', value);
        if (active && value.phase !== 'disposed') callback.current(document, value, host.current?.info ?? { events: [], shots: [], explore: null });
      }, 30000, diagnostic ? effectTrace : undefined);
    host.current = instance;
    const receive = (event: MessageEvent) => {
      const frameWindow = frameElement?.contentWindow;
      const matches = !!frameWindow && event.source === frameWindow;
      effectTrace('parent-message-received', { message: event.data, sourceMatches: matches, connected: frameElement?.isConnected, endpoint: frameWindow && diagnostic ? traceId(frameWindow, 'window') : null });
      if (matches && event.data?.protocol === 'astrid.scene/trace') { effectTrace('child-record', { record: event.data.record }); return; }
      if (matches && event.data && typeof event.data === 'object') instance.receive(event.data);
      else effectTrace('parent-message-rejected', { reason: 'endpoint/source/payload' });
    };
    window.addEventListener('message', receive);
    effectTrace('parent-listener-installed');
    const retire = () => {
      if (!active) return;
      active = false;
      effectTrace('parent-effect-cleanup'); instance.dispose(false);
      window.removeEventListener('message', receive); effectTrace('parent-listener-removed');
      window.removeEventListener('pagehide', pageTransition);
      window.removeEventListener('pageshow', pageTransition);
      if (host.current === instance) host.current = undefined;
    };
    const pageTransition = (event: PageTransitionEvent) => {
      // BFCache preserves React effects, but the child disposes on pagehide.
      // Retire synchronously, before suspension can leave deadlines/listeners
      // alive, even when React cannot commit a cleanup until restoration.
      if (event.persisted) retire();
    };
    window.addEventListener('pagehide', pageTransition);
    window.addEventListener('pageshow', pageTransition);
    instance.initialize();
    if (desiredFrame.current) instance.request(desiredFrame.current);
    // StrictMode can replay effects while the document survives. Stop this
    // transport only; removal/navigation's pagehide owns scene disposal.
    return retire;
  }, [document, component, diagnostic]);
  useLayoutEffect(() => {
    if (desiredFrame.current) host.current?.request(desiredFrame.current);
  }, [frame?.sourceTime, frame?.width, frame?.height, frame?.view, frame?.worldTime]);
  return createElement('iframe', {
    ref: iframe, title: `Live scene ${document.pkg.package.revision}`, sandbox: 'allow-scripts',
    'data-scene-instance': document.instance, 'data-scene-role': visible ? 'displayed' : 'candidate',
    onLoad: () => {
      sceneTrace(diagnostic, { component: traceId(component, 'component'), document: document.instance, revision: document.pkg.package.revision }, 'parent-iframe-load');
      host.current?.initialize();
    },
    srcDoc: document.html,
    // A candidate must keep rendering while it is not shown. `visibility: hidden` (like display:none or
    // off-screen) lets Chromium render-throttle a cross-origin frame, and a throttled frame can run no
    // lifecycle update: document.fonts.ready and requestAnimationFrame then never settle, so the
    // candidate never acknowledges initialization. opacity 0 keeps it composited and unthrottled.
    'aria-hidden': visible ? undefined : true,
    style: { position: 'absolute', inset: 0, width, height, border: 0, display: 'block', opacity: visible ? 1 : 0, pointerEvents: 'none' },
  });
}

function sceneOverlay(state: 'loading' | 'unavailable', width: number, height: number, detail?: string) {
  const loading = state === 'loading';
  return createElement('div', {
    'data-testid': loading ? 'astrid-live-scene-loading' : 'astrid-live-scene-unavailable',
    role: loading ? 'status' : 'alert',
    style: {
      position: 'absolute', inset: 0, width, height, display: 'grid', placeItems: 'center', padding: 16,
      background: '#111', color: 'white', font: '12px sans-serif', textAlign: 'center',
    },
  }, loading
    ? 'Loading live scene…'
    : createElement('span', null, UNAVAILABLE, detail ? createElement('span', {
      style: { display: 'block', marginTop: 6, opacity: 0.7, font: '11px monospace' },
    }, detail) : null));
}

export function SceneSurface({ source, storage, sourceTime, width, height, view, worldTime, capture, onStatus, onReady, showStatus = false }: {
  /** A loaded document, or a stored reference (loaded from `storage` when it has no inline HTML). */
  source: unknown; storage?: SceneObjectStorage; sourceTime: number; width: number; height: number;
  /** The explorer's free view. Never set by the timeline preview or exports: the scene then draws its authored camera. */
  view?: SceneView;
  /** World time for the explorer (sourceTime equals it); never set by the timeline preview or exports. */
  worldTime?: number;
  /** Thumbnail instances only: the acknowledged frame then carries `image` in its status. */
  capture?: boolean;
  onStatus?: (status: SceneStatus) => void; showStatus?: boolean;
  /** Called once per loaded scene, when its document first becomes the displayed one. */
  onReady?: (info: SceneInfo) => void;
}) {
  const resolved = useLiveSceneDocument(source, storage);
  const revision = suppliedRevision(source);
  const requested = useMemo<SceneRequest>(() => {
    if (resolved.error) return { error: resolved.error, revision };
    if (!resolved.document) return { revision, pending: true };
    try {
      const pkg = validateScenePackage(resolved.document);
      return { pkg, revision: pkg.package.revision };
    } catch (error) { return { error: messageOf(error), revision }; }
  }, [resolved.document, resolved.error, revision]);
  // Fence completion immediately when props change, including before effects
  // clean up an obsolete integrity check or host.
  const desired = useRef(requested); desired.current = requested;
  const frame = useRef<SceneFrame>({ sourceTime, width, height }); frame.current = { sourceTime, width, height, ...(view ? { view } : {}), ...(worldTime === undefined ? {} : { worldTime }), ...(capture ? { capture } : {}) };
  const component = useRef({});
  const callback = useRef(onStatus); callback.current = onStatus;
  const readyCallback = useRef(onReady); readyCallback.current = onReady;
  const announced = useRef(new WeakSet<SceneDocument>());
  const [status, setStatus] = useState<SceneStatus>(() => ({
    phase: requested.error ? 'error' : 'loading', revision: requested.revision,
    ...(requested.error ? { error: requested.error } : {}),
  }));
  const [displayed, setDisplayed] = useState<SceneDocument>();
  const [candidate, setCandidate] = useState<SceneDocument>();
  const [displayedStatus, setDisplayedStatus] = useState<SceneStatus>();
  const lifecycle = useRef({ suspended: false, generation: 0 });
  const [restoreGeneration, setRestoreGeneration] = useState(0);
  useEffect(() => {
    const hide = (event: PageTransitionEvent) => {
      if (event.persisted) lifecycle.current.suspended = true;
    };
    const show = (event: PageTransitionEvent) => {
      if (!event.persisted) return;
      lifecycle.current.suspended = false;
      const generation = ++lifecycle.current.generation;
      // A restored iframe's bridge has already disposed. Clear last-good
      // ownership and require a fresh document to acknowledge the latest frame.
      setDisplayed(undefined); setDisplayedStatus(undefined); setCandidate(undefined);
      const request = desired.current;
      const value: SceneStatus = request.error
        ? { phase: 'error', revision: request.revision, error: request.error }
        : { phase: 'loading', revision: request.revision };
      setStatus(value); callback.current?.(value);
      setRestoreGeneration(generation);
    };
    window.addEventListener('pagehide', hide);
    window.addEventListener('pageshow', show);
    return () => {
      window.removeEventListener('pagehide', hide);
      window.removeEventListener('pageshow', show);
    };
  }, []);
  useEffect(() => {
    let active = true;
    const current = () => active && desired.current === requested
      && !lifecycle.current.suspended && lifecycle.current.generation === restoreGeneration;
    const report = (value: SceneStatus) => {
      if (!current()) return;
      setStatus(value); callback.current?.(value);
    };
    setCandidate(undefined);
    if (requested.pending) {
      report({ phase: 'loading', revision: requested.revision });
    } else if (!requested.pkg) {
      report({ phase: 'error', revision: requested.revision, error: requested.error ?? 'Invalid scene package' });
    } else {
      const pkg = requested.pkg;
      report({ phase: 'loading', revision: requested.revision });
      verifyScenePackageIntegrity(pkg).then(() => {
        if (!current()) return;
        const instance = crypto.randomUUID();
        setCandidate({ request: requested, generation: restoreGeneration, pkg, instance, html: sceneDocument(pkg, instance) });
      }).catch(error => report({ phase: 'error', revision: requested.revision, error: messageOf(error) }));
    }
    return () => { active = false; };
  }, [requested, restoreGeneration]);
  const receiveStatus = (document: SceneDocument, value: SceneStatus, info: SceneInfo) => {
    if (document.request !== desired.current || lifecycle.current.suspended
      || document.generation !== lifecycle.current.generation) return;
    if (value.phase === 'ready') {
      // SceneFrameHost reports the complete acknowledged frame. Recheck against
      // props as well as its request fence, since props may precede its effect.
      const ready = value as SceneStatus & SceneFrame;
      const current = frame.current;
      if (ready.sourceTime !== current.sourceTime || ready.width !== current.width || ready.height !== current.height) return;
      setDisplayed(document); setDisplayedStatus(value);
      if (!announced.current.has(document)) { announced.current.add(document); readyCallback.current?.(info); }
      setCandidate(previous => previous === document ? undefined : previous);
    } else if (value.phase === 'error') {
      setCandidate(previous => previous === document ? undefined : previous);
    }
    setStatus(value); callback.current?.(value);
  };
  const currentCandidate = candidate?.request === requested ? candidate : undefined;
  const documents = [displayed, currentCandidate].filter((document): document is SceneDocument => !!document);
  const diagnostic = requested.pkg?.package.html.includes('<!-- astrid-l1b-diagnostic -->') || requested.pkg?.__l1bTrace === true;
  // Never a blank frame: while the scene loads, or when it cannot be loaded or verified, say so inline.
  const overlay = displayed ? null
    : requested.pending ? sceneOverlay('loading', width, height)
    : !requested.pkg || status.phase === 'error' ? sceneOverlay('unavailable', width, height, requested.error)
    : null;
  return createElement('div', {
    'data-testid': 'astrid-live-scene-entry', 'data-phase': status.phase,
    'data-desired-time': sourceTime.toFixed(3), 'data-source-time': displayedStatus?.sourceTime?.toFixed(3),
    'data-revision': requested.revision, 'data-desired-revision': requested.revision, 'data-displayed-revision': displayedStatus?.revision,
    ...(diagnostic ? { 'data-document': (currentCandidate ?? displayed)?.instance, 'data-request-id': status.requestId, 'data-exact-time': status.sourceTime, 'data-component': traceId(component.current, 'component') } : {}),
    style: { position: 'relative', width, height, overflow: 'hidden', background: '#111' },
  }, ...documents.map(document => createElement(SceneInstance, {
    key: document.instance, document, component: component.current,
    frame: document.request === requested ? frame.current : undefined,
    visible: document === displayed, width, height, onStatus: receiveStatus,
  })), overlay, showStatus ? createElement('div', {
    style: { position: 'absolute', left: 8, top: 8, background: '#000b', color: 'white', font: '12px monospace', padding: 6 },
  }, `${status.phase} · displayed ${displayedStatus?.revision ?? '…'} · requested ${requested.revision} · source ${displayedStatus?.sourceTime?.toFixed(3) ?? '…'}s · requested ${sourceTime.toFixed(3)}s${status.error ? ` · ${status.error}` : ''}`) : null);
}
