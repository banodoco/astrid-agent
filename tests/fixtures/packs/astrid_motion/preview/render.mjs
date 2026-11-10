// Fast element preview loop for the astrid_motion pack (dev tool, not shipped).
// Bundles this harness once, then renders each scene in scenes.json as PNG stills
// (and mp4 when "mp4": true). Each layer mounts the element component directly,
// so no timeline, registry alias or runtime is needed.
//
//   node preview/render.mjs                 # all scenes, into $TMPDIR/astrid-motion-preview
//   OUT=<dir> ONLY=type,sprite node preview/render.mjs
//   node preview/make_art.py <dir>          # regenerate the test art first if needed
//
// Remotion comes from remotion/node_modules; Node must be 20.19.4 (remotion pins it).
import fs from 'node:fs';
import {createRequire} from 'node:module';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const REM = path.resolve(here, '../../../../remotion');
// This file sits outside the Remotion project, so resolve its packages from there.
const require = createRequire(path.join(REM, 'package.json'));
const {bundle} = require('@remotion/bundler');
const {renderMedia, renderStill, selectComposition} = require('@remotion/renderer');
const OUT = process.env.OUT ?? path.join(os.tmpdir(), 'astrid-motion-preview');
const only = process.env.ONLY ? process.env.ONLY.split(',') : null;
// SCENES=<file.json> renders a scene list kept outside the pack (production scenes).
const scenes = JSON.parse(fs.readFileSync(process.env.SCENES ?? path.join(here, 'scenes.json'), 'utf8'));
fs.mkdirSync(OUT, {recursive: true});

// Public dir for staticFile(): test art under am/ plus the shipped fonts.
const publicDir = path.join(OUT, '.public');
fs.mkdirSync(publicDir, {recursive: true});
const fontsLink = path.join(publicDir, 'fonts');
if (!fs.existsSync(fontsLink)) fs.cpSync(path.join(REM, 'public/fonts'), fontsLink, {recursive: true});
// EXTRA_PUBLIC=name=/abs/dir,... copies folders into the public dir (read-only art).
for (const pair of (process.env.EXTRA_PUBLIC ?? '').split(',').filter(Boolean)) {
  const [name, dir] = pair.split('=');
  const link = path.join(publicDir, name);
  if (!fs.existsSync(link)) fs.cpSync(dir, link, {recursive: true});
}
if (!fs.existsSync(path.join(publicDir, 'am'))) {
  throw new Error(`missing ${publicDir}/am: run python3 preview/make_art.py ${publicDir}/am first`);
}

const t0 = Date.now();
const serveUrl = await bundle({
  entryPoint: path.join(here, 'src/index.ts'),
  publicDir,
  webpackOverride: (config) => ({
    ...config,
    resolve: {...config.resolve, modules: [...(config.resolve?.modules ?? ['node_modules']), path.join(REM, 'node_modules')]},
  }),
});
console.log(`bundled in ${((Date.now() - t0) / 1000).toFixed(1)}s`);

for (const scene of scenes) {
  if (only && !only.includes(scene.name)) continue;
  const inputProps = {layers: scene.layers, assets: scene.assets ?? {}};
  const composition = await selectComposition({serveUrl, id: 'Scene', inputProps});
  const ts = Date.now();
  for (const frame of scene.frames ?? []) {
    const output = path.join(OUT, `${scene.name}-f${String(frame).padStart(3, '0')}.png`);
    await renderStill({composition, serveUrl, output, frame, inputProps, imageFormat: 'png'});
  }
  if (scene.mp4) {
    await renderMedia({composition, serveUrl, codec: 'h264', outputLocation: path.join(OUT, `${scene.name}.mp4`), inputProps});
  }
  console.log(`scene ${scene.name}: ${((Date.now() - ts) / 1000).toFixed(1)}s`);
}
