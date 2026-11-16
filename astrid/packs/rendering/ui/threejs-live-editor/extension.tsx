import { createElement, lazy, Suspense, type FC } from 'react';
import {
  defineExtension,
  type ClipRenderer,
  type ClipTypeContribution,
  type ContributionId,
  type DisposeHandle,
  type ExtensionContext,
  type ExtensionId,
  type PanelRenderProps,
} from '@astrid/editor-sdk';
import { LIVE_SCENE_CLIP_TYPE_ID, THREEJS_LIVE_EDITOR_EXTENSION_ID } from './identity';
import type { CommandContribution } from '@astrid/editor-sdk';

/** Stable first-party identity used by the host release allowlist. */
// IDs are validated by defineExtension; these casts only attach the SDK brands.
export { LIVE_SCENE_CLIP_TYPE_ID, THREEJS_LIVE_EDITOR_EXTENSION_ID } from './identity';
export type {
  LiveSceneObjectInput,
  LiveScenePackageInput,
  LiveScenePublicationCapture,
  LiveScenePublicationResult,
  PreparedScenePlacement,
  PreparedMaplePlacement,
  PublishLiveSceneEditInput,
} from './authoring';

type LiveSceneAuthoringModule = typeof import('./authoring');
let liveSceneAuthoringModule: Promise<LiveSceneAuthoringModule> | undefined;

function loadLiveSceneAuthoring(): Promise<LiveSceneAuthoringModule> {
  return liveSceneAuthoringModule ??= import('./authoring');
}

// IDs are validated by defineExtension; this cast only attaches the SDK brand.
const THREEJS_LIVE_EDITOR_EXTENSION_ID_BRANDED = THREEJS_LIVE_EDITOR_EXTENSION_ID as unknown as ExtensionId;
export const LIVE_SCENE_CLIP_CONTRIBUTION_ID = 'threejs-live-editor-scene-clip' as unknown as ContributionId;
export const LIVE_SCENE_IMPORT_COMMAND_ID = `${THREEJS_LIVE_EDITOR_EXTENSION_ID}.importPreparedScene`;
// Acceptance-only helper identity; deliberately not a normal contribution.
export const LIVE_SCENE_EDIT_COMMAND_ID = `${THREEJS_LIVE_EDITOR_EXTENSION_ID}.applyMapleCameraEdit`;
// Compatibility alias for existing command callers.
export const LIVE_SCENE_AUTHORING_COMMAND_ID = LIVE_SCENE_IMPORT_COMMAND_ID;
export const WORLD_EXPLORER_PANEL_CONTRIBUTION_ID = 'threejs-live-editor-world-explorer';
export const WORLD_EXPLORER_PANEL_RENDER_ID = `${THREEJS_LIVE_EDITOR_EXTENSION_ID}.worldExplorer`;

type LiveSceneClipProps = {
  readonly clipId: string;
  readonly clipTypeId: string;
  readonly time: number;
  readonly sourceTime: number;
  readonly source?: Record<string, unknown>;
  readonly width: number;
  readonly height: number;
};

const LazySceneSurface = lazy(() =>
  import('./SceneSurface').then(({ SceneSurface }) => ({ default: SceneSurface })),
);

const LiveSceneClip: FC<LiveSceneClipProps> = ({
  width,
  height,
  sourceTime,
  source,
}) => {
  return createElement(Suspense, {
    fallback: createElement('div', {
      'data-testid': 'astrid-live-scene-loading',
      style: {
        width,
        height,
        overflow: 'hidden',
        background: '#111',
        color: 'white',
        font: '12px monospace',
        display: 'grid',
        placeItems: 'center',
      },
    }, 'Loading live scene…'),
  }, createElement(LazySceneSurface, { source: source?.liveScene, sourceTime, width, height, showStatus: false }));
};

const LazyWorldExplorerPanel = lazy(() =>
  import('./explorer/WorldExplorerPanel').then(({ WorldExplorerPanel }) => ({ default: WorldExplorerPanel })),
);

const LIVE_SCENE_CONTRIBUTION = {
  id: LIVE_SCENE_CLIP_CONTRIBUTION_ID,
  kind: 'clipType',
  clipTypeId: LIVE_SCENE_CLIP_TYPE_ID,
  label: 'Live Three.js Scene',
  allowBrowserExport: false,
  allowWorkerExport: false,
  order: 0,
} satisfies ClipTypeContribution;

const LIVE_SCENE_AUTHORING_COMMAND = {
  id: 'threejs-live-editor-import-prepared-scene' as unknown as ContributionId,
  kind: 'command',
  command: LIVE_SCENE_IMPORT_COMMAND_ID,
  label: 'Import prepared scene',
  category: 'Three.js Live Editor',
  order: 10,
} satisfies CommandContribution;

