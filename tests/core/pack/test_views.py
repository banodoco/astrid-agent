"""V3 read-view contracts; all timeline/transport values are disposable fixtures."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from astrid.core.pack import views
from astrid.core.pack.canonical import CanonicalPackValidationError, validate_canonical_pack
from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline._common import TimelineEditError

ROOT = Path(__file__).resolve().parents[3]
IDENTITY = 'timeline.view/fixture_views.story_overview'
SOURCE_IDENTITY = 'timeline.view/story_views.story_overview'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv('ASTRID_PACKS_PATH', raising=False)
    root = tmp_path / 'packs/fixture_views'
    root.mkdir(parents=True)
    descriptor = yaml.safe_load((ROOT / 'astrid/packs/story_views/extensions/timeline.view/story_overview.yaml').read_text())
    descriptor['id'] = 'fixture_views.story_overview'
    (root / 'view.yaml').write_text(yaml.safe_dump(descriptor))
    (root / 'code.py').write_text("raise RuntimeError('discovery executed code')\n")
    (root / 'docs').mkdir()
    (root / 'docs/SKILL.md').write_text('---\nname: sample\ndescription: A disposable view.\n---\n')
    manifest = dict(schema_version=3, id='fixture_views', name='Fixture', version='1.0.0',
        documentation=dict(kind='skill', path='docs/SKILL.md'),
        extensions=dict(timeline=dict(views=['view.yaml'])),
        resources=[dict(path='view.yaml', kind='view'), dict(path='code.py', kind='support')])
    (root / 'pack.yaml').write_text(yaml.safe_dump(manifest))
    discovery = dict(project_root=tmp_path / 'empty', extra_pack_roots=(str(root.parent),))
    view = views.resolve_view(IDENTITY, **discovery)
    return root, view, discovery


def projection(**changes):
    data = dict(project_id='p', timeline_id='t', revision_id='rev-0', content_digest='a' * 64,
                fps=30, ranges=[dict(range=dict(name='OPEN', kind='chapter', **{'from':'c1', 'to':None}),
                                    start_frame=0, end_frame=270)], legacy_chapters=[], findings=[])
    return dict(data, **changes)


def read(view, data=None, **kwargs):
    return views.read_view(view, project='p', timeline='t', revision_id='rev-0',
        host=SimpleNamespace(story_overview=lambda **binding: projection() if data is None else data), **kwargs)


def edit(root, change):
    path = root / 'view.yaml'
    d = yaml.safe_load(path.read_text())
    change(d)
    path.write_text(yaml.safe_dump(d))


def test_discovery_closure_single_source_and_no_execution(setup):
    root, view, kwargs = setup
    entry = validate_canonical_pack(root)
    assert entry.definition.extensions['timeline']['views'] == ('view.yaml',)
    assert view.identity == IDENTITY and view.eligible and view.source_kind == 'extra'
    assert [v.identity for v in views.discover_views(**kwargs)].count(IDENTITY) == 1
    assert view.descriptor['read_model'] == 'story_overview'
    assert view.descriptor['reads'] == ['project', 'timeline', 'revision_id']
    old = view.implementation_digest
    (root / 'code.py').write_text("raise RuntimeError('still never executes')\n")
    assert views.resolve_view(IDENTITY, **kwargs).implementation_digest != old
    with pytest.raises(TimelineEditError, match='changed'):
        read(view)


@pytest.mark.parametrize('change', [
    lambda d: d.update(id='neighbour.story_overview'),
    lambda d: d.update(implementation={'path':'code.py'}),
    lambda d: d.update(reads=['project','timeline']),
    lambda d: d['schema'].update(**{'$ref':'https://example.invalid/schema'}),
])
def test_admission_refuses_wrong_owner_code_undeclared_reads_external_schema(setup, change):
    root, _, _ = setup
    edit(root, change)
    with pytest.raises(CanonicalPackValidationError):
        validate_canonical_pack(root)


@pytest.mark.parametrize('identity', ['story_overview', 'fixture_views.story_overview',
    'timeline.operation/fixture_views.story_overview', 'timeline.view/missing.story_overview'])
def test_point_qualified_identity_required(setup, identity):
    with pytest.raises(views.UnsupportedView):
        views.resolve_view(identity, **setup[2])


@pytest.mark.parametrize('field,value', [('project_id','other'), ('timeline_id','other'),
    ('revision_id','stale'), ('content_digest','bad')])
def test_result_identity_and_digest_refused(setup, field, value):
    with pytest.raises(TimelineEditError):
        read(setup[1], projection(**{field:value}))


def test_generic_text_uses_declared_schema_and_detaches_output(setup):
    data = projection()
    result = read(setup[1], data)
    assert 'OPEN' in views.render_view_text(result) and '270' in views.render_view_text(result)
    result['data']['ranges'].clear()
    assert data['ranges']
    with pytest.raises(views.UnsupportedView):
        views.read_view(setup[1], project='p', timeline='t', revision_id='r', host=None)
    with pytest.raises(views.UnsupportedView):
        views.read_view(setup[1], project='p', timeline='t', revision_id='r', host=SimpleNamespace())


def test_descriptor_mutation_cannot_override_read_model(setup):
    _, view, _ = setup
    view.descriptor['read_model'] = 'other'
    with pytest.raises(TimelineEditError, match='changed'):
        read(view)


def test_inspect_only_env_cannot_execute(setup, monkeypatch, tmp_path):
    root, _, _ = setup
    monkeypatch.setenv('ASTRID_PACKS_PATH', str(root.parent))
    view = views.resolve_view(IDENTITY, project_root=tmp_path / 'empty')
    assert not view.eligible
    with pytest.raises(views.UnsupportedView):
        read(view)


@pytest.mark.parametrize('verb,flag', [('show','--as'), ('visualize','--view')])
def test_both_public_cli_parsers_use_generic_route(setup, monkeypatch, capsys, verb, flag):
    from astrid.packs.timeline import cli_views
    from astrid.packs.timeline.cli import build_parser
    _, view, _ = setup
    calls = []
    def opener(project, timeline, *, revision_id=None):
        calls.append((project,timeline,revision_id))
        return DomainResult.success(dict(project_id='p',timeline_id='t',revision_id='rev-0',
            bundle=dict(project_id='p',timeline_id='t',base_parent=dict(revision_id='rev-0'))))
    client = SimpleNamespace(timelines=SimpleNamespace(open_bundle=opener))
    monkeypatch.setattr(cli_views, 'resolve_view', lambda _: view)
    class Host:
        def __init__(self, _client): pass
        def story_overview(self, **binding):
            assert binding == dict(project='p',timeline='t',revision_id='rev-0')
            bundle = dict(project_id='p',timeline_id='t',base_parent=dict(revision_id='rev-0'))
            digest = hashlib.sha256(json.dumps(bundle,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            return projection(content_digest=digest)
    monkeypatch.setattr(cli_views, 'TimelineReadModels', Host)
    p = build_parser(client).parse_args([verb,'alias','--project','alias-p',flag,IDENTITY,'--json'])
    assert p.handler(p) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['data']['data']['ranges'] == projection()['ranges'] and len(calls) == 1
    p.revision_id = 'different'
    assert p.handler(p) != 0
    assert 'different revision' in capsys.readouterr().out
    client.timelines = SimpleNamespace()
    assert p.handler(p) != 0
    assert 'unsupported' in capsys.readouterr().out


def test_public_listing_has_no_runtime_client_or_code_execution(monkeypatch, capsys):
    from astrid.core.gateway import main
    from astrid.sdk import AstridClient
    monkeypatch.setattr(AstridClient, 'open_from_launcher', lambda *a, **k: pytest.fail('listing opened Runtime'))
    for verb in ('show','visualize'):
        assert main(['timelines',verb,'--list-views','--json']) == 0
        assert SOURCE_IDENTITY in capsys.readouterr().out


def test_actual_producer_exact_revision_ranges_findings_and_zero_writes(setup, monkeypatch):
    """Two exact source closures joined at the data boundary, not Runtime transport.

    A owns integration. Run the actual J host adapter against I's complete
    producer environment, then validate/present its data on this J source.
    """
    import subprocess
    source = os.environ.get('ASTRID_STORY_PRODUCER_CHECKOUT')
    if not source:
        pytest.skip('requires explicitly supplied final STORY producer checkout')
    script = r'''import sys, json, copy, importlib.util, subprocess, hashlib
from pathlib import Path
from types import SimpleNamespace
from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline import journal, store
def git(*args): return subprocess.check_output(['git',*args],text=True).strip()
head=git('rev-parse','HEAD')
assert not git('status','--porcelain'), 'producer tree must be clean'
spec=importlib.util.spec_from_file_location('j_view_host', sys.argv[1])
module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module)
spec=importlib.util.spec_from_file_location('fixture', 'tests/sdk/test_timeline_reflow_golden.py')
fixture=importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
bundle=fixture.film()
bundle['parent']['config']['app']={'astrid':{'v':2,'ranges':[
 {'name':'OPEN','kind':'chapter','from':'c1','to':'c2'},
 {'name':'LIVE','kind':'chapter','from':'c2','to':None},
 {'name':'viral','kind':'beat','from':'"viral"','to':'after "viral"'}]}}
def forbidden(*a, **k): raise AssertionError('view attempted a write or journal')
journal.append=forbidden; store._write_step=forbidden; store.publish_bundle=forbidden
calls=[]
def open_bundle(project,timeline,*,revision_id):
 calls.append([project,timeline,revision_id]); return DomainResult.success({'bundle':bundle})
host=module.TimelineReadModels(SimpleNamespace(timelines=SimpleNamespace(open_bundle=open_bundle)))
before=copy.deepcopy(bundle)
good=host.story_overview(project='p',timeline='t',revision_id='rev-0')
assert bundle==before and calls==[['p','t','rev-0']]
bundle['parent']['config']['app']['astrid']['ranges'] += [
 {'name':'unknown','kind':'beat','from':'"missing word"','to':None}, {'invalid':True}]
before=copy.deepcopy(bundle)
invalid=host.story_overview(project='p',timeline='t',revision_id='rev-0')
assert bundle==before
for key in ('project_id','timeline_id'):
 old=bundle[key];bundle[key]='other'
 try: host.story_overview(project='p',timeline='t',revision_id='rev-0')
 except ValueError: pass
 else: raise AssertionError('identity mismatch accepted')
 bundle[key]=old
bundle['base_parent']['revision_id']='stale'
try: host.story_overview(project='p',timeline='t',revision_id='rev-0')
except ValueError: pass
else: raise AssertionError('stale revision accepted')
assert git('rev-parse','HEAD') == head and not git('status','--porcelain'), 'producer changed during proof'
identity={'head':head,'tree':git('rev-parse','HEAD^{tree}'),'parents':git('show','-s','--format=%P'),
 'branch':git('branch','--show-current'),'common_dir':git('rev-parse','--git-common-dir'),'status':'',
 'modules':{str(Path(m.__file__).resolve()):hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
  for m in tuple(sys.modules.values()) if getattr(m,'__file__',None) and str(m.__file__).endswith('.py')
  and ('/astrid/' in str(m.__file__) or m.__name__ == 'j_view_host')}}
print(json.dumps({'good':good,'invalid':invalid,'producer_identity':identity}))
'''
    completed = subprocess.run([sys.executable,'-B','-c',script,str(ROOT/'astrid/core/pack/views.py')],
        cwd=source, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
    produced = json.loads(completed.stdout)
    result = read(setup[1], produced['good'])
    for declared in views.discover_views():
        assert declared.identity == SOURCE_IDENTITY  # this source admits its first view only
        result = read(declared, produced['good'])
        assert 'OPEN' in views.render_view_text(result)
    assert [(r['start_frame'],r['end_frame']) for r in result['data']['ranges']] == [(0,120),(120,270),(39,57)]
    assert result['data']['findings'] == []
    invalid = read(setup[1], produced['invalid'])
    assert {f['code'] for f in invalid['data']['findings']} >= {'unresolved-anchor','invalid-range'}
    assert invalid['data']['content_digest'] != result['data']['content_digest']
    if os.environ.get('ASTRID_EXT_VIEW_WITNESS'):
        Path(os.environ['ASTRID_EXT_VIEW_WITNESS']).write_text(json.dumps(result,indent=2)+'\n')
        Path(os.environ['ASTRID_EXT_VIEW_WITNESS']).with_name('producer-joined-identity.json').write_text(
            json.dumps(produced['producer_identity'],indent=2)+'\n')
