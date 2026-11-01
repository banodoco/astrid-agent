const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const ts = require('typescript');

// Load Root.tsx in-process with the same transpile/stub shape as bounds.test.cjs.
// The registered calculateMetadata callbacks remain Root's implementation; only
// React, Remotion and Composition package imports are replaced.
const src = process.env.ROOT_SOURCE_DIR
  ? path.resolve(process.env.ROOT_SOURCE_DIR)
  : path.resolve(__dirname, '..', 'src');

const compileRoot = () => {
  const registered = [];
  const Composition = (props) => {
    registered.push(props);
    return null;
  };
  const modules = {
    'react': {},
    'react/jsx-runtime': {
      Fragment: 'Fragment',
      jsx: (type, props) => ({type, props}),
      jsxs: (type, props) => ({type, props}),
    },
    'remotion': {
      Composition,
      Sequence: () => null,
      useVideoConfig: () => ({width: 1920, height: 1080, fps: 30}),
    },
    '@remotion/media': {Video: () => null},
    '@banodoco/timeline-composition': {
      DEFAULT_THEME: {},
      TimelineComposition: () => null,
      getTimelineDurationInFrames: (timeline) => timeline.__testDurationInFrames ?? 1,
    },
    './ThreeTimelineComposition': {ThreeTimelineComposition: () => null},
    './fonts': {FontProvider: () => null},
    './host-registries': {resolveHostRegistries: () => ({})},
    './ReviewOverlay': {ReviewOverlay: () => null},
    './BoundsProbe': {BoundsProbe: () => null},
    './FrameDiagnostics': {FrameDiagnostics: () => null},
    './asset-source': {resolveInvocationAsset: (file) => file},
  };
  const filename = path.join(src, 'Root.tsx');
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
      jsx: ts.JsxEmit.ReactJSX,
      esModuleInterop: true,
    },
  }).outputText;
  const mod = {exports: {}};
  const requireStub = (name) => {
    if (!(name in modules)) throw new Error(`Root.tsx imported an unstubbed module: ${name}`);
    return modules[name];
  };
  new Function('require', 'module', 'exports', code)(requireStub, mod, mod.exports);

  const walkTree = (node) => {
    if (node == null || typeof node !== 'object') return;
    if (Array.isArray(node)) {
      node.forEach(walkTree);
      return;
    }
    if (node.type === Composition) return node.type(node.props);
    if (typeof node.type === 'function') {
      walkTree(node.type(node.props));
      return;
    }
    walkTree(node.props && node.props.children);
  };
  walkTree(mod.exports.Root());
  assert.equal(registered.length, 2);
  assert.deepEqual(registered.map(({id}) => id), [
    'TimelineComposition',
    'ThreeTimelineComposition',
  ]);
  assert.ok(registered.every(({calculateMetadata}) => typeof calculateMetadata === 'function'));
  return registered;
};

const assets = {assets: {}};
const timelineWithDuration = (duration, extra = {}) => ({
  theme: 'banodoco-default',
  theme_overrides: {},
  clips: [],
  __testDurationInFrames: duration,
  ...extra,
});
const propsFor = (timeline, extra = {}) => ({timeline, assets, ...extra});

const metadataFor = async (props) => {
  const registrations = compileRoot();
  return Promise.all(registrations.map(({calculateMetadata}) => calculateMetadata({props})));
};

const assertSameMetadata = (metadata, expected) => {
  assert.equal(metadata.length, 2);
  metadata.forEach((value) => assert.deepEqual(value, expected));
};

test('Root registers both compositions with actual metadata callbacks', () => {
  const registrations = compileRoot();
  assert.notEqual(registrations[0].calculateMetadata, undefined);
  assert.notEqual(registrations[1].calculateMetadata, undefined);
});

test('native empty and sparse props retain the original 300-frame extent', async () => {
  const cases = [
    propsFor(timelineWithDuration(1), {astridCompositionDurationInFrames: 300}),
    propsFor(timelineWithDuration(31, {clips: [{id: 'early'}]}), {astridCompositionDurationInFrames: 300}),
  ];
  for (const props of cases) {
    assertSameMetadata(await metadataFor(props), {
      width: 1920,
      height: 1080,
      fps: 30,
      durationInFrames: 300,
    });
  }
});

test('without the internal override, empty and ordinary durations keep their existing values', async () => {
  assertSameMetadata(await metadataFor(propsFor(timelineWithDuration(1))), {
    width: 1920,
    height: 1080,
    fps: 30,
    durationInFrames: 1,
  });
  assertSameMetadata(await metadataFor(propsFor(timelineWithDuration(300, {clips: [{id: 'ordinary'}]}))), {
    width: 1920,
    height: 1080,
    fps: 30,
    durationInFrames: 300,
  });
});

test('legacy local-window metadata remains a 30-frame composition', async () => {
  assertSameMetadata(await metadataFor(propsFor(timelineWithDuration(1, {
    metadata: {source_window_start_seconds: 5, duration_seconds: 1},
  }))), {
    width: 1920,
    height: 1080,
    fps: 30,
    durationInFrames: 30,
  });
});

test('default canvas, explicit canvas and explicit render-tail metadata remain unchanged', async () => {
  assertSameMetadata(await metadataFor(propsFor(timelineWithDuration(90, {
    theme_overrides: {visual: {canvas: {width: 1280, height: 720, fps: 24}}},
    clips: [{id: 'ordinary'}],
  }))), {
    width: 1280,
    height: 720,
    fps: 24,
    durationInFrames: 90,
  });
  assertSameMetadata(await metadataFor(propsFor(timelineWithDuration(300, {
    app: {
      astrid_render_clock: {
        authored_duration_frames: 300,
        render_duration_frames: 330,
        tail: {
          policy: 'unmapped_excess_rendered_region',
          source_asset: 'tail',
          start_frame: 300,
          end_frame: 330,
          source_start_frame: 0,
          source_end_frame: 30,
        },
      },
    },
  }))), {
    width: 1920,
    height: 1080,
    fps: 30,
    durationInFrames: 330,
  });
});

test('invalid, noninteger, unsafe and smaller overrides are refused', async () => {
  const invalid = [0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, '300', null];
  for (const value of invalid) {
    await assert.rejects(
      () => metadataFor(propsFor(timelineWithDuration(1), {astridCompositionDurationInFrames: value})),
      `invalid override ${String(value)}`,
    );
  }
  await assert.rejects(
    () => metadataFor(propsFor(timelineWithDuration(300, {clips: [{id: 'ordinary'}]}), {
      astridCompositionDurationInFrames: 299,
    })),
    'override smaller than ordinary clip extent',
  );
});

test('an override conflicting with either legacy clock is refused', async () => {
  await assert.rejects(
    () => metadataFor(propsFor(timelineWithDuration(1, {
      metadata: {source_window_start_seconds: 5, duration_seconds: 1},
    }), {astridCompositionDurationInFrames: 300})),
    'override conflicting with legacy local window',
  );
  await assert.rejects(
    () => metadataFor(propsFor(timelineWithDuration(300, {
      app: {
        astrid_render_clock: {
          authored_duration_frames: 300,
          render_duration_frames: 330,
          tail: {
            policy: 'unmapped_excess_rendered_region',
            source_asset: 'tail',
            start_frame: 300,
            end_frame: 330,
            source_start_frame: 0,
            source_end_frame: 30,
          },
        },
      },
    }), {astridCompositionDurationInFrames: 300})),
    'override conflicting with explicit render clock',
  );
});