export const threejsLiveEditorExtension = defineExtension({
  manifest: {
    id: THREEJS_LIVE_EDITOR_EXTENSION_ID_BRANDED,
    version: '1.0.0',
    label: 'Three.js Live Editor',
    description: 'Live Three.js scene clips and prepared-scene import for the Astrid App editor.',
    apiVersion: 1,
    contributions: [LIVE_SCENE_CONTRIBUTION, LIVE_SCENE_AUTHORING_COMMAND, {
      id: WORLD_EXPLORER_PANEL_CONTRIBUTION_ID as unknown as ContributionId,
      kind: 'panel',
      label: 'World Explorer',
      render: WORLD_EXPLORER_PANEL_RENDER_ID,
    }],
    messages: {
      activated: 'Three.js Live Editor registered.',
      disposed: 'Three.js Live Editor disposed.',
    },
  },

  activate(ctx: ExtensionContext): DisposeHandle {
    const registration = ctx.clipTypes.registerClipType(
      LIVE_SCENE_CLIP_TYPE_ID,
      // The SDK renderer union is intentionally broad; the host invokes this
      // trusted component with the same props shape used by the timeline.
      LiveSceneClip as unknown as ClipRenderer,
      undefined,
      { label: 'Live Three.js Scene' },
    );
    ctx.services.diagnostics.report({
      severity: 'info',
      code: 'threejs-live-editor/registered',
      message: ctx.services.i18n.t('activated'),
    });

    // Guarded: some host and test contexts omit the UI service entirely.
    const worldExplorerRegistration = typeof ctx.ui?.registerRenderer === 'function'
      ? ctx.ui.registerRenderer<PanelRenderProps>(
        WORLD_EXPLORER_PANEL_RENDER_ID,
        (props) => createElement(Suspense, {
          fallback: createElement('div', {
            'data-testid': 'astrid-world-explorer-loading',
            style: { padding: 12, font: '12px sans-serif', opacity: 0.7 },
          }, 'Opening World Explorer…'),
        }, createElement(LazyWorldExplorerPanel, { ...props, context: ctx })),
      )
      : undefined;

    const pickerAbortController = new AbortController();
    const authoringRegistration = ctx.liveSceneAuthoring?.register((request, execution) =>
      loadLiveSceneAuthoring().then(({ executeLiveSceneAuthoring }) => {
        execution.assertActive();
        return executeLiveSceneAuthoring(ctx, request, execution);
      }));

    // The picker is pack-owned and deliberately opened before the first await
    // so the command-palette gesture reaches the native browser input.
    const importCommandRegistration = ctx.commands && typeof ctx.commands.registerCommand === 'function'
      ? ctx.commands.registerCommand(
        LIVE_SCENE_IMPORT_COMMAND_ID,
        async () => {
          const inputPromise = openPreparedSceneFile(pickerAbortController.signal);
          let bytes: Uint8Array;
          try {
            bytes = await inputPromise;
          } catch (error) {
            if (error instanceof Error && error.name === 'AbortError') {
              ctx.chrome.toast('Prepared scene import cancelled.', 'info');
              return;
            }
            throw error;
          }
          let importPreparedSceneFile: LiveSceneAuthoringModule['importPreparedSceneFile'];
          try {
            ({ importPreparedSceneFile } = await loadLiveSceneAuthoring());
          } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            ctx.services.diagnostics.report({
              severity: 'error',
              code: 'threejs-live-editor/authoring-failed',
              message: `Import prepared scene failed: ${message}`,
              detail: { stage: 'import-prepared-scene', error: message },
            });
            ctx.chrome.toast(`Prepared scene import failed: ${message}`, 'error');
            throw error;
          }
          await importPreparedSceneFile(ctx, bytes, pickerAbortController.signal);
        },
        { label: 'Import prepared scene', category: 'Three.js Live Editor' },
      )
      : undefined;

    return {
      dispose(): void {
        try {
          pickerAbortController.abort();
          authoringRegistration?.dispose();
          registration.dispose();
          importCommandRegistration?.dispose();
          ctx.services.diagnostics.report({
            severity: 'info',
            code: 'threejs-live-editor/disposed',
            message: ctx.services.i18n.t('disposed'),
          });
        } finally {
          worldExplorerRegistration?.dispose();
        }
      },
    };
  },
});

function openPreparedSceneFile(signal: AbortSignal): Promise<Uint8Array> {
  if (typeof document === 'undefined') throw new Error('Prepared scene import requires a browser document');
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.json,application/json';
  input.style.position = 'fixed';
  input.style.left = '-10000px';
  input.style.top = '0';
  input.style.width = '1px';
  input.style.height = '1px';
  input.style.opacity = '0';
  const parent = document.body ?? document.documentElement;
  if (!parent) throw new Error('Prepared scene import has no document root');
  parent.appendChild(input);
  return new Promise((resolve, reject) => {
    const abortError = (): Error => {
      const error = new Error('Prepared scene selection cancelled');
      error.name = 'AbortError';
      return error;
    };
    const cleanup = (): void => {
      input.removeEventListener('change', onChange);
      input.removeEventListener('cancel', onCancel);
      signal.removeEventListener('abort', onAbort);
      input.remove();
    };
    const onCancel = (): void => {
      cleanup();
      reject(abortError());
    };
    const onAbort = (): void => {
      cleanup();
      reject(abortError());
    };
    const onChange = (): void => {
      const file = input.files?.[0];
      if (!file) {
        onCancel();
        return;
      }
      file.arrayBuffer().then((buffer) => {
        cleanup();
        resolve(new Uint8Array(buffer));
      }, (error: unknown) => {
        cleanup();
        reject(error);
      });
    };
    if (signal.aborted) {
      onAbort();
      return;
    }
    input.addEventListener('change', onChange, { once: true });
    input.addEventListener('cancel', onCancel, { once: true });
    signal.addEventListener('abort', onAbort, { once: true });
    try {
      input.click();
    } catch (error) {
      cleanup();
      reject(error);
    }
  });
}

export default threejsLiveEditorExtension;
