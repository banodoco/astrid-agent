"""Tests of the fail-closed selected-install input preflight."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import m46_selected_install_preflight as subject


def command(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True).stdout.strip()


def commit(root, message):
    command('git', 'add', '.', cwd=root)
    command('git', '-c', 'user.name=Venue Test', '-c', 'user.email=venue@example.invalid',
            'commit', '-qm', message, cwd=root)
    return command('git', 'rev-parse', 'HEAD', cwd=root)


class SelectedInstallPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.roots = {}
        for name in ('app', 'agent', 'runtime', 'pinokio', 'runner'):
            path = self.root / name
            path.mkdir()
            command('git', 'init', '-q', str(path))
            (path / 'seed').write_text(name)
            self.roots[name] = path
        self.refs = {name: commit(self.roots[name], name) for name in ('agent', 'runtime', 'pinokio')}
        manifest = {'agent': {'sha': self.refs['agent']}, 'runtime': {'sha': self.refs['runtime']}}
        app_file = self.roots['app'] / 'public/install-manifest.json'
        app_file.parent.mkdir()
        app_file.write_text(json.dumps(manifest))
        self.refs['app'] = commit(self.roots['app'], 'app')
        runner = self.roots['runner']
        self.base = commit(runner, 'base')
        script = runner / 'scripts/m46_selected_install_preflight.py'
        script.parent.mkdir()
        script.write_text('fixture only\n')
        self.head = commit(runner, 'runner')
        output = self.root / 'output'
        output.mkdir(mode=0o700)
        self.output = output
        self.request = {'schema': subject.SCHEMA, 'runner_head': self.head,
                        'checkouts': {name: str(path) for name, path in self.roots.items()},
                        'mode': 'backend', 'output_directory': str(output)}
        self.hashes = {('app', 'public/install-manifest.json'): hashlib.sha256(app_file.read_bytes()).hexdigest()}
        self.patches = [mock.patch.object(subject, 'REFS', self.refs),
                        mock.patch.object(subject, 'BASE', self.base),
                        mock.patch.object(subject, 'SOURCE_HASHES', self.hashes)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.script = script

    def validate(self):
        return subject.validate_request(self.request, script=self.script)

    def test_exact_clean_inputs_are_accepted_for_preflight(self):
        roots, output, hashes = self.validate()
        self.assertEqual(roots, self.roots)
        self.assertEqual(output, self.output)
        self.assertEqual(hashes['app/public/install-manifest.json'], next(iter(self.hashes.values())))

    def test_unknown_keys_bad_mode_and_duplicate_json_refused(self):
        self.request['extra'] = 1
        with self.assertRaises(subject.Refusal): self.validate()
        del self.request['extra']
        self.request['mode'] = 'other'
        with self.assertRaises(subject.Refusal): self.validate()
        with self.assertRaises(subject.Refusal):
            json.loads('{"schema":1,"schema":2}', object_pairs_hook=subject.pairs_no_duplicates)

    def test_mismatched_head_dirty_ignored_and_nested_refused(self):
        self.request['runner_head'] = '0' * 40
        with self.assertRaises(subject.Refusal): self.validate()
        self.request['runner_head'] = self.head
        (self.roots['agent'] / 'unknown').write_text('untracked')
        with self.assertRaises(subject.Refusal): self.validate()
        (self.roots['agent'] / 'unknown').unlink()
        (self.roots['agent'] / '.git' / 'info' / 'exclude').write_text('ignored\n')
        (self.roots['agent'] / 'ignored').write_text('ignored')
        with self.assertRaises(subject.Refusal): self.validate()
        (self.roots['agent'] / 'ignored').unlink()
        self.request['output_directory'] = str(self.roots['app'])
        with self.assertRaises(subject.Refusal): self.validate()

    def test_source_hash_and_output_permissions_refused(self):
        self.hashes[('app', 'public/install-manifest.json')] = '0' * 64
        with self.assertRaises(subject.Refusal): self.validate()
        self.hashes[('app', 'public/install-manifest.json')] = hashlib.sha256(
            (self.roots['app'] / 'public/install-manifest.json').read_bytes()).hexdigest()
        self.output.chmod(0o755)
        with self.assertRaises(subject.Refusal): self.validate()

    def test_result_is_exclusively_created_blocked_and_never_success(self):
        fake = subprocess.CompletedProcess(['docker'], 0, b'daemon-id\n', b'')
        actual_run = subprocess.run
        def run(args, **kwargs):
            if args[0] == 'docker': return fake
            return actual_run(args, **kwargs)
        validated = self.validate()
        with mock.patch.object(subject, 'validate_request', return_value=validated), \
             mock.patch.object(subject.subprocess, 'run', side_effect=run):
            self.assertEqual(subject.run(self.request), 2)
        result = json.loads((self.output / 'preflight-blocked.json').read_bytes())
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['install'], 'not-run')
        self.assertEqual(result['observer_binding'], 'not-produced')
        self.assertTrue(result['docker_probe']['daemon_available'])
        self.assertEqual((self.output / 'preflight-blocked.json').stat().st_mode & 0o777, 0o600)

    def test_schema_is_closed_and_matches_request_fields(self):
        schema_path = Path(subject.__file__).with_name('m46-selected-install-preflight.schema.json')
        schema = json.loads(schema_path.read_text())
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(set(schema['required']), set(self.request))
        self.assertFalse(schema['properties']['checkouts']['additionalProperties'])
        self.assertEqual(set(schema['properties']['checkouts']['required']), set(self.roots))
        self.assertEqual(schema['properties']['mode']['enum'], ['backend', 'local'])


if __name__ == '__main__':
    unittest.main()
