// @vitest-environment jsdom
import { createElement } from 'react';
import { render, cleanup, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { SceneSurface } from '../../../astrid/packs/rendering/ui/threejs-live-editor/SceneSurface';
import { ingestPreparedPackage } from '../../../astrid/packs/rendering/ui/threejs-live-editor/authoring';

afterEach(cleanup);

const HTML = '<html><head></head><body>Lantern</body></html>';

async function digest(bytes: Uint8Array): Promise<string> {
  const hash = await globalThis.crypto.subtle.digest('SHA-256', bytes as unknown as BufferSource);
  return `sha256:${Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, '0')).join('')}`;
}

function makeStorage() {
  const objects = new Map<string, Uint8Array>();
  const reads: string[] = [];
  return {
    objects,
    reads,
    async ingest(input: Uint8Array, mediaType: string, filename?: string) {
      const bytes = new Uint8Array(input);
      const id = await digest(bytes);
      objects.set(id, bytes);
      return { object_id: id, digest: id, media_type: mediaType, size: bytes.byteLength, filename };
    },
    async read(objectId: string) {
      reads.push(objectId);
      const value = objects.get(objectId);
      if (!value) throw new Error(`unknown object ${objectId}`);
      return new Uint8Array(value);
    },
  };
}

async function fixture(storage = makeStorage()) {
  const input = {
    manifest: { formatVersion: 1 as const, entry: 'scene.html', duration: 4, authoredFps: 30 },
    entry: { bytes: new TextEncoder().encode(HTML), mediaType: 'text/html', filename: 'scene.html' },
    assets: [],
  };
  const { liveScene } = await ingestPreparedPackage(storage, input);
  return { storage, liveScene };
}

const mount = (source: unknown, storage?: unknown) => render(createElement(SceneSurface, {
  source, storage, sourceTime: 0, width: 320, height: 180,
} as Parameters<typeof SceneSurface>[0]));

describe('SceneSurface with a stored reference', () => {
  it('shows a loading state, then mounts the frame once the document is loaded', async () => {
    const { storage, liveScene } = await fixture();
    const { container } = mount(liveScene, storage);
    expect(container.querySelector('[data-testid="astrid-live-scene-loading"]')).not.toBeNull();
    expect(container.querySelector('iframe')).toBeNull();
    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull());
    expect(container.querySelector('[data-testid="astrid-live-scene-loading"]')).toBeNull();
    expect(container.querySelector('[data-testid="astrid-live-scene-unavailable"]')).toBeNull();
  });

  it('loads the package once per revision: a re-mount reads nothing new', async () => {
    const { storage, liveScene } = await fixture();
    const first = mount(liveScene, storage);
    await waitFor(() => expect(first.container.querySelector('iframe')).not.toBeNull());
    const packageReads = storage.reads.filter((id) => id === liveScene.source.objectId).length;
    expect(packageReads).toBe(1);
    cleanup();

    const second = mount(liveScene, storage);
    await waitFor(() => expect(second.container.querySelector('iframe')).not.toBeNull());
    expect(storage.reads.filter((id) => id === liveScene.source.objectId)).toHaveLength(1);
  });

  it('shows an inline error, never a blank frame, when the object is missing', async () => {
    const { storage, liveScene } = await fixture();
    storage.objects.clear();
    const { container } = mount(liveScene, storage);
    await waitFor(() => expect(container.querySelector('[data-testid="astrid-live-scene-unavailable"]')).not.toBeNull());
    expect(container.textContent).toContain("This scene's files could not be loaded");
    expect(container.querySelector('iframe')).toBeNull();
  });

  it('reports unavailable storage as an error instead of waiting forever', async () => {
    const { liveScene } = await fixture();
    const { container } = mount(liveScene, undefined);
    await waitFor(() => expect(container.querySelector('[data-testid="astrid-live-scene-unavailable"]')).not.toBeNull());
    expect(container.textContent).toContain('Project storage is unavailable');
  });

  it('uses inline HTML without touching storage (legacy clips)', async () => {
    const { storage, liveScene } = await fixture();
    const readsBefore = storage.reads.length;
    const { container } = mount({ ...liveScene, html: HTML }, storage);
    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull());
    expect(storage.reads.length).toBe(readsBefore);
  });
});
