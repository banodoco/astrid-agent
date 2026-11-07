import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from astrid.pack_build import build_pack_assets

ROOT = Path(__file__).resolve().parents[2]
NODE = Path(os.environ.get('ASTRID_BUILD_TEST_NODE', '/opt/node22/bin/node'))
ESBUILD = Path(os.environ['ASTRID_BUILD_TEST_ESBUILD'])
SINGLETONS = ('react', 'react/jsx-runtime', 'react/jsx-dev-runtime', 'react-dom/client', 'remotion', '@astrid/editor-sdk')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def toolchain():
    return {'node_path': str(NODE), 'node_version': os.environ.get('ASTRID_BUILD_TEST_NODE_VERSION', '22.22.0'), 'node_sha256': sha(NODE.read_bytes()),
            'esbuild_path': str(ESBUILD), 'esbuild_version': '0.27.7', 'esbuild_sha256': sha(ESBUILD.read_bytes()),
            'lock_sha256': sha(b'test-only lock'), 'shared_modules': {
                s: {'url': '/assets/' + str(i) + '.js', 'sha256': sha(s.encode()), 'version': '1.0.0'}
                for i, s in enumerate(SINGLETONS)}}


class PackBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.root = Path(self.temp.name).resolve()
        # This is a real rendering pack effect and its actual shared dependency.
        pack = ROOT / 'astrid/packs/rendering'
        for name in ('rendering/elements/effects/audio-reactive-colour/component.tsx', 'shared/element_contracts.ts'):
            dest = self.root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pack / name, dest)
        self.manifest = {'id': 'example.effect', 'version': '1.0.0', 'label': 'Example', 'apiVersion': 1, 'contributions': []}
        (self.root / 'entry.tsx').write_text('''import {defineExtension} from '@astrid/editor-sdk';
const manifest = %s;
export default defineExtension({manifest, activate(ctx) {
  return ctx.effects.register('example', () => import('./rendering/elements/effects/audio-reactive-colour/component'));
}});
''' % json.dumps(self.manifest))
        self.request = {'version': 1, 'transaction_id': 'test', 'pack_id': 'rendering', 'stage_root': str(self.root),
                        'source_digest': self.source_digest(), 'entry': 'entry.tsx',
                        'extension_manifest': self.manifest, 'toolchain': toolchain()}

    def source_digest(self):
        # C3 v1, for this fixture's regular files and no excluded payloads.
        files = sorted((p for p in self.root.rglob('*') if p.is_file() and '.build' not in p.relative_to(self.root).parts), key=lambda p: p.relative_to(self.root).as_posix().encode())
        return sha(b''.join(p.relative_to(self.root).as_posix().encode() + b'\0' + sha(p.read_bytes()).encode() + b'\n' for p in files))

    def tearDown(self):
        self.temp.cleanup()

    def build(self):
        return build_pack_assets(self.request)

    def test_real_pack_reproducible_and_graph_bound(self):
        first = self.build()
        self.assertTrue(first['ok'], first)
        manifest = first['artifact_manifest']
        before = {x['path']: (self.root / x['disk']).read_bytes() for x in manifest['files']}
        self.assertGreater(len(before), 1)
        self.assertEqual(manifest['digest'], sha(before['index.js']))
        self.assertIn(manifest['graphIndexDigest'].encode(), before['index.js'])
        for item in manifest['files']:
            self.assertEqual(item['sha256'], sha(before[item['path']]))
            self.assertEqual(item['size'], len(before[item['path']]))
        original_entry = (self.root / 'entry.tsx').read_text()
        (self.root / 'entry.tsx').write_text('export default <broken')
        self.assertEqual(self.build()['code'], 'compile_failed')
        self.assertEqual(before, {x['path']: (self.root / x['disk']).read_bytes() for x in manifest['files']})
        (self.root / 'entry.tsx').write_text(original_entry)
        second = self.build()
        self.assertEqual(first, second)
        self.assertEqual(before, {x['path']: (self.root / x['disk']).read_bytes() for x in second['artifact_manifest']['files']})
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as other:
            relocated = Path(other) / 'pack'
            shutil.copytree(self.root, relocated, ignore=shutil.ignore_patterns('.build'))
            request = dict(self.request, stage_root=str(relocated))
            self.assertEqual(first, build_pack_assets(request))
        component = self.root / 'rendering/elements/effects/audio-reactive-colour/component.tsx'
        component.write_text(component.read_text().replace('#000000', '#123456'))
        self.request['source_digest'] = self.source_digest()
        changed = self.build()
        self.assertTrue(changed['ok'], changed)
        self.assertNotEqual(manifest['digest'], changed['artifact_manifest']['digest'])
        self.assertNotEqual(manifest['graphIndexDigest'], changed['artifact_manifest']['graphIndexDigest'])

    def test_bad_source_and_unsafe_imports_fail_closed(self):
        entry = self.root / 'entry.tsx'
        for text, code in [('export default <broken', 'compile_failed'),
                           ("import x from 'unknown-module'; export default x;", 'unsafe_import'),
                           ("const x = './unknown'; export default import(x);", 'unsafe_import'),
                           ("const x = 'name'; export default import(`./${x}.ts`);", 'unsafe_import'),
                           ("const x = 'name'; export default import('./' + x);", 'unsafe_import'),
                           ("import x from 'https://example.test/x.js'; export default x;", 'unsafe_import'),
                           ("import x from '../outside'; export default x;", 'unsafe_import')]:
            with self.subTest(text=text):
                entry.write_text(text)
                result = self.build()
                self.assertFalse(result['ok'], result)
                self.assertEqual(result['code'], code, result)
                self.assertEqual(result['diagnostics'], [])

    def test_lock_and_request_refusals(self):
        self.request['toolchain']['node_sha256'] = '0' * 64
        self.assertEqual(self.build()['code'], 'tool_mismatch')
        self.request['toolchain'] = toolchain()
        del self.request['toolchain']['shared_modules']['react']
        self.assertEqual(self.build()['code'], 'unsafe_import')
        self.request['toolchain'] = toolchain()
        self.request['entry'] = '../entry.tsx'
        self.assertEqual(self.build()['code'], 'invalid_request')
        self.request['entry'] = None
        self.request['extension_manifest'] = None
        self.assertIsNone(self.build()['artifact_manifest'])

    def test_css_assets_are_enumerated_and_relative(self):
        (self.root / 'style.css').write_text(".icon { background: url(icon.png); }")
        (self.root / 'icon.png').write_bytes(b'fake image bytes')
        (self.root / 'entry.tsx').write_text("import './style.css'; import {defineExtension} from '@astrid/editor-sdk'; export default defineExtension({manifest: {}}); ")
        result = self.build()
        self.assertTrue(result['ok'], result)
        files = result['artifact_manifest']['files']
        self.assertTrue(any(f['path'].endswith('.css') for f in files))
        self.assertTrue(any(f['path'].endswith('.png') for f in files))
        css = next((self.root / f['disk']).read_text() for f in files if f['path'].endswith('.css'))
        self.assertIn('./assets/', css)
        (self.root / 'entry.tsx').write_text("export const named = 1;")
        self.assertEqual(self.build()['code'], 'invalid_request')
        (self.root / 'entry.tsx').write_text("export default {};")
        self.assertEqual(self.build()['code'], 'invalid_request')

    def test_unicode_case_collision_is_refused(self):
        (self.root / 'style.css').write_text('.a{background:url(Straße.png)}.b{background:url(STRASSE.png)}')
        (self.root / 'Straße.png').write_bytes(b'x')
        (self.root / 'STRASSE.png').write_bytes(b'x')
        (self.root / 'entry.tsx').write_text("import './style.css'; import {defineExtension} from '@astrid/editor-sdk'; export default defineExtension({manifest:{}});")
        self.assertEqual(self.build()['code'], 'unsafe_output')

    def test_symlink_import_is_refused(self):
        other = self.root / 'outside.ts'
        other.write_text('export default 1')
        (self.root / 'link.ts').symlink_to(other)
        (self.root / 'entry.tsx').write_text("import value from './link'; export default value;")
        self.assertEqual(self.build()['code'], 'unsafe_import')


if __name__ == '__main__':
    unittest.main()
