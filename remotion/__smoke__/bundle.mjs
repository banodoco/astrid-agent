import {mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';

import {bundle} from '@remotion/bundler';
import {selectComposition} from '@remotion/renderer';
import {applyWorkspaceEffectsAlias} from '../webpack-alias.mjs';
import ts from 'typescript';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const projectDir = path.resolve(__dirname, '..');
const repoDir = path.resolve(projectDir, '..');
const entryPoint = path.resolve(projectDir, 'src/index.ts');
const typesGeneratedPath = path.resolve(projectDir, 'src/types.generated.ts');
// Sprint 5 rename: HypeComposition → TimelineComposition.
const compositionId = 'TimelineComposition';

const EXPECTED_ALLOWED = {
  _ASSET_ENTRY_ALLOWED: [
    'content_sha256',
    'duration',
    'etag',
    'file',
    'fps',
    'generationId',
    'resolution',
    'thumbnailUrl',
    'type',
    'url',
    'url_expires_at',
    'variantId',
  ],
  _CLIP_ALLOWED: [
    'asset',
    'at',
    'clipType',
    'clip_order',
    'continuous',
    'cropBottom',
    'cropLeft',
    'cropRight',
    'cropTop',
    'effects',
    'entrance',
    'exit',
    'from',
    'generation',
    'height',
    'hold',
    'id',
    'opacity',
    'params',
    'pool_id',
    'source_uuid',
    'speed',
    'text',
    'to',
    'track',
    'transition',
    'volume',
    'width',
    'x',
    'y',
  ],
  _THEME_OVERRIDES_ALLOWED: ['audio', 'generation', 'pacing', 'visual', 'voice'],
  _TIMELINE_TOP_ALLOWED: ['clips', 'generation_defaults', 'output', 'pinnedShotGroups', 'theme', 'theme_overrides', 'tracks'],
  _TRACK_ALLOWED: ['blendMode', 'fit', 'id', 'kind', 'label', 'muted', 'opacity', 'scale', 'volume'],
};

const FIXTURES = [
  {
    name: 'golden',
    timelinePath: path.resolve(repoDir, 'examples/hype.timeline.json'),
    assetsPath: path.resolve(repoDir, 'examples/hype.assets.json'),
  },
  {
    name: 'full',
    timelinePath: path.resolve(repoDir, 'examples/hype.timeline.full.json'),
    assetsPath: path.resolve(repoDir, 'examples/hype.assets.full.json'),
  },
];

const fail = (message) => {
  throw new Error(message);
};

const readJson = async (jsonPath) => {
  const raw = await readFile(jsonPath, 'utf8');
  return JSON.parse(raw);
};

const resolveAssets = (assetsPath, registry) => {
  const resolvedAssets = {};
  for (const [assetKey, entry] of Object.entries(registry.assets ?? {})) {
    const resolvedPath = path.isAbsolute(entry.file)
      ? entry.file
      : path.resolve(path.dirname(assetsPath), entry.file);
    resolvedAssets[assetKey] = {
      ...entry,
      file: resolvedPath,
    };
  }

  return {assets: resolvedAssets};
};

const loadFixtureProps = async ({timelinePath, assetsPath}) => {
  const [timeline, registry] = await Promise.all([readJson(timelinePath), readJson(assetsPath)]);
  return {
    timeline,
    assets: resolveAssets(assetsPath, registry),
  };
};

const importGeneratedAllowedArrays = async () => {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), 'remotion-smoke-types-'));
  try {
    const source = await readFile(typesGeneratedPath, 'utf8');
    const transpiled = ts.transpileModule(source, {
      compilerOptions: {
        module: ts.ModuleKind.ES2020,
        target: ts.ScriptTarget.ES2020,
      },
      fileName: 'types.generated.ts',
    }).outputText;
    const tempModulePath = path.join(tempDir, 'types.generated.mjs');
    await writeFile(tempModulePath, transpiled, 'utf8');
    const imported = await import(pathToFileURL(tempModulePath).href);
    return {
      _ASSET_ENTRY_ALLOWED: [...imported._ASSET_ENTRY_ALLOWED],
      _CLIP_ALLOWED: [...imported._CLIP_ALLOWED],
      _THEME_OVERRIDES_ALLOWED: [...imported._THEME_OVERRIDES_ALLOWED],
      _TIMELINE_TOP_ALLOWED: [...imported._TIMELINE_TOP_ALLOWED],
      _TRACK_ALLOWED: [...imported._TRACK_ALLOWED],
    };
  } finally {
    await rm(tempDir, {recursive: true, force: true});
  }
};

const assertAllowedArrays = (actual) => {
  for (const [name, expected] of Object.entries(EXPECTED_ALLOWED)) {
    const actualArray = actual[name];
    if (!Array.isArray(actualArray)) {
      fail(`Generated module did not export ${name} as an array`);
    }
    if (JSON.stringify(actualArray) !== JSON.stringify(expected)) {
      fail(
        `${name} mismatch between src/types.generated.ts and smoke snapshot\nexpected=${JSON.stringify(expected)}\nactual=${JSON.stringify(actualArray)}`,
      );
    }
  }
};

const assertPositiveMetadata = (fixtureName, composition) => {
  if (!composition || composition.id !== compositionId) {
    fail(`Fixture '${fixtureName}' did not resolve composition '${compositionId}'`);
  }

  const metadata = {
    width: composition.width,
    height: composition.height,
    fps: composition.fps,
    durationInFrames: composition.durationInFrames,
  };

  for (const [key, value] of Object.entries(metadata)) {
    if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) {
      fail(`Fixture '${fixtureName}' returned invalid ${key}: ${String(value)}`);
    }
  }
};

const main = async () => {
  const bundleLocation = await bundle({
    entryPoint,
    onProgress: () => undefined,
    webpackOverride: applyWorkspaceEffectsAlias,
  });

  const allowedArrays = await importGeneratedAllowedArrays();
  assertAllowedArrays(allowedArrays);

  for (const fixture of FIXTURES) {
    const inputProps = await loadFixtureProps(fixture);
    const composition = await selectComposition({
      serveUrl: bundleLocation,
      id: compositionId,
      inputProps,
    });
    assertPositiveMetadata(fixture.name, composition);
  }

  console.log(`Smoke bundle OK for ${FIXTURES.map((fixture) => fixture.name).join(', ')} fixtures`);
};

main().catch((error) => {
  const message = error instanceof Error ? error.stack ?? error.message : String(error);
  console.error(`Smoke test failed: ${message}`);
  process.exit(1);
});
