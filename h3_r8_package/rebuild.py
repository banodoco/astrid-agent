"""Build or verify frozen R8 source content; never authorize a payload execution."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
SNAPSHOTS = frozenset(('driver.py', 'capture.py', 'run.sh', 'install.sh',
                       'release-install-manifest.json'))


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def serialize(value):
    return (json.dumps(value, indent=2) + '\n').encode()


def unique_rows(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate identity/manifest row: ' + key)
        result[key] = value
    return result


def load(path):
    return json.loads(path.read_bytes(), object_pairs_hook=unique_rows)


def selected_environment(work):
    return {'HOME': str(work / 'home'), 'TMPDIR': str(work / 'tmp'),
            'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_TERMINAL_PROMPT': '0',
            'GIT_ASKPASS': '/bin/false', 'PYTHONDONTWRITEBYTECODE': '1'}


def git(repo, env, *args, **kwargs):
    return subprocess.check_output(
        ['git', '-c', 'credential.helper=', '-C', str(repo), *args], env=env, **kwargs)


def contracts(inputs):
    frozen = load(HERE / 'expected-sources.json')
    expected = load(HERE / 'expected-SHA256SUMS.json')
    if set(frozen) != set(load(HERE / 'repositories.json')):
        raise ValueError('missing or extra frozen identity rows')
    for name, row in frozen.items():
        if set(row) != {'sha', 'tree', 'objects', 'pack_sha256', 'pack_bytes'}:
            raise ValueError('wrong identity fields: ' + name)
        if any(not re.fullmatch('[0-9a-f]{40}', row[k]) for k in ('sha', 'tree')):
            raise ValueError('invalid commit/tree identity: ' + name)
        if type(row['objects']) is not int or row['objects'] < 2:
            raise ValueError('invalid object count: ' + name)
    members = {name + '.pack' for name in frozen} | SNAPSHOTS | {'sources.json'}
    if set(expected) != members:
        raise ValueError('missing or extra checksum rows')
    if sha(HERE / 'expected-sources.json') != expected['sources.json']:
        raise ValueError('frozen sources.json snapshot checksum mismatch')
    for name, row in frozen.items():
        if row['pack_sha256'] != expected[name + '.pack']:
            raise ValueError('retained pack contract differs: ' + name)
    if set(p.name for p in inputs.iterdir()) != SNAPSHOTS:
        raise ValueError('missing or extra text snapshots')
    for name in SNAPSHOTS:
        path = inputs / name
        if path.is_symlink() or not path.is_file() or sha(path) != expected[name]:
            raise ValueError('input checksum mismatch: ' + name)
    return frozen, expected


def selected_objects(repo, env, pin, frozen):
    if git(repo, env, 'cat-file', '-t', pin).strip() != b'commit':
        raise ValueError('selected ref is not a commit')
    tree = git(repo, env, 'rev-parse', pin + '^{tree}').decode().strip()
    if tree != frozen['tree']:
        raise ValueError('selected tree differs')
    objects = {pin.encode(), tree.encode()}
    for entry in filter(None, git(repo, env, 'ls-tree', '-r', '-t', '-z', pin).split(b'\0')):
        mode, kind, rest = entry.split(b' ', 2)
        if mode not in {b'040000', b'100644', b'100755'} or kind not in {b'tree', b'blob'}:
            raise ValueError('unsupported source member: ' + repr(entry))
        objects.add(rest.split(b'\t', 1)[0])
    if len(objects) != frozen['objects']:
        raise ValueError('selected object count differs')
    return objects


def verify_pack(pack, store, env, frozen):
    git(store.parent, env, 'init', '--bare', '-q', str(store))
    # Frozen packs select one commit and its whole tree, not ancestor history.
    # Declare that exact shallow boundary before strict connectivity validation.
    (store / 'shallow').write_text(frozen['sha'] + '\n')
    with pack.open('rb') as stream:
        indexed = git(store, env, 'index-pack', '--strict', '--stdin', stdin=stream)
    pack_id = indexed.decode().strip().split('\t')[-1]
    if not re.fullmatch('[0-9a-f]{40}', pack_id):
        raise ValueError('unexpected strict index-pack result')
    rows = git(store, env, 'verify-pack', '-v',
               str(store / 'objects/pack' / ('pack-' + pack_id + '.idx'))).splitlines()
    actual = {}
    for row in rows:
        fields = row.split()
        if fields and re.fullmatch(b'[0-9a-f]{40}', fields[0]):
            if fields[0] in actual:
                raise ValueError('duplicate packed object')
            actual[fields[0]] = fields[1]
    objects = selected_objects(store, env, frozen['sha'], frozen)
    if set(actual) != objects or sum(v == b'commit' for v in actual.values()) != 1:
        raise ValueError('missing or extra packed identity objects')
    ref = 'refs/heads/frozen'
    git(store, env, 'update-ref', ref, frozen['sha'])
    refs = git(store, env, 'for-each-ref', '--format=%(refname) %(objectname)').decode().splitlines()
    if refs != [ref + ' ' + frozen['sha']] or git(store, env, 'rev-parse', ref).decode().strip() != frozen['sha']:
        raise ValueError('selected refs differ')
    return {'sha': frozen['sha'], 'tree': frozen['tree'], 'objects': len(actual),
            'commits': 1, 'trees': sum(v == b'tree' for v in actual.values()),
            'blobs': sum(v == b'blob' for v in actual.values()),
            'selected_refs': refs, 'index_pack_strict': 'passed',
            'pack_sha256': sha(pack), 'pack_bytes': pack.stat().st_size}


def new_work(work):
    if not work.is_absolute() or work.exists() or work.is_symlink():
        raise ValueError('work must be a nonexistent absolute path')
    work.mkdir(parents=True)
    for name in ('home', 'tmp'):
        (work / name).mkdir()
    return selected_environment(work)


def verify_package(output, work, inputs, frozen, expected, env):
    members = set(expected) | {'SHA256SUMS.json'}
    if set(p.name for p in output.iterdir()) != members:
        raise ValueError('missing or extra package members')
    if any(p.is_symlink() or not p.is_file() for p in output.iterdir()):
        raise ValueError('package members must be regular files')
    nonpacks = {name: inputs / name for name in SNAPSHOTS}
    nonpacks.update({'sources.json': HERE / 'expected-sources.json',
                     'SHA256SUMS.json': HERE / 'expected-SHA256SUMS.json'})
    for name, retained in nonpacks.items():
        if (output / name).read_bytes() != retained.read_bytes():
            raise ValueError('non-pack bytes differ: ' + name)
    identities = {name: verify_pack(output / (name + '.pack'), work / name, env, row)
                  for name, row in frozen.items()}
    result = {'status': 'passed', 'acceptance': 'frozen-source-content-only',
              'non_pack_byte_identical': True,
              'retained_manifest_sha256': sha(output / 'SHA256SUMS.json'),
              'non_pack_sha256': {name: sha(output / name) for name in sorted(nonpacks)},
              'identities': identities,
              'pack_byte_mismatches': [name for name in frozen
                  if identities[name]['pack_sha256'] != expected[name + '.pack']],
              'pod_submission': 'machine-1 original retained package only',
              'git_version': subprocess.check_output(['git', '--version'], env=env, text=True).strip()}
    (work / 'comparison.json').write_bytes(serialize(result))
    return result


def rebuild(source_paths, output, work, inputs):
    frozen, expected = contracts(inputs)
    if set(source_paths) != set(frozen):
        raise ValueError('exactly the six frozen sources are required')
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ValueError('output must be a nonexistent absolute path')
    env = new_work(work)
    output.mkdir(parents=True)
    for name, row in frozen.items():
        repo = Path(source_paths[name])
        if not repo.is_absolute() or repo.resolve() != repo or not repo.is_dir():
            raise ValueError('canonical absolute source required: ' + name)
        if git(repo, env, 'rev-parse', 'HEAD').decode().strip() != row['sha']:
            raise ValueError('source HEAD differs: ' + name)
        if git(repo, env, 'status', '--porcelain', '--untracked-files=all'):
            raise ValueError('dirty selected source: ' + name)
        git(repo, env, 'fsck', '--full')
        objects = selected_objects(repo, env, row['sha'], row)
        with (output / (name + '.pack')).open('wb') as stream:
            subprocess.run(['git', '-c', 'credential.helper=', '-C', str(repo),
                            'pack-objects', '--stdout'],
                           input=b'\n'.join(sorted(objects)) + b'\n',
                           env=env, stdout=stream, check=True)
    for name in SNAPSHOTS:
        (output / name).write_bytes((inputs / name).read_bytes())
    (output / 'sources.json').write_bytes((HERE / 'expected-sources.json').read_bytes())
    (output / 'SHA256SUMS.json').write_bytes((HERE / 'expected-SHA256SUMS.json').read_bytes())
    return verify_package(output, work, inputs, frozen, expected, env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--source', action='append', metavar='NAME=ABSOLUTE_GIT_CHECKOUT')
    mode.add_argument('--verify-package', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, default=HERE / 'inputs')
    args = parser.parse_args()
    try:
        if args.verify_package:
            if args.output or not args.verify_package.is_absolute():
                parser.error('--verify-package requires an absolute package and no --output')
            frozen, expected = contracts(args.inputs)
            result = verify_package(args.verify_package, args.work, args.inputs,
                                    frozen, expected, new_work(args.work))
        else:
            if args.output is None:
                parser.error('--source requires --output')
            sources = {}
            for item in args.source:
                name, sep, path = item.partition('=')
                if not sep or not path or name in sources:
                    parser.error('each source must appear exactly once as NAME=PATH')
                sources[name] = path
            result = rebuild(sources, args.output, args.work, args.inputs)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print('package verification failed: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
