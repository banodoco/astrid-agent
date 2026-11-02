"""Real Git/CLI composition; synthetic frozen sources, no product execution."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOL = Path(__file__).resolve().parents[1] / 'h3_r8_package'
NAMES = tuple(json.loads((TOOL / 'repositories.json').read_text()))


def encoded(value):
    return (json.dumps(value, indent=2) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tool = self.root / 'tool'
        shutil.copytree(TOOL, self.tool)
        self.env = {'HOME': str(self.root), 'PATH': '/usr/local/bin:/usr/bin:/bin',
                    'LANG': 'C.UTF-8', 'GIT_CONFIG_NOSYSTEM': '1',
                    'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
                    'GIT_NO_REPLACE_OBJECTS': '1'}
        self.repo = self.root / 'source'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / 'file').write_text('parent')
        self.commit()
        (self.repo / 'file').write_text('frozen source\n' * 1000)
        (self.repo / 'dir').mkdir()
        (self.repo / 'dir/other').write_text('selected nested member')
        self.commit()
        self.pin = self.git('rev-parse', 'HEAD').strip().decode()
        self.tree = self.git('rev-parse', 'HEAD^{tree}').strip().decode()
        self.objects = {self.pin.encode(), self.tree.encode()}
        for entry in self.git('ls-tree', '-r', '-t', '-z', self.pin).split(b'\0'):
            if entry:
                self.objects.add(entry.split(b' ', 2)[2].split(b'\t')[0])
        # Historical fixture bytes deliberately differ from the builder's encoding.
        self.historical = self.pack('--compression=0')
        self.frozen = {name: {'sha': self.pin, 'tree': self.tree, 'objects': len(self.objects),
                     'pack_sha256': digest(self.historical), 'pack_bytes': len(self.historical)}
                       for name in NAMES}
        self.write_contracts()
        self.output = self.root / 'package'
        self.work_index = 0

    def git(self, *args, **kwargs):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], env=self.env, **kwargs)

    def commit(self):
        self.git('add', '.')
        self.git('-c', 'user.name=CPU fixture', '-c', 'user.email=cpu@example.invalid',
                 'commit', '-qm', 'fixture [skip ci]')

    def pack(self, *args, objects=None):
        return self.git('pack-objects', '--stdout', *args,
                        input=b'\n'.join(sorted(self.objects if objects is None else objects)) + b'\n')

    def write_contracts(self):
        sources = encoded(self.frozen)
        (self.tool / 'expected-sources.json').write_bytes(sources)
        expected = {name + '.pack': digest(self.historical) for name in NAMES}
        expected.update({p.name: digest(p.read_bytes()) for p in (self.tool / 'inputs').iterdir()})
        expected['sources.json'] = digest(sources)
        (self.tool / 'expected-SHA256SUMS.json').write_bytes(encoded(expected))

    def cli(self, *args, verify=False):
        self.work_index += 1
        cmd = [sys.executable, '-I', '-B', str(self.tool / 'rebuild.py'),
               '--work', str(self.root / ('work-' + str(self.work_index)))]
        if verify:
            cmd += ['--verify-package', str(self.output)]
        else:
            cmd += ['--output', str(self.output)]
            for name in NAMES:
                cmd += ['--source', name + '=' + str(self.repo)]
        return subprocess.run([*cmd, *args], env=self.env, capture_output=True, text=True)

    def build(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def reject(self, result, message):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)

    def test_full_build_accepts_content_with_different_pack_bytes(self):
        result = self.build()
        self.assertEqual(result['pack_byte_mismatches'], list(NAMES))
        self.assertTrue(result['non_pack_byte_identical'])
        self.assertEqual(set(result['identities']), set(NAMES))
        for row in result['identities'].values():
            self.assertEqual((row['sha'], row['tree'], row['objects']),
                             (self.pin, self.tree, len(self.objects)))
            self.assertEqual(row['index_pack_strict'], 'passed')
            self.assertEqual(row['selected_refs'], ['refs/heads/frozen ' + self.pin])
        for name in ('sources.json', 'SHA256SUMS.json'):
            contract = 'expected-' + name
            self.assertEqual((self.output / name).read_bytes(), (self.tool / contract).read_bytes())
        self.assertEqual(self.cli(verify=True).returncode, 0)

    def test_strict_pack_rejects_corruption(self):
        self.build()
        path = self.output / 'agent.pack'
        path.write_bytes(path.read_bytes()[:-8])
        self.reject(self.cli(verify=True), 'index-pack')

    def test_strict_pack_rejects_invalid_tree_even_with_valid_pack_checksum(self):
        self.build()
        invalid = self.git('hash-object', '--literally', '-w', '-t', 'tree', '--stdin',
                           input=b'invalid tree').strip()
        (self.output / 'agent.pack').write_bytes(self.pack(objects=self.objects | {invalid}))
        self.reject(self.cli(verify=True), 'index-pack')

    def test_pack_rejects_extra_objects(self):
        self.build()
        extra = self.git('hash-object', '-w', '--stdin', input=b'unselected object').strip()
        (self.output / 'agent.pack').write_bytes(self.pack(objects=self.objects | {extra}))
        self.reject(self.cli(verify=True), 'missing or extra packed identity objects')

    def test_pack_rejects_missing_objects(self):
        self.build()
        blob = self.git('rev-parse', 'HEAD:file').strip()
        (self.output / 'agent.pack').write_bytes(self.pack(objects=self.objects - {blob}))
        self.reject(self.cli(verify=True), 'index-pack')

    def test_pack_rejects_wrong_selected_commit(self):
        self.build()
        # A valid pack with the correct tree/count must still select the frozen commit.
        wrong = self.git('-c', 'user.name=CPU fixture', '-c',
                         'user.email=cpu@example.invalid', 'commit-tree', self.tree,
                         input=b'wrong selected commit\n').strip()
        objects = (self.objects - {self.pin.encode()}) | {wrong}
        (self.output / 'agent.pack').write_bytes(self.pack(objects=objects))
        self.reject(self.cli(verify=True), 'cat-file')

    def test_source_rejects_wrong_head(self):
        (self.repo / 'file').write_text('wrong HEAD')
        self.commit()
        self.reject(self.cli(), 'source HEAD differs')

    def test_wrong_tree_identity(self):
        self.frozen['agent']['tree'] = '0' * 40
        self.write_contracts()
        self.reject(self.cli(), 'selected tree differs')

    def test_wrong_count_identity(self):
        self.frozen['agent']['objects'] += 1
        self.write_contracts()
        self.reject(self.cli(), 'selected object count differs')

    def test_missing_identity_row(self):
        del self.frozen['agent']
        self.write_contracts()
        self.reject(self.cli(), 'missing or extra frozen identity rows')

    def test_extra_identity_row(self):
        self.frozen['extra'] = self.frozen['agent']
        self.write_contracts()
        self.reject(self.cli(), 'missing or extra frozen identity rows')

    def test_duplicate_identity_row(self):
        path = self.tool / 'expected-sources.json'
        path.write_text('{"agent": {}, "agent": {}}')
        self.reject(self.cli(), 'duplicate identity/manifest row')

    def test_wrong_identity_fields(self):
        del self.frozen['agent']['tree']
        self.write_contracts()
        self.reject(self.cli(), 'wrong identity fields')

    def test_snapshot_changed(self):
        (self.tool / 'inputs/driver.py').write_text('changed snapshot')
        self.reject(self.cli(), 'input checksum mismatch: driver.py')
        self.assertFalse(self.output.exists())

    def test_snapshot_missing(self):
        (self.tool / 'inputs/capture.py').unlink()
        self.reject(self.cli(), 'missing or extra text snapshots')

    def test_snapshot_extra(self):
        (self.tool / 'inputs/extra').write_text('extra')
        self.reject(self.cli(), 'missing or extra text snapshots')

    def test_nonpack_bytes_reject_all_drift_including_whitespace(self):
        self.build()
        for name in ('driver.py', 'capture.py', 'run.sh', 'install.sh',
                     'release-install-manifest.json', 'sources.json', 'SHA256SUMS.json'):
            with self.subTest(name=name):
                path = self.output / name
                original = path.read_bytes()
                path.write_bytes(original + b'\n')
                self.reject(self.cli(verify=True), 'non-pack bytes differ: ' + name)
                path.write_bytes(original)

    def test_reject_missing_and_extra_package_members(self):
        self.build()
        path = self.output / 'extra'
        path.write_text('extra')
        self.reject(self.cli(verify=True), 'missing or extra package members')
        path.unlink()
        (self.output / 'driver.py').unlink()
        self.reject(self.cli(verify=True), 'missing or extra package members')

    def test_reject_duplicate_source_cli_and_reused_output(self):
        self.reject(self.cli('--source', 'agent=' + str(self.repo)), 'each source must appear exactly once')
        self.build()
        before = (self.output / 'SHA256SUMS.json').read_bytes()
        self.reject(self.cli(), 'output must be a nonexistent absolute path')
        self.assertEqual(before, (self.output / 'SHA256SUMS.json').read_bytes())

    def test_frozen_sources_metadata_cannot_be_regenerated(self):
        path = self.tool / 'expected-sources.json'
        path.write_bytes(path.read_bytes() + b'\n')
        self.reject(self.cli(), 'frozen sources.json snapshot checksum mismatch')


if __name__ == '__main__':
    unittest.main()
